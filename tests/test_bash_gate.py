"""tools/bash_gate.py 的判据测试。

最要紧的一条不是"能查出语法错"，而是"**假 bash 不会被当成查过了**"：
这台 Windows 机器上 PATH 里的 `bash` 是 WSL 占位程序，对任何文件都返回非 0，
拿它跑闸门会得到 42/42 全红 —— 那副样子比不查更坏，因为它像是在认真把关。
所以这里用注入的假 run 把三种情况都钉住。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import bash_gate  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def fake_run(good_rc=0, bad_rc=2, err="line 3: syntax error"):
    """按文件内容给 rc：闸门自己写的两个对照文件叫 good/bad（其余按坏的处理，
    这样真步骤里忘了写 rc 的分支也会露出来）。"""
    def run(bash, path):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        if src == bash_gate.GOOD_SRC:
            return good_rc, ""
        if src == bash_gate.BAD_SRC:
            return bad_rc, ""
        return 1, err
    return run


class TestProbe(unittest.TestCase):
    def test_stub_bash_is_not_usable(self) -> None:
        """WSL 占位程序那种"什么都非 0"的 bash 必须被判成不可信。"""
        state, path, why = bash_gate.probe_bash(fake_run(good_rc=1, bad_rc=1))
        self.assertEqual(state, "unreliable")
        self.assertIsNone(path)
        self.assertIn("好文件 rc=1", why)

    def test_always_zero_bash_is_not_usable(self) -> None:
        """另一种骗人法：永远返回 0。坏文件必须非 0，否则它根本没在检查。"""
        state, _, _ = bash_gate.probe_bash(fake_run(good_rc=0, bad_rc=0))
        self.assertEqual(state, "unreliable")

    def test_missing_bash_reports_missing(self) -> None:
        def boom(bash, path):
            raise OSError("no such file")
        state, path, why = bash_gate.probe_bash(boom)
        self.assertEqual(state, "missing")
        self.assertIsNone(path)
        self.assertIn("OSError", why)

    def test_unusable_reason_reaches_the_note(self) -> None:
        """原因要能传到那行 SKIP 里 —— 只写"不可用"的话，下一个人就不会去查是哪种。"""
        def boom(bash, path):
            raise OSError("no such file")
        good = bash_gate.GOOD_SRC
        _, note = bash_gate.check([("x", good)], boom)
        self.assertIn("missing", note)
        _, note2 = bash_gate.check([("x", good)], fake_run(good_rc=1, bad_rc=1))
        self.assertIn("unreliable", note2)

    def test_real_bash_passes_both_controls(self) -> None:
        """正向对照：真 bash（本机 Git 的那份）必须被判成 usable。
        这条在 runner 上一定真跑；本机若没有可用 bash 就跳过，并明确说明跳过了。"""
        state, path, _ = bash_gate.probe_bash()
        if state != "usable":
            self.skipTest(f"本机没有可信的 bash（{state}）—— CI 的 ubuntu runner 上会真查")
        self.assertTrue(path)


class TestCheck(unittest.TestCase):
    def test_unusable_bash_yields_skip_not_green(self) -> None:
        problems, note = bash_gate.check([("a.sh:j#0", "if [ x ]\n")],
                                          fake_run(good_rc=1, bad_rc=1))
        self.assertEqual(problems, [])
        self.assertIn("SKIP", note)

    def test_reports_the_broken_step_and_labels_it(self) -> None:
        problems, note = bash_gate.check(
            [("wf.yml:job#1(名字)", "if [ x ]\n"), ("wf.yml:job#2", bash_gate.GOOD_SRC)],
            fake_run())
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("wf.yml:job#1", problems[0])
        self.assertIn("语法检查了 2 段", note)

    def test_step_bodies_reads_every_run_from_workflows(self) -> None:
        files = [os.path.join(ROOT, ".github", "workflows", f)
                 for f in ("ci.yml", "engine-live.yml", "package.yml")]
        bodies = bash_gate.step_bodies(files)
        self.assertGreater(len(bodies), 40, "run 步骤数掉下来了，说明抽取逻辑没命中")
        self.assertTrue(all(lbl and src for lbl, src in bodies))

    def test_all_shipped_steps_are_syntax_clean(self) -> None:
        """整仓扫一遍：这条在没 bash 的机器上会 skip，但那条 skip 会被上面的正向对照看到。"""
        state = bash_gate.probe_bash()[0]
        if state != "usable":
            self.skipTest("本机没有可信的 bash")
        problems, _ = bash_gate.check(
            bash_gate.step_bodies([os.path.join(ROOT, ".github", "workflows", f)
                                   for f in ("ci.yml", "engine-live.yml", "package.yml")]))
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
