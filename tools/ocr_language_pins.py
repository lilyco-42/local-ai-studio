#!/usr/bin/env python3
"""把 OCR 的"识别能力"那一轴（14 种语言）立成**下载前就能校验**的 pin。

为什么单独一个工具：`docs/i18n.md` 原先只说这些语言包"可以按 sha256 钉"，依据是上游
models.md 里"release 产物都带 digest"那句概括。这一轮逐条量完，量出四个概括盖住的事实，
其中一个直接改变了 pin 的强度，另一个直接改变了查找方式：

  1. 语言权重分散在**四个老 release**（v0.1.0 / v0.3.0 / v0.6.0 / v0.7.0）——
     v0.4.0 一直到 v0.9.2 的 release **产物数是 0**。"最新版 release 里应该有它"这种推断会直接落空。
  2. **同一个文件名在不同 tag 里可以是两个不同文件**：112 个产物名里有 7 个跨 tag 重名，
     其中 2 个内容不同（`ppocrv5_latin_dict.txt` 在 v0.1.0 是 1,634 B / `3c0a8a79…`，
     在 v0.3.0 是 2,616 B / `ccbcc457…`；`uvdoc.onnx` 同理）。所以查找必须**按 models.md
     那条链接里的 tag 定位**，按名字在"全部产物"里找第一个命中会钉到另一份文件上。
     （第一版就是这么写的，跑 --live 时立刻报出"来源哈希不一致"，才把这条抓出来。）
  3. 上游有第二个分发方：`oar-ocr-core/src/core/download/registry.rs` 是 name → sha256+size
     的登记表，文件头明写这些对象镜像在 **ModelScope 的 `greatv/oar-ocr`**。实测 ModelScope
     有 101 个文件，GitHub 产物里 94 个两边都在，且两家给的 sha256 逐字符相等
     → 语言权重不是"只有一家给哈希"的 3 级，而是**两家分发方元数据一致**的 4.5 级
     （见 model-catalog.md 的阶梯；仍然是同一个作者，不是第 6 级）。
  4. v3 时代三个语言（繁中 / 日文 / 格鲁吉亚）的字典**两家都没有**：models.md 明写它们
     "不在 auto-download 注册表内，要从 PaddleOCR 取再把本地路径传给 rec builder"，链接指着
     `blob/main/…`（可变分支）。那三份只能"解析成一个 commit + 取 raw 自算 sha256"。

配对按**文件名**而不是表里的英文语言名：rec 表写 `Chinese Traditional`，字典表写
`Traditional Chinese` —— 同一个语言两种词序，按人话标签配对恰好在最需要它的那三个 v3 语言上断掉。
文件名两边都是 `chinese_cht_…` / `chinese_cht_dict.txt`。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
FIX = os.path.join(REPO, "tests", "fixtures")
sys.path.insert(0, HERE)
import file_index  # noqa: E402  （复用那个已经记了"请求主机≠回答提供方"的 ModelScope 取表函数）

OWNER_REPO = "GreatV/oar-ocr"
MD_REF = "84817f58"                      # 与 OCR 车道同一个 commit
MS_REPO = "greatv/oar-ocr"               # 上游自己的 ModelScope 镜像
MS_REV = "master"
PADDLE = "PaddlePaddle/PaddleOCR"
REGISTRY_PATH = "oar-ocr-core/src/core/download/registry.rs"
MIB = 1048576
RELEASE_PREFIX = f"https://github.com/{OWNER_REPO}/releases/"
ASSET_URL = re.compile(r"/releases/download/([^/]+)/(.+)$")
SECTIONS = {
    "rec": "### Language-Specific Models",
    "det": "## Text Detection Models",
    "dict_v5": "### PP-OCRv5 Language-Specific Dictionaries",
    "dict_v3": "### PP-OCRv3 and PP-OCRv4 Language-Specific Dictionaries",
}
LINK = re.compile(r"\[`([^`]+)`\]\((https?://[^)]+)\)")
REC_NAME = re.compile(r"^(?P<key>[a-z_]+?)_pp-ocrv(?P<ver>\d)_(?P<kind>mobile|server)_rec\.onnx$")
MIB_CELL = re.compile(r"([0-9]+(?:\.[0-9]+)?) MiB")
ENTRY = re.compile(r'Entry \{ name: "([^"]+)", sha256: "([0-9a-f]{64})", size: (\d+) \}')


def fetch(url: str, timeout: int = 90) -> bytes:
    """curl 而不是 urllib：这台机器上两者的代理行为不一样，而 `--fail` 让"取不到"是退出码，
    不是 200 + 一张错误页。"""
    p = subprocess.run(["curl", "-sSL", "--fail", "--max-time", str(timeout),
                        "-A", "ocr-language-pins", url], capture_output=True)
    if p.returncode != 0:
        raise SystemExit(f"取不到 {url}：curl 退出码 {p.returncode}，{p.stderr[:140]!r}")
    return p.stdout


def section(md: str, name: str) -> str:
    """取某个标题到下一个标题之前的正文。标题不存在就当场抛 ——
    上游改章节名时必须红，而不是让后面的解析悄悄拿到空集合。"""
    i = md.find(name)
    if i < 0:
        raise SystemExit(f"上游 models.md 里没有这一段：{name!r}（章节被改名或删了）")
    rest = md[i + len(name):]
    m = re.search(r"^#{2,3} ", rest, re.M)
    return rest[: m.start()] if m else rest


def table_rows(text: str) -> list[dict]:
    """表格行 → {file, url, tag, cells, mib}。只收**带产物链接**的行，表头与说明行自然被滤掉。
    tag 从链接本身取：文件名在多个 release 里可以指向不同内容。"""
    out = []
    for line in text.split("\n"):
        m = LINK.search(line)
        if not m or not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        size = MIB_CELL.search(line)
        u = m.group(2)
        tm = ASSET_URL.search(u)
        out.append({"file": m.group(1), "url": u,
                    "tag": tm.group(1) if tm else None,
                    "cells": cells,
                    "mib": float(size.group(1)) if size else None,
                    "language": cells[1] if len(cells) > 1 else ""})
    return out


def parse_registry(rs: str) -> dict[str, dict]:
    """registry.rs 的 Entry 表。顺带查它是不是真按名字排序 ——
    文件头写着"排序是为了二分查找"，上游插条目没排序时它自己的 find 就会查不到，
    这条判据是在替上游守住它声明的不变量，不是我们想当然的整洁要求。"""
    out: dict[str, dict] = {}
    names: list[str] = []
    for m in ENTRY.finditer(rs):
        name, sha, size = m.group(1), m.group(2), int(m.group(3))
        out[name] = {"sha256": sha, "size": size}
        names.append(name)
    if not names:
        raise SystemExit("registry.rs 里一个 Entry 都没解析出来（格式变了）")
    bad = [f"{names[i-1]} → {names[i]}" for i in range(1, len(names)) if names[i] < names[i - 1]]
    if bad:
        raise SystemExit(f"registry.rs 不再按名字排序，上游声明的二分查找前提破了：{bad[:4]}")
    return out


def latest_recs(md: str) -> dict[str, dict]:
    """每种语言取版本号最大的 rec；语言 key 从文件名推，不依赖表里的英文词序。"""
    best: dict[str, dict] = {}
    for r in table_rows(section(md, SECTIONS["rec"])):
        m = REC_NAME.match(r["file"])
        if not m:
            continue
        key, ver = m.group("key"), int(m.group("ver"))
        r["key"], r["version"] = key, ver
        cur = best.get(key)
        if cur is None or ver > cur["version"]:
            best[key] = r
    return best


def dict_index(md: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in ("dict_v5", "dict_v3"):
        for r in table_rows(section(md, SECTIONS[name])):
            out[r["file"]] = r
    return out


def dict_file_for(key: str, ver: int) -> str:
    """v5+ → `ppocrv5_<key>_dict.txt`；v3/v4 → `<key>_dict.txt`。
    两条都是 models.md 两张字典表里逐行写着的形式，不是命名猜测。"""
    return f"ppocrv5_{key}_dict.txt" if ver >= 5 else f"{key}_dict.txt"


def overlay(row: dict, gh: dict, ms: dict, reg: dict) -> tuple[dict | None, list[str]]:
    """三家来源叠一个文件。返回 ({sha256,size,sources,level,…}, 问题)。

    规则只有一条：**在场来源给的哈希与体积必须完全一致**，不一致就不给 pin（宁缺毋假）。
    体积不等尤其危险 —— 那通常是同名不同物（见模块头的第 2 条）。
    """
    name, tag = row["file"], row.get("tag")
    by_tag = gh.get(name) or {}
    if tag is not None and tag not in by_tag:
        return None, [f"{name}: models.md 指的 tag {tag} 里没有这个产物（上游挪过 release）"]
    # 按 tag 定位；tag 未知时（不是 release 链接）退回按名字，但必须唯一
    hits = [by_tag[tag]] if tag is not None else sorted(by_tag.values(), key=lambda a: a["size"])
    if not hits:
        return None, [f"{name}: GitHub 的 release 产物表里没有它"]
    if len(hits) > 1:
        return None, [f"{name}: 没给出 tag，而它在多个 release 里都有且内容不同"]
    want = hits[0]
    srcs = ["gh"]
    hashes = [want["sha256"]] if want.get("sha256") else []
    sizes = [want.get("size")]
    if not hashes:
        return None, [f"{name}: GitHub 的 digest 字段是空的（上游哪天不发哈希了）"]
    if name in ms:
        srcs.append("ms")
        if ms[name].get("sha256"):
            hashes.append(ms[name]["sha256"])
        sizes.append(ms[name].get("size"))
    if name in reg:
        srcs.append("reg")
        hashes.append(reg[name]["sha256"])
        sizes.append(reg[name]["size"])
    uniq_h = sorted(set(hashes))
    if len(uniq_h) > 1:
        return None, [f"{name}: 在场来源哈希不一致 {uniq_h}（{srcs}）—— 不立 pin"]
    uniq_s = sorted({s for s in sizes if s is not None})
    if len(uniq_s) > 1:
        return None, [f"{name}: 在场来源体积不一致 {uniq_s}（{srcs}）"]
    two = "ms" in srcs
    other = {t: a for t, a in by_tag.items() if t != tag}
    pin = {"sha256": uniq_h[0], "size": uniq_s[0] if uniq_s else None,
           "sources": srcs, "level": "4.5" if two else "3",
           "note": ("GitHub release 的 digest 与 ModelScope 元数据 Sha256 相等"
                    if two else "只有一家给哈希" + ("（registry.rs 也写了同一个值）" if "reg" in srcs else ""))}
    if other:
        pin["same_name_in_other_tags"] = {t: a["sha256"] for t, a in other.items()}
    return pin, []


def shared_det(md: str) -> dict:
    """共享检测权重取 models.md 的 PP-OCRv5 Mobile 那一行。上游有只差一个连字符的成对产物
    （`pp-ocrv5_mobile_det` / `ppocrv5_mobile_det`），所以选择必须由表来定。"""
    rows5 = [r for r in table_rows(section(md, SECTIONS["det"]))
             if r["cells"] and r["cells"][0].startswith("PP-OCRv5") and "Mobile" in r["cells"][1]]
    if not rows5:
        raise SystemExit("det 表里没有 PP-OCRv5 Mobile 那一行，共享检测权重定不下来")
    return rows5[0]


def build(md: str, gh: dict, ms: dict, reg: dict) -> tuple[dict, list[str], dict[str, dict]]:
    problems: list[str] = []
    recs = latest_recs(md)
    dicts = dict_index(md)
    det = shared_det(md)
    entries: dict[str, dict] = {}
    external: dict[str, dict] = {}

    for key, r in sorted(recs.items()):
        pin, bad = overlay(r, gh, ms, reg)
        problems += bad
        if pin is None:
            problems.append(f"{key}: rec {r['file']} 定不下来，整个语言轴缺一角")
            continue
        if r["mib"] is not None and pin["size"] is not None and \
                abs(pin["size"] / MIB - r["mib"]) > 0.05:
            problems.append(f"{key}: models.md 写 {r['mib']} MiB，来源表实为 {pin['size'] / MIB:.3f} MiB")
        dfile = dict_file_for(key, r["version"])
        drow = dicts.get(dfile)
        if drow is None:
            problems.append(f"{key}: 两张字典表里都没有 {dfile} —— rec 与 dict 必须成对")
            continue
        # "三家都没有"只有在**上游说这该是它自己的 release 产物**时才算缺陷。
        # 表里那条链接指向 PaddleOCR 的 blob 时，缺席是设计如此（models.md 明写不在注册表内），
        # 报成 BAD 会把"三个 v3 语言的真实缺口"与"本来就不在"混成同一种红。
        is_release = drow["url"].startswith(RELEASE_PREFIX)
        dpin, dbad = overlay(drow, gh, ms, reg) if is_release else (None, [])
        problems += dbad
        e = {"language": r["language"], "version": r["version"],
             "rec": dict(pin, file=r["file"], url=r["url"], release=r["tag"]),
             "det": det["file"]}
        if dpin:
            e["dict"] = dict(dpin, file=dfile, url=drow["url"], release=drow["tag"])
        else:
            e["dict"] = {"file": dfile, "url": drow["url"], "sha256": None, "size": None,
                         "sources": [], "level": "自算 + 钉 commit",
                         "note": "两家都没有；models.md 明写不在 auto-download 注册表内，"
                                 "链接指着可变分支"}
            external[dfile] = drow
        entries[key] = e

    det_pin, dbad = overlay(det, gh, ms, reg)
    problems += dbad
    per = sum(e["rec"]["size"] + (e["dict"].get("size") or 0) for e in entries.values())
    levels: dict[str, int] = {}
    for e in entries.values():
        for kind in ("rec", "dict"):
            lv = str(e[kind].get("level"))
            levels[lv] = levels.get(lv, 0) + 1
    pins = {"schema": 3, "from_repo": OWNER_REPO, "md_ref": MD_REF,
            "ms_repo": MS_REPO, "registry_path": REGISTRY_PATH,
            "levels": {
                "4.5": "GitHub release 的 digest == ModelScope 元数据 Sha256（两家分发方，零下载）；"
                       "仍然是同一个作者，所以不是第 6 级",
                "3": "只有一家给哈希（registry.rs 若在场也只是同一作者的第三个副本，不算独立来源）",
                "自算 + 钉 commit": "两家都没有：按 commit 取 raw 整文件自算 sha256"},
            "lookup": "按 models.md 链接里的 tag 定位产物 —— 同名文件在不同 tag 可以是不同内容",
            "pairing": "语言 key 取自 rec 文件名（rec 表与字典表的英文词序不一样）",
            "languages": len(entries), "level_counts": levels,
            "rec_dict_bytes": per, "rec_dict_mib": round(per / MIB, 1),
            "shared_det": (dict(det_pin, file=det["file"], url=det["url"], release=det["tag"])
                           if det_pin else None),
            "entries": entries}
    if pins["shared_det"]:
        pins["with_det_mib"] = round((per + pins["shared_det"]["size"]) / MIB, 1)
    return pins, problems, external


def resolve_external(external: dict[str, dict], pins: dict) -> list[str]:
    """把可变分支上的字典钉成 commit + 自算 sha256（几 KB 级下载；不下载就只能标"未验证"）。"""
    if not external:
        return []
    sha = json.loads(fetch(f"https://api.github.com/repos/{PADDLE}/commits/HEAD").decode(
        "utf-8", "replace")).get("sha")
    if not sha:
        return ["PaddleOCR 的 HEAD 解析不出 commit：返回的 JSON 没有 sha 键"]
    problems = []
    for f, row in sorted(external.items()):
        raw = re.sub(r"/blob/(main|master)/", f"/{sha}/",
                     row["url"].replace("github.com/", "raw.githubusercontent.com/"), count=1)
        body = fetch(raw)
        users = [e for e in pins["entries"].values() if e["dict"]["file"] == f]
        if not users:
            problems.append(f"{f}: 算出哈希了但没有语言在用它（配对逻辑漂了）")
            continue
        for e in users:
            e["dict"].update(sha256=hashlib.sha256(body).hexdigest(), size=len(body),
                             sources=["self-hash"], pinned_commit=sha, resolved_url=raw)
    return problems


def recompute(pins: dict) -> None:
    """聚合值必须在**所有**字段都补齐之后重算。

    这条是被自己的数据抓出来的：`build()` 先算了一次 per，外部字典的体积要等
    `resolve_external()` 下载完才知道，于是落盘的 `rec_dict_bytes` 比逐条相加少了 51,227 B
    —— 差 0.05 MiB，四舍五入进 117.5 里，任何"看总数对不对"的人工检查都发现不了。
    """
    per = sum(e["rec"]["size"] + (e["dict"].get("size") or 0) for e in pins["entries"].values())
    levels: dict[str, int] = {}
    for e in pins["entries"].values():
        for kind in ("rec", "dict"):
            lv = str(e[kind].get("level"))
            levels[lv] = levels.get(lv, 0) + 1
    pins["rec_dict_bytes"] = per
    pins["rec_dict_mib"] = round(per / MIB, 1)
    pins["level_counts"] = levels
    pins["with_det_mib"] = (round((per + pins["shared_det"]["size"]) / MIB, 1)
                            if pins.get("shared_det") else None)


def check(pins: dict, gh: dict, ms: dict, reg: dict) -> list[str]:
    """漂移检查：已钉的每条还在不在、三家给的哈希与体积还等不等于钉值。"""
    bad = []

    def one(ent, where):
        if not ent.get("file"):
            # 缺文件名的 pin 会让消费方只能去解析 URL，而 check 若在这里 KeyError 崩掉，
            # 红字看起来像"判据坏了"，不像"pin 少了一个字段"。
            bad.append(f"{where}: pin 里没有 file 字段（键 = {sorted(ent)}）")
            return
        row = {"file": ent["file"], "url": ent.get("url", ""), "tag": ent.get("release")}
        pin, _ = overlay(row, gh, ms, reg)
        if pin is None:
            bad.append(f"{where}: {ent['file']} 现在定不下来（来源缺了或互相冲突）")
            return
        if pin["sha256"] != ent["sha256"]:
            bad.append(f"{where}: {ent['file']} 的哈希变了（钉 {str(ent['sha256'])[:12]}…，"
                       f"现在 {str(pin['sha256'])[:12]}…）")
        if pin["size"] != ent["size"]:
            bad.append(f"{where}: {ent['file']} 的体积变了（{ent['size']} → {pin['size']}）")
        if pin["level"] != ent.get("level"):
            bad.append(f"{where}: {ent['file']} 的来源层级从 {ent.get('level')} 变成 {pin['level']}"
                       f"（多了一家或少了一家，要显式确认）")

    for key, e in pins["entries"].items():
        for kind in ("rec", "dict"):
            ent = e.get(kind) or {}
            if not ent.get("file"):
                bad.append(f"{key}: 没有 {kind}（表被改坏了）")
                continue
            if ent.get("level") not in ("3", "4.5"):
                continue                      # 外部字典的漂移由 commit 那条管
            one(ent, f"{key}/{kind}")
    if pins.get("shared_det"):
        one(pins["shared_det"], "shared_det")
    return bad


def load_fixtures() -> tuple[str, dict, dict, dict]:
    md = open(os.path.join(FIX, "ocr_models_md.md"), encoding="utf-8").read()
    gh = json.load(open(os.path.join(FIX, "ocr_release_assets.json"), encoding="utf-8"))
    ms = json.load(open(os.path.join(FIX, "ocr_modelscope_files.json"), encoding="utf-8"))
    reg = parse_registry(open(os.path.join(FIX, "ocr_registry.rs"), encoding="utf-8").read())
    return md, gh, ms, reg


def load_live() -> tuple[str, dict, dict, dict]:
    md = fetch(f"https://raw.githubusercontent.com/{OWNER_REPO}/{MD_REF}/docs/models.md").decode(
        "utf-8", "replace")
    reg = parse_registry(fetch(
        f"https://raw.githubusercontent.com/{OWNER_REPO}/{MD_REF}/{REGISTRY_PATH}").decode(
        "utf-8", "replace"))
    gh: dict[str, dict] = {}
    for rel in json.loads(fetch(f"https://api.github.com/repos/{OWNER_REPO}/releases?per_page=100")
                          .decode("utf-8", "replace")):
        for x in rel.get("assets") or []:
            dg = x.get("digest") or ""
            gh.setdefault(x["name"], {})[rel["tag_name"]] = {
                "size": x["size"],
                "sha256": dg.split(":", 1)[1] if dg.startswith("sha256:") else None}
    ms, answered, err = file_index.ms_files(MS_REPO, MS_REV)
    if err:
        raise SystemExit(f"ModelScope 取不到 {MS_REPO}：{err}（answered_by={answered}）")
    return md, gh, ms, reg


def augment_json_pins(path: str, gh: dict, ms: dict, reg: dict,
                      write: bool = False) -> tuple[list[dict], list[str]]:
    """给一张既有的 pin 表（`{"文件名": {...sha256, size...}}`）补 `meta_hashes`，
    顺便回答"这条到底是 3 级还是 4.5 级"。

    只允许**追加**：原有条目的每个键、每个值、以及顶层键集合都必须一字不变，
    否则拒写。理由很具体 —— 这张表是车道的输入，一次"顺手重写"把 `bytes_verified`
    弄丢，车道不会红，只会从此不再复核那件事。
    """
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        return [], [f"{path}: 顶层不是对象（这张工具只认 文件名→条目 的形状）"]
    rows, problems, new_doc = [], [], {}
    for name, ent in doc.items():
        if not isinstance(ent, dict) or not isinstance(ent.get("sha256"), str):
            problems.append(f"{name}: 条目里没有 sha256 字符串，跳过（不猜它是哪颗）")
            continue
        url = ent.get("primary") or (ent.get("bytes_verified") or {}).get("url") or ""
        m = ASSET_URL.search(url)
        row = {"file": name, "url": url, "tag": m.group(1) if m else ent.get("release")}
        pin, bad = overlay(row, gh, ms, reg)
        problems += [f"{name}: {b}" for b in bad]
        if pin is None:
            continue
        if pin["sha256"] != ent["sha256"]:
            problems.append(f"{name}: 钉的是 {ent['sha256'][:12]}…，两家元数据现在给 "
                            f"{pin['sha256'][:12]}… —— 先查上游换没换文件，再谈改 pin")
            continue
        if pin["size"] != ent.get("size"):
            problems.append(f"{name}: 体积不符（表里 {ent.get('size')}，现在 {pin['size']}）")
            continue
        added = {
            "meta_hashes": {
                "sources": pin["sources"], "sha256": pin["sha256"], "size": pin["size"],
                "level": pin["level"], "note": pin["note"], "as_of": "2026-10-20",
                "release": row["tag"]},
        }
        rows.append({"file": name, "level": pin["level"], "sources": pin["sources"]})
        if write:
            merged = dict(ent)
            merged.update(added)
            new_doc[name] = merged
        else:
            new_doc[name] = ent
    if write:
        problems += shape_guard(doc, new_doc)
        if not problems:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(new_doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
                fh.write("\n")
    return rows, problems


def shape_guard(old: dict, new: dict) -> list[str]:
    """追加写入的守门：顶层键集合不变、每个条目只多不少、原值一个都不许动。"""
    bad = []
    if set(old) != set(new):
        bad.append(f"顶层键集合变了：少了 {sorted(set(old) - set(new))}、"
                   f"多了 {sorted(set(new) - set(old))}")
        return bad
    for k, v in old.items():
        nv = new[k]
        if not isinstance(v, dict):
            if nv != v:
                bad.append(f"{k}: 标量值被改写")
            continue
        lost = sorted(set(v) - set(nv))
        changed = sorted(x for x in set(v) & set(nv) if v[x] != nv[x])
        if lost:
            bad.append(f"{k}: 丢了键 {lost}")
        if changed:
            bad.append(f"{k}: 改了既有键 {changed}（这个工具只许追加 meta_hashes）")
    return bad


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="ocr_language_pins.py")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--live", action="store_true", help="取上游四份表（CI 用这条）")
    g.add_argument("--fixtures", action="store_true",
                   help="用仓库夹具离线跑；外部字典只查形状、不自算哈希")
    ap.add_argument("--pins", default=os.path.join(REPO, "pins", "ocr-languages.json"))
    ap.add_argument("--pins-out", default="", help="落盘路径（默认 --pins；只有无 BAD 才写）")
    ap.add_argument("--check", action="store_true", help="与已落盘的 pin 比漂移，而不是重立一张表")
    ap.add_argument("--check-pins", default="",
                    help="只核对另一张 pin 表（如 pins/ocr.json）的哈希与两家元数据是否还一致")
    ap.add_argument("--augment-pins", default="",
                    help="给那张表**追加** meta_hashes（只追加，原值动一下就拒写）")
    a = ap.parse_args(argv)

    md, gh, ms, reg = load_live() if a.live else load_fixtures()

    if a.check_pins or a.augment_pins:
        target = a.augment_pins or a.check_pins
        rows, problems = augment_json_pins(target, gh, ms, reg, write=bool(a.augment_pins))
        for r in rows:
            print(f"  {r['file']:<32} 级 {r['level']:<4} 来源 {'+'.join(r['sources'])}")
        for x in problems:
            print("BAD:", x)
        verb = "追加 meta_hashes" if a.augment_pins else "核对"
        print(f"{verb} {target}：{len(rows)} 条对上，问题 {len(problems)} 条")
        return 1 if problems else 0

    if a.check:
        pins = json.load(open(a.pins, encoding="utf-8"))
        bad = check(pins, gh, ms, reg)
        for x in bad:
            print("BAD:", x)
        print(f"复核 {len(pins['entries'])} 种语言 + 1 个共享 det，问题 {len(bad)} 条")
        return 1 if bad else 0

    pins, problems, external = build(md, gh, ms, reg)
    if external and a.live:
        problems += resolve_external(external, pins)
        recompute(pins)
    print(f"语言 {pins['languages']} 种；rec+dict {pins['rec_dict_mib']} MiB；"
          f"加共享 det {pins.get('with_det_mib', '（det 没定下来）')} MiB")
    print("各层级条目数 =", pins["level_counts"])
    if external:
        print("两家都没有、要自算的字典 =", ", ".join(sorted(external)))
    collided = {k: e["rec"].get("same_name_in_other_tags") for k, e in pins["entries"].items()
                if e["rec"].get("same_name_in_other_tags")}
    if collided:
        print("同名但别的 tag 里还有另一份（已按 models.md 的 tag 定位）=",
              ", ".join(sorted(collided)))
    for x in problems:
        print("BAD:", x)
    incomplete = sorted(f"{k}/{kind}" for k, e in pins["entries"].items()
                        for kind in ("rec", "dict")
                        if not e[kind].get("sha256") or e[kind].get("size") is None)
    if pins["shared_det"] is None:
        incomplete.append("shared_det")
    if incomplete:
        print("BAD：这些条目还缺哈希或体积，不落盘（--fixtures 只查形状，立全表要 --live）：",
              ", ".join(incomplete))
        return 1
    summed = sum(e["rec"]["size"] + (e["dict"].get("size") or 0) for e in pins["entries"].values())
    if summed != pins["rec_dict_bytes"]:
        print(f"BAD：聚合值与逐条相加不符（表里 {pins['rec_dict_bytes']}，逐条 {summed}）—— "
              f"外部字典的体积是在 resolve 之后才知道的，忘了 recompute")
        return 1
    print("BAD 数量 =", len(problems))
    if problems:
        return 1
    out = a.pins_out or a.pins
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(pins, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
    print("落盘", out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
