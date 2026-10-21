"""出包车道要跑的那一步：把选中的引擎产物落成**装机负载目录 + 逐文件清单**。

只在 CI 跑（本机的硬约束是不编译不运行，连下载解包都不在这台机器上做）。
它做四件事，每件都有对应的自查：

① 按 `pins/payload-plan.json` 的默认档决定"这个 OS 的这项能力取哪个产物"；
② 下载后**先按上游 digest 算 sha256 校一遍**，对不上就停 —— 不许"先解包再说"；
③ 解包（stdlib 的 zipfile/tarfile，不调外部命令）并按 ADR 的收集规则落成扁平布局：
   llama 的 `llama-b11727/` 顶层目录与 whisper 的 `Release/` 都拉平，
   排除 `test-*.exe`，保留 `*-impl.dll` / `.so` / `.dylib` 运行时闭包（同目录才是 `$ORIGIN` 的前提）；
   **只留一个可执行文件**：上游 release 里十几个工具（llama-cli/llama-bench/llama-quantize…）
   连同它们各自的 `-impl.dll` 都不进负载，只留外壳真正 exec 的那个 server。
   共享运行库照旧全收 —— 那是动态链接闭包，删一个就起不来；
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

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)          # 被 import 时 sys.path[0] 不是 tools/
import launch_plan as lp           # noqa: E402  server 的名字只有一张权威表，见下

ROOT = os.path.abspath(os.path.join(HERE, ".."))

MAIN_BINARY = {"llama": ("llama-server", "llama-cli"), "whisper": ("whisper-cli", "whisper-server"),
               "sd": ("sd-cli", "sd-server")}
# MAIN_BINARY 只说"这包里有个主二进制"，**不能**当"外壳起得来"用：
# 外壳按 launch_plan 的表 exec `llama-server` / `whisper-server` / `sd-server`，
# 一份只带 CLI 的负载会过前者却起不来。所以 server 的名字从那张表取，不在这儿再写一遍。
SERVER_BINARY = {e: set(lp.binary_names(e)) for e in ("llama", "whisper", "sd")}
SERVER_FILES_LOWER = {e: {b.lower() for b in names} for e, names in SERVER_BINARY.items()}
SERVER_STEMS_LOWER = {
    e: {b[:-4] if b.lower().endswith(".exe") else b.lower() for b in names}
    for e, names in SERVER_BINARY.items()
}
EXCLUDE = re.compile(r"(^|/)test-[^/]*$")
LIB = re.compile(r"\.(dll|so|dylib)$", re.I)
IMPL = re.compile(r"^(?:lib)?(.+?)-impl(?:\.\d+)*\.(?:dll|so|dylib)$", re.I)
LICENSE = re.compile(r"^(license|copying|notice)(?:[-._]|$)", re.I)
DOCS = re.compile(r"^(readme|changelog|history)(?:[-._]|$)|\.(md|txt)$", re.I)


def is_lib(base: str) -> bool:
    low = base.lower()
    return bool(LIB.search(low)) or ".so." in low


def is_executable(entry: dict) -> bool:
    """tar 看可执行位；Windows 的 zip 不存 mode，只能看 `.exe` 后缀。"""
    if entry.get("kind") != "file":
        return False
    return bool(entry.get("mode", 0) & 0o111) or entry["dst"].lower().endswith(".exe")


def ship_reason(engine: str, entry: dict) -> str | None:
    """None = 进负载；否则返回不收的理由。

    判据只有一条：**一个引擎只放一个可执行文件**。上游把 CLI、bench、quantize、
    gguf-split 等十几个工具都打进同一个 release，我们只用 LAUNCH_PLAN 点名的那一个。
    每个工具还常带一份 `x-impl.dll` / `libx-impl.so`，所以只丢可执行文件不够，
    它那份 impl 也要丢；server 自己的 impl 必须留。`.dll/.so/.dylib` 是共享运行库
    （含 `libfoo.so.1` 与 `libfoo.0.7.0.dylib` 这种带版本号的），一个都不能按名字
    猜着删 —— 那是动态链接闭包。非可执行的数据文件（如 `ggml-metal.metal`、
    LICENSE）原样保留：真正决定"是不是工具"的是归档里的可执行位，不是后缀白名单。
    """
    low = entry["dst"].lower()
    if low in SERVER_FILES_LOWER.get(engine, set()):
        return None
    impl = IMPL.match(low)
    if impl:
        if impl.group(1) in SERVER_STEMS_LOWER.get(engine, set()):
            return None
        return "其它工具的 impl 运行库"
    if is_lib(low):
        return None
    if LICENSE.match(low):
        return None
    if DOCS.search(low):
        return "文档不随包"
    if is_executable(entry):
        return "只随包放 server 与运行库"
    return None


def _final_target(by_dst: dict[str, dict], entry: dict, depth: int = 8) -> dict | None:
    cur = entry
    for _ in range(depth):
        if cur.get("kind") != "symlink":
            return cur
        cur = by_dst.get(flatten(cur.get("link") or ""))
        if cur is None:
            return None
    return None


def collect_ship(archive: str, engine: str) -> tuple[list[dict], list[str]]:
    """在通用 collect 之上套出包策略；被策略挡掉的同样要留在 dropped 里点名。

    符号链接单独收：macOS 的 dylib 真身是 `libllama.0.7.0.dylib`，可执行文件按
    `@rpath/libllama.0.dylib` 找的却是链接名。只收普通文件会让闭包缺一环，
    所以链接保留，但它必须最终指向一个真的随包的普通文件，否则点名丢掉。
    """
    keep, dropped = collect(archive)
    shipped: list[dict] = []
    for entry in keep:
        why = ship_reason(engine, entry)
        if why is None:
            shipped.append(entry)
        else:
            dropped.append(f"{entry['src']}  # {why}")
    by_dst = {e["dst"]: e for e in shipped}
    final: list[dict] = []
    for entry in shipped:
        if entry.get("kind") != "symlink":
            final.append(entry)
            continue
        target = _final_target(by_dst, entry)
        if target is None:
            dropped.append(f"{entry['src']}  # 链接指向的 {entry.get('link')} 没有随包")
            continue
        final.append(entry)
    return final, dropped


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def entries(archive: str) -> list[dict]:
    """zip 与 tar(.gz|.zst) 都按同一套接口给成员，顺序稳定。

    每条带上 `kind`（file/symlink）、`mode` 与链接目标：判断可执行文件与动态链接
    闭包只能靠归档元数据，靠文件名猜会把 `libllama.0.7.0.dylib` 和工具搞混。
    """
    if archive.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            return [{"src": i.filename, "kind": "file", "mode": (i.external_attr >> 16) & 0xFFFF,
                     "link": None}
                    for i in z.infolist() if not i.is_dir()]
    out = []
    with tarfile.open(archive, "r:*") as t:
        for m in t.getmembers():
            if m.isfile():
                out.append({"src": m.name, "kind": "file", "mode": m.mode, "link": None})
            elif m.issym():
                out.append({"src": m.name, "kind": "symlink", "mode": m.mode, "link": m.linkname})
    return out


def flatten(name: str) -> str:
    """`llama-b11727/bin/llama-server.exe` → `llama-server.exe`。

    拉平不是省事：ADR 记的运行库查找靠 `$ORIGIN`/同目录，
    顶层目录原样搬进去只会让 `*.so` 与可执行文件分家。"""
    return os.path.basename(name.replace("\\", "/"))


def collect(archive: str) -> tuple[list[dict], list[str]]:
    """(要收的条目, 被排除的名字)；条目里带 `dst` = 落地相对路径。"""
    keep: list[dict] = []
    drop: list[str] = []
    seen: set[str] = set()
    for entry in entries(archive):
        src = entry["src"]
        if EXCLUDE.search(src.replace("\\", "/")):
            drop.append(src)
            continue
        base = flatten(src)
        if not base:
            continue
        if base in seen:          # 同名不同目录（例如两个包都把 README 放顶上）必须点名，
            drop.append(src + "  # 与已收录的同名文件冲突")   # 不能静默覆盖前一个
            continue
        seen.add(base)
        keep.append({**entry, "dst": base})
    return keep, drop


def extract_into(archive: str, keep: list[dict], out_dir: str) -> list[str]:
    """落盘；返回建不起来的链接（空列表 = 闭包完整）。"""
    os.makedirs(out_dir, exist_ok=True)
    files = [e for e in keep if e.get("kind") == "file"]
    links = [e for e in keep if e.get("kind") == "symlink"]

    def land(path: str, mode: int) -> None:
        # Windows 的 zip 不存 Unix 权限位（mode=0），照旧给可执行位；
        # tar 则照抄归档的 0644/0755，数据文件不该被改成可执行。
        os.chmod(path, (mode & 0o777) or 0o755)

    if archive.lower().endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for entry in files:
                dst = os.path.join(out_dir, entry["dst"])
                with z.open(entry["src"]) as fin, open(dst, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                land(dst, entry.get("mode") or 0)
    else:
        want = {e["src"]: e for e in files}
        with tarfile.open(archive, "r:*") as t:
            for m in t.getmembers():
                entry = want.get(m.name)
                if not m.isfile() or entry is None:
                    continue
                fin = t.extractfile(m)
                if fin is None:
                    continue
                dst = os.path.join(out_dir, entry["dst"])
                with open(dst, "wb") as fout:
                    shutil.copyfileobj(fin, fout)
                land(dst, entry.get("mode") or 0)

    # 链接目标是同目录的扁平名（与 files 的落地名一致）。链式链接（A→B→真身）
    # 按依赖顺序多轮落盘；Windows 上没有建链接的权限时退化成一份副本：
    # 内容与哈希仍然对得上，包也能跑。
    pending = [(e, flatten(e.get("link") or "")) for e in links]
    failed: list[str] = []
    while pending:
        rest = []
        progress = False
        for entry, target in pending:
            src = os.path.join(out_dir, target) if target else ""
            if not target or not os.path.isfile(src):
                rest.append((entry, target))
                continue
            dst = os.path.join(out_dir, entry["dst"])
            try:
                os.symlink(target, dst)
            except (OSError, NotImplementedError):
                shutil.copy2(src, dst)
            progress = True
        if not progress:
            failed = [f"{e['dst']} → {t or '(空)'}" for e, t in rest]
            break
        pending = rest
    return failed


def manifest_of(out_dir: str, entries: list[dict] | None = None) -> list[dict]:
    meta = {e["dst"]: e for e in entries or []}
    rows = []
    for name in sorted(os.listdir(out_dir)):
        p = os.path.join(out_dir, name)
        if not os.path.isfile(p):        # 断链不算落地文件
            continue
        e = meta.get(name, {})
        rows.append({"file": name, "kind": e.get("kind", "file"),
                     "exec": bool(e and is_executable(e)),
                     "bytes": os.path.getsize(p), "sha256": sha256_file(p)})
    return rows


def check(engine: str, rows: list[dict]) -> list[str]:
    bad = []
    names = {r["file"] for r in rows}
    mains = [b for b in MAIN_BINARY.get(engine, ()) if any(n == b or n.startswith(b + ".") for n in names)]
    if not mains:
        bad.append(f"{engine}: 收集后没有主二进制（期望 {MAIN_BINARY.get(engine)} 之一）—— 装了也不能用")
    servers = {b for b in SERVER_BINARY.get(engine, set())
               if b in names or (b + ".dylib") in names}
    if SERVER_BINARY.get(engine) and not servers:
        bad.append(f"{engine}: 负载里没有外壳要点名执行的那个 server（期望 "
                   f"{sorted(SERVER_BINARY[engine])} 之一）—— 只有 CLI 不算，装了也起不来")
    if any(EXCLUDE.search(n) for n in names):
        bad.append(f"{engine}: 清单里还有 test-* 被排除的东西，说明排除与收集两处规则不一致")
    # 有归档元数据时按可执行位判；旧清单没有 exec 字段时退回 `.exe` 后缀。
    extras = sorted(r["file"] for r in rows
                    if (r.get("exec") or r["file"].lower().endswith(".exe"))
                    and not is_lib(r["file"])
                    and r["file"].lower() not in SERVER_FILES_LOWER.get(engine, set()))
    if extras:
        bad.append(f"{engine}: 负载里除 server 外还有可执行文件 {extras} —— 只留一个")
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
    keep, dropped = collect_ship(archive, engine)
    failed = extract_into(archive, keep, out_dir)
    bad += [f"{engine}: 链接建不起来 {x}（运行时找不到这个名字）" for x in failed]
    rows = manifest_of(out_dir, keep)
    bad += check(engine, rows)
    entry = {"engine": engine, "status": "ok", "asset": row["asset"], "url": row["url"],
             "archive_sha256": got, "archive_bytes": os.path.getsize(archive),
             "policy": "server-only",
             "executables": [r["file"] for r in rows if r.get("exec")],
             "symlinks": [r["file"] for r in rows if r.get("kind") == "symlink"],
             "excluded": [os.path.basename(d.split("#")[0].strip()) for d in dropped],
             "shipped_mib": round(sum(r["bytes"] for r in rows
                                      if r.get("kind") != "symlink") / 1048576, 1),
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
        names = [e["dst"] for e in keep]
        cases = [
            ("排除 test-*（同名冲突也归入排除，但要带得出来的理由）",
             "test-ggml.exe" in dropped[0] and len(dropped) == 2, dropped),
            ("拉平目录", names == ["llama-server.exe", "llama-server-impl.dll"], names),
            ("同名冲突要点名", any("冲突" in x for x in dropped), dropped),
        ]
        extract_into(a, keep, os.path.join(d, "out"))
        rows = manifest_of(os.path.join(d, "out"), keep)
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
            for name, payload, mode in (("whisper-bin-ubuntu-x64/Release/whisper-cli", b"cli", 0o755),
                                        ("whisper-bin-ubuntu-x64/Release/whisper-server", b"srv", 0o755),
                                        ("whisper-bin-ubuntu-x64/Release/test-vad", b"junk", 0o755),
                                        ("whisper-bin-ubuntu-x64/Release/libwhisper.so", b"so", 0o755)):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mode = mode
                t.addfile(info, io.BytesIO(payload))
        tkeep, tdrop = collect_ship(tg, "whisper")
        tnames = [e["dst"] for e in tkeep]
        cases.append(("tar.gz 同样能拉平；只留 server，CLI 被点名排除",
                      tnames == ["whisper-server", "libwhisper.so"] and
                      any("whisper-cli" in x for x in tdrop) and
                      any("test-vad" in x for x in tdrop), (tnames, tdrop)))
        tout = os.path.join(d, "tout")
        extract_into(tg, tkeep, tout)
        trows = manifest_of(tout, tkeep)
        cases.append(("tar.gz 解出来的文件可执行位与清单",
                      len(trows) == 2 and check("whisper", trows) == [],
                      check("whisper", trows)))
        # 新判据的正反两面：只有 CLI 的负载过去是**绿的**，而外壳起不来。
        only_cli = [dict(file="whisper-cli", sha256="a" * 64, bytes=3),
                    dict(file="libwhisper.so", sha256="b" * 64, bytes=2)]
        oc = check("whisper", only_cli)
        cases.append(("只有 CLI 的负载必须红（server 名字与 launch_plan 同一张表）",
                      any("只有 CLI 不算" in x for x in oc), oc))
        only_server = [dict(file="whisper-server", sha256="c" * 64, bytes=4),
                       dict(file="libwhisper.so", sha256="b" * 64, bytes=2)]
        cases.append(("server + 运行库就绿（正向对照）",
                      check("whisper", only_server) == [], check("whisper", only_server)))
        with_cli = only_server + [dict(file="whisper-cli", exec=True, sha256="a" * 64, bytes=3)]
        cases.append(("多带一个 CLI 必须红（只留一个可执行文件）",
                      any("只留一个" in x for x in check("whisper", with_cli)),
                      check("whisper", with_cli)))
        # 只用判据不够：真的把一份带 13 个工具的包收一遍，看策略有没有落地。
        poly = io.BytesIO()
        with zipfile.ZipFile(poly, "w") as z:
            for name, payload in (
                    ("llama-b11727/llama-server.exe", b"server"),
                    ("llama-b11727/llama-server-impl.dll", b"srv-impl"),
                    ("llama-b11727/llama-cli.exe", b"cli"),
                    ("llama-b11727/llama-cli-impl.dll", b"cli-impl"),
                    ("llama-b11727/llama-bench.exe", b"bench"),
                    ("llama-b11727/ggml.dll", b"ggml"),
                    ("llama-b11727/libggml.so.1", b"so-versioned"),
                    ("llama-b11727/LICENSE", b"license text"),
                    ("llama-b11727/README.md", b"readme")):
                z.writestr(name, payload)
        pa = os.path.join(d, "p.zip")
        with open(pa, "wb") as fh:
            fh.write(poly.getvalue())
        pkeep, pdropped = collect_ship(pa, "llama")
        pnames = [e["dst"] for e in pkeep]
        cases.append(("只留 server：其它 CLI/bench/README 全部点名丢掉",
                      pnames == ["llama-server.exe", "llama-server-impl.dll", "ggml.dll",
                                 "libggml.so.1", "LICENSE"] and
                      any("llama-cli.exe" in x for x in pdropped) and
                      any("llama-cli-impl.dll" in x for x in pdropped) and
                      any("README.md" in x for x in pdropped), (pnames, pdropped)))
        pout = os.path.join(d, "pout")
        extract_into(pa, pkeep, pout)
        prows = manifest_of(pout, pkeep)
        cases.append(("收窄后的负载通过 check", check("llama", prows) == [], check("llama", prows)))
        cases.append(("手工塞回一个 llama-cli.exe 必须红",
                      any("只留一个" in x for x in check(
                          "llama", prows + [dict(file="llama-cli.exe", exec=True,
                                                 sha256="a" * 64, bytes=3)])),
                      check("llama", prows + [dict(file="llama-cli.exe", exec=True,
                                                   sha256="a" * 64, bytes=3)])))
        # macOS 的 tar 里 dylib 真身是 libx.0.7.0.dylib，可执行文件按 libx.0.dylib
        # 找；只考普通文件的收集器会把闭包拆掉。数据文件（.metal）不许被"非库"误删。
        mac = os.path.join(d, "m.tar.gz")
        with tarfile.open(mac, "w:gz") as t:
            for name, payload in (("llama-b11727/llama-server", b"server"),
                                  ("llama-b11727/llama-cli", b"cli"),
                                  ("llama-b11727/libllama.0.7.0.dylib", b"real-dylib"),
                                  ("llama-b11727/libllama-quantize-impl.so", b"other-impl"),
                                  ("llama-b11727/ggml-metal.metal", b"shader")):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                info.mode = 0o755 if not name.endswith((".dylib", ".metal")) else 0o644
                if name.endswith(".dylib"):
                    info.mode = 0o755
                t.addfile(info, io.BytesIO(payload))
            for link, target in (("libllama.0.dylib", "libllama.0.7.0.dylib"),
                                 ("libllama.dylib", "libllama.0.dylib")):
                info = tarfile.TarInfo("llama-b11727/" + link)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                info.mode = 0o777
                t.addfile(info)
        mkeep, mdrop = collect_ship(mac, "llama")
        mnames = [e["dst"] for e in mkeep]
        cases.append(("macOS 闭包：链式 dylib 链接保留、别的工具 impl 与 CLI 丢掉、.metal 留下",
                      mnames == ["llama-server", "libllama.0.7.0.dylib", "ggml-metal.metal",
                                 "libllama.0.dylib", "libllama.dylib"] and
                      any("llama-cli" in x for x in mdrop) and
                      any("quantize-impl" in x for x in mdrop), (mnames, mdrop)))
        mout = os.path.join(d, "mout")
        failed = extract_into(mac, mkeep, mout)
        mrows = manifest_of(mout, mkeep)
        hashes = {r["file"]: r["sha256"] for r in mrows}
        cases.append(("链接能落盘且哈希指向真身（没建链接权限时是副本）",
                      failed == [] and hashes.get("libllama.dylib") ==
                      hashes.get("libllama.0.7.0.dylib") == hashes.get("libllama.0.dylib") and
                      check("llama", mrows) == [], (failed, mrows)))
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
