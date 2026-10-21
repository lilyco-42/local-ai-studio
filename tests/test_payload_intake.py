"""摆好的数据根 → 首启逐文件交付清单：这一步会静默错的四件事。

  * 落点/对象键不是从合同派生的（改合同，清单必须跟着改）；
  * 清单里的哈希与目录里的字节对不上，却照样生成"可交付"的表；
  * 两个引擎指到同一个目标，或者键前缀跑出桶命名空间；
  * 输出里混进 runner 的绝对路径 —— 两台机器摆同一棵树会得到两份不同的"交付物"。
"""
from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import payload_intake as pi  # noqa: E402
import stage_payload as sp  # noqa: E402

CLI = os.path.join(ROOT, "tools", "payload_intake.py")
CONTRACT = os.path.join(ROOT, "pins", "shell-contract.json")


def write_contract(root: str, caps: dict) -> str:
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, "contract.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"capabilities": caps}, fh)
    return path


def write_pkg(root: str, engines: list[dict], os_label: str = "win-x64") -> str:
    pkg = os.path.join(root, "pkg")
    rows = []
    for spec in engines:
        engine = spec["engine"]
        engine_dir = os.path.join(pkg, engine)
        os.makedirs(engine_dir, exist_ok=True)
        files = []
        for name, payload in spec["files"].items():
            with open(os.path.join(engine_dir, name), "wb") as fh:
                fh.write(payload)
            files.append({"file": name, "bytes": len(payload),
                          "sha256": hashlib.sha256(payload).hexdigest()})
        rows.append({"engine": engine, "status": "ok",
                     "asset": spec.get("asset"), "files": files})
    with open(os.path.join(pkg, sp.MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump({"os": os_label, "engines": rows}, fh)
    return pkg


def two_engines() -> dict:
    return {
        "对话": {"engine": "llama", "os": {"win-x64": {
            "default_asset": "llama.zip",
            "asset_dir": "<data>/runtime/win-x64/llama/b1/llama-pkg"}}},
        "生图": {"engine": "sd", "os": {"win-x64": {
            "default_asset": "sd.zip",
            "asset_dir": "<data>/runtime/win-x64/sd/m1/sd-pkg"}}},
    }


def llama_pkg() -> list[dict]:
    return [{"engine": "llama", "asset": "llama.zip",
             "files": {"llama-server.exe": b"bin", "llama-impl.dll": b"dll"}}]


def stage_fixture(root: str, caps: dict | None = None,
                  engines: list[dict] | None = None) -> tuple[str, str, str]:
    contract = write_contract(root, caps or two_engines())
    pkg = write_pkg(root, engines or llama_pkg())
    data = os.path.join(root, "data")
    sp.stage(pkg, data, "win-x64", contract)
    return data, contract, pkg


class TestBuild(unittest.TestCase):
    def test_selftest_passes(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = pi.selftest()
        self.assertEqual(rc, 0, buf.getvalue())

    def test_real_contract_whisper_key_is_derived_not_typed(self) -> None:
        with open(CONTRACT, encoding="utf-8") as fh:
            contract = json.load(fh)
        row = sp.targets_for(contract, "win-x64")["whisper"]
        with tempfile.TemporaryDirectory() as d:
            pkg = write_pkg(d, [{"engine": "whisper", "asset": row["asset"],
                                 "files": {"whisper-server.exe": b"srv",
                                           "whisper-cli.exe": b"cli"}}])
            data = os.path.join(d, "data")
            sp.stage(pkg, data, "win-x64")
            rep = pi.build(data, "win-x64")
            dest_dir = os.path.relpath(
                sp.expand_dir(row["template"], data), data).replace(os.sep, "/")
            self.assertEqual({f["dest"] for f in rep["files"]},
                             {f"{dest_dir}/whisper-server.exe",
                              f"{dest_dir}/whisper-cli.exe"})
            for f in rep["files"]:
                self.assertEqual(f["key"], "payload/win-x64/" + f["dest"])
            self.assertEqual(rep["manifest"]["key"],
                             "payload/win-x64/payload-manifest.json")
            self.assertEqual(rep["manifest"]["land"], "last")
            with open(os.path.join(data, sp.MANIFEST_NAME), "rb") as fh:
                blob = fh.read()
            self.assertEqual(rep["manifest"]["bytes"], len(blob))
            self.assertEqual(rep["manifest"]["sha256"],
                             hashlib.sha256(blob).hexdigest())
            targets = sp.targets_for(contract, "win-x64")
            self.assertEqual(set(rep["not_in_manifest"]),
                             {r["capability"] + "/" + e
                              for e, r in targets.items() if e != "whisper"})
            self.assertIn("文字识别/oar-ocr", rep["not_in_manifest"])

    def test_contract_change_moves_dest_and_key(self) -> None:
        caps = two_engines()
        caps["对话"]["os"]["win-x64"]["asset_dir"] = "<data>/runtime/custom/llama"
        with tempfile.TemporaryDirectory() as d:
            data, contract, _pkg = stage_fixture(d, caps)
            rep = pi.build(data, "win-x64", contract)
            self.assertEqual(rep["files"][0]["dest"],
                             "runtime/custom/llama/llama-impl.dll")
            self.assertEqual(rep["files"][0]["key"],
                             "payload/win-x64/runtime/custom/llama/llama-impl.dll")

    def test_output_is_identical_across_roots_and_has_no_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            a_data, a_contract, _ = stage_fixture(os.path.join(d, "a"))
            b_data, b_contract, _ = stage_fixture(os.path.join(d, "b"))
            pa, pb = os.path.join(d, "a.json"), os.path.join(d, "b.json")
            pi.write(pi.build(a_data, "win-x64", a_contract), pa)
            pi.write(pi.build(b_data, "win-x64", b_contract), pb)
            with open(pa, "rb") as fh:
                a = fh.read()
            with open(pb, "rb") as fh:
                b = fh.read()
            self.assertEqual(a, b)
            self.assertNotIn(os.path.abspath(d).encode(), a)
            self.assertNotIn(b"\\", a)

    def test_every_engine_is_listed_with_its_own_totals(self) -> None:
        engines = llama_pkg() + [{"engine": "sd", "asset": "sd.zip",
                                  "files": {"sd-server.exe": b"srv"}}]
        with tempfile.TemporaryDirectory() as d:
            data, contract, _ = stage_fixture(d, engines=engines)
            rep = pi.build(data, "win-x64", contract)
            self.assertEqual(set(rep["totals"]["by_engine"]), {"llama", "sd"})
            self.assertEqual(rep["totals"]["files"], 3)
            self.assertEqual(rep["totals"]["bytes"], 6 + 3)
            self.assertEqual(rep["not_in_manifest"], [])
            self.assertEqual(rep["totals"]["bytes"], sum(f["bytes"] for f in rep["files"]))

    def test_files_are_sorted_and_keys_are_unique(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            data, contract, _ = stage_fixture(d)
            rep = pi.build(data, "win-x64", contract)
            dests = [f["dest"] for f in rep["files"]]
            keys = [f["key"] for f in rep["files"]]
            self.assertEqual(dests, sorted(dests))
            self.assertEqual(len(dests), len(set(dests)))
            self.assertEqual(len(keys), len(set(keys)))
            self.assertEqual(rep["totals"]["bytes"],
                             sum(f["bytes"] for f in rep["files"]))

    def test_check_accepts_a_fresh_manifest_and_rejects_edits(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            data, contract, _ = stage_fixture(d)
            out = os.path.join(d, "intake.json")
            pi.write(pi.build(data, "win-x64", contract), out)
            self.assertEqual(pi.check(data, "win-x64", out, contract)["os"], "win-x64")
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
            for label, mutate in (
                ("改哈希", lambda x: x["files"][0].__setitem__("sha256", "0" * 64)),
                ("改落点", lambda x: x["files"][0].__setitem__("dest", "elsewhere")),
                ("加字段", lambda x: x.__setitem__("extra", 1)),
                ("清单不再最后落", lambda x: x["manifest"].__setitem__("land", "first")),
            ):
                bad = json.loads(json.dumps(doc))
                mutate(bad)
                path = os.path.join(d, "bad.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(bad, fh)
                with self.subTest(label=label), self.assertRaises(sp.StageError):
                    pi.check(data, "win-x64", path, contract)


class TestNegative(unittest.TestCase):
    def test_tamper_extra_and_missing_are_red(self) -> None:
        def target(root: str, name: str) -> str:
            return os.path.join(root, "runtime", "win-x64", "llama", "b1",
                                "llama-pkg", name)

        def append(root: str) -> None:
            with open(target(root, "llama-server.exe"), "ab") as fh:
                fh.write(b"x")

        def ghost(root: str) -> None:
            with open(target(root, "ghost.dll"), "wb") as fh:
                fh.write(b"x")

        cases = {"tamper": append, "ghost": ghost,
                 "missing": lambda root: os.unlink(target(root, "llama-impl.dll"))}
        for label, mutate in cases.items():
            with tempfile.TemporaryDirectory() as d:
                data, contract, _ = stage_fixture(d)
                mutate(data)
                with self.subTest(label=label), self.assertRaises(sp.StageError):
                    pi.build(data, "win-x64", contract)

    def test_manifest_fields_must_match(self) -> None:
        for field, value in (("os", "linux-x64"), ("asset", "other.zip"),
                             ("engine", "ghost")):
            with tempfile.TemporaryDirectory() as d:
                data, contract, _ = stage_fixture(d)
                mpath = os.path.join(data, sp.MANIFEST_NAME)
                with open(mpath, encoding="utf-8") as fh:
                    doc = json.load(fh)
                if field == "os":
                    doc["os"] = value
                else:
                    doc["engines"][0][field] = value
                with open(mpath, "w", encoding="utf-8") as fh:
                    json.dump(doc, fh)
                with self.subTest(field=field), self.assertRaises(sp.StageError):
                    pi.build(data, "win-x64", contract)

    def test_manifest_absence_and_bad_json_are_red(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            data, contract, _ = stage_fixture(d)
            path = os.path.join(data, sp.MANIFEST_NAME)
            os.unlink(path)
            with self.assertRaises(sp.StageError):
                pi.build(data, "win-x64", contract)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("{not json")
            with self.assertRaises(sp.StageError):
                pi.build(data, "win-x64", contract)

    def test_prefix_and_os_labels_are_one_safe_segment(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            data, contract, _ = stage_fixture(d)
            for prefix in ("../evil", "a//b", "a/./b", "a\\b", ""):
                with self.subTest(prefix=prefix), self.assertRaises(sp.StageError):
                    pi.build(data, "win-x64", contract, prefix=prefix)
            for os_label in ("../win", "win/x", "..", ""):
                with self.subTest(os_label=os_label), self.assertRaises(sp.StageError):
                    pi.build(data, os_label, contract)

    def test_two_engines_on_one_target_are_red(self) -> None:
        caps = {
            "对话": {"engine": "llama", "os": {"win-x64": {
                "default_asset": "same.zip", "asset_dir": "<data>/runtime/shared"}}},
            "生图": {"engine": "sd", "os": {"win-x64": {
                "default_asset": "same.zip", "asset_dir": "<data>/runtime/shared"}}},
        }
        engines = [{"engine": "llama", "asset": "same.zip", "files": {"server.bin": b"x"}},
                   {"engine": "sd", "asset": "same.zip", "files": {"server.bin": b"x"}}]
        with tempfile.TemporaryDirectory() as d:
            data, contract, _ = stage_fixture(d, caps, engines)
            with self.assertRaises(sp.StageError):
                pi.build(data, "win-x64", contract)


class TestCli(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, CLI, *args], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

    def test_generate_then_check(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            data, contract, _ = stage_fixture(d)
            out = os.path.join(d, "intake.json")
            r = self.run_cli("--data", data, "--os", "win-x64", "--out", out,
                             "--contract", contract)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertTrue(os.path.isfile(out))
            r = self.run_cli("--check", "--data", data, "--os", "win-x64", "--out", out,
                             "--contract", contract)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("复核通过", r.stdout)
            with open(out, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["files"][0]["bytes"] = 999
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            r = self.run_cli("--check", "--data", data, "--os", "win-x64", "--out", out,
                             "--contract", contract)
            self.assertEqual(r.returncode, 1)
            self.assertIn("BAD", r.stdout)

    def test_selftest_green(self) -> None:
        r = self.run_cli("--selftest")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


def _safe_rel_dest(dest) -> bool:
    if not isinstance(dest, str) or not dest or dest in (".", ".."):
        return False
    if dest.startswith("/") or "\\" in dest or "\0" in dest:
        return False
    if os.path.splitdrive(dest)[0]:
        return False
    return all(part not in ("", ".", "..") for part in dest.split("/"))


def _sha_ok(value) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def consumer_land(intake: dict, objects: dict[str, bytes], root: str) -> None:
    """消费端契约的可执行规格（测试用，不是交付代码）。

    只拿 `(key, dest, sha256, bytes)` 与根清单行，把对象字典还原成数据根：
    先验完全部对象再写任何文件，根清单最后写；清单本身不可信，越界落点、
    重复落点、与根清单撞名都在动盘之前拒绝。真正的消费者是
    `supervisor::verify_payload`；这里证明交付清单本身够用。
    """
    if not isinstance(intake, dict) or intake.get("schema") != 1:
        raise sp.StageError("交付清单 schema 不是 1")
    prefix = intake.get("key_prefix")
    if (not isinstance(prefix, str) or not prefix or "\\" in prefix
            or any(part in ("", ".", "..") for part in prefix.split("/"))):
        raise sp.StageError(f"key_prefix {prefix!r} 不安全")
    rows = intake.get("files")
    manifest = intake.get("manifest")
    if not isinstance(rows, list) or not isinstance(manifest, dict):
        raise sp.StageError("交付清单缺 files 或 manifest")
    md_dest = manifest.get("dest")
    if (not isinstance(md_dest, str) or md_dest in ("", ".", "..")
            or "/" in md_dest or "\\" in md_dest):
        raise sp.StageError(f"根清单落点 {md_dest!r} 不是平铺文件名")
    if manifest.get("key") != f"{prefix}/{md_dest}":
        raise sp.StageError("根清单的 key 与 key_prefix 不符")
    if manifest.get("land") != "last":
        raise sp.StageError("根清单没有声明最后落")

    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise sp.StageError("files 里有不是对象的行")
        dest, key = row.get("dest"), row.get("key")
        if not _safe_rel_dest(dest):
            raise sp.StageError(f"落点 {dest!r} 不安全")
        if dest in seen:
            raise sp.StageError(f"{dest}: 两条清单指到同一个落点")
        seen.add(dest)
        if dest == md_dest:
            raise sp.StageError(f"{dest}: 与根清单撞名")
        if key != f"{prefix}/{dest}":
            raise sp.StageError(f"{dest}: key 与落点不符")
        if (not isinstance(row.get("bytes"), int) or isinstance(row.get("bytes"), bool)
                or row["bytes"] <= 0):
            raise sp.StageError(f"{dest}: bytes 不是正整数")
        if not _sha_ok(row.get("sha256")):
            raise sp.StageError(f"{dest}: sha256 不是 64 位小写十六进制")

    need = [row["key"] for row in rows] + [manifest["key"]]
    missing = [key for key in need if key not in objects]
    if missing:
        raise sp.StageError(f"对象缺失: {missing[:3]}")
    for row in rows:
        blob = objects[row["key"]]
        if len(blob) != row["bytes"] or hashlib.sha256(blob).hexdigest() != row["sha256"]:
            raise sp.StageError(f"{row['dest']}: 对象字节或哈希不符")
    root_blob = objects[manifest["key"]]
    if (len(root_blob) != manifest.get("bytes")
            or hashlib.sha256(root_blob).hexdigest() != manifest.get("sha256")):
        raise sp.StageError("根清单对象字节或哈希不符")

    os.makedirs(root, exist_ok=True)
    for row in rows:
        path = os.path.join(root, *row["dest"].split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(objects[row["key"]])
    with open(os.path.join(root, md_dest), "wb") as fh:
        fh.write(root_blob)


def shell_verify(root: str, contract_path: str, os_label: str) -> set[str]:
    """按 `supervisor::verify_payload` 的顺序验：根清单 → 合同目录 → 逐文件。"""
    with open(os.path.join(root, sp.MANIFEST_NAME), encoding="utf-8") as fh:
        doc = json.load(fh)
    if doc.get("os") != os_label:
        raise sp.StageError(f"根清单是 {doc.get('os')!r} 的，要验的是 {os_label!r}")
    with open(contract_path, encoding="utf-8") as fh:
        contract = json.load(fh)
    targets = sp.targets_for(contract, os_label)
    seen: set[str] = set()
    for entry in doc.get("engines") or []:
        engine = entry.get("engine")
        row = targets.get(engine)
        if row is None:
            raise sp.StageError(f"{engine}: 外壳合同里没有这个引擎的 runtime 目录")
        if row["asset"] is not None and entry.get("asset") != row["asset"]:
            raise sp.StageError(f"{engine}: 根清单的产物与合同默认不符")
        sp._verify_tree(entry, sp.expand_dir(row["template"], root))
        seen.add(engine)
    return seen


class TestConsumerReplay(unittest.TestCase):
    """交付清单够不够用：删掉原数据根，只凭对象字典还原，外壳必须愿意起。

    这里不再测生成器，而是把消费端契约写成可执行规格：对象先验完再动盘、
    根清单最后写；对象坏了、缺了或清单被手改成越界/重复落点时，根上不许出现清单。
    """

    def fixture(self, d: str) -> tuple[str, dict, dict[str, bytes], str]:
        data, contract, _ = stage_fixture(d)
        intake = pi.build(data, "win-x64", contract)
        objects: dict[str, bytes] = {}
        for row in intake["files"]:
            path = os.path.join(data, *row["dest"].split("/"))
            with open(path, "rb") as fh:
                objects[row["key"]] = fh.read()
        with open(os.path.join(data, sp.MANIFEST_NAME), "rb") as fh:
            objects[intake["manifest"]["key"]] = fh.read()
        return data, intake, objects, contract

    def test_objects_rebuild_a_shell_verifiable_root(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            data, intake, objects, contract = self.fixture(d)
            shutil.rmtree(data)          # 原树不在了，还原只能靠清单与对象字典
            root = os.path.join(d, "restored")
            consumer_land(intake, objects, root)
            self.assertEqual(shell_verify(root, contract, "win-x64"), {"llama"})
            self.assertEqual(pi.build(root, "win-x64", contract), intake)

    def test_bad_objects_leave_no_root_manifest(self) -> None:
        def corrupt(objs: dict, doc: dict) -> None:
            row = doc["files"][0]
            objs[row["key"]] = b"X" * row["bytes"]   # 同长度、错内容

        cases = {
            "文件对象被改": (corrupt, "对象字节或哈希不符"),
            "文件对象缺失": (lambda objs, doc: objs.pop(doc["files"][0]["key"]), "对象缺失"),
            "根清单对象被改": (lambda objs, doc: objs.__setitem__(
                doc["manifest"]["key"], b"{}"), "根清单对象字节或哈希不符"),
        }
        for label, (mutate, needle) in cases.items():
            with tempfile.TemporaryDirectory() as d:
                _data, intake, objects, _contract = self.fixture(d)
                mutate(objects, intake)
                root = os.path.join(d, "restored")
                with self.subTest(label=label), self.assertRaises(sp.StageError) as caught:
                    consumer_land(intake, objects, root)
                self.assertIn(needle, str(caught.exception))
                self.assertFalse(os.path.exists(os.path.join(root, sp.MANIFEST_NAME)))

    def test_untrusted_manifest_cannot_escape_or_collide(self) -> None:
        def collide(doc: dict) -> None:
            doc["files"][0]["dest"] = sp.MANIFEST_NAME
            doc["files"][0]["key"] = doc["key_prefix"] + "/" + doc["files"][0]["dest"]

        cases = {
            "落点穿越": (lambda doc: doc["files"][0].__setitem__("dest", "../evil"),
                         "不安全"),
            "落点重复": (lambda doc: doc["files"].append(
                copy.deepcopy(doc["files"][0])), "同一个落点"),
            "键与落点不符": (lambda doc: doc["files"][0].__setitem__(
                "key", doc["files"][0]["key"] + "x"), "key 与落点不符"),
            "与根清单撞名": (collide, "撞名"),
            "根清单不再最后落": (lambda doc: doc["manifest"].__setitem__("land", "first"),
                                 "最后落"),
        }
        for label, (mutate, needle) in cases.items():
            with tempfile.TemporaryDirectory() as d:
                _data, intake, objects, _contract = self.fixture(d)
                bad = copy.deepcopy(intake)
                mutate(bad)
                root = os.path.join(d, "restored")
                with self.subTest(label=label), self.assertRaises(sp.StageError) as caught:
                    consumer_land(bad, objects, root)
                self.assertIn(needle, str(caught.exception))
                self.assertFalse(os.path.exists(os.path.join(root, sp.MANIFEST_NAME)))
                self.assertFalse(os.path.exists(os.path.join(d, "evil")))


MUTANTS = [
    ("不再逐文件复核（只信清单上的字节数）",
     '        verified = sp._verify_tree(entry, target)',
     '        verified = sum(f["bytes"] for f in entry["files"])'),
    ("重复目标不再拦（合同歧义被吞掉）",
     '            if dest in seen_dest:',
     '            if False and dest in seen_dest:'),
    ("数据根 os 不符也继续",
     '    if doc.get("os") != os_label:',
     '    if False and doc.get("os") != os_label:'),
    ("asset 与合同默认不符也继续",
     '        if row["asset"] is not None and asset != row["asset"]:',
     '        if False and row["asset"] is not None and asset != row["asset"]:'),
    ("键前缀不做段检查",
     '        if part in ("", ".", ".."):',
     '        if False and part in ("", ".", ".."):'),
    ("根清单哈希写死零",
     '"bytes": len(blob), "sha256": hashlib.sha256(blob).hexdigest(),',
     '"bytes": len(blob), "sha256": "0" * 64,'),
]


def load_mutant(src: str):
    mod = types.ModuleType("payload_intake_mutant")
    mod.__dict__["__file__"] = CLI
    mod.__dict__["__name__"] = "payload_intake_mutant"
    exec(compile(src, CLI, "exec"), mod.__dict__)
    return mod


def selfcheck_green(mod) -> bool:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            return mod.selftest() == 0
    except Exception:                       # noqa: BLE001
        return False


class TestSourceMutants(unittest.TestCase):
    def test_baseline_is_green(self) -> None:
        self.assertTrue(selfcheck_green(pi))

    def test_every_mutant_makes_the_selftest_red(self) -> None:
        with open(CLI, encoding="utf-8") as fh:
            src = fh.read()
        missed = []
        for name, old, new in MUTANTS:
            self.assertEqual(src.count(old), 1, f"锚点不唯一或已漂：{name}")
            if selfcheck_green(load_mutant(src.replace(old, new, 1))):
                missed.append(name)
        self.assertEqual(missed, [], "这些变异没人报：对应判据是装饰\n" + "\n".join(missed))

    def test_mutant_count(self) -> None:
        self.assertEqual(len(MUTANTS), 6)


class TestToolParses(unittest.TestCase):
    def test_ast_ok(self) -> None:
        with open(CLI, encoding="utf-8") as fh:
            ast.parse(fh.read())


if __name__ == "__main__":
    unittest.main()
