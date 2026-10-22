"""入门模型 pin：0.6B 不存在这件事必须留在表里，而不是悄悄改成 0.8B。

这里不联网。在线那一半（HF API 对表、真下 527 MB 复算 sha256）由
`engine-live.yml` 的 `starter-model` 车道跑；本文件只证明离线判据会红，
以及提交在仓库里的那几个字面量与上游那次测量一致。
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import starter_pin as sp  # noqa: E402

PIN = os.path.join(ROOT, "pins", "starter.json")
CI = os.path.join(ROOT, ".github", "workflows", "ci.yml")

# 2026-10-22 从 HF API 的 models/<id>?blobs=true 读到的值；测试里再写一遍，
# 这样 pin 被误改时不是"工具自己同意自己"。
WANT_ID = "lainlives/Qwen3.5-0.8B-uncensored"
WANT_REV = "6bfd8d84676b943c701ce1c16af1756cf27a2e46"
WANT_FILE = "Qwen3.5-0.8B-finetuned-lora-uncensored-Q4_K_M.gguf"
WANT_BYTES = 527495776
WANT_SHA = "a7d500b70b5c0d0e790e4d683bf5762e6a46d092a051b493073c006234e5d320"


class StarterPinTest(unittest.TestCase):
    def setUp(self) -> None:
        with open(PIN, encoding="utf-8") as fh:
            self.doc = json.load(fh)

    def test_the_committed_pin_is_green(self) -> None:
        self.assertEqual(sp.problems(self.doc), [])

    def test_identity_is_the_measured_one(self) -> None:
        model = self.doc["model"]
        self.assertEqual(model["id"], WANT_ID)
        self.assertEqual(model["revision"], WANT_REV)
        self.assertEqual(model["file"], WANT_FILE)
        self.assertEqual(model["bytes"], WANT_BYTES)
        self.assertEqual(model["sha256"], WANT_SHA)
        self.assertEqual(model["license"], "apache-2.0")

    def test_the_requested_0_6b_is_not_silently_renamed(self) -> None:
        requested = self.doc["requested"]
        self.assertEqual(requested["size"], "0.6B")
        self.assertEqual(requested["resolved_size"], "0.8B")
        self.assertIn("0.6B", requested["resolution"])
        self.assertIn("0.8B", requested["resolution"])

    def test_every_url_is_immutable_and_points_at_the_file(self) -> None:
        model = self.doc["model"]
        self.assertGreaterEqual(len(model["urls"]), 2)
        for url in model["urls"]:
            self.assertIn(model["revision"], url)
            self.assertTrue(url.endswith("/" + model["file"]))
            self.assertTrue(url.startswith("https://"))
            for floating in ("/main/", "/master/", "/latest/"):
                self.assertNotIn(floating, url)

    def test_engine_release_is_the_one_payload_plan_ships(self) -> None:
        with open(os.path.join(ROOT, "pins", "payload-plan.json"), encoding="utf-8") as fh:
            plan = json.load(fh)
        releases = {
            row["release"]
            for rows in plan["os"].values()
            for row in rows
            if row.get("engine") == "llama"
        }
        self.assertIn(self.doc["engine"]["release"], releases)
        self.assertEqual(self.doc["engine"]["release"], "b11804")

    def test_disk_budget_can_hold_the_bytes(self) -> None:
        fit = self.doc["fit"]
        self.assertGreaterEqual(fit["disk_mib"] * 1024 * 1024, self.doc["model"]["bytes"])
        self.assertGreaterEqual(fit["ram_mib_min"], 512)

    def test_a_wrong_hash_is_red(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["model"]["sha256"] = "0" * 64
        self.assertTrue(sp.problems(doc))

    def test_a_floating_revision_is_red(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["model"]["revision"] = "main"
        self.assertTrue(sp.problems(doc))

    def test_plain_http_is_red(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["model"]["urls"][0] = doc["model"]["urls"][0].replace("https://", "http://", 1)
        self.assertTrue(sp.problems(doc))

    def test_an_url_without_the_revision_is_red(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["model"]["urls"][0] = doc["model"]["urls"][0].replace(doc["model"]["revision"] + "/", "")
        self.assertTrue(sp.problems(doc))

    def test_a_path_traversal_filename_is_red(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["model"]["file"] = "../escape.gguf"
        self.assertTrue(sp.problems(doc))

    def test_zero_bytes_is_red(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["model"]["bytes"] = 0
        self.assertTrue(sp.problems(doc))

    def test_a_release_not_in_payload_plan_is_red(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["engine"]["release"] = "b0"
        self.assertTrue(sp.problems(doc))

    def test_a_non_redistributable_license_is_red(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["model"]["license"] = "gpl-3.0"
        self.assertTrue(sp.problems(doc))

    def test_dropping_the_size_resolution_is_red(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["requested"]["resolution"] = "0.6B 存在"
        self.assertTrue(sp.problems(doc))

    def test_a_too_small_disk_budget_is_red(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["fit"]["disk_mib"] = 1
        self.assertTrue(sp.problems(doc))

    def test_selftest_catches_every_mutation(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            self.assertEqual(sp.selftest(), 0)
        self.assertIn("self-test ok", buf.getvalue())

    def test_ci_runs_the_offline_gate(self) -> None:
        with open(CI, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("tools/starter_pin.py --check", text)
        self.assertIn("tools/starter_pin.py --selftest", text)


if __name__ == "__main__":
    unittest.main()
