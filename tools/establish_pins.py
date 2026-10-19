"""立权重 pin，并打印每条 pin 的凭据强度到哪一步。

三家源的实测差别（决定走哪条路）：
  · GitHub release：产物 API 里带 `digest = sha256:…`，下载验证过就是内容哈希 → 直接可钉；
  · HF / hf-mirror：**`tree` 的 `lfs.oid` 就是内容 sha256**（2026-10-20 更正。上一版这里写"只给文件名+大小"
    是探针读错键名造成的假结论 —— 读的是 `lfs.oidsha`/`lfs.sha`，HF 用的是 `lfs.oid`，
    三颗独立 pin 与 oid 逐字符相等才算把它钉死）。但 resolve 的 `ETag` 是 64 位十六进制
    却**不是**内容哈希（Xet/cas-bridge 指纹，长得极像 sha256，是最危险的假朋友）；
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
import os
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
    # 对话车道用的那颗 0.5B 原先只是把哈希写进 yaml，来源没在这里复现过。
    # 现在它走同一条零下载的路：元数据 Sha256 + CDN 内容寻址 + hf-mirror 总长三方一致才认。
    ("对话 Qwen2.5-0.5B q2_k", "modelscope", "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
     "qwen2.5-0.5b-instruct-q2_k.gguf", "master"),
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

    # 必须用 GET 跟跳转：ModelScope 的 CDN 对 HEAD 不返回这条重定向链，
    # 用 -sIL 时 url_effective 停在 api 端点上 → 还原不出哈希 → 明明上游给了第二套机制却报"没有"。
    # 2026-10-19 就是这么把两条零下载的 pin 误判成"少一条独立机制"的。
    rc, eff, _ = sh(["curl", "-sSL", "--max-time", "60", "-A", "establish-pins/1.0", "-o", "/dev/null",
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

    # hf-mirror 只镜像文件端点，/api/... 回 308 跳回 huggingface.co 本体。
    # 上一版没加 -L → 收到 0 字节 → 误报"HF 侧没拿到总长"，把两条本来能立的 pin 判成不成立。
    # 加上 -L 之后必须把"真正回答的那台"打出来：对照的另一侧其实是 HF，不是国内镜像。
    rc2, hfs, err2 = sh(["curl", "-sSL", "--max-time", "60", "-A", "establish-pins/1.0",
                         "-w", "\n@@EFFECTIVE@@%{url_effective}",
                         f"https://hf-mirror.com/api/models/{repo}/tree/main"])
    hf_host = ""
    if "\n@@EFFECTIVE@@" in hfs.decode("utf-8", "replace"):
        hfs, _, tail = hfs.partition(b"\n@@EFFECTIVE@@")
        hf_host = tail.decode("utf-8", "replace").split("//", 1)[-1].split("/", 1)[0]
    hf_size = None
    try:
        for x in json.loads(hfs.decode("utf-8", "replace")):
            if x.get("path") == path:
                hf_size = (x.get("lfs") or {}).get("size")
    except Exception:
        pass
    if hf_size:
        print(f"  HF 侧 lfs.size = {hf_size:,}（真正回答的是 {hf_host or '未知主机'}）")
    else:
        print(f"  HF 侧没拿到总长（rc={rc2} host={hf_host or '未知'} err={err2[:80]}）→ 不能只靠单源立 pin")
    if hf_size != ms_size:
        print("  ✗ 两个提供方的总长不一致")
        return None
    print("  ✔ 元数据哈希 == 内容寻址路径，且总长与另一家一致 → 立 pin（零下载）")
    return {"sha256": ms_sha, "size": ms_size, "primary": "ModelScope repo/files 元数据",
            # pin 记录必须自带可下载地址：清单、镜像、外壳下载层都要靠它，
            # 只有说明文字的话，下游只能猜 URL —— 猜出来的地址不算证据（2026-10-19 就撞上了）。
            "url": sources(repo, path, "main")[0],
            "alt_sources": [f"https://www.modelscope.cn/models/{repo}/resolve/{rev}/{path}"],
            "cross_checked_against": f"同家 CDN lfs-objects 路径 + {hf_host or 'HF'} tree 总长",
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


def sha_stream(url: str) -> tuple[str | None, int, str]:
    """边下边算，不整包进内存（1.5 GB 的权重不该为了复核把机器 RAM 吃掉）。
    返回 (sha256 或 None, 收到字节数, 错误摘要)。"""
    import hashlib
    p = subprocess.Popen(["curl", "-sSL", "--fail", "--max-time", "3600",
                          "-A", "establish-pins/1.0", url], stdout=subprocess.PIPE)
    h = hashlib.sha256()
    n = 0
    while True:
        chunk = p.stdout.read(1 << 20)
        if not chunk:
            break
        n += len(chunk)
        h.update(chunk)
    rc = p.wait()
    if rc != 0:
        return None, n, f"curl rc={rc}"
    return h.hexdigest(), n, ""


def verify(pins_path: str, only: str) -> int:
    """用真字节复核已有 pin。两类来源，强度不同，必须分开写：

    · 元数据来的（ModelScope 给哈希）→ 从**另一家**（hf-mirror）下整包。对上就是
      "跨提供方 + 真字节"级：一家说哈希是 A，另一家给的字节算出来也是 A。
    · 上游 digest 来的（pin 里直接带 URL，例如 OCR 那三条）→ 只能下同一个提供方，
      对上涨的是"这颗 digest 确实是内容哈希"，**不是**来源独立，别混着写。

    只对元数据来的那几条有意义？不 —— 两条路都值得，但必须如实标级别。"""
    with open(pins_path, encoding="utf-8") as fh:
        pins = json.load(fh)
    hf_repo = {path: repo for _d, route, repo, path, _r in TARGETS if route == "modelscope"}
    cands = [(k, v) for k, v in pins.items() if not only or only in k]
    targets = [(k, v) for k, v in cands if "ModelScope" in str(v.get("primary", ""))
               or str(v.get("primary", "")).startswith("https://github.com/")]
    if not targets:
        print(f"{pins_path} 里没有可复核的 pin（要么哈希来自元数据，要么 pin 里带可下载的 URL）")
        return 1
    print("待复核字节合计 = %.1f MiB" % (sum(v["size"] for _, v in targets) / 1048576))
    rc_all = 0
    for key, v in targets:
        cross = "ModelScope" in str(v.get("primary", ""))
        if cross:
            repo = hf_repo.get(key)
            if not repo:
                print(f"  ✗ {key}: TARGETS 里没有它的 HF 同名仓库，换不了提供方")
                rc_all = 1
                continue
            url = sources(repo, key, "main")[0]   # 与立 pin 时同一套 URL 规则：hf-mirror 在前
        else:
            url = v["primary"]
        print("=" * 72)
        print(f"{key}\n  钉的 sha256 = {v['sha256']}  体积 = {v['size']:,}\n  下载 = {url}")
        got, n, err = sha_stream(url)
        if got is None:
            print(f"  ✗ 下载失败：{err}（已收 {n:,} B）→ 这颗 pin 仍是『整包未校验』级")
            rc_all = 1
            continue
        ok_hash = got == v["sha256"]
        ok_size = n == v["size"]
        print(f"  实收 {n:,} B 体积一致 = {ok_size}；实算 sha256 = {got}")
        print(f"  与钉的哈希一致 = {ok_hash}")
        if ok_hash and ok_size:
            v["bytes_verified"] = {
                "url": url, "bytes": n, "sha256": got,
                "level": "跨提供方 + 真字节（一家给哈希，另一家给字节）" if cross
                         else "同提供方真字节（只证明 digest 是内容哈希，不证明来源独立）"}
            print(f"  ✔ 升级：{v['bytes_verified']['level']}")
        else:
            rc_all = 1
            print("  ✗ 不一致 → 上游被换过或元数据/digest 说谎，这条不能当 pin 用")
    if rc_all == 0:
        with open(pins_path, "w", encoding="utf-8") as fh:
            json.dump(pins, fh, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"复核记录已写回 {pins_path}")
    return rc_all


def merge_prior_records(new_pins: dict, path: str) -> tuple[int, list[str]]:
    """重新立 pin 时保留已有的字节复核记录 —— 但只在哈希与体积都没变时保留。
    没有这条，重跑一次 `--json` 就会把几百 MB 的跨提供方复核成果默默清空，
    而报告看上去和昨天一样绿。"""
    kept, lost = 0, []
    if not os.path.exists(path):
        return 0, lost
    with open(path, encoding="utf-8") as fh:
        old = json.load(fh)
    for key, v in new_pins.items():
        prior = old.get(key) or {}
        for f in ("bytes_verified", "cross_provider_bytes_verified"):
            rec = prior.get(f)
            if not rec:
                continue
            if prior.get("sha256") == v.get("sha256") and prior.get("size") == v.get("size"):
                if f == "cross_provider_bytes_verified":   # 旧字段名统一成新的
                    rec = {"url": rec.get("url") or sources_of(key, prior, rec),
                           "bytes": rec.get("bytes", v.get("size")),
                           "sha256": rec.get("sha256", v.get("sha256")),
                           "level": rec.get("level") or "跨提供方 + 真字节（一家给哈希，另一家给字节）"}
                v["bytes_verified"] = rec
                kept += 1
            else:
                lost.append(f"{key}: 哈希或体积变了，旧复核记录作废")
    return kept, lost


def sources_of(key, prior, rec):
    """旧记录只存了 provider 名。能从新 pin 拿到 url 就用新的，拿不到就留个说明，
    总之不许把"验过"这件事写成"没地址"。"""
    return prior.get("url") or "（旧记录只存了提供方名：" + str(rec.get("provider")) + "）"


def hf_tree_entry(host: str, repo: str, name: str, rev: str = "main"):
    """读 HF 系 tree 元数据里这个文件的 (lfs.oid, size)，返回 (oid, size, 错误说明)。

    键名就是这条判据的全部风险：HF 给的是 `lfs.oid`，不是 `oidsha`/`sha`。
    上一轮探针读错键，于是"HF 不给哈希"这句假结论进了五处文档。
    """
    try:
        rc, body, err = get(f"https://{host}/api/models/{repo}/tree/{rev}")
        if rc != 0:
            return None, None, f"curl 退出码 {rc}：{str(err)[:80]}"
        tree = json.loads(body)
    except Exception as e:      # noqa: BLE001  取不到要变成一行结论，不能抛死整轮
        return None, None, f"{type(e).__name__}: {e}"
    if not isinstance(tree, list):
        return None, None, f"返回不是列表（{str(tree)[:70]}）"
    hit = [e for e in tree if e.get("path") == name]
    if not hit:
        return None, None, f"{host} 的树里没有 {name}（条目 {len(tree)} 个）"
    lfs = hit[0].get("lfs") or {}
    return lfs.get("oid") or hit[0].get("oid"), lfs.get("size") or hit[0].get("size"), None


def hf_repos_for(rec: dict) -> set[str]:
    """从 pin 记录里任何一条 hf-mirror / huggingface 的 URL 反推同名 HF 仓库。"""
    out = set()
    for u in [rec.get("url"), rec.get("primary"), *(rec.get("alt_sources") or [])]:
        m = re.match(r"https?://(?:hf-mirror\.com|huggingface\.co)/([^/]+/[^/]+)/resolve/", str(u or ""))
        if m:
            out.add(m.group(1))
    return out


def cross_meta(pins_path: str) -> int:
    """第二家给哈希：把 HF 系 `tree.lfs.oid` 与已钉的 sha256 对表。

    为什么值得单独一条：ModelScope 那侧的 pin 本质是"一家元数据说哈希是 A"，
    而 HF 的 `lfs.oid` 是**另一家分发方**独立给的同一个值 —— 两家都给哈希，
    不需要下几百 MB 就把"单一声明"升级成"两家一致声明"。这**不是**真字节，
    所以级别名要如实写，也不许冒充 `bytes_verified`。
    """
    with open(pins_path, encoding="utf-8") as fh:
        pins = json.load(fh)
    rc = 0
    for name, rec in sorted(pins.items()):
        repos = hf_repos_for(rec)
        if not repos:
            print(f"  · {name}: 记录里没有 HF 侧 URL → 换不了第二家（这不等于「它没哈希」）")
            continue
        repo = sorted(repos)[0]
        hashes = {}
        for host in ("huggingface.co", "hf-mirror.com"):
            oid, size, err = hf_tree_entry(host, repo, name)
            if err:
                print(f"  ✗ {name}: {host} —— {err}")
                rc = 1
                continue
            hashes[host] = {"oid": oid, "size": size}
            if oid != rec["sha256"]:
                print(f"  ✗ {name}: {host} 给的 oid={str(oid)[:16]}… ≠ 钉的 {rec['sha256'][:16]}…")
                rc = 1
            if size not in (None, rec["size"]):
                print(f"  ✗ {name}: {host} 给的 size={size} ≠ 钉的 {rec['size']}")
                rc = 1
        if len(hashes) == 2 and all(h["oid"] == rec["sha256"] for h in hashes.values()):
            rec["meta_hashes"] = {"repo": repo, "hosts": sorted(hashes), "oid": rec["sha256"],
                                  "level": "两家分发方元数据各给哈希且相等（仍不是真字节）"}
            print(f"  ✔ {name}: HF 两主机的 oid 都等于钉的哈希（仓库 {repo}）")
        else:
            rc = 1
    if rc:
        print("有不等项或取不到 → 不写盘（哈希对不上时改文件是最坏的动作）")
        return 1
    with open(pins_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(pins, fh, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"哈希对照记录已写回 {pins_path}")
    return 0


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    ap.add_argument("--verify", default="", help="用另一家的真字节复核已有 pin 文件")
    ap.add_argument("--only", default="", help="配合 --verify，只复核名字里含这段的那条")
    ap.add_argument("--cross-hf", default="", help="把 HF 系 tree.lfs.oid 与已有 pin 对表（第二家哈希）")
    args = ap.parse_args(argv)
    if args.verify:
        return verify(args.verify, args.only)
    if args.cross_hf:
        return cross_meta(args.cross_hf)
    pins = {}
    for display, route, repo, path, rev in TARGETS:
        p = via_modelscope(display, repo, path, rev) if route == "modelscope" \
            else handle(display, repo, path, rev)
        if p:
            pins[path] = p
    print("=" * 72)
    print("立住的 pin：", list(pins) or "无")
    if args.json:
        kept, lost = merge_prior_records(pins, args.json)
        if kept:
            print(f"保留旧字节复核记录 {kept} 条（哈希与体积都没变才保留）")
        for l in lost:
            print("  !", l)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(pins, fh, ensure_ascii=False, indent=1, sort_keys=True)
    return 0 if len(pins) == len(TARGETS) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
