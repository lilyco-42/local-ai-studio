"""生图档位表的离线测试：候选池来自上游搜索，角色/许可/成人标记都按上游给的东西判。

夹具 `tests/fixtures/image_search.json` 与 `image_files.json` 是一次 `--live --snapshot`
真取回来的（43 个仓库 × 两家的 `.gguf` 文件表），不是我背的名单。
两份 sd.cpp 文档（`sd_performance.md`、`sd_docs_sd.md`、`sd_readme.md`）也是真捕获，
`pins/citations.json` 里那 5 条新出处就按它们核对。
"""
import contextlib
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import recommend_image as ri          # noqa: E402
import recommend_models as rmk        # noqa: E402
import recommend_subtitle as rsub     # noqa: E402

FIXDIR = os.path.join(HERE, "fixtures")
TABLE = os.path.join(ROOT, "catalog", "image-tiers.json")
PINS = os.path.join(ROOT, "pins", "models.json")


def load_table():
    with open(TABLE, encoding="utf-8") as fh:
        return json.load(fh)


def weights(table=None):
    d = table or load_table()
    return {(r["repo"], r["file"]): r for r in d["weights"]}


def fixtures_fetch():
    return ri.load_fixtures(FIXDIR)


class TestRoles(unittest.TestCase):
    """角色判据用的是真清单里出现过的名字，不是我编的形状。"""

    def test_text_encoder_and_vae_are_not_the_main_weight(self) -> None:
        for f, want in (("t5xxl_q3_k.gguf", "text_encoder"),
                        ("clip_l-q8_0.gguf", "text_encoder"),
                        ("SD3_prompt-llama_8b.Q8_0.gguf", "text_encoder"),
                        ("ae-f16.gguf", "vae"), ("sdxl_vae.safetensors.gguf", "vae"),
                        ("flux1-schnell-q4_k.gguf", "unet"),
                        ("stable-diffusion-v1-5-pruned-emaonly-f16.gguf", "unet"),
                        ("lora-upscale.s1.gguf", "lora")):
            self.assertEqual(ri.role_of(f)[0], want, f)

    def test_unknown_shape_is_other_not_silently_unet(self) -> None:
        self.assertEqual(ri.role_of("mystery_weights.gguf")[0], "unet")
        self.assertEqual(ri.role_of("vae_decoder.gguf")[0], "vae")

    def test_no_companion_file_reaches_any_tier(self) -> None:
        d, rows = load_table(), weights()
        for label, trows in d["tiers_rows"].items():
            self.assertGreaterEqual(len(trows), 2, label)
            for x in trows:
                row = rows[(x["repo"], x["file"])]
                self.assertEqual(row["role"], "unet",
                                 "%s 档出现了 %s（%s）" % (label, row["role"], x["file"]))
        self.assertGreater(d["roles"]["text_encoder"], 0,
                           "捕获里本来就有文本编码器 —— 上面那条判据才有内容")
        self.assertEqual(d["row_count"], len(d["weights"]))
        self.assertEqual(sum(d["roles"].values()), d["row_count"])


class TestUpstreamFlags(unittest.TestCase):
    def test_mature_repos_are_kept_out_of_tiers_by_upstreams_own_tag(self) -> None:
        d, rows = load_table(), weights()
        mature = {r["repo"] for r in d["weights"] if r["mature"]}
        self.assertTrue(mature, "这次捕获里确实有 not-for-all-audiences 的仓库")
        self.assertTrue(any(r["repo"] in mature for r in d["weights"]
                            if r["role"] == "unet" and not r["fits"]["4GB"]))
        for trows in d["tiers_rows"].values():
            self.assertFalse({x["repo"] for x in trows} & mature)
        self.assertEqual(d["mature_rows"], sum(1 for r in d["weights"] if r["mature"]))

    def test_license_is_passed_through_and_unknown_is_also_flagged(self) -> None:
        d = load_table()
        by_lic = {}
        for r in d["weights"]:
            by_lic.setdefault(str(r["license"]), set()).add(r["license_flag"])
        self.assertEqual(by_lic["apache-2.0"], {"ok"})
        self.assertTrue(all("需向用户显示" in f for f in by_lic["creativeml-openrail-m"]))
        self.assertTrue(all("许可不明" in f for f in by_lic["None"]),
                        "没有许可标签的行必须被点名，不能因为 str(None) 不像 nc 就放过")
        self.assertEqual(d["license_unknown_rows"],
                         sum(1 for r in d["weights"] if "许可不明" in r["license_flag"]))
        self.assertIn("creativeml-openrail-m", d["license_flags"])

    def test_engine_compatibility_claim_is_carried_not_invented(self) -> None:
        d = load_table()
        tagged = {r["repo"] for r in d["weights"] if r["engine_tags"]}
        self.assertTrue(tagged)
        for r in d["weights"]:
            if r["engine_tags"]:
                self.assertTrue(set(r["engine_tags"]) <= set(ri.ENGINE_TAGS))
        self.assertEqual(d["engine_tag_claims"], sum(1 for r in d["weights"] if r["engine_tags"]))


