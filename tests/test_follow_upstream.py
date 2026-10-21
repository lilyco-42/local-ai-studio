"""`tools/follow_upstream.py` 的离线判据：新版只报不跟，钉值坏了才红。

这条车道最容易写错的两个方向都在这里钉住：
- 上游一天发好几个版本 —— 把"有新版"当红，红就不再是信号；
- 反向也危险 —— 把"钉住那颗被撤/被重打包"也放过去，钉值就悄悄失去证据。
所以每条"不该红"的用例都配一条"该红"的正向对照，apply 那条还要求别的引擎一个字节不许动。

"有新版"这一侧用合成候选（把钉住那颗改名后插到窗口顶部）：钉值一旦追平上游，
真实窗口里就没有更新的版本，判据不能靠"钉值恰好落后"才验得动。
"""
import contextlib
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import follow_upstream as fu  # noqa: E402
import payload_plan as pp     # noqa: E402
import backends as bd         # noqa: E402

CAP = os.path.join(HERE, "fixtures", "release-window.json")
ASSETS = os.path.join(HERE, "fixtures", "backend_assets.json")
PLAN = os.path.join(ROOT, "pins", "payload-plan.json")
MATRIX = os.path.join(ROOT, "pins", "backend-matrix.json")
CAND = "b99999"


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def base_inputs():
    return load(CAP), load(PLAN), load(MATRIX), load(ASSETS)


def key_of(plan):
    return f"{fu.ep.UPSTREAM['llama']['repo']}@{fu.pinned_release(plan, 'llama')}"


def pin_of(plan):
    return fu.pinned_release(plan, "llama")


def release_of(caps, tag):
    return next(r for r in caps[key_of(load(PLAN))]["releases"] if r["tag"] == tag)


def with_candidate(caps, plan):
    """把钉住那颗改个更新的 tag 插到窗口顶部，当作上游刚发的候选。"""
    pin = pin_of(plan)
    key = key_of(plan)
    rel = copy.deepcopy(next(r for r in caps[key]["releases"] if r["tag"] == pin))
    rel["tag"] = CAND
    rel["published_at"] = "2099-01-01T00:00:00Z"
    rel["assets"] = [dict(a, name=a["name"].replace(pin, CAND)) for a in rel["assets"]]
    caps[key]["releases"].insert(0, rel)
    return rel


def fake_entries(url):
    if url.endswith(".zip"):
        return [{"name": f"llama-{CAND}/llama-server.exe", "size": 10, "packed": 5},
                {"name": f"llama-{CAND}/llama-server-impl.dll", "size": 10, "packed": 5}]
    return [{"name": f"llama-{CAND}/llama-server", "size": 10, "packed": None}]


def fake_release(tag=CAND):
    """窗口捕获只有 name/size/sha256；真 GitHub release 还带下载地址，`rows_for` 要用。"""
    caps = load(CAP)
    with_candidate(caps, load(PLAN))
    rel = fu.as_ep_release(release_of(caps, tag))
    for a in rel["assets"]:
        a["browser_download_url"] = f"https://example.invalid/{a['name']}"
    return rel


