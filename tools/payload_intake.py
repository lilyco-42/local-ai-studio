#!/usr/bin/env python3
"""把摆好的数据根翻译成"首启逐文件交付"清单。

`tools/stage_payload.py` 负责把出包产物摆成外壳认的目录树；这个工具**只读**那棵树，
为每个文件算出它落到用户机器的相对路径与自家桶里的对象键，产出一张
`(key, dest, sha256, bytes)` 的清单。首启下载照这张表逐文件取、逐文件校验即可。
它不上传、不下载、不执行任何二进制。

外壳根部的 `payload-manifest.json` 只有扁平文件名，没有"引擎 → 目录"这一层
（那一层从合同派生）。这张表把两件事补齐：对象键，以及清单自身在桶里的位置。
顺序纪律与摆位同源：文件先到位，`payload-manifest.json` **最后**落
（表里那条 `land: last`），半套 runtime 不会先配上一份"看起来完整"的清单。

三条判据：

① 文件的落点只从 `pins/shell-contract.json` 派生（工具里没有引擎/版本字面量）；
② 每个文件重新算 sha256 与体积，与数据根清单对不上就整步失败；
③ 同一目标不许出现两次、清单不许与文件撞键、键前缀不许越界。

    python tools/payload_intake.py --data /tmp/root-win-x64 --os win-x64 --out intake.json
    python tools/payload_intake.py --check --data /tmp/root-win-x64 --os win-x64 --out intake.json

`--check` 用同一份数据根与合同重算，与已写出的清单逐键相等才算过。清单里不写绝对路径，
所以两个 runner 摆出的同一棵树会得到同一份字节（测试钉着）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import stage_payload as sp  # noqa: E402

ROOT = os.path.dirname(HERE)
CONTRACT = os.path.join(ROOT, "pins", "shell-contract.json")
MANIFEST_NAME = sp.MANIFEST_NAME
StageError = sp.StageError


def _one_segment(label: str, value: str) -> str:
    if (not isinstance(value, str) or not value
            or value in (".", "..") or "/" in value or "\\" in value):
        raise StageError(f"{label} {value!r} 不是一个安全的名字")
    return value


def _clean_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or not prefix or "\\" in prefix:
        raise StageError(f"键前缀 {prefix!r} 不合法")
    for part in prefix.split("/"):
        if part in ("", ".", ".."):
            raise StageError(f"键前缀 {prefix!r} 含不安全的段 {part!r}")
    return prefix


def key_of(prefix: str, os_label: str, dest: str) -> str:
    return f"{prefix}/{os_label}/{dest}"


def _read_manifest(data_root: str) -> tuple[dict, bytes]:
    """一次读原始字节再解析 —— 哈希与内容必须来自同一份字节，不能读两遍。"""
    path = os.path.join(data_root, MANIFEST_NAME)
    if os.path.islink(path) or not os.path.isfile(path):
        raise StageError(f"数据根里没有普通的 {MANIFEST_NAME}: {path}")
    with open(path, "rb") as fh:
        blob = fh.read()
    try:
        doc = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StageError(f"{path}: 读不成 JSON（{exc}）") from exc
    if not isinstance(doc, dict):
        raise StageError(f"{path}: 顶层不是对象")
    return doc, blob


def _dest_dir(target: str, data_root: str) -> str:
    rel = os.path.relpath(target, data_root)
    if os.path.isabs(rel) or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise StageError(f"目标目录 {target} 跑出了数据根 {data_root}")
    dest = rel.replace(os.sep, "/")
    if dest in ("", "."):
        raise StageError(f"目标目录 {target} 就是数据根本身")
    return dest


def build(data_root: str, os_label: str, contract_path: str = CONTRACT,
          prefix: str = "payload") -> dict:
    data_root = os.path.abspath(data_root)
    os_label = _one_segment("os", os_label)
    prefix = _clean_prefix(prefix)
    if not os.path.isdir(data_root):
        raise StageError(f"数据根不在: {data_root}")
    with open(contract_path, encoding="utf-8") as fh:
        contract = json.load(fh)
    targets = sp.targets_for(contract, os_label)
    doc, blob = _read_manifest(data_root)
    if doc.get("os") != os_label:
        raise StageError(f"数据根清单是 {doc.get('os')!r} 的，要的是 {os_label!r}")
    engines = doc.get("engines")
    if not isinstance(engines, list) or not engines:
        raise StageError("数据根清单里一个引擎都没有")

    rows: list[dict] = []
    by_engine: dict[str, dict] = {}
    seen_engines: set[str] = set()
    seen_dest: set[str] = set()
    total = 0
    for entry in engines:
        engine = entry.get("engine")
        if not isinstance(engine, str) or not engine:
            raise StageError("数据根清单里有条目没写 engine")
        if engine in seen_engines:
            raise StageError(f"{engine}: 数据根清单里出现两次")
        seen_engines.add(engine)
        row = targets.get(engine)
        if row is None:
            raise StageError(f"{engine}: 外壳合同里没有这个引擎的 runtime 目录")
        asset = entry.get("asset")
        if row["asset"] is not None and asset != row["asset"]:
            raise StageError(f"{engine}: 数据根清单的产物是 {asset!r}，外壳默认要 {row['asset']!r}")
        target = sp.expand_dir(row["template"], data_root)
        verified = sp._verify_tree(entry, target)
        dest_dir = _dest_dir(target, data_root)
        files = 0
        size = 0
        for f in entry["files"]:
            dest = f"{dest_dir}/{f['file']}"
            if dest in seen_dest:
                raise StageError(f"{dest}: 两个引擎指到同一个目标文件 —— 合同有歧义")
            seen_dest.add(dest)
            rows.append({"engine": engine, "capability": row["capability"], "asset": asset,
                         "dest": dest, "key": key_of(prefix, os_label, dest),
                         "bytes": f["bytes"], "sha256": f["sha256"]})
            files += 1
            size += f["bytes"]
        if size != verified:
            raise StageError(f"{engine}: 清单合计 {size} B 与目录实测 {verified} B 不符")
        by_engine[engine] = {"capability": row["capability"], "asset": asset,
                             "files": files, "bytes": size}
        total += size

    rows.sort(key=lambda r: r["dest"])
    manifest_key = key_of(prefix, os_label, MANIFEST_NAME)
    if manifest_key in {r["key"] for r in rows}:
        raise StageError(f"{manifest_key}: 清单键与某个负载文件撞了")
    not_in_manifest = sorted({r["capability"] + "/" + engine
                              for engine, r in targets.items() if engine not in seen_engines})
    return {
        "schema": 1,
        "os": os_label,
        "key_prefix": f"{prefix}/{os_label}",
        "manifest": {"dest": MANIFEST_NAME, "key": manifest_key,
                     "bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest(),
                     "land": "last"},
        "files": rows,
        "totals": {"files": len(rows), "bytes": total, "by_engine": by_engine},
        "not_in_manifest": not_in_manifest,
    }


def _first_diff(got: dict, want: dict) -> str:
    if not isinstance(got, dict):
        return "写出的清单顶层不是对象"
    if got.get("os") != want["os"]:
        return f"os 是 {got.get('os')!r}，现在是 {want['os']!r}"
    gf = got.get("files")
    wf = want["files"]
    if not isinstance(gf, list):
        return "写出的清单没有 files 列表"
    if len(gf) != len(wf):
        return f"文件条数 {len(gf)} ≠ {len(wf)}"
    for old, new in zip(gf, wf):
        if old != new:
            if not isinstance(old, dict):
                return f"{new['dest']} 这一行不是对象"
            return (f"{new['dest']} 这一行不同（清单 {str(old.get('sha256'))[:12]}… / "
                    f"现在 {new['sha256'][:12]}…）")
    for key in sorted(set(got) | set(want)):
        if got.get(key) != want.get(key):
            return f"{key} 不同"
    return "内容不同"


def check(data_root: str, os_label: str, intake_path: str,
          contract_path: str = CONTRACT, prefix: str = "payload") -> dict:
    want = build(data_root, os_label, contract_path, prefix)
    try:
        with open(intake_path, encoding="utf-8") as fh:
            got = json.load(fh)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StageError(f"{intake_path}: 读不出交付清单（{exc}）") from exc
    if got != want:
        raise StageError(f"交付清单与数据根/合同不一致：{_first_diff(got, want)}")
    return want


def write(intake: dict, out_path: str) -> None:
    out_path = os.path.abspath(out_path)
    parent = os.path.dirname(out_path)
    os.makedirs(parent, exist_ok=True)
    text = json.dumps(intake, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".intake-", dir=parent)
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def selftest() -> int:
    """合成合同/合成树：证明每条负向判据都会红，且输出不随数据根的绝对路径漂。"""
    contract = {"capabilities": {
        "对话": {"engine": "llama", "os": {"win-x64": {
            "default_asset": "llama-x.zip",
            "asset_dir": "<data>/runtime/win-x64/llama/b1/llama-x"}}},
        "生图": {"engine": "sd", "os": {"win-x64": {
            "default_asset": "sd.zip",
            "asset_dir": "<data>/runtime/win-x64/sd/m1/sd-x"}}},
    }}

    def write_contract(root: str, doc: dict) -> str:
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, "contract.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return path

    def write_pkg(root: str, engines: list[tuple[str, str, dict]], os_label="win-x64",
                  write_manifest=True) -> str:
        pkg = os.path.join(root, "pkg")
        rows = []
        for engine, asset, files in engines:
            engine_dir = os.path.join(pkg, engine)
            os.makedirs(engine_dir, exist_ok=True)
            listed = []
            for name, payload in files.items():
                with open(os.path.join(engine_dir, name), "wb") as fh:
                    fh.write(payload)
                listed.append({"file": name, "bytes": len(payload),
                               "sha256": hashlib.sha256(payload).hexdigest()})
            rows.append({"engine": engine, "status": "ok", "asset": asset, "files": listed})
        if write_manifest:
            with open(os.path.join(pkg, MANIFEST_NAME), "w", encoding="utf-8") as fh:
                json.dump({"os": os_label, "engines": rows}, fh)
        return pkg

    def staged(tag: str, engines=None, contract_doc=None) -> tuple[str, str, str]:
        root = os.path.join(d, tag)
        os.makedirs(root, exist_ok=True)
        cf = write_contract(root, contract_doc or contract)
        pkg = write_pkg(root, engines or [("llama", "llama-x.zip",
                                           {"llama-server.exe": b"bin",
                                            "llama-impl.dll": b"dll"})])
        data = os.path.join(root, "data")
        sp.stage(pkg, data, "win-x64", cf)
        return data, cf, pkg

    cases: list[tuple[str, bool, str]] = []

    def expect_red(label: str, fn) -> None:
        try:
            fn()
        except StageError as exc:
            cases.append((label, True, str(exc)[:80]))
        else:
            cases.append((label, False, "没有报错"))

    with tempfile.TemporaryDirectory() as d:
        data, cf, _pkg = staged("ok")
        intake = build(data, "win-x64", cf)
        out = os.path.join(d, "ok-intake.json")
        write(intake, out)
        target = os.path.join(data, "runtime", "win-x64", "llama", "b1", "llama-x")
        with open(os.path.join(data, MANIFEST_NAME), "rb") as fh:
            root_blob = fh.read()
        cases.append(("正向：文件按合同落点、键带前缀、清单最后落",
                      os.path.isfile(os.path.join(target, "llama-server.exe"))
                      and intake["files"][0]["dest"].startswith("runtime/win-x64/llama/b1/llama-x/")
                      and all(r["key"].startswith("payload/win-x64/") for r in intake["files"])
                      and intake["manifest"]["key"] == "payload/win-x64/" + MANIFEST_NAME
                      and intake["manifest"]["land"] == "last"
                      and intake["manifest"]["sha256"] == hashlib.sha256(root_blob).hexdigest()
                      and intake["totals"]["files"] == 2,
                      f"{intake['totals']}"))
        cases.append(("check 认出刚写出的清单", check(data, "win-x64", out, cf) == intake, ""))

        data2, cf2, _ = staged("other-root")
        out2 = os.path.join(d, "other-intake.json")
        write(build(data2, "win-x64", cf2), out2)
        with open(out, "rb") as fh:
            a = fh.read()
        with open(out2, "rb") as fh:
            b = fh.read()
        cases.append(("两个数据根产出同一份字节（不含绝对路径）",
                      a == b and os.path.abspath(data).encode() not in a, ""))

        def fresh(tag: str) -> str:
            root, _cf, _pkg = staged(tag)
            return root

        tampered = fresh("tamper")
        with open(os.path.join(tampered, "runtime", "win-x64", "llama", "b1", "llama-x",
                               "llama-server.exe"), "ab") as fh:
            fh.write(b"tamper")
        expect_red("改了一颗文件必须红", lambda: build(tampered, "win-x64", cf))

        polluted = fresh("ghost")
        with open(os.path.join(polluted, "runtime", "win-x64", "llama", "b1", "llama-x",
                               "ghost.dll"), "wb") as fh:
            fh.write(b"x")
        expect_red("多出清单外的文件必须红", lambda: build(polluted, "win-x64", cf))

        missing = fresh("missing")
        os.unlink(os.path.join(missing, "runtime", "win-x64", "llama", "b1", "llama-x",
                               "llama-impl.dll"))
        expect_red("少一颗清单里的文件必须红", lambda: build(missing, "win-x64", cf))

        no_manifest = fresh("no-manifest")
        os.unlink(os.path.join(no_manifest, MANIFEST_NAME))
        expect_red("没有根清单必须红", lambda: build(no_manifest, "win-x64", cf))

        for label, field, value in (
            ("根清单的 os 不符必须红", "os", "linux-x64"),
            ("某个引擎的 asset 不符必须红", "asset", "other.zip"),
            ("未知引擎必须红", "engine", "ghost"),
        ):
            root = fresh("field-" + field)
            mpath = os.path.join(root, MANIFEST_NAME)
            with open(mpath, encoding="utf-8") as fh:
                doc = json.load(fh)
            if field == "os":
                doc["os"] = value
            else:
                doc["engines"][0][field] = value
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            expect_red(label, lambda root=root: build(root, "win-x64", cf))

        expect_red("键前缀越界必须红",
                   lambda: build(fresh("prefix"), "win-x64", cf, prefix="../evil"))

        dup_contract = {"capabilities": {
            "对话": {"engine": "llama", "os": {"win-x64": {
                "default_asset": "same.zip",
                "asset_dir": "<data>/runtime/win-x64/shared"}}},
            "生图": {"engine": "sd", "os": {"win-x64": {
                "default_asset": "same.zip",
                "asset_dir": "<data>/runtime/win-x64/shared"}}},
        }}
        cf_dup = write_contract(os.path.join(d, "dup-contract"), dup_contract)
        pkg_dup = write_pkg(os.path.join(d, "dup-pkg"), [
            ("llama", "same.zip", {"server.bin": b"x"}),
            ("sd", "same.zip", {"server.bin": b"x"}),
        ])
        data_dup = os.path.join(d, "dup-data")
        sp.stage(pkg_dup, data_dup, "win-x64", cf_dup)
        expect_red("两个引擎指到同一个目标文件必须红",
                   lambda: build(data_dup, "win-x64", cf_dup))

        edited = os.path.join(d, "edited-intake.json")
        with open(out, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["files"][0]["sha256"] = "0" * 64
        with open(edited, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        expect_red("check：清单被手改过必须红",
                   lambda: check(data, "win-x64", edited, cf))

    rc = 0
    for label, ok, detail in cases:
        rc |= 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label} → {detail}")
    return rc


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="")
    ap.add_argument("--os", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--contract", default=CONTRACT)
    ap.add_argument("--prefix", default="payload")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not (args.data and args.os and args.out):
        ap.error("需要 --data / --os / --out（或 --selftest）")
    try:
        if args.check:
            report = check(args.data, args.os, args.out, args.contract, args.prefix)
        else:
            report = build(args.data, args.os, args.contract, args.prefix)
            write(report, args.out)
    except StageError as exc:
        print(f"BAD: {exc}")
        return 1
    verb = "复核通过" if args.check else "已生成"
    print(f"{args.os}: {verb} 交付清单 {len(report['totals']['by_engine'])} 个引擎 / "
          f"{report['totals']['files']} 个文件 / {report['totals']['bytes'] / 1048576:.1f} MiB")
    for engine, row in sorted(report["totals"]["by_engine"].items()):
        print(f"  {row['capability']}/{engine} → {row['files']} 个文件，"
              f"{row['bytes'] / 1048576:.1f} MiB")
    print(f"  根清单 {report['manifest']['key']}（{report['manifest']['bytes']} B，"
          f"{report['manifest']['sha256'][:12]}…）最后落")
    for name in report["not_in_manifest"]:
        print(f"  未在出包清单里: {name}（缺口如实记，不算这一步失败）")
    if not args.check:
        print(f"写入 {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
