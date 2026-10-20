"""「换哪一档、要多下多少」这张表的测试。

它的两份输入都是**别的工具已经落盘的产物**（矩阵 + payload-plan），所以测试要证两件事：
① 这张表没有自己另挑默认档（逐行等于 payload-plan 那一行）；
② 派生出来的数（体积差、伴生包配对、CPU 退路状态）是被算出来的 —— 每样都配一条"改坏它就红"。
判据形状与 backends 一致：先证它会红，再证它不冤枉（真输入 0 问题）。
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import backend_choice as bc       # noqa: E402
import backends as bd             # noqa: E402

CLI = os.path.join(ROOT, "tools", "backend_choice.py")
ART = os.path.join(ROOT, "pins", "backend-choice.json")
PAR = os.path.join(ROOT, "pins", "runtime-parity.json")
MATRIX = os.path.join(ROOT, "pins", "backend-matrix.json")
PLAN = os.path.join(ROOT, "pins", "payload-plan.json")


def artifact() -> dict:
    with open(ART, encoding="utf-8") as fh:
        return json.load(fh)


def matrix() -> dict:
    with open(MATRIX, encoding="utf-8") as fh:
        return json.load(fh)


def plan() -> dict:
    with open(PLAN, encoding="utf-8") as fh:
        return json.load(fh)


def parity() -> dict:
    with open(PAR, encoding="utf-8") as fh:
        return json.load(fh)


def rebuild(m = None, p = None, par = None) -> tuple[dict, list[str]]:
    return bc.build(m if m is not None else matrix(),
                    p if p is not None else plan(),
                    par if par is not None else parity())


class TestArtifact(unittest.TestCase):
    def test_check_zero(self) -> None:
        _doc, problems = rebuild()
        self.assertEqual(problems, [], problems)

    def test_committed_equals_recompute(self) -> None:
        fresh, _p = rebuild()
        self.assertEqual(bc.check(artifact(), fresh), [])

    def test_stats_are_measured_not_typed(self) -> None:
        s = artifact()["stats"]
        self.assertEqual(s["引擎×OS"], 16)
        self.assertEqual(s["格子"], 41)
        self.assertEqual(s["可选档"], 45)
        self.assertEqual(s["CUDA 档"], 10)
        self.assertEqual(s["伴生包"], 7)
        self.assertEqual(s["配不上的CUDA档"], 3)
        self.assertEqual(s["已装默认档"], 8)
        # 落地体积：45 个可选档里 29 个有数（zip + 真装默认档的 tar），16 个是可选 tar → 未知
        self.assertEqual(s["有落地体积的档"], 29)
        self.assertEqual(s["落地体积未知的档"], 16)
        self.assertEqual(s["有落地体积的档"] + s["落地体积未知的档"], s["可选档"])

    def test_options_equal_matrix_cells(self) -> None:
        """可选档总数必须等于矩阵的格子条目数 —— 两张表是同一段数据，不许各数一套。"""
        self.assertEqual(artifact()["stats"]["可选档"],
                         sum(v["格子条目"] for v in matrix()["engines"].values()))

    def test_matrix_input_is_reproducible_from_the_release_fixture(self) -> None:
        """这张表的输入本身要能从真捕获重算出来（链条断在第一环就等于没有证据）。"""
        cap_path = os.path.join(ROOT, "tests", "fixtures", "backend_assets.json")
        with open(cap_path, encoding="utf-8") as fh:
            cap = json.load(fh)
        picks, repos = bd.picks_from_payload(PLAN)
        fresh, problems = bd.build(cap, picks, repos)
        self.assertEqual(problems, [])
        self.assertEqual(bd.check(matrix(), fresh), [])
        _d2, p2 = rebuild(fresh, plan())
        self.assertEqual(p2, [])

    def test_default_rows_are_payload_plan_rows(self) -> None:
        want = {(r["engine"], o): r["asset"] for o, rows in plan()["os"].items()
                for r in rows if r.get("engine")}
        got = {(e, o): r["default"] for e, b in artifact()["engines"].items()
               for o, r in b["os"].items() if r["install"] == "shipped"}
        self.assertEqual(got, want)

    def test_landed_columns_agree_with_the_parity_table(self) -> None:
        """三张表说的是同一颗包：可选档的落地体积逐颗等于 runtime-parity 量出来的那个数；
        未知的那些必须带 landed_note，且必须是 parity 登记为未读的那颗（不许有第三种来源）。"""
        par = parity()
        pmap = {a["asset"]: a.get("landed_mib") for a in par["assets"] if a.get("read")}
        pmap_unread = {a["asset"] for a in par["assets"] if not a.get("read")}
        opts = [o for b in artifact()["engines"].values() for r in b["os"].values()
                for o in r["options"]]
        self.assertEqual(len(opts), artifact()["stats"]["可选档"])
        for o in opts:
            if o["landed_mib"] is None:
                self.assertIn("landed_note", o, o["asset"])
                self.assertIn(o["asset"], pmap_unread, o["asset"])
            else:
                self.assertEqual(o["landed_mib"], pmap[o["asset"]], o["asset"])

    def test_default_landed_equals_payload_plan_measurement(self) -> None:
        """真装默认档那颗的落地体积，还要等于 payload-plan 独立量过的 uncompressed_mib ——
        两个生产者各量一遍再对表，比只信自己强。"""
        pmap = {a["asset"]: a.get("landed_mib") for a in parity()["assets"] if a.get("read")}
        rows = {r["asset"]: r for oo in plan()["os"].values() for r in oo if r.get("asset")}
        n = 0
        for b in artifact()["engines"].values():
            for r in b["os"].values():
                if r["install"] == "shipped" and r["default"] in rows:
                    self.assertEqual(pmap.get(r["default"]),
                                     rows[r["default"]]["uncompressed_mib"], r["default"])
                    n += 1
        self.assertEqual(n, 8)

    def test_disk_extra_is_a_real_difference_not_a_copy(self) -> None:
        """多占多少 = 这一颗落地 - 默认那颗落地；两边都从 parity 取，手抄就在这里红。"""
        pmap = {a["asset"]: a.get("landed_mib") for a in parity()["assets"] if a.get("read")}
        for b in artifact()["engines"].values():
            for r in b["os"].values():
                # 基准取的是"默认那颗"，它可能不在按名字分出来的清单里（whisper 的 bin-x64 就是）
                base = pmap.get(r["default"]) if r["default"] else None
                for o in r["options"]:
                    if base is None or o["landed_mib"] is None:
                        self.assertIsNone(o["disk_extra_mib_vs_default"], o["asset"])
                    else:
                        self.assertAlmostEqual(o["disk_extra_mib_vs_default"],
                                               round(o["landed_mib"] - base, 1), places=1)

    def test_extra_mib_is_recomputed(self) -> None:
        e = artifact()["engines"]["llama"]["os"]["win-x64"]
        base = {x["asset"]: x["mib"] for x in e["options"]}[e["default"]]
        for o in e["options"]:
            self.assertAlmostEqual(o["extra_mib_vs_default"], round(o["mib"] - base, 1), places=1)

    def test_switching_to_cuda_costs_more_than_the_runtime(self) -> None:
        """这一条就是这张表存在的全部理由：伴生包比 runtime 本体还大几倍。"""
        e = artifact()["engines"]["llama"]["os"]["win-x64"]
        cuda124 = [o for o in e["options"] if o["backend"] == "cuda" and o["version"] == "12.4"][0]
        self.assertEqual(cuda124["cudart"]["asset"], "cudart-llama-bin-win-cuda-12.4-x64.zip")
        self.assertEqual(cuda124["extra_mib_vs_default"], 236.3)
        self.assertEqual(cuda124["cudart"]["mib"], 373.3)
        self.assertTrue(bc.HEX64.match(cuda124["cudart"]["sha256"]))

    def test_two_cuda_versions_are_both_available(self) -> None:
        """同档两个 CUDA 版本都要在（一格一行的写法会只剩字典序最后那颗）。"""
        e = artifact()["engines"]["llama"]["os"]["win-x64"]
        self.assertEqual(sorted(o["version"] for o in e["options"] if o["backend"] == "cuda"),
                         ["12.4", "13.4"])

    def test_sd_spelling_difference_is_paired(self) -> None:
        """sd 写 cu12、runtime 写 cuda12 —— 拼法不同但必须配上同一颗伴生包。"""
        e = artifact()["engines"]["sd"]["os"]["win-x64"]
        cuda = [o for o in e["options"] if o["backend"] == "cuda"][0]
        self.assertEqual(cuda["version"], "12")
        self.assertEqual(cuda["cudart"]["asset"], "cudart-sd-bin-win-cu12-x64.zip")

    def test_whisper_cuda_has_no_companion_and_says_so(self) -> None:
        d = artifact()
        self.assertEqual(sorted(x["asset"] for x in d["cuda_rows_without_companion"]),
                         ["whisper-bin-win-cuda-11.8.0-x64.zip",
                          "whisper-bin-win-cuda-12.4.0-x64.zip",
                          "whisper-bin-win-cuda-13.4-arm64.zip"])
        for x in d["cuda_rows_without_companion"]:
            self.assertIn("没有", x["because"], "「没有」与「不需要」必须是两句话")
        for o in d["engines"]["whisper"]["os"]["win-x64"]["options"]:
            self.assertIsNone(o["cudart"])
            self.assertIn("cudart_note", o)

    def test_cpu_fallback_states(self) -> None:
        e = artifact()["engines"]
        self.assertEqual(e["llama"]["os"]["win-x64"]["cpu_fallback"], "named_cpu")
        self.assertEqual(e["whisper"]["os"]["win-x64"]["cpu_fallback"], "only_unnamed_asset")
        self.assertEqual(e["llama"]["os"]["macos-arm64"]["cpu_fallback"], "name_does_not_say")
        self.assertEqual(e["sd"]["os"]["macos-arm64"]["cpu_fallback"], "name_does_not_say")
        for body in e.values():
            for row in body["os"].values():
                self.assertIn(row["cpu_fallback"], bc.CPU_STATES, row["cpu_fallback"])

    def test_whisper_win_x64_default_is_outside_the_named_list(self) -> None:
        """那颗 CPU 包名字里不写平台：它既是被装的那一颗，又不在按名字分出来的清单里。"""
        d = artifact()
        self.assertEqual(d["default_outside_cell"],
                         [{"asset": "whisper-bin-x64.zip", "because": d["default_outside_cell"][0]["because"],
                           "engine": "whisper", "inferred_platform": True, "os": "win-x64"}])
        self.assertFalse(d["engines"]["whisper"]["os"]["win-x64"]["default_in_cell"])

    def test_macos_subtitle_has_no_cell_at_all(self) -> None:
        d = artifact()
        self.assertEqual(d["missing_cell_default"],
                         [{"because": d["missing_cell_default"][0]["because"], "capability": "字幕",
                           "os": "macos-arm64", "status": "上游无产物"}])
        self.assertNotIn("macos-arm64", d["engines"]["whisper"]["os"])

    def test_cells_without_acceleration_are_named_not_hidden(self) -> None:
        d = artifact()
        self.assertEqual(sorted((x["engine"], x["os"]) for x in d["no_acceleration_named"]),
                         [("llama", "linux-s390x"), ("whisper", "linux-arm64"),
                          ("whisper", "linux-x64"), ("whisper", "win-x86")])

    def test_every_option_can_be_verified_after_download(self) -> None:
        for body in artifact()["engines"].values():
            for row in body["os"].values():
                for o in row["options"]:
                    self.assertTrue(bc.HEX64.match(o["sha256"]), o["asset"])

    def test_cli_check_exits_zero(self) -> None:
        r = subprocess.run([sys.executable, CLI, "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("问题 0 条", r.stdout)
        self.assertIn("伴生包 7 份", r.stdout)

    def test_selftest_cli_exits_zero(self) -> None:
        r = subprocess.run([sys.executable, CLI, "--selftest"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("BAD 数量 = 0", r.stdout)


class TestSelftest(unittest.TestCase):
    def test_selftest_exit_zero(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(bc.selftest(), 0)
        self.assertIn("BAD 数量 = 0", buf.getvalue())


OLD_CHECKED = "\n".join([
    'CHECKED = ("backend", "version", "asset", "sha256", "mib", "is_default",',
    '           "extra_mib_vs_default", "cudart", "landed_mib", "disk_extra_mib_vs_default")'])
NEW_CHECKED = "\n".join([
    'CHECKED = ("backend", "version", "asset", "sha256", "mib", "is_default",',
    '           "cudart")'])


class TestSourceMutants(unittest.TestCase):
    @staticmethod
    def src() -> str:
        with open(CLI, encoding="utf-8") as fh:
            return fh.read()

    MUTANTS = [
        ("配不上的伴生包被静默吸收", "            if key not in cuda_rows:",
         "            if False and key not in cuda_rows:"),
        ("一档对上两颗伴生包不报", "        if len(rows) > 1:", "        if False and len(rows) > 1:"),
        ("没哈希的档照样进表", '                    if not HEX64.match(x["sha256"] or ""):',
         '                    if False and not HEX64.match(x["sha256"] or ""):'),
        ("默认档凭空消失不报", "                if d_asset not in named:",
         "                if False and d_asset not in named:"),
        ("把推断写成事实不报", '                    elif not dflt.get("inferred_platform"):',
         '                    elif False and not dflt.get("inferred_platform"):'),
        ("CPU 退路没了不报", '            if install == "shipped" and cpu_state == "absent":',
         '            if False and install == "shipped" and cpu_state == "absent":'),
        ("默认档改成自己挑一颗", '                d_asset = dflt["asset"]',
         '                d_asset = sorted(named)[0] if named else dflt["asset"]'),
        ("输入形状漂了不报", '            elif dflt.get("status") != "ok":',
         '            elif False and dflt.get("status") != "ok":'),
        ("分层地板清空", '            if not row or not row["acceleration_named"]:',
         '            if False and not row["acceleration_named"]:'),
        ("体积差不进比对表", OLD_CHECKED, NEW_CHECKED),
        ("落地体积未知被写成 0", '                    opt["landed_mib"] = landed_map.get(x["asset"])',
         '                    opt["landed_mib"] = landed_map.get(x["asset"]) or 0.0'),
        ("默认档缺落地体积不报",
         '            if d_asset and base_landed is None and d_asset not in gap_assets:',
         '            if False and d_asset and base_landed is None and d_asset not in gap_assets:'),
    ]

    def load(self, text: str):
        import types
        mod = types.ModuleType("backend_choice_mutant")
        mod.__dict__["__file__"] = CLI
        mod.__dict__["__name__"] = "backend_choice_mutant"
        exec(compile(text, CLI, "exec"), mod.__dict__)
        return mod

    def probes(self, mod) -> dict:
        out = {}
        par = parity()
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out["a 离线对照"] = mod.selftest() == 0
        except Exception:                            # noqa: BLE001
            out["a 离线对照"] = False
        try:
            m, p = matrix(), plan()
            committed = artifact()
            fresh, _pr = mod.build(m, p, par)
            out["b 重算与漂移"] = mod.check(committed, fresh) == []
            m1 = copy.deepcopy(m)
            for r in m1["engines"]["llama"]["not_runtime"]:
                if r["asset"].startswith("cudart-llama-bin-win-cuda-12.4"):
                    r["asset"] = "cudart-llama-bin-win-cuda-99.9-x64.zip"
            out["c 假伴生包被报"] = any("配不到任何一档" in x for x in mod.build(m1, p, par)[1])
            m2 = copy.deepcopy(m)
            m2["engines"]["llama"]["not_runtime"].append(
                {"asset": "cudart-llama-bin-win-cuda-12.4-x64.zip", "kind": "cudart_companion",
                 "mib": 1.0, "sha256": "0" * 64})
            out["d 一档两颗被报"] = any("对上 2 份" in x for x in mod.build(m2, p, par)[1])
            m3 = copy.deepcopy(m)
            m3["engines"]["sd"]["by_os"]["win-x64"]["vulkan"][0]["sha256"] = ""
            out["e 没哈希被报"] = any("没有 64 位 sha256" in x for x in mod.build(m3, p, par)[1])
            p4 = copy.deepcopy(p)
            for r in p4["os"]["win-x64"]:
                if r.get("engine") == "llama":
                    r["asset"] = "llama-b11727-bin-不存在-x64.zip"
            out["f 默认档凭空消失被报"] = any(
                "两份产者对同一个 release 已经漂了" in x for x in mod.build(m, p4, par)[1])
            p5 = copy.deepcopy(p)
            for r in p5["os"]["win-x64"]:
                if r.get("engine") == "whisper":
                    r["inferred_platform"] = False
            out["g 推断写成事实被报"] = any(
                "推断要写明是推断" in x for x in mod.build(m, p5, par)[1])
            m6 = copy.deepcopy(m)
            del m6["engines"]["llama"]["by_os"]["win-x64"]["cpu"]
            out["h CPU 退路没了被报"] = any("CPU 退路" in x for x in mod.build(m6, p, par)[1])
            m7 = copy.deepcopy(m)
            m7["engines"]["whisper"]["platform_not_in_name"] = [
                x for x in m7["engines"]["whisper"]["platform_not_in_name"]
                if x["asset"] != "whisper-bin-x64.zip"]
            out["i 缺席清单被抹被报"] = any(
                "两份产者对同一个 release 已经漂了" in x for x in mod.build(m7, p, par)[1])
            p8 = copy.deepcopy(p)
            for r in p8["os"]["linux-x64"]:
                if r.get("engine") == "llama":
                    r["status"] = "上游无产物"
            out["j 输入形状漂了被报"] = any(
                "有 engine 但 status" in x for x in mod.build(m, p8, par)[1])
            m9 = copy.deepcopy(m)
            m9["engines"]["llama"]["by_os"]["win-x64"] = {
                "cpu": m["engines"]["llama"]["by_os"]["win-x64"]["cpu"]}
            out["k 撤光加速档被地板拦"] = any(
                "按后端分层" in x for x in mod.build(m9, p, par)[1])
            shipped = {(e, o): r["default"] for e, b in fresh["engines"].items()
                       for o, r in b["os"].items() if r["install"] == "shipped"}
            want = {(r["engine"], o): r["asset"] for o, rows in p["os"].items()
                    for r in rows if r.get("engine")}
            out["l 默认档搬对"] = shipped == want
            c10 = copy.deepcopy(committed)
            for o in c10["engines"]["llama"]["os"]["win-x64"]["options"]:
                if o["backend"] == "cuda":
                    o["extra_mib_vs_default"] += 100
            out["m 体积差被手改要叫"] = mod.check(c10, fresh) != []
            opts = [o for b in fresh["engines"].values() for r in b["os"].values()
                    for o in r["options"]]
            out["p 落地体积要么有数要么写明未知"] = (
                len(opts) == fresh["stats"]["可选档"]
                and all((o["landed_mib"] is None) == ("landed_note" in o) for o in opts)
                and sum(1 for o in opts if o["landed_mib"] is not None)
                == fresh["stats"]["有落地体积的档"])
            pmap = {a["asset"]: a.get("landed_mib") for a in par["assets"] if a.get("read")}
            out["q 落地体积逐颗等于 parity"] = all(
                o["landed_mib"] == pmap.get(o["asset"]) for o in opts if o["landed_mib"])
            par2 = json.loads(json.dumps(par))
            for a in par2["assets"]:
                if a.get("is_default") and a.get("read"):
                    a["read"] = False
                    a.pop("landed_mib", None)
                    break
            out["r 默认档缺落地体积被报"] = any(
                "没有落地体积" in x or "两份产者对这颗包不一致" in x
                for x in mod.build(matrix(), plan(), par2)[1])
        except Exception as exc:                     # noqa: BLE001
            for k in ("b 重算与漂移", "c 假伴生包被报", "d 一档两颗被报", "e 没哈希被报",
                      "f 默认档凭空消失被报", "g 推断写成事实被报", "h CPU 退路没了被报",
                      "i 缺席清单被抹被报", "j 输入形状漂了被报", "k 撤光加速档被地板拦",
                      "l 默认档搬对", "m 体积差被手改要叫", "p 落地体积要么有数要么写明未知",
                      "q 落地体积逐颗等于 parity", "r 默认档缺落地体积被报"):
                out.setdefault(k, False)
            out["_b 异常"] = str(exc)[:60]
        return out

    def test_baseline_probes_all_hold(self) -> None:
        self.assertEqual({k: v for k, v in self.probes(bc).items() if not k.startswith("_")},
                         {"a 离线对照": True, "b 重算与漂移": True, "c 假伴生包被报": True,
                          "d 一档两颗被报": True, "e 没哈希被报": True,
                          "f 默认档凭空消失被报": True, "g 推断写成事实被报": True,
                          "h CPU 退路没了被报": True, "i 缺席清单被抹被报": True,
                          "j 输入形状漂了被报": True, "k 撤光加速档被地板拦": True,
                          "l 默认档搬对": True, "m 体积差被手改要叫": True,
                          "p 落地体积要么有数要么写明未知": True,
                          "q 落地体积逐颗等于 parity": True,
                          "r 默认档缺落地体积被报": True})

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
        self.assertEqual(len(self.MUTANTS), 12)


if __name__ == "__main__":
    unittest.main()
