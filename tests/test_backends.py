"""后端矩阵的测试：分层那句话（约束第 5 条）与"runtime 可替换"（第 3 条）到底有没有数据。

夹具是 GitHub release 的真实 asset 表（name / size / digest，见 `backend_assets.meta.json`）。
判据的形状和别的表一样：先证它会红（新后端词、缺 digest、漏标推断、撤掉一整档），
再证它不冤枉（真实捕获必须 0 问题、每一格与 payload-plan 的钉值对得上）。
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import backends as bd            # noqa: E402

CLI = os.path.join(ROOT, "tools", "backends.py")
CAP = os.path.join(ROOT, "tests", "fixtures", "backend_assets.json")
ART = os.path.join(ROOT, "pins", "backend-matrix.json")


def captures() -> dict:
    with open(CAP, encoding="utf-8") as fh:
        return json.load(fh)


def artifact() -> dict:
    with open(ART, encoding="utf-8") as fh:
        return json.load(fh)


def payload_picks() -> list:
    with open(os.path.join(ROOT, "pins", "payload-plan.json"), encoding="utf-8") as fh:
        d = json.load(fh)
    return [r for rows in d["os"].values() for r in rows if r.get("engine")]


def repos() -> dict:
    return {"llama": "ggml-org/llama.cpp", "whisper": "ggml-org/whisper.cpp",
            "sd": "leejet/stable-diffusion.cpp"}


def one(engines: dict, engine: str, os_key: str, backend: str) -> dict:
    """格子里现在是一列资产；测试里取"那一档唯一那颗"的写法收进一个函数。"""
    rows = engines[engine]["by_os"][os_key][backend]
    assert len(rows) == 1, (engine, os_key, backend, rows)
    return rows[0]


def rebuild(capt=None) -> tuple:
    return bd.build(capt or captures(), payload_picks(), repos())


class TestCaptureIsReal(unittest.TestCase):
    def test_fixture_bytes_match_the_recorded_hash(self) -> None:
        with open(CAP, "rb") as fh:
            raw = fh.read()
        with open(CAP.replace(".json", ".meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), meta["sha256"])
        self.assertEqual(len(raw), meta["size"])
        self.assertEqual({k: len(v) for k, v in json.loads(raw).items()}, meta["refs"])

    def test_every_asset_carries_an_upstream_digest(self) -> None:
        """这张表能说"上游给的 digest"是因为 GitHub 真的给了；少一颗就要在这里说。"""
        missing = [f"{ref}/{a['name']}" for ref, assets in captures().items()
                   for a in assets if not a.get("sha256")]
        self.assertEqual(missing, [], missing)


class TestClassifier(unittest.TestCase):
    CASES = [
        ("llama-b11727-bin-win-cuda-12.4-x64.zip", "b11727", "win-x64", "cuda", "12.4"),
        ("sd-master-95b3090-bin-win-cuda12-x64.zip", "master-975-95b3090", "win-x64", "cuda", "12"),
        ("cudart-sd-bin-win-cu12-x64.zip", "master-975-95b3090", None, None, None),
        ("llama-b11727-bin-ubuntu-rocm-10.0-x64.tar.gz", "b11727", "linux-x64", "rocm", "10.0"),
        ("llama-b11727-bin-ubuntu-sycl-fp16-x64.tar.gz", "b11727", "linux-x64", "sycl", "16"),
        ("llama-b11727-bin-win-opencl-adreno-arm64.zip", "b11727", "win-arm64",
         "opencl-adreno", None),
        ("llama-b11727-bin-linux-arm64-snapdragon.tar.gz", "b11727", "linux-arm64",
         "qnn-snapdragon", None),
        ("whisper-blas-bin-Win32.zip", "b5454", "win-x86", "blas", None),
        ("llama-b11727-bin-macos-arm64.tar.gz", "b11727", "macos-arm64", "unknown_from_name", None),
        ("llama-b11727-ui.tar.gz", "b11727", None, None, None),
    ]

    def test_each_shape_classifies_as_claimed(self) -> None:
        for name, release, want_os, want_backend, want_version in self.CASES:
            c = bd.classify(name, release)
            if want_backend is None:
                self.assertNotEqual(c["kind"], "runtime", f"{name} 不该被当 runtime：{c}")
                continue
            self.assertEqual((c["os"], c["backend"], c["version"]),
                             (want_os, want_backend, want_version), f"{name} → {c}")

    def test_a_new_backend_word_must_be_reported_not_become_cpu(self) -> None:
        """上游加一档（hip / mystery）时必须红 —— 静默归成 cpu 是最难发现的一种错。"""
        for fake in ("llama-b11727-bin-ubuntu-hip-x64.tar.gz",
                     "llama-b11727-bin-ubuntu-mystery-x64.tar.gz"):
            c = bd.classify(fake, "b11727")
            self.assertEqual(c["backend"], "unknown_token", f"{fake} → {c}")
            self.assertEqual(c["os"], "linux-x64", c)

    def test_win32_is_not_arm64_and_darwin_is_not_windows(self) -> None:
        """`Darwin` 里含 `win`：不给词界就会把 macOS 的包分到 Windows 那一列。"""
        self.assertEqual(bd.classify("sd-master-95b3090-bin-Darwin-macOS-26.6.2-arm64.zip",
                                     "master-975-95b3090")["os"], "macos-arm64")
        self.assertEqual(bd.classify("whisper-bin-Win32.zip", "b5454")["os"], "win-x86")


class TestArtifact(unittest.TestCase):
    def test_committed_equals_recompute(self) -> None:
        fresh, problems = rebuild()
        self.assertEqual(problems, [])
        self.assertEqual(bd.check(artifact(), fresh), [])

    def test_stats_are_a_literal_that_fails_loudly(self) -> None:
        self.assertEqual(artifact()["stats"],
                         {"捕获资产": 57, "runtime 行": 47, "不是 runtime 的资产": 10, "引擎": 3,
                          "后端种类": ["blas", "cpu", "cuda", "opencl-adreno", "openvino",
                                       "qnn-snapdragon", "rocm", "sycl", "unknown_from_name",
                                       "vulkan"]})

    def test_the_cells_the_shell_will_read(self) -> None:
        """分层要能用，前提是这些格子真在，而且体积差是真的（CPU 16.9 vs CUDA 327.7）。"""
        e = artifact()["engines"]
        self.assertEqual(one(e, "llama", "win-x64", "cpu")["mib"], 23.0)
        self.assertEqual(one(e, "llama", "win-x64", "vulkan")["mib"], 35.2)
        self.assertEqual(one(e, "sd", "win-x64", "cpu")["mib"], 16.9)
        self.assertEqual(one(e, "sd", "win-x64", "cuda")["mib"], 327.7)
        self.assertEqual(one(e, "sd", "linux-x64", "vulkan")["mib"], 34.9)

    def test_every_runtime_row_is_listed_per_engine(self) -> None:
        """`rows` 必须还是全部 runtime 行 —— 上一版内层循环借用了同名变量，把它换成了一格。

        这条钉的是**产物形状**；工具里那条同名自洽检查是车道现取那一步的保险
        （那条路没有单测在跑）。两者不是同一件事，所以两道都要留。
        """
        for e, body in artifact()["engines"].items():
            self.assertEqual(len(body["rows"]), body["runtimes"], e)
            self.assertEqual(len(body["not_runtime"]) + body["runtimes"], body["assets_total"], e)

    def test_one_cell_holds_every_version_upstream_shipped(self) -> None:
        """一格不是一个资产，是**这一档后端的全部资产**。

        上一版写成 `cell[backend] = 一行`，于是 llama 在 win-x64 只剩 cuda-13.4，
        cuda-12.4 静默消失；而"驱动只到 12.x"那台机器恰恰需要它。
        """
        e = artifact()["engines"]
        self.assertEqual(sorted(x["version"] for x in e["llama"]["by_os"]["win-x64"]["cuda"]),
                         ["12.4", "13.4"])
        self.assertEqual(sorted(x["version"] for x in e["llama"]["by_os"]["linux-x64"]["sycl"]),
                         ["16", "32"])
        self.assertEqual(sorted(x["version"] for x in e["whisper"]["by_os"]["win-x64"]["cuda"]),
                         ["11.8.0", "12.4.0"])
        self.assertEqual(sum(v["格子条目"] for v in e.values()), 45,
                         "47 个 runtime 减 2 个名字里不写平台的")

    def test_whisper_win_x64_has_only_cuda_by_name(self) -> None:
        """这条最难看：whisper 在 win-x64 那一格里只剩 CUDA。

        非 CUDA 那两颗（bin-x64 / blas-bin-x64）名字里根本不写平台 —— 所以"按资产名挑后端"
        在 whisper 上做不到。这不是分类器坏了，是上游命名如此，必须记成缺口而不是猜一个。
        """
        e = artifact()["engines"]["whisper"]
        self.assertEqual(sorted(e["by_os"]["win-x64"]), ["cuda"])
        self.assertEqual([r["asset"] for r in e["platform_not_in_name"]],
                         ["whisper-bin-x64.zip", "whisper-blas-bin-x64.zip"])

    def test_gap_names_each_asset_once(self) -> None:
        """缺口点的是**颗**，不是点名次数。

        行侧与 payload-plan 那侧会点名同一颗（whisper-bin-x64.zip 两边都有），
        第一版直接把两次的点名拼一起，于是 2 颗写成 3 颗 —— 而总数那格照样对。
        """
        for g in artifact()["gaps"]:
            if g.get("kind") == "platform_not_in_name":
                self.assertEqual(g["assets"], sorted(set(g["assets"])), g)

    def test_dropping_two_backend_rows_is_caught(self) -> None:
        cap = copy.deepcopy(captures())
        for ref in cap:
            cap[ref] = [a for a in cap[ref]
                        if "vulkan" not in a["name"] and "cuda" not in a["name"]]
        fresh, problems = rebuild(cap)
        self.assertTrue(any("地板" in p for p in problems), problems)
        self.assertTrue(any("条目数变了" in x for x in bd.check(artifact(), fresh)), problems)
        self.assertTrue(any("后端种类变了" in x for x in bd.check(artifact(), fresh)), problems)

    def test_a_pick_with_a_stale_digest_is_reported(self) -> None:
        picks = copy.deepcopy(payload_picks())
        picks[0]["digest"] = "sha256:" + "0" * 64
        _d, problems = bd.build(captures(), picks, repos())
        self.assertTrue(any("digest 与 payload-plan 钉的不等" in p for p in problems), problems)

    def test_an_unflagged_platform_guess_is_reported(self) -> None:
        """名字里没平台、payload-plan 又没标 inferred —— 推断被写成事实，必须红。"""
        picks = copy.deepcopy(payload_picks())
        for p in picks:
            if p["asset"] == "whisper-bin-x64.zip":
                p["inferred_platform"] = False
        _d, problems = bd.build(captures(), picks, repos())
        self.assertTrue(any("推断要写明是推断" in p for p in problems), problems)

    def test_gaps_say_the_three_real_boundaries(self) -> None:
        blob = json.dumps(artifact()["gaps"], ensure_ascii=False)
        kinds = {g.get("kind") for g in artifact()["gaps"]}
        self.assertIn("backend_not_in_name", kinds)
        self.assertIn("platform_not_in_name", kinds)
        self.assertIn("#29", blob)                       # whisper 在 mac 没有 runtime
        self.assertIn("Cargo", blob)                     # OCR 根本没有预编译产物这一说

    def test_macos_is_never_claimed_as_a_backend(self) -> None:
        """不许为了表格好看把 mac 写成 metal/cpu —— 名字里没有这个信息。"""
        for e, body in artifact()["engines"].items():
            for o, cells in body["by_os"].items():
                if o.startswith("macos"):
                    self.assertEqual(list(cells), ["unknown_from_name"], f"{e}/{o}")


class TestCli(unittest.TestCase):
    def run_cli(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True)

    def test_selftest_zero(self) -> None:
        r = self.run_cli("--selftest")
        self.assertEqual(r.returncode, 0, r.stdout[-600:] + r.stderr[-300:])
        self.assertIn("BAD 数量 = 0", r.stdout)

    def test_check_zero(self) -> None:
        r = self.run_cli("--check")
        self.assertEqual(r.returncode, 0, r.stdout[-600:] + r.stderr[-300:])
        self.assertIn("问题 0 条", r.stdout)


# 变异锚点里带真换行，写成字面量拼串最容易看错；这里明确 join 一次。
OLD_CELL = "\n".join([
    '            matrix.setdefault(r["os"], {}).setdefault(r["backend"], []).append(',
    '                {"asset": r["asset"], "version": r["version"], "mib": r["mib"],',
    '                 "sha256": r["sha256"]})'])
NEW_CELL = "\n".join([
    '            matrix.setdefault(r["os"], {})[r["backend"]] = [',
    '                {"asset": r["asset"], "version": r["version"], "mib": r["mib"],',
    '                 "sha256": r["sha256"]}]'])
OLD_DEDUP = "\n".join([
    '    unassigned = [{(r["engine"], r["asset"]): r for r in unassigned}[k]',
    '                  for k in sorted({(r["engine"], r["asset"]) for r in unassigned})]'])
NEW_DEDUP = "    unassigned = list(unassigned)"


class TestSourceMutants(unittest.TestCase):
    @staticmethod
    def src() -> str:
        with open(CLI, encoding="utf-8") as fh:
            return fh.read()

    MUTANTS = [
        ("认不得的词不再报（新后端会被当成 cpu）",
         "    if left:\n        head =", "    if False and left:\n        head ="),
        ("钉值比对关掉（两份产者各说一套也过）",
         '        if "sha256:" + got["sha256"] != p["digest"]:',
         '        if False and "sha256:" + got["sha256"] != p["digest"]:'),
        ("平台推断不要求标注（把猜写成事实）",
         '            if not p.get("inferred_platform"):',
         '            if False and p.get("inferred_platform"):'),
        ("后端地板清空（上游撤档看不见）",
         'BACKEND_FLOOR = {"llama": 5, "whisper": 3, "sd": 4}', "BACKEND_FLOOR = {}"),
        ("一格只留最后一行（同后端的别的版本静默消失）", OLD_CELL, NEW_CELL),
        ("Darwin 的词界去掉（mac 会被分到 win 那一列）",
         '(re.compile(r"(?:macos|darwin).*arm64|arm64.*(?:macos|darwin)", re.IGNORECASE), '
         '"macos-arm64"),',
         '(re.compile(r"arm64", re.IGNORECASE), "win-arm64"),'),
        ("两个产者的点名不去重（2 颗写成 3 颗）", OLD_DEDUP, NEW_DEDUP),
    ]

    def load(self, text: str):
        import types
        mod = types.ModuleType("backends_mutant")
        mod.__dict__["__file__"] = CLI
        mod.__dict__["__name__"] = "backends_mutant"
        exec(compile(text, CLI, "exec"), mod.__dict__)
        return mod

    def probes(self, mod) -> dict:
        out = {}
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out["a 离线对照"] = mod.selftest() == 0
        except Exception:                            # noqa: BLE001
            out["a 离线对照"] = False
        try:
            committed = artifact()
            fresh, _p = mod.build(captures(), payload_picks(), repos())
            out["b 重算与漂移"] = mod.check(committed, fresh) == []
            out["c 新后端词被报"] = mod.classify(
                "llama-b11727-bin-ubuntu-hip-x64.tar.gz", "b11727")["backend"] == "unknown_token"
            picks = copy.deepcopy(payload_picks())
            picks[0]["digest"] = "sha256:" + "0" * 64
            _d, pr = mod.build(captures(), picks, repos())
            out["d 钉值不符被报"] = any("digest" in x for x in pr)
            picks2 = copy.deepcopy(payload_picks())
            for p in picks2:
                if p["asset"] == "whisper-bin-x64.zip":
                    p["inferred_platform"] = False
            _d2, pr2 = mod.build(captures(), picks2, repos())
            out["e 未标注的推断被报"] = any("推断要写明" in x for x in pr2)
            cap = copy.deepcopy(captures())
            for ref in cap:
                cap[ref] = [a for a in cap[ref]
                            if "vulkan" not in a["name"] and "cuda" not in a["name"]]
            fresh3, pr3 = mod.build(cap, payload_picks(), repos())
            out["f 撤两档后被地板拦"] = any("地板" in x for x in pr3)
            out["g 撤两档也被漂移拦"] = mod.check(committed, fresh3) != []
            out["h 一格留着所有版本"] = len(
                mod.build(captures(), payload_picks(), repos())[0]["engines"]["llama"]
                ["by_os"]["win-x64"]["cuda"]) == 2
            out["i mac 不被分错"] = mod.classify(
                "sd-master-95b3090-bin-Darwin-macOS-26.6.2-arm64.zip",
                "master-975-95b3090")["os"] == "macos-arm64"
            doc4, _p4 = mod.build(captures(), payload_picks(), repos())
            plats = [g["assets"] for g in doc4["gaps"]
                     if g.get("kind") == "platform_not_in_name"]
            out["j 缺口不重复点名"] = (
                plats == [["whisper-bin-x64.zip", "whisper-blas-bin-x64.zip"]])
        except Exception as exc:                     # noqa: BLE001
            for k in ("b 重算与漂移", "c 新后端词被报", "d 钉值不符被报", "e 未标注的推断被报",
                      "f 撤两档后被地板拦", "g 撤两档也被漂移拦", "h 一格留着所有版本",
                      "i mac 不被分错", "j 缺口不重复点名"):
                out.setdefault(k, False)
            out["_b 异常"] = str(exc)[:60]
        return out

    def test_baseline_probes_all_hold(self) -> None:
        self.assertEqual({k: v for k, v in self.probes(bd).items() if not k.startswith("_")},
                         {"a 离线对照": True, "b 重算与漂移": True, "c 新后端词被报": True,
                          "d 钉值不符被报": True, "e 未标注的推断被报": True,
                          "f 撤两档后被地板拦": True, "g 撤两档也被漂移拦": True,
                          "h 一格留着所有版本": True, "i mac 不被分错": True,
                          "j 缺口不重复点名": True})

    def test_every_mutant_is_caught(self) -> None:
        missed = []
        for name, old, new in self.MUTANTS:
            self.assertEqual(self.src().count(old), 1, f"锚点不唯一或已漂：{name}")
            got = {k: v for k, v in self.probes(
                self.load(self.src().replace(old, new, 1))).items() if not k.startswith("_")}
            if all(got.values()):
                missed.append(f"{name} → {got}")
        self.assertEqual(missed, [], "这些变异没人报：对应判据是装饰\n" + "\n".join(missed))

    def test_mutant_count(self) -> None:
        self.assertEqual(len(self.MUTANTS), 7)


if __name__ == "__main__":
    unittest.main()
