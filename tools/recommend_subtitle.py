#!/usr/bin/env python3
"""按内存档位给字幕生成挑 whisper 模型 —— 每个数字都有上游出处，一个都不拍。

为什么单独立一个工具：`catalog/recommendations.json` 那张对话表靠的是 llmfit 的
`recommended_ram_gb` 拟合值，而 **llmfit 里 ASR 只有 5 条**（OCR / 生图 0 条，已实测），
字幕这一档根本没有拟合输入可用。好在 whisper.cpp 上游自己把该给的都写了：

  · `README.md` 的 `## Memory usage` 表：`Model | Disk | Mem` —— `Mem` 就是峰值内存，
    只给未量化的五个族（tiny ~273 MB / base ~388 MB / small ~852 MB / medium ~2.1 GB / large ~3.9 GB）；
  · `models/README.md` 的 `Model | Disk | SHA` 表：17 个名字的磁盘大小 + 40 位十六进制 **git blob sha1**
    （注意：那一列不是 sha256，不能当内容哈希用，只能当"上游自己认这个名字"的旁证）；
  · 同文件明写 **"Models are multilingual unless the model name includes `.en`"**，
    `-q5_0` 是量化、`-tdrz` 带说话人分离；
  · 同文件还明写 `for-tests-` 前缀的模型文件**是空的**，只给 CI 用 —— 按"仓库里所有 .bin"
    收集就会把空壳当权重发出去，这里按名字规则排除；
  · `models/download-ggml-model.sh` 里有一份 `models="..."` 白名单，还有真正的下载源
    （`src=` / `pfx=`，本工具从脚本里读，不硬编码域名）。

内容哈希只有一个来源可核：模型宿主仓库 `ggerganov/whisper.cpp` 的 `tree.lfs.oid`（实测
`ggml-tiny.bin` 的 oid 与我们已钉的 `be07e048…` 逐字符相等）。ModelScope 对这个仓库返回
**0 个文件**（实测），所以字幕这一整轴只能到"单家声明"级 —— 每条都如实标
`usable_before_download=false`，UI 必须说"下完自己算一遍"，不许假装是两家确认。

选择规则（都写进产物 `rule`，UI 说得出为什么推荐它）：
  1. 档位用上游的 `Mem` 列，不用我拍的换算：`Mem <= 档位` 才算塞得进；
     量化档上游没给内存数字 → `fit=unknown`，只列进候选表、不进档位推荐（宁缺毋滥）；
  2. 同等合档时**优先多语言档**（产品要 i18n，`.en` 只有英文是上游明写的），
     再按上游 `Mem` 从大到小 —— 这个顺序是上游表格自己的顺序，不是我加的；
  3. `large-v3-turbo` 这种 1.5 GiB 的新档上游没给 `Mem`，所以它落在 `fit=unknown`，
     这是**事实**而不是我的偏好：想把它当默认档，得先由有人给出可核对的数字。
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
import file_index  # noqa: E402

REF = "b5454"                       # 与字幕车道钉的同一个 release tag
ENGINE_REPO = "ggml-org/whisper.cpp"
TIERS = (("4GB", 4.0), ("8GB", 8.0), ("16GB", 16.0))
UNIT = {"KiB": 1024.0, "MiB": 1024.0 ** 2, "GiB": 1024.0 ** 3,
        "KB": 1e3, "MB": 1e6, "GB": 1e9}
QUANTS = ("q5_0", "q5_1", "q8_0")
FAMILIES = ("tiny", "base", "small", "medium", "large")
# 默认档要留余量：4 GB 的机器跑"峰值 ~3.9 GB"的上游大档算术上塞得进、实际会把系统挤死。
# 这个 0.5 是我们的规则（与对话表同一个数），所以它进产物 rule 字段，UI 要说得出；
# 而"塞得进吗"那一列（fits）只用上游数字做纯算术，两件事分开给。
HEADROOM = 0.5


def to_bytes(num: float, unit: str) -> float:
    return num * UNIT[unit]


def human(n: float | None) -> str:
    if n is None:
        return "-"
    return "%.1f GiB" % (n / UNIT["GiB"]) if n >= UNIT["GiB"] else "%.0f MiB" % (n / UNIT["MiB"])


def parse_mem_table(readme: str) -> tuple[dict[str, dict], list[str]]:
    """`README.md` 的 `## Memory usage`：族 → {disk 原文/字节, mem 原文/字节, 行号}。"""
    out, problems = {}, []
    lines = readme.split("\n")
    for i, line in enumerate(lines, 1):
        m = re.match(r"^\|\s*([A-Za-z][\w.]*)\s*\|\s*([\d.]+)\s*(KiB|MiB|GiB|KB|MB|GB)\s*\|"
                     r"\s*~?([\d.]+)\s*(KiB|MiB|GiB|KB|MB|GB)\s*\|$", line)
        if not m:
            continue
        fam = m.group(1).lower()
        if fam not in FAMILIES:
            continue
        row = {"disk_text": f"{m.group(2)} {m.group(3)}", "disk_bytes": to_bytes(float(m.group(2)), m.group(3)),
               "mem_text": f"{m.group(4)} {m.group(5)}", "mem_bytes": to_bytes(float(m.group(4)), m.group(5)),
               "line": i}
        if fam in out:
            problems.append(f"README 内存表里 {fam} 出现两次（:{out[fam]['line']} 和 :{i}），表格漂了")
        out[fam] = row
    if len(out) != len(FAMILIES):
        problems.append(f"内存表只认到 {sorted(out)}，应有 {list(FAMILIES)} —— 上游改了表头或单位写法")
    return out, problems


def parse_model_table(models_readme: str) -> tuple[dict[str, dict], list[str]]:
    """`models/README.md` 的 `Model | Disk | SHA`：名字 → {disk 字节, sha1, 行号}。"""
    out, problems = {}, []
    for i, line in enumerate(models_readme.split("\n"), 1):
        m = re.match(r"^\|\s*([A-Za-z0-9._-]+)\s*\|\s*([\d.]+)\s*(KiB|MiB|GiB|KB|MB|GB)\s*\|"
                     r"\s*`([0-9a-f]{40})`\s*\|$", line)
        if not m:
            continue
        name = m.group(1)
        if name.lower() in ("model",):
            continue
        out[name] = {"disk_text": f"{m.group(2)} {m.group(3)}",
                     "disk_bytes": to_bytes(float(m.group(2)), m.group(3)),
                     "blob_sha1": m.group(4), "line": i}
    if len(out) < 15:
        problems.append(f"上游模型表只认到 {len(out)} 行（<15），表格漂了")
    return out, problems


def parse_script(script: str) -> tuple[str, list[str], list[str]]:
    """从上游脚本读"下载源"和"它自己认为存在的模型名单"，域名不写死在我们代码里。"""
    problems = []
    src = re.search(r'^src="([^"]+)"', script, re.M)
    pfx = re.search(r'^pfx="([^"]+)"', script, re.M)
    if not src or not pfx:
        problems.append("脚本里读不到 src=/pfx= —— 上游改了下载脚本，别继续用硬编码 URL")
        return "", [], problems
    block = re.search(r'^models="([^"]*)"', script, re.M | re.S)
    if not block:
        problems.append("脚本里读不到 models= 白名单")
        names = []
    else:
        names = [n.strip() for n in block.group(1).split("\n") if n.strip()]
    return src.group(1).rstrip("/") + "/" + pfx.group(1), names, problems


def rule_lines(models_readme: str) -> dict:
    """把上游那三条语义规则的行号钉下来（改了我们就要重新读一遍文档）。"""
    out = {}
    for i, line in enumerate(models_readme.split("\n"), 1):
        if "multilingual unless" in line:
            out["multilingual"] = i
        if "support local diarization" in line:
            out["tdrz"] = i
        if "prefixed with `for-tests-`" in line:
            out["for_tests"] = i
    missing = [k for k in ("multilingual", "tdrz", "for_tests") if k not in out]
    return {"lines": out, "missing": missing}


def parse_langs(src_text: str, base_line: int = 0) -> tuple[list[dict], int, list[str]]:
    """`src/whisper.cpp` 的 `g_lang` 表 → (code, id, name) 清单 + 声明行 + 问题。

    字幕的第二个用户问题是"我给中文视频打字幕要选什么"，这件事只有引擎源码答得准：
    b5454 实测这张表 **100 条、id 从 0 连续到 99**（`en`=0、`zh`=1、`yue`=99 粤语）。
    UI 的语言下拉应当由运行时问 `whisper_lang_str()` 拿，不是我们把 100 个名字抄进 JSON ——
    这里读它是为了**量出这一轴有多宽**并盯住上游改动（表形状一变就该红，而不是下拉少一项没人知道）。
    """
    lines = src_text.split("\n")
    decl_pat = re.compile(r"^static const std::map<std::string, std::pair<int, std::string>> "
                          r"g_lang = \{$")
    decl = next((i for i, l in enumerate(lines, 1) if decl_pat.match(l)), None)
    if decl is None:
        return [], 0, ["读不到 g_lang 声明 —— 上游改了语言表的形状，语言这一轴不能再按老样子解"]
    # 先在**文本内**找收尾（夹具只存了那一块，从第 1 行开始），行号偏移留到最后再加；
    # 早一点加会让扫描范围冲出文本（我第一版就是这么错的：夹具里那条 `};` 明明在，却说找不到）
    end = next((i for i in range(decl + 1, min(len(lines) + 1, decl + 600))
                if re.match(r"^\};\s*$", lines[i - 1])), None)
    if end is None:
        return [], decl + base_line, ["g_lang 表的收尾 `};` 在 600 行内没找到"]
    body = "\n".join(lines[decl:end - 1])
    # 代码字符集留了数字：合成语料里写 l00 这种名字时，纯字母的正则会**静默少解 98 条**
    # （踩过，报出来的只有 en 与 zh）。宁可放宽匹配，也不要让"少一项"变成悄悄的事。
    found = re.findall(r'\{\s*"([A-Za-z0-9\-]+)"\s*,\s*\{\s*(\d+)\s*,\s*"([^"]+)"', body)
    problems = []
    if len(found) < 90:
        problems.append(f"语言表只解出 {len(found)} 条（b5454 实测 100），上游大概改写了这一项")
    ids = [int(i) for _, i, _ in found]
    if sorted(ids) != list(range(len(ids))):
        problems.append("语言 id 不是从 0 连续的一串 —— `whisper_lang_max_id()+1` 那个前提漂了")
    codes = [c for c, _, _ in found]
    if len(set(codes)) != len(codes):
        problems.append("语言代码有重复项")
    for must in ("en", "zh"):
        if must not in codes:
            problems.append(f"语言表里没有 {must} —— 中文与英文是这一轴的基本盘")
    return [{"code": c, "id": int(i), "name": n} for c, i, n in found], decl + base_line, problems


def host_files(tree: dict) -> tuple[dict[str, dict], dict[str, str]]:
    """宿主仓库里真能下载的重量文件：排空壳、排非 LFS、排不是 .bin 的东西。"""
    problems, out = {}, {}
    seen = {}
    for name, meta in tree.items():
        n = name
        if not n.endswith(".bin"):
            continue
        if os.path.basename(n).startswith("for-tests-"):
            seen[n] = "空壳（上游明写 for-tests- 不含权重）"
            continue
        if not meta.get("sha256"):
            problems[n] = "没有 oid"
            continue
        out[n] = {"sha256": meta["sha256"], "size": meta.get("size")}
    return out, problems


def split_variant(file: str) -> tuple[str, str, str | None, bool]:
    """ggml-small.en-q5_1.bin → (family=small, name=small.en-q5_1, quant=q5_1, english=True)。

    `name` 用**完整变体名**（含量化后缀）：上游那两张表与脚本白名单都按 `small.en-q5_1` 这种
    全名给行，把它截成 `small` 就会拿量化文件去比 f32 的磁盘数字（我第一版就是这么错的，
    24 条"表格与文件不一致"全是假警报）。
    """
    stem = os.path.basename(file)[len("ggml-"):-len(".bin")]
    quant = next((q for q in QUANTS if stem.endswith("-" + q)), None)
    bare = stem[:-(len(quant) + 1)] if quant else stem
    fam = next((f for f in FAMILIES if bare == f or bare.startswith(f + "-") or bare.startswith(f + ".")),
               None)
    return fam or bare.split("-")[0], stem, quant, ".en" in bare


def inventory(tree: dict, mem: dict, upstream: dict, dl_prefix: str, ms_tree: dict,
              whitelist: list[str]) -> tuple[list[dict], list[str], dict]:
    rows, problems = [], []
    files, errs = host_files(tree)
    for name, why in errs.items():
        problems.append(f"{name}: {why}")
    host_names = {split_variant(f)[1] for f in files}
    # 上游三张表自己互不相同 —— 这是**量到的事实**，不是车道该红的东西：
    # b5454 实测脚本白名单有 small.en-tdrz 而宿主没有该文件，宿主有 4 个 `.en-q8_0`
    # 而脚本没列。把它们记成 deltas 进产物，并由 check() 盯着这个集合变化：
    # 上游哪天补齐或改名，车道才红（红要有原因，不该天天红）。
    deltas = {"whitelist_only": sorted(set(whitelist) - host_names),
              "host_only": sorted(host_names - set(whitelist))}
    for file in sorted(files):
        fam, up_name, quant, english = split_variant(file)
        sha, size = files[file]["sha256"], files[file]["size"]
        cls = file_index.classify(file, {file: {"sha256": sha, "size": size}},
                                  {file: {"sha256": (ms_tree.get(file) or {}).get("sha256"),
                                          "size": (ms_tree.get(file) or {}).get("size")}},
                                  "hf", "modelscope")
        up = upstream.get(up_name)
        # Mem 只认"上游两张表自己说它是同一档"：按 Disk 原文精确相等来接（1.5 GiB 对 1.5 GiB），
        # 不做单位换算上的宽容 —— 上游自己就不严格（medium 实测 1.43 GiB 却写 1.5 GiB）。
        mem_row = None
        mem_src = "上游没给这一档的数字"
        if not quant and up and fam in mem and up["disk_text"] == mem[fam]["disk_text"]:
            mem_row, mem_src = mem[fam], "上游 Memory usage 表（:%d）" % mem[fam]["line"]
        elif quant:
            mem_src = "量化档：上游只给未量化档的 Mem，量化档没有官方峰值内存数字"
        elif up and fam in mem:
            mem_src = ("实测 %s 与上游 %s 档的 Disk %s 不是同一档，不敢套用它的 Mem"
                       % (human(size), fam, mem[fam]["disk_text"]))
        if up and size and abs(size - up["disk_bytes"]) > 0.2 * up["disk_bytes"]:
            problems.append(f"{up_name}: 上游模型表写 {up['disk_text']}（:{up['line']}），"
                            f"宿主实测 {human(size)} —— 差超过 20%，行与文件对不上了")
        fits = {label: (None if not mem_row else
                        mem_row["mem_bytes"] <= cap * UNIT["GiB"]) for label, cap in TIERS}
        recommended = {label: (None if not mem_row else
                               mem_row["mem_bytes"] <= cap * UNIT["GiB"] * HEADROOM)
                       for label, cap in TIERS}
        rows.append({"file": file, "name": up_name, "family": fam, "quant": quant,
                     "english_only": english, "multilingual": not english,
                     "size": size, "sha256": sha,
                     "status": cls["status"], "usable_before_download": cls["usable_before_download"],
                     "providers": cls["providers"],
                     "mem_text": mem_row["mem_text"] if mem_row else None,
                     "mem_bytes": mem_row["mem_bytes"] if mem_row else None,
                     "mem_source": mem_src,
                     "upstream_disk_text": up["disk_text"] if up else None,
                     "upstream_blob_sha1": up["blob_sha1"] if up else None,
                     "upstream_table_line": up["line"] if up else None,
                     "fits": fits, "recommended_for": recommended,
                     # 候选源顺序按实测吞吐（ModelScope 第一），但这个仓库它**没有**（实测 0 个文件）。
                     # 所以如实标出来，让 UI 把这条候选灰掉而不是让用户对着一个必 404 的快源发呆。
                     "distributor_absent": ([c["source"] for c in
                                             file_index.candidates("ggerganov/whisper.cpp", file)
                                             if not (ms_tree.get(file) or {}).get("sha256")
                                             and c["source"] == "modelscope"] or []),
                     "urls": [c["url"] for c in file_index.candidates("ggerganov/whisper.cpp", file)],
                     "download_url_upstream": f"{dl_prefix}/{file}" if dl_prefix else None})
    return rows, problems, deltas


def pick_tiers(rows: list[dict], key: str = "recommended_for") -> dict[str, list[dict]]:
    """每档按"多语言优先 + 上游 Mem 从大到小"排；`fit=unknown` 的行不参与档位推荐。

    `key` 选的是判据本身：`recommended_for` 带 HEADROOM 余量（我们的规则），
    `fits` 只问"塞得进去吗"（上游数字的算术）。UI 两个都要能拿到，默认档用前者。
    """
    out = {}
    for label, _cap in TIERS:
        fit = [r for r in rows if r[key][label] is True]
        fit.sort(key=lambda r: (not r["multilingual"], -(r["mem_bytes"] or 0), r["name"]))
        out[label] = [{"name": r["name"], "file": r["file"], "size": r["size"],
                       "mem_text": r["mem_text"], "multilingual": r["multilingual"],
                       "sha256": r["sha256"], "usable_before_download": r["usable_before_download"]}
                      for r in fit]
    return out


def build(tree: dict, ms_tree: dict, readme: str, models_readme: str, script: str,
          langs: dict | None = None) -> tuple[dict, list[str]]:
    problems = []
    mem, p1 = parse_mem_table(readme)
    upstream, p2 = parse_model_table(models_readme)
    prefix, whitelist, p3 = parse_script(script)
    rules = rule_lines(models_readme)
    problems += p1 + p2 + p3
    if rules["missing"]:
        problems.append(f"上游 models/README.md 里读不到 {rules['missing']} 这三条语义规则的行 —— "
                        "多语言/量化/空壳的判据失去出处，不许凭记忆")
    rows, p4, deltas = inventory(tree, mem, upstream, prefix, ms_tree, whitelist)
    problems += p4
    if not langs:
        problems.append("没拿到 src/whisper.cpp 的语言表 —— 语言这一轴不能被静默丢掉")
        lang_rows, lang_decl, lang_problems = [], 0, ["缺输入"]
    else:
        lang_rows, lang_decl, lang_problems = parse_langs(langs["text"],
                                                           langs.get("base_line", 0))
        problems += lang_problems
    tiers = pick_tiers(rows, "recommended_for")
    fit_only = pick_tiers(rows, "fits")
    for label, _ in TIERS:
        if len(tiers[label]) < 2:
            problems.append(f"档位 {label} 只推荐得出 {len(tiers[label])} 条（<2）—— "
                            "上游内存表大概改过了")
    unknown = sum(1 for r in rows if r["mem_bytes"] is None)
    return {"schema": 1, "ref": REF,
            "from": ["whisper.cpp README.md 的 Memory usage 表",
                     "whisper.cpp models/README.md 的 Model|Disk|SHA 表与三条语义规则",
                     "whisper.cpp models/download-ggml-model.sh 的白名单与下载源",
                     "whisper.cpp src/whisper.cpp 的 g_lang 语言表",
                     "宿主仓库 ggerganov/whisper.cpp 的 tree.lfs.oid（+ ModelScope 对照）"],
            "rule": {"tier": "默认档要求 上游 Mem <= 档位 × %s（我们的余量规则，与对话表同一个数）"
                             % HEADROOM,
                     "fits": "另给一列纯算术：Mem <= 档位，不带余量",
                     "order": "多语言优先（上游明写 .en 只有英文），再按上游 Mem 从大到小",
                     "mem_join": "Mem 只按上游两张表的 Disk 原文精确相等来接；量化档与 turbo 没有官方 Mem → unknown",
                     "hash": "tree.lfs.oid；ModelScope 不镜像该仓库 → 整轴 single，必须下载后自算",
                     "excluded": "for-tests- 前缀（上游明写是空壳）、非 .bin、无 oid"},
            "semantic_rule_lines": rules["lines"],
            "inventory": len(rows), "fit_unknown": unknown, "upstream_deltas": deltas,
            "languages": {"count": len(lang_rows), "decl_line": lang_decl,
                          "file": (langs or {}).get("file", "src/whisper.cpp"),
                          "ids_contiguous_from_zero": [x["id"] for x in lang_rows] ==
                                                       list(range(len(lang_rows))),
                          "has": {c: next((x["id"] for x in lang_rows if x["code"] == c), None)
                                  for c in ("en", "zh", "ja", "ko", "fr", "de", "es", "ru", "yue")},
                          "codes": [x["code"] for x in lang_rows]},
            "tiers": pick_tiers_labels(), "tiers_rows": tiers,
            "tiers_rows_fit_only": fit_only, "rows": rows}, problems


def pick_tiers_labels() -> list[str]:
    return [t[0] for t in TIERS]


def check(committed: dict, fresh: dict) -> list[str]:
    bad = []
    if committed.get("rule") != fresh.get("rule"):
        bad.append("选择规则变了，表和文档要一起重看")
    if committed.get("ref") != fresh.get("ref"):
        bad.append("钉的 tag 变了（%s → %s）" % (committed.get("ref"), fresh.get("ref")))
    old = {r["file"]: r for r in committed["rows"]}
    new = {r["file"]: r for r in fresh["rows"]}
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k), new.get(k)
        if a is None:
            bad.append(f"{k}: 宿主仓库新出一个文件，要不要收由人定")
            continue
        if b is None:
            bad.append(f"{k}: 这个文件现在取不到了（被撤或改名）")
            continue
        for f in ("sha256", "size", "status", "mem_bytes"):
            if a[f] != b[f]:
                bad.append(f"{k}.{f}: {str(a[f])[:34]!r} → {str(b[f])[:34]!r}")
    if committed.get("upstream_deltas") != fresh.get("upstream_deltas"):
        bad.append("上游三张表之间的出入变了（白名单 vs 宿主文件）：%s → %s —— "
                   "这不是我们的错，但推荐表该重看一遍"
                   % (committed.get("upstream_deltas"), fresh.get("upstream_deltas")))
    lc, lf = committed.get("languages", {}), fresh.get("languages", {})
    if lc.get("count") != lf.get("count") or lc.get("codes") != lf.get("codes"):
        bad.append("语言表变了：%s 条 → %s 条 —— UI 的语言下拉与 i18n 的承诺都要重看"
                   % (lc.get("count"), lf.get("count")))
    if lc.get("decl_line") != lf.get("decl_line"):
        bad.append("g_lang 声明从 :%s 挪到 :%s（出处行号要跟着改）"
                   % (lc.get("decl_line"), lf.get("decl_line")))
    for label in fresh["tiers"]:
        got = [r["name"] for r in fresh["tiers_rows"][label]]
        want = [r["name"] for r in committed["tiers_rows"].get(label, [])]
        if got != want:
            bad.append(f"档位 {label} 的默认档顺序变了 {want} → {got}")
        got2 = [r["name"] for r in fresh["tiers_rows_fit_only"][label]]
        want2 = [r["name"] for r in committed.get("tiers_rows_fit_only", {}).get(label, [])]
        if got2 != want2:
            bad.append(f"档位 {label} 的「塞得进」清单变了 {want2} → {got2}")
    return bad


FIXTURES = {"tree": "whisper_hf_tree.json", "ms": "whisper_ms_tree.json",
            "readme": "whisper_readme_b5454.md", "models_readme": "whisper_models_readme_b5454.md",
            "script": "whisper_download_script_b5454.sh", "langs": "whisper_langs_b5454.json"}


def load_fixtures(d: str) -> tuple[dict, dict, str, str, str, dict]:
    def read(key, binary=False):
        p = os.path.join(d, FIXTURES[key])
        with open(p, encoding=None if binary else "utf-8", errors="replace") as fh:
            return fh.read()
    tree = json.loads(read("tree"))["files"]
    ms = json.loads(read("ms"))["files"]
    langs = json.loads(read("langs"))
    return tree, ms, read("readme"), read("models_readme"), read("script"), \
        {"file": langs["file"], "text": langs["block"],
         "base_line": langs["decl_line"] - 1, "decl_line": langs["decl_line"]}


def live_inputs() -> tuple[dict, dict, str, str, str, dict]:
    def raw(url: str) -> str:
        rc, out, err = file_index.sh(["curl", "-sSL", "--max-time", "90",
                                      "-A", "recommend-subtitle", url])
        if rc != 0 or not out:
            raise SystemExit(f"取不到 {url}（curl rc={rc}）：{err}")
        return out
    base = f"https://raw.githubusercontent.com/{ENGINE_REPO}/{REF}"
    tree, answered, err = file_index.hf_files("ggerganov/whisper.cpp")
    if err:
        raise SystemExit(f"宿主仓库元数据取不到：{err} —— 这是取不到，不是没有模型")
    ms, _mb, _e2 = file_index.ms_files("ggerganov/whisper.cpp")
    src = raw(base + "/src/whisper.cpp")
    print(f"宿主 answered_by={answered} 文件 {len(tree)}；ModelScope 返回 {len(ms)} 个文件；"
          f"src/whisper.cpp {len(src)} 字节")
    return tree, ms, raw(base + "/README.md"), raw(base + "/models/README.md"), \
        raw(base + "/models/download-ggml-model.sh"), {"file": "src/whisper.cpp", "text": src}


def selftest() -> int:
    """离线对照：明知是错的必须被认成错，否则这四张表的交叉核对是装饰。"""
    readme = ('## Memory usage\n\n| Model  | Disk    | Mem     |\n| --- | --- | --- |\n'
              '| tiny   | 75 MiB  | ~273 MB |\n| base   | 142 MiB | ~388 MB |\n'
              '| small  | 466 MiB | ~852 MB |\n| medium | 1.5 GiB | ~2.1 GB |\n'
              '| large  | 2.9 GiB | ~3.9 GB |\n')
    rows_txt = [("tiny", "75 MiB"), ("tiny.en", "75 MiB"), ("tiny-q5_1", "31 MiB"),
                ("tiny-q8_0", "42 MiB"), ("base", "142 MiB"), ("base.en", "142 MiB"),
                ("base-q5_1", "57 MiB"), ("small", "466 MiB"), ("small.en", "466 MiB"),
                ("small-q5_1", "181 MiB"), ("medium", "1.5 GiB"), ("medium-q5_0", "514 MiB"),
                ("large-v1", "2.9 GiB"), ("large-v3", "2.9 GiB"), ("large-v3-q5_0", "1.1 GiB")]
    models_readme = ("| Model | Disk | SHA |\n| --- | --- | --- |\n"
                     + "".join("| %s | %s | `%s` |\n" % (n, d, "0" * 40) for n, d in rows_txt)
                     + "Models are multilingual unless the model name includes `.en`. "
                       "-tdrz support local diarization\n"
                       "The model files prefixed with `for-tests-` are empty\n")
    script = ('src="https://huggingface.co/ggerganov/whisper.cpp"\n'
              'pfx="resolve/main/ggml"\nmodels="tiny\ntiny.en\ntiny-q5_1\nbase\n"')
    tree = {"ggml-tiny.bin": {"sha256": "a" * 64, "size": 77691713},
            "ggml-base.bin": {"sha256": "d" * 64, "size": 147951465},
            "ggml-small.bin": {"sha256": "e" * 64, "size": 487601967},
            "ggml-medium.bin": {"sha256": "f" * 64, "size": 1533763059},
            "ggml-large-v3.bin": {"sha256": "0" * 64, "size": 3095033483},
            "ggml-tiny.en-q8_0.bin": {"sha256": "b" * 64, "size": 43550795},
            "for-tests-ggml-tiny.bin": {"sha256": "c" * 64, "size": 0},
            "ggml-nothing.bin": {"sha256": None, "size": 10}}
    bad = []
    mem, p = parse_mem_table(readme)
    if len(mem) != 5 or p:
        bad.append(f"正向对照失败：内存表认出 {len(mem)} 族，问题 {p}")
    if parse_mem_table(readme.replace("| tiny   | 75 MiB  | ~273 MB |", "| tiny | 75 MiB | ? |"))[1] == []:
        bad.append("负向对照失败：Mem 列读不出单位时，内存表只剩 4 族却没报问题")
    upstream = parse_model_table(models_readme)[0]
    if len(upstream) != 15:
        bad.append(f"模型表认出 {len(upstream)} 行（应 15）")
    r1, r2, r3, r4, r5 = tree, {}, readme, models_readme, script
    rows, pr, r2d = inventory(r1, mem, upstream, "https://x/ggml", r2,
                              ["tiny", "tiny.en", "vanished"])
    files = [x["file"] for x in rows]
    if "for-tests-ggml-tiny.bin" in files:
        bad.append("空壳对照失败：for-tests- 的文件进了推荐表")
    if files != ["ggml-base.bin", "ggml-large-v3.bin", "ggml-medium.bin", "ggml-small.bin",
                 "ggml-tiny.bin", "ggml-tiny.en-q8_0.bin"]:
        bad.append(f"清单形状不符：{files}")
    if "ggml-nothing.bin" not in " ".join(pr):
        bad.append("没有 oid 的文件被悄悄丢了（要说出来）")
    if "vanished" not in r2d["whitelist_only"]:
        bad.append("白名单里有一个宿主没有的名字却没记进 deltas")
    q = next(x for x in rows if x["quant"])
    if q["fits"]["8GB"] is not None or q["recommended_for"]["8GB"] is not None:
        bad.append("量化档在没有上游 Mem 时被判成可推荐（应 unknown）")
    f32 = next(x for x in rows if x["name"] == "tiny")
    if f32["mem_text"] != "273 MB" or f32["fits"]["4GB"] is not True:
        bad.append(f"未量化档没接上 Mem：{f32['mem_text']} / {f32['fits']}")
    if parse_script('src="https://x"\n')[2] == []:
        bad.append("读不到 pfx= 还当正常")
    if rule_lines("没有那三句话")["missing"] != ["multilingual", "tdrz", "for_tests"]:
        bad.append("语义规则行号丢了却不报")
    codes = ["en", "zh"] + ["x" + chr(97 + i // 26) + chr(97 + i % 26) for i in range(98)]
    decl_langs = ("static const std::map<std::string, std::pair<int, std::string>> g_lang = {")
    langs_block = (decl_langs + "\n"
                   + "\n".join('    { "%s",  { %d,  "lang-%d", } },' % (c, i, i)
                               for i, c in enumerate(codes)) + "\n};\n")
    script_full = (script.split('models="')[0]
                   + 'models="' + chr(10).join(["tiny", "base", "small", "medium",
                                                "large-v3", "tiny.en"]) + '"')
    built, bp = build(tree, {}, readme, models_readme, script_full,
                      {"file": "src/whisper.cpp", "text": langs_block})
    extra = [x for x in bp if "没有 oid" not in x]
    if extra:
        bad.append(f"合成输入本应全绿（除了故意放的无 oid 文件），却有 {extra[:3]}")
    if not built["tiers_rows"]["4GB"]:
        bad.append("4GB 档一条都推不出来（tiny ~273 MB 应该在里面）")
    if [x["name"] for x in built["tiers_rows"]["4GB"]][:1] != ["medium"]:
        bad.append("4GB 档默认档不是 medium（~2.1 GB 是带余量还能塞下的最大档）：%s"
                   % [x["name"] for x in built["tiers_rows"]["4GB"]])
    if [x["name"] for x in built["tiers_rows"]["8GB"]][:1] != ["large-v3"]:
        bad.append("8GB 档默认档不是 large-v3")
    if [x["name"] for x in built["tiers_rows"]["16GB"][:1]] != ["large-v3"]:
        bad.append("16GB 档默认档不是 large-v3")
    if any("只推荐得出" in x for x in bp):
        bad.append(f"合成输入下档位不满：{bp}")
    if built["tiers_rows"]["4GB"] == built["tiers_rows_fit_only"]["4GB"]:
        bad.append("带余量的默认档与纯算术的 fits 完全同列 —— HEADROOM 没起作用")
    if "large-v3" not in [x["name"] for x in built["tiers_rows_fit_only"]["4GB"]]:
        bad.append("纯算术的 fits 里 4GB 档竟然放不下 large-v3（~3.9 GB <= 4 GiB）")
    if "large-v3" in [x["name"] for x in built["tiers_rows"]["4GB"]]:
        bad.append("默认档把 large-v3 塞给 4GB 机器了 —— 余量规则没起作用")
    if parse_langs(langs_block)[0] != [{"code": c, "id": i, "name": "lang-%d" % i}
                                       for i, c in enumerate(codes)]:
        bad.append("正向对照失败：合成的 100 条语言表解不回原样")
    if parse_langs("没有这张表")[2] == []:
        bad.append("读不到 g_lang 声明却不报")
    if parse_langs(langs_block.replace(
            "std::map<std::string, std::pair<int, std::string>>", "std::vector<Whatever>"))[2] == []:
        bad.append("表形状改了（不再是那张 map）却没报")
    if parse_langs(langs_block)[1] != 1:
        bad.append("live 模式的行号就该是文本里的行号，别多偏移")
    if parse_langs(langs_block, 285)[1] != 286:
        bad.append("base_line 没把夹具的行号补回上游位置")
    if parse_langs(langs_block.replace('  { 7,  "lang-7"', '  { 70, "lang-7"'))[2] == []:
        bad.append("语言 id 不连续了却不报")
    if check(built, built):
        bad.append("check 对同一张表也报红：%s" % check(built, built))
    mutated = json.loads(json.dumps(built))
    mutated["upstream_deltas"] = {"whitelist_only": ["x"], "host_only": []}
    if not check(built, mutated):
        bad.append("上游三张表的出入变了，check 却不红")
    mutated2 = json.loads(json.dumps(built))
    mutated2["rows"][0]["sha256"] = "9" * 64
    if not check(built, mutated2):
        bad.append("哈希漂了，check 却不红")
    print("---- 结论 ----")
    for x in bad:
        print("  BAD:", x)
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="recommend_subtitle.py")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--live", action="store_true", help="真取四张表（CI 用这条）")
    g.add_argument("--fixtures", default="", help="离线读一份真捕获目录（tests/fixtures）")
    ap.add_argument("--out", default=os.path.join(ROOT, "catalog", "subtitle-tiers.json"))
    ap.add_argument("--check", action="store_true", help="重算并与已提交的那张比漂移")
    ap.add_argument("--selftest", action="store_true", help="离线对照，明知错的必须认成错")
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if not (a.live or a.fixtures):
        ap.error("要 --live 或 --fixtures（或 --selftest）")

    if a.fixtures:
        inputs = load_fixtures(a.fixtures)
    else:
        inputs = live_inputs()
    fresh, problems = build(*inputs)
    if a.check:
        with open(a.out, encoding="utf-8") as fh:
            committed = json.load(fh)
        bad = check(committed, fresh) + problems
        for x in bad:
            print("BAD:", x)
        print(f"比对 {fresh['inventory']} 个文件，问题 {len(bad)} 条")
        return 1 if bad else 0

    print(f"清单 {fresh['inventory']} 个可下载模型，其中 {fresh['fit_unknown']} 个上游没给峰值内存（不推只列）")
    for label in fresh["tiers"]:
        names = ", ".join("%s(%s)" % (r["name"], r["mem_text"]) for r in fresh["tiers_rows"][label][:4])
        print(f"  {label:<5} {names or '（没有一条塞得进）'}")
    for x in problems:
        print("BAD:", x)
    print("BAD 数量 =", len(problems))
    if problems:
        return 1
    if a.fixtures:
        print("（--fixtures 只做离线重算，不落盘）")
        return 0
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(fresh, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    print("落盘", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
