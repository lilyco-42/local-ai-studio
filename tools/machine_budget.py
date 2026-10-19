#!/usr/bin/env python3
"""装机预算：把已经钉好的五份产物加成"这台机器首启要下多少、按实测吞吐要等多久"。

为什么单独要这一件事：三张档位表各自回答"这颗选哪个"，但普通人问的是另一句
——「我装齐这四样要占多少、要等多久」。这个数字此前**没有在任何地方被算过**
（`payload-plan` 只算引擎净负载，档位表只给单颗大小），而它能立刻暴露一类设计问题：
比如生图的 unet 只是三份文件里的一份，FLUX 还要 `clip_l` + `t5` + `vae`，
只报主体大小等于把真实下载量少算好几倍。

三条口径先说死，别让数字被误读：

  1. **加的是磁盘与下载量，不是内存。** 四项能力共用一个常驻引擎、一次只跑一项
     （ADR-0001 的常驻服务那条），所以"权重大小相加当内存需求"是错的；
     内存那一轴由三张档位表各自的判据负责（对话用 llmfit 拟合值、字幕用上游 Mem 列、
     生图明确 `memory_claim=none`）。产物里 `ram_statement` 就是把这句话写死，
     免得下一个人顺手把它当内存表用。
  2. **只加已钉产物里有的东西**，一个字节都不估算：引擎净负载取 `pins/payload-plan.json`
     的 `totals_mib`（Range 读 zip 中央目录量的），权重取三张档位表**该档第一行**
     （也就是默认档），OCR 取 `catalog/defaults.json` 里已钉的默认三条与
     `pins/ocr-languages.json` 的全语言合计两个数，生图的配套件从同一张表的 `weights` 里查
     真实字节 —— 查不到就报问题，不按"通常多大"补。
  3. **等待时间用实测吞吐换算，并且只当量级**：ModelScope 4.2 MiB/s、hf-mirror 0.25 MiB/s
     是从 lain42.top 这台 CN 出口一次量的（`r2_manifest.RATES_MIB_S`），
     不同城市/运营商必然不同；产物里 `measured_at_exit` 就写这台机器，不装作普适。

macOS 那一档现在必然缺一块：`pins/payload-plan.json` 的 gaps 里写着
**字幕在 macos-arm64 上游无产物**（whisper.cpp 不发 macOS 包），所以 macOS 的预算
天生只含三项能力 + 一句 flag —— 这不是 bug，是把未决事项算进去。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import r2_manifest  # 实测吞吐表在这里，别处也用它，不另抄一份数字  # noqa: E402

MIB = 1024 ** 2
ARTIFACTS = ("pins/payload-plan.json", "catalog/defaults.json", "pins/ocr-languages.json",
             "catalog/recommendations.json", "catalog/subtitle-tiers.json",
             "catalog/image-tiers.json")
OUT = os.path.join(ROOT, "catalog", "machine-budget.json")
# 每个档位取"这张表该档的第一行"当默认档。顺序是被上游数字决定的，不是我觉得哪个好用。
CAPS = ("chat", "subtitle", "image")
# 逐 OS 要比的字段。等待时间也在里面：它是从 r2_manifest 的实测吞吐算出来的，
# 吞吐表一改这份预算就必须重出，否则 UI 上那句"约 X 分钟"就和量出来的速率脱钩了。
CHECKED_OS_KEYS = ("download_bytes", "engine_download_mib", "engine_landed_mib",
                   "minutes_at_measured_rate", "worst_case_hours_at_slowest_measured")


def load(root: str) -> dict:
    out = {}
    for rel in ARTIFACTS:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            out[rel] = json.load(fh)
    return out


def rate_mib_s(needle: str) -> tuple[float, str]:
    for key, rate, note in r2_manifest.RATES_MIB_S:
        if needle in key:
            return float(rate), note
    return 0.0, "没有该出口的实测速率 → 不给等待时间，不编一个数字"


def need_key(row: dict) -> str:
    """这颗权重对应上游例子里的哪个旗标：VAE 一个，文本编码器按 clip_l / clip_g / t5xxl 分开。

    用 role 当键会少算 —— flux 那一行点名的 clip_l 与 t5xxl 的 role 都是 text_encoder。
    """
    return "VAE" if row["role"] == "vae" else (row.get("role_token") or row["role"])


def weight_items(arts: dict, tier: str) -> tuple[list[dict], list[str], dict | None]:
    """这一档的默认权重行：三张档位表各取第一行，生图再带上同仓库的配套件。

    第三个返回值是生图那一行的配套件与完备性情况（`{"repo","roles","needs_counted","alternates",
    "state","needs","missing","source","citation"}`）；没有生图行时为 None。
    它单独返回是因为"这一族按上游例子还需要什么、这一档到底装没装齐"必须能进产物：
    上游 `docs/sd.md` 的 txt2img 例子逐行写着要什么（flux 要 `--vae --clip_l --t5xxl`，
    SD1.5 只给 `-m`），**需要而仓库里没有**的那些档就是下限，判不了家族的写 unknown_arch，
    也不许当装齐。
    """
    items, problems = [], []
    companions = None

    def one(artifact, label, kind):
        rows = arts[artifact]["tiers_rows"].get(label) or []
        if not rows:
            problems.append(f"{kind} 在 {label} 档没有任何行（{artifact} 漂了）")
            return None
        return rows[0]

    chat = one("catalog/recommendations.json", tier, "对话")
    if chat:
        items.append({"kind": "chat", "name": chat["name"], "file": chat["file"],
                      "bytes": int(chat["size"]), "hash_status": chat["status"],
                      "trusted_before_download": chat["usable_before_download"],
                      "from": "catalog/recommendations.json"})
    sub = one("catalog/subtitle-tiers.json", tier, "字幕")
    if sub:
        items.append({"kind": "subtitle", "name": sub["name"], "file": sub["file"],
                      "bytes": int(sub["size"]), "hash_status": "single",
                      "trusted_before_download": False,
                      "mem_text": sub.get("mem_text"),
                      "from": "catalog/subtitle-tiers.json"})
    img = one("catalog/image-tiers.json", tier, "生图")
    if img:
        weights = {(r["repo"], r["file"]): r for r in arts["catalog/image-tiers.json"]["weights"]}
        items.append({"kind": "image", "name": img["repo"], "file": img["file"],
                      "bytes": int(img["size"]), "hash_status": img["status"],
                      "trusted_before_download": img["usable_before_download"],
                      "role": "unet", "from": "catalog/image-tiers.json"})
        found = []
        for c in img.get("companions") or []:
            row = weights.get((img["repo"], c["file"]))
            if row is None:
                problems.append(f"{img['repo']}/{c['file']}: 配套件在 weights 里查不到字节 —— "
                                "不能按'通常多大'补一个数")
                continue
            found.append(row)

        # 分组键是"上游那一行里那个旗标对应的东西"，**不是 role**：flux 那一行要 clip_l、t5xxl
        # 和 vae 三份，前两颗的 role 都是 text_encoder —— 按 role 分组会把 t5xxl 丢掉，
        # 而档位表这时候还写着 state=complete（这一版就错过一次，所以写下来）。
        # 同一个键下面的十几个量化才是备选，取最小的那颗。
        groups: dict[str, dict] = {}
        for row in found:
            key = need_key(row)
            cur = groups.get(key)
            if cur is None or int(row["size"]) < int(cur["size"]):
                groups[key] = row
        for key in sorted(groups):
            row = groups[key]
            items.append({"kind": "image", "name": img["repo"], "file": row["file"],
                          "bytes": int(row["size"]), "hash_status": row["status"],
                          "trusted_before_download": row["usable_before_download"],
                          "role": row["role"], "need_key": key, "companion_of": img["file"],
                          "group_alternates": sum(1 for r in found if need_key(r) == key) - 1,
                          "from": "catalog/image-tiers.json"})
        counted = set(groups)
        comp = img.get("completeness") or {}
        for need in comp.get("needs") or []:
            if need not in counted and need not in (comp.get("missing") or []):
                problems.append(f"{img['repo']}/{img['file']}: 档位表说这一族需要 {need}，"
                                "但同仓库的配套件里没有它，而它自己写着不缺 —— "
                                "加法会少算（两张表不一致）")
        companions = {"repo": img["repo"], "roles": sorted({r["role"] for r in groups.values()}),
                      "needs_counted": sorted(counted), "alternates": len(found) - len(groups)}
        # 预算只转述档位表已经判过的 state/needs/missing（依据是 docs/sd.md 的 txt2img 例子那四行），
        # 并据此决定这一档能不能当"装齐"：需要但仓库没有 = 下限；上游例子只给 -m = 不是下限。
        for k in ("state", "needs", "missing", "source", "citation"):
            companions[k] = comp.get(k)
    return items, problems, companions


def ocr_items(arts: dict) -> list[dict]:
    """OCR 的两个数：默认档三条（已钉字节）与全部 14 种语言的合计（上游成对表算出来的）。"""
    out = []
    for name, e in sorted(arts["catalog/defaults.json"]["entries"].items()):
        if not re.search(r"ocr|ppocr", name, re.I):
            continue
        out.append({"kind": "ocr", "name": "默认档", "file": name, "bytes": int(e["size"]),
                    "hash_status": "agree" if "4.5" in (e.get("strength") or {}).get("rungs", [])
                    else "single",
                    "trusted_before_download": "4.5" in (e.get("strength") or {}).get("rungs", []),
                    "from": "catalog/defaults.json"})
    agg = arts["pins/ocr-languages.json"]
    if "rec_dict_bytes" not in agg:
        agg = agg.get("aggregate") or {}
    if agg.get("rec_dict_bytes"):
        out.append({"kind": "ocr", "name": "全 14 种语言（rec+dict）", "file": "(合计)",
                    "bytes": int(agg["rec_dict_bytes"]), "hash_status": "mixed",
                    "trusted_before_download": None, "from": "pins/ocr-languages.json",
                    "note": "这是把默认档换成全语言时多出来的量，不是叠在默认档上"})
    return out


def build(arts: dict) -> tuple[dict, list[str]]:
    problems: list[str] = []
    plan = arts["pins/payload-plan.json"]
    tiers = list(arts["catalog/recommendations.json"]["tiers"])
    per: dict[str, dict] = {}
    for tier in tiers:
        items, p, comps = weight_items(arts, tier)
        problems += p
        items += ocr_items(arts)
        if not p and not items:
            problems.append(f"{tier} 档一项权重都没有，预算等于空表")
        by_os = {}
        for os_key, eng in sorted(plan["os"].items()):
            eng_mib = float(plan["totals_mib"][os_key])
            total_bytes = sum(i["bytes"] for i in items) + int(round(eng_mib * MIB))
            landed = [r.get("uncompressed_mib") for r in eng]
            landed_mib = round(sum(v for v in landed if v), 1)
            rows = [{"capability": r["capability"], "engine": r.get("engine"),
                     "asset": r.get("asset"), "status": r["status"],
                     "download_mib": r.get("archive_mib"), "landed_mib": r.get("uncompressed_mib")}
                    for r in eng]
            # 两件事必须分开：**没有产物**（字幕在 macOS）与**产物落地大小未知**（tar.gz 读不了
            # 中央目录）。把后者当缺失能力报出去，就等于谎称那项功能在这台机器上没得用。
            missing = sorted({r["capability"] for r in eng if not r.get("engine")})
            unknown_landed = sorted({r["capability"] for r in eng
                                     if r.get("engine") and r.get("uncompressed_mib") is None})
            fast, note = rate_mib_s("modelscope")
            slow, _ = rate_mib_s("hf-mirror")
            by_os[os_key] = {
                "engine_download_mib": eng_mib,
                # tar.gz 读不了中央目录（payload-plan 里标着 needs_extract）→ 落地大小只能是未知，
                # 于是这一档的"磁盘总量"是**下限**，不是承诺。宁可少说，不要编。
                "engine_landed_mib": landed_mib if all(v is not None for v in landed) else None,
                "engine_rows": rows,
                "download_bytes": total_bytes,
                "download_gib": round(total_bytes / 1024 ** 3, 2),
                "minutes_at_measured_rate": (round(total_bytes / MIB / fast / 60, 1)
                                             if fast else None),
                "worst_case_hours_at_slowest_measured": (round(total_bytes / MIB / slow / 3600, 2)
                                                        if slow else None),
                "missing_capabilities": missing,
                "unknown_landed_sizes": unknown_landed,
            }
        per[tier] = {
            "items": sorted(items, key=lambda i: (i["kind"], -i["bytes"])),
            "weights_bytes": sum(i["bytes"] for i in items),
            "untrusted_rows": sorted({i["file"] for i in items
                                      if i["trusted_before_download"] is False}),
            "companion_bytes": sum(i["bytes"] for i in items if i.get("companion_of")),
            "image_companions": comps,
            # **需要但同仓库没有**（incomplete）或**上游例子里没这一族、判不了**（unknown_arch）
            # 时，这一档的生图量只能当**下限**；上游例子只给 -m 的（self_contained）不是下限。
            "image_lower_bound": bool(comps) and comps.get("state") in ("incomplete", "unknown_arch"),
            "by_os": by_os,
        }
    totals = {t: {o: per[t]["by_os"][o]["download_gib"] for o in per[t]["by_os"]} for t in tiers}
    return {"schema": 1,
            "from": list(ARTIFACTS),
            "rule": {"sum": "只加已钉产物里的字节：引擎净负载(payload-plan totals_mib) + "
                            "各档默认权重(三张档位表该档第一行) + OCR(默认档三条已钉字节)",
                     "image_companions": "生图的配套件（clip/t5/vae）按 weights 里的真实字节加，"
                                          "查不到就报问题、不估算；同一角色的多个量化是**备选**不是都要下，"
                                          "只把最小那颗计入，其余记 alternates；"
                                          "档位表判为 incomplete（需要但同仓库没有）或 unknown_arch（判不了）"
                                          "的那些档，生图部分只是下限，判定依据是上游 docs/sd.md 的 "
                                          "txt2img 例子那四行（每行都有 file:line 出处）",
                     "ram": "不加内存：四项能力共用一个常驻引擎、一次跑一项，"
                            "内存那一轴由各档位表自己的判据负责",
                     "time": "等待时间用 lain42.top 这台 CN 出口一次量的吞吐换算，只当量级",
                     "ocr": "默认档三条与全 14 语言合计是两个数，不能相加"},
            "measured_at_exit": "lain42.top（CN 出口，2026-10 一次量）",
            "rates_mib_s": {"modelscope": rate_mib_s("modelscope")[0],
                            "hf-mirror": rate_mib_s("hf-mirror")[0]},
            "ram_statement": "本文件的 download_bytes 是磁盘与下载量，不是峰值内存",
            "tiers": tiers, "totals_gib": totals, "per_tier": per}, problems


def check(committed: dict, fresh: dict) -> list[str]:
    bad = []
    if committed.get("rule") != fresh.get("rule"):
        bad.append("口径变了（加什么/不加什么/时间怎么算），文档与 UI 措辞要一起重看")
    if committed.get("rates_mib_s") != fresh.get("rates_mib_s"):
        bad.append(f"换算用的吞吐变了 {committed.get('rates_mib_s')} → {fresh['rates_mib_s']}"
                   "（r2_manifest 那张表是历史实测，一改这里的所有等待时间都要重出）")
    if committed.get("tiers") != fresh.get("tiers"):
        bad.append(f"档位集合变了：{committed.get('tiers')} → {fresh.get('tiers')}")
        return bad
    for t in fresh["tiers"]:
        c, f = committed["per_tier"].get(t, {}), fresh["per_tier"][t]
        if [i["file"] for i in c.get("items", [])] != [i["file"] for i in f["items"]]:
            bad.append(f"{t}: 默认权重行变了 {[i['file'] for i in c.get('items', [])]} → "
                       f"{[i['file'] for i in f['items']]}（档位表被重算过，预算要跟着重出）")
            continue
        if c.get("image_companions") != f["image_companions"]:
            bad.append(f"{t}: 生图默认行的配套件变了 {c.get('image_companions')} → "
                       f"{f['image_companions']}（档位表重算过，'装齐'的含义要跟着重看）")
        if c.get("companion_bytes") != f["companion_bytes"]:
            bad.append(f"{t}: 配套件字节变了 {c.get('companion_bytes')} → {f['companion_bytes']}")
        if c.get("image_lower_bound") != f["image_lower_bound"]:
            bad.append(f"{t}: '这一档是不是下限'变了 {c.get('image_lower_bound')} → "
                       f"{f['image_lower_bound']}（配套件判定漂了，UI 那句'装完就能出图'要重看）")
        for o, ov in f["by_os"].items():
            cv = c.get("by_os", {}).get(o)
            if not cv:
                bad.append(f"{t}/{o}: 已提交那张里没有这个 OS")
                continue
            for k in CHECKED_OS_KEYS:
                if cv[k] != ov[k]:
                    bad.append(f"{t}/{o}.{k}: {cv[k]} → {ov[k]}")
            if sorted(cv.get("unknown_landed_sizes", [])) != sorted(ov["unknown_landed_sizes"]):
                bad.append(f"{t}/{o}: 落地大小未知的能力集合变了 {cv.get('unknown_landed_sizes')}"
                           f" → {ov['unknown_landed_sizes']}")
            if sorted(cv["missing_capabilities"]) != sorted(ov["missing_capabilities"]):
                bad.append(f"{t}/{o}: 缺失能力集合变了 {cv['missing_capabilities']} → "
                           f"{ov['missing_capabilities']}")
    return bad


def selftest() -> int:
    """离线对照：明知会被少算的东西必须被算出来，否则这份预算就是个装饰加法。"""
    bad = []
    arts = {
        "pins/payload-plan.json": {
            "totals_mib": {"win-x64": 17.1},
            "os": {"win-x64": [{"capability": "对话", "engine": "llama", "asset": "a.tar.gz",
                               "status": "needs_extract", "archive_mib": 17.1},
                              {"capability": "字幕", "engine": None, "asset": None,
                               "status": "上游无产物", "archive_mib": None}]},
            "gaps": [{"capability": "字幕", "os": "win-x64", "status": "上游无产物"}]},
        "catalog/defaults.json": {"entries": {
            "pp-ocrv6_tiny_det.onnx": {"size": 100, "strength": {"rungs": ["3", "4.5"]}},
            "qwen2.5-0.5b-instruct-q2_k.gguf": {"size": 999, "strength": {"rungs": ["3"]}}}},
        "pins/ocr-languages.json": {"aggregate": {"rec_dict_bytes": 200}},
        "catalog/recommendations.json": {"tiers": ["4GB"], "tiers_rows": {
            "4GB": [{"name": "a/b", "file": "b.gguf", "size": 1000, "status": "agree",
                     "usable_before_download": True}]}},
        "catalog/subtitle-tiers.json": {"tiers_rows": {
            "4GB": [{"name": "tiny", "file": "ggml-tiny.bin", "size": 2000,
                    "mem_text": "273 MB"}]}},
        "catalog/image-tiers.json": {"tiers_rows": {
            "4GB": [{"repo": "x/y", "file": "unet.gguf", "size": 3000, "status": "single",
                     "usable_before_download": False,
                     "completeness": {"state": "incomplete", "arch": "flux",
                                      "needs": ["clip_l", "t5xxl", "VAE"], "missing": ["VAE"],
                                      "source": "docs/sd.md:16", "citation": "sd-flux-needs-companions"},
                     "companions": [{"file": "t5.gguf", "role": "text_encoder"},
                                    {"file": "clip.gguf", "role": "text_encoder"},
                                    {"file": "clip_hi.gguf", "role": "text_encoder"},
                                    {"file": "gone.gguf", "role": "vae"}]}]},
            "weights": [{"repo": "x/y", "file": "unet.gguf", "size": 3000, "status": "single",
                         "role": "unet", "role_token": "", "usable_before_download": False},
                        {"repo": "x/y", "file": "t5.gguf", "size": 7000, "status": "agree",
                         "role": "text_encoder", "role_token": "t5xxl",
                         "usable_before_download": True},
                        {"repo": "x/y", "file": "clip.gguf", "size": 500, "status": "agree",
                         "role": "text_encoder", "role_token": "clip_l",
                         "usable_before_download": True},
                        {"repo": "x/y", "file": "clip_hi.gguf", "size": 9000, "status": "agree",
                         "role": "text_encoder", "role_token": "clip_l",
                         "usable_before_download": True}]},
    }
    out, problems = build(arts)
    o = out["per_tier"]["4GB"]["by_os"]["win-x64"]
    want = 1000 + 2000 + 3000 + 500 + 7000 + 100 + 200 + int(round(17.1 * MIB))
    if o["download_bytes"] != want:
        bad.append(f"求和不对：{o['download_bytes']} ≠ {want}")
    if out["per_tier"]["4GB"]["companion_bytes"] != 7500:
        bad.append(f"配套件没按'每个需要的旗标取最小那颗'算：{out['per_tier']['4GB']['companion_bytes']} ≠ 7500 "
                   "（clip_l 与 t5xxl 都是 flux 那一行点名的，按 role 分组会丢掉 t5xxl）")
    files = [i["file"] for i in out["per_tier"]["4GB"]["items"]]
    if "t5.gguf" not in files:
        bad.append(f"t5xxl 被丢掉或当成备选没进加法：{files}")
    if "clip_hi.gguf" in files:
        bad.append("同一颗 clip_l 的另一个量化被当成第二份必下的文件加进预算了")
    ic = out["per_tier"]["4GB"]["image_companions"]
    if not ic or ic["roles"] != ["text_encoder"] or ic["alternates"] != 1:
        bad.append(f"配套件的角色/备选数没记进产物：{ic}")
    if ic["needs_counted"] != ["clip_l", "t5xxl"]:
        bad.append(f"加法到底装了哪些旗标没记下来：{ic}")
    if ic["state"] != "incomplete" or not out["per_tier"]["4GB"]["image_lower_bound"]:
        bad.append(f"需要但仓库缺配套件的那一档没被标成下限：{ic['state']} / "
                   f"{out['per_tier']['4GB']['image_lower_bound']}")
    if ic.get("citation") != "sd-flux-needs-companions":
        bad.append(f"'这一档是下限'没把上游出处带进产物：{ic}")
    # 两张表不一致（档位表说齐了、文件表里却没有那颗旗标对应的文件）必须报问题：
    # 这就是上一版按 role 分组时发生的事 —— 少算的时候没人喊。
    dis = json.loads(json.dumps(arts))
    dis["catalog/image-tiers.json"]["tiers_rows"]["4GB"][0]["companions"] = [
        {"file": "clip.gguf", "role": "text_encoder"}]
    dis["catalog/image-tiers.json"]["tiers_rows"]["4GB"][0]["completeness"] = {
        "state": "complete", "needs": ["clip_l", "t5xxl", "VAE"], "missing": [],
        "source": "docs/sd.md:16", "citation": "sd-flux-needs-companions"}
    dp = build(dis)[1]
    if not any("需要 t5xxl" in p for p in dp):
        bad.append(f"档位表说齐、加法却少了一颗旗标时没报问题：{dp}")
    # 反向对照：上游例子只给 -m 的那一档（SD1.5）**不是**下限 —— 防的是"仓库里没配套件就等于
    # 装不齐"这种粗判，它会把能直接出图的档也标成可疑。
    sc = json.loads(json.dumps(arts))
    sc["catalog/image-tiers.json"]["tiers_rows"]["4GB"][0]["companions"] = []
    sc["catalog/image-tiers.json"]["tiers_rows"]["4GB"][0]["completeness"] = {
        "state": "self_contained", "arch": "sd1.5", "needs": [], "missing": [],
        "source": "docs/sd.md:13", "citation": "sd-example-sd15-only-model"}
    s = build(sc)[0]["per_tier"]["4GB"]
    if s["image_lower_bound"] or s["companion_bytes"]:
        bad.append("上游说只给 -m 就够的那一档被错标成下限（或还留着配套件字节）")
    if s["image_companions"]["roles"] or s["image_companions"]["state"] != "self_contained":
        bad.append(f"self_contained 那档的配套件表不对：{s['image_companions']}")
    # 认不出家族也不能算"装齐"：查不到 ≠ 不需要
    ua = json.loads(json.dumps(arts))
    ua["catalog/image-tiers.json"]["tiers_rows"]["4GB"][0]["completeness"] = {"state": "unknown_arch"}
    if not build(ua)[0]["per_tier"]["4GB"]["image_lower_bound"]:
        bad.append("unknown_arch 被当成不需要配套件了")
    if not any("gone.gguf" in p for p in problems):
        bad.append(f"配套件查不到字节却没报问题：{problems}")
    if o["missing_capabilities"] != ["字幕"]:
        bad.append(f"缺失能力没带进预算：{o['missing_capabilities']}")
    if o["unknown_landed_sizes"] != ["对话"]:
        bad.append(f"needs_extract 应记成'落地大小未知'而不是缺失：{o['unknown_landed_sizes']}")
    if "pp-ocrv6_tiny_det.onnx" not in [i["file"] for i in out["per_tier"]["4GB"]["items"]]:
        bad.append("OCR 默认档没进加法（引擎与权重之外还有它）")
    if [i["file"] for i in out["per_tier"]["4GB"]["items"]].count("b.gguf") != 1:
        bad.append("全语言合计与默认档被叠在一起加了两遍")
    if out["per_tier"]["4GB"]["untrusted_rows"] != ["ggml-tiny.bin", "unet.gguf"]:
        bad.append("哪些行要下完自算，没列出来")
    if o["minutes_at_measured_rate"] is None or o["engine_download_mib"] != 17.1:
        bad.append("等待时间或引擎负载丢了")
    # 等待时间必须是"字节 / 吞吐 / 60"，而且是独立再算一遍比出来 —— 只判 None 的话，
    # 少除一个 60（把秒当分钟报给用户）也能全绿。
    fast = float(next(r for k, r, _n in r2_manifest.RATES_MIB_S if k == "modelscope"))
    slow = float(next(r for k, r, _n in r2_manifest.RATES_MIB_S if k == "hf-mirror.com"))
    if o["minutes_at_measured_rate"] != round(want / MIB / fast / 60, 1):
        bad.append(f"分钟数不是字节/吞吐/60：{o['minutes_at_measured_rate']} ≠ "
                   f"{round(want / MIB / fast / 60, 1)}")
    if o["worst_case_hours_at_slowest_measured"] != round(want / MIB / slow / 3600, 2):
        bad.append("最慢档的小时数也不是字节/慢吞吐/3600 算出来的")
    # 查表方向：工具里用的是短针（"hf-mirror"），表里的键是主机名（"hf-mirror.com"）。
    # 方向写反了不会抛错，只会静默把慢吞吐读成 0，于是最慢档整个消失。
    for needle, key in (("modelscope", "modelscope"), ("hf-mirror", "hf-mirror.com")):
        want_rate = float(next(r for k, r, _n in r2_manifest.RATES_MIB_S if k == key))
        if rate_mib_s(needle)[0] != want_rate:
            bad.append(f"短针 '{needle}' 没匹配到表里的键 '{key}'（查表方向反了？）")
    if rate_mib_s("这台机器没量过这个出口")[0] != 0.0:
        bad.append("查不到的出口返回了速率 —— 等待时间就变成编的了")
    # OCR 的"下之前就能当真"必须从 strength.rungs 读。写死成真的那条默认档恰好全含 4.5，
    # 所以只有合成用例能证明这个字段不是装饰。
    trust = ocr_items({"catalog/defaults.json": {"entries": {
                           "pp-ocrv6_tiny_det.onnx": {"size": 1,
                                                      "strength": {"rungs": ["3", "4.5"]}},
                           "ppocrv3_chinese_dict.txt": {"size": 2,
                                                        "strength": {"rungs": ["3"]}}}},
                       "pins/ocr-languages.json": {}})
    if [i["trusted_before_download"] for i in trust] != [True, False]:
        bad.append(f"OCR 的信任标记不是从 strength.rungs 读的：{trust}")
    if [i["hash_status"] for i in trust] != ["agree", "single"]:
        bad.append(f"只有一家给哈希的 OCR 条目被标成两家一致：{[i['hash_status'] for i in trust]}")
    if check(out, out):
        bad.append(f"同一份预算自比不干净：{check(out, out)}")
    mutated = json.loads(json.dumps(out))
    mutated["per_tier"]["4GB"]["by_os"]["win-x64"]["download_bytes"] += 1
    if not check(out, mutated):
        bad.append("字节数变了 check 却不红")
    empty = json.loads(json.dumps(arts))
    empty["catalog/recommendations.json"]["tiers_rows"]["4GB"] = []
    if not any("对话 在 4GB 档没有任何行" in p for p in build(empty)[1]):
        bad.append("档位表空了，预算当无事发生")
    print("---- 结论 ----  合计 %d 字节 / %s GiB / 实测 ModelScope 约 %s 分钟"
          % (o["download_bytes"], o["download_gib"], o["minutes_at_measured_rate"]))
    for x in bad:
        print("  BAD:", x)
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="machine_budget.py")
    ap.add_argument("--check", action="store_true", help="与已提交那张比（它必须等于输入的重算）")
    ap.add_argument("--selftest", action="store_true", help="离线对照，明知会被少算的必须算出来")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    arts = load(ROOT)
    fresh, problems = build(arts)
    if a.check:
        with open(a.out, encoding="utf-8") as fh:
            committed = json.load(fh)
        bad = check(committed, fresh) + problems
        for x in bad:
            print("BAD:", x)
        print(f"比对了 {len(fresh['tiers'])} 个档位 × {len(next(iter(fresh['per_tier'].values()))['by_os'])} 个 OS，"
              f"问题 {len(bad)} 条")
        return 1 if bad else 0
    for t in fresh["tiers"]:
        for o, ov in sorted(fresh["per_tier"][t]["by_os"].items()):
            print(f"  {t:<5} {o:<12} {ov['download_gib']:>7.2f} GiB"
                  f"（引擎 {ov['engine_download_mib']:.1f} MiB，配套件 "
                  f"{round(fresh['per_tier'][t]['companion_bytes'] / 1024 ** 3, 2)} GiB）"
                  f" 实测吞吐约 {ov['minutes_at_measured_rate']} 分钟"
                  f"{'，缺:' + '/'.join(ov['missing_capabilities']) if ov['missing_capabilities'] else ''}"
                  f"{'，落地未知:' + '/'.join(ov['unknown_landed_sizes']) if ov['unknown_landed_sizes'] else ''}")
    print("要下完自算的行：", sorted({f for t in fresh['tiers']
                                for f in fresh['per_tier'][t]['untrusted_rows']}))
    for t in fresh["tiers"]:
        blk = fresh["per_tier"][t]
        if blk["image_lower_bound"]:
            ic = blk["image_companions"] or {}
            print(f"  {t} 档：生图默认行 {(ic.get('repo') or '(无)')} 还缺 "
                  f"{ic.get('missing') or '判不了这一族需要些什么'}"
                  f"（{ic.get('state')}，依据 {ic.get('source') or '—'}）"
                  " —— 这一档的生图量是**下限**，UI 不能说装完就能出图")
    for x in problems:
        print("BAD:", x)
    print("BAD 数量 =", len(problems))
    if problems:
        return 1
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(fresh, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    print("落盘", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
