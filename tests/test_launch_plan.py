"""启动计划的测试：判据本身必须会红，产物必须能被重算，旗标必须来自表而不是手写。

分工要说清楚（这套结构是刻意的）：
  * 本文件**不联网** —— 它证明的是"判据不是装饰"：needle 换了要红、absent 的东西混进来要红、
    出处 id 引用错了要红、生图编码器与预算不一致要红。
  * 上游**真的**写着这些旗标 —— 那是车道的事（`launch-plan` 那步按 pin ref 现查）。
  * 已提交那张 `pins/launch-plan.json` 与离线重算那张必须逐字节等价（`check()` 深比）。
"""
from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import launch_plan as lp            # noqa: E402
import citation_check as cc         # noqa: E402

ART = os.path.join(ROOT, "pins", "launch-plan.json")
CITES = os.path.join(ROOT, "pins", "citations.json")
CLI = os.path.join(ROOT, "tools", "launch_plan.py")
OS3 = ["win-x64", "linux-x64", "macos-arm64"]
TIERS = ["4GB", "8GB", "16GB"]


def load(rel: str) -> dict:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return json.load(fh)


def artifact():
    return load("pins/launch-plan.json")


def recomputed():
    return lp.build(OS3, TIERS, "", False, check_provenance=False)[0]


def cite_ids():
    with open(CITES, encoding="utf-8") as fh:
        return {r["id"] for r in json.load(fh)}


def budget_stub(**per_tier):
    return {"per_tier": per_tier}


QFIX = os.path.join(ROOT, "tests", "fixtures", "first-run-queue.json")


def queue_report(mutate=None) -> dict:
    """另一个生产者的**真实捕获**（`file_index.py --queue … --json`），每次给一份新拷贝。"""
    with open(QFIX, encoding="utf-8") as fh:
        rep = copy.deepcopy(json.load(fh))
    if mutate:
        mutate(rep)
    return rep


def report_build(mod, rep: dict):
    return mod.build(OS3, TIERS, "", False, check_provenance=False,
                     tables={"queue_report": rep})


FLUX16 = budget_stub(**{"16GB": {
    "image_companions": {"needs": ["clip_l", "t5xxl", "VAE"]},
    "items": [{"role": "unet", "file": "flux.gguf"},
              {"companion_of": "flux.gguf", "need_key": "clip_l", "file": "clip_l-Q8_0.gguf"},
              {"companion_of": "flux.gguf", "need_key": "t5xxl", "file": "t5xxl-Q2_K.gguf"},
              {"companion_of": "flux.gguf", "need_key": "VAE", "file": "ae-f16.gguf"}]}})


class TestOfflineRecompute(unittest.TestCase):
    """产物不是"手写一张表再比一张表"：它必须能从输入重新算出来。"""

    def test_committed_artifact_equals_offline_recompute(self) -> None:
        self.assertEqual(lp.check(artifact(), recomputed()), [])

    def test_recompute_is_deterministic(self) -> None:
        self.assertEqual(recomputed(), recomputed())

    def test_check_detects_a_changed_bind_address(self) -> None:
        fresh = recomputed()
        fresh["capabilities"]["对话"]["bind"] = ["--host", "0.0.0.0", "--port", "{port}"]
        bad = lp.check(artifact(), fresh)
        self.assertTrue(any("bind" in x for x in bad), bad)

    def test_check_detects_a_new_capability(self) -> None:
        fresh = recomputed()
        fresh["capabilities"]["语音"] = {"mode": "server"}
        bad = lp.check(artifact(), fresh)
        self.assertTrue(any("新增了一项能力" in x for x in bad), bad)

    def test_check_detects_a_dropped_binary_name(self) -> None:
        fresh = recomputed()
        fresh["capabilities"]["生图"]["binaries"]["win-x64"] = "sd-server.exe.bak"
        self.assertTrue(any("名字变了" in x for x in lp.check(artifact(), fresh)))

    def test_positive_control_check_of_identical_docs_is_silent(self) -> None:
        """上面每条都要能红 —— 这条保证"红"不是因为判据永远红。"""
        self.assertEqual(lp.check(recomputed(), recomputed()), [])


