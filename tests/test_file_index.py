"""`tools/file_index.py` 的离线测试：下载层的"两家一致才可信"这条规矩本身得是可证的。

全部不联网（`http_json` / `hf_files` / `ms_files` 用替身），因为要证明的是**判定形状**：
两家不等时必须没有"可信哈希"、只有一家时必须标 single 且不许提前放行、
取数失败必须说"取不到"而不是"这仓库没这文件"。
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import file_index as fi  # noqa: E402

SHA = "f" * 64
OTHER = "a" * 64


def hf_tree(**entries):
    return {k: {"sha256": v, "size": 10} for k, v in entries.items()}


class TestClassify(unittest.TestCase):
    def test_two_providers_agree_is_the_only_trusted_state(self) -> None:
        row = fi.classify("a.gguf", hf_tree(**{"a.gguf": SHA}), hf_tree(**{"a.gguf": SHA}),
                          "HF", "MS")
        self.assertEqual((row["status"], row["usable_before_download"]), ("agree", True))
        self.assertEqual(row["sha256"], SHA)

    def test_conflict_refuses_to_pick_one(self) -> None:
        row = fi.classify("a.gguf", hf_tree(**{"a.gguf": SHA}), hf_tree(**{"a.gguf": OTHER}),
                          "HF", "MS")
        self.assertEqual(row["status"], "conflict")
        self.assertIsNone(row["sha256"], "两家不等时不许返回任何一个当答案")
        self.assertFalse(row["usable_before_download"])

    def test_single_provider_is_not_pre_trusted(self) -> None:
        row = fi.classify("a.gguf", hf_tree(**{"a.gguf": SHA}), {}, "HF", "MS")
        self.assertEqual((row["status"], row["usable_before_download"]), ("single", False))

    def test_absent_file(self) -> None:
        self.assertEqual(fi.classify("nope.gguf", {}, {}, "HF", "MS")["status"], "absent")

    def test_agreeing_metadata_that_disagrees_with_the_pin_is_worse_than_conflict(self) -> None:
        """两家都说 A、而仓库里钉的是 B —— 这就是"上游被换过"，绝不能因为两家一致就放过。"""
        row = fi.classify("a.gguf", hf_tree(**{"a.gguf": SHA}), hf_tree(**{"a.gguf": SHA}),
                          "HF", "MS", want_sha=OTHER)
        self.assertEqual((row["status"], row["usable_before_download"]), ("pin_conflict", False))

    def test_size_comes_from_metadata(self) -> None:
        self.assertEqual(fi.classify("a.gguf", hf_tree(**{"a.gguf": SHA}),
                                     hf_tree(**{"a.gguf": SHA}), "HF", "MS")["size"], 10)


class TestCandidates(unittest.TestCase):
    def test_order_follows_measured_egress_not_wishes(self) -> None:
        got = [c["source"] for c in fi.candidates("Qwen/X-GGUF", "q.bin")]
        self.assertEqual(got, ["modelscope", "hf-mirror.com", "huggingface.co"])

    def test_huggingface_direct_is_not_a_first_choice_in_cn(self) -> None:
        c = [x for x in fi.candidates("A/B", "q.bin") if x["source"] == "huggingface.co"][0]
        self.assertEqual(c["measured_mib_s"], 0.0)
        self.assertIn("CN", c["note"])

    def test_every_candidate_has_a_real_url(self) -> None:
        for c in fi.candidates("A/B", "q.bin"):
            self.assertTrue(c["url"].startswith("https://"), c)
            self.assertIn("q.bin", c["url"])


class TestMarkerDialect(unittest.TestCase):
    def test_marker_is_not_a_curl_file_reference(self) -> None:
        self.assertFalse(fi.MARKER.startswith("@"), fi.MARKER)
        self.assertNotIn(chr(10), fi.MARKER)


class TestHfFiles(unittest.TestCase):
    def test_only_file_entries_and_lfs_oid(self) -> None:
        real = fi.http_json
        fi.http_json = lambda url, timeout=60: (
            [{"type": "directory", "path": "sub"},
             {"type": "file", "path": "m.gguf", "lfs": {"oid": SHA, "size": 7}, "size": 7}],
            "huggingface.co", None)
        try:
            out, by, err = fi.hf_files("A/B")
            self.assertEqual(list(out), ["m.gguf"])
            self.assertEqual(out["m.gguf"]["sha256"], SHA)
            self.assertEqual(by, "huggingface.co")
            self.assertIsNone(err)
        finally:
            fi.http_json = real

    def test_second_entrypoint_is_used_when_first_fails(self) -> None:
        calls = []

        def fake(url, timeout=60):
            calls.append(url.split("/")[2])
            if "hf-mirror" in url:
                return None, "", "curl 退出码 28"
            return [{"type": "file", "path": "m.gguf", "lfs": {"oid": SHA, "size": 7}}], "huggingface.co", None
        real = fi.http_json
        fi.http_json = fake
        try:
            out, by, err = fi.hf_files("A/B")
            self.assertIsNone(err)
            self.assertEqual(calls, ["hf-mirror.com", "huggingface.co"])
            self.assertEqual(by, "huggingface.co")
            self.assertEqual(out["m.gguf"]["sha256"], SHA)
        finally:
            fi.http_json = real

    def test_both_failing_is_an_error_not_an_empty_tree(self) -> None:
        real = fi.http_json
        fi.http_json = lambda url, timeout=60: (None, "", "curl 退出码 6")
        try:
            out, _by, err = fi.hf_files("A/B")
            self.assertEqual(out, {})
            self.assertTrue(err, "两家都取不到必须报出来，否则会被读成「这仓库没文件」")
        finally:
            fi.http_json = real


class TestControl(unittest.TestCase):
    """control() 的三种结局：确认、取数失败、以及负向对照必须真的失效过。"""

    PINS = {"m.gguf": {"sha256": SHA, "size": 7,
                       "url": f"https://hf-mirror.com/A/B/resolve/main/m.gguf"}}

    def run_control(self, hf_ret, ms_ret, pins=None, floor=1):
        real_hf, real_ms = fi.hf_files, fi.ms_files
        fi.hf_files, fi.ms_files = lambda repo, rev="main": hf_ret, lambda repo, rev="master": ms_ret
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pins.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(pins or self.PINS, fh)
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    rc = fi.control(p, floor)
            finally:
                fi.hf_files, fi.ms_files = real_hf, real_ms
            return rc, buf.getvalue()

    def test_agreeing_providers_pass(self) -> None:
        tree = {"m.gguf": {"sha256": SHA, "size": 7}}
        rc, out = self.run_control((tree, "huggingface.co", None), (tree, "www.modelscope.cn", None))
        self.assertEqual(rc, 0, out)

    def test_transport_failure_is_named_as_failure(self) -> None:
        rc, out = self.run_control(({}, "", "curl 退出码 28"), ({}, "", None))
        self.assertEqual(rc, 1)
        self.assertIn("取数失败", out, out)
        self.assertNotIn("absent", out, "取不到不该被说成没这个文件")

    def test_missing_file_is_red(self) -> None:
        rc, out = self.run_control(({}, "huggingface.co", None), ({}, "www.modelscope.cn", None))
        self.assertEqual(rc, 1)
        self.assertIn("absent", out)

    def test_negative_control_would_catch_a_rubber_stamp(self) -> None:
        """如果比对逻辑对任何东西都"有答案"，那根故意造出来的假文件名就会被判成 agree ——
        这条就是为了让那种实现红。"""
        tree = {"m.gguf": {"sha256": SHA, "size": 7}, "__deliberately_absent__m.gguf":
                {"sha256": SHA, "size": 7}}
        rc, out = self.run_control((tree, "HF", None), (tree, "MS", None))
        self.assertEqual(rc, 1, out)
        self.assertIn("负向对照失效", out)

    def test_floor_needs_two_confirmed(self) -> None:
        """只有一条被两家确认时不许报"通过"—— 覆盖度掉了必须能看见。"""
        pins = {k: {"sha256": SHA, "size": 7, "url": f"https://hf-mirror.com/A/B/resolve/main/{k}"}
                for k in ("m1.gguf", "m2.gguf")}
        tree = {"m1.gguf": {"sha256": SHA, "size": 7}}
        rc, out = self.run_control((tree, "HF", None), (tree, "MS", None), pins=pins, floor=2)
        self.assertEqual(rc, 1)
        self.assertIn("地板", out)


class TestSelfCheck(unittest.TestCase):
    def test_offline_controls_all_pass(self) -> None:
        self.assertEqual(fi.selfcheck(), 0)

    def test_reused_rate_table_imports_without_side_effects(self) -> None:
        """file_index 直接复用 r2_manifest 的实测速率表，前提是这个 import 不打印也不写盘。
        哪天它被改成脚本式（模块级就跑起来），这条要红，而不是让 CI 里一次 import 顺手生成清单。"""
        import importlib
        import r2_manifest
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            importlib.reload(r2_manifest)
        self.assertEqual(buf.getvalue(), "")
        self.assertTrue(r2_manifest.RATES_MIB_S)


if __name__ == "__main__":
    unittest.main()
