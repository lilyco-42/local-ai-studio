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
PLAN = os.path.join(HERE, "..", "pins", "payload-plan.json")


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
    def load_plan(self):
        import json
        with open(PLAN, encoding="utf-8") as fh:
            return json.load(fh)

    def test_plan_mode_selects_the_committed_rows(self) -> None:
        """车道曾经对着 b11727 夹具跑：钉子换到 b11799 后旧附件还在，车道一直绿着。
        现在 --plan 的输入就是出包那张表，逐格必须等于它的 ok 行（缺口行也照原样保留）。"""
        doc = self.load_plan()
        out, bad = pp.plan(["win-x64", "linux-x64", "macos-arm64"], False, plan_path=PLAN)
        self.assertEqual(bad, [])
        committed = {(r["os"], r["capability"]): r.get("asset") for r in doc["rows"]}
        got = {(r["os"], r["capability"]): r.get("asset") for r in out["rows"]}
        self.assertEqual(got, committed)
        releases = {(r["os"], r["capability"]): r.get("release") for r in doc["rows"]}
        for r in out["rows"]:
            if r["status"] == "ok":
                self.assertIn(f"/{releases[r['os'], r['capability']]}/", r["url"])

    def test_plan_mode_keeps_exact_verified_bytes(self) -> None:
        """tar 行必须用实测整包字节当 expect_bytes —— 用 0.1 MiB 的圆整值会把截断查成通过。"""
        doc = self.load_plan()
        targets, skipped = pp.targets_from_plan(PLAN)
        self.assertEqual(skipped, [])
        for r in doc["rows"]:
            if r["status"] != "ok":
                continue
            t = targets[f"{r['os']}/{r['capability']}"]
            self.assertEqual((t["url"], t["digest"], t["release"]),
                             (r["url"], r["digest"], r["release"]))
            vb = r.get("verified_bytes") or {}
            if vb.get("bytes"):
                self.assertEqual(t["size"], vb["bytes"],
                                 f"{r['asset']} 的 size 不是实测字节")

    def test_plan_mode_live_reads_the_planned_assets(self) -> None:
        """用替身条目表证明 --plan --live 读的是计划里那三个 URL，而不是夹具窗口里的旧包；
        同时替身 whisper 表里的 test-* 仍被排除（排除规则真命中过）。"""
        import json
        seen = []

        def fake(url):
            seen.append(url)
            for asset, rows in TABLES.items():
                if asset.split(".")[0].rstrip("-") in url:
                    return rows
            raise AssertionError(f"替身表没覆盖这个 URL：{url}")

        real = pp.ENTRIES_OVERRIDE
        pp.ENTRIES_OVERRIDE = fake
        try:
            out, bad = pp.plan(["win-x64"], True, plan_path=PLAN)
        finally:
            pp.ENTRIES_OVERRIDE = real
        self.assertEqual(bad, [])
        with open(PLAN, encoding="utf-8") as fh:
            doc = json.load(fh)
        want = {r["url"] for r in doc["rows"] if r["os"] == "win-x64" and r["status"] == "ok"}
        self.assertEqual(set(seen), want)
        # 替身 whisper 表里放了 2 个 test-*，所以排除计数就是 2（真包是 10）—— 这里证的是
        # 排除规则在 plan 模式下仍然真命中过，而不是数字本身
        self.assertEqual(sum(r.get("excluded_count") or 0 for r in out["rows"]), 2)

    def test_plan_mode_refuses_an_inconsistent_row(self) -> None:
        """负向对照：手改过的计划（引擎对不上默认档 / 空表）必须当场报错，不能悄悄少一格。"""
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for name, rows in {
                "wrong-engine": [{"os": "win-x64", "capability": "对话", "engine": "whisper",
                                  "release": "x", "asset": "a.zip", "url": "https://x/a.zip",
                                  "status": "ok"}],
                "empty": [],
            }.items():
                p = os.path.join(d, name + ".json")
                with open(p, "w", encoding="utf-8") as fh:
                    json.dump({"rows": rows}, fh)
                with self.assertRaises(SystemExit):
                    pp.targets_from_plan(p)

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