class TestProvenanceGate(unittest.TestCase):
    """verify_spec 的四条分支各配一个已知答案：正向证明它认得，负向证明它抓得住。"""

    def setUp(self) -> None:
        self.real = cc.read_cache
        self.addCleanup(setattr, cc, "read_cache", self.real)

    def feed(self, texts: dict) -> None:
        """把"按 pin ref 取原文"换成给定的文本（按 path 认领）。"""
        def fake(c, cache):
            return texts.get(c["path"])
        cc.read_cache = fake

    def spec_for(self, cap: str) -> dict:
        return lp.SPEC[cap]

    def test_real_needles_pass_for_a_real_source(self) -> None:
        spec = self.spec_for("字幕")
        text = ('arg == "--host" { hostname = v; }\n'
                'arg == "--port" { port = atoi(v); }\n'
                'arg == "-m" { model = v; }\n'
                'svr_get("/health");\n')
        self.feed({p: text for _n, p in spec["verify"]})
        self.assertEqual(lp.verify_spec("字幕", spec, "cache", False), [])

    def test_renamed_flag_is_caught(self) -> None:
        spec = self.spec_for("字幕")
        text = ('arg == "--hostname" { x }\narg == "--port" { y }\n'
                'arg == "-m" { z }\nsvr_get("/health");\n')
        self.feed({p: text for _n, p in spec["verify"]})
        bad = lp.verify_spec("字幕", spec, "cache", False)
        self.assertEqual(len(bad), 1, bad)
        self.assertIn('arg == "--host"', bad[0])

    def test_flag_that_only_looks_absent_is_caught(self) -> None:
        """"whisper 没有鉴权"要是哪天上游加了 --api-key，结论必须当场翻。"""
        spec = self.spec_for("字幕")
        text = ('arg == "--host" { x }\narg == "--port" { y }\narg == "-m" { z }\n'
                'svr_get("/health");\n  arg == "--api-key" { auth = v; }\n')
        self.feed({p: text for _n, p in spec["verify"]})
        bad = lp.verify_spec("字幕", spec, "cache", False)
        self.assertTrue(any("--api-key" in x for x in bad), bad)

    def test_unreachable_source_is_reported_not_skipped(self) -> None:
        spec = self.spec_for("生图")
        self.feed({})
        real = cc.fetch
        try:
            cc.fetch = lambda *a, **k: None
            bad = lp.verify_spec("生图", spec, "cache", False)
        finally:
            cc.fetch = real
        self.assertTrue(any("取不到上游" in x for x in bad), bad)
        self.assertEqual(len(bad), len({p for _n, p in spec["verify"]}), bad)

    def test_needle_direction_is_needle_in_text(self) -> None:
        """曾经写反过一次（needle in key）：文本里含旗标才算找到，反过来不算。"""
        self.assertFalse(lp.needle_present("--port", 'xx = {"--api-key"};'))
        self.assertTrue(lp.needle_present('xx = {"--port"};', "--port"))

    def test_flattening_only_helps_for_split_lines(self) -> None:
        self.assertFalse(lp.needle_present("", "--port"))
        self.assertFalse(lp.needle_present("no flags here", "--api-key"))
        self.assertTrue(lp.needle_present('x =\n    {"-m",\n     "--model"},',
                                          '{"-m", "--model"}'))

    def test_stats_count_what_was_actually_read(self) -> None:
        """「核对过」与「没报错」不是一回事：输出里的条数必须来自真读到的那些文件。"""
        spec = self.spec_for("字幕")
        text = ('arg == "--host" { x }\narg == "--port" { y }\narg == "-m" { z }\n'
                'svr_get("/health");\n')
        self.feed({p: text for _n, p in spec["verify"]})
        stats: dict = {}
        self.assertEqual(lp.verify_spec("字幕", spec, "cache", False, stats), [])
        self.assertEqual(stats["needles"], len(spec["verify"]))
        self.assertEqual(stats["absent_claims"], len(spec["absent"]))
        self.assertEqual(stats["cached"], len({p for _n, p in spec["verify"]}))
        self.assertEqual(stats.get("fetched", 0), 0)

    def test_stats_separate_cache_from_live_fetch(self) -> None:
        spec = self.spec_for("字幕")
        text = ('arg == "--host" { x }\narg == "--port" { y }\narg == "-m" { z }\n'
                'svr_get("/health");\n')
        real_read, real_fetch = cc.read_cache, cc.fetch
        try:
            cc.read_cache = lambda c, cache: None
            cc.fetch = lambda c, *a, **k: text
            stats: dict = {}
            lp.verify_spec("字幕", spec, "cache", False, stats)
        finally:
            cc.read_cache, cc.fetch = real_read, real_fetch
        self.assertEqual(stats["cached"], 0)
        self.assertEqual(stats["fetched"], len({p for _n, p in spec["verify"]}))

    def test_needle_and_claim_totals_are_the_reported_numbers(self) -> None:
        """车道输出那句「核对 N 条 needle」的 N 是算出来的，也是写死的：
        顺手删掉一条 needle 时，派生值跟着变小而字面量不变 —— 这条才拦得住。"""
        needles = sum(len(s["verify"]) for s in lp.SPEC.values())
        claims = sum(len(s.get("absent") or []) for s in lp.SPEC.values())
        files = len({(s["repo"], p) for s in lp.SPEC.values()
                     for p in {n[1] for n in s["verify"]} | set(s.get("absent_paths") or [])})
        self.assertEqual(needles, 28)
        self.assertEqual(claims, 8)
        self.assertEqual(files, 8)


class TestCiteIdsExist(unittest.TestCase):
    """SPEC 里引用的每个出处 id 都必须在出处表里 —— 引用一个不存在的 id 与没查过一样。"""

    def test_every_cite_resolves(self) -> None:
        ids = cite_ids()
        for cap, spec in lp.SPEC.items():
            for cid in lp.cites_of(spec):
                self.assertIn(cid, ids, f"{cap} 引用了表里没有的出处 {cid}")

    def test_build_reports_a_bogus_cite(self) -> None:
        saved = lp.SPEC["对话"]["request"]["cite"]
        try:
            lp.SPEC["对话"]["request"]["cite"] = "根本没有这条出处"
            _doc, problems = lp.build(OS3, TIERS, "", False, check_provenance=False)
        finally:
            lp.SPEC["对话"]["request"]["cite"] = saved
        self.assertTrue(any("根本没有这条出处" in p and "没有这条" in p for p in problems),
                        problems)

    def test_cites_of_accepts_both_shapes(self) -> None:
        self.assertEqual(lp.cites_of({"readiness": {"cite": "a"},
                                      "request": {"cite": ["b", "a"]}}), ["a", "b"])
        self.assertEqual(lp.cites_of({"readiness": None, "request": {}}), [])


