"""立权重 pin，并打印每条 pin 的凭据强度到哪一步。

三家源的实测差别（决定走哪条路）：
  · GitHub release：产物 API 里带 `digest = sha256:…`，下载验证过就是内容哈希 → 直接可钉；
  · HF / hf-mirror：tree 与 siblings 只给文件名+大小；resolve 的 `ETag` 是 64 位十六进制
    但**不是**内容哈希（Xet/cas-bridge 指纹，长得极像 sha256，是最危险的假朋友）；
  · ModelScope：`repo/files` 元数据每个文件带 `Sha256`，且 CDN 重定向路径本身就是
    `lfs-objects/<sha[0:2]>/<sha[2:4]>/<sha[4:]>` —— 两条独立机制给的哈希一致，
    总长又能与 HF 的 `lfs.size` 对上。**所以 HF 上的权重不必只能自算**，
    一个字节都不用下载就能拿到上游哈希（本轮实测，用于 >1 GB 的扩散权重）。

于是有两条路子，都保留：
  A `modelscope`：零下载，靠上游元数据 + 内容寻址路径 + 跨源总长对照；
  B `download`：整包算 sha256 + 第二主机头尾 4 MiB 逐字节对照（whisper 权重/样本用这条）。

B 路的强度必须如实写：它只证明传输与镜像未改写，**不证明来源独立**
（想找两个独立发布方时实测过：ModelScope 没有 ggerganov/whisper.cpp，HF 也没有 ggml-org 模型仓库）。
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

# (显示名, 路子, 仓库, 仓库内路径, 分支/rev)
TARGETS = [
    ("whisper ggml-tiny", "download", "ggerganov/whisper.cpp", "ggml-tiny.bin", "main"),
    ("whisper 样本 jfk.wav", "download", "ggml-org/whisper.cpp", "samples/jfk.wav", "master"),
    # 1.5 GB 的东西不该为了立 pin 就下一遍，所以这条走零下载的路子
    ("生图 SD1.5 Q4_0", "modelscope", "second-state/stable-diffusion-v1-5-GGUF",
     "stable-diffusion-v1-5-pruned-emaonly-Q4_0.gguf", "master"),
]


def via_modelscope(display, repo, path, rev):
    """零下载立 pin：ModelScope 元数据的 Sha256 + 它 CDN 路径里的内容寻址哈希 + 跨源总长对照。
    两条机制若不一致就是有问题，绝不能挑一个好看的用。"""
    print("=" * 72)
    print(f"{display}  <- ModelScope {repo}/{path}@{rev}（不下载整包）")
    rc, out, err = sh(["curl", "-sS", "--max-time", "60", "-A", "establish-pins/1.0",
                       f"https://www.modelscope.cn/api/v1/models/{repo}/repo/files?Revision={rev}&Root=."])
    if rc or not out.strip():
        print("  文件元数据取不到", (err or "")[:110])
        return None
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except Exception:
        print("  元数据不是 JSON：", out[:100])
        return None
    files = {f.get("Path"): f for f in (data.get("Data") or {}).get("Files", [])}
    ent = files.get(path)
    if not ent:
        print(f"  ModelScope 这个仓库里没有 {path}（同目录有 {list(files)[:4]}）→ 这条源不适用")
        return None
    ms_sha, ms_size = ent.get("Sha256"), ent.get("Size")
    print(f"  元数据 Sha256 = {ms_sha}  Size = {ms_size:,}  IsLFS = {ent.get('IsLFS')}")

    rc, eff, _ = sh(["curl", "-sIL", "--max-time", "60", "-A", "establish-pins/1.0", "-o", "/dev/null",
                     "-w", "%{url_effective}",
                     f"https://www.modelscope.cn/api/v1/models/{repo}/repo?Revision={rev}&FilePath={path}"])
    m = re.search(rb"lfs-objects/([0-9a-f]{2})/([0-9a-f]{2})/([0-9a-f]{56,64})", eff)
    path_sha = "".join(x.decode() for x in m.groups()) if m else None
    print(f"  CDN 路径还原 = {path_sha}")
    if not path_sha:
        print("  重定向里没有 lfs-objects 路径 → 少一条独立机制，不立 pin")
        return None
    if path_sha != ms_sha:
        print("  ✗ 两条机制给的哈希不一致 → 绝不能挑一个好看的用")
        return None

    rc2, hfs, _ = sh(["curl", "-sS", "--max-time", "60", "-A", "establish-pins/1.0",
                      f"https://hf-mirror.com/api/models/{repo}/tree/main"])
    hf_size = None
    try:
        for x in json.loads(hfs.decode("utf-8", "replace")):
            if x.get("path") == path:
                hf_size = (x.get("lfs") or {}).get("size")
    except Exception:
        pass
    print(f"  HF 侧 lfs.size = {hf_size:,}" if hf_size else "  HF 侧没拿到总长（不能只靠单源就立 pin）")
    if hf_size != ms_size:
        print("  ✗ 两个提供方的总长不一致")
        return None
    print("  ✔ 元数据哈希 == 内容寻址路径，且总长与 HF 一致 → 立 pin（零下载）")
    return {"sha256": ms_sha, "size": ms_size, "primary": "ModelScope repo/files 元数据",
            "cross_checked_against": "同家 CDN lfs-objects 路径 + hf-mirror tree 总长",
            "provenance": "两个提供方对总长一致；哈希出自 ModelScope 的两条独立机制，"
                          "整包字节未经下载校验 —— 首次真正下载时仍要按此 pin 复核"}


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
    for display, route, repo, path, rev in TARGETS:
        p = via_modelscope(display, repo, path, rev) if route == "modelscope" \
            else handle(display, repo, path, rev)
        if p:
            pins[path] = p
    print("=" * 72)
    print("立住的 pin：", list(pins) or "无")
    if args.json:
        json.dump(pins, open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
    return 0 if len(pins) == len(TARGETS) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
