"""车道分段的**计数**必须是算出来的，不能只活在散文里。

起因：`docs/requirements.md` 那段"12 段 heredoc / 6 段断言段 / 5 段预跑 / 27 条用例"四个数字
都漂过，而且是一次改车道就全漂。漂的原因是它们从没被断言过 —— 这里把账立成断言，
改车道的人必须同时改这里的数，否则这条就红。
"""
from __future__ import annotations

import glob
import os
import re
import sys
import unittest

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

# `sys.exit("…")` 也是判据：车道里那段工具链版本闸就是靠它红的（只找 assert/SystemExit 会漏）
ASSERT_RX = re.compile(r"\bcheck\(|SystemExit|\bassert |sys\.exit\(")
BLOCK_RX = re.compile(r"<<'PY'[^\n]*\n(.*?)\nPY", re.S)
# 期望值不是估的：2026-10-20 按这两条正则数出来的（重数：本文件的 -v 输出那一行）
EXPECTED_TOTAL = 17
EXPECTED_ASSERT = 12
EXPECTED_REPLAYED = {
    ("headless-inference", "llama-server"),
    ("image-proof", "常驻 sd-server"),
    ("image-proof", "生成一张小图"),
    ("ocr-catalog", "digest"),
    ("ocr-proof", "编译 CLI"),
    ("ocr-proof", "认得出那个词"),
    ("subtitle-proof", "常驻 whisper-server"),
    ("toolbelt-pins", "--json"),
}
REPLAY_FILES = ("tests/test_lane_assertions.py", "tests/test_image_proof_block.py")
# 每张 workflow 的 job 数（2026-10-20 实测；docs/toolbelt.md 那句"其余 21 条 job 一次都没跑过"
# 就靠这个数与它绑在一起）：加一条 job 必须同时改文档里那句
EXPECTED_JOBS = {"engine-live.yml": 22, "ci.yml": 1, "package.yml": 1, "shell.yml": 2}


def blocks():
    """三条 workflow 里的每一段 `<<'PY' … PY`（与 yaml_gate / lane_replay 同一套抽取规则）。"""
    for path in sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))):
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        for job, spec in (doc.get("jobs") or {}).items():
            for step in spec.get("steps") or []:
                for i, code in enumerate(BLOCK_RX.findall(step.get("run") or "")):
                    yield os.path.basename(path), job, step.get("name") or "", i, code


def replayed_pairs():
    """被本地预跑引用到的 (job, 步骤名片段) —— 从测试源码里读，不靠人数。"""
    out = set()
    for rel in REPLAY_FILES:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            src = fh.read()
        for m in re.finditer(r'JOB, NEEDLE = "([^"]+)", "([^"]+)"', src):
            out.add((m.group(1), m.group(2)))
        for m in re.finditer(r'(?:SUB|IMG) = \("([^"]+)", "([^"]+)"', src):
            out.add((m.group(1), m.group(2)))
    return out


class TestCounts(unittest.TestCase):
    def test_heredoc_block_census(self) -> None:
        all_blocks = list(blocks())
        n_assert = sum(1 for *_x, code in all_blocks if ASSERT_RX.search(code))
        self.assertEqual(len(all_blocks), EXPECTED_TOTAL,
                         "车道里的 heredoc 段数变了，文档那段计数要一起改")
        self.assertEqual(n_assert, EXPECTED_ASSERT,
                         "断言段数量变了：新加的段是不含判据的取数段，还是判据漏了标记？")
        self.assertLess(n_assert, len(all_blocks),
                        "全成了断言段不可能：取数/打印段本来就不含判据")
        print(f"车道 heredoc {len(all_blocks)} 段（断言 {n_assert} / 取数打印 "
              f"{len(all_blocks) - n_assert}）；本地预跑引用 {len(replayed_pairs())} 个 (job, 步骤名) 组合")

    def test_job_counts_match_the_documented_numbers(self) -> None:
        got = {}
        for path in sorted(glob.glob(os.path.join(ROOT, ".github", "workflows", "*.yml"))):
            with open(path, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
            got[os.path.basename(path)] = len(doc.get("jobs") or {})
        self.assertEqual(got, EXPECTED_JOBS,
                         "job 数变了：docs/toolbelt.md 那句「其余 N 条一次都没跑过」要一起改")

    def test_replayed_set_is_the_documented_one(self) -> None:
        got = replayed_pairs()
        self.assertEqual(got, EXPECTED_REPLAYED,
                         f"本地预跑覆盖的片段变了：多 {sorted(got - EXPECTED_REPLAYED)} / "
                         f"少 {sorted(EXPECTED_REPLAYED - got)}")

    def test_every_replayed_pair_resolves_in_engine_live(self) -> None:
        """引用的 (job, 步骤名) 必须真能在车道里定位到唯一步骤 —— 否则预跑指错了判据。"""
        import lane_replay
        with open(lane_replay.WF, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        jobs = doc["jobs"]
        for job, needle in sorted(EXPECTED_REPLAYED):
            self.assertIn(job, jobs, f"{job} 这个 job 已经不在了")
            hits = [i for i, s in enumerate(jobs[job]["steps"]) if needle in (s.get("name") or "")]
            self.assertEqual(len(hits), 1, f"{job} 里 {needle!r} 命中 {hits}：判据指不准")


if __name__ == "__main__":
    unittest.main()