class TestTarAndGapEvidence(unittest.TestCase):
    """tar.gz 那几条路（读成员表 + 体积与整包哈希复核）与缺口行的"上游到底发了什么"。"""

    def make_tar(self, d, names, gz=True):
        import io
        import tarfile
        path = os.path.join(d, ("p.tar.gz" if gz else "p.tar"))
        with tarfile.open(path, "w:gz" if gz else "w") as tf:
            for n, sz in names:
                ti = tarfile.TarInfo(n)
                ti.size = sz
                tf.addfile(ti, io.BytesIO(b"x" * sz))
        return path

    def sha(self, path):
        import hashlib
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def test_tar_summary_reads_members_and_confirms_bytes(self) -> None:
        import tempfile
        names = [("llama-b11727/llama-server", 300), ("llama-b11727/libggml-cpu.so", 700),
                 ("llama-b11727/test-me", 5)]
        with tempfile.TemporaryDirectory() as d:
            p = self.make_tar(d, names)
            es, meta = pp.tar_summary(p, os.path.getsize(p), self.sha(p))
        self.assertEqual([e["name"] for e in es], [n for n, _ in names])
        self.assertEqual(sum(e["size"] for e in es), 1005)
        self.assertTrue(all(e["packed"] is None for e in es),
                        "tar 没有逐文件压缩大小，不能填个数字假装是 zip")
        self.assertTrue(meta["digest_matches"], meta)

    def test_digest_matches_is_false_when_nothing_was_compared(self) -> None:
        """没给 digest 可比时不许写 True —— 那一栏是给"验过"用的，不是给"没验"用的。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = self.make_tar(d, [("a/bin", 10)])
            size = os.path.getsize(path)
            _es, meta = pp.tar_summary(path)
        self.assertIs(meta["digest_matches"], False, meta)
        self.assertEqual(meta["bytes"], size, meta)

    def test_truncated_package_is_not_reported_as_read(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = self.make_tar(d, [("a/bin", 10)])
            with self.assertRaises(SystemExit) as e:
                pp.tar_summary(p, os.path.getsize(p) + 1, None)
        self.assertIn("截断", str(e.exception))

    def test_unexpected_digest_is_fatal(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = self.make_tar(d, [("a/bin", 10)])
            with self.assertRaises(SystemExit) as e:
                pp.tar_summary(p, os.path.getsize(p), "f" * 64)
        self.assertIn("不是上游那个", str(e.exception))

    def test_unknown_format_is_not_guessed(self) -> None:
        es, reader, meta = pp.entries_for("https://x/pkg.7z", 10, None)
        self.assertEqual((es, reader, meta), ([], "unknown-format", {}))

    def gap_targets(self):
        """macos 上 llama/sd 有主包、whisper 没有 —— 缺口行该点名 xcframework。"""
        def t(eng, plat, arch, backend, asset):
            return {"engine": eng, "platform": plat, "arch": arch, "backend": backend,
                    "bundle": "main", "asset": asset, "url": f"https://x/{asset}",
                    "size": 1048576, "digest": "sha256:" + "0" * 64, "release": "b5454"}
        return {
            "llama": t("llama", "macos", "arm64", "base", "llama-macos-arm64.tar.gz"),
            "sd": t("sd", "macos", "arm64", "base", "sd-macos-arm64.zip"),
            "whisper-win": t("whisper", "win", "x64", "base", "whisper-win.zip"),
        }

    SKIPPED = [{"engine": "whisper", "release": "b5454", "asset": "whisper-b5454-xcframework.zip",
                "url": "https://x/whisper-b5454-xcframework.zip", "size": 62800000,
                "because": "排除词 'xcframework'（库包/附带资产，不是能起进程的产物）"}]

    FRAMEWORK = es("whisper.xcframework/macos-arm64/whisper.framework/whisper",
                   "whisper.xcframework/macos-arm64/whisper.framework/Headers/whisper.h",
                   "whisper.xcframework/macos-arm64/dSYMs/whisper.dSYM/Contents/Info.plist")

    def run_gap(self, entries):
        def fake(url):
            if "xcframework" in url:
                return entries
            if "llama" in url:
                return es("llama-server")
            return es("sd-cli", "sd-server")
        real = pp.ENTRIES_OVERRIDE
        pp.ENTRIES_OVERRIDE = fake
        try:
            return pp.rows_for("macos-arm64", self.gap_targets(), True, self.SKIPPED)
        finally:
            pp.ENTRIES_OVERRIDE = real

    def test_gap_row_names_the_apple_asset_and_proves_it_is_not_a_cli(self) -> None:
        rows, bad = self.run_gap(self.FRAMEWORK)
        sub = next(r for r in rows if r["capability"] == "字幕")
        self.assertEqual(sub["status"], "上游无产物")
        self.assertEqual([a["asset"] for a in sub["same_engine_platform_assets"]],
                         ["whisper-b5454-xcframework.zip"])
        self.assertIn("xcframework", sub["same_engine_platform_assets"][0]["because"])
        self.assertEqual(sub["asset_evidence"][0]["has_main_binary"], False,
                         "framework 里没有 whisper-cli，这一条要数得出来")
        self.assertEqual(bad, [])

    def test_same_judge_says_true_when_the_asset_does_carry_the_binary(self) -> None:
        """正向对照：换成含 whisper-cli 的条目表，判据就得说 True（否则上面那条是永远 False）。"""
        _rows, _bad = None, None
        real = pp.ENTRIES_OVERRIDE
        pp.ENTRIES_OVERRIDE = lambda url: es("whisper-cli", "whisper-server")
        try:
            rows = pp.rows_for("macos-arm64", self.gap_targets(), True, self.SKIPPED)[0]
        finally:
            pp.ENTRIES_OVERRIDE = real
        sub = next(r for r in rows if r["capability"] == "字幕")
        self.assertEqual(sub["asset_evidence"][0]["has_main_binary"], True, sub)

    def test_committed_plan_carries_the_apple_evidence(self) -> None:
        """已提交那张表里，macOS 字幕这一行必须点名 xcframework 并给出成员表证据。"""
        import json
        with open(os.path.join(HERE, "..", "pins", "payload-plan.json"), encoding="utf-8") as fh:
            plan = json.load(fh)
        sub = next(r for r in plan["os"]["macos-arm64"] if r["capability"] == "字幕")
        self.assertTrue(sub["same_engine_platform_assets"], sub)
        self.assertTrue(sub["asset_evidence"], sub)
        for ev in sub["asset_evidence"]:
            self.assertFalse(ev["has_main_binary"], ev)
            self.assertGreater(ev["entries"], 0)
        # 三颗 tar 包读了成员表之后，Linux/macOS 不再是"只能由 CI 解开作答"
        for os_label in ("linux-x64", "macos-arm64"):
            known = [r for r in plan["os"][os_label] if r.get("engine")]
            self.assertTrue(all(r.get("uncompressed_mib") for r in known),
                            (os_label, [(r["capability"], r["status"]) for r in known]))
            for r in known:
                if r["status"] == "needs_extract":
                    self.fail(f"{os_label}/{r['capability']} 还标着 needs_extract")
                if r.get("verified_bytes"):
                    self.assertTrue(r["verified_bytes"]["digest_matches"], r["asset"])


if __name__ == "__main__":
    unittest.main()
