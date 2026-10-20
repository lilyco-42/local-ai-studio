#!/usr/bin/env python3
"""把出包车道产出的平铺负载，摆成外壳运行时认的目录树。

出包（`tools/pack_engine.py`）的产物是：

    dist/<os>/payload-manifest.json
    dist/<os>/<engine>/<files...>              # 文件名已拉平

外壳（`shell/core/src/supervisor.rs::verify_payload`）只认：

    <data>/payload-manifest.json
    <data>/runtime/<os>/<engine>/<version>/<asset-stem>/<files...>

两者之间此前没有任何一步 —— 这个工具就是那一步。三条判据：

① 目标目录只从 `pins/shell-contract.json` 派生（不在工具里手写任何路径）；
② 每个文件复制后重新算 sha256 与体积，对不上就整步失败；
③ `payload-manifest.json` **最后**才落盘 —— 半套 runtime 不会配上一份"看起来完整"的清单。

它只搬引擎负载，不搬权重：权重按内容寻址、由首启下载/sideload 负责（见 model-catalog.md）。
本工具不执行任何二进制，CI 与本机都能跑。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT = os.path.join(ROOT, "pins", "shell-contract.json")
MANIFEST_NAME = "payload-manifest.json"


class StageError(Exception):
    """一步没通过就不该继续搬 —— 半套目录比报错更难查。"""


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def expand_dir(template: str, data_root: str) -> str:
    """与 Rust `contract::expand_dir` 同一套规则：必须 `<data>/` 开头、段不许空/. /..。"""
    if not isinstance(template, str) or not template.startswith("<data>/"):
        raise StageError(f"目录模板 {template!r} 不以 <data>/ 开头")
    rest = template[len("<data>/"):]
    parts = rest.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise StageError(f"目录模板 {template!r} 含不安全的段 {part!r}")
    return os.path.join(os.path.abspath(data_root), *parts)


def safe_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise StageError("清单里有空文件名")
    if name in (".", "..") or "/" in name or "\\" in name:
        raise StageError(f"清单里的文件名 {name!r} 不是平铺文件 —— 拒绝路径穿越")
    return name


def targets_for(contract: dict, os_label: str) -> dict[str, dict]:
    """engine → {capability, template, asset}；同引擎两个目录模板算歧义。"""
    caps = contract.get("capabilities")
    if not isinstance(caps, dict) or not caps:
        raise StageError("合同里没有 capabilities")
    out: dict[str, dict] = {}
    for cap, spec in caps.items():
        cell = (spec.get("os") or {}).get(os_label)
        if not cell:
            continue
        template = cell.get("asset_dir") or cell.get("version_dir")
        if not template:
            continue
        engine = spec.get("engine")
        if not isinstance(engine, str) or not engine:
            raise StageError(f"{cap}: 合同里没有 engine")
        row = {"capability": cap, "engine": engine, "template": template,
               "asset": cell.get("default_asset")}
        old = out.get(engine)
        if old is not None and old["template"] != template:
            raise StageError(f"{engine}: 合同里有两个 runtime 目录（{old['template']} / {template}）")
        out[engine] = row
    return out


def scan_dir(path: str) -> set[str]:
    """目标目录只许出现平铺的普通文件；子目录/符号链接都算污染。"""
    found: set[str] = set()
    with os.scandir(path) as it:
        for entry in it:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise StageError(f"{path} 里有非普通文件 {entry.name!r}")
            found.add(entry.name)
    return found


def copy_verified(src: str, dst: str, want_bytes: int, want_sha: str) -> None:
    """写到同目录临时文件，复核哈希后再原子替换 —— 中途失败不留下半颗文件。"""
    if os.path.islink(src) or not os.path.isfile(src):
        raise StageError(f"源文件不是普通文件: {src}")
    got_bytes = os.path.getsize(src)
    if got_bytes != want_bytes:
        raise StageError(f"{src}: 体积 {got_bytes} 与清单 {want_bytes} 不符")
    got_sha = sha256_file(src)
    if got_sha != want_sha:
        raise StageError(f"{src}: sha256 {got_sha} 与清单 {want_sha} 不符")
    fd, tmp = tempfile.mkstemp(prefix=".stage-", dir=os.path.dirname(dst))
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, os.stat(src).st_mode & 0o7777)
        if os.path.getsize(tmp) != want_bytes or sha256_file(tmp) != want_sha:
            raise StageError(f"{dst}: 复制后复核失败")
        os.replace(tmp, dst)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _load_manifest(package: str) -> dict:
    path = os.path.join(package, MANIFEST_NAME)
    if not os.path.isfile(path):
        raise StageError(f"出包目录里没有 {MANIFEST_NAME}: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise StageError(f"{path}: 读不成 JSON（{exc}）") from exc
    if not isinstance(doc, dict):
        raise StageError(f"{path}: 顶层不是对象")
    return doc


def _entry_files(entry: dict) -> list[dict]:
    files = entry.get("files")
    if entry.get("status") != "ok":
        raise StageError(f"{entry.get('engine')}: 出包状态是 {entry.get('status')!r}，不是 ok")
    if not isinstance(files, list) or not files:
        raise StageError(f"{entry.get('engine')}: 清单是空的")
    seen: set[str] = set()
    for row in files:
        name = safe_name(row.get("file"))
        if name in seen:
            raise StageError(f"{entry.get('engine')}: 文件名 {name!r} 在清单里出现两次")
        seen.add(name)
        want = row.get("sha256")
        if not isinstance(want, str) or len(want) != 64 or any(c not in "0123456789abcdef" for c in want):
            raise StageError(f"{entry.get('engine')}/{name}: sha256 不是 64 位小写十六进制")
        if not isinstance(row.get("bytes"), int) or row["bytes"] <= 0:
            raise StageError(f"{entry.get('engine')}/{name}: bytes 不是正整数")
    return files


def _verify_tree(entry: dict, target: str) -> int:
    files = _entry_files(entry)
    want = {row["file"]: row for row in files}
    if not os.path.isdir(target):
        raise StageError(f"目标目录不在: {target}")
    got = scan_dir(target)
    extra = sorted(got - set(want))
    missing = sorted(set(want) - got)
    if extra:
        raise StageError(f"{target}: 多出清单外的文件 {extra[:6]}")
    if missing:
        raise StageError(f"{target}: 缺少清单里的文件 {missing[:6]}")
    total = 0
    for name, row in want.items():
        path = os.path.join(target, name)
        if os.path.getsize(path) != row["bytes"]:
            raise StageError(f"{path}: 体积与清单不符")
        if sha256_file(path) != row["sha256"]:
            raise StageError(f"{path}: sha256 与清单不符")
        total += row["bytes"]
    return total


def stage(package: str, data_root: str, os_label: str,
          contract_path: str = CONTRACT) -> dict:
    package = os.path.abspath(package)
    data_root = os.path.abspath(data_root)
    with open(contract_path, encoding="utf-8") as fh:
        contract = json.load(fh)
    targets = targets_for(contract, os_label)
    doc = _load_manifest(package)
    if doc.get("os") != os_label:
        raise StageError(f"出包清单是 {doc.get('os')!r} 的，要摆的是 {os_label!r}")
    engines = doc.get("engines")
    if not isinstance(engines, list) or not engines:
        raise StageError("出包清单里一个引擎都没有")
    staged = []
    seen: set[str] = set()
    for entry in engines:
        engine = entry.get("engine")
        if not isinstance(engine, str) or not engine:
            raise StageError("出包清单里有条目没写 engine")
        if engine in seen:
            raise StageError(f"{engine}: 出包清单里出现两次")
        seen.add(engine)
        row = targets.get(engine)
        if row is None:
            raise StageError(f"{engine}: 外壳合同里没有这个引擎的 runtime 目录")
        asset = entry.get("asset")
        if row["asset"] is not None and asset != row["asset"]:
            raise StageError(f"{engine}: 出包清单的产物是 {asset!r}，外壳默认要 {row['asset']!r}")
        target = expand_dir(row["template"], data_root)
        files = _entry_files(entry)
        want = {f["file"] for f in files}
        if os.path.exists(target) and not os.path.isdir(target):
            raise StageError(f"{target}: 已存在但不是目录")
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as exc:
            raise StageError(f"{target}: 建不了目录（{exc}）") from exc
        got = scan_dir(target)
        extra = sorted(got - want)
        if extra:
            raise StageError(f"{target}: 已有多余文件 {extra[:6]} —— 不替你删，先人工确认")
        for f in files:
            src = os.path.join(package, engine, f["file"])
            dst = os.path.join(target, f["file"])
            copy_verified(src, dst, f["bytes"], f["sha256"])
        total = _verify_tree(entry, target)
        staged.append({"engine": engine, "capability": row["capability"], "asset": asset,
                       "dir": os.path.relpath(target, data_root).replace(os.sep, "/"),
                       "files": len(files), "bytes": total})
    not_in_manifest = sorted({row["capability"] + "/" + engine
                              for engine, row in targets.items() if engine not in seen})
    # 清单最后落盘：上面的任何一步失败都不会留下一份指向半套目录的清单。
    src_manifest = os.path.join(package, MANIFEST_NAME)
    with open(src_manifest, "rb") as fh:
        blob = fh.read()
    fd, tmp = tempfile.mkstemp(prefix=".manifest-", dir=data_root)
    os.close(fd)
    try:
        with open(tmp, "wb") as fh:
            fh.write(blob)
        dst_manifest = os.path.join(data_root, MANIFEST_NAME)
        os.replace(tmp, dst_manifest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    with open(os.path.join(data_root, MANIFEST_NAME), "rb") as fh:
        if fh.read() != blob:
            raise StageError("落盘后的 payload-manifest 与出包清单不是同一份字节")
    return {"os": os_label, "package": package, "data_root": data_root,
            "manifest": MANIFEST_NAME, "staged": staged,
            "not_in_manifest": not_in_manifest,
            "total_bytes": sum(s["bytes"] for s in staged)}


def check(package: str, data_root: str, os_label: str,
          contract_path: str = CONTRACT) -> dict:
    package = os.path.abspath(package)
    data_root = os.path.abspath(data_root)
    with open(contract_path, encoding="utf-8") as fh:
        contract = json.load(fh)
    targets = targets_for(contract, os_label)
    doc = _load_manifest(package)
    if doc.get("os") != os_label:
        raise StageError(f"出包清单是 {doc.get('os')!r} 的，要复核的是 {os_label!r}")
    staged_manifest = os.path.join(data_root, MANIFEST_NAME)
    if not os.path.isfile(staged_manifest):
        raise StageError(f"数据目录里没有 {MANIFEST_NAME}: {staged_manifest}")
    with open(staged_manifest, encoding="utf-8") as fh:
        try:
            landed = json.load(fh)
        except json.JSONDecodeError as exc:
            raise StageError(f"{staged_manifest}: 不是合法 JSON（{exc}）") from exc
    if landed != doc:
        raise StageError("数据目录里的清单与出包清单内容不同")
    verified = []
    for entry in doc.get("engines") or []:
        engine = entry.get("engine")
        row = targets.get(engine)
        if row is None:
            raise StageError(f"{engine}: 外壳合同里没有这个引擎的 runtime 目录")
        if row["asset"] is not None and entry.get("asset") != row["asset"]:
            raise StageError(f"{engine}: 出包清单的产物与外壳默认不符")
        target = expand_dir(row["template"], data_root)
        total = _verify_tree(entry, target)
        verified.append({"engine": engine, "capability": row["capability"],
                         "dir": os.path.relpath(target, data_root).replace(os.sep, "/"),
                         "files": len(entry["files"]), "bytes": total})
    return {"os": os_label, "data_root": data_root, "verified": verified,
            "total_bytes": sum(v["bytes"] for v in verified)}


def selftest() -> int:
    """不碰真合同、不碰真包：用合成目录证明每条负向判据都会红。"""
    contract = {"capabilities": {"对话": {"engine": "llama", "os": {"win-x64": {
        "default_asset": "llama-x.zip",
        "asset_dir": "<data>/runtime/win-x64/llama/b1/llama-x"}}}}}

    def build_pkg(root: str, *, files=None, engine: str = "llama",
                  asset="llama-x.zip", os_label="win-x64", write=True) -> str:
        pkg = os.path.join(root, "pkg")
        os.makedirs(os.path.join(pkg, engine), exist_ok=True)
        files = files if files is not None else {"llama-server.exe": b"bin",
                                                 "llama-server-impl.dll": b"dll"}
        rows = []
        for name, payload in files.items():
            with open(os.path.join(pkg, engine, name), "wb") as fh:
                fh.write(payload)
            rows.append({"file": name, "bytes": len(payload),
                         "sha256": hashlib.sha256(payload).hexdigest()})
        doc = {"os": os_label, "engines": [{"engine": engine, "status": "ok",
                                            "asset": asset, "files": rows}]}
        if write:
            with open(os.path.join(pkg, MANIFEST_NAME), "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
        return pkg

    def contract_file(root: str, doc: dict) -> str:
        os.makedirs(root, exist_ok=True)
        path = os.path.join(root, "contract.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        return path

    cases = []

    def expect_red(label, fn):
        try:
            fn()
        except StageError as exc:
            cases.append((label, True, str(exc)[:80]))
        else:
            cases.append((label, False, "没有报错"))

    with tempfile.TemporaryDirectory() as d:
        cf = contract_file(d, contract)
        pkg = build_pkg(d)
        data = os.path.join(d, "data")
        rep = stage(pkg, data, "win-x64", cf)
        target = os.path.join(data, "runtime", "win-x64", "llama", "b1", "llama-x")
        cases.append(("正向：文件落到合同目录、清单在根部",
                      os.path.isfile(os.path.join(target, "llama-server.exe"))
                      and os.path.isfile(os.path.join(data, MANIFEST_NAME))
                      and rep["staged"][0]["files"] == 2, rep["staged"]))
        cases.append(("check 认出刚摆好的目录", len(check(pkg, data, "win-x64", cf)["verified"]) == 1, ""))

        # 每条变异都用一份全新的 data root：否则前一个坏文件会让后一条"红得没有理由"。
        def fresh(tag: str) -> str:
            root = os.path.join(d, "mut-" + tag)
            stage(pkg, root, "win-x64", cf)
            return root

        tampered = fresh("tamper")
        with open(os.path.join(tampered, "runtime", "win-x64", "llama", "b1", "llama-x",
                               "llama-server.exe"), "ab") as fh:
            fh.write(b"tamper")
        expect_red("check：改了一颗文件必须红", lambda: check(pkg, tampered, "win-x64", cf))

        polluted = fresh("ghost")
        with open(os.path.join(polluted, "runtime", "win-x64", "llama", "b1", "llama-x",
                               "ghost.dll"), "wb") as fh:
            fh.write(b"x")
        expect_red("check：多出清单外的文件必须红", lambda: check(pkg, polluted, "win-x64", cf))

        dirty = os.path.join(d, "dirty")
        stage(pkg, dirty, "win-x64", cf)
        with open(os.path.join(dirty, "runtime", "win-x64", "llama", "b1", "llama-x",
                               "ghost.dll"), "wb") as fh:
            fh.write(b"x")
        expect_red("stage：目标目录已有多余文件必须拒绝而不是覆盖",
                   lambda: stage(pkg, dirty, "win-x64", cf))

        for label, mutate in (
            ("哈希不符时不许落清单", dict(files={"llama-server.exe": b"bin"})),
            ("产物名与合同默认不符必须红", dict(asset="other.zip")),
            ("未知引擎必须红", dict(engine="ghost")),
            ("OS 不符必须红", dict(os_label="linux-x64")),
            ("没有清单必须红", dict(write=False)),
            ("文件名带路径必须红", dict(files={"../evil.dll": b"x"})),
        ):
            sub = os.path.join(d, "case-" + str(len(cases)))
            os.makedirs(sub)
            cf2 = cf
            if label.startswith("哈希"):
                # 正向建包，落盘前把清单里的哈希改坏
                p2 = build_pkg(sub)
                mp = os.path.join(p2, MANIFEST_NAME)
                with open(mp, encoding="utf-8") as fh:
                    doc = json.load(fh)
                doc["engines"][0]["files"][0]["sha256"] = "0" * 64
                with open(mp, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh)
            else:
                p2 = build_pkg(sub, **mutate)
            data2 = os.path.join(sub, "data")
            expect_red(label, lambda p2=p2, data2=data2, cf2=cf2: stage(p2, data2, "win-x64", cf2))
            if label.startswith("哈希"):
                cases.append(("失败时根部没有清单",
                              not os.path.exists(os.path.join(data2, MANIFEST_NAME)), data2))

        bad_contract = contract_file(os.path.join(d, "c2"), {"capabilities": {"对话": {
            "engine": "llama", "os": {"win-x64": {"default_asset": "llama-x.zip",
                                                   "asset_dir": "<data>/runtime/../../etc"}}}}})
        p3 = build_pkg(os.path.join(d, "p3"))
        expect_red("合同模板越界必须红",
                   lambda: stage(p3, os.path.join(d, "d3"), "win-x64", bad_contract))

    rc = 0
    for label, ok, detail in cases:
        rc |= 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {label} → {detail}")
    return rc


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", default="")
    ap.add_argument("--data", default="")
    ap.add_argument("--os", default="")
    ap.add_argument("--contract", default=CONTRACT)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--json", default="")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    if not (args.package and args.data and args.os):
        ap.error("需要 --package / --data / --os（或 --selftest）")
    try:
        report = (check if args.check else stage)(args.package, args.data, args.os, args.contract)
    except StageError as exc:
        print(f"BAD: {exc}")
        return 1
    verb = "复核通过" if args.check else "已摆放"
    rows = report["verified"] if args.check else report["staged"]
    print(f"{args.os}: {verb} {len(rows)} 个引擎 / "
          f"{sum(r['files'] for r in rows)} 个文件 / {report['total_bytes'] / 1048576:.1f} MiB")
    for r in rows:
        print(f"  {r['engine']} → {r['dir']}（{r['files']} 个文件，{r['bytes'] / 1048576:.1f} MiB）")
    for name in report.get("not_in_manifest") or []:
        print(f"  未在出包清单里: {name}（缺口如实记，不算这一步失败）")
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
