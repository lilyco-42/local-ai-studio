"""Action 钉法的测试：浮动 tag 必须被抓，形状基线必须由权威读者对过。

这一轮的教训值得留在测试里：`shape()` 第一版是自己写的缩进正则，
把 `actions/checkout` 的 21 个输入读成 8 个（`description: |-` 折叠块让它提前收尾）。
如果只测"判据会红"，这张基线表就一直是假的 —— 所以这里有一条**拿 pyyaml 对每个
真 action.yml 复核键名**的用例（读缓存，没有缓存就跳过并明说是跳过）。
"""
from __future__ import annotations

import ast
import contextlib
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
import action_pins as ap        # noqa: E402

ART = os.path.join(ROOT, "pins", "actions.json")
CLI = os.path.join(ROOT, "tools", "action_pins.py")
SHA = "a" * 40
REAL_SHAS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/setup-python": "a26af69be951a213d495a4c3e4e4022e16d87065",
    "actions/upload-artifact": "ea165f8d65b6e75b540449e92b4886f43607fa02",
    "actions/download-artifact": "d3f86a106a0bac45b974a628896c90dbdf5c8093",
    "tauri-apps/tauri-action": "1deb371b0cd8bd54025b384f1cd735e725c4060f",
}


def artifact():
    with open(ART, encoding="utf-8") as fh:
        return json.load(fh)


class TestParseUses(unittest.TestCase):
    def test_sha_with_tag_comment_is_read(self) -> None:
        rows = ap.parse_uses({"ci.yml": f"      - uses: actions/checkout@{SHA}  # v4.4.0\n"})
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["ref"], rows[0]["tag_comment"]), (SHA, "v4.4.0"))

    def test_floating_tag_is_a_row_too(self) -> None:
        rows = ap.parse_uses({"ci.yml": "      - uses: actions/checkout@v4\n"})
        self.assertEqual(rows[0]["ref"], "v4")

    def test_occurrences_are_counted_per_file(self) -> None:
        rows = ap.parse_uses({"a.yml": f"- uses: x@{SHA}\n        uses: x@{SHA}\n",
                              "b.yml": f"uses: x@{SHA}\n"})
        self.assertEqual(rows[0]["workflows"], {"a.yml": 2, "b.yml": 1})

    def test_local_actions_are_not_third_party(self) -> None:
        """要带 ref 才测得到这条排除 —— 不带 `@` 的写法本来就匹配不上 USES_RX。"""
        self.assertEqual(ap.parse_uses({"c.yml": "uses: ./my-action@v1\n"}), [])
        self.assertEqual(len(ap.parse_uses({"c.yml": "uses: actions/checkout@v4\n"})), 1)

    def test_workflows_have_no_floating_tag_left(self) -> None:
        """这条是整个改动的落点：仓库自己的出包路径上不许留会移动的 tag。"""
        rows = ap.parse_uses(ap.read_workflows())
        loose = [r["uses"] for r in rows if not ap.SHA_RX.match(r["ref"])]
        self.assertEqual(loose, [], f"还有浮动 tag 没钉：{loose}")

    def test_counts_match_the_measured_totals(self) -> None:
        doc = artifact()
        self.assertEqual(doc["counts"], {"workflow_files": 6, "uses_total": 66, "distinct": 5})


