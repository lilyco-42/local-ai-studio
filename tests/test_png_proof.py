"""tools/png_proof.py 的判据测试。

产物侧有两条真路：CLI 直接写文件，常驻服务把图塞在 result.images[0].b64_json。
两边都用同一个判据，所以这里既测解析器（用 **Pillow 写的真 PNG**，不是本仓库任何
代码拼的字节 —— 块序、CRC、tEXt 写法都由别人负责），也测判据本身
（在解析出来的字段上做最小改动，红必须红在对应那一句）。

最后一类是"与 CLI 车道那段内联判据的判定一致"：同一段字节，两处判据必须同生同死。
判据抽成工具之后最怕的就是两份悄悄分家。

PNG 语料的来历：`png_*.png` 由本机 Pillow 12.3.0 写出（不是本仓库任何代码拼的字节），
像素是随机噪声 —— 为的是让文件真的超过 1 KiB，好让"体积下限"这条判据在真产物形状上被测；
`tEXt` 里那条 `parameters` 文本的**写法**照 sd.cpp `examples/common/common.cpp:3239/3269`
（`Steps: N, ` 与 `Size: WxH, `）摆，但它是 Pillow 写的，不等于 sd.cpp 产物。
"""
import base64
import json
import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import lane_replay  # noqa: E402
import png_proof  # noqa: E402

FX = os.path.join(HERE, "fixtures")
GOOD_PNG = os.path.join(FX, "png_with_parameters.png")
NO_META_PNG = os.path.join(FX, "png_without_metadata.png")
TINY_PNG = os.path.join(FX, "png_tiny.png")


def read(p):
    with open(p, "rb") as fh:
        return fh.read()


