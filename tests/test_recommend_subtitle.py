"""字幕档推荐表的离线测试：四张上游表互相咬，任何一个漂了都要红。

夹具是从 whisper.cpp **钉住的 b5454** 真取回来的四样东西（`tests/fixtures/whisper_*`）：
README 的 Memory usage 表、models/README 的 Model|Disk|SHA 表与三条语义规则、
download-ggml-model.sh 的白名单与下载源，加上宿主仓库 `ggerganov/whisper.cpp` 的文件表
（两家各一份）。所以这里断言的数字不是我抄的，工具重算一遍就能对上。
"""
import ast
import contextlib
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import recommend_subtitle as rs  # noqa: E402

FIXDIR = os.path.join(HERE, "fixtures")
TABLE = os.path.join(ROOT, "catalog", "subtitle-tiers.json")
PINS = os.path.join(ROOT, "pins", "models.json")


def inputs():
    return rs.load_fixtures(FIXDIR)


def rows_of(table=None):
    with open(TABLE, encoding="utf-8") as fh:
        d = table or json.load(fh)
    return {r["name"]: r for r in d["rows"]}


class TestParsers(unittest.TestCase):
    def test_mem_table_gives_five_families_with_bytes_and_lines(self) -> None:
        mem, problems = rs.parse_mem_table(inputs()[2])
        self.assertEqual(problems, [])
        self.assertEqual(sorted(mem), ["base", "large", "medium", "small", "tiny"])
        self.assertEqual(mem["tiny"]["mem_bytes"], 273e6)
        self.assertEqual(mem["medium"]["mem_bytes"], 2.1e9)
        self.assertEqual(mem["large"]["line"], 144, "行号要钉住，上游改表我们才看得见")

    def test_mem_unit_that_cannot_be_parsed_is_a_problem_not_a_missing_row(self) -> None:
        readme = inputs()[2].replace("| small  | 466 MiB | ~852 MB |", "| small | 466 MiB | 852 TB |")
        mem, problems = rs.parse_mem_table(readme)
        self.assertNotIn("small", mem)
        self.assertTrue(any("内存表只认到" in p for p in problems), problems)

    def test_model_table_reads_every_real_row_and_keeps_sha1_separate(self) -> None:
        upstream, problems = rs.parse_model_table(inputs()[3])
        self.assertEqual(problems, [])
        self.assertEqual(len(upstream), 16, "b5454 的模型表实测 16 行（表头与分隔行不算）")
        row = upstream["large-v3-turbo"]
        self.assertEqual(row["disk_text"], "1.5 GiB")
        self.assertEqual(len(row["blob_sha1"]), 40, "上游那一列是 40 位 git blob sha1")

    def test_script_gives_the_download_prefix_and_the_whitelist(self) -> None:
        prefix, names, problems = rs.parse_script(inputs()[4])
        self.assertEqual(problems, [])
        self.assertEqual(prefix, "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml")
        self.assertEqual(len(names), 30, "白名单条数是量出来的，改了就该红")
        self.assertIn("tiny.en-q5_1", names)

    def test_script_without_pfx_is_refused(self) -> None:
        prefix, names, problems = rs.parse_script('src="https://x"\nmodels="tiny\n"')
        self.assertEqual(prefix, "")
        self.assertTrue(any("读不到 src=/pfx=" in p for p in problems))

    def test_a_short_model_table_is_a_problem_not_a_small_table(self) -> None:
        # 变异扫描发现的空档：把 `if len(out) < 15` 改掉时，没有一条测试会红 ——
        # 也就是说"上游把表改没了"这件事原本只靠运气被发现
        head = "| Model | Disk | SHA |\n| --- | --- | --- |\n"
        short = head + "| tiny | 75 MiB | `%s` |\n" % ("0" * 40)
        upstream, problems = rs.parse_model_table(short)
        self.assertEqual(len(upstream), 1)
        self.assertTrue(any("上游模型表只认到" in p for p in problems), problems)

    def test_a_tier_that_cannot_be_filled_is_a_problem(self) -> None:
        tree, ms, readme, models_readme, script, langs = inputs()
        thin = readme.replace("| base   | 142 MiB | ~388 MB |\n"
                              "| small  | 466 MiB | ~852 MB |\n"
                              "| medium | 1.5 GiB | ~2.1 GB |\n"
                              "| large  | 2.9 GiB | ~3.9 GB |\n", "")
        # 只剩 tiny 一族有 Mem；再把 tiny.en 从模型表里抽掉，它就没有可对齐的 Disk 原文，
        # 于是 4GB 档只剩 1 条 —— 这一条测的就是"凑不满要红"，不是"表变小了也没事"
        thin_models = models_readme.replace("| tiny.en             | 75 MiB  |", "| tiny-en-noise   | 75 MiB  |")
        d, problems = rs.build(tree, ms, thin, thin_models, script)
        self.assertEqual([x["name"] for x in d["tiers_rows"]["4GB"]], ["tiny"])
        self.assertTrue(any("档位 4GB 只推荐得出 1 条" in p for p in problems), problems)
        self.assertTrue(any("内存表只认到" in p for p in problems), problems)

    def test_semantic_rule_lines_are_all_present_in_the_real_file(self) -> None:
        rules = rs.rule_lines(inputs()[3])
        self.assertEqual(rules["missing"], [])
        self.assertEqual(sorted(rules["lines"]), ["for_tests", "multilingual", "tdrz"])
        self.assertEqual(rs.rule_lines("什么都没写")["missing"],
                         ["multilingual", "tdrz", "for_tests"])


