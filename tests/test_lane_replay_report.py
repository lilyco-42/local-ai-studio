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
EXPECTED_TOTAL = 18
EXPECTED_ASSERT = 13
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
EXPECTED_JOBS = {"engine-live.yml": 22, "ci.yml": 1, "package.yml": 1, "shell.yml": 2,
                 "installer.yml": 1, "upstream-follow.yml": 1}


def blocks():
    """所有 workflow 里的每一段 `<<'PY' … PY`（与 yaml_gate / lane_replay 同一套抽取规则）。"""
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


REQUIREMENTS = os.path.join(ROOT, "docs", "requirements.md")
MODULE_RX = re.compile(r"`(test_[a-z_0-9]+\.py)`")
PAIR_RX = re.compile(
    r"`(test_[a-z_0-9]+\.py)`\s*\+\s*`(test_[a-z_0-9]+\.py)`(.*?)(?=`test_[a-z_0-9]+\.py`|$)",
    re.S)


def doc_test_counts(text: str) -> tuple[dict[str, int], int, list[str]]:
    """把验收标准第 1 条那段逐文件清单解析成 {模块: 用例数}、散文总数与解析问题。

    清单会折行，所以不能按行切；用"出现过的模块名"当分隔符。两个模块合写成
    `A` + `B` 并给 `a + b = c` 的那种条目按顺序取前两个数，并要求加法自洽。
    """
    problems: list[str] = []
    start = text.index("实测逐文件：")
    end = text.index("`yaml_gate.py` 0 BAD", start)
    block = text[start:end]
    m = re.search(r"(\d+) 个单测绿", text)
    if not m:
        return {}, -1, ["找不到「N 个单测绿」总数"]
    total = int(m.group(1))
    counts: dict[str, int] = {}
    cleaned = block
    for pair in PAIR_RX.finditer(block):
        add = re.search(r"(\d+)\s*\+\s*(\d+)\s*=\s*(\d+)", pair.group(3))
        if not add:
            problems.append(f"{pair.group(1)} + {pair.group(2)} 合写但没有 a + b = c")
            continue
        a, b, c = (int(x) for x in add.groups())
        if a + b != c:
            problems.append(f"{pair.group(1)} + {pair.group(2)} 写成 {a} + {b} = {c}，加法不自洽")
            continue
        for name, value in ((pair.group(1), a), (pair.group(2), b)):
            if name in counts:
                problems.append(f"{name} 在清单里出现两次")
            counts[name] = value
        cleaned = cleaned.replace(pair.group(0), "")
    for seg in re.split(r"(?=`test_[a-z_0-9]+\.py`)", cleaned):
        names = MODULE_RX.findall(seg)
        if not names:
            continue
        if len(names) != 1:
            problems.append(f"清单片段里出现多个模块名 {names}")
            continue
        nums = re.findall(r"(\d+)\s*[、）]", seg)
        if not nums:
            problems.append(f"{names[0]} 后面没写用例数")
            continue
        if names[0] in counts:
            problems.append(f"{names[0]} 在清单里出现两次")
            continue
        counts[names[0]] = int(nums[-1])
    return counts, total, problems


_ACTUAL: dict[str, int] | None = None


def actual_test_counts() -> dict[str, int]:
    """按模块 import 后数一遍（与 `python -m unittest tests.<模块>` 同一个口径）。"""
    global _ACTUAL
    if _ACTUAL is None:
        loader = unittest.TestLoader()
        _ACTUAL = {}
        for path in sorted(glob.glob(os.path.join(HERE, "test_*.py"))):
            name = os.path.basename(path)
            suite = loader.discover(HERE, pattern=name, top_level_dir=HERE)
            _ACTUAL[name] = suite.countTestCases()
    return dict(_ACTUAL)


def count_findings(counts: dict[str, int], total: int, actual: dict[str, int]) -> list[str]:
    out = []
    for name in sorted(set(actual) - set(counts)):
        out.append(f"{name} 在磁盘上但清单没列")
    for name in sorted(set(counts) - set(actual)):
        out.append(f"{name} 在清单里但磁盘没有这个模块")
    for name in sorted(set(actual) & set(counts)):
        if actual[name] != counts[name]:
            out.append(f"{name}: 清单写 {counts[name]}，实测 {actual[name]}")
    if sum(counts.values()) != total:
        out.append(f"清单逐文件和 {sum(counts.values())} ≠ 散文总数 {total}")
    if sum(actual.values()) != total:
        out.append(f"实测总数 {sum(actual.values())} ≠ 散文总数 {total}")
    return out


class TestRequirementsTestCounts(unittest.TestCase):
    def test_requirements_per_file_counts_match_the_suite(self) -> None:
        with open(REQUIREMENTS, encoding="utf-8") as fh:
            text = fh.read()
        counts, total, problems = doc_test_counts(text)
        self.assertEqual(problems, [])
        self.assertEqual(count_findings(counts, total, actual_test_counts()), [])

    def test_count_mutations_are_caught(self) -> None:
        with open(REQUIREMENTS, encoding="utf-8") as fh:
            counts, total, problems = doc_test_counts(fh.read())
        self.assertEqual(problems, [])
        actual = actual_test_counts()
        victim = "test_png_proof.py"
        cases = {
            "某模块少写一条": ({**counts, victim: counts[victim] - 1}, total),
            "磁盘模块没列": ({k: v for k, v in counts.items() if k != "test_srt_proof.py"}, total),
            "清单有幽灵模块": ({**counts, "test_ghost.py": 1}, total),
            "散文总数错": (counts, total + 1),
        }
        for label, (c, t) in cases.items():
            with self.subTest(label=label):
                self.assertTrue(count_findings(c, t, actual), label)
        # 合写那条自己不自洽也必须红（避免只对"列出名字"交差）
        bad_doc = ("1. `ci.yml`：3 个单测绿（实测逐文件：\n"
                   "`test_a.py` 1、`test_b.py` + `test_c.py` 2 + 1 = 4、\n"
                   "`yaml_gate.py` 0 BAD")
        _, _, bad = doc_test_counts(bad_doc)
        self.assertTrue(any("不自洽" in p for p in bad), bad)


