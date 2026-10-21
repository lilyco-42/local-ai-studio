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
  3. **等待时间逐颗按它自己候选源里最快的一档算，并且只当量级**：引擎只发 GitHub release
     （0.014 MiB/s）、whisper 权重只有 hf-mirror（0.25）、对话/生图/OCR 有 ModelScope（4.2）。
     旧版把**所有字节**都按 4.2 除，于是首装时间被少算一个数量级（4GB 档 15.9 分钟 → 实测源
     约 150 分钟）。速率仍取自 `r2_manifest.RATES_MIB_S`，不同城市/运营商必然不同；
     产物里 `measured_at_exit` 就写这台机器，不装作普适。另给一栏"全按 ModelScope"的理想值，
     它回答的是"网络造成的差距有多大"，不是承诺。
  4. **`(合计)` 不是文件，不进字节也不进时间**：全语言 OCR 合计是"换成全语言时多出来的量"，
     它的 note 一直写着不能叠在默认档上，但旧版求和把它加进了 download/first_run 字节。

macOS 那一档现在必然缺一块：`pins/payload-plan.json` 的 gaps 里写着
**字幕在 macos-arm64 上游无产物**（whisper.cpp 不为 Apple 平台发可执行文件，只发 xcframework 库；逐文件成员表由 payload-plan 的 `asset_evidence` 记着），所以 macOS 的预算
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
             "catalog/image-tiers.json", "pins/ocr.json")
OUT = os.path.join(ROOT, "catalog", "machine-budget.json")
# 每个档位取"这张表该档的第一行"当默认档。顺序是被上游数字决定的，不是我觉得哪个好用。
CAPS = ("chat", "subtitle", "image")
# 首装默认装哪几类。这是**产品约束第 5 条**（docs/requirements.md），不是我想省事的取舍：
# 生图最省的量化档实测也 1,494 MiB，按这台 CN 出口量到的速率要 25–30 分钟，
# 而 OCR（det+rec 4.6+15.8 MiB）与字幕（181.3 MiB）是分钟级 —— 所以生图必须显式触发、后台、可续传。
# 这一条此前**只写在文档里，预算与首启队列都把它当"默认装"**：同一份预研里两个生产者给了相反的答案，
# UI 上那句"首装约 X 分钟"就会把生图的量算进用户刚点完"安装"的那段时间。
DEFAULT_INSTALL = {"chat": True, "subtitle": True, "ocr": True, "image": False}
# 逐 OS 要比的字段。等待时间也在里面：它是从 r2_manifest 的实测吞吐算出来的，
# 吞吐表一改这份预算就必须重出，否则 UI 上那句"约 X 分钟"就和量出来的速率脱钩了。
CHECKED_OS_KEYS = ("download_bytes", "engine_download_mib", "engine_landed_mib",
                   "engine_minutes_at_github_rate",
                   "total_seconds_at_fastest_sources", "minutes_at_measured_rate",
                   "worst_case_hours_at_slowest_measured",
                   "first_run_bytes", "first_run_gib", "first_run_seconds_at_fastest_sources",
                   "first_run_minutes_at_measured_rate",
                   "first_run_worst_case_hours_at_slowest_measured",
                   "ideal_all_modelscope_minutes", "first_run_ideal_all_modelscope_minutes",
                   "deferred_image_bytes")


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


def url_rate(url: str) -> tuple[float, str]:
    """按 URL 认出口。不认 provider 字段的写法：字幕表写 'hf'，别处写主机名。"""
    for needle in ("github.com", "hf-mirror.com", "modelscope", "huggingface.co"):
        if needle in url:
            if needle == "huggingface.co":
                return 0.0, needle          # CN 直连实测 0 字节：只作兜底，不参与"最快"
            return rate_mib_s(needle)[0], needle
    return 0.0, "unknown"