class TestHostFiles(unittest.TestCase):
    def test_empty_ci_placeholder_is_excluded_by_upstreams_own_rule(self) -> None:
        files, problems = rs.host_files({"for-tests-ggml-tiny.bin": {"sha256": "a" * 64, "size": 0},
                                        "ggml-tiny.bin": {"sha256": "b" * 64, "size": 10}})
        self.assertEqual(list(files), ["ggml-tiny.bin"])
        self.assertEqual(problems, {})

    def test_file_without_oid_is_named_not_dropped(self) -> None:
        files, problems = rs.host_files({"ggml-weird.bin": {"sha256": None, "size": 5}})
        self.assertEqual(files, {})
        self.assertIn("ggml-weird.bin", problems)

    def test_non_bin_entries_are_not_models(self) -> None:
        files, _ = rs.host_files({"README.md": {"sha256": "a" * 64, "size": 10},
                                 "ggml-tiny.bin": {"sha256": "b" * 64, "size": 10}})
        self.assertEqual(list(files), ["ggml-tiny.bin"])


class TestSplitVariant(unittest.TestCase):
    def test_quantized_english_and_family_are_separate_axes(self) -> None:
        self.assertEqual(rs.split_variant("ggml-small.en-q5_1.bin"),
                         ("small", "small.en-q5_1", "q5_1", True))
        self.assertEqual(rs.split_variant("ggml-large-v3-turbo-q8_0.bin"),
                         ("large", "large-v3-turbo-q8_0", "q8_0", False))
        self.assertEqual(rs.split_variant("ggml-tiny.bin"), ("tiny", "tiny", None, False))

    def test_quant_suffix_is_not_confused_with_a_family_name(self) -> None:
        fam, name, quant, eng = rs.split_variant("ggml-medium.en-q8_0.bin")
        self.assertEqual((fam, name, quant, eng), ("medium", "medium.en-q8_0", "q8_0", True))


