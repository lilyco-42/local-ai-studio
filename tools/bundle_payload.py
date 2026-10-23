#!/usr/bin/env python3
"""把交付清单与摆好的数据根打成安装包里的内置载荷。

`tools/payload_intake.py` 产出的是"怎么送"：每个文件的对象键、落点、体积与 sha256。
这个工具把它变成"随包装"：对象键布局放进 Tauri 资源目录，`delivery-manifest.json`
放在根部；外壳启动时发现这份清单，按同一套 `delivery::install` 逐文件校验落盘。
它不上传、不下载、不执行任何二进制。

四条判据：

① 清单本身先验一遍（schema/os/前缀/键=前缀+落点/根清单 land=last/合计）；
② 源文件的体积与 sha256 必须与清单相符，复制后再复核一次；
③ 输出目录已存在且非空时拒绝，不覆盖用户或上一次的产物；
④ 根清单**最后**写 —— 半套内置对象不会配上一份"看起来完整"的清单。

    python tools/bundle_payload.py --manifest dist/linux-x64/delivery-manifest.json \
        --stage /tmp/root-linux-x64 --out shell/src-tauri/bundled
    python tools/bundle_payload.py --check \
        --manifest dist/linux-x64/delivery-manifest.json --out shell/src-tauri/bundled
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile

MANIFEST_NAME = "payload-manifest.json"
TABLE_NAME = "delivery-manifest.json"


class BundleError(RuntimeError):
    pass


def _sha_file(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _safe_rel(value, label: str) -> str:
    if (not isinstance(value, str) or not value
            or value.startswith("/") or value.endswith("/")
            or "\\" in value or ":" in value or "\0" in value):
        raise BundleError(f"{label} {value!r} 不是安全的相对路径")
    for part in value.split("/"):
        if part in ("", ".", ".."):
            raise BundleError(f"{label} {value!r} 含不安全的段 {part!r}")
    return value


def _sha_ok(value, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 \
            or any(c not in "0123456789abcdef" for c in value):
        raise BundleError(f"{label} 的 sha256 不是 64 位小写十六进制：{value!r}")
    return value


def _positive_int(value, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BundleError(f"{label} 必须是正整数：{value!r}")
    return value


def validate(doc: dict) -> dict:
    if not isinstance(doc, dict):
        raise BundleError("交付清单顶层不是对象")
    if doc.get("schema") != 1:
        raise BundleError(f"交付清单 schema 是 {doc.get('schema')!r}，只认 1")
    os_label = doc.get("os")
    if not isinstance(os_label, str) or not os_label:
        raise BundleError(f"交付清单的 os 不是非空字符串：{os_label!r}")
    prefix = doc.get("key_prefix")
    _safe_rel(prefix, "key_prefix")
    manifest = doc.get("manifest")
    if not isinstance(manifest, dict):
        raise BundleError("交付清单缺 manifest 行")
    if manifest.get("dest") != MANIFEST_NAME:
        raise BundleError(f"根清单落点必须是 {MANIFEST_NAME}，清单写的是 {manifest.get('dest')!r}")
    if manifest.get("land") != "last":
        raise BundleError("根清单没有标 land=last —— 落盘顺序是它的前提")
    _safe_rel(manifest.get("key"), "根清单 key")
    if manifest["key"] != f"{prefix}/{MANIFEST_NAME}":
        raise BundleError(f"根清单键 {manifest['key']!r} 与前缀/落点不符")
    _positive_int(manifest.get("bytes"), "根清单 bytes")
    _sha_ok(manifest.get("sha256"), "根清单")

    files = doc.get("files")
    if not isinstance(files, list) or not files:
        raise BundleError("交付清单里一个文件都没有")
    seen_dest: set[str] = set()
    seen_key: set[str] = set()
    total = 0
    rows = []
    for row in files:
        if not isinstance(row, dict):
            raise BundleError("交付清单 files 里有不是对象的行")
        dest = _safe_rel(row.get("dest"), "dest")
        key = _safe_rel(row.get("key"), "key")
        if dest == MANIFEST_NAME:
            raise BundleError(f"{dest}: 根清单的名字不许出现在文件表里")
        if key != f"{prefix}/{dest}":
            raise BundleError(f"{dest}: 键是 {key!r}，按前缀应当是 {prefix}/{dest!r}")
        if dest in seen_dest:
            raise BundleError(f"{dest}: 同一个落点出现两次")
        if key in seen_key:
            raise BundleError(f"{key}: 同一个键出现两次")
        seen_dest.add(dest)
        seen_key.add(key)
        total += _positive_int(row.get("bytes"), f"{dest} 的 bytes")
        _sha_ok(row.get("sha256"), dest)
        rows.append(row)
    if manifest["key"] in seen_key:
        raise BundleError("根清单键与某个负载文件撞了")

    totals = doc.get("totals")
    if totals is not None:
        if not isinstance(totals, dict):
            raise BundleError("交付清单的 totals 不是对象")
        if totals.get("files") != len(rows):
            raise BundleError(f"合计写 {totals.get('files')!r} 个文件，表里有 {len(rows)}")
        if totals.get("bytes") != total:
            raise BundleError(f"合计写 {totals.get('bytes')!r} 字节，逐行相加是 {total}")
    return {"os": os_label, "prefix": prefix, "rows": rows, "manifest": manifest,
            "bytes": total}


def _read_table(manifest_path: str) -> tuple[dict, bytes]:
    try:
        with open(manifest_path, "rb") as fh:
            blob = fh.read()
    except OSError as exc:
        raise BundleError(f"读不到交付清单 {manifest_path}: {exc}") from exc
    try:
        doc = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(f"{manifest_path}: 读不成 JSON（{exc}）") from exc
    return doc, blob


def _copy_verified(src: str, dst: str, row: dict) -> None:
    if os.path.islink(src) or not os.path.isfile(src):
        raise BundleError(f"源文件不是普通文件：{src}")
    got_sha, got_bytes = _sha_file(src)
    if got_bytes != row["bytes"]:
        raise BundleError(f"{row['dest']}: 清单写 {row['bytes']} 字节，源文件是 {got_bytes}")
    if got_sha != row["sha256"]:
        raise BundleError(f"{row['dest']}: 源文件 sha256 与清单不符（{got_sha[:12]}…）")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst)
    out_sha, out_bytes = _sha_file(dst)
    if out_bytes != row["bytes"] or out_sha != row["sha256"]:
        raise BundleError(f"{row['dest']}: 复制后复核失败（{out_bytes} B / {out_sha[:12]}…）")


def _write_table(out_root: str, blob: bytes) -> None:
    path = os.path.join(out_root, TABLE_NAME)
    fd, tmp = tempfile.mkstemp(prefix=".table-", dir=out_root)
    os.close(fd)
    try:
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def bundle(manifest_path: str, stage_root: str, out_root: str) -> dict:
    doc, blob = _read_table(manifest_path)
    spec = validate(doc)
    stage_root = os.path.abspath(stage_root)
    out_root = os.path.abspath(out_root)
    if not os.path.isdir(stage_root):
        raise BundleError(f"摆好的数据根不在：{stage_root}")
    if os.path.exists(out_root) and os.listdir(out_root):
        raise BundleError(f"输出目录已存在且非空，先换一个或删掉：{out_root}")
    os.makedirs(out_root, exist_ok=True)

    for row in spec["rows"]:
        src = os.path.join(stage_root, *row["dest"].split("/"))
        dst = os.path.join(out_root, *row["key"].split("/"))
        _copy_verified(src, dst, row)
    root_row = spec["manifest"]
    src = os.path.join(stage_root, *root_row["dest"].split("/"))
    dst = os.path.join(out_root, *root_row["key"].split("/"))
    _copy_verified(src, dst, root_row)

    _write_table(out_root, blob)
    report = {
        "os": spec["os"],
        "files": len(spec["rows"]),
        "bytes": spec["bytes"],
        "root_manifest": MANIFEST_NAME,
        "table": TABLE_NAME,
    }
    return report


def check(manifest_path: str, out_root: str) -> dict:
    doc, blob = _read_table(manifest_path)
    spec = validate(doc)
    out_root = os.path.abspath(out_root)
    table_path = os.path.join(out_root, TABLE_NAME)
    if not os.path.isfile(table_path):
        raise BundleError(f"内置载荷根部没有 {TABLE_NAME}: {table_path}")
    with open(table_path, "rb") as fh:
        bundled_table = fh.read()
    if bundled_table != blob:
        raise BundleError("包内 delivery-manifest.json 与源清单不是逐字节相同")
    expected = {TABLE_NAME}
    for row in spec["rows"]:
        expected.add(row["key"])
    expected.add(spec["manifest"]["key"])
    actual = set()
    for base, dirs, files in os.walk(out_root):
        for name in dirs:
            path = os.path.join(base, name)
            if os.path.islink(path):
                raise BundleError(f"内置载荷里出现符号链接目录：{path}")
        for name in files:
            path = os.path.join(base, name)
            if os.path.islink(path):
                raise BundleError(f"内置载荷里出现符号链接：{path}")
            rel = os.path.relpath(path, out_root).replace(os.sep, "/")
            actual.add(rel)
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    if missing:
        raise BundleError(f"内置载荷缺文件：{missing[:4]}")
    if extra:
        raise BundleError(f"内置载荷多了清单外文件：{extra[:4]}")

    for row in spec["rows"] + [spec["manifest"]]:
        path = os.path.join(out_root, *row["key"].split("/"))
        got_sha, got_bytes = _sha_file(path)
        if got_bytes != row["bytes"] or got_sha != row["sha256"]:
            raise BundleError(f"{row['key']}: 包内字节与清单不符")
    return {"os": spec["os"], "files": len(spec["rows"]), "bytes": spec["bytes"],
            "table": TABLE_NAME}


def _write_json(path: str, doc: dict) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    text = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(prefix=".bundle-report-", dir=parent)
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _fixture(root: str, platform: str = "linux-x64") -> tuple[str, str]:
    stage = os.path.join(root, "stage")
    prefix = f"payload/{platform}"
    bodies = {
        f"runtime/{platform}/llama/b9/llama-server": b"synthetic server",
        f"runtime/{platform}/llama/b9/libggml.so": b"synthetic shared library",
    }
    rows = []
    total = 0
    for dest, body in bodies.items():
        path = os.path.join(stage, *dest.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(body)
        rows.append({"dest": dest, "key": f"{prefix}/{dest}", "bytes": len(body),
                     "sha256": hashlib.sha256(body).hexdigest()})
        total += len(body)
    root_body = json.dumps({"os": platform, "engines": []}, separators=(",", ":")).encode()
    root_rel = MANIFEST_NAME
    root_path = os.path.join(stage, root_rel)
    with open(root_path, "wb") as fh:
        fh.write(root_body)
    doc = {
        "schema": 1,
        "os": platform,
        "key_prefix": prefix,
        "manifest": {"dest": root_rel, "key": f"{prefix}/{root_rel}",
                     "bytes": len(root_body), "sha256": hashlib.sha256(root_body).hexdigest(),
                     "land": "last"},
        "files": rows,
        "totals": {"files": len(rows), "bytes": total},
    }
    manifest_path = os.path.join(root, "delivery-manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    return stage, manifest_path


def selftest() -> int:
    global _copy_verified
    cases: list[tuple[str, bool, str]] = []

    def expect_red(label: str, fn) -> None:
        try:
            fn()
        except BundleError as exc:
            cases.append((label, True, str(exc)[:80]))
        else:
            cases.append((label, False, "没有报错"))

    with tempfile.TemporaryDirectory() as tmp:
        stage, manifest = _fixture(tmp)
        out = os.path.join(tmp, "out")
        report = bundle(manifest, stage, out)
        cases.append(("正向：对象键布局 + 根清单最后落",
                      os.path.isfile(os.path.join(out, TABLE_NAME))
                      and os.path.isfile(os.path.join(out, MANIFEST_NAME)) is False
                      and os.path.isfile(os.path.join(
                          out, "payload", "linux-x64", MANIFEST_NAME))
                      and report["files"] == 2,
                      str(report)))
        cases.append(("check 接受刚打出的包", check(manifest, out)["files"] == 2, ""))

        with open(os.path.join(out, TABLE_NAME), "ab") as fh:
            fh.write(b" ")
        expect_red("包内清单被改必须红", lambda: check(manifest, out))
        os.unlink(os.path.join(out, TABLE_NAME))
        with open(manifest, "rb") as fh:
            _write_table(out, fh.read())
        cases.append(("恢复清单后 check 重新通过", check(manifest, out)["files"] == 2, ""))

        tampered = os.path.join(tmp, "tampered")
        stage2, manifest2 = _fixture(tampered)
        with open(os.path.join(stage2, "runtime", "linux-x64", "llama", "b9",
                               "llama-server"), "ab") as fh:
            fh.write(b"tamper")
        expect_red("源文件被改必须红",
                   lambda: bundle(manifest2, stage2, os.path.join(tampered, "out2")))

        ok_stage, ok_manifest = _fixture(os.path.join(tmp, "missing"))
        os.unlink(os.path.join(ok_stage, "runtime", "linux-x64", "llama", "b9",
                               "libggml.so"))
        expect_red("源文件缺失必须红",
                   lambda: bundle(ok_manifest, ok_stage, os.path.join(tmp, "missing", "out")))

        bad = _read_table(manifest)[0]
        bad["files"][0]["key"] = "payload/win-x64/nope"
        bad_path = os.path.join(tmp, "bad-key.json")
        with open(bad_path, "w", encoding="utf-8") as fh:
            json.dump(bad, fh)
        expect_red("键不等于前缀+落点必须红",
                   lambda: bundle(bad_path, stage, os.path.join(tmp, "out-bad-key")))

        dup = _read_table(manifest)[0]
        dup["files"].append(dict(dup["files"][0]))
        dup_path = os.path.join(tmp, "dup.json")
        with open(dup_path, "w", encoding="utf-8") as fh:
            json.dump(dup, fh)
        expect_red("同落点两次必须红",
                   lambda: bundle(dup_path, stage, os.path.join(tmp, "out-dup")))

        escape = _read_table(manifest)[0]
        escape["files"][0]["dest"] = "../escape"
        escape["files"][0]["key"] = f"payload/linux-x64/../escape"
        escape_path = os.path.join(tmp, "escape.json")
        with open(escape_path, "w", encoding="utf-8") as fh:
            json.dump(escape, fh)
        expect_red("落点越界必须红",
                   lambda: bundle(escape_path, stage, os.path.join(tmp, "out-escape")))

        extra_root = os.path.join(tmp, "extra")
        bundle(manifest, stage, extra_root)
        with open(os.path.join(extra_root, "ghost.bin"), "wb") as fh:
            fh.write(b"ghost")
        expect_red("包内多出清单外文件必须红", lambda: check(manifest, extra_root))

        fail_root = os.path.join(tmp, "fail")
        original = _copy_verified
        calls = {"n": 0}

        def boom(src, dst, row):
            calls["n"] += 1
            if calls["n"] == 2:
                raise BundleError("模拟第二次复制失败")
            return original(src, dst, row)

        _copy_verified = boom
        try:
            expect_red("复制中途失败必须红", lambda: bundle(manifest, stage, fail_root))
        finally:
            _copy_verified = original
        cases.append(("复制中途失败时根部没有 delivery-manifest.json",
                      not os.path.exists(os.path.join(fail_root, TABLE_NAME)), ""))

        collisions = os.path.join(tmp, "collisions")
        os.makedirs(collisions)
        with open(os.path.join(collisions, "keep.txt"), "w", encoding="utf-8") as fh:
            fh.write("do not overwrite")
        expect_red("输出目录非空必须拒绝",
                   lambda: bundle(manifest, stage, collisions))

    red = [row for row in cases if not row[1]]
    for label, ok, detail in cases:
        print(("PASS " if ok else "FAIL ") + label + (f" — {detail}" if detail else ""))
    print(f"{len(cases) - len(red)}/{len(cases)} 条判据通过")
    return 0 if not red else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把交付清单与摆好的数据根打成包内资源")
    parser.add_argument("--manifest", help="delivery-manifest.json 路径")
    parser.add_argument("--stage", help="摆好的数据根（bundle 时必填）")
    parser.add_argument("--out", help="输出目录（如 shell/src-tauri/bundled）")
    parser.add_argument("--check", action="store_true", help="复核已打出的包")
    parser.add_argument("--json", help="把回执写到这个文件")
    parser.add_argument("--selftest", action="store_true", help="用合成清单证明每条判据会红")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.manifest or not args.out:
        parser.error("--manifest 与 --out 必填")
    try:
        if args.check:
            report = check(args.manifest, args.out)
            print(f"check ok: {report['os']} / {report['files']} 个文件 / {report['bytes']} B")
        else:
            if not args.stage:
                parser.error("bundle 需要 --stage")
            report = bundle(args.manifest, args.stage, args.out)
            print(f"bundle ok: {report['os']} / {report['files']} 个文件 / {report['bytes']} B")
        if args.json:
            _write_json(args.json, report)
    except BundleError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