def pick_rates(urls: list[str], absent: list[str] | tuple = ()) -> dict:
    """一颗文件的候选源里：最快 / 最慢的**有实测速率**的出口，各自要多少秒。

    `distributor_absent` 是现成的证据（whisper 那批 ModelScope 0 条），
    不删掉它就会拿一条已知 404 的源算出 4.2 MiB/s 的假时间。
    """
    cands = []
    for u in urls or []:
        if not str(u).startswith("https://"):
            continue
        if any(a and a in u for a in absent or ()):
            continue
        rate, source = url_rate(u)
        if rate > 0:
            cands.append((rate, source))
    empty = {"fastest_source": None, "measured_mib_s": None, "seconds_at_fastest_measured": None,
             "slowest_source": None, "slowest_mib_s": None, "seconds_at_slowest_measured": None}
    return empty if not cands else {
        "fastest_source": max(cands)[1], "measured_mib_s": max(cands)[0],
        "slowest_source": min(cands)[1], "slowest_mib_s": min(cands)[0],
    }


def add_time(item: dict) -> dict:
    """给一行补上"按它自己最快的源要几秒 / 最慢的源要几秒"。不可下载的行两项都是 None。"""
    if item.get("queueable") is False or item.get("resolve_via") == "not_a_file":
        item.update(fastest_source=None, measured_mib_s=None, seconds_at_fastest_measured=None,
                    slowest_source=None, slowest_mib_s=None, seconds_at_slowest_measured=None)
        return item
    r = pick_rates(item.get("urls") or [], item.get("distributor_absent") or ())
    item.update(r)
    for key, rate in (("fastest", r["measured_mib_s"]), ("slowest", r["slowest_mib_s"])):
        item[f"seconds_at_{key}_measured"] = (round(item["bytes"] / MIB / rate, 6)
                                              if rate else None)
    return item


def downloadable(item: dict) -> bool:
    return item.get("queueable") is not False and item.get("resolve_via") != "not_a_file"


def sum_seconds(rows: list[dict], key: str) -> float | None:
    vals = [i.get(key) for i in rows]
    if not vals or any(v is None for v in vals):
        return None
    return round(sum(vals), 6)


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

    # 档位表的 `tiers_rows` 是给人看的一行摘要，**不带 urls**；字幕的完整行在同文件 `rows`，
    # 生图的在 `weights`。只读摘要行会让每颗权重都"没有源"，时间整栏变 None。
    sub_src = {(r.get("repo"), r.get("file")): r
               for r in arts["catalog/subtitle-tiers.json"].get("rows") or []}
    img_src = {(r.get("repo"), r.get("file")): r
               for r in arts["catalog/image-tiers.json"].get("weights") or []}

    def routed(row: dict, table: str) -> dict:
        src = row
        if table == "subtitle":
            src = sub_src.get((row.get("repo"), row.get("file")), row)
        elif table == "image":
            src = img_src.get((row.get("repo"), row.get("file")), row)
        return {"urls": src.get("urls") or row.get("urls") or [],
                "distributor_absent": (src.get("distributor_absent")
                                       or row.get("distributor_absent") or [])}

    chat = one("catalog/recommendations.json", tier, "对话")
    if chat:
        items.append({"kind": "chat", "name": chat["name"], "file": chat["file"],
                      "repo": chat.get("repo"), "sha256": chat.get("sha256"),
                      "bytes": int(chat["size"]), "hash_status": chat["status"],
                      "trusted_before_download": chat["usable_before_download"],
                      "from": "catalog/recommendations.json", **routed(chat, "chat")})
    sub = one("catalog/subtitle-tiers.json", tier, "字幕")
    if sub:
        items.append({"kind": "subtitle", "name": sub["name"], "file": sub["file"],
                      "repo": sub.get("repo"), "sha256": sub.get("sha256"),
                      "bytes": int(sub["size"]), "hash_status": "single",
                      "trusted_before_download": False,
                      "mem_text": sub.get("mem_text"),
                      "from": "catalog/subtitle-tiers.json", **routed(sub, "subtitle")})
    img = one("catalog/image-tiers.json", tier, "生图")
    if img:
        weights = {(r["repo"], r["file"]): r for r in arts["catalog/image-tiers.json"]["weights"]}
        items.append({"kind": "image", "name": img["repo"], "file": img["file"],
                      "repo": img["repo"], "sha256": img.get("sha256"),
                      "bytes": int(img["size"]), "hash_status": img["status"],
                      "trusted_before_download": img["usable_before_download"],
                      "role": "unet", "from": "catalog/image-tiers.json", **routed(img, "image")})
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
                          "repo": row["repo"], "sha256": row.get("sha256"),
                          "bytes": int(row["size"]), "hash_status": row["status"],
                          "trusted_before_download": row["usable_before_download"],
                          "role": row["role"], "need_key": key, "companion_of": img["file"],
                          "group_alternates": sum(1 for r in found if need_key(r) == key) - 1,
                          "from": "catalog/image-tiers.json", **routed(row, "image")})
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
    # 每一项都得说自己该走哪条确认路线（首启队列按这个分流，不会把 GitHub release 那几颗
    # 当成 HF 仓库去查，也不会把"没查"混成"查过"）
    for i in items:
        i.setdefault("resolve_via", "hf_metadata")
        add_time(i)
    return items, problems, companions


