"""外壳源码门禁：它必须能在源码被改坏时红，而不是只会在今天绿。

这里不编译 Rust（本机禁止编译），只检查**结构**：引擎名/旗标/端口写没写死、
校验载荷是不是排在起进程之前、外壳自己是不是只 bind 了一次临时端口、
UI 存不存在偷跑的 HTTP 客户端。真正的编译由 `.github/workflows/shell.yml` 负责。
"""
from __future__ import annotations

import glob
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import shell_source as gate  # noqa: E402

CORE = os.path.join(ROOT, "shell", "core", "src")
APP = os.path.join(ROOT, "shell", "src-tauri", "src")
UI = os.path.join(ROOT, "shell", "ui")
AVAILABILITY_RULE = "let available = cap.os.contains_key(self.platform);"


def availability_findings(core_text: str, js_text: str, html_text: str) -> list[str]:
    """#29 的可逆半边：合同没有这一格时，UI 要禁用并说清，而不是点了才报错。"""
    out = []
    if "pub available: bool" not in core_text:
        out.append("Status 没有 available 字段")
    if AVAILABILITY_RULE not in core_text:
        out.append("available 没有按平台从合同推导")
    if "row.available === false" not in js_text:
        out.append("UI 没有处理 available=false")
    if "data-capability" not in js_text:
        out.append("按能力禁用按钮没有落到 data-capability")
    if 'data-capability="字幕"' not in html_text:
        out.append("字幕按钮没有标 capability")
    return out


class TestCapabilityAvailability(unittest.TestCase):
    @staticmethod
    def read(*parts: str) -> str:
        with open(os.path.join(*parts), encoding="utf-8") as fh:
            return fh.read()

    def test_repository_passes(self) -> None:
        self.assertEqual(availability_findings(
            self.read(CORE, "supervisor.rs"),
            self.read(UI, "main.js"),
            self.read(UI, "index.html"),
        ), [])

    def test_mutations_are_caught(self) -> None:
        core = self.read(CORE, "supervisor.rs")
        js = self.read(UI, "main.js")
        html = self.read(UI, "index.html")
        self.assertTrue(availability_findings(
            core.replace(AVAILABILITY_RULE, "let available = true;"), js, html))
        self.assertTrue(availability_findings(
            core, js.replace("row.available === false", "false"), html))
        self.assertTrue(availability_findings(
            core, js, html.replace('data-capability="字幕"', "")))


I18N_KEY = re.compile(r"^    (\w+):", re.M)
I18N_VALUE = re.compile(r"^    (\w+):\s*'(.*)'", re.M)


def i18n_findings(js: str, html: str) -> list[str]:
    """zh/en 键集必须一致，且 HTML 与 `t('…')` 引用的键必须都在表里。

    `t()` 的回退链是 en → zh → 原始键：en 缺一条会静默显示中文，两边都缺才会露出
    `tabChat` 这种东西 —— 所以不能靠"跑起来看看"发现。
    """
    out = []
    try:
        start = js.index("const I18N = {")
        end = js.index("\n};", start)
        body = js[start:end]
        zh_at = body.index("\n  zh: {")
        en_at = body.index("\n  en: {")
    except ValueError:
        return ["main.js 里找不到 I18N 的 zh/en 块"]
    zh_block, en_block = body[zh_at:en_at], body[en_at:]
    zh, en = I18N_KEY.findall(zh_block), I18N_KEY.findall(en_block)
    for label, keys in (("zh", zh), ("en", en)):
        dup = sorted({k for k in keys if keys.count(k) > 1})
        if dup:
            out.append(f"{label} 有重复键 {dup}")
        empty = [k for k, v in I18N_VALUE.findall(
            zh_block if label == "zh" else en_block) if not v.strip()]
        if empty:
            out.append(f"{label} 有空值 {empty}")
    missing_en = sorted(set(zh) - set(en))
    missing_zh = sorted(set(en) - set(zh))
    if missing_en:
        out.append(f"en 缺键 {missing_en}")
    if missing_zh:
        out.append(f"zh 缺键 {missing_zh}")
    known = set(zh) | set(en)
    used = set(re.findall(r'data-i18n(?:-ph)?="([\w-]+)"', html))
    used |= set(re.findall(r"\bt\(\s*'([\w-]+)'\s*\)", js))
    ghosts = sorted(used - known)
    if ghosts:
        out.append(f"引用了不存在的键 {ghosts}")
    return out


