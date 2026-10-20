#!/usr/bin/env python3
"""sideload（用户从别处弄来的权重文件）按**内容**认领 —— 产品约束第 2 条的机器面。

LM Studio 官方支持 sideload / `lms import`；我们的理由更硬：一个权重要 6–20 分钟，
必然有人从别的机器拷过来。约束写的是"导入后仍按 sha256 认领/提示"，此前这句话**没有任何产物**。

这一层只做两件事，都是离线可证的：
  ① 把已有 pin 汇成一张 `sha256 → 身份` 的表（四个来源：对话推荐表、字幕表、生图表、OCR 两份 pin）；
  ② 给一个本地文件算 sha256（流式，不把 GB 级权重读进内存），按表给一个**有承重的**结论。

结论只有四种，每种都必须能被子翻出来（见 selftest 与 tests/test_sideload.py）：
  * `known_by_hash` —— 这颗字节我们钉过：说清它属于哪项能力、证据到哪一级；
  * `name_matches_hash_differs` —— 文件名与某颗 pin 相同但字节不同：**不许认领**。
    这不是假想：`ppocrv5_latin_dict.txt` 在上游 v0.1.0 是 1,634 B、v0.3.0 是 2,616 B，
    两个真文件都在 `tests/fixtures/sideload/` 里，一个钉着、一个没钉；
  * `unknown` —— 表里没有这颗。仍可以给 `name_shape_hint`（按文件名形态猜能力/量化），
    但 hint 带 `claim: false`，UI 不许把它当认领；
  * OCR 的 rec 与字典**必须成对**：认出一半就说清还缺哪一半（`pairing`），不含糊。

**这一层不解读文件头**：`.gguf` 的 `general.architecture` 那套由 `gguf-parser-go` 在 CI/外壳里做
（见 docs/toolbelt.md），本机既不编译也不运行，所以这里只保证"按内容寻址"这一句是真的。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
OUT = os.path.join(ROOT, "pins", "sideload-index.json")
FIXDIR = os.path.join(ROOT, "tests", "fixtures", "sideload")

SOURCES = ("pins/models.json", "pins/ocr.json", "pins/ocr-languages.json",
           "catalog/recommendations.json", "catalog/subtitle-tiers.json",
           "catalog/image-tiers.json")
CHUNK = 4 * 1024 * 1024

# 文件名形态 → 能力。只用来给提示，不用来认领。
HINTS = (
    (re.compile(r"^ggml-.*\.bin$"), "字幕"),
    (re.compile(r"\.gguf$"), "对话或生图"),
    (re.compile(r"_rec\.onnx$"), "文字识别"),
    (re.compile(r"_det\.onnx$"), "文字识别"),
    (re.compile(r"_dict\.txt$"), "文字识别"),
)
QUANT_RX = re.compile(r"[-.]((?:IQ|iq|Q|q)[0-9]{1,2}(?:_[A-Za-z0-9]+){0,2}|f16|f32)")


def sha256_of(path: str) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(CHUNK)
            if not buf:
                break
            n += len(buf)
            h.update(buf)
    return h.hexdigest(), n


def entries_from(arts: dict) -> tuple[list[dict], list[str]]:
    """把六份已钉表里"有 sha256 的每一行"摊平成身份记录。"""
    out, problems = [], []

    def add(cap, rec, source, detail=None):
        sha, name, size = rec.get("sha256"), rec.get("file"), rec.get("size") or rec.get("bytes")
        if not sha or not name:
            return
        if not size:
            problems.append(f"{source}/{name}: 有钉住的哈希却没有体积 —— 认领头不许缺尾巴")
        for prev in out:
            if prev["sha256"] == sha and prev["bytes"] != size:
                problems.append(f"{sha[:12]}: 两个来源对同一串哈希给了不同体积 "
                                f"{prev['bytes']} vs {size}（{prev['source']} vs {source}）")
        out.append({"sha256": sha, "file": name, "bytes": size, "capability": cap,
                    "repo": rec.get("repo"), "evidence": rec.get("level") or rec.get("status") or "",
                    "release": rec.get("release"), "source": source, "detail": detail or ""})

    models = arts["pins/models.json"]
    for name, rec in models.items():
        if isinstance(rec, dict):
            cap = ("字幕" if name.startswith("ggml-") else
                   "文字识别" if "_ocr" in name or name.endswith((".onnx", "_dict.txt")) else
                   "生图" if "diffusion" in name or name.startswith("stable-diffusion") else
                   "对话" if name.endswith(".gguf") else "样本/其他")
            add(cap, dict(rec, file=name), "pins/models.json",
                detail=(rec.get("provenance") or "")[:80])

    ocr = arts["pins/ocr.json"]
    trio = sorted(k for k, v in ocr.items() if isinstance(v, dict))
    for name, rec in ocr.items():
        if isinstance(rec, dict):
            add("文字识别", dict(rec, file=name), "pins/ocr.json",
                detail="默认档三颗之一，运行时还要一起给：" +
                       "、".join(x for x in trio if x != name))

    langs = arts["pins/ocr-languages.json"]
    for key, ent in langs["entries"].items():
        add("文字识别", ent["rec"], "pins/ocr-languages.json", detail=f"语言 {key}")
        add("文字识别", ent["dict"], "pins/ocr-languages.json",
            detail=f"语言 {key}；必须与 rec {ent['rec']['file']} 成对")

    recs = arts["catalog/recommendations.json"]
    for tier, rows in recs["tiers_rows"].items():
        for row in rows:
            add("对话", row, "catalog/recommendations.json", detail=f"档位 {tier}")

    sub = arts["catalog/subtitle-tiers.json"]
    for row in sub["rows"]:
        add("字幕", row, "catalog/subtitle-tiers.json",
            detail="英文专用" if row.get("english_only") else "")

    img = arts["catalog/image-tiers.json"]
    for row in img["weights"]:
        add("生图", row, "catalog/image-tiers.json", detail=f"角色 {row.get('role')}")
    return out, problems


def build(arts: dict) -> tuple[dict, list[str]]:
    entries, problems = entries_from(arts)
    by_hash: dict[str, list] = {}
    names: dict[str, list] = {}
    for e in entries:
        one = {k: e[k] for k in ("file", "bytes", "capability", "repo", "evidence",
                                 "release", "source", "detail") if e.get(k)}
        by_hash.setdefault(e["sha256"], []).append(one)
        names.setdefault(e["file"], set()).add(e["sha256"])
    dup_names = {n: sorted(h) for n, h in names.items() if len(h) > 1}
    for h, rows in by_hash.items():
        rows.sort(key=lambda r: (r["capability"], r.get("source") or ""))
    return {"schema": 1,
            "rule": {"claim": "认领只看 sha256；文件名相同而字节不同**不算**认出（上游真有同名不同内容的两颗）",
                     "hint": "name_shape_hint 是从文件名形态猜的，带 claim:false，UI 不许当认领",
                     "header": "这一层不解读 .gguf/.onnx 文件头：那由 gguf-parser-go 在 CI 或外壳里做"
                               "（本机禁止编译与运行），所以这里只保证『按内容寻址』这一句是真的",
                     "pairing": "OCR 的 rec 与 *_dict.txt 必须成对；认出一半时要说清缺哪一半"},
            "from": list(SOURCES),
            "stats": {"独立哈希": len(by_hash), "身份行": len(entries),
                      "同名不同哈希的文件名": len(dup_names)},
            "by_hash": {h: v for h, v in sorted(by_hash.items())},
            "names_with_multiple_hashes": dup_names}, problems


def load_artifacts(root: str = ROOT) -> dict:
    out = {}
    for rel in SOURCES:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            out[rel] = json.load(fh)
    return out


def check(committed: dict, fresh: dict) -> list[str]:
    bad = []
    if committed.get("rule") != fresh.get("rule"):
        bad.append("认领口径变了（什么算认出、hint 怎么用、文件头归谁）")
    if committed.get("stats") != fresh.get("stats"):
        bad.append(f"表的大小变了 {committed.get('stats')} → {fresh['stats']}"
                   "（任何一张 pin 或档位表重算过，这张合并表都要跟着重出）")
    if committed.get("names_with_multiple_hashes") != fresh.get("names_with_multiple_hashes"):
        bad.append(f"同名不同哈希那组变了 {committed.get('names_with_multiple_hashes')} → "
                   f"{fresh['names_with_multiple_hashes']}（这正是最容易骗过用户的那批名字）")
    ch, fh = committed.get("by_hash", {}), fresh.get("by_hash", {})
    only_c, only_f = sorted(set(ch) - set(fh)), sorted(set(fh) - set(ch))
    if only_c or only_f:
        bad.append(f"哈希集合变了：只在已提交里有 {only_c[:3]}、只在新表里有 {only_f[:3]}")
    for h in set(ch) & set(fh):
        if ch[h] != fh[h]:
            bad.append(f"{h[:12]}: 身份行变了 {ch[h]} → {fh[h]}")
    return bad


def view(index: dict) -> dict:
    """查表用的两个映射：`by_hash` 与 `by_name`。

    `by_name` 必须建好而不是每次扫一遍：sideload 的第二个结论（同名不同字节）
    靠的就是"这个名字我们钉过、钉的是哪串哈希"。
    """
    by_name: dict[str, set] = {}
    for h, rows in index["by_hash"].items():
        for r in rows:
            by_name.setdefault(r["file"], set()).add(h)
    return {"by_hash": index["by_hash"], "by_name": {k: sorted(v) for k, v in by_name.items()}}


def hint_for(name: str) -> dict:
    caps = [cap for rx, cap in HINTS if rx.search(name)]
    q = QUANT_RX.search(name)
    return {"capability_guess": caps[0] if caps else None,
            "quantization_in_name": q.group(1) if q else None,
            "claim": False}


def lookup(path: str, index: dict) -> dict:
    """给一个本地文件，说要还是不说 —— 说不清的时候**宁可不说**。"""
    name = os.path.basename(path)
    try:
        sha, size = sha256_of(path)
    except OSError as exc:
        return {"file": name, "verdict": "unreadable", "because": f"{type(exc).__name__}: {exc}",
                "claim": False}
    rows = index["by_hash"].get(sha)
    if rows:
        sizes = {r.get("bytes") for r in rows if r.get("bytes")}
        out = {"file": name, "sha256": sha, "bytes": size, "verdict": "known_by_hash",
               "claim": True, "entries": rows,
               "capabilities": sorted({r["capability"] for r in rows})}
        if sizes and sizes != {size}:
            out["verdict"] = "hash_known_size_differs"
            out["note"] = (f"这颗哈希钉过，但钉的体积是 {sorted(sizes)}，手上这个是 {size} —— "
                           "同一串哈希不该有两种体积，先查 pin 别先信文件")
            out["claim"] = False
        return out
    pinned = index["by_name"].get(name)
    if pinned:
        return {"file": name, "sha256": sha, "bytes": size, "claim": False,
                "verdict": "name_matches_hash_differs",
                "note": "这个名字在我们钉过的表里存在，但你手上这串字节不是它 —— 不许按那颗认领；"
                        "上游确实有同名不同内容的先例（见 tests/fixtures/sideload 那两颗）",
                "pinned_hashes_for_this_name": pinned,
                "name_shape_hint": hint_for(name)}
    return {"file": name, "sha256": sha, "bytes": size, "claim": False, "verdict": "unknown",
            "name_shape_hint": hint_for(name)}


def selftest() -> int:
    bad = []
    arts = load_artifacts()
    index, problems = build(arts)
    if problems:
        bad += problems
    if index["stats"]["同名不同哈希的文件名"] < 1:
        bad.append("这张表里一个同名不同哈希的文件名都没有 —— 那第 ② 种结论就没东西可证")
    v_index = view(index)
    if "ppocrv5_latin_dict.txt" not in v_index["by_name"]:
        bad.append("表里没有 ppocrv5_latin_dict.txt 这个名字 —— 那条负向对照失去对照对象")

    pinned = os.path.join(FIXDIR, "v0.7.0", "ppocrv6_tiny_dict.txt")
    twin_a = os.path.join(FIXDIR, "v0.3.0", "ppocrv5_latin_dict.txt")
    twin_b = os.path.join(FIXDIR, "v0.1.0", "ppocrv5_latin_dict.txt")
    for p in (pinned, twin_a, twin_b):
        if not os.path.exists(p):
            bad.append(f"夹具不在：{p}")
            return _report(bad)
    v1 = lookup(pinned, v_index)
    if v1.get("verdict") != "known_by_hash" or not v1.get("claim"):
        bad.append(f"正向对照没过：钉过的字典被判成 {v1.get('verdict')}")
    v2 = lookup(twin_a, v_index)
    if v2.get("verdict") != "known_by_hash":
        bad.append(f"v0.3.0 那颗 latin 字典是 pin 里的，却判成 {v2.get('verdict')}")
    v3 = lookup(twin_b, v_index)
    if v3.get("verdict") != "name_matches_hash_differs" or v3.get("claim"):
        bad.append(f"同名不同字节的真文件（v0.1.0）必须被拒认领，实际 {v3.get('verdict')} / claim={v3.get('claim')}")
    if v2["sha256"] == v3["sha256"]:
        bad.append("两份同名夹具字节相同 —— 那这条负向对照是空的")
    if v3["pinned_hashes_for_this_name"] != [v2["sha256"]]:
        bad.append(f"同名那颗的钉住哈希没指对：{v3['pinned_hashes_for_this_name']} vs {v2['sha256']}")
    # 成对这一句必须有出口：认出默认档里的一颗，就要说清运行时还得一起给哪几颗。
    if "还要一起给" not in json.dumps(v1, ensure_ascii=False):
        bad.append(f"认出默认档字典却没说清要配哪几颗：{v1}")
    # unknown 的提示必须带 claim:false，否则 UI 会把猜当认领。
    unknown = os.path.join(FIXDIR, "zzz-Q4_K_M.gguf")
    with open(unknown, "wb") as fh:
        fh.write(b"not a real weight")
    try:
        v4 = lookup(unknown, v_index)
    finally:
        os.remove(unknown)
    if v4["verdict"] != "unknown" or v4["name_shape_hint"]["claim"] is not False:
        bad.append(f"没钉过的文件给了认领或 hint 没标 claim:false：{v4}")
    if v4["name_shape_hint"]["capability_guess"] is None:
        bad.append("hint 连形态都没给 —— 那这一格是装饰")
    # 体积那一格必须能红：把表里某颗的 bytes 改掉，结论要变成 hash_known_size_differs。
    import copy
    tampered = copy.deepcopy(index)
    h = v1["sha256"]
    tampered["by_hash"][h][0]["bytes"] = 1
    v5 = lookup(pinned, view(tampered))
    if v5["verdict"] != "hash_known_size_differs" or v5["claim"]:
        bad.append(f"哈希对但体积不符时仍被认领：{v5['verdict']} / claim={v5['claim']}")
    print("---- 结论 ----  ", index["stats"], "；同名不同内容那组里第一颗：",
          next(iter(index["names_with_multiple_hashes"])))
    return _report(bad)


def _report(bad: list[str]) -> int:
    for x in bad:
        print("  BAD:", x)
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="sideload_lookup.py")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--build", action="store_true", help="重算并落盘这张合并表")
    ap.add_argument("--check", action="store_true", help="重算并与已提交那张比漂移")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--lookup", nargs="*", default=[], help="按内容认领这些本地文件")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    index, problems = build(load_artifacts())
    if a.check:
        with open(a.out, encoding="utf-8") as fh:
            committed = json.load(fh)
        bad = check(committed, index) + problems
        for x in bad:
            print("BAD:", x)
        print(f"比对了 {index['stats']['独立哈希']} 串哈希，问题 {len(bad)} 条")
        return 1 if bad else 0
    if a.lookup:
        rc, v_index = 0, view(index)
        for p in a.lookup:
            v = lookup(p, v_index)
            print(f"{v['file']}: {v['verdict']} 认领={v['claim']}"
                  + (f" 能力={'/'.join(v['capabilities'])}" if v.get("capabilities") else ""))
            if v["verdict"] == "unreadable":
                rc = 1
        return rc
    for x in problems:
        print("BAD:", x)
    if problems:
        print("有 BAD → 不落盘")
        return 1
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"落盘 {a.out}：{index['stats']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
