#!/usr/bin/env python3
"""每个 runtime 包里"外壳要点名执行的那个 server 在不在场"——换后端的前提。

换档表（`pins/backend-choice.json`）说"换后端 = 换一个 asset"，可那一句的前提是
**每一档都带着同一个可执行文件**。上游并不保证这件事：CUDA / Vulkan / ROCm 各自是一份
独立构建，少了 `llama-server` 就只有 CLI，而 CLI 与常驻服务是两种活法（约束第 4 条）。
所以这句话必须有表可查，不能是印象。

只读元数据，不违反"禁止本机编译与运行"：zip 用 `tools/zip_peek.py` 的 HTTP Range 读
**中央目录**（每颗约 1 MiB 尾部，不整包下载、不解压、不执行），拿到的就是成员文件名表。
`.tar.gz` 读不了中央目录（gzip 流不可随机寻址，整包流式读是计费流量），一律标
`not_read` 并说清"要证明它得走哪条路" —— 缺席与未知不许混成一件事。

server 的名字**只从 `launch_plan.SERVER_BASE` 取**，这张表里不许出现第二个定义
（测试用一条 ast 扫描盯着）。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
MATRIX = os.path.join(ROOT, "pins", "backend-matrix.json")
PLAN = os.path.join(ROOT, "pins", "payload-plan.json")
CAPTURES = os.path.join(ROOT, "tests", "fixtures", "parity-captures.json")
OUT = os.path.join(ROOT, "pins", "runtime-parity.json")

sys.path.insert(0, HERE)
import launch_plan as lp  # noqa: E402  可执行文件名字的唯一来源


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def server_name(engine: str, os_key: str) -> str:
    """launch_plan 那张表 + Windows 要带 .exe。别的名字这里都不许出现。"""
    base = lp.SERVER_BASE[engine]
    return base + (".exe" if os_key.startswith("win") else "")


def rows_from_matrix(matrix: dict) -> list[dict]:
    out = []
    for e, body in matrix["engines"].items():
        for r in body["rows"]:
            out.append({"engine": e, "repo": body["repo"], "release": body["release"],
                        "os": r["os"], "backend": r["backend"], "version": r["version"],
                        "asset": r["asset"], "mib": r["mib"], "sha256": r["sha256"]})
    return sorted(out, key=lambda r: (r["engine"], str(r["os"]), r["backend"], r["asset"]))


def shipped_defaults(plan: dict) -> dict:
    """payload-plan 真装的那几颗：(engine, os) -> asset。这一档必须有 server，硬要求。"""
    return {(r["engine"], o): r["asset"] for o, rows in plan["os"].items()
            for r in rows if r.get("engine")}


def verified_bytes_of(plan: dict) -> dict:
    """tar.gz 只能在取回整包后读成员表，所以这一条**必须**建在字节与哈希都对得上的包上：
    体积与 digest 直接取 payload-plan 已经量过的 `verified_bytes`，不在这里重打数字。"""
    out = {}
    for rows in plan["os"].values():
        for r in rows:
            v = r.get("verified_bytes") or {}
            if r.get("asset") and v.get("bytes") and v.get("sha256"):
                out[r["asset"]] = {"bytes": v["bytes"], "sha256": v["sha256"]}
    return out


def summarize(names: list[str], engine: str, os_key: str) -> dict:
    """成员文件名表 → 这台引擎的可执行文件与库闭包形状。"""
    bases = [n.replace("\\", "/").rsplit("/", 1)[-1] for n in names]
    want = server_name(engine, os_key)
    stem = want[:-4] if want.endswith(".exe") else want
    exes = sorted({b for b in bases if b.lower().endswith(".exe")})
    noext = sorted({b for b in bases if "." not in b and not b.endswith("/")})
    bins = exes or [b for b in noext if b.startswith(stem)]
    libs = {}
    for b in bases:
        for ext in (".dll", ".so", ".dylib"):
            if b.endswith(ext):
                libs[ext] = libs.get(ext, 0) + 1
    has_server = want in bases
    # llama 的 win 包是 `x.exe` + `x-impl.dll` 成对的（条目表量出来的）；whisper/sd 的依赖是
    # ggml/whisper 这类共享库，不套这条判据 —— 那个形状只对 llama 被量过。
    impl = ((stem + ".dll") in bases or (stem + "-impl.dll") in bases) if engine == "llama" else None
    return {"expect_server": want, "has_server": has_server,
            "server_sibling_lib": impl,
            "executables": bins, "libs": libs,
            "entries": len(names),
            "test_hits": sorted(b for b in bases if b.startswith("test-"))}


def build(captures: dict, matrix: dict, plan: dict) -> tuple[dict, list[str]]:
    problems: list[str] = []
    defaults = shipped_defaults(plan)
    os_of_asset = {r["asset"]: o for o, rows in plan["os"].items() for r in rows}
    # 落地体积这件事 payload-plan 早就单独量过一次（那 8 颗默认档的 uncompressed_mib）。
    # 两个生产者各量一遍再对表，比只信自己强。
    landed_expected = {r["asset"]: r["uncompressed_mib"] for rows in plan["os"].values()
                       for r in rows if r.get("asset") and r.get("uncompressed_mib")}
    rows = rows_from_matrix(matrix)
    assets: list[dict] = []
    for r in rows:
        item = dict(r)
        # 名字里不写平台的那两颗（whisper 的 bin-x64 / blas-bin-x64）：OS 从 payload-plan 取，
        # 那是数据不是猜；plan 里没有这颗就连 OS 都不知道，只能标未知。
        os_key = r["os"] if r["os"] is not None else os_of_asset.get(r["asset"])
        item["os_named"] = r["os"] is not None
        item["os_resolved"] = os_key
        item["is_default"] = os_key is not None and defaults.get((r["engine"], os_key)) == r["asset"]
        item["expect_server"] = None if os_key is None else server_name(r["engine"], os_key)
        cap = captures.get(r["asset"])
        is_tar = not r["asset"].endswith(".zip")
        if cap is None:
            if not is_tar:
                problems.append(f"{r['asset']}: 是 zip 却没有捕获 —— 这张表说它存在而捕获里没有，"
                                "两份产者已经漂了（不许把没读到当成读过了）")
                continue
            if item["is_default"]:
                problems.append(f"{r['asset']}: 真装的默认档却是 tar.gz 且捕获里没有它 —— 这一档"
                                "起不起来没人证过（读它必须把整包字节与上游 digest 都对上）")
            item["read"] = False
            item["read_via"] = "tar_stream"
            item["why_not_read"] = ("gzip 流不可随机寻址，读成员表要付整包流量；这一档不是真装的那一颗，"
                                    "所以不为它付 —— server 在场与否未证，要证就出包车道解包后点名")
            assets.append(item)
            continue
        if os_key is None:
            item.update({"read": False, "read_via": "zip_unnamed_os",
                         "why_not_read": "包名里不写平台，而 payload-plan 也没这一颗 —— "
                                         "不知道该按哪个 OS 的可执行文件名去找"})
            assets.append(item)
            continue
        if "unpacked_total" not in cap:
            problems.append(f"{r['asset']}: 捕获里没有 unpacked_total —— 那是旧版 --fetch 读的，"
                            "只有名字与压缩后合计，量不出落地体积。重跑 --fetch 再对表")
            item["read"] = False
            item["read_via"] = "capture_missing_sizes"
            item["why_not_read"] = "捕获缺解压后大小这一项；不假装知道落地体积"
            assets.append(item)
            continue
        s = summarize(cap["names"], r["engine"], os_key)
        item.update(s)
        if cap.get("reader") == "tar_stream":
            size_mib = round(cap["bytes"] / 1048576, 1)
            item.update({"read": True, "read_via": "tar_stream", "archive_bytes": cap["bytes"],
                         "sha256_verified": bool(cap["digest_matches"]), "packed_sum_mib": None,
                         "landed_mib": round(cap["unpacked_total"] / 1048576, 1)})
            if not cap["digest_matches"]:
                problems.append(f"{r['asset']}: 整包 sha256 与上游 release digest 不等 —— "
                                "成员表是从另一个包读的，不能算这一档的证据")
        else:
            size_mib = round(cap["packed_total"] / 1048576, 1)
            item.update({"read": True, "read_via": "zip_central_directory",
                         "packed_sum_mib": size_mib,
                         "landed_mib": round(cap["unpacked_total"] / 1048576, 1)})
        if s["entries"] == 0:
            problems.append(f"{r['asset']}: 解析出 0 条成员 —— 读法坏了，不是包空了")
        if r["mib"] is not None and abs(size_mib - r["mib"]) > 1.5:
            problems.append(f"{r['asset']}: 读到的体积 {size_mib} MiB 与矩阵声明的 "
                            f"{r['mib']} MiB 差过 1.5 —— 量的不是同一份东西")
        landed = item.get("landed_mib")
        if landed is not None:
            if landed <= 0:
                problems.append(f"{r['asset']}: 落地体积 {landed} MiB 不是正数 —— 成员表里的解压后大小读坏了")
            if item["read_via"] == "zip_central_directory" and landed < item["packed_sum_mib"]:
                problems.append(f"{r['asset']}: 落地体积 {landed} MiB 比压缩后 {item['packed_sum_mib']} MiB 还小"
                                " —— 这两件事不可能同时成立，读法坏了")
            want = landed_expected.get(r["asset"])
            if want is not None and abs(landed - want) > 0.2:
                problems.append(f"{r['asset']}: 这张表量到落地 {landed} MiB，payload-plan 量的是 {want} MiB "
                                "—— 两份产者对同一个包的成员表不一致，先查谁读坏了")
        if not s["has_server"] and item["is_default"]:
            problems.append(f"{r['asset']}: 这是 payload-plan 真装的那一颗，可成员表里没有 "
                            f"{s['expect_server']} —— 装上起不来")
        assets.append(item)
    gaps = [{"engine": a["engine"], "os": a["os_resolved"], "backend": a["backend"],
             "asset": a["asset"], "missing": a["expect_server"],
             "because": "这一档的成员表里没有外壳要点名执行的那个 server（上游这档只发了 CLI 或另有名）"
                        "—— 换到这一档不是换个文件就行"}
            for a in assets if a.get("read") and not a["has_server"] and not a["is_default"]]
    not_read = [{"engine": a["engine"], "os": a["os_resolved"], "backend": a["backend"],
                 "asset": a["asset"], "expect_server": a["expect_server"],
                 "read_via": a["read_via"]}
                for a in assets if not a.get("read")]
    # 地板：每个"真装的" 引擎×OS 都必须已被证过（在捕获里、读到了成员表）。
    for (e, o), asset in defaults.items():
        a = next((x for x in assets if x["asset"] == asset), None)
        if a is None:
            problems.append(f"{e}/{o}: 默认档 {asset} 在捕获后不见了 —— 这张表没法说它起不起来")
    stats = {"runtime 资产": len(rows), "读到成员表": sum(1 for a in assets if a.get("read")),
             "没读要走出包车道": len(not_read),
             "有 server": sum(1 for a in assets if a.get("has_server")),
             "缺 server 的可选档": len(gaps), "默认档": len(defaults),
             "包名不写平台": sum(1 for a in assets if not a["os_named"])}
    doc = {"schema": 1,
           "rule": {"server_name": "可执行文件名只从 tools/launch_plan.py 的 SERVER_BASE 取，"
                                   "Windows 加 .exe；这张表里没有第二个定义",
                    "reader": "zip 用 Range 读中央目录（约 1 MiB 尾部，不整包下载、不解压、不运行）；"
                              "tar.gz 不可随机寻址，只能取整包读成员表 —— 这一层只为 payload-plan 真装的"
                              "默认档付那份流量，且取回的字节数与整包 sha256 都按 payload-plan 已量过的数复核",
                    "cost": "16 颗可选档的 tar.gz 不读：不为没人会下载的那一颗付整包流量，"
                            "如实标未证（要证就走出包车道解包后点名）",
                    "absent_vs_unknown": "读到成员表而里面没 server = 缺（进 gaps 点名）；"
                                         "没读到 = 未知（进 not_read），两者不许混",
                    "landed": "landed_mib 是成员表里逐个成员解压后大小之和（不含目录项）——"
                              "换后端要多占多少盘就取这一列；它与 payload-plan 独立量过的 "
                              "uncompressed_mib 逐颗对表，差过 0.2 MiB 就红",
                    "hard": "payload-plan 真装的那几颗必须有 server —— 装上起不来就是红",
                    "impl_pair": "llama 的 Windows 包是 x.exe + x-impl.dll 成对的（条目表量出来的），"
                                 "这一列只对 llama 求值；whisper/sd 的依赖形状没被这样量过，写 null 不写 false"},
           "from": "pins/backend-matrix.json 的 runtime 行 + zip 中央目录捕获 + payload-plan 的默认档",
           "assets": assets, "server_gaps": gaps, "not_read": not_read, "stats": stats}
    return doc, problems


def drift(committed: dict, fresh: dict) -> list[str]:
    bad = []
    if committed.get("rule") != fresh.get("rule"):
        bad.append("口径变了（名字从哪取、什么算读到、缺席与未知怎么分）")
    if committed.get("stats") != fresh.get("stats"):
        bad.append(f"覆盖度变了 {committed.get('stats')} → {fresh.get('stats')}")
    if committed.get("server_gaps") != fresh.get("server_gaps"):
        bad.append(f"缺 server 的可选档变了 {committed.get('server_gaps')} → "
                   f"{fresh.get('server_gaps')} —— 上游某一档换了构建形态")
    if [c.get("read_via") for c in committed.get("not_read", [])] != \
            [c.get("read_via") for c in fresh.get("not_read", [])]:
        bad.append("「没读到、要走出包车道」那份清单的读法变了 —— 这一层的边界改了")
    old = {a["asset"]: a for a in committed["assets"]}
    for a in fresh["assets"]:
        o = old.get(a["asset"])
        if o is None:
            bad.append(f"{a['asset']}: 新增资产，已提交那张没有")
            continue
        for f in ("expect_server", "has_server", "read", "read_via", "entries",
                  "server_sibling_lib", "packed_sum_mib", "archive_bytes", "sha256_verified",
                  "landed_mib", "libs", "is_default", "os_resolved"):
            if o.get(f) != a.get(f):
                bad.append(f"{a['asset']}: {f} 变了 {o.get(f)} → {a.get(f)}")
    return bad


def fetch(rows: list[dict], path: str, plan: dict) -> None:
    """增量落盘：这个 CN 出口会在半路 TLS 断（实测过），一次抓取十几分钟，
    全部攒在内存里最后才写 = 一断就前功尽弃。每读一颗就写一次，重跑自动接上。

    zip 用 Range 读中央目录；tar.gz **只读真装的那几颗默认档**，且体积与整包哈希都按
    payload-plan 已经量过的数复核 —— 不为一个没人会下载的可选档付整包流量。
    """
    zp = _load("zip_peek")
    pp = _load("payload_plan")
    verified = verified_bytes_of(plan)
    defaults = set(shipped_defaults(plan).values())
    cap = load(path) if os.path.exists(path) else {}
    for r in rows:
        asset = r["asset"]
        url = f"https://github.com/{r['repo']}/releases/download/{r['release']}/{asset}"
        if asset in cap and "unpacked_total" in cap[asset]:
            continue
        if asset.endswith(".zip"):
            entries = zp.entries(url)
            cap[asset] = {"url": url, "reader": "zip_central_directory",
                          "names": [x["name"] for x in entries],
                          "packed_total": sum(x["packed"] for x in entries if not x["dir"]),
                          "unpacked_total": sum(x["size"] for x in entries if not x["dir"])}
            print("捕获", asset, len(entries), "条")
        elif asset in defaults and asset in verified:
            got, meta = pp.read_tar_entries(url, verified[asset]["bytes"],
                                            verified[asset]["sha256"])
            cap[asset] = {"url": url, "reader": "tar_stream",
                          "names": [x["name"] for x in got],
                          "unpacked_total": sum(x["size"] for x in got if not x["isdir"]),
                          "bytes": meta["bytes"],
                          "sha256": meta["sha256"], "digest_matches": meta["digest_matches"],
                          "packed_total": None}
            print("捕获(tar)", asset, len(got), "条", meta["bytes"], "B",
                  "digest 对上了" if meta["digest_matches"] else "digest 不符")
        else:
            continue
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(cap, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
    print("落盘捕获", path, len(cap), "颗")


def selftest() -> int:
    bad: list[str] = []
    matrix, plan = load(MATRIX), load(PLAN)
    caps = load(CAPTURES)
    rows = rows_from_matrix(matrix)
    doc, problems = build(caps, matrix, plan)
    if problems:
        bad.append(f"真输入应当 0 问题，实有 {len(problems)}：{problems[:2]}")
    if doc["stats"]["读到成员表"] < 20:
        bad.append(f"zip 应该有 20+ 颗读到成员表，现在 {doc['stats']['读到成员表']} —— "
                   "捕获没在场就等于这一层没跑")
    if doc["stats"]["runtime 资产"] != matrix["stats"]["runtime 行"]:
        bad.append("资产数必须等于矩阵的 runtime 行数，现在不等 —— 这一层在偷偷丢行")
    # 实测的数（2026-10-20 那一份捕获）：30 读到 / 17 读不到 / 0 档缺 server。
    # "换到任意一档都起得来"与"真装的那 8 颗一定起得来"两句都靠这些数撑着。
    if (doc["stats"]["读到成员表"], doc["stats"]["没读要走出包车道"],
            doc["stats"]["缺 server 的可选档"]) != (30, 17, 0):
        bad.append(f"实测应是 30 读到 / 17 读不到 / 0 档缺 server，现在 "
                   f"{doc['stats']['读到成员表']}/{doc['stats']['没读要走出包车道']}/"
                   f"{doc['stats']['缺 server 的可选档']}")
    if any(not (a.get("read") and a.get("has_server")) for a in doc["assets"] if a["is_default"]):
        bad.append("8 颗真装的默认档必须逐颗证过 server —— 缺一个就是装了起不来")
    if doc["stats"]["有 server"] != doc["stats"]["读到成员表"]:
        bad.append("读到的每一颗都必须带着 server —— 有缺的就得进 gaps 点名，不许只算总数")
    llama_win = [a for a in doc["assets"] if a["engine"] == "llama"
                 and str(a["os_resolved"]).startswith("win")]
    if not llama_win or not all(a["server_sibling_lib"] for a in llama_win):
        bad.append("llama 的 Windows 包是 x.exe + x-impl.dll 成对的，这条形状现在不成立了")
    # ① 把默认档那一颗的 server 从成员表里删掉，必须红（装上起不来是最贵的错）。
    dflt = doc["assets"][next(i for i, a in enumerate(doc["assets"])
                              if a.get("read") and a["is_default"])]
    c1 = json.loads(json.dumps(caps))
    c1[dflt["asset"]]["names"] = [n for n in c1[dflt["asset"]]["names"]
                                  if not n.endswith(dflt["expect_server"])]
    if not any("装上起不来" in x for x in build(c1, matrix, plan)[1]):
        bad.append("默认档没有 server 时没红")
    # ② 正向对照：同一颗删掉的若是**可选档**的 server，不该红，但必须进 gaps 点名。
    alt = [a for a in doc["assets"] if a.get("read") and not a["is_default"]]
    c2 = json.loads(json.dumps(caps))
    victim = alt[0]
    c2[victim["asset"]]["names"] = [n for n in c2[victim["asset"]]["names"]
                                    if not n.endswith(victim["expect_server"])]
    d2, p2 = build(c2, matrix, plan)
    if p2:
        bad.append(f"可选档少 server 不该算问题，实有 {p2[:1]}")
    if not any(g["asset"] == victim["asset"] for g in d2["server_gaps"]):
        bad.append("可选档少 server 没被点名 —— 那 gaps 是装饰")
    # ③ 捕获里凭空少一颗 zip 必须红（不许把没读到当成读过了）。
    c3 = json.loads(json.dumps(caps))
    c3.pop(victim["asset"])
    if not any("是 zip 却没有捕获" in x for x in build(c3, matrix, plan)[1]):
        bad.append("捕获少一颗时没红")
    # ④ 中央目录体积与矩阵声明差太远必须红（量的不是同一份东西）。
    c4 = json.loads(json.dumps(caps))
    c4[victim["asset"]]["packed_total"] = 10
    if not any("差过 1.5" in x for x in build(c4, matrix, plan)[1]):
        bad.append("体积对不上时没红")
    # ⑤ 成员表解析出 0 条必须红（读法坏了不能报成"包是空的"）。
    c5 = json.loads(json.dumps(caps))
    c5[victim["asset"]]["names"] = []
    if not any("解析出 0 条" in x for x in build(c5, matrix, plan)[1]):
        bad.append("0 条成员时没红")
    # ⑥ 没读到的 tar 不许被算成"缺 server"；读到的 tar 必须逐颗 digest 对上。
    unread_tar = {a["asset"] for a in doc["assets"]
                  if a["asset"].endswith(".tar.gz") and not a.get("read")}
    if len(unread_tar) != 16:
        bad.append(f"未读的 tar 应有 16 颗（19 减掉 3 颗读过的默认档），现在 {len(unread_tar)} "
                   "—— 整包流量只给默认档这条门槛被改了")
    if any(a["asset"] in unread_tar for a in doc["server_gaps"]):
        bad.append("没读到的 tar 被写成了「缺 server」—— 未知与缺席混成一件事")
    read_tar = [a for a in doc["assets"] if a.get("read_via") == "tar_stream" and a.get("read")]
    if len(read_tar) != 3 or not all(a["sha256_verified"] for a in read_tar):
        bad.append(f"读到的 tar 应是 3 颗且逐颗 digest 对上，现在 "
                   f"{[(a['asset'], a.get('sha256_verified')) for a in read_tar]}")
    # ⑧ 名字里不写平台那两颗：OS 只能从 payload-plan 取，plan 里没有的那颗必须停在"未知"。
    unnamed = {a["asset"]: a for a in doc["assets"] if not a["os_named"]}
    if sorted(unnamed) != ["whisper-bin-x64.zip", "whisper-blas-bin-x64.zip"]:
        bad.append(f"包名不写平台的那两颗变了：{sorted(unnamed)}")
    if unnamed["whisper-bin-x64.zip"]["os_resolved"] != "win-x64" or \
            unnamed["whisper-blas-bin-x64.zip"]["read"] is not False:
        bad.append("OS 必须从 payload-plan 取；plan 里没有的那颗只能标未知，不许替它猜 win-x64")
    # ⑦ 已提交那张被手改一个 has_server，漂移判据必须叫。
    c7 = json.loads(json.dumps(doc))
    for a in c7["assets"]:
        if a.get("read") and not a["is_default"]:
            a["has_server"] = not a["has_server"]
            break
    if not any("has_server 变了" in x for x in drift(doc, c7)):
        bad.append("has_server 被手改时漂移判据没红")
    # ⑩ 整包 digest 对不上必须红：成员表是从另一个包读的，这一档就不算有证据。
    c10 = json.loads(json.dumps(caps))
    for v in c10.values():
        if v.get("reader") == "tar_stream":
            v["digest_matches"] = False
    if not any("成员表是从另一个包读的" in x for x in build(c10, matrix, plan)[1]):
        bad.append("tar 的整包 digest 不符时没红")
    # ⑪ tar 的整包体积与矩阵声明差太远必须红（读到的不是那一颗的量）。
    c11 = json.loads(json.dumps(caps))
    for v in c11.values():
        if v.get("reader") == "tar_stream":
            v["bytes"] = 1000
    if not any("差过 1.5" in x for x in build(c11, matrix, plan)[1]):
        bad.append("tar 体积对不上时没红")
    # ⑫ 落地体积与 payload-plan 独立量过的那次不一致，必须红（两份产者读的是同一个包）。
    c12 = json.loads(json.dumps(caps))
    for v in c12.values():
        v["unpacked_total"] = v["packed_total"] if v.get("packed_total") else v["bytes"]
    if not any("两份产者对同一个包的成员表不一致" in x for x in build(c12, matrix, plan)[1]):
        bad.append("落地体积与 payload-plan 对不上时没红")
    # ⑬ 落地比压缩后还小 = 读法坏了，必须红。
    c13 = json.loads(json.dumps(caps))
    for k, v in c13.items():
        if v.get("reader") == "zip_central_directory":
            v["unpacked_total"] = v["packed_total"] // 100
    if not any("还小" in x for x in build(c13, matrix, plan)[1]):
        bad.append("落地体积小于压缩后时没红")
    # ⑭ 三颗 tar 的落地体积也必须与 payload-plan 对得上（正向对照：真输入 0 问题已经覆盖了，
    #    这里数一下到底有几颗被两个生产者各量过一次）。
    both = [a for a in doc["assets"] if a.get("landed_mib") and a["is_default"]]
    if len(both) != 8:
        bad.append(f"8 颗默认档都该有两个生产者量的落地体积，现在 {len(both)}")
    print("---- 结论 ----", doc["stats"], f"BAD 数量 = {len(bad)}")
    for x in bad:
        print("BAD:", x)
    return 1 if bad else 0


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--captures", default=CAPTURES)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    matrix, plan = load(MATRIX), load(PLAN)
    rows = rows_from_matrix(matrix)
    if a.fetch:
        fetch(rows, a.captures, load(PLAN))
    caps = load(a.captures)
    doc, problems = build(caps, matrix, plan)
    if a.check:
        bad = drift(load(a.out), doc) + problems
        for x in bad:
            print("BAD:", x)
        print(f"比对了 {doc['stats']['runtime 资产']} 个 runtime 资产，读到成员表 "
              f"{doc['stats']['读到成员表']} 颗，问题 {len(bad)} 条")
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