def ocr_items(arts: dict, problems: list[str] | None = None) -> list[dict]:
    """OCR 的两个数：默认档三条（已钉字节）与全部 14 种语言的合计（上游成对表算出来的）。

    计时用的源取自 `pins/ocr.json`：`mirrors`（ModelScope，整包复核过）与 release 地址。
    只写一条源就等于把"能不能换源"这件事从预算里抹掉 —— 镜像不在 pin 里就不许计时。
    """
    out = []
    pins = arts.get("pins/ocr.json") or {}
    for name, e in sorted(arts["catalog/defaults.json"]["entries"].items()):
        if not re.search(r"ocr|ppocr", name, re.I):
            continue
        pin = pins.get(name) or {}
        urls = [m.get("url") for m in (pin.get("mirrors") or []) if m.get("url")]
        primary = ((pin.get("bytes_verified") or {}).get("url")
                   or pin.get("primary") or "")
        if primary:
            urls.append(primary)
        if not urls and problems is not None:
            problems.append(f"{name}: pins/ocr.json 里没有源地址 —— 不知道按哪条出口计时")
        out.append(add_time({"kind": "ocr", "name": "默认档", "file": name,
                             "repo": (e.get("meta_hashes") or {}).get("repo"),
                             "sha256": e.get("sha256"), "bytes": int(e["size"]),
                             "hash_status": "agree" if "4.5" in (e.get("strength") or {}).get("rungs", [])
                             else "single",
                             "trusted_before_download": "4.5" in (e.get("strength") or {}).get("rungs", []),
                             # OCR 默认档这三颗来自 GitHub release（不是 HF 仓库），所以它的确认路线
                             # 与另外三项不同：release 的 asset digest。队列工具按这个字段分流，
                             # 不许把"这条我没查"混进"这条查过了"。
                             "resolve_via": "github_release_digest",
                             "urls": urls, "distributor_absent": [],
                             "from": "catalog/defaults.json"}))
    agg = arts["pins/ocr-languages.json"]
    if "rec_dict_bytes" not in agg:
        agg = agg.get("aggregate") or {}
    if agg.get("rec_dict_bytes"):
        out.append(add_time({"kind": "ocr", "name": "全 14 种语言（rec+dict）", "file": "(合计)",
                             "repo": None, "sha256": None,
                             "bytes": int(agg["rec_dict_bytes"]), "hash_status": "mixed",
                             "trusted_before_download": None, "from": "pins/ocr-languages.json",
                             "queueable": False, "resolve_via": "not_a_file",
                             "note": "这是把默认档换成全语言时多出来的量，不是叠在默认档上；"
                                     "它不是一个文件，所以进不了首启队列、也不进字节与时间"
                                     "（不等于没验过 —— 逐条在 pins/ocr-languages.json 与 "
                                     "ocr-languages 车道）"}))
    return out