class TestI18n(unittest.TestCase):
    @staticmethod
    def read(*parts: str) -> str:
        with open(os.path.join(*parts), encoding="utf-8") as fh:
            return fh.read()

    def test_repository_passes(self) -> None:
        self.assertEqual(i18n_findings(
            self.read(UI, "main.js"), self.read(UI, "index.html")), [])

    def test_mutations_are_caught(self) -> None:
        js = self.read(UI, "main.js")
        html = self.read(UI, "index.html")
        cases = {
            "en 缺键": (js.replace("    send: 'Send',\n", ""), html),
            "en 空值": (js.replace("send: 'Send'", "send: ''"), html),
            "zh 重复键": (js.replace("    tabChat: '对话',\n",
                                   "    tabChat: '对话',\n    tabChat: '对话',\n"), html),
            "HTML 引用幽灵键": (js, html.replace('data-i18n="send"', 'data-i18n="ghost"')),
            "t() 引用幽灵键": (js.replace("t('oneShot')", "t('ghost')"), html),
        }
        for label, (j, h) in cases.items():
            with self.subTest(label=label):
                self.assertTrue(i18n_findings(j, h), label)


def raw_string_end(text: str, i: int):
    """`r"…"` / `r#"…"#` / `r##"…"##` 的结束位置；i 指向 `r`，不是 raw string 就返回 None。"""
    j = i + 1
    hashes = 0
    while j < len(text) and text[j] == "#":
        hashes += 1
        j += 1
    if j >= len(text) or text[j] != '"':
        return None
    end = text.find('"' + "#" * hashes, j + 1)
    return len(text) if end < 0 else end + 1 + hashes


def strip_rust(text: str) -> str:
    """去掉注释与字符串（含 raw string），只留用于配平检查的代码字符。"""
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
        elif c == "r":
            end = raw_string_end(text, i)
            if end is None:
                out.append(c)
                i += 1
            else:
                i = end
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
        for path in glob.glob(os.path.join(CORE, "*.rs")) + glob.glob(os.path.join(APP, "*.rs")):
            with open(path, encoding="utf-8") as fh:
                body = strip_rust(fh.read())
            for op, cl in ("{}", "()", "[]"):
                depth = low = 0
                for c in body:
                    if c == op:
                        depth += 1
                    elif c == cl:
                        depth -= 1
                        low = min(low, depth)
                self.assertEqual((depth, low), (0, 0), f"{path} 的 {op}{cl} 不配平")

    def test_raw_strings_are_skipped_by_the_delimiter_scan(self) -> None:
        body = strip_rust('const J: &str = r#"{"a":[1,2]}"#;\nfn f() {}\n')
        self.assertEqual((body.count("{"), body.count("}")), (1, 1))
        body = strip_rust('let s = r##"a } " b"##; fn g() { if true { } }')
        self.assertEqual((body.count("{"), body.count("}")), (2, 2))
        # 标识符里的 r 不是 raw string 的开头
        body = strip_rust('fn bar() { let x = "}"; }')
        self.assertEqual((body.count("{"), body.count("}")), (1, 1))

    def test_gate_reads_the_committed_contract(self) -> None:
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "pins", "shell-contract.json")))
        contract = open(os.path.join(CORE, "contract.rs"), encoding="utf-8").read()
        self.assertIn('include_str!("../../../pins/shell-contract.json")', contract)


if __name__ == "__main__":
    unittest.main()