class TestEvaluate(unittest.TestCase):
    def test_newer_candidate_is_a_report_not_a_red(self):
        caps, plan, matrix, assets = base_inputs()
        with_candidate(caps, plan)
        r = fu.evaluate(caps, plan, matrix, assets)
        self.assertEqual(r["decision"], "newer_available")
        self.assertEqual(r["behind"], 1)
        self.assertEqual(r["candidate"], CAND)
        self.assertIs(r["red"], False)
        self.assertEqual(r["pinned_damage"], [])
        self.assertEqual(r["candidate_missing"], [])
        self.assertEqual(r["candidate_unknown"], [])
        self.assertEqual(r["candidate_no_digest"], [])

    def test_repackaged_pin_is_red(self):
        caps, plan, matrix, assets = base_inputs()
        rt = fu.runtime_assets(release_of(caps, pin_of(plan)))[0]
        rt["sha256"] = "f" * 64
        r = fu.evaluate(caps, plan, matrix, assets)
        self.assertEqual(r["decision"], "pinned_damaged")
        self.assertIs(r["red"], True)

    def test_removed_pin_is_red(self):
        caps, plan, matrix, assets = base_inputs()
        k = key_of(plan)
        pin = pin_of(plan)
        caps[k]["releases"] = [r for r in caps[k]["releases"] if r["tag"] != pin]
        r = fu.evaluate(caps, plan, matrix, assets)
        self.assertEqual(r["decision"], "pinned_damaged")
        self.assertIs(r["red"], True)

    def test_missing_capture_is_not_treated_as_up_to_date(self):
        caps, plan, matrix, assets = base_inputs()
        caps.pop(key_of(plan))
        r = fu.evaluate(caps, plan, matrix, assets)
        self.assertEqual(r["decision"], "pinned_damaged")
        self.assertIsNone(r["behind"])

    def test_up_to_date_is_not_red(self):
        caps, plan, matrix, assets = base_inputs()
        k = key_of(plan)
        pin = pin_of(plan)
        caps[k]["releases"] = [r for r in caps[k]["releases"]
                               if fu.ep.release_rank(r["tag"]) <= fu.ep.release_rank(pin)]
        r = fu.evaluate(caps, plan, matrix, assets)
        self.assertEqual(r["decision"], "up_to_date")
        self.assertIs(r["red"], False)

    def test_candidate_missing_a_shipped_target_blocks(self):
        caps, plan, matrix, assets = base_inputs()
        cand = with_candidate(caps, plan)
        cand["assets"] = [a for a in cand["assets"]
                          if not (bd.classify(a["name"], CAND)["os"] == "win-x64"
                                  and bd.classify(a["name"], CAND)["backend"] == "cpu")]
        r = fu.evaluate(caps, plan, matrix, assets)
        self.assertEqual(r["decision"], "blocked")
        self.assertIs(r["red"], False, "上游发版本身不是红")
        self.assertIn("llama/win-cpu-x64", r["candidate_missing"])

    def test_candidate_without_a_digest_blocks(self):
        caps, plan, matrix, assets = base_inputs()
        cand = with_candidate(caps, plan)
        fu.runtime_assets(cand)[0]["sha256"] = ""
        r = fu.evaluate(caps, plan, matrix, assets)
        self.assertEqual(r["decision"], "blocked")
        self.assertEqual(len(r["candidate_no_digest"]), 1)

    def test_unrecognised_backend_word_blocks(self):
        caps, plan, matrix, assets = base_inputs()
        cand = with_candidate(caps, plan)
        cand["assets"].append({"name": f"llama-{CAND}-bin-ubuntu-hip-x64.tar.gz",
                               "size": 1, "sha256": "0" * 64})
        r = fu.evaluate(caps, plan, matrix, assets)
        self.assertEqual(r["decision"], "blocked")
        self.assertTrue(r["candidate_unknown"])

    def test_matrix_disagreeing_with_the_plan_is_red(self):
        caps, plan, matrix, assets = base_inputs()
        matrix["engines"]["llama"]["release"] = "b11769"
        r = fu.evaluate(caps, plan, matrix, assets)
        self.assertEqual(r["decision"], "pinned_damaged")


