"""tools/shell_vars.py 的判据测试：`set -u` 下车道引用未定义变量会当场死掉，
而这类错误 CI 只有真跑那天才告诉你（本仓库现在就跑不了）。所以把它做成本地静态判据。

每条都成对写：一个"必须报"，一个"不能报"。只写前者会养出一条永远红的闸门。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import shell_vars  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
WF = os.path.join(ROOT, ".github", "workflows", "engine-live.yml")


def names(body: str, env_keys=()) -> list[str]:
    return [n for _, n in shell_vars.unbound_refs(body, env_keys)]


class TestRealAccident(unittest.TestCase):
    """新加 llama-server 步骤时真写过 `export LD_LIBRARY_PATH="$(dirname "$SRV"):$LD_LIBRARY_PATH"`；
    runner 上这个变量不存在，set -u 会在起服务之前就死。修成 ${…:+…} 之后不该再报。"""

    BAD = ('set -euo pipefail\nSRV=$(find engine -name llama-server | head -1)\n'
           'export LD_LIBRARY_PATH="$(dirname "$SRV"):$LD_LIBRARY_PATH"\n')
    FIXED = ('set -euo pipefail\nSRV=$(find engine -name llama-server | head -1)\n'
             'export LD_LIBRARY_PATH="$(dirname "$SRV")${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\n')

    def test_bare_self_reference_is_red(self) -> None:
        self.assertEqual(names(self.BAD), ["LD_LIBRARY_PATH"])

    def test_guarded_form_is_green(self) -> None:
        self.assertEqual(names(self.FIXED), [])


class TestCommentsAndHeredocs(unittest.TestCase):
    """注释与 heredoc 是两种相反的文本：前者根本不展开，后者（未加引号时）照旧展开。
    搞混任何一边都会让闸门变成谎话 —— 一边是假红，一边是把真 bug 洗白。"""

    def test_full_line_comment_is_not_a_reference(self) -> None:
        body = ('set -euo pipefail\nSRV=x\n# 用 ${LD_LIBRARY_PATH:+…} 而不是 :$LD_LIBRARY_PATH\n'
                'echo "$SRV"\n')
        self.assertEqual(names(body), [])

    def test_assignment_written_only_in_a_comment_does_not_count(self) -> None:
        """反向对照：洗注释不能顺手把 `# FOO=1` 当成"FOO 定义过了"。"""
        body = 'set -euo pipefail\n# FOO=1\necho "$FOO"\n'
        self.assertEqual(names(body), ["FOO"])

    def test_unquoted_heredoc_expands_even_on_a_hash_line(self) -> None:
        body = 'set -u\ncat <<PY\n# $MISSING\nPY\n'
        self.assertEqual(names(body), ["MISSING"])

    def test_quoted_heredoc_is_inert(self) -> None:
        """把上一条的 `<<PY` 加引号 —— 同一段文本就不再展开了。"""
        body = "set -u\ncat <<'PY'\n# $MISSING\nPY\n"
        self.assertEqual(names(body), [])


class TestNotReported(unittest.TestCase):
    def test_assignment_earlier_on_same_line(self) -> None:
        self.assertEqual(names('set -euo pipefail\nd=$(dirname x); echo "$d"\n'), [])

    def test_env_keys_and_github_builtins(self) -> None:
        body = 'set -euo pipefail\ncurl "$MODEL_URL" -o "$GITHUB_OUTPUT"\n'
        self.assertEqual(names(body), ["MODEL_URL"])   # GITHUB_* 是 runner 给的，不报
        self.assertEqual(names('set -euo pipefail\necho "$PIN"\n', env_keys=["PIN"]), [])

    def test_loop_and_read_variables(self) -> None:
        self.assertEqual(names('set -euo pipefail\nfor u in 1 2; do echo "$u"; done\n'), [])
        self.assertEqual(names('set -euo pipefail\nwhile IFS="\\t" read -r target url; do echo "$target $url"; done\n'), [])

    def test_quoted_heredoc_body_is_not_shell(self) -> None:
        """PY 段里的 `$xxx` 由 Python 解释，扫它会变成误报源。"""
        body = ("set -euo pipefail\npython3 - <<'PY'\n"
                "print('$not_a_var {also}')\nPY\n")
        self.assertEqual(names(body), [])

    def test_steps_without_set_u_are_not_checked(self) -> None:
        self.assertEqual(names('echo "$WHO_KNOWS"\n'), [])


class TestGenuineTraps(unittest.TestCase):
    def test_command_prefix_assignment_is_unbound_for_its_own_args(self) -> None:
        """`FOO=1 bar "$FOO"` 看着像先赋值，但那个 FOO 只进 bar 的环境，
        同一行的 "$FOO" 由当前 shell 展开 —— set -u 下就是未定义。这条不是误报。"""
        self.assertEqual(names('set -euo pipefail\nFOO=1 bar "$FOO"\n'), ["FOO"])

    def test_never_defined(self) -> None:
        self.assertEqual(names('set -euo pipefail\necho "$NOPE"\n'), ["NOPE"])


class TestWorkflowsAreClean(unittest.TestCase):
    """仓库里两条 workflow 现在必须一个都不报 —— 这条是"以后别改回来"的守卫。"""

    def test_engine_live(self) -> None:
        import yaml
        with open(WF, encoding="utf-8") as fh:      # CI 跑的是 -W error::ResourceWarning，裸 open 会变成错误
            d = yaml.safe_load(fh)
        for jn, job in d["jobs"].items():
            keys = list((d.get("env") or {})) + list((job.get("env") or {}))
            for i, st in enumerate(job["steps"]):
                hits = shell_vars.unbound_refs(st.get("run") or "", keys + list((st.get("env") or {})))
                self.assertEqual(hits, [], f"{jn}#{i}({st.get('name','')}) 在 set -u 下会死：{hits}")


if __name__ == "__main__":
    unittest.main()
