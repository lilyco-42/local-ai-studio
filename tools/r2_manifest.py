"""生成"该进自家镜像的清单"`mirror/r2-intake.json`，并把省下来的时间算出来。

为什么要这张表：2026-10-19 从 CN 出口实测，GitHub release 的 1 MiB 档吞吐只有
0.012–0.017 MiB/s，而自家 R2 是 0.82–1.01 MiB/s。工具带与权重都是"装到用户机器上"
的东西，慢一个数量级就是第一次启动等 45 分钟还是等 40 秒的差别（见 docs/toolbelt.md）。

清单全部从 `pins/*.json` 推出来，**一个数字都不手打** —— 手打就会漂，漂了就变成
交付说明里的假承诺。引擎不在这里：按 ADR 的决定引擎跟 nightly 走（钉死等于天天红），
所以它由 `discover` 每次选 release 时带上游 digest，不是静态清单。

    python tools/r2_manifest.py            # 写 mirror/r2-intake.json 并打印合计
    python tools/r2_manifest.py --check    # 只看会不会漂，不写盘

注意：这张表**只是清单**，不含任何上传动作。往 R2 灌东西花的是他的存储与流量，
由他决定；这里只保证"真要灌的时候，照着这张表灌就行"。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))

# 2026-10-19 都从 lain42.top 这个 CN 出口实测，1 MiB 起以上档位；单主机单时段，只用于决定层级。
# ModelScope 三个样本是 4.2 / 22.9 / 12.1 MiB/s（1/16/32 MiB），这里取最保守的 4.2 ——
# 因为 1 MiB 那档含 0.22 s 的 ttfb，大文件更可能靠近日而不是它。
RATES_MIB_S = [
    ("github.com", 0.014, "GitHub release 三个 1 MiB 样本 0.012–0.017"),
    ("hf-mirror.com", 0.25, "1 MiB 样本，含 ttfb 1.8 s"),
    ("modelscope", 4.2, "1/16/32 MiB 三档最保守一档"),
]
R2_MIB_S = 0.90          # 自家 R2，同机早前三次采样 0.82–1.01
MIB = 1048576.0

# 车道样本不是给用户装的东西，不进镜像清单
EXCLUDE = ("samples/",)


def gather() -> tuple[list[dict], list[str]]:
    rows, problems = [], []
    for fname in ("toolbelt.json", "models.json", "ocr.json"):
        p = os.path.join(ROOT, "pins", fname)
        if not os.path.exists(p):
            problems.append(f"缺少 {p}：清单不能从不存在的 pin 文件生成")
            continue
        with open(p, encoding="utf-8") as fh:
            pins = json.load(fh)
        for key, v in sorted(pins.items()):
            if any(key.startswith(e) for e in EXCLUDE):
                continue
            url = v.get("url") or v.get("primary") or ""
            if not str(url).startswith("https://"):
                problems.append(f"{fname}:{key} 没有可下载的源地址（primary 是一段说明文字？）")
                continue
            if not v.get("sha256") or not v.get("size"):
                problems.append(f"{fname}:{key} 缺 sha256 或 size")
                continue
            rows.append({
                "kind": fname[:-5],
                "name": key.rsplit("/", 1)[-1],
                "sha256": v["sha256"],
                "size": int(v["size"]),
                "source": url,
                # 候选源要一起参与判断：权重那几条 primary 是 hf-mirror（0.25 MiB/s），
                # 但同一颗在 ModelScope 上是 4.2 MiB/s —— 只看 primary 会得出"该镜像"的错结论。
                "candidates": [url] + [a for a in (v.get("alt_sources") or []) if a != url],
                "digest_from": ("上游 release digest + sidecar 对照" if fname == "toolbelt.json"
                                else "跨提供方真字节" if "bytes_verified" in v
                                else "上游 release digest（同提供方真字节已验）" if fname == "ocr.json"
                                else v.get("provenance", "")),
            })
    return rows, problems


def rate_for(url: str) -> tuple[str, float, str]:
    for needle, rate, note in RATES_MIB_S:
        if needle in url:
            return needle, rate, note
    return "未知源", 0.0, "没有实测速率 → 不许换算成时间"


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只报告，不写盘")
    args = ap.parse_args(argv)

    rows, problems = gather()
    total = sum(r["size"] for r in rows)
    print(f"清单 {len(rows)} 条，合计 {total / MIB:,.1f} MiB")
    for r in rows:
        print(f"  {r['kind']:9} {r['name'][:46]:46} {r['size']:>13,} B  {r['sha256'][:12]}…")
    for p in problems:
        print("  ✗", p)

    # 按源分组：一个总均值会把"GitHub 慢得不能用"和"ModelScope 比我们 R2 还快"两件事糊成一团
    groups: dict[str, dict] = {}
    for r in rows:
        # 每条取它"现成候选里最快的一档"：权重的 primary 是 hf-mirror（0.25 MiB/s），
        # 但同一颗在 ModelScope 上是 4.2 MiB/s —— 只看 primary 会得出"该镜像"的错结论。
        cands = [rate_for(u) for u in (r.get("candidates") or [r["source"]])]
        host, rate, note = max(cands, key=lambda x: x[1])
        g = groups.setdefault(host, {"bytes": 0, "count": 0, "rate": rate, "note": note,
                                     "others": sorted({c[0] for c in cands} - {host})})
        g["bytes"] += r["size"]
        g["count"] += 1
    print("按实测吞吐分组（MiB/s 来自 2026-10-19 的 CN 出口实测）：")
    verdicts = {}
    for host, g in sorted(groups.items()):
        mib = g["bytes"] / MIB
        now = mib / g["rate"] if g["rate"] else float("inf")
        r2 = mib / R2_MIB_S
        if not g["rate"]:
            call = "无法判断（没有该源的实测速率）"
        elif g["rate"] >= R2_MIB_S:
            call = (f"不必镜像：现源 {g['rate']} 已 ≥ R2 {R2_MIB_S}"
                    f"（把 {host} 列为首选，其余 {g['others']} 留作备选）")
        else:
            call = f"值得镜像：现源 {g['rate']} → R2 {R2_MIB_S}，{now/60:.1f} min 变 {r2:.0f} s"
        verdicts[host] = {"bytes": g["bytes"], "count": g["count"], "measured_mib_s": g["rate"],
                          "sample_note": g["note"], "other_candidates": g["others"], "verdict": call}
        print(f"  {host:14} {g['count']:2} 条 {mib:8,.1f} MiB · {g['rate']} MiB/s → "
              f"{now/60:6.1f} min ｜ 走 R2 {r2:6.0f} s ｜ {call}")
    if problems:
        print("有问题 → 不写清单")
        return 1
    if args.check:
        return 0
    out = os.path.join(ROOT, "mirror", "r2-intake.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"schema": 1, "note": "只是清单，不含上传动作；灌不灌由用户决定",
                   "measured_mib_s": {"own_r2_cn_egress": R2_MIB_S},
                   "by_source": verdicts,
                   "total_bytes": total, "artifacts": rows},
                  fh, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