class TestShape(unittest.TestCase):
    def test_reads_using_and_inputs(self) -> None:
        using, inputs = ap.shape("name: t\nruns:\n  using: 'node20'\n  main: i.js\n"
                                 "inputs:\n  b:\n    default: 1\n  a:\n    description: x\n")
        self.assertEqual((using, inputs), ("node20", ["a", "b"]))

    def test_folded_description_does_not_end_the_input_table(self) -> None:
        """就是这条把第一版的缩进正则骗了：`|-` 折叠块之后还有别的输入键。"""
        yml = ("runs:\n  using: node20\ninputs:\n  fetch-depth:\n"
               "    description: |-\n      第一行\n      第二行\n    default: 1\n"
               "  clean:\n    default: true\n")
        self.assertEqual(ap.shape(yml)[1], ["clean", "fetch-depth"])

    def test_nested_inputs_under_steps_are_not_top_level(self) -> None:
        using, inputs = ap.shape("runs:\n  using: composite\nsteps:\n  - uses: x\n"
                                 "    inputs:\n      fake: 1\n")
        self.assertEqual((using, inputs), ("composite", []))

    def test_unparsable_file_reads_as_nothing(self) -> None:
        self.assertEqual(ap.shape("runs: [oops\n"), (None, []))

    def test_shape_agrees_with_yaml_on_the_real_pinned_files(self) -> None:
        """基线必须由权威读者复核：把每个已钉 SHA 的 action.yml 用 pyyaml 再读一遍。"""
        import yaml
        doc = artifact()
        rows = doc["pins"] + doc["reserved_for_installer_lane"]
        checked = 0
        for r in rows:
            yml = ap.raw(r["repo"], r["ref"])
            if yml is None:
                continue                     # 取不到就明说不查，不当通过
            d = yaml.safe_load(yml)
            self.assertEqual(r["inputs"], sorted((d.get("inputs") or {}).keys()), r["repo"])
            self.assertEqual(r["runs_using"], (d.get("runs") or {}).get("using"), r["repo"])
            checked += 1
        if checked == 0:
            self.skipTest("一个 action.yml 都没取到（这台机器没网）—— "
                          "这条不能算通过，只算没查")


