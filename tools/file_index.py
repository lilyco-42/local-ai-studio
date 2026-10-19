"""下载层的文件级解析：`repo + 文件名` → 期望 sha256 + 候选源（按实测吞吐排序）。

为什么现在才做得动：目录里 14,364 条只有 2,290 条自带"可下载仓库"，而 2026-10-20 之前我们还
以为 HF 的元数据不给内容哈希（那是探针读错键名的假结论，见 docs/model-catalog.md 的对照表）。
实际上两家分发方都零下载给哈希：

  · HF 系：`GET /api/models/{repo}/tree/{rev}` → 每个文件 `lfs.oid`（就是内容 sha256）
  · ModelScope：`GET /api/v1/models/{repo}/repo/files?Revision={rev}&Root=.` → 每个文件 `Sha256`

规矩只有一条：**两家都给且相等才叫可信**；只有一家就给 `single`，两家不等就给 `conflict`
并且 `usable=False` —— 绝不"挑一个好看的"。这与 `establish_pins.py` 的对照口径一致：
域名不等于提供方（`hf-mirror` 在某些出口只是 308 回 HF），所以提供方数量按**真正回答的那台**算。

候选源的顺序不是猜的，用的是 lain42.top 那轮实测吞吐（ModelScope 4.2 MiB/s、hf-mirror 0.25、
GitHub release 0.012–0.017、自家 R2 0.82–1.01）；`huggingface.co` 从 CN 实测是 0 字节，
所以它只能当"海外或兜底"，不能排第一。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

MARKER = "|@@ANSWERED@@|"      # curl -w 的格式串不能以 @ 开头（会被当成"从文件读格式"，退出码 26）
UA = "file-index/1.0"
# 与 r2_manifest 同一批实测数字（那里按源分组换算时间，这里按源排候选顺序）
from r2_manifest import RATES_MIB_S                                    # noqa: E402
HF_FAMILY = ("hf-mirror.com", "huggingface.co")
FALLBACK_RATE = 0.0


def sh(args):
    p = subprocess.run(args, capture_output=True)
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr[:130]


def http_json(url: str, timeout: int = 60):
    """返回 (对象 | None, 真正回答的主机, 错误说明)。带 -w 拿 url_effective：
    请求的主机名与回答的提供方可以不一样。"""
    rc, out, err = sh(["curl", "-sSL", "--max-time", str(timeout), "-A", UA,
                       "-w", MARKER + "%{url_effective}", url])
    body, _, tail = out.rpartition(MARKER)
    answered = tail.strip().split("/")[2] if "://" in tail else ""
    if rc != 0:
        return None, answered, f"curl 退出码 {rc}：{err[:80]}"
    try:
        return json.loads(body), answered, None
    except Exception as e:      # noqa: BLE001  不是 JSON 也要变成一行结论
        return None, answered, f"{type(e).__name__}：{body[:80]}"


def hf_files(repo: str, rev: str = "main") -> tuple[dict, str, str | None]:
    """两个入口都试（hf-mirror 与 huggingface.co 是同一家，谁先答用谁），
    只有一个答不了才算错 —— 否则一次网络抖动就会被 classify 读成"这仓库没这文件"。"""
    errs = []
    for host in HF_FAMILY:
        obj, answered, err = http_json(f"https://{host}/api/models/{repo}/tree/{rev}")
        if err or not isinstance(obj, list):
            errs.append(f"{host}: {err or '返回不是列表'}")
            continue
        out = {}
        for e in obj:
            if e.get("type") != "file":
                continue
            lfs = e.get("lfs") or {}
            out[e.get("path")] = {"sha256": lfs.get("oid") or e.get("oid"),
                                 "size": lfs.get("size") or e.get("size")}
        return out, (answered or host), None
    return {}, "", "；".join(errs) or "HF 两家都没答"


def ms_files(repo: str, rev: str = "master") -> tuple[dict, str, str | None]:
    url = f"https://www.modelscope.cn/api/v1/models/{repo}/repo/files?Revision={rev}&Root=."
    obj, answered, err = http_json(url)
    if err or not isinstance(obj, dict):
        return {}, answered, err or "返回不是对象"
    out = {}
    for f in ((obj.get("Data") or {}).get("Files") or []):
        if f.get("Type") == "tree":
            continue
        out[f.get("Path")] = {"sha256": f.get("Sha256"), "size": f.get("Size")}
    return out, answered, None


def classify(name: str, hf: dict, ms: dict, hf_provider: str, ms_provider: str,
             want_sha: str | None = None) -> dict:
    """把一个文件的元数据比对归成五种状态之一。usable 只有 agree / 与已知 pin 相符才 True。"""
    a = (hf.get(name) or {}).get("sha256")
    b = (ms.get(name) or {}).get("sha256")
    size = (hf.get(name) or {}).get("size") or (ms.get(name) or {}).get("size")
    got = {k: v for k, v in ((hf_provider, a), (ms_provider, b)) if v}
    if a and b and a == b:
        status, sha, usable = "agree", a, True
    elif a and b:
        status, sha, usable = "conflict", None, False       # 绝不挑一个好看的
    elif a or b:
        status, sha, usable = "single", (a or b), False      # 单家声明：可用但要下载后自算
    else:
        status, sha, usable = ("absent" if name not in hf and name not in ms else "no_hash"), None, False
    row = {"file": name, "status": status, "sha256": sha, "size": size,
           "usable_before_download": usable, "providers": sorted(got),
           "declared": got}
    if want_sha:
        row["matches_pin"] = (sha == want_sha) if sha else None
        if status == "agree" and row["matches_pin"] is False:
            row["status"], row["sha256"], row["usable_before_download"] = "pin_conflict", None, False
    return row


def candidates(repo: str, name: str, rev: str = "main", ms_rev: str = "master") -> list[dict]:
    """候选源按实测吞吐从快到慢；没有实测速率的源标 0，不许编一个数字。"""
    urls = [
        ("modelscope", f"https://www.modelscope.cn/models/{repo}/resolve/{ms_rev}/{name}"),
        ("hf-mirror.com", f"https://hf-mirror.com/{repo}/resolve/{rev}/{name}"),
        ("github.com", ""),
        ("huggingface.co", f"https://huggingface.co/{repo}/resolve/{rev}/{name}"),
    ]
    out = []
    for key, url in urls:
        if not url:
            continue
        rate = next((r for needle, r, _ in RATES_MIB_S if needle in key), FALLBACK_RATE)
        note = next((n for needle, _r, n in RATES_MIB_S if needle in key),
                    "没有该出口的实测速率 → 只作兜底，不排第一")
        if key == "huggingface.co":
            rate, note = 0.0, "2026-10-19 从 CN 出口实测 0 字节（不可用），海外或直连时才用"
        out.append({"source": key, "url": url, "measured_mib_s": rate, "note": note})
    return sorted(out, key=lambda c: -c["measured_mib_s"])


def selfcheck() -> int:
    """离线对照：明知是错的必须被认成错。"""
    hf = {"a.gguf": {"sha256": "f" * 64, "size": 10}, "solo.gguf": {"sha256": "e" * 64, "size": 2}}
    ms = {"a.gguf": {"sha256": "f" * 64, "size": 10}, "b.gguf": {"sha256": "d" * 64, "size": 3}}
    cases = [
        ("两家一致 → 可信", classify("a.gguf", hf, ms, "HF", "MS"), ("agree", True)),
        ("两家不等 → conflict 且绝不可信", classify("x.gguf",
                                                  {"x.gguf": {"sha256": "1" * 64, "size": 1}},
                                                  {"x.gguf": {"sha256": "2" * 64, "size": 1}},
                                                  "HF", "MS"), ("conflict", False)),
        ("只有一家 → single，下载前不可信", classify("solo.gguf", hf, ms, "HF", "MS"), ("single", False)),
        ("两家都没有 → absent", classify("nope.gguf", hf, ms, "HF", "MS"), ("absent", False)),
        ("两家一致但与已知 pin 不符 → pin_conflict",
         classify("a.gguf", hf, ms, "HF", "MS", want_sha="9" * 64), ("pin_conflict", False)),
    ]
    rc = 0
    for label, row, (want_status, want_usable) in cases:
        ok = row["status"] == want_status and row["usable_before_download"] == want_usable
        rc |= 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label} → {row['status']} usable={row['usable_before_download']}")
    order = candidates("Qwen/X-GGUF", "q.bin")
    ok = [c["source"] for c in order] == ["modelscope", "hf-mirror.com", "huggingface.co"]
    rc |= 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} 候选顺序按实测吞吐 → {[c['source'] for c in order]}")
    return rc


def queue_items(budget: dict) -> list[dict]:
    """把三档的默认权重行去重成一张首启队列：同一颗文件在几个档都出现，只排一次。"""
    idx: dict[tuple, int] = {}
    out: list[dict] = []
    for tier, block in sorted(budget["per_tier"].items()):
        for it in block["items"]:
            key = (it.get("repo"), it["file"], it.get("sha256"))
            if key in idx:
                out[idx[key]]["tiers"].append(tier)
                continue
            idx[key] = len(out)
            out.append(dict(it, tiers=[tier]))
    return out


def check_queue(items: list[dict], fetch_hf=hf_files, fetch_ms=ms_files,
                floor_live: int = 8) -> tuple[list[dict], list[str]]:
    """每一项都要说出：它走哪条确认路线、这条路线此刻有没有确认它。

    只有 `resolve_via == "hf_metadata"` 的项在这里做 live 比对（两家的元数据哈希 vs 表里钉的
    哈希 vs 体积）。GitHub release 那几颗（OCR 默认档）的确认路线在 `ocr-languages` 车道
    （`--check-pins --live`），这里**如实标 `checked: false` 并写出路线** —— 把"我没查"混进
    "查过了"是最容易骗过自己的一种错。地板按 live 查到的条数算：整批没解析成功不算「队列已验」。
    """
    rows, bad = [], []
    cache: dict[str, tuple] = {}
    live_ok = 0
    for it in items:
        via = it.get("resolve_via") or "hf_metadata"
        base = {"kind": it["kind"], "repo": it.get("repo"), "file": it["file"],
                "bytes": it.get("bytes"), "sha256": it.get("sha256"),
                "tiers": it["tiers"], "route": via, "checked": False}
        if via == "not_a_file":
            base["note"] = it.get("note") or "合计数字，不是一个文件"
            rows.append(base)
            continue
        if via != "hf_metadata":
            base["note"] = ("确认路线不在这条车道（GitHub release 的 asset digest；"
                            "live 由 ocr-languages 的 --check-pins --live 查）")
            rows.append(base)
            continue
        repo, name, pin = it.get("repo"), it["file"], it.get("sha256")
        if not repo or "/" not in repo or not pin:
            bad.append(f"{it['kind']}/{name}: 缺仓库 id 或缺钉住的哈希 —— 这一项进不了队列")
            rows.append(base)
            continue
        if repo not in cache:
            cache[repo] = (fetch_hf(repo), fetch_ms(repo))
        (hf, hf_by, e1), (ms, ms_by, e2) = cache[repo]
        if e1 or e2:
            bad.append(f"{repo}/{name}: 元数据取不到（HF {e1 or 'ok'} / ModelScope {e2 or 'ok'}）"
                       " —— 这是取数失败，不是上游没给哈希")
            rows.append(base)
            continue
        row = classify(name, hf, ms, hf_by or "hf", ms_by or "ms", pin)
        cands = candidates(repo, name)
        base.update({"status": row["status"], "providers": row["providers"],
                     "metadata_sha256": row["sha256"], "metadata_size": row["size"],
                     "matches_pin": row.get("matches_pin"),
                     "usable_before_download": row["usable_before_download"],
                     "candidate_sources": [c["source"] for c in cands],
                     "fastest": cands[0]["url"] if cands else None})
        if row["status"] in ("absent", "no_hash", "conflict", "pin_conflict"):
            bad.append(f"{repo}/{name}: 比对状态 {row['status']} —— 表里钉的这颗现在对不上")
        elif row.get("matches_pin") is not True:
            bad.append(f"{repo}/{name}: 元数据哈希 ≠ 表里钉的（{str(row['sha256'])[:12]}… vs "
                       f"{str(pin)[:12]}…）")
        elif row["size"] not in (None, it.get("bytes")):
            bad.append(f"{repo}/{name}: 元数据体积 {row['size']} 与预算里的 {it.get('bytes')} 不符")
        elif not cands:
            bad.append(f"{repo}/{name}: 一个候选源都没有 —— 队列下不动")
        else:
            base["checked"] = True
            live_ok += 1
        # 负向对照：同一个仓库里编一个不存在的名字，必须被认成"没有"。
        # 它要是被判成 agree/single，就说明这套比对无论输入是什么都有答案。
        neg = classify("__deliberately_absent__" + name, hf, ms, hf_by or "hf", ms_by or "ms")
        if neg["status"] not in ("absent", "no_hash"):
            bad.append(f"{repo}: 负向对照失效（假名字被判成 {neg['status']}）")
        rows.append(base)
    if live_ok < floor_live:
        bad.append(f"live 确认到的只有 {live_ok} 项（地板 {floor_live}）—— "
                   "整批没解析成功不能算「首启队列已验」")
    return rows, bad


def control(pins_path: str, floor: int = 2) -> int:
    """正向对照用仓库里已钉的 pin；负向对照用一根故意造出来的文件名。"""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from establish_pins import hf_repos_for        # 同一套 URL→仓库的认法，不另写一遍
    with open(pins_path, encoding="utf-8") as fh:
        pins = json.load(fh)
    bad = []
    checked = 0
    agrees = 0
    for name, rec in sorted(pins.items()):
        repos = hf_repos_for(rec)
        if not repos:
            print(f"  · {name}: 没有 HF 侧 URL，跳过（不等于它没哈希）")
            continue
        repo = sorted(repos)[0]
        hf, hf_by, e1 = hf_files(repo)
        ms, ms_by, e2 = ms_files(repo)
        # "取不到"与"这家没有这个仓库/文件"是两件事：后者会让 ms_files 返回空表但不算错，
        # 前者必须红着说清楚，否则一次抖动就被 classify 读成 absent。
        if e1 or e2:
            bad.append(f"{name}: 元数据取不到（HF: {e1 or 'ok'} / ModelScope: {e2 or 'ok'}）"
                       f"→ 这是取数失败，不是上游没给哈希")
            continue
        row = classify(name, hf, ms, hf_by or "hf", ms_by or "ms", rec.get("sha256"))
        checked += 1
        if row["status"] == "agree":
            agrees += 1
        print(f"  {name}: 状态={row['status']} 提供方={row['providers']} "
              f"大小={row['size']} 与 pin 相符={row['matches_pin']}")
        if row["status"] == "single":
            # 这不是失败：有些权重（如 ggml-tiny.bin）ModelScope 上确实没有同名仓库。
            # 但必须看得见"这一条只有一家声明过"，否则强度阶梯会被这句悄悄抹平。
            print(f"    · 只有一家给哈希 → 强度只到 single；下载后仍要自己算一边")
        if row["status"] in ("conflict", "pin_conflict", "absent", "no_hash") \
                or row["matches_pin"] is not True:
            bad.append(f"{name}: 已钉的 pin 没被确认（状态 {row['status']}，"
                       f"相符 {row['matches_pin']}）")
        # 负向对照：故意造一个不存在的名字，必须被认成 absent。
        # 它要是被判成 agree/single，就说明这套比对无论输入是什么都"有答案"。
        neg_name = "__deliberately_absent__" + name
        neg = classify(neg_name, hf, ms, hf_by or "hf", ms_by or "ms")
        if neg["status"] != "absent":
            bad.append(f"负向对照失效：{neg_name} 被判成 {neg['status']}")
    print(f"---- 结论 ---- 比对 {checked} 条，其中两家都给哈希的 {agrees} 条")
    if agrees < floor:
        bad.append(f"两家确认的只有 {agrees} 条（地板是 {floor}）—— "
                   "要么某家元数据端点漂了，要么整个源不可用；不许当成「今天就这样」")
    for b in bad:
        print("  BAD:", b)
    print("BAD 数量 =", len(bad))
    return 1 if bad or not checked else 0


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selfcheck", action="store_true", help="离线对照，不联网")
    ap.add_argument("--control", default="", help="用已钉的 pin 做正向+负向对照（联网）")
    ap.add_argument("--queue", default="",
                    help="读装机预算那张表，逐项确认首启队列（联网：两家元数据哈希 vs 钉值）")
    ap.add_argument("--min-confirmed", type=int, default=8,
                    help="live 至少确认几项（覆盖度地板，别调低来凑绿）")
    ap.add_argument("--repo", default="")
    ap.add_argument("--file", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--min-agreed", type=int, default=2,
                    help="至少几条要被两家共同确认（覆盖度地板，别调低来凑绿）")
    args = ap.parse_args(argv)
    if args.selfcheck:
        return selfcheck()
    if args.control:
        return control(args.control, args.min_agreed)
    if args.queue:
        with open(args.queue, encoding="utf-8") as fh:
            budget = json.load(fh)
        rows, bad = check_queue(queue_items(budget), floor_live=args.min_confirmed)
        for r in rows:
            print(f"  {r['kind']:<8} {str(r.get('repo'))[:33]:<35} {r['file'][:36]:<38}"
                  f"路线={r['route']:<21} 确认={str(r['checked']):<5}"
                  f"{('状态=' + str(r.get('status'))) if r.get('status') else (r.get('note') or '')[:44]}")
        n_ok = sum(1 for r in rows if r["checked"])
        print(f"队列 {len(rows)} 项（去重自 {len(budget['per_tier'])} 档），"
              f"live 确认 {n_ok} 项，未确认 {len(rows) - n_ok} 项")
        if args.json:
            with open(args.json, "w", encoding="utf-8", newline="\n") as fh:
                json.dump({"from": args.queue, "items": rows, "problems": bad},
                          fh, ensure_ascii=False, indent=1, sort_keys=True)
                fh.write("\n")
        for b in bad:
            print("  BAD:", b)
        print("BAD 数量 =", len(bad))
        return 1 if bad else 0
    if not args.repo or not args.file:
        print("要 --repo 与 --file（或用 --control 跑对照）")
        return 2
    hf, hf_by, e1 = hf_files(args.repo)
    ms, ms_by, e2 = ms_files(args.repo)
    row = classify(args.file, hf, ms, hf_by or "hf", ms_by or "ms")
    row["candidates"] = candidates(args.repo, args.file)
    row["errors"] = [e for e in (e1, e2) if e]
    text = json.dumps(row, ensure_ascii=False, indent=1)
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
    print(text)
    return 0 if row["usable_before_download"] or row["status"] == "single" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
