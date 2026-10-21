"""tools/wd_ipc_probe.py 的判据测试（全部离线）。

真正连窗口的那一步只能在 CI 的 Xvfb 里跑；这里钉住的是它赖以判红判绿的东西：
会话请求的形状、status 的字段与能力集合、data_root 的绝对路径、UI 渲染证据，
以及"车道真的调用了这套判据并带了已知坏对照"。
"""
import json
import os
import re
import sys
import tempfile
import time
import unittest

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import wd_ipc_probe as wd  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
WF = os.path.join(ROOT, ".github", "workflows", "shell.yml")
MAIN_JS = os.path.join(ROOT, "shell", "ui", "main.js")
CONTRACT = os.path.join(ROOT, "pins", "shell-contract.json")


def row(name, **overrides):
    base = {
        "capability": name, "engine": "llama", "available": True, "installed": False,
        "running": False,
        "pid": None, "port": None, "idle_secs_left": None, "lifecycle": "resident",
    }
    base.update(overrides)
    return base


def rows():
    return [row(name) for name in wd.EXPECTED_CAPABILITIES]


def ui_state(**overrides):
    base = {
        "title": "Local AI Studio", "tauri": True, "boot_hidden": True,
        "chip": "对话 · 未启动", "data_root_text": "/home/runner/.local/share/local-ai-studio",
    }
    base.update(overrides)
    return base


class TestSessionShape(unittest.TestCase):
    def test_absolute_app_path_is_required(self) -> None:
        with self.assertRaises(ValueError):
            wd.session_payload("target/debug/local-ai-studio-shell")

    def test_capabilities_carry_browser_and_tauri_application(self) -> None:
        app = "/home/runner/work/local-ai-studio/shell/src-tauri/target/debug/local-ai-studio-shell"
        payload = wd.session_payload(app)
        always = payload["capabilities"]["alwaysMatch"]
        self.assertEqual(always["browserName"], "wry")
        self.assertEqual(always["tauri:options"]["application"], app)

    def test_driver_argv_uses_both_ports_and_native_binary(self) -> None:
        argv = wd.driver_argv("tauri-driver", 4444, 4445, "/usr/bin/WebKitWebDriver")
        self.assertEqual(argv[:2], ["tauri-driver", "--port"])
        self.assertIn("4444", argv)
        self.assertIn("4445", argv)
        self.assertEqual(argv[argv.index("--native-driver") + 1], "/usr/bin/WebKitWebDriver")

    def test_driver_argv_rejects_same_port(self) -> None:
        with self.assertRaises(ValueError):
            wd.driver_argv("tauri-driver", 4444, 4444, None)

    def test_driver_error_body_is_reported(self) -> None:
        body = b'{"value":{"error":"session not created","message":"no such app"}}'
        text = wd.parse_driver_error(400, body)
        self.assertIn("session not created", text)
        self.assertIn("no such app", text)

    def test_driver_error_falls_back_to_raw_body(self) -> None:
        self.assertIn("HTTP 502", wd.parse_driver_error(502, b"<html>bad gateway</html>"))


class TestStatusGate(unittest.TestCase):
    def test_four_ui_capabilities_pass(self) -> None:
        wd.check_status(rows())

    def test_capability_names_match_the_ui_tab_mapping(self) -> None:
        with open(MAIN_JS, encoding="utf-8") as fh:
            source = fh.read()
        block = re.search(r"const TAB_CAP = \{([^}]*)\};", source)
        self.assertIsNotNone(block, "main.js 里找不到 TAB_CAP")
        values = set(re.findall(r"'([^']+)'", block.group(1)))
        self.assertEqual(values, set(wd.EXPECTED_CAPABILITIES))

    def test_extra_or_missing_row_is_rejected(self) -> None:
        with self.assertRaisesRegex(wd.ProbeError, "应有 4 行"):
            wd.check_status(rows()[:3])

    def test_unknown_capability_is_rejected(self) -> None:
        bad = rows()
        bad[3]["capability"] = "不存在的能力"
        with self.assertRaisesRegex(wd.ProbeError, "能力集合"):
            wd.check_status(bad)

    def test_duplicate_capability_is_rejected(self) -> None:
        bad = rows()
        bad[3]["capability"] = bad[0]["capability"]
        with self.assertRaisesRegex(wd.ProbeError, "重复能力"):
            wd.check_status(bad)

    def test_missing_field_is_rejected(self) -> None:
        bad = rows()
        del bad[0]["lifecycle"]
        with self.assertRaisesRegex(wd.ProbeError, "缺字段.*lifecycle"):
            wd.check_status(bad)

    def test_non_boolean_available_is_rejected(self) -> None:
        bad = rows()
        bad[1]["available"] = 1
        with self.assertRaisesRegex(wd.ProbeError, "available 不是布尔值"):
            wd.check_status(bad)

    def test_boolean_pid_is_rejected(self) -> None:
        bad = rows()
        bad[2]["pid"] = True
        with self.assertRaisesRegex(wd.ProbeError, "pid 既不是 null 也不是整数"):
            wd.check_status(bad)

    def test_installed_must_be_boolean(self) -> None:
        bad = rows()
        bad[0]["installed"] = "false"
        with self.assertRaisesRegex(wd.ProbeError, "installed 不是布尔值"):
            wd.check_status(bad)

    def test_fresh_installation_expectation_passes_when_all_are_false(self) -> None:
        wd.check_status(rows(), installed=False)

    def test_fresh_installation_expectation_rejects_an_installed_row(self) -> None:
        bad = rows()
        bad[1]["installed"] = True
        with self.assertRaisesRegex(wd.ProbeError, "installed 期望全是 False"):
            wd.check_status(bad, installed=False)

    def test_non_list_is_rejected(self) -> None:
        with self.assertRaisesRegex(wd.ProbeError, "应是数组"):
            wd.check_status({"capability": "对话"})


