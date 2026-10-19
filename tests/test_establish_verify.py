"""establish_pins 的 --verify（跨提供方真字节复核）的行为测试。

这条检查在真跑时要下载几百 MB，所以这里把 `sha_stream` 换掉来证明判定本身有承重：
对不上必须红、体积对不上也必须红、下载失败不能当成"没问题"、
而 TARGETS 里认不出仓库的键必须拒绝下载而不是猜一个 URL。
真字节那次跑通是这些用例的正向对照，两者缺一不可。
"""
import copy
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "establish_pins", os.path.join(HERE, "..", "tools", "establish_pins.py"))
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)

KEY = "qwen2.5-0.5b-instruct-q2_k.gguf"


def real_pin():
    with open(os.path.join(HERE, "..", "pins", "models.json"), encoding="utf-8") as fh:
        return json.load(fh)[KEY]


class TestVerify(unittest.TestCase):
    def setUp(self):
        self._orig = tb.sha_stream
        rec = copy.deepcopy(real_pin())
        # 真 pin 现在已经带着复核记录（这是好事），但那三条"必须红"的用例要验的是
        # verify() 会不会**新写**一条，所以先把既有的摘掉，别拿历史证据当本轮结论。
        rec.pop("bytes_verified", None)
        self.pin = {KEY: rec}

    def tearDown(self):
        tb.sha_stream = self._orig

    def _run(self, fake_stream):
        tb.sha_stream = fake_stream
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pins.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(self.pin, fh)
            rc = tb.verify(p, "")
            with open(p, encoding="utf-8") as fh:
                after = json.load(fh)
        return rc, after

    def test_bytes_from_the_other_provider_match(self) -> None:
        """正向对照：字节与元数据哈希一致 → rc=0 且把复核记录写回，级别要写明跨提供方。"""
        v = self.pin[KEY]
        rc, after = self._run(lambda url: (v["sha256"], v["size"], ""))
        self.assertEqual(rc, 0)
        got = after[KEY].get("bytes_verified")
        self.assertIsNotNone(got)
        self.assertEqual(got["bytes"], v["size"])
        self.assertIn("跨提供方", got["level"])
        self.assertIn("hf-mirror", got["url"])

    def test_same_provider_bytes_are_labelled_as_such(self) -> None:
        """上游 digest 那类（pin 里直接带 GitHub URL）只能同提供方复核，
        记录必须写"不证明来源独立"，不能被读成第 5 级。"""
        pin = {"pp-ocrv6_tiny_det.onnx": {
            "sha256": "c" * 64, "size": 1780590,
            "primary": "https://github.com/GreatV/oar-ocr/releases/download/v0.7.0/pp-ocrv6_tiny_det.onnx"}}
        tb.sha_stream = lambda url: ("c" * 64, 1780590, "")
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pins.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(pin, fh)
            rc = tb.verify(p, "")
            with open(p, encoding="utf-8") as fh:
                after = json.load(fh)
        self.assertEqual(rc, 0)
        self.assertIn("同提供方", after["pp-ocrv6_tiny_det.onnx"]["bytes_verified"]["level"])
        self.assertIn("不证明来源独立", after["pp-ocrv6_tiny_det.onnx"]["bytes_verified"]["level"])

    def test_hash_disagreement_is_red(self) -> None:
        """另一家给的字节对不上元数据哈希 → 必须红，且不写"已复核"。"""
        v = self.pin[KEY]
        rc, after = self._run(lambda url: ("f" * 64, v["size"], ""))
        self.assertEqual(rc, 1)
        self.assertNotIn("bytes_verified", after[KEY])

    def test_size_disagreement_is_red_even_with_a_hash_match(self) -> None:
        """哈希碰巧对但体积不符（说明下载被截断）也不能算复核过。"""
        v = self.pin[KEY]
        rc, after = self._run(lambda url: (v["sha256"], v["size"] - 1, ""))
        self.assertEqual(rc, 1)
        self.assertNotIn("bytes_verified", after[KEY])

    def test_transport_failure_is_not_reported_as_clean(self) -> None:
        """下载失败要红并留在"整包未校验"级，不能静默 0。"""
        rc, after = self._run(lambda url: (None, 0, "curl rc=35"))
        self.assertEqual(rc, 1)
        self.assertNotIn("bytes_verified", after[KEY])

    def test_unknown_repo_refuses_to_download(self) -> None:
        """仓库名来自 TARGETS；认不出就拒绝，绝不拼一个猜出来的 URL。"""
        tb.sha_stream = lambda url: self.fail("不该发起下载")
        pin = {"not-in-targets.bin": {"sha256": "a" * 64, "size": 1,
                                      "primary": "ModelScope repo/files 元数据"}}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pins.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(pin, fh)
            self.assertEqual(tb.verify(p, ""), 1)

    def test_only_downloads_the_selected_pin(self) -> None:
        """--only 命中时不能顺带把 1.5 GB 那条也拉下来。"""
        seen = []
        v = self.pin[KEY]

        def fake(url):
            seen.append(url)
            return v["sha256"], v["size"], ""
        tb.sha_stream = fake
        pin = dict(self.pin)
        pin["stable-diffusion-v1-5-pruned-emaonly-Q4_0.gguf"] = {
            "sha256": "b" * 64, "size": 1566768416, "primary": "ModelScope repo/files 元数据"}
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pins.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(pin, fh)
            rc = tb.verify(p, KEY)
        self.assertEqual(rc, 0)
        self.assertEqual(len(seen), 1)
        self.assertIn(KEY, seen[0])

    def test_url_uses_the_hf_mirror_side(self) -> None:
        """复核的一侧必须是 hf-mirror（与 ModelScope 不同的提供方），URL 形状要钉住。"""
        seen = []
        v = self.pin[KEY]

        def fake(url):
            seen.append(url)
            return v["sha256"], v["size"], ""
        tb.sha_stream = fake
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "pins.json")
            with open(p, "w", encoding="utf-8") as fh:
                json.dump(self.pin, fh)
            tb.verify(p, "")
        self.assertTrue(seen[0].startswith("https://hf-mirror.com/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/main/"),
                        seen[0])


