"""装机负载计划：普通人的电脑上"这个应用装完要带哪些文件、多大"，按 OS 默认档量出来。

为什么现在就要这张表：硬约束是"只能靠 GitHub Actions 出包"，而打包需要知道
① 每个 OS 该取哪个产物（llama 的 win CPU 包与 ubuntu 的 base 包**格式都不一样**：zip vs tar.gz），
② 包里哪些是运行时闭包（`.so`/`.dll`/`*-impl.dll` 成对）、哪些是开发期垃圾（`test-*.exe`），
③ 首装要下多少字节、占多少盘。这三件事 ADR 只有定性结论，没有可对账的数字。

`--artifacts` 完全离线（读 discover 的输入夹具），只给产物级 size/digest；
`--live` 额外用 HTTP Range 读 zip 中央目录拿逐文件大小（不解压、不下载整包、不运行任何东西）。
tar.gz 没有中央目录可读（格式决定），所以那条路改成**取到临时文件只读 tar 成员表**：
顺手把整包体积与 sha256 对上 release 声明与上游 digest —— 对不上就是截断或换过附件，
当场抛错而不是给一个"读到了"的清单。只认识 zip 与 tar.gz 两种格式，
其余（或没跑 `--live`）的行如实留在 `needs_extract`，不猜大小。

对照与不变量（`plan()` 里，任何一条不成立就不写盘）：
· 正向：每个 zip 必须含它那个能力的主二进制（`llama-server`/`whisper-cli`/`sd-cli`）——
  收集规则要是把主程序过滤掉了，装机就直接不可用；
· 负向：`test-*` 必须在源列表里出现过又被排除掉（排除一个本来就空的东西不叫排除）；
· 配对：win 的 `x-impl.dll` 必须有同名的 `x.exe`，孤儿要点名（ADR 记的"成对"约束）；
· 缺口：上游没有的产物（whisper 没有 macOS）必须成为一行，而不是被静默跳过。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))


def _load(modname: str):
    spec = importlib.util.spec_from_file_location(modname, os.path.join(HERE, modname + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# (OS 标签, {能力: 选择条件})。backend 用 "cpu"/"base" 是因为普通人默认没 CUDA；
# macos 的 arm64 是默认（Intel mac 的量太小，不值得为它单列一档）。
OS_DEFAULTS = [
    ("win-x64", {"对话": {"engine": "llama", "platform": "win", "arch": "x64", "backend": "cpu"},
                 "字幕": {"engine": "whisper", "platform": "win", "arch": "x64", "backend": "base"},
                 "生图": {"engine": "sd", "platform": "win", "arch": "x64", "backend": "cpu"}}),
    ("linux-x64", {"对话": {"engine": "llama", "platform": "ubuntu", "arch": "x64", "backend": "base"},
                   "字幕": {"engine": "whisper", "platform": "ubuntu", "arch": "x64", "backend": "base"},
                   "生图": {"engine": "sd", "platform": "ubuntu", "arch": "x64", "backend": "base"}}),
    ("macos-arm64", {"对话": {"engine": "llama", "platform": "macos", "arch": "arm64", "backend": "base"},
                     "字幕": {"engine": "whisper", "platform": "macos"},     # 上游没有 → 会变成缺口行
                     "生图": {"engine": "sd", "platform": "macos", "arch": "arm64", "backend": "base"}}),
]
MAIN_BINARY = {"llama": "llama-server", "whisper": "whisper-cli", "sd": "sd-cli"}
EXCLUDE = (re.compile(r"(^|/)test-[^/]*$"),)      # 8 个 whisper 测试程序等，见 ADR 的条目表


ENTRIES_OVERRIDE = None      # 测试缝：给一个 fn(url) -> entries，就能不联网证明三条判据会红


def read_entries(url: str) -> list[dict]:
    if ENTRIES_OVERRIDE is not None:
        return ENTRIES_OVERRIDE(url)
    return _load("zip_peek").entries(url)


def tar_summary(path: str, expect_bytes: int | None = None,
                want_sha: str | None = None) -> tuple[list[dict], dict]:
    """读一个 tar(.gz) 的成员表 + 体积与整包哈希的复核；不解包、不执行任何东西。"""
    size = os.path.getsize(path)
    if expect_bytes is not None and size != expect_bytes:
        raise SystemExit(f"{path} 取回 {size} B，与 release 声明的 {expect_bytes} B 不符 "
                         "—— 这是截断，成员表不能算读到了")
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    sha = h.hexdigest()
    if want_sha and sha != want_sha:
        raise SystemExit(f"{path} 整包 sha256={sha} 与上游 release digest={want_sha} 不符 "
                         "—— 我们钉的这个包不是上游那个（或上游换过附件）")
    with tarfile.open(path) as tf:
        ms = tf.getmembers()
    return ([{"name": m.name, "size": m.size, "isdir": m.isdir(), "packed": None} for m in ms],
            {"sha256": sha, "bytes": size, "digest_matches": bool(want_sha) and sha == want_sha})


def read_tar_entries(url: str, expect_bytes: int | None = None,
                     want_sha: str | None = None) -> tuple[list[dict], dict]:
    """把 tar.gz 取到临时文件、只读成员表：不解包、更不执行任何东西。

    为什么不走流式：tarfile 吃管道时与 curl 的写入互相卡住（本机实测 curl rc=23 EPIPE +
    "invalid header"），而 CPU 包本身只有 10–18 MiB —— 落个临时文件的代价远小于把判据做虚。
    体积与整包哈希都按上游声明复核（`tar_summary`），所以"读到了成员表"这件事
    是建立在一个字节都对得上的包上的。
    """
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "pkg.bin")
        p = subprocess.run(["curl", "-sSL", "--fail", "--max-time", "900",
                            "-A", "payload-plan/1.0", "-o", path, url],
                           capture_output=True)
        if p.returncode:
            raise SystemExit(f"curl 取 {url} 失败 rc={p.returncode}："
                             f"{(p.stderr or b'').decode('latin-1')[:120]}")
        return tar_summary(path, expect_bytes, want_sha)


def entries_for(url: str, expect_bytes: int | None = None,
                want_sha: str | None = None) -> tuple[list[dict], str, dict]:
    """.zip 走中央目录（Range 读，只看尾部）；tar.gz 读成员表并顺手把整包哈希对上上游 digest。"""
    if ENTRIES_OVERRIDE is not None:
        return ENTRIES_OVERRIDE(url), "override", {}
    low = url.lower()
    if low.endswith(".zip"):
        return read_entries(url), "zip-central-directory", {}
    if low.endswith((".tar.gz", ".tgz")):
        es, meta = read_tar_entries(url, expect_bytes, want_sha)
        return es, "tar-stream", meta
    return [], "unknown-format", {}


def pick(targets: dict, want: dict) -> dict | None:
    hits = [t for t in targets.values() if all(t.get(k) == v for k, v in want.items())]
    hits = [t for t in hits if t.get("bundle") in (None, "main")]
    return hits[0] if len(hits) == 1 else (hits[0] if hits else None)


def partition(names: list[str]) -> tuple[list[str], list[str]]:
    keep, drop = [], []
    for n in names:
        (drop if any(rx.search(n) for rx in EXCLUDE) else keep).append(n)
    return keep, drop


def pairing_problems(entries: list[dict]) -> list[str]:
    """win 的 `x-impl.dll` 要配 `x.exe`（ADR：llama-cli.exe + llama-cli-impl.dll 成对）。"""
    names = [os.path.basename(e["name"]) for e in entries if not e.get("dir")]
    stem = {n for n in names if n.lower().endswith(".exe")}
    stem = {n[:-4].lower() for n in stem}
    out = []
    for n in names:
        low = n.lower()
        if low.endswith("-impl.dll"):
            base = low[:-len("-impl.dll")]
            if base not in stem:
                out.append(f"{n} 没有同名的 .exe（孤儿运行库，收集规则或上游条目变了）")
    return out


def rows_for(os_label: str, targets: dict, live: bool,
             skipped: list[dict] | None = None) -> tuple[list[dict], list[str]]:
    rows, bad = [], []
    for cap, want in OS_DEFAULTS[[i for i, (l, _s) in enumerate(OS_DEFAULTS) if l == os_label][0]][1].items():
        t = pick(targets, want)
        if not t:
            row = {"os": os_label, "capability": cap, "status": "上游无产物", "asset": None,
                   "note": f"选择条件 {want} 在 discover 的产物表里唯一命中 0 次"}
            # "没有主包"与"上游为这个平台其实发了别的东西"是两件事。把同引擎、名字里带
            # 该平台痕迹的附件名列出来，live 时再读一眼成员表 —— 里面到底有没有可执行文件。
            # 被 discover 挡掉的附件（`skipped`）才是这一行该点名的东西：它们存在，
            # 但形状告诉我们不能当引擎用（如 xcframework = 库，不是可执行产物）。
            same_engine = want.get("engine")
            plat_tok = {"macos-arm64": r"macos|darwin|apple|xcframework",
                        "linux-x64": r"linux|ubuntu", "win-x64": r"win"}.get(os_label, "")
            near = [x for x in (skipped or [])
                    if x.get("engine") == same_engine
                    and re.search(plat_tok or ".", str(x.get("asset")), re.I)]
            row["same_engine_platform_assets"] = [
                {"asset": x["asset"], "release": x["release"], "because": x["because"],
                 "mib": round((x.get("size") or 0) / 1048576, 1)} for x in near]
            if live:
                ev = []
                for x in near:
                    if not str(x.get("url", "")).lower().endswith(".zip"):
                        continue
                    es2 = read_entries(x["url"])
                    names2 = [os.path.basename(e["name"]) for e in es2]
                    bin_ = MAIN_BINARY.get(same_engine or "", "")
                    ev.append({"asset": x["asset"], "entries": len(es2),
                               "has_main_binary": bin_ in names2 or f"{bin_}.exe" in names2,
                               "kinds": sorted({os.path.splitext(n)[1] or "(无扩展名)"
                                                for n in names2})[:10]})
                row["asset_evidence"] = ev
            rows.append(row)
            continue
        row = {"os": os_label, "capability": cap, "engine": t["engine"], "release": t["release"],
               "asset": t["asset"], "url": t["url"], "archive_mib": round(t["size"] / 1048576, 1),
               "digest": t.get("digest"), "inferred_platform": t.get("inferred_platform"),
               "status": "ok"}
        es, reader, meta = (entries_for(t["url"], t.get("size"),
                                        (t.get("digest") or "").removeprefix("sha256:") or None)
                            if live else ([], "", {}))
        if not live:
            row["note"] = "只到产物级；逐文件要 --live（zip 读中央目录、tar.gz 流式读 tar 头）"
        elif reader in ("", "unknown-format", "override-none"):
            row["status"] = "needs_extract"
            row["note"] = f"这种格式不知道怎么只读成员表（{t['asset']}）—— 如实标出，不猜大小"
        else:
            names = [e["name"] for e in es]
            keep, drop = partition(names)
            row.update({"reader": reader, "entries": len(es), "shipped": len(keep),
                        "excluded": drop,
                        "uncompressed_mib": round(sum(e["size"] for e in es) / 1048576, 1),
                        "packed_sum_mib": (round(sum(e["packed"] for e in es) / 1048576, 1)
                                           if all(e.get("packed") is not None for e in es)
                                           else None),
                        # tar 那条路顺手把整包体积与 sha256 都对上上游声明了（zip 走 Range 读，
                        # 只读尾部中央目录，本来就没有"整包字节"可算 —— 两行的证据强度不一样，
                        # 所以只有真验过的行才带这个字段）
                        "verified_bytes": meta or None})
            base_names = [os.path.basename(n) for n in keep]
            want_bin = MAIN_BINARY[t["engine"]]
            if not any(b == want_bin or b == want_bin + ".exe" for b in base_names):
                bad.append(f"{os_label}/{cap}: 收集后的清单里没有主二进制 {want_bin} —— 装机直接不可用")
            row["excluded_count"] = len(drop)
            # 不在这里要求"每个包都要有 test-*"：那是 whisper 的形状（ADR 条目表），
            # llama/sd 的包里没有测试程序，逐包强制只会把正确的输入判成红。
            # 排除机制是否真的起作用 由 plan() 层面的总量地板来查。
            row["pairing"] = pairing_problems(es)
            bad += [f"{os_label}/{cap}: {p}" for p in row["pairing"]]
        rows.append(row)
    return rows, bad


def load_targets(fixture_dir: str, manifest_path: str = "") -> tuple[dict, list[dict]]:
    """两种输入：discover 车道产出的 manifest（真跑用的就是这个），或离线夹具里的三家 release。
    只留离线那条路的话，打包车道就只能对着昨天的产物表选文件 —— 那正是命名漂移会咬人的地方。"""
    if manifest_path:
        with open(manifest_path, encoding="utf-8") as fh:
            doc = json.load(fh)
        targets = doc.get("targets") if isinstance(doc, dict) else None
        if not isinstance(targets, dict) or not targets:
            raise SystemExit(f"{manifest_path} 里没有非空的 targets 表，无法选产物")
        return targets, doc.get("skipped") or []
    ep = _load("engine_probe")
    rel = {}
    for e in ep.UPSTREAM:
        with open(os.path.join(fixture_dir, f"{e}.json"), encoding="utf-8") as fh:
            rel[e] = json.load(fh)      # CI 的 unit 跑 -W error::ResourceWarning，裸 open 会变成错误
    return ep.manifest_of(ep.plan(rel), ep.skipped_of(rel))["targets"],         ep.manifest_of(ep.plan(rel), ep.skipped_of(rel))["skipped"]


def plan(os_labels: list[str], live: bool, fixture_dir: str = "",
         manifest_path: str = "") -> tuple[dict, list[str]]:
    targets, skipped = load_targets(fixture_dir, manifest_path)
    all_rows, bad = [], []
    for label in os_labels:
        rows, b = rows_for(label, targets, live, skipped)
        all_rows += rows
        bad += b
    gaps = [r for r in all_rows if r["status"] != "ok"]
    exercised = sum(r.get("excluded_count", 0) or 0 for r in all_rows)
    if live and exercised == 0:
        # 负向对照：排除规则要是从没命中过任何东西，就等于没这条规则（而清单看上去是对的）。
        bad.append("排除规则在所有被读的包里一条都没命中 —— 要么上游条目变了，"
                   "要么这条规则已经没必要（那就删掉它并改 ADR，别留着当装饰）")
    out = {"os": {l: [r for r in all_rows if r["os"] == l] for l in os_labels},
           "totals_mib": {l: round(sum(r.get("archive_mib", 0) or 0
                                       for r in all_rows if r["os"] == l and r.get("archive_mib")), 1)
                          for l in os_labels},
           "gaps": [{"os": r["os"], "capability": r["capability"], "status": r["status"]} for r in gaps],
           "rows": all_rows}
    if len(all_rows) < len(os_labels) * 3:
        bad.append(f"只有 {len(all_rows)} 行（{len(os_labels)} 个 OS × 3 项能力），说明有行被丢了")
    return out, bad


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", default=os.path.join(ROOT, "tests", "fixtures"))
    ap.add_argument("--manifest", default="", help="discover 产出的 manifest.json（给了就用它，不读夹具）")
    ap.add_argument("--live", action="store_true", help="Range 读 zip 中央目录，出逐文件清单")
    ap.add_argument("--os", default="win-x64,linux-x64,macos-arm64")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    labels = [s.strip() for s in args.os.split(",") if s.strip()]
    out, bad = plan(labels, args.live, "" if args.manifest else args.fixtures, args.manifest)
    for l in labels:
        print(f"== {l}：引擎合计 {out['totals_mib'][l]} MiB（还没算权重）")
        for r in out["os"][l]:
            extra = ""
            if r.get("shipped"):
                extra = (f"，条目 {r['entries']} → 收集 {r['shipped']}，"
                         f"解压 {r['uncompressed_mib']} MiB（读法 {r['reader']}）"
                         + (f"，压缩计 {r['packed_sum_mib']} MiB"
                            if r.get("packed_sum_mib") is not None else ""))
            print(f"  {r['capability']:4} {str(r.get('asset'))[:46]:46} "
                  f"{str(r.get('archive_mib')) + ' MiB':>10}  [{r['status']}]{extra}"
                  + (f"  排除 {len(r['excluded'])} 项" if r.get("excluded") else ""))
            for p in (r.get("pairing") or []):
                print("      !", p)
    print(f"缺口 {len(out['gaps'])} 处：{out['gaps']}")
    for b in bad:
        print("BAD:", b)
    if args.json and not bad:
        with open(args.json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"写出 {args.json}")
    elif bad:
        print("有 BAD → 不写盘")
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