class TestPinGuards(unittest.TestCase):
    def test_floating_tag_is_rejected(self) -> None:
        _r, bad = ap.pin_one({"uses": "actions/checkout@v4", "repo": "actions/checkout",
                              "ref": "v4", "workflows": {}}, live=False)
        self.assertTrue(any("40 位" in x for x in bad), bad)

    def test_sha_shape_passes_offline(self) -> None:
        _r, bad = ap.pin_one({"uses": f"x@{SHA}", "repo": "x", "ref": SHA, "workflows": {}},
                             live=False)
        self.assertEqual(bad, [])

    def test_unreachable_sha_is_red_not_silent(self) -> None:
        real_api, real_status, real_raw = ap.api, ap.api_status, ap.raw
        try:
            ap.api_status = lambda p: (404, None)
            _r, bad = ap.pin_one({"uses": f"x@{SHA}", "repo": "x", "ref": SHA, "workflows": {}},
                                 live=True)
        finally:
            ap.api, ap.api_status, ap.raw = real_api, real_status, real_raw
        self.assertTrue(any("取不到提交" in x for x in bad), bad)

    def test_rate_limit_is_not_called_a_missing_commit(self) -> None:
        """403 要说"被限流"，404 才能说"上游没这个提交" —— 混起来会让人去查不存在的事。"""
        real = ap.api_status
        try:
            for code, want in ((403, "被限流"), (404, "取不到提交"), (None, "取不到（HTTP None）")):
                ap.api_status = lambda p, c=code: (c, None)
                _r, bad = ap.pin_one({"uses": f"x@{SHA}", "repo": "x", "ref": SHA,
                                      "workflows": {}}, live=True)
                self.assertTrue(any(want in x and "不能算核过" in x for x in bad), (code, bad))
        finally:
            ap.api_status = real

    def test_action_yml_unreadable_is_red(self) -> None:
        real_api, real_status, real_raw = ap.api, ap.api_status, ap.raw
        try:
            ap.api_status = lambda p: (200, {"sha": SHA, "commit": {"committer": {"date": "x"}}})
            ap.api = lambda p: {"sha": SHA, "commit": {"committer": {"date": "x"}}}
            ap.raw = lambda repo, sha, name="action.yml": None
            _r, bad = ap.pin_one({"uses": f"x@{SHA}", "repo": "x", "ref": SHA, "workflows": {}},
                                 live=True)
        finally:
            ap.api, ap.api_status, ap.raw = real_api, real_status, real_raw
        self.assertTrue(any("action.yml 取不到" in x for x in bad), bad)

    def test_runtime_change_is_red(self) -> None:
        real_api, real_status, real_raw = ap.api, ap.api_status, ap.raw
        yml = "runs:\n  using: node24\ninputs:\n  a:\n    default: 1\n"
        try:
            ap.api_status = lambda p: (200, {"sha": SHA, "commit": {"committer": {"date": "x"}},
                                "license": {"spdx_id": "MIT"}})
            ap.api = lambda p: {"sha": SHA, "commit": {"committer": {"date": "x"}},
                                "license": {"spdx_id": "MIT"}}
            ap.raw = lambda repo, sha, name="action.yml": yml
            _r, bad = ap.pin_one({"uses": f"x@{SHA}", "repo": "x", "ref": SHA, "workflows": {},
                                  "expect_using": "node20", "expect_inputs": ["a"]}, live=True)
        finally:
            ap.api, ap.api_status, ap.raw = real_api, real_status, real_raw
        self.assertTrue(any("runs.using" in x for x in bad), bad)

    def test_input_set_change_is_named(self) -> None:
        real_api, real_status, real_raw = ap.api, ap.api_status, ap.raw
        yml = "runs:\n  using: node20\ninputs:\n  a:\n    default: 1\n  z:\n    default: 2\n"
        try:
            ap.api_status = lambda p: (200, {"sha": SHA, "commit": {"committer": {"date": "x"}},
                                "license": {"spdx_id": "MIT"}})
            ap.api = lambda p: {"sha": SHA, "commit": {"committer": {"date": "x"}},
                                "license": {"spdx_id": "MIT"}}
            ap.raw = lambda repo, sha, name="action.yml": yml
            _r, bad = ap.pin_one({"uses": f"x@{SHA}", "repo": "x", "ref": SHA, "workflows": {},
                                  "expect_using": "node20", "expect_inputs": ["a", "b"]},
                                 live=True)
        finally:
            ap.api, ap.api_status, ap.raw = real_api, real_status, real_raw
        self.assertTrue(any("多 ['z'] / 少 ['b']" in x for x in bad), bad)

    def test_tag_drift_is_reported_but_not_followed(self) -> None:
        real_api, real_status = ap.api, ap.api_status
        try:
            ap.api_status = lambda p: (200, {"sha": "b" * 40})
            ap.api = lambda p: {"sha": "b" * 40}
            bad = ap.tag_drift([{"repo": "x", "ref": SHA, "floating_tag": "v4"}], live=True)
        finally:
            ap.api, ap.api_status = real_api, real_status
        self.assertTrue(any("已经指向" in x and "不许悄悄跟" in x for x in bad), bad)

    def test_tag_matching_the_pin_is_silent(self) -> None:
        real_api, real_status = ap.api, ap.api_status
        try:
            ap.api_status = lambda p: (200, {"sha": SHA})
            ap.api = lambda p: {"sha": SHA}
            bad = ap.tag_drift([{"repo": "x", "ref": SHA, "floating_tag": "v4"}], live=True)
        finally:
            ap.api, ap.api_status = real_api, real_status
        self.assertEqual(bad, [])

    def test_committed_table_agrees_with_the_real_workflows(self) -> None:
        """正向对照：按现在这三份 workflow 重算一遍，与已提交那张没有漂移。"""
        real_api, real_status, real_raw = ap.api, ap.api_status, ap.raw
        doc = artifact()
        try:
            ap.api_status = lambda p: (200, stub_api(p, doc))
            ap.api = lambda p: stub_api(p, doc)
            ap.raw = lambda repo, sha, name="action.yml": shape_yaml(repo, doc)
            fresh, problems = ap.build(live=True, known=doc)
        finally:
            ap.api, ap.api_status, ap.raw = real_api, real_status, real_raw
        self.assertEqual(problems, [], problems)
        self.assertEqual(ap.check(doc, fresh), [], ap.check(doc, fresh))


def rows_of(doc: dict) -> list[dict]:
    return doc["pins"] + doc["reserved_for_installer_lane"]


def by_repo(doc: dict, repo: str) -> dict:
    return next(r for r in rows_of(doc) if r["repo"] == repo)


def doc_sha(path: str, doc: dict) -> str:
    """把 API 请求"回答"成表里的样子：tag 解析回同一个 SHA（= 没有漂移）。"""
    for r in rows_of(doc):
        if path.startswith(f"repos/{r['repo']}"):
            return r["ref"]
    return "b" * 40


