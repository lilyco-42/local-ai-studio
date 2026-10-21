"""下载计划：committed 的那张表必须能被工具逐键重算出来，且每个源都有出处。

只读 `pins/download-plan.json` 做形状断言是不够的 —— 生成器改了而文件没重生成、
或者文件被手改，两种都会让"计划是对的"这句话变成空话。所以这里的主判据是
`download_plan.build()` 与已提交文件**整份相等**，其余用例分别钉住一条来源规矩。
"""
from __future__ import annotations

import copy
import json
import math
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import download_plan as dp  # noqa: E402
import file_index  # noqa: E402


class TestDownloadPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, dp.OUT), encoding="utf-8") as fh:
            cls.plan = json.load(fh)
        cls.built, cls.problems = dp.build()
        cls.budget = dp.read_json(dp.BUDGET)
        cls.ocr_pins = dp.read_json(dp.OCR_PINS)

    def test_committed_plan_is_exactly_what_the_tool_recomputes(self) -> None:
        self.assertEqual(self.problems, [])
        self.assertEqual(self.plan, self.built)
        self.assertEqual(dp.check(self.plan), [])

    def test_every_downloadable_budget_row_is_planned_once(self) -> None:
        rows = {}
        for block in self.budget["per_tier"].values():
            for it in block["items"]:
                if it.get("resolve_via") != "not_a_file":
                    rows[it["sha256"]] = it
        self.assertEqual({x["sha256"] for x in self.plan["items"]}, set(rows))
        for x in self.plan["items"]:
            src = rows[x["sha256"]]
            self.assertEqual((x["file"], x["bytes"], x["kind"]),
                             (src["file"], src["bytes"], src["kind"]))
        self.assertFalse(any(x["file"] == "(合计)" for x in self.plan["items"]))
        self.assertEqual([x["file"] for x in self.plan["skipped"]], ["(合计)"])

    def test_each_hf_source_matches_the_candidate_list_minus_known_absent(self) -> None:
        for x in self.plan["items"]:
            if x["route"] != "hf_metadata":
                continue
            expected = []
            for c in file_index.candidates(x["repo"], x["file"]):
                if c["source"] in x["known_absent"]:
                    continue
                expected.append((c["source"], c["url"], c["measured_mib_s"]))
            got = [(s["name"], s["url"], s["measured_mib_s"]) for s in x["sources"]]
            self.assertEqual(got, expected, x["file"])

    def test_ocr_sources_are_the_pinned_release_plus_verified_mirrors(self) -> None:
        ocr = [x for x in self.plan["items"] if x["kind"] == "ocr"]
        self.assertEqual(len(ocr), 3)
        for x in ocr:
            pin = self.ocr_pins[x["file"]]
            self.assertEqual(x["sha256"], pin["sha256"])
            self.assertEqual(x["bytes"], pin["size"])
            self.assertEqual([s["name"] for s in x["sources"]], ["modelscope", "github.com"])
            mirror = pin["mirrors"][0]
            self.assertEqual((mirror["sha256"], mirror["bytes"]), (pin["sha256"], pin["size"]))
            self.assertIn("整包", mirror["verified"])
            self.assertEqual(x["sources"][0]["url"],
                             f"https://www.modelscope.cn/models/{mirror['repo']}/resolve/"
                             f"{mirror['revision']}/{x['file']}")
            self.assertEqual(x["sources"][-1]["url"], pin["primary"])
            self.assertFalse(x["single_source"])

    def test_every_default_install_survives_one_distributor_outage(self) -> None:
        """单源的首装会卡死在一家：ModelScope 4.2 / hf-mirror 0.25 / GitHub 0.014 MiB/s。"""
        self.assertEqual([x["file"] for x in self.plan["items"] if x["single_source"]], [])
        for x in self.plan["items"]:
            if x["default_install"]:
                self.assertGreaterEqual(len(x["sources"]),
                                        dp.POLICY["default_install_min_sources"], x["file"])

    def test_rate_order_holds_and_direct_huggingface_never_leads(self) -> None:
        for x in self.plan["items"]:
            rates = [s["measured_mib_s"] for s in x["sources"]]
            self.assertEqual(rates, sorted(rates, reverse=True), x["file"])
            self.assertNotEqual(x["sources"][0]["name"], "huggingface.co", x["file"])
            self.assertGreater(rates[0], 0, x["file"])

    def test_whisper_model_scope_is_removed_because_it_has_no_files_there(self) -> None:
        whisper = [x for x in self.plan["items"] if x["repo"] == "ggerganov/whisper.cpp"]
        self.assertEqual(len(whisper), 2)
        for x in whisper:
            self.assertIn("modelscope", x["known_absent"])
            self.assertNotIn("modelscope", [s["name"] for s in x["sources"]])
            self.assertEqual(x["sources"][0]["name"], "hf-mirror.com")
            self.assertFalse(x["single_source"])

    def test_the_same_sha_is_not_planned_twice_across_tiers(self) -> None:
        shas = [x["sha256"] for x in self.plan["items"]]
        self.assertEqual(len(shas), len(set(shas)))
        large = next(x for x in self.plan["items"] if x["file"] == "ggml-large-v1.bin")
        self.assertEqual(sorted(large["tiers"]), ["16GB", "8GB"])

    def test_check_reddens_on_each_source_rule(self) -> None:
        def bad(mutate) -> None:
            p = copy.deepcopy(self.plan)
            mutate(p)
            self.assertTrue(dp.check(p))

        def swap(p):
            p["items"][0]["sources"].reverse()

        def hf_first(p):
            p["items"][0]["sources"].insert(0, {
                "name": "huggingface.co",
                "url": "https://huggingface.co/o/r/resolve/main/" + p["items"][0]["file"],
                "measured_mib_s": 0.0})

        def aggregate(p):
            p["items"].append({"sha256": "0" * 64, "file": "(合计)", "bytes": 1,
                               "kind": "ocr", "sources": [
                                   {"name": "github.com", "url": "https://x/(合计)",
                                    "measured_mib_s": 0.014}]})

        def wrong_tail(p):
            p["items"][0]["sources"][0]["url"] = "https://x/other.bin"

        def drop(p):
            p["items"].pop()

        def ocr_no_release(p):
            item = next(x for x in p["items"] if x["kind"] == "ocr")
            item["sources"] = [s for s in item["sources"] if s["name"] != "github.com"]

        def default_single(p):
            item = next(x for x in p["items"] if x["default_install"])
            item["sources"] = item["sources"][:1]

        for mutate in (swap, hf_first, aggregate, wrong_tail, ocr_no_release, default_single):
            bad(mutate)
        dropped = copy.deepcopy(self.plan)
        drop(dropped)
        self.assertTrue(dp.diff(dropped, self.plan))

    def test_generator_refuses_input_drift_instead_of_guessing(self) -> None:
        arts = {"recommendations": dp.read_json(dp.RECO),
                "subtitle": dp.read_json(dp.SUBTITLE),
                "image": dp.read_json(dp.IMAGE),
                "ocr": copy.deepcopy(self.ocr_pins)}
        victim = next(iter(arts["ocr"]))
        arts["ocr"][victim]["sha256"] = "f" * 64
        _, problems = dp.build(arts=arts)
        self.assertTrue(any(victim in p for p in problems), problems)

        target = self.plan["items"][0]
        arts2 = {"recommendations": dp.read_json(dp.RECO),
                 "subtitle": dp.read_json(dp.SUBTITLE),
                 "image": dp.read_json(dp.IMAGE),
                 "ocr": self.ocr_pins}
        for keys in (("recommendations", "tiers_rows"),):
            for rows in arts2[keys[0]][keys[1]].values():
                rows[:] = [r for r in rows if r.get("sha256") != target["sha256"]]
        for rows in (arts2["subtitle"]["rows"], arts2["image"]["weights"]):
            rows[:] = [r for r in rows if r.get("sha256") != target["sha256"]]
        _, problems = dp.build(arts=arts2)
        self.assertTrue(any(target["file"] in p for p in problems), problems)

        arts3 = {"recommendations": dp.read_json(dp.RECO),
                 "subtitle": dp.read_json(dp.SUBTITLE),
                 "image": dp.read_json(dp.IMAGE),
                 "ocr": copy.deepcopy(self.ocr_pins)}
        victim3 = next(iter(arts3["ocr"]))
        arts3["ocr"][victim3]["mirrors"][0]["sha256"] = "f" * 64
        _, problems = dp.build(arts=arts3)
        self.assertTrue(any(victim3 in p and "镜像" in p for p in problems), problems)

    def test_cli_check_and_selftest_are_green(self) -> None:
        tool = os.path.join(ROOT, "tools", "download_plan.py")
        for flag in ("--check", "--selftest"):
            r = subprocess.run([sys.executable, tool, flag], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_plan_fields_exist_in_the_rust_reader(self) -> None:
        with open(os.path.join(ROOT, "shell", "core", "src", "download.rs"),
                  encoding="utf-8") as fh:
            rust = fh.read()

        def fields(struct: str) -> set[str]:
            m = re.search(r"pub struct " + struct + r" \{(.*?)\n\}", rust, re.S)
            self.assertIsNotNone(m, f"download.rs 里没有 struct {struct}")
            return set(re.findall(r"pub (\w+):", m.group(1)))

        self.assertTrue(fields("Plan") <= set(self.plan))
        self.assertTrue(fields("PlanItem") <= set(self.plan["items"][0]))
        self.assertTrue(fields("PlanSource") <= set(self.plan["items"][0]["sources"][0]))

    def test_plan_would_deserialize_and_be_addressable_by_the_rust_reader(self) -> None:
        """字段名在场不等于 serde 读得动：类型、u32/u64 边界、NaN、同名、扁平名都要对上。"""

        def is_int(v) -> bool:
            return isinstance(v, int) and not isinstance(v, bool)

        def is_num(v) -> bool:
            return (isinstance(v, (int, float)) and not isinstance(v, bool)
                    and math.isfinite(v))

        def probe(doc: dict) -> None:
            self.assertTrue(is_int(doc.get("schema")), "schema 不是整数")
            self.assertTrue(0 <= doc["schema"] < 2 ** 32, "schema 超出 u32")
            items = doc.get("items")
            self.assertIsInstance(items, list, "items 不是列表")
            seen: set[str] = set()
            for i, it in enumerate(items):
                where = f"items[{i}]"
                for key in ("file", "sha256", "kind"):
                    self.assertIsInstance(it.get(key), str, f"{where}.{key} 不是字符串")
                self.assertTrue(is_int(it.get("bytes")) and 0 <= it["bytes"] < 2 ** 64,
                                f"{where}.bytes 超出 u64")
                self.assertTrue(len(it["sha256"]) == 64
                                and all(c in "0123456789abcdef" for c in it["sha256"]),
                                f"{where}.sha256 不是 64 位小写十六进制")
                if "repo" in it:
                    self.assertTrue(it["repo"] is None or isinstance(it["repo"], str),
                                    f"{where}.repo 既不是字符串也不是 null")
                if "default_install" in it:
                    self.assertIsInstance(it["default_install"], bool,
                                          f"{where}.default_install 不是布尔")
                name = it["file"]
                self.assertTrue(name and name not in (".", "..")
                                and not any(c in name for c in "/\\\0"),
                                f"{where}.file 不是扁平文件名: {name!r}")
                self.assertNotIn(name, seen,
                                 f"file 重名，Rust item() 会当成歧义拒绝: {name}")
                seen.add(name)
                srcs = it.get("sources")
                self.assertIsInstance(srcs, list, f"{where}.sources 不是列表")
                self.assertTrue(srcs, f"{where} 没有任何源，这一步下不了")
                for j, s in enumerate(srcs):
                    sw = f"{where}.sources[{j}]"
                    self.assertIsInstance(s.get("name"), str, f"{sw}.name 不是字符串")
                    self.assertIsInstance(s.get("url"), str, f"{sw}.url 不是字符串")
                    self.assertTrue(is_num(s.get("measured_mib_s")),
                                    f"{sw}.measured_mib_s 不是有限数字")

        probe(self.plan)
        mutations = {
            "bytes 变成字符串": lambda d: d["items"][0].__setitem__("bytes", "5"),
            "schema 变成 true": lambda d: d.__setitem__("schema", True),
            "速率是 NaN": lambda d: d["items"][0]["sources"][0].__setitem__(
                "measured_mib_s", float("nan")),
            "文件名带斜杠": lambda d: d["items"][0].__setitem__("file", "a/b"),
            "两颗同名": lambda d: d["items"].append(copy.deepcopy(d["items"][0])),
            "没有源": lambda d: d["items"][0].__setitem__("sources", []),
        }
        for label, mutate in mutations.items():
            bad = copy.deepcopy(self.plan)
            mutate(bad)
            with self.subTest(label=label), self.assertRaises(AssertionError):
                probe(bad)

    def test_probe_covers_every_planned_url_verbatim(self) -> None:
        jobs = dp.probe_jobs(self.plan)
        self.assertEqual([(j["file"], j["source"], j["url"]) for j in jobs],
                         [(x["file"], s["name"], s["url"])
                          for x in self.plan["items"] for s in x["sources"]])
        self.assertEqual(len(jobs), 37)
        # known_absent 在计划里没有 URL；探针不许为它按模板造一条
        for x in self.plan["items"]:
            for provider in x["known_absent"]:
                self.assertFalse([j for j in jobs
                                  if j["file"] == x["file"] and provider in j["url"]],
                                 (x["file"], provider))

    def test_probe_grades_a_real_byte_and_a_missing_source(self) -> None:
        def fake(status, got, error="", total=None):
            return lambda url, timeout: {"status": status, "got": got, "final": url,
                                         "error": error, "total": total}

        jobs = dp.probe_jobs(self.plan)[:3]
        rows = dp.probe_all(jobs, fetch=fake(206, 1))
        self.assertEqual([r["result"] for r in rows], ["ok"] * 3)
        self.assertTrue(dp.probe_ok(dp.summarize(rows)))
        self.assertFalse(dp.probe_ok(dp.summarize(dp.probe_all(jobs, fetch=fake(404, 0)))))
        # 200 却没有正文字节（被代理/劫持的典型形状）不算通
        self.assertEqual([r["result"] for r in dp.probe_all(jobs[:1], fetch=fake(200, 0))],
                         ["bad"])
        # 连接错误没有状态码，也要落成一行结论
        err = dp.probe_all(jobs[:1], fetch=fake(0, 0, "URLError: timed out"))
        self.assertEqual(err[0]["result"], "bad")
        self.assertIn("timed out", err[0]["error"])

    def test_probe_compares_the_advertised_total_with_the_pinned_bytes(self) -> None:
        expected = dp.probe_jobs(self.plan)[0]["bytes"]

        def fake(total):
            return lambda url, timeout: {"status": 206, "got": 1, "total": total,
                                         "final": url, "error": ""}

        job = dp.probe_jobs(self.plan)[:1]
        self.assertEqual(dp.probe_all(job, fetch=fake(expected))[0]["result"], "ok")
        short = dp.probe_all(job, fetch=fake(expected - 1))
        self.assertEqual(short[0]["result"], "mismatch")
        self.assertFalse(dp.probe_ok(dp.summarize(short)))
        self.assertEqual(dp.summarize(short)["mismatch"], 1)
        # 206 的 Content-Length 是这 1 个字节，不是总长；不能拿它冒充
        self.assertEqual(dp.parse_total({"Content-Range": "bytes 0-0/2497281312"}, 206),
                         2497281312)
        self.assertIsNone(dp.parse_total({"Content-Range": "bytes 0-0/*"}, 206))
        self.assertEqual(dp.parse_total({"Content-Length": "12345"}, 200), 12345)
        self.assertIsNone(dp.parse_total({"Content-Length": "1"}, 206))

    def test_plan_policy_matches_the_rust_curl_arguments(self) -> None:
        """policy 是给下载器看的，Rust 里另有一份数；两边漂了就在这里红。"""
        with open(os.path.join(ROOT, "shell", "core", "src", "download.rs"),
                  encoding="utf-8") as fh:
            rust = fh.read()

        def after(flag: str) -> str:
            m = re.search(r'"' + re.escape(flag) + r'",\s*"([^"]+)"', rust)
            self.assertIsNotNone(m, f"download.rs 里找不到 {flag}")
            return m.group(1)

        low = self.plan["policy"]["low_speed"]
        self.assertEqual(int(after("--speed-limit")), low["bytes_per_s"])
        self.assertEqual(int(after("--speed-time")), low["seconds"])
        self.assertEqual(after("--proto-redir"), "=https")
        default_impl = re.search(r"impl Default for Policy \{(.*?)\n\}", rust, re.S)
        self.assertIsNotNone(default_impl, "download.rs 里没有 Policy::default")
        attempts = re.search(r"attempts_per_source:\s*(\d+)", default_impl.group(1))
        self.assertIsNotNone(attempts)
        self.assertEqual(int(attempts.group(1)), self.plan["policy"]["attempts_per_source"])

    def test_probe_summary_names_sources_hosts_and_never_green_on_empty(self) -> None:
        job = dp.probe_jobs(self.plan)[0]
        rows = [dict(job, status=206, got=1, total=job["bytes"],
                     final="https://cdn.example/a.bin", error="", result="ok")]
        s = dp.summarize(rows)
        self.assertEqual((s["total"], s["ok"]), (1, 1))
        self.assertEqual(s["by_source"][job["source"]]["ok"], 1)
        self.assertEqual(s["by_final_host"]["cdn.example"]["ok"], 1)
        self.assertFalse(dp.probe_ok(dp.summarize([])))


if __name__ == "__main__":
    unittest.main()
