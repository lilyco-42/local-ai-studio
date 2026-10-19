"""`tools/ocr_build_surface.py` 的离线测试。

这工具唯一的作用是回答一个问题：**"钉住 commit" 对 OCR 到底钉住了什么**。
分类逻辑与漂移比对都是纯函数，可以在不联网的情况下用合成 manifest 证明它们不是永远绿；
联网那半（measure / lockfile_state）留给 CI 车道。
"""
import contextlib
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import ocr_build_surface as obs  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, ".."))

MANIFEST = '''
[package]
name = "oar-ocr-cli"

[dependencies]
anyhow = "1"
ort = { version = "=2.0.0-rc.13", default-features = false }
oar-ocr = { version = "0.10.0", path = "..", default-features = false }
imageproc = { workspace = true }
libm = { git = "https://github.com/rust-lang/libm", rev = "abc123" }
'''

WORKSPACE = '''
[workspace]
members = ["oar-ocr-cli"]

[workspace.dependencies]
imageproc = "0.27.0"
regex = "^1.11"
'''


def rows(text, crate="x"):
    return obs.deps_from_toml(text, crate)


class TestClassify(unittest.TestCase):
    def test_caret_default_is_not_pinned(self) -> None:
        """cargo 里 `"1"` 就是 `^1`：这是最容易被当成"钉住了"的一种写法。"""
        self.assertEqual(obs.classify("anyhow", "1")["kind"], "caret")

    def test_exact_and_git_rev_are_pinned(self) -> None:
        self.assertEqual(obs.classify("ort", {"version": "=2.0.0-rc.13"})["kind"], "exact")
        self.assertEqual(obs.classify("libm", {"git": "u", "rev": "abc"})["kind"], "git-rev")
        self.assertIn("exact", obs.PINNED_KINDS)
        self.assertIn("git-rev", obs.PINNED_KINDS)

    def test_git_without_rev_is_floating(self) -> None:
        self.assertEqual(obs.classify("libm", {"git": "u"})["kind"], "git-floating")

    def test_workspace_pointer_is_its_own_kind(self) -> None:
        """上一版把 `{ workspace = true }` 归成 unknown，报告就长出 14 条假"未钉"。"""
        self.assertEqual(obs.classify("imageproc", {"workspace": True})["kind"], "workspace-inherited")
        self.assertNotIn(obs.classify("imageproc", {"workspace": True})["kind"],
                         obs.PINNED_KINDS)

    def test_path_dep_is_not_an_external_package(self) -> None:
        self.assertEqual(obs.classify("oar-ocr", {"version": "0.10.0", "path": ".."})["kind"], "path")

    def test_range_and_tilde_are_named_not_binned(self) -> None:
        self.assertEqual(obs.classify("a", ">=1, <2")["kind"], "range")
        self.assertEqual(obs.classify("b", "~1.2")["kind"], "tilde")

    def test_sections_are_separated(self) -> None:
        """`[package]` / `[[bin]]` 里的 readme、path 之类不能混进依赖计数 ——
        第一版拿正则扫全文，57 条里混了一堆 `readme = "README.md"`。"""
        names = {r["name"] for r in rows(MANIFEST)}
        self.assertEqual(names, {"anyhow", "ort", "oar-ocr", "imageproc", "libm"})
        self.assertNotIn("name", names)

    def test_workspace_table_is_collected(self) -> None:
        got = {r["name"]: r["kind"] for r in rows(WORKSPACE, "_workspace")}
        self.assertEqual(got, {"imageproc": "caret", "regex": "caret"})


class TestDiff(unittest.TestCase):
    def rec(self, live):
        return {"ref": live["ref"], "fingerprint": live["fingerprint"],
                "unpinned_packages": [],
                "lockfile_upstream": {"cargo_lock_http": 404, "control_cargo_toml_http": 206,
                                      "lock_absent": True, "control_ok": True}}

    def fake(self, fp="a" * 64, ctrl=206, lock=404):
        return {"ref": obs.REF, "fingerprint": fp, "unpinned_packages": [],
                "lockfile_upstream": {"cargo_lock_http": lock, "control_cargo_toml_http": ctrl,
                                      "lock_absent": lock == 404, "control_ok": ctrl in (200, 206)}}

    def test_same_measurement_is_not_a_problem(self) -> None:
        live = self.fake()
        self.assertEqual(obs.diff_against(self.rec(live), live), [])

    def test_fingerprint_drift_lists_what_changed(self) -> None:
        rec = self.rec(self.fake())
        rec["fingerprint"] = "b" * 64
        bad = obs.diff_against(rec, self.fake())
        self.assertEqual(len(bad), 1)
        self.assertIn("漂了", bad[0])

    def test_upstream_starting_to_ship_a_lock_is_reported(self) -> None:
        """哪天上游开始提交 Cargo.lock，比"漂了"更重要的信号 —— 车道该改成取它的锁。"""
        live = self.fake(lock=200)
        bad = obs.diff_against(self.rec(live), live)
        self.assertTrue(any("Cargo.lock" in b and "状态变了" in b for b in bad), bad)

    def test_failed_control_is_not_reported_as_drift(self) -> None:
        """正向对照挂了（Cargo.toml 取不到 2xx）时只能说"这次测量不可信"，
        不能报"上游漂了" —— 那是把网络问题写成结论。"""
        live = self.fake(ctrl=0, lock=0)
        bad = obs.diff_against(self.rec(live), live)
        self.assertEqual(len(bad), 1, bad)
        self.assertIn("正向对照失效", bad[0])

    def test_two_hundred_six_counts_as_present(self) -> None:
        """探测带 Range: bytes=0-0，所以存在时回 206；按 200 判会把每条对照都判成失效。"""
        self.assertTrue(self.fake(ctrl=206)["lockfile_upstream"]["control_ok"])
        self.assertFalse(self.fake(ctrl=404)["lockfile_upstream"]["control_ok"])
        self.assertEqual(obs.diff_against(self.rec(self.fake(ctrl=206)), self.fake(ctrl=206)), [])


class TestCommittedRecord(unittest.TestCase):
    def test_record_exists_and_says_inputs_are_not_pinned(self) -> None:
        with open(os.path.join(ROOT, "pins", "ocr-build-surface.json"), encoding="utf-8") as fh:
            rec = json.load(fh)
        self.assertEqual(rec["ref"], obs.REF)
        self.assertTrue(rec["lockfile_upstream"]["lock_absent"],
                        "上游若开始发 Cargo.lock，这条就该红着提醒改车道")
        self.assertGreaterEqual(len(rec["unpinned_packages"]), 20,
                                f"未钉的包只剩 {len(rec['unpinned_packages'])} 个？要么上游改严了，要么分类器坏了")
        self.assertIn("ort", rec["pinned"])
        # 分档加起来必须等于包总数，否则有包被漏 classify（这轮真的漏过 2 个）
        acc = len(rec["unpinned_packages"]) + len(rec["pinned"]) + len(rec["pointer_only"])
        self.assertEqual(acc, rec["unique_packages"],
                         f"{acc} ≠ {rec['unique_packages']}：{rec['unpinned_packages']}")

    def test_main_offline_check_refuses_a_missing_record(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            rc = obs.main(["--check", "--record-file",
                           os.path.join(os.sep, "nonexistent-dir", "x.json")])
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
