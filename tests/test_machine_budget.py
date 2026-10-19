"""装机预算的离线测试：每一条求和都要能被重新算出来，且**必须穿过工具本身**。

上一轮的教训（自己踩的）：只读已提交产物的测试抓不住生成器的错，所以这个文件里
凡是要证明"数字对"的地方，都同时用 `mb.build(mb.load(mb.ROOT))` 重算一遍再比 ——
不是比两张 JSON 长得像。
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import machine_budget as mb      # noqa: E402
import r2_manifest               # noqa: E402

TABLE = os.path.join(ROOT, "catalog", "machine-budget.json")
MIB = 1024 ** 2


def artifact():
    with open(TABLE, encoding="utf-8") as fh:
        return json.load(fh)


def recomputed():
    return mb.build(mb.load(mb.ROOT))


def src(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return json.load(fh)


class TestAgainstSourceArtifacts(unittest.TestCase):
    """预算不是自说自话：每行的字节必须能在来源产物里找到同一颗。"""

    def setUp(self) -> None:
        self.d = artifact()
        self.chat = src("catalog/recommendations.json")
        self.sub = src("catalog/subtitle-tiers.json")
        self.img = src("catalog/image-tiers.json")

    def test_default_rows_are_the_same_rows_the_tier_tables_picked(self) -> None:
        for tier, block in self.d["per_tier"].items():
            files = {i["file"] for i in block["items"]}
            self.assertIn(self.chat["tiers_rows"][tier][0]["file"], files)
            self.assertIn(self.sub["tiers_rows"][tier][0]["file"], files)
            self.assertIn(self.img["tiers_rows"][tier][0]["file"], files)
            chat = next(i for i in block["items"] if i["kind"] == "chat")
            self.assertEqual(chat["bytes"], self.chat["tiers_rows"][tier][0]["size"])

    def test_subtitle_file_name_is_not_rewrapped(self) -> None:
        # 我自己踩过一次：档位表给的已经是 ggml-medium.bin，再套一层就成了
        # ggml-ggml-medium.bin.bin —— 文件名会直接对不上下载层
        for tier, block in self.d["per_tier"].items():
            names = [i["file"] for i in block["items"] if i["kind"] == "subtitle"]
            self.assertEqual(names, [self.sub["tiers_rows"][tier][0]["file"]])
            for n in names:
                self.assertNotIn("ggml-ggml", n)
                self.assertTrue(n.startswith("ggml-") and n.endswith(".bin"), n)

    def test_ocr_default_trio_sizes_match_the_pins_exactly(self) -> None:
        entries = src("catalog/defaults.json")["entries"]
        ocr = {i["file"]: i for i in self.d["per_tier"]["4GB"]["items"] if i["kind"] == "ocr"
               and i["file"] != "(合计)"}
        pinned = {k: v for k, v in entries.items() if "ocr" in k.lower()}
        self.assertEqual(set(ocr), set(pinned), "默认档三条必须全进预算")
        for k, v in pinned.items():
            self.assertEqual(ocr[k]["bytes"], v["size"])
            two_distributors = "4.5" in (v.get("strength") or {}).get("rungs", [])
            self.assertEqual(ocr[k]["trusted_before_download"], two_distributors,
                             "两家一致要从 strength.rungs 读，不是无条件当真")

    def test_all_languages_number_is_reported_once_and_not_stacked(self) -> None:
        items = [i for i in self.d["per_tier"]["4GB"]["items"] if i["kind"] == "ocr"]
        self.assertEqual(sum(1 for i in items if i["file"] == "(合计)"), 1)
        langs = src("pins/ocr-languages.json")["rec_dict_bytes"]
        self.assertEqual(next(i["bytes"] for i in items if i["file"] == "(合计)"), langs)
        trio = sum(i["bytes"] for i in items if i["file"] != "(合计)")
        self.assertLess(trio, langs, "全语言合计本身就比默认档大，两者是替代不是相加")

    def test_trust_flags_are_copied_from_the_tables_not_recomputed(self) -> None:
        for tier, block in self.d["per_tier"].items():
            for i in block["items"]:
                if i["kind"] == "chat":
                    src_row = next(r for r in self.chat["tiers_rows"][tier] if r["file"] == i["file"])
                elif i["kind"] == "image":
                    # 配套件不在 tiers_rows 里（那里只有主体行），必须回 weights 按（仓库, 文件）查
                    src_row = next(r for r in self.img["weights"]
                                   if (r["repo"], r["file"]) == (i["name"], i["file"]))
                elif i["kind"] == "subtitle":
                    src_row = {"usable_before_download": False}   # 整轴只有一家给哈希
                else:
                    continue
                self.assertEqual(i["trusted_before_download"], src_row["usable_before_download"],
                                 (tier, i["file"]))
            self.assertEqual(sorted(block["untrusted_rows"]),
                             sorted({i["file"] for i in block["items"]
                                     if i["trusted_before_download"] is False}))


class TestArithmetic(unittest.TestCase):
    def setUp(self) -> None:
        self.d = artifact()

    def test_download_bytes_is_weights_plus_engine(self) -> None:
        plan = src("pins/payload-plan.json")
        for tier, block in self.d["per_tier"].items():
            self.assertEqual(block["weights_bytes"], sum(i["bytes"] for i in block["items"]))
            for os_key, ov in block["by_os"].items():
                self.assertEqual(ov["download_bytes"],
                                 block["weights_bytes"] + int(round(ov["engine_download_mib"] * MIB)),
                                 (tier, os_key))
                self.assertEqual(ov["engine_download_mib"], plan["totals_mib"][os_key])
                self.assertAlmostEqual(ov["download_gib"], ov["download_bytes"] / 1024 ** 3, places=2)
            self.assertEqual({o: v["download_gib"] for o, v in block["by_os"].items()},
                             self.d["totals_gib"][tier], "汇总数字必须由每档算出")

    def test_companions_are_counted_and_separately_visible(self) -> None:
        for tier, block in self.d["per_tier"].items():
            comp = sum(i["bytes"] for i in block["items"] if i.get("companion_of"))
            self.assertEqual(block["companion_bytes"], comp)
            counted = [i["need_key"] for i in block["items"] if i.get("companion_of")]
            self.assertEqual(len(counted), len(set(counted)),
                             "同一个旗标的两个量化都被当成必下的文件加进预算了")
            self.assertEqual(sorted(counted), block["image_companions"]["needs_counted"])
            ic = block["image_companions"]
            # 上游例子点名的每一样：要么进了加法，要么明写着缺（不能不吭声地少算）
            for need in ic.get("needs") or []:
                self.assertTrue(need in counted or need in (ic.get("missing") or []),
                                (tier, need, counted, ic.get("missing")))
            tiers_src = src("catalog/image-tiers.json")
            repo_rows = [w for w in tiers_src["weights"] if w["repo"] == ic["repo"]]
            for i in block["items"]:
                if not i.get("companion_of"):
                    continue
                self.assertNotEqual(i["file"], i["companion_of"])
                self.assertIn(i["role"], ("vae", "text_encoder", "lora"))
                cands = [w["size"] for w in repo_rows if mb.need_key(w) == i["need_key"]]
                self.assertEqual(i["bytes"], min(cands), (tier, i["file"]))
                self.assertEqual(i["group_alternates"], len(cands) - 1)
        with_comp = [i for t in self.d["per_tier"].values() for i in t["items"]
                     if i.get("companion_of")]
        self.assertTrue(with_comp,
                        "默认档至少要有一行真的带配套件，否则上面的加法判据全是空的（合成那半在 selftest）")

    def test_time_is_derived_from_the_measured_rate_module(self) -> None:
        self.assertEqual(self.d["rates_mib_s"]["modelscope"],
                         float(next(r for k, r, _n in r2_manifest.RATES_MIB_S if "modelscope" in k)))
        self.assertEqual(self.d["rates_mib_s"]["hf-mirror"], 0.25)
        for tier, block in self.d["per_tier"].items():
            for os_key, ov in block["by_os"].items():
                want = round(ov["download_bytes"] / MIB / self.d["rates_mib_s"]["modelscope"] / 60, 1)
                self.assertEqual(ov["minutes_at_measured_rate"], want, (tier, os_key))
                self.assertGreater(ov["worst_case_hours_at_slowest_measured"],
                                   ov["minutes_at_measured_rate"] / 60)
        self.assertIn("lain42", self.d["measured_at_exit"],
                      "换算用的吞吐是哪台机器量的，必须写在产物里")

    def test_missing_and_unknown_landed_are_different_things(self) -> None:
        plan = src("pins/payload-plan.json")
        mac = self.d["per_tier"]["4GB"]["by_os"]["macos-arm64"]
        self.assertEqual(mac["missing_capabilities"], ["字幕"], "whisper 上游没有 macOS 产物")
        self.assertNotIn("对话", mac["missing_capabilities"])
        for os_key, ov in self.d["per_tier"]["4GB"]["by_os"].items():
            # 三个 OS 都不该再有"落地大小未知"：tar.gz 那几行现在读了成员表
            self.assertEqual(ov["unknown_landed_sizes"], [], os_key)
            self.assertIsNotNone(ov["engine_landed_mib"], os_key)
            want = round(sum(r.get("uncompressed_mib") or 0 for r in plan["os"][os_key]), 1)
            self.assertAlmostEqual(ov["engine_landed_mib"], want, places=1, msg=os_key)
        # "没有产物"与"落地大小未知"是两件事这条判据本身，由 --selftest 里那行
        # needs_extract 的合成包负责（真数据现在三行都有落地大小了，读产物的测试碰不到它）

    def test_budget_refuses_to_invent_a_memory_number(self) -> None:
        self.assertEqual(self.d["ram_statement"], "本文件的 download_bytes 是磁盘与下载量，不是峰值内存")
        self.assertIn("不加内存", self.d["rule"]["ram"])
        for tier, block in self.d["per_tier"].items():
            for os_key, ov in block["by_os"].items():
                self.assertNotIn("peak_ram", json.dumps(ov))


class TestRecompute(unittest.TestCase):
    def test_rebuild_equals_the_committed_artifact(self) -> None:
        fresh, problems = recomputed()
        self.assertEqual(problems, [])
        self.assertEqual(mb.check(artifact(), fresh), [])
        self.assertEqual(fresh["totals_gib"], artifact()["totals_gib"])
        self.assertEqual(fresh["per_tier"]["16GB"]["by_os"]["win-x64"]["download_bytes"],
                         artifact()["per_tier"]["16GB"]["by_os"]["win-x64"]["download_bytes"])

    def test_check_is_load_bearing(self) -> None:
        fresh, _ = recomputed()
        for label, mutate in [
            ("download_bytes", lambda d: d["per_tier"]["4GB"]["by_os"]["win-x64"].__setitem__(
                "download_bytes", 1)),
            ("items", lambda d: d["per_tier"]["4GB"]["items"].pop(0)),
            ("missing", lambda d: d["per_tier"]["4GB"]["by_os"]["macos-arm64"].__setitem__(
                "missing_capabilities", [])),
            ("landed", lambda d: d["per_tier"]["4GB"]["by_os"]["win-x64"].__setitem__(
                "unknown_landed_sizes", ["生图"])),
            ("rule", lambda d: d.__setitem__("rule", {})),
            ("tiers", lambda d: d.__setitem__("tiers", ["2GB"])),
            ("minutes", lambda d: d["per_tier"]["4GB"]["by_os"]["win-x64"].__setitem__(
                "minutes_at_measured_rate", 1.0)),
            ("worst_case", lambda d: d["per_tier"]["4GB"]["by_os"]["win-x64"].__setitem__(
                "worst_case_hours_at_slowest_measured", 9.99)),
            ("rates", lambda d: d.__setitem__(
                "rates_mib_s", {"modelscope": 4.2, "hf-mirror": 0.0})),
            ("engine_mib", lambda d: d["per_tier"]["4GB"]["by_os"]["win-x64"].__setitem__(
                "engine_download_mib", 0.0)),
            ("companion_bytes", lambda d: d["per_tier"]["4GB"].__setitem__("companion_bytes", 1)),
            ("image_companions", lambda d: d["per_tier"]["4GB"].__setitem__(
                "image_companions", {"repo": "x/y", "roles": ["vae"], "alternates": 3})),
            ("carries", lambda d: d["per_tier"]["4GB"].__setitem__("image_lower_bound", True)),
        ]:
            mutated = json.loads(json.dumps(fresh))
            mutate(mutated)
            self.assertTrue(mb.check(fresh, mutated), label)
        self.assertEqual(mb.check(fresh, fresh), [])

    def test_ocr_trust_goes_through_the_builder_not_the_artifact(self) -> None:
        # 已提交那张里 OCR 三条全含 4.5，所以读产物的测试天然验不出"标记是读来的"。
        # 把其中一条的 strength 改成只有一家，必须穿过 build() 立刻掉进 untrusted_rows。
        arts = mb.load(mb.ROOT)
        ent = arts["catalog/defaults.json"]["entries"]
        key = next(k for k in sorted(ent) if "ocr" in k.lower())
        ent[key]["strength"] = {"rungs": ["3"]}
        fresh, problems = mb.build(arts)
        self.assertEqual(problems, [])
        row = next(i for i in fresh["per_tier"]["4GB"]["items"] if i["file"] == key)
        self.assertFalse(row["trusted_before_download"], "把只有一家的条目当真了")
        self.assertEqual(row["hash_status"], "single")
        self.assertIn(key, fresh["per_tier"]["4GB"]["untrusted_rows"])

    def test_an_unmeasured_exit_gives_no_wait_time(self) -> None:
        # 吞吐表里没有的出口只能换来"不给等待时间"，不能是一个编出来的数
        self.assertEqual(mb.rate_mib_s("这台机器没量过这个出口")[0], 0.0)
        self.assertIn("不编一个数字", mb.rate_mib_s("没有这个出口")[1])
        for needle, key in (("modelscope", "modelscope"), ("hf-mirror", "hf-mirror.com")):
            want = float(next(r for k, r, _n in r2_manifest.RATES_MIB_S if k == key))
            self.assertEqual(mb.rate_mib_s(needle)[0], want,
                             f"短针 {needle} 必须匹配到表里的长键 {key}")


class TestCli(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = mb.main(argv)
        return rc, out.getvalue()

    def test_selftest_passes(self) -> None:
        rc, text = self.run_cli(["--selftest"])
        self.assertEqual(rc, 0, text)
        self.assertIn("BAD 数量 = 0", text)

    def test_selftest_is_load_bearing(self) -> None:
        orig = mb.ocr_items
        try:
            mb.ocr_items = lambda arts: []
            rc, text = self.run_cli(["--selftest"])
        finally:
            mb.ocr_items = orig
        self.assertEqual(rc, 1, text)
        self.assertIn("OCR 默认档没进加法", text)

    def test_check_passes_against_the_committed_table(self) -> None:
        rc, text = self.run_cli(["--check"])
        self.assertEqual(rc, 0, text)
        self.assertIn("问题 0 条", text)

    def test_every_tier_and_os_is_printed(self) -> None:
        # 必须给一个临时 --out：变异扫描里"模块被改坏 + 这条走默认 --out"会把已提交那张
        # 真的覆盖掉 —— 同一个教训第二次犯（离线重算不该拥有产品表的写权）
        with tempfile.TemporaryDirectory() as d:
            rc, text = self.run_cli(["--out", os.path.join(d, "budget.json")])
            self.assertEqual(rc, 0, text)
            self.assertEqual(text.count("实测吞吐约"), 9, "3 档 × 3 个 OS")
            self.assertIn("要下完自算的行", text)
            self.assertTrue(os.path.exists(os.path.join(d, "budget.json")),
                            "给了 --out 却没落盘")


class TestAbsentVersusUnknown(unittest.TestCase):
    """缺席与未知是两件事，落地大小这一栏最容易被拖浑。"""

    def arts(self, eng_rows, totals):
        return {
            "pins/payload-plan.json": {"totals_mib": {"win-x64": totals}, "os": {"win-x64": eng_rows},
                                       "gaps": []},
            "catalog/defaults.json": {"entries": {}},
            "pins/ocr-languages.json": {},
            "catalog/recommendations.json": {"tiers": ["4GB"], "tiers_rows": {"4GB": []}},
            "catalog/subtitle-tiers.json": {"tiers_rows": {"4GB": []}},
            "catalog/image-tiers.json": {"tiers_rows": {"4GB": []}, "weights": []},
        }

    def test_a_row_without_an_engine_contributes_nothing_rather_than_unknown(self) -> None:
        arts = self.arts([{"capability": "对话", "engine": "llama", "status": "ok",
                           "archive_mib": 5.0, "uncompressed_mib": 10.0},
                          {"capability": "字幕", "engine": None, "status": "上游无产物",
                           "archive_mib": None}], 5.0)
        blk = mb.build(arts)[0]["per_tier"]["4GB"]["by_os"]["win-x64"]
        self.assertEqual(blk["engine_landed_mib"], 10.0, blk)
        self.assertEqual(blk["unknown_landed_sizes"], [], blk)
        self.assertEqual(blk["missing_capabilities"], ["字幕"], blk)

    def test_a_real_needs_extract_row_still_makes_landed_size_unknown(self) -> None:
        """反向：真读不了成员表的那一行，仍然必须把整机标成未知。"""
        arts = self.arts([{"capability": "对话", "engine": "llama", "status": "needs_extract",
                           "archive_mib": 5.0},
                          {"capability": "字幕", "engine": "whisper", "status": "ok",
                           "archive_mib": 2.0, "uncompressed_mib": 4.0}], 7.0)
        blk = mb.build(arts)[0]["per_tier"]["4GB"]["by_os"]["win-x64"]
        self.assertIsNone(blk["engine_landed_mib"], blk)
        self.assertEqual(blk["unknown_landed_sizes"], ["对话"], blk)


if __name__ == "__main__":
    unittest.main()
