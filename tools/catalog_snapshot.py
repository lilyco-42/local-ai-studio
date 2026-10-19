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
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

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


def emit(path: str, obj) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return len(text.encode("utf-8"))


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
    import urllib.request
    req = urllib.request.Request(UPSTREAM["url"], headers={"User-Agent": "catalog-snapshot"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read(), "上游 raw.githubusercontent", UPSTREAM["sha256"]


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--live", action="store_true")
    g.add_argument("--fixtures", default="")
    ap.add_argument("--check", action="store_true", help="只校验，不写 catalog/*.json")
    args = ap.parse_args(argv)

    raw, src, expect_sha = load_bytes(args)
    with open(os.path.join(ROOT, "pins", "models.json"), encoding="utf-8") as fh:
        defaults = json.load(fh)
    snap, stats, bad = build(raw, expect_sha)
    print(f"来源：{src}  字节 {len(raw):,}")
    for k, v in stats.items():
        print(f"  {k} = {v}")
    for b in bad:
        print("  ✗", b)

    if args.check:
        return 1 if bad else 0
    if bad:
        print("有问题 → 不写快照（半张目录比没有目录更危险）")
        return 1

    dp = os.path.join(ROOT, "catalog", "describe.json")
    n = emit(dp, snap)
    gz = len(gzip.compress(open(dp, "rb").read(), 9))
    print(f"  写入 {dp}：{n:,} B，gzip-9 后 {gz:,} B（{100*gz/n:.1f}%）")
    # 默认档不重打数字：直接从 pins/models.json 抄记录本体，出处跟着走。
    picks = picks_of(defaults)
    dpp = os.path.join(ROOT, "catalog", "defaults.json")
    m = emit(dpp, {"schema": 1, "from": "pins/models.json",
                   "entries": {k: {f: v[f] for f in ("sha256", "size", "primary", "provenance")
                                   if f in v} for k, v in picks.items()}})
    print(f"  写入 {dpp}：{m:,} B，默认档 {len(picks)} 条（哈希与出处都来自 pins/models.json，"
          f"排除 samples/ 车道样本 {len(defaults) - len(picks)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
