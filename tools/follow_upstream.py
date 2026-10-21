#!/usr/bin/env python3
"""llama.cpp 上游跟随：定时只报不跟，重钉是一次显式动作。

为什么不能把"上游又发版"当红：llama 一天发好几个 release（b11727 → b11755 只用了几天），
一条每次 schedule 都红的车道，红就不再是信号。

三种动作：
- `--check`：读已提交的窗口捕获（`--live` 现取），报告落后几个带产物的版本、
  候选版还能不能覆盖我们真装的 OS、钉住那颗有没有被撤/重打包。**只有钉住那颗坏了才红**；
  新版本身退出码 0，`--strict` 才把"候选不可升"也当红。
- `--update --tag bNNNNN`：显式选一颗候选，走 `payload_plan.rows_for` 的真路径
  （zip 读中央目录、tar 读成员表；只下载与读取，不执行任何东西）量出三行装机负载，输出 proposal。
- `--update ... --apply`：只改 `pins/payload-plan.json` 里该引擎的行。派生表
  （backend-matrix / runtime-versions / runtime-parity / backend-choice / shell-contract /
  machine-budget / launch-plan / citations）不会自动重写 —— 它们各有各的证据要求。
  命令会打印出来；只要还有派生表带着旧 tag，退出码 2，不许把半套重钉说成完成。

离线判据吃 `tests/fixtures/release-window.json` 与 `backend_assets.json`，所以没有 runner
也能重算；`--live` 只给定时车道用。
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import backends as bd  # noqa: E402
import engine_probe as ep  # noqa: E402
import payload_plan as pp  # noqa: E402
import runtime_versions as rv  # noqa: E402

PAYLOAD = os.path.join(ROOT, "pins", "payload-plan.json")
MATRIX = os.path.join(ROOT, "pins", "backend-matrix.json")
CAPTURES = os.path.join(ROOT, "tests", "fixtures", "release-window.json")
ASSETS = os.path.join(ROOT, "tests", "fixtures", "backend_assets.json")

# 重钉之后必须跟着重出的表。命令按依赖顺序写死在这里：少一步就会留下"payload 说新版本、
# 矩阵还说旧版本"的半套目录，而每一张表看上去都还是合法的 JSON。
DEPENDENTS = [
    ("tests/fixtures/backend_assets.json",
     "python tools/backends.py --fetch --captures tests/fixtures/backend_assets.json"),
    ("pins/backend-matrix.json",
     "python tools/backends.py --out pins/backend-matrix.json"),
    ("tests/fixtures/release-window.json",
     "python tools/runtime_versions.py --fetch --captures tests/fixtures/release-window.json"),
    ("pins/runtime-versions.json",
     "python tools/runtime_versions.py --out pins/runtime-versions.json"),
    ("tests/fixtures/parity-captures.json",
     "python tools/runtime_parity.py --fetch --captures tests/fixtures/parity-captures.json"),
    ("pins/runtime-parity.json",
     "python tools/runtime_parity.py --out pins/runtime-parity.json"),
    ("pins/backend-choice.json",
     "python tools/backend_choice.py --out pins/backend-choice.json"),
    ("pins/shell-contract.json",
     "python tools/shell_contract.py --out pins/shell-contract.json"),
    ("catalog/machine-budget.json",
     "python tools/machine_budget.py --out catalog/machine-budget.json"),
    ("pins/launch-plan.json",
     "python tools/launch_plan.py --out pins/launch-plan.json"),
]
PROSE = ["README.md", "docs/requirements.md", "docs/adr/0001-engines-from-upstream-binaries.md"]


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def pinned_release(plan: dict, engine: str) -> str:
    tags = {r.get("release") for r in plan["rows"]
            if r.get("engine") == engine and r.get("status") == "ok"}
    if len(tags) != 1:
        raise SystemExit(f"BAD: {engine} 在 payload-plan 里有 {sorted(tags)} 个 release —— 钉值不唯一")
    return tags.pop()


def shipped_os(plan: dict, engine: str) -> list[str]:
    return [r["os"] for r in plan["rows"]
            if r.get("engine") == engine and r.get("status") == "ok"]


def assets_of(rel: dict) -> list[dict]:
    return rel.get("assets") or []


def runtime_assets(rel: dict) -> list[dict]:
    tag = rel.get("tag") or rel.get("tag_name") or ""
    return [a for a in assets_of(rel) if bd.classify(a["name"], tag)["kind"] == "runtime"]


def has_runtime(rel: dict) -> bool:
    return bool(runtime_assets(rel))


def sha_of(asset: dict) -> str:
    return asset.get("sha256") or (asset.get("digest") or "").removeprefix("sha256:")


def as_ep_release(rel: dict) -> dict:
    """窗口捕获与 GitHub 原始 release 的同一形状：`engine_probe.plan` 只认后者。"""
    tag = rel.get("tag") or rel.get("tag_name") or ""
    return {"tag_name": tag, "published_at": rel.get("published_at", ""),
            "assets": [{"name": a["name"], "size": a["size"],
                        "digest": ("sha256:" + sha_of(a)) if sha_of(a) else None,
                        "browser_download_url": a.get("url") or a.get("browser_download_url") or ""}
                       for a in assets_of(rel)]}


def norm(name: str, tag: str) -> str:
    return name.replace(tag, "<tag>")


def required_missing(rel: dict, engine: str) -> list[str]:
    got = ep.plan({engine: [as_ep_release(rel)]})
    return [x for x in ep.check(got, False) if x.startswith(engine + "/")]


def unknown_tokens(rel: dict) -> list[str]:
    tag = rel.get("tag") or rel.get("tag_name") or ""
    out = []
    for a in runtime_assets(rel):
        c = bd.classify(a["name"], tag)
        if c["backend"] == "unknown_token" or "认不得的词" in c["note"]:
            out.append(a["name"])
    return out


def digest_missing(rel: dict) -> list[str]:
    return [a["name"] for a in runtime_assets(rel) if not sha_of(a)]


def evaluate(captures: dict, plan: dict, matrix: dict, assets: dict,
             engine: str = "llama") -> dict:
    repo = matrix["engines"][engine]["repo"]
    pin = pinned_release(plan, engine)
    key = f"{repo}@{pin}"
    entry = captures.get(key)
    if entry is None:
        return {"engine": engine, "repo": repo, "pinned": pin, "decision": "pinned_damaged",
                "reasons": [f"窗口捕获里没有 {key} —— 取数失败不能当成已是最新"],
                "behind": None, "candidate": None, "pinned_damage": [key]}
    rels = entry["releases"]
    damage: list[str] = []
    if matrix["engines"][engine]["release"] != pin:
        damage.append(f"矩阵钉的是 {matrix['engines'][engine]['release']}，payload-plan 钉的是 {pin}")
    pinned_rel = next((r for r in rels if r["tag"] == pin), None)
    if pinned_rel is None:
        damage.append(f"上游窗口里已经没有钉住的 {pin}（撤标签或翻不到）")
    else:
        want = {a["name"]: (a["size"], a["sha256"]) for a in assets.get(key, [])}
        got = {a["name"]: (a["size"], a["sha256"]) for a in assets_of(pinned_rel)}
        if not want:
            damage.append(f"没有 {key} 的已提交钉值捕获（无法核对上游有没有换过附件）")
        elif want != got:
            added = sorted(set(got) - set(want))[:4]
            gone = sorted(set(want) - set(got))[:4]
            changed = sorted(k for k in set(want) & set(got) if want[k] != got[k])[:4]
            damage.append(f"{pin} 的产物变了（增 {added}，删 {gone}，体积/哈希 {changed}）"
                          " —— 上游重打包过，钉值要重立")
    runtime = [r for r in rels if has_runtime(r)]
    newer = [r for r in runtime if ep.release_rank(r["tag"]) > ep.release_rank(pin)]
    cand = runtime[0] if runtime else None
    report = {"engine": engine, "repo": repo, "pinned": pin,
              "latest_tag": rels[0]["tag"] if rels else None,
              "latest_runtime_tag": cand["tag"] if cand else None,
              "behind": len(newer), "newer_tags": [r["tag"] for r in newer],
              "candidate": cand["tag"] if cand else None,
              "candidate_missing": [], "candidate_unknown": [], "candidate_no_digest": [],
              "added": [], "removed": [], "pinned_damage": damage,
              "reasons": []}
    if cand and newer:
        report["candidate_missing"] = required_missing(cand, engine)
        report["candidate_unknown"] = unknown_tokens(cand)
        report["candidate_no_digest"] = digest_missing(cand)
        base_rel = pinned_rel or next((r for r in rels if r["tag"] == pin), {})
        base = {norm(a["name"], pin) for a in runtime_assets(base_rel)}
        now = {norm(a["name"], cand["tag"]) for a in runtime_assets(cand)}
        report["added"] = sorted(now - base)
        report["removed"] = sorted(base - now)
        if report["candidate_missing"]:
            report["reasons"].append("候选版缺我们真装的 target："
                                     + ", ".join(report["candidate_missing"]))
        if report["candidate_unknown"]:
            report["reasons"].append("候选版有认不出的产物名，必须人来认一次："
                                     + ", ".join(report["candidate_unknown"][:4]))
        if report["candidate_no_digest"]:
            report["reasons"].append("候选版有产物没有上游 digest，不能进钉表："
                                     + ", ".join(report["candidate_no_digest"][:4]))
    if damage:
        report["decision"] = "pinned_damaged"
        report["reasons"] = damage + report["reasons"]
    elif not newer:
        report["decision"] = "up_to_date"
    elif report["reasons"]:
        report["decision"] = "blocked"
    else:
        report["decision"] = "newer_available"
    report["red"] = report["decision"] == "pinned_damaged"
    report["note"] = "新版只报不跟：升级是一次改动 + 一次复核，不是自动动作"
    return report


def render(r: dict) -> None:
    if r["decision"] == "pinned_damaged":
        print(f"BAD: {r['engine']} 钉住的 {r['pinned']} 不可信 —— 这条要红")
    elif r["decision"] == "up_to_date":
        print(f"{r['engine']}: 钉住 {r['pinned']}，窗口里没有更新的带产物版本；钉值未变")
    else:
        head = "可升" if r["decision"] == "newer_available" else "候选不可直接升"
        print(f"{r['engine']}: 钉住 {r['pinned']} → 最新带产物 {r['latest_runtime_tag']}"
              f"（落后 {r['behind']} 个）；候选 {r['candidate']} {head}")
    for x in r["reasons"]:
        print("  -", x)
    if r["added"]:
        print("  新名字:", r["added"][:6])
    if r["removed"]:
        print("  消失的名字:", r["removed"][:6])
    print("  结论:", {"newer_available": "有新版本（只报不跟）",
                      "up_to_date": "已最新",
                      "blocked": "上游新版不可直接升（上游发版本身不算红）",
                      "pinned_damaged": "钉值坏了（红）"}[r["decision"]])


def fetch_window(engine: str, repo: str, pin: str) -> dict:
    with tempfile.TemporaryDirectory() as d:
        dest = os.path.join(d, "window.json")
        rv.fetch(dest, {engine: repo}, {engine: pin}, rv.PER_PAGE)
        return load(dest)


def build_rows(plan: dict, engine: str, release: dict) -> tuple[list[dict], list[str]]:
    rows, bad = [], []
    targets = ep.plan({engine: [release]})
    skipped = ep.skipped_of({engine: [release]})
    for label in shipped_os(plan, engine):
        got, problems = pp.rows_for(label, targets, True, skipped)
        rows += [r for r in got if r.get("engine") == engine]
        bad += problems
    for r in rows:
        if r.get("status") != "ok":
            bad.append(f"{r['os']}/{r['capability']} 候选行状态是 {r.get('status')}")
        elif not str(r.get("digest", "")).startswith("sha256:"):
            bad.append(f"{r['os']}/{r['capability']} 没有上游 digest")
        elif not str(r.get("url", "")).startswith("https://"):
            bad.append(f"{r['os']}/{r['capability']} 的 URL 不是 https：{r.get('url')}")
    return rows, bad


def apply_rows(plan: dict, engine: str, rows: list[dict]) -> dict:
    out = copy.deepcopy(plan)
    by_os = {r["os"]: r for r in rows}
    for label in shipped_os(plan, engine):
        idx = next(i for i, r in enumerate(out["os"][label]) if r.get("engine") == engine)
        out["os"][label][idx] = by_os[label]
    out["rows"] = [r for label in out["os"] for r in out["os"][label]]
    out["totals_mib"] = {label: round(sum((r.get("archive_mib") or 0)
                                          for r in out["os"][label]), 1)
                         for label in out["os"]}
    out["gaps"] = [{"os": r["os"], "capability": r["capability"], "status": r["status"]}
                   for r in out["rows"] if r.get("status") != "ok"]
    return out


def residual(root: str, old_tag: str) -> list[tuple[str, str]]:
    stale = []
    for path, command in DEPENDENTS:
        full = os.path.join(root, path)
        if not os.path.isfile(full):
            stale.append((path, command))
            continue
        with open(full, encoding="utf-8") as fh:
            if old_tag in fh.read():
                stale.append((path, command))
    return stale


def prose_refs(root: str, old_tag: str) -> list[str]:
    out = []
    for rel in PROSE:
        full = os.path.join(root, rel)
        if os.path.isfile(full):
            with open(full, encoding="utf-8") as fh:
                if old_tag in fh.read():
                    out.append(rel)
    tests_dir = os.path.join(root, "tests")
    if os.path.isdir(tests_dir):
        for name in sorted(os.listdir(tests_dir)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(tests_dir, name), encoding="utf-8") as fh:
                if old_tag in fh.read():
                    out.append(f"tests/{name}")
    return out


def fetch_release(repo: str, tag: str, path: str = "") -> dict:
    if path:
        return load(path)
    return ep.github_api(f"repos/{repo}/releases/tags/{tag}")


def update(plan: dict, engine: str, tag: str, release: dict, apply: bool) -> int:
    old = pinned_release(plan, engine)
    if tag == old:
        print(f"BAD: {tag} 就是当前钉值 —— 没有要改的东西")
        return 1
    if ep.release_rank(tag) <= ep.release_rank(old):
        print(f"BAD: {tag} 不比当前钉值 {old} 新 —— 回退不是这条车道的动作")
        return 1
    missing = required_missing(release, engine)
    if missing:
        print("BAD: 候选缺我们真装的 target：", missing)
        return 1
    rows, bad = build_rows(plan, engine, release)
    if bad or len(rows) != len(shipped_os(plan, engine)):
        for x in bad:
            print("BAD:", x)
        print(f"BAD: 只量出 {len(rows)} 行（{len(shipped_os(plan, engine))} 个 OS）—— 不落盘")
        return 1
    proposal = {"schema": 1, "engine": engine, "from": old, "to": tag,
                "repo": plan_row_repo(release, engine), "rows": rows,
                "digest_missing": digest_missing(release),
                "unknown_tokens": unknown_tokens(release),
                "commands": [cmd for _p, cmd in DEPENDENTS],
                "note": "只改 payload-plan 的引擎行；派生表按 commands 逐张重出后再提交"}
    print(json.dumps(proposal, ensure_ascii=False, indent=1, sort_keys=True))
    if not apply:
        print(f"proposal：{old} → {tag}；加 --apply 才写 pins/payload-plan.json", file=sys.stderr)
        return 0
    new_plan = apply_rows(plan, engine, rows)
    with open(PAYLOAD, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(new_plan, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"已写 {PAYLOAD}: {engine} {old} → {tag}")
    stale = residual(ROOT, old)
    for path, cmd in stale:
        print(f"  还没重出: {path}  ←  {cmd}")
    refs = prose_refs(ROOT, old)
    if refs:
        print("  还需人工复核的断言/文案:", ", ".join(refs))
    if stale:
        print(f"BAD: 还有 {len(stale)} 张派生表带着旧 tag —— 重钉不算完成（退出码 2）")
        return 2
    return 0


def plan_row_repo(release: dict, engine: str) -> str:
    return ep.UPSTREAM[engine]["repo"]

def selftest() -> int:
    bad: list[str] = []
    plan, matrix = load(PAYLOAD), load(MATRIX)
    caps, assets = load(CAPTURES), load(ASSETS)
    pin = pinned_release(plan, "llama")
    repo = matrix["engines"]["llama"]["repo"]
    key = f"{repo}@{pin}"
    new_tag = "b99999"

    def with_candidate(c: dict) -> dict:
        """合成一颗比钉值新的 release：刚重钉完窗口里没有新版，判据也不能失去正向对照。"""
        rel = copy.deepcopy(next(r for r in c[key]["releases"] if r["tag"] == pin))
        rel["tag"] = new_tag
        rel["published_at"] = "2099-01-01T00:00:00Z"
        rel["assets"] = [dict(a, name=a["name"].replace(pin, new_tag))
                         for a in rel["assets"]]
        c[key]["releases"].insert(0, rel)
        return c

    caps2 = with_candidate(copy.deepcopy(caps))
    base = evaluate(caps2, plan, matrix, assets)
    if base["decision"] != "newer_available" or base["behind"] != 1:
        bad.append(f"合成一颗新版后应报落后 1 个可升版本，现为 {base['decision']} / {base['behind']}")
    if base["candidate"] != new_tag:
        bad.append(f"候选应是最新那颗 {new_tag}，现为 {base['candidate']}")
    if base["red"]:
        bad.append("新版本本身被当成了红")
    if base["candidate_missing"] or base["candidate_unknown"] or base["candidate_no_digest"]:
        bad.append(f"候选自检不过：{base['candidate_missing']} / {base['candidate_unknown']}")
    # ① 窗口里没有更新的版本 → up_to_date，不是 red
    r1 = evaluate(copy.deepcopy(caps), plan, matrix, assets)
    if r1["decision"] != "up_to_date" or r1["red"]:
        bad.append(f"没有新版时应 up_to_date 且不红，现为 {r1['decision']}")
    # ② 钉住那颗被重打包 → 必须红
    c2 = copy.deepcopy(caps2)
    pin_rel = next(r for r in c2[key]["releases"] if r["tag"] == pin)
    runtime_assets(pin_rel)[0]["sha256"] = "f" * 64
    r2 = evaluate(c2, plan, matrix, assets)
    if r2["decision"] != "pinned_damaged" or not r2["red"]:
        bad.append(f"钉值被重打包时没红：{r2['decision']}")
    # ③ 钉住那颗被撤标签 → 必须红
    c3 = copy.deepcopy(caps2)
    c3[key]["releases"] = [r for r in c3[key]["releases"] if r["tag"] != pin]
    r3 = evaluate(c3, plan, matrix, assets)
    if r3["decision"] != "pinned_damaged":
        bad.append(f"钉住那颗被撤时没红：{r3['decision']}")
    # ④ 候选丢掉真装的 win-x64/cpu → blocked，但不红（上游发版本身不是红）
    c4 = copy.deepcopy(caps2)
    cand4 = next(r for r in c4[key]["releases"] if r["tag"] == new_tag)
    cand4["assets"] = [a for a in cand4["assets"]
                       if not (bd.classify(a["name"], new_tag)["os"] == "win-x64"
                               and bd.classify(a["name"], new_tag)["backend"] == "cpu")]
    r4 = evaluate(c4, plan, matrix, assets)
    if r4["decision"] != "blocked" or r4["red"]:
        bad.append(f"候选丢档时应 blocked 且不红，现为 {r4['decision']}")
    if not r4["candidate_missing"]:
        bad.append("候选丢档没被点名")
    # ⑤ 候选产物没 digest → blocked
    c5 = copy.deepcopy(caps2)
    cand5 = next(r for r in c5[key]["releases"] if r["tag"] == new_tag)
    runtime_assets(cand5)[0]["sha256"] = ""
    r5 = evaluate(c5, plan, matrix, assets)
    if r5["decision"] != "blocked" or not r5["candidate_no_digest"]:
        bad.append(f"候选缺 digest 时应 blocked：{r5['decision']}")
    # ⑥ 候选多了一个认不出的后端词 → blocked（新词必须人来认）
    c6 = copy.deepcopy(caps2)
    cand6 = next(r for r in c6[key]["releases"] if r["tag"] == new_tag)
    cand6["assets"].append({"name": f"llama-{new_tag}-bin-ubuntu-hip-x64.tar.gz",
                            "size": 1, "sha256": "0" * 64})
    r6 = evaluate(c6, plan, matrix, assets)
    if r6["decision"] != "blocked" or not r6["candidate_unknown"]:
        bad.append(f"候选新后端词没被拦：{r6['decision']}")
    # ⑦ proposal 的 apply 只替换该引擎的行，另外两个引擎原样；旧 tag 仍被点名
    def fake_entries(url):
        if url.endswith(".zip"):
            return [{"name": f"llama-{new_tag}/llama-server.exe", "size": 10, "packed": 5},
                    {"name": f"llama-{new_tag}/llama-server-impl.dll", "size": 10, "packed": 5}]
        return [{"name": f"llama-{new_tag}/llama-server", "size": 10, "packed": None}]

    old_override = pp.ENTRIES_OVERRIDE
    pp.ENTRIES_OVERRIDE = fake_entries
    rel7 = as_ep_release(next(r for r in caps2[key]["releases"] if r["tag"] == new_tag))
    # 窗口捕获只存 name/size/sha256（版本轴不需要 URL）；真 GitHub release 带下载地址。
    for a in rel7["assets"]:
        a["browser_download_url"] = f"https://example.invalid/{a['name']}"
    try:
        rows, bad_rows = build_rows(plan, "llama", rel7)
    finally:
        pp.ENTRIES_OVERRIDE = old_override
    if bad_rows or len(rows) != 3:
        bad.append(f"proposal 行构建失败：{bad_rows} / {len(rows)} 行")
    else:
        new_plan = apply_rows(plan, "llama", rows)
        others = {r.get("engine") for label in ("win-x64", "linux-x64", "macos-arm64")
                  for r in new_plan["os"][label] if r.get("engine") not in (None, "llama")}
        if others != {"whisper", "sd"}:
            bad.append(f"apply 动了别的引擎：{others}")
        if {r["release"] for r in new_plan["rows"]
            if r.get("engine") == "llama"} != {new_tag}:
            bad.append("apply 没把三行都换成候选 tag")
        if len(new_plan["rows"]) != len(plan["rows"]):
            bad.append("apply 改了总行数")
    # ⑧ 回退/同标签必须拒绝
    with contextlib.redirect_stdout(io.StringIO()):
        same = update(plan, "llama", pin,
                      {"tag_name": pin, "assets": []}, False)
    if same != 1:
        bad.append("把同一颗 tag 当成更新时没拒绝")
    print("---- 结论 ----  baseline =", {k: base[k] for k in
                                        ("pinned", "latest_runtime_tag", "behind", "decision")})
    for x in bad:
        print("BAD:", x)
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="follow_upstream.py")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--live", action="store_true", help="现取窗口（只读 GitHub API）")
    ap.add_argument("--strict", action="store_true", help="候选不可升也红（定时车道默认不红）")
    ap.add_argument("--engine", default="llama")
    ap.add_argument("--captures", default=CAPTURES)
    ap.add_argument("--json", default="")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--tag", default="")
    ap.add_argument("--release-json", default="", help="离线：直接读一份 GitHub release JSON")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    plan, matrix = load(PAYLOAD), load(MATRIX)
    if a.update:
        if not a.tag:
            ap.error("--update 必须带 --tag（重钉是显式动作，不自动挑最新版）")
        return update(plan, a.engine, a.tag,
                      fetch_release(matrix["engines"][a.engine]["repo"], a.tag, a.release_json),
                      a.apply)
    if a.check:
        caps = (fetch_window(a.engine, matrix["engines"][a.engine]["repo"],
                             pinned_release(plan, a.engine))
                if a.live else load(a.captures))
        assets = load(ASSETS)
        report = evaluate(caps, plan, matrix, assets, a.engine)
        render(report)
        if a.json:
            with open(a.json, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(report, fh, ensure_ascii=False, indent=1, sort_keys=True)
                fh.write("\n")
            print("写出", a.json)
        rc = 1 if report["red"] else (1 if a.strict and report["decision"] == "blocked" else 0)
        return rc
    ap.error("要 --check 或 --update")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