class TestInstalledMapGate(unittest.TestCase):
    def want(self):
        return {name: name == "对话" for name in wd.EXPECTED_CAPABILITIES}

    def test_only_the_planted_capability_is_true(self) -> None:
        data = rows()
        data[0]["installed"] = True
        wd.check_installed_map(data, self.want())

    def test_another_row_flipping_true_is_rejected(self) -> None:
        data = rows()
        data[0]["installed"] = True
        data[1]["installed"] = True
        with self.assertRaisesRegex(wd.ProbeError, "installed 期望"):
            wd.check_installed_map(data, self.want())


class TestPlantedPayload(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "data")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_expand_data_dir_replaces_the_placeholder(self) -> None:
        path = wd.expand_data_dir("<data>/runtime/linux-x64/llama/b1/x",
                                  "/home/runner/.local/share/app")
        self.assertEqual(path, "/home/runner/.local/share/app/runtime/linux-x64/llama/b1/x")

    def test_expand_data_dir_rejects_escapes_and_relative_roots(self) -> None:
        for template in ("<data>/../etc", "<data>/a//b", "<data>/a\\b", "runtime/x"):
            with self.subTest(template=template), self.assertRaises(ValueError):
                wd.expand_data_dir(template, "/data")
        with self.assertRaises(ValueError):
            wd.expand_data_dir("<data>/runtime", "relative/root")

    def test_plant_writes_a_manifest_matching_the_contract_cell(self) -> None:
        info = wd.plant_payload(self.root, CONTRACT, capability="对话", platform="linux-x64", size=3)
        with open(info["manifest"], encoding="utf-8") as fh:
            manifest = json.load(fh)
        with open(CONTRACT, encoding="utf-8") as fh:
            cell = json.load(fh)["capabilities"]["对话"]["os"]["linux-x64"]
        self.assertEqual(manifest["os"], "linux-x64")
        self.assertEqual(len(manifest["engines"]), 1)
        entry = manifest["engines"][0]
        self.assertEqual(entry["engine"], "llama")
        self.assertEqual(entry["asset"], cell["default_asset"])
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["files"][0]["file"], "probe-presence.bin")
        self.assertEqual(entry["files"][0]["bytes"], 3)
        self.assertRegex(entry["files"][0]["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(info["dir"], wd.expand_data_dir(cell["asset_dir"], self.root))
        self.assertEqual(os.path.dirname(info["file"]), info["dir"])
        self.assertEqual(os.path.getsize(info["file"]), 3)

    def test_plant_is_idempotent_and_leaves_no_temp_file(self) -> None:
        wd.plant_payload(self.root, CONTRACT)
        wd.plant_payload(self.root, CONTRACT)
        leftovers = [name for name in os.listdir(self.root) if name.endswith(".probe-tmp")]
        self.assertEqual(leftovers, [])

    def test_plant_refuses_a_non_server_capability(self) -> None:
        with self.assertRaisesRegex(ValueError, "不是 server"):
            wd.plant_payload(self.root, CONTRACT, capability="文字识别")

    def test_plant_refuses_unknown_capability_or_platform(self) -> None:
        with self.assertRaises(ValueError):
            wd.plant_payload(self.root, CONTRACT, capability="不存在")
        with self.assertRaises(ValueError):
            wd.plant_payload(self.root, CONTRACT, platform="plan9-x64")


class TestDataRootGate(unittest.TestCase):
    def test_linux_and_windows_absolute_paths_pass(self) -> None:
        wd.check_data_root("/home/runner/.local/share/local-ai-studio")
        wd.check_data_root("C:\\Users\\runner\\AppData\\Roaming\\local-ai-studio")
        wd.check_data_root("C:/Users/runner/AppData")

    def test_empty_or_relative_paths_are_rejected(self) -> None:
        for bad in ("", "   ", "local-ai-studio/data", None, 7):
            with self.subTest(bad=bad), self.assertRaises(wd.ProbeError):
                wd.check_data_root(bad)


class TestUiGate(unittest.TestCase):
    def test_rendered_ui_passes(self) -> None:
        wd.check_ui(ui_state(), "/home/runner/.local/share/local-ai-studio")

    def test_visible_boot_banner_is_rejected(self) -> None:
        with self.assertRaisesRegex(wd.ProbeError, "boot"):
            wd.check_ui(ui_state(boot_hidden=False), ui_state()["data_root_text"])

    def test_unrendered_chip_is_rejected(self) -> None:
        with self.assertRaisesRegex(wd.ProbeError, "引擎状态条"):
            wd.check_ui(ui_state(chip="…"), ui_state()["data_root_text"])

    def test_backend_error_text_is_rejected(self) -> None:
        with self.assertRaisesRegex(wd.ProbeError, "引擎状态条"):
            wd.check_ui(ui_state(chip="TypeError: invoke is not a function"), ui_state()["data_root_text"])

    def test_data_root_mismatch_is_rejected(self) -> None:
        with self.assertRaisesRegex(wd.ProbeError, "不一致"):
            wd.check_ui(ui_state(data_root_text="/tmp/other"), "/home/runner/.local/share/local-ai-studio")

    def test_missing_tauri_global_is_rejected(self) -> None:
        with self.assertRaisesRegex(wd.ProbeError, "__TAURI__"):
            wd.check_ui(ui_state(tauri=False), ui_state()["data_root_text"])

    def test_wrong_title_is_rejected(self) -> None:
        with self.assertRaisesRegex(wd.ProbeError, "标题"):
            wd.check_ui(ui_state(title="about:blank"), ui_state()["data_root_text"])

    def test_not_installed_chip_suffix_passes_match(self) -> None:
        state = ui_state(chip="对话 · 运行组件未安装")
        wd.check_ui(state, state["data_root_text"], chip_suffix="运行组件未安装")

    def test_not_installed_chip_suffix_rejects_the_idle_chip(self) -> None:
        state = ui_state(chip="对话 · 未启动")
        with self.assertRaisesRegex(wd.ProbeError, "运行组件未安装"):
            wd.check_ui(state, state["data_root_text"], chip_suffix="运行组件未安装")


class TestPolling(unittest.TestCase):
    def test_transient_probe_errors_are_retried(self) -> None:
        """原生驱动比中间节点晚 listen：第一次连接被关掉时必须重试，而不是当场判死。"""
        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            if calls["n"] < 3:
                raise wd.ProbeError("连接失败：Remote end closed connection without response")
            return True

        self.assertTrue(wd.poll_until(probe, time.monotonic() + 5, "ready", interval=0.001))
        self.assertEqual(calls["n"], 3)

    def test_timeout_reports_the_last_error(self) -> None:
        def probe():
            raise wd.ProbeError("no such window")

        with self.assertRaisesRegex(wd.ProbeError, "no such window"):
            wd.poll_until(probe, time.monotonic() + 0.05, "ready", interval=0.01)


class TestWorkflow(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(WF, encoding="utf-8") as fh:
            cls.steps = yaml.safe_load(fh)["jobs"]["app"]["steps"]

    def step(self, needle: str) -> dict:
        hits = [s for s in self.steps if needle in (s.get("name") or "")]
        self.assertEqual(len(hits), 1, f"{needle!r} 命中 {len(hits)} 步，判据不知道该指哪一步")
        return hits[0]

    def test_ipc_step_is_linux_only_and_after_the_window_step(self) -> None:
        window = self.step("真起一次窗口")
        ipc = self.step("WebDriver")
        self.assertEqual(ipc["if"], "matrix.os == 'ubuntu-latest'")
        self.assertLess(self.steps.index(window), self.steps.index(ipc))
        self.assertLessEqual(ipc["timeout-minutes"], 20)

    def test_ipc_step_installs_the_native_driver_and_pins_tauri_driver(self) -> None:
        run = self.step("WebDriver")["run"]
        for needle in (
            "webkit2gtk-driver",
            'command -v WebKitWebDriver',
            "cargo install tauri-driver --locked --version 2.1.0",
            'xvfb-run -a',
            "$GITHUB_WORKSPACE/tools/wd_ipc_probe.py",
            '--app "$PWD/target/debug/local-ai-studio-shell"',
            '--native-driver "$(command -v WebKitWebDriver)"',
            "--expect-not-installed",
            "--plant-payload 对话",
            "--negative-control",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, run)

    def test_ipc_step_does_not_start_engines_or_touch_the_network(self) -> None:
        run = self.step("WebDriver")["run"]
        for forbidden in ("start_engine", "curl ", "wget "):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, run)


if __name__ == "__main__":
    unittest.main()