class TestArithmetic(unittest.TestCase):
    def test_tier_rows_obey_the_headroom_rule_and_are_sorted_by_size(self) -> None:
        d, rows = load_table(), weights()
        caps = dict(rmk.TIERS)
        for label, trows in d["tiers_rows"].items():
            prev = None
            for x in trows:
                full = rows[(x["repo"], x["file"])]
                self.assertLess(full["size"], caps[label] * rsub.UNIT["GiB"] * rsub.HEADROOM,
                                "%s 档给了塞不下的 %s" % (label, x["file"]))
                self.assertTrue(full["fits"][label])
                if prev is not None:
                    self.assertLessEqual(x["size"], prev)
                prev = x["size"]

    def test_equal_size_does_not_mean_equal_file(self) -> None:
        # 实测：leejet 的 q4_0 与 q4_k 大小一模一样而 sha256 不同。
        # 任何"按大小认文件"的想法都会在这里翻车。
        rows = weights()
        a = rows[("leejet/FLUX.1-schnell-gguf", "flux1-schnell-q4_0.gguf")]
        b = rows[("leejet/FLUX.1-schnell-gguf", "flux1-schnell-q4_k.gguf")]
        self.assertEqual(a["size"], b["size"])
        self.assertNotEqual(a["sha256"], b["sha256"])

    def test_pinned_sd15_hash_is_the_same_pin_the_image_lane_uses(self) -> None:
        with open(PINS, encoding="utf-8") as fh:
            pin = json.load(fh)["stable-diffusion-v1-5-pruned-emaonly-Q4_0.gguf"]["sha256"]
        row = weights()[("second-state/stable-diffusion-v1-5-GGUF",
                         "stable-diffusion-v1-5-pruned-emaonly-Q4_0.gguf")]
        self.assertEqual(row["sha256"], pin)
        self.assertEqual(row["status"], "agree")
        self.assertTrue(row["usable_before_download"])

    def test_two_distributor_agreement_is_counted_from_rows(self) -> None:
        d = load_table()
        self.assertEqual(d["usable_before_download"],
                         sum(1 for r in d["weights"] if r["usable_before_download"]))
        self.assertGreater(d["usable_before_download"], 100,
                           "生图这一轴大部分仓库 ModelScope 也镜像；掉太多就是解析漂了")
        single = [r for r in d["weights"] if r["status"] == "single"]
        self.assertTrue(all(not r["usable_before_download"] for r in single))

    def test_companions_travel_with_a_tier_row(self) -> None:
        d = load_table()
        with_companions = [x for trows in d["tiers_rows"].values() for x in trows
                           if x["companions"]]
        self.assertTrue(with_companions, "FLUX 那类仓库的配套件必须跟着走")
        for x in with_companions:
            for c in x["companions"]:
                self.assertIn(c["role"], ("vae", "text_encoder", "lora"))
                self.assertNotEqual(c["file"], x["file"])


