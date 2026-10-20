#!/usr/bin/env python3
"""把提交的 32×32 ICO 派生成三平台打包要的 PNG 与 ICNS —— 纯 stdlib，可离线复算。

Tauri 的打包器按平台取图标：Windows 读 .ico，macOS 读 .icns，Linux 读 PNG
（https://v2.tauri.app/develop/icons/）。仓库里只有一张 `icon.ico`，而 ICO 里那 32×32
的像素数据是未压缩的 BGRA —— 所以不需要图像库，也不需要在本机跑 `tauri icon`
（那是下载并执行一个二进制，违背"本机不运行"的约束）。

派生规则写死在这里：同样的 ICO + 同样的 zlib 得到同样的字节。`--check` 重新算一遍，
若字节不同就**解压后逐像素比**再下结论 —— 压缩流随 zlib 实现/版本而变（本机实测
zlib-ng 1.3.1 与 zlib 1.3.2 对同一份像素给出 6,389 vs 6,393 B），逐字节比会在 CI
的另一种 zlib 上假红。它要证明的是"提交的图标是这张 ICO 派生的"，不是"由某个
zlib 版本压缩的"。
"""
from __future__ import annotations

import argparse
import hashlib
import math
import os
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
ICO = os.path.join(ROOT, "shell", "src-tauri", "icons", "icon.ico")
OUT = os.path.join(ROOT, "shell", "src-tauri", "icons")

# 每个输出文件对应的边长。ICNS 允许直接放 PNG，因此两张表都从同一批像素派生。
PNGS = ((32, "32x32.png"), (128, "128x128.png"), (256, "128x128@2x.png"))
ICNS_CHUNKS = (("icp4", 16), ("icp5", 32), ("icp6", 64),
               ("ic07", 128), ("ic08", 256), ("ic09", 512))


class IconError(Exception):
    pass


def read_ico(path: str) -> tuple[int, int, bytearray]:
    """取 ICO 里最大的 32bpp 条目，返回自顶向下的 RGBA。"""
    with open(path, "rb") as fh:
        blob = fh.read()
    if len(blob) < 22:
        raise IconError(f"{path}: 连 ICONDIR 都不够 22 字节")
    reserved, kind, count = struct.unpack_from("<HHH", blob, 0)
    if reserved != 0 or kind != 1 or not 1 <= count <= 64:
        raise IconError(f"{path}: 不像 ICO（reserved={reserved} type={kind} count={count}）")
    best = None
    for i in range(count):
        w, h, _colors, _res, _planes, bpp, size, off = struct.unpack_from(
            "<BBBBHHII", blob, 6 + 16 * i)
        w, h = w or 256, h or 256
        if bpp != 32:
            continue
        dib = blob[off:off + size]
        if len(dib) < 40:
            continue
        (dib_size, width, height, _planes2, bits, comp, _img,
         _xp, _yp, _clr, _imp) = struct.unpack_from("<IiiHHIIiiII", dib, 0)
        # 32bpp 的 biHeight 是 2×高（XOR 位图 + AND 掩码）；少数写入器只写 XOR。
        xor_h = abs(height) // 2 if abs(height) == 2 * h else abs(height)
        if dib_size < 40 or width != w or bits != 32 or comp not in (0, 3):
            continue
        need = dib_size + w * xor_h * 4
        if len(dib) < need:
            continue
        if best is None or w * h > best[0] * best[1]:
            best = (w, h, dib, dib_size, xor_h)
    if best is None:
        raise IconError(f"{path}: 没有可用的 32bpp 条目")
    w, h, dib, start, xor_h = best
    rgba = bytearray(w * h * 4)
    stride = w * 4
    for y in range(h):
        src_row = start + (xor_h - 1 - y) * stride
        for x in range(w):
            b, g, r, a = dib[src_row + x * 4:src_row + x * 4 + 4]
            i = (y * w + x) * 4
            rgba[i:i + 4] = bytes((r, g, b, a))
    return w, h, rgba


