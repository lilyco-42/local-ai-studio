#!/usr/bin/env python3
"""引擎的"后端矩阵"：每个 release 到底发了哪几种加速版本、体积与哈希是多少。

要钉的是两条产品约束：
  * 第 5 条"按检测到的后端分层" —— 前提是**知道有哪些后端可换**，这句话此前只在
    ADR-0001 里写着"36 个产物含 win-cuda / ubuntu-vulkan …"，没有任何产物能红的了它；
  * 第 3 条"引擎按 runtime 打包、可替换" —— 可替换的对象就是这张表里的这些资产。

分类只从**上游真实的资产名**读，规则表在 `BACKEND_TOKENS` / `OS_TOKENS` 里，读不出来
就报 `unknown_*` 并让它红 —— 那是"上游换了拼法"的信号，不是可以静默归类的东西。
两个真实存在的拼法坑（都在这次的捕获里）：sd 的 CUDA 一边写 `cuda12` 一边写 `cu12`；
llama 的 CUDA 还有一份 `cudart-` 前缀的伴生包（把 CUDA 运行时一起装），体积差 100+ MiB。

**这张表不猜没写在名字里的东西**：llama/sd 的 macOS 资产名不带后端标记，
所以 mac 上"有没有 Metal"不能由资产名断言 —— 记成 `claim: none` 的缺口，
而不是替上游填一个好看的默认值。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "pins", "backend-matrix.json")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "backend_assets.json")
PAYLOAD = os.path.join(ROOT, "pins", "payload-plan.json")

# 顺序敏感：先认更具体的组合（opencl-adreno 必须在 opencl 之前，如果哪天出现的话）。
BACKEND_TOKENS = (
    (re.compile(r"opencl-adreno", re.IGNORECASE), "opencl-adreno", None),
    (re.compile(r"cublas", re.IGNORECASE), "cublas", None),
    (re.compile(r"cuda[-_.]?([0-9]+(?:[._][0-9]+)*)", re.IGNORECASE), "cuda", 1),
    (re.compile(r"(?<![a-z])cu([0-9]+)", re.IGNORECASE), "cuda", 1),
    (re.compile(r"rocm[-_.]?([0-9]+(?:[._][0-9]+)*)", re.IGNORECASE), "rocm", 1),
    (re.compile(r"vulkan", re.IGNORECASE), "vulkan", None),
    (re.compile(r"sycl(?:-fp(16|32))?", re.IGNORECASE), "sycl", 1),
    (re.compile(r"openvino[-_.]?([0-9]+(?:\.[0-9]+)*)", re.IGNORECASE), "openvino", 1),
    (re.compile(r"snapdragon", re.IGNORECASE), "qnn-snapdragon", None),
    (re.compile(r"(?<![a-z])blas", re.IGNORECASE), "blas", None),
    (re.compile(r"(?:^|-)cpu(?:-|$)", re.IGNORECASE), "cpu", None),
)
OS_TOKENS = (
    (re.compile(r"(?:macos|darwin).*arm64|arm64.*(?:macos|darwin)", re.IGNORECASE), "macos-arm64"),
    (re.compile(r"(?:macos|darwin).*x64|x64.*(?:macos|darwin)", re.IGNORECASE), "macos-x64"),
    (re.compile(r"android-arm64", re.IGNORECASE), "android-arm64"),
    (re.compile(r"Win32", re.IGNORECASE), "win-x86"),
    (re.compile(r"(?<![a-z])win(?![a-z]).*arm64|arm64.*(?<![a-z])win(?![a-z])", re.IGNORECASE), "win-arm64"),
    (re.compile(r"(?<![a-z])(?:win|windows)(?![a-z]).*(?:x64|x86_64)|(?:x64|x86_64).*(?<![a-z])(?:win|windows)(?![a-z])",
                   re.IGNORECASE), "win-x64"),
    (re.compile(r"(?:ubuntu|linux).*(?:x86_64|x64)|(?:x86_64|x64).*(?:ubuntu|linux)", re.IGNORECASE),
     "linux-x64"),
    (re.compile(r"(?:ubuntu|linux).*arm64|arm64.*(?:ubuntu|linux)", re.IGNORECASE), "linux-arm64"),
    (re.compile(r"s390x", re.IGNORECASE), "linux-s390x"),
)
# 不是"引擎 runtime"的资产也要有名有姓地说出来，不许静默消失。
NON_RUNTIME = (
    (re.compile(r"^cudart-", re.IGNORECASE), "cudart_companion", "把 CUDA 运行时一起装的伴生包（体积另算）"),
    (re.compile(r"xcframework", re.IGNORECASE), "library_only", "只有库与头（Apple 侧没有可执行文件，见 #29）"),
    (re.compile(r"-ui\.tar\.gz$", re.IGNORECASE), "not_a_runtime", "webUI 资源，不是引擎"),
)
# 每个引擎"至少要有几种后端"——上游撤掉一档就该在这里红。
BACKEND_FLOOR = {"llama": 5, "whisper": 3, "sd": 4}


# 名字里"不是后端"的那些词。上游哪天加一档新后端（hip？mtl？），不会在这里，
# 于是哨兵会报 —— 这正是要的效果：新词必须由人来认一次，不许顺手归成 cpu。
KNOWN_WORDS = {"bin", "master", "nightly", "ui", "cudart", "xcframework", "win", "windows", "win32",
               "ubuntu", "linux", "macos", "darwin", "android", "x64", "x86", "arm64", "s390x",
               "i686", "cpu", "blas", "appimage", "deb", "rpm", "tar", "gz", "zip",
               "llama", "whisper", "sd"}


def unknown_tokens(name: str, release: str, consumed) -> list[str]:
    base = re.sub(r"\.(?:zip|tar\.gz|tgz)$", "", name, flags=re.IGNORECASE)
    for a, b in sorted(consumed, reverse=True):
        base = base[:a] + "-" + base[b:]
    for piece in release.split("-"):
        base = base.replace(piece, "-")
    return sorted({t for t in re.split(r"[^0-9A-Za-z]+", base)
                   if t and not t.isdigit() and t.lower() not in KNOWN_WORDS})


def classify(name: str, release: str = "") -> dict:
    for rx, kind, note in NON_RUNTIME:
        if rx.search(name):
            return {"kind": kind, "note": note, "os": None, "backend": None, "version": None}
    backend, version, consumed = None, None, []
    for rx, b, vgrp in BACKEND_TOKENS:
        m = rx.search(name)
        if m:
            backend = b
            version = m.group(vgrp) if vgrp else None
            consumed.append(m.span())
            break
    os_bucket = None
    for rx, o in OS_TOKENS:
        if rx.search(name):
            os_bucket = o
            break
    # 顺序要紧：先把"名字里剩下的词"算出来，再决定归哪一档。上一版在这里先替
    # 没有后端标记的名字写了 cpu，于是哨兵永远看不到 mystery 这类新词 —— 橡皮图章。
    # 顺序要紧：先把"名字里剩下的词"算出来再决定归哪一档。先写 cpu 再查剩词是没用的 ——
    # 那一版让 hip 这类新词一路被归成 cpu，哨兵永远不会响。
    left = unknown_tokens(name, release, consumed)
    if left:
        head = ("名字里有这张表认不得的词：" if backend is None else
                "认得出后端 " + backend + "，但名字里还有认不得的词：")
        return {"kind": "runtime", "note": head + ",".join(left), "os": os_bucket,
                "backend": "unknown_token" if backend is None else backend, "version": version}
    if os_bucket and os_bucket.startswith("macos") and backend is None:
        # macOS 的资产名不写后端 —— 这是事实，不是失败：不许猜 Metal，也不许当 CPU。
        return {"kind": "runtime", "note": "上游资产名不标后端；mac 上有没有加速不能由名字断言",
                "os": os_bucket, "backend": "unknown_from_name", "version": None}
    if os_bucket is None:
        return {"kind": "runtime", "note": "名字里不写平台（上游确实有这样的包），这一格只能空着",
                "os": None, "backend": backend or "cpu", "version": version}
    if backend is None:
        # bare 名字（ubuntu-x64 / whisper-bin-x64）：上游的默认构建就是 CPU 那一份
        backend = "cpu"
    return {"kind": "runtime", "note": "", "os": os_bucket, "backend": backend, "version": version}


def captures_path() -> str:
    return FIXTURE


def build(captures: dict, picks: list[dict], repos: dict) -> tuple[dict, list[str]]:
    problems, engines = [], {}
    for ref_key, assets in sorted(captures.items()):
        repo, _, release = ref_key.partition("@")
        engine = next((k for k, v in repos.items() if v == repo), None)
        if engine is None:
            problems.append(f"{ref_key}: 这张表不认识这个仓库（捕获与 payload-plan 的仓库表漂了）")
            continue
        rows, others = [], []
        for a in assets:
            c = classify(a["name"], release)
            if c["kind"] != "runtime":
                # 不是 runtime 也要有名有姓：whisper 在 mac 上只有 xcframework（库），
                # 这正是 #29 的数据面；静默丢掉就等于把证据扔了。
                others.append({"asset": a["name"], "mib": round(a["size"] / 1024 ** 2, 1),
                               "sha256": a["sha256"], "kind": c["kind"], "note": c["note"]})
                continue
            if c["backend"] == "unknown_token" or "认不得的词" in c["note"]:
                problems.append(f"{engine}/{a['name']}: {c['note']} —— 上游换了拼法或加了新后端，"
                                "这张表要人来认一次，不许静默归类")
            rows.append({"asset": a["name"], "mib": round(a["size"] / 1024 ** 2, 1),
                         "sha256": a["sha256"], **c})
        runtimes = rows
        matrix: dict = {}
        no_platform = []
        for r in runtimes:
            if r["os"] is None:
                # 名字里不写平台的那些不进矩阵：矩阵是按 OS 查"有哪些后端可换"的，
                # 空键会让读的人以为那一格是"没有"，而真相是"看不出来"。
                no_platform.append({"asset": r["asset"], "backend": r["backend"], "mib": r["mib"]})
                continue
            # 一格是**一个列表**，不是一个：同一个后端可以有好几个版本
            # （llama 在 win-x64 同时发 cuda-12.4 与 cuda-13.4，ubuntu 的 sycl 有 fp16/fp32，
            # whisper 在 win-x64 有 cuda 11.8.0 与 12.4.0）。上一版写成 dict[key]=一行，
            # 于是每格只剩字典序最后那颗，而"36 个产物"数着 36、格子只装得下十来个 ——
            # 少掉那几颗不会红，只会让选择表以为上游没发。
            matrix.setdefault(r["os"], {}).setdefault(r["backend"], []).append(
                {"asset": r["asset"], "version": r["version"], "mib": r["mib"],
                 "sha256": r["sha256"]})
        for cells in matrix.values():
            for same_backend in cells.values():        # 别叫 rows：会把外层的 rows 换掉
                same_backend.sort(key=lambda x: (x["version"] or "", x["asset"]))
        if sum(len(rows) for cells in matrix.values() for rows in cells.values()) != len(runtimes)                 - len(no_platform):
            problems.append("有 runtime 行没进任何格子 —— 一格一行的写法会把同后端的多个版本吃掉")
        engines[engine] = {"repo": repo, "release": release, "assets_total": len(assets),
                           "runtimes": len(runtimes), "backends": sorted(
                               {r["backend"] for r in runtimes}),
                           "by_os": matrix, "格子条目": sum(
                               len(v) for c in matrix.values() for v in c.values()),
                           "rows": sorted(rows, key=lambda r: r["asset"]),
                           "not_runtime": sorted(others, key=lambda r: r["asset"]),
                           "platform_not_in_name": sorted(no_platform, key=lambda r: r["asset"])}
        # 自洽检查：这张表里留下的 runtime 行，必须**逐个等于**分类出来的那些名字。
        # 上一版内层循环借用了 rows 这个名字，把它换成了"最后一格的列表"，
        # 于是产物里的 rows 少到 1 行，而格子看起来一切正常。
        named_runtime = sorted(a["name"] for a in assets
                               if classify(a["name"], release)["kind"] == "runtime")
        if sorted(r["asset"] for r in engines[engine]["rows"]) != named_runtime:
            problems.append(f"{engine}: rows 与分类出来的 runtime 名字对不上"
                            f"（{len(engines[engine]['rows'])} 行 vs {len(named_runtime)} 个）"
                            " —— 有变量被内层循环换了名，或某条分支漏了")
        floor = BACKEND_FLOOR[engine]
        have = len(engines[engine]["backends"])
        if have < floor:
            problems.append(f"{engine}: 只有 {have} 种后端（地板 {floor}）—— "
                            "上游撤档 = 分层那句要重写，这里不许默默过")

    by_asset = {r["asset"]: r for e in engines.values() for r in e["rows"]}
    gaps, gaps_seed = [], []
    unassigned = [{"engine": e, **r} for e, v in engines.items() for r in v["platform_not_in_name"]]
    for p in picks:
        got = by_asset.get(p["asset"])
        if not got:
            problems.append(f"{p['engine']}/{p['os']}: payload-plan 选中的 {p['asset']} "
                            "在这张表里不存在 —— 两份产者对同一个 release 的认知已经漂了")
            continue
        if "sha256:" + got["sha256"] != p["digest"]:
            problems.append(f"{p['asset']}: 矩阵里的 digest 与 payload-plan 钉的不等 —— 先查哪份是旧的")
        if got["os"] is None:
            # 名字里不写平台（whisper 的 bin-x64 / blas-bin-x64 就是这样）。那只能由
            # payload-plan 自己标 inferred_platform —— 标了才算诚实，没标就是红。
            unassigned.append({"engine": p["engine"], "asset": p["asset"], "mib": got["mib"]})
            if not p.get("inferred_platform"):
                problems.append(f"{p['asset']}: 名字里没有平台，payload-plan 却把它当 {p['os']} 用"
                                " 且没标 inferred_platform —— 推断要写明是推断")
            continue
        if got["os"] != p["os"]:
            problems.append(f"{p['asset']}: 分出的 OS 是 {got['os']}，payload-plan 却把它当 {p['os']} 用")
    # 两个产者会点名同一颗：行侧（这张表里所有名字不写平台的 runtime）与 pick 侧
    # （payload-plan 选中且名字不写平台的那些）。缺口是"哪几颗不能靠名字挑"，
    # 数的是颗，不是点名次数 —— 不去重就会把 2 颗写成 3 颗。
    unassigned = [{(r["engine"], r["asset"]): r for r in unassigned}[k]
                  for k in sorted({(r["engine"], r["asset"]) for r in unassigned})]
    if unassigned:
        gaps_seed.append({"engine": "whisper", "os": "win-x64 与 win-x86",
                          "kind": "platform_not_in_name",
                          "assets": sorted(r["asset"] for r in unassigned if r["engine"] == "whisper"),
                          "because": "上游这些包的名字不写平台（bin-x64 / blas-bin-x64 / Win32），"
                                     "所以外壳不能靠资产名挑后端；payload-plan 那边按条目表推断并标了 "
                                     "inferred_platform=true，这张表原样记下来，不改写成「已知」"})
    if "macos-arm64" not in engines.get("whisper", {}).get("by_os", {}):
        gaps.append({"engine": "whisper", "os": "macos-arm64",
                     "because": "上游这个 release 没有 mac 的 runtime 资产，只有 xcframework（库）",
                     "task": "#29"})
    for e, body in engines.items():
        for o, cells in body["by_os"].items():
            if "unknown_from_name" in cells:
                gaps.append({"engine": e, "os": o, "kind": "backend_not_in_name",
                             "because": "资产名不标后端，mac 上有没有加速不能由这张表断言",
                             "claim": "none"})
    non_whisper = sorted(r["asset"] for r in unassigned if r["engine"] != "whisper")
    if non_whisper:
        problems.append(f"这些 runtime 的名字里不写平台，但不在已记的缺口里：{non_whisper}"
                        " —— 新增一类就要在缺口里说一声")
    gaps += gaps_seed
    gaps.append({"engine": "oar-ocr", "os": "全部",
                 "because": "文字识别没有上游预编译产物，它的加速轴是 Cargo feature/EP，"
                            "不在这张表里（证据：pins/ocr-build-surface.json 与 oar-ocr release 产物表）"})
    stats = {"捕获资产": sum(v["assets_total"] for v in engines.values()),
             "runtime 行": sum(v["runtimes"] for v in engines.values()),
             "不是 runtime 的资产": sum(len(v["not_runtime"]) for v in engines.values()),
             "引擎": len(engines),
             "后端种类": sorted({b for v in engines.values() for b in v["backends"]})}
    return {"schema": 1,
            "rule": {"classify": "只按上游资产名分；分不出来就报，不静默归类，也不替上游补默认值",
                     "spelling": "同一个后端可能有两种拼法（实测 sd 的 CUDA 同时写 cuda12 与 cu12），"
                                 "分类表必须都认，少一种就是漏一档加速",
                     "cudart": "llama/sd 的 CUDA 另有 cudart- 前缀的伴生包（把 CUDA 运行时一起装），"
                               "体积差 100 MiB 以上 —— 选哪一份是安装量决策，不是命名细节",
                     "macos": "macOS 的资产名不标后端：这张表只说 unknown_from_name，不断言 Metal",
                     "purpose": "这张表给约束第 5 条（按检测到的后端分层）与第 3 条（runtime 可替换）"
                                "当输入；外壳换后端 = 在这张表里换一个 asset，并按 sha256 校验"},
            "from": "GitHub release 的 asset 表（name / size / digest=sha256），按 payload-plan 钉的 ref 取",
            "engines": engines, "gaps": gaps, "stats": stats}, problems


def picks_from_payload(path: str) -> tuple[list[dict], dict]:
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    picks = [r for rows in d["os"].values() for r in rows if r.get("engine")]
    repos = {}
    for r in picks:
        m = re.search(r"github\.com/([^/]+/[^/]+)/", r["url"])
        if m:
            repos[r["engine"]] = m.group(1)
    return picks, repos


def check(committed: dict, fresh: dict) -> list[str]:
    bad = []
    if committed.get("rule") != fresh.get("rule"):
        bad.append("分法变了（什么算后端、拼法怎么认、mac 那句怎么说）")
    if committed.get("stats") != fresh.get("stats"):
        bad.append(f"覆盖度变了 {committed.get('stats')} → {fresh['stats']}")
    if committed.get("gaps") != fresh.get("gaps"):
        bad.append(f"缺口集合变了：{committed.get('gaps')} → {fresh.get('gaps')}"
                   "（上游补了 mac 的产物或改了拼法，这两句话都要重看）")
    for e, body in fresh["engines"].items():
        old = committed["engines"].get(e)
        if not old:
            bad.append(f"{e}: 新增了一个引擎，但已提交那张没有")
            continue
        if old.get("release") != body["release"]:
            bad.append(f"{e}: 钉的 release 变了 {old.get('release')} → {body['release']}")
        if old.get("backends") != body["backends"]:
            bad.append(f"{e}: 后端种类变了 {old.get('backends')} → {body['backends']}"
                       " —— 分层那句话的可选面跟着变")
        if old.get("格子条目") != body["格子条目"]:
            bad.append(f"{e}: 格子里的条目数变了 {old.get('格子条目')} → {body['格子条目']}"
                       "（一格一行的写法会静默丢同后端的其它版本）")
        if old.get("rows") != body["rows"]:
            bad.append(f"{e}: 资产表内容变了（体积或 digest 有出入），重看 payload-plan 的选择")
    return bad


def selftest() -> int:
    bad = []
    # ① 分类器自己：每一档都有正反两面。
    cases = [
        ("llama-b11727-bin-win-cuda-12.4-x64.zip", "win-x64", "cuda", "12.4"),
        ("sd-master-95b3090-bin-win-cuda12-x64.zip", "win-x64", "cuda", "12"),
        ("cudart-sd-bin-win-cu12-x64.zip", None, None, None),
        ("sd-master-95b3090-bin-Linux-Ubuntu-24.04-x86_64-rocm-10.1.0.zip", "linux-x64", "rocm", "10.1.0"),
        ("llama-b11727-bin-ubuntu-sycl-fp16-x64.tar.gz", "linux-x64", "sycl", "16"),
        ("llama-b11727-bin-win-opencl-adreno-arm64.zip", "win-arm64", "opencl-adreno", None),
        ("whisper-blas-bin-x64.zip", None, "blas", None),
        ("llama-b11727-bin-macos-arm64.tar.gz", "macos-arm64", "unknown_from_name", None),
        ("llama-b11727-ui.tar.gz", None, None, None),
        ("whisper-b5454-xcframework.zip", None, None, None),
    ]
    for name, want_os, want_backend, want_version in cases:
        c = classify(name, "b11727" if name.startswith("llama") else
                     "b5454" if name.startswith("whisper") else "master-975-95b3090")
        if want_backend is None:
            if c["kind"] == "runtime":
                bad.append(f"{name}: 应当不是 runtime 资产，却被分了（kind={c['kind']}）")
            continue
        if c["os"] != want_os or c["backend"] != want_backend or c["version"] != want_version:
            bad.append(f"{name}: 分错 → os={c['os']} backend={c['backend']} v={c['version']}"
                       f"（期望 {want_os}/{want_backend}/{want_version}）")
    # ② 负向对照：上游改拼法时必须红，而不是归到 cpu。
    fake = classify("llama-b11727-bin-ubuntu-mystery-x64.tar.gz", "b11727")
    if fake["backend"] != "unknown_token":
        bad.append(f"假后端名被认成 {fake['backend']} / {fake['note']} —— 分类表成了橡皮图章")
    if fake["os"] != "linux-x64":
        bad.append(f"假后端名那条把平台也弄丢了：{fake['os']}")
    # ③ 真实捕获必须全部分得出来（这一条就是"上游改名"的哨兵）。
    with open(captures_path(), encoding="utf-8") as fh:
        captures = json.load(fh)
    with open(PAYLOAD, encoding="utf-8") as fh:
        picks = [r for rows in json.load(fh)["os"].values() for r in rows if r.get("engine")]
    _p2, repos2 = picks_from_payload(PAYLOAD)
    doc, problems = build(captures, picks, repos2)
    if problems:
        bad += problems
    if len(cases) and doc["stats"]["捕获资产"] != 57:
        bad.append(f"捕获里的资产数不再是 57（{doc['stats']['捕获资产']}）—— 上游动过 release，重看这张表")
    # 同一个格子里有多个版本时必须都在，不是"最后一个赢"。
    win_cuda = [r["version"] for r in doc["engines"]["llama"]["by_os"]["win-x64"]["cuda"]]
    if sorted(win_cuda) != ["12.4", "13.4"]:
        bad.append(f"llama/win-x64 的 CUDA 版本应当两颗都在，现在是 {win_cuda}")
    if doc["engines"]["whisper"]["by_os"]["win-x64"]["cuda"]:
        wc = sorted(r["version"] for r in doc["engines"]["whisper"]["by_os"]["win-x64"]["cuda"])
        if wc != ["11.8.0", "12.4.0"]:
            bad.append(f"whisper/win-x64 的 CUDA 版本不对：{wc}")
    entries = sum(v["格子条目"] for v in doc["engines"].values())
    if entries != 45:
        bad.append(f"格子条目总数应是 45（47 个 runtime 减 2 个名字里不写平台的），现在 {entries}")
    # ④ 每个引擎的后端地板 + mac 那句不许被写成有后端。
    for e, floor in BACKEND_FLOOR.items():
        have = len(doc["engines"][e]["backends"])
        if have < floor:
            bad.append(f"{e} 只有 {have} 种后端，低于地板 {floor}")
    kinds = sorted({g.get("kind") or "?" for g in doc["gaps"]})
    if "backend_not_in_name" not in kinds or "platform_not_in_name" not in kinds:
        bad.append(f"缺口种类少了：{kinds} —— mac 不写后端、whisper 不写平台这两句必须都在")
    if not any(g.get("task") == "#29" for g in doc["gaps"]):
        bad.append("whisper 在 mac 上没有 runtime 这件事不在缺口里 —— #29 的数据面丢了")
    # ⑤ 选中的那颗必须能被这张表复现（两个产者不许各说一套）。
    by_asset = {r["asset"]: r for v in doc["engines"].values() for r in v["rows"]}
    # 10 = cudart 伴生包 7 + xcframework 2 + webUI 1（都是量出来的，不是印象）
    if doc["stats"]["不是 runtime 的资产"] != 10:
        bad.append(f"不是 runtime 的资产应是 10（7 个 cudart 伴生 + 2 个 xcframework + 1 个 webUI），"
                   f"现在 {doc['stats']['不是 runtime 的资产']} —— 分类或计数漂了")
    kinds = {}
    for v in doc["engines"].values():
        for r in v["not_runtime"]:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    if kinds != {"cudart_companion": 7, "library_only": 2, "not_a_runtime": 1}:
        bad.append(f"非 runtime 那 10 个的分类不对：{kinds}")
    for p in picks:
        got = by_asset.get(p["asset"])
        ok_os = got and (got["os"] == p["os"] or (got["os"] is None and p.get("inferred_platform")))
        if not got or "sha256:" + got["sha256"] != p["digest"] or not ok_os:
            bad.append(f"payload-plan 选的 {p['asset']} 在这张表里对不上：{got}")
    print("---- 结论 ----  ", doc["stats"], "；macOS 那句 =",
          "unknown_from_name" in doc["engines"]["llama"]["backends"],
          "；缺口 =", sorted({g.get("kind") or "?" for g in doc["gaps"]}))
    return report(bad)


def fetch(repos: dict, releases: dict, dest: str) -> dict:
    """按钉住的 ref 现取一次产物表（车道用；离线判据不吃它）。

    只要 name / size / digest 三个字段 —— 与夹具同一形状，所以 `--check --captures <现取的>`
    就是"上游今天还发这些档吗"的哨兵：加档、撤档、改拼法都会红，而不会悄悄过期。
    """
    import urllib.request
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("ACTIONS_TOKEN")
    out = {}
    for engine, repo in sorted(repos.items()):
        tag = releases[engine]
        url = f"https://api.github.com/repos/{repo}/releases/tags/{tag}"
        req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                                   "User-Agent": "local-ai-studio-backends"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = json.loads(r.read().decode("utf-8"))
        except Exception as exc:                        # noqa: BLE001
            raise SystemExit(f"BAD: 取不到 {repo}@{tag} 的 release 表（{type(exc).__name__}: {exc}）"
                             " —— 取数失败不能算核对过") from exc
        out[f"{repo}@{tag}"] = [{"name": a["name"], "size": a["size"],
                                 "sha256": (a.get("digest") or "").removeprefix("sha256:"),
                                 "id": a["id"]} for a in body.get("assets", [])]
    missing = {k: 0 for k, v in out.items() if not v}
    if missing:
        raise SystemExit(f"BAD: 这些 ref 回了一个空产物表：{sorted(missing)} —— 空表不是核对")
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"现取到 {sum(len(v) for v in out.values())} 个资产，写进 {dest}")
    return out


def report(bad: list[str]) -> int:
    for x in bad:
        print("  BAD:", x)
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="backends.py")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--captures", default=captures_path())
    ap.add_argument("--fetch", action="store_true",
                    help="按 payload-plan 钉的 ref 现取一次产物表，写进 --captures 那个路径")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    picks, repos = picks_from_payload(PAYLOAD)
    if a.fetch:
        with open(PAYLOAD, encoding="utf-8") as fh:
            rel = {r["engine"]: r["release"] for rows in json.load(fh)["os"].values()
                   for r in rows if r.get("engine")}
        fetch(repos, rel, a.captures)
    with open(a.captures, encoding="utf-8") as fh:
        captures = json.load(fh)
    doc, problems = build(captures, picks, repos)
    if a.check:
        with open(a.out, encoding="utf-8") as fh:
            committed = json.load(fh)
        bad = check(committed, doc) + problems
        for x in bad:
            print("BAD:", x)
        print(f"比对了 {len(doc['engines'])} 个引擎 / {doc['stats']['捕获资产']} 个资产，问题 {len(bad)} 条")
        return 1 if bad else 0
    for x in problems:
        print("BAD:", x)
    if problems:
        print("有 BAD → 不落盘")
        return 1
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    print("落盘", a.out, doc["stats"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
