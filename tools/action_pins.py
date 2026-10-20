#!/usr/bin/env python3
"""第三方 GitHub Action 也要按不可变坐标钉住 —— 这是唯一出包路径上的供应链面。

为什么这一轮专门做它：整个项目的硬约束是「只能靠 GitHub Actions 出包」，
而此前三个 workflow 里 **全部 4 个 `uses:` 都写的是浮动大版本 tag**
（`actions/checkout@v4`、`actions/setup-python@v5`、`actions/upload-artifact@v4`、
`actions/download-artifact@v4`）。仓库对引擎、权重、工具带都定了"release + sha256"的规矩，
自己跑包的那只手却一直在跟着别人随时会移动的 tag 走 —— 典型的"规矩写在文档里、代码里没执行"。
现在把它变成判据：

  * 车道里每个 `uses:` 必须是 40 位 commit SHA（不是就红）；
  * 钉住的那个 SHA 必须还能取到，且那份 `action.yml` 的 `runs.using` 与 `inputs`
    集合必须与已提交那张一致（换 runtime 或改输入就红）；
  * `# v4` 那种尾注记着原来的浮动 tag：车道每次重新解析它，一旦与本表不同就红 ——
    意思是"上游出了新版，要么显式重钉、要么写明不跟"，**不许悄悄跟**。

`tauri-apps/tauri-action` 现在由 `installer.yml` 使用（外壳出包车道 #30），量好的形状继续
留作漂移判据：`v1.0.0` → `1deb371b…`、`runs.using = node24`、23 个输入、入口是仓库里提交的
`dist/index.js`（所以钉 commit 就等于钉住那份 bundle）。顺带记一处容易看错的形状：
它的 **release 叫 `action-v1.0.0`，而 tag 叫 `v1.0.0`** —— 按 tag 拼 raw URL 时别拿 release 名。
`RESERVED` 里的条目一旦真被某个 workflow 用上，就只出现在 `pins` 里，不再重复记成"预留"。

用法：`--selftest` 先证明判据本身会红；`--offline` 只算本地能算的部分（`uses:` 的形状）；
`--check` 走网络逐条核对并与已提交那张比漂移。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

OUT = os.path.join(ROOT, "pins", "actions.json")
USES_RX = re.compile(r"^\s*(?:-\s+)?uses:\s*([^\s@]+)@(\S+)(?:\s+#\s*([A-Za-z0-9._-]+))?\s*$",
                     re.M)
SHA_RX = re.compile(r"^[0-9a-f]{40}$")
WORKFLOWS = (".github/workflows/ci.yml", ".github/workflows/engine-live.yml",
             ".github/workflows/package.yml", ".github/workflows/installer.yml",
             ".github/workflows/shell.yml")
# 已经由 installer.yml 用上；这行留着是为了让 release 名与 tag 名不一致这件事每次都被复核。
RESERVED = {"tauri-apps/tauri-action": ("v1.0.0", "1deb371b0cd8bd54025b384f1cd735e725c4060f")}


def headers() -> dict:
    """带上 runner 给的 GITHUB_TOKEN：匿名 60 次/小时在这条车道上会撞 403，
    而 403 被吞掉之后"取不到"看起来就像"没漂"。没 token 也能跑，只是更容易整片报取不到。"""
    h = {"User-Agent": "action-pins", "Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("ACTIONS_TOKEN")
    if tok:
        h["Authorization"] = "Bearer " + tok.strip()
    return h


def api_status(path: str) -> tuple[int | None, dict | None]:
    """返回 (HTTP 状态, 解析后的 JSON)。状态要留着，因为 403 与 404 是两件不同的事：
    一个是"这台机器没带 token、被限流"，另一个才是"上游真的没这个提交"。"""
    req = urllib.request.Request(f"https://api.github.com/{path}", headers=headers())
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, (json.load(r) if r.status == 200 else None)
    except urllib.error.HTTPError as e:      # 403/404 也要关连接：漏了就是一条 ResourceWarning
        e.close()
        return e.code, None
    except Exception:                        # noqa: BLE001  网络断了也是"没证到"，别抛死整轮
        return None, None


def api(path: str) -> dict | None:
    return api_status(path)[1]


def raw(repo: str, sha: str, name: str = "action.yml") -> str | None:
    url = f"https://raw.githubusercontent.com/{repo}/{sha}/{name}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers()),
                                    timeout=40) as r:
            return r.read().decode("utf-8", errors="replace") if r.status == 200 else None
    except urllib.error.HTTPError as e:
        e.close()
        return None
    except Exception:                        # noqa: BLE001
        return None


def parse_uses(texts: dict[str, str]) -> list[dict]:
    """这些 workflow 里的 `uses:`，按 owner/repo@ref 归并并数每个文件用了几次。"""
    out: dict[str, dict] = {}
    for fname, text in sorted(texts.items()):
        for m in USES_RX.finditer(text):
            repo, ref, tag = m.group(1), m.group(2), m.group(3)
            if repo.startswith("./") or repo.startswith("../"):
                continue                     # 本仓库自己的本地 action，不是第三方
            key = f"{repo}@{ref}"
            row = out.setdefault(key, {"uses": key, "repo": repo, "ref": ref,
                                       "tag_comment": tag, "workflows": {}})
            row["workflows"][fname] = row["workflows"].get(fname, 0) + 1
    return [out[k] for k in sorted(out)]


def shape(action_yml: str) -> tuple[str | None, list[str]]:
    """`action.yml` 的 `runs.using` 与 `inputs` 键名集合。

    这里**用 YAML 解析器**，不是随手写的缩进正则：先写过一版正则，把 checkout 的
    21 个输入读成 8 个（`description: |-` 那种折叠块让它提前收尾），
    而这张表是漂移判据的基线 —— 基线读少了，上游真加输入时那次比对反而像"没变"。
    自己写的读者要拿权威读者对一遍才知道对不对，这一条现在有单测钉着
    （`tests/test_action_pins.py::TestShapeAgainstYaml`）。
    """
    try:
        import yaml
    except Exception:                        # noqa: BLE001  没有 pyyaml 就当没读出来，别硬猜
        return None, []
    try:
        d = yaml.safe_load(action_yml) or {}
    except Exception:                        # noqa: BLE001
        return None, []
    runs = d.get("runs") or {}
    return runs.get("using"), sorted((d.get("inputs") or {}).keys())


def pin_one(row: dict, live: bool = True) -> tuple[dict, list[str]]:
    """钉住的那个 ref 必须是 SHA、必须还能取到，形状还得与已提交那张对得上。"""
    bad = []
    if not SHA_RX.match(row["ref"]):
        bad.append(f"{row['uses']}: 不是 40 位 commit SHA —— 出包路径上不许用会移动的 tag")
        return row, bad
    if not live:
        return row, bad
    status, meta = api_status(f"repos/{row['repo']}/commits/{row['ref']}")
    if not meta or not meta.get("sha"):
        why = ("被限流/没权限（本机跑请带 GITHUB_TOKEN）" if status in (401, 403)
               else "取不到提交" if status == 404 else f"取不到（HTTP {status}）")
        bad.append(f"{row['uses']}: {why} —— 这一条现在是空的，不能算核过")
        return row, bad
    row["commit_date"] = (meta.get("commit") or {}).get("committer", {}).get("date")
    yml = raw(row["repo"], row["ref"])
    if yml is None:
        bad.append(f"{row['uses']}: 那份 action.yml 取不到 —— 车道根本不知道它会怎么跑")
        return row, bad
    using, inputs = shape(yml)
    if not using:
        bad.append(f"{row['uses']}: action.yml 里读不到 runs.using（composite 还是 js？）")
    row["runs_using"], row["inputs"] = using, inputs
    row["license"] = ((api(f"repos/{row['repo']}") or {}).get("license") or {}).get("spdx_id")
    if row.get("expect_using") and using != row["expect_using"]:
        bad.append(f"{row['uses']}: runs.using {row['expect_using']!r} → {using!r} "
                   "—— 上游换了 runtime，重钉前先确认 runner 上有它")
    if row.get("expect_inputs") is not None and inputs != sorted(row["expect_inputs"]):
        added = sorted(set(inputs) - set(row["expect_inputs"]))
        gone = sorted(set(row["expect_inputs"]) - set(inputs))
        bad.append(f"{row['uses']}: action.yml 的输入集合变了（多 {added} / 少 {gone}）"
                   " —— 我们用的那几个键得重看")
    return row, bad


def tag_drift(rows: list[dict], live: bool = True) -> list[str]:
    """尾注里那个浮动 tag 现在指哪儿：与本表不一致就报，让人**显式**决定要不要跟。"""
    bad = []
    for row in rows:
        tag = row.get("floating_tag")
        if not tag or not live:
            continue
        now = (api(f"repos/{row['repo']}/commits/{tag}") or {}).get("sha")
        row["tag_now_points_to"] = now
        if now and now != row["ref"]:
            bad.append(f"{row['repo']}: tag {tag} 已经指向 {now[:10]}，本表钉的是 "
                       f"{row['ref'][:10]} —— 要么显式重钉，要么在这条上写明不跟，不许悄悄跟")
    return bad


def read_workflows() -> dict[str, str]:
    texts = {}
    for rel in WORKFLOWS:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            texts[rel] = fh.read()
    return texts


def build(live: bool = True, known: dict | None = None) -> tuple[dict, list[str]]:
    known = known or {}
    old_rows = {r["uses"]: r for r in (known.get("pins") or [])}
    rows = parse_uses(read_workflows())
    problems = []
    for row in rows:
        old = old_rows.get(row["uses"], {})
        row["floating_tag"] = row.get("tag_comment") or old.get("floating_tag")
        row["expect_using"] = old.get("runs_using")
        row["expect_inputs"] = old.get("inputs")
        _pinned, bad = pin_one(row, live)
        problems += bad
        if row["repo"] in RESERVED:
            # release 名与 tag 名不是一回事（tauri-action 的 release 叫 action-v1.0.0、
            # tag 叫 v1.0.0）；这两个字段对已启用的 action 也要留着，否则"名字对不上"的
            # 教训只存在于预留记录里。
            row["latest_release_tag"] = (api(f"repos/{row['repo']}/releases/latest")
                                         or {}).get("tag_name")
            row["pin_is_a_release_tag"] = row["latest_release_tag"] == row["floating_tag"]
        for k in ("expect_using", "expect_inputs"):
            row.pop(k, None)                 # 内部比对用的暂存，不进表（表里重复两份会漂）
    problems += tag_drift(rows, live)
    old_res = {r.get("repo"): r for r in (known.get("reserved_for_installer_lane") or [])}
    used_repos = {r["repo"] for r in rows}
    reserved = []
    for repo, (tag, sha) in RESERVED.items():
        if repo in used_repos:
            continue                          # 已经进了 pins，不再重复记一份"预留"
        old = old_res.get(repo, {})
        row = {"uses": f"{repo}@{sha}", "repo": repo, "ref": sha, "floating_tag": tag,
               "tag_comment": None, "workflows": {},
               "expect_using": old.get("runs_using"), "expect_inputs": old.get("inputs")}
        _pinned, bad = pin_one(row, live)
        problems += bad
        # release 名与 tag 名不是一回事：tauri-action 的 release 叫 action-v1.0.0、tag 叫 v1.0.0。
        # 这句话以前写在文档里，现在让它每次都被查（两个名字都进表，漂了就红）。
        row["latest_release_tag"] = (api(f"repos/{repo}/releases/latest") or {}).get("tag_name")
        row["pin_is_a_release_tag"] = row["latest_release_tag"] == tag
        for k in ("expect_using", "expect_inputs"):
            row.pop(k, None)
        reserved.append(row)
    doc = {"schema": 1,
           "policy": {"rule": "出包路径上的每个 uses: 都钉 40 位 commit SHA；"
                              "浮动 tag 移动只报不跟，重钉必须是显式动作",
                      "why": "硬约束是「只能靠 GitHub Actions 出包」，这些 action 就是那条路径本身",
                      "runtime_note": "官方四个都是 node20，tauri-action v1.0.0 是 node24 —— "
                                      "runtime 由 Actions 自己装，不依赖镜像里的 Node"},
           "counts": {"workflow_files": len(WORKFLOWS),
                      "uses_total": sum(sum(r["workflows"].values()) for r in rows),
                      "distinct": len(rows)},
           "pins": rows,
           "reserved_for_installer_lane": reserved}
    return doc, problems


def check(committed: dict, fresh: dict) -> list[str]:
    bad = []
    if committed.get("policy") != fresh.get("policy"):
        bad.append("钉法口径变了（谁必须钉 SHA、tag 动了跟不跟）—— 文档与实现要一起重看")
    if committed.get("counts") != fresh.get("counts"):
        bad.append(f"uses 计数变了 {committed.get('counts')} → {fresh.get('counts')}")
    for field in ("pins", "reserved_for_installer_lane"):
        old = {r["uses"]: r for r in committed.get(field) or []}
        new = {r["uses"]: r for r in fresh.get(field) or []}
        for extra in sorted(set(new) - set(old)):
            bad.append(f"{field}: 新增 {extra}，但没有已钉的记录")
        for gone in sorted(set(old) - set(new)):
            bad.append(f"{field}: {gone} 不在表里了 —— 是真的不用了，还是 uses 写法变了？")
        for uses in sorted(set(old) & set(new)):
            a, b = old[uses], new[uses]
            for f in ("ref", "runs_using", "inputs", "workflows", "license", "floating_tag",
                      "latest_release_tag", "pin_is_a_release_tag"):
                if a.get(f) != b.get(f):
                    bad.append(f"{field}.{uses}.{f}: {str(a.get(f))[:50]!r} → "
                               f"{str(b.get(f))[:50]!r}")
    return bad


def selftest() -> int:
    """离线对照：每条判据都得有一正一负，正向不跑就等于"红"没有信息量。"""
    bad = []
    good = ("      - uses: actions/checkout@" + "1" * 40 + "        # v4.2.2\n"
            "        uses: actions/setup-python@" + "2" * 40 + "\n")
    rows = parse_uses({"ci.yml": good})
    if [r["uses"] for r in rows] != ["actions/checkout@" + "1" * 40,
                                     "actions/setup-python@" + "2" * 40]:
        bad.append(f"uses 抽取不对：{[r['uses'] for r in rows]}")
    if rows[0]["tag_comment"] != "v4.2.2":
        bad.append(f"尾注里的浮动 tag 没抓到（那就没人知道该盯哪个 tag）：{rows[0]}")
    if rows[1]["tag_comment"] is not None:
        bad.append(f"没有尾注时不该凭空造一个 tag：{rows[1]}")
    twice = parse_uses({"a.yml": "- uses: actions/upload-artifact@" + "3" * 40 + "\n"
                                  "        uses: actions/upload-artifact@" + "3" * 40 + "\n"})
    if twice[0]["workflows"] != {"a.yml": 2}:
        bad.append(f"同一个 action 用两次被数成一次：{twice[0]}")
    _r, b1 = pin_one({"uses": "actions/checkout@v4", "repo": "actions/checkout", "ref": "v4",
                      "workflows": {}}, live=False)
    if not any("40 位" in x for x in b1):
        bad.append(f"浮动 tag 没被抓：{b1}")
    _r, b2 = pin_one({"uses": "x@" + "a" * 40, "repo": "x", "ref": "a" * 40,
                      "workflows": {}, "expect_using": "node20", "expect_inputs": ["a"]},
                     live=False)
    if b2:
        bad.append(f"SHA 写法被误报，或 live=False 时多比了形状：{b2}")
    using, inputs = shape("name: t\nruns:\n  using: 'node20'\n  main: 'index.js'\n"
                          "inputs:\n  a:\n    default: 1\n  b:\n    description: x\n")
    if using != "node20" or inputs != ["a", "b"]:
        bad.append(f"action.yml 形状没读对：{using!r} / {inputs}")
    u2, i2 = shape("runs:\n  using: node24\ninputs:\n  a:\n    description: |\n"
                   "      多行说明里这一行看起来像键:\n      another: 1\n  b:\n    default: 1\n")
    if u2 != "node24" or i2 != ["a", "b"]:
        bad.append(f"多行说明被当成输入键：{u2!r} / {i2}")
    # 反向：inputs 块里有空行 —— 用"连续块"正则会在这里提前停，把 21 个输入读成 0 个
    u3, i3 = shape("runs:\n  using: node20\ninputs:\n  a:\n    description: x\n"
                   "\n  b:\n    default: 1\n\nruns-else:\n  k: v\n")
    if i3 != ["a", "b"]:
        bad.append(f"inputs 块里的空行让读取提前结束：{i3}")
    u4, i4 = shape("runs:\n  using: node20\nsteps:\n  - uses: x\n    inputs:\n      fake: 1\n")
    if i4:
        bad.append(f"`steps:` 里缩进的 inputs 被当成了输入表：{i4}")
    if parse_uses({"c.yml": "uses: ./my-local-action@v1\n"}):
        bad.append("本仓库的本地 action 被当成第三方钉住了（这条必须带 ref，否则匹配不上就是假绿）")
    drift = tag_drift([{"repo": "x", "ref": "a" * 40, "floating_tag": None}], live=False)
    if drift:
        bad.append(f"没有 tag 的行被 tag_drift 报了：{drift}")
    print("---- 结论 ----  抓到 uses =", len(rows), "；尾注 tag =", rows[0]["tag_comment"],
          "；形状 =", using, inputs)
    for x in bad:
        print("  BAD:", x)
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="action_pins.py")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check", action="store_true", help="联网逐条核对并与已提交那张比漂移")
    ap.add_argument("--offline", action="store_true", help="只算 uses: 的形状，不取上游")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.offline and a.check:
        print("BAD: --offline 与 --check 不能同时用 —— 没取上游的核对不是核对")
        return 1
    known: dict = {}
    if a.check:
        with open(a.out, encoding="utf-8") as fh:
            known = json.load(fh)
    doc, problems = build(live=not a.offline, known=known)
    if a.offline:
        print("NOTE: --offline —— 本轮只核对了 uses: 的形状，没有取上游")
    elif not (os.environ.get("GITHUB_TOKEN") or os.environ.get("ACTIONS_TOKEN")):
        print("NOTE: 没带 GITHUB_TOKEN —— 匿名 60 次/小时，撞限流时这张表会整片报取不到")
    if a.check:
        bad = check(known, doc) + problems
        for x in bad:
            print("BAD:", x)
        print(f"比对了 {doc['counts']['distinct']} 个 action"
              f"（{doc['counts']['uses_total']} 处 uses），问题 {len(bad)} 条")
        return 1 if bad else 0
    for x in problems:
        print("BAD:", x)
    if problems:
        print("有 BAD → 不落盘")
        return 1
    if a.offline:
        # 离线那张表没有 runs.using / inputs / license —— 让它落盘就是拿一张
        # "看起来核对过"的表冒充核对结果（同一类错这一轮已经抓过两次）。
        print("--offline 不落盘：只核对了 uses: 的形状")
        for r in doc["pins"]:
            print(f"  {r['repo']:26} {r['ref'][:10]}  尾注 tag={r.get('floating_tag') or '-'} "
                  f"用于 {sum(r['workflows'].values())} 处")
        return 0
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    for r in doc["pins"]:
        print(f"  {r['repo']:26} {r['ref'][:10]}  尾注 tag={r.get('floating_tag') or '-'} "
              f"runtime={r.get('runs_using')} 输入 {len(r.get('inputs') or [])} "
              f"用于 {sum(r['workflows'].values())} 处")
    for r in doc["reserved_for_installer_lane"]:
        print(f"  {r['repo']:26} {r['ref'][:10]}  预留（#30 外壳车道） "
              f"runtime={r.get('runs_using')} 输入 {len(r.get('inputs') or [])}")
    print("落盘", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
