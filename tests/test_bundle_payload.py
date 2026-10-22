"""随包载荷：把交付清单变成安装包里的对象键布局时，会静默错的几件事。

  * 清单自己没验：键不等于前缀+落点、同落点两次、`..` 跑出资源目录、根清单没标 `land=last`；
  * 源文件体积或 sha256 与清单不符，却照样被复制进安装包；
  * 根清单先写 —— 半套对象配上一份"看起来完整"的表，外壳会按它落盘；
  * `--check` 只看表不看字节，或者放过多出来的文件，让包内实际内容与表脱钩。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
import sys

sys.path.insert(0, os.path.join(ROOT, "tools"))
import bundle_payload as bp  # noqa: E402

PREFIX = "payload/linux-x64"


def fixture(root: str, platform: str = "linux-x64") -> tuple[str, str]:
    stage = os.path.join(root, "stage")
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
        rows.append({"dest": dest, "key": f"payload/{platform}/{dest}",
                     "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()})
        total += len(body)
    root_body = json.dumps({"os": platform, "engines": []},
                           separators=(",", ":")).encode()
    root_path = os.path.join(stage, bp.MANIFEST_NAME)
    with open(root_path, "wb") as fh:
        fh.write(root_body)
    doc = {
        "schema": 1,
        "os": platform,
        "key_prefix": f"payload/{platform}",
        "manifest": {"dest": bp.MANIFEST_NAME,
                     "key": f"payload/{platform}/{bp.MANIFEST_NAME}",
                     "bytes": len(root_body),
                     "sha256": hashlib.sha256(root_body).hexdigest(),
                     "land": "last"},
        "files": rows,
        "totals": {"files": len(rows), "bytes": total},
    }
    manifest_path = os.path.join(root, "delivery-manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return stage, manifest_path


def read(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_doc(path: str, doc: dict) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


class BundleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.stage, self.manifest = fixture(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def bundle(self, out: str = "out", stage: str | None = None,
               manifest: str | None = None) -> tuple[str, dict]:
        out_root = os.path.join(self.tmp, out)
        report = bp.bundle(manifest or self.manifest, stage or self.stage, out_root)
        return out_root, report

    def test_bundle_lays_out_object_keys_and_reports_totals(self) -> None:
        out, report = self.bundle()
        self.assertEqual(report, {"os": "linux-x64", "files": 2, "bytes": 40,
                                  "root_manifest": bp.MANIFEST_NAME,
                                  "table": bp.TABLE_NAME})
        self.assertTrue(os.path.isfile(os.path.join(out, bp.TABLE_NAME)))
        self.assertFalse(os.path.isfile(os.path.join(out, bp.MANIFEST_NAME)))
        self.assertTrue(os.path.isfile(
            os.path.join(out, PREFIX, "runtime/linux-x64/llama/b9/llama-server")))
        self.assertTrue(os.path.isfile(
            os.path.join(out, PREFIX, bp.MANIFEST_NAME)))

    def test_root_table_is_written_only_after_every_object(self) -> None:
        expected = [row["key"] for row in read(self.manifest)["files"]]
        seen_at_write = []
        original = bp._write_table

        def spy(out_root, blob):
            seen_at_write.append([key for key in expected
                                  if os.path.isfile(os.path.join(out_root, *key.split("/")))])
            original(out_root, blob)

        bp._write_table = spy
        try:
            out, _ = self.bundle()
        finally:
            bp._write_table = original
        self.assertEqual(seen_at_write, [expected])
        self.assertTrue(os.path.isfile(os.path.join(out, bp.TABLE_NAME)))

    def test_check_accepts_fresh_bundle(self) -> None:
        out, _ = self.bundle()
        report = bp.check(self.manifest, out)
        self.assertEqual(report, {"os": "linux-x64", "files": 2, "bytes": 40,
                                  "table": bp.TABLE_NAME})

    def test_check_rejects_tampered_bundled_table(self) -> None:
        out, _ = self.bundle()
        with open(os.path.join(out, bp.TABLE_NAME), "ab") as fh:
            fh.write(b" ")
        with self.assertRaisesRegex(bp.BundleError, "逐字节相同"):
            bp.check(self.manifest, out)

    def test_check_rejects_extra_file(self) -> None:
        out, _ = self.bundle()
        with open(os.path.join(out, "ghost.bin"), "wb") as fh:
            fh.write(b"ghost")
        with self.assertRaisesRegex(bp.BundleError, "清单外文件"):
            bp.check(self.manifest, out)

    def test_check_rejects_missing_object(self) -> None:
        out, _ = self.bundle()
        os.unlink(os.path.join(out, PREFIX, "runtime/linux-x64/llama/b9/libggml.so"))
        with self.assertRaisesRegex(bp.BundleError, "缺文件"):
            bp.check(self.manifest, out)

    def test_check_rejects_tampered_object_bytes(self) -> None:
        out, _ = self.bundle()
        with open(os.path.join(out, PREFIX, "runtime/linux-x64/llama/b9/llama-server"),
                  "ab") as fh:
            fh.write(b"tamper")
        with self.assertRaisesRegex(bp.BundleError, "字节与清单不符"):
            bp.check(self.manifest, out)

    def test_bundle_rejects_tampered_source_bytes(self) -> None:
        with open(os.path.join(self.stage, "runtime/linux-x64/llama/b9/llama-server"),
                  "ab") as fh:
            fh.write(b"tamper")
        with self.assertRaisesRegex(bp.BundleError, "清单写 .* 字节"):
            bp.bundle(self.manifest, self.stage, os.path.join(self.tmp, "out"))

    def test_bundle_rejects_source_sha_mismatch(self) -> None:
        path = os.path.join(self.stage, "runtime/linux-x64/llama/b9/libggml.so")
        with open(path, "wb") as fh:
            fh.write(b"synthetic shared librarX")
        with self.assertRaisesRegex(bp.BundleError, "sha256 与清单不符"):
            bp.bundle(self.manifest, self.stage, os.path.join(self.tmp, "out"))

    def test_bundle_rejects_missing_source(self) -> None:
        os.unlink(os.path.join(self.stage, "runtime/linux-x64/llama/b9/libggml.so"))
        with self.assertRaisesRegex(bp.BundleError, "不是普通文件"):
            bp.bundle(self.manifest, self.stage, os.path.join(self.tmp, "out"))

    def test_bundle_rejects_nonempty_out_without_touching_it(self) -> None:
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        keep = os.path.join(out, "keep.txt")
        with open(keep, "w", encoding="utf-8") as fh:
            fh.write("do not overwrite")
        with self.assertRaisesRegex(bp.BundleError, "非空"):
            bp.bundle(self.manifest, self.stage, out)
        self.assertTrue(os.path.isfile(keep))
        self.assertFalse(os.path.exists(os.path.join(out, bp.TABLE_NAME)))
        self.assertFalse(os.path.exists(os.path.join(out, PREFIX)))

    def test_copy_failure_leaves_no_table_or_partial_objects_credited(self) -> None:
        out = os.path.join(self.tmp, "out")
        original = bp._copy_verified
        calls = {"n": 0}

        def boom(src, dst, row):
            calls["n"] += 1
            if calls["n"] == 2:
                raise bp.BundleError("模拟第二次复制失败")
            return original(src, dst, row)

        bp._copy_verified = boom
        try:
            with self.assertRaisesRegex(bp.BundleError, "模拟第二次复制失败"):
                bp.bundle(self.manifest, self.stage, out)
        finally:
            bp._copy_verified = original
        self.assertFalse(os.path.exists(os.path.join(out, bp.TABLE_NAME)))
        with self.assertRaisesRegex(bp.BundleError, "没有"):
            bp.check(self.manifest, out)

    def test_bundle_rejects_missing_stage_root(self) -> None:
        with self.assertRaisesRegex(bp.BundleError, "摆好的数据根不在"):
            bp.bundle(self.manifest, os.path.join(self.tmp, "nope"),
                      os.path.join(self.tmp, "out"))


class ValidateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.stage, self.manifest = fixture(self.tmp)
        self.doc = read(self.manifest)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def red(self, doc: dict, pattern: str) -> None:
        path = write_doc(os.path.join(self.tmp, "bad.json"), doc)
        with self.assertRaisesRegex(bp.BundleError, pattern):
            bp.bundle(path, self.stage, os.path.join(self.tmp, "out"))

    def test_rejects_key_not_derived_from_prefix(self) -> None:
        self.doc["files"][0]["key"] = "payload/win-x64/nope"
        self.red(self.doc, "按前缀应当是")

    def test_rejects_duplicate_dest(self) -> None:
        self.doc["files"].append(dict(self.doc["files"][0]))
        self.red(self.doc, "同落点|同一个落点")

    def test_rejects_path_escape(self) -> None:
        self.doc["files"][0]["dest"] = "../escape"
        self.doc["files"][0]["key"] = "payload/linux-x64/../escape"
        self.red(self.doc, "不安全的段")

    def test_rejects_root_manifest_not_last(self) -> None:
        self.doc["manifest"]["land"] = "first"
        self.red(self.doc, "land=last")

    def test_rejects_root_manifest_wrong_dest(self) -> None:
        self.doc["manifest"]["dest"] = "other.json"
        self.red(self.doc, "根清单落点")

    def test_rejects_root_manifest_key_collision(self) -> None:
        self.doc["files"][0]["dest"] = bp.MANIFEST_NAME
        self.doc["files"][0]["key"] = f"{PREFIX}/{bp.MANIFEST_NAME}"
        self.red(self.doc, "根清单")

    def test_rejects_wrong_schema(self) -> None:
        self.doc["schema"] = 2
        self.red(self.doc, "只认 1")

    def test_rejects_zero_bytes(self) -> None:
        self.doc["files"][0]["bytes"] = 0
        self.red(self.doc, "正整数")

    def test_rejects_uppercase_sha(self) -> None:
        self.doc["files"][0]["sha256"] = "A" * 64
        self.red(self.doc, "小写十六进制")

    def test_rejects_totals_drift(self) -> None:
        self.doc["totals"]["bytes"] += 1
        self.red(self.doc, "逐行相加")

    def test_rejects_empty_file_table(self) -> None:
        self.doc["files"] = []
        self.red(self.doc, "一个文件都没有")

    def test_rejects_unsafe_prefix(self) -> None:
        self.doc["key_prefix"] = "payload/../linux"
        self.doc["manifest"]["key"] = "payload/../linux/" + bp.MANIFEST_NAME
        self.red(self.doc, "不安全的段")


class CliTest(unittest.TestCase):
    def test_cli_bundle_check_and_json_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stage, manifest = fixture(tmp)
            out = os.path.join(tmp, "out")
            report_path = os.path.join(tmp, "report.json")
            self.assertEqual(bp.main(["--manifest", manifest, "--stage", stage,
                                      "--out", out, "--json", report_path]), 0)
            self.assertEqual(read(report_path)["files"], 2)
            self.assertEqual(bp.main(["--manifest", manifest, "--out", out,
                                      "--check"]), 0)
            with open(os.path.join(out, bp.TABLE_NAME), "ab") as fh:
                fh.write(b" ")
            self.assertEqual(bp.main(["--manifest", manifest, "--out", out,
                                      "--check"]), 1)

    def test_selftest_passes(self) -> None:
        self.assertEqual(bp.main(["--selftest"]), 0)


if __name__ == "__main__":
    unittest.main()