def resize(w: int, h: int, src: bytearray, sw: int, sh: int) -> bytearray:
    """预乘 alpha 的可分离双线性缩放；整数倍放大也不会出黑边。"""
    if (w, h) == (sw, sh):
        return bytearray(src)
    prem = [0.0] * (sw * sh * 4)
    for i in range(sw * sh):
        r, g, b, a = src[4 * i:4 * i + 4]
        af = a / 255.0
        prem[4 * i] = r * af
        prem[4 * i + 1] = g * af
        prem[4 * i + 2] = b * af
        prem[4 * i + 3] = float(a)

    tmp = [0.0] * (w * sh * 4)
    xmap = []
    for x in range(w):
        s = (x + 0.5) * sw / w - 0.5
        x0 = math.floor(s)
        fx = s - x0
        xmap.append((min(max(x0, 0), sw - 1), min(max(x0 + 1, 0), sw - 1), fx))
    for y in range(sh):
        base = y * sw * 4
        for x in range(w):
            xa, xb, fx = xmap[x]
            ia, ib, o = base + xa * 4, base + xb * 4, (y * w + x) * 4
            tmp[o] = prem[ia] * (1 - fx) + prem[ib] * fx
            tmp[o + 1] = prem[ia + 1] * (1 - fx) + prem[ib + 1] * fx
            tmp[o + 2] = prem[ia + 2] * (1 - fx) + prem[ib + 2] * fx
            tmp[o + 3] = prem[ia + 3] * (1 - fx) + prem[ib + 3] * fx

    out = bytearray(w * h * 4)
    ymap = []
    for y in range(h):
        s = (y + 0.5) * sh / h - 0.5
        y0 = math.floor(s)
        fy = s - y0
        ymap.append((min(max(y0, 0), sh - 1), min(max(y0 + 1, 0), sh - 1), fy))
    for y in range(h):
        ya, yb, fy = ymap[y]
        for x in range(w):
            ia = (ya * w + x) * 4
            ib = (yb * w + x) * 4
            o = (y * w + x) * 4
            a = tmp[ia + 3] * (1 - fy) + tmp[ib + 3] * fy
            if a <= 0.5:
                continue
            for c in range(3):
                v = (tmp[ia + c] * (1 - fy) + tmp[ib + c] * fy) * 255.0 / a
                out[o + c] = min(255, max(0, int(v + 0.5)))
            out[o + 3] = min(255, max(0, int(a + 0.5)))
    return out


def png_bytes(w: int, h: int, rgba: bytes) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)                       # filter type 0
        raw.extend(rgba[y * stride:(y + 1) * stride])
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def icns_bytes(source: tuple[int, int, bytearray]) -> bytes:
    sw, sh, src = source
    body = bytearray()
    for kind, size in ICNS_CHUNKS:
        png = png_bytes(size, size, resize(size, size, src, sw, sh))
        body += kind.encode("ascii") + struct.pack(">I", 8 + len(png)) + png
    return b"icns" + struct.pack(">I", 8 + len(body)) + bytes(body)


def build(ico_path: str = ICO) -> dict[str, bytes]:
    sw, sh, src = read_ico(ico_path)
    files: dict[str, bytes] = {}
    for size, name in PNGS:
        files[name] = png_bytes(size, size, resize(size, size, src, sw, sh))
    files["icon.icns"] = icns_bytes((sw, sh, src))
    return files


