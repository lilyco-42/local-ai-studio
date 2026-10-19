"""把 image-proof 车道里那段"读 PNG 产物"的判据抽出来，用合成产物回放。

为什么要有这个文件：车道要在 CI 里真跑一次才会告诉我们判据写错了，而 CI 现在跑不起来；
上一版就是靠 CI 之外的源码核对才发现"Steps:/Size: 根本不在日志里，而在 PNG 的 tEXt 块"。
所以这段判据必须能在不启动 sd.cpp 的前提下被证明：好产物过、坏产物各自红在对的地方。

合成的 PNG 是按上游写出格式拼的（签名 + IHDR + tEXt(parameters) + 补长的 IDAT + IEND），
判据本身不验 CRC，所以这里也不填真 CRC —— 它验的是块结构与字段一致性。
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lane_replay  # noqa: E402  （discover -s tests 会自己加这条路径，单跑一个模块时不会）

JOB, NEEDLE = "image-proof", "生成一张小图"


def lane_block() -> str:
    """取出 image-proof 里那段真正做产物判据的 heredoc PY（与 yaml_gate 同一个抽法）。"""
    return lane_replay.block(JOB, lane_replay.find_index(JOB, NEEDLE))

def chunk(typ: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + typ + data + b"\x00\x00\x00\x00"

def make_png(w: int, h: int, params: str | None = None) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
    if params is not None:
        out += chunk(b"tEXt", b"parameters\x00" + params.encode("utf-8"))
    out += chunk(b"IDAT", b"\x00" * 1400)      # 判据要求 >1024 B，这里只凑体积不画像素
    return out + chunk(b"IEND", b"")

def run_block(png: bytes) -> tuple[int, str]:
    """把合成产物交给车道自己那段判据。"""
    return lane_replay.run(lane_block(),
                           {"out.png": png, "gen.log": "some log without any Steps line\n"})

GOOD = "prompt | Steps: 3, CFG scale: 5.000000, Seed: 42, Size: 256x256, Model: sd.gguf"

class TestImageProofBlock(unittest.TestCase):
    def test_good_artifact_passes(self) -> None:
        """正向对照：合法产物必须 0 退出，否则后面三条的"红"没有信息量。"""
        code, out = run_block(make_png(256, 256, GOOD))
        self.assertEqual(code, 0, out)

    def test_missing_metadata_is_named(self) -> None:
        code, out = run_block(make_png(256, 256, None))
        self.assertEqual(code, 1)
        self.assertIn("tEXt/parameters", out)

    def test_wrong_step_count_is_caught(self) -> None:
        """参数没吃进去（或上游改了写法）时，必须红在 Steps 这一句，而不是含糊地说不是 PNG。"""
        code, out = run_block(make_png(256, 256, GOOD.replace("Steps: 3", "Steps: 20")))
        self.assertEqual(code, 1)
        self.assertIn("--steps 3", out)

    def test_metadata_size_must_match_the_canvas(self) -> None:
        code, out = run_block(make_png(256, 256, GOOD.replace("Size: 256x256", "Size: 512x512")))
        self.assertEqual(code, 1)
        self.assertIn("与 IHDR", out)

    def test_canvas_must_be_the_requested_one(self) -> None:
        code, out = run_block(make_png(128, 128, GOOD.replace("Size: 256x256", "Size: 128x128")))
        self.assertEqual(code, 1)
        self.assertIn("画布是 128x128", out)

    def test_non_png_is_rejected_before_anything_else(self) -> None:
        code, out = run_block(b"GIF89a" + b"\x00" * 1500)
        self.assertEqual(code, 1)
        self.assertIn("合法 PNG", out)

if __name__ == "__main__":
    unittest.main()
