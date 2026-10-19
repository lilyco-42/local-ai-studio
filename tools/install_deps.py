#!/usr/bin/env python3
"""外壳在 Linux 上要装哪些包：从 Tauri 官方前提页**读出来**，不在文档里手抄。

起因：`docs/packaging.md` 把九个包名抄成了一段散文，`pins/citations.json` 只钉得住
`libwebkit2gtk-4.1-dev` 那一行（:33）。上游哪天加一个包或把 4.1 换成 4.2，
散文不会红 —— 而这条恰恰是"安装包车道第一步 apt install 什么"的唯一依据。
所以这一轮把它变成派生产物：`tools/install_deps.py` 解析那份 mdx 的 `<TabItem>` 分叉，
每个发行版家族各自一份包表，我们只取 ubuntu 对应的那一族（Debian）。

第二件事也必须写清楚，否则容易自我欺骗：**runner 镜像的 readme 里没提到某个包，
不等于那台机器上没装**（readme 是给人看的清单，不是 `dpkg -l`）。
所以本工具给的判语只有 `mentioned / not_mentioned`，并且把**搜索用的词**一起落盘；
"到底装没装"要等 CI 第一步 `apt-get install` + `pkg-config --modversion webkit2gtk-4.1`
打印出来才算数（这条已经在 `pins/citations.json` 里以 `absent` 型盯着 readme）。

用法：`--selftest` 先证明判据本身会红；`--check` 把已提交那张按输入重算一遍比漂移；
`--cache/--fill` 与出处闸门共用一套上游原文缓存。
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
import citation_check  # noqa: E402  复用"按 pin 住的 ref 取上游原文"

OUT = os.path.join(ROOT, "pins", "install-deps.json")
TAURI = ("tauri-apps/tauri-docs", "2e513e3f7934d68ec7fef37fc46a5aa482f8a091",
         "src/content/docs/start/prerequisites.mdx")
IMAGE = ("actions/runner-images", "b4067e48bd9bd803e958bfe1503ea0a06a3a127f",
         "images/ubuntu/Ubuntu2404-Readme.md")
OS_FAMILY = "Debian"        # ubuntu-24.04 runner 走这一族


def cite(spec: tuple) -> dict:
    repo, ref, path = spec
    return {"id": f"install-deps:{path}", "repo": repo, "ref": ref, "path": path,
            "line": 1, "expect": "x"}


def fetch(spec: tuple, cache: str, fill: bool) -> str | None:
    c = cite(spec)
    text = citation_check.read_cache(c, cache) if cache else None
    if text is not None:
        return text
    text = citation_check.fetch(c)
    if text is None:
        return None
    if fill and cache:
        citation_check.write_cache(c, cache, text)
    return text


FENCE_RX = re.compile(r"```(?:sh|bash)?\n(.*?)\n```", re.S)
TAB_RX = re.compile(r'<TabItem label="([^"]+)">')
CONT_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
NOISE = {"sudo", "apt", "apt-get", "install", "update", "dnf", "yum", "zypper", "in",
         "pacman", "-Syu", "-S", "--needed", "emerge", "apk", "add", "rpm-ostree",
         "yes", "--yes", "-y"}


PM_RX = re.compile(r"^\s*(?:sudo\s+)?(apt-get|apt|dnf|yum|zypper|apk|pacman|emerge|rpm-ostree)"
                   r"(?:\s(.*))?\s*\\?\s*$")
INSTALL_WORDS = ("install", "-S", "--needed", "in", "add", "--ask", "--no-ask")


def parse_families(text: str) -> dict:
    """每个 `<TabItem label="X">` 取它后面第一个代码块，只认**包管理器那一条安装命令**的包表。

    为什么不能"块里所有像包名的词都算"：真页里 Windows/Linux/macOS 那几个 tab 放的是
    `export JAVA_HOME=…` 和 `:::caution` 散文，第一版把 `Most / apps / refresh / export`
    当成了一族的包 —— 于是 `source.families` 里出现 10 个假家族。
    续行以 `\\` 结尾、`\\` 前还有空格，且包名常与 `sudo apt install` 同行：
    整行当一个 token 会漏掉第一颗（本机第一版就错过 webkit 那颗）。
    """
    marks = [(m.group(1), m.start()) for m in TAB_RX.finditer(text)]
    out: dict[str, dict] = {}
    for idx, (label, at) in enumerate(marks):
        end = marks[idx + 1][1] if idx + 1 < len(marks) else len(text)
        m = FENCE_RX.search(text, at, end)
        if not m:
            continue
        offset = text[:m.start(1)].count("\n")
        body = m.group(1).split("\n")
        for k, raw in enumerate(body):
            pm = PM_RX.match(raw)
            if not pm:
                continue
            head = (pm.group(2) or "").split()
            if not any(w in INSTALL_WORDS for w in head):
                continue          # `apt update` / `pacman -Syu` / `zypper up` 不是包表
            pkgs, seen = [], set()
            j = k
            stream = list(head)
            while True:
                line = body[j]
                cont = line.rstrip().endswith("\\")
                if j > k:
                    stream += line.strip().rstrip("\\").strip().split()
                for tok in stream:
                    stream = []                      # 只有第一条命令行的余部算包名
                    if tok in NOISE or tok.startswith("-") or not CONT_RX.match(tok) \
                            or tok in seen:
                        continue
                    seen.add(tok)
                    pkgs.append({"package": tok, "line": offset + j + 1})
                if not cont or j + 1 >= len(body):
                    break
                j += 1
            if pkgs:
                out[label] = {"pm": pm.group(1), "install_line": offset + k + 1,
                              "packages": pkgs}
            break
    return out


def image_packages(readme: str) -> dict:
    """镜像 readme 里的 `| 包名 | 版本 |` 表，按**包名逐字**建表。

    不用子串模糊搜：`file` 会命中 "profile"、`libssl` 会命中别的东西，
    模糊搜出来的"在场"是假在场。逐字命中才有得说，没命中只能说 readme 没提它。
    """
    out = {}
    for m in re.finditer(r"^\|\s*([^|]+?)\s*\|\s*([^|]+?)\s*\|", readme, re.M):
        k, v = m.group(1).strip(), m.group(2).strip()
        if not k or set(k) <= set("-: ") or k.lower() in ("package", "version", "tool", "name"):
            continue
        out.setdefault(k, v)
    return out


def probe_readme(pkg: str, table: dict) -> dict:
    """readme 逐字查这个包名；查不到只说"没提到"，不说"没装"（readme 不是 dpkg -l）。"""
    ver = table.get(pkg)
    return {"package": pkg, "probe": pkg, "verdict": "listed" if ver else "not_listed",
            "readme_version": ver}


def toolchain(readme: str) -> dict:
    out = {}
    for name, rx in (("rust", r"^- Rust ([0-9.]+)"), ("node", r"^- Node\.js ([0-9.]+)")):
        m = re.search(rx, readme, re.M)
        out[name] = m.group(1) if m else None
    return out


def build(cache: str = "", fill: bool = False) -> tuple[dict, list[str]]:
    problems = []
    doc = fetch(TAURI, cache, fill)
    rd = fetch(IMAGE, cache, fill)
    if doc is None:
        problems.append("装依赖清单：取不到 Tauri 前提页 → 无法核对包表")
    if rd is None:
        problems.append("装依赖清单：取不到 runner 镜像 readme → 无法核对在场情况")
    if problems:
        return {}, problems
    fams = parse_families(doc)
    fam = fams.get(OS_FAMILY) or {}
    if not fam:
        problems.append(f"前提页里认不出 {OS_FAMILY} 那一族 —— 上游改了页的结构，"
                        "apt 那一步的依据要重看，不许回退到手抄的表")
    apt = fam.get("packages") or []
    if fam and fam["pm"] not in ("apt", "apt-get"):
        problems.append(f"{OS_FAMILY} 那一族的包管理器不再是 apt（读到 {fam['pm']!r}）"
                        " —— 车道那一步的命令行要跟着改")
    names = [p["package"] for p in apt]
    if "libwebkit2gtk-4.1-dev" not in names:
        problems.append(f"{OS_FAMILY} 那一族里没有 libwebkit2gtk-4.1-dev：上游换版本了？"
                        "（换版本不是错，但 docs/packaging.md 与车道里的 4.1 要一起改）")
    table = image_packages(rd)
    rows = [dict(probe_readme(p, table),
                 doc_line=next(x["line"] for x in apt if x["package"] == p)) for p in names]
    doc_lines = sorted({x["line"] for x in apt})
    doc_range = [doc_lines[0], doc_lines[-1]] if doc_lines else [None, None]
    return {
        "schema": 1,
        "source": {"repo": TAURI[0], "ref": TAURI[1], "path": TAURI[2],
                   "block_lines": doc_range, "families": sorted(fams)},
        "os_family": OS_FAMILY,
        "install_pm": fam.get("pm"),
        "install_line": fam.get("install_line"),
        "apt_packages": apt,
        "runner_probe": rows,
        "toolchain_from_image": toolchain(rd),
        "apt_get_line": "sudo apt-get install -y " + " ".join(names),
        "missing_per_readme": [r["package"] for r in rows if r["verdict"] == "not_listed"],
        "verdict_legend": {
            "listed": "readme 的包表里逐字有这个名字（版本照 readme 那一列，不等于适合我们）",
            "not_listed": "readme 没提到 —— 不等于机器上没装，readme 不是 dpkg -l",
        },
        "rule": {
            "why_derived": "包表必须从上游那一页读出来：手抄的那段散文在上游改名时不会红",
            "must_verify_in_ci": "apt 装完必须打印 pkg-config --modversion webkit2gtk-4.1，"
                                 "否则等于没人知道装对没",
            "image_pins": "ubuntu-latest 的 readme 是按 commit 钉的，标签本身会漂到下一代镜像",
        },
    }, problems


DRIFT_KEYS = ("apt_packages", "runner_probe", "toolchain_from_image", "os_family", "source",
              "rule", "apt_get_line", "missing_per_readme", "verdict_legend",
              "install_pm", "install_line")


def check(committed: dict, fresh: dict) -> list[str]:
    bad = []
    for k in DRIFT_KEYS:
        if committed.get(k) != fresh.get(k):
            bad.append(f"{k}: {str(committed.get(k))[:70]!r} → {str(fresh.get(k))[:70]!r}")
    return bad


def selftest() -> int:
    """离线对照：解析器与判语都得有一正一负，否则"读出来了"没有信息量。"""
    bad = []
    mdx = ('<Tabs>\n  <TabItem label="Debian">\n\n```sh\nsudo apt update\n'
           'sudo apt install libwebkit2gtk-4.1-dev \\\n'
           '  build-essential \\\n'
           '  curl\n```\n\n  </TabItem>\n'
           '  <TabItem label="Arch">\n\n```sh\nsudo pacman -S --needed \\\n'
           '  base-devel\n```\n  </TabItem>\n</Tabs>\n')
    fams = parse_families(mdx)
    got = [p["package"] for p in (fams.get("Debian") or {}).get("packages", [])]
    if got != ["libwebkit2gtk-4.1-dev", "build-essential", "curl"]:
        bad.append(f"Debian 那一族没抽全（续行的反斜杠后面还有空格）：{got}")
    if [p["package"] for p in (fams.get("Arch") or {}).get("packages", [])] != ["base-devel"]:
        bad.append(f"Arch 那一族读错了：{fams.get('Arch')}")
    if (fams.get("Debian") or {}).get("pm") != "apt":
        bad.append(f"Debian 那一族的 pm 没读对：{(fams.get('Debian') or {}).get('pm')}")
    # 反向：真页里 Windows/Linux/macOS 那几个 tab 放的是 export 与散文，不该被当成包表
    # （第一版就是把 `Most / apps / refresh / export` 读成了包，产物里冒出 10 个假家族）
    NL = chr(10)
    prose = ('<TabItem label="Windows">' + NL + '```sh' + NL
             + 'export JAVA_HOME=/opt/jbr' + NL + 'Most apps dont refresh them' + NL + '```')
    if parse_families(prose):
        bad.append("散文 tab 被当成了一族包表")
    # 反向：`sudo apt update` 那一行不是包表
    upd = ('<TabItem label="Debian">' + NL + '```sh' + NL + 'sudo apt update' + NL + '```')
    if parse_families(upd):
        bad.append("apt update 被当成了包表 —— 空表不能算「读出来了」")
    # 反向：块里没有包名时不该硬凑一族
    if parse_families('```sh\nsudo apt update\n```'):
        bad.append("只有一行 update 的块被当成了一族 —— 空表也算「读出来了」")
    # 反向：没有 TabItem 的页不该产出 Debian 族
    if "Debian" in parse_families("```sh\nsudo apt install curl\n```"):
        bad.append("没有 TabItem 也能认出 Debian 族 —— 上游改版时会拿一张空表当依据")
    rd = ("| package | version |\n|---|---|\n| curl | 8.5.0-2ubuntu10.16 |\n"
          "| libssl-dev | 3.0.13 |\n- Rust 1.99.1\n- Node.js 22.23.3\n")
    tbl = image_packages(rd)
    p1 = probe_readme("libwebkit2gtk-4.1-dev", tbl)
    p2 = probe_readme("curl", tbl)
    if p1["verdict"] != "not_listed":
        bad.append(f"缺席判语失效：{p1}")
    if p2["verdict"] != "listed" or p2["readme_version"] != "8.5.0-2ubuntu10.16":
        bad.append(f"在场判语失效（正向对照，readme 那列的版本要一起带出来）：{p2}")
    # 反向：模糊搜会造出假在场 —— 表里只有 profile 时 file 必须算没提到
    if probe_readme("file", image_packages("| profile | 1 |\n"))["verdict"] != "not_listed":
        bad.append("逐字查被写成模糊查了：profile 让 file 变成假在场")
    if "不等于机器上没装" not in json.dumps(build_legend(), ensure_ascii=False):
        bad.append("判语说明里没了「不等于没装」这句 —— not_listed 会被读成缺席证据")
    t = toolchain(rd)
    if t != {"rust": "1.99.1", "node": "22.23.3"}:
        bad.append(f"工具链版本没读出来：{t}")
    if toolchain("- Rust nothing\n")["rust"] is not None:
        bad.append("读不到版本号时返回了假值 —— 该是 None 而不是猜")
    print("---- 结论 ----  抽出 Debian 族 =", bool(fams.get("Debian")),
          "；webkit 判语 =", p1["verdict"], "；curl 判语 =", p2["verdict"])
    for x in bad:
        print("  BAD:", x)
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


def build_legend() -> dict:
    return {"not_listed": "readme 没提到 —— 不等于机器上没装，readme 不是 dpkg -l"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="install_deps.py")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--cache", default="")
    ap.add_argument("--fill", action="store_true")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    fresh, problems = build(a.cache, a.fill)
    if a.check:
        with open(a.out, encoding="utf-8") as fh:
            committed = json.load(fh)
        bad = check(committed, fresh) + problems
        for x in bad:
            print("BAD:", x)
        print(f"比对了 {len(fresh.get('apt_packages', []))} 个包，问题 {len(bad)} 条")
        return 1 if bad else 0
    for x in problems:
        print("BAD:", x)
    if problems:
        print("有 BAD → 不落盘")
        return 1
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(fresh, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"  {OS_FAMILY} 族 {len(fresh['apt_packages'])} 个包："
          f"{' '.join(p['package'] for p in fresh['apt_packages'])}")
    for r in fresh["runner_probe"]:
        ver = r["readme_version"]
        print(f"    {r['package']:32} → {r['verdict']}"
              + (f"  readme 版本 {ver}" if ver else ""))
    print("  车道那一步：", fresh["apt_get_line"])
    print("  工具链（镜像自带）:", fresh["toolchain_from_image"])
    print("落盘", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
