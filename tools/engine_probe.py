#!/usr/bin/env python3
"""引擎产物发现：把三家上游各不相同的命名规则，映射成稳定的 target 表。

为什么不用一套通用启发式：2026-10-18 抓下来的真实名单里——
  llama   llama-b11719-bin-win-cpu-x64.zip / cudart-llama-b11719-bin-ubuntu-cuda-12.8-x64.tar.gz
  whisper whisper-bin-x64.zip（连 OS 名都没有）/ whisper-blas-bin-Win32.zip /
          whisper-cublas-12.4.0-bin-x64.zip（CUDA 构建的旧命名，OS 也不写）/
          whisper-bin-win-cuda-12.4.0-x64.zip
  sd      sd-master-95b3090-bin-Linux-Ubuntu-24.04-x86_64.zip（tag 是 master-975-95b3090，
          文件名里只有 sha，没有 975）/ ...-bin-Darwin-macOS-26.6.2-arm64.zip /
          cudart-sd-bin-win-cu12-x64.zip（CUDA 写成 cu12，不是 cuda-12）
所以每个引擎一张表，改动看得见；通用正则会把它们全塞进 unknown。

另两条实测事实：
  1. `releases/latest` 可能一个产物都没有（whisper 的 v1.9.5/1.9.4/1.9.3 都是 0 个），
     所以一律遍历 release 列表并跳过空产物，不看 latest。
  2. releases 列表按新→旧返回（llama b11719→b11714、sd master-975→master-967 实测如此），
     所以同 target 冲突时先比 release 序号再比后端版本，不能让 CUDA 标签写法
     （13.4.0 比 13.4 "大"）把旧引擎构建顶上来。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request

UPSTREAM = {
    "llama": {"repo": "ggml-org/llama.cpp"},
    "whisper": {"repo": "ggml-org/whisper.cpp"},
    "sd": {"repo": "leejet/stable-diffusion.cpp"},
}

ARCH_CANON = {"x86_64": "x64", "amd64": "x64", "Win32": "x86", "i686": "x86"}
# 桌面端用不到的架构（s390x 是 IBM 主机），直接不进表，别混进 target 里当噪音。
EXCLUDE_ARCH = {"s390x"}
# xcframework 是给 Xcode 工程嵌入的库包，不是能起进程的可执行产物；
# llama 另有 bin-macos-*，whisper 没有——所以 mac 侧字幕只能由 CI 自编，这里不假装它可用。
EXCLUDE_TOKENS = ("xcframework", "-ui.tar.gz")

BACKENDS = ("cuda", "cublas", "cu12", "cu11", "rocm", "vulkan", "openvino", "sycl", "blas",
            "opencl", "adreno", "snapdragon", "metal", "cpu")

# 每条：正则 → (bundle, 强制后端)。强制后端用于名字里后端和版本号分开的写法（whisper cublas）。
PATTERNS = {
    "llama": [
        # 两种都实测存在：cudart-llama-b11719-bin-… 和没有构建号的 cudart-llama-bin-win-…
        # 所以连字符必须写在可选组里面，写成 `cudart-llama-(?:b\d+)?-bin-` 会把后者整批漏掉。
        (re.compile(r"^cudart-llama(?:-b\d+)?-bin-(?P<rest>.+)\.(?:tar\.gz|zip)$"), "cudart", None),
        (re.compile(r"^llama-b\d+-bin-(?P<rest>.+)\.(?:tar\.gz|zip)$"), "main", None),
    ],
    "whisper": [
        # 旧 CUDA 命名：whisper-cublas-<ver>-bin-<arch>.zip，后端写在 bin 之前，rest 里只剩 arch。
        (re.compile(r"^whisper-cublas-(?P<ver>[0-9.]+)-bin-(?P<rest>.+)\.zip$"), "main", ("cuda", "ver")),
        # 同理：whisper-blas-bin-x64.zip 的 blas 也写在 bin 之前。
        (re.compile(r"^whisper-blas-bin-(?P<rest>.+)\.(?:zip|tar\.gz)$"), "main", ("blas", None)),
        (re.compile(r"^whisper-bin-(?P<rest>.+)\.(?:zip|tar\.gz)$"), "main", None),
    ],
    "sd": [
        (re.compile(r"^cudart-sd-bin-(?P<rest>.+)\.(?:zip|tar\.gz)$"), "cudart", None),
        # tag 是 master-975-<sha> 但文件名只带 sha，所以 975- 这段必须可选。
        (re.compile(r"^sd-(?:master-(?:\d+-)?[0-9a-f]{7,40}|[0-9a-f]{7,40})-bin-(?P<rest>.+)\.zip$"),
         "main", None),
    ],
}


def release_rank(tag: str | None) -> int:
    """release 列表新→旧，tag 里第一段数字就是构建号（b11719 / master-975-<sha>）。"""
    m = re.search(r"\d+", tag or "")
    return int(m.group()) if m else -1


def _platform_of(rest: str) -> str:
    low = rest.lower()
    if "darwin" in low or "macos" in low:
        return "macos"
    if "ubuntu" in low or "linux" in low or "debian" in low:
        return "ubuntu"
    if "win32" in low:
        return "win"
    if low.startswith("win-") or "-win-" in low or low.startswith("win"):
        return "win"
    if "android" in low:
        return "android"
    return "unknown"


def _arch_of(rest: str) -> str:
    m = re.search(r"(x86_64|x64|arm64|aarch64|Win32|i686|s390x)\b", rest)
    if not m:
        return "?"
    return ARCH_CANON.get(m.group(1), m.group(1))


def _backend_of(rest: str) -> tuple[str, str | None]:
    m = re.search(r"(?:cuda|cu|rocm|openvino|sycl|cublas|vulkan)-?(\d+(?:\.\d+)*)", rest, re.I)
    ver = m.group(1) if m else None
    for name in BACKENDS:
        if name in rest.lower():
            if name in ("cu12", "cu11"):
                return "cuda", name[2:]
            if name == "cublas":
                return "cuda", ver
            if name == "adreno":
                continue
            if name == "opencl":
                return "opencl", ver
            return name, ver
    return "base", None


def parse_asset(engine: str, name: str) -> dict | None:
    for tok in EXCLUDE_TOKENS:
        if tok in name:
            return None
    for rx, bundle, forced in PATTERNS[engine]:
        m = rx.match(name)
        if not m:
            continue
        g = m.groupdict()
        rest = g.get("rest") or ""
        arch = _arch_of(rest)
        if arch in EXCLUDE_ARCH:
            return None
        plat = _platform_of(rest)
        inferred = False
        if plat == "unknown":
            # whisper 的 `whisper-bin-x64.zip` / `-Win32` / `whisper-cublas-*-bin-x64` 不写 OS。
            # 这个推断只对 whisper 的这几个形状成立，且只用来生成候选——
            # 真凭据由 CI 解压看 .exe/.dll 给，不靠这里的猜测下结论。
            if engine != "whisper":
                return None
            plat = "win"
            inferred = True
        if forced:
            backend = forced[0]
            ver = g.get(forced[1]) if forced[1] else None
        else:
            backend, ver = _backend_of(rest)
        target = f"{engine}-{plat}-{backend}-{arch}"
        if bundle != "main":
            # cudart 只是 CUDA 运行时支持包，和引擎主程序同名 target 会互相顶掉：
            # 2026-10-18 实测 llama-ubuntu-cuda-x64 被 cudart 抢占，装上的是运行时而非引擎。
            target += f"-{bundle}"
        return {
            "asset": name, "platform": plat, "backend": backend, "backend_version": ver,
            "arch": arch, "bundle": bundle, "inferred_platform": inferred, "target": target,
        }
    return None


def _ver(v: str | None) -> tuple:
    return tuple(int(x) for x in v.split(".")) if v else ()


def _better(cand: dict, cur: dict) -> bool:
    if cand["release_rank"] != cur["release_rank"]:
        return cand["release_rank"] > cur["release_rank"]
    return _ver(cand["backend_version"]) > _ver(cur["backend_version"])


def plan(releases: dict) -> dict:
    """releases: {engine: [release,...]}（GitHub 原始结构，按新→旧）。返回 {target: 产物信息}。"""
    out: dict[str, dict] = {}
    for engine, rels in releases.items():
        for rel in rels:
            tag = rel.get("tag_name")
            rank = release_rank(tag)
            for a in rel.get("assets", []):
                info = parse_asset(engine, a["name"])
                if not info:
                    continue
                info.update({"engine": engine, "release": tag, "release_rank": rank,
                             "url": a["browser_download_url"], "size": a["size"]})
                cur = out.get(info["target"])
                if cur is None or _better(info, cur):
                    out[info["target"]] = info
    return out


# 按 (后端, 架构) 要求：上一版只比到后端，`llama-win-cpu-x64` 被 `llama-win-cpu-arm64` 顶过，
# 负向对照删掉 x64 也没报缺失——普通个人电脑要的正是 x64/arm64 那一个具体包。
REQUIRED = {
    "llama": {"win": ("cpu", "x64"), "ubuntu": ("base", "x64"), "macos": ("base", "arm64")},
    # whisper 不发 macOS 单体二进制（只有 xcframework，不能起进程），mac 字幕只能 CI 自编。
    "whisper": {"win": ("base", "x64"), "ubuntu": ("base", "x64")},
    "sd": {"win": ("cpu", "x64"), "ubuntu": ("base", "x64"), "macos": ("base", "arm64")},
}
GPU = {
    "llama": {"win": ("cuda", "x64"), "ubuntu": ("vulkan", "x64")},
    "whisper": {"win": ("cuda", "x64")},
    "sd": {"win": ("cuda", "x64"), "ubuntu": ("vulkan", "x64")},
}


def check(got: dict, need_gpu: bool) -> list[str]:
    bad = []
    for table in ([REQUIRED] + ([GPU] if need_gpu else [])):
        for engine, by_plat in table.items():
            for plat, (backend, arch) in by_plat.items():
                if not any(v["engine"] == engine and v["platform"] == plat
                           and v["backend"] == backend and v["arch"] == arch
                           for v in got.values()):
                    bad.append(f"{engine}/{plat}-{backend}-{arch}")
    return bad


def github_api(path: str) -> object:
    headers = {"User-Agent": "engine-probe", "Accept": "application/vnd.github+json"}
    # 匿名调用 60 次/小时，一条车道跑 3 个仓库 × 两次就快见底了；runner 自带的 GITHUB_TOKEN 能用。
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        headers["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(f"https://api.github.com/{path}", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def manifest_of(got: dict) -> dict:
    """CI 直接按这份结构取产物：inferred 是"OS 靠猜、必须解压证实"的 target 清单，
    车道要遍历的正是它，不能拿一个本来就写了 win 的产物去当证据。"""
    return {
        "targets": got,
        "counts": {e: sum(1 for v in got.values() if v["engine"] == e) for e in UPSTREAM},
        "inferred": sorted(k for k, v in got.items() if v["inferred_platform"]),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="打真实 GitHub API")
    ap.add_argument("--fixtures", default="", help="离线模式：含 <engine>.json 的目录")
    ap.add_argument("--require-gpu", action="store_true", help="额外要求 cuda/vulkan 族可解析")
    ap.add_argument("--json", default="", help="把 manifest 写到文件")
    args = ap.parse_args(argv)

    releases = {}
    if args.live:
        for engine, meta in UPSTREAM.items():
            releases[engine] = github_api(f"repos/{meta['repo']}/releases?per_page=6")
    else:
        if not args.fixtures:
            raise SystemExit("离线模式需要 --fixtures")
        for engine in UPSTREAM:
            releases[engine] = json.load(open(os.path.join(args.fixtures, f"{engine}.json"), encoding="utf-8"))

    got = plan(releases)
    manifest = manifest_of(got)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=1, sort_keys=True)
    for engine in UPSTREAM:
        names = sorted(v["target"] for v in got.values() if v["engine"] == engine)
        print(f"{engine}: {len(names)} 个 target -> {', '.join(names[:8])}{' …' if len(names) > 8 else ''}")
    if manifest["inferred"]:
        print(f"OS 靠推断（待 CI 解压证实）：{manifest['inferred']}")
    bad = check(got, args.require_gpu)
    if bad:
        print(f"NOT RESOLVED: {bad}", file=sys.stderr)
        return 1
    print(f"OK：{len(got)} 个 target 可解析，必需项全命中")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
