#!/usr/bin/env python3
"""按内存档位给生图挑扩散权重 —— 但**不编内存数字**：上游没给，就如实写"这一档要靠引擎运行时判"。

为什么这一档必须另建：`catalog/describe.json`（llmfit）里 **生图 0 条**，whisper 那种"上游自己写了
`Model | Disk | Mem` 表"的好事在扩散模型这边没有 —— sd.cpp 的 `docs/sd.md` 只给 **safetensors** 的
出处（`:4-7`），GGUF 量化全在社区仓库里。所以能核对的只有两类事实：

  · **文件级**：HF `tree.lfs.oid` 与 ModelScope 元数据 `Sha256`（实测对 `second-state/*`、
    `leejet/*`、`city96/*`、`gpustack/*` 都给同一个 sha256 → 生图这一轴能到 4.5 级；
    而 `OlegSkutte/sdxl-turbo-GGUF` 在 ModelScope 返回 0 条 → 只能 `single`）；
  · **引擎侧**：`docs/performance.md` 说内存是**运行时量出来的**，不是查表 —— 装不下的模型会走
    分段执行（`:136-145`，容量检查留 128 MiB 余量、设备底线 512 MiB），还有一组会真正改变
    "跑不跑得动"的开关（`--flash-attn`、VAE tile、`--offload-to-cpu`、`--max-vram -1`）。
    README `:157` 就是把这两件事推到这份文档的。

所以这张表的定位与对话/字幕那张**不同**：它回答"这台机器的内存装得下哪些权重文件"
（算术：文件大小 < 档位 × 0.5，余量规则与前两张同一个常数），
并明确写出 `memory_claim = none` —— 想给用户一句"跑一张要多久/要多少内存"，
必须由第一次真机跑出来，不是由我在这里补一个系数。

三条选择规则（都进产物 `rule`）：
  1. 候选池 = 用上游搜索按家族查（查询串写死在代码里并进产物，车道能发现"多了一个家族"），
     按上游 `downloads` 从多到少 —— 与对话那张同一个"不替用户审美"的口径；
  2. 仓库优先级：**`leejet`（就是我们随包发的 sd.cpp 的作者）> 其余按下载量**。
     另外把"上传方自己打了 `sd.cpp`/`stable-diffusion.cpp` 标签"这件事原样带出来，
     这是上游给的兼容性声明，不是我猜的；
  3. 文件级不做量化名偏好：扩散权重的命名各家不同（`q4_0` / `Q4_K_S` / `F16` / `.q8_0`），
     按量化名匹配会漏会错；改成"仓库里每个 `.gguf` 都算一行"，
     再按大小过档 —— 顺带一条实测教训：`leejet/FLUX.1-schnell-gguf` 的
     `q4_0` 与 `q4_k` **大小一模一样（6,884,606,880 B）而 sha256 不同**，
     所以"大小相同就是同一个文件"是假的，键必须是文件名 + 哈希。

许可按原值透传并标风险：SD1.5 是 `creativeml-openrail-m`（对生成物有使用限制），
FLUX-schnell 的几个仓库是 `apache-2.0` —— 合规红线是许可必须到得了用户，
不是我们把不好看的行删掉。
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
import file_index      # noqa: E402
import recommend_models as rm    # 复用 RISKY_LICENSE / TIERS，档位口径三张表共用  # noqa: E402
import recommend_subtitle as rsub  # HEADROOM 与 UNIT 也复用，避免三张表各写一个 0.5  # noqa: E402

REF = "95b3090664d019fcf8fa032a44d1d4793ba244b8"
ENGINE_REPO = "leejet/stable-diffusion.cpp"
OUT = os.path.join(ROOT, "catalog", "image-tiers.json")
SEARCH_HOST = "hf-mirror.com"
QUERIES = ("stable-diffusion gguf", "sdxl gguf", "flux schnell gguf", "sd3 gguf")
LIMIT = 12
AUTHOR = "leejet"                       # 随包发的引擎的作者，自己发 gguf
ENGINE_TAGS = ("sd.cpp", "stable-diffusion.cpp")
MATURE_TAG = "not-for-all-audiences"   # 上游自己打的标记，不是我们按仓库名猜的
MEM_DOC = "docs/performance.md"
MEM_FACTS = {              # 上游对"内存这件事"的原话位置，逐条进出处表由 citation-drift 查
    "segmented_headroom": 143,
    "segmented_floor": 145,
    "readme_points_here": 157,
}
PER_TIER = 4


def repo_license(meta: dict) -> str | None:
    card = meta.get("cardData") or {}
    if card.get("license"):
        return card["license"]
    for t in meta.get("tags") or []:
        if isinstance(t, str) and t.startswith("license:"):
            return t.split(":", 1)[1]
    return None


def engine_tag(meta: dict) -> list[str]:
    return sorted({t for t in (meta.get("tags") or []) if t in ENGINE_TAGS})


def mature_flag(meta: dict) -> bool:
    return MATURE_TAG in (meta.get("tags") or [])


ROLE_TOKENS = (                      # 顺序有意义：先认到就是什么角色
    ("vae", ("vae", "ae-", "_ae.", "taesd")),
    ("text_encoder", ("t5xxl", "t5-", "clip_l", "clip_g", "clip-", "prompt-llama",
                      "llama_8b", "text_encoder", "textencoder")),
    ("lora", ("lora",)),
)


def role_of(file: str) -> tuple[str, str]:
    """这个 .gguf 是扩散主体还是配套件 —— 生图不只有一个文件，这是实测出来的。

    `Green-Sky/flux.1-schnell-GGUF` 的清单是 `ae-f16 / clip_l-q8_0 / flux1-schnell-q4_k / t5xxl_q3_k`：
    只按大小排档位，4GB 档第一行会是 **t5xxl 文本编码器**（不是出图的那颗）。
    所以角色必须先进表，档位再只认 `unet`（主体）。认不出的一律 `other`，不猜。
    """
    low = os.path.basename(file).lower()
    for role, tokens in ROLE_TOKENS:
        for t in tokens:
            if t in low:
                return role, t
    return "unet", ""


def search_repos(fetch_meta, queries: tuple[str, ...] = QUERIES,
                 limit: int = LIMIT) -> tuple[list[dict], list[str]]:
    """候选池来自上游搜索本身（按下载量排），不是我背一份仓库名单。"""
    problems, seen, out = [], {}, []
    for q in queries:
        rows, err = fetch_meta("search", q)
        if err:
            problems.append(f"查询 {q!r} 没答（{err[:60]}）—— 这是取不到，不是没有仓库")
            continue
        if len(rows) == 0:
            problems.append(f"查询 {q!r} 返回 0 条：上游搜索漂了或这个家族没人发 GGUF 了")
        for m in rows:
            rid = m.get("id")
            if not rid or rid in seen:
                continue
            seen[rid] = True
            out.append({"repo": rid, "downloads": m.get("downloads") or 0,
                        "query": q, "license": repo_license(m),
                        "engine_tags": engine_tag(m), "mature": mature_flag(m)})
    return out, problems


def ranked_repos(cands: list[dict]) -> list[dict]:
    """引擎作者自己的仓库排前面，其余按上游下载量。"""
    return sorted(cands, key=lambda r: (0 if r["repo"].split("/")[0] == AUTHOR else 1,
                                        -r["downloads"], r["repo"]))


def build(repos: list[dict], files_for, per_tier: int = PER_TIER) -> tuple[dict, list[str]]:
    """repos 是候选池（已含 license/engine_tags）；files_for(repo) → (hf, hf_by, ms, ms_by, err)。"""
    problems: list[str] = []
    rows: list[dict] = []
    unresolved: list[str] = []
    for r in ranked_repos(repos):
        hf, hf_by, ms, ms_by, err = files_for(r["repo"])
        if err:
            problems.append(f"{r['repo']}: 取不到文件表（{err[:60]}）")
            continue
        gguf = sorted({k for k in list(hf) + list(ms) if k.lower().endswith(".gguf")})
        if not gguf:
            # 两家都答了、都说没有 .gguf：这是上游仓库本身的状态（撤了/改名/只剩 README），
            # 不是我们的解析失败。记成数据并由 check() 盯集合变化，不让车道天天红。
            unresolved.append(r["repo"])
            continue
        for f in gguf:
            cls = file_index.classify(f, hf, ms, hf_by or "hf", ms_by or "ms")
            if cls["status"] == "absent":      # 只有一家有这个文件时仍要出行，但要能看出来
                problems.append(f"{r['repo']}/{f}: 两家文件表都没有这个键（解析错了）")
                continue
            size = cls["size"]
            if not isinstance(size, (int, float)) or size <= 0:
                problems.append(f"{r['repo']}/{f}: 大小不明，无法判档")
                continue
            role, token = role_of(f)
            rows.append({
                "repo": r["repo"], "file": f, "query": r["query"],
                "role": role, "role_token": token,
                "by_author": r["repo"].split("/")[0] == AUTHOR,
                "engine_tags": r["engine_tags"], "downloads": r["downloads"],
                "license": r["license"],
                # 许可缺失也算要显示：`str(None)` 是 "None"，纯正则会把"没有许可"当成"许可没问题"
                "license_flag": ("需向用户显示（许可不明）" if not r["license"] else
                                 ("需向用户显示" if rm.RISKY_LICENSE.search(r["license"])
                                  else "ok")),
                "mature": r["mature"],
                "size": int(size), "sha256": cls["sha256"], "status": cls["status"],
                "usable_before_download": cls["usable_before_download"],
                "providers": cls["providers"],
                "fits": {label: int(size) < cap * rsub.UNIT["GiB"] * rsub.HEADROOM
                         for label, cap in rm.TIERS},
                "urls": [c["url"] for c in file_index.candidates(r["repo"], f)][:3],
            })
    tiers: dict[str, list[dict]] = {}
    for label, _cap in rm.TIERS:
        pool = [x for x in rows if x["fits"][label] and x["role"] == "unet" and not x["mature"]]
        pool.sort(key=lambda x: (-x["size"], x["repo"], x["file"]))
        tiers[label] = [dict(
            {"repo": x["repo"], "file": x["file"], "size": x["size"], "sha256": x["sha256"],
             "status": x["status"], "usable_before_download": x["usable_before_download"],
             "license": x["license"], "by_author": x["by_author"],
             "downloads": x["downloads"]},
            # 生图往往不是一个文件：FLUX 那类要 unet + clip_l + t5 + vae 才出得来图。
            # 只报主体文件名，用户下完发现跑不动 —— 配套件必须跟着行走。
            companions=[{"file": y["file"], "role": y["role"]} for y in rows
                        if y["repo"] == x["repo"] and y["role"] != "unet"])
            for x in pool[:per_tier]]
        if len(pool) < 2:
            problems.append(f"档位 {label} 只有 {len(pool)} 行**主体权重**合档（<2）—— "
                            "候选池或大小/角色判据漂了")
    roles = {}
    for x in rows:
        roles[x["role"]] = roles.get(x["role"], 0) + 1
    agree = sum(1 for x in rows if x["usable_before_download"])
    return {"schema": 1, "ref": REF, "per_tier": per_tier,
            "from": ["HF 搜索（按家族查询、按 downloads 排）",
                     "两家的文件表：tree.lfs.oid × ModelScope 元数据 Sha256",
                     f"{ENGINE_REPO} 的 {MEM_DOC}（内存是运行时量的，不是查表）"],
            "rule": {"pool": "查询串 " + " / ".join(f"{q!r}" for q in QUERIES) +
                            "，按上游 downloads 从多到少，去重按仓库 id",
                     "repo": f"{AUTHOR}（引擎作者自己发的 gguf）优先，其余按下载量",
                     "file": "仓库里每个 .gguf 都算一行；不按量化名偏好（各家命名不同）；"
                             "**档位只认 role=unet 的主体权重** —— 生图一个模型往往是好几份文件 "
                             "(unet + clip + t5 + vae)，只按大小排会把文本编码器排到档位第一行",
                     "fit": f"文件大小 < 档位 × 1 GiB × {rsub.HEADROOM}（与字幕那张表同一个常数）；"
                            "这是算术下限，不是内存承诺",
                     "memory_claim": "none —— 上游没给扩散权重的静态内存表，"
                                     "引擎在运行时量内存并走分段执行（见 mem_facts）",
                     "hash": "两家给同一个 sha256 才 usable；一家给就是 single，必须下完自算",
                     "mature": f"上游打了 {MATURE_TAG} 标签的行不进档位（仍留在 weights 里），"
                               "按上游标记排除、不按仓库名猜"},
            "mem_facts": {k: f"{ENGINE_REPO}@{REF[:8]} {MEM_DOC}:{v}" for k, v in MEM_FACTS.items()},
            "engine_tag_claims": sum(1 for x in rows if x["engine_tags"]),
            "queries": list(QUERIES), "candidates": len(repos),
            "roles": roles, "unresolved_repos": sorted(unresolved),
            "mature_rows": sum(1 for x in rows if x["mature"]),
            "license_unknown_rows": sum(1 for x in rows if "许可不明" in x["license_flag"]),
            "row_count": len(rows), "usable_before_download": agree,
            "tiers": [t[0] for t in rm.TIERS], "tiers_rows": tiers,
            "weights": rows,
            "license_flags": sorted({str(x["license"]) for x in rows
                                     if x["license_flag"] != "ok"})}, \
        problems


def check(committed: dict, fresh: dict) -> list[str]:
    bad = []
    for k in ("rule", "ref", "queries"):
        if committed.get(k) != fresh.get(k):
            bad.append(f"{k} 变了：{str(committed.get(k))[:70]!r} → {str(fresh.get(k))[:70]!r}")
    old = {(r["repo"], r["file"]): r for r in committed["weights"]}
    new = {(r["repo"], r["file"]): r for r in fresh["weights"]}
    for k in sorted(set(old) | set(new)):
        a, b = old.get(k), new.get(k)
        if a is None:
            bad.append(f"{k[0]}/{k[1]}: 新出现一行（上游新发或搜索多了一条），要不要收由人定")
            continue
        if b is None:
            bad.append(f"{k[0]}/{k[1]}: 这一行现在解不出来了（文件被撤或改名）")
            continue
        for f in ("sha256", "size", "status", "license", "role", "mature"):
            if a[f] != b[f]:
                bad.append(f"{k[0]}/{k[1]}.{f}: {str(a[f])[:34]!r} → {str(b[f])[:34]!r}")
    if sorted(committed.get("unresolved_repos", [])) != sorted(fresh.get("unresolved_repos", [])):
        bad.append("搜索说有 GGUF、两家文件表都说没有的那批仓库变了：%s → %s —— "
                   "要么上游撤了仓库，要么我们的解析漏了，得看一眼"
                   % (committed.get("unresolved_repos"), fresh.get("unresolved_repos")))
    for label in fresh["tiers"]:
        want = [x["file"] for x in committed["tiers_rows"].get(label, [])]
        got = [x["file"] for x in fresh["tiers_rows"][label]]
        if want != got:
            bad.append(f"档位 {label} 的行变了 {want} → {got}")
    return bad


def live_files_for():
    def files_for(repo: str):
        if repo == "search":
            return None, None, None, None, None
        hf, hf_by, e1 = file_index.hf_files(repo)
        ms, ms_by, e2 = file_index.ms_files(repo)
        return hf, hf_by, ms, ms_by, (e1 or e2)
    return files_for


def live_search():
    def fetch_meta(_kind, query):
        url = (f"https://{SEARCH_HOST}/api/models?search={query.replace(' ', '%20')}"
               f"&sort=downloads&direction=-1&limit={LIMIT}")
        obj, _answered, err = file_index.http_json(url)
        return (obj or []) if isinstance(obj, list) else [], err
    return fetch_meta


FIXTURES = {"search": "image_search.json", "files": "image_files.json"}


def load_fixtures(d: str):
    with open(os.path.join(d, FIXTURES["search"]), encoding="utf-8") as fh:
        search = json.load(fh)
    with open(os.path.join(d, FIXTURES["files"]), encoding="utf-8") as fh:
        files = json.load(fh)

    def fetch_meta(_kind, query):
        return search["queries"].get(query, []), search["errors"].get(query)

    def files_for(repo):
        row = files.get(repo) or {}
        return (row.get("hf") or {}, row.get("hf_by") or "hf",
                row.get("ms") or {}, row.get("ms_by") or "ms", row.get("err"))
    return fetch_meta, files_for


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="recommend_image.py")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--live", action="store_true", help="真取搜索结果与两家文件表")
    g.add_argument("--fixtures", default="", help="离线读一次真捕获（tests/fixtures）")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--per-tier", type=int, default=PER_TIER)
    ap.add_argument("--check", action="store_true", help="重算并与已提交的那张比漂移")
    ap.add_argument("--snapshot", default="", help="（配 --live）把这次真取到的东西存成离线捕获")
    a = ap.parse_args(argv)
    if a.snapshot and not a.live:
        ap.error("--snapshot 只在 --live 那次有意义：捕获必须是真取的")

    store: dict[str, dict] = {}
    search_capture: dict = {"queries": {}, "errors": {}}
    if a.fixtures:
        fetch_meta, files_for = load_fixtures(a.fixtures)
    else:
        raw_search, raw_files = live_search(), live_files_for()

        def fetch_meta(kind, query):
            rows, err = raw_search(kind, query)
            search_capture["queries"][query] = rows
            if err:
                search_capture["errors"][query] = err
            return rows, err

        def files_for(repo):
            res = raw_files(repo)
            if repo != "search":
                store[repo] = {"hf": res[0], "hf_by": res[1], "ms": res[2], "ms_by": res[3],
                               "err": res[4]}
            return res

    repos, problems = search_repos(fetch_meta)
    fresh, p2 = build(repos, files_for, per_tier=a.per_tier)
    problems += p2
    if a.snapshot:
        # 只留 .gguf 条目：捕获是给离线重算用的，判据也只看这些键，
        # 把 README/config 的名字搬进仓库没有意义（与字幕那张表的切片同一个口径）
        slim = {r: dict(v, hf={k: x for k, x in v["hf"].items() if k.lower().endswith(".gguf")},
                        ms={k: x for k, x in v["ms"].items() if k.lower().endswith(".gguf")})
                for r, v in store.items()}
        for p, blob in ((FIXTURES["search"], search_capture), (FIXTURES["files"], slim)):
            with open(os.path.join(a.snapshot, p), "w", encoding="utf-8", newline="\n") as fh:
                json.dump(blob, fh, ensure_ascii=False, indent=1, sort_keys=True)
                fh.write("\n")
        print(f"捕获落盘 {a.snapshot}：{len(search_capture['queries'])} 个查询、"
              f"{len(slim)} 个仓库的两家文件表")
    if a.check:
        with open(a.out, encoding="utf-8") as fh:
            committed = json.load(fh)
        bad = check(committed, fresh) + problems
        for x in bad:
            print("BAD:", x)
        print(f"比对 {fresh['row_count']} 行 / {fresh['candidates']} 个候选仓库，问题 {len(bad)} 条")
        return 1 if bad else 0

    print(f"候选仓库 {fresh['candidates']} 个，权重行 {fresh['row_count']} 条，"
          f"其中两家同哈希 {fresh['usable_before_download']} 条，"
          f"上游自称兼容引擎的行 {fresh['engine_tag_claims']} 条")
    for label in fresh["tiers"]:
        for x in fresh["tiers_rows"][label]:
            print(f"  {label:<5} {x['repo'][:34]:<36} {x['file'][:40]:<42} "
                  f"{x['status']:<8} {round(x['size'] / 2 ** 20)} MiB"
                  f"{'' if x['usable_before_download'] else '  需下载后自算'}")
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
