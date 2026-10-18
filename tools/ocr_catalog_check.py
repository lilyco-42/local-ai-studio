#!/usr/bin/env python3
"""OCR 权重目录完整性检查：oar-ocr 文档里链接的模型 URL，是否真的还能在 GitHub release 里找到。

为什么要单独一条车道：OCR 的权重只能按 URL 钉（GitHub release 只给 size，**没有官方 sha256**，
哈希必须我们自己算）。一旦上游挪了 tag 或删了产物，钉住的 URL 会变 404——
这条车道要在 CI 里先红，而不是等用户装到一半报下载失败。

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


def release_assets() -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    page = 1
    while True:
        rels = gh(f"repos/{REPO}/releases?per_page=100&page={page}")
        if not rels:
            break
        for r in rels:
            for a in r["assets"]:
                out[a["browser_download_url"]] = (a["size"], a["name"])
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
    args = ap.parse_args(argv)

    path = args.models_md
    if not path:
        blob = gh(f"repos/{REPO}/contents/docs/models.md")
        path = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "models.md")
        open(path, "w", encoding="utf-8").write(base64.b64decode(blob["content"]).decode("utf-8"))

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
    onnx = [(sz, name) for sz, name in assets_n.values() if name.endswith(".onnx")]
    report = {
        "documented": len(linked),
        "assets": len(assets_n),
        "missing": missing,
        "onnx_count": len(onnx),
        "onnx_total_mib": round(sum(s for s, _ in onnx) / 1048576, 1),
        "largest": [max(onnx)[1], round(max(onnx)[0] / 1048576, 1)] if onnx else None,
        "sha256_upstream_provided": False,
    }
    if args.json:
        json.dump(report, open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
    print(f"文档链接 {report['documented']} 个，release 产物 {report['assets']} 个，"
          f"缺失 {len(missing)} 个；onnx {report['onnx_count']} 个共 {report['onnx_total_mib']} MiB")
    for u in missing[:20]:
        print("  MISSING", u)
    if missing:
        print("NOT RESOLVED：有文档链接的权重在 release 里找不到，URL 级 pin 会 404", file=sys.stderr)
        return 1
    print("OK：全部文档链接的权重都能在 release 里取到（哈希仍需我们自己算）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