class TestJoinAgainstRealCaptures(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = rows_of()

    def test_tiny_hash_is_the_same_pin_the_subtitle_lane_uses(self) -> None:
        # 跨产物不变量：这张表算出的 sha256 必须等于 pins/models.json 里那颗已钉的 pin，
        # 否则"车道用的模型"和"推荐给用户的模型"就是两套真相
        with open(PINS, encoding="utf-8") as fh:
            pin = json.load(fh)["ggml-tiny.bin"]["sha256"]
        self.assertEqual(self.rows["tiny"]["sha256"], pin)
        self.assertEqual(self.rows["tiny"]["size"], 77691713)

    def test_mem_join_only_accepts_the_same_disk_row_from_both_tables(self) -> None:
        self.assertEqual(self.rows["tiny"]["mem_text"], "273 MB")
        self.assertIn("上游 Memory usage 表", self.rows["tiny"]["mem_source"])
        turbo = self.rows["large-v3-turbo"]
        self.assertIsNone(turbo["mem_text"])
        self.assertIn("不是同一档", turbo["mem_source"])
        q = self.rows["tiny-q8_0"]
        self.assertIsNone(q["mem_text"])
        self.assertIn("量化档", q["mem_source"])

    def test_whole_axis_is_single_distributor_because_modelscope_does_not_mirror_it(self) -> None:
        statuses = {r["status"] for r in self.rows.values()}
        self.assertEqual(statuses, {"single"})
        self.assertFalse(any(r["usable_before_download"] for r in self.rows.values()))
        self.assertEqual(self.rows["tiny"]["distributor_absent"], ["modelscope"],
                         "实测最快的那个源对这个仓库根本不存在，要标出来给 UI 灰掉")

    def test_upstream_tables_disagree_in_exactly_the_measured_way(self) -> None:
        with open(TABLE, encoding="utf-8") as fh:
            d = json.load(fh)
        self.assertEqual(d["upstream_deltas"],
                         {"host_only": ["base.en-q8_0", "medium.en-q8_0", "small.en-q8_0",
                                        "tiny.en-q8_0"],
                          "whitelist_only": ["small.en-tdrz"]})

    def test_candidate_urls_keep_the_measured_throughput_order(self) -> None:
        hosts = [u.split("/")[2] for u in self.rows["tiny"]["urls"]]
        self.assertEqual(hosts, ["www.modelscope.cn", "hf-mirror.com", "huggingface.co"])
        self.assertTrue(self.rows["tiny"]["download_url_upstream"].startswith(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml/"))

    def test_sha1_column_never_leaks_into_the_content_hash_field(self) -> None:
        for name, r in self.rows.items():
            if r["upstream_blob_sha1"]:
                self.assertEqual(len(r["upstream_blob_sha1"]), 40, name)
                self.assertNotEqual(r["sha256"], r["upstream_blob_sha1"],
                                    "40 位那列是 git blob sha1，不能当 sha256 用")


class TestTierArithmetic(unittest.TestCase):
    def setUp(self) -> None:
        with open(TABLE, encoding="utf-8") as fh:
            self.d = json.load(fh)
        self.rows = rows_of(self.d)

    def test_default_tier_respects_headroom_and_fit_list_is_pure_arithmetic(self) -> None:
        cap = {"4GB": 4.0, "8GB": 8.0, "16GB": 16.0}
        for label, rows in self.d["tiers_rows"].items():
            for r in rows:
                full = self.rows[r["name"]]
                self.assertLessEqual(full["mem_bytes"], cap[label] * rs.UNIT["GiB"] * rs.HEADROOM,
                                     "%s 档给了塞不进（含余量）的 %s" % (label, r["name"]))
        big = [x["name"] for x in self.d["tiers_rows_fit_only"]["4GB"]]
        self.assertIn("large-v3", big, "3.9 GB 峰值内存算术上塞得进 4 GiB")
        self.assertNotIn("large-v3", [x["name"] for x in self.d["tiers_rows"]["4GB"]],
                         "但默认档不能把它给 4 GB 的机器")

    def test_multilingual_rows_sort_before_english_only_ones(self) -> None:
        for label, rows in self.d["tiers_rows"].items():
            names = [r["name"] for r in rows]
            for base in ("tiny", "base", "small", "medium"):
                if base in names and base + ".en" in names:
                    self.assertLess(names.index(base), names.index(base + ".en"), label)

    def test_no_row_without_a_mem_number_reaches_any_tier(self) -> None:
        for label in self.d["tiers"]:
            for r in self.d["tiers_rows"][label] + self.d["tiers_rows_fit_only"][label]:
                self.assertIsNotNone(self.rows[r["name"]]["mem_bytes"])

    def test_every_tier_has_at_least_two_choices(self) -> None:
        for label, rows in self.d["tiers_rows"].items():
            self.assertGreaterEqual(len(rows), 2, label)

    def test_inventory_counts_are_derived_from_rows(self) -> None:
        self.assertEqual(self.d["inventory"], len(self.d["rows"]))
        self.assertEqual(self.d["fit_unknown"],
                         sum(1 for r in self.d["rows"] if r["mem_bytes"] is None))


class TestCheck(unittest.TestCase):
    def fresh(self) -> dict:
        return rs.build(*inputs())[0]

    def test_identical_table_is_clean(self) -> None:
        with open(TABLE, encoding="utf-8") as fh:
            committed = json.load(fh)
        self.assertEqual(rs.check(committed, self.fresh()), [])

    def test_hash_drift_is_caught(self) -> None:
        with open(TABLE, encoding="utf-8") as fh:
            committed = json.load(fh)
        committed["rows"][0]["sha256"] = "0" * 64
        self.assertTrue(any("sha256" in x for x in rs.check(committed, self.fresh())))

    def test_mem_number_change_is_caught(self) -> None:
        with open(TABLE, encoding="utf-8") as fh:
            committed = json.load(fh)
        committed["rows"][0]["mem_bytes"] = 1.0
        self.assertTrue(any("mem_bytes" in x for x in rs.check(committed, self.fresh())))

    def test_tier_order_change_is_caught(self) -> None:
        with open(TABLE, encoding="utf-8") as fh:
            committed = json.load(fh)
        committed["tiers_rows"]["4GB"] = list(reversed(committed["tiers_rows"]["4GB"]))
        self.assertTrue(any("默认档顺序" in x for x in rs.check(committed, self.fresh())))

    def test_upstream_delta_change_is_caught(self) -> None:
        fresh = self.fresh()
        moved = json.loads(json.dumps(fresh))
        moved["upstream_deltas"]["whitelist_only"].append("忽然多出来的一个")
        self.assertTrue(any("出入变了" in x for x in rs.check(fresh, moved)))

    def test_ref_change_is_caught(self) -> None:
        fresh = self.fresh()
        moved = json.loads(json.dumps(fresh))
        moved["ref"] = "b9999"
        self.assertTrue(any("钉的 tag 变了" in x for x in rs.check(fresh, moved)))


class TestLanguages(unittest.TestCase):
    """字幕的第二个用户问题：我给中文视频打字幕，能不能选语言。只有引擎源码答得准。"""

    def block(self) -> str:
        with open(os.path.join(FIXDIR, "whisper_langs_b5454.json"), encoding="utf-8") as fh:
            blob = json.load(fh)
        self.assertEqual(blob["decl_line"], 286, "上游把语言表挪了位置，出处行号要跟着改")
        return blob["block"]

    def test_hundred_languages_with_contiguous_ids_from_zero(self) -> None:
        rows, decl, problems = rs.parse_langs(self.block(), 285)
        self.assertEqual(problems, [])
        self.assertEqual((decl, len(rows)), (286, 100))
        self.assertEqual([x["id"] for x in rows], list(range(100)),
                         "whisper_lang_max_id()+1 那个前提就是这条连续性")
        by = {x["code"]: x["id"] for x in rows}
        self.assertEqual((by["en"], by["zh"], by["yue"]), (0, 1, 99))

    def test_codes_with_digits_are_not_silently_dropped(self) -> None:
        # 我踩过的那种静默少项：字符集只写 `[A-Za-z-]` 时，带数字的代码一条都不解，
        # "100 条"变"2 条"。所以这里喂 100 个**带数字的代码**，要求它们全都解析出来 ——
        # 把正则收紧回纯字母，这条就必须红（条数地板接不住 2 条以下以外的情况）
        block = self.block()
        head = block.split("\n")[0]
        digits = head + "\n" + "\n".join('    { "l%02d",  { %d,  "lang-%d", } },' % (i, i, i)
                                         for i in range(100)) + "\n};\n"
        rows, _decl, problems = rs.parse_langs(digits)
        self.assertEqual(len(rows), 100, "带数字的语言代码被吃掉了")
        self.assertEqual([x["code"] for x in rows][:2], ["l00", "l01"])
        self.assertTrue(any("没有 en" in p for p in problems),
                        "这张合成表里没有 en/zh，基本盘检查要照样响")
        few = head + "\n" + "\n".join(block.split("\n")[1:31]) + "\n};\n"
        self.assertTrue(any("语言表只解出" in p for p in rs.parse_langs(few)[2]))

    def test_english_and_chinese_must_be_present(self) -> None:
        block = self.block()
        renamed = block.replace('{ "zh",  { 1,', '{ "cn",  { 1,')
        problems = rs.parse_langs(renamed)[2]
        self.assertTrue(any("语言表里没有 zh" in p for p in problems), problems)
        self.assertTrue(rs.parse_langs(block)[2] == [], "正向对照：真表不该报基本盘缺失")

    def test_shape_change_and_id_gap_are_problems(self) -> None:
        block = self.block()
        self.assertTrue(any("读不到 g_lang 声明" in p
                            for p in rs.parse_langs("没有这张表")[2]))
        self.assertTrue(any("读不到 g_lang 声明" in p for p in rs.parse_langs(
            block.replace("std::map<std::string, std::pair<int, std::string>>",
                          "std::vector<Whatever>"))[2]))
        self.assertTrue(any("语言 id 不是从 0 连续" in p for p in rs.parse_langs(
            block.replace('{ "de",  { 2,', '{ "de",  { 22,'))[2]))
        self.assertTrue(any("重复" in p for p in rs.parse_langs(
            block.replace('{ "zh",  { 1,', '{ "en",  { 1,'))[2]))

    def test_artifact_carries_the_axis_and_build_refuses_to_drop_it(self) -> None:
        with open(TABLE, encoding="utf-8") as fh:
            d = json.load(fh)
        self.assertEqual(d["languages"]["count"], 100)
        self.assertEqual(d["languages"]["count"], len(d["languages"]["codes"]))
        self.assertTrue(d["languages"]["ids_contiguous_from_zero"])
        self.assertEqual(d["languages"]["has"]["zh"], 1)
        tree, ms, readme, models_readme, script, langs = inputs()
        _d, problems = rs.build(tree, ms, readme, models_readme, script)
        self.assertTrue(any("语言表" in p for p in problems),
                        "缺语言表必须是问题，不能悄悄出一张少一轴的表")

    def test_check_catches_language_axis_drift(self) -> None:
        tree, ms, readme, models_readme, script, langs = inputs()
        fresh, _ = rs.build(tree, ms, readme, models_readme, script, langs)
        for field, value in (("count", 99), ("decl_line", 300)):
            moved = json.loads(json.dumps(fresh))
            moved["languages"][field] = value
            self.assertTrue(rs.check(fresh, moved), field)
        moved = json.loads(json.dumps(fresh))
        moved["languages"]["codes"] = moved["languages"]["codes"][:-1]
        self.assertTrue(any("语言表变了" in x for x in rs.check(fresh, moved)))


class TestCli(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = rs.main(argv)
        return rc, out.getvalue()

    def test_selftest_passes(self) -> None:
        rc, text = self.run_cli(["--selftest"])
        self.assertEqual(rc, 0, text)
        self.assertIn("BAD 数量 = 0", text)

    def test_selftest_is_load_bearing(self) -> None:
        orig = rs.parse_model_table
        try:
            rs.parse_model_table = lambda text: ({}, [])
            rc, text = self.run_cli(["--selftest"])
        finally:
            rs.parse_model_table = orig
        self.assertEqual(rc, 1, text)

    def test_offline_check_passes(self) -> None:
        rc, text = self.run_cli(["--fixtures", FIXDIR, "--check"])
        self.assertEqual(rc, 0, text)
        self.assertIn("问题 0 条", text)

    def test_fixtures_run_never_writes_the_committed_table(self) -> None:
        with open(TABLE, "rb") as fh:
            before = fh.read()
        rc, text = self.run_cli(["--fixtures", FIXDIR])
        self.assertEqual(rc, 0, text)
        self.assertIn("不落盘", text)
        with open(TABLE, "rb") as fh:
            self.assertEqual(fh.read(), before)

    def test_no_input_mode_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as e:
            with contextlib.redirect_stderr(io.StringIO()):
                rs.main([])
        self.assertEqual(e.exception.code, 2)


class TestNoHardcodedHost(unittest.TestCase):
    """这份工具的整条纪律是"宿主与域名从上游脚本里读"，那代码里就不该再有那家仓库的字面量。

    用 ast 而不是 grep：docstring 里出现 `ggerganov/whisper.cpp` 是**解释**（该留），
    代码里的字符串常量是**行为**（不该留）。上一版就是"嘴上读、手上还是查字面量那家"。
    """

    def test_host_literal_never_appears_in_code(self) -> None:
        path = os.path.join(ROOT, "tools", "recommend_subtitle.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        offenders = []
        for fn in [n for n in ast.walk(ast.parse(src))
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            body = list(fn.body)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]          # 函数 docstring 不算
            for sub in ast.walk(ast.Module(body=body, type_ignores=[])):
                if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)
                        and "ggerganov/whisper.cpp" in sub.value):
                    offenders.append(f"{fn.name}:{sub.lineno}")
        self.assertEqual(offenders, [],
                         "代码里又出现了宿主仓库的字面量（应改为脚本里读到的 host_repo）："
                         + ", ".join(offenders))

    def test_repo_is_read_from_the_captured_script(self) -> None:
        """正向对照：真捕获的脚本里读出的就是那家宿主 —— 不是我们背下来的。"""
        with open(os.path.join(FIXDIR, "whisper_download_script_b5454.sh"),
                  encoding="utf-8") as fh:
            prefix, _names, problems = rs.parse_script(fh.read())
        self.assertEqual(problems, [])
        self.assertEqual(rs.repo_from_prefix(prefix), "ggerganov/whisper.cpp")
        self.assertEqual(rs.repo_from_prefix("https://x/resolve/main/ggml"), "",
                         "少一层 owner/repo 的前缀不该被编成仓库 id")

    def test_host_follows_whichever_script_is_given(self) -> None:
        """live 查的那家必须是脚本说的那家：换一份 src=，宿主就得跟着换。

        这条是给 `host_for_inputs()` 用的 —— `live_inputs()` 自己离线跑不到（要 curl），
        所以"读宿主"这一步单独成函数，才能这样直接验。
        """
        with open(os.path.join(FIXDIR, "whisper_download_script_b5454.sh"),
                  encoding="utf-8") as fh:
            real = fh.read()
        self.assertEqual(rs.host_for_inputs(real), "ggerganov/whisper.cpp")
        swapped = real.replace('src="https://huggingface.co/ggerganov/whisper.cpp"',
                               'src="https://huggingface.co/some/other"')
        self.assertNotEqual(swapped, real, "夹具里那句 src= 变了，正向对照失效")
        self.assertEqual(rs.host_for_inputs(swapped), "some/other",
                         "宿主没跟着脚本变 —— 又写回字面量了")


if __name__ == "__main__":
    unittest.main()
