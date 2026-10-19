#!/usr/bin/env python3
"""按内存档位生成"这台机器能跑的对话模型"推荐表，每行尽量给到下载前就能校验的 sha256。

为什么需要：出厂默认档 `catalog/defaults.json` 里那 6 条是**车道与冒烟用的最小集合**
（对话那颗是 Qwen2.5-0.5B q2_k，415 MB，它是能跑通的最小样本，不是给用户的推荐）。
用户打开应用的第一个问题就是"我这台 8 GB 的笔记本该装哪个"，而目录里 14,364 条
`format=gguf` 的记录只有描述（`min_ram_gb` / `recommended_ram_gb` 是上游 llmfit 拟合出来的输入，
100% 非空），**没有一条带可校验的文件级哈希** —— 之前量到 `quantization` 有 88% 写 Q4_K_M
且是模型级不是文件级，所以"该下哪个文件"必须回到仓库文件表去解。

四条刻意选择的规则（每条都写进产物，UI 要说得出为什么推荐它）：
  1. 档位窗口 = **半档 < recommended_ram_gb <= 档位**，两个数都来自上游 llmfit，我们不拍：
     一开始只写 `<= 档位`，实测 8GB 与 16GB 档的前两行和 4GB 一模一样（Qwen3-0.6B 30,575,964 次下载、
     openai-community/gpt2 15,559,744 次，两条 `recommended_ram_gb` 都是 2.0），
     "我这台 16G 的机器装哪个"回答 0.6B 等于没回答，所以下限也要有，而且下限同样是上游的数除以 2；
  2. 窗口内按上游自己的 `hf_downloads` 从多到少，不用我感觉哪个模型好；
  3. 仓库优先级 **ggml-org → unsloth → mradermacher**：ggml-org 就是我们随包发的引擎
     （llama.cpp）的维护方，同一个引擎的作者重打出来的量化最不容易踩格式分支；
     这不是审美，是"引擎与权重同源"这一条约束的直接结果。但它是**依次尝试**，不是"只取第一个"：
     实测偏好第一名的 ggml-org/Qwen3-8B-GGUF 只能给出 Q8_0（8,709,518,656 字节，比 8 GB 机器的
     总内存还大），只取第一个就会推荐一颗这台机器载不进来的权重；
  4. 量化按偏好顺序找，且**文件要整只、要塞得进本档**：排除名字含 `-of-` 的分片（半个模型不是
     可加载文件，只按量化标签匹配会选中它），并要求大小 < 档位 × 1 GiB；一个仓库找不到就换
     下一个，全部找不到就报问题，**不退而求其次挑一个能下到的**。

哈希一律走 `file_index` 的两家比对（HF `tree.lfs.oid` × ModelScope 元数据 `Sha256`），
只有两家给同一个哈希才标 `usable_before_download`；单家一致的行照样留在表里，但级数是 3，
UI 得能说"这颗要下完自己算"。许可按原值透传（`license` 是 llmfit 的字段，unknown 3,404 条、
含 non-commercial 376 条），不因为"不好看"就悄悄丢掉 —— 合规红线是许可必须到得了用户。

两条实测出来的数据处理细节：
  · 同名去重按 `name.casefold()`：`microsoft/Phi-3-mini-4k-instruct` 与
    `microsoft/phi-3-mini-4k-instruct` 在目录里是两条记录、下载量一模一样（各 370,784），
    不去重同一档里会出现两次同一个模型；
  · `use_case` **只透传不过滤**：上游把 Qwen3-8B（9,650,573 次下载）标成
    "General purpose text generation"，按它筛 chat 会踢掉最强的那颗、放进
    Llama-2-13b-chat（2023 年，195,992 次）—— 这个字段该由 UI 说给用户，不是我们替它判断。
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

LAYER = os.path.join(ROOT, "catalog", "describe.json")
OUT = os.path.join(ROOT, "catalog", "recommendations.json")
TIERS = (("4GB", 4.0), ("8GB", 8.0), ("16GB", 16.0))
FIT_FRACTION = 0.5           # 窗口下限 = 档位 × 这个数；上限就是档位本身
PER_TIER = 5
SCAN_MULT = 3                # 每档最多扫 per_tier×SCAN_MULT 条候选来凑满，防止无限联网
PROVIDER_PREF = ("ggml-org", "unsloth", "mradermacher")
QUANT_PREF = ("Q4_K_M", "Q4_K_S", "Q5_K_M", "Q3_K_M", "Q2_K", "Q4_0", "IQ4_XS", "Q8_0")
RISKY_LICENSE = re.compile(r"(^|-)(nc|other|unknown)(-|$)")


def chat_rows(models: list[dict]) -> list[dict]:
    """对话这一项的候选：text-generation、有可下载仓库、且 recommended_ram_gb 是个数。"""
    out = []
    for m in models:
        if m.get("pipeline_tag") != "text-generation" or not m.get("has_sources"):
            continue
        rec = m.get("recommended_ram_gb")
        if not isinstance(rec, (int, float)):
            continue
        out.append(m)
    return out


def window(cands: list[dict], cap: float) -> list[dict]:
    """档位窗口（半档 < rec <= 档）内按上游下载量排，同名忽略大小写只留第一条。

    单独成一个函数是因为 `build` 要报告"这个窗口里本来有几条"——只剩 2 条和只剩 2 条
    是因为窗口里没有货，是两件事，UI/文档要能区分。
    """
    fit = [m for m in cands if cap * FIT_FRACTION < m["recommended_ram_gb"] <= cap]
    fit.sort(key=lambda m: (-(m.get("hf_downloads") or 0), m["name"]))
    seen: set[str] = set()
    out = []
    for m in fit:
        key = m["name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def tier_rows(cands: list[dict], cap: float, k: int) -> list[dict]:
    return window(cands, cap)[:k]


def ranked_repos(sources: list[dict]) -> list[tuple[str, str]]:
    """这台模型的全部可下载仓库，按 PROVIDER_PREF 排好序（不是"取第一个偏好方"）。

    为什么要整张排序表：一个 provider 的文件表不一定有塞得进这台机器的量化，
    只取第一个就意味着"偏好"会盖过"能不能跑"（实测 8GB 档的 Qwen3-8B 撞上这条）。
    """
    seen, out = set(), []
    for s in sources:
        repo = s.get("repo")
        if not repo or repo in seen:
            continue
        seen.add(repo)
        out.append((repo, s.get("provider")))

    def rank(item):
        prov = item[1]
        return PROVIDER_PREF.index(prov) if prov in PROVIDER_PREF else len(PROVIDER_PREF)

    out.sort(key=lambda item: (rank(item), item[0]))
    return [(repo, f"provider={prov}" + ("" if prov in PROVIDER_PREF else "（不在偏好里，排在偏好方之后）"))
            for repo, prov in out]


def file_size(meta: dict, name: str) -> int | None:
    """任一家给的文件字节数（两家都只给元数据不给内容，这是下载前唯一能拿到的大小）。"""
    for k in ("hf", "ms"):
        s = (meta[k].get(name) or {}).get("size")
        if isinstance(s, (int, float)) and s > 0:
            return int(s)
    return None


SHARD = re.compile(r"-of-\d+", re.I)


def pick_file(names: list[str], size_of, cap_gib: float, quant_hint: str | None,
              stems: tuple[str, ...] = ()) -> tuple[str | None, str]:
    """在仓库文件表里按量化偏好找一个**标准量化、整只、塞得进这台机器**的 .gguf；找不到返回 None。

    三条硬排除都是实测出来的，不是审美：
      · 形状：文件名必须是 `<仓库名去掉 -GGUF 或模型名> + 分隔符 + 量化标签 + .gguf`。
        只按"名字里含 Q8_0"匹配会选中 `dflash-Qwen3-8B-Q8_0.gguf`（那是投机解码的草稿模型，
        实测 8GB 档就这么被带偏过一次），mmproj / lora / adapter 同理都是附带产物；
      · 分片文件（名字含 `-of-`）不是可独立加载的整模型，半个模型不能当成品；
      · 大小必须 < 档位 × 1 GiB —— 比机器总内存还大的文件载不进来，这是算术不是审美
        （实测 ggml-org/Qwen3-8B-GGUF 只给得出 Q8_0，8,709,518,656 字节 > 8 GiB）。
    同一量化标签有多个文件时按大小升序取第一个能塞下的（可复现，且小的一片更容易合用）。
    """
    max_bytes = int(cap_gib * 2 ** 30)
    whole = [n for n in names if n.lower().endswith(".gguf") and not SHARD.search(n)]
    rejected: list[str] = []
    tried = 0
    for q in list(QUANT_PREF) + ([quant_hint] if quant_hint else []):
        std = []
        for n in whole:
            base = n.rsplit("/", 1)[-1]
            if stems and not any(re.fullmatch(rf"{re.escape(s)}[-._]?{re.escape(q)}\.gguf",
                                              base, re.I) for s in stems):
                continue
            std.append(n)
        tried += len(std)

        def by_size(n):
            s = size_of(n)
            return (1 << 62) if s is None else s, n

        for n in sorted(std, key=by_size):
            s = size_of(n)
            if s is None:
                rejected.append(f"{n}(两家都没给大小)")
                continue
            if s < max_bytes:
                why = f"量化偏好命中 {q}（{s:,} 字节 < {cap_gib:.0f} GiB）"
                if q == quant_hint and q not in QUANT_PREF:
                    why = f"偏好量化都不合用，按模型级 quantization={q} 找到（{s:,} 字节）"
                return n, why
            rejected.append(f"{n}={s / 2 ** 30:.1f} GiB 超档")
    if tried:
        return None, (f"{tried} 个标准量化文件全都装不进 {cap_gib:.0f} GB："
                      + "；".join(rejected[:3]) + ("…" if len(rejected) > 3 else ""))
    return None, (f"没有形状合适的整只 .gguf（排除分片后剩 {len(whole)} 个；"
                  "标准形状 = 仓库名/模型名 + 量化标签，草稿模型与 mmproj 这类附带产物已排除）")


def repo_stem(repo: str, model_name: str) -> tuple[str, ...]:
    """文件名该长什么样：仓库名去掉 -GGUF 后缀，再兜上模型名本身（两者实测各有命中的仓库）。"""
    tail = repo.rsplit("/", 1)[-1]
    out = [re.sub(r"[-._]gguf$", "", tail, flags=re.I)]
    name = model_name.rsplit("/", 1)[-1]
    if name not in out:
        out.append(name)
    return tuple(s for s in out if s)


def resolve_repo(repo: str, fetch) -> tuple[list[str], dict, list[str]]:
    """一个 repo 两家元数据都取；两家都取不到就报问题，不当"没有这个文件"。"""
    hf, hf_by, hf_err = fetch(repo, "hf")
    ms, ms_by, ms_err = fetch(repo, "ms")
    if hf_err and ms_err:
        return [], {}, [f"{repo}: 两家都没答，这是取不到而不是没有这个文件"
                        f"（hf: {hf_err[:60]}；ms: {ms_err[:60]}）"]
    names = sorted(set(hf) | set(ms))
    return names, {"hf": hf, "ms": ms, "hf_by": hf_by, "ms_by": ms_by,
                   "hf_err": hf_err, "ms_err": ms_err}, []


def build(models: list[dict], fetch, per_tier: int = PER_TIER,
          min_candidates: int | None = 200) -> tuple[dict, list[str]]:
    """生成推荐表。fetch(repo, kind) -> (dict, answered_by, err) 是注入点：
    单测喂字典，`--live` 喂 file_index 的真取表函数。
    `min_candidates` 是描述层的地板（离线切片只装选中的行，所以那条路径传 None）。"""
    problems: list[str] = []
    cands = chat_rows(models)
    if min_candidates is not None and len(cands) < min_candidates:
        problems.append(f"候选只有 {len(cands)} 条对话模型（<{min_candidates}），描述层大概漂了")
    memo: dict[str, tuple[list[str], dict, list[str]]] = {}
    tiers: dict[str, list[dict]] = {}
    pools: dict[str, int] = {}
    dropped: list[dict] = []
    for label, cap in TIERS:
        pool = window(cands, cap)
        pools[label] = len(pool)
        if len(pool) < 3:
            problems.append(f"窗口 {cap:.0f}GB（半档到满档）里只有 {len(pool)} 条候选："
                            "这一档等于没内容。放宽窗口是人的决定，工具不自动放宽")
        rows = []
        budget = per_tier * SCAN_MULT      # 上游命名一漂就会解不出文件，不封顶等于无限取
        scanned = 0
        for m in pool:
            if len(rows) >= per_tier or scanned >= budget:
                break
            scanned += 1
            tries = ranked_repos(m.get("gguf_sources") or [])
            if not tries:
                dropped.append({"tier": label, "name": m["name"], "why": "没有可下载的仓库条目"})
                continue
            row = None
            fails: list[str] = []
            for repo, why_repo in tries:
                if repo not in memo:      # 同一个仓库会被多档/多行选中，一次别取两遍
                    memo[repo] = resolve_repo(repo, fetch)
                names, meta, errs = memo[repo]
                problems += [e for e in errs if e not in problems]
                if not names:             # 两家都没答：那是取数失败，已经报过，不重复报"没文件"
                    continue
                def size_of(n, meta=meta):
                    return file_size(meta, n)

                file, why_file = pick_file(names, size_of, cap, m.get("quantization"),
                                           repo_stem(repo, m["name"]))
                if not file:
                    fails.append(f"{repo}: {why_file}")
                    continue
                cls = file_index.classify(file, meta["hf"], meta["ms"],
                                          meta["hf_by"] or "hf", meta["ms_by"] or "ms")
                row = {
                    "name": m["name"], "repo": repo, "file": file,
                    "provider_reason": why_repo, "file_reason": why_file,
                    "repos_tried": len(fails) + 1,
                    "recommended_ram_gb": m["recommended_ram_gb"], "min_ram_gb": m["min_ram_gb"],
                    "parameter_count": m.get("parameter_count"),
                    "context_length": m.get("context_length"),
                    "hf_downloads": m.get("hf_downloads"),
                    "use_case": m.get("use_case"),
                    "license": m.get("license"),
                    "license_flag": ("需向用户显示" if RISKY_LICENSE.search(str(m.get("license")))
                                     else "ok"),
                    "sha256": cls["sha256"], "size": cls["size"],
                    "status": cls["status"],
                    "usable_before_download": cls["usable_before_download"],
                    "providers": cls["providers"],
                    "urls": [c["url"] for c in file_index.candidates(repo, file)][:3],
                }
                break
            if row is None:
                if fails:
                    dropped.append({"tier": label, "name": m["name"],
                                    "why": f"{len(tries)} 个仓库都没给出塞进 {cap:.0f} GB 的"
                                           f"标准量化 —— " + "；".join(fails)})
                elif not any(memo[r][0] for r, _ in tries):
                    dropped.append({"tier": label, "name": m["name"],
                                    "why": "列出的仓库里没有一家给得出文件表"
                                           "（取数失败会单独报，这里是仓库本身不存在）"})
                continue
            rows.append(row)
        tiers[label] = rows
        if len(rows) < per_tier:
            problems.append(f"档位 {label} 想要 {per_tier} 条，只凑到 {len(rows)} 条"
                            f"（窗口里 {pools[label]} 条候选、扫了 {scanned} 条，预算 "
                            f"{budget}=per_tier×{SCAN_MULT}）：要么上游命名漂了，"
                            "要么窗口里真没货 —— 加大预算是人的决定，工具不自动加")
        if len(rows) < 3:
            problems.append(f"档位 {label} 只剩 {len(rows)} 条（<3，窗口里有 {pools[label]} 条候选）："
                            "这一档等于没内容")
    agree = sum(1 for rows in tiers.values() for r in rows if r["usable_before_download"])
    total = sum(len(v) for v in tiers.values())
    return {"schema": 1, "from": ["catalog/describe.json", "file_index 两家元数据"],
            "tiers": [t[0] for t in TIERS], "per_tier": per_tier,
            "rule": {"tier": f"半档 < recommended_ram_gb <= 档位（FIT_FRACTION={FIT_FRACTION}，"
                             "两个数都用上游 llmfit 的拟合值）",
                     "order": "窗口内按上游 hf_downloads 从多到少，同名忽略大小写只留第一条",
                     "repo": "依次尝试 " + " > ".join(PROVIDER_PREF) +
                             "（引擎同方优先），直到某个仓库能给出符合下面两条的文件；"
                             "都不符合就报问题而不是硬选一个",
                     "file": "量化偏好顺序 + 排除分片（名字含 -of-）+ 大小 < 档位 × 1 GiB",
                     "quant": "/".join(QUANT_PREF),
                     "hash": "HF tree.lfs.oid × ModelScope 元数据 Sha256；两家一致才 usable"},
            "candidate_pool": {"chat": len(cands), **pools},
            "rows": total, "usable_before_download": agree,
            "dropped": dropped,
            "tiers_rows": tiers}, problems


def check(committed: dict, fresh: dict) -> list[str]:
    """漂移检查：行集合（按档位+模型名认，所以"从 8GB 档挪到 16GB 档"也算漂移）、
    每行的 repo/file/sha256/size/status 都得一样。"""
    bad = []
    if committed.get("rule") != fresh.get("rule"):
        bad.append("选择规则变了（tier/order/repo/file/quant/hash 任一条），表和文档要一起重看")
    old = {(k, r["name"]): r for k in committed["tiers_rows"] for r in committed["tiers_rows"][k]}
    new = {(k, r["name"]): r for k in fresh["tiers_rows"] for r in fresh["tiers_rows"][k]}
    for k in sorted(set(old) | set(new), key=str):
        a, b = old.get(k), new.get(k)
        if a is None:
            bad.append(f"{k[0]}/{k[1]}: 上游新进来一条（{b['repo']}），要不要收由人定")
            continue
        if b is None:
            bad.append(f"{k[0]}/{k[1]}: 这一档现在解不出来了（仓库或文件漂了，或它挪了档）")
            continue
        for f in ("repo", "file", "sha256", "size", "status"):
            if a[f] != b[f]:
                bad.append(f"{k[1]}.{f}: {str(a[f])[:40]!r} → {str(b[f])[:40]!r}")
    return bad


def live_fetch(repo: str, kind: str):
    if kind == "hf":
        return file_index.hf_files(repo)
    return file_index.ms_files(repo)


def recording(fetch, store: dict):
    """把 fetch 每次真回答的东西记进 store —— `--fixtures-out` 的切片就是这么来的，
    不是我事后照着记忆补的形状。"""
    def wrapped(repo, kind):
        res = fetch(repo, kind)
        row = store.setdefault(repo, {})
        row[kind] = {n: v for n, v in res[0].items() if n.lower().endswith(".gguf")}
        row[f"{kind}_by"] = res[1]
        if res[2]:
            row[f"{kind}_err"] = res[2]
        return res
    return wrapped


def write_slice(models: list[dict], store: dict, table: dict, per_tier: int, path: str) -> None:
    """切片 = 这次真扫过的候选（每档窗口前 per_tier×SCAN_MULT 条）+ 这次真取到的两家元数据。

    只留 .gguf 条目：判据只看 .gguf，把仓库的 README/config 名字搬进仓库没有意义，而它们也不会
    改变任何一行的结论（这件事由单测里"用切片重算 = 已提交的表"那条 gate 证明，不靠我说）。
    `table` 存的是联网那次算出的整张表，单测拿切片重算后逐行比对它。
    """
    cands = chat_rows(models)
    scanned = []
    for _, cap in TIERS:
        scanned += window(cands, cap)[:per_tier * SCAN_MULT]
    blob = {"captured": "真元数据切片：只留 .gguf 条目；用 models+files 离线重算，"
                        "tiers_rows 必须与 table 逐行相同（单测 gate）",
            "from": "catalog/describe.json + file_index.hf_files/ms_files",
            "per_tier": per_tier, "models": scanned, "files": store, "table": table}
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(blob, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"切片落盘 {path}：{len(scanned)} 条候选、{len(store)} 个仓库的两家元数据")


def slice_fetch(table: dict):
    """把切片里的 files 表包成 fetch —— 单测与 `--fixtures` 共用同一条路，不另写一遍。"""
    def fetch(repo, kind):
        row = table.get(repo) or {}
        return row.get(kind) or {}, row.get(f"{kind}_by", kind), row.get(f"{kind}_err")
    return fetch


def write_guard(fresh: dict, from_fixtures: bool) -> tuple[bool, str]:
    """落盘闸门。两条都是被自己咬出来的：某次 `--fixtures`（不带 `--check`）把整张产品表
    覆盖成了切片算出的那张 —— 15 行看起来照样像回事，但 `candidate_pool` 已经从 1,283 变成 45。

    所以：离线重算永远不许落盘；真跑但候选池不像整层的，也不许落盘。
    """
    if from_fixtures:
        return False, ("--fixtures 只做离线重算，不许落盘：切片里没有整层，"
                       "candidate_pool 会变成切片的计数。要出表用 --live")
    chat = fresh["candidate_pool"]["chat"]
    if chat < 200:
        return False, f"候选池只有 {chat} 条，不像整层算出来的表 → 不落盘"
    return True, ""


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="recommend_models.py")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--live", action="store_true", help="取真两家元数据（CI 用这条）")
    g.add_argument("--fixtures", default="", help="用一份描述层切片 + 一个字典式 fake fetch 跑")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--fixtures-out", default="",
                    help="（配 --live）把这次真扫过的候选与真取到的元数据存成离线切片")
    ap.add_argument("--per-tier", type=int, default=PER_TIER)
    ap.add_argument("--check", action="store_true", help="重算并与已提交的那张比漂移")
    a = ap.parse_args(argv)
    if a.fixtures_out and not a.live:
        ap.error("--fixtures-out 只在 --live 那次有意义：切片必须是真捕获")

    store: dict = {}
    if a.fixtures:
        with open(a.fixtures, encoding="utf-8") as fh:
            blob = json.load(fh)
        fresh, problems = build(blob["models"], slice_fetch(blob["files"]),
                                per_tier=a.per_tier, min_candidates=None)
    else:
        with open(LAYER, encoding="utf-8") as fh:
            models = json.load(fh)["models"]
        fetch = recording(live_fetch, store) if a.fixtures_out else live_fetch
        fresh, problems = build(models, fetch, per_tier=a.per_tier)
        if a.fixtures_out:
            write_slice(models, store, fresh, a.per_tier, a.fixtures_out)
    if a.check:
        with open(a.out, encoding="utf-8") as fh:
            committed = json.load(fh)
        bad = check(committed, fresh) + problems
        for x in bad:
            print("BAD:", x)
        print(f"比对 {fresh['rows']} 行，问题 {len(bad)} 条")
        return 1 if bad else 0

    print(f"行数 {fresh['rows']}，两家一致（下载前就可信）{fresh['usable_before_download']} 行")
    for label in fresh["tiers"]:
        for r in fresh["tiers_rows"][label]:
            print(f"  {label:<4} {r['name'][:34]:<36} {r['file'][:40]:<42} "
                  f"{r['status']:<8} {'' if r['usable_before_download'] else '需下载后自算'}")
    print(f"跳过 {len(fresh['dropped'])} 条候选（原因写在产物 dropped 字段里，不等于失败）")
    for d in fresh["dropped"][:6]:
        print(f"  · {d['tier']:<5} {d['name'][:30]:<32} {d['why'][:96]}")
    for x in problems:
        print("BAD:", x)
    print("BAD 数量 =", len(problems))
    if problems:
        return 1
    ok, why = write_guard(fresh, bool(a.fixtures))
    if not ok:
        print("不落盘：", why)
        return 1 if not a.fixtures else 0
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(fresh, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    print("落盘", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
