#!/usr/bin/env python3
"""OCR 权重目录完整性检查：oar-ocr 文档里链接的模型 URL，是否真的还能在 GitHub release 里找到。

为什么要单独一条车道：OCR 权重的 URL 会随上游挪 tag / 删产物而 404，
这条要在 CI 里先红，而不是等用户装到一半报下载失败。
（本文件早先写着"GitHub release 没有官方 sha256，哈希必须我们自己算"—— 那句已被 2026-10-19
的实测推翻：每个产物都带 `digest = sha256:<64hex>`，且下载算过的与它逐字节相等。
下面 `no_digest` 那条红就是按新事实写的：哪天某条不给了，宁可红也不退回"不校验"。）

两道自我校验（缺任何一道，"0 缺失"这个结论本身就没意义）：
  · 文档里必须至少解析出 1 个链接、release 里必须至少读到 1 个产物，否则退出码 2；
  · 故意拿一个不存在的文件名去比，必须被判成缺失，否则说明比对逻辑永远为真。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys

REPO = "greatv/oar-ocr"
URL_RX = re.compile(r"https://github\.com/[^\s)\]`]+/releases/download/[^\s)\]`]+", re.I)
OWNER_RX = re.compile(r"(github\.com/)[^/]+/", re.I)


def gh(path: str) -> object:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    cmd = ["gh", "api", path]
    p = subprocess.run(cmd, capture_output=True, text=True,
                       env={**os.environ, **({"GH_TOKEN": tok} if tok else {})})
    if p.returncode:
        raise SystemExit(f"gh api {path} 失败：{p.stderr.strip()[:200]}")
    return json.loads(p.stdout)


def documented_urls(path: str) -> list[str]:
    text = open(path, encoding="utf-8").read()
    return sorted({u.rstrip(".,);") for u in URL_RX.findall(text)})


def release_assets() -> dict[str, tuple[int, str, str | None]]:
    out: dict[str, tuple[int, str, str | None]] = {}
    page = 1
    while True:
        rels = gh(f"repos/{REPO}/releases?per_page=100&page={page}")
        if not rels:
            break
        for r in rels:
            for a in r["assets"]:
                out[a["browser_download_url"]] = (a["size"], a["name"], a.get("digest"))
        if len(rels) < 100:
            break
        page += 1
    return out


def norm(url: str) -> str:
    """文档写 GreatV、API 回 greatv：owner 大小写不参与比较。"""
    return OWNER_RX.sub(r"\g<1>OWNER/", url)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-md", default="", help="已下载的 models.md（不给就用 gh 现取）")
    ap.add_argument("--json", default="", help="把报告写到文件")
    ap.add_argument("--pins-out", default="",
                    help="把 OCR 默认档（tiny det/rec/dict）的上游 digest 写成 pin 文件")
    args = ap.parse_args(argv)

    path = args.models_md
    if not path:
        blob = gh(f"repos/{REPO}/contents/docs/models.md")
        path = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "models.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(base64.b64decode(blob["content"]).decode("utf-8"))

    linked = documented_urls(path)
    assets = release_assets()
    assets_n = {norm(k): v for k, v in assets.items()}

    if not linked or not assets_n:
        print(f"对照失效：文档链接 {len(linked)} 个、release 产物 {len(assets_n)} 个", file=sys.stderr)
        return 2
    fake = norm(linked[0]).rsplit("/", 1)[0] + "/__deliberately_absent__.onnx"
    if fake in assets_n:
        print("对照失效：故意造的假 URL 竟然命中，比对逻辑永远为真", file=sys.stderr)
        return 2

    missing = [u for u in linked if norm(u) not in assets_n]
    onnx = [(sz, name) for sz, name, _ in assets_n.values() if name.endswith(".onnx")]
    # 2026-10-19 实测：GitHub release 每个产物都带 `digest = sha256:<64hex>`，
    # 且下载两个真实包本地算 sha256 与 digest 完全相等（见 ADR-0001 的更正）。
    # 所以 OCR 权重的 pin 有上游来源；哪天某条不给了，要在这里变红而不是"退回不校验"。
    no_digest = sorted(name for _sz, name, dg in assets_n.values()
                       if not (dg and dg.startswith("sha256:") and len(dg) == 71))
    doc_no_digest = sorted(u for u in linked
                           if not (assets_n.get(norm(u)) or (0, "", ""))[2])
    report = {
        "documented": len(linked),
        "assets": len(assets_n),
        "missing": missing,
        "onnx_count": len(onnx),
        "onnx_total_mib": round(sum(s for s, _ in onnx) / 1048576, 1),
        "largest": [max(onnx)[1], round(max(onnx)[0] / 1048576, 1)] if onnx else None,
        "sha256_upstream_provided": not no_digest,
        "assets_without_digest": no_digest,
        "documented_without_digest": doc_no_digest,
    }
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"文档链接 {report['documented']} 个，release 产物 {report['assets']} 个，"
          f"缺失 {len(missing)} 个；onnx {report['onnx_count']} 个共 {report['onnx_total_mib']} MiB")
    for u in missing[:20]:
        print("  MISSING", u)
    if no_digest:
        print(f"  NO DIGEST：{len(no_digest)} 个产物没有上游 sha256，例如 {no_digest[:5]}", file=sys.stderr)
    if missing:
        print("NOT RESOLVED：有文档链接的权重在 release 里找不到，URL 级 pin 会 404", file=sys.stderr)
        return 1
    if no_digest or doc_no_digest:
        print("NOT RESOLVED：上游 sha256 缺失，目录不能降级成不校验", file=sys.stderr)
        return 1
    print("OK：全部文档链接的权重都在 release 里，且每条都有上游给的 sha256")
    if args.pins_out:
        # OCR 的默认档只有三件，但它是四项能力里唯一"权重来自 GitHub 而非 HF"的，
        # 所以 pin 单独一个文件，让 yaml_gate 的寻源检查能覆盖车道里那三个 *_SHA。
        trio = ["pp-ocrv6_tiny_det.onnx", "pp-ocrv6_tiny_rec.onnx", "ppocrv6_tiny_dict.txt"]
        pins = {}
        # 用未归一的原始 URL 当 primary：assets_n 的键被 norm() 换成 OWNER/ 占位了，
        # 那种键只适合比对，写进 pin 文件就是给下游一个取不到的地址。
        for url, (sz, name, dg) in assets.items():
            if name in trio and dg and dg.startswith("sha256:") and len(dg) == 71:
                pins[name] = {"sha256": dg.split(":", 1)[1], "size": sz, "primary": url,
                              "provenance": "上游 release 的 digest（GitHub 给的内容哈希）；"
                                            "本工具不整包下载，车道真下载时按这颗 pin 复核"}
        absent = [n for n in trio if n not in pins]
        if absent:
            print(f"NOT RESOLVED：默认档 {absent} 拿不到上游 digest → 不写 pin", file=sys.stderr)
            return 1
        os.makedirs(os.path.dirname(args.pins_out) or ".", exist_ok=True)
        with open(args.pins_out, "w", encoding="utf-8") as fh:
            json.dump(pins, fh, ensure_ascii=False, indent=1, sort_keys=True)
        print(f"  写入 {args.pins_out}：默认档 {len(pins)} 条（哈希来自上游 digest）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