class TestCrossMeta(unittest.TestCase):
    """--cross-hf：HF 的 `tree.lfs.oid` 是第二家分发方给的哈希。
    这里 stub 掉 `get`（真跑要联网），证明判定本身会红，尤其证明"对不上时不许写盘"。"""

    REC = {KEY: {"sha256": "a" * 64, "size": 415182688,
                 "primary": "ModelScope repo/files 元数据",
                 "url": f"https://hf-mirror.com/Qwen/X-GGUF/resolve/main/{KEY}"}}

    def stub(self, oids, rc=0):
        def fake_get(url, rng=None):
            host = url.split("/")[2]
            oid = oids.get(host)
            body = json.dumps([{"path": KEY, "lfs": {"oid": oid, "size": 415182688}}]).encode()
            return rc, (body if rc == 0 else b""), "" if rc == 0 else "curl 22"
        return fake_get

    def run_on(self, tmp, oids, rc=0):
        real = tb.get
        tb.get = self.stub(oids, rc)
        path = os.path.join(tmp, "pins.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(copy.deepcopy(self.REC), fh)
        try:
            return tb.cross_meta(path), json.load(open(path, encoding="utf-8"))
        finally:
            tb.get = real

    def test_both_hosts_agree(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rc, pins = self.run_on(d, {"huggingface.co": "a" * 64, "hf-mirror.com": "a" * 64})
            self.assertEqual(rc, 0)
            self.assertEqual(pins[KEY]["meta_hashes"]["level"],
                             "两家分发方元数据各给哈希且相等（仍不是真字节）")
            self.assertEqual(sorted(pins[KEY]["meta_hashes"]["hosts"]),
                             ["hf-mirror.com", "huggingface.co"])

    def test_disagreeing_host_is_red_and_nothing_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rc, pins = self.run_on(d, {"huggingface.co": "a" * 64, "hf-mirror.com": "b" * 64})
            self.assertEqual(rc, 1)
            self.assertNotIn("meta_hashes", pins[KEY], "对不上却写盘 = 把一次失败的对照记成成功")

    def test_unreachable_host_is_red_not_skipped(self) -> None:
        """取不到不能当成"没这条"，否则网络一抖车道就绿。"""
        with tempfile.TemporaryDirectory() as d:
            rc, pins = self.run_on(d, {"huggingface.co": "a" * 64}, rc=22)
            self.assertEqual(rc, 1)
            self.assertNotIn("meta_hashes", pins[KEY])

    def test_size_mismatch_is_red(self) -> None:
        real = tb.get
        def fake_get(url, rng=None):
            return 0, json.dumps([{"path": KEY, "lfs": {"oid": "a" * 64, "size": 1}}]).encode(), ""
        tb.get = fake_get
        try:
            with tempfile.TemporaryDirectory() as d:
                path = os.path.join(d, "pins.json")
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(copy.deepcopy(self.REC), fh)
                self.assertEqual(tb.cross_meta(path), 1)
        finally:
            tb.get = real

    def test_repo_extraction_ignores_github_urls(self) -> None:
        self.assertEqual(tb.hf_repos_for({"url": f"https://hf-mirror.com/A/B/resolve/main/{KEY}"}),
                         {"A/B"})
        self.assertEqual(tb.hf_repos_for({"alt_sources": [f"https://huggingface.co/C/D/resolve/main/{KEY}",
                                                          "https://github.com/x/y/raw/z"]}), {"C/D"})
        self.assertEqual(tb.hf_repos_for({"primary": "https://www.modelscope.cn/models/E/F"}), set())

    def test_committed_pins_all_have_the_second_hash(self) -> None:
        """仓库里那三条 HF 权重必须都带 meta_hashes —— 这条是"别忘了跑 --cross-hf"的守卫。"""
        with open(os.path.join(HERE, "..", "pins", "models.json"), encoding="utf-8") as fh:
            pins = json.load(fh)
        hf = [k for k, v in pins.items() if tb.hf_repos_for(v)]
        self.assertEqual(len(hf), 3, hf)
        for k in hf:
            self.assertEqual(pins[k]["meta_hashes"]["oid"], pins[k]["sha256"], k)
            self.assertIn("仍不是真字节", pins[k]["meta_hashes"]["level"],
                          "级别名不许把它冒充成真字节")



if __name__ == "__main__":
    unittest.main()
