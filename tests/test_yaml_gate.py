"""闸门自己的测试：yaml_gate 必须会红，而且红得有坐标。

上一轮我写的字幕/OCR 车道里有一个多缩进两格的注释（Python 里就是 IndentationError），
而当时那版闸门只查 heredoc 是否闭合，放它过去了。所以现在闸门会 compile 每段 PY，
这里就钉住这件事：好文件必须 0 BAD，坏文件必须非零退出并指名道姓。
"""
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "..", "tools", "yaml_gate.py")
ROOT = os.path.abspath(os.path.join(HERE, ".."))
WORKFLOWS = [os.path.join(ROOT, ".github", "workflows", "ci.yml"),
             os.path.join(ROOT, ".github", "workflows", "engine-live.yml")]


def run_gate(paths):
    return subprocess.run([sys.executable, GATE] + paths, capture_output=True, text=True)


def wf_with_run(body):
    """造一条最小可用的 workflow，run 内容原样嵌入（body 里的缩进就是 YAML 块里的缩进）。"""
    return ("name: t\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n"
            "      - name: s\n        run: |\n" + body)


class TestGate(unittest.TestCase):
    def test_real_workflows_pass(self) -> None:
        r = run_gate(WORKFLOWS)
        self.assertEqual(r.returncode, 0, r.stdout[-900:] + r.stderr[-400:])
        self.assertIn("BAD 数量 = 0", r.stdout)

    def test_coverage_is_nonzero(self) -> None:
        """闸门必须真的编译了东西：0 段和 7 段都可能是"绿"，只有打印出来才分得清。"""
        out = run_gate(WORKFLOWS).stdout
        seen = int(out.split("编译了 ")[1].split(" 段")[0])
        self.assertGreaterEqual(seen, 5, f"只编到 {seen} 段，说明大部分 heredoc 被跳过")

    def test_bad_python_in_heredoc_is_caught(self) -> None:
        body = ("          python3 - <<'PY'\n"
                "          def ok():\n"
                "          import sys\n"
                "              print('故意缩进错')\n"
                "          PY\n")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bad.yml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(wf_with_run(body))
            r = run_gate([p])
        self.assertEqual(r.returncode, 1, "缩进错的 PY 段必须让闸门非零退出")
        self.assertIn("PY 语法错", r.stdout, r.stdout[-500:])
        self.assertIn("bad.yml", r.stdout, "报错没指明文件，改的人找不到地方")
        self.assertIn("第0段", r.stdout, "没报第几段，多个 heredoc 时说不清是哪一段")

    def test_unclosed_heredoc_is_caught(self) -> None:
        body = "          python3 - <<'PY'\n          print('忘了关')\n"
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "unclosed.yml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(wf_with_run(body))
            r = run_gate([p])
        self.assertEqual(r.returncode, 1, "heredoc 没闭合必须红")
        self.assertIn("没闭合", r.stdout, r.stdout[-400:])

    def test_missing_target_key_is_caught(self) -> None:
        """车道里写死的 target 名不在 manifest 里 → 之前用假名验过一次，这里钉成回归。"""
        body = ('          python3 - <<\'PY\' >> "$GITHUB_OUTPUT"\n'
                '          print("x")\n'
                '          PY\n'
                '          echo "llama-ubuntu-base-x99"\n')
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ghost.yml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(wf_with_run(body))
            r = run_gate([p])
        self.assertEqual(r.returncode, 1, f"引用了不存在的 target 却没红：{r.stdout[-400:]}")
        self.assertIn("llama-ubuntu-base-x99", r.stdout)

    def test_good_python_passes(self) -> None:
        """正向对照：好代码必须 0 BAD，否则上面四条的"红"没有信息量。"""
        body = ("          python3 - <<'PY' >> \"$GITHUB_OUTPUT\"\n"
                "          import json\n"
                "          print(json.dumps({\"a\": 1}))\n"
                "          PY\n")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "good.yml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(wf_with_run(body))
            r = run_gate([p])
        self.assertEqual(r.returncode, 0, r.stdout[-500:] + r.stderr[-300:])
        self.assertIn("编译了 1 段", r.stdout)


if __name__ == "__main__":
    unittest.main()