def stub_api(path: str, doc: dict) -> dict:
    """一个桩同时回答"提交/元数据/release 名"三类请求。"""
    if path.endswith("/releases/latest"):
        for r in rows_of(doc):
            if path.startswith(f"repos/{r['repo']}"):
                return {"tag_name": r.get("latest_release_tag")}
        return {}
    return {"sha": doc_sha(path, doc), "commit": {"committer": {"date": "x"}},
            "license": {"spdx_id": "MIT"}}


def shape_yaml(repo: str, doc: dict) -> str:
    r = by_repo(doc, repo)
    keys = "\n".join(f"  {k}:\n    default: 1" for k in r["inputs"])
    return f"runs:\n  using: {r['runs_using']}\ninputs:\n{keys}\n"


class TestArtifact(unittest.TestCase):
    def test_every_pin_has_a_runtime_and_inputs(self) -> None:
        for r in artifact()["pins"] + artifact()["reserved_for_installer_lane"]:
            self.assertTrue(r["runs_using"], r["repo"])
            self.assertTrue(r["inputs"], r["repo"])
            self.assertEqual(r["license"], "MIT", r["repo"])

    def test_pinned_shas_are_the_measured_ones(self) -> None:
        doc = artifact()
        got = {r["repo"]: r["ref"] for r in doc["pins"] + doc["reserved_for_installer_lane"]}
        self.assertEqual(got, REAL_SHAS)

    def test_tauri_action_is_used_by_the_installer_lane(self) -> None:
        doc = artifact()
        r = by_repo(doc, "tauri-apps/tauri-action")
        self.assertEqual(r["workflows"], {".github/workflows/installer.yml": 1})
        self.assertEqual(r["runs_using"], "node24")
        self.assertEqual(doc["reserved_for_installer_lane"], [])

    def test_release_name_is_not_the_tag_name(self) -> None:
        """文档里那句"release 叫 action-v1.0.0、tag 叫 v1.0.0"现在有机读版本。"""
        r = by_repo(artifact(), "tauri-apps/tauri-action")
        self.assertEqual(r["latest_release_tag"], "action-v1.0.0")
        self.assertEqual(r["floating_tag"], "v1.0.0")
        self.assertFalse(r["pin_is_a_release_tag"])

    def test_no_internal_expect_fields_land_in_the_table(self) -> None:
        blob = json.dumps(artifact(), ensure_ascii=False)
        for k in ("expect_using", "expect_inputs"):
            self.assertNotIn(k, blob)

    def test_policy_says_repinning_must_be_explicit(self) -> None:
        doc = artifact()
        self.assertIn("显式", doc["policy"]["rule"])
        self.assertIn("只能靠 GitHub Actions 出包", doc["policy"]["why"])

    def test_check_detects_a_moved_pin_and_a_changed_runtime(self) -> None:
        doc = artifact()
        import copy
        a = copy.deepcopy(doc)
        a["pins"][0]["ref"] = "f" * 40
        self.assertTrue(any(".ref" in x for x in ap.check(a, doc)))
        b = copy.deepcopy(doc)
        b["pins"][1]["runs_using"] = "node24"
        self.assertTrue(any("runs_using" in x for x in ap.check(b, doc)))
        c = copy.deepcopy(doc)
        c["pins"][2]["inputs"] = c["pins"][2]["inputs"] + ["新输入"]
        self.assertTrue(any("inputs" in x for x in ap.check(c, doc)))
        d = copy.deepcopy(doc)
        d["counts"]["uses_total"] = 43
        self.assertTrue(any("计数变了" in x for x in ap.check(d, doc)))


