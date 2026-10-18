#!/usr/bin/env python3
"""目录"能搜"探针：证明 app 里的"浏览/搜索模型"这一步在某个源上真能跑。

为什么单独一条：文件能下 ≠ 目录能搜，而"打开就能挑模型"才是 LM Studio 的招牌交互。
三条判据，缺一条就退出非零（否则探针会变成永远为真的摆设）：
  1. 泛词（gguf）必须有结果；
  2. 正向对照：按精确仓库名搜，必须搜到那个仓库；
  3. 负向对照：乱词必须 0 条——不然"有结果"说明不了任何事。

用法：python3 tools/catalog_probe.py            # 打所有候选源
      python3 tools/catalog_probe.py --json c.json
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

UA = "catalog-probe/1.0"
GARBAGE = "zzqt_not_a_real_repo_918273"
CONTROL = "Qwen/Qwen2.5-0.5B-Instruct-GGUF"


def fetch(url: str, method: str = "GET", body: dict | None = None, timeout: int = 30):
    """走 curl：本机/国内出口的 urllib 行为不稳，curl 是验过的通道。"""
    cmd = ["curl", "-sSL", "--fail-with-body", "--max-time", str(timeout),
           "--max-filesize", str(8 << 20), "-A", UA, "-X", method, "-w", "\n%{http_code}"]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "--data", json.dumps(body)]
    cmd.append(url)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return None, f"rc={p.returncode} {p.stderr.strip()[:80]}"
    raw, _, code = p.stdout.rpartition("\n")
    try:
        return json.loads(raw), code.strip()
    except Exception:
        return None, f"HTTP {code.strip()}，响应不是 JSON：{raw[:60]!r}"


def items_of(d):
    """上游形状不一：HF 直接回数组，ModelScope 把列表塞在 Data 里。"""
    if isinstance(d, list):
        return d, "<顶层数组>"
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, list) and v:
                return v, k
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    if isinstance(v2, list):
                        return v2, f"{k}.{k2}"
    return [], None


def ident(it):
    for f in ("modelId", "id", "Name", "Path", "repository"):
        if isinstance(it, dict) and it.get(f):
            return str(it[f])
    return "?"


def probe_hfmirror(label, url):
    t0 = time.time()
    d, note = fetch(url)
    arr, key = items_of(d)
    ids = [ident(x) for x in arr[:5]]
    ok_fields = sum(1 for x in arr if isinstance(x, dict) and x.get("tags") and x.get("downloads") is not None)
    print(f"[{label}] {note} 用时 {time.time()-t0:.2f}s 条目={len(arr)} 数组键={key}")
    print(f"    前 5: {ids}")
    print(f"    带 tags+downloads 的条目 {ok_fields}/{len(arr)}（决定能不能显示大小/许可/热度）")
    return arr, ids


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    report, bad = {}, []

    # 1. 泛词
    arr, _ = probe_hfmirror("hf-mirror 泛搜 gguf",
                             "https://hf-mirror.com/api/models?search=gguf&sort=downloads&limit=20")
    if not arr:
        bad.append("hf-mirror 泛搜没有结果")
    report["hfmirror_broad"] = len(arr)

    # 2. 正向对照
    arr2, ids2 = probe_hfmirror("hf-mirror 正向对照",
                                f"https://hf-mirror.com/api/models?search={CONTROL}&limit=20")
    if CONTROL not in ids2:
        bad.append(f"正向对照失败：搜不到 {CONTROL}，实际 {ids2}")

    # 3. 负向对照
    arr3, ids3 = probe_hfmirror("hf-mirror 负向对照", f"https://hf-mirror.com/api/models?search={GARBAGE}&limit=5")
    if arr3:
        bad.append(f"负向对照失败：乱词也返回 {len(arr3)} 条（{ids3[:3]}），说明搜索不可信")

    # 4. ModelScope：搜索端点形状未知，先按仓库名做存在性对照，再试两种搜索写法
    d, note = fetch("https://www.modelscope.cn/api/v1/models/Qwen/Qwen2.5-0.5B-Instruct-GGUF")
    print(f"[modelscope 按名取仓库] {note}")
    ms_ok = isinstance(d, dict) and bool(d.get("Data") or d.get("Info"))
    print(f"    命中={ms_ok} 顶层键={list(d)[:6] if isinstance(d, dict) else type(d).__name__}")
    if not ms_ok:
        bad.append("ModelScope 按名取仓库没拿到 Data——这条源还不能用")
    report["modelscope_byname"] = ms_ok

    for label, url, body in [
        ("modelscope POST /api/v1/models", "https://www.modelscope.cn/api/v1/models",
         {"Query": "gguf", "PageSize": 10, "PageNumber": 1, "SortBy": "Default"}),
        ("modelscope dolphin 搜索", "https://www.modelscope.cn/api/v1/dolphin/models",
         {"Query": "gguf", "PageSize": 10, "PageNumber": 1}),
    ]:
        t0 = time.time()
        d, note = fetch(url, "POST", body)
        arr, key = items_of(d)
        print(f"[{label}] {note} 用时 {time.time()-t0:.2f}s 条目={len(arr)} 数组键={key}")
        if arr:
            print(f"    前 3: {[ident(x) for x in arr[:3]]}")
        report[label] = len(arr)

    if args.json:
        json.dump(report, open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1, sort_keys=True)
    print("---- 判据 ----")
    for b in bad:
        print("BAD:", b)
    print("BAD 数量 =", len(bad), "| 报告:", report)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
