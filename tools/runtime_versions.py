#!/usr/bin/env python3
"""runtime 的版本轴：能升到哪一颗、能退回哪一颗、每颗能不能校验。

约束第 3 条写的是"引擎按 runtime 打包、可替换，和 LM Studio 的 LM Runtimes 同形"。
`pins/backend-matrix.json` 只覆盖**钉住的那一个 ref**（有哪些后端可换），版本轴一句都没有：
外壳要给用户"更新 runtime / 回退 runtime"，得知道上游相邻版本发不发同样的包、每颗带不带 sha256。

这条路上有一个已经量到的坑：**whisper 的 release 是 `v1.9.5`（0 个产物，只有源码）
与 `b5454`（12 个产物）交替发的，而 `releases/latest` 恰好指向 0 产物那一颗**。
按 latest 取 runtime 的外壳会"检查更新成功、装不上任何东西"。这是这台机器上第三次栽在
`latest` 这类"名字好看但不带内容"的指针上，所以它必须是表里的一列，而不是一句提醒。

只读元数据：上游 release 表（tag / published_at / asset 的 name+size+digest）。
不下载任何包、不解包、不运行。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
PAYLOAD = os.path.join(ROOT, "pins", "payload-plan.json")
MATRIX = os.path.join(ROOT, "pins", "backend-matrix.json")
CAPTURES = os.path.join(ROOT, "tests", "fixtures", "release-window.json")
OUT = os.path.join(ROOT, "pins", "runtime-versions.json")

sys.path.insert(0, HERE)
import backends as bd  # noqa: E402  同一份名字读者，不在这里另写一遍分类规则

PER_PAGE = 100
MAX_PAGES = 5
KEEP_MIN = 25
# 这些扩展名不可能是可执行产物（nightly 指针文件、jar 包、校验清单、文档）。
# backends.classify 只看包名里的平台与后端词，不看扩展名 —— 这一层要看，否则
# nightly-tag.txt 会被当成「认不得的后端词」，把真信号（某档没了）淹在噪音里。
ATTACHMENT = re.compile(r"\.(txt|json|sha256|md5|css|html|md)$|\.jar(\.zip)?$", re.IGNORECASE)

RULE = {
    "window": "每个引擎从最新往下翻，**翻到钉住那一颗为止**（上限 MAX_PAGES 页 × PER_PAGE 个）。"
              "llama 一天就发 6 个 release，固定 12 个的窗口根本装不下我们钉的 b11727 —— "
              "窗口不含钉值时'能退回哪'这句就不成立，所以这是政策不是手感",
    "window_shape": "产物里逐引擎记 pages_fetched / per_page / captured / oldest_captured_tag，"
                    "窗口大小是被量出来的数，不是猜的",
    "candidates": "只有**带 runtime 产物**的 release 才是升级/回退的候选；"
                  "0 产物的源标签（whisper 的 vX.Y.Z）单列在 source_only_tags 里，"
                  "并且明写 latest 指针就落在它上面 —— 这就是不许按 releases/latest 取 runtime 的原因",
    "digest": "runtime 资产带上游 digest（sha256）才叫「可校验」。这条是量出来的边界："
              "whisper 早年那批 release 根本没有 digest 字段，所以回退深度不是「翻到最老」，"
              "而是 oldest_verifiable_tag。不可校验的版本单列在 unverifiable_tags 里，"
              "不冒充候选，也不许当噪音删掉",
    "attachments": "nightly-tag.txt / *.jar / checksum 这类附件不参与后端与平台分类，"
                   "单独计数（它们是上游的附属文件，不是可执行产物）",
    "pinned": "钉住那颗必须在窗口内，且它的产物名集合必须与 pins/backend-matrix.json 逐颗一致 —— "
              "两张表读的是同一个 release，对不上就是有一份旧了",
    "drift": "上游发了新 release 只报不跟（与 pins/actions.json 同一条纪律）：产物里写清"
             "pinned 之上新增了几个带产物的版本，升级是一次改动+一次复核，不是一个自动动作",
    "capability": "相邻版本之间少了哪些 引擎×OS 组合记进 capability_delta。**丢的恰好是我们装的那一档**"
                  "（payload-plan 的默认档组合）就直接红，不许只记一笔差异",
}


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def shipped_combos(plan: dict) -> dict:
    """每个引擎真装的 OS 组合 —— 新版本少了这些是要红的，少了别的可以只记差异。"""
    out: dict[str, set] = {}
    for os_key, rows in plan["os"].items():
        for r in rows:
            if r.get("engine"):
                out.setdefault(r["engine"], set()).add(os_key)
    return {k: sorted(v) for k, v in out.items()}


def summarize_release(tag: str, published: str, name: str, assets: list, repo: str) -> dict:
    runtime, other, missing, unknown, attachments = [], [], [], [], []
    for a in assets:
        if ATTACHMENT.search(a["name"]):
            attachments.append(a["name"])
            continue
        got = bd.classify(a["name"], tag)
        if got["backend"] == "unknown_token" or got["os"] == "unknown_token":
            unknown.append(a["name"])
        if got["kind"] == "runtime":
            runtime.append({"asset": a["name"], "os": got["os"], "backend": got["backend"],
                            "version": got["version"], "size": a["size"],
                            "sha256": a.get("sha256") or ""})
            if not a.get("sha256"):
                missing.append(a["name"])
        else:
            other.append(a["name"])
    combos = sorted({x["os"] for x in runtime if x["os"]})
    return {"tag": tag, "published_at": published, "name": name, "repo": repo,
            "assets_total": len(assets), "runtime_assets": len(runtime),
            "non_runtime_assets": len(other), "os_covered": combos,
            "has_runtime": bool(runtime), "runtime": runtime,
            "attachments": attachments,
            "digest_missing": missing, "unknown_tokens": unknown,
            # 上游 asset.digest 是后来才有的字段：老 release 的包没有可核对的哈希。
            # 于是「能退回哪」有第二道边界 —— 不是所有发过的版本都能当带校验的候选。
            "verifiable": bool(runtime) and not missing}


def build(captures: dict, matrix: dict, plan: dict) -> tuple[dict, list[str]]:
    problems: list[str] = []
    combos = shipped_combos(plan)
    pinned = {e: v["release"] for e, v in matrix["engines"].items()}
    repo_of = {e: v["repo"] for e, v in matrix["engines"].items()}
    matrix_names = {e: sorted(r["asset"] for r in v["rows"]) for e, v in matrix["engines"].items()}

    engines: dict[str, dict] = {}
    deltas: list[dict] = []
    stats = {"引擎": len(pinned), "窗口内 release": 0, "带产物的版本": 0,
             "只有源码的标签": 0, "runtime 资产行": 0, "可校验的候选版本": 0,
             "不可校验的版本（上游没发 digest）": 0, "附件（非产物）": 0,
             "认不出的词": 0, "latest 是空标签的引擎": 0,
             "窗口之外还看了的 release": 0, "窗口之外全没有 digest 的 release": 0}
    for engine, tag in sorted(pinned.items()):
        key = f"{repo_of[engine]}@{tag}"
        entry = captures.get(key)
        if entry is None:
            problems.append(f"{engine}: 捕获里没有 {key} 这一组 release 窗口 —— 这张表没法说版本轴")
            continue
        rows = entry["releases"]
        rels = [summarize_release(x["tag"], x["published_at"], x.get("name", ""),
                                  x.get("assets", []), repo_of[engine]) for x in rows]
        for r in rels:
            stats["窗口内 release"] += 1
            stats["runtime 资产行"] += len(r["runtime"])
            stats["附件（非产物）"] += len(r["attachments"])
            stats["认不出的词"] += len(r["unknown_tokens"])
            if not r["has_runtime"]:
                pass
            elif r["digest_missing"]:
                stats["不可校验的版本（上游没发 digest）"] += 1
            else:
                stats["可校验的候选版本"] += 1
            if r["unknown_tokens"]:
                problems.append(f"{engine}@{r['tag']}: 认不出的名字 {r['unknown_tokens']} —— "
                                "上游改了拼法或加了一档，必须人来认一次，不许静默归类")
            if r["has_runtime"]:
                stats["带产物的版本"] += 1
            else:
                stats["只有源码的标签"] += 1
        cand = [r for r in rels if r["has_runtime"]]
        good = [r for r in cand if r["verifiable"]]
        if len(good) < 2:
            problems.append(f"{engine}: 带产物且可校验的版本只有 {len(good)} 个 —— "
                            "版本轴不能只剩钉住那一颗")
        if len(cand) < 2:
            problems.append(f"{engine}: 窗口里只有 {len(cand)} 个带产物的版本 —— "
                            "升级与回退至少要有两个候选，否则版本轴是假的")
        mine = next((r for r in rels if r["tag"] == tag), None)
        if mine is None:
            problems.append(f"{engine}: 钉住的 {tag} 不在捕获的 {len(rels)} 个 release 里 —— "
                            "窗口没翻到它，这张表说的「能退回哪」就不成立")
        else:
            got = sorted(x["asset"] for x in mine["runtime"])
            want = matrix_names[engine]
            if got != want:
                problems.append(f"{engine}@{tag}: 这张表数出 {len(got)} 颗 runtime，矩阵数出 {len(want)} 颗 —— "
                                f"差异 {sorted(set(got) ^ set(want))[:4]}（两份产者读的是同一个 release）")
            lost = sorted(set(combos[engine]) - set(mine["os_covered"]))
            if lost:
                problems.append(f"{engine}@{tag}: 我们真装的 {lost} 在这一版的产物名里找不到 —— "
                                "这张表不能说它起得来")
        if mine and not mine["verifiable"]:
            problems.append(f"{engine}@{tag}: 我们钉住的这颗有 {len(mine['digest_missing'])} 个包"
                            "没有上游 digest —— 下载层就没有可对照的第二家哈希")
        newest = cand[0] if cand else None
        if newest and mine and newest["tag"] != tag:
            gone = sorted(set(mine["os_covered"]) - set(newest["os_covered"]))
            hit_shipped = sorted(set(gone) & set(combos[engine]))
            if hit_shipped:
                problems.append(f"{engine}: 从 {tag} 升到 {newest['tag']} 会丢掉真装的那几档 {hit_shipped} —— "
                                "这条升级路现在不能走")
            deltas.append({"engine": engine, "from": tag, "to": newest["tag"],
                           # 这是"窗口里还有多少个带产物的版本"，不是"漂了几个"——
                           # 后者是 engines[engine].newer_runtime_releases（钉住那颗上面有几个）。
                           "runtime_releases_in_window": len(cand),
                           "os_lost": gone,
                           "os_added": sorted(set(newest["os_covered"]) - set(mine["os_covered"]))
                           if mine else [],
                           "because": "只报不跟：上游发了新版本是一次改动 + 一次复核，不是自动动作"})
        if not cand and rels:
            problems.append(f"{engine}: 窗口里一个带产物的 release 都没有 —— 上游改了发布方式")
        if rels and rels[0] is not None and not rels[0]["has_runtime"]:
            stats["latest 是空标签的引擎"] += 1
        older = entry.get("older_releases", [])
        older_no_digest = [x["tag"] for x in older if x["assets"] and not x["no_digest_assets"]]
        older_all_missing = [x["tag"] for x in older
                             if x["assets"] and x["no_digest_assets"] == x["assets"]]
        stats["窗口之外还看了的 release"] = stats.get("窗口之外还看了的 release", 0) + len(older)
        stats["窗口之外全没有 digest 的 release"] = stats.get(
            "窗口之外全没有 digest 的 release", 0) + len(older_all_missing)
        engines[engine] = {
            "repo": repo_of[engine], "pinned": tag, "shipped_os": combos[engine],
            "pages_fetched": entry["pages_fetched"], "per_page": entry["per_page"],
            "captured": len(rels), "oldest_captured_tag": rels[-1]["tag"] if rels else None,
            "releases": rels, "latest_tag": rels[0]["tag"] if rels else None,
            "latest_has_runtime": rels[0]["has_runtime"] if rels else None,
            "latest_runtime_tag": newest["tag"] if newest else None,
            "newer_runtime_releases": (cand.index(mine) if mine in cand else None),
            "verifiable_tags": [r["tag"] for r in good],
            "oldest_verifiable_tag": good[-1]["tag"] if good else None,
            "unverifiable_tags": [r["tag"] for r in cand if not r["verifiable"]],
            "source_only_tags": [r["tag"] for r in rels if not r["has_runtime"]],
            "older_releases_seen": len(older),
            "older_without_digest_tags": older_all_missing,
            "newest_older_release_with_digest": older_no_digest[0] if older_no_digest else None,
        }
    doc = {"schema": 1, "rule": RULE, "per_page_policy": {"per_page": PER_PAGE,
                                                           "max_pages": MAX_PAGES},
           "from": "GitHub releases 列表（tag / published_at / asset 的 name+size+digest），"
                   "按 payload-plan 钉的三个仓库各自翻到钉住那一颗为止",
           "engines": engines, "capability_delta": deltas, "stats": stats}
    return doc, problems


def drift(committed: dict, fresh: dict) -> list[str]:
    bad = []
    if committed.get("rule") != fresh.get("rule"):
        bad.append("口径变了（窗口多大、什么算候选、latest 那句怎么说、漂了跟不跟）")
    if committed.get("per_page_policy") != fresh.get("per_page_policy"):
        bad.append(f"窗口政策变了 {committed.get('per_page_policy')} → {fresh.get('per_page_policy')}")
    if committed.get("stats") != fresh.get("stats"):
        bad.append(f"覆盖度变了 {committed.get('stats')} → {fresh.get('stats')}")
    if committed.get("capability_delta") != fresh.get("capability_delta"):
        bad.append("相邻版本的组合差异变了 —— 上游加档或撤档，升级那句要重看")
    for e, body in fresh["engines"].items():
        old = committed["engines"].get(e)
        if not old:
            bad.append(f"{e}: 新增引擎，已提交那张没有")
            continue
        for f in ("pinned", "latest_tag", "latest_has_runtime", "latest_runtime_tag",
                  "newer_runtime_releases", "shipped_os"):
            if old.get(f) != body[f]:
                bad.append(f"{e}: {f} 变了 {old.get(f)} → {body[f]}")
        if old.get("source_only_tags") != body["source_only_tags"]:
            bad.append(f"{e}: 只有源码的标签集合变了 {old.get('source_only_tags')} → "
                       f"{body['source_only_tags']} —— latest 陷阱的形状改了")
        if [r["tag"] for r in old["releases"]] != [r["tag"] for r in body["releases"]]:
            bad.append(f"{e}: 窗口里的 release 序列变了 —— 上游撤了标签或补发了版本")
        for o, n in zip(old["releases"], body["releases"]):
            if o["os_covered"] != n["os_covered"] or o["runtime_assets"] != n["runtime_assets"]:
                bad.append(f"{e}@{n['tag']}: 这一版的产物覆盖面变了 "
                           f"{o['os_covered']}/{o['runtime_assets']} → {n['os_covered']}/{n['runtime_assets']}")
            for x, y in zip(o["runtime"], n["runtime"]):
                if x["sha256"] != y["sha256"] or x["size"] != y["size"]:
                    bad.append(f"{e}@{n['tag']}/{x['asset']}: 同一颗资产的哈希或体积变了 —— "
                               "上游换过附件，钉值要重立")
                    break
    return bad


def pin_drift(committed: dict, fresh: dict) -> list[str]:
    """现取窗口时只核对钉住那颗还在、内容没被重打包。

    llama 一天发几十个 release：把「上游又发了新版」也当红，这条车道会永远红，
    红就不再是信号。新版本只在下一次显式重抓时进表（漂移只报不跟）。
    """
    bad = []
    for e, body in fresh["engines"].items():
        old = committed["engines"].get(e)
        if not old:
            bad.append(f"{e}: 新增引擎，已提交那张没有")
            continue
        if old["pinned"] != body["pinned"]:
            bad.append(f"{e}: 钉值变了 {old['pinned']} → {body['pinned']}")
            continue
        op = next((r for r in old["releases"] if r["tag"] == old["pinned"]), None)
        np_ = next((r for r in body["releases"] if r["tag"] == body["pinned"]), None)
        if np_ is None:
            bad.append(f"{e}: 上游窗口里已经没有钉住的 {body['pinned']} —— "
                       "撤标签或翻不到，回退/升级都无从谈起")
            continue
        if op is not None:
            oldmap = {x["asset"]: (x["size"], x["sha256"]) for x in op["runtime"]}
            newmap = {x["asset"]: (x["size"], x["sha256"]) for x in np_["runtime"]}
            if oldmap != newmap:
                diff = sorted(set(oldmap) ^ set(newmap))[:3]
                changed = sorted(k for k in set(oldmap) & set(newmap)
                                 if oldmap[k] != newmap[k])[:3]
                bad.append(f"{e}@{body['pinned']}: 钉住那颗的产物变了（增删 {diff}，"
                           f"体积/哈希 {changed}）—— 上游重打包过，钉值要重立")
        if old["shipped_os"] != body["shipped_os"]:
            bad.append(f"{e}: 真装的 OS 集合变了 {old['shipped_os']} → {body['shipped_os']}")
    return bad


def compare(committed: dict, fresh: dict, live: bool = False) -> list[str]:
    return pin_drift(committed, fresh) if live else drift(committed, fresh)


def _get(url: str) -> list:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("ACTIONS_TOKEN")
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "local-ai-studio-versions"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:                            # noqa: BLE001
        raise SystemExit(f"BAD: 取不到 {url}（{type(exc).__name__}: {exc}）—— 取数失败不能算核对过") from exc


def fetch(dest: str, repos: dict, pinned: dict, per_page: int = PER_PAGE) -> None:
    """从最新往下翻，翻到包含钉住那一颗为止（上限 MAX_PAGES 页）。

    为什么要翻而不是写死 12：llama 一天发 6 个 release，固定窗口根本装不下我们钉的 b11727；
    窗口不含钉值时"能退回哪"这句就是空的 —— 而这张表看起来仍然是完整的一张表。
    """
    out = {}
    for engine, repo in sorted(repos.items()):
        want = pinned[engine]
        body: list = []
        pages = 0
        while pages < MAX_PAGES:
            pages += 1
            got = _get(f"https://api.github.com/repos/{repo}/releases"
                       f"?per_page={per_page}&page={pages}")
            body += got
            if any(x["tag_name"] == want for x in got) or len(got) < per_page:
                break
        if len(body) < 2:
            raise SystemExit(f"BAD: {repo} 只回了 {len(body)} 个 release，窗口太小不能算版本轴")
        at = next(i for i, x in enumerate(body) if x["tag_name"] == want)
        keep = max(KEEP_MIN, at + 1)
        if len(body) > keep:
            print("裁剪", repo, "丢掉更老的", len(body) - keep, "个（保留到钉住那颗与最近",
                  KEEP_MIN, "个）")
        older = [{"tag": x["tag_name"], "published_at": x["published_at"],
                  "assets": len(x.get("assets", [])),
                  "no_digest_assets": sum(1 for a in x.get("assets", [])
                                          if not (a.get("digest") or ""))}
                 for x in body[keep:]]
        body = body[:keep]
        if not any(x["tag_name"] == want for x in body):
            raise SystemExit(f"BAD: {repo} 翻了 {pages} 页仍没有 {want} —— "
                             "上游撤了标签，这张表不能说能退回它")
        out[f"{repo}@{want}"] = {
            "pages_fetched": pages, "per_page": per_page,
            "older_releases": older,
            "releases": [{"tag": x["tag_name"], "published_at": x["published_at"],
                          "name": x.get("name") or "",
                          "assets": [{"name": a["name"], "size": a["size"],
                                      "sha256": (a.get("digest") or "").removeprefix("sha256:")}
                                     for a in x.get("assets", [])]} for x in body]}
        print("捕获", repo, len(body), "个 release（", pages, "页 ）")
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    with open(dest, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    print("落盘捕获", dest, sorted(out))


def selftest() -> int:
    bad: list[str] = []
    matrix, plan, caps = load(MATRIX), load(PAYLOAD), load(CAPTURES)
    doc, problems = build(caps, matrix, plan)
    if problems:
        bad.append(f"真输入应当 0 问题，实有 {len(problems)}：{problems[:2]}")
    if len(doc["engines"]) != 3:
        bad.append(f"三个引擎都该有版本轴，现在 {sorted(doc['engines'])}")
    if any(e["newer_runtime_releases"] is None for e in doc["engines"].values()):
        bad.append("钉住那颗必须能数出它上面还有几个带产物的版本 —— 那是'漂了几个'那一格")
    # 正向对照：whisper 的 latest 确实是 0 产物的源标签（这就是不许按 latest 取 runtime 的证据）
    w = doc["engines"].get("whisper", {})
    if w.get("latest_has_runtime") is not False or not w.get("source_only_tags"):
        bad.append(f"whisper 的 latest 陷阱没被表抓住：latest_has_runtime="
                   f"{w.get('latest_has_runtime')} source_only={w.get('source_only_tags')}")
    if doc["stats"]["认不出的词"] != 0:
        bad.append(f"真输入里不该有认不出的产物名（附件另算），实有 {doc['stats']['认不出的词']}")
    if any(not e["verifiable_tags"] for e in doc["engines"].values()):
        bad.append("每个引擎都至少要有一个可校验的候选版本")
    if not any(e["older_releases_seen"] > 0 for e in doc["engines"].values()):
        bad.append("窗口之外一段都没看过 —— 那「回退深度被 digest 边界限制」这句是印象不是测量")
    if doc["stats"]["窗口之外全没有 digest 的 release"] < 1:
        bad.append("翻窗口时没量到任何「整包都没有 digest」的老 release —— "
                   "上游后来补了字段就重看这一格，但现在它必须是量出来的数")
    # ① 抹掉一颗 runtime 的 digest 必须被报（换版本要能校验）
    c1 = json.loads(json.dumps(caps))
    k1 = sorted(c1)[0]
    pin1 = matrix["engines"][next(e for e, v in matrix["engines"].items()
                                  if v["repo"] == k1.split("@")[0])]["release"]
    for r in c1[k1]["releases"]:
        if r["tag"] == pin1:
            # 只动"确实是 runtime"的那一颗：清了附件或非 runtime 的哈希，
            # verifiable 本来就不该变 —— 那不是判据坏了，是探针打错了地方。
            for a in r["assets"]:
                if a["sha256"] and bd.classify(a["name"], pin1)["kind"] == "runtime"                         and not ATTACHMENT.search(a["name"]):
                    a["sha256"] = ""
                    break
            break
    if not any("没有上游 digest" in x for x in build(c1, matrix, plan)[1]):
        bad.append("钉住那颗缺 digest 时没红 —— 那 verifiable 这一列是装饰")
    # ② 把钉住的 tag 从窗口里删掉必须红（窗口政策与钉值脱节）
    c2 = json.loads(json.dumps(caps))
    k2 = sorted(c2)[0]
    eng2 = next(e for e, v in matrix["engines"].items() if v["repo"] == k2.split("@")[0])
    drop = matrix["engines"][eng2]["release"]
    c2[k2]["releases"] = [r for r in c2[k2]["releases"] if r["tag"] != drop]
    if not any("不在捕获的" in x for x in build(c2, matrix, plan)[1]):
        bad.append(f"钉住的 {drop} 被从窗口里删掉时没红")
    # ③ 最新那个带产物的版本里，把我们真装的那一档删掉 —— 必须报"这条升级路现在不能走"。
    c3 = json.loads(json.dumps(caps))
    for key, entry in c3.items():
        engine = next(e for e, v in matrix["engines"].items() if v["repo"] == key.split("@")[0])
        os_want = shipped_combos(plan)[engine][0]
        for r in entry["releases"]:
            keep = [x for x in r["assets"] if ATTACHMENT.search(x["name"])
                    or bd.classify(x["name"], r["tag"])["kind"] != "runtime"
                    or bd.classify(x["name"], r["tag"])["os"] != os_want]
            if len(keep) < len(r["assets"]):
                r["assets"] = keep
                break
        break
    _d3, p3 = build(c3, matrix, plan)
    if not any("升级路" in x or "找不到" in x for x in p3):
        bad.append("新版本丢掉真装的那一档时没红")
    # ④ 上游改成拼法（加一个 hip 档）必须被认出来
    c4 = json.loads(json.dumps(caps))
    c4[sorted(c4)[0]]["releases"][0]["assets"].append(
        {"name": "llama-b99999-bin-ubuntu-hip-x64.tar.gz", "size": 1, "sha256": "0" * 64})
    if not any("认不出的名字" in x for x in build(c4, matrix, plan)[1]):
        bad.append("新的后端词被静默放过了")
    # ⑤ 已提交那张被手改一个 latest 指针，漂移判据必须叫
    c5 = json.loads(json.dumps(doc))
    for e in c5["engines"].values():
        e["latest_runtime_tag"] = "不存在的标签"
        break
    if not any("latest_runtime_tag 变了" in x for x in drift(doc, c5)):
        bad.append("latest 指针被手改时漂移判据没红")
    # ⑥ 同一颗资产的哈希被改，必须叫（上游换过附件 = 钉值要重立）
    c6 = json.loads(json.dumps(doc))
    for e in c6["engines"].values():
        for r in e["releases"]:
            if r["runtime"]:
                r["runtime"][0]["sha256"] = "f" * 64
                break
        break
    if not any("哈希或体积变了" in x for x in drift(doc, c6)):
        bad.append("上游换附件时漂移判据没红")
    # ⑦ live 口径：上游又发新版不该红（llama 一天几十个版本），但钉住那颗被重打包必须红。
    c7 = json.loads(json.dumps(doc))
    for e in c7["engines"].values():
        e["latest_tag"] = "更新的标签"
        e["newer_runtime_releases"] = 999
    if pin_drift(doc, c7):
        bad.append(f"新版发布被 live 口径当成了红：{pin_drift(doc, c7)[:1]}")
    if not compare(doc, c7, live=False):
        bad.append("离线口径没把 latest 变化算进漂移 —— 两种口径被写成了同一种")
    c8 = json.loads(json.dumps(doc))
    for e in c8["engines"].values():
        pin = next(r for r in e["releases"] if r["tag"] == e["pinned"])
        pin["runtime"][0]["sha256"] = "e" * 64
        break
    if not any("钉住那颗的产物变了" in x for x in pin_drift(doc, c8)):
        bad.append("钉住那颗被重打包时 live 口径没红")
    print("---- 结论 ----", doc["stats"],
          {e: (v["latest_tag"], v["latest_has_runtime"], v["latest_runtime_tag"],
               v["newer_runtime_releases"]) for e, v in doc["engines"].items()},
          f"BAD 数量 = {len(bad)}")
    for x in bad:
        print("BAD:", x)
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--live", action="store_true",
                    help="现取窗口时只核对钉住那颗还在、哈希没变；上游又发新版不算红")
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--captures", default=CAPTURES)
    ap.add_argument("--per-page", dest="per_page", type=int, default=PER_PAGE)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    if a.fetch:
        _p, repos = bd.picks_from_payload(PAYLOAD)
        matrix = load(MATRIX)
        fetch(a.captures, repos, {e: v["release"] for e, v in matrix["engines"].items()},
              a.per_page)
    if a.selftest:
        return selftest()
    matrix, plan = load(MATRIX), load(PAYLOAD)
    doc, problems = build(load(a.captures), matrix, plan)
    if a.check:
        bad = compare(load(a.out), doc, a.live) + problems
        for x in bad:
            print("BAD:", x)
        print(f"比对了 {len(doc['engines'])} 个引擎 / 窗口内 {doc['stats']['窗口内 release']} 个 release，"
              f"带产物 {doc['stats']['带产物的版本']} 个，问题 {len(bad)} 条")
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
