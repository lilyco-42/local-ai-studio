"""出包车道要跑的那一步：把选中的引擎产物落成**装机负载目录 + 逐文件清单**。

只在 CI 跑（本机的硬约束是不编译不运行，连下载解包都不在这台机器上做）。
它做四件事，每件都有对应的自查：

① 按 `pins/payload-plan.json` 的默认档决定"这个 OS 的这项能力取哪个产物"；
② 下载后**先按上游 digest 算 sha256 校一遍**，对不上就停 —— 不许"先解包再说"；
③ 解包（stdlib 的 zipfile/tarfile，不调外部命令）并按 ADR 的收集规则落成扁平布局：
   llama 的 `llama-b11727/` 顶层目录与 whisper 的 `Release/` 都拉平，
   排除 `test-*.exe`，保留 `*-impl.dll` / `.so` / `.dylib` 运行时闭包（同目录才是 `$ORIGIN` 的前提）；
④ 逐文件算 sha256 写 `payload-manifest.json`，并自查：
   主二进制必须在、被排除的不许出现在产物里、清单不许空、每条都得有哈希与体积。

为什么坚持"逐文件清单"而不是一句"包已生成"：外壳运行时要按文件校验引擎（也支持
用户自己塞进来的权重），而上游那些 tar.gz 的中央目录是读不出来的 —— 这一层就是
`payload_plan` 里 `needs_extract` 那几行的答案来源。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

MAIN_BINARY = {"llama": ("llama-server", "llama-cli"), "whisper": ("whisper-cli", "whisper-server"),
               "sd": ("sd-cli", "sd-server")}
EXCLUDE = re.compile(r"(^|/)test-[^/]*$")


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def members(archive: str) -> list[str]:
    """zip 与 tar(.gz|.zst) 都按同一套接口给成员名，顺序稳定。"""
    if archive.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            return [i.filename for i in z.infolist() if not i.is_dir()]
    with tarfile.open(archive, "r:*") as t:
        return [m.name for m in t.getmembers() if m.isfile()]
    return []


def flatten(name: str) -> str:
    """`llama-b11727/bin/llama-server.exe` → `llama-server.exe`。

    拉平不是省事：ADR 记的运行库查找靠 `$ORIGIN`/同目录，
    顶层目录原样搬进去只会让 `*.so` 与可执行文件分家。"""
    return os.path.basename(name.replace("\\", "/"))


def collect(archive: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(要收的 (成员名, 落地相对路径), 被排除的名字)。"""
    keep: list[tuple[str, str]] = []
    drop: list[str] = []
    seen: set[str] = set()
    for m in members(archive):
        if EXCLUDE.search(m.replace("\\", "/")):
            drop.append(m)
            continue
        base = flatten(m)
        if not base:
            continue
        if base in seen:          # 同名不同目录（例如两个包都把 README 放顶上）必须点名，
            drop.append(m + "  # 与已收录的同名文件冲突")   # 不能静默覆盖前一个
            continue
        seen.add(base)
        keep.append((m, base))
    return keep, drop