def chunk(typ: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + typ + data + b"\x00\x00\x00\x00"


def synth(w: int, h: int, params: str | None, *, text_chunk: str = "tEXt") -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
    if params is not None:
        out += chunk(text_chunk.encode(), b"parameters\x00" + params.encode("utf-8"))
    return out + chunk(b"IDAT", b"\x00" * 1400) + chunk(b"IEND", b"")


GOOD_TEXT = "prompt here\nNegative prompt: , Steps: 3, Seed: 42, Size: 64x64, Clip skip: 2"


class TestRealProducerFile(unittest.TestCase):
    """Pillow 写的 PNG：块序是 IHDR/tEXt/IDAT/IEND，CRC 是真算的。"""

    def test_parses_canvas_and_metadata(self) -> None:
        info, problems = png_proof.parse_png(read(GOOD_PNG))
        self.assertEqual(problems, [])
        self.assertEqual((info["width"], info["height"]), (64, 64))
        self.assertIn("parameters", info["texts"])
        self.assertEqual(info["chunks"][:2], ["IHDR", "tEXt"])
        self.assertIn("IEND", info["chunks"])

    def test_good_file_passes_the_judge(self) -> None:
        """正向对照：不过这条，后面所有的"红"都没有信息量。"""
        info, problems = png_proof.parse_png(read(GOOD_PNG))
        self.assertEqual(png_proof.judge(info, problems, steps=3, size=(64, 64)), [])

    def test_metadata_size_agrees_with_ihdr_on_the_real_file(self) -> None:
        info, _ = png_proof.parse_png(read(GOOD_PNG))
        self.assertIn("Size: 64x64", info["params"])


class TestJudgeRedsInRightPlace(unittest.TestCase):
    def mutated(self, replace: str, with_: str):
        info, problems = png_proof.parse_png(read(GOOD_PNG))
        info = dict(info, params=info["params"].replace(replace, with_))
        return png_proof.judge(info, problems, steps=3, size=(64, 64))

    def test_wrong_step_count(self) -> None:
        self.assertIn("Steps: 20", " ".join(self.mutated("Steps: 3", "Steps: 20")))

    def test_metadata_size_disagrees_with_canvas(self) -> None:
        self.assertIn("与 IHDR", " ".join(self.mutated("Size: 64x64", "Size: 512x512")))

    def test_missing_steps_key(self) -> None:
        self.assertIn("没有 Steps", " ".join(self.mutated("Steps: 3", "Scheduler: x")))

    def test_canvas_not_the_requested_one(self) -> None:
        info, problems = png_proof.parse_png(read(GOOD_PNG))
        bad = png_proof.judge(info, problems, steps=3, size=(256, 256))
        self.assertTrue(any("画布是 64x64" in x for x in bad), bad)

    def test_too_small_is_a_problem_but_only_after_the_shape_checks(self) -> None:
        info, problems = png_proof.parse_png(read(TINY_PNG))
        bad = png_proof.judge(info, problems, steps=None, size=None, min_bytes=20000)
        self.assertTrue(any("小于下限" in x for x in bad), bad)
        self.assertIn("tEXt/parameters", " ".join(bad))     # 8x8 纯色图确实没嵌元数据

    def test_no_metadata_file_is_named_as_such(self) -> None:
        info, problems = png_proof.parse_png(read(NO_META_PNG))
        bad = png_proof.judge(info, problems, steps=3, size=(64, 64))
        self.assertEqual(len(bad), 1, bad)
        self.assertIn("tEXt/parameters", bad[0])


class TestBadBytes(unittest.TestCase):
    def test_non_png_does_not_invent_a_metadata_conclusion(self) -> None:
        """签名都不对的时候，绝不能顺带说"元数据没嵌进去"——那是另一件事。"""
        info, problems = png_proof.parse_png(b"GIF89a" + b"\x00" * 1500)
        self.assertIn("不是 PNG 签名", " ".join(problems))
        bad = png_proof.judge(info, problems, steps=3, size=None)
        self.assertNotIn("tEXt", " ".join(bad))

    def test_first_chunk_not_ihdr(self) -> None:
        b = b"\x89PNG\r\n\x1a\n" + chunk(b"tEXt", b"k\x00v") + chunk(b"IHDR", b"\x00" * 13)
        info, problems = png_proof.parse_png(b)
        self.assertIn("第一个块不是 IHDR", " ".join(problems))

    def test_ztxt_is_not_mistaken_for_plain_text(self) -> None:
        """sd.cpp 这条路只写 tEXt；遇到 zTXt 不能把 deflate 原始字节当参数文本去比。"""
        info, _ = png_proof.parse_png(synth(64, 64, GOOD_TEXT, text_chunk="zTXt"))
        self.assertEqual(info["texts"], {})
        self.assertIsNone(info["params"])

    def test_truncated_chunk_is_reported(self) -> None:
        b = synth(64, 64, GOOD_TEXT)[:240]
        _, problems = png_proof.parse_png(b)
        self.assertTrue(any("超出文件尾" in x for x in problems), problems)


class TestB64FromJobResponse(unittest.TestCase):
    def shaped(self, b64, extra=None, fmt="png"):
        d = {"id": "job_1", "status": "completed",
             "result": {"output_format": fmt, "images": [{"index": 0, "b64_json": b64}]}}
        if extra is not None:
            d.update(extra)
        return json.dumps(d)

    def writes(self, text):
        p = os.path.join(FX, "_tmp_job.json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, p)
        return p

    def test_round_trip_equals_the_file(self) -> None:
        raw = read(GOOD_PNG)
        b = png_proof.read_b64(self.writes(self.shaped(base64.b64encode(raw).decode())))
        self.assertEqual(b, raw)

    def test_html_gateway_error_says_not_json(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            png_proof.read_b64(self.writes("<html>502 Bad Gateway</html>"))
        self.assertIn("不是 JSON", str(cm.exception))

    def test_missing_result_says_so(self) -> None:
        with self.assertRaises(SystemExit) as cm:
            png_proof.read_b64(self.writes('{"status": "queued"}'))
        self.assertIn("没有 result", str(cm.exception))

    def test_empty_images(self) -> None:
        p = self.writes(json.dumps({"result": {"output_format": "png", "images": []}}))
        with self.assertRaises(SystemExit) as cm:
            png_proof.read_b64(p)
        self.assertIn("images", str(cm.exception))

    def test_jpeg_response_is_refused_not_misjudged(self) -> None:
        p = self.writes(self.shaped(base64.b64encode(b"\xff\xd8\xff" + b"\x00" * 2000).decode(),
                                    fmt="jpeg"))
        with self.assertRaises(SystemExit) as cm:
            png_proof.read_b64(p)
        self.assertIn("不是 png", str(cm.exception))

    def test_bad_base64_is_named(self) -> None:
        p = self.writes(self.shaped("!!!!not base64!!!!"))
        with self.assertRaises(SystemExit) as cm:
            png_proof.read_b64(p)
        self.assertIn("b64_json 解不开", str(cm.exception))

    def test_error_field_in_response_is_surfaced(self) -> None:
        p = self.writes(self.shaped("AAA=", extra={"error": "generate_image returned no results"}))
        with self.assertRaises(SystemExit) as cm:
            png_proof.read_b64(p)
        self.assertIn("no results", str(cm.exception))


class TestMainEndToEnd(unittest.TestCase):
    def test_good_file_returns_zero(self) -> None:
        self.assertEqual(png_proof.main(["--png", GOOD_PNG, "--steps", "3", "--size", "64x64"]), 0)

    def test_mutated_expectation_returns_one(self) -> None:
        self.assertEqual(png_proof.main(["--png", GOOD_PNG, "--steps", "9", "--size", "64x64"]), 1)

    def test_bad_size_flag_is_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit):
            png_proof.main(["--png", GOOD_PNG, "--size", "64*64"])

    def test_both_sources_are_mutually_exclusive(self) -> None:
        with self.assertRaises(SystemExit):
            png_proof.main(["--png", GOOD_PNG, "--b64-from", GOOD_PNG])


class TestParityWithCliLane(unittest.TestCase):
    """同一段字节，工具与 image-proof 里那段内联判据必须给同一个结论。"""
    JOB, NEEDLE = "image-proof", "生成一张小图"
    CASES = [("合法产物", synth(256, 256, "p\nSteps: 3, Size: 256x256")),
             ("Steps 没吃进去", synth(256, 256, "p\nSteps: 20, Size: 256x256")),
             ("元数据尺寸与画布不符", synth(256, 256, "p\nSteps: 3, Size: 512x512")),
             ("画布不是请求的尺寸", synth(128, 128, "p\nSteps: 3, Size: 128x128")),
             ("没有元数据", synth(256, 256, None)),
             ("根本不是 PNG", b"GIF89a" + b"\x00" * 1500)]

    def test_verdicts_agree_case_by_case(self) -> None:
        code = lane_replay.block(self.JOB, lane_replay.find_index(self.JOB, self.NEEDLE))
        agree = 0
        for label, png in self.CASES:
            rc, _ = lane_replay.run(code, {"out.png": png, "gen.log": "no params here\n"})
            info, problems = png_proof.parse_png(png)
            tool_bad = png_proof.judge(info, problems, steps=3, size=(256, 256))
            self.assertEqual((rc == 0), (not tool_bad), f"{label}：两处判据分家了（车道 rc={rc}）")
            agree += 1
        self.assertEqual(agree, len(self.CASES))
        # 至少一条是过的、至少一条是红的，否则"一致"可能是两边都永远失败
        rcs = [lane_replay.run(code, {"out.png": p, "gen.log": "x\n"})[0] for _, p in self.CASES]
        self.assertIn(0, rcs)
        self.assertIn(1, rcs)


if __name__ == "__main__":
    unittest.main()
