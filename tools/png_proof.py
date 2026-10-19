#!/usr/bin/env python3
"""判一张 PNG 是不是"按请求参数生成出来的"，读产物本身，不读日志。

两条车道共用同一个判据：
  · CLI 那步 —— `--png out.png`（sd-cli 直接写文件）；
  · 常驻服务那步 —— `--b64-from job.json`（sd-server 把异步任务结果塞在
    `result.images[0].b64_json`：examples/server/async_jobs.cpp:190 逐张 b64、
    :192-194 拼成 result.images，:284 产出 base64）。

为什么读 tEXt 而不是读 stdout：上游把参数字符串交给编码函数
（examples/common/media_io.cpp 的 stbi_write_png(..., parameters) →
thirdparty/stb_image_write.h 落成 keyword="parameters" 的 tEXt 块），
且默认开着 —— examples/common/common.h:219 写着 `embed_image_metadata = true`，
只有 `--disable-image-metadata` 会关（examples/common/common.cpp:1392-1395）。
所以 tEXt 里能读到 Steps/Size 同时证明两件事：参数被吃进去了、也被写进产物了。
只比文件签名与块结构，绝不比像素：像素会因指令集/浮点差异而不同。
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import struct
import sys

SIGN = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])


def parse_png(b: bytes) -> tuple[dict, list[str]]:
    """返回 (信息, 问题列表)。信息里没把握的键一律留 None，不猜。"""
    problems: list[str] = []
    info = {"bytes": len(b), "width": None, "height": None, "chunks": [], "texts": {}}
    if b[:8] != SIGN:
        problems.append(f"前 8 字节不是 PNG 签名（实为 {b[:8].hex()}）")
        return info, problems
    if b[12:16] != b"IHDR":
        problems.append(f"第一个块不是 IHDR（实为 {b[12:16]!r}）")
        return info, problems
    info["width"], info["height"] = struct.unpack(">II", b[16:24])
    pos = 8
    while pos + 8 <= len(b):
        ln = struct.unpack(">I", b[pos:pos + 4])[0]
        typ = b[pos + 4:pos + 8].decode("latin-1")
        body = b[pos + 8:pos + 8 + ln]
        info["chunks"].append(typ)
        if typ in ("tEXt", "zTXt") and b"\x00" in body:
            kw, _, val = body.partition(b"\x00")
            # zTXt 的正文是 "压缩标志 + deflate"，而 sd.cpp 这条路只写 tEXt；
            # 真遇到 zTXt 时不要把原始字节当文本比进去。
            if typ == "tEXt":
                info["texts"][kw.decode("latin-1")] = val.decode("utf-8", "replace")
        if ln + 12 > len(b) - pos:
            problems.append(f"块 {typ} 声明长度 {ln} 超出文件尾")
            break
        pos += 12 + ln
    info["params"] = info["texts"].get("parameters")
    return info, problems


def judge(info: dict, problems: list[str], *, steps: int | None,
          size: tuple[int, int] | None, min_bytes: int = 1024) -> list[str]:
    out = list(problems)
    if info["bytes"] < min_bytes:
        out.append(f"只有 {info['bytes']} 字节，小于下限 {min_bytes} —— 多半是空图或截断")
    w, h = info["width"], info["height"]
    if w is None:
        # 连 IHDR 都没读到（签名不对或第一块不是 IHDR）就别再往下编结论：
        # 这种产物报"元数据没嵌进去"会把人引到完全错误的方向。
        return out
    params = info["params"]
    if not params:
        out.append('产物里没有 tEXt/parameters —— 元数据没嵌进去（默认嵌，'
                   '只有 --disable-image-metadata 会关）；关键字实有：'
                   + (",".join(sorted(info["texts"])) or "无"))
        return out
    m = re.search(r"Steps:\s*(\d+)", params)
    s = re.search(r"Size:\s*(\d+)x(\d+)", params)
    if steps is not None:
        if not m:
            out.append("parameters 里没有 Steps: —— 判据落空，需要看上游把参数写成了什么")
        elif int(m.group(1)) != steps:
            out.append(f"产物元数据写 Steps: {m.group(1)}，不是请求的 {steps}：参数可能压根没吃进去")
    if s and w is not None and h is not None:
        if (int(s.group(1)), int(s.group(2))) != (w, h):
            out.append(f"元数据写的尺寸 {s.group(1)}x{s.group(2)} 与 IHDR 的 {w}x{h} 不一致")
    elif s is None:
        out.append("parameters 里没有 Size: WxH —— 无法与 IHDR 交叉核对")
    if size is not None and (w, h) != size:
        out.append(f"画布是 {w}x{h}，不是请求的 {size[0]}x{size[1]}")
    return out


def read_png(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def read_b64(path: str) -> bytes:
    """从 sd-server 的异步任务响应里取第一张图的 base64。

    形状按 async_jobs.cpp:190-194（result.images[] 每项有 index 与 b64_json）。
    任何一步取不到都要说清楚取到哪一步失败的 —— "响应不是 JSON"（网关 502 是 HTML）
    与 "字段名变了" 是两件完全不同的事，混成一条消息就会误导下一轮排查。
    """
    with open(path, encoding="utf-8", errors="replace") as fh:
        raw = fh.read()
    try:
        d = json.loads(raw)
    except Exception as e:
        raise SystemExit(f"{path} 不是 JSON（{e}）：前 200 字节 = {raw[:200]!r}")
    if not isinstance(d, dict):
        raise SystemExit(f"{path} 顶层不是对象，而是 {type(d).__name__}")
    for key in ("error", "message"):
        if d.get(key):
            raise SystemExit(f"任务响应里带着 {key}：{str(d[key])[:200]}")
    res = d.get("result")
    if not isinstance(res, dict):
        raise SystemExit(f"响应没有 result 对象（实为 {type(res).__name__}）；顶层键 = {sorted(d)[:12]}")
    if res.get("output_format") not in (None, "png"):
        raise SystemExit(f"output_format = {res['output_format']!r}，不是 png —— "
                         "判据只认 PNG 的块结构，别拿 JPEG 凑")
    imgs = res.get("images")
    if not isinstance(imgs, list) or not imgs:
        raise SystemExit(f"result.images 不是非空数组（实为 {type(imgs).__name__} = {str(imgs)[:120]}）")
    first = imgs[0]
    if not isinstance(first, dict):
        raise SystemExit(f"result.images[0] 不是对象（实为 {type(first).__name__}）")
    b64 = first.get("b64_json")
    if not isinstance(b64, str) or not b64:
        raise SystemExit(f"result.images[0].b64_json 缺失或为空；该项键 = {sorted(first)}")
    try:
        return base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise SystemExit(f"b64_json 解不开（{e}）：前 80 字符 = {b64[:80]!r}")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="png_proof.py")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--png", help="直接判一个 PNG 文件")
    src.add_argument("--b64-from", help="判一个 JSON 响应里的 result.images[0].b64_json")
    ap.add_argument("--steps", type=int, default=None, help="要求元数据里的 Steps 等于这个数")
    ap.add_argument("--size", default=None, help="要求画布等于 WxH，如 256x256")
    ap.add_argument("--min-bytes", type=int, default=1024,
                    help="产物体积下限；真实 256x256 图远大于这个数，测试用合成小图时才调低")
    a = ap.parse_args(argv)

    size = None
    if a.size:
        m = re.fullmatch(r"(\d+)x(\d+)", a.size)
        if not m:
            raise SystemExit(f"--size 要写成 WxH，收到 {a.size!r}")
        size = (int(m.group(1)), int(m.group(2)))

    b = read_b64(a.b64_from) if a.b64_from else read_png(a.png)
    info, problems = parse_png(b)
    print(f"字节数 = {info['bytes']}（来源 {'b64_json' if a.b64_from else a.png}）")
    print(f"IHDR 画布 = {info['width']}x{info['height']}")
    print(f"块序列 = {info['chunks'][:14]}{' …' if len(info['chunks']) > 14 else ''}")
    print(f"tEXt 关键字 = {sorted(info['texts'])}")
    if info["params"]:
        print("parameters 片段 = " + info["params"][:220].replace("\n", " | "))
    bad = judge(info, problems, steps=a.steps, size=size, min_bytes=a.min_bytes)
    for x in bad:
        print("BAD:", x)
    if bad:
        return 1
    print(f"通过：合法 PNG，Steps/尺寸与请求一致（{info['width']}x{info['height']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
