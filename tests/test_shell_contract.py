"""外壳合同：入口、鉴权、生命周期、runtime 目录 —— 全部从四张表推导，不看外壳源码。

这张表存在的理由是把"外壳还没写"与"外壳可以怎么写"分开：
  * 这里钉住的是**外壳必须遵守的约束**（llama 才有钥匙；没钥匙的 server 不许常驻；
    起的正是下的那一颗；目录里带版本），每一条都能指回上游事实或验收条款；
  * 外壳 UI 源码已经存在，所以 `ui_checked` 为 true，并记录扫的是 `shell/ui`；
    不传 `ui_dir` 时仍然必须为 false —— 写了规则不等于扫过了源码。

判据必须会红：改坏四张输入里的任何一张，`build()` 都要报；改坏合同里的任何一个
字段，`check()` 都要报。
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import shell_contract as sc      # noqa: E402

CLI = os.path.join(ROOT, "tools", "shell_contract.py")
ART = os.path.join(ROOT, "pins", "shell-contract.json")
PLAN = os.path.join(ROOT, "pins", "launch-plan.json")
CHOICE = os.path.join(ROOT, "pins", "backend-choice.json")
VERSIONS = os.path.join(ROOT, "pins", "runtime-versions.json")
PAYLOAD = os.path.join(ROOT, "pins", "payload-plan.json")
OCR = os.path.join(ROOT, "pins", "ocr-build-surface.json")
UI = os.path.join(ROOT, "shell", "ui")

ENGINES = {"字幕": "whisper", "对话": "llama", "文字识别": "oar-ocr", "生图": "sd"}


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def inputs() -> dict:
    return {"plan": load(PLAN), "choice": load(CHOICE),
            "versions": load(VERSIONS), "payload": load(PAYLOAD), "ocr": load(OCR)}


def rebuild(ins: dict | None = None) -> tuple[dict, list[str]]:
    i = ins if ins is not None else inputs()
    return sc.build(i["plan"], i["choice"], i["versions"], i["payload"], i["ocr"], ui_dir=UI)


class TestArtifact(unittest.TestCase):
    def test_check_zero(self) -> None:
        _doc, problems = rebuild()
        self.assertEqual(problems, [], problems)

    def test_committed_equals_recompute(self) -> None:
        fresh, _p = rebuild()
        self.assertEqual(sc.load(ART), fresh)

    def test_cli_check_exits_zero(self) -> None:
        r = subprocess.run([sys.executable, CLI, "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("问题 0 条", r.stdout)

    def test_contract_would_deserialize_in_the_rust_shell(self) -> None:
        """`include_str!` 嵌的是这张表，类型漂一个，四个能力在运行时全起不来。"""

        def is_int(v, hi: int) -> bool:
            return isinstance(v, int) and not isinstance(v, bool) and 0 <= v < hi

        def opt_str(v, where: str) -> None:
            self.assertTrue(v is None or isinstance(v, str), f"{where} 类型不对")

        def check(doc: dict) -> None:
            self.assertTrue(is_int(doc.get("schema"), 2 ** 32), "schema 不是 u32")
            caps = doc.get("capabilities")
            self.assertIsInstance(caps, dict, "capabilities 不是表")
            for name, cap in caps.items():
                w = f"capabilities[{name}]"
                self.assertIsInstance(cap, dict, f"{w} 不是表")
                for key in ("engine", "mode"):
                    self.assertIsInstance(cap.get(key), str, f"{w}.{key} 不是字符串")
                opt_str(cap.get("bind"), f"{w}.bind")
                self.assertIsInstance(cap.get("lifecycle"), dict, f"{w}.lifecycle 缺失或不是表")
                self.assertIsInstance(cap["lifecycle"].get("mode"), str,
                                      f"{w}.lifecycle.mode 不是字符串")
                idle = cap["lifecycle"].get("idle_timeout_secs")
                if idle is not None:
                    self.assertTrue(is_int(idle, 2 ** 64),
                                    f"{w}.lifecycle.idle_timeout_secs 不是 u64")
                self.assertIsInstance(cap.get("version"), dict, f"{w}.version 缺失或不是表")
                opt_str(cap["version"].get("pinned"), f"{w}.version.pinned")
                if "auth" in cap:
                    auth = cap["auth"]
                    self.assertIsInstance(auth, dict, f"{w}.auth 不是表")
                    if "mode" in auth:
                        self.assertIsInstance(auth["mode"], str, f"{w}.auth.mode 不是字符串")
                    opt_str(auth.get("flag"), f"{w}.auth.flag")
                    opt_str(auth.get("header"), f"{w}.auth.header")
                    if auth.get("token_bytes") is not None:
                        self.assertTrue(is_int(auth["token_bytes"], 2 ** 64),
                                        f"{w}.auth.token_bytes 不是 usize")
                    if "persisted" in auth:
                        self.assertIsInstance(auth["persisted"], bool,
                                              f"{w}.auth.persisted 不是布尔")
                if cap.get("cors") is not None:
                    self.assertIsInstance(cap["cors"], dict, f"{w}.cors 不是表")
                    for key in ("flag", "value", "upstream_default"):
                        opt_str(cap["cors"].get(key), f"{w}.cors.{key}")
                if cap.get("readiness") is not None:
                    r = cap["readiness"]
                    self.assertIsInstance(r, dict, f"{w}.readiness 不是表")
                    for key in ("method", "path"):
                        self.assertIsInstance(r.get(key), str, f"{w}.readiness.{key} 不是字符串")
                    self.assertTrue(is_int(r.get("expect_status"), 2 ** 16),
                                    f"{w}.readiness.expect_status 不是 u16")
                if cap.get("request") is not None:
                    q = cap["request"]
                    self.assertIsInstance(q, dict, f"{w}.request 不是表")
                    for key in ("method", "path", "multipart_field", "poll"):
                        opt_str(q.get(key), f"{w}.request.{key}")
                    if q.get("expect_status") is not None:
                        self.assertTrue(is_int(q["expect_status"], 2 ** 16),
                                        f"{w}.request.expect_status 不是 u16")
                    if q.get("extra_fields") is not None:
                        self.assertIsInstance(q["extra_fields"], dict,
                                              f"{w}.request.extra_fields 不是表")
                        for k, v in q["extra_fields"].items():
                            self.assertIsInstance(v, str,
                                                  f"{w}.request.extra_fields[{k}] 不是字符串")
                cells = cap.get("os")
                self.assertIsInstance(cells, dict, f"{w}.os 不是表")
                for os_label, cell in cells.items():
                    cw = f"{w}.os[{os_label}]"
                    self.assertIsInstance(cell, dict, f"{cw} 不是表")
                    self.assertIsInstance(cell.get("binary"), str, f"{cw}.binary 不是字符串")
                    if "argv_by_tier" in cell:
                        self.assertIsInstance(cell["argv_by_tier"], dict,
                                              f"{cw}.argv_by_tier 不是表")
                        for tier, argv in cell["argv_by_tier"].items():
                            self.assertIsInstance(argv, list, f"{cw}.argv_by_tier[{tier}] 不是列表")
                            for a in argv:
                                self.assertIsInstance(a, str,
                                                      f"{cw}.argv_by_tier[{tier}] 里有非字符串")
                    if cell.get("argv") is not None:
                        self.assertIsInstance(cell["argv"], list, f"{cw}.argv 不是列表")
                        for a in cell["argv"]:
                            self.assertIsInstance(a, str, f"{cw}.argv 里有非字符串")
                    for key in ("default_asset", "version_dir", "asset_dir"):
                        opt_str(cell.get(key), f"{cw}.{key}")

        contract = load(ART)
        check(contract)
        mutations = {
            "schema 是 true": lambda d: d.__setitem__("schema", True),
            "schema 缺失": lambda d: d.pop("schema"),
            "capabilities 是列表": lambda d: d.__setitem__("capabilities", []),
            "engine 是数字": lambda d: d["capabilities"]["对话"].__setitem__("engine", 5),
            "lifecycle 缺失": lambda d: d["capabilities"]["对话"].pop("lifecycle"),
            "idle 是负数": lambda d: d["capabilities"]["对话"]["lifecycle"].__setitem__(
                "idle_timeout_secs", -1),
            "pinned 是数字": lambda d: d["capabilities"]["对话"]["version"].__setitem__(
                "pinned", 5),
            "token_bytes 是小数": lambda d: d["capabilities"]["对话"]["auth"].__setitem__(
                "token_bytes", 1.5),
            "persisted 是字符串": lambda d: d["capabilities"]["对话"]["auth"].__setitem__(
                "persisted", "false"),
            "binary 是数字": lambda d: d["capabilities"]["对话"]["os"]["win-x64"].__setitem__(
                "binary", 1),
            "argv_by_tier 的值不是列表": lambda d: d["capabilities"]["对话"]["os"]["win-x64"][
                "argv_by_tier"].__setitem__("4GB", "not-a-list"),
            "default_asset 是数字": lambda d: d["capabilities"]["对话"]["os"]["win-x64"].__setitem__(
                "default_asset", 5),
        }
        for label, mutate in mutations.items():
            bad = copy.deepcopy(contract)
            mutate(bad)
            with self.subTest(label=label), self.assertRaises(AssertionError):
                check(bad)

    def test_four_capabilities_one_engine_each(self) -> None:
        caps = sc.load(ART)["capabilities"]
        self.assertEqual({k: v["engine"] for k, v in caps.items()}, ENGINES)

    def test_only_llama_carries_a_key_and_a_narrowed_cors(self) -> None:
        caps = sc.load(ART)["capabilities"]
        self.assertEqual(caps["对话"]["auth"]["mode"], "api-key")
        self.assertEqual(caps["对话"]["auth"]["flag"], "--api-key")
        self.assertEqual(caps["对话"]["auth"]["header"], "Authorization: Bearer {token}")
        self.assertEqual(caps["对话"]["auth"]["token_bytes"], 32)
        self.assertIs(caps["对话"]["auth"]["persisted"], False)
        self.assertEqual(caps["对话"]["cors"],
                         {"flag": "--cors-origins", "value": "localhost",
                          "upstream_default": "*"})
        for cap in ("字幕", "文字识别", "生图"):
            self.assertEqual(caps[cap]["auth"]["mode"], "none", cap)
            self.assertIsNone(caps[cap]["cors"], cap)

    def test_no_auth_means_no_resident_server(self) -> None:
        caps = sc.load(ART)["capabilities"]
        self.assertEqual(caps["对话"]["lifecycle"]["mode"], "resident")
        for cap in ("字幕", "生图"):
            self.assertEqual(caps[cap]["lifecycle"]["mode"], "on_demand", cap)
            self.assertEqual(caps[cap]["lifecycle"]["idle_timeout_secs"], 300, cap)
            self.assertIn("没有鉴权旗标",
                          caps[cap]["lifecycle"]["because"] + "上游没有鉴权旗标", cap)
        self.assertEqual(caps["文字识别"]["lifecycle"]["mode"], "one_shot")

    def test_versions_are_the_axes_own_pins(self) -> None:
        versions = load(VERSIONS)["engines"]
        ocr = load(OCR)["ref"]
        caps = sc.load(ART)["capabilities"]
        for cap, engine in ENGINES.items():
            want = ocr if engine == "oar-ocr" else versions[engine]["pinned"]
            self.assertEqual(caps[cap]["version"]["pinned"], want, cap)

    def test_runtime_dirs_carry_engine_and_version(self) -> None:
        caps = sc.load(ART)["capabilities"]
        for cap, spec in caps.items():
            engine = spec["engine"]
            pin = spec["version"]["pinned"]
            for os_label, cell in spec["os"].items():
                base = f"<data>/runtime/{os_label}/{engine}/{sc.version_dir(pin)}"
                self.assertEqual(cell["version_dir"], base, f"{cap}/{os_label}")
                if cell["default_asset"]:
                    self.assertEqual(cell["asset_dir"],
                                     base + "/" + sc.asset_stem(cell["default_asset"]))
                else:
                    self.assertIsNone(cell["asset_dir"], f"{cap}/{os_label}")

    def test_default_assets_are_the_ones_we_ship_and_seal(self) -> None:
        caps = sc.load(ART)["capabilities"]
        versions, payload = load(VERSIONS)["engines"], load(PAYLOAD)["os"]
        shipped = 0
        for cap, spec in caps.items():
            engine = spec["engine"]
            for os_label, cell in spec["os"].items():
                asset = cell["default_asset"]
                if spec["mode"] == "subprocess":
                    self.assertIsNone(asset, cap)
                    continue
                row = next((r for r in payload[os_label] if r.get("engine") == engine), None)
                self.assertIsNotNone(row, f"{cap}/{os_label}")
                self.assertEqual(asset, row["asset"], f"{cap}/{os_label}")
                body = versions[engine]
                pin = next(r for r in body["releases"] if r["tag"] == body["pinned"])
                names = {a["asset"]: a["sha256"] for a in pin["runtime"]}
                self.assertEqual(str(row["digest"]), "sha256:" + names[asset], f"{cap}/{os_label}")
                shipped += 1
        self.assertEqual(shipped, 8, "三个 server 能力 × 各自的 OS 格 = 8 颗默认档")

    def test_backend_options_come_from_the_choice_table(self) -> None:
        choice = load(CHOICE)["engines"]
        caps = sc.load(ART)["capabilities"]
        for cap, engine in ENGINES.items():
            if cap == "文字识别":
                for cell in caps[cap]["os"].values():
                    self.assertEqual(cell["backends"], {"options": [], "default": None})
                continue
            for os_label, cell in caps[cap]["os"].items():
                want = (choice.get(engine) or {}).get("os", {}).get(os_label) or {}
                opts = want.get("options") or []
                self.assertEqual([o["asset"] for o in cell["backends"]["options"]],
                                 [o["asset"] for o in opts], f"{cap}/{os_label}")
                self.assertEqual(cell["backends"]["default"],
                                 next((o["asset"] for o in opts if o["is_default"]), None))

    def test_subtitle_request_fields_come_from_the_table(self) -> None:
        caps = sc.load(ART)["capabilities"]
        self.assertEqual(caps["字幕"]["request"]["multipart_field"], "file")
        self.assertEqual(caps["字幕"]["request"]["extra_fields"],
                         {"response_format": "srt"})
        for cap in ("对话", "生图", "文字识别"):
            req = caps[cap]["request"] or {}
            self.assertNotIn("extra_fields", req, cap)

    def test_without_a_ui_dir_nothing_is_claimed(self) -> None:
        fresh, _p = sc.build(load(PLAN), load(CHOICE), load(VERSIONS), load(PAYLOAD), load(OCR))
        self.assertIs(fresh["ui_checked"], False)
        self.assertIsNone(fresh["ui_dir"])
        self.assertIn("127.0.0.1", sc.UI_FORBIDDEN)
        self.assertIn("fetch(", sc.UI_FORBIDDEN)

    def test_the_real_ui_dir_is_scanned_and_recorded(self) -> None:
        doc = sc.load(ART)
        self.assertIs(doc["ui_checked"], True)
        self.assertEqual(doc["ui_dir"], "shell/ui")
        self.assertTrue(os.path.isdir(UI))

    def test_known_gap_is_recorded_not_erased(self) -> None:
        gaps = sc.load(ART)["gaps"]
        self.assertEqual([g["id"] for g in gaps], ["ocr-manifest"])
        self.assertIn("payload-manifest.json", gaps[0]["what"])


class TestSelftest(unittest.TestCase):
    def test_selftest_exit_zero(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(sc.selftest(), 0)
        self.assertIn("BAD 数量 = 0", buf.getvalue())


class TestSourceMutants(unittest.TestCase):
    @staticmethod
    def src() -> str:
        with open(CLI, encoding="utf-8") as fh:
            return fh.read()

    MUTANTS = [
        ("whisper 文件字段被改名不报",
         '        if cap in MULTIPART_FIELD and req.get("multipart_field") != MULTIPART_FIELD[cap]:',
         '        if False and cap in MULTIPART_FIELD and req.get("multipart_field") != MULTIPART_FIELD[cap]:'),
        ("有钥匙却没收窄 CORS 不报",
         '        if has_key and not cors:',
         '        if False and has_key and not cors:'),
        ("无钥匙 server 常驻不报",
         '            if life["mode"] == "resident":',
         '            if False and life["mode"] == "resident":'),
        ("行里没带 --api-key 不报",
         '                    if "--api-key" not in row or "{token}" not in row:',
         '                    if False and ("--api-key" not in row or "{token}" not in row):'),
        ("没钥匙却带 --api-key 不报",
         '                    if "--api-key" in row or "api_key" in " ".join(row):',
         '                    if False and ("--api-key" in row or "api_key" in " ".join(row)):'),
        ("默认档不在钉住的 release 不报",
         "            if asset not in names:",
         "            if False and asset not in names:"),
        ("默认档 digest 与版本轴不一致不报",
         "            if digest.startswith(\"sha256:\") and digest[7:] != names[asset]:",
         "            if False and digest.startswith(\"sha256:\") and digest[7:] != names[asset]:"),
        ("OCR 的 ref 不是 commit 不报",
         '            if not re.fullmatch(r"[0-9a-f]{7,40}", ref):',
         '            if False and not re.fullmatch(r"[0-9a-f]{7,40}", ref):'),
        ("check 不看鉴权",
         '        for key in ("engine", "mode", "bind", "auth", "cors", "lifecycle", "install_stage",',
         '        for key in ("engine", "mode", "bind", "cors", "lifecycle", "install_stage",'),
        ("check 不看缺口清单",
         '    if committed.get("gaps") != fresh.get("gaps"):',
         '    if False and committed.get("gaps") != fresh.get("gaps"):'),
        ("check 不看 UI 扫描位置",
         '    if committed.get("ui_dir") != fresh.get("ui_dir"):',
         '    if False and committed.get("ui_dir") != fresh.get("ui_dir"):'),
        ("ui_checked 恒真",
         "    ui_checked = bool(ui_dir) and os.path.isdir(ui_dir)",
         "    ui_checked = True"),
        ("runtime 目录不清洗标签",
         '    return re.sub(r"[^A-Za-z0-9._-]", "_", tag)',
         "    return tag"),
        ("runtime 目录不写版本",
         '                "version_dir": f"<data>/runtime/{os_label}/{engine}/{version_dir(vbody[\'pinned\'])}",',
         '                "version_dir": f"<data>/runtime/{os_label}/{engine}/latest",'),
    ]

    def load(self, text: str):
        mod = types.ModuleType("shell_contract_mutant")
        mod.__dict__["__file__"] = CLI
        mod.__dict__["__name__"] = "shell_contract_mutant"
        exec(compile(text, CLI, "exec"), mod.__dict__)
        return mod

    def probes(self, mod) -> dict:
        out = {}
        ins = {k: copy.deepcopy(v) for k, v in inputs().items()}
        committed = load(ART)

        def build(i=None):
            i = i or ins
            return mod.build(i["plan"], i["choice"], i["versions"], i["payload"], i["ocr"],
                             ui_dir=UI)

        def red(i=None) -> bool:
            return bool(build(i)[1])

        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out["a 离线对照"] = mod.selftest() == 0
        except Exception:                                    # noqa: BLE001
            out["a 离线对照"] = False
        try:
            fresh, problems = build()
            out["b 重算与漂移"] = problems == [] and mod.check(committed, fresh) == []
            caps = fresh["capabilities"]
            out["c 只有 llama 有钥匙"] = (
                caps["对话"]["auth"]["mode"] == "api-key"
                and all(caps[c]["auth"]["mode"] == "none" for c in ("字幕", "文字识别", "生图")))
            out["d 无钥匙不许常驻"] = (
                caps["对话"]["lifecycle"]["mode"] == "resident"
                and caps["字幕"]["lifecycle"] == {
                    "mode": "on_demand", "idle_timeout_secs": 300,
                    "because": mod.LIFECYCLE["字幕"]["because"]}
                and caps["生图"]["lifecycle"]["mode"] == "on_demand"
                and caps["文字识别"]["lifecycle"]["mode"] == "one_shot")
            out["e 版本钉在轴上"] = (
                caps["对话"]["version"]["pinned"] == "b11804"
                and caps["字幕"]["version"]["pinned"] == "b5454"
                and caps["文字识别"]["version"]["pinned"] == ins["ocr"]["ref"])
            okdir = True
            for cap, spec in caps.items():
                for os_label, cell in spec["os"].items():
                    base = (f"<data>/runtime/{os_label}/{spec['engine']}/"
                            f"{mod.version_dir(spec['version']['pinned'])}")
                    okdir = okdir and cell["version_dir"] == base
            out["f runtime 目录带版本"] = okdir
            shipped = [cell for spec in caps.values() for cell in spec["os"].values()
                       if cell["default_asset"]]
            out["g 默认档 8 颗且带哈希"] = len(shipped) == 8 and all(
                cell["asset_dir"].endswith(mod.asset_stem(cell["default_asset"]))
                for cell in shipped)
            no_ui, _p2 = mod.build(ins["plan"], ins["choice"], ins["versions"],
                                   ins["payload"], ins["ocr"])
            out["h UI 没扫就不算扫过"] = (
                no_ui["ui_checked"] is False and no_ui["ui_dir"] is None
                and "127.0.0.1" in mod.UI_FORBIDDEN)
            out["i 缺口如实记录"] = [g["id"] for g in fresh["gaps"]] == ["ocr-manifest"]
            c1 = copy.deepcopy(committed)
            c1["capabilities"]["对话"]["auth"]["mode"] = "none"
            out["j 鉴权漂移被叫"] = bool(mod.check(committed, c1))
            c2 = copy.deepcopy(committed)
            c2["capabilities"]["生图"]["os"]["win-x64"]["binary"] = "sd-cli.exe"
            out["k 运行文件漂移被叫"] = bool(mod.check(committed, c2))
            c3 = copy.deepcopy(committed)
            c3["gaps"] = []
            out["l 缺口被删被叫"] = bool(mod.check(committed, c3))
            c4 = copy.deepcopy(committed)
            c4["ui_dir"] = "shell/另一个-ui"
            out["l2 UI 扫描位置漂移被叫"] = bool(mod.check(committed, c4))

            i1 = copy.deepcopy(ins)
            i1["plan"]["capabilities"]["字幕"]["engine_auth"]["engine_supports_api_key"] = True
            out["m whisper 有钥匙被报"] = red(i1)
            i2 = copy.deepcopy(ins)
            i2["plan"]["capabilities"]["对话"]["cors"] = None
            out["n llama 丢 CORS 被报"] = red(i2)
            i3 = copy.deepcopy(ins)
            i3["ocr"]["ref"] = "main"
            out["o OCR ref 不是 commit 被报"] = red(i3)
            i4 = copy.deepcopy(ins)
            i4["payload"]["os"]["win-x64"][0]["digest"] = "sha256:" + "0" * 64
            out["p 默认档 digest 不一致被报"] = red(i4)
            i5 = copy.deepcopy(ins)
            i5["payload"]["os"]["win-x64"][0]["asset"] = "llama-另一个.zip"
            out["q 默认档不在 release 被报"] = red(i5)
            i6 = copy.deepcopy(ins)
            row = next(k for k in i6["plan"]["capabilities"]["字幕"]["by_tier"]
                       if k.endswith("/win-x64"))
            i6["plan"]["capabilities"]["字幕"]["by_tier"][row].append("--cors-origins")
            out["r 没这条旗标却带上被报"] = red(i6)
            i8 = copy.deepcopy(ins)
            row = next(k for k in i8["plan"]["capabilities"]["字幕"]["by_tier"]
                       if k.endswith("/win-x64"))
            i8["plan"]["capabilities"]["字幕"]["by_tier"][row].append("--api-key")
            out["r2 没钥匙却带 --api-key 被报"] = red(i8)
            i7 = copy.deepcopy(ins)
            row = next(k for k in i7["plan"]["capabilities"]["对话"]["by_tier"]
                       if k.endswith("/win-x64"))
            i7["plan"]["capabilities"]["对话"]["by_tier"][row] = [
                x for x in i7["plan"]["capabilities"]["对话"]["by_tier"][row]
                if x not in ("--api-key", "{token}")]
            out["s llama 行没带钥匙被报"] = red(i7)
            old = mod.LIFECYCLE["字幕"]
            mod.LIFECYCLE["字幕"] = {"mode": "resident", "idle_timeout_secs": 300,
                                     "because": old["because"]}
            try:
                out["t 无钥匙常驻被报"] = red()
            finally:
                mod.LIFECYCLE["字幕"] = old
            out["u 标签里的斜杠被清洗"] = mod.version_dir("a/b") == "a_b"
            out["v 后端选项来自选择表"] = (
                len(caps["对话"]["os"]["win-x64"]["backends"]["options"]) > 0
                and caps["文字识别"]["os"]["win-x64"]["backends"]["options"] == [])
        except Exception as exc:                             # noqa: BLE001
            out["_b 异常"] = str(exc)[:120]
        return out

    EXPECTED = {
        "a 离线对照": True, "b 重算与漂移": True, "c 只有 llama 有钥匙": True,
        "d 无钥匙不许常驻": True, "e 版本钉在轴上": True,
        "f runtime 目录带版本": True, "g 默认档 8 颗且带哈希": True,
        "h UI 没扫就不算扫过": True, "i 缺口如实记录": True,
        "j 鉴权漂移被叫": True, "k 运行文件漂移被叫": True, "l 缺口被删被叫": True,
        "l2 UI 扫描位置漂移被叫": True,
        "m whisper 有钥匙被报": True, "n llama 丢 CORS 被报": True,
        "o OCR ref 不是 commit 被报": True, "p 默认档 digest 不一致被报": True,
        "q 默认档不在 release 被报": True, "r 没这条旗标却带上被报": True,
        "r2 没钥匙却带 --api-key 被报": True,
        "s llama 行没带钥匙被报": True, "t 无钥匙常驻被报": True,
        "u 标签里的斜杠被清洗": True, "v 后端选项来自选择表": True,
    }

    def test_baseline_probes_all_hold(self) -> None:
        self.assertEqual({k: v for k, v in self.probes(sc).items()
                          if not k.startswith("_")}, self.EXPECTED)

    def test_every_mutant_is_caught(self) -> None:
        missed = []
        for name, old, new in self.MUTANTS:
            self.assertEqual(self.src().count(old), 1, f"锚点不唯一或已漂：{name}")
            got = {k: v for k, v in self.probes(
                self.load(self.src().replace(old, new, 1))).items()
                if not k.startswith("_")}
            if all(got.get(k, False) for k in self.EXPECTED):
                missed.append(f"{name} → {got}")
        self.assertEqual(missed, [], "这些变异没人报：对应判据是装饰\n" + "\n".join(missed))

    def test_mutant_count(self) -> None:
        self.assertEqual(len(self.MUTANTS), 14)


if __name__ == "__main__":
    unittest.main()
