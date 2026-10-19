"""`augment_json_pins` / `shape_guard` 的测试 —— 给 pins/ocr.json 补两家元数据这一步。

为什么单独盯"只许追加"：`pins/ocr.json` 是 OCR 车道的输入。一次"顺手重写"把
`bytes_verified` 或 `provenance` 弄丢，车道不会红，只会从此不再复核那件事 ——
这正是本轮反复踩的那类"判据指向一个不存在的东西却照样跑完"。
"""
import copy
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import ocr_language_pins as P  # noqa: E402

OCR_PINS = os.path.join(ROOT, "pins", "ocr.json")


def sources():
    return P.load_fixtures()


class TestCheckMode(unittest.TestCase):
    def test_the_three_default_rows_are_two_source(self) -> None:
        """正向对照：这张表三条都该是 gh+ms+reg 一致（4.5 级）。
        0 条对上不叫通过，叫没查。"""
        md, gh, ms, reg = sources()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ocr.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(json.load(open(OCR_PINS, encoding="utf-8")), fh)
            rows, problems = P.augment_json_pins(p, gh, ms, reg, write=False)
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 3, rows)
        self.assertEqual({r["level"] for r in rows}, {"4.5"})
        for r in rows:
            self.assertEqual(sorted(r["sources"]), ["gh", "ms", "reg"])

    def test_source_conflict_is_a_problem_not_a_silent_skip(self) -> None:
        """只有一家变了（另一家还是老哈希）时必须红，且红在"来源互相不一致"。"""
        md, gh, ms, reg = sources()
        gh = copy.deepcopy(gh)
        name = "pp-ocrv6_tiny_det.onnx"
        gh[name][list(gh[name])[0]]["sha256"] = "d" * 64
        rows, problems = P.augment_json_pins(OCR_PINS, gh, ms, reg, write=False)
        self.assertTrue(any("在场来源哈希不一致" in x for x in problems), problems)
        self.assertNotIn(name, [r["file"] for r in rows])
        self.assertEqual(len(rows), 2, "漂了那条要被剔除，另两条该照常对上")

    def test_unanimous_change_against_the_pin_is_named(self) -> None:
        """三家一起变成新值 = 上游重发过一次，这时要说的不是"两家打架"，
        而是"我们钉的这颗已经不是上游给的那颗了"。"""
        md, gh, ms, reg = sources()
        gh, ms, reg = copy.deepcopy(gh), copy.deepcopy(ms), copy.deepcopy(reg)
        name = "pp-ocrv6_tiny_det.onnx"
        for tag in gh[name]:
            gh[name][tag]["sha256"] = "e" * 64
        ms[name]["sha256"] = "e" * 64
        reg[name]["sha256"] = "e" * 64
        rows, problems = P.augment_json_pins(OCR_PINS, gh, ms, reg, write=False)
        self.assertTrue(any("先查上游换没换文件" in x for x in problems), problems)
        self.assertNotIn(name, [r["file"] for r in rows])

    def test_unresolvable_row_is_reported(self) -> None:
        """三家都查不到这条时不能"少查一条也算过"。"""
        md, gh, ms, reg = sources()
        doc = json.load(open(OCR_PINS, encoding="utf-8"))
        doc["not_a_real_file.onnx"] = {"sha256": "0" * 64, "size": 1,
                                       "primary": "https://github.com/GreatV/oar-ocr/"
                                                  "releases/download/v9.9.9/not_a_real_file.onnx"}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            rows, problems = P.augment_json_pins(p, gh, ms, reg, write=False)
        self.assertTrue(any("not_a_real_file" in x for x in problems), problems)
        self.assertEqual(len(rows), 3)


