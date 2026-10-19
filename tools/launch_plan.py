#!/usr/bin/env python3
"""启动计划：外壳该用哪条命令行起每一项能力 —— 每个旗标都要按钉住的 ref 查到过。

为什么单独要这一件事：验收第 10 条说"外壳持有唯一监听口、自己发 token、UI 永不直连引擎端口"，
可这三条能不能成立取决于**引擎自己支持什么**。本轮全部按钉住的 ref 读上游源码量清楚了：

  * `llama-server` 有 `--api-key`（`common/arg.cpp:3513`），且 `/health` 明写是
    "public endpoint (no API key check)"（`tools/server/server.cpp:263`）→ 带钥匙也能探活；
  * `whisper-server` 只有 `--host/--port`（`examples/server/server.cpp:251-252`），
    **没有任何鉴权旗标**；
  * `sd-server` 只有 `--listen-ip/--listen-port`（`examples/server/runtime.cpp:193/198`），
    同样没有鉴权；
  * OCR（oar-ocr）没有服务形态，而且是 **子命令** CLI：`oar ocr <图片…>`
    （`oar-ocr-cli/src/args.rs:10/20-54`）。更关键的是 `--det/--rec/--dict` 三颗
    **必须一起给**（`args.rs:59-66` 的 `requires_all`），而默认档 `--size` 与这三颗
    **互斥**（`args.rs:84` `conflicts_with_all`）—— 我们传自己钉住的权重，就绝对不能
    再带 `--size`，否则 clap 直接拒。

所以"唯一监听口"这件事对三项能力是**外壳的责任，不是引擎的开关**：whisper/sd 谁都能连，
只能靠我们把它们绑在 127.0.0.1 上并且只让外壳访问。产物里 `engine_auth` 就是把这条事实
写成数据，UI 与实现都不许以为"起了 server 就自带鉴权"。

生图那一项还要多一步：GGUF 的 flux / sd3.5 主体权重之外必须把 `--vae/--clip_l/--t5xxl`
一起给（依据 `docs/sd.md:16/17`，那五行已经进了出处表），而 SD1.5 那颗只要 `-m`。
这条**不靠人记**：直接读 `catalog/image-tiers.json` 的 `completeness` 与
`catalog/machine-budget.json` 里算进预算的那几颗配套件 —— **下的和用的必须是同一批文件**，
否则预算说 20.82 GiB、命令行却少传一份编码器。

用法：`--selftest` 先证明判据本身会红；`--check` 把已提交那张表按输入重算一遍比漂移；
`--cache/--fill` 与出处闸门共用一套上游原文缓存。
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import citation_check  # 复用"按 pin 住的 ref 取上游原文"那套（含缓存）  # noqa: E402

OUT = os.path.join(ROOT, "pins", "launch-plan.json")
LLAMA = ("ggml-org/llama.cpp", "b11727")
WHISPER = ("ggml-org/whisper.cpp", "b5454")
SD = ("leejet/stable-diffusion.cpp", "95b3090664d019fcf8fa032a44d1d4793ba244b8")
OCR = ("GreatV/oar-ocr", "84817f58")
BIND = "127.0.0.1"           # 唯一允许引擎绑的地址（第 10 条：UI 永不直连）

# 每项能力的规格。`verify` 里的每条 needle 都必须在钉住的 ref 的那份文件里出现，
# `absent` 里的则必须**不**出现（"whisper 没有鉴权"也得是可证的，不能是一句印象）。
SPEC = {
    "对话": {
        "mode": "server", "engine": "llama", "repo": LLAMA,
        "binary": {"win-x64": "llama-server.exe", "linux-x64": "llama-server",
                   "macos-arm64": "llama-server"},
        "bind": ["--host", BIND, "--port", "{port}"],
        "model": ["-m", "{model}"],
        "auth": {"engine_supports_api_key": True,
                 "argv": ["--api-key", "{token}"],
                 "note": "llama 有 --api-key；即便这样外壳仍是唯一监听口"},
        "readiness": {"method": "GET", "path": "/health", "expect_status": 200,
                      "cite": "llama-health-is-public",
                      "note": "上游明写这是 public endpoint（不查 api-key），所以带钥匙也能探活"},
        "request": {"method": "POST", "path": "/v1/chat/completions",
                    "cite": "llama-chat-completions-route"},
        "verify": [("--api-key", "common/arg.cpp"), ('{"-m", "--model"}', "common/arg.cpp"),
                   ("--port", "common/arg.cpp"), ('"/health"', "tools/server/server.cpp")],
        "absent": [],
    },
    "字幕": {
        "mode": "server", "engine": "whisper", "repo": WHISPER,
        "binary": {"win-x64": "whisper-server.exe", "linux-x64": "whisper-server",
                   "macos-arm64": None},        # 上游 Apple 侧只有 xcframework，没有可执行文件
        "bind": ["--host", BIND, "--port", "{port}"],
        "model": ["-m", "{model}"],
        "auth": {"engine_supports_api_key": False, "argv": [],
                 "note": "whisper-server 没有任何鉴权旗标 —— 绑 127.0.0.1 是唯一防线"},
        "readiness": {"method": "GET", "path": "/health", "expect_status": 200,
                      "cite": ["ws-health-loading", "ws-health-ok"],
                      "note": "加载期回 503 + loading model，就绪体才是 {\"status\":\"ok\"}"},
        "request": {"method": "POST", "path": "/inference", "multipart_field": "file",
                    "cite": ["whisper-inference-route", "ws-file-field"]},
        "verify": [('arg == "--host"', "examples/server/server.cpp"),
                   ('arg == "--port"', "examples/server/server.cpp"),
                   ('arg == "-m"', "examples/server/server.cpp"),
                   ('"/health"', "examples/server/server.cpp")],
        "absent": ["--api-key", "api_key"],
    },
    "生图": {
        "mode": "server", "engine": "sd", "repo": SD,
        "binary": {"win-x64": "sd-server.exe", "linux-x64": "sd-server",
                   "macos-arm64": "sd-server"},
        "bind": ["--listen-ip", BIND, "--listen-port", "{port}"],
        "model": ["-m", "{model}"],
        # 配套件角色 → 上游旗标（`examples/common/common.cpp` 里逐个查到）。这里必须是**唯一的**
        # 一份映射：model_argv() 只从这张表取旗标，写第二份就会有两处漂移。
        "model_flags": {"unet": "--diffusion-model", "VAE": "--vae",
                        "clip_l": "--clip_l", "clip_g": "--clip_g", "t5xxl": "--t5xxl"},
        "auth": {"engine_supports_api_key": False, "argv": [],
                 "note": "sd-server 只有 --listen-ip/--listen-port，没有鉴权"},
        "readiness": {"method": "GET", "path": "/v1/models", "expect_status": 200,
                      "cite": "sd-listen-after-load",
                      "note": "与 whisper 相反：模型先加载完才 listen，能答就是就绪"},
        "request": {"method": "POST", "path": "/sdcpp/v1/img_gen", "expect_status": 202,
                    "poll": "poll_url", "cite": "sd-imggen-202"},
        "verify": [('"--listen-ip"', "examples/server/runtime.cpp"),
                   ('"--listen-port"', "examples/server/runtime.cpp"),
                   ('"/v1/models"', "examples/server/routes_openai.cpp")] +
                  [(f'"{f}"', "examples/common/common.cpp") for f in
                   ("--diffusion-model", "--vae", "--clip_l", "--clip_g", "--t5xxl", "-m")],
        "absent": ["--api-key"],
    },
    "文字识别": {
        "mode": "subprocess", "engine": "oar-ocr", "repo": OCR,
        "binary": {"win-x64": "oar.exe", "linux-x64": "oar", "macos-arm64": "oar"},
        "bind": [], "subcommand": "ocr",
        "model": ["--det", "{det}", "--rec", "{rec}", "--dict", "{dict}"],
        # 上游把这三颗写成 requires_all，并把默认档 --size 与它们 conflicts_with_all：
        # 我们给的是自己钉住的权重文件，所以 --size 必须**不出现**（传了直接被 clap 拒）。
        "omit": ["--size"],
        "auth": {"engine_supports_api_key": False, "argv": [],
                 "note": "没有服务形态：一次任务一个进程，所以既没有端口也没有鉴权问题"},
        "readiness": None,
        "request": {"argv": ["--device", "{device}", "--format", "json",
                             "--output", "{out_dir}", "{image}"],
                    "cite": ["oar-images-positional-required", "oar-output-is-dir",
                              "oar-device-default-auto", "oar-subcommand-ocr"],
                    "note": "位置参数是图片/PDF（num_args=1..，可多张）；--output 是目录，"
                            "每张写一个 .md/.json；不给才打到 stdout"},
        "verify": [('name = "oar"', "oar-ocr-cli/src/args.rs"),
                   ('pub(crate) command: Command', "oar-ocr-cli/src/args.rs"),
                   ('Ocr(Ocr),', "oar-ocr-cli/src/args.rs"),
                   ('pub(crate) det: Option<PathBuf>', "oar-ocr-cli/src/args.rs"),
                   ('pub(crate) rec: Option<PathBuf>', "oar-ocr-cli/src/args.rs"),
                   ('pub(crate) dict: Option<PathBuf>', "oar-ocr-cli/src/args.rs"),
                   ('requires_all = ["rec", "dict"]', "oar-ocr-cli/src/args.rs"),
                   ('conflicts_with_all = ["det", "rec", "dict"]', "oar-ocr-cli/src/args.rs"),
                   ('value_name = "INPUTS"', "oar-ocr-cli/src/args.rs"),
                   ('value_name = "DIR"', "oar-ocr-cli/src/args.rs"),
                   ('--format', "oar-ocr-cli/src/args.rs")],
        "absent_paths": ["oar-ocr-cli/src/main.rs"],
        "absent": ["--api-key", "api_key", "--listen", "--port", "--host"],
    },
}


def needle_present(text: str, needle: str) -> bool:
    """C++/Rust 源码里 `{"--port", …}` 会被拆行写，所以先把源码的引号间隙压平再找。"""
    flat = " ".join(text.split())
    return needle in text or needle in flat


def verify_spec(cap: str, spec: dict, cache: str, fill: bool, stats: dict | None = None) -> list[str]:
    """每个旗标都要在钉住的 ref 里查到；`absent` 的必须查不到（缺席也要能证）。

    `stats` 不是给判据用的，是给**输出**用的：车道要能说出这轮是真去上游查了，
    还是躺在缓存里没动 ——「核对过」与「没报错」不是一回事。
    """
    repo, ref = spec["repo"]
    bad, seen_absent = [], {}
    paths = {p for _n, p in spec["verify"]} | set(spec.get("absent_paths") or [])
    if stats is not None:
        for k, v in (("needles", len(spec["verify"])),
                     ("absent_claims", len(spec.get("absent") or [])),
                     ("files", len(paths)), ("cached", 0), ("fetched", 0)):
            stats[k] = stats.get(k, 0) + v
    for path in paths:
        cite = {"id": f"{cap}:{path}", "repo": repo, "ref": ref, "path": path,
                "line": 1, "expect": "x"}
        text = citation_check.read_cache(cite, cache) if cache else None
        if text is None:
            text = citation_check.fetch(cite)
            if text is None:
                bad.append(f"{cap}: 取不到上游 {repo}@{ref[:8]} {path} → 无法核对旗标")
                continue
            if stats is not None:
                stats["fetched"] = stats.get("fetched", 0) + 1
            if fill and cache:
                citation_check.write_cache(cite, cache, text)
        elif stats is not None:
            stats["cached"] = stats.get("cached", 0) + 1
        seen_absent[path] = text
    for needle, path in spec["verify"]:
        text = seen_absent.get(path)
        if text is not None and not needle_present(text, needle):
            bad.append(f"{cap}: 旗标 {needle!r} 在 {repo}@{ref[:8]} 的 {path} 里查不到 "
                       "—— 上游改了命令行，启动计划要重写")
    for needle in spec.get("absent") or []:
        hit = [p for p, t in seen_absent.items() if needle_present(t, needle)]
        if hit:
            bad.append(f"{cap}: 以为 {needle!r} 不存在，其实在 {hit} 里查到了 "
                       "—— 引擎鉴权这件事的结论要重看")
    return bad


def model_argv(cap: str, tier: str, image: dict, budget: dict) -> tuple[list[str], list[str]]:
    """把"要用哪些文件"从档位表与预算里读出来，而不是在这里手写一份。

    两张表都得说话：`catalog/machine-budget.json` 说预算里真算进了哪几颗，
    `catalog/image-tiers.json` 说这一档的默认行是哪颗、那一族需要几份编码器。
    两边不一致就是**下的与用的不是同一批文件**，当场报，不选一边。
    模板里写的是**文件名**（`{flux1-schnell-Q5_0.gguf}`），外壳按名字换成落地路径 ——
    这样"这一行起的是哪一颗"是可核对的，而不是一个谁都能填的 `{model}`。
    """
    spec = SPEC[cap]
    if cap != "生图":
        return list(spec["model"]), []
    flags = spec["model_flags"]
    blk = budget["per_tier"][tier]
    unet = next((i for i in blk["items"] if i.get("role") == "unet"), None)
    if unet is None:
        return list(spec["model"]), [f"{tier} 档没有生图默认行"]
    comps = [i for i in blk["items"] if i.get("companion_of")]
    needs = (blk["image_companions"] or {}).get("needs") or []
    rows = (image.get("tiers_rows") or {}).get(tier) or []
    problems = []
    if rows:
        trow = rows[0]
        if trow.get("file") != unet["file"]:
            problems.append(f"{tier}: 档位表默认行是 {trow.get('file')!r}，预算里的主体却是 "
                            f"{unet['file']!r} —— 两张表已经不是同一颗权重")
        tneeds = sorted((trow.get("completeness") or {}).get("needs") or [])
        if tneeds != sorted(needs):
            problems.append(f"{tier}: 档位表说这一族需要 {tneeds}，预算却按 {sorted(needs)} 算的 "
                            "—— 命令行会缺参数或多参数")
    argv = [flags["unet"] if needs else spec["model"][0], "{" + unet["file"] + "}"]
    for need in needs:
        if need not in flags:
            problems.append(f"{tier}/{need}: 预算里有一颗配套件没有对应旗标 —— 上游旗标名要重查")
            continue
        comp = next((c for c in comps if c.get("need_key") == need), None)
        if comp is None:
            problems.append(f"{tier}/{need}: 档位表说这一族需要它，可预算里没有这颗配套件 "
                            "—— 命令行会缺参数（下的和用的必须同一批文件）")
            continue
        argv += [flags[need], "{" + comp["file"] + "}"]
    return argv, problems


def cites_of(spec: dict) -> list[str]:
    """把 readiness/request 引用的出处 id 收平（写成 str 或 list 都行）。"""
    out = []
    for block in (spec.get("readiness"), spec.get("request")):
        c = (block or {}).get("cite")
        out += c if isinstance(c, list) else ([c] if c else [])
    return sorted(set(out))


def build(os_labels: list[str], tiers: list[str], cache: str = "", fill: bool = False,
          check_provenance: bool = True, stats: dict | None = None) -> tuple[dict, list[str]]:
    with open(os.path.join(ROOT, "catalog", "image-tiers.json"), encoding="utf-8") as fh:
        image = json.load(fh)
    with open(os.path.join(ROOT, "catalog", "machine-budget.json"), encoding="utf-8") as fh:
        budget = json.load(fh)
    # 出处 id 必须真的在出处表里：本轮就写过两个表里根本没有的 id，
    # 而那时"引用了出处"这件事看起来和"核对过出处"一模一样。
    with open(os.path.join(ROOT, "pins", "citations.json"), encoding="utf-8") as fh:
        cited = {r["id"] for r in json.load(fh)}
    plan, problems = {}, []
    for cap, spec in SPEC.items():
        for cid in cites_of(spec):
            if cid not in cited:
                problems.append(f"{cap}: 引用了出处 {cid!r}，可 pins/citations.json 里没有这条 "
                                "—— 等于没查过")
        if check_provenance:
            problems += verify_spec(cap, spec, cache, fill, stats)
        entry = {"mode": spec["mode"], "engine": spec["engine"],
                 "repo": f"{spec['repo'][0]}@{spec['repo'][1]}",
                 # 每一项都 deepcopy：不复制的话产物里的一个字段就是 SPEC 里那同一个对象，
                 # 谁改了产物就把规格改了（测试先撞上过一次，这里当场焊死）。
                 "binaries": copy.deepcopy(spec["binary"]), "bind": list(spec["bind"]),
                 "engine_auth": copy.deepcopy(spec["auth"]),
                 "readiness": copy.deepcopy(spec["readiness"]),
                 "request": copy.deepcopy(spec["request"]),
                 "subcommand": spec.get("subcommand"), "omit": list(spec.get("omit") or []),
                 "companion_flags": copy.deepcopy(spec.get("model_flags")),
                 "cites": cites_of(spec),
                 "flags_verified": [{"needle": n, "path": p} for n, p in spec["verify"]],
                 "flags_absent": list(spec.get("absent") or [])}
        if spec["mode"] == "server":
            entry["by_tier"] = {}
            for tier in tiers:
                for os_label in os_labels:
                    if spec["binary"].get(os_label) is None:
                        continue
                    argv, p = model_argv(cap, tier, image, budget)
                    problems += p
                    tail = (spec["auth"]["argv"] if spec["auth"]["argv"] else [])
                    entry["by_tier"][f"{tier}/{os_label}"] = \
                        ["{bin}"] + argv + spec["bind"] + tail
            if cap == "字幕":
                entry["macos_note"] = ("上游 Apple 侧只有 xcframework（无 whisper-cli 可执行文件）："
                                       "要么 CI 自己编，要么这台机器不做字幕（#29）")
        else:
            head = ["{bin}"] + ([spec["subcommand"]] if spec.get("subcommand") else [])
            one = head + spec["model"] + list(spec["request"]["argv"])
            for f in spec.get("omit") or []:
                if f in one:
                    problems.append(f"{cap}: 命令行里带了 {f} —— 上游把它与钉住权重的三颗设成互斥，"
                                    "带了就直接被 clap 拒")
            entry["argv_template"] = one
        plan[cap] = entry
    doc = {"schema": 1, "bind_address": BIND,
           "rule": {"listener": "每一项能力都只绑 127.0.0.1；外壳是唯一监听口，UI 只连外壳",
                    "auth": "engine_auth 是上游事实：只有 llama 有 --api-key，whisper/sd 没有，"
                            "所以那两项的隔离只能靠绑地址，不能指望引擎自带门禁",
                    "image_model_args": "生图给主体还是主体+编码器，两处一起读：image-tiers 的 "
                                        "completeness.needs 说这一族要几份，machine-budget 的 items "
                                        "说预算里真算进了哪几颗；两张表对不上就报，不选一边。"
                                        "模板里写的是文件名（{flux1-schnell-Q5_0.gguf}），"
                                        "所以「起的正是下的那一颗」是可核对的",
                    "provenance": "每个 needle 都按钉住的 ref 现查（absent 也要查不到才算数）"},
           "capabilities": plan}
    return doc, problems


def check(committed: dict, fresh: dict) -> list[str]:
    bad = []
    if committed.get("rule") != fresh.get("rule"):
        bad.append("启动口径变了（谁绑哪个地址、鉴权怎么办、生图给不给编码器），实现与文档要一起重看")
    for cap, spec in fresh["capabilities"].items():
        old = committed.get("capabilities", {}).get(cap)
        if not old:
            bad.append(f"{cap}: 新增了一项能力，但已提交那张表里没有")
            continue
        for key in ("mode", "bind", "engine_auth", "readiness", "request", "by_tier",
                    "argv_template", "subcommand", "omit", "companion_flags", "cites",
                    "flags_verified", "flags_absent"):
            if key in spec and old.get(key) != spec[key]:
                bad.append(f"{cap}.{key}: {str(old.get(key))[:60]!r} → {str(spec[key])[:60]!r}")
        if old.get("binaries") != spec["binaries"]:
            bad.append(f"{cap}: 二进制名字变了 {old.get('binaries')} → {spec['binaries']}")
    return bad


def selftest() -> int:
    """离线对照：明知上游没有的东西必须被认成没有，明知有的必须找得到。"""
    bad = []
    # needle_present 不能是"恒真"：空文本与不含该旗标的文本都必须找不到。
    if needle_present("", "--port"):
        bad.append("needle_present 对空文本返回真 —— 整个出处核对就是装饰")
    if needle_present('x = {"-m", "--model"};', "--api-key"):
        bad.append("needle_present 在任何输入下都返回真？")
    # 反向：跨行写的旗标必须靠压平找到，否则 verify_spec 天天假红。
    if not needle_present('x =\n    {"-m",\n     "--model"},', '{"-m", "--model"}'):
        bad.append("跨行写的旗标认不出来 —— verify_spec 会天天假红")
    if not SPEC["字幕"]["auth"]["engine_supports_api_key"] is False:
        bad.append("whisper 的鉴权结论被写成真 —— 与上游事实相反")
    if cites_of({"readiness": {"cite": "a"}, "request": {"cite": ["c", "b"]}}) != ["a", "b", "c"]:
        bad.append("cites_of 没把 str 与 list 两种写法收平 —— 出处 id 的核对会漏一半")
    if cites_of({"readiness": None, "request": {}}) != []:
        bad.append("cites_of 对没有引用的规格返回了东西 —— 空与有会混在一起")
    if SPEC["对话"]["auth"]["argv"][:1] != ["--api-key"]:
        bad.append("llama 的 --api-key 没进 argv 模板 —— 验收第 10 条第 ② 步就落不了地")
    argv, problems = model_argv("生图", "16GB",
                                {"tiers_rows": {}},
                                {"per_tier": {"16GB": {
                                    "image_companions": {"needs": ["clip_l", "t5xxl", "VAE"]},
                                    "items": [{"role": "unet", "file": "f.gguf"},
                                              {"companion_of": "f.gguf", "need_key": "clip_l",
                                               "file": "clip_l-Q8_0.gguf"},
                                              {"companion_of": "f.gguf", "need_key": "t5xxl",
                                               "file": "t5xxl-Q2_K.gguf"},
                                              {"companion_of": "f.gguf", "need_key": "VAE",
                                               "file": "ae-f16.gguf"}]}}})
    if argv[0] != "--diffusion-model" or argv.count("--t5xxl") != 1 or problems:
        bad.append(f"flux 那一档的命令行没凑齐三份编码器：{argv} / {problems}")
    # 旗标必须来自那张表而不是代码里另写一份：改表就要改命令行。
    saved = SPEC["生图"]["model_flags"]["t5xxl"]
    try:
        SPEC["生图"]["model_flags"]["t5xxl"] = "--t5XXL-from-table"
        argv_t, _p = model_argv("生图", "16GB", {}, {
            "per_tier": {"16GB": {"image_companions": {"needs": ["t5xxl"]},
                                  "items": [{"role": "unet", "file": "f.gguf"},
                                            {"companion_of": "f.gguf", "need_key": "t5xxl",
                                             "file": "t5xxl.gguf"}]}}})
        if "--t5XXL-from-table" not in argv_t:
            bad.append("生图旗标是代码里手写的第二份 —— 上游改名时表与命令行会各走一边")
    finally:
        SPEC["生图"]["model_flags"]["t5xxl"] = saved
    argv2, p2 = model_argv("生图", "4GB", {"tiers_rows": {}},
                           {"per_tier": {"4GB": {"image_companions": {"needs": []},
                                                 "items": [{"role": "unet", "file": "sd.gguf"}]}}})
    if argv2 != ["-m", "{sd.gguf}"] or p2:
        bad.append(f"自包含的那档还硬塞编码器旗标：{argv2} / {p2}")
    argv3, p3 = model_argv("生图", "4GB", {"tiers_rows": {}},
                           {"per_tier": {"4GB": {
                               "image_companions": {"needs": ["clip_l", "t5xxl", "VAE"]},
                               "items": [{"role": "unet", "file": "x.gguf"},
                                         {"companion_of": "x.gguf", "need_key": "clip_l",
                                          "file": "c.gguf"}]}}})
    if not any("预算里没有这颗配套件" in x for x in p3):
        bad.append(f"下的与用的不一致时没报：{argv3} / {p3}")
    trow_bad = {"tiers_rows": {"8GB": [{"file": "另一颗.gguf",
                                      "completeness": {"needs": ["clip_l", "t5xxl", "VAE"]}}]}}
    _a5, p5 = model_argv("生图", "8GB", trow_bad, {
        "per_tier": {"8GB": {"image_companions": {"needs": ["clip_l", "t5xxl", "VAE"]},
                             "items": [{"role": "unet", "file": "x.gguf"},
                                       {"companion_of": "x.gguf", "need_key": "clip_l",
                                        "file": "c.gguf"},
                                       {"companion_of": "x.gguf", "need_key": "t5xxl",
                                        "file": "t.gguf"},
                                       {"companion_of": "x.gguf", "need_key": "VAE",
                                        "file": "v.gguf"}]}}})
    if not any("两张表已经不是同一颗权重" in x for x in p5):
        bad.append(f"档位表默认行与预算主体不一致时没报：{p5}")
    trow_need = {"tiers_rows": {"8GB": [{"file": "x.gguf",
                                       "completeness": {"needs": ["clip_l"]}}]}}
    _a6, p6 = model_argv("生图", "8GB", trow_need, {
        "per_tier": {"8GB": {"image_companions": {"needs": ["clip_l", "VAE"]},
                             "items": [{"role": "unet", "file": "x.gguf"},
                                       {"companion_of": "x.gguf", "need_key": "clip_l",
                                        "file": "c.gguf"},
                                       {"companion_of": "x.gguf", "need_key": "VAE",
                                        "file": "v.gguf"}]}}})
    if not any("命令行会缺参数或多参数" in x for x in p6):
        bad.append(f"两张表的 needs 数量不一致时没报：{p6}")
    argv4, p4 = model_argv("生图", "8GB", {}, {
        "per_tier": {"8GB": {"image_companions": {"needs": ["clip_q"]},
                             "items": [{"role": "unet", "file": "x.gguf"},
                                       {"companion_of": "x.gguf", "need_key": "clip_q",
                                        "file": "q.gguf"}]}}})
    if not any("没有对应旗标" in x for x in p4):
        bad.append(f"预算里冒出表上没有的角色时没报：{argv4} / {p4}")
    print("---- 结论 ----  对话 argv 模板含鉴权旗标 =",
          "--api-key" in " ".join(SPEC["对话"]["auth"]["argv"]),
          "；生图 16GB 会传三份编码器 =", "--t5xxl" in " ".join(argv))
    for x in bad:
        print("  BAD:", x)
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="launch_plan.py")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check", action="store_true", help="重算并与已提交那张比漂移")
    ap.add_argument("--skip-provenance", action="store_true",
                    help="不去上游取原文（离线复算用；车道不加这条，必须真查）")
    ap.add_argument("--os", default="win-x64,linux-x64,macos-arm64")
    ap.add_argument("--tiers", default="4GB,8GB,16GB")
    ap.add_argument("--cache", default="")
    ap.add_argument("--fill", action="store_true", help="配合 --cache：缺的原文抓下来存盘")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    if a.skip_provenance:
        # 这行不是装饰：车道靠"输出里没有这句"确认自己真的查了上游，
        # 免得哪天有人图快加了 --skip-provenance 而结论还是绿的。
        print("NOTE: --skip-provenance —— 本轮没有向钉住的 ref 核对任何旗标")
    stats: dict = {}
    fresh, problems = build([s.strip() for s in a.os.split(",") if s.strip()],
                            [s.strip() for s in a.tiers.split(",") if s.strip()],
                            a.cache, a.fill, not a.skip_provenance, stats)
    print(f"核对 {stats.get('needles', 0)} 条 needle / {stats.get('absent_claims', 0)} 条缺席声明，"
          f"取自 {stats.get('files', 0)} 份上游原文（缓存 {stats.get('cached', 0)} / 现抓 "
          f"{stats.get('fetched', 0)}）")
    if a.check:
        with open(a.out, encoding="utf-8") as fh:
            committed = json.load(fh)
        bad = check(committed, fresh) + problems
        for x in bad:
            print("BAD:", x)
        print(f"比对了 {len(fresh['capabilities'])} 项能力，问题 {len(bad)} 条")
        return 1 if bad else 0
    for x in problems:
        print("BAD:", x)
    if problems:
        print("有 BAD → 不落盘")
        return 1
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(fresh, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    for cap, spec in fresh["capabilities"].items():
        one = (spec.get("by_tier") or {}).get("16GB/win-x64") or spec.get("argv_template")
        print(f"  {cap:5} {spec['mode']:<10} 引擎鉴权={str(spec['engine_auth']['engine_supports_api_key']):5}"
              f" 例：{' '.join(one) if one else '—'}")
    print("落盘", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