class TestPoolDiscovery(unittest.TestCase):
    def make(self, rows_by_query, errors=None, files=None):
        def fetch_meta(_kind, q):
            return rows_by_query.get(q, []), (errors or {}).get(q)
        files = files or {}

        def files_for(repo):
            row = files.get(repo) or {}
            return (row.get("hf") or {}, "hf", row.get("ms") or {}, "ms", row.get("err"))
        return fetch_meta, files_for

    def test_dedupe_across_queries_keeps_the_first_hit(self) -> None:
        a = {"id": "leejet/X", "downloads": 5, "tags": ["gguf", "license:mit"]}
        b = {"id": "leejet/X", "downloads": 9, "tags": ["gguf"]}
        fetch_meta, _ = self.make({"q1": [a], "q2": [b]})
        repos, problems = ri.search_repos(fetch_meta, queries=("q1", "q2"))
        self.assertEqual(problems, [])
        self.assertEqual([r["repo"] for r in repos], ["leejet/X"])

    def test_a_query_returning_nothing_is_a_problem(self) -> None:
        fetch_meta, _ = self.make({}, errors={"q1": "curl 退出码 6"})
        repos, problems = ri.search_repos(fetch_meta, queries=("q1",))
        self.assertEqual(repos, [])
        self.assertTrue(any("没答" in p for p in problems), problems)
        fetch_meta2, _ = self.make({})
        self.assertTrue(any("返回 0 条" in p
                            for p in ri.search_repos(fetch_meta2, queries=("q1",))[1]))

    def test_repo_with_no_gguf_is_data_not_a_red_lane(self) -> None:
        rows = [{"id": "foo/Empty", "downloads": 3, "tags": ["gguf"]}]
        fetch_meta, files_for = self.make({"q1": rows})
        out, problems = ri.build(ri.search_repos(fetch_meta, queries=("q1",))[0], files_for)
        self.assertEqual(out["unresolved_repos"], ["foo/Empty"])
        self.assertFalse([p for p in problems if "没有" in p and "gguf" in p.lower()],
                         "上游撤了仓库不该被写成我们的判据失败")
        self.assertTrue(all("主体权重" in p for p in problems),
                        "只剩档位凑不满这一类真问题：%s" % problems)

    def test_unreachable_file_table_is_a_problem(self) -> None:
        rows = [{"id": "foo/X", "downloads": 3, "tags": ["gguf"]}]
        fetch_meta, files_for = self.make({"q1": rows}, files={"foo/X": {"err": "curl 6"}})
        out, problems = ri.build(ri.search_repos(fetch_meta, queries=("q1",))[0], files_for)
        self.assertTrue(any("取不到文件表" in p for p in problems), problems)
        self.assertEqual(out["unresolved_repos"], [], "取不到不等于没有")

    def test_tier_floor_is_a_problem_with_a_positive_control(self) -> None:
        def build_with(sizes):
            rows = [{"id": "leejet/One", "downloads": 5, "tags": ["gguf"]}]
            files = {"leejet/One": {
                "hf": {("a%d.gguf" % i): {"sha256": "a" * 64, "size": s}
                       for i, s in enumerate(sizes)},
                "ms": {("a%d.gguf" % i): {"sha256": "a" * 64, "size": s}
                       for i, s in enumerate(sizes)},
            }}
            fetch_meta, files_for = self.make({"q1": rows}, files=files)
            return ri.build(ri.search_repos(fetch_meta, queries=("q1",))[0], files_for)
        out, problems = build_with([10 ** 9])
        self.assertTrue(any("主体权重" in p for p in problems), problems)
        self.assertTrue(out["tiers_rows"]["4GB"], "1 GB 的文件应当进 4GB 档")
        _out2, problems2 = build_with([10 ** 9, 2 * 10 ** 9])
        self.assertEqual([p for p in problems2 if "主体权重" in p], [],
                         "凑满两行还报地板，就是判据写错了")

    def test_aggregate_counts_are_derived_not_copied(self) -> None:
        # 把 usable 写成常数（比如这次捕获的 191）必须被抓住：合成一份只有 1 行两家一致的小表
        rows = [{"id": "leejet/Two", "downloads": 5, "tags": ["gguf"]}]
        files = {"leejet/Two": {
            "hf": {"a.gguf": {"sha256": "a" * 64, "size": 10 ** 9},
                   "b.gguf": {"sha256": "b" * 64, "size": 2 * 10 ** 9}},
            "ms": {"a.gguf": {"sha256": "a" * 64, "size": 10 ** 9}}}}
        fetch_meta, files_for = self.make({"q1": rows}, files=files)
        out, _p = ri.build(ri.search_repos(fetch_meta, queries=("q1",))[0], files_for)
        self.assertEqual(out["row_count"], 2)
        self.assertEqual(out["usable_before_download"], 1,
                         "只有一行是两家同哈希，计数必须由行算")
        self.assertEqual(out["roles"], {"unet": 2})

    def test_author_repo_outranks_a_more_downloaded_third_party(self) -> None:
        rows = [{"id": "someone/Big", "downloads": 900, "tags": ["gguf"]},
                {"id": "leejet/Small", "downloads": 4, "tags": ["gguf"]}]
        repos = [{"repo": r["id"], "downloads": r["downloads"], "query": "q",
                  "license": None, "engine_tags": [], "mature": False} for r in rows]
        self.assertEqual([r["repo"] for r in ri.ranked_repos(repos)],
                         ["leejet/Small", "someone/Big"])

    def test_tier_floor_is_a_problem(self) -> None:
        rows = [{"id": "leejet/One", "downloads": 5, "tags": ["gguf"]}]
        files = {"leejet/One": {"hf": {"a.gguf": {"sha256": "a" * 64, "size": 10 ** 9}},
                                "ms": {"a.gguf": {"sha256": "a" * 64, "size": 10 ** 9}}}}
        fetch_meta, files_for = self.make({"q1": rows}, files=files)
        out, problems = ri.build(ri.search_repos(fetch_meta, queries=("q1",))[0], files_for)
        self.assertTrue(all("主体权重" in p for p in problems), problems)
        self.assertEqual(out["tiers_rows"]["4GB"][0]["file"], "a.gguf")

    def flux_files(self, unet_size: int, sha: str) -> dict:
        """一颗"齐"的 flux 仓库：unet + clip_l + t5xxl + ae，四样都带（上游 :16 点名要后三样）。"""
        names = {"flux1-big.gguf": unet_size, "clip_l-a.gguf": 200_000_000,
                 "t5xxl-a.gguf": 900_000_000, "ae-a.gguf": 200_000_000}
        return {"hf": {k: {"sha256": sha, "size": v} for k, v in names.items()},
                "ms": {k: {"sha256": sha, "size": v} for k, v in names.items()}}

    def test_mature_is_excluded_even_when_that_row_would_win_every_other_gate(self) -> None:
        """成人标记这条判据曾被"完备性优先"遮住：真实捕获里那两颗成人仓库排不到前面。

        所以这里造一颗**又大又齐**的成人 unet（7 GB，16GB 档里比另一家大），
        它必须仍然被上游自己的 `not-for-all-audiences` 挡在档位之外。
        """
        base = {"id": "leejet/Adult", "downloads": 50, "tags": ["gguf", ri.MATURE_TAG]}
        other = {"id": "someone/Okay", "downloads": 40, "tags": ["gguf"]}
        files = {"leejet/Adult": self.flux_files(7_000_000_000, "a" * 64),
                 "someone/Okay": self.flux_files(5_000_000_000, "b" * 64)}
        for tags, expect_in in ((True, False), (False, True)):
            adult = dict(base, tags=["gguf", ri.MATURE_TAG] if tags else ["gguf"])
            fetch_meta, files_for = self.make({"q1": [adult, other]}, files=files)
            out = ri.build(ri.search_repos(fetch_meta, queries=("q1",))[0], files_for)[0]
            repos_16 = [r["repo"] for r in out["tiers_rows"]["16GB"]]
            if expect_in:
                self.assertEqual(repos_16[0], "leejet/Adult",
                                 "正向对照：去掉成人标记，这颗又大又齐的就该是第一行")
            else:
                self.assertNotIn("leejet/Adult", repos_16,
                                 "带成人标记的行进了档位（它同时又是最大最齐的那颗）")


