"""图标派生：打包器要的三份格式必须能由提交的那张 ICO 离线复算出来。

这里防的不是"文件存在"，而是三种会静默发生的漂移：
  * 提交的 PNG/ICNS 是某台机器手工导出的，与 `icon.ico` 已经不是一张图；
  * ICNS 的块类型与尺寸对不上（macOS 打包器只按类型挑，不看像素）；
  * 缩放把透明像素的颜色混进边缘（预乘 alpha 写错时的黑边）。
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import make_icons as mi  # noqa: E402

CLI = os.path.join(ROOT, "tools", "make_icons.py")
ICONS = os.path.join(ROOT, "shell", "src-tauri", "icons")


def png_dims(blob: bytes) -> tuple[int, int]:
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("不是 PNG")
    return struct.unpack(">II", blob[16:24])


def icns_chunks(blob: bytes) -> dict[str, int]:
    if blob[:4] != b"icns":
        raise AssertionError("不是 ICNS")
    total = struct.unpack(">I", blob[4:8])[0]
    if total != len(blob):
        raise AssertionError(f"ICNS 头里的长度 {total} ≠ 实际 {len(blob)}")
    out = {}
    off = 8
    while off < len(blob):
        kind = blob[off:off + 4].decode("ascii")
        size = struct.unpack(">I", blob[off + 4:off + 8])[0]
        out[kind] = size
        off += size
    if off != len(blob):
        raise AssertionError("ICNS 块没有正好铺满文件")
    return out


class TestBuild(unittest.TestCase):
    def test_png_sizes(self):
        files = mi.build()
        for size, name in mi.PNGS:
            self.assertEqual(png_dims(files[name]), (size, size), name)

    def test_icns_chunk_types_and_sizes(self):
        files = mi.build()
        chunks = icns_chunks(files["icon.icns"])
        self.assertEqual(set(chunks), {k for k, _ in mi.ICNS_CHUNKS})
        blob = files["icon.icns"]
        off = 8
        while off < len(blob):
            kind = blob[off:off + 4].decode("ascii")
            size = struct.unpack(">I", blob[off + 4:off + 8])[0]
            png = blob[off + 8:off + size]
            want = dict(mi.ICNS_CHUNKS)[kind]
            self.assertEqual(png_dims(png), (want, want), kind)
            off += size

    def test_deterministic(self):
        self.assertEqual(mi.build(), mi.build())

    def test_resize_is_premultiplied(self):
        # 2×2：左上不透明红，其余全透明。放大后不透明区域必须仍是纯红，
        # 透明区域不能出现"黑边"（未预乘 alpha 时红会往黑里掉）。
        src = bytearray([255, 0, 0, 255, 0, 0, 0, 0,
                         0, 0, 0, 0, 0, 0, 0, 0])
        out = mi.resize(8, 8, src, 2, 2)
        self.assertEqual(tuple(out[0:4]), (255, 0, 0, 255))
        for i in range(8 * 8):
            r, g, b, a = out[4 * i:4 * i + 4]
            if a == 0:
                self.assertEqual((r, g, b), (0, 0, 0))
            else:
                self.assertEqual(b, 0)
                self.assertEqual(g, 0)

    def test_read_ico_rejects_junk(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.ico")
            with open(path, "wb") as fh:
                fh.write(b"not an ico at all")
            with self.assertRaises(mi.IconError):
                mi.read_ico(path)


class TestSamePicture(unittest.TestCase):
    """跨 zlib 版本的判据：压缩流可以变，画面不许变。"""

    @staticmethod
    def recompress(data: bytes, level: int) -> bytes:
        pos, out = 8, bytearray(data[:8])
        while pos < len(data):
            (n,) = struct.unpack_from(">I", data, pos)
            kind = data[pos + 4:pos + 8]
            body = data[pos + 8:pos + 8 + n]
            if kind == b"IDAT":
                _w, _h, raw = mi.png_pixels(data)
                body = zlib.compress(raw, level)
            out += struct.pack(">I", len(body)) + kind + body
            out += struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
            pos += 12 + n
        return bytes(out)

    def test_recompressed_png_is_the_same_picture(self):
        blob = mi.build()["128x128.png"]
        other = self.recompress(blob, 1)
        self.assertNotEqual(other, blob, "level 1 与 level 9 必须真的不同，否则这条对照空转")
        self.assertTrue(mi.same_picture("128x128.png", other, blob))

    def test_one_pixel_change_is_a_different_picture(self):
        blob = mi.build()["32x32.png"]
        w, h, raw = mi.png_pixels(blob)
        rows = bytearray()
        for y in range(h):
            rows += raw[y * (1 + w * 4) + 1:(y + 1) * (1 + w * 4)]
        rows[0] ^= 0x01                       # 第一行第一个像素的 R
        other = mi.png_bytes(w, h, bytes(rows))
        self.assertFalse(mi.same_picture("32x32.png", other, blob))

    def test_recompressed_icns_is_the_same_picture(self):
        blob = mi.build()["icon.icns"]
        body = bytearray()
        for kind, chunk in mi.icns_chunks(blob):
            if chunk.startswith(b"\x89PNG"):
                chunk = self.recompress(chunk, 1)
            body += kind.encode("ascii") + struct.pack(">I", 8 + len(chunk)) + chunk
        other = b"icns" + struct.pack(">I", 8 + len(body)) + bytes(body)
        self.assertNotEqual(other, blob)
        self.assertTrue(mi.same_picture("icon.icns", other, blob))


class TestCommittedArtifacts(unittest.TestCase):
    def test_committed_icons_are_recomputable(self):
        """--check 必须真的重算（不是只比文件存在）。"""
        r = subprocess.run([sys.executable, CLI, "--check", "--out", ICONS],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("问题 0 条", r.stdout)

    def test_check_detects_tamper_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("32x32.png", "128x128.png", "128x128@2x.png", "icon.icns"):
                with open(os.path.join(ICONS, name), "rb") as fh:
                    blob = fh.read()
                with open(os.path.join(d, name), "wb") as fh:
                    fh.write(blob)
            # 缺一个
            os.remove(os.path.join(d, "icon.icns"))
            r = subprocess.run([sys.executable, CLI, "--check", "--out", d],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn("icon.icns", r.stdout)
            # 改一个字节
            with open(os.path.join(d, "icon.icns"), "wb") as fh:
                fh.write(mi.build()["icon.icns"])
            path = os.path.join(d, "32x32.png")
            with open(path, "rb") as fh:
                blob = bytearray(fh.read())
            blob[-1] ^= 0xFF
            with open(path, "wb") as fh:
                fh.write(blob)
            r = subprocess.run([sys.executable, CLI, "--check", "--out", d],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn("32x32.png", r.stdout)


if __name__ == "__main__":
    unittest.main()
