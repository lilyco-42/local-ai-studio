"""量 OCR 那条车道的**构建输入**到底钉没钉住（纯元数据，不编译不运行任何东西）。

为什么要有这个工具：其余三项能力的引擎都是"上游预编译产物 + release digest 的 sha256"，
钉了就等于钉了字节。OCR 不是 —— 上游 `.gitignore` 里明写 `Cargo.lock`（库的惯例），
所以我们钉住 commit `84817f58` 只钉住了**我们的源码**，`cargo build` 仍会在构建时重新解析
依赖图。依赖一旦用 caret（`"1"` 等价于 `^1`），今天编出来的 `oar` 和下个月编出来的 `oar`
可以不是同一份代码，而 sha256 侧表、`--verify`、跨提供方复核那套全都覆盖不到它。

这里把"有多少依赖其实是没钉的"量出来，并留一条漂移判据（`--check` 比对的记录由本工具生成，
不手打数字）。附带一条形状判据：上游那个 `Cargo.lock` 到底在不在 —— 用一个**必定存在**的
文件当正向对照，免得 404 是网络问题却被读成"上游没锁文件"。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tomllib
import urllib.error
import urllib.request

REF = "84817f58"          # oar-ocr main 的 commit（v0.10.0 tag 里没有 CLI，见 ADR）
REPO = "GreatV/oar-ocr"
CRATES = ("oar-ocr-cli", "oar-ocr-core", "oar-ocr-vl", "oar-ocr-derive", "oar-ocr-bench")
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
RECORD = os.path.join(ROOT, "pins", "ocr-build-surface.json")
# 分类结果里"算钉住"的那几档：exact(=) 与 git+rev。其余都会随时间漂。
PINNED_KINDS = ("exact", "git-rev")


def raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{REF}/{path}"


def fetch(url: str, timeout: int = 40) -> tuple[int, bytes]:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ocr-build-surface"}),
                                timeout=timeout) as r:
        return r.status, r.read()


def classify(name: str, spec) -> dict:
    """把一个依赖声明归到某档：exact / caret / tilde / range / git / path / unknown。"""
    v = spec if isinstance(spec, str) else (spec.get("version") if isinstance(spec, dict) else None)
    d = spec if isinstance(spec, dict) else {}
    if d.get("workspace"):
        # `{ workspace = true }` 的包把真正的版本声明放在根 Cargo.toml 的 [workspace.dependencies] 里，
        # 上一版把它归成 unknown，报告就长出 14 条"看起来更可怕"的假数字。
        kind, req = "workspace-inherited", "见根 Cargo.toml"
    elif "path" in d:
        kind, req = "path", v or "*"
    elif "git" in d:
        kind, req = ("git-rev", d["rev"]) if d.get("rev") else ("git-floating", d["git"])
    elif v is None:
        kind, req = "unknown", "?"
    elif v.startswith("="):
        kind, req = "exact", v[1:]
    elif v.startswith("~"):
        kind, req = "tilde", v
    elif v.startswith(">=") or v.startswith("<") or "," in v:
        kind, req = "range", v
    else:
        kind, req = "caret", v          # cargo 默认语义：`"1"` == `^1`
    return {"name": name, "kind": kind, "req": req,
            "optional": bool(d.get("optional")), "features": sorted(d.get("features") or [])}


def deps_from_toml(text: str, crate: str) -> list[dict]:
    doc = tomllib.loads(text)
    out = []
    for section in ("dependencies", "build-dependencies"):
        for name, spec in (doc.get(section) or {}).items():
            row = classify(name, spec)
            row.update(crate=crate, section=section)
            out.append(row)
    for tgt, body in (doc.get("target") or {}).items():
        for name, spec in (body.get("dependencies") or {}).items():
            row = classify(name, spec)
            row.update(crate=crate, section=f"target.{tgt}")
            out.append(row)
    for name, spec in ((doc.get("workspace") or {}).get("dependencies") or {}).items():
        row = classify(name, spec)
        row.update(crate="_workspace", section="workspace.dependencies")
        out.append(row)
    return out


def measure() -> dict:
    manifests = [("Cargo.toml", fetch(raw_url("Cargo.toml"))[1].decode("utf-8"))]
    for c in CRATES:
        p = f"{c}/Cargo.toml"
        if fetch(raw_url(p))[0] == 200:
            manifests.append((p, fetch(raw_url(p))[1].decode("utf-8")))
    rows: list[dict] = []
    for p, text in manifests:
        for r in deps_from_toml(text, p.split("/")[0]):
            r["manifest"] = p
            rows.append(r)
    # 同一个 workspace 里 workspace.dependencies 会与成员 crate 的声明重复出现，
    # 数"多少个不同的包被留成 caret"要按包名去重，否则计数会被放大（本轮第一版就把 4 数成 8）。
    io_deps = [r for r in rows if r["kind"] != "path"]
    names_by_kind: dict[str, set[str]] = {}
    for r in io_deps:
        names_by_kind.setdefault(r["kind"], set()).add(r["name"])
    by_kind_names = {k: sorted(v) for k, v in names_by_kind.items()}
    # 一个包可能出现多次（成员 crate 的 `{ workspace = true }` 只是指针，根表才是版本）。
    # 所以"钉没钉"要按**包名**归并后再判：只要有一条声明是 =精确 就算钉住；
    # workspace-inherited 本身既不算钉也不算未钉，否则 14 条指针会变成 14 个假"未钉"。
    kinds_by_name: dict[str, set[str]] = {}
    for r in io_deps:
        kinds_by_name.setdefault(r["name"], set()).add(r["kind"])
    pinned_names = sorted(n for n, ks in kinds_by_name.items() if ks & set(PINNED_KINDS))
    unpinned = sorted(n for n, ks in kinds_by_name.items()
                      if not (ks & set(PINNED_KINDS)) and (ks - {"workspace-inherited"}))
    blob = "\n".join(sorted(f"{r['name']}@{r['req']}" for r in io_deps)).encode("utf-8")
    return {
        "ref": REF,
        "manifests": [p for p, _ in manifests],
        "decls_total": len(rows),
        "crates_io_total": len(io_deps),
        "unique_packages": len(kinds_by_name),
        "by_kind": {k: len(v) for k, v in by_kind_names.items()},
        "by_kind_names": by_kind_names,
        "unpinned_packages": unpinned,
        "pinned": pinned_names,
        "pinned_versions": sorted({f"{r['name']}={r['req']}" for r in io_deps if r["kind"] == "exact"}),
        # 只写了 `{ workspace = true }`、而根表里查不到版本的包：既不算钉也不算未钉，必须单列，
        # 不然 40 个包被报成"37 未钉 + 1 钉"，剩下的 2 个就这么消失了。
        "pointer_only": sorted(n for n, ks in kinds_by_name.items() if ks == {"workspace-inherited"}),
        "fingerprint": hashlib.sha256(blob).hexdigest(),
        "lockfile_upstream": lockfile_state(),
    }


def lockfile_state() -> dict:
    """上游 Cargo.lock 到底在不在，带一条正向对照（必定存在的 Cargo.toml）。

    探测用 `Range: bytes=0-0`，所以**存在时回的是 206 不是 200** —— 第一版按 200 判"对照齐"，
    于是每条正向对照都失效，整张表会永远红。判"在不在"要认 (200, 206)，判"不在"只认 404。
    """
    code_lock, _ = head(f"{REPO}/{REF}/Cargo.lock")
    code_toml, _ = head(f"{REPO}/{REF}/Cargo.toml")
    return {"cargo_lock_http": code_lock, "control_cargo_toml_http": code_toml,
            "lock_absent": code_lock == 404,
            "control_ok": code_toml in (200, 206)}


def head(path_ref: str, timeout: int = 30) -> tuple[int, bytes]:
    """走 raw.githubusercontent 的 GET（HEAD 会被某些 CDN 区别对待），只读前 1 字节。"""
    url = f"https://raw.githubusercontent.com/{path_ref}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "ocr-build-surface",
                                                   "Range": "bytes=0-0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:      # noqa: BLE001  网络故障要能被看见，不能伪装成 404
        return 0, b""


def diff_against(record: dict, live: dict) -> list[str]:
    bad = []
    if not live["lockfile_upstream"]["control_ok"]:
        bad.append(f"正向对照失效：Cargo.toml 取到 {live['lockfile_upstream']['control_cargo_toml_http']}"
                   f"（探测带 Range 头，正常是 206，200 也算；不是这两个就说明这次测量本身不可信）")
        return bad
    if live["lockfile_upstream"]["cargo_lock_http"] == 0:
        bad.append("上游 Cargo.lock 那次请求根本没回来（网络/超时），不能当成「没有锁文件」")
    if record["ref"] != live["ref"]:
        bad.append(f"ref 变了：记录 {record['ref']} / 现在 {live['ref']}")
    if record["fingerprint"] != live["fingerprint"]:
        gone = set(record["unpinned_packages"]) - set(live["unpinned_packages"])
        new = set(live["unpinned_packages"]) - set(record["unpinned_packages"])
        bad.append(f"依赖声明漂了（fingerprint {record['fingerprint'][:12]}… → {live['fingerprint'][:12]}…）"
                   f"；新变未钉 {sorted(new)} / 新被钉住 {sorted(gone)}")
    if record["lockfile_upstream"]["cargo_lock_http"] != live["lockfile_upstream"]["cargo_lock_http"]:
        bad.append(f"上游锁文件状态变了：记录 {record['lockfile_upstream']['cargo_lock_http']} → "
                   f"{live['lockfile_upstream']['cargo_lock_http']}（上游开始发 Cargo.lock 的话，"
                   f"车道就该改成取它而不是自己解析）")
    return bad


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="与已提交的记录比对（漂移就红）")
    ap.add_argument("--record", action="store_true", help="把本次测量写进 --record-file")
    ap.add_argument("--record-file", default=RECORD,
                    help="测量记录路径（比对的基准），默认 pins/ocr-build-surface.json")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)
    if args.check and not os.path.exists(args.record_file):
        # 先查基准再干活：不然一次 --check 要先跑 6 次网络取数，才在最后一刻发现记录不存在，
        # 而网络抖动会让"缺记录"这个真原因被盖成 urlopen 异常。
        print(f"BAD：没有测量记录 {args.record_file} —— 先跑 --record 生成并提交，比对才有基准")
        return 1
    live = measure()
    print(f"ref {live['ref']}：清单 {len(live['manifests'])} 份，声明 {live['decls_total']} 条，"
          f"其中非本地包 {live['crates_io_total']} 条 / {live['unique_packages']} 个不同的包")
    print(f"  分档：{live['by_kind']}")
    print(f"  上游 Cargo.lock：HTTP {live['lockfile_upstream']['cargo_lock_http']}"
          f"（正向对照 Cargo.toml HTTP {live['lockfile_upstream']['control_cargo_toml_http']}）")
    print(f"  未钉住的包 {len(live['unpinned_packages'])} 个；钉住的 {len(live['pinned'])} 个 {live['pinned_versions']}")
    if live["pointer_only"]:
        print(f"  只有 workspace 指针、根表里查不到版本的 {len(live['pointer_only'])} 个：{live['pointer_only']}")
    acc = len(live["unpinned_packages"]) + len(live["pinned"]) + len(live["pointer_only"])
    if acc != live["unique_packages"]:
        print(f"BAD：分档加起来 {acc} ≠ 包总数 {live['unique_packages']}，有包被漏classify了")
        return 1
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(live, fh, ensure_ascii=False, indent=1)
    if args.record:
        with open(args.record_file, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(live, fh, ensure_ascii=False, indent=1)
        print(f"  写入 {args.record_file}")
        return 0
    if args.check:
        if not os.path.exists(args.record_file):
            print(f"BAD：没有测量记录 {args.record_file} —— 先跑 --record 生成并提交，比对才有基准")
            return 1
        with open(args.record_file, encoding="utf-8") as fh:
            rec = json.load(fh)
        bad = diff_against(rec, live)
        for b in bad:
            print("  ✗", b)
        print("  OK：依赖声明与锁文件状态都没漂" if not bad else f"BAD {len(bad)} 条")
        return 1 if bad else 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
