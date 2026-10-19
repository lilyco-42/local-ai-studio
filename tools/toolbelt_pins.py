"""把工具带（llmfit / llama-swap / gguf-parser）钉成 "具体 release + sha256"。

为什么单独一条而不复用 engine_probe：ADR-0001 那条"不许跟 latest 漂"的规矩对工具带同样成立，
但三家的产物命名完全不同（rust triple / go os_arch / 裸二进制），套不进引擎那张表。

哈希来源的实测差别（决定了每颗 pin 的强度，脚本自己数，不把结论写死）：
  · 三家 GitHub release 产物的 API `digest` 全都有；
  · llmfit 每个包配一个 `.sha256`、llama-swap 配一份 `checksums.txt`，
    格式都是 `<64hex>␣␣<文件名>`（`sha256sum -c` 直接吃）。sidecar 与 API digest 是
    **两套独立机制**，对得上才算高置信；
  · gguf-parser-go 没有 sidecar，只有 API digest 一套 —— 这不是失败，
    但也不能假装对照过，所以 pin 里如实记 `sidecar: null`。

用法：
    python tools/toolbelt_pins.py --live --json pins/toolbelt.json      # 立钉（写盘）
    python tools/toolbelt_pins.py --check pins/toolbelt.json            # CI：漂移就红
    python tools/toolbelt_pins.py --fixtures tests/fixtures             # 离线（夹具）
    python tools/toolbelt_pins.py --live --dump-fixtures tests/fixtures # 重新捕获夹具
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine_probe import github_api  # noqa: E402  复用同一个带 GITHUB_TOKEN 的取数口子

PINNED = [
    {
        "tool": "llmfit", "repo": "AlexsJones/llmfit", "tag": "v1.1.17",
        "pattern": r"^llmfit-(?P<ver>v\d+\.\d+\.\d+)-(?P<plat>[a-z0-9_]+(?:-[a-z0-9_]+)*)\.(?P<ext>tar\.gz|zip)$",
        "sidecar": "per-asset", "suffix": ".sha256",
        "require": ["x86_64-pc-windows-msvc", "x86_64-apple-darwin",
                    "aarch64-apple-darwin", "x86_64-unknown-linux-gnu"],
    },
    {
        "tool": "llama-swap", "repo": "mostlygeek/llama-swap", "tag": "v265",
        "pattern": r"^llama-swap_(?P<ver>\d+)_(?P<plat>[a-z0-9]+_[a-z0-9]+)\.(?P<ext>tar\.gz|zip)$",
        "sidecar": "aggregate", "suffix": None,
        "require": ["windows_amd64", "darwin_amd64", "darwin_arm64", "linux_amd64"],
    },
    {
        # 产物名里没有版本号，靠 "钉住 tag 查 releases/tags/<tag>" 来防漂
        "tool": "gguf-parser", "repo": "gpustack/gguf-parser-go", "tag": "v0.26.4",
        "pattern": r"^gguf-parser-(?P<plat>[a-z]+-[a-z0-9]+)(?P<ext>\.exe)?$",
        "sidecar": "none", "suffix": None,
        "require": ["windows-amd64", "darwin-amd64", "darwin-arm64", "linux-amd64"],
    },
]

SHA256_LINE = re.compile(r"^([0-9a-f]{64})\s+\*?(.+?)\s*$")


def parse_sidecar(text: str) -> dict[str, str]:
    """`sha256sum` 家族格式：`<hash>␣␣<name>`，二进制模式是 `<hash>␣*<name>`。"""
    out: dict[str, str] = {}
    for line in text.replace("\r\n", "\n").split("\n"):
        m = SHA256_LINE.match(line)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def digest_of(asset: dict) -> str | None:
    d = asset.get("digest") or ""
    return d if d.startswith("sha256:") and len(d) == 71 else None


def build(releases: dict[str, dict], sidecars: dict[str, str]) -> tuple[dict, list[str]]:
    """纯函数：release 原始 payload + sidecar 文本 → pin 表 + 问题清单。

    bad 非空就不能立钉。缺失、digest 没有、版本对不上、sidecar 与 API 不一致
    都要单独点名，不许静默退回"下载时再算"。
    """
    pins: dict[str, dict] = {}
    bad: list[str] = []
    for meta in PINNED:
        tool = meta["tool"]
        rel = releases.get(tool)
        if rel is None:
            bad.append(f"{tool}: 没拿到 {meta['repo']}@{meta['tag']} 的 release 数据")
            continue
        pat = re.compile(meta["pattern"])
        tagver = meta["tag"].lstrip("v")
        rows: dict[str, dict] = {}
        unnamed = 0
        for a in rel.get("assets") or []:
            g = pat.match(a["name"])
            if not g:
                unnamed += 1
                continue
            plat = g.group("plat")
            v = g.groupdict().get("ver")
            if v and v.lstrip("v") != tagver:
                bad.append(f"{tool}/{plat}: 产物名里的版本 {v} 与钉的 {meta['tag']} 不一致")
                continue
            d = digest_of(a)
            if not d:
                bad.append(f"{tool}/{plat}: 上游 digest 缺失或不是 sha256（{a.get('digest')!r}）")
                continue
            rows[plat] = {"asset": a["name"], "url": a["browser_download_url"],
                          "size": a["size"], "sha256": d.split(":", 1)[1]}
        # 上游改名不会让上面的循环报错，只会让 require 落空，所以两个都要说
        if not rows:
            bad.append(f"{tool}: 产物表里一个都没匹配上（{unnamed} 个候选全被规则否掉）→ 规则过期")
        for plat in meta["require"]:
            if plat not in rows:
                bad.append(f"{tool}: 钉在 {meta['tag']} 的产物表里没有 {plat}")
                continue
            r = dict(rows[plat], tool=tool, repo=meta["repo"], release=meta["tag"])
            r["sidecar"] = sidecar_check(meta, r, sidecars, bad)
            pins[f"{tool}/{plat}"] = r
    return pins, bad


def sidecar_check(meta: dict, r: dict, sidecars: dict[str, str], bad: list[str]) -> str | None:
    """两套机制的对照。返回 "一致" / None（上游没给第二套）；对不上就进 bad。"""
    kind = meta["sidecar"]
    if kind == "none":
        return None
    if kind == "per-asset":
        text = sidecars.get(r["asset"] + meta["suffix"])
        if text is None:
            bad.append(f"{r['tool']}/{r['asset']}: 该有 sidecar 却没取到")
            return None
        declared = parse_sidecar(text)
        if r["asset"] not in declared:
            bad.append(f"{r['tool']}/{r['asset']}: sidecar 里没有这个文件的名字")
            return None
        if declared[r["asset"]] != r["sha256"]:
            bad.append(f"{r['tool']}/{r['asset']}: sidecar {declared[r['asset']][:12]}… "
                       f"!= API digest {r['sha256'][:12]}…")
            return None
        return "一致"
    # aggregate：一份 checksums.txt 覆盖整张表，按产物名逐个对
    text = next((sidecars[n] for n in sidecars if n.endswith("_checksums.txt")), None)
    if text is None:
        bad.append(f"{r['tool']}: 该有 checksums.txt 却没取到")
        return None
    declared = parse_sidecar(text)
    if r["asset"] not in declared:
        bad.append(f"{r['tool']}/{r['asset']}: checksums.txt 里没有这个产物")
        return None
    if declared[r["asset"]] != r["sha256"]:
        bad.append(f"{r['tool']}/{r['asset']}: checksums {declared[r['asset']][:12]}… "
                   f"!= API digest {r['sha256'][:12]}…")
        return None
    return "一致"


def fetch_text(url: str) -> str | None:
    import urllib.request
    try:
        with urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "toolbelt-pins"}), timeout=60) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception:
        return None


def load_live() -> tuple[dict, dict]:
    releases, sidecars = {}, {}
    for meta in PINNED:
        releases[meta["tool"]] = github_api(f"repos/{meta['repo']}/releases/tags/{meta['tag']}")
    for meta in PINNED:
        rel = releases.get(meta["tool"]) or {}
        assets = [a["name"] for a in (rel.get("assets") or [])]
        need = []
        if meta["sidecar"] == "per-asset":
            need = [n + meta["suffix"] for n in assets if not n.endswith(meta["suffix"])]
        elif meta["sidecar"] == "aggregate":
            need = [n for n in assets if n.endswith("_checksums.txt")]
        for name in need:
            url = f"https://github.com/{meta['repo']}/releases/download/{meta['tag']}/{name}"
            text = fetch_text(url)
            if text is not None:
                sidecars[name] = text
            # 取不到就干脆不放进字典，让 sidecar_check 报"该有却没取到"，
            # 而不是存个空串把它误判成"sidecar 里没有这个文件的名字"。
            else:
                print(f"  ! sidecar 取不到：{url}")
    return releases, sidecars


def load_fixtures(d: str) -> tuple[dict, dict]:
    releases, sidecars = {}, {}
    for meta in PINNED:
        p = os.path.join(d, f"toolbelt_{meta['tool']}.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                releases[meta["tool"]] = json.load(fh)
    for p in glob.glob(os.path.join(d, "toolbelt_sidecar_*")):
        with open(p, encoding="utf-8") as fh:
            sidecars[os.path.basename(p)[len("toolbelt_sidecar_"):]] = fh.read()
    return releases, sidecars


def dump_fixtures(d: str, releases: dict, sidecars: dict) -> None:
    os.makedirs(d, exist_ok=True)
    for tool, rel in releases.items():
        with open(os.path.join(d, f"toolbelt_{tool}.json"), "w", encoding="utf-8") as fh:
            json.dump(rel, fh, ensure_ascii=False)
    for name, text in sidecars.items():
        with open(os.path.join(d, f"toolbelt_sidecar_{name}"), "w",
                  encoding="utf-8", newline="") as fh:
            fh.write(text)


def diff_against_file(pins: dict, path: str) -> list[str]:
    """CI 用的漂移判定：逐字段比，不许只比"还存不存在"。"""
    drift = []
    with open(path, encoding="utf-8") as fh:
        want = json.load(fh)
    for key, old in want.items():
        new = pins.get(key)
        if new is None:
            drift.append(f"{key}: 上游这边已经取不到（钉过 {old['release']}）")
            continue
        for f in ("release", "asset", "sha256", "size"):
            if new.get(f) != old.get(f):
                drift.append(f"{key}: {f} 钉的是 {old.get(f)!r}，上游现在是 {new.get(f)!r}")
    for key in pins:
        if key not in want:
            drift.append(f"{key}: 钉表里没有这条（新平台？要显式加进去）")
    return drift


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--live", action="store_true")
    g.add_argument("--fixtures", default="")
    ap.add_argument("--json", default="", help="把 pin 表写到这个路径")
    ap.add_argument("--check", default="", help="与已提交的钉表逐字段比对，漂移即红")
    ap.add_argument("--dump-fixtures", default="")
    ap.add_argument("--print-urls", action="store_true",
                    help="按 key|url|size 输出钉住的产物地址，给国内出口探针当输入（不手抄 URL）")
    args = ap.parse_args(argv)

    if args.fixtures:
        releases, sidecars = load_fixtures(args.fixtures)
        src = f"夹具 {args.fixtures}"
    else:
        releases, sidecars = load_live()
        src = "GitHub API 实时"
    pins, bad = build(releases, sidecars)
    if args.dump_fixtures:
        dump_fixtures(args.dump_fixtures, releases, sidecars)

    if args.print_urls:
        # 机读模式：只输出 key|url|size，给 tools/gh_release_reach.sh 当输入
        for key in sorted(pins):
            print("%s|%s|%s" % (key, pins[key]["url"], pins[key]["size"]))
        for b in bad:
            print("BAD " + b, file=sys.stderr)
        return 1 if bad else 0

    n_two = sum(1 for v in pins.values() if v["sidecar"] == "一致")
    print(f"来源：{src} · 立住 {len(pins)} 条，其中两套机制对照一致 {n_two} 条，"
          f"只有 API digest {len(pins) - n_two} 条")
    for key in sorted(pins):
        v = pins[key]
        print(f"  {key:34} {v['release']:9} {v['asset'][:44]:44} "
              f"{v['size']:>11,} B  {v['sha256'][:16]}…  sidecar={v['sidecar'] or '无'}")
    for b in bad:
        print(f"  ✗ {b}")

    if args.check:
        drift = diff_against_file(pins, args.check)
        print(f"漂移 {len(drift)} 条（对照 {args.check}）")
        for d in drift:
            print(f"  ! {d}")
        if drift:
            for line in drift:
                print(f"::warning::{line}")
            return 1
    if args.json:
        if bad:
            # 半张钉表写盘比不写更糟：CI 会照着它去下载，而缺的那几条从此没人知道。
            print(f"有 {len(bad)} 个问题 → 不写 {args.json}（立不住就不立）")
            return 1
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(pins, fh, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"写入 {args.json}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