class TestRowsAndApply(unittest.TestCase):
    def build(self, plan):
        old = pp.ENTRIES_OVERRIDE
        pp.ENTRIES_OVERRIDE = fake_entries
        try:
            return fu.build_rows(plan, "llama", fake_release())
        finally:
            pp.ENTRIES_OVERRIDE = old

    def test_rows_are_measured_with_the_upstream_digest(self):
        plan = load(PLAN)
        rows, bad = self.build(plan)
        self.assertEqual(bad, [])
        self.assertEqual([r["os"] for r in rows], fu.shipped_os(plan, "llama"))
        self.assertEqual({r["os"] for r in rows}, {"win-x64", "linux-x64", "macos-arm64"})
        self.assertTrue(all(r["release"] == CAND for r in rows))
        self.assertTrue(all(r["digest"].startswith("sha256:") for r in rows))
        self.assertTrue(all(r["status"] == "ok" for r in rows))

    def test_apply_touches_only_the_engine_it_was_given(self):
        plan = load(PLAN)
        rows, _bad = self.build(plan)
        new = fu.apply_rows(plan, "llama", rows)
        self.assertEqual(len(new["rows"]), len(plan["rows"]), "行数不许因为重钉而变")
        for label in ("win-x64", "linux-x64", "macos-arm64"):
            before = [r for r in plan["os"][label] if r.get("engine") != "llama"]
            after = [r for r in new["os"][label] if r.get("engine") != "llama"]
            self.assertEqual(after, before, f"{label} 里别的引擎/gap 行被改动了")
        self.assertEqual({r["release"] for r in new["rows"] if r.get("engine") == "llama"},
                         {CAND})
        for label in new["os"]:
            want = round(sum((r.get("archive_mib") or 0) for r in new["os"][label]), 1)
            self.assertEqual(new["totals_mib"][label], want)

    def test_apply_keeps_the_gap_rows(self):
        plan = load(PLAN)
        rows, _bad = self.build(plan)
        new = fu.apply_rows(plan, "llama", rows)
        gaps = [(g["os"], g["capability"]) for g in plan["gaps"]]
        self.assertEqual([(g["os"], g["capability"]) for g in new["gaps"]], gaps)

    def test_update_refuses_a_same_or_older_tag(self):
        plan = load(PLAN)
        pin = pin_of(plan)
        for tag in (pin, "b11700"):
            with contextlib.redirect_stdout(io.StringIO()):
                rc = fu.update(plan, "llama", tag, {"tag_name": tag, "assets": []}, False)
            self.assertEqual(rc, 1, tag)

    def test_apply_to_a_temp_copy_reports_the_stale_tables(self):
        """写盘成功但派生表还带旧 tag 时必须退出码 2 —— 半套重钉不许说成完成。"""
        with tempfile.TemporaryDirectory() as d:
            tmp = os.path.join(d, "payload-plan.json")
            shutil.copyfile(fu.PAYLOAD, tmp)
            old_payload, old_override = fu.PAYLOAD, pp.ENTRIES_OVERRIDE
            fu.PAYLOAD, pp.ENTRIES_OVERRIDE = tmp, fake_entries
            try:
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    rc = fu.update(load(PLAN), "llama", CAND, fake_release(), True)
            finally:
                fu.PAYLOAD, pp.ENTRIES_OVERRIDE = old_payload, old_override
            self.assertEqual(rc, 2)
            self.assertIn("退出码 2", out.getvalue())
            doc = load(tmp)
            self.assertEqual({r["release"] for r in doc["rows"]
                              if r.get("engine") == "llama"}, {CAND})
            self.assertEqual(fu.pinned_release(load(PLAN), "llama"), pin_of(load(PLAN)),
                             "committed plan 不该被这条测试改动")

    def test_dependents_list_matches_the_files_on_disk(self):
        for path, _cmd in fu.DEPENDENTS:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, path)), path)

    def test_residual_names_every_table_still_carrying_the_old_tag(self):
        stale = {p for p, _cmd in fu.residual(ROOT, pin_of(load(PLAN)))}
        self.assertEqual(len(stale), len(fu.DEPENDENTS), "有派生表没被扫到或被静默跳过")
        self.assertIn("pins/backend-matrix.json", stale)
        self.assertIn("pins/runtime-versions.json", stale)


class TestCliExitCode(unittest.TestCase):
    def run_check(self, *args):
        with contextlib.redirect_stdout(io.StringIO()):
            return fu.main(["--check", *args])

    def write_caps(self, d, mutate):
        caps = load(CAP)
        mutate(caps)
        path = os.path.join(d, "caps.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(caps, fh, ensure_ascii=False)
        return path

    def test_up_to_date_exits_zero(self):
        self.assertEqual(self.run_check(), 0)

    def test_newer_candidate_exits_zero(self):
        def mutate(caps):
            with_candidate(caps, load(PLAN))
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_check("--captures", self.write_caps(d, mutate)), 0)

    def test_damaged_pin_exits_one(self):
        def mutate(caps):
            fu.runtime_assets(release_of(caps, pin_of(load(PLAN))))[0]["sha256"] = "f" * 64
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.run_check("--captures", self.write_caps(d, mutate)), 1)

    def test_blocked_candidate_only_fails_under_strict(self):
        def mutate(caps):
            cand = with_candidate(caps, load(PLAN))
            cand["assets"] = [a for a in cand["assets"]
                              if not (bd.classify(a["name"], CAND)["os"] == "win-x64"
                                      and bd.classify(a["name"], CAND)["backend"] == "cpu")]
        with tempfile.TemporaryDirectory() as d:
            path = self.write_caps(d, mutate)
            self.assertEqual(self.run_check("--captures", path), 0)
            self.assertEqual(self.run_check("--captures", path, "--strict"), 1)


if __name__ == "__main__":
    unittest.main()
