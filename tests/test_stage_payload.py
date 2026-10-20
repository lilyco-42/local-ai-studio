"""出包产物 → 外壳 runtime 目录：搬家这一步必须在本地就能证明会红。

关键不是"复制成功"，而是三件会被静默吞掉的事：
  * 目标目录只从 `pins/shell-contract.json` 派生（改合同，落点必须跟着改）；
  * 出一个文件的哈希/体积对不上时，**根部不许出现清单**（半套目录 + 完整清单最危险）；
  * 目标目录里已有的多余文件不许被"顺手覆盖/忽略"。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import stage_payload as sp  # noqa: E402

CLI = os.path.join(ROOT, "tools", "stage_payload.py")
CONTRACT = os.path.join(ROOT, "pins", "shell-contract.json")


def write_contract(root: str, caps: dict) -> str:
    path = os.path.join(root, "contract.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"capabilities": caps}, fh)
    return path


def write_pkg(root: str, os_label: str, engines: list[dict]) -> str:
    pkg = os.path.join(root, "pkg")
    rows = []
    for spec in engines:
        engine = spec["engine"]
        os.makedirs(os.path.join(pkg, engine), exist_ok=True)
        files = []
        for name, payload in spec["files"].items():
            dst = os.path.join(pkg, engine, name)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as fh:
                fh.write(payload)
            files.append({"file": name, "bytes": len(payload),
                          "sha256": hashlib.sha256(payload).hexdigest()})
        rows.append({"engine": engine, "status": "ok",
                     "asset": spec.get("asset"), "files": files})
    with open(os.path.join(pkg, sp.MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump({"os": os_label, "engines": rows}, fh)
    return pkg


def two_engines() -> dict:
    return {
        "对话": {"engine": "llama", "os": {"win-x64": {
            "default_asset": "llama.zip",
            "asset_dir": "<data>/runtime/win-x64/llama/b1/llama-pkg"}}},
        "生图": {"engine": "sd", "os": {"win-x64": {
            "default_asset": "sd.zip",
            "asset_dir": "<data>/runtime/win-x64/sd/m1/sd-pkg"}}},
    }


def whisper_real() -> dict:
    with open(CONTRACT, encoding="utf-8") as fh:
        return sp.targets_for(json.load(fh), "win-x64")["whisper"]


class TestStage(unittest.TestCase):
    def test_selftest_passes(self) -> None:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sp.selftest()
        self.assertEqual(rc, 0, buf.getvalue())

    def test_real_contract_whisper_lands_where_shell_looks(self) -> None:
        row = whisper_real()
        with tempfile.TemporaryDirectory() as d:
            pkg = write_pkg(d, "win-x64", [{
                "engine": "whisper", "asset": row["asset"],
                "files": {"whisper-server.exe": b"srv", "whisper-cli.exe": b"cli"}}])
            data = os.path.join(d, "data")
            rep = sp.stage(pkg, data, "win-x64")
            target = sp.expand_dir(row["template"], data)
            self.assertTrue(os.path.isfile(os.path.join(target, "whisper-server.exe")))
            self.assertTrue(os.path.isfile(os.path.join(data, sp.MANIFEST_NAME)))
            self.assertEqual(rep["staged"][0]["dir"],
                             "runtime/win-x64/whisper/b5454/whisper-bin-x64")
            self.assertIn("文字识别/oar-ocr", rep["not_in_manifest"])
            self.assertEqual(len(sp.check(pkg, data, "win-x64")["verified"]), 1)

    def test_target_comes_from_contract_not_a_literal(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            caps = two_engines()
            caps["对话"]["os"]["win-x64"]["asset_dir"] = "<data>/runtime/custom/llama-elsewhere"
            contract = write_contract(d, caps)
            pkg = write_pkg(d, "win-x64", [{"engine": "llama", "asset": "llama.zip",
                                            "files": {"llama-server.exe": b"bin"}}])
            data = os.path.join(d, "data")
            sp.stage(pkg, data, "win-x64", contract)
            self.assertTrue(os.path.isfile(os.path.join(
                data, "runtime", "custom", "llama-elsewhere", "llama-server.exe")))

    def test_no_manifest_when_a_later_engine_fails(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            contract = write_contract(d, two_engines())
            pkg = write_pkg(d, "win-x64", [
                {"engine": "llama", "asset": "llama.zip", "files": {"llama-server.exe": b"bin"}},
                {"engine": "sd", "asset": "sd.zip", "files": {"sd-server.exe": b"srv"}},
            ])
            mpath = os.path.join(pkg, sp.MANIFEST_NAME)
            with open(mpath, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["engines"][1]["files"][0]["sha256"] = "0" * 64
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            data = os.path.join(d, "data")
            with self.assertRaises(sp.StageError):
                sp.stage(pkg, data, "win-x64", contract)
            self.assertFalse(os.path.exists(os.path.join(data, sp.MANIFEST_NAME)))
            # 第一个引擎已经搬过去了 —— 这是允许的，但根部没有清单，外壳不会把它当完整负载。
            self.assertTrue(os.path.isfile(os.path.join(
                data, "runtime", "win-x64", "llama", "b1", "llama-pkg", "llama-server.exe")))

    def test_existing_extra_file_is_refused_not_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            contract = write_contract(d, two_engines())
            pkg = write_pkg(d, "win-x64", [{"engine": "llama", "asset": "llama.zip",
                                            "files": {"llama-server.exe": b"bin"}}])
            data = os.path.join(d, "data")
            sp.stage(pkg, data, "win-x64", contract)
            ghost = os.path.join(data, "runtime", "win-x64", "llama", "b1", "llama-pkg", "ghost.dll")
            with open(ghost, "wb") as fh:
                fh.write(b"x")
            with self.assertRaises(sp.StageError):
                sp.stage(pkg, data, "win-x64", contract)
            self.assertTrue(os.path.isfile(ghost), "拒绝时不许顺手删用户目录里的东西")

    def test_check_detects_tamper_and_extra(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            contract = write_contract(d, two_engines())
            pkg = write_pkg(d, "win-x64", [{"engine": "llama", "asset": "llama.zip",
                                            "files": {"llama-server.exe": b"bin"}}])
            data = os.path.join(d, "data")
            sp.stage(pkg, data, "win-x64", contract)
            target = os.path.join(data, "runtime", "win-x64", "llama", "b1", "llama-pkg")
            with open(os.path.join(target, "llama-server.exe"), "ab") as fh:
                fh.write(b"tamper")
            with self.assertRaises(sp.StageError):
                sp.check(pkg, data, "win-x64", contract)
            with open(os.path.join(target, "llama-server.exe"), "wb") as fh:
                fh.write(b"bin")
            with open(os.path.join(target, "ghost.dll"), "wb") as fh:
                fh.write(b"x")
            with self.assertRaises(sp.StageError):
                sp.check(pkg, data, "win-x64", contract)

    def test_asset_and_os_and_engine_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            contract = write_contract(d, two_engines())
            good = {"engine": "llama", "asset": "llama.zip", "files": {"llama-server.exe": b"bin"}}
            cases = [
                ("asset", write_pkg(os.path.join(d, "a"), "win-x64",
                                    [dict(good, asset="other.zip")]), "win-x64"),
                ("os", write_pkg(os.path.join(d, "b"), "linux-x64", [good]), "win-x64"),
                ("engine", write_pkg(os.path.join(d, "c"), "win-x64",
                                     [dict(good, engine="ghost")]), "win-x64"),
            ]
            for label, pkg, os_label in cases:
                with self.subTest(label=label), self.assertRaises(sp.StageError):
                    sp.stage(pkg, os.path.join(d, "data-" + label), os_label, contract)

    def test_traversal_and_duplicate_names_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            contract = write_contract(d, two_engines())
            for label, name in (("traversal", "../evil.dll"), ("nested", "sub/evil.dll")):
                pkg = write_pkg(os.path.join(d, label), "win-x64", [
                    {"engine": "llama", "asset": "llama.zip", "files": {name: b"x"}}])
                with self.subTest(label=label), self.assertRaises(sp.StageError):
                    sp.stage(pkg, os.path.join(d, "data-" + label), "win-x64", contract)
            pkg = write_pkg(os.path.join(d, "dup"), "win-x64", [
                {"engine": "llama", "asset": "llama.zip", "files": {"a.dll": b"x"}}])
            mpath = os.path.join(pkg, sp.MANIFEST_NAME)
            with open(mpath, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["engines"][0]["files"].append(dict(doc["engines"][0]["files"][0]))
            with open(mpath, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            with self.assertRaises(sp.StageError):
                sp.stage(pkg, os.path.join(d, "data-dup"), "win-x64", contract)

    def test_contract_escape_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            caps = two_engines()
            caps["对话"]["os"]["win-x64"]["asset_dir"] = "<data>/runtime/../../etc"
            contract = write_contract(d, caps)
            pkg = write_pkg(d, "win-x64", [{"engine": "llama", "asset": "llama.zip",
                                            "files": {"llama-server.exe": b"bin"}}])
            with self.assertRaises(sp.StageError):
                sp.stage(pkg, os.path.join(d, "data"), "win-x64", contract)

    def test_cli_writes_report_and_returns_nonzero_on_bad_package(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            contract = write_contract(d, two_engines())
            pkg = write_pkg(d, "win-x64", [{"engine": "llama", "asset": "llama.zip",
                                            "files": {"llama-server.exe": b"bin"}}])
            data = os.path.join(d, "data")
            report = os.path.join(d, "report.json")
            ok = subprocess.run([sys.executable, CLI, "--package", pkg, "--data", data,
                                 "--os", "win-x64", "--contract", contract, "--json", report],
                                text=True, encoding="utf-8", errors="replace",
                                capture_output=True)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            with open(report, encoding="utf-8") as fh:
                doc = json.load(fh)
            self.assertEqual(doc["staged"][0]["engine"], "llama")
            bad = subprocess.run([sys.executable, CLI, "--package", os.path.join(d, "nope"),
                                  "--data", os.path.join(d, "nope-data"),
                                  "--os", "win-x64", "--contract", contract],
                                 text=True, encoding="utf-8", errors="replace",
                                 capture_output=True)
            self.assertEqual(bad.returncode, 1)
            self.assertIn("BAD", bad.stdout)


if __name__ == "__main__":
    unittest.main()
