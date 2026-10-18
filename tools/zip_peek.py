#!/usr/bin/env python3
"""只读 zip 元数据：不下载整包、不解压、不运行任何东西，只看条目文件名。

用途：发现器把 `whisper-bin-x64.zip` 这种没写 OS 的产物推断成 win，CI 车道靠真解压证实。
本机不能跑二进制，但可以用 HTTP Range 读尾部拿到中央目录——文件名后缀本身就是 PE 的证据，
而且只花几十 KB～1 MB 流量。

为什么用 curl 而不是 urllib：2026-10-18 实测同一个 URL，`curl -sIL` 回 200、
`curl -r 5500000-5523978` 回 206 并给到 23,979 字节；urllib 的 HEAD 卡死到超时。
传输层的问题不该由猜测下结论，所以这里直接用验证过能通的通道。
"""
from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
import tempfile

TAIL = 1 << 20


def _curl(url: str, rng: str | None) -> tuple[bytes, int | None]:
    # rng 是裸区间（"0-0"），不是 "bytes=0-0"：curl 自己会补 `Range: bytes=` 前缀，
    # 传成 "bytes=0-0" 会拼出 `bytes=bytes=0-0`，服务端回 501——第一次跑就是栽在这里。
    if rng and rng.startswith("bytes="):
        raise SystemExit("传给 _curl 的区间不要带 bytes= 前缀")
    body_fd, body = tempfile.mkstemp(suffix=".bin")
    hdr = body + ".hdr"
    os.close(body_fd)
    try:
        cmd = ["curl", "-sSL", "--fail", "--max-time", "120", "-D", hdr, "-o", body]
        if rng:
            cmd += ["-r", rng]
        cmd.append(url)
        p = subprocess.run(cmd, capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit(f"curl rc={p.returncode}: {p.stderr.strip()[:200]}")
        head = open(hdr, "rb").read().decode("latin-1")
        m = re.findall(r"(?im)^content-range:\s*bytes\s+\d+-\d+/(\d+|\*)", head)
        total = int(m[-1]) if m and m[-1] != "*" else None
        code = re.findall(r"(?m)^HTTP/[\d.]+\s+(\d+)", head)
        if not code or code[-1] not in ("200", "206"):
            raise SystemExit(f"HTTP 状态 {code}，不是 200/206")
        if rng and code[-1] != "206":
            raise SystemExit("给了 Range 却没回 206：服务器不支持分段读，别整包下载")
        return open(body, "rb").read(), total
    finally:
        for f in (body, hdr):
            try:
                os.remove(f)
            except OSError:
                pass


def _total(url: str) -> int:
    """用 `0-0` 这一字节换 Content-Range 里的整包大小，避免整包下载。"""
    _, total = _curl(url, "0-0")
    if not total:
        raise SystemExit("0-0 没带回 Content-Range 总长，无法定位中央目录")
    return total


def names(url: str) -> list[str]:
    total = _total(url)
    back = min(total, TAIL)
    tail, _ = _curl(url, f"{total - back}-{total - 1}")
    if len(tail) != back:
        raise SystemExit(f"尾部只拿回 {len(tail)} 字节（要 {back}）")
    i = tail.rfind(b"PK\x05\x06")
    if i < 0:
        raise SystemExit(f"尾部 {len(tail)} 字节内没有 EOCD：中央目录比这还大，或不是 zip")
    _, _, _, n1, n2, cd_size, cd_off, clen = struct.unpack_from("<IHHHHIIH", tail, i)
    entries = n2 or n1
    if 0xFFFFFFFF in (cd_size, cd_off) or entries == 0xFFFF:
        raise SystemExit("zip64 中央目录未实现，请改用整包下载")
    slice_start = total - len(tail)
    if cd_off < slice_start or cd_off + cd_size > total - 22 - clen:
        cd, _ = _curl(url, f"{cd_off}-{cd_off + cd_size - 1}")
    else:
        cd = tail[cd_off - slice_start: cd_off - slice_start + cd_size]
    out, p = [], 0
    while p + 46 <= len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        nl, el, cm = struct.unpack_from("<HHH", cd, p + 28)
        out.append(cd[p + 46:p + 46 + nl].decode("utf-8", "replace"))
        p += 46 + nl + el + cm
    if len(out) != entries:
        raise SystemExit(f"中央目录声明 {entries} 条，只解析出 {len(out)} 条")
    return out


def classify(ns: list[str]) -> str:
    pe = [n for n in ns if n.lower().endswith((".exe", ".dll"))]
    so = [n for n in ns if ".so" in n.lower()]
    dy = [n for n in ns if n.lower().endswith((".dylib", ".framework/")) or ".framework/" in n]
    if pe and not so and not dy:
        return f"win（PE {len(pe)} 个，无 .so/.dylib）"
    if so and not pe and not dy:
        return f"linux（.so {len(so)} 个，无 PE/.dylib）"
    if dy and not pe and not so:
        return f"macos（.dylib/.framework {len(dy)} 个）"
    return f"混合或无 native 文件（pe={len(pe)} so={len(so)} dy={len(dy)}）"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        raise SystemExit("用法：python tools/zip_peek.py <zip-url>")
    ns = names(argv[0])
    print(f"条目 {len(ns)} 个；传输只读了尾部，没有整包下载")
    for n in ns[:25]:
        print("   ", n)
    verdict = classify(ns)
    print("判定：", verdict)
    return 0 if verdict.startswith(("win", "linux", "macos")) else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
