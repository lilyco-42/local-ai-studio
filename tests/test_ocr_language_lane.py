"""ocr-languages 那条车道的两段判据（离线回放）。

车道本身跑不起来（private 拿不到 runner），而这两段是"上游改了我们就得知道"的唯一守门人。
所以它们必须被证明：好输入过、坏输入红在对应那一句、并且**每天必然不同的那两个字段
不会把它染红**（pinned_commit —— PaddleOCR 的 main 一直在动）。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import lane_replay  # noqa: E402

JOB = "ocr-languages"
DIFF_NEEDLE = "重新立一张表"
CONTROL_NEEDLE = "正向对照"
PINS_REL = os.path.join("pins", "ocr-languages.json")


def committed() -> dict:
    with open(os.path.join(ROOT, PINS_REL), encoding="utf-8") as fh:
        return json.load(fh)


def diff_block() -> str:
    return lane_replay.block(JOB, lane_replay.find_index(JOB, DIFF_NEEDLE))


def control_block() -> str:
    return lane_replay.block(JOB, lane_replay.find_index(JOB, CONTROL_NEEDLE))


def run_diff(pins: dict, fresh: dict):
    return lane_replay.run(diff_block(), {
        PINS_REL.replace("\\", "/"): json.dumps(pins, ensure_ascii=False),
        "fresh-ocr-languages.json": json.dumps(fresh, ensure_ascii=False)})


class TestFieldDiffStep(unittest.TestCase):
    def test_identical_table_passes(self) -> None:
        """正向对照：一模一样必须 0 退出，否则后面三条的红没有信息量。"""
        pins = committed()
        rc, log = run_diff(pins, json.loads(json.dumps(pins)))
        self.assertEqual(rc, 0, log)
        self.assertIn("漂移 0 条", log)

    def test_changed_hash_is_named(self) -> None:
        pins, fresh = committed(), json.loads(json.dumps(committed()))
        fresh["entries"]["en"]["rec"]["sha256"] = "b" * 64
        rc, log = run_diff(pins, fresh)
        self.assertEqual(rc, 1, log)
        self.assertIn(".entries.en.rec.sha256", log)

    def test_language_disappearing_is_named(self) -> None:
        pins, fresh = committed(), json.loads(json.dumps(committed()))
        del fresh["entries"]["th"]
        rc, log = run_diff(pins, fresh)
        self.assertEqual(rc, 1, log)
        self.assertIn("上游现在没有", log)

    def test_volatile_commit_alone_does_not_redden(self) -> None:
        """PaddleOCR 的 main 每天在动。如果这两个字段参与比对，
        这条车道会天天红，然后所有人把它当噪声 —— 那它就等于不存在。"""
        pins = committed()
        fresh = json.loads(json.dumps(pins))
        moved = 0
        for e in fresh["entries"].values():
            if e["dict"].get("pinned_commit"):
                e["dict"]["pinned_commit"] = "0" * 40
                e["dict"]["resolved_url"] = "https://raw.githubusercontent.com/x/y/0000/zz.txt"
                moved += 1
        self.assertGreaterEqual(moved, 1, "夹具里没有外部字典，这条就没在测它该测的东西")
        rc, log = run_diff(pins, fresh)
        self.assertEqual(rc, 0, log)

    def test_external_dict_content_change_is_still_caught(self) -> None:
        """但忽略 commit 不等于忽略内容：字典换字节必须红。"""
        pins, fresh = committed(), json.loads(json.dumps(committed()))
        fresh["entries"]["japan"]["dict"]["sha256"] = "c" * 64
        rc, log = run_diff(pins, fresh)
        self.assertEqual(rc, 1, log)
        self.assertIn("japan.dict.sha256", log)


class TestPositiveControlStep(unittest.TestCase):
    def run_control(self, pins: dict):
        return lane_replay.run(control_block(), {
            PINS_REL.replace("\\", "/"): json.dumps(pins, ensure_ascii=False)})

    def test_shipped_table_passes(self) -> None:
        rc, log = self.run_control(committed())
        self.assertEqual(rc, 0, log)

    def test_aggregate_that_is_not_a_sum_is_red(self) -> None:
        pins = json.loads(json.dumps(committed()))
        pins["rec_dict_bytes"] += 1
        rc, log = self.run_control(pins)
        self.assertEqual(rc, 1, log)
        self.assertIn("逐条相加不符", log)

    def test_losing_the_second_distributor_is_red(self) -> None:
        """4.5 级掉下来说明 ModelScope 镜像不再同步 —— 那时语言包不能按这张表发货。"""
        pins = json.loads(json.dumps(committed()))
        pins["level_counts"] = {"3": 25, "自算 + 钉 commit": 3}
        rc, log = self.run_control(pins)
        self.assertEqual(rc, 1, log)
        self.assertIn("ModelScope", log)

    def test_language_count_change_is_red(self) -> None:
        pins = json.loads(json.dumps(committed()))
        pins["languages"] = 13
        rc, log = self.run_control(pins)
        self.assertEqual(rc, 1, log)
        self.assertIn("14", log)


if __name__ == "__main__":
    unittest.main()
