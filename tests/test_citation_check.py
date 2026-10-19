"""`tools/citation_check.py` 与 `pins/citations.json` 的测试。

这里全部离线：真联网核对留给 CI 车道（`citation-drift`），单测只查三件 CI 查不到的事 ——
① 判据函数自己会不会红（含工具自带的一组对照），② 提交进仓库的这张表**自不自洽**
（行号上那一行真的含着 expect 吗、ref 是不是可不可变坐标、有没有人手写错一个数字），
③ `--cache` 模式不得把"缓存里没有原文"说成"查过且通过"（TestCacheMode）。
"""
import contextlib
import copy
import importlib.util
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
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
                     "sd-reflect-origin", "llama-host-default", "sd-listen-default",
                     # 2026-10-20 新加的两条常驻服务车道所依赖的出处
                     "ws-health-ok", "ws-health-loading", "ws-file-field", "ws-srt-mime",
                     "sd-listen-ip", "sd-listen-after-load", "sd-imggen-202", "sd-job-images",
                     "sd-embed-default", "sd-steps-key"):
            self.assertIn(must, ids)

    def test_spec_clears_the_floor_the_code_enforces(self) -> None:
        """地板写死在代码里，文档写"至少 20 条"：这条把两边钉成同一个数，
        否则"表被删空了"与"文档改了个数字"这两种情况会长得一模一样。"""
        self.assertGreaterEqual(len(self.spec()), cc.MIN_CITATIONS)
        self.assertEqual(cc.MIN_CITATIONS, 20)

    def test_thin_spec_is_refused(self) -> None:
        """覆盖度地板是有承重的：拿一张 3 条的表跑，结论必须是红，不能"少查也算过"。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "thin.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(self.spec()[:3], fh)
            self.assertEqual(cc.main(["--spec", p]), 1)


class TestCacheMode(unittest.TestCase):
    """`--cache` 是给"别拿网络当判据"用的：本机复算时从缓存读原文。
    它最容易骗人的地方是"缓存里没有原文"被说成"查过了、没问题"。

    这里不能拿一张只有一条的表跑：地板（`MIN_CITATIONS`）会先把它拒了，
    于是三条用例全在测地板分支，缓存的跳过/命中/红一条都没碰过 ——
    第一版就是这么写错的，红过之后才看出来。正向对照改用"把第一条复制 N 遍"：
    这些行要的原文是同一个缓存文件、同一行、同一个子串，所以能一起满足。
    """

    @staticmethod
    def spec():
        with open(SPEC_PATH, encoding="utf-8") as fh:
            return json.load(fh)

    def run_cached(self, text, shift=0, rows=None):
        import tempfile
        full = copy.deepcopy(self.spec())
        c = full[0]
        if shift:
            c["line"] += shift
        spec = [copy.deepcopy(c) for _ in range(rows)] if rows else full
        with tempfile.TemporaryDirectory() as d:
            if text is not None:
                with open(os.path.join(d, cc.cache_name(c)), "w", encoding="utf-8",
                          newline="\n") as fh:
                    fh.write(text)
            p = os.path.join(d, "spec.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(spec, fh, ensure_ascii=False)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cc.main(["--spec", p, "--cache", d])
            return rc, buf.getvalue(), len(spec)

    def test_cache_name_has_no_path_separators(self) -> None:
        """repo 本身带斜杠（ggml-org/llama.cpp）。第一版只换 path 的斜杠，
        --fill 第一次写就 FileNotFoundError —— 名字必须是一个扁平文件名。"""
        for c in self.spec()[:6]:
            n = cc.cache_name(c)
            self.assertNotIn("/", n)
            self.assertNotIn("\\", n)

    def test_empty_cache_says_it_checked_nothing(self) -> None:
        rc, log, n = self.run_cached(None)
        self.assertEqual(rc, 0, log)
        self.assertIn("真查了 0 条，缓存里没有原文而跳过 %d 条" % n, log)
        self.assertIn("不是通过，是没查", log)

    def test_cached_line_verifies_and_reports_the_count(self) -> None:
        c = self.spec()[0]
        text = "\n" * (c["line"] - 1) + "x " + c["expect"] + " y\n"
        rc, log, n = self.run_cached(text, rows=cc.MIN_CITATIONS)
        self.assertEqual(rc, 0, log)
        self.assertIn("核对 %d 条 file:line 出处，问题 0 条；真查了 %d 条" % (n, n), log)

    def test_wrong_line_in_cache_is_red(self) -> None:
        """行号挪一位就必须红 —— 这条是文档里那句"行号整体 +1 全红"的可复现版本。
        两个方向都测：同一份原文，行号对得上就过、差一行就红，
        否则很容易写出" mutate 没落到文件上"的假验证。"""
        c = self.spec()[0]
        text = ("\n" * (c["line"] - 1)) + c["expect"] + "\nnot what we want\n"
        rc_ok, log_ok, _ = self.run_cached(text, rows=cc.MIN_CITATIONS)
        self.assertEqual(rc_ok, 0, log_ok)
        rc, log, _ = self.run_cached(text, shift=1, rows=cc.MIN_CITATIONS)
        self.assertEqual(rc, 1, log)
        self.assertIn("不含", log)
        self.assertIn(c["id"], log)

    def test_whole_table_checks_clean_from_a_full_cache(self) -> None:
        """整表在缓存上必须 0 问题。缓存不在仓库里（.gitignore 的 .cache/refs），
        所以这条只在有缓存的机器上跑，没有就明确 skip —— 不许把"没跑"报成"绿了"。"""
        d = os.path.join(ROOT, ".cache", "refs")
        need = {cc.cache_name(c) for c in self.spec()}
        have = set(os.listdir(d)) if os.path.isdir(d) else set()
        if not need <= have:
            self.skipTest("缓存缺 %d 份原文（共需 %d），先跑 "
                          "python tools/citation_check.py --cache .cache/refs --fill"
                          % (len(need - have), len(need)))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cc.main(["--cache", d])
        log = buf.getvalue()
        self.assertEqual(rc, 0, log)
        self.assertIn("问题 0 条", log)
        self.assertIn("真查了 %d 条" % len(self.spec()), log)


if __name__ == "__main__":
    unittest.main()
