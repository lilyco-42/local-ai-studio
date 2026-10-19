"""tools/ocr_language_pins.py 的判据测试 —— 全部离线跑在仓库夹具上。

夹具是 2026-10-20 从四张真表抓的（见 tests/fixtures/ocr_languages.meta.json）：
models.md、GitHub release 产物（112 个名字 / 按 tag 分层）、ModelScope 镜像的文件表、
以及上游 auto-download 的 registry.rs。所以这些用例测的是"真数据上的判据"，
不是我自己编的形状。

最要紧的一条是 TestTagScoping：`ppocrv5_latin_dict.txt` 这个名字在 v0.1.0 与 v0.3.0
里是**两个不同文件**（1,634 B / 2,616 B，哈希也不同）。第一版按名字找、后写的覆盖先写的，
直接把拉丁语字典钉成了另一份文件 —— --live 报"来源哈希不一致"才把它逼出来。
"""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ocr_language_pins as P  # noqa: E402

PINS = os.path.join(ROOT, "pins", "ocr-languages.json")


def sources():
    return P.load_fixtures()


class TestBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.md, cls.gh, cls.ms, cls.reg = sources()
        cls.pins, cls.problems, cls.external = P.build(cls.md, cls.gh, cls.ms, cls.reg)

    def test_no_problems_on_real_tables(self) -> None:
        """正向对照：真表上必须 0 问题（否则下面所有"红"都可能是判据本身坏了）。"""
        self.assertEqual(self.problems, [])

    def test_fourteen_languages_derived_not_typed(self) -> None:
        self.assertEqual(self.pins["languages"], 14)
        self.assertEqual(len(self.pins["entries"]), 14)

    def test_totals_are_summed_from_entries(self) -> None:
        """aggregate 必须由逐条相加得出，不许抄文档里的数字。"""
        per = sum(e["rec"]["size"] + (e["dict"].get("size") or 0)
                  for e in self.pins["entries"].values())
        self.assertEqual(per, self.pins["rec_dict_bytes"])
        self.assertEqual(self.pins["rec_dict_mib"], round(per / P.MIB, 1))
        self.assertEqual(self.pins["with_det_mib"],
                         round((per + self.pins["shared_det"]["size"]) / P.MIB, 1))

    def test_i18n_doc_numbers_reproduced(self) -> None:
        """docs/i18n.md 写的是"每种取最新版 117.5 MiB，加共享 det ≈ 122.1 MiB"。
        这两个数字以前是手抄的，现在这条用例把它变成可重跑的推导结果。"""
        self.assertEqual(self.pins["rec_dict_mib"], 117.5,
                         "与 docs/i18n.md 的 117.5 MiB 不符：上游换过权重，文档与这张表要一起重看")
        self.assertEqual(self.pins["with_det_mib"], 122.1,
                         "与 docs/i18n.md 的「加共享 det ≈ 122.1 MiB」不符，同上")

    def test_three_dictionaries_are_external_by_design(self) -> None:
        """繁中/日文/格鲁吉亚的字典两家都没有 —— 这是**缺口**，不是解析失败。"""
        self.assertEqual(sorted(self.external),
                         ["chinese_cht_dict.txt", "japan_dict.txt", "ka_dict.txt"])
        for e in self.pins["entries"].values():
            if e["dict"].get("level") != "4.5":
                self.assertEqual(e["dict"]["level"], "自算 + 钉 commit")
                self.assertIsNone(e["dict"]["sha256"])   # --fixtures 不下载，所以是空

    def test_every_pinned_row_carries_file_hash_size_and_release(self) -> None:
        """缺 file 的 pin 会让消费方去解析 URL；缺 release 的会让漂移检查找不到 tag。
        这两个字段都曾经真缺过（shared_det 少 file，check() 直接 KeyError 崩掉）。"""
        for key, e in self.pins["entries"].items():
            for kind in ("rec", "dict"):
                ent = e[kind]
                self.assertTrue(ent.get("file"), f"{key}/{kind} 没有 file")
                if ent.get("level") in ("3", "4.5"):
                    self.assertTrue(ent.get("sha256"), f"{key}/{kind} 有等级却没哈希")
                    self.assertIsNotNone(ent.get("size"), f"{key}/{kind} 没体积")
                    self.assertTrue(ent.get("release"), f"{key}/{kind} 没记 release tag")
        self.assertTrue(self.pins["shared_det"].get("file"))
        self.assertTrue(self.pins["shared_det"].get("release"))

    def test_pairing_ignores_english_word_order(self) -> None:
        """rec 表写 'Chinese Traditional'，字典表写 'Traditional Chinese'。
        按人话标签配对会在这里断，所以 key 来自文件名。"""
        self.assertIn("chinese_cht", self.pins["entries"])
        self.assertEqual(self.pins["entries"]["chinese_cht"]["language"], "Chinese Traditional")
        self.assertEqual(self.pins["entries"]["chinese_cht"]["dict"]["file"],
                         "chinese_cht_dict.txt")

    def test_latest_version_per_language(self) -> None:
        self.assertEqual(self.pins["entries"]["arabic"]["version"], 5)
        self.assertEqual(self.pins["entries"]["en"]["version"], 5)
        # 只有 v3 的三个必须留在 v3，不能被"最新版"逻辑推没
        for k in ("chinese_cht", "japan", "ka"):
            self.assertEqual(self.pins["entries"][k]["version"], 3, k)

    def test_level_counts_add_up_to_rows(self) -> None:
        s = sum(self.pins["level_counts"].values())
        self.assertEqual(s, 2 * len(self.pins["entries"]))


