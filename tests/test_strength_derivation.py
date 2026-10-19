"""`strength_of()` / `defaults_check()` —— 出厂默认档上那条"强度级"必须是从 pin 推出来的。

requirements 第 7 条早就写"defaults.json 会把这条 pin 的强度级一起带出去，UI 才有东西可显示"，
而那张表当时只有 `provenance` 一句话与整块 `bytes_verified`，**没有一个能显示的级数**：
写文档的时候顺手把承诺当现状，是这一整轮反复踩的那类坑。
现在级数由字段推导，推导本身当场纠正了我上一轮的两句过誉 ——
把 OCR 默认档写成"5 + 4.5"（它的整包字节来自同一家，按阶梯到不了第 5 级），
以及把对话/生图写成"5 + 4.5"（记进 pin 的第二家证据只有真字节那一条，
`meta_hashes.provider_count` 是 1，两家元数据各给哈希那件事对它们并没有被记下来）。
"""
import copy
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import catalog_snapshot as C  # noqa: E402

PIN = "a" * 64


def pin(**extra):
    base = {"sha256": PIN, "size": 100, "primary": "https://x/y"}
    base.update(extra)
    return base


def rungs(name="k", **extra):
    st, bad = C.strength_of(name, pin(**extra))
    assert not bad, bad
    return st["rungs"]


class TestDerivation(unittest.TestCase):
    def test_nothing_but_a_hash_is_not_a_level(self) -> None:
        """光有 sha256 什么背书都没有 → 推不出现实级数，必须报问题而不是给个好看的数字。"""
        st, bad = C.strength_of("k", pin())
        self.assertTrue(bad and "推不出来" in bad[0], bad)
        self.assertEqual(st, {})

    def test_missing_or_short_hash_is_refused(self) -> None:
        for p in ({"sha256": "abc"}, {"sha256": None}, {}):
            with self.subTest(p=str(p)[:24]):
                _st, bad = C.strength_of("k", p)
                self.assertTrue(any("sha256" in x for x in bad), bad)

    def test_upstream_oid_is_level_3(self) -> None:
        self.assertEqual(rungs(meta_hashes={"oid": PIN, "provider_count": 1,
                                            "hosts": ["hf-mirror.com", "huggingface.co"]}),
                         ["3"])

    def test_two_distributor_hashes_is_45(self) -> None:
        self.assertEqual(rungs(meta_hashes={"sources": ["gh", "ms", "reg"]}), ["3", "4.5"])

    def test_same_provider_bytes_do_not_reach_5(self) -> None:
        """这正是本轮改掉两句文档措辞的那条：同家整包真字节只是附加证据。"""
        r = rungs(meta_hashes={"sources": ["gh", "ms"]},
                  bytes_verified={"sha256": PIN, "bytes": 100,
                                  "level": "同提供方真字节（只证明 digest 是内容哈希）"})
        self.assertEqual(r, ["3", "4.5"])

    def test_cross_provider_bytes_reach_5(self) -> None:
        self.assertEqual(rungs(meta_hashes={"oid": PIN, "provider_count": 1},
                               bytes_verified={"sha256": PIN, "bytes": 100,
                                               "level": "跨提供方 + 真字节（一家给哈希，另一家给字节）"}),
                         ["3", "5"])

    def test_bytes_that_disagree_with_the_pin_is_a_problem(self) -> None:
        _st, bad = C.strength_of("k", pin(bytes_verified={"sha256": "b" * 64, "bytes": 100,
                                                           "level": "跨提供方 + 真字节"}))
        self.assertTrue(any("与钉值不等" in x for x in bad), bad)

    def test_unreadable_meta_block_is_named_not_skipped(self) -> None:
        """meta_hashes 在、但既不是两家一致也不等于钉值 —— 不能当它不存在。"""
        _st, bad = C.strength_of("k", pin(meta_hashes={"oid": "c" * 64}))
        self.assertTrue(any("要重看" in x for x in bad), bad)

    def test_git_blob_is_evidence_not_a_rung(self) -> None:
        st, _ = C.strength_of("k", pin(git_blob_sha1="3184d372",
                                       meta_hashes={"oid": PIN, "provider_count": 1}))
        self.assertEqual(st["rungs"], ["3"])
        self.assertTrue(any("git blob" in x for x in st["evidence"]))

    def test_unknown_provider_wording_is_still_reported_as_3_only(self) -> None:
        """bv.level 换了措辞时不许悄悄升到 5：只有写着"跨提供方"才算跨家。"""
        st, bad = C.strength_of("k", pin(
            meta_hashes={"sources": ["gh"]},
            bytes_verified={"sha256": PIN, "bytes": 100, "level": "整包算过了"}))
        self.assertEqual(bad, [])
        self.assertEqual(st["rungs"], ["3"])
        self.assertTrue(any("不够第 5 级" in x for x in st["evidence"]))