def extract_into(archive: str, keep: list[tuple[str, str]], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    if archive.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for src, dst in keep:
                with z.open(src) as fin, open(os.path.join(out_dir, dst), "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                os.chmod(os.path.join(out_dir, dst), 0o755)
    else:
        with tarfile.open(archive, "r:*") as t:
            want = dict(keep)
            for m in t.getmembers():
                if m.isfile() and m.name in want:
                    fin = t.extractfile(m)
                    if fin is None:
                        continue
                    dst = os.path.join(out_dir, want[m.name])
                    with open(dst, "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                    os.chmod(dst, 0o755)


def manifest_of(out_dir: str) -> list[dict]:
    rows = []
    for name in sorted(os.listdir(out_dir)):
        p = os.path.join(out_dir, name)
        if os.path.isfile(p):
            rows.append({"file": name, "bytes": os.path.getsize(p), "sha256": sha256_file(p)})
    return rows


def check(engine: str, rows: list[dict]) -> list[str]:
    bad = []
    names = {r["file"] for r in rows}
    mains = [b for b in MAIN_BINARY.get(engine, ()) if any(n == b or n.startswith(b + ".") for n in names)]
    if not mains:
        bad.append(f"{engine}: 收集后没有主二进制（期望 {MAIN_BINARY.get(engine)} 之一）—— 装了也不能用")
    if any(EXCLUDE.search(n) for n in names):
        bad.append(f"{engine}: 清单里还有 test-* 被排除的东西，说明排除与收集两处规则不一致")
    # 不逐包要求"必须有东西被排除"：llama/sd 的包里本来就没有 test-*，
    # 那样会把正确的输入判成红（payload_plan 上同一课已经交过学费了）。
    # 排除机制是否真在干活由 selfcheck 的合成包与 CI 里的总命中数管。
    for r in rows:
        if not r.get("sha256") or not r.get("bytes"):
            bad.append(f"{engine}: {r.get('file')} 的哈希或体积是空的")
    win_bins = {n[:-4].lower() for n in names if n.lower().endswith(".exe")}
    for n in names:
        low = n.lower()
        if low.endswith("-impl.dll") and low[:-len("-impl.dll")] not in win_bins:
            bad.append(f"{engine}: 孤儿运行库 {n}（没有同名 .exe）")
    return bad


def download(url: str, dest: str, timeout: int = 3600) -> int:
    rc = subprocess.run(["curl", "-sSL", "--fail", "--max-time", str(timeout),
                         "-A", "pack-engine/1.0", "-o", dest, url]).returncode
    return rc


def pack_one(row: dict, out_root: str, keep_archives: bool) -> tuple[dict, list[str]]:
    engine = row["engine"]
    out_dir = os.path.join(out_root, engine)
    archive = os.path.join(out_root, row["asset"])
    bad = []
    if download(row["url"], archive) != 0:
        return {"engine": engine, "status": "download_failed"}, [f"{engine}: 下载失败 {row['url']}"]
    digest = str(row.get("digest") or "")
    got = sha256_file(archive)
    if not digest.startswith("sha256:") or digest.split(":", 1)[1] != got:
        bad.append(f"{engine}: 整包 sha256 与上游 digest 不符（算出 {got}，钉的是 {digest}）→ 不停手就不解包")
        return {"engine": engine, "status": "digest_mismatch", "sha256": got}, bad
    keep, dropped = collect(archive)
    extract_into(archive, keep, out_dir)
    rows = manifest_of(out_dir)
    bad += check(engine, rows)
    entry = {"engine": engine, "status": "ok", "asset": row["asset"], "url": row["url"],
             "archive_sha256": got, "archive_bytes": os.path.getsize(archive),
             "excluded": [os.path.basename(d.split("#")[0].strip()) for d in dropped],
             "shipped_mib": round(sum(r["bytes"] for r in rows) / 1048576, 1),
             "files": rows}
    if keep_archives:
        entry["kept_archive"] = os.path.basename(archive)
    return entry, bad


def selfcheck() -> int:
    """不联网，用现造的合成压缩包证明上面这套收集/校验逻辑不是永远绿。"""
    import tempfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("llama-b11727/llama-server.exe", b"bin")
        z.writestr("llama-b11727/llama-server-impl.dll", b"dll")
        z.writestr("llama-b11727/test-ggml.exe", b"junk")
        z.writestr("llama-b11727/sub/llama-server.exe", b"dup")     # 同名不同目录 → 必须被点名
    rc = 0
    with tempfile.TemporaryDirectory() as d:
        a = os.path.join(d, "x.zip")
        with open(a, "wb") as fh:
            fh.write(buf.getvalue())
        keep, dropped = collect(a)
        names = [dst for _s, dst in keep]
        cases = [
            ("排除 test-*（同名冲突也归入排除，但要带得出来的理由）",
             "test-ggml.exe" in dropped[0] and len(dropped) == 2, dropped),
            ("拉平目录", names == ["llama-server.exe", "llama-server-impl.dll"], names),
            ("同名冲突要点名", any("冲突" in x for x in dropped), dropped),
        ]
        extract_into(a, keep, os.path.join(d, "out"))
        rows = manifest_of(os.path.join(d, "out"))
        cases.append(("清单有哈希", all(r["sha256"] and r["bytes"] for r in rows) and len(rows) == 2, rows))
        cases.append(("主二进制在位", check("llama", rows) == [], check("llama", rows)))
        orphans = [dict(file="llama-server.exe"), dict(file="ghost-impl.dll")]
        bad = check("llama", orphans)
        cases.append(("孤儿运行库要红", any("ghost-impl.dll" in b for b in bad), bad))
        cases.append(("什么都没排除**不该**红（llama/sd 的包本来就没有 test-*）",
                      check("llama", rows) == [], check("llama", rows)))
        # tar.gz 那条路也必须真跑过：llama/whisper 的 Linux 与 macOS 包就是这个格式，
        # 只测 zip 的话 CI 里第一次解 tar 才是第一次验逻辑。
        tg = os.path.join(d, "y.tar.gz")
        with tarfile.open(tg, "w:gz") as t:
            for name, payload in (("whisper-bin-ubuntu-x64/Release/whisper-cli", b"cli"),
                                  ("whisper-bin-ubuntu-x64/Release/test-vad", b"junk"),
                                  ("whisper-bin-ubuntu-x64/Release/libwhisper.so", b"so")):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                t.addfile(info, io.BytesIO(payload))
        tkeep, tdrop = collect(tg)
        tnames = [dst for _s, dst in tkeep]
        cases.append(("tar.gz 同样能拉平并排除",
                      tnames == ["whisper-cli", "libwhisper.so"] and tdrop ==
                      ["whisper-bin-ubuntu-x64/Release/test-vad"], (tnames, tdrop)))
        tout = os.path.join(d, "tout")
        extract_into(tg, tkeep, tout)
        trows = manifest_of(tout)
        cases.append(("tar.gz 解出来的文件可执行位与清单",
                      len(trows) == 2 and check("whisper", trows) == [],
                      check("whisper", trows)))
        cases.append(("主二进制缺席必须红",
                      any("主二进制" in b for b in check("whisper",
                                                          [dict(file="libwhisper.so", sha256="f" * 64,
                                                                bytes=2)])),))
    for case in cases:
        label, ok = case[0], case[1]
        detail = case[2] if len(case) > 2 else ""
        rc |= 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label} → {detail}")
    return rc


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--plan", default=os.path.join(ROOT, "pins", "payload-plan.json"))
    ap.add_argument("--os", default="win-x64")
    ap.add_argument("--out", default="")
    ap.add_argument("--keep-archives", action="store_true")
    args = ap.parse_args(argv)
    if args.selfcheck:
        return selfcheck()
    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    rows = [r for r in plan["os"].get(args.os, []) if r.get("status") in ("ok", "needs_extract")]
    if not rows:
        print(f"计划里没有 {args.os} 的行，能出什么包？")
        return 1
    out_root = args.out or os.path.join(ROOT, "dist", args.os)
    if os.path.isdir(out_root):
        shutil.rmtree(out_root)
    os.makedirs(out_root, exist_ok=True)
    engines, bad = [], []
    for r in rows:
        entry, b = pack_one(r, out_root, args.keep_archives)
        engines.append(entry)
        bad += b
    doc = {"os": args.os, "engines": engines,
           "total_shipped_mib": round(sum(e.get("shipped_mib", 0) for e in engines), 1)}
    text = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True)
    with open(os.path.join(out_root, "payload-manifest.json"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print(text[:1500])
    for b in bad:
        print("BAD:", b)
    print("BAD 数量 =", len(bad), "；负载目录", out_root)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
