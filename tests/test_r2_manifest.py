"""r2_manifest 的行为测试：这张清单是"要不要花钱灌镜像"的依据，
所以它不许悄悄漏条目、不许出现没有地址的行，也不许在手打数字上通过。"""
import importlib.util
import json
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "r2_manifest", os.path.join(HERE, "..", "tools", "r2_manifest.py"))
rm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rm)

PINS = os.path.join(HERE, "..", "pins")


def pin_total():
    n = 0
    for f in ("toolbelt.json", "models.json", "ocr.json"):
        with open(os.path.join(PINS, f), encoding="utf-8") as fh:
            n += len(json.load(fh))
    return n


class TestManifest(unittest.TestCase):
    def test_rows_cover_every_pin_minus_lane_samples(self) -> None:
        """条数要能从 pin 文件本身推出来 —— 写死一个数就等着哪天漂。"""
        rows, problems = rm.gather()
        self.assertEqual(problems, [])
        expected = pin_total() - 1  # 只该排除 samples/jfk.wav 这一条车道样本
        self.assertEqual(len(rows), expected, f"清单 {len(rows)} 条 vs pin {pin_total()} 条")
        self.assertFalse([r for r in rows if r["name"].startswith("jfk")])

    def test_every_row_can_actually_be_fetched(self) -> None:
        """没有 https 地址 / 没有 sha / 没有体积的行，灌镜像时就是死条目。"""
        rows, _ = rm.gather()
        for r in rows:
            self.assertTrue(r["source"].startswith("https://"), r["name"])
            self.assertEqual(len(r["sha256"]), 64, r["name"])
            self.assertGreater(r["size"], 0, r["name"])

    def test_missing_source_address_is_a_problem_not_a_skip(self) -> None:
        """pin 只有说明文字没有 URL 时必须报问题（真撞过一次：元数据路线的 pin
        早期版本 `primary` 写的是"ModelScope repo/files 元数据"，清单当场少两条大权重）。"""
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "pins"))
            with open(os.path.join(d, "pins", "models.json"), "w", encoding="utf-8") as fh:
                json.dump({"a.gguf": {"sha256": "a" * 64, "size": 10,
                                      "primary": "ModelScope repo/files 元数据"}}, fh)
            saved = rm.ROOT
            rm.ROOT = d
            try:
                rows, problems = rm.gather()
            finally:
                rm.ROOT = saved
        self.assertEqual(rows, [])
        self.assertTrue(any("没有可下载的源地址" in p for p in problems), problems)

    def test_every_row_has_a_measured_source_rate(self) -> None:
        """分组换算不许出现"未知源"：那意味着清单里多了一条没量过吞吐的下载路，
        时间估算就会悄悄把它当成 0 或当成别的源。"""
        rows, _ = rm.gather()
        unknown = [r["name"] for r in rows
                   if max((rm.rate_for(u)[1] for u in r["candidates"]), default=0.0) == 0.0]
        self.assertEqual(unknown, [])

    def test_weights_prefer_the_faster_candidate_not_the_primary(self) -> None:
        """权重那条 primary 是 hf-mirror（0.25），但 ModelScope 实测 4.2 ——
        清单必须按现成候选里最快的一档给结论，否则会把"不必镜像"算成"值得镜像"。"""
        rows, _ = rm.gather()
        w = [r for r in rows if r["kind"] == "models" and r["name"].endswith(".gguf")]
        self.assertTrue(w)
        for r in w:
            best = max((rm.rate_for(u) for u in r["candidates"]), key=lambda x: x[1])
            self.assertEqual(best[0], "modelscope", r["name"])
            self.assertGreaterEqual(best[1], rm.R2_MIB_S, r["name"])

    def test_empty_pins_dir_does_not_produce_an_empty_green_manifest(self) -> None:
        """三个 pin 文件都不在时要红，不能报"0 条，通过"。"""
        with tempfile.TemporaryDirectory() as d:
            saved = rm.ROOT
            rm.ROOT = d
            try:
                rows, problems = rm.gather()
            finally:
                rm.ROOT = saved
        self.assertEqual(len(problems), 3)


if __name__ == "__main__":
    unittest.main()
