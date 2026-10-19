"""生成内置模型目录的两层快照（ADR-0001 已定的那两层）。

描述层  = llmfit 自带的 HF 目录里我们认的那几个字段，按 commit + sha256 钉死后内置，联网只刷新。
默认档 = 我们自己按"repo + 文件名 + sha256 + 多候选源"钉住的那几条（出处是 pins/models.json，
         不在这里重打数字 —— 抄一遍就会漂一次）。

为什么描述层必须钉 commit+sha256：上游那张表 91% 的 `_revalidated` 是空的，
它自己都不保证复核过；我们拿它当"出厂内置"的那份，就必须知道是哪一刻的哪一版。

    python tools/catalog_snapshot.py --live                 # 取上游、校验、写 catalog/*.json
    python tools/catalog_snapshot.py --fixtures tests/fixtures
    python tools/catalog_snapshot.py --check                # 只验快照还钉得住

硬规矩：
· 上游文件哈希与 `UPSTREAM["sha256"]` 不符 → 不写盘（这是"上游改了快照"而不是"我算错了"，要显式重钉）；
· 每条保留项都必须有 name/provider，license 为空要归一成 `unknown` 而不是删掉字段 ——
  实测上游 17,700 条里 3,784 条无许可、1,327 条 cc-by-nc-4.0，UI 要能把许可显示出来才谈得上合规。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
# describe.json 本身 .gitignore（5.5 MB 派生物，坐标可复现），但它的内容哈希要提交 ——
# 否则"这张目录长什么样"只存在于某台机器的盘上，slim() 改了也没人能发现。
SIDECAR = os.path.join(ROOT, "catalog", "describe.sha256")

# 上游快照的坐标（2026-10-19 实测：按 commit 重抓与 HEAD 抓的逐字节相同）
UPSTREAM = {
    "repo": "AlexsJones/llmfit",
    "commit": "fbde9aaf8de3abbc7cc0eb3bb7ea4f705227bb7a",
    "path": "llmfit-core/data/hf_models.json",
    "sha256": "0d72c2eee81b0e3671c4a1ba374e987afb57d7eaa7075a9a4488c3ab0e3f4e83",
    "size": 15922575,
    "url": ("https://raw.githubusercontent.com/AlexsJones/llmfit/"
            "fbde9aaf8de3abbc7cc0eb3bb7ea4f705227bb7a/llmfit-core/data/hf_models.json"),
}

# 只带我们外壳真会用的字段。上游有 38 个字段，全量搬进来等于把别人的 schema 变更
# 变成我们的破坏性变更，而且大部分（head_dim、moe_intermediate_size…）是它自己拟合用的。
KEEP = ("name", "provider", "parameter_count", "quantization", "format",
        "min_ram_gb", "recommended_ram_gb", "min_vram_gb", "context_length",
        "use_case", "capabilities", "pipeline_tag", "license", "languages",
        "hf_downloads", "gguf_sources")


def slim(entry: dict) -> dict | None:
    """返回我们那一层的记录；不合格返回 None（调用方必须计数，不许静默丢）。"""
    if entry.get("format") != "gguf":
        return None
    name = entry.get("name")
    if not name or not entry.get("provider"):
        return None
    out = {k: entry.get(k) for k in KEEP if k in entry}
    out["name"] = name
    out["provider"] = entry["provider"]
    # 许可字段不许消失：上游为空就明写 unknown，UI 才有东西可显示
    out["license"] = entry.get("license") or "unknown"
    out["has_sources"] = bool(entry.get("gguf_sources"))
    return out


def build(raw: bytes, expect_sha: str) -> tuple[dict, dict, list[str]]:
    bad: list[str] = []
    sha = hashlib.sha256(raw).hexdigest()
    if sha != expect_sha:
        bad.append(f"上游快照哈希变了：钉的是 {expect_sha[:16]}…，现在是 {sha[:16]}… → 要显式重钉")
    try:
        entries = json.loads(raw.decode("utf-8"))
    except Exception as e:
        return {}, {}, [f"上游快照不是 JSON：{e}"]
    if not isinstance(entries, list) or not entries:
        return {}, {}, [f"上游快照不是非空列表（拿到 {type(entries).__name__}）"]

    kept, dropped = [], {"not_gguf": 0, "unnamed": 0}
    for e in entries:
        s = slim(e)
        if s is None:
            if e.get("format") != "gguf":
                dropped["not_gguf"] += 1
            else:
                dropped["unnamed"] += 1
            continue
        kept.append(s)
    if not kept:
        bad.append("一条都没留下：字段规则过期了，不是上游没数据")
    if dropped["unnamed"]:
        bad.append(f"有 {dropped['unnamed']} 条 format=gguf 却没有 name/provider，不能进目录")

    stats = {
        "上游条目": len(entries), "留下": len(kept),
        "丢弃-非gguf": dropped["not_gguf"], "丢弃-缺name或provider": dropped["unnamed"],
        "有可下载仓库的留下条目": sum(1 for k in kept if k["has_sources"]),
        "许可 unknown 的留下条目": sum(1 for k in kept if k["license"] == "unknown"),
        "许可含 non-commercial 的留下条目": sum(1 for k in kept if "nc" in str(k["license"])),
    }
    snap = {
        "schema": 1,
        "source": {"repo": UPSTREAM["repo"], "path": UPSTREAM["path"],
                   "commit": UPSTREAM["commit"], "sha256": sha, "size": len(raw)},
        "stats": stats,
        "models": sorted(kept, key=lambda x: (x["provider"], x["name"])),
    }
    return snap, stats, bad


def picks_of(defaults: dict) -> dict:
    """`samples/` 那些是车道的已知答案样本，不是给用户装的模型，混进来目录就多了个假条目。"""
    return {k: v for k, v in defaults.items() if not k.startswith("samples/")}


def snapshot_bytes(snap) -> bytes:
    """快照的唯一序列化路径。侧表钉的就是这串字节，所以"写盘"和"复现校验"必须走同一个函数 ——
    分两处写迟早一份加个 key、一份没加，然后侧表永远对不上，或者更糟：永远对得上但测的是别的形状。"""
    return json.dumps(snap, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sidecar_line(data: bytes) -> str:
    return f"{hashlib.sha256(data).hexdigest()}  {len(data):,}  catalog/describe.json\n"


def parse_sidecar(text: str) -> tuple[str, int]:
    """返回 (哈希, 字节数)。格式是 `<64hex>␣␣<带千分位的体积>␣␣<路径>`，只认一行。"""
    lines = [l for l in text.splitlines() if l.strip()]
    if len(lines) != 1:
        raise ValueError(f"侧表应该只有一行，现在 {len(lines)} 行")
    h, size, _path = lines[0].split()
    if not re.fullmatch(r"[0-9a-f]{64}", h):
        raise ValueError(f"侧表哈希不是 64 位十六进制：{h!r}")
    return h, int(size.replace(",", ""))


def emit(path: str, obj) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = snapshot_bytes(obj)
    with open(path, "wb") as fh:
        fh.write(data)
    return len(data)


def load_bytes(args) -> tuple[bytes, str, str]:
    """返回 (字节, 来源描述, 应当匹配哈希).
    夹具带自己的 meta，因为钉死的哈希属于上游整文件；拿切片去比整文件的哈希只会一直红。"""
    if args.fixtures:
        p = os.path.join(args.fixtures, "catalog_upstream.json")
        with open(p, "rb") as fh:
            raw = fh.read()
        with open(os.path.join(args.fixtures, "catalog_upstream.meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        return raw, f"夹具 {p}", meta["sha256"]
    if args.raw:
        # 离线复现用的入口：上游那张表 15.9 MB，CI 里直连很快，但在别的出口上未必要重下一遍。
        # 仍然要求它等于钉住的那颗 —— 不然"用本地文件复现"就成了"随便拿个文件当快照"。
        with open(args.raw, "rb") as fh:
            return fh.read(), f"本地文件 {args.raw}", UPSTREAM["sha256"]
    import urllib.request
    req = urllib.request.Request(UPSTREAM["url"], headers={"User-Agent": "catalog-snapshot"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read(), "上游 raw.githubusercontent", UPSTREAM["sha256"]


def load_pin_layers() -> dict:
    """默认档 = 权重层 (pins/models.json) + OCR 层 (pins/ocr.json)，都存在就合。
    OCR 的权重来自 GitHub release 而不是 HF，所以是另一个文件、另一条来源，
    但对用户而言都是"出厂默认档"，分成两个 artifact 只会让外壳多写一遍分支。"""
    out = {}
    for name in ("models.json", "ocr.json"):
        p = os.path.join(ROOT, "pins", name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                out.update(json.load(fh))
    return out


def check_sidecar(data: bytes) -> int:
    """盘上的 catalog/describe.sha256 是唯一被提交的"这张目录应该长什么样"的记录。
    它把两份东西绑在一起：钉住的上游整文件 + slim() 现在的形状。任一边改了（比如给条目多
    抄一个字段），这里就红，而不是等某个下游拿着 5.5 MB 的 bulk 才发现对不上。"""
    if not os.path.exists(SIDECAR):
        print(f"BAD：缺侧表 {SIDECAR}（快照本身不进仓库，可复现性只靠这颗哈希）")
        return 1
    with open(SIDECAR, encoding="utf-8") as fh:
        try:
            want, want_size = parse_sidecar(fh.read())
        except ValueError as e:
            print(f"BAD：侧表读不动：{e}")
            return 1
    got, got_size = hashlib.sha256(data).hexdigest(), len(data)
    if (got, got_size) == (want, want_size):
        print(f"  ✅ 快照可复现：{got_size:,} B {got[:12]}… 与侧表一致")
        return 0
    print(f"BAD：重导出的快照与侧表不符 —— 侧表 {want_size:,} B {want[:12]}…，"
          f"现在 {got_size:,} B {got[:12]}…")
    print("     要么 slim()/UPSTREAM 真的变了（那就显式重钉并重生成侧表），"
          "要么上游漂了而 --live 的哈希校验被绕过了。不许改成 warning 放行。")
    return 1


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--live", action="store_true")
    g.add_argument("--fixtures", default="")
    g.add_argument("--raw", default="", help="用本地已下载的上游整文件（仍按钉住哈希校验）")
    ap.add_argument("--check", action="store_true", help="只校验，不写 catalog/*.json")
    args = ap.parse_args(argv)
    if not args.live:
        # 夹具是上游文件的一个切片，拿它写盘会产出一份"source 声称是整文件那版"的假目录。
        # 离线只允许 --check（验规则还认得上游形状），生成必须走 --live。
        if not args.check and not args.raw:
            print("--fixtures 只能配 --check 用；生成快照要 --live")
            return 2

    raw, src, expect_sha = load_bytes(args)
    defaults = load_pin_layers()
    snap, stats, bad = build(raw, expect_sha)
    print(f"来源：{src}  字节 {len(raw):,}")
    for k, v in stats.items():
        print(f"  {k} = {v}")
    for b in bad:
        print("  ✗", b)

    if args.check:
        if bad:
            return 1
        # 侧表只有走整文件（--live / --raw）时才可比：夹具是切片，切片算出来的哈希天然不等于侧表。
        if args.fixtures:
            return 0
        return check_sidecar(snapshot_bytes(snap))

    if bad:
        print("有问题 → 不写快照（半张目录比没有目录更危险）")
        return 1

    dp = os.path.join(ROOT, "catalog", "describe.json")
    n = emit(dp, snap)
    with open(SIDECAR, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(sidecar_line(snapshot_bytes(snap)))
    print(f"  写入 {SIDECAR}（describe.json 不进仓库，靠这颗哈希复现）")
    gz = len(gzip.compress(open(dp, "rb").read(), 9))
    print(f"  写入 {dp}：{n:,} B，gzip-9 后 {gz:,} B（{100*gz/n:.1f}%）")
    # 默认档不重打数字：直接从 pins/models.json 抄记录本体，出处跟着走。
    picks = picks_of(defaults)
    dpp = os.path.join(ROOT, "catalog", "defaults.json")
    m = emit(dpp, {"schema": 1, "from": ["pins/models.json", "pins/ocr.json"],
                   # 一起带出"这颗 pin 的字节被谁验过"，UI 才谈得出这句强度；
                   # 只给哈希不给强度，等于把六级证据压成一句"已校验"。
                   "entries": {k: {f: v[f] for f in ("sha256", "size", "primary", "provenance",
                                                   "bytes_verified")
                                   if f in v} for k, v in picks.items()}})
    print(f"  写入 {dpp}：{m:,} B，默认档 {len(picks)} 条（哈希与出处都来自 pins/models.json，"
          f"排除 samples/ 车道样本 {len(defaults) - len(picks)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
