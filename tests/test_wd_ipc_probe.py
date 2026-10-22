"""tools/wd_ipc_probe.py 的判据测试（全部离线）。

真正连窗口的那一步只能在 CI 的 Xvfb 里跑；这里钉住的是它赖以判红判绿的东西：
会话请求的形状、status 的字段与能力集合、data_root 的绝对路径、UI 渲染证据，
以及"车道真的调用了这套判据并带了已知坏对照"。
"""
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
import urllib.request

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
        "env_hidden": False, "env_backend": "CPU 推理 · CPU",
        "env_cpu": "x · 4 核 · 8.0 GiB · linux/x86_64", "env_accel_chips": 1,
        "starter_hidden": False,
        "starter_meta": "lainlives/Qwen3.5-0.8B-uncensored · apache-2.0 · 建议内存 ≥ 1024 MiB",
        "hub_enabled": False,
    }
    base.update(overrides)
    return base


def hw_doc(**overrides):
    base = {
        "os": "linux", "arch": "x86_64",
        "cpu": {"arch": "x86_64", "cores": 4, "brand": "x"},
        "ram_mib": 8192, "accelerators": [],
        "backend": {"id": "cpu", "label": "CPU", "accelerated": False, "reason": "没找到加速器"},
    }
    base.update(overrides)
    return base


def starter_view(**overrides):
    with open(os.path.join(ROOT, "pins", "starter.json"), encoding="utf-8") as fh:
        pin = json.load(fh)
    model = pin["model"]
    base = {
        "installed": False, "id": model["id"], "file": model["file"],
        "bytes": model["bytes"], "size_mib": (model["bytes"] + 1048575) // 1048576,
        "license": model["license"], "ram_mib_min": pin["fit"]["ram_mib_min"], "path": None,
    }
    base.update(overrides)
    return base


def hub_view(**overrides):
    base = {
        "enabled": False, "host": "lain42.top", "ssh_user": "", "ssh_key": "~/.ssh/lain42_ed25519",
        "node_id": "", "ssh_command": "ssh -i ~/.ssh/lain42_ed25519 -o IdentitiesOnly=yes lain42.top",
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


class TestInstallFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.work = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def fixture(self) -> dict:
        return wd.build_install_fixture(self.work, CONTRACT, capability="对话")

    def install_offline(self, fixture: dict, root: str, with_manifest: bool = True) -> None:
        rows = list(fixture["delivery"]["files"])
        if with_manifest:
            rows.append(fixture["delivery"]["manifest"])
        for row in rows:
            src = os.path.join(fixture["server_root"], *row["key"].split("/"))
            dst = os.path.join(root, *row["dest"].split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)

    def test_fixture_comes_from_the_real_stage_and_intake_tools(self) -> None:
        fixture = self.fixture()
        delivery = fixture["delivery"]
        self.assertEqual(delivery["os"], "linux-x64")
        self.assertEqual(len(delivery["files"]), 2)
        self.assertEqual(delivery["manifest"]["land"], "last")
        self.assertEqual(delivery["manifest"]["dest"], "payload-manifest.json")
        self.assertRegex(fixture["manifest_url_path"],
                         r"^/payload/linux-x64/delivery-manifest\.json$")
        for row in list(delivery["files"]) + [delivery["manifest"]]:
            with self.subTest(dest=row["dest"]):
                served = os.path.join(fixture["server_root"], *row["key"].split("/"))
                self.assertTrue(os.path.isfile(served), served)
                self.assertEqual(os.path.getsize(served), row["bytes"])
                staged = os.path.join(fixture["stage_root"], *row["dest"].split("/"))
                with open(staged, "rb") as fh:
                    self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), row["sha256"])
        manifest_path = os.path.join(
            fixture["server_root"], *fixture["manifest_url_path"].lstrip("/").split("/"))
        with open(manifest_path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh), delivery)

    def test_fixture_refuses_unknown_or_non_server_capabilities(self) -> None:
        with self.assertRaisesRegex(ValueError, "不是 server"):
            wd.build_install_fixture(self.work, CONTRACT, capability="文字识别")
        with self.assertRaises(ValueError):
            wd.build_install_fixture(self.work, CONTRACT, capability="不存在")
        with self.assertRaises(ValueError):
            wd.build_install_fixture(self.work, CONTRACT, platform="plan9-x64")

    def test_source_is_written_atomically_and_names_one_base(self) -> None:
        root = os.path.join(self.work, "data")
        os.makedirs(root)
        manifest_url = "http://127.0.0.1:9/payload/linux-x64/delivery-manifest.json"
        path = wd.write_source(root, manifest_url, "http://127.0.0.1:9")
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh),
                             {"manifest_url": manifest_url, "bases": ["http://127.0.0.1:9"]})
        self.assertFalse(os.path.exists(path + ".probe-tmp"))

    def test_source_refuses_a_relative_data_root(self) -> None:
        with self.assertRaises(ValueError):
            wd.write_source("data", "http://127.0.0.1:9/m.json", "http://127.0.0.1:9")

    def test_tree_check_reads_every_object_and_the_root_manifest(self) -> None:
        fixture = self.fixture()
        root = os.path.join(self.work, "data")
        os.makedirs(root)
        self.install_offline(fixture, root)
        self.assertEqual(wd.check_installed_tree(root, fixture["delivery"]), 3)
        victim = os.path.join(root, *fixture["delivery"]["files"][0]["dest"].split("/"))
        with open(victim, "ab") as fh:
            fh.write(b"x")
        with self.assertRaisesRegex(wd.ProbeError, "清单写|sha256"):
            wd.check_installed_tree(root, fixture["delivery"])

    def test_tree_check_needs_the_root_manifest(self) -> None:
        fixture = self.fixture()
        root = os.path.join(self.work, "data")
        os.makedirs(root)
        self.install_offline(fixture, root, with_manifest=False)
        with self.assertRaisesRegex(wd.ProbeError, "缺文件"):
            wd.check_installed_tree(root, fixture["delivery"])

    def test_reinstall_report_requires_zero_written_and_all_skipped(self) -> None:
        wd.check_reinstall_report({"platform": "linux-x64", "written": 0, "skipped": 3}, 2)
        bad = (
            {"platform": "linux-x64", "written": 1, "skipped": 2},
            {"platform": "linux-x64", "written": 0, "skipped": 2},
            {"written": 0, "skipped": 3},
            None,
        )
        for value in bad:
            with self.subTest(value=value), self.assertRaises(wd.ProbeError):
                wd.check_reinstall_report(value, 2)


class TestBundledFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.work = self.tmp.name

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_layout_is_object_keys_plus_a_root_table(self) -> None:
        fixture = wd.build_bundled_fixture(self.work, CONTRACT, capability="对话")
        root = fixture["bundled_root"]
        delivery = fixture["delivery"]
        self.assertTrue(os.path.isfile(os.path.join(root, "delivery-manifest.json")))
        self.assertFalse(os.path.isfile(os.path.join(root, "payload-manifest.json")))
        for row in delivery["files"]:
            self.assertTrue(os.path.isfile(os.path.join(root, *row["key"].split("/"))),
                            row["key"])
        self.assertTrue(os.path.isfile(
            os.path.join(root, *delivery["manifest"]["key"].split("/"))))
        self.assertEqual(fixture["bundle_check"]["files"], len(delivery["files"]))
        self.assertEqual(fixture["bundle_report"]["bytes"],
                         sum(row["bytes"] for row in delivery["files"]))
        self.assertFalse(os.path.exists(os.path.join(self.work, "payload-source.json")))
        self.assertFalse(os.path.exists(os.path.join(self.work, "payload-manifest.json")))

    def test_fixture_refuses_non_server_and_unknown_capabilities(self) -> None:
        with self.assertRaisesRegex(ValueError, "不是 server"):
            wd.build_bundled_fixture(self.work, CONTRACT, capability="文字识别")
        with self.assertRaises(ValueError):
            wd.build_bundled_fixture(self.work, CONTRACT, capability="不存在")


class TestPayloadSourceView(unittest.TestCase):
    def test_bundled_must_not_carry_remote_addresses(self) -> None:
        wd.check_payload_source(
            {"mode": "bundled", "manifest_url": "", "bases": []}, "bundled")
        for bad in (
            {"mode": "bundled", "manifest_url": "https://x", "bases": []},
            {"mode": "bundled", "manifest_url": "", "bases": ["https://x"]},
            {"mode": "remote", "manifest_url": "", "bases": []},
            {"mode": "remote", "manifest_url": "https://x", "bases": ["https://x"]},
        ):
            with self.subTest(bad=bad), self.assertRaises(wd.ProbeError):
                wd.check_payload_source(bad, "bundled")

    def test_remote_needs_a_manifest_url_and_at_least_one_base(self) -> None:
        wd.check_payload_source(
            {"mode": "remote", "manifest_url": "https://x/m.json", "bases": ["https://x"]},
            "remote")
        for bad in (
            None,
            {"mode": "remote", "manifest_url": "", "bases": ["https://x"]},
            {"mode": "remote", "manifest_url": "https://x/m.json", "bases": []},
            {"mode": "bundled", "manifest_url": "https://x/m.json", "bases": ["https://x"]},
            {"mode": "remote", "manifest_url": "https://x/m.json", "bases": [""]},
        ):
            with self.subTest(bad=bad), self.assertRaises(wd.ProbeError):
                wd.check_payload_source(bad, "remote")


