"""`tools/citation_check.py` 与 `pins/citations.json` 的测试。

这里全部离线：网络核对留给 CI 车道（`citation-drift`），单测只查两件 CI 查不到的事 ——
① 判据函数自己会不会红（含工具自带的一组对照），② 提交进仓库的这张表**自不自洽**
（行号上那一行真的含着 expect 吗、ref 是不是可不可变坐标、有没有人手写错一个数字）。
"""
import importlib.util
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import citation_check as cc  # noqa: E402

SPEC_PATH = os.path.join(HERE, "..", "pins", "citations.json")
TEXT = 'line one\n  cors_origins = "*";\nline three'
GOOD = {"id": "t", "repo": "a/b", "ref": "r", "path": "p", "line": 2,
        'expect': 'cors_origins = "*"', "claim": "CORS 默认 *"}


class TestMatcher(unittest.TestCase):
    def test_tool_ships_its_own_controls(self) -> None:
        self.assertEqual(cc.selftest(), 0)

    def test_matching_line_is_not_a_problem(self) -> None:
        self.assertEqual(cc.check_one(GOOD, TEXT), [])

    def test_changed_default_is_reported_with_the_real_line(self) -> None:
        """红字必须带上游那一行实际写了什么，否则"红了"没有信息量。"""
        bad = cc.check_one(dict(GOOD, expect='cors_origins = "none"'), TEXT)
        self.assertEqual(len(bad), 1)
        self.assertIn('cors_origins = "*"', bad[0])

    def test_line_number_out_of_range_names_the_file_size(self) -> None:
        bad = cc.check_one(dict(GOOD, line=999), TEXT)
        self.assertIn("只有 3 行", bad[0])

    def test_unfetchable_source_is_a_problem_not_a_skip(self) -> None:
        self.assertEqual(len(cc.check_one(GOOD, None)), 1)

    def test_missing_keys_crash_loudly(self) -> None:
        for key in ("line", "expect", "path"):
            with self.assertRaises(KeyError, msg=key):
                cc.check_one({k: v for k, v in GOOD.items() if k != key}, TEXT)

    def test_url_is_built_from_pinned_coord(self) -> None:
        self.assertEqual(
            cc.url_of({"repo": "x/y", "ref": "abc123", "path": "a/b.cpp"}),
            "https://raw.githubusercontent.com/x/y/abc123/a/b.cpp")


class TestCommittedSpec(unittest.TestCase):
    def spec(self):
        with open(SPEC_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def test_covers_the_three_servers_and_the_lane_flags(self) -> None:
        s = self.spec()
        self.assertGreaterEqual(len(s), 20, f"只有 {len(s)} 条，这张表的意义就是覆盖度")
        repos = {c["repo"] for c in s}
        self.assertEqual(repos, {"ggml-org/llama.cpp", "ggml-org/whisper.cpp",
                                 "leejet/stable-diffusion.cpp", "GreatV/oar-ocr",
                                 "tauri-apps/tauri-docs", "actions/runner-images"}, repos)
        ids = [c["id"] for c in s]
        self.assertEqual(len(ids), len(set(ids)), "id 重复会让一条声明永远查不到")

    def test_every_ref_is_immutable(self) -> None:
        """分支名不能当出处：本轮就是"按 HEAD 读成员表、却钉了 tag"才发现 tag 里没有 CLI。"""
        for c in self.spec():
            self.assertNotIn(c["ref"], ("main", "master", "HEAD", "latest"), c["id"])

    def test_spec_is_self_consistent(self) -> None:
        """每条记录的 line_text 必须真的含 expect —— 抓"行号或子串被手抄错"。
        这条不查网络，所以本地就能挡住一张自相矛盾的表。
        absent 那条走另一套形状：没有行号、也不该有抄录行。"""
        for c in self.spec():
            if c.get("absent"):
                self.assertIsNone(c.get("line"), f"{c['id']} 声明的是「不存在」，不该带行号")
                self.assertNotIn(c["expect"], c["line_text"], c["id"])
                continue
            self.assertIn(c["expect"], c["line_text"], f"{c['id']} 的 expect 与抄录的整行不符")
            self.assertGreaterEqual(c["line"], 1, c["id"])

    def test_absent_claims_are_two_sided(self) -> None:
        """"这份文件里没有 X"同样要被查：假装缺席必须红，真缺席必须过。"""
        text = "line one" + chr(10) + "Rust 1.99.1" + chr(10)
        hit = {"id": "x", "repo": "a/b", "ref": "r", "path": "p", "expect": "Rust",
               "claim": "c", "absent": True}
        self.assertEqual(len(cc.check_one(hit, text)), 1)
        self.assertIn("不该出现", cc.check_one(hit, text)[0])
        self.assertEqual(cc.check_one(dict(hit, expect="webkit2gtk"), text), [])

    def test_absent_claim_still_needs_the_file(self) -> None:
        """取不到文件时"没找到"不许被当成"证实了缺席"。"""
        c = {"id": "x", "repo": "a/b", "ref": "r", "path": "p", "expect": "webkit2gtk",
             "claim": "c", "absent": True}
        bad = cc.check_one(c, None)
        self.assertEqual(len(bad), 1)
        self.assertIn("取不到", bad[0])

    def test_security_claims_are_on_the_list(self) -> None:
        """这一轮的结论是"默认无鉴权 + CORS *"，这几条不许哪天被顺手删掉。"""
        ids = {c["id"] for c in self.spec()}
        for must in ("llama-cors-star", "llama-apikeys-empty", "whisper-cors-star",
                     "sd-reflect-origin", "llama-host-default", "sd-listen-default"):
            self.assertIn(must, ids)

    def test_thin_spec_is_refused(self) -> None:
        """覆盖度地板是有承重的：拿一张 3 条的表跑，结论必须是红，不能"少查也算过"。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "thin.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(self.spec()[:3], fh)
            self.assertEqual(cc.main(["--spec", p]), 1)


if __name__ == "__main__":
    unittest.main()
