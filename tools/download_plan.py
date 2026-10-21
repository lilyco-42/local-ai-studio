#!/usr/bin/env python3
"""把"首启要下哪些文件、每个文件从哪几个源下"落成一张可被外壳直接吃的表。

`catalog/machine-budget.json` 只回答"哪一档要下什么、一共多少字节"，
`catalog/{recommendations,subtitle-tiers,image-tiers}.json` 只回答"这颗文件有哪几个候选源"，
两张表此前没有被接起来 —— 下载器拿不到 URL，档位表也不知道首装顺序。
`pins/download-plan.json` 就是这条接缝：**行来自预算，源来自档位表，顺序来自实测吞吐**。

四条口径写死，别让下游自己猜：

  1. **不编 URL。** 每个源都必须能在某张已提交的档位表里找到同一颗
     `repo + file + sha256` 的行；找不到就是两张表漂了，构建直接报问题，不按模板猜一个。
  2. **已知不存在的源要删掉。** whisper.cpp 那次捕获里 ModelScope 对该仓库 0 条，
     档位表把它记在 `distributor_absent` 里；把一条已知 404 的源排在第一位，
     等于每次下载都先浪费一次重试。
  3. **顺序只按实测吞吐。** ModelScope 4.2 / hf-mirror 0.25 / GitHub 0.014 MiB/s
     （`r2_manifest.RATES_MIB_S`，2026-10-19 lain42.top）。`huggingface.co` 直连在 CN
     实测 0 字节，只能是最后兜底 —— 测试会拒绝"它排第一"。
  4. **合计行不是文件。** OCR 那行 `(合计)` 是 14 种语言的 rec+dict 总量，
     `resolve_via=not_a_file`，进 `skipped`，绝不进下载队列。

OCR 默认档三颗的源同样只认 pin：GitHub release URL 来自 `bytes_verified`，其余来自
`mirrors`，每条镜像的 `sha256`/`bytes` 必须与 pin 逐字相等，否则 build 报问题而不是照抄。
ModelScope 那条是 2026-10-20 整包下载后复核过哈希的；R2 镜像还没建（#23），
这件事写在 `gaps` 里，不假装"国内源已经齐了"。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import file_index                                        # noqa: E402
import r2_manifest                                       # noqa: E402

BUDGET = "catalog/machine-budget.json"
OCR_PINS = "pins/ocr.json"
RECO = "catalog/recommendations.json"
SUBTITLE = "catalog/subtitle-tiers.json"
IMAGE = "catalog/image-tiers.json"
OUT = "pins/download-plan.json"

KIND_ORDER = {"chat": 0, "subtitle": 1, "ocr": 2, "image": 3}
PROVIDER_OF_SOURCE = {
    "modelscope": "modelscope",
    "hf-mirror.com": "hf-mirror.com",
    "huggingface.co": "huggingface.co",
    "github.com": "github.com",
}
# 实测吞吐表里没有的源只能当兜底；速率 0 且不是最后一名的源一律算排序错误。
POLICY = {
    "item_order": "默认安装先；同组按 chat < subtitle < ocr < image，再按字节升序",
    "source_order": "measured_mib_s 降序；速率相同保持来源表里（pin 里）的候选顺序",
    "default_install_min_sources": 2,
    "attempts_per_source": 2,
    "low_speed": {"bytes_per_s": 65536, "seconds": 30},
    "resume": "Range: bytes=<已有字节>-；206 追加，200 丢弃 .part 重下，416/404 保留进度换源",
    "verify": "收满 bytes 后算 sha256；不符删掉 .part 从 0 重来；通过后落 blobs/<sha> 再硬链接到文件名",
    "known_absent_sources_removed": True,
}


def read_json(rel: str) -> dict:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return json.load(fh)


def rate_of(source: str) -> tuple[float, str]:
    for needle, rate, note in r2_manifest.RATES_MIB_S:
        if needle == source:
            return rate, note
    raise ValueError(f"没有 {source!r} 的实测吞吐 —— 不许编一个 0 当排序依据")


def tier_rows(arts: dict) -> list[dict]:
    """三张档位表摊平成一张行表：每行至少 {repo, file, sha256, providers?, distributor_absent?}。"""
    out: list[dict] = []
    for rows in arts["recommendations"]["tiers_rows"].values():
        out.extend(dict(r, _table="recommendations") for r in rows)
    out.extend(dict(r, _table="subtitle-tiers") for r in arts["subtitle"]["rows"])
    out.extend(dict(r, _table="image-tiers") for r in arts["image"]["weights"])
    return out


def absent_for(repo: str, name: str, sha: str, rows: list[dict]) -> set[str]:
    """只认同一颗文件那一行的证据；providers 里没有 ModelScope 也算缺席。"""
    absent: set[str] = set()
    matched = 0
    for r in rows:
        if r.get("repo") != repo or r.get("file") != name or r.get("sha256") != sha:
            continue
        matched += 1
        absent |= set(r.get("distributor_absent") or [])
        providers = r.get("providers")
        if providers is not None:
            if not any("modelscope" in p for p in providers):
                absent.add("modelscope")
    if not matched:
        raise ValueError(f"{repo}/{name}: 三张档位表里找不到同一颗 sha256 的行 —— 表漂了，不按模板猜 URL")
    return absent


def ocr_sources(it: dict, pins: dict) -> list[dict]:
    """release URL + pin 里记过、且哈希/体积与 pin 相等的镜像；顺序由实测吞吐决定。"""
    name = it["file"]
    pin = pins.get(name)
    if not pin:
        raise ValueError(f"{name}: pins/ocr.json 里没有这一颗")
    if pin.get("sha256") != it.get("sha256") or pin.get("size") != it.get("bytes"):
        raise ValueError(f"{name}: pins/ocr.json 与预算的体积/哈希不一致")
    release = ((pin.get("bytes_verified") or {}).get("url") or pin.get("primary") or "").strip()
    if not release.startswith("https://"):
        raise ValueError(f"{name}: pin 里没有可用的 https URL")
    rate, note = rate_of("github.com")
    out = [{"name": "github.com", "url": release, "measured_mib_s": rate, "note": note}]
    for m in pin.get("mirrors") or []:
        source = str(m.get("source") or "")
        url = str(m.get("url") or "").strip()
        if not source or not url.startswith("https://"):
            raise ValueError(f"{name}: 镜像条目缺 source 或不是 https URL")
        if m.get("sha256") != pin["sha256"] or m.get("bytes") != pin["size"]:
            raise ValueError(f"{name}: {source} 镜像的哈希/体积与 pin 不一致")
        repo, rev = m.get("repo"), m.get("revision")
        if source == "modelscope" and repo and rev:
            want = f"https://www.modelscope.cn/models/{repo}/resolve/{rev}/{name}"
            if url != want:
                raise ValueError(f"{name}: 镜像 URL 与记录的 repo/revision 推不出一致地址")
        rate, note = rate_of(source)
        out.append({"name": source, "url": url, "measured_mib_s": rate,
                    "note": f"{note}；{m.get('verified') or '镜像哈希与 pin 相同'}"})
    if len({s["name"] for s in out}) != len(out):
        raise ValueError(f"{name}: 同一来源写了两遍")
    out.sort(key=lambda s: s["measured_mib_s"], reverse=True)
    return out


def build(budget: dict | None = None, arts: dict | None = None) -> tuple[dict, list[str]]:
    budget = budget if budget is not None else read_json(BUDGET)
    if arts is None:
        arts = {"recommendations": read_json(RECO), "subtitle": read_json(SUBTITLE),
                "image": read_json(IMAGE)}
    pins = arts.get("ocr") or read_json(OCR_PINS)
    rows = tier_rows(arts)
    problems: list[str] = []
    merged: dict[str, dict] = {}
    order: list[str] = []
    skipped: dict[tuple, dict] = {}
    for tier, block in budget["per_tier"].items():
        for it in block["items"]:
            route = it.get("resolve_via") or "hf_metadata"
            if route == "not_a_file":
                key = (it["kind"], it["file"], it.get("bytes"))
                if key not in skipped:
                    skipped[key] = {"kind": it["kind"], "file": it["file"],
                                    "bytes": it.get("bytes"),
                                    "note": it.get("note") or "合计数字，不是一个文件"}
                continue
            sha = str(it.get("sha256") or "").lower()
            if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                problems.append(f"{it['kind']}/{it['file']}: sha256 不是 64 位十六进制")
                continue
            key = sha
            if key in merged:
                prev = merged[key]
                if (prev["file"], prev["bytes"], prev["kind"]) != (it["file"], it["bytes"], it["kind"]):
                    problems.append(f"{sha[:12]}…: 同一哈希在两行里指向不同文件/体积/能力")
                    continue
                if tier not in prev["tiers"]:
                    prev["tiers"].append(tier)
                continue
            item = {"sha256": sha, "file": it["file"], "bytes": it.get("bytes"),
                    "kind": it["kind"], "repo": it.get("repo"),
                    "default_install": bool(it.get("default_install")),
                    "tiers": [tier], "route": route,
                    "sources": [], "known_absent": [], "single_source": False}
            try:
                if route == "github_release_digest":
                    item["sources"] = ocr_sources(it, pins)
                elif route == "hf_metadata":
                    repo = it.get("repo")
                    if not repo or "/" not in repo:
                        raise ValueError(f"{it['file']}: hf_metadata 行没有仓库 id")
                    absent = absent_for(repo, it["file"], sha, rows)
                    cands = file_index.candidates(repo, it["file"])
                    kept = []
                    for c in cands:
                        provider = PROVIDER_OF_SOURCE.get(c["source"], c["source"])
                        if provider in absent:
                            item["known_absent"].append(provider)
                            continue
                        kept.append({"name": c["source"], "url": c["url"],
                                     "measured_mib_s": c["measured_mib_s"], "note": c["note"]})
                    item["sources"] = kept
                else:
                    raise ValueError(f"{it['file']}: 未知 resolve_via {route!r}")
                if not item["sources"]:
                    problems.append(f"{it['file']}: 一个可用源都没有")
                else:
                    rates = [s["measured_mib_s"] for s in item["sources"]]
                    if rates != sorted(rates, reverse=True):
                        problems.append(f"{it['file']}: 源顺序不是实测吞吐降序 {rates}")
                    if any(r <= 0 for r in rates[:-1]):
                        problems.append(f"{it['file']}: 零速率源排在了非兜底位置")
                    if item["sources"][0]["name"] == "huggingface.co":
                        problems.append(f"{it['file']}: CN 直连实测不可用的源排第一")
                    for s in item["sources"]:
                        if not s["url"].startswith("https://"):
                            problems.append(f"{it['file']}: 源不是 https")
                        if not s["url"].rstrip("/").split("/")[-1] == it["file"]:
                            problems.append(f"{it['file']}: URL 末段与文件名不符 {s['url']}")
                    item["single_source"] = len(item["sources"]) == 1
            except Exception as e:                       # noqa: BLE001 输入漂移要变成一行结论
                problems.append(f"{it['kind']}/{it['file']}: {e}")
            merged[key] = item
            order.append(key)
    items = [merged[k] for k in order]
    items.sort(key=lambda x: (not x["default_install"], KIND_ORDER.get(x["kind"], 9),
                              x["bytes"] or 0, x["file"]))
    totals = {"items": len(items),
              "default_install_items": sum(1 for x in items if x["default_install"]),
              "scope": "三档并集；一台机器只装其中一档，逐档首启量见 machine-budget 的 first_run_*",
              "union_bytes": {
                  "default_install": sum(x["bytes"] or 0 for x in items if x["default_install"]),
                  "optional": sum(x["bytes"] or 0 for x in items if not x["default_install"])}}
    by_kind: dict[str, dict] = {}
    for x in items:
        k = by_kind.setdefault(x["kind"], {"items": 0, "bytes": 0})
        k["items"] += 1
        k["bytes"] += x["bytes"] or 0
    totals["by_kind"] = by_kind
    by_tier: dict[str, dict] = {}
    for tier in budget["per_tier"]:
        picked = [x for x in items if tier in x["tiers"]]
        by_tier[tier] = {
            "items": len(picked),
            "default_install_bytes": sum(x["bytes"] or 0 for x in picked if x["default_install"]),
            "optional_bytes": sum(x["bytes"] or 0 for x in picked if not x["default_install"]),
        }
    totals["by_tier_weights_bytes"] = by_tier
    ocr_only = [x["file"] for x in items if x["kind"] == "ocr"]
    plan = {
        "schema": 1,
        "note": "由 tools/download_plan.py 生成；不要手改。行来自装机预算，源来自三张档位表，顺序来自实测吞吐。",
        "from": [BUDGET, RECO, SUBTITLE, IMAGE, OCR_PINS, "tools/r2_manifest.py"],
        "measured_at_exit": budget.get("measured_at_exit") or "",
        "rates_mib_s": {needle: rate for needle, rate, _ in r2_manifest.RATES_MIB_S},
        "policy": POLICY,
        "items": items,
        "skipped": list(skipped.values()),
        "totals": totals,
        "gaps": [
            f"OCR 默认档 {len(ocr_only)} 颗已有 ModelScope（整包下载复核 sha256）与 GitHub release 两条源；"
            "R2 镜像仍未建（#23），海外/兜底只剩 GitHub release 直连",
            "whisper.cpp 的 ModelScope 源被删掉是证据决定的（同次捕获 0 条），不是漏配；"
            "它的 `single` 强度也来自同一份捕获",
            "`huggingface.co` 直连 2026-10-19 从 CN 实测 0 字节，只作海外/兜底，"
            "不参与「哪家更快」的排序",
        ],
    }
    return plan, problems


def check(plan: dict, problems: list[str] | None = None) -> list[str]:
    """对一张已经成型的计划做独立复核：committed 文件被手改也逃不过这里。"""
    bad = list(problems or [])
    seen: set[str] = set()
    for x in plan.get("items", []):
        sha, name = x.get("sha256"), x.get("file")
        if not sha or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            bad.append(f"{name}: sha256 不合格")
        if sha in seen:
            bad.append(f"{name}: 同一哈希在计划里出现两次")
        seen.add(sha)
        if not isinstance(x.get("bytes"), int) or x["bytes"] <= 0:
            bad.append(f"{name}: bytes 不是正整数")
        srcs = x.get("sources") or []
        if not srcs:
            bad.append(f"{name}: 没有源")
            continue
        rates = [s.get("measured_mib_s") for s in srcs]
        if rates != sorted(rates, reverse=True):
            bad.append(f"{name}: 源顺序不是降序 {rates}")
        if rates and rates[0] <= 0:
            bad.append(f"{name}: 第一个源没有实测吞吐")
        if srcs[0].get("name") == "huggingface.co":
            bad.append(f"{name}: huggingface.co 排第一")
        for s in srcs:
            if not str(s.get("url", "")).startswith("https://"):
                bad.append(f"{name}: 非 https 源")
            if not str(s.get("url", "")).rstrip("/").split("/")[-1] == name:
                bad.append(f"{name}: URL 末段不是文件名")
        if x.get("kind") == "ocr" and not any(
                s.get("name") == "github.com" and "/download/" in str(s.get("url"))
                for s in srcs):
            bad.append(f"{name}: OCR 行丢了 GitHub release 兜底源")
        if x.get("default_install") and len(srcs) < POLICY["default_install_min_sources"]:
            bad.append(f"{name}: 默认安装只有 {len(srcs)} 个源，一家不可达就装不上")
    if any(x.get("file") == "(合计)" for x in plan.get("items", [])):
        bad.append("(合计) 进了下载队列 —— 它不是文件")
    return bad


def diff(committed: dict, plan: dict) -> list[str]:
    """committed 必须与重算结果逐键相等；少一项/多一项/手改一个数字都算。"""
    if committed == plan:
        return []
    return [f"{OUT} 与重算结果不同 —— 手改或没重生成"]


PROBE_UA = "Mozilla/5.0 (X11; Linux x86_64) local-ai-studio-probe/1.0"


def probe_jobs(plan: dict) -> list[dict]:
    """一个源一行，URL 逐字来自计划 —— 探针不按模板补一条（known_absent 没 URL 就不探）。"""
    return [{"file": x["file"], "kind": x["kind"], "sha256": x["sha256"],
             "bytes": x["bytes"], "source": s["name"], "url": s["url"]}
            for x in plan["items"] for s in x["sources"]]


def parse_total(headers, status: int):
    """206 的 `Content-Range: bytes 0-0/<总数>` 就是整份文件长度；200 退而看 Content-Length。"""
    m = re.match(r"bytes\s+\d+-\d+/(\d+|\*)", headers.get("Content-Range") or "")
    if m and m.group(1) != "*":
        return int(m.group(1))
    cl = headers.get("Content-Length") or ""
    if status == 200 and cl.isdigit():
        return int(cl)
    return None


def classify(status: int, got: int, total=None, expected=None) -> str:
    """要真的拿到 1 个字节才算通；给了总长就必须与预算字节相等。"""
    if status in (200, 206) and got > 0:
        if total is not None and expected is not None and total != expected:
            return "mismatch"
        return "ok"
    if status in (404, 410):
        return "absent"
    return "bad"


def fetch_head_byte(url: str, timeout: float = 25.0) -> dict:
    """Range bytes=0-0 只读 1 字节：比 HEAD 可信（有 CDN 对 HEAD 回 200 却没有正文）。"""
    req = urllib.request.Request(url, headers={
        "Range": "bytes=0-0", "User-Agent": PROBE_UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read(1)
            return {"status": r.status, "got": len(body), "total": parse_total(r.headers, r.status),
                    "final": r.geturl(), "error": ""}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "got": 0, "total": None, "final": url, "error": ""}
    except Exception as e:                       # noqa: BLE001 网络失败要变成一行结论
        return {"status": 0, "got": 0, "total": None, "final": url,
                "error": f"{type(e).__name__}: {e}"[:200]}


def probe_all(jobs: list[dict], timeout: float = 25.0, workers: int = 6,
              fetch=None) -> list[dict]:
    fetch = fetch or fetch_head_byte

    def one(job: dict) -> dict:
        r = fetch(job["url"], timeout)
        total = r.get("total")
        return dict(job, status=r["status"], got=r["got"], total=total, final=r["final"],
                    error=r["error"],
                    result=classify(r["status"], r["got"], total, job["bytes"]))

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(one, jobs))


def summarize(rows: list[dict]) -> dict:
    by_source: dict[str, dict] = {}
    by_host: dict[str, dict] = {}
    for r in rows:
        b = by_source.setdefault(r["source"], {"total": 0, "ok": 0, "absent": 0,
                                               "bad": 0, "mismatch": 0})
        b["total"] += 1
        b[r["result"]] += 1
        host = urllib.parse.urlsplit(r["final"]).hostname or "?"
        h = by_host.setdefault(host, {"ok": 0, "absent": 0, "bad": 0, "mismatch": 0})
        h[r["result"]] += 1
    sizes = [r for r in rows if r["total"] is not None]
    return {"total": len(rows),
            "ok": sum(1 for r in rows if r["result"] == "ok"),
            "absent": sum(1 for r in rows if r["result"] == "absent"),
            "bad": sum(1 for r in rows if r["result"] == "bad"),
            "mismatch": sum(1 for r in rows if r["result"] == "mismatch"),
            "size_checked": len(sizes),
            "size_unverified": len(rows) - len(sizes),
            "by_source": by_source, "by_final_host": by_host}


def probe_ok(summary: dict) -> bool:
    return summary["total"] > 0 and summary["ok"] == summary["total"]


def selftest() -> int:
    """离线变异：每条判据都要能因为一处改动而变红。"""
    plan, problems = build()
    bad = check(plan, problems)
    if bad:
        for x in bad:
            print("  BAD:", x)
        return 1

    def mutate(label: str, fn) -> None:
        p = copy.deepcopy(plan)
        fn(p)
        if not check(p):
            bad.append(f"负向对照「{label}」没被抓住")

    def swap_sources(p):
        p["items"][0]["sources"].reverse()

    def add_aggregate(p):
        p["items"].append({"sha256": "0" * 64, "file": "(合计)", "bytes": 1, "kind": "ocr",
                           "sources": [{"name": "github.com", "url": "https://x/(合计)",
                                        "measured_mib_s": 0.014}], "single_source": True})

    def first_hf(p):
        p["items"][0]["sources"] = [{"name": "huggingface.co",
                                     "url": "https://huggingface.co/x/" + p["items"][0]["file"],
                                     "measured_mib_s": 0.0}] + p["items"][0]["sources"]

    mutate("源顺序颠倒", swap_sources)
    mutate("合计行进队列", add_aggregate)
    mutate("HF 直连排第一", first_hf)

    def ocr_without_release(p):
        item = next(x for x in p["items"] if x["kind"] == "ocr")
        item["sources"] = [s for s in item["sources"] if s["name"] != "github.com"]

    def default_single(p):
        item = next(x for x in p["items"] if x["default_install"])
        item["sources"] = item["sources"][:1]

    mutate("OCR 丢 release 兜底", ocr_without_release)
    mutate("默认安装只剩一个源", default_single)
    # 少一项不是形状问题，只能靠"committed 与重算逐键相等"抓 —— 直接验那条闸。
    dropped = copy.deepcopy(plan)
    dropped["items"].pop()
    if not diff(dropped, plan):
        bad.append("负向对照「计划少一项」没被 diff 抓住")
    # 生成器侧：把一颗 OCR 的 pin 哈希改坏，build 必须报问题，而不是照抄。
    arts = {"recommendations": read_json(RECO), "subtitle": read_json(SUBTITLE),
            "image": read_json(IMAGE), "ocr": copy.deepcopy(read_json(OCR_PINS))}
    victim = next(iter(arts["ocr"]))
    arts["ocr"][victim]["sha256"] = "f" * 64
    _, probs = build(arts=arts)
    if not any(victim in x for x in probs):
        bad.append("负向对照「OCR pin 哈希与预算不符」没被抓住")
    # 生成器侧：档位表里抽掉一颗已下载文件，build 必须报"找不到行"，不许按模板猜 URL。
    arts2 = {"recommendations": read_json(RECO), "subtitle": read_json(SUBTITLE),
             "image": read_json(IMAGE), "ocr": read_json(OCR_PINS)}
    target = plan["items"][0]
    for rows in list(arts2["recommendations"]["tiers_rows"].values()):
        for i in range(len(rows) - 1, -1, -1):
            if rows[i].get("sha256") == target["sha256"]:
                rows.pop(i)
    for rows in (arts2["subtitle"]["rows"], arts2["image"]["weights"]):
        for i in range(len(rows) - 1, -1, -1):
            if rows[i].get("sha256") == target["sha256"]:
                rows.pop(i)
    _, probs2 = build(arts=arts2)
    if not any(target["file"] in x for x in probs2):
        bad.append("负向对照「档位表缺行」没被抓住")
    # 生成器侧：镜像哈希被改成另一颗，build 必须报问题；URL 与 repo/revision 推不出来也一样。
    arts3 = {"recommendations": read_json(RECO), "subtitle": read_json(SUBTITLE),
             "image": read_json(IMAGE), "ocr": copy.deepcopy(read_json(OCR_PINS))}
    victim3 = next(iter(arts3["ocr"]))
    arts3["ocr"][victim3]["mirrors"][0]["sha256"] = "f" * 64
    _, probs3 = build(arts=arts3)
    if not any(victim3 in x and "镜像" in x for x in probs3):
        bad.append("负向对照「OCR 镜像哈希与 pin 不符」没被抓住")
    arts4 = {"recommendations": read_json(RECO), "subtitle": read_json(SUBTITLE),
             "image": read_json(IMAGE), "ocr": copy.deepcopy(read_json(OCR_PINS))}
    victim4 = next(iter(arts4["ocr"]))
    mirror = arts4["ocr"][victim4]["mirrors"][0]
    mirror["url"] = mirror["url"].replace("/master/", "/main/")
    _, probs4 = build(arts=arts4)
    if not any(victim4 in x and "推不出一致地址" in x for x in probs4):
        bad.append("负向对照「OCR 镜像 URL 与 repo/revision 不符」没被抓住")
    for x in bad:
        print("  BAD:", x)
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


def probe_main(a) -> int:
    plan, problems = build()
    bad = check(plan, problems)
    if bad:
        for b in bad:
            print("BAD:", b)
        return 1
    jobs = probe_jobs(plan)
    rows = probe_all(jobs, a.timeout, a.workers)
    summary = summarize(rows)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    absent_notes = [(x["file"], p) for x in plan["items"] for p in x["known_absent"]]
    if a.json:
        print(json.dumps({"probed_at": stamp,
                          "policy": {"range": "bytes=0-0", "timeout_s": a.timeout},
                          "summary": summary, "rows": rows,
                          "known_absent_not_probed": absent_notes},
                         ensure_ascii=False, indent=1, sort_keys=True))
    else:
        print(f"下载源探测 {stamp}：{summary['total']} 个 URL，Range bytes=0-0，超时 {a.timeout:g}s")
        for r in rows:
            if r["result"] != "ok":
                if r["result"] == "mismatch":
                    detail = f"  总长 {r['total']} ≠ 预算 {r['bytes']}"
                else:
                    detail = f"  ({r['error']})" if r["error"] else ""
                print(f"  {r['result'].upper():8} {r['file']}  {r['source']}  "
                      f"HTTP {r['status']}  {r['url']}{detail}")
        for name, b in sorted(summary["by_source"].items()):
            print(f"  {name:16} {b['ok']}/{b['total']} ok")
        hosts = "；".join(f"{h} {b['ok']} ok" for h, b in sorted(summary["by_final_host"].items()))
        print(f"  最终主机：{hosts}")
        print(f"  尺寸：{summary['size_checked'] - summary['mismatch']} 条总长与预算相等、"
              f"{summary['size_unverified']} 条上游没给总长、{summary['mismatch']} 条不符")
        print(f"  known_absent {len(absent_notes)} 条没有 URL，未探测（不为探针按模板补）")
        print("  可达只证明 Range 取到 1 字节，不证明吞吐；排序仍按计划里的实测吞吐。")
    return 0 if probe_ok(summary) else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="download_plan.py")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check", action="store_true", help="重算并与已提交的 pins/download-plan.json 比")
    ap.add_argument("--probe", action="store_true",
                    help="现探计划里的每个源 URL（只读 1 字节，报告式，不写文件）")
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--json", action="store_true", help="--probe 时输出整份结果 JSON")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.probe:
        return probe_main(a)
    plan, problems = build()
    bad = check(plan, problems)
    out = os.path.join(ROOT, OUT)
    if a.check:
        with open(out, encoding="utf-8") as fh:
            committed = json.load(fh)
        bad.extend(diff(committed, plan))
    else:
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(plan, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
    for b in bad:
        print("BAD:", b)
    by_kind = ", ".join(f"{k} {v['items']} 项/{v['bytes'] / 1024 ** 2:.1f} MiB"
                        for k, v in sorted(plan["totals"]["by_kind"].items()))
    print(f"下载计划：{plan['totals']['items']} 项（默认 {plan['totals']['default_install_items']} 项），"
          f"{by_kind}；跳过合计行 {len(plan['skipped'])} 条")
    per_tier = "；".join(f"{t} 默认 {v['default_install_bytes'] / 1024 ** 2:.1f} MiB"
                        for t, v in sorted(plan["totals"]["by_tier_weights_bytes"].items()))
    print(f"逐档权重（不含引擎）：{per_tier}；问题 {len(bad)} 条")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
