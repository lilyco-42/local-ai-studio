"""闸门自己的测试：yaml_gate 必须会红，而且红得有坐标。

上一轮我写的字幕/OCR 车道里有一个多缩进两格的注释（Python 里就是 IndentationError），
而当时那版闸门只查 heredoc 是否闭合，放它过去了。所以现在闸门会 compile 每段 PY，
这里就钉住这件事：好文件必须 0 BAD，坏文件必须非零退出并指名道姓。
"""
import glob
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "..", "tools", "yaml_gate.py")
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import action_pins  # noqa: E402
import bash_gate  # noqa: E402
import workflow_files  # noqa: E402
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

    def test_lane_hashes_are_actually_sourced(self) -> None:
        """正向对照之二：真实车道的写死哈希确实被寻到源，数量要打印出来才算覆盖。
        上一版按 `*_SHA256` 键名认，OCR 车道用的是 `DET_SHA/REC_SHA/DICT_SHA`，
        三条真 pin 就这么漏在覆盖度外面而闸门照样报绿 —— 所以现在按"值是 64 位十六进制"认。"""
        out = run_gate(WORKFLOWS).stdout
        n = int(out.split("哈希 ")[1].split(" 条")[0])
        self.assertGreaterEqual(n, 8, f"只寻到 {n} 条，说明 env 里的哈希大面积没被覆盖")
        self.assertIn("models.json:qwen2.5-0.5b-instruct-q2_k.gguf", out)
        self.assertIn("ocr.json:pp-ocrv6_tiny_det.onnx", out)

    def test_differently_named_lane_hash_is_still_checked(self) -> None:
        """换个键名（*_SHA 而不是 *_SHA256）不能绕开寻源检查。"""
        wf = ("name: t\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
              "    env:\n      DET_SHA: " + "f" * 64 + "\n    steps:\n      - name: s\n        run: echo hi\n")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sha_name.yml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(wf)
            r = run_gate([p])
        self.assertEqual(r.returncode, 1, f"*_SHA 命名的假哈希绕过了闸门：{r.stdout[-400:]}")
        self.assertIn("没有任何出处", r.stdout)

    def test_lane_hash_without_provenance_is_caught(self) -> None:
        """车道里一个来路不明的哈希必须红。今天就是靠这件事查出 415 MB 那颗对话权重
        只写在 yaml 里、从没被任何工具复现过 —— 写下来不等于钉住。"""
        wf = ("name: t\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
              "    env:\n      MODEL_SHA256: " + "a" * 64 + "\n      MODEL_SIZE: \"1\"\n"
              "    steps:\n      - name: s\n        run: echo hi\n")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "ghosthash.yml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(wf)
            r = run_gate([p])
        self.assertEqual(r.returncode, 1, f"来路不明的哈希没被拦：{r.stdout[-400:]}")
        self.assertIn("没有任何出处", r.stdout)

    def test_all_digit_hash_needing_quotes_is_caught(self) -> None:
        """全是数字的 64 位哈希不加引号会被 YAML 读成 int —— 上一版闸门因此"寻源 0 条"却报绿。
        现在这条必须红，而且要指出是没加引号。"""
        wf = ("name: t\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
              "    env:\n      MODEL_SHA256: " + "0" * 64 + "\n"
              "    steps:\n      - name: s\n        run: echo hi\n")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "numeric.yml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(wf)
            r = run_gate([p])
        self.assertEqual(r.returncode, 1, f"被当成数字的哈希没被拦：{r.stdout[-400:]}")
        self.assertIn("别让它被当成数字", r.stdout)

    def test_needs_ref_to_an_unexported_output_is_caught(self) -> None:
        """引用上游没导出的 outputs：拿到的是空字符串而不是报错，所以只能靠闸门。"""
        wf = ("name: t\non:\n  push:\njobs:\n"
              "  up:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
              "  down:\n    needs: up\n    runs-on: ubuntu-latest\n    steps:\n"
              "      - run: echo ${{ needs.up.outputs.llama_url }}\n")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "needs.yml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(wf)
            r = run_gate([p])
        self.assertEqual(r.returncode, 1, f"引用了没导出的输出却没红：{r.stdout[-400:]}")
        self.assertIn("up 没导出它", r.stdout)

    def test_needs_ref_without_the_needs_is_caught(self) -> None:
        """用了 needs.a.outputs.x 却没 needs a：Actions 里那是空字符串，不是错误。"""
        wf = ("name: t\non:\n  push:\njobs:\n"
              "  up:\n    runs-on: ubuntu-latest\n    outputs:\n      url: hello\n    steps:\n"
              "      - run: echo hi\n"
              "  down:\n    runs-on: ubuntu-latest\n    steps:\n"
              "      - run: echo ${{ needs.up.outputs.url }}\n")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "noneeds.yml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(wf)
            r = run_gate([p])
        self.assertEqual(r.returncode, 1, f"没 needs 却引用了它的输出：{r.stdout[-400:]}")
        self.assertIn("却没 needs up", r.stdout)

    def test_needs_refs_are_blamed_on_the_job_that_wrote_them(self) -> None:
        """回归：上一版扫的是整份文件，任何带 needs 的 job 都要为别人的引用背锅 ——
        给 launch-plan 加了一句 needs: file-index（它不导出东西），它凭空背上六条错。"""
        wf = ("name: t\non:\n  push:\njobs:\n"
              "  up:\n    runs-on: ubuntu-latest\n    outputs:\n      url: hello\n    steps:\n"
              "      - run: echo hi\n"
              "  silent:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n"
              "  user:\n    needs: up\n    runs-on: ubuntu-latest\n    steps:\n"
              "      - run: echo ${{ needs.up.outputs.url }}\n"
              "  bystander:\n    needs: silent\n    runs-on: ubuntu-latest\n    steps:\n"
              "      - run: echo hi\n")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "blame.yml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(wf)
            r = run_gate([p])
        self.assertEqual(r.returncode, 0, f"别人写的引用算到了 bystander 头上：{r.stdout[-500:]}")
        self.assertIn("BAD 数量 = 0", r.stdout)

    def test_lane_size_mismatch_against_pin_is_caught(self) -> None:
        """哈希对得上但体积抄错也要红 —— pin 正确、旁边的数字漂了是同一类事故。"""
        import json
        with open(os.path.join(ROOT, "pins", "models.json"), encoding="utf-8") as fh:
            rec = json.load(fh)["ggml-tiny.bin"]
        wf = ("name: t\non:\n  push:\njobs:\n  j:\n    runs-on: ubuntu-latest\n"
              "    env:\n      MODEL_SHA256: " + rec["sha256"] + "\n      MODEL_SIZE: \"9\"\n"
              "    steps:\n      - name: s\n        run: echo hi\n")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "wrongsize.yml")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(wf)
            r = run_gate([p])
        self.assertEqual(r.returncode, 1, f"体积不符必须红：{r.stdout[-400:]}")
        self.assertIn("体积不符", r.stdout)