# "车道 X … N 条单测"散落在 README 与两份文档里，和验收段那份账是两套写法。
# 锚点必须在该文件里只出现一次，N 必须在锚点后 90 字内 —— 窗口取宽了会捞到隔壁车道的数，
# 把"数字被删"误判成通过；找不到算解析失败，不是跳过。
DOC_CLAIMS = (
    ("README.md", "`subtitle-tiers`：`--selftest`（含明知是错的必须认成错）→",
     "test_recommend_subtitle.py"),
    ("README.md", "`machine-budget` 三步全离线（输入是那五张已提交的表 + `pins/ocr.json` 的源地址 + 吞吐表）",
     "test_machine_budget.py"),
    ("README.md", "`launch-plan` 前三步离线（判据自测 /", "test_launch_plan.py"),
    ("README.md", "14 条源码变异体 × 24 条探针、", "test_shell_contract.py"),
    ("docs/model-catalog.md", "`machine-budget` 三步**全离线**", "test_machine_budget.py"),
    ("docs/requirements.md", "那边一漂这边就跟着红）：`--selftest` →", "test_machine_budget.py"),
    ("docs/requirements.md", "whisper 的多段字段被改名……），", "test_shell_contract.py"),
)
CLAIM_RX = re.compile(r"(\d+) 条单测")
CLAIM_WINDOW = 90


def doc_claim_findings(text: str, anchor: str, module: str,
                       actual: dict[str, int], window: int = CLAIM_WINDOW) -> list[str]:
    flat = re.sub(r"\s+", " ", text)
    hits = [m.start() for m in re.finditer(re.escape(anchor), flat)]
    if len(hits) != 1:
        return [f"锚点 {anchor!r} 命中 {len(hits)} 次（要正好 1 次）"]
    m = CLAIM_RX.search(flat, hits[0] + len(anchor), hits[0] + len(anchor) + window)
    if not m:
        return [f"锚点 {anchor!r} 后面 {window} 字内没有「N 条单测」"]
    if module not in actual:
        return [f"散文点名的 {module} 不在磁盘上"]
    if int(m.group(1)) != actual[module]:
        return [f"锚点 {anchor!r} 写 {m.group(1)} 条单测，{module} 实测 {actual[module]} 条"]
    return []


class TestLaneProseTestCounts(unittest.TestCase):
    def test_every_lane_prose_count_matches_its_module(self) -> None:
        actual = actual_test_counts()
        cache: dict[str, str] = {}
        out = []
        for rel, anchor, module in DOC_CLAIMS:
            if rel not in cache:
                with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                    cache[rel] = fh.read()
            out += doc_claim_findings(cache[rel], anchor, module, actual)
        self.assertEqual(out, [])

    def test_claim_mutations_are_caught(self) -> None:
        actual = actual_test_counts()
        rel, anchor, module = DOC_CLAIMS[0]
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            flat = re.sub(r"\s+", " ", fh.read())
        at = flat.index(anchor)
        end = at + len(anchor)
        m = CLAIM_RX.search(flat, end, end + CLAIM_WINDOW)
        self.assertIsNotNone(m, f"{rel} 里连基线都解析不出来，变异用例没意义")
        wrong = flat[:m.start()] + str(int(m.group(1)) + 1) + flat[m.end():]
        dropped = flat[:m.start()] + flat[m.end():]
        cases = {
            "数字多一条": doc_claim_findings(wrong, anchor, module, actual),
            "数字短语被删": doc_claim_findings(dropped, anchor, module, actual),
            "锚点被删": doc_claim_findings(flat.replace(anchor, ""), anchor, module, actual),
            "锚点重复": doc_claim_findings(flat + " " + anchor, anchor, module, actual),
            "模块不存在": doc_claim_findings(flat, anchor, "test_ghost.py", actual),
        }
        for label, found in cases.items():
            with self.subTest(label=label):
                self.assertTrue(found, label)
        # 每条声明都要单独过一遍"数字被删"：窗口若宽到能捞到隔壁车道的数，这里就会漏
        for rel2, anchor2, module2 in DOC_CLAIMS:
            with open(os.path.join(ROOT, rel2), encoding="utf-8") as fh:
                flat2 = re.sub(r"\s+", " ", fh.read())
            at2 = flat2.index(anchor2)
            end2 = at2 + len(anchor2)
            m2 = CLAIM_RX.search(flat2, end2, end2 + CLAIM_WINDOW)
            self.assertIsNotNone(m2, rel2)
            dropped2 = flat2[:m2.start()] + flat2[m2.end():]
            with self.subTest(label=f"{rel2} 删数字", anchor=anchor2):
                self.assertTrue(doc_claim_findings(dropped2, anchor2, module2, actual), anchor2)


if __name__ == "__main__":
    unittest.main()