class TestTagScoping(unittest.TestCase):
    """同名不同 tag 是不同文件：这条是整个工具的存在理由之一。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.md, cls.gh, cls.ms, cls.reg = sources()

    def test_latin_dict_has_two_different_files_under_two_tags(self) -> None:
        by_tag = self.gh["ppocrv5_latin_dict.txt"]
        self.assertEqual(sorted(by_tag), ["v0.1.0", "v0.3.0"])
        self.assertNotEqual(by_tag["v0.1.0"]["sha256"], by_tag["v0.3.0"]["sha256"])
        self.assertEqual(by_tag["v0.1.0"]["size"], 1634)
        self.assertEqual(by_tag["v0.3.0"]["size"], 2616)

    def test_wrong_tag_is_refused_not_pinned_wrong(self) -> None:
        """挑错 tag 不会静默钉成另一份文件：另外两家（ModelScope 与 registry.rs）
        都只认 v0.3.0 那份，所以 v0.1.0 会撞成"来源哈希不一致"而拒绝立 pin。"""
        row3 = {"file": "ppocrv5_latin_dict.txt", "tag": "v0.3.0", "url": ""}
        row1 = {"file": "ppocrv5_latin_dict.txt", "tag": "v0.1.0", "url": ""}
        p3, b3 = P.overlay(row3, self.gh, self.ms, self.reg)
        p1, b1 = P.overlay(row1, self.gh, self.ms, self.reg)
        self.assertEqual(b3, [])
        self.assertEqual(p3["size"], 2616)
        self.assertIsNone(p1)
        self.assertIn("哈希不一致", b1[0])

    def test_without_tag_a_ambiguous_name_is_refused(self) -> None:
        """没 tag 又跨 tag 重名时**不许挑一个**：宁可不立 pin。"""
        bad, problems = P.overlay({"file": "ppocrv5_latin_dict.txt", "tag": None, "url": ""},
                                  self.gh, self.ms, self.reg)
        self.assertIsNone(bad)
        self.assertTrue(any("没给出 tag" in x for x in problems), problems)

    def test_other_tags_are_recorded_on_the_row(self) -> None:
        p, _ = P.overlay({"file": "ppocrv5_latin_dict.txt", "tag": "v0.3.0", "url": ""},
                         self.gh, self.ms, self.reg)
        self.assertEqual(list(p["same_name_in_other_tags"]), ["v0.1.0"])


class TestOverlayRefusals(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.md, cls.gh, cls.ms, cls.reg = sources()
        cls.row = {"file": "arabic_pp-ocrv5_mobile_rec.onnx", "tag": "v0.3.0", "url": ""}

    def test_agreement_gives_45(self) -> None:
        pin, bad = P.overlay(self.row, self.gh, self.ms, self.reg)
        self.assertEqual(bad, [])
        self.assertEqual(pin["level"], "4.5")
        self.assertEqual(sorted(pin["sources"]), ["gh", "ms", "reg"])

    def test_conflicting_ms_hash_is_refused(self) -> None:
        ms = copy.deepcopy(self.ms)
        ms["arabic_pp-ocrv5_mobile_rec.onnx"]["sha256"] = "f" * 64
        pin, bad = P.overlay(self.row, self.gh, ms, self.reg)
        self.assertIsNone(pin)
        self.assertIn("哈希不一致", bad[0])

    def test_conflicting_size_is_refused(self) -> None:
        reg = copy.deepcopy(self.reg)
        reg["arabic_pp-ocrv5_mobile_rec.onnx"]["size"] += 1
        pin, bad = P.overlay(self.row, self.gh, self.ms, reg)
        self.assertIsNone(pin)
        self.assertIn("体积不一致", bad[0])

    def test_missing_everywhere_is_refused(self) -> None:
        pin, bad = P.overlay({"file": "nope_not_real.onnx", "tag": "v0.3.0", "url": ""},
                             self.gh, self.ms, self.reg)
        self.assertIsNone(pin)
        self.assertIn("models.md 指的 tag v0.3.0 里没有这个产物", bad[0])

    def test_empty_digest_field_is_refused(self) -> None:
        """上游哪天不发 digest，这里必须报"哈希为空"而不是立出一个 None 的 pin。"""
        gh = copy.deepcopy(self.gh)
        gh["arabic_pp-ocrv5_mobile_rec.onnx"]["v0.3.0"]["sha256"] = None
        ms, reg = {}, {}
        pin, bad = P.overlay(self.row, gh, ms, reg)
        self.assertIsNone(pin)
        self.assertIn("digest 字段是空的", bad[0])

    def test_single_source_only_falls_to_level_3(self) -> None:
        pin, _ = P.overlay(self.row, self.gh, {}, {})
        self.assertEqual(pin["level"], "3")


class TestCheckIsLoadBearing(unittest.TestCase):
    """每一条红都要能点名，且不许崩 —— 崩掉的判据下次没人敢信它测过什么。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.md, cls.gh, cls.ms, cls.reg = sources()
        cls.pins, cls.problems, cls.external = P.build(cls.md, cls.gh, cls.ms, cls.reg)
        # 外部字典在 --fixtures 下没有哈希，漂移检查按设计跳过它们（"由 commit 那条管"）
        assert cls.problems == [], cls.problems

    def test_clean_check_is_empty(self) -> None:
        self.assertEqual(P.check(self.pins, self.gh, self.ms, self.reg), [])

    def test_hash_drift_is_named(self) -> None:
        pins = copy.deepcopy(self.pins)
        e = pins["entries"]["en"]
        e["rec"]["sha256"] = "a" * 64
        bad = P.check(pins, self.gh, self.ms, self.reg)
        self.assertTrue(any("en/rec" in x and "哈希变了" in x for x in bad), bad)

    def test_wrong_release_tag_is_caught(self) -> None:
        """把 latin 的 release 改成 v0.1.0，钉的哈希就对不上了 ——
        这条正是第一版按名字找时踩的坑。"""
        pins = copy.deepcopy(self.pins)
        pins["entries"]["latin"]["dict"]["release"] = "v0.1.0"
        bad = P.check(pins, self.gh, self.ms, self.reg)
        self.assertTrue(any("latin/dict" in x for x in bad), bad)

    def test_asset_removed_upstream_is_caught(self) -> None:
        gh = copy.deepcopy(self.gh)
        del gh["th_pp-ocrv5_mobile_rec.onnx"]
        bad = P.check(self.pins, gh, self.ms, self.reg)
        self.assertTrue(any("th/rec" in x and "定不下来" in x for x in bad), bad)

    def test_level_change_must_be_confirmed(self) -> None:
        """少了一家分发方 = pin 的可信度下降，必须显式红，不许静默降档。"""
        ms = copy.deepcopy(self.ms)
        del ms["th_pp-ocrv5_mobile_rec.onnx"]
        bad = P.check(self.pins, self.gh, ms, self.reg)
        self.assertTrue(any("层级从 4.5 变成 3" in x for x in bad), bad)

    def test_missing_file_field_reports_instead_of_crashing(self) -> None:
        pins = copy.deepcopy(self.pins)
        del pins["shared_det"]["file"]
        bad = P.check(pins, self.gh, self.ms, self.reg)
        self.assertTrue(any("没有 file 字段" in x for x in bad), bad)