class TestWorkflowCoverage(unittest.TestCase):
    """三份手抄清单各自漏过：shell.yml 的 4 处 uses 从未被审计、总数还是 49。"""

    @staticmethod
    def on_disk() -> set[str]:
        names: set[str] = set()
        for ext in ("*.yml", "*.yaml"):
            names |= {os.path.basename(p) for p in
                      glob.glob(os.path.join(ROOT, ".github", "workflows", ext))}
        return names

    def test_shared_list_matches_disk_and_every_consumer_reads_it(self) -> None:
        disk = self.on_disk()
        self.assertTrue(disk)
        self.assertEqual(set(workflow_files.WORKFLOW_FILES), disk)
        for name in workflow_files.WORKFLOW_FILES:
            self.assertTrue(os.path.isfile(
                os.path.join(ROOT, ".github", "workflows", name)), name)
        self.assertEqual({os.path.basename(p) for p in action_pins.WORKFLOWS}, disk)
        self.assertEqual({os.path.basename(p) for p in bash_gate.default_workflows(ROOT)}, disk)

    def test_yaml_gate_default_covers_every_workflow(self) -> None:
        """不带参数跑闸门：它打印的每个文件名必须覆盖磁盘上的每一条。"""
        r = subprocess.run([sys.executable, GATE], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout[-900:] + r.stderr[-400:])
        for name in self.on_disk():
            self.assertIn(name + ":", r.stdout, f"{name} 没被默认闸门读到")

    def test_dropping_one_file_is_visible(self) -> None:
        """负向对照：判据不是拿同一份表自比 —— 少一条必须看得出来。"""
        dropped = set(workflow_files.WORKFLOW_FILES) - {workflow_files.WORKFLOW_FILES[0]}
        self.assertNotEqual(dropped, self.on_disk())


if __name__ == "__main__":
    unittest.main()