def build(arts: dict) -> tuple[dict, list[str]]:
    problems: list[str] = []
    plan = arts["pins/payload-plan.json"]
    tiers = list(arts["catalog/recommendations.json"]["tiers"])
    per: dict[str, dict] = {}
    for tier in tiers:
        items, p, comps = weight_items(arts, tier)
        problems += p
        items += ocr_items(arts, problems)
        for it in items:
            if it["kind"] not in DEFAULT_INSTALL:
                problems.append(f"{tier}: 出现未知的 kind={it['kind']!r} —— "
                                "首装默认那张政策表没这一类，不许猜一个默认值")
                continue
            it["default_install"] = DEFAULT_INSTALL[it["kind"]]
        real = [i for i in items if downloadable(i)]
        deferred = sum(i["bytes"] for i in real if not i.get("default_install", True))
        if not p and not items:
            problems.append(f"{tier} 档一项权重都没有，预算等于空表")
        by_os = {}
        for os_key, eng in sorted(plan["os"].items()):
            eng_mib = float(plan["totals_mib"][os_key])
            engine_bytes = int(round(eng_mib * MIB))
            total_bytes = sum(i["bytes"] for i in real) + engine_bytes
            # 只有"真的有引擎产物"的行才参与落地大小：没产物那一行是**缺席不是未知**，
            # 把它算进 all(...) 会让整个 macOS 的落地大小被一个不存在的包拖成未知。
            landed = [r.get("uncompressed_mib") for r in eng if r.get("engine")]
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
            fast, _ = rate_mib_s("modelscope")
            gh, _ = rate_mib_s("github.com")
            # 引擎只发 GitHub release（0.014 MiB/s）；权重逐颗按候选源里最快的一档。
            # 旧版拿 4.2 MiB/s 除全部字节，4GB 档首装因此从约 150 分钟被写成 15.9 分钟。
            eng_sec = round(engine_bytes / MIB / gh, 6) if gh else None
            first_real = [i for i in real if i.get("default_install", True)]
            first_bytes = sum(i["bytes"] for i in first_real) + engine_bytes
            all_fast = sum_seconds(real, "seconds_at_fastest_measured")
            all_slow = sum_seconds(real, "seconds_at_slowest_measured")
            first_fast = sum_seconds(first_real, "seconds_at_fastest_measured")
            first_slow = sum_seconds(first_real, "seconds_at_slowest_measured")
            total_sec = (round(eng_sec + all_fast, 6)
                         if eng_sec is not None and all_fast is not None else None)
            first_sec = (round(eng_sec + first_fast, 6)
                         if eng_sec is not None and first_fast is not None else None)
            total_worst = (eng_sec + all_slow
                           if eng_sec is not None and all_slow is not None else None)
            first_worst = (eng_sec + first_slow
                           if eng_sec is not None and first_slow is not None else None)
            by_os[os_key] = {
                "engine_download_mib": eng_mib,
                # tar.gz 读不了中央目录（payload-plan 里标着 needs_extract）→ 落地大小只能是未知，
                # 于是这一档的"磁盘总量"是**下限**，不是承诺。宁可少说，不要编。
                "engine_landed_mib": landed_mib if all(v is not None for v in landed) else None,
                "engine_rows": rows,
                "download_bytes": total_bytes,
                "download_gib": round(total_bytes / 1024 ** 3, 2),
                "engine_minutes_at_github_rate": round(eng_sec / 60, 1) if eng_sec else None,
                "total_seconds_at_fastest_sources": total_sec,
                "minutes_at_measured_rate": (round(total_sec / 60, 1)
                                             if total_sec is not None else None),
                "worst_case_hours_at_slowest_measured": (round(total_worst / 3600, 2)
                                                         if total_worst is not None else None),
                "first_run_bytes": first_bytes,
                "first_run_gib": round(first_bytes / 1024 ** 3, 2),
                "first_run_seconds_at_fastest_sources": first_sec,
                "first_run_minutes_at_measured_rate": (round(first_sec / 60, 1)
                                                       if first_sec is not None else None),
                "first_run_worst_case_hours_at_slowest_measured": (
                    round(first_worst / 3600, 2) if first_worst is not None else None),
                # 旧口径（全部按 ModelScope）留着只为回答"网络造成的差距有多大"，
                # 引擎与 whisper 现在没有 ModelScope 源，它不是任何一台机器能达成的时间。
                "ideal_all_modelscope_minutes": (round(total_bytes / MIB / fast / 60, 1)
                                                 if fast else None),
                "first_run_ideal_all_modelscope_minutes": (
                    round(first_bytes / MIB / fast / 60, 1) if fast else None),
                "deferred_image_bytes": deferred,
                "missing_capabilities": missing,
                "unknown_landed_sizes": unknown_landed,
            }
            if deferred > total_bytes or first_bytes > total_bytes:
                problems.append(f"{tier}/{os_key}: 首装量算得比总量还大 —— 减法用错了")
        per[tier] = {
            "items": sorted(items, key=lambda i: (i["kind"], -i["bytes"])),
            "weights_bytes": sum(i["bytes"] for i in real),
            "not_downloaded_bytes": sum(i["bytes"] for i in items if not downloadable(i)),
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
    first_totals = {t: {o: per[t]["by_os"][o]["first_run_gib"] for o in per[t]["by_os"]}
                    for t in tiers}
    return {"schema": 1,
            "from": list(ARTIFACTS),
            "rule": {"sum": "只加已钉产物里的字节：引擎净负载(payload-plan totals_mib) + "
                            "各档默认权重(三张档位表该档第一行) + OCR(默认档三条已钉字节)；"
                            "`resolve_via=not_a_file` 的行（OCR 全语言合计）只作信息，"
                            "不进字节也不进时间",
                     "default_install": "首装默认只下 " + "、".join(
                         sorted(k for k, v in DEFAULT_INSTALL.items() if v)) +
                         "；" + "、".join(sorted(k for k, v in DEFAULT_INSTALL.items() if not v)) +
                         " 按产品约束第 5 条要显式触发 + 后台 + 可续传（最省的量化档实测也 1,494 MiB，"
                         "按这台 CN 出口量到的速率是 25–30 分钟，而 OCR 4.6+15.8 MiB、字幕 181.3 MiB "
                         "是分钟级）。download_* 是装齐四项能力的总量，first_run_* 才是点完安装要等的量，"
                         "两个数都进产物，UI 不许把它们混成一个",
                     "image_companions": "生图的配套件（clip/t5/vae）按 weights 里的真实字节加，"
                                          "查不到就报问题、不估算；同一角色的多个量化是**备选**不是都要下，"
                                          "只把最小那颗计入，其余记 alternates；"
                                          "档位表判为 incomplete（需要但同仓库没有）或 unknown_arch（判不了）"
                                          "的那些档，生图部分只是下限，判定依据是上游 docs/sd.md 的 "
                                          "txt2img 例子那四行（每行都有 file:line 出处）",
                     "ram": "不加内存：四项能力共用一个常驻引擎、一次跑一项，"
                            "内存那一轴由各档位表自己的判据负责",
                     "time": "等待时间逐颗按它自己候选源里最快的实测出口相加（引擎只发 GitHub release "
                             "0.014、whisper 只有 hf-mirror 0.25、对话/生图/OCR 有 ModelScope 4.2），"
                             "源缺席（distributor_absent）的不参与；ideal_all_modelscope_* 是"
                             "「全部字节都走 4.2」的理想值，只用来量网络差距，不是任何机器的承诺",
                     "ocr": "默认档三条与全 14 语言合计是两个数，不能相加"},
            "measured_at_exit": "lain42.top（CN 出口，2026-10 一次量）",
            "rates_mib_s": {"modelscope": rate_mib_s("modelscope")[0],
                            "hf-mirror": rate_mib_s("hf-mirror")[0],
                            "github": rate_mib_s("github.com")[0]},
            "ram_statement": "本文件的 download_bytes 是磁盘与下载量，不是峰值内存",
            "tiers": tiers, "totals_gib": totals, "first_run_totals_gib": first_totals,
            "per_tier": per}, problems


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
    for key in ("totals_gib", "first_run_totals_gib"):
        if committed.get(key) != fresh.get(key):
            bad.append(f"{key} 变了 {committed.get(key)} → {fresh.get(key)}"
                       "（装齐与首装是两个数，UI 上那句'约 X GiB / 约 Y 分钟'按哪一个说的要跟着重看）")
    for t in fresh["tiers"]:
        c, f = committed["per_tier"].get(t, {}), fresh["per_tier"][t]
        if [(i["file"], i.get("repo"), i.get("sha256")) for i in c.get("items", [])] != \
                [(i["file"], i.get("repo"), i.get("sha256")) for i in f["items"]]:
            bad.append(f"{t}: 默认权重行（文件名 / 仓库 / 钉的哈希）变了 "
                       f"{[(i['file'], i.get('repo'), str(i.get('sha256'))[:8]) for i in c.get('items', [])]} → "
                       f"{[(i['file'], i.get('repo'), str(i.get('sha256'))[:8]) for i in f['items']]}"
                       "（档位表被重算过，预算与首启队列都要跟着重出）")
            continue
        # 逐行的字节与"按哪条源、多快"也要比：只比文件名/哈希的话，把 whisper 的源手改成
        # ModelScope（4.2）或把 (合计) 的 123 MB 塞回权重和，汇总仍可能蒙混过关。
        for label, keys in (
                ("字节", ("bytes", "queueable", "resolve_via")),
                ("计时源", ("fastest_source", "measured_mib_s", "slowest_source",
                            "slowest_mib_s", "seconds_at_fastest_measured",
                            "seconds_at_slowest_measured"))):
            before = [(i["file"], *(i.get(k) for k in keys)) for i in c.get("items", [])]
            after = [(i["file"], *(i.get(k) for k in keys)) for i in f["items"]]
            if before != after:
                bad.append(f"{t}: 逐行的{label}变了 {before} → {after}")
        for k in ("weights_bytes", "not_downloaded_bytes"):
            if c.get(k) != f.get(k):
                bad.append(f"{t}.{k}: {c.get(k)} → {f.get(k)}")
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
        "pins/ocr.json": {"pp-ocrv6_tiny_det.onnx": {
            "mirrors": [{"source": "modelscope",
                         "url": "https://www.modelscope.cn/models/g/o/resolve/master/"
                                "pp-ocrv6_tiny_det.onnx"}],
            "primary": "https://github.com/g/o/releases/download/v1/pp-ocrv6_tiny_det.onnx"}},
        "catalog/recommendations.json": {"tiers": ["4GB"], "tiers_rows": {
            "4GB": [{"name": "a/b", "file": "b.gguf", "size": 1000, "status": "agree",
                     "usable_before_download": True,
                     "urls": ["https://www.modelscope.cn/models/a/b/resolve/master/b.gguf",
                              "https://hf-mirror.com/a/b/resolve/main/b.gguf"]}]}},
        "catalog/subtitle-tiers.json": {"tiers_rows": {
            "4GB": [{"name": "tiny", "file": "ggml-tiny.bin", "size": 2000,
                     "mem_text": "273 MB", "distributor_absent": ["modelscope"],
                     # 字幕表里就有一条已知 404 的 ModelScope 地址：不按缺席证据删掉，
                     # 这颗会被算成 4.2 MiB/s，首装时间直接少一个数量级。
                     "urls": ["https://www.modelscope.cn/models/ggerganov/whisper.cpp/resolve/"
                              "master/ggml-tiny.bin",
                              "https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/"
                              "ggml-tiny.bin"]}]}},
        "catalog/image-tiers.json": {"tiers_rows": {
            "4GB": [{"repo": "x/y", "file": "unet.gguf", "size": 3000, "status": "single",
                     "usable_before_download": False,
                     "urls": ["https://www.modelscope.cn/models/x/y/resolve/master/unet.gguf"],
                     "completeness": {"state": "incomplete", "arch": "flux",
                                      "needs": ["clip_l", "t5xxl", "VAE"], "missing": ["VAE"],
                                      "source": "docs/sd.md:16", "citation": "sd-flux-needs-companions"},
                     "companions": [{"file": "t5.gguf", "role": "text_encoder"},
                                    {"file": "clip.gguf", "role": "text_encoder"},
                                    {"file": "clip_hi.gguf", "role": "text_encoder"},
                                    {"file": "gone.gguf", "role": "vae"}]}]},
            "weights": [{"repo": "x/y", "file": f, "size": n, "status": "single",
                         "role": role, "role_token": token, "usable_before_download": usable,
                         "urls": [f"https://www.modelscope.cn/models/x/y/resolve/master/{f}"]}
                        for f, n, role, token, usable in (
                            ("unet.gguf", 3000, "unet", "", False),
                            ("t5.gguf", 7000, "text_encoder", "t5xxl", True),
                            ("clip.gguf", 500, "text_encoder", "clip_l", True),
                            ("clip_hi.gguf", 9000, "text_encoder", "clip_l", True))]},
    }
    out, problems = build(arts)
    o = out["per_tier"]["4GB"]["by_os"]["win-x64"]
    # 200 是 OCR 全语言合计（not_a_file）：它只作信息，不进字节。
    want = 1000 + 2000 + 3000 + 500 + 7000 + 100 + int(round(17.1 * MIB))
    if o["download_bytes"] != want:
        bad.append(f"求和不对（合计行是否被加进去了？）：{o['download_bytes']} ≠ {want}")
    agg_item = next((i for i in out["per_tier"]["4GB"]["items"] if i["file"] == "(合计)"), None)
    if agg_item is None:
        bad.append("OCR 默认档没进加法：items 里连 (合计) 都没有")
    elif agg_item["seconds_at_fastest_measured"] is not None:
        bad.append("不是文件的行被算出了下载时间")
    if out["per_tier"]["4GB"]["not_downloaded_bytes"] != 200:
        bad.append("被排除的合计字节没记下来")
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
    # 等待时间必须逐颗按**它自己的源**算，而且独立再算一遍比：用一个全局 4.2 除全部字节
    # （旧版就是这么错的）会让 4GB 档首装从约 150 分钟变成 15.9 分钟。
    gh = float(next(r for k, r, _n in r2_manifest.RATES_MIB_S if k == "github.com"))
    ms = float(next(r for k, r, _n in r2_manifest.RATES_MIB_S if k == "modelscope"))
    hf = float(next(r for k, r, _n in r2_manifest.RATES_MIB_S if k == "hf-mirror.com"))

    def secs(b, rate):
        return round(b / MIB / rate, 6)

    by_file = {i["file"]: i for i in out["per_tier"]["4GB"]["items"]}
    if by_file["ggml-tiny.bin"]["fastest_source"] != "hf-mirror.com":
        bad.append(f"缺席证据没生效：whisper 仍按 ModelScope 计时 {by_file['ggml-tiny.bin']}")
    if by_file["b.gguf"]["fastest_source"] != "modelscope":
        bad.append(f"有 ModelScope 的对话档没选它当最快源 {by_file['b.gguf']}")
    ocr_row = by_file.get("pp-ocrv6_tiny_det.onnx")
    if ocr_row is None:
        bad.append("OCR 默认档没进加法：逐源计时里也没有它")
    elif ocr_row["slowest_source"] != "github.com":
        bad.append(f"OCR 的最慢源不是 release {ocr_row}")
    agg_row = by_file.get("(合计)")
    if agg_row is None:
        bad.append("OCR 默认档没进加法：连 (合计) 那一行都没有")
    elif agg_row["fastest_source"] is not None:
        bad.append("不是文件的行被选出了一个源")
    eng_sec = secs(17.1 * MIB, gh)
    img_bytes = sum(i["bytes"] for i in out["per_tier"]["4GB"]["items"] if i["kind"] == "image")
    first_bytes = want - img_bytes
    first_fast = round(eng_sec + secs(1000, ms) + secs(2000, hf) + secs(100, ms), 6)
    all_fast = round(first_fast + secs(img_bytes, ms), 6)
    # 最慢一档用的是每颗**可用的**最慢源（hf.co 直连 0 字节不算"慢"，它不可用）。
    first_worst = round(eng_sec + secs(1000, hf) + secs(2000, hf) + secs(100, gh), 6)
    all_worst = round(first_worst + secs(img_bytes, ms), 6)
    for label, got, want_sec in (("装齐", o["total_seconds_at_fastest_sources"], all_fast),
                                 ("首装", o["first_run_seconds_at_fastest_sources"], first_fast)):
        if got is None or abs(got - want_sec) > 1e-3:
            bad.append(f"{label}秒数不是逐颗按最快源算出来的：{got} ≠ {want_sec}")
    if o["minutes_at_measured_rate"] != round(all_fast / 60, 1):
        bad.append(f"分钟数没从秒数推：{o['minutes_at_measured_rate']}")
    if o["first_run_minutes_at_measured_rate"] != round(first_fast / 60, 1):
        bad.append(f"首装分钟数没从秒数推：{o['first_run_minutes_at_measured_rate']}")
    if o["worst_case_hours_at_slowest_measured"] != round(all_worst / 3600, 2):
        bad.append("最慢档不是每颗按可用最慢源相加")
    if o["first_run_worst_case_hours_at_slowest_measured"] != round(first_worst / 3600, 2):
        bad.append("首装最慢档不是逐颗按可用最慢源相加")
    if o["ideal_all_modelscope_minutes"] != round(want / MIB / ms / 60, 1):
        bad.append("全 ModelScope 的理想值不是独立算的")
    if o["first_run_ideal_all_modelscope_minutes"] != round(first_bytes / MIB / ms / 60, 1):
        bad.append("首装的全 ModelScope 理想值不是独立算的")
    if o["engine_minutes_at_github_rate"] != round(eng_sec / 60, 1):
        bad.append("引擎没按 GitHub release 的速率单独计时")
    # 首装与装齐是两个数（产品约束第 5 条）。这里全部**独立再算一遍**比：
    # 只判"字段在不在"的话，把生图也偷偷算进首装是不会红的。
    if img_bytes <= 0:
        bad.append("夹具里没有生图权重，下面三条对照全是空转")
    if o["deferred_image_bytes"] != img_bytes:
        bad.append(f"显式触发的那部分算错：{o['deferred_image_bytes']} ≠ {img_bytes}")
    if o["first_run_bytes"] != o["download_bytes"] - o["deferred_image_bytes"]:
        bad.append(f"首装量不是总量减负量：{o['first_run_bytes']} vs "
                   f"{o['download_bytes']} - {o['deferred_image_bytes']}")
    # 这张政策表必须是**读出来的**：翻一下它，首装量就要跟着变，否则说明代码里另写死了一份。
    saved_di = DEFAULT_INSTALL["image"]
    try:
        DEFAULT_INSTALL["image"] = True
        flipped = build(arts)[0]["per_tier"]["4GB"]["by_os"]["win-x64"]
    finally:
        DEFAULT_INSTALL["image"] = saved_di
    if flipped["deferred_image_bytes"] != 0 or flipped["first_run_bytes"] != flipped["download_bytes"]:
        bad.append("把政策表里生图改成默认装之后首装量没跟着变 —— 这个数不是从政策读的")
    if build(arts)[0]["per_tier"]["4GB"]["by_os"]["win-x64"]["deferred_image_bytes"] != img_bytes:
        bad.append("还原政策后首装量没还原（对照之间脏了数据）")
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
    # 合计行只作信息：把它的数字改大，下载量与时间都不许动。
    agg_art = json.loads(json.dumps(arts))
    agg_art["pins/ocr-languages.json"]["aggregate"]["rec_dict_bytes"] = 999999
    agg_out = build(agg_art)[0]["per_tier"]["4GB"]["by_os"]["win-x64"]
    if agg_out["download_bytes"] != want or agg_out["download_bytes"] != o["download_bytes"]:
        bad.append("合计行字节变了，下载量跟着变 —— not_a_file 仍在参与求和")
    # 没有源就没有时间：不许退回一个全局速率把它填上。
    no_url = json.loads(json.dumps(arts))
    no_url["catalog/subtitle-tiers.json"]["tiers_rows"]["4GB"][0]["urls"] = []
    nu = build(no_url)[0]["per_tier"]["4GB"]["by_os"]["win-x64"]
    if nu["first_run_seconds_at_fastest_sources"] is not None:
        bad.append("没有候选源的行仍被算出了下载时间")
    empty = json.loads(json.dumps(arts))
    empty["catalog/recommendations.json"]["tiers_rows"]["4GB"] = []
    if not any("对话 在 4GB 档没有任何行" in p for p in build(empty)[1]):
        bad.append("档位表空了，预算当无事发生")
    print("---- 结论 ----  装齐 %d 字节 / %s GiB / 约 %s 分钟；"
          "首装 %d 字节 / %s GiB / 约 %s 分钟（生图 %d 字节按约束第 5 条显式触发）"
          % (o["download_bytes"], o["download_gib"], o["minutes_at_measured_rate"],
             o["first_run_bytes"], o["first_run_gib"], o["first_run_minutes_at_measured_rate"],
             o["deferred_image_bytes"]))
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
                  f" 逐源实测约 {ov['minutes_at_measured_rate']} 分钟"
                  f"（引擎单 GitHub {ov['engine_minutes_at_github_rate']} 分钟；"
                  f"全按 ModelScope 的理想值 {ov['ideal_all_modelscope_minutes']} 分钟）"
                  f"{'，缺:' + '/'.join(ov['missing_capabilities']) if ov['missing_capabilities'] else ''}"
                  f"{'，落地未知:' + '/'.join(ov['unknown_landed_sizes']) if ov['unknown_landed_sizes'] else ''}")
            print(f"        其中首装（不含生图）{ov['first_run_gib']:>7.2f} GiB"
                  f" 约 {ov['first_run_minutes_at_measured_rate']} 分钟"
                  f"（理想 {ov['first_run_ideal_all_modelscope_minutes']} 分钟）；"
                  f"生图 {round(ov['deferred_image_bytes'] / 1024 ** 2, 1)} MiB 要显式触发")
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
