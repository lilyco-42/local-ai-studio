"""外壳源码门禁：它必须能在源码被改坏时红，而不是只会在今天绿。

这里不编译 Rust（本机禁止编译），只检查**结构**：引擎名/旗标/端口写没写死、
校验载荷是不是排在起进程之前、外壳自己是不是只 bind 了一次临时端口、
UI 存不存在偷跑的 HTTP 客户端。真正的编译由 `.github/workflows/shell.yml` 负责。
"""
from __future__ import annotations

import glob
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import shell_source as gate  # noqa: E402

CORE = os.path.join(ROOT, "shell", "core", "src")
APP = os.path.join(ROOT, "shell", "src-tauri", "src")


class TestGate(unittest.TestCase):
    def test_repository_passes(self) -> None:
        self.assertEqual(gate.scan(), [])

    def test_selftest_mutations_are_caught(self) -> None:
        self.assertEqual(gate.selftest(), 0)

    def test_cli_check_exits_zero(self) -> None:
        import subprocess
        r = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "shell_source.py"), "--check"],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_engine_names_in_test_modules_do_not_trip_the_gate(self) -> None:
        text = open(os.path.join(CORE, "supervisor.rs"), encoding="utf-8").read()
        cut = text.find(gate.TEST_CUT)
        self.assertGreater(cut, 0, "supervisor.rs 没有测试模块？")
        self.assertIn("llama", text[cut:])
        self.assertNotIn("llama", gate.production(text))

    def test_a_new_file_with_a_hardcoded_engine_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            for name in os.listdir(CORE):
                if name.endswith(".rs"):
                    with open(os.path.join(CORE, name), encoding="utf-8") as fh:
                        text = fh.read()
                    with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                        fh.write(text)
            with open(os.path.join(d, "sneaky.rs"), "w", encoding="utf-8") as fh:
                fh.write('pub const E: &str = "--api-key";\n')
            got = gate.scan(core=d)
        self.assertTrue(any("sneaky.rs" in x for x in got), got)

    def test_ui_scan_is_green_on_invoke_only_and_red_on_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "main.js"), "w", encoding="utf-8") as fh:
                fh.write("const s = await window.__TAURI__.core.invoke('status');\n")
            self.assertEqual(gate.ui_scan(d), [])
            with open(os.path.join(d, "main.js"), "a", encoding="utf-8") as fh:
                fh.write("fetch('http://127.0.0.1:8080/x');\n")
            got = gate.ui_scan(d)
        self.assertTrue(any("fetch(" in x for x in got), got)
        self.assertTrue(any("127.0.0.1" in x for x in got), got)

    def test_an_absent_ui_produces_no_findings(self) -> None:
        missing = os.path.join(ROOT, "shell", "definitely-not-a-ui")
        self.assertEqual(gate.ui_scan(missing), [])

    def test_app_layer_is_green_and_a_hardcoded_engine_is_red(self) -> None:
        self.assertEqual(gate.app_scan(), [])
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "bad.rs"), "w", encoding="utf-8") as fh:
                fh.write('pub const E: &str = "whisper";\n')
            self.assertTrue(gate.app_scan(d))

    def test_every_declared_module_has_a_file(self) -> None:
        lib = open(os.path.join(CORE, "lib.rs"), encoding="utf-8").read()
        declared = set(gate.re.findall(r"^pub mod (\w+);", lib, gate.re.M))
        files = {os.path.splitext(os.path.basename(p))[0]
                 for p in glob.glob(os.path.join(CORE, "*.rs"))}
        self.assertEqual(declared, files - {"lib"})

    def test_rust_delimiters_balance(self) -> None:
        def stripped(text: str) -> str:
            out, i, n = [], 0, len(text)
            while i < n:
                c = text[i]
                if c == "/" and text.startswith("//", i):
                    j = text.find("\n", i)
                    i = n if j < 0 else j
                elif c == "/" and text.startswith("/*", i):
                    depth, i = 1, i + 2
                    while i < n and depth:
                        if text.startswith("/*", i):
                            depth, i = depth + 1, i + 2
                        elif text.startswith("*/", i):
                            depth, i = depth - 1, i + 2
                        else:
                            i += 1
                elif c == '"':
                    i += 1
                    while i < n:
                        if text[i] == "\\":
                            i += 2
                        elif text[i] == '"':
                            i += 1
                            break
                        else:
                            i += 1
                elif c == "'":
                    m = gate.re.match(r"'(?:\\.|[^\\'])'", text[i:])
                    if m:
                        i += len(m.group(0))
                    else:                       # 生命周期 'a，按普通字符处理
                        out.append(c)
                        i += 1
                else:
                    out.append(c)
                    i += 1
            return "".join(out)

        for path in glob.glob(os.path.join(CORE, "*.rs")) + glob.glob(os.path.join(APP, "*.rs")):
            with open(path, encoding="utf-8") as fh:
                body = stripped(fh.read())
            for op, cl in ("{}", "()", "[]"):
                depth = low = 0
                for c in body:
                    if c == op:
                        depth += 1
                    elif c == cl:
                        depth -= 1
                        low = min(low, depth)
                self.assertEqual((depth, low), (0, 0), f"{path} 的 {op}{cl} 不配平")

    def test_gate_reads_the_committed_contract(self) -> None:
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "pins", "shell-contract.json")))
        contract = open(os.path.join(CORE, "contract.rs"), encoding="utf-8").read()
        self.assertIn('include_str!("../../../pins/shell-contract.json")', contract)


if __name__ == "__main__":
    unittest.main()
