"""sideload 认领的测试：结论必须由真文件撑起来，而且每条都配一条反着来的对照。

夹具是三个真实下载的上游文件（见 `tests/fixtures/sideload.meta.json`）：
两颗钉过的字典（正方向）与一颗**同名不同字节**的真文件（负向）。
最后那颗不是假想的坑 —— 上游 v0.1.0 与 v0.3.0 的 `ppocrv5_latin_dict.txt` 真的不一样。
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
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import sideload_lookup as sl        # noqa: E402

CLI = os.path.join(ROOT, "tools", "sideload_lookup.py")
FIX = os.path.join(ROOT, "tests", "fixtures", "sideload")
PINNED = os.path.join(FIX, "v0.7.0", "ppocrv6_tiny_dict.txt")
TWIN_ON = os.path.join(FIX, "v0.3.0", "ppocrv5_latin_dict.txt")
TWIN_OFF = os.path.join(FIX, "v0.1.0", "ppocrv5_latin_dict.txt")


def meta() -> dict:
    with open(os.path.join(ROOT, "tests", "fixtures", "sideload.meta.json"), encoding="utf-8") as fh:
        return json.load(fh)


def index_view() -> dict:
    with open(os.path.join(ROOT, "pins", "sideload-index.json"), encoding="utf-8") as fh:
        return sl.view(json.load(fh))


class TestFixturesAreWhatTheyClaim(unittest.TestCase):
    def test_bytes_match_the_recorded_digests(self) -> None:
        """夹具不是"我记得是这样"：每份都按 meta 里的 sha256 与体积复算一遍。"""
        m = meta()
        for rel, rec in m["files"].items():
            p = os.path.join(FIX, *rel.split("/"))
            with open(p, "rb") as fh:
                raw = fh.read()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), rec["sha256"], rel)
            self.assertEqual(len(raw), rec["size"], rel)

    def test_the_two_twins_differ_but_share_a_name(self) -> None:
        """负向对照的全部依据：同一个文件名，两串不同字节。"""
        self.assertEqual(os.path.basename(TWIN_ON), os.path.basename(TWIN_OFF))
        self.assertNotEqual(sl.sha256_of(TWIN_ON)[0], sl.sha256_of(TWIN_OFF)[0])

    def test_the_pinned_ones_really_are_in_the_pins(self) -> None:
        """夹具的"钉过"必须去 pin 里查得到，不是我给文件起的名字。"""
        arts = sl.load_artifacts()
        index, problems = sl.build(arts)
        self.assertEqual(problems, [])
        by_name = index["names_with_multiple_hashes"]
        self.assertIn("flux1-schnell-q2_k.gguf", by_name,
                      "上游同名不同内容的例子应当不止一颗 —— 少了就是读者漂了")
        self.assertNotIn("ppocrv5_latin_dict.txt", by_name,
                         "这颗在我们表里只钉了一个 tag，负向对照靠的是 by_name 而不是重名统计")


class TestVerdicts(unittest.TestCase):
    def setUp(self) -> None:
        self.ix = index_view()

    def test_pinned_files_are_claimed(self) -> None:
        for p in (PINNED, TWIN_ON):
            v = sl.lookup(p, self.ix)
            self.assertEqual(v["verdict"], "known_by_hash", v)
            self.assertTrue(v["claim"], v)
        self.assertEqual(sl.lookup(PINNED, self.ix)["capabilities"], ["文字识别"])

    def test_the_default_trio_pairing_is_spoken(self) -> None:
        """认出一颗只说一半没用：OCR 的 det/rec/dict 必须一起给（上游 requires_all）。"""
        v = sl.lookup(PINNED, self.ix)
        blob = json.dumps(v, ensure_ascii=False)
        self.assertIn("还要一起给", blob)
        self.assertIn("pp-ocrv6_tiny_rec.onnx", blob)

    def test_same_name_different_bytes_is_refused(self) -> None:
        v = sl.lookup(TWIN_OFF, self.ix)
        self.assertEqual(v["verdict"], "name_matches_hash_differs", v)
        self.assertFalse(v["claim"], "同名不同字节却认领 = 把用户的文件当成另一颗")
        self.assertEqual(v["pinned_hashes_for_this_name"], [sl.sha256_of(TWIN_ON)[0]])

    def test_unknown_file_gets_a_labelled_hint_only(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "someone-else-Q4_K_M.gguf")
            with open(p, "wb") as fh:
                fh.write(b"\x00gguf-not-really")
            v = sl.lookup(p, self.ix)
        self.assertEqual(v["verdict"], "unknown", v)
        self.assertFalse(v["claim"])
        self.assertIs(v["name_shape_hint"]["claim"], False, v["name_shape_hint"])
        self.assertEqual(v["name_shape_hint"]["quantization_in_name"], "Q4_K_M")

    def test_hash_known_but_size_differs_is_not_a_claim(self) -> None:
        tampered = copy.deepcopy(self.ix)
        h = sl.sha256_of(PINNED)[0]
        for r in tampered["by_hash"][h]:
            r["bytes"] = 1
        v = sl.lookup(PINNED, tampered)
        self.assertEqual(v["verdict"], "hash_known_size_differs", v)
        self.assertFalse(v["claim"])

    def test_unreadable_path_says_so(self) -> None:
        v = sl.lookup(os.path.join(FIX, "没有这个文件.bin"), self.ix)
        self.assertEqual(v["verdict"], "unreadable")
        self.assertFalse(v["claim"])

    def test_hint_table_covers_each_capability_shape(self) -> None:
        self.assertEqual(sl.hint_for("ggml-large-v1.bin")["capability_guess"], "字幕")
        self.assertEqual(sl.hint_for("arabic_pp-ocrv5_mobile_rec.onnx")["capability_guess"], "文字识别")
        self.assertEqual(sl.hint_for("ppocrv5_arabic_dict.txt")["capability_guess"], "文字识别")
        self.assertIsNone(sl.hint_for("readme.md")["capability_guess"])

    def test_quantization_is_read_off_real_file_names(self) -> None:
        """量化名是从我们表里真实存在的文件取的。上一版正则把 `Q4_K_M` 读成 `Q4` ——
        短了不说错，只是把用户的 Q4_K_M 与 Q4_0 混成一类，UI 上看不出来。"""
        self.assertEqual(sl.hint_for("Qwen3-14B-Q4_K_M.gguf")["quantization_in_name"], "Q4_K_M")
        self.assertEqual(sl.hint_for("flux1-schnell-q2_k.gguf")["quantization_in_name"], "q2_k")
        self.assertEqual(sl.hint_for("ggml-base-q5_1.bin")["quantization_in_name"], "q5_1")
        self.assertEqual(sl.hint_for("stable-diffusion-v1-5-pruned-emaonly-f16.gguf")
                         ["quantization_in_name"], "f16")
        self.assertIsNone(sl.hint_for("ppocrv6_tiny_dict.txt")["quantization_in_name"])


class TestRecompute(unittest.TestCase):
    def test_committed_index_equals_recompute(self) -> None:
        with open(os.path.join(ROOT, "pins", "sideload-index.json"), encoding="utf-8") as fh:
            committed = json.load(fh)
        fresh, _p = sl.build(sl.load_artifacts())
        self.assertEqual(sl.check(committed, fresh), [])

    def test_stats_are_a_literal_that_fails_loudly(self) -> None:
        fresh, _p = sl.build(sl.load_artifacts())
        self.assertEqual(fresh["stats"], {"独立哈希": 354, "身份行": 420, "同名不同哈希的文件名": 30},
                         fresh["stats"])

    def test_check_catches_a_dropped_identity(self) -> None:
        with open(os.path.join(ROOT, "pins", "sideload-index.json"), encoding="utf-8") as fh:
            committed = json.load(fh)
        dropped = copy.deepcopy(committed)
        dropped["by_hash"].pop(next(iter(dropped["by_hash"])))
        self.assertTrue(sl.check(committed, dropped))

    def test_check_catches_a_changed_size(self) -> None:
        with open(os.path.join(ROOT, "pins", "sideload-index.json"), encoding="utf-8") as fh:
            committed = json.load(fh)
        mutated = copy.deepcopy(committed)
        next(iter(mutated["by_hash"].values()))[0]["bytes"] += 1
        self.assertTrue(any("身份行变了" in x for x in sl.check(committed, mutated)))


class TestCli(unittest.TestCase):
    def run_cli(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True)

    def test_selftest_is_zero(self) -> None:
        r = self.run_cli("--selftest")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("BAD 数量 = 0", r.stdout)

    def test_check_is_quiet(self) -> None:
        r = self.run_cli("--check")
        self.assertEqual(r.returncode, 0, r.stdout[-600:] + r.stderr[-300:])
        self.assertIn("问题 0 条", r.stdout)

    def test_lookup_reports_the_refusal_not_a_claim(self) -> None:
        r = self.run_cli("--lookup", TWIN_OFF)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("name_matches_hash_differs", r.stdout)
        self.assertIn("认领=False", r.stdout)

    def test_unreadable_lookup_is_a_nonzero_verdict(self) -> None:
        r = self.run_cli("--lookup", os.path.join(FIX, "ghost.gguf"))
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("unreadable", r.stdout)


class TestSourceMutants(unittest.TestCase):
    """把工具源码逐条改坏，证明"认领"这件事不是装饰。

    坏形状全是**会把用户的文件当成另一颗**的那种：不再按哈希找、把同名不同字节放行、
    把 hint 当认领、把体积不符当认领、少加一类来源。
    """

    @staticmethod
    def src() -> str:
        with open(CLI, encoding="utf-8") as fh:
            return fh.read()

    MUTANTS = [
        ("不再按哈希认（哈希命中也当不认识）", "    if rows:", "    if False and rows:"),
        ("同名不同字节的闸门关掉", 'pinned = index["by_name"].get(name)', "pinned = None"),
        ("认出来了却不给声明（反着坏：UI 无从下手）",
         '               "claim": True, "entries": rows,', '               "claim": False, "entries": rows,'),
        ("形态猜测变成声明",
         '            "quantization_in_name": q.group(1) if q else None,\n            "claim": False}',
         '            "quantization_in_name": q.group(1) if q else None,\n            "claim": True}'),
        ("哈希对但体积不符仍当认领", "if sizes and sizes != {size}:", "if False and sizes:"),
        ("生图那 337 行不进表", '    for row in img["weights"]:', "    for row in []:"),
    ]

    def load(self, text: str):
        import types
        mod = types.ModuleType("sideload_mutant")
        mod.__dict__["__file__"] = CLI
        mod.__dict__["__name__"] = "sideload_mutant"
        exec(compile(text, CLI, "exec"), mod.__dict__)
        return mod

    def probes(self, mod) -> dict:
        out = {}
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out["a 离线对照"] = mod.selftest() == 0
        except Exception:                        # noqa: BLE001
            out["a 离线对照"] = False
        try:
            with open(os.path.join(ROOT, "pins", "sideload-index.json"), encoding="utf-8") as fh:
                committed = json.load(fh)
            fresh, _p = mod.build(mod.load_artifacts())
            out["b 重算与漂移"] = mod.check(committed, fresh) == []
            ix = mod.view(fresh)
            out["c 钉过的被认领"] = (mod.lookup(PINNED, ix)["verdict"] == "known_by_hash"
                                    and mod.lookup(PINNED, ix)["claim"] is True)
            out["d 同名不同字节被拒"] = (mod.lookup(TWIN_OFF, ix)["verdict"] == "name_matches_hash_differs"
                                         and mod.lookup(TWIN_OFF, ix)["claim"] is False)
            with tempfile.TemporaryDirectory() as dd:
                p = os.path.join(dd, "zzz-Q4_K_M.gguf")
                with open(p, "wb") as fh:
                    fh.write(b"\x00x")
                v = mod.lookup(p, ix)
                out["e hint 不算认领"] = (v["verdict"] == "unknown"
                                          and v["name_shape_hint"]["claim"] is False
                                          and v["name_shape_hint"]["capability_guess"] is not None)
                tampered = copy.deepcopy(ix)
                h = mod.sha256_of(PINNED)[0]
                for r in tampered["by_hash"].get(h, []):
                    r["bytes"] = 1
                out["f 体积不符被拦"] = mod.lookup(PINNED, tampered)["verdict"] == "hash_known_size_differs"
        except Exception as exc:                  # noqa: BLE001
            for k in ("b 重算与漂移", "c 钉过的被认领", "d 同名不同字节被拒",
                      "e hint 不算认领", "f 体积不符被拦"):
                out.setdefault(k, False)
            out["_b 异常"] = str(exc)[:60]
        return out

    def test_baseline_probes_all_hold(self) -> None:
        self.assertEqual({k: v for k, v in self.probes(sl).items() if not k.startswith("_")},
                         {"a 离线对照": True, "b 重算与漂移": True, "c 钉过的被认领": True,
                          "d 同名不同字节被拒": True, "e hint 不算认领": True, "f 体积不符被拦": True})

    def test_every_mutant_is_caught(self) -> None:
        missed = []
        for name, old, new in self.MUTANTS:
            self.assertEqual(self.src().count(old), 1, f"锚点不唯一或已漂：{name}")
            got = {k: v for k, v in self.probes(self.load(self.src().replace(old, new, 1))).items()
                   if not k.startswith("_")}
            if all(got.values()):
                missed.append(f"{name} → {got}")
        self.assertEqual(missed, [], "这些变异没人报：对应判据是装饰\n" + "\n".join(missed))

    def test_mutant_count(self) -> None:
        self.assertEqual(len(self.MUTANTS), 6)


if __name__ == "__main__":
    unittest.main()