class TestFixtureServer(unittest.TestCase):
    def test_serves_the_delivery_manifest_and_every_object_key(self) -> None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with tempfile.TemporaryDirectory() as work:
            fixture = wd.build_install_fixture(work, CONTRACT)
            server, thread = wd.serve_dir(fixture["server_root"])
            try:
                base = wd.base_url(server)
                self.assertTrue(base.startswith("http://127.0.0.1:"))
                with opener.open(base + fixture["manifest_url_path"], timeout=5) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(json.loads(resp.read().decode("utf-8")), fixture["delivery"])
                row = fixture["delivery"]["files"][0]
                with opener.open(base + "/" + row["key"], timeout=5) as resp:
                    body = resp.read()
                self.assertEqual(len(body), row["bytes"])
                self.assertEqual(hashlib.sha256(body).hexdigest(), row["sha256"])
            finally:
                wd.stop_server(server, thread)


class TestSourceName(unittest.TestCase):
    def test_python_constant_matches_the_rust_source_name(self) -> None:
        path = os.path.join(ROOT, "shell", "core", "src", "payload_fetch.rs")
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        self.assertIn('pub const SOURCE_NAME: &str = "payload-source.json";', source)


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


class TestFeatureStrips(unittest.TestCase):
    def test_hardware_starter_and_default_hub_pass(self) -> None:
        expect = wd.starter_expect()
        wd.check_hardware(hw_doc())
        wd.check_starter_view(starter_view(), expect)
        wd.check_hub_default(hub_view())
        wd.check_feature_strips(ui_state(), hw_doc(), expect, fresh=True)

    def test_hardware_and_strip_mutations_are_rejected(self) -> None:
        expect = wd.starter_expect()
        hw_cases = {
            "缺架构": hw_doc(arch=""),
            "核数为 0": hw_doc(cpu={"cores": 0}),
            "backend 缺 label": hw_doc(backend={"accelerated": False}),
            "accelerated 不是布尔": hw_doc(backend={"label": "CPU", "accelerated": "no"}),
            "accelerators 不是数组": hw_doc(accelerators={}),
        }
        for label, doc in hw_cases.items():
            with self.subTest(label=label), self.assertRaises(wd.ProbeError):
                wd.check_hardware(doc)
        starter_cases = {
            "许可与 pin 不符": starter_view(license="gpl-3.0"),
            "内存门槛与 pin 不符": starter_view(ram_mib_min=1),
            "bytes 是 0": starter_view(bytes=0),
            "installed 不是布尔": starter_view(installed="false"),
        }
        for label, doc in starter_cases.items():
            with self.subTest(label=label), self.assertRaises(wd.ProbeError):
                wd.check_starter_view(doc, expect)
        strip_cases = {
            "硬件条被藏起来": ui_state(env_hidden=True),
            "后端标签没画": ui_state(env_backend="GPU 加速"),
            "系统架构没画": ui_state(env_cpu="x · 4 核 · 8.0 GiB"),
            "加速器区是空的": ui_state(env_accel_chips=0),
            "入门档卡片被藏起来": ui_state(starter_hidden=True),
            "卡片没写许可": ui_state(starter_meta="lainlives/Qwen3.5-0.8B-uncensored"),
            "中心服务器默认是开的": ui_state(hub_enabled=True),
        }
        for label, state in strip_cases.items():
            with self.subTest(label=label), self.assertRaises(wd.ProbeError):
                wd.check_feature_strips(state, hw_doc(), expect, fresh=True)

    def test_default_hub_mutations_are_rejected(self) -> None:
        cases = {
            "默认开着": hub_view(enabled=True),
            "主机不对": hub_view(host="evil.example"),
            "命令不是 ssh": hub_view(ssh_command="bash -c whoami"),
            "命令少了 IdentitiesOnly": hub_view(ssh_command="ssh -i ~/.ssh/key lain42.top"),
            "命令没指向中心": hub_view(ssh_command="ssh -i ~/.ssh/key -o IdentitiesOnly=yes other.example"),
            "还没保存就有 node_id": hub_view(node_id="a" * 32),
        }
        for label, view in cases.items():
            with self.subTest(label=label), self.assertRaises(wd.ProbeError):
                wd.check_hub_default(view)


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
            "--install-payload 对话",
            "--negative-control",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, run)

    def test_ipc_step_does_not_start_engines_or_touch_the_network(self) -> None:
        run = self.step("WebDriver")["run"]
        for forbidden in ("start_engine", "curl ", "wget "):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, run)

    def test_bundled_step_uses_a_fresh_data_root_and_no_network(self) -> None:
        step = self.step("内置载荷")
        self.assertEqual(step["if"], "matrix.os == 'ubuntu-latest'")
        self.assertLessEqual(step["timeout-minutes"], 15)
        run = step["run"]
        for needle in (
            'XDG_DATA_HOME="$RUNNER_TEMP/data-bundled"',
            'HOME="$RUNNER_TEMP/home-bundled"',
            "--expect-not-installed",
            "--install-bundled 对话",
            "--negative-control",
            "$GITHUB_WORKSPACE/tools/wd_ipc_probe.py",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, run)
        for forbidden in ("start_engine", "curl ", "wget ", "--install-payload"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, run)


if __name__ == "__main__":
    unittest.main()