class TestCli(unittest.TestCase):
    def test_selftest_green(self) -> None:
        r = subprocess.run([sys.executable, CLI, "--selftest"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_offline_does_not_write(self) -> None:
        """离线那张表没有 runtime/输入 —— 让它落盘就是拿"看起来核对过"冒充核对结果。"""
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "actions.json")
            r = subprocess.run([sys.executable, CLI, "--offline", "--out", out],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("不落盘", r.stdout)
            self.assertFalse(os.path.exists(out))

    def test_offline_and_check_are_mutually_exclusive(self) -> None:
        r = subprocess.run([sys.executable, CLI, "--offline", "--check"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("不能同时用", r.stdout)

    def test_check_reports_no_drift(self) -> None:
        buf = io.StringIO()
        real_api, real_status, real_raw = ap.api, ap.api_status, ap.raw
        doc = artifact()
        try:
            ap.api_status = lambda p: (200, stub_api(p, doc))
            ap.api = lambda p: stub_api(p, doc)
            ap.raw = lambda repo, sha, name="action.yml": shape_yaml(repo, doc)
            with contextlib.redirect_stdout(buf):
                rc = ap.main(["--check", "--out", ART])
        finally:
            ap.api, ap.api_status, ap.raw = real_api, real_status, real_raw
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("问题 0 条", buf.getvalue())


class TestSourceMutants(unittest.TestCase):
    MUTANTS = [
        ("SHA 形状闸门关掉：浮动 tag 就不该再被拦",
         "    if not SHA_RX.match(row[\"ref\"]):",
         "    if False and not SHA_RX.match(row[\"ref\"]):"),
        ("取不到提交时不再报（钉了个不存在的 SHA 也算过）",
         "    if not meta or not meta.get(\"sha\"):",
         "    if False and (not meta or not meta.get(\"sha\")):"),
        ("读不到 action.yml 就当没事",
         "    if yml is None:",
         "    if False and yml is None:"),
        ("tag 漂了不再报（悄悄跟版）",
         '        if now and now != row["ref"]:',
         '        if False and now and now != row["ref"]:'),
        ("本地 action 被当成第三方（./ 的排除被拆）",
         '            if repo.startswith("./") or repo.startswith("../"):',
         '            if False and repo.startswith("./"):'),
        ("输入集合的比对被拆：上游加输入不会红",
         '    if row.get("expect_inputs") is not None and inputs != sorted(row["expect_inputs"]):',
         '    if False and inputs != sorted(row["expect_inputs"]):'),
        ("--offline 也有写权（拿没查过的表冒充查过）",
         "    if a.offline:\n        # 离线那张表没有",
         "    if False and a.offline:\n        # 离线那张表没有"),
    ]

    def src(self) -> str:
        with open(CLI, encoding="utf-8") as fh:
            return fh.read()

    def load(self, s: str):
        import types
        mod = types.ModuleType("action_pins_mutant")
        mod.__dict__["__file__"] = CLI
        mod.__dict__["__name__"] = "action_pins_mutant"
        exec(compile(s, CLI, "exec"), mod.__dict__)
        return mod

    def probes(self, mod) -> dict:
        doc = artifact()
        out = {}
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out["a 离线对照"] = mod.selftest() == 0
        except Exception:                     # noqa: BLE001
            out["a 离线对照"] = False
        try:
            _r, bad = mod.pin_one({"uses": "x@v4", "repo": "x", "ref": "v4", "workflows": {}},
                                  live=False)
            out["b 浮动 tag 拦得住"] = any("40 位" in x for x in bad)
        except Exception:                     # noqa: BLE001
            out["b 浮动 tag 拦得住"] = False
        try:
            real_api, real_status, real_raw = mod.api, mod.api_status, mod.raw
            mod.api_status = lambda p: (404, None)
            mod.raw = lambda *a, **k: None
            _r, bad = mod.pin_one({"uses": f"x@{SHA}", "repo": "x", "ref": SHA,
                                   "workflows": {}}, live=True)
            mod.api, mod.api_status, mod.raw = real_api, real_status, real_raw
            out["c 取不到提交要红"] = any("取不到提交" in x for x in bad)
        except Exception:                     # noqa: BLE001
            out["c 取不到提交要红"] = False
        try:
            real_api, real_status, real_raw = mod.api, mod.api_status, mod.raw
            mod.api_status = lambda p: (200, {"sha": SHA, "commit": {"committer": {"date": "x"}},
                                "license": {"spdx_id": "MIT"}})
            mod.api = lambda p: {"sha": SHA, "commit": {"committer": {"date": "x"}},
                                "license": {"spdx_id": "MIT"}}
            mod.raw = lambda *a, **k: None    # 提交在、action.yml 取不到
            _r, bad = mod.pin_one({"uses": f"x@{SHA}", "repo": "x", "ref": SHA,
                                   "workflows": {}}, live=True)
            mod.api, mod.api_status, mod.raw = real_api, real_status, real_raw
            out["g action.yml 取不到要红"] = any("action.yml 取不到" in x for x in bad)
        except Exception:                     # noqa: BLE001
            out["g action.yml 取不到要红"] = False
        try:
            real_api, real_status, real_raw = mod.api, mod.api_status, mod.raw
            mod.api_status = lambda p: (200, {"sha": SHA, "commit": {"committer": {"date": "x"}},
                                 "license": {"spdx_id": "MIT"}})
            mod.api = lambda p: {"sha": SHA, "commit": {"committer": {"date": "x"}},
                                 "license": {"spdx_id": "MIT"}}
            mod.raw = lambda *a, **k: "runs:\n  using: node20\ninputs:\n  a:\n    default: 1\n"
            _r, bad = mod.pin_one({"uses": f"x@{SHA}", "repo": "x", "ref": SHA,
                                   "workflows": {}, "expect_using": "node20",
                                   "expect_inputs": ["a", "b"]}, live=True)
            mod.api, mod.api_status, mod.raw = real_api, real_status, real_raw
            out["h 输入集合变了要红"] = any("输入集合变了" in x for x in bad)
        except Exception:                     # noqa: BLE001
            out["h 输入集合变了要红"] = False
        try:
            real_api, real_status = mod.api, mod.api_status
            mod.api_status = lambda p: (200, {"sha": "b" * 40})
            mod.api = lambda p: {"sha": "b" * 40}
            bad = mod.tag_drift([{"repo": "x", "ref": SHA, "floating_tag": "v4"}], live=True)
            mod.api, mod.api_status = real_api, real_status
            out["d tag 漂了会报"] = bool(bad)
        except Exception:                     # noqa: BLE001
            out["d tag 漂了会报"] = False
        try:
            out["e 本地 action 不算第三方"] = (mod.parse_uses({"c.yml": "uses: ./x@v1\n"}) == [])
            real_api, real_status, real_raw = mod.api, mod.api_status, mod.raw
            mod.api_status = lambda p: (200, stub_api(p, doc))
            mod.api = lambda p: stub_api(p, doc)
            mod.raw = lambda repo, sha, name="action.yml": shape_yaml(repo, doc)
            fresh, problems = mod.build(live=True, known=doc)
            mod.api, mod.api_status, mod.raw = real_api, real_status, real_raw
            out["f 现在的表没有漂移"] = (not problems) and mod.check(doc, fresh) == []
        except Exception:                     # noqa: BLE001
            out["f 现在的表没有漂移"] = False
        try:
            with tempfile.TemporaryDirectory() as d:
                out_path = os.path.join(d, "actions.json")
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    rc = mod.main(["--offline", "--out", out_path])
                out["i 离线不许写表"] = (rc == 0 and not os.path.exists(out_path))
        except Exception:                     # noqa: BLE001
            out["i 离线不许写表"] = False
        return out

    def test_baseline_probes_all_hold(self) -> None:
        self.assertTrue(all(self.probes(ap).values()), self.probes(ap))

    def test_every_mutant_is_caught(self) -> None:
        s = self.src()
        missed = []
        for name, old, new in self.MUTANTS:
            self.assertEqual(s.count(old), 1, f"锚点不唯一或已漂：{name}")
            got = self.probes(self.load(s.replace(old, new, 1)))
            if all(got.values()):
                missed.append(f"{name} → {got}")
        self.assertEqual(missed, [], "这些变异没人报：对应判据是装饰\n" + "\n".join(missed))

    def test_mutant_count(self) -> None:
        self.assertEqual(len(self.MUTANTS), 7)


class TestToolParses(unittest.TestCase):
    def test_ast_ok(self) -> None:
        with open(CLI, encoding="utf-8") as fh:
            ast.parse(fh.read())


if __name__ == "__main__":
    unittest.main()
