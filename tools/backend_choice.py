#!/usr/bin/env python3
"""「换哪一档、要多下多少」这张表 —— 矩阵给"有哪些档"，这张表给外壳该下哪一颗。

约束第 3 条（runtime 可替换）与第 5 条（按检测到的后端分层）要落到一次下载上，缺的
不是"有哪些后端"（`pins/backend-matrix.json` 已经答了），而是这三件事：
  ① 这一档默认下哪一颗 —— 从 `pins/payload-plan.json` 读，**不在这张表里重打数字**；
  ② 换成加速档要多下多少 —— 体积是量出来的（同一 engine×OS 内相减，每次核对重算），
     而 CUDA 那一档**还要配一颗 `cudart-` 伴生包**，实测 373–567 MiB 一颗，
     比 runtime 本体还大几倍：不写这一列，"换后端"这件事的安装量承诺就是错的；
  ③ 换过去还能不能退回 CPU —— 三种情况必须分开：名字里写着 cpu、
     CPU 包名字里不写平台（whisper 的 `bin-x64`）、名字根本不标后端（macOS）。

**这张表不说的**：这台机器的驱动能不能吃到某一档 CUDA。那要 `nvidia-smi` / 驱动版本，
属外壳（#25）的活；表里只列上游真发了哪些版本。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
MATRIX = os.path.join(ROOT, "pins", "backend-matrix.json")
PLAN = os.path.join(ROOT, "pins", "payload-plan.json")
PARITY = os.path.join(ROOT, "pins", "runtime-parity.json")
OUT = os.path.join(ROOT, "pins", "backend-choice.json")

sys.path.insert(0, HERE)
import backends as bd  # noqa: E402  同一份名字读者，不在这里另写一遍分类规则

CPU_STATES = ("named_cpu", "only_unnamed_asset", "name_does_not_say", "absent")
HEX64 = re.compile(r"^[0-9a-f]{64}$")

RULE = {
    "default": "默认那颗从 pins/payload-plan.json 读（含 digest），这张表只搬运不重打数字；"
               "那一行 status 不是 ok 就是上游没发，原样写出来不替它挑一颗",
    "pairing": "cudart- 伴生包按 (引擎, OS, CUDA 版本) 与 CUDA runtime 配；配到的行数不是 1 就红，"
               "配不上不静默 —— 进 cuda_rows_without_companion 并写明是"
               "「这张表里没有对应的伴生包」，不是「不需要」",
    "extra": "extra_mib = 这一颗的 mib - 这一格默认那颗的 mib，两边都取矩阵量出来的数，"
             "每次核对重算一遍（手抄的差值会跟着体积漂）",
    "cpu_fallback": "named_cpu=名字里写着 cpu；only_unnamed_asset=CPU 包名字里不写平台，"
                    "只能靠 payload-plan 的 inferred_platform；name_does_not_say=名字不标后端"
                    "（macOS），不断言有没有加速；absent=这一格真没有 cpu 档",
    "not_shipped": "payload-plan 没覆盖的 OS（android / linux-s390x / win-x86 …）这里只列档不装，"
                   "写 install=not_shipped，不假装它是要装的那一档",
    "landed": "landed_mib 与 disk_extra_mib_vs_default 从 pins/runtime-parity.json 取（那颗包逐个"
                              "成员解压后大小相加，并与 payload-plan 独立量过的数对表过）；没读到成员表"
                              "的档写 null + landed_note，**不许写成 0**",
    "driver": "「这台机器的驱动能不能吃到这一档 CUDA」不在这张表里说 —— 要运行时问 nvidia-smi，"
              "属外壳 #25；表里只列上游真发了哪些版本",
}


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def companion_key(name: str, release: str) -> tuple | None:
    """`cudart-llama-bin-win-cuda-12.4-x64.zip` → ("llama", "win-x64", "12.4")。

    去掉 `cudart-` 前缀后剩下的名字**必须**被同一份分类器认成 CUDA runtime，
    认不成就由调用方报红 —— 那是上游改了命名的信号，不是可以跳过的行。
    """
    body = name[len("cudart-"):]
    got = bd.classify(body, release)
    eng = body.split("-", 1)[0]
    if got["kind"] != "runtime" or got["backend"] != "cuda":
        return None
    return eng, got["os"], got["version"]


def build(matrix: dict, plan: dict, parity: dict) -> tuple[dict, list[str]]:
    problems: list[str] = []
    # 落地体积（换后端要多占多少盘）不在矩阵也不在 payload-plan 里 —— 它由 runtime-parity
    # 把那颗包的成员逐个解压后大小加出来。没读到成员表的档只能写未知，不许写 0。
    landed_map = {a["asset"]: a.get("landed_mib") for a in parity["assets"] if a.get("read")}
    unread_assets = {a["asset"] for a in parity["assets"] if not a.get("read")}
    # CUDA runtime 档先收集齐：伴生包必须**双向**对得上，只配一半就是静默丢一档。
    cuda_rows: dict[tuple, str] = {}
    for e, body in matrix["engines"].items():
        for os_key, cells in body["by_os"].items():
            for x in cells.get("cuda", []):
                cuda_rows[(e, os_key, x["version"])] = x["asset"]
    # 伴生包先配好，下面每个 CUDA 档都要查它。
    comps: dict[tuple, list[dict]] = {}
    for e, body in matrix["engines"].items():
        rel = body["release"]
        for r in body["not_runtime"]:
            if r["kind"] != "cudart_companion":
                continue
            key = companion_key(r["asset"], rel)
            if key is None:
                problems.append(f"{r['asset']}: 去掉 cudart- 前缀后分类器认不出它是哪一档 CUDA runtime"
                                " —— 上游改了命名，这一档的体积承诺要重看")
                continue
            eng, os_key, ver = key
            if eng != e:
                problems.append(f"{r['asset']}: 名字里的引擎是 {eng}，却被放在 {e} 这一张表下"
                                " —— 归属漂了，不许靠目录位置猜")
                continue
            if key not in cuda_rows:
                problems.append(f"{r['asset']}: 这份伴生包配不到任何一档 CUDA runtime"
                                f"（引擎/OS/版本 = {key}）—— 上游改了拼法或撤了那一档，"
                                "配不上的伴生包不许被静默吸收成「没有」")
                continue
            comps.setdefault(key, []).append(
                {"asset": r["asset"], "mib": r["mib"], "sha256": r["sha256"]})
    for key, rows in comps.items():
        if len(rows) > 1:
            problems.append(f"{key}: 一颗 CUDA runtime 对上 {len(rows)} 份伴生包 "
                            f"{[x['asset'] for x in rows]} —— 配哪颗都行 = 谁都没被钉住")

    # payload-plan 的默认档：按 (engine, os) 取，status 不是 ok 的那种没有 asset。
    defaults: dict[tuple, dict] = {}
    for os_key, rows in plan["os"].items():
        for r in rows:
            if r.get("engine"):
                defaults[(r["engine"], os_key)] = r

    by_asset = {r["asset"]: r for v in matrix["engines"].values() for r in v["rows"]}
    gap_assets = {r["asset"] for v in matrix["engines"].values()
                  for r in v["platform_not_in_name"]}

    engines: dict[str, dict] = {}
    no_accel: list[dict] = []
    no_companion: list[dict] = []
    stats = {"引擎×OS": 0, "格子": 0, "可选档": 0, "CUDA 档": 0,
             "已装默认档": 0, "格子没有默认档": 0, "有落地体积的档": 0, "落地体积未知的档": 0}
    for e, body in matrix["engines"].items():
        per_os = {}
        for os_key, cells in body["by_os"].items():
            stats["引擎×OS"] += 1
            stats["格子"] += len(cells)
            dflt = defaults.get((e, os_key))
            named = {x["asset"]: x for c in cells.values() for x in c}
            # 默认那颗必须能在这一格里找到；找不到的唯一合法情形是"CPU 包名字里不写平台"。
            install, dnote, d_asset = "shipped", None, None
            if dflt is None:
                install, dnote = "not_shipped", "payload-plan 没覆盖这个 OS（默认档不由这张表决定）"
                stats["格子没有默认档"] += 1
            elif dflt.get("status") != "ok":
                # 有 engine 却没 ok 状态 = payload-plan 的形状变了（今天"上游无产物"那行连 engine 都没有）。
                # 这种行不许悄悄搬成默认档，也不许悄悄丢掉 —— 红。
                problems.append(f"{e}/{os_key}: payload-plan 这一行有 engine 但 status="
                                f"{dflt.get('status')} —— 这张表不知道怎么搬它，先红再说")
            else:
                d_asset = dflt["asset"]
                stats["已装默认档"] += 1
                if d_asset not in named:
                    if d_asset not in gap_assets:
                        problems.append(f"{e}/{os_key}: 默认档 {d_asset} 既不在这一格，"
                                        "也不在「名字里不写平台」那份清单里 —— 两份产者对同一个 release 已经漂了")
                    elif not dflt.get("inferred_platform"):
                        problems.append(f"{e}/{os_key}: 默认档 {d_asset} 的名字里不写平台，"
                                        "payload-plan 却没标 inferred_platform —— 推断要写明是推断")
                elif HEX64.match(dflt["digest"].split(":", 1)[-1]) and \
                        dflt["digest"] != "sha256:" + named[d_asset]["sha256"]:
                    problems.append(f"{e}/{os_key}: 默认档的 digest 与矩阵里的 sha256 不等 —— 先查哪份是旧的")
            base_mib = (by_asset.get(d_asset) or {}).get("mib")
            base_landed = landed_map.get(d_asset) if d_asset else None
            if d_asset and base_landed is None and d_asset not in gap_assets:
                problems.append(f"{e}/{os_key}: 默认档 {d_asset} 在 runtime-parity 里没有落地体积 —— "
                                "「换这一档要多占多少盘」没得算，两份产者先对一下")
            options = []
            for backend in sorted(cells):
                for x in cells[backend]:
                    stats["可选档"] += 1
                    opt = {"backend": backend, "version": x["version"], "asset": x["asset"],
                           "sha256": x["sha256"], "mib": x["mib"], "is_default": x["asset"] == d_asset,
                           "extra_mib_vs_default": None if base_mib is None
                           else round(x["mib"] - base_mib, 1)}
                    if not HEX64.match(x["sha256"] or ""):
                        problems.append(f"{e}/{os_key}/{x['asset']}: 没有 64 位 sha256 —— "
                                        "可替换的前提是下完能校验，这颗不许进这张表")
                    if opt["extra_mib_vs_default"] is not None and abs(
                            opt["extra_mib_vs_default"] - round(x["mib"] - base_mib, 1)) > 1e-9:
                        problems.append(f"{e}/{os_key}/{x['asset']}: 体积差没重算对")
                    if backend == "cuda":
                        stats["CUDA 档"] += 1
                        got = comps.get((e, os_key, x["version"]))
                        if got:
                            opt["cudart"] = got[0]
                        else:
                            opt["cudart"] = None
                            opt["cudart_note"] = ("这份 release 的产物表里没有与该档同 (引擎, OS, CUDA 版本) "
                                                  "的 cudart- 伴生包 —— 是「没有」，不是「不需要」")
                            no_companion.append({"engine": e, "os": os_key, "asset": x["asset"],
                                                 "version": x["version"],
                                                 "because": "cudart- 前缀的产物里没有对上这一档的那些"})
                    opt["landed_mib"] = landed_map.get(x["asset"])
                    if opt["landed_mib"] is None:
                        stats["落地体积未知的档"] += 1
                        if x["asset"] in unread_assets:
                            opt["landed_note"] = ("这一档的 tar.gz 没读整包（只为真装的默认档付那份流量）"
                                                  "—— 换到这一档要多占多少盘是未知，不是 0")
                        else:
                            problems.append(f"{e}/{os_key}/{x['asset']}: runtime-parity 里既没登记"
                                            "读到过、也没登记未读 —— 两份产者对这颗包不一致")
                    else:
                        stats["有落地体积的档"] += 1
                    opt["disk_extra_mib_vs_default"] = None if (
                        base_landed is None or opt["landed_mib"] is None) else round(
                        opt["landed_mib"] - base_landed, 1)
                    options.append(opt)
            accel = sorted({o["backend"] for o in options if o["backend"] not in ("cpu", "blas")})
            if not accel:
                no_accel.append({"engine": e, "os": os_key,
                                 "because": "按资产名分不出加速档（上游没发，或名字里不写后端）"})
            # CPU 退路的四种状态必须算出来，不许手写。
            if "cpu" in cells:
                cpu_state = "named_cpu"
            elif d_asset in gap_assets and (by_asset.get(d_asset) or {}).get("backend") == "cpu":
                cpu_state = "only_unnamed_asset"
            elif set(cells) == {"unknown_from_name"}:
                cpu_state = "name_does_not_say"
            else:
                cpu_state = "absent"
            if install == "shipped" and cpu_state == "absent":
                problems.append(f"{e}/{os_key}: 这一档要装，可这一格里连 CPU 退路都没有 —— "
                                "给普通人的机器不能只剩加速档")
            per_os[os_key] = {"cell_backends": sorted(cells), "cpu_fallback": cpu_state,
                              "install": install, "default": d_asset,
                              "default_in_cell": d_asset in named, "default_note": dnote,
                              "acceleration_named": accel, "options": options}
        engines[e] = {"release": body["release"], "os": per_os}
    # 反向点名：payload-plan 给了默认档，但这张表里要么没有那一格（whisper 在 mac 就是这类，
    # 上游真没发 runtime），要么那颗不在可换清单里（whisper 在 win-x64 的 CPU 包名字不写平台）。
    # 不列出来的话，"这一档没东西可换"与"我没查到"在表里长得一模一样。
    missing_cell, default_off_cell = [], []
    for os_key, rows in plan["os"].items():
        for r in rows:
            if not r.get("engine"):
                # payload-plan 这一行连 engine 都没有（上游没发能起进程的产物），
                # 表里因此不存在这一格 —— 必须点名，不能与"我没查到"混成一件事。
                missing_cell.append({"capability": r.get("capability"), "os": os_key,
                                     "status": r.get("status"),
                                     "because": "payload-plan 这一行没有 engine（上游无产物），"
                                                "矩阵里因此没有这一格"})
                continue
            cell = engines.get(r["engine"], {}).get("os", {}).get(os_key)
            if cell is None:
                missing_cell.append({"engine": r["engine"], "os": os_key,
                                     "status": r.get("status"),
                                     "because": "矩阵里没有这个 引擎×OS 的格子（上游没发 runtime，"
                                                "或按资产名分不出平台）"})
            elif not cell["default_in_cell"]:
                default_off_cell.append({"engine": r["engine"], "os": os_key, "asset": r.get("asset"),
                                         "inferred_platform": bool(r.get("inferred_platform")),
                                         "because": "默认档那颗的名字里不写平台，所以不在按名字分出来的"
                                                    "清单里 —— 外壳要换回 CPU 时不能靠资产名挑"})
    stats["伴生包"] = sum(len(v) for v in comps.values())
    stats["配不上的CUDA档"] = len(no_companion)
    stats["无加速档可换的引擎×OS"] = len(no_accel)
    stats["默认档没有格子可换的"] = len(missing_cell)
    stats["默认档写在格子外的"] = len(default_off_cell)
    # 三条地板：这三档 OS 里 llama 与 sd 至少各有一条名字里写着的加速档。
    for e in ("llama", "sd"):
        for os_key in ("win-x64", "linux-x64"):
            row = engines[e]["os"].get(os_key)
            if not row or not row["acceleration_named"]:
                problems.append(f"{e}/{os_key}: 「按后端分层」在这一档没有可换的加速档 —— "
                                "那句话要重写，不许默默过")
    doc = {"schema": 1, "rule": RULE,
           "from": "pins/backend-matrix.json（体积与哈希）+ pins/payload-plan.json（默认档）",
           "engines": engines,
           "companions": {r["asset"]: {"engine": k[0], "os": k[1], "cuda": k[2]}
                          for k, rows in comps.items() for r in rows},
           "cuda_rows_without_companion": no_companion,
           "no_acceleration_named": no_accel,
           "missing_cell_default": missing_cell,
           "default_outside_cell": default_off_cell,
           "stats": stats}
    return doc, problems


CHECKED = ("backend", "version", "asset", "sha256", "mib", "is_default",
           "extra_mib_vs_default", "cudart", "landed_mib", "disk_extra_mib_vs_default")


def check(committed: dict, fresh: dict) -> list[str]:
    bad = []
    if committed.get("rule") != fresh.get("rule"):
        bad.append("口径变了（默认档怎么取、伴生包怎么配、CPU 退路那四个词怎么说）")
    if committed.get("stats") != fresh.get("stats"):
        bad.append(f"覆盖度变了 {committed.get('stats')} → {fresh.get('stats')}")
    if committed.get("cuda_rows_without_companion") != fresh.get("cuda_rows_without_companion"):
        bad.append("「哪几档 CUDA 没有伴生包」这个集合变了 —— 上游补发了，或者配对规则漂了")
    if committed.get("no_acceleration_named") != fresh.get("no_acceleration_named"):
        bad.append("「哪些引擎×OS 按名字分不出加速档」变了 —— 分层那句的覆盖面跟着变")
    if committed.get("missing_cell_default") != fresh.get("missing_cell_default"):
        bad.append("「有默认档但表里没这一格」的清单变了 —— 上游发没发 runtime 这件事要重看")
    if committed.get("default_outside_cell") != fresh.get("default_outside_cell"):
        bad.append("「默认档那颗不在按名字分出的清单里」变了 —— 退回 CPU 那一步的挑法要重看")
    for e, body in fresh["engines"].items():
        old = committed["engines"].get(e)
        if not old:
            bad.append(f"{e}: 新增引擎，已提交那张没有")
            continue
        if old.get("release") != body["release"]:
            bad.append(f"{e}: 钉的 release 变了 {old.get('release')} → {body['release']}")
        if sorted(old["os"]) != sorted(body["os"]):
            bad.append(f"{e}: 有档的 OS 集合变了 {sorted(old['os'])} → {sorted(body['os'])}")
            continue
        for os_key, row in body["os"].items():
            o = old["os"][os_key]
            for f in ("cell_backends", "cpu_fallback", "install", "default",
                      "default_in_cell", "acceleration_named"):
                if o.get(f) != row[f]:
                    bad.append(f"{e}/{os_key}: {f} 变了 {o.get(f)} → {row[f]}")
            a, b = o["options"], row["options"]
            if len(a) != len(b):
                bad.append(f"{e}/{os_key}: 可选档从 {len(a)} 条变成 {len(b)} 条（一格一行的写法会静默丢版本）")
                continue
            for x, y in zip(a, b):
                if {k: x.get(k) for k in CHECKED} != {k: y.get(k) for k in CHECKED}:
                    bad.append(f"{e}/{os_key}/{x['asset']}: 这一档的字段变了 —— "
                               f"{ {k: (x.get(k), y.get(k)) for k in CHECKED if x.get(k) != y.get(k)} }")
    return bad


def selftest() -> int:
    bad = []
    matrix, plan, par = load(MATRIX), load(PLAN), load(PARITY)
    landed_map = {a["asset"]: a.get("landed_mib") for a in par["assets"] if a.get("read")}
    doc, problems = build(matrix, plan, par)
    if problems:
        bad.append(f"真输入应当 0 问题，实有 {len(problems)}：{problems[:2]}")
    if doc["stats"]["可选档"] != matrix["stats"]["runtime 行"] - sum(
            len(v["platform_not_in_name"]) for v in matrix["engines"].values()):
        bad.append(f"可选档总数必须等于矩阵里「名字写了平台的那些 runtime」，"
                   f"现在 {doc['stats']['可选档']}")
    if doc["stats"]["伴生包"] != 7 or doc["stats"]["CUDA 档"] != 10:
        bad.append(f"实测应是 7 份伴生包 / 10 档 CUDA，现在 {doc['stats']['伴生包']}/{doc['stats']['CUDA 档']}")
    if doc["stats"]["配不上的CUDA档"] != 3:
        bad.append(f"whisper 那三档 CUDA 没有伴生包（实测 3），现在 {doc['stats']['配不上的CUDA档']}")
    if (doc["stats"]["默认档没有格子可换的"], doc["stats"]["默认档写在格子外的"]) != (1, 1):
        bad.append(f"实测应有 1 条「没有 engine 那一格」（macOS 字幕）与 1 条「默认档那颗不在清单里」"
                   f"（whisper win-x64 的 bin-x64），现在 {doc['stats']['默认档没有格子可换的']}"
                   f"/{doc['stats']['默认档写在格子外的']}")
    # 正向对照：默认档确实是 payload-plan 里那三行，不是这张表自己挑的。
    shipped = {(e, o): r["default"] for e, b in doc["engines"].items()
               for o, r in b["os"].items() if r["install"] == "shipped"}
    want = {(r["engine"], o): r["asset"] for o, rows in plan["os"].items() for r in rows if r.get("engine")}
    if shipped != want:
        bad.append(f"默认档搬运错了：{sorted(set(shipped.items()) ^ set(want.items()))}")
    # ① 假伴生包（版本对上任何东西）必须红。
    m1 = json.loads(json.dumps(matrix))
    for r in m1["engines"]["llama"]["not_runtime"]:
        if r["asset"].startswith("cudart-llama-bin-win-cuda-12.4"):
            r["asset"] = "cudart-llama-bin-win-cuda-99.9-x64.zip"
    if build(m1, plan, par)[1] == []:
        bad.append("配不上的假伴生包没被抓 —— 那这一列是装饰")
    # ② 同档两份伴生包必须红（配哪颗都行 = 谁都没钉住）。
    m2 = json.loads(json.dumps(matrix))
    m2["engines"]["llama"]["not_runtime"].append(
        {"asset": "cudart-llama-bin-win-cuda-12.4-x64.zip", "kind": "cudart_companion",
         "mib": 1.0, "sha256": "0" * 64})
    if not any("对上 2 份" in x for x in build(m2, plan, par)[1]):
        bad.append("一份 CUDA 档对上两颗伴生包时没红")
    # ③ 默认档被换成不在这一格、也不在缺席清单里的那颗必须红。
    p3 = json.loads(json.dumps(plan))
    for r in p3["os"]["win-x64"]:
        if r.get("engine") == "llama":
            r["asset"] = "llama-b11727-bin-不存在-x64.zip"
    if not any("两份产者对同一个 release 已经漂了" in x for x in build(matrix, p3, par)[1]):
        bad.append("默认档凭空消失时没红")
    # ④ whisper 的 inferred_platform 被抹掉必须红。
    p4 = json.loads(json.dumps(plan))
    for r in p4["os"]["win-x64"]:
        if r.get("engine") == "whisper":
            r["inferred_platform"] = False
    if not any("推断要写明是推断" in x for x in build(matrix, p4, par)[1]):
        bad.append("把推断写成事实时没红")
    # ⑤ 某格体积被改坏时，差值必须重算出来并由漂移判据叫。
    m5 = json.loads(json.dumps(matrix))
    m5["engines"]["llama"]["by_os"]["win-x64"]["cuda"][1]["mib"] += 100
    fresh5, _p5 = build(m5, plan, par)
    if check(doc, fresh5) == []:
        bad.append("体积差变了而漂移判据没听见")
    # ⑥ sha256 变空必须红（可替换 = 能校验）。
    m6 = json.loads(json.dumps(matrix))
    m6["engines"]["sd"]["by_os"]["win-x64"]["vulkan"][0]["sha256"] = ""
    if not any("没有 64 位 sha256" in x for x in build(m6, plan, par)[1]):
        bad.append("没哈希的档照样进了表")
    # ⑦ 已提交的默认档被人手改成另一颗，必须由 check 叫（跨产物对表不许靠眼睛）。
    c7 = json.loads(json.dumps(doc))
    for o in c7["engines"]["llama"]["os"]["win-x64"]["options"]:
        o["is_default"] = (o["backend"] == "vulkan")
    c7["engines"]["llama"]["os"]["win-x64"]["default"] = \
        "llama-b11727-bin-win-vulkan-x64.zip"
    if not any("llama/win-x64: default 变了" in x for x in check(c7, doc)):
        bad.append("默认档被手改时漂移判据没红（那 check 只是长得像）")
    # ⑧ whisper 那颗"名字里不写平台"的 CPU 默认档从缺席清单里被抹掉，必须红 ——
    #    否则"这一格找不到默认档"与"上游撤了那颗包"在表里长得一样。
    m8 = json.loads(json.dumps(matrix))
    m8["engines"]["whisper"]["platform_not_in_name"] = [
        x for x in m8["engines"]["whisper"]["platform_not_in_name"]
        if x["asset"] != "whisper-bin-x64.zip"]
    if not any("两份产者对同一个 release 已经漂了" in x for x in build(m8, plan, par)[1]):
        bad.append("默认档那颗从缺席清单里消失时没红")
    # ⑨ 某格的 CPU 档被撤掉后，"这一档要装却没退路"必须红。
    m9 = json.loads(json.dumps(matrix))
    del m9["engines"]["llama"]["by_os"]["win-x64"]["cpu"]
    if not any("CPU 退路" in x for x in build(m9, plan, par)[1]):
        bad.append("CPU 退路那一档消失时没红 —— 给普通人的机器不能只剩加速档那句是空的")
    # ⑩ payload-plan 那行有 engine 却 status 不是 ok，必须红（不许悄悄搬成默认档，也不许悄悄丢掉）。
    p10 = json.loads(json.dumps(plan))
    for r in p10["os"]["linux-x64"]:
        if r.get("engine") == "llama":
            r["status"] = "上游无产物"
    if not any("有 engine 但 status" in x for x in build(matrix, p10, par)[1]):
        bad.append("输入形状漂了的那一行被静默处理了")
    # ⑪ 落地体积：每一档要么有数，要么写明未知 —— 不许静默当 0。
    if doc["stats"]["有落地体积的档"] + doc["stats"]["落地体积未知的档"] != doc["stats"]["可选档"]:
        bad.append(f"落地体积没覆盖全部可选档：{doc['stats']['有落地体积的档']}+"
                   f"{doc['stats']['落地体积未知的档']} != {doc['stats']['可选档']}")
    if any(landed_map.get(r["default"]) is None for b in doc["engines"].values()
           for r in b["os"].values() if r["install"] == "shipped" and r["default"]):
        bad.append("真装的默认档必须有落地体积 —— 那是减法的基准")
    # ⑫ parity 与可选档不一致（读到过的被改成未读）必须红。
    par2 = json.loads(json.dumps(par))
    for a in par2["assets"]:
        if a.get("read"):
            a["read"] = False
            break
    if not any("两份产者对这颗包不一致" in x or "没有落地体积" in x
               for x in build(matrix, plan, par2)[1]):
        bad.append("parity 与可选档不一致时没红")
    # ⑬ 落地体积差被人手改过，漂移判据必须叫。
    c13 = json.loads(json.dumps(doc))
    for o in c13["engines"]["llama"]["os"]["win-x64"]["options"]:
        if o["landed_mib"] is not None:
            o["disk_extra_mib_vs_default"] = (o["disk_extra_mib_vs_default"] or 0) + 50
            break
    if not any("这一档的字段变了" in x for x in check(doc, c13)):
        bad.append("落地体积差被手改时漂移判据没红（那 check 只是长得像）")
    print("---- 结论 ----", doc["stats"], f"BAD 数量 = {len(bad)}")
    for x in bad:
        print("BAD:", x)
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--matrix", default=MATRIX)
    ap.add_argument("--plan", default=PLAN)
    ap.add_argument("--parity", default=PARITY)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    doc, problems = build(load(a.matrix), load(a.plan), load(a.parity))
    if a.check:
        bad = check(load(a.out), doc) + problems
        for x in bad:
            print("BAD:", x)
        print(f"比对了 {len(doc['engines'])} 个引擎 / {doc['stats']['可选档']} 个可选档，"
              f"伴生包 {doc['stats']['伴生包']} 份，问题 {len(bad)} 条")
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