class TestImageArgsFollowTheBudget(unittest.TestCase):
    """生图那行的编码器不是手打的：它跟着 machine-budget 真算进预算的那几颗。"""

    def test_flux_tier_gets_all_three_encoders(self) -> None:
        argv, problems = lp.model_argv("生图", "16GB", {}, FLUX16)
        self.assertEqual(problems, [])
        self.assertEqual(argv[0], "--diffusion-model")
        for flag, f in (("--clip_l", "clip_l-Q8_0.gguf"), ("--t5xxl", "t5xxl-Q2_K.gguf"),
                        ("--vae", "ae-f16.gguf")):
            self.assertEqual(argv.count(flag), 1, argv)
            self.assertIn("{" + f + "}", argv, argv)

    def test_self_contained_tier_uses_plain_model_flag(self) -> None:
        argv, problems = lp.model_argv("生图", "4GB", {}, budget_stub(**{"4GB": {
            "image_companions": {"needs": []},
            "items": [{"role": "unet", "file": "sd15.gguf"}]}}))
        self.assertEqual(argv, ["-m", "{sd15.gguf}"])
        self.assertEqual(problems, [])

    def test_missing_companion_is_named_not_silently_dropped(self) -> None:
        argv, problems = lp.model_argv("生图", "8GB", {}, budget_stub(**{"8GB": {
            "image_companions": {"needs": ["clip_l", "t5xxl"]},
            "items": [{"role": "unet", "file": "x.gguf"},
                      {"companion_of": "x.gguf", "need_key": "clip_l", "file": "c.gguf"}]}}))
        self.assertTrue(any("t5xxl" in p and "预算里没有这颗配套件" in p for p in problems),
                        problems)
        self.assertNotIn("--t5xxl", argv)

    def test_unknown_role_is_reported(self) -> None:
        _argv, problems = lp.model_argv("生图", "8GB", {}, budget_stub(**{"8GB": {
            "image_companions": {"needs": ["clip_q"]},
            "items": [{"role": "unet", "file": "x.gguf"},
                      {"companion_of": "x.gguf", "need_key": "clip_q", "file": "q.gguf"}]}}))
        self.assertTrue(any("没有对应旗标" in p for p in problems), problems)

    def test_tier_without_unet_is_reported(self) -> None:
        _argv, problems = lp.model_argv("生图", "8GB", {}, budget_stub(**{"8GB": {
            "image_companions": {"needs": ["VAE"]}, "items": []}}))
        self.assertTrue(any("没有生图默认行" in p for p in problems), problems)

    def test_committed_image_row_names_the_default_file(self) -> None:
        """模板里那颗主体权重必须就是档位表的默认行 —— 不是某个谁都能填的占位符。"""
        it = load("catalog/image-tiers.json")
        doc = artifact()
        for tier, rows in it["tiers_rows"].items():
            argv = doc["capabilities"]["生图"]["by_tier"][f"{tier}/win-x64"]
            self.assertIn("{" + rows[0]["file"] + "}", argv, (tier, argv))

    def test_tier_table_and_budget_disagreement_is_a_problem(self) -> None:
        """两张表只要不再是同一颗权重，build 就必须报，不能挑一张用。"""
        argv, problems = lp.model_argv("生图", "16GB",
                                       {"tiers_rows": {"16GB": [
                                           {"file": "别的.gguf",
                                            "completeness": {"needs": ["clip_l", "t5xxl",
                                                                      "VAE"]}}]}},
                                       FLUX16)
        self.assertTrue(any("两张表已经不是同一颗权重" in p for p in problems), problems)
        self.assertEqual(argv[0], "--diffusion-model")

    def test_flags_come_from_the_table_not_a_second_literal(self) -> None:
        """改表就要改命令行：代码里若还手写着一份旗标，这条会绿而现实是红的。"""
        saved = dict(lp.SPEC["生图"]["model_flags"])
        try:
            lp.SPEC["生图"]["model_flags"]["t5xxl"] = "--t5xxl-renamed"
            argv, problems = lp.model_argv("生图", "16GB", {}, FLUX16)
        finally:
            lp.SPEC["生图"]["model_flags"] = saved
        self.assertEqual(problems, [])
        self.assertIn("--t5xxl-renamed", argv, argv)

    def test_source_has_no_hardcoded_companion_flag_list(self) -> None:
        """同一件事不许有两份写法：model_argv() 里只许从 model_flags 取。"""
        with open(CLI, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "model_argv")
        lits = {n.value for n in ast.walk(fn)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertFalse({f for f in lits if f.startswith("--")}, lits)

    def test_committed_image_rows_match_the_budget_needs(self) -> None:
        """产物里那一行的编码器个数，必须与预算表说"这一族需要"的条数一致。"""
        b = load("catalog/machine-budget.json")
        doc = artifact()
        for tier, blk in b["per_tier"].items():
            needs = (blk.get("image_companions") or {}).get("needs") or []
            argv = doc["capabilities"]["生图"]["by_tier"][f"{tier}/win-x64"]
            self.assertEqual(sum(1 for a in argv if a.startswith("--clip") or
                                 a in ("--t5xxl", "--vae")), len(needs), (tier, argv, needs))


class TestSecurityShape(unittest.TestCase):
    """验收第 10 条的地基：谁能鉴权、谁只能靠绑地址，都得写成数据。"""

    def test_every_server_capability_binds_loopback_only(self) -> None:
        doc = artifact()
        for cap, spec in doc["capabilities"].items():
            if spec["mode"] != "server":
                continue
            i = spec["bind"].index("127.0.0.1")
            self.assertIn(spec["bind"][i - 1], ("--host", "--listen-ip"), cap)
            self.assertIn(spec["bind"][i + 1], ("--port", "--listen-port"), cap)
            for row in spec["by_tier"].values():
                self.assertIn("127.0.0.1", row, row)

    def test_ocr_has_no_bind_because_it_has_no_port(self) -> None:
        doc = artifact()
        self.assertEqual(doc["capabilities"]["文字识别"]["mode"], "subprocess")
        self.assertEqual(doc["capabilities"]["文字识别"]["bind"], [])

    def test_only_llama_reports_engine_auth(self) -> None:
        doc = artifact()
        self.assertTrue(doc["capabilities"]["对话"]["engine_auth"]["engine_supports_api_key"])
        for cap in ("字幕", "生图", "文字识别"):
            self.assertFalse(doc["capabilities"][cap]["engine_auth"]["engine_supports_api_key"],
                             cap)
        self.assertIn("--api-key", doc["capabilities"]["对话"]["by_tier"]["16GB/win-x64"])

    def test_auth_flags_absent_list_is_what_the_lanes_probe(self) -> None:
        doc = artifact()
        self.assertEqual(doc["capabilities"]["字幕"]["flags_absent"], ["--api-key", "api_key"])
        self.assertEqual(doc["capabilities"]["生图"]["flags_absent"], ["--api-key"])
        self.assertIn("--port", doc["capabilities"]["文字识别"]["flags_absent"])

    def test_macos_subtitle_row_is_absent_not_empty(self) -> None:
        """上游 Apple 侧没有可执行文件 → 产物里就不该出现 mac 那一行（#29 的数据侧）。"""
        doc = artifact()
        self.assertIsNone(doc["capabilities"]["字幕"]["binaries"]["macos-arm64"])
        self.assertNotIn("16GB/macos-arm64", doc["capabilities"]["字幕"]["by_tier"])
        self.assertIn("macos_note", doc["capabilities"]["字幕"])

    def test_ocr_argv_omits_the_conflicting_size_flag(self) -> None:
        doc = artifact()
        tpl = doc["capabilities"]["文字识别"]["argv_template"]
        self.assertIn("ocr", tpl)
        self.assertNotIn("--size", tpl)
        self.assertEqual(doc["capabilities"]["文字识别"]["omit"], ["--size"])

    def test_size_flag_in_the_template_is_rejected(self) -> None:
        """omit 不是一句注释：把它塞进命令行时 build() 必须报。"""
        saved = list(lp.SPEC["文字识别"]["request"]["argv"])
        try:
            lp.SPEC["文字识别"]["request"]["argv"] = ["--size", "tiny"] + saved
            _doc, problems = lp.build(OS3, TIERS, "", False, check_provenance=False)
        finally:
            lp.SPEC["文字识别"]["request"]["argv"] = saved
        self.assertTrue(any("带了就直接被 clap 拒" in p for p in problems), problems)


class TestArgvNamesWhatItDownloads(unittest.TestCase):
    """argv 点名的每一颗权重，都要能在首启队列里找到，且 server 名字与出包判据同一张表。

    这一环是"下的和用的是同一批文件"这句话跨产物的最后一格：
    machine-budget 算钱 → file_index 出队列 → 这里起进程。
    """

    def chat_budget(self, *items):
        return {"per_tier": {"8GB": {"items": [dict(i, kind="chat") for i in items]}}}

    def test_chat_argv_names_the_tier_weight(self) -> None:
        argv, problems = lp.model_argv("对话", "8GB", {}, self.chat_budget(
            {"file": "Qwen3-8B-Q4_K_M.gguf", "sha256": "d" * 64}))
        self.assertEqual(argv, ["-m", "{Qwen3-8B-Q4_K_M.gguf}"])
        self.assertEqual(problems, [])

    def test_two_candidates_is_a_problem_not_a_pick(self) -> None:
        """这一档有两颗就说两颗，不自作主张挑一颗 —— 挑了就看不出表已经不一致。"""
        _argv, problems = lp.model_argv("对话", "8GB", {}, self.chat_budget(
            {"file": "a.gguf", "sha256": "d" * 64}, {"file": "b.gguf", "sha256": "e" * 64}))
        self.assertTrue(any("实到 2 颗" in p for p in problems), problems)
        _a2, p2 = lp.model_argv("对话", "8GB", {}, self.chat_budget())
        self.assertTrue(any("实到 0 颗" in p for p in p2), p2)

    def test_unpinned_weight_is_refused(self) -> None:
        _argv, problems = lp.model_argv("对话", "8GB", {}, self.chat_budget({"file": "a.gguf"}))
        self.assertTrue(any("没有钉住的 sha256" in p for p in problems), problems)

    def test_named_files_all_come_from_the_first_run_queue(self) -> None:
        import file_index as fi
        budget = load("catalog/machine-budget.json")
        queue = {i["file"]: i for i in fi.queue_items(budget)}
        named = sorted({n for e in artifact()["capabilities"].values()
                        for n in e.get("named_files") or []})
        self.assertEqual(len(named), 11, named)
        for name in named:
            self.assertIn(name, queue, f"{name} 被 argv 点名，但首启队列里没有它")
            self.assertTrue(queue[name].get("sha256"), name)

    def test_a_named_file_the_report_drops_is_refused(self) -> None:
        """跨生产者核对：报告里少了 argv 点名的那一颗，必须当场说"起的不是下的那一颗"。

        负向对照不能再靠改预算来造 —— 预算是 argv 名字的**同一个来源**，改它两边一起动，
        那条核对就成了自证空转。所以这份夹具是 file_index 自己写出的 JSON。
        """
        rep = queue_report()
        dropped = next(i for i in rep["items"] if i["file"] in
                       artifact()["capabilities"]["对话"]["named_files"])
        rep["items"].remove(dropped)
        _doc, problems = report_build(lp, rep)
        self.assertTrue(any("起的不是下的那一颗" in p and dropped["file"] in p
                            for p in problems), problems)

    def test_full_report_is_a_clean_cross_check(self) -> None:
        """正向对照：报告逐颗确认时不许有任何问题 —— 否则上面那条红是"永远红"。"""
        _doc, problems = report_build(lp, queue_report())
        self.assertEqual(problems, [])

    def test_a_mismatch_is_reported_as_a_mismatch(self) -> None:
        rep = queue_report(lambda r: [it.update(matches_pin=False, checked=True, status="disagree")
                                      for it in r["items"]
                                      if it["file"] == artifact()["capabilities"]["字幕"]
                                      ["named_files"][0]])
        _doc, problems = report_build(lp, rep)
        self.assertTrue(any("两家哈希没对上" in p and "disagree" in p for p in problems), problems)

    def test_a_route_that_did_not_live_check_is_not_called_a_mismatch(self) -> None:
        """"没查"与"查了没对上"是两句话：混在一起就是把没核过的当成核过 —— 反过来也一样。"""
        f = artifact()["capabilities"]["对话"]["named_files"][0]
        rep = queue_report(lambda r: [it.update(matches_pin=False, checked=False)
                                      for it in r["items"] if it["file"] == f])
        _doc, problems = report_build(lp, rep)
        hits = [p for p in problems if f in p]
        self.assertTrue(any("没在这条车道做 live 比对" in p for p in hits), hits)
        self.assertFalse(any("两家哈希没对上" in p for p in hits), hits)

    def test_a_report_that_itself_has_problems_is_refused(self) -> None:
        """生产者的结论也是输入：它自己报了问题，就不许拿它的行来谈起进程。"""
        rep = queue_report(lambda r: r.update(problems=["有一颗两家哈希不一致"]))
        _doc, problems = report_build(lp, rep)
        self.assertTrue(any("报告自己就有 1 条问题" in p for p in problems), problems)

    def test_without_a_report_the_link_says_budget_only(self) -> None:
        """没有报告时不假装核对过：产物里那句必须写着 budget-only。"""
        doc = artifact()
        for cap, spec in doc["capabilities"].items():
            self.assertIn("budget-only", spec["queue_link"], cap)
        fresh, _p = report_build(lp, queue_report())
        for cap, spec in fresh["capabilities"].items():
            self.assertIn("live 确认状态", spec["queue_link"], cap)

    def test_the_report_fixture_is_the_capture_recorded_in_meta(self) -> None:
        """夹具不是随手编的形状：它必须还是那次捕获的字节。"""
        with open(QFIX, "rb") as fh:
            raw = fh.read()
        with open(QFIX.replace(".json", ".meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), meta["sha256"])
        self.assertEqual(len(raw), meta["size"])
        rep = json.loads(raw)
        self.assertEqual(len(rep["items"]), 15)
        self.assertEqual(sum(1 for i in rep["items"] if i.get("matches_pin")), 11)

    def test_ocr_keeps_generic_flags_but_checks_the_trio(self) -> None:
        doc = artifact()
        self.assertEqual(doc["capabilities"]["文字识别"]["queue_ocr_files"],
                         ["pp-ocrv6_tiny_det.onnx", "pp-ocrv6_tiny_rec.onnx",
                          "ppocrv6_tiny_dict.txt"])
        self.assertNotIn("--size", doc["capabilities"]["文字识别"]["argv_template"])

    def test_server_names_are_one_table_two_consumers(self) -> None:
        """出包判据(pack_engine)与启动计划不许各写一份 server 名字。"""
        import pack_engine as pe
        for engine in ("llama", "whisper", "sd"):
            self.assertEqual(pe.SERVER_BINARY[engine], set(lp.binary_names(engine)), engine)

    def test_cli_only_payload_is_rejected_by_the_packing_lane(self) -> None:
        """这就是那张表合起来挡掉的事：负载里只有 CLI，检查说没事，外壳却起不来。"""
        import pack_engine as pe
        only_cli = [dict(file="whisper-cli", sha256="a" * 64, bytes=3)]
        bad = pe.check("whisper", only_cli)
        self.assertTrue(any("只有 CLI 不算" in x for x in bad), bad)
        with_server = only_cli + [dict(file="whisper-server", sha256="c" * 64, bytes=4)]
        self.assertEqual(pe.check("whisper", with_server), [])

    def test_binary_names_follow_the_os_rule(self) -> None:
        self.assertEqual(lp.binary_for("llama", "win-x64"), "llama-server.exe")
        self.assertEqual(lp.binary_for("llama", "linux-x64"), "llama-server")
        self.assertIsNone(lp.binary_for("whisper", "macos-arm64"))
        self.assertEqual(lp.SPEC["对话"]["binary"],
                         {o: lp.binary_for("llama", o) for o in lp.OS_LABELS})

    def test_the_artifact_names_come_from_the_one_table(self) -> None:
        """产物里每颗二进制名字都必须等于那张表算出来的值，命令行里则只放 {bin}。

        这张表在导入时就评估完了，所以"运行时改表看 argv 变不变"是错的探针 ——
        改表要能红，得走源码变异（见 TestSourceMutants 里 sd-cli 那条）。
        """
        doc = artifact()
        for cap, spec in lp.SPEC.items():
            for os_label in lp.OS_LABELS:
                self.assertEqual(doc["capabilities"][cap]["binaries"][os_label],
                                 lp.binary_for(spec["engine"], os_label), f"{cap}/{os_label}")
        for cap in ("对话", "字幕", "生图"):
            rows = doc["capabilities"][cap]["by_tier"]
            self.assertTrue(rows, cap)
            for key, row in rows.items():
                self.assertEqual(row[0], "{bin}", key)
                for os_label, name in doc["capabilities"][cap]["binaries"].items():
                    if name:
                        self.assertNotIn(name, row, f"{key} 里手写了 {name}")


class TestCli(unittest.TestCase):
    def test_selftest_exits_zero(self) -> None:
        r = subprocess.run([sys.executable, CLI, "--selftest"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("BAD 数量 = 0", r.stdout)

    def test_offline_recompute_drift_check_is_silent(self) -> None:
        """--skip-provenance 那一层只比"重算 vs 已提交"，全程不联网。"""
        r = subprocess.run([sys.executable, CLI, "--check", "--skip-provenance"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("问题 0 条", r.stdout)
        self.assertIn("--skip-provenance", r.stdout)     # 模式必须自己说出来
        self.assertIn("核对 0 条 needle", r.stdout)       # 没查就报 0，不许装作查过

    def test_a_missing_queue_report_is_a_refusal_not_a_green(self) -> None:
        """车道里 download-artifact 缺件只警告不红 —— 所以"核对没有输入"必须由工具自己说。"""
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "plan.json")
            r = subprocess.run([sys.executable, CLI, "--skip-provenance", "--queue-report",
                                os.path.join(d, "nope.json"), "--out", out],
                               capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout[-400:] + r.stderr[-400:])
        self.assertIn("不存在", r.stdout)
        self.assertNotIn("读不动", r.stdout, "缺件与读不动是两件事，混报就分不清哪步坏了")
        self.assertNotIn("Traceback", r.stderr, "抛异常也算红，但说不清是这件事")
        self.assertFalse(os.path.exists(out), "拒绝了还落盘 = 那张表是凭空来的")

    def test_an_unreadable_queue_report_is_refused_too(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bad = os.path.join(d, "r.json")
            with open(bad, "w", encoding="utf-8") as fh:
                fh.write("{ 不是 JSON")
            r = subprocess.run([sys.executable, CLI, "--skip-provenance", "--queue-report", bad,
                                "--out", os.path.join(d, "plan.json")],
                               capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout[-400:] + r.stderr[-400:])
        self.assertIn("读不动", r.stdout)
        self.assertNotIn("不存在", r.stdout, "文件明明在，报成不在会把人支到错误的那一步")

    def test_a_real_report_keeps_the_cli_green(self) -> None:
        """正向对照：真实捕获那份报告喂进去必须 0 问题并落盘（否则上面两条是"永远红"）。"""
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "plan.json")
            r = subprocess.run([sys.executable, CLI, "--skip-provenance", "--queue-report", QFIX,
                                "--out", out], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout[-600:] + r.stderr[-300:])
        self.assertIn("落盘", r.stdout)
        self.assertIn("NOTE: --skip-provenance", r.stdout)   # 没查上游这句必须自报
        self.assertIn("核对 0 条 needle", r.stdout)

    def test_write_refuses_to_land_a_bad_plan(self) -> None:
        """上游原文一份都取不到时，落盘这件事必须被拒 —— 不能"没查过也算出一张表"。"""
        real_read, real_fetch = cc.read_cache, cc.fetch
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "plan.json")
            buf = io.StringIO()
            try:
                cc.read_cache = lambda c, cache: None
                cc.fetch = lambda *a, **k: None
                with contextlib.redirect_stdout(buf):
                    rc = lp.main(["--cache", d, "--out", out])
            finally:
                cc.read_cache, cc.fetch = real_read, real_fetch
            self.assertEqual(rc, 1, buf.getvalue())
            self.assertIn("不落盘", buf.getvalue())
            self.assertFalse(os.path.exists(out))

    def test_stdout_reports_one_example_line_per_capability(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = lp.main(["--skip-provenance", "--out", os.devnull])
        self.assertEqual(rc, 0, buf.getvalue())
        for cap in lp.SPEC:
            self.assertIn(cap, buf.getvalue())


class TestArtifactShape(unittest.TestCase):
    """形状与覆盖度立成字面量：少一项能力、少一档、少一条 needle，都该当场红。"""

    def test_four_capabilities_three_tiers_three_os(self) -> None:
        doc = artifact()
        self.assertEqual(sorted(doc["capabilities"]), ["字幕", "对话", "文字识别", "生图"])
        self.assertEqual(len(doc["capabilities"]["对话"]["by_tier"]), 9)
        self.assertEqual(len(doc["capabilities"]["生图"]["by_tier"]), 9)
        self.assertEqual(len(doc["capabilities"]["字幕"]["by_tier"]), 6)

    def test_every_capability_has_at_least_four_verified_flags(self) -> None:
        doc = artifact()
        for cap, spec in doc["capabilities"].items():
            self.assertGreaterEqual(len(spec["flags_verified"]), 4, cap)

    def test_rule_texts_keep_the_four_answers(self) -> None:
        doc = artifact()
        self.assertEqual(sorted(doc["rule"]),
                         ["auth", "image_model_args", "listener", "provenance"])
        self.assertIn("127.0.0.1", doc["rule"]["listener"])

    def test_citation_floor_still_clears_after_adding_launch_cites(self) -> None:
        self.assertGreaterEqual(len(cite_ids()), cc.MIN_CITATIONS)


class TestSourceMutants(unittest.TestCase):
    """把工具源码逐条改坏，证明上面那些判据不是装饰。

    每个变异体必须让六条探针里**至少一条**不再"判据在位"：
      a 离线对照（selftest）仍然全绿；
      b 重算并与已提交那张比漂移，仍然没有问题；
      c 出处 id 闸门仍然抓得住一个引用了不存在的 id 的规格；
      d 声明"上游没有"的那面旗标真混进原文时仍然点名它；
      e 跨生产者核对（首启队列报告）的四种说法仍然各说各的；
      f 报告文件不在时 CLI 仍然拒绝，而不是抛异常或"没输入也算核过"。
    探针只读，不落盘 —— 变异扫描里"顺手覆盖产品表"是这一轮已经犯过两次的错。
    """

    @staticmethod
    def src() -> str:
        with open(CLI, encoding="utf-8") as fh:
            return fh.read()

    MUTANTS = [
        ("needle_present 变成恒真", 'return needle in text or needle in flat', 'return True'),
        ("压平那一半被删（跨行旗标就认不出）", 'return needle in text or needle in flat',
         'return needle in text'),
        ("生图旗标表被绕过（第二份手写就看不见了）", 'if need not in flags:', 'if need in flags:'),
        ("两张表的 needs 不一致不再报", 'if tneeds != sorted(needs):', 'if False and tneeds != sorted(needs):'),
        ("档位表默认行与预算主体不一致不再报", 'if trow.get("file") != unet["file"]:',
         'if False and trow.get("file") != unet["file"]:'),
        ("主体旗标永远用 -m（flux 那档会少三份）", 'argv = [flags["unet"] if needs else spec["model"][0]',
         'argv = [spec["model"][0]'),
        ("预算里缺配套件时不报而继续拼", 'if comp is None:', 'if comp is not None:'),
        ("出处 id 闸门关掉", 'if cid not in cited:', 'if False and cid not in cited:'),
        ("缺席声明不再扫（whisper 加了鉴权也不知道）", 'for needle in spec.get("absent") or []:',
         'for needle in []:'),
        ("llama 的 --api-key 从规格里掉出去", '"argv": ["--api-key", "{token}"],', '"argv": [],'),
        ("报告被忽略，仍旧只按预算自证",
         'pool = ({r["file"]: r for r in report.get("items", [])} if report is not None else queue)',
         'pool = queue'),
        ("报告自己报问题时被当成没问题", 'if report is not None and report.get("problems"):',
         'if False and report.get("problems"):'),
        ("生图的二进制名字不经过那张表", 'binary_for("sd", o)', '"sd-cli"'),
        ("报告不存在也照样往下核", "if not os.path.exists(a.queue_report):",
         "if False and a.queue_report:"),
    ]

    def load(self, src: str):
        import types
        mod = types.ModuleType("launch_plan_mutant")
        mod.__dict__["__file__"] = CLI
        mod.__dict__["__name__"] = "launch_plan_mutant"
        exec(compile(src, CLI, "exec"), mod.__dict__)
        return mod

    def probes(self, mod) -> dict:
        out = {}
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out["a 离线对照"] = mod.selftest() == 0
        except Exception as exc:                       # noqa: BLE001  炸了也算"没在位"
            out["a 离线对照"] = False
            out["_a 异常"] = str(exc)[:60]
        try:
            doc, problems = mod.build(OS3, TIERS, "", False, check_provenance=False)
            out["b 重算与漂移"] = (not problems) and (mod.check(artifact(), doc) == [])
        except Exception as exc:                       # noqa: BLE001
            out["b 重算与漂移"] = False
            out["_b 异常"] = str(exc)[:60]
        cc_mod = mod.citation_check
        saved_read, saved_fetch = cc_mod.read_cache, cc_mod.fetch
        try:
            spec = mod.SPEC["字幕"]
            # 故意把"以为没有"的那面旗标塞进原文：扫缺席的那一段必须点名它。
            cc_mod.read_cache = lambda c, cache: (
                'arg == "--host" {x}' + chr(10) + 'arg == "--port" {y}' + chr(10)
                + 'arg == "-m" {z}' + chr(10) + 'svr_get("/health");' + chr(10)
                + '  arg == "--api-key" {auth = v;}')
            cc_mod.fetch = lambda *a, **k: ""
            bad = mod.verify_spec("字幕", spec, "cache", False)
        except Exception as exc:                       # noqa: BLE001
            bad = [str(exc)]
        finally:
            cc_mod.read_cache, cc_mod.fetch = saved_read, saved_fetch
        out["d 缺席声明照扫"] = any("--api-key" in x for x in bad)
        saved_cite = mod.SPEC["对话"]["request"]["cite"]
        try:
            mod.SPEC["对话"]["request"]["cite"] = "这条出处根本不存在"
            _doc, problems = mod.build(OS3, TIERS, "", False, check_provenance=False)
            out["c 出处 id 闸门"] = any("没有这条" in p for p in problems)
        except Exception as exc:                       # noqa: BLE001
            out["c 出处 id 闸门"] = False
            out["_c 异常"] = str(exc)[:60]
        finally:                                       # 不还回去，e 拿到的就是被 c 改脏的规格
            mod.SPEC["对话"]["request"]["cite"] = saved_cite
        try:
            base, p0 = report_build(mod, queue_report())
            named = {n for e in base["capabilities"].values() for n in e.get("named_files") or []}

            def one(**kv):
                rep = queue_report()
                for it in rep["items"]:
                    if it["file"] in named:
                        it.update(**kv)
                        break
                return rep

            rep_drop = queue_report()
            rep_drop["items"] = [i for i in rep_drop["items"] if i["file"] not in named][:5]
            _b1, p1 = report_build(mod, rep_drop)
            _b2, p2 = report_build(mod, one(matches_pin=False, checked=True, status="disagree"))
            _b3, p3 = report_build(mod, one(matches_pin=False, checked=False))
            _b4, p4 = report_build(mod, queue_report(lambda r: r.update(problems=["有一颗没对上"])))
            out["e 跨生产者核对"] = (
                len(named) == 11 and p0 == []
                and any("起的不是下的那一颗" in x for x in p1)
                and any("两家哈希没对上" in x for x in p2)
                and any("没在这条车道做 live 比对" in x for x in p3)
                and any("报告自己就有" in x for x in p4))
        except Exception as exc:                       # noqa: BLE001
            out["e 跨生产者核对"] = False
            out["_e 异常"] = str(exc)[:60]
        try:
            import tempfile as _tf
            with _tf.TemporaryDirectory() as d:
                out_path = os.path.join(d, "plan.json")
                buf_e = io.StringIO()
                with contextlib.redirect_stdout(buf_e):
                    rc_missing = mod.main(["--skip-provenance", "--queue-report",
                                           os.path.join(d, "nope.json"), "--out", out_path])
                    nothing_written = not os.path.exists(out_path)
                buf_f = io.StringIO()
                with contextlib.redirect_stdout(buf_f):
                    rc_real = mod.main(["--skip-provenance", "--queue-report", QFIX,
                                        "--out", out_path])
            # 报的是**哪一件**也要核对：只要求 rc==1 的话，"读不动"那句会替"文件不在"这句顶包
            # （两条消息原先共用"核对没有输入"这个说法，那条变异就是这么溜过去的）。
            out["f 核对没有输入就红"] = (rc_missing == 1 and nothing_written and rc_real == 0
                                       and "不存在" in buf_e.getvalue()
                                       and "读不动" not in buf_e.getvalue())
        except Exception as exc:                       # noqa: BLE001
            out["f 核对没有输入就红"] = False
            out["_f 异常"] = str(exc)[:60]
        return out

    def test_baseline_probes_all_hold(self) -> None:
        """正向对照：不改坏的源码，六条探针必须全部说"判据在位"。"""
        self.assertEqual({k: v for k, v in self.probes(lp).items() if not k.startswith("_")},
                         {"a 离线对照": True, "b 重算与漂移": True, "c 出处 id 闸门": True,
                          "d 缺席声明照扫": True, "e 跨生产者核对": True,
                          "f 核对没有输入就红": True})

    def test_every_mutant_is_caught(self) -> None:
        missed = []
        for name, old, new in self.MUTANTS:
            self.assertEqual(self.src().count(old), 1, f"锚点不唯一或已漂：{name}")
            mod = self.load(self.src().replace(old, new, 1))
            got = {k: v for k, v in self.probes(mod).items() if not k.startswith("_")}
            if all(got.values()):
                missed.append(f"{name} → {got}")
        self.assertEqual(missed, [], "这些变异没人报：对应判据是装饰\n" + "\n".join(missed))

    def test_mutant_probe_counts(self) -> None:
        """变异体数量也是账：删掉一条锚点而不说原因，就该在这里红。"""
        self.assertEqual(len(self.MUTANTS), 14)


if __name__ == "__main__":
    unittest.main()
