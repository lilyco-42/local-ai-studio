"""`tools/recommend_models.py` 的离线测试：推荐表的四条规则每条都要能被"明知是错的输入"证明。

数据源是 `tests/fixtures/recommend_slice.json` —— 由
`python3 tools/recommend_models.py --live --fixtures-out ...` 在同一次联网运行里录下来的
真元数据（不是我照记忆写的形状）。里面 `ggml-org/Qwen3-8B-GGUF` 只有 BF16 与 Q8_0
（16,388,044,096 / 8,709,518,656 字节）和 dflash-/dspark- 草稿模型，`unsloth/Qwen3-8B-GGUF`
有完整量化族 —— 这两份真实清单正好构成一组正反对照：8 GB 档必须拒绝前者、必须选中
后者的 Q4_K_M，而且**绝不能**因为 1.1 GB 的 `dflash-Qwen3-8B-Q8_0.gguf` 更小就选它。
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
import recommend_models as rm  # noqa: E402

SLICE = os.path.join(HERE, "fixtures", "recommend_slice.json")
TABLE = os.path.join(HERE, "..", "catalog", "recommendations.json")
SHA_A = "a" * 64
SHA_B = "b" * 64


def blob():
    with open(SLICE, encoding="utf-8") as fh:
        return json.load(fh)


def model(name, dl, rec, sources, quant="Q4_K_M", lic="apache-2.0", min_ram=1.0):
    return {"name": name, "pipeline_tag": "text-generation", "has_sources": True,
            "recommended_ram_gb": rec, "min_ram_gb": min_ram, "hf_downloads": dl,
            "quantization": quant, "license": lic, "use_case": "Instruction following, chat",
            "parameter_count": "8B", "context_length": 4096, "gguf_sources": sources}


def files_of(repo, hf, ms=None, hf_by="huggingface.co", ms_by="www.modelscope.cn",
             hf_err=None, ms_err=None):
    row = {"hf": hf, "hf_by": hf_by}
    if ms is not None:
        row["ms"], row["ms_by"] = ms, ms_by
    if hf_err:
        row["hf_err"] = hf_err
    if ms_err:
        row["ms_err"] = ms_err
    return {repo: row}


def fetch_from(table):
    return rm.slice_fetch(table)


def entry(sha, size):
    return {"sha256": sha, "size": size}


class TestChatRows(unittest.TestCase):
    def test_only_resolvable_text_generation_rows_count(self) -> None:
        cands = rm.chat_rows([
            model("a/keep", 10, 4.0, [{"repo": "x/y", "provider": "unsloth"}]),
            dict(model("a/embed", 10, 4.0, []), pipeline_tag="feature-extraction"),
            dict(model("a/nosrc", 10, 4.0, []), has_sources=False),
            dict(model("a/noram", 10, None, []), recommended_ram_gb=None),
        ])
        self.assertEqual([m["name"] for m in cands], ["a/keep"])


class TestWindow(unittest.TestCase):
    def test_upper_bound_inclusive_lower_bound_exclusive(self) -> None:
        cands = [model("m/at-cap", 1, 8.0, []), model("m/half-cap", 2, 4.0, []),
                 model("m/over", 3, 8.1, []), model("m/in", 4, 7.9, [])]
        got = [m["name"] for m in rm.window(cands, 8.0)]
        self.assertEqual(got, ["m/in", "m/at-cap"], "半档是开区间：等于 4.0 的那条不属于 8GB 档")

    def test_the_measured_0p6b_case_does_not_fill_the_big_tiers(self) -> None:
        # 这是把规则从 `<= 档` 改成窗口的原因，钉成测试而不是只写在 docstring 里：
        # Qwen3-0.6B 有 30,575,964 次下载但 recommended_ram_gb=2.0，窗口规则下三档都不该出现它
        cands = [model("Qwen/Qwen3-0.6B", 30_575_964, 2.0, []),
                 model("Qwen/Qwen3-8B", 9_650_573, 7.6, []),
                 model("Qwen/Qwen3-14B", 5_776_369, 13.8, [])]
        self.assertEqual([m["name"] for m in rm.window(cands, 16.0)], ["Qwen/Qwen3-14B"])
        self.assertEqual([m["name"] for m in rm.window(cands, 8.0)], ["Qwen/Qwen3-8B"])
        self.assertEqual(rm.window(cands, 4.0), [], "2.0 是 4GB 档的下限本身，开区间要把它挡掉")

    def test_dedupe_is_case_insensitive_and_keeps_the_larger_download_count(self) -> None:
        cands = [model("microsoft/Phi-3-mini-4k-instruct", 370_784, 3.6, []),
                 model("microsoft/phi-3-mini-4k-instruct", 500_000, 3.6, [])]
        got = [m["name"] for m in rm.window(cands, 4.0)]
        self.assertEqual(got, ["microsoft/phi-3-mini-4k-instruct"], "同名只留一条，且留下下载量高的")

    def test_order_is_upstream_downloads_then_name(self) -> None:
        cands = [model("z/one", 5, 7.0, []), model("a/two", 5, 7.0, []),
                 model("m/three", 9, 7.0, [])]
        self.assertEqual([m["name"] for m in rm.window(cands, 8.0)],
                         ["m/three", "a/two", "z/one"])


class TestRankedRepos(unittest.TestCase):
    def test_preferred_providers_first_and_unknown_last(self) -> None:
        tries = rm.ranked_repos([{"repo": "foo/A", "provider": "foo"},
                                 {"repo": "mradermacher/B", "provider": "mradermacher"},
                                 {"repo": "ggml-org/C", "provider": "ggml-org"},
                                 {"repo": "ggml-org/C", "provider": "ggml-org"}])
        self.assertEqual([r for r, _ in tries], ["ggml-org/C", "mradermacher/B", "foo/A"])
        self.assertIn("不在偏好里", tries[-1][1])
        self.assertEqual(sum(1 for r, _ in tries if r == "ggml-org/C"), 1, "同仓库不许出现两次")

    def test_empty_sources_gives_empty_list(self) -> None:
        self.assertEqual(rm.ranked_repos([{"repo": "", "provider": "unsloth"}, {}]), [])


class TestPickFile(unittest.TestCase):
    """用切片里两份真清单当对照：一份只有超档与草稿，一份有完整量化族。"""

    def setUp(self) -> None:
        self.b = blob()

    def pick(self, repo, cap):
        meta = self.b["files"][repo]
        names = sorted(meta["hf"])
        size_of = lambda n: rm.file_size({"hf": meta["hf"], "ms": meta.get("ms", {})}, n)
        return rm.pick_file(names, size_of, cap, "Q4_K_M", rm.repo_stem(repo, "Qwen/Qwen3-8B"))

    def test_real_preferred_repo_picks_the_preferred_quant(self) -> None:
        got, why = self.pick("unsloth/Qwen3-8B-GGUF", 8.0)
        self.assertEqual(got, "Qwen3-8B-Q4_K_M.gguf")
        self.assertIn("量化偏好命中 Q4_K_M", why)

    def test_real_draft_files_cannot_win_even_though_they_are_smaller(self) -> None:
        # dflash-Qwen3-8B-Q8_0.gguf 是 1,120,250,336 字节，按大小排序它最先合档；
        # 形状判据必须在大小判据之前把它挡掉，否则这条测试会绿在错的文件上
        got, _ = self.pick("ggml-org/Qwen3-8B-GGUF", 8.0)
        self.assertIsNone(got, "ggml-org 这份真清单里没有任何整只量化塞得进 8 GB")

    def test_same_repo_resolves_at_16gb_to_the_standard_file_not_a_draft(self) -> None:
        got, _ = self.pick("ggml-org/Qwen3-8B-GGUF", 16.0)
        self.assertEqual(got, "Qwen3-8B-Q8_0.gguf")

    def test_over_capacity_rejection_says_why(self) -> None:
        _, why = self.pick("ggml-org/Qwen3-8B-GGUF", 8.0)
        self.assertIn("超档", why)
        self.assertIn("8 GB", why)

    def test_shards_are_never_returned_as_a_whole_model(self) -> None:
        hf = {"Q-X-Q4_K_M-00001-of-00002.gguf": entry(SHA_A, 2_000_000_000),
              "Q-X-Q4_K_M-00002-of-00002.gguf": entry(SHA_B, 2_000_000_000)}
        got, why = rm.pick_file(sorted(hf), lambda n: hf[n]["size"], 8.0, "Q4_K_M", ("Q-X",))
        self.assertIsNone(got, "半个模型不是可加载文件")
        self.assertIn("整只", why)
        # 单独测分片判据：把形状判据关掉（stems=()），挡住的必须还是分片那一条，
        # 否则"两道闸其实只有一道在起作用"这件事没人知道（变异扫描就是这样漏掉的）
        got2, why2 = rm.pick_file(sorted(hf), lambda n: hf[n]["size"], 8.0, "Q4_K_M", ())
        self.assertIsNone(got2, "关掉形状判据后仍不许返回分片")
        self.assertIn("整只", why2)

    def test_licensed_regex_covers_openrail_family(self) -> None:
        # 这条正则被三张表共用（生图那边先发现 openrail 系漏标），所以它得单独钉住，
        # 不能只靠"某一行恰好带这个许可"的语料 —— 对话这张表的语料里就没有
        for lic, risky in (("creativeml-openrail-m", True), ("openrail++", True),
                           ("cc-by-nc-4.0", True), ("unknown", True),
                           ("mit", False), ("apache-2.0", False), ("qwen-research", False)):
            self.assertEqual(bool(rm.RISKY_LICENSE.search(lic)), risky, lic)

    def test_model_level_quant_hint_used_when_preferred_list_misses(self) -> None:
        hf = {"Q-X-Q5_K_L.gguf": entry(SHA_A, 3_000_000_000)}
        got, why = rm.pick_file(sorted(hf), lambda n: hf[n]["size"], 8.0, "Q5_K_L", ("Q-X",))
        self.assertEqual(got, "Q-X-Q5_K_L.gguf")
        self.assertIn("quantization=Q5_K_L", why)

    def test_unknown_size_is_rejected_not_assumed_fine(self) -> None:
        hf = {"Q-X-Q4_K_M.gguf": {"sha256": SHA_A, "size": None}}
        got, why = rm.pick_file(["Q-X-Q4_K_M.gguf"], lambda n: rm.file_size({"hf": hf, "ms": {}}, n),
                                8.0, "Q4_K_M", ("Q-X",))
        self.assertIsNone(got)
        self.assertIn("都没给大小", why)

    def test_repo_stem_and_model_stem_both_accepted(self) -> None:
        self.assertEqual(rm.repo_stem("unsloth/Qwen3-8B-GGUF", "Qwen/Qwen3-8B"),
                         ("Qwen3-8B",))
        self.assertEqual(rm.repo_stem("mradermacher/Foo-GGUF", "bar/Foo-v2"),
                         ("Foo", "Foo-v2"))

    def test_dot_separator_names_match_too(self) -> None:
        # mradermacher 的真清单用点号分隔（Qwen2.5-3B-Instruct.Q4_K_M.gguf），必须能命中
        hf = {"Qwen2.5-3B-Instruct.Q4_K_M.gguf": entry(SHA_A, 2_000_000_000)}
        got, _ = rm.pick_file(sorted(hf), lambda n: hf[n]["size"], 4.0, "Q4_K_M",
                              ("Qwen2.5-3B-Instruct",))
        self.assertEqual(got, "Qwen2.5-3B-Instruct.Q4_K_M.gguf")


def tier_models():
    """三档各五条塞得进的候选 —— 条数必须等于 PER_TIER，否则测到的是地板而不是我要测的规则。

    这条踩过一次：合成的单档输入让另外两档各冒出「想要 5 条只凑到 N 条」的问题，
    `problems == []` 永远不可能成立，看起来像判据失效，其实是喂的数据不够。
    """
    specs = []
    for label, prefix, recs in (("4", "T4", (4.0, 3.9, 3.5, 3.0, 2.5)),
                                ("8", "T8", (7.6, 7.0, 6.0, 5.0, 4.5)),
                                ("16", "T16", (15.0, 13.0, 11.0, 10.0, 9.0))):
        for j, rec in enumerate(recs):
            specs.append(("m/%s%s" % (label, "abcde"[j]), rec, prefix + "abcde"[j]))
    return [model(n, 1000 - i, rec, [{"repo": "unsloth/" + repo, "provider": "unsloth"}])
            for i, (n, rec, repo) in enumerate(specs)]


def good_table(models, size=4_000_000_000, sha=SHA_A):
    """给每条候选的偏好仓库造一份两家一致的元数据（默认全绿，测试只改自己关心的那家）。"""
    t = {}
    for m in models:
        repo = m["gguf_sources"][0]["repo"]
        f = repo.rsplit("/", 1)[-1] + "-Q4_K_M.gguf"
        t.update(files_of(repo, {f: entry(sha, size)}, {f: entry(sha, size)}))
    return t


def repo_of(models, name):
    return next(m["gguf_sources"][0]["repo"] for m in models if m["name"] == name)


def file_of(models, name):
    return repo_of(models, name).rsplit("/", 1)[-1] + "-Q4_K_M.gguf"


class TestBuildRules(unittest.TestCase):
    def test_happy_path_is_silent_and_fills_every_tier(self) -> None:
        models = tier_models()
        got, problems = rm.build(models, fetch_from(good_table(models)), min_candidates=None)
        self.assertEqual(problems, [])
        self.assertEqual(got["rows"], 15)
        self.assertEqual(got["usable_before_download"], 15)
        self.assertEqual(got["dropped"], [])

    def test_two_distributors_agree_is_the_only_usable_state(self) -> None:
        models = tier_models()
        got, problems = rm.build(models, fetch_from(good_table(models)), min_candidates=None)
        self.assertEqual(problems, [])
        row = got["tiers_rows"]["8GB"][0]
        self.assertEqual((row["status"], row["usable_before_download"]), ("agree", True))
        self.assertEqual(row["sha256"], SHA_A)

    def test_single_distributor_row_stays_in_the_table_but_is_not_trusted(self) -> None:
        models = tier_models()
        table = good_table(models)
        del table[repo_of(models, "m/8a")]["ms"]
        got, problems = rm.build(models, fetch_from(table), min_candidates=None)
        self.assertEqual(problems, [])
        row = next(r for r in got["tiers_rows"]["8GB"] if r["name"] == "m/8a")
        self.assertEqual((row["status"], row["usable_before_download"]), ("single", False))
        self.assertEqual(got["usable_before_download"], 14,
                        "aggregate 必须由行算出来，不是手打的")

    def test_hash_conflict_never_becomes_a_recommended_row_with_a_hash(self) -> None:
        models = tier_models()
        table = good_table(models)
        f = file_of(models, "m/8a")
        table[repo_of(models, "m/8a")]["ms"][f] = entry(SHA_B, 4_000_000_000)
        got, problems = rm.build(models, fetch_from(table), min_candidates=None)
        self.assertEqual(problems, [])
        row = next(r for r in got["tiers_rows"]["8GB"] if r["name"] == "m/8a")
        self.assertEqual(row["status"], "conflict")
        self.assertIsNone(row["sha256"], "两家不等时不许返回任何一个当答案")
        self.assertFalse(row["usable_before_download"])

    def test_preferred_repo_without_a_fitting_file_falls_through(self) -> None:
        models = tier_models()
        table = good_table(models)
        repo = repo_of(models, "m/8a")
        want_file = file_of(models, "m/8a")
        models[next(i for i, m in enumerate(models) if m["name"] == "m/8a")]["gguf_sources"] = [
            {"repo": "ggml-org/T8A", "provider": "ggml-org"}, {"repo": repo, "provider": "unsloth"}]
        table.update(files_of("ggml-org/T8A",
                              {"T8A-Q8_0.gguf": entry(SHA_A, 8_709_518_656)},
                              {"T8A-Q8_0.gguf": entry(SHA_A, 8_709_518_656)}))
        got, problems = rm.build(models, fetch_from(table), min_candidates=None)
        self.assertEqual(problems, [])
        row = next(r for r in got["tiers_rows"]["8GB"] if r["name"] == "m/8a")
        self.assertEqual((row["repo"], row["file"]), (repo, want_file))
        self.assertEqual(row["repos_tried"], 2, "偏好方超档后要换下一家，并记下试了几家")

    def test_unresolvable_candidate_is_a_note_not_a_red_lane(self) -> None:
        models = tier_models()
        bad = model("m/bad", 1_000, 7.6, [{"repo": "ggml-org/BAD", "provider": "ggml-org"}])
        table = good_table(models)
        table.update(files_of("ggml-org/BAD", {"BAD-Q8_0.gguf": entry(SHA_A, 8_709_518_656)},
                              {"BAD-Q8_0.gguf": entry(SHA_A, 8_709_518_656)}))
        got, problems = rm.build([bad] + models, fetch_from(table), min_candidates=None)
        self.assertEqual(problems, [], "上游多一条解不出的候选不该让车道天天红")
        self.assertEqual([r["name"] for r in got["tiers_rows"]["8GB"]],
                         ["m/8a", "m/8b", "m/8c", "m/8d", "m/8e"], "跳过的行不能挤掉档位名额")
        self.assertEqual(len(got["dropped"]), 1)
        self.assertEqual(got["dropped"][0]["name"], "m/bad")
        self.assertIn("装不进 8 GB", got["dropped"][0]["why"])

    def test_repo_that_gives_no_file_table_is_dropped_with_its_own_reason(self) -> None:
        models = tier_models()
        bad = model("m/ghost", 1_000, 7.6, [{"repo": "nope/GHOST", "provider": "ggml-org"}])
        got, problems = rm.build([bad] + models, fetch_from(good_table(models)),
                                 min_candidates=None)
        self.assertEqual(problems, [])
        self.assertIn("没有一家给得出文件表", got["dropped"][0]["why"])

    def test_fetch_failure_is_a_problem_not_a_missing_file(self) -> None:
        models = tier_models()
        table = good_table(models)
        repo = repo_of(models, "m/8a")
        table[repo] = {"hf": {}, "ms": {}, "hf_by": "", "ms_by": "",
                       "hf_err": "curl 退出码 6", "ms_err": "curl 退出码 28"}
        got, problems = rm.build(models, fetch_from(table), min_candidates=None)
        self.assertTrue(any("两家都没答" in p and "取不到" in p for p in problems),
                        "取数失败要说取不到，不能被 classify 读成这仓库没这文件")
        self.assertNotIn("m/8a", [r["name"] for r in got["tiers_rows"]["8GB"]])

    def test_tier_floor_is_red_and_names_the_window_size(self) -> None:
        models = [m for m in tier_models() if m["name"] not in ("m/8b", "m/8c", "m/8d", "m/8e")]
        got, problems = rm.build(models, fetch_from(good_table(models)), min_candidates=None)
        self.assertTrue(any("档位 8GB 只剩 1 条" in p for p in problems), problems)
        self.assertTrue(any("窗口 8GB" in p for p in problems), problems)

    def test_candidate_floor_is_red_for_the_whole_layer(self) -> None:
        got, problems = rm.build(tier_models()[:2], fetch_from({}))
        self.assertTrue(any("描述层大概漂了" in p for p in problems),
                        "地板判据只在该被证的时候被证：喂进 2 条候选必须红")
        self.assertEqual(got["rows"], 0)

    def test_scan_budget_stops_at_per_tier_times_three(self) -> None:
        cands = [model("q/%02d" % i, 100 - i, 7.6,
                       [{"repo": "unsloth/R%02d" % i, "provider": "unsloth"}])
                 for i in range(20)]
        calls = []

        def fetch(repo, kind):
            calls.append((repo, kind))
            return {}, "", "刻意不答"      # 两家都失败：每条候选都要两次取数才能确认
        got, problems = rm.build(cands, fetch, per_tier=2, min_candidates=None)
        self.assertEqual(len(calls), 2 * 2 * rm.SCAN_MULT,
                         "预算封顶（per_tier×SCAN_MULT 条候选 × 两家）：解不出的候选"
                         "不能把车道拖成无限联网")
        self.assertEqual(got["tiers_rows"]["8GB"], [])
        self.assertTrue(any("只凑到 0 条" in p for p in problems))
        self.assertTrue(any("预算 6=per_tier×3" in p for p in problems))

    def test_license_passes_through_and_flags_the_risky_ones(self) -> None:
        models = tier_models()
        by_name = {m["name"]: m for m in models}
        by_name["m/8a"]["license"] = "cc-by-nc-4.0"
        by_name["m/8b"]["license"] = "unknown"
        got, problems = rm.build(models, fetch_from(good_table(models)), min_candidates=None)
        self.assertEqual(problems, [])
        rows = {r["name"]: r["license_flag"] for r in got["tiers_rows"]["8GB"]
                if r["name"] in ("m/8a", "m/8b", "m/8c")}
        self.assertEqual(rows, {"m/8a": "需向用户显示", "m/8b": "需向用户显示", "m/8c": "ok"})
        self.assertEqual(got["tiers_rows"]["8GB"][0]["license"], "cc-by-nc-4.0",
                         "许可要透传给用户，不能因为不好看到就丢")

    def test_use_case_travels_with_the_row_instead_of_filtering_it(self) -> None:
        models = tier_models()
        models[0]["use_case"] = "General purpose text generation"
        got, _ = rm.build(models, fetch_from(good_table(models)), min_candidates=None)
        row = next(r for r in got["tiers_rows"]["4GB"] if r["name"] == "m/4a")
        self.assertEqual(row["use_case"], "General purpose text generation",
                         "这个字段只透传：按它筛会踢掉 Qwen3-8B 这类被标成通用生成的强模型")

    def test_urls_come_from_the_measured_source_order(self) -> None:
        models = tier_models()
        got, _ = rm.build(models, fetch_from(good_table(models)), min_candidates=None)
        row = got["tiers_rows"]["4GB"][0]
        self.assertEqual([u.split("/")[2] for u in row["urls"]],
                         ["www.modelscope.cn", "hf-mirror.com", "huggingface.co"])


class TestParityWithSlice(unittest.TestCase):
    """切片必须能离线重算出联网那次的表 —— 这条是整个文件的地基。"""

    def test_slice_recomputes_the_captured_table(self) -> None:
        b = blob()
        got, problems = rm.build(b["models"], fetch_from(b["files"]),
                                 per_tier=b["per_tier"], min_candidates=None)
        self.assertEqual(problems, [])
        self.assertEqual(got["tiers_rows"], b["table"]["tiers_rows"])
        self.assertEqual(got["dropped"], b["table"]["dropped"])

    def test_committed_table_matches_the_slice(self) -> None:
        with open(TABLE, encoding="utf-8") as fh:
            committed = json.load(fh)
        b = blob()
        self.assertEqual(committed["tiers_rows"], b["table"]["tiers_rows"],
                         "已提交的表与捕获那次不一致：要么有人手改过，要么上游漂了")
        self.assertEqual(committed["rule"], b["table"]["rule"])
        self.assertGreaterEqual(committed["candidate_pool"]["chat"], 200,
                                "这张表必须由整层算出；候选池像切片规模就是被 --fixtures 覆盖过")
        self.assertEqual(committed["usable_before_download"],
                         sum(1 for k in committed["tiers_rows"]
                             for r in committed["tiers_rows"][k] if r["usable_before_download"]),
                         "aggregate 必须由行算出来")

    def test_every_committed_row_carries_an_explainable_choice(self) -> None:
        with open(TABLE, encoding="utf-8") as fh:
            committed = json.load(fh)
        for tier, rows in committed["tiers_rows"].items():
            self.assertGreaterEqual(len(rows), 3, "%s 档少于 3 条等于没内容" % tier)
            for r in rows:
                self.assertTrue(r["provider_reason"] and r["file_reason"])
                self.assertIsNotNone(r["size"], "%s 行没有大小，档位判断就是空的" % r["name"])
                self.assertLess(r["size"], float(tier[:-2]) * 2 ** 30,
                                "%s 档不该出现塞不进去的文件" % r["name"])
                self.assertNotIn("-of-", r["file"], "分片不能当整模型推荐")
                self.assertEqual(r["file"].lower().rsplit("/", 1)[-1].count(".gguf"), 1)


class TestCheck(unittest.TestCase):
    def fresh(self):
        b = blob()
        got, _ = rm.build(b["models"], fetch_from(b["files"]),
                          per_tier=b["per_tier"], min_candidates=None)
        return got

    def test_identical_is_clean(self) -> None:
        self.assertEqual(rm.check(self.fresh(), self.fresh()), [])

    def test_hash_drift_is_caught(self) -> None:
        committed = self.fresh()
        committed["tiers_rows"]["8GB"][0]["sha256"] = "0" * 64
        bad = rm.check(committed, self.fresh())
        self.assertEqual(len(bad), 1)
        self.assertIn("sha256", bad[0])

    def test_a_row_moving_tier_is_drift_not_a_pass(self) -> None:
        committed = self.fresh()
        committed["tiers_rows"]["16GB"] = committed["tiers_rows"]["8GB"]
        bad = rm.check(committed, self.fresh())
        self.assertTrue(any("解不出来" in x for x in bad), "按 档位+模型名 认行，挪档要红")

    def test_new_row_is_reported_not_silently_accepted(self) -> None:
        committed = self.fresh()
        committed["tiers_rows"]["4GB"] = committed["tiers_rows"]["4GB"][:-1]
        bad = rm.check(committed, self.fresh())
        self.assertTrue(any("上游新进来一条" in x for x in bad))

    def test_rule_change_is_caught(self) -> None:
        committed = self.fresh()
        committed["rule"]["quant"] = "Q8_0/Q4_K_M"
        bad = rm.check(committed, self.fresh())
        self.assertTrue(any("选择规则变了" in x for x in bad))

    def test_license_or_ram_reclassification_is_caught(self) -> None:
        # 许可与内存字段也进比对：RISKY_LICENSE 一改（生图那边发现 openrail 系漏标），
        # 这张表的 license_flag 就悄悄过期，而"许可必须到得了用户"是合规红线
        for field, value in (("license", "cc-by-nc-4.0"), ("license_flag", "需向用户显示"),
                             ("use_case", "Instruction following, chat"),
                             ("recommended_ram_gb", 0.1), ("min_ram_gb", 0.2)):
            committed = self.fresh()
            committed["tiers_rows"]["8GB"][0][field] = value
            bad = rm.check(committed, self.fresh())
            self.assertTrue(any(field in x for x in bad), (field, bad[:2]))


class TestCli(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = rm.main(argv)
        return rc, out.getvalue()

    def test_fixtures_check_passes_offline(self) -> None:
        rc, text = self.run_cli(["--fixtures", SLICE, "--check"])
        self.assertEqual(rc, 0, text)
        self.assertIn("问题 0 条", text)

    def test_check_is_load_bearing(self) -> None:
        b = blob()
        with open(TABLE, encoding="utf-8") as fh:
            committed = json.load(fh)
        committed["tiers_rows"]["8GB"][0]["size"] = 1
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "rec.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(committed, fh)
            rc, text = self.run_cli(["--fixtures", SLICE, "--check", "--out", p])
        self.assertEqual(rc, 1, text)
        self.assertIn("size:", text)

    def test_fixtures_out_refuses_the_non_live_path(self) -> None:
        with self.assertRaises(SystemExit) as e:
            with contextlib.redirect_stderr(io.StringIO()):
                rm.main(["--fixtures", SLICE, "--fixtures-out", os.devnull])
        self.assertEqual(e.exception.code, 2)

    def test_printed_table_shows_the_untrusted_rows(self) -> None:
        rc, text = self.run_cli(["--fixtures", SLICE])
        self.assertEqual(rc, 0, text)
        self.assertIn("需下载后自算", text, "single 行要在输出里看得见，不然级数等于没标")
        self.assertIn("跳过 0 条", text)

    def test_fixtures_run_can_never_overwrite_the_committed_table(self) -> None:
        # 这条是被自己咬出来的：某次 `--fixtures`（不带 --check）把整张表覆盖成了
        # 切片算出的那张（candidate_pool 从 1,283 变成 45），而 15 行看起来照样像回事
        before = open(TABLE, "rb").read()
        rc, text = self.run_cli(["--fixtures", SLICE])
        self.assertEqual(rc, 0, text)
        self.assertIn("不许落盘", text)
        self.assertEqual(open(TABLE, "rb").read(), before, "离线重算不该动产品表")

    def test_write_guard_blocks_slice_sized_and_fixtures_runs(self) -> None:
        b = blob()
        table = good_table(b["models"][:6])
        got, _ = rm.build(b["models"][:6], fetch_from(table), per_tier=1, min_candidates=None)
        self.assertLess(got["candidate_pool"]["chat"], 200, "这份就是切片规模，正是要挡的形状")
        ok, why = rm.write_guard(got, False)
        self.assertFalse(ok, "候选池像切片的规模就不许落盘")
        self.assertIn("不像整层", why)
        ok, why = rm.write_guard(got, True)
        self.assertFalse(ok, "离线重算永远不许写产品表")
        self.assertIn("离线重算", why)
        with open(TABLE, encoding="utf-8") as fh:
            committed = json.load(fh)
        ok, why = rm.write_guard(committed, False)
        self.assertTrue(ok, "正向对照：真表必须过得去闸门，否则这条判据只会一直红")


if __name__ == "__main__":
    unittest.main()