class TestAdditiveWrite(unittest.TestCase):
    def write(self, doc):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = os.path.join(d.name, "ocr.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        md, gh, ms, reg = sources()
        rows, problems = P.augment_json_pins(p, gh, ms, reg, write=True)
        with open(p, encoding="utf-8") as fh:
            return p, rows, problems, json.load(fh)

    def test_writes_only_the_new_key(self) -> None:
        """先把仓库里那张表已有 meta_hashes 拿掉，测的才是"从无到有那一次追加"。"""
        before = json.load(open(OCR_PINS, encoding="utf-8"))
        for v in before.values():
            v.pop("meta_hashes", None)
        p, rows, problems, after = self.write(copy.deepcopy(before))
        self.assertEqual(problems, [])
        self.assertEqual(len(rows), 3)
        self.assertEqual(set(before), set(after))
        for k, v in before.items():
            self.assertEqual(set(after[k]) - set(v), {"meta_hashes"})
            for kk, vv in v.items():
                self.assertEqual(after[k][kk], vv, f"{k}.{kk} 被改了")
            self.assertEqual(after[k]["meta_hashes"]["level"], "4.5")
            self.assertEqual(after[k]["meta_hashes"]["as_of"], "2026-10-20")

    def test_existing_file_on_disk_is_already_augmented(self) -> None:
        """仓库里那张表必须已经带 meta_hashes —— 否则文档涨级就是空头承诺。"""
        doc = json.load(open(OCR_PINS, encoding="utf-8"))
        self.assertEqual(len(doc), 3)
        for k, v in doc.items():
            self.assertIn("meta_hashes", v, f"{k} 没有 meta_hashes）".rstrip("）"))
            self.assertEqual(v["meta_hashes"]["sha256"], v["sha256"])

    def test_shape_guard_refuses_a_changed_value(self) -> None:
        old = {"a": {"sha256": "x", "size": 1}}
        new = {"a": {"sha256": "y", "size": 1}}
        bad = P.shape_guard(old, new)
        self.assertTrue(any("改了既有键" in x for x in bad), bad)

    def test_shape_guard_refuses_a_dropped_key(self) -> None:
        """丢键必须红。少了 bytes_verified 之后 OCR 车道照样绿 —— 那是判据失踪，不是通过。"""
        old = {"a": {"sha256": "x", "bytes_verified": {"sha256": "x"}}}
        new = {"a": {"sha256": "x"}}
        bad = P.shape_guard(old, new)
        self.assertTrue(any("丢了键" in x for x in bad), bad)

    def test_shape_guard_refuses_a_dropped_row(self) -> None:
        old = {"a": {"sha256": "x"}, "b": {"sha256": "y"}}
        bad = P.shape_guard(old, {"a": {"sha256": "x"}})
        self.assertTrue(any("顶层键集合变了" in x for x in bad), bad)

    def test_guard_failure_leaves_the_file_untouched(self) -> None:
        """拒写必须真的没写：半截写会把好表毁掉。"""
        good = json.load(open(OCR_PINS, encoding="utf-8"))
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ocr.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump({"extra_row": {"sha256": "0" * 64, "size": 1,
                                         "primary": "https://github.com/GreatV/oar-ocr/"
                                                    "releases/download/v0.7.0/extra_row"}}, fh)
            original = open(p, encoding="utf-8").read()
            md, gh, ms, reg = sources()
            rows, problems = P.augment_json_pins(p, gh, ms, reg, write=True)
            after = open(p, encoding="utf-8").read()
        self.assertEqual(after, original, "拒写却动了文件")
        self.assertEqual(rows, [])
        self.assertTrue(any("extra_row" in x for x in problems), problems)
        self.assertTrue(good)


class TestRealFileIsIdempotent(unittest.TestCase):
    def test_re_running_changes_nothing(self) -> None:
        """已经补过的表再补一次必须逐字节相同 —— 不然"重跑一遍就漂"迟早坑到人。
        仓库里那张表本轮已经补过，所以这里直接拿它当输入。"""
        with open(OCR_PINS, "rb") as fh:
            original = fh.read()
        md, gh, ms, reg = sources()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ocr.json")
            for _ in range(2):
                with open(p, "wb") as fh:
                    fh.write(original)
                rows, problems = P.augment_json_pins(p, gh, ms, reg, write=True)
                self.assertEqual(problems, [], p)
                self.assertEqual(len(rows), 3)
                with open(p, "rb") as fh:
                    self.assertEqual(fh.read(), original, "重跑一次就动了字节")


if __name__ == "__main__":
    unittest.main()
