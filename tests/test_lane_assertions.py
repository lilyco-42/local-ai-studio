"""把剩下的三段车道判据在本地"预跑"一遍（第四段见 test_image_proof_block.py）。

这三段都是"对上/对下全靠字符串判断"的断言，CI 又跑不了；
不证明它们有承重，就等于把一条可能永远绿（或永远假红）的检查留在车道里。
每段都配一条正向（合成输入必须过），否则那几条"必须红"没有信息量。

用 (job, 步骤序号) 定位段落，并顺手核对步骤名 —— 步骤一重排，序号就会指到别的判据上，
那种指错必须在这里红，而不是悄悄测了别的东西。
"""
import json
import os
import unittest

import lane_replay

WORD = "OARCI2291"


def files_of(d: dict) -> dict:
    return d


class TestOcrKnownWord(unittest.TestCase):
    JOB, IDX = "ocr-proof", 5

    def code(self) -> str:
        assert "认得出那个词" in lane_replay.step_name(self.JOB, self.IDX), \
            f"{self.JOB}#{self.IDX} 步骤名变了，判据指向可能错位"
        return lane_replay.block(self.JOB, self.IDX)

    def test_recognised_word_passes(self) -> None:
        """正向对照：带排版噪声的输出里有那个词，就必须过。"""
        out = f"## 第 1 页\n检测置信度: 0.98\n文本：【{WORD}】\n"
        rc, log = lane_replay.run(self.code(), files_of({"out.txt": out}), env={"WORD": WORD})
        self.assertEqual(rc, 0, log)

    def test_unrecognised_output_is_red(self) -> None:
        rc, log = lane_replay.run(self.code(), files_of({"out.txt": "hello world 123"}),
                                  env={"WORD": WORD})
        self.assertEqual(rc, 1, log)
        self.assertIn("没认出来", log)
        self.assertIn("tiny", log, "红的时候要告诉读的人这是模型质量底线，不是链路坏了")

    def test_empty_output_is_red_not_green(self) -> None:
        rc, _ = lane_replay.run(self.code(), files_of({"out.txt": ""}), env={"WORD": WORD})
        self.assertEqual(rc, 1)

    def test_normalisation_is_substring_by_design(self) -> None:
        """归一化后按子串比是刻意的（不假设上游怎么排版），钉住这个行为别被"顺手改严"。"""
        out = f"前缀{WORD}后缀"
        rc, log = lane_replay.run(self.code(), files_of({"out.txt": out}), env={"WORD": WORD})
        self.assertEqual(rc, 0, log)


class TestLlmfitJsonShape(unittest.TestCase):
    JOB, IDX = "toolbelt-pins", 4

    def code(self) -> str:
        assert "--json" in lane_replay.step_name(self.JOB, self.IDX), lane_replay.step_name(self.JOB, self.IDX)
        return lane_replay.block(self.JOB, self.IDX)

    def test_valid_json_passes_even_if_empty(self) -> None:
        """runner 上一个模型都没有，"查无结果"是合法输出：这条必须过，否则车道会因无关原因红。"""
        rc, log = lane_replay.run(self.code(),
                                  files_of({"rec.json": '{"models": []}', "rec.err": "", "help.txt": "recommend"})
                                  , env={})
        self.assertEqual(rc, 0, log)

    def test_non_json_stdout_is_red(self) -> None:
        rc, log = lane_replay.run(self.code(),
                                  files_of({"rec.json": "error: no such subcommand",
                                            "rec.err": "usage: llmfit ...", "help.txt": "recommend"}), env={})
        self.assertEqual(rc, 1, log)
        self.assertIn("不是 JSON", log)

    def test_truncated_json_is_red(self) -> None:
        rc, log = lane_replay.run(self.code(),
                                  files_of({"rec.json": '{"models": [', "rec.err": "", "help.txt": "x"}), env={})
        self.assertEqual(rc, 1, log)


class TestOcrPinDrift(unittest.TestCase):
    JOB, IDX = "ocr-catalog", 2

    def code(self) -> str:
        assert "digest" in lane_replay.step_name(self.JOB, self.IDX), lane_replay.step_name(self.JOB, self.IDX)
        return lane_replay.block(self.JOB, self.IDX)

    @staticmethod
    def pins(sha: str) -> str:
        return json.dumps({"pp-ocrv6_tiny_det.onnx": {"sha256": sha, "size": 1780590}},
                          ensure_ascii=False)

    def test_no_drift_passes(self) -> None:
        body = self.pins("a" * 64)
        rc, log = lane_replay.run(self.code(),
                                  files_of({"pins/ocr.json": body, "fresh-ocr.json": body}), env={})
        self.assertEqual(rc, 0, log)
        self.assertIn("漂移 0 条", log)

    def test_changed_digest_is_red_and_names_the_field(self) -> None:
        rc, log = lane_replay.run(self.code(),
                                  files_of({"pins/ocr.json": self.pins("a" * 64),
                                            "fresh-ocr.json": self.pins("b" * 64)}), env={})
        self.assertEqual(rc, 1, log)
        self.assertIn("pp-ocrv6_tiny_det.onnx", log)
        self.assertIn("sha256", log)

    def test_vanished_artifact_is_red(self) -> None:
        rc, log = lane_replay.run(self.code(),
                                  files_of({"pins/ocr.json": self.pins("a" * 64),
                                            "fresh-ocr.json": "{}"}), env={})
        self.assertEqual(rc, 1, log)
        self.assertIn("不再给", log)

    def test_new_default_is_reported_not_silently_accepted(self) -> None:
        rc, log = lane_replay.run(self.code(),
                                  files_of({"pins/ocr.json": "{}",
                                            "fresh-ocr.json": self.pins("a" * 64)}), env={})
        self.assertEqual(rc, 1, log)
        self.assertIn("多出一条", log)


class TestHarnessItself(unittest.TestCase):
    def test_unexpected_exception_is_not_reported_as_a_legit_red(self) -> None:
        """判据段自己抛异常（比如忘了造某个文件）要报 99，不能和"判据认为不合格"混成一回事。"""
        rc, log = lane_replay.run("import os\nos.chdir('/nonexistent-zz')\n", files_of({}), env={})
        self.assertEqual(rc, 99, log)
        self.assertIn("FileNotFoundError", log)

    def test_step_index_drift_is_caught(self) -> None:
        """(job, 序号) 指错时必须立刻报错，而不是静默去测另一段判据。"""
        with self.assertRaises(AssertionError):
            lane_replay.block("ocr-proof", 5, n=3)


if __name__ == "__main__":
    unittest.main()
