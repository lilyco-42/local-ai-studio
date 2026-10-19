"""catalog_snapshot 的行为测试。

夹具 `catalog_upstream.json` 是真实上游文件（llmfit 的 hf_models.json @ fbde9aaf…）的前 60 条切片，
不是照 README 编的；它的哈希记在同目录的 `catalog_upstream.meta.json` 里 ——
那是**切片自己的**哈希，不能拿来证明上游整文件没变（那件事只有 UPSTREAM["sha256"] 管）。
"""
import copy
import hashlib
import importlib.util
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "catalog_snapshot", os.path.join(HERE, "..", "tools", "catalog_snapshot.py"))
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)

FIX = os.path.join(HERE, "fixtures")


def fixture():
    with open(os.path.join(FIX, "catalog_upstream.json"), "rb") as fh:
        raw = fh.read()
    with open(os.path.join(FIX, "catalog_upstream.meta.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    return raw, meta["sha256"]


class TestSnapshot(unittest.TestCase):
    def test_real_slice_replays(self) -> None:
        """正向对照：真数据切片必须留下 59 条、丢掉那 1 条非 gguf，且零问题。"""
        raw, sha = fixture()
        snap, stats, bad = cs.build(raw, sha)
        self.assertEqual(bad, [])
        self.assertEqual((stats["上游条目"], stats["留下"], stats["丢弃-非gguf"]), (60, 59, 1))
        self.assertEqual(snap["stats"], stats)

    def test_drifted_upstream_is_refused(self) -> None:
        """哈希比对是有承重的：换一个 expect_sha 必须红，而不是"照样生成"。"""
        raw, _ = fixture()
        _, _, bad = cs.build(raw, hashlib.sha256(b"x").hexdigest())
        self.assertEqual(len(bad), 1)
        self.assertIn("哈希变了", bad[0])

    def test_license_never_disappears(self) -> None:
        """上游 license 为空的条目要归一成 unknown。实测切片里 60 条有 22 条是空的，
        字段一旦消失，UI 就没东西可显示，商用合规也就无从谈起。"""
        raw, sha = fixture()
        snap, stats, _ = cs.build(raw, sha)
        self.assertEqual(stats["许可 unknown 的留下条目"], 22)
        self.assertTrue(all("license" in m for m in snap["models"]), "许可字段不许从记录里消失")

    def test_unnamed_gguf_is_reported_not_dropped_silently(self) -> None:
        """缺 name/provider 的 gguf 条目必须点名。静默丢一条，目录就少一条而没人知道。"""
        raw, _ = fixture()
        entries = json.loads(raw.decode("utf-8"))
        entries[0]["format"] = "gguf"
        entries[0].pop("name", None)
        entries[0]["provider"] = "someone"
        blob = json.dumps(entries, ensure_ascii=False).encode("utf-8")
        _, _, bad = cs.build(blob, hashlib.sha256(blob).hexdigest())
        self.assertTrue(any("却没有 name/provider" in b for b in bad), bad)
        self.assertTrue(any("1 条" in b for b in bad), bad)

    def test_everything_filtered_out_is_a_rule_bug(self) -> None:
        """全被规则否掉时不能报"目录为空但成功"。"""
        entries = [{"name": "a/b", "provider": "a", "format": "safetensors"}] * 3
        blob = json.dumps(entries).encode("utf-8")
        _, _, bad = cs.build(blob, hashlib.sha256(blob).hexdigest())
        self.assertTrue(any("一条都没留下" in b for b in bad), bad)

    def test_defaults_layer_excludes_lane_samples(self) -> None:
        """catalog/defaults.json 是给用户看的默认档；车道样本 jfk.wav 不许混进来，
        但 OCR 那三件（来自 pins/ocr.json）必须在 —— 四项能力的默认档要在一个 artifact 里齐。"""
        layers = cs.load_pin_layers()
        picks = cs.picks_of(layers)
        self.assertNotIn("samples/jfk.wav", picks)
        self.assertEqual(len(picks), len(layers) - 1)
        self.assertEqual(sum(1 for k in picks if k.endswith((".onnx", ".txt"))), 3, "OCR 默认档必须也在")
        path = os.path.join(HERE, "..", "catalog", "defaults.json")
        with open(path, encoding="utf-8") as fh:
            made = json.load(fh)
        self.assertEqual(sorted(made["entries"]), sorted(picks))
        for k, v in made["entries"].items():
            self.assertEqual(v["sha256"], layers[k]["sha256"], k)
            self.assertEqual(v["size"], layers[k]["size"], k)

    def test_kept_record_has_no_upstream_schema_leak(self) -> None:
        """只带 KEEP 里的字段：上游那 38 个字段（head_dim/moe_intermediate_size…）
        是它自己拟合用的，搬进来就等于把别人的 schema 变更变成我们的破坏性变更。"""
        raw, sha = fixture()
        snap, _, _ = cs.build(raw, sha)
        allowed = set(cs.KEEP) | {"license", "has_sources"}
        extra = set()
        for m in snap["models"]:
            extra |= set(m) - allowed
        self.assertEqual(extra, set())

    def test_slim_rejects_non_gguf(self) -> None:
        self.assertIsNone(cs.slim({"name": "a/b", "provider": "a", "format": "mlx"}))
        self.assertEqual(cs.slim({"name": "a/b", "provider": "a", "format": "gguf",
                                  "license": None})["license"], "unknown")


if __name__ == "__main__":
    unittest.main()