def png_pixels(data: bytes) -> tuple[int, int, bytes]:
    """解出 (宽, 高, 原始扫描线)。只接受本工具会写的形状：8 位 RGBA、无隔行、filter 0。"""
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise IconError("不是 PNG")
    pos, ihdr, idat, seen_end = 8, None, bytearray(), False
    while pos + 8 <= len(data):
        (n,) = struct.unpack_from(">I", data, pos)
        kind = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + n]
        if len(body) != n or pos + 12 + n > len(data):
            raise IconError("PNG chunk 被截断")
        (crc,) = struct.unpack_from(">I", data, pos + 8 + n)
        if zlib.crc32(kind + body) & 0xFFFFFFFF != crc:
            raise IconError(f"PNG {kind!r} CRC 不符")
        pos += 12 + n
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            seen_end = True
            break
    if ihdr is None or not idat or not seen_end:
        raise IconError("PNG 缺 IHDR/IDAT/IEND")
    w, h, depth, color, comp, filt, inter = ihdr
    if (depth, color, comp, filt, inter) != (8, 6, 0, 0, 0):
        raise IconError(f"PNG 形状不是 8 位 RGBA 无隔行：{ihdr}")
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as e:
        raise IconError(f"PNG IDAT 解不开：{e}")
    if len(raw) != h * (1 + w * 4):
        raise IconError("PNG 解出的扫描线长度不对")
    for y in range(h):
        if raw[y * (1 + w * 4)] != 0:
            raise IconError("PNG 用了非 0 过滤器")
    return w, h, raw


def icns_chunks(data: bytes) -> list[tuple[str, bytes]]:
    if not data.startswith(b"icns") or len(data) < 8:
        raise IconError("不是 ICNS")
    (total,) = struct.unpack_from(">I", data, 4)
    if total != len(data):
        raise IconError("ICNS 长度字段与文件不符")
    out, pos = [], 8
    while pos < len(data):
        if pos + 8 > len(data):
            raise IconError("ICNS chunk 头被截断")
        kind = data[pos:pos + 4].decode("ascii", "replace")
        (size,) = struct.unpack_from(">I", data, pos + 4)
        if size < 8 or pos + size > len(data):
            raise IconError(f"ICNS {kind} 的尺寸字段不对")
        out.append((kind, data[pos + 8:pos + size]))
        pos += size
    return out


def same_picture(name: str, got: bytes, want: bytes) -> bool:
    """字节不等时判断"是不是同一张图"：PNG 比像素与尺寸，ICNS 比逐块像素。"""
    try:
        if name.endswith(".png"):
            return png_pixels(got) == png_pixels(want)
        if name == "icon.icns":
            a, b = icns_chunks(got), icns_chunks(want)
            if [k for k, _ in a] != [k for k, _ in b]:
                return False
            for (_ka, ba), (_kb, bb) in zip(a, b):
                if ba.startswith(b"\x89PNG") or bb.startswith(b"\x89PNG"):
                    if png_pixels(ba) != png_pixels(bb):
                        return False
                elif ba != bb:
                    return False
            return True
    except (IconError, struct.error):
        return False
    return False


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="make_icons.py")
    ap.add_argument("--ico", default=ICO)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--check", action="store_true",
                    help="重算后比像素（压缩流随 zlib 版本变，不逐字节比），不写盘")
    a = ap.parse_args(argv)
    try:
        files = build(a.ico)
    except (IconError, OSError) as e:
        print(f"BAD: {e}")
        return 1
    if a.check:
        bad, recoded = [], []
        for name, want in sorted(files.items()):
            path = os.path.join(a.out, name)
            try:
                with open(path, "rb") as fh:
                    got = fh.read()
            except OSError as e:
                bad.append(f"{name}: 读不到（{e}）")
                continue
            if got == want:
                continue
            if same_picture(name, got, want):
                recoded.append(name)
                continue
            bad.append(f"{name}: {len(got)} 字节 sha256 {sha256(got)[:12]} 与重算的 "
                       f"{len(want)} 字节 {sha256(want)[:12]} 既不相同，解出来的图也不一致")
        for x in bad:
            print("BAD:", x)
        for name in recoded:
            print(f"NOTE: {name} 字节不同但像素一致（压缩流随 zlib 版本变）—— 不报错")
        print(f"比对了 {len(files)} 个图标文件，问题 {len(bad)} 条"
              f"（{len(recoded)} 个仅压缩流不同）")
        return 1 if bad else 0
    os.makedirs(a.out, exist_ok=True)
    for name, data in sorted(files.items()):
        path = os.path.join(a.out, name)
        with open(path, "wb") as fh:
            fh.write(data)
        print(f"  {name:16} {len(data):>8} B  sha256 {sha256(data)[:16]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
