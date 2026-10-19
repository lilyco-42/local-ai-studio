"""给没有上游哈希的源（HF / 样本）立 pin，并打印这个 pin 的凭据强度到哪一步。

背景：GitHub release 每个产物都带 `digest = sha256:…`（已实测），但 HF 的 tree/siblings 只给
文件名+大小，而 resolve 响应那个 64 位十六进制的 ETag **不是**内容哈希；自家 R2 也只有 Content-Length。
所以从 HF 取的权重必须由我们自己立 pin。

"先试两个独立发布方"不成立，都是实测：ModelScope 上没有 ggerganov/whisper.cpp（404），
HF 上也没有 ggml-org/whisper.cpp 这个模型仓库（401）。因此这里用的是能拿到的最强对照：

  1) 从 hf-mirror 整包下载算 sha256 —— 这就是将来实际会走的下载路径；
  2) 与 HEAD 声明的总长核对（优先 Content-Range，其次 Content-Length；
     上一版只 regex content-range，把"我取法不对"读成了"文件长度不符"）；
  3) 从第二个主机（HF 直连 / GitHub raw 之外的镜像路径）取头 4 MiB 与尾 4 MiB 比哈希
     —— 能证"这条镜像没有改写这份文件"，**但不能证来源独立**；
  4) 小文件再补一道 git blob sha1（git 本身就是内容寻址），与 GitHub contents API 的 sha 对得上。

结论强度必须跟着 pin 一起写进目录：provenance 只有一家的东西，不能假装是多方共识。

用法：python3 tools/establish_pins.py [--json pins.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys

CHUNK = 4 << 20
NUL = bytes([0])

# (显示名, 仓库, 仓库内路径, 分支/rev)
TARGETS = [
    ("whisper ggml-tiny", "ggerganov/whisper.cpp", "ggml-tiny.bin", "main"),
    ("whisper 样本 jfk.wav", "ggml-org/whisper.cpp", "samples/jfk.wav", "master"),
]


def sh(args):
    p = subprocess.run(args, capture_output=True)
    return p.returncode, p.stdout, p.stderr.decode("utf-8", "replace")[:130]


def get(url, rng=None):
    args = ["curl", "-sSL", "--fail", "--max-time", "900", "-A", "establish-pins/1.0"]
    if rng:
        args += ["-r", rng]
    args.append(url)
    return sh(args)


def sources(repo: str, path: str, rev: str):
    """样本只在 GitHub 上（HF 没有这个模型仓库），模型在 HF 家族里 —— 两套 URL 规则分开。"""
    if repo == "ggml-org/whisper.cpp" and path.startswith("samples/"):
        return (f"https://raw.githubusercontent.com/{repo}/{rev}/{path}",
                f"https://github.com/{repo}/raw/{rev}/{path}")
    return (f"https://hf-mirror.com/{repo}/resolve/{rev}/{path}",
            f"https://huggingface.co/{repo}/resolve/{rev}/{path}")


def declared_len(url: str):
    rc, out, _ = sh(["curl", "-sIL", "--max-time", "60", "-A", "establish-pins/1.0", url])
    hdr = out.decode("latin-1")
    cr = [int(x) for x in re.findall(r"(?im)^content-range:\s*bytes\s+\d+-\d+/(\d+)", hdr)]
    cl = [int(x) for x in re.findall(r"(?im)^content-length:\s*(\d+)", hdr)]
    return ((cr or cl or [None])[-1], cr[:1], cl[-1:])


def handle(display, repo, path, rev):
    print("=" * 72)
    print(f"{display}  <- {repo}/{path}@{rev}")
    mirror, direct = sources(repo, path, rev)
    rc, blob, err = get(mirror)
    if rc:
        print(f"  主源取不到 rc={rc} {err}")
        return None
    sha = hashlib.sha256(blob).hexdigest()
    print(f"  主源 {mirror.split('//')[1][:34]} 整包 {len(blob):,} B  sha256={sha}")

    total, cr, cl = declared_len(mirror)
    print(f"  头里声明总长 = {total:,} B（Content-Range {cr} / Content-Length {cl}）"
          if total else "  头里没有长度声明")
    size_ok = total == len(blob)
    print(f"  与实收字节一致 = {size_ok}")

    ok_head = ok_tail = None
    rc, hb, _ = get(direct, f"0-{CHUNK - 1}")
    if rc == 0 and hb:
        ok_head = hashlib.sha256(hb).hexdigest() == hashlib.sha256(blob[:len(hb)]).hexdigest()
    rc, tb, _ = get(direct, f"{max(0, len(blob) - CHUNK)}-{len(blob) - 1}")
    if rc == 0 and tb:
        ok_tail = hashlib.sha256(tb).hexdigest() == hashlib.sha256(blob[-len(tb):]).hexdigest()
    print(f"  第二主机 {direct.split('//')[1][:28]} 头 4 MiB 相同 = {ok_head}；尾 4 MiB 相同 = {ok_tail}")

    blob_sha1 = None
    if len(blob) < (8 << 20):
        blob_sha1 = hashlib.sha1(b"blob " + str(len(blob)).encode() + NUL + blob).hexdigest()
        rc, out, _ = sh(["gh", "api", f"repos/{repo}/contents/{path}", "--jq", ".sha"])
        api = out.decode().strip()
        if api:
            print(f"  git blob sha1 = {blob_sha1}  与 GitHub contents API 一致 = {api == blob_sha1}")

    if size_ok and ok_head and ok_tail:
        return {"sha256": sha, "size": len(blob), "git_blob_sha1": blob_sha1,
                "primary": mirror, "cross_checked_against": direct,
                "provenance": "只有单一发布方（HF 或 GitHub）；本对照证明的是传输与镜像未改写，不是来源独立"}
    print("  → 对照不齐，先不立 pin")
    return None


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    pins = {}
    for display, repo, path, rev in TARGETS:
        p = handle(display, repo, path, rev)
        if p:
            pins[path] = p
    print("=" * 72)
    print("立住的 pin：", list(pins) or "无")
    if args.json:
        json.dump(pins, open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
    return 0 if len(pins) == len(TARGETS) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
