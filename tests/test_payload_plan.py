"""`tools/payload_plan.py` 的离线测试：装机清单的三条判据必须真会红。

全部不联网（`ENTRIES_OVERRIDE` 是测试缝）。要证明的是：
主二进制被过滤掉时必须红、排除规则从没命中过时必须红、win 的 `x-impl.dll` 成了孤儿时必须红 ——
以及"每个包都得有 test-*"那种**错误的严格**不会被写回来（llama/sd 的包本来就没有测试程序）。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import payload_plan as pp  # noqa: E402

FIX = os.path.join(HERE, "fixtures")


def targets(**over):
    """造一份最小 discover 产物表：三个 OS × 三个能力，够用就行。"""
    base = {}
    for eng, (plat, arch, backend, asset) in {
        "llama": ("win", "x64", "cpu", "llama.zip"),
        "whisper": ("win", "x64", "base", "whisper.zip"),
        "sd": ("win", "x64", "cpu", "sd.zip"),
    }.items():
        t = {"engine": eng, "platform": plat, "arch": arch, "backend": backend,
             "bundle": "main", "asset": asset, "url": f"https://x/{asset}",
             "size": 1048576, "digest": "sha256:" + "0" * 64, "release": "r1"}
        t.update(over.get(eng, {}))
        base[f"{eng}-{plat}-{backend}-{arch}"] = t
    return base


def es(*names, size=1000):
    return [{"name": n, "size": size, "packed": size // 2, "dir": n.endswith("/")} for n in names]


LLAMA = es("llama-b11727/", "llama-b11727/llama-server.exe", "llama-b11727/llama-server-impl.dll",
           "llama-b11727/ggml-cpu-alderlake.dll")
WHISPER = es("Release/whisper-cli.exe", "Release/test-arg.exe", "Release/test-vad.exe",
             "Release/whisper-server.exe", "Release/SDL2.dll")
SD = es("sd-cli", "sd-server", "libggml-cpu-sapphirerapids.so", "libstable-diffusion.so")

TABLES = {"llama.zip": LLAMA, "whisper.zip": WHISPER, "sd.zip": SD}
OS = ["win-x64"]


def run(entries_table, os_labels=None):
    """plan() 走的是真夹具（里面的 asset 名是上游真名），所以按引擎子串认领替身条目表。"""
    def fake(url):
        for asset, rows in entries_table.items():
            if asset.split(".")[0].rstrip("-") in url:
                return rows
        raise AssertionError(f"替身表没覆盖这个 URL：{url}")
    real = pp.ENTRIES_OVERRIDE
    pp.ENTRIES_OVERRIDE = fake
    try:
        return pp.plan(os_labels or OS, True, fixture_dir=FIX)
    finally:
        pp.ENTRIES_OVERRIDE = real


class TestRules(unittest.TestCase):
    def test_partition_drops_only_test_binaries(self) -> None:
        keep, drop = pp.partition(["Release/whisper-cli.exe", "Release/test-arg.exe",
                                   "bin/test-vad", "lib/libggml.so"])
        self.assertEqual(drop, ["Release/test-arg.exe", "bin/test-vad"])
        self.assertIn("lib/libggml.so", keep)

    def test_orphan_runtime_library_is_named(self) -> None:
        bad = pp.pairing_problems(es("llama-cli.exe", "llama-cli-impl.dll", "stray-impl.dll"))
        self.assertEqual(bad, ['stray-impl.dll 没有同名的 .exe（孤儿运行库，收集规则或上游条目变了）'])

    def test_paired_windows_payload_is_clean(self) -> None:
        self.assertEqual(pp.pairing_problems(LLAMA), [])

    def test_selection_prefers_the_main_bundle(self) -> None:
        t = targets()
        t["extra"] = dict(t["llama-win-cpu-x64"], bundle="cudart", asset="cudart.zip")
        got = pp.pick(t, {"engine": "llama", "platform": "win", "arch": "x64", "backend": "cpu"})
        self.assertEqual(got["asset"], "llama.zip")


class TestControlsAreLoadBearing(unittest.TestCase):
    def test_baseline_passes(self) -> None:
        out, bad = run(TABLES)
        self.assertEqual(bad, [], bad)
        self.assertEqual({r["capability"] for r in out["rows"]}, {"对话", "字幕", "生图"})

    def test_dropping_the_main_binary_is_red(self) -> None:
        """收集规则要是把 `llama-server` 滤掉了，装机就不可用 —— 这条必须红。"""
        table = dict(TABLES, **{"llama.zip": es("llama-b11727/ggml-cpu-alderlake.dll")})
        _out, bad = run(table)
        self.assertTrue(any("主二进制" in b for b in bad), bad)

    def test_exclusion_that_never_fires_is_red(self) -> None:
        """负向对照：包里没有 test-* 时整张计划要红一次，提醒"这条规则是装饰还是形状变了"。
        但**不能**逐包红 —— 那是上一版写错的严格（llama/sd 本来就没有测试程序）。"""
        table = {k: [e for e in v if "test-" not in e["name"]] for k, v in TABLES.items()}
        _out, bad = run(table)
        self.assertTrue(any("排除规则在所有被读的包里一条都没命中" in b for b in bad), bad)
        self.assertEqual(len([b for b in bad if "排除规则" in b]), 1, bad)

    def test_orphan_pairing_is_red(self) -> None:
        table = dict(TABLES, **{"llama.zip": LLAMA + es("ghost-impl.dll")})
        _out, bad = run(table)
        self.assertTrue(any("ghost-impl.dll" in b for b in bad), bad)

    def test_missing_capability_becomes_a_gap_row_not_a_silent_skip(self) -> None:
        """选不中时必须留一行"上游无产物"，而不是少一行让人以为清单本来就这么短。"""
        import copy
        pp.OS_DEFAULTS = copy.deepcopy(pp.OS_DEFAULTS)
        old = pp.OS_DEFAULTS[0][1]["字幕"]
        pp.OS_DEFAULTS[0][1]["字幕"] = {"engine": "whisper", "platform": "win",
                                       "arch": "x64", "backend": "不存在的后端"}
        try:
            out, bad = run(TABLES)
        finally:
            pp.OS_DEFAULTS[0][1]["字幕"] = old
        gaps = [r for r in out["rows"] if r["status"] == "上游无产物"]
        self.assertEqual(len(gaps), 1, out["rows"])
        self.assertIn("唯一命中 0 次", gaps[0]["note"])
        self.assertEqual(len(out["rows"]), 3, "行数不许因为选不中而变少")

    def test_offline_mode_does_not_claim_per_file_lists(self) -> None:
        out, bad = run(TABLES)
        del out, bad
        plan, bad2 = pp.plan(OS, False, fixture_dir=FIX)
        self.assertEqual(bad2, [])
        for r in plan["rows"]:
            self.assertNotIn("shipped", r, "没读中央目录就不许给出逐文件结论")


class TestCommittedPlan(unittest.TestCase):
    def test_plan_json_matches_the_measured_shape(self) -> None:
        p = os.path.join(HERE, "..", "pins", "payload-plan.json")
        with open(p, encoding="utf-8") as fh:
            import json
            doc = json.load(fh)
        self.assertEqual(len(doc["rows"]), 9, "3 个 OS × 3 项能力")
        self.assertIn("macos-arm64/字幕",
                      {f"{g['os']}/{g['capability']}" for g in doc["gaps"]},
                      "whisper 上游没有 macOS 产物，这个缺口必须一直看得见")
        win = {r["capability"]: r for r in doc["os"]["win-x64"]}
        self.assertEqual(win["字幕"]["excluded_count"], 10,
                         "whisper 的 41 条里排除 10 个 test-* 是 ADR 条目表记过的形状")
        for r in doc["rows"]:
            if r["status"] == "ok":
                self.assertTrue(r.get("digest", "").startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
