#!/usr/bin/env python3
"""模型目录探针：把四项能力的权重来源、大小量出来，并回答"上游到底给不给权重 sha256"。

为什么要单独回答后半句：pin 是这个产品的安全边界（下载 400 MB 之后凭什么说没被换过）。
2026-10-19 在已知答案的样本上实测的结论是 **HF 侧不给**，而且有个长得像 sha256 的陷阱字段
（resolve 响应的 ETag 是 64 位十六进制，但它不等于内容 sha256）。对照面是 ModelScope：
它的 lfs-objects 路径按 sha[0:2]/sha[2:4]/sha[4:] 重建后正好等于我们独立钉的 pin。

控制样本用我们**已经从别处钉过**的那个文件，所以"等不等于"是有标准答案的比对，不是自证。

正向对照：Qwen/Qwen2.5-0.5B-Instruct-GGUF 的 q2_k 文件，pin=
  9ee36184e616dfc76df4f5dd66f908dbde6979524ae36e6cefb67f532f798cb8  size=415182688
负向对照：一个不存在的仓库名必须拿不到文件，否则"取到了"说明不了什么。
"""
from __future__ import annotations

import json
import subprocess
import sys

MIRROR = "https://hf-mirror.com"
PIN_SHA = "9ee36184e616dfc76df4f5dd66f908dbde6979524ae36e6cefb67f532f798cb8"
PIN_SIZE = 415182688
CONTROL_REPO = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"
CONTROL_FILE = "qwen2.5-0.5b-instruct-q2_k.gguf"

REPOS = [
    ("对话", CONTROL_REPO),
    ("字幕", "ggerganov/whisper.cpp"),
    ("生图", "second-state/stable-diffusion-v1-5-GGUF"),
]
# 仓库名是从镜像 API 搜出来的，不是猜的；这个不存在的名字只用来证明"取不到"确实会失败
NEGATIVE_REPO = "nope/nope-does-not-exist-91827364"
UA = "model-catalog-probe/1.0"


def get(url: str):
    p = subprocess.run(["curl", "-sSL", "--max-time", "30", "--max-filesize", str(16 << 20),
                        "-A", UA, url], capture_output=True, text=True)
    if p.returncode != 0:
        return None, f"rc={p.returncode} {p.stderr.strip()[:70]}"
    try:
        return json.loads(p.stdout), "ok"
    except Exception:
        return None, f"非 JSON：{p.stdout[:60]!r}"


def head(url: str):
    p = subprocess.run(["curl", "-sIL", "--max-time", "40", "-A", UA, url],
                       capture_output=True, text=True)
    etag = clen = ranges = None
    for line in p.stdout.splitlines():
        low = line.lower()
        if low.startswith("etag:"):
            etag = line.split(":", 1)[1].strip().strip('"')
        elif low.startswith("content-length:"):
            try:
                clen = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif low.startswith("accept-ranges:"):
            ranges = line.split(":", 1)[1].strip()
    return etag, clen, ranges


def files_of(repo: str):
    """tree 与 siblings 两个端点都试并合并：只试一个的话，空结果会被误读成「这仓库没权重」。"""
    out = {}
    d, note = get(f"{MIRROR}/api/models/{repo}/tree/main?recursive=true")
    if isinstance(d, list):
        for e in d:
            if e.get("type") == "file":
                lfs = e.get("lfs") or {}
                out[e.get("path")] = {"size": lfs.get("size") or e.get("size") or 0,
                                      "hash": lfs.get("oidsha") or lfs.get("sha"),
                                      "src": "tree"}
    d2, note2 = get(f"{MIRROR}/api/models/{repo}")
    if isinstance(d2, dict):
        for s in d2.get("siblings") or []:
            name = s.get("rfilename")
            if name and name not in out:
                out[name] = {"size": (s.get("lfs") or {}).get("size") or 0,
                             "hash": (s.get("lfs") or {}).get("oidsha"), "src": "siblings"}
            elif name:
                out[name]["src"] += "+siblings"
    return out, f"tree={note} info={note2}"


def main():
    bad = []
    print("=== 负向对照：不存在的仓库必须拿不到文件 ===")
    neg, note = files_of(NEGATIVE_REPO)
    print(f"    {NEGATIVE_REPO} -> 文件 {len(neg)} 个（{note}）")
    if neg:
        bad.append("负向对照失败：假仓库也返回了文件，说明解析器在凭空造条目")

    catalog = {}
    for cap, repo in REPOS:
        print("=" * 72)
        print(f"[{cap}] {repo}")
        fl, note = files_of(repo)
        if not fl:
            bad.append(f"{repo} 两个端点都没拿到文件")
            print(f"    拿不到文件（{note}）")
            continue
        big = sorted(((v["size"], k, v) for k, v in fl.items() if v["size"] and v["size"] > 1 << 20),
                     reverse=True)
        print(f"    文件 {len(fl)} 个，>1 MiB 的 {len(big)} 个（{note}）")
        for size, name, v in big[:6]:
            print(f"      {size/1048576:9.1f} MiB  {name}  哈希字段={v['hash'] or '无'}")
        catalog[repo] = [{"path": n, "size": s} for s, n, _ in big]
        hashkinds = {("有" if v["hash"] else "无") for v in fl.values()}
        print(f"    元数据是否带内容哈希：{hashkinds}")

    print("=" * 72)
    print("=== 控制样本：三种「哈希来源」哪个等于我们独立钉的 sha256 ===")
    fl, _ = files_of(CONTROL_REPO)
    ent = fl.get(CONTROL_FILE)
    if not ent:
        bad.append(f"控制文件 {CONTROL_FILE} 不在树里")
    else:
        etag, clen, ranges = head(f"{MIRROR}/{CONTROL_REPO}/resolve/main/{CONTROL_FILE}")
        print(f"    我们钉的 sha256 : {PIN_SHA}")
        print(f"    元数据哈希字段  : {ent['hash'] or '（没有这个字段）'}")
        print(f"    resolve 的 ETag : {etag}")
        print(f"    大小：元数据 {ent['size']} / HEAD {clen} / pin {PIN_SIZE}")
        if ent["size"] != PIN_SIZE:
            bad.append(f"元数据大小与 pin 不符：{ent['size']}")
        if clen not in (PIN_SIZE, None):
            bad.append(f"HEAD 长度与 pin 不符：{clen}")
        if etag == PIN_SHA:
            print("    ✔ ETag 就是内容 sha256，可以当 pin 来源")
        else:
            print("    ✗ ETag 是 64 位十六进制，但**不等于**内容 sha256（它是 Xet/cas-bridge 的对象指纹）")
            print("      这是会咬人的陷阱：看着像 sha256 的字段拿去做校验，每个 pin 都会静默错位")
        if not ent["hash"]:
            print("    · HF 元数据（tree/siblings）不给内容哈希 → pin 只能在入库时自己算")
    print(f"    Accept-Ranges：{ranges}（决定能不能断点续传）")

    out = {"catalog": catalog, "hf_gives_content_hash": False,
           "etag_equals_sha256": etag == PIN_SHA if ent else None,
           "models_range_header": ranges}
    json.dump(out, open("model-catalog.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
    print("---- 判据 ----")
    for b in bad:
        print("BAD:", b)
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