class TestCheck(unittest.TestCase):
    def fresh(self):
        fetch_meta, files_for = fixtures_fetch()
        repos, _ = ri.search_repos(fetch_meta)
        return ri.build(repos, files_for)[0]

    def mutated(self, fn):
        d = json.loads(json.dumps(self.fresh()))
        fn(d)
        return d

    def test_identical_is_clean(self) -> None:
        self.assertEqual(ri.check(self.fresh(), self.fresh()), [])

    def test_committed_matches_the_replay(self) -> None:
        self.assertEqual(ri.check(load_table(), self.fresh()), [])

    def test_replay_equals_the_committed_artifact_deeply(self) -> None:
        # check() 只比对它关心的字段；这条是整张表的深比对 ——
        # 变异扫描发现"配套件被改成假角色"这种问题它能活下来，就是因为没人真的比全表
        committed, fresh = load_table(), self.fresh()
        for key in ("tiers_rows", "roles", "mature_rows", "license_unknown_rows",
                    "usable_before_download", "row_count", "unresolved_repos",
                    "engine_tag_claims", "license_flags", "mem_facts"):
            self.assertEqual(committed[key], fresh[key], key)

    def test_each_field_drift_is_caught(self) -> None:
        cases = [
            ("sha256", lambda d: d["weights"].__setitem__(0, dict(d["weights"][0],
                                                                  sha256="0" * 64))),
            ("license", lambda d: d["weights"].__setitem__(0, dict(d["weights"][0],
                                                                   license="mit"))),
            ("role", lambda d: d["weights"].__setitem__(0, dict(d["weights"][0], role="vae"))),
            ("mature", lambda d: d["weights"].__setitem__(0, dict(d["weights"][0],
                                                                  mature=True))),
        ]
        for name, fn in cases:
            self.assertTrue(any(name in x or "新出现" in x or "解不出来" in x
                                for x in ri.check(self.fresh(), self.mutated(fn))), name)

    def test_completeness_and_default_state_drift_are_caught(self) -> None:
        # 完备性判定是"装完能不能出图"这件事的唯一记录，两张表一漂就得红
        label = next(t for t, s in self.fresh()["default_row_states"].items()
                     if s == "complete")
        bad = ri.check(self.fresh(), self.mutated(
            lambda d: d["tiers_rows"][label][0].__setitem__(
                "completeness", {"state": "incomplete", "needs": ["VAE"], "missing": ["VAE"]})))
        self.assertTrue(any("completeness 变了" in x for x in bad), bad)
        bad2 = ri.check(self.fresh(), self.mutated(
            lambda d: d["default_row_states"].__setitem__(label, "incomplete")))
        self.assertTrue(any("判定变了" in x for x in bad2), bad2)
        self.assertTrue(any("completeness 变了" in x for x in ri.check(
            self.fresh(), self.mutated(lambda d: d["tiers_rows"][label][0].pop("completeness")))),
                        "整张表少了 completeness 字段也不红")

    def test_rule_ref_queries_and_unresolved_sets_are_compared(self) -> None:
        self.assertTrue(any("rule" in x for x in
                            ri.check(self.fresh(), self.mutated(lambda d: d.__setitem__(
                                "rule", dict(d["rule"], fit="改了"))))))
        self.assertTrue(any("ref" in x for x in
                            ri.check(self.fresh(), self.mutated(lambda d: d.__setitem__(
                                "ref", "deadbeef")))))
        self.assertTrue(any("queries" in x for x in
                            ri.check(self.fresh(), self.mutated(lambda d: d.__setitem__(
                                "queries", ["别的查询"])))) )
        self.assertTrue(any("那批仓库变了" in x for x in
                            ri.check(self.fresh(), self.mutated(lambda d: d.__setitem__(
                                "unresolved_repos", ["冒出来的一个"])))),
                        "unresolved 集合变了必须看得见 —— 那是一批真实存在的仓库")

    def test_tier_membership_change_is_caught(self) -> None:
        def fn(d):
            d["tiers_rows"]["16GB"] = []
        self.assertTrue(any("16GB" in x for x in ri.check(self.fresh(), self.mutated(fn))))


class TestCli(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = ri.main(argv)
        return rc, out.getvalue()

    def test_offline_check_passes(self) -> None:
        rc, text = self.run_cli(["--fixtures", FIXDIR, "--check"])
        self.assertEqual(rc, 0, text)
        self.assertIn("问题 0 条", text)

    def test_fixtures_never_write(self) -> None:
        with open(TABLE, "rb") as fh:
            before = fh.read()
        rc, text = self.run_cli(["--fixtures", FIXDIR])
        self.assertEqual(rc, 0, text)
        self.assertIn("不落盘", text)
        with open(TABLE, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_snapshot_requires_live(self) -> None:
        with self.assertRaises(SystemExit) as e:
            with contextlib.redirect_stderr(io.StringIO()):
                ri.main(["--fixtures", FIXDIR, "--snapshot", FIXDIR])
        self.assertEqual(e.exception.code, 2)

    def test_memory_claim_is_written_as_none_with_a_reason(self) -> None:
        d = load_table()
        self.assertTrue(d["rule"]["memory_claim"].startswith("none"))
        self.assertEqual(len(d["mem_facts"]), 3,
                         "'内存是运行时量的' 这三条依据都要能被 citation-drift 查")


if __name__ == "__main__":
    unittest.main()