class TestGuardsOnSourceShape(unittest.TestCase):
    """输入形状变了要当场红，不要退化成"少查了几条"。"""

    def test_missing_section_raises(self) -> None:
        with self.assertRaises(SystemExit):
            P.section("# nothing\n\nbody\n", "### Language-Specific Models")

    def test_unsorted_registry_raises(self) -> None:
        rs = ('Entry { name: "b.onnx", sha256: "' + "1" * 64 + '", size: 1 }\n'
              'Entry { name: "a.onnx", sha256: "' + "2" * 64 + '", size: 2 }\n')
        with self.assertRaises(SystemExit) as cm:
            P.parse_registry(rs)
        self.assertIn("排序", str(cm.exception))

    def test_empty_registry_raises_not_returns_zero(self) -> None:
        """上游把 Entry 写法改了会解析出 0 条；0 条必须炸，不能"复核 0 条，问题 0 条"。"""
        with self.assertRaises(SystemExit):
            P.parse_registry("pub static REGISTRY: &[Entry] = &[];\n")

    def test_det_row_missing_raises(self) -> None:
        md, gh, ms, reg = sources()
        cut = md.replace("## Text Detection Models", "## Detection Models Renamed")
        with self.assertRaises(SystemExit):
            P.section(cut, P.SECTIONS["det"])


class TestCommittedPinsAgainstFixtures(unittest.TestCase):
    def test_pins_file_is_complete_and_matches_fixtures(self) -> None:
        """仓库里那张 pin 表必须能独立被夹具复算出来 —— 这是"pin 不是手抄"的证明。"""
        pins = json.load(open(PINS, encoding="utf-8"))
        md, gh, ms, reg = sources()
        bad = P.check(pins, gh, ms, reg)
        self.assertEqual(bad, [])
        self.assertEqual(pins["languages"], len(pins["entries"]))
        self.assertEqual(sum(e["rec"]["size"] + (e["dict"].get("size") or 0)
                             for e in pins["entries"].values()),
                         pins["rec_dict_bytes"])

    def test_fixtures_mode_refuses_to_write_an_incomplete_table(self) -> None:
        """离线跑不得覆盖好表：外部字典没自算哈希时必须 rc=1 且不落盘。"""
        out = os.path.join(HERE, "_should_not_exist.json")
        if os.path.exists(out):
            os.unlink(out)
        rc = P.main(["--fixtures", "--pins", PINS, "--pins-out", out])
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(out))


if __name__ == "__main__":
    unittest.main()