class TestShippedRows(unittest.TestCase):
    """仓库里那六条默认档的级数（把我上一轮写过头的两句钉在这里）。"""

    def setUp(self) -> None:
        self.pins = C.picks_of(C.load_pin_layers())

    def levels(self, name):
        st, bad = C.strength_of(name, self.pins[name])
        self.assertEqual(bad, [])
        return st["rungs"]

    def test_ocr_default_trio_is_3_plus_45_not_5(self) -> None:
        for k in ("pp-ocrv6_tiny_det.onnx", "pp-ocrv6_tiny_rec.onnx", "ppocrv6_tiny_dict.txt"):
            self.assertEqual(self.levels(k), ["3", "4.5"], k)

    def test_cross_provider_weights_are_3_plus_5(self) -> None:
        for k in ("qwen2.5-0.5b-instruct-q2_k.gguf",
                  "stable-diffusion-v1-5-pruned-emaonly-Q4_0.gguf"):
            self.assertEqual(self.levels(k), ["3", "5"], k)

    def test_whisper_weight_is_only_3(self) -> None:
        """ModelScope 没有 ggerganov/whisper.cpp 这个仓库 → 到不了 4.5，也到不了 5。"""
        self.assertEqual(self.levels("ggml-tiny.bin"), ["3"])

    def test_lane_sample_is_kept_out_of_the_default_table(self) -> None:
        self.assertNotIn("samples/jfk.wav", self.pins)


class TestDefaultsCheck(unittest.TestCase):
    def test_shipped_table_passes(self) -> None:
        """正向对照：仓库里这张 defaults.json 必须自洽，否则后面几条红得没意义。"""
        self.assertEqual(C.defaults_check(), 0)

    def mutate(self, fn):
        path = os.path.join(ROOT, "catalog", "defaults.json")
        with open(path, encoding="utf-8") as fh:
            original = fh.read()
        doc = json.loads(original)
        fn(doc)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=1, sort_keys=True)
        try:
            rc = C.defaults_check()
        finally:
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(original)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), json.loads(original), "还原没回到原样")
        return rc

    def test_hand_writtten_level_5_is_red(self) -> None:
        """有人在表上把 OCR 那条改成"5"（就像我上一轮在文档里干的那样）→ 当场红。"""
        def up(doc):
            doc["entries"]["pp-ocrv6_tiny_det.onnx"]["strength"]["rungs"] = ["3", "4.5", "5"]
        self.assertEqual(self.mutate(up), 1)

    def test_evidence_edited_is_red(self) -> None:
        def up(doc):
            doc["entries"]["qwen2.5-0.5b-instruct-q2_k.gguf"]["strength"]["evidence"] = ["好看的话"]
        self.assertEqual(self.mutate(up), 1)

    def test_row_added_by_hand_is_red(self) -> None:
        def up(doc):
            doc["entries"]["someone_elses_model.gguf"] = {"sha256": "f" * 64, "size": 1}
        self.assertEqual(self.mutate(up), 1)

    def test_row_dropped_is_red(self) -> None:
        def up(doc):
            del doc["entries"]["ppocrv6_tiny_dict.txt"]
        self.assertEqual(self.mutate(up), 1)


if __name__ == "__main__":
    unittest.main()
