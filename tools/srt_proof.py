#!/usr/bin/env python3
"""判一份 SRT 是不是真字幕 —— 解析成块再逐块查，而不是 grep 一行 '-->'。

为什么不 grep 就够：一条 `-->` 只能证明"文本里有这个串"。编号断了、时间倒着走、
时间戳后面没有正文，这三种产物 grep 全都能过，而播放器会直接罢工。

两条车道共用：
  · CLI 那步判 `whisper-cli -osrt` 写出的文件；
  · 常驻服务那步判 `POST /inference` 的响应体 —— examples/server/server.cpp:1023-1041
    在 response_format=srt 时逐段拼 `编号\n起 --> 止\n正文\n\n`，并以
    application/x-subrip（:1041）回吐；编号是 `i + 1 + params.offset_n`，
    我们不发 offset_n，所以它必须从 1 开始连续。
"""
from __future__ import annotations

import argparse
import re
import sys

# 小时允许 1~2 位、毫秒分隔符允许逗号或点：whisper 写的是 `HH:MM:SS,mmm`
# （server.cpp:1038 的 to_timestamp），但既有语料里见过 `0:00:00.500` 这种写法
# （samples/srt_gradio_asset.srt，gradio 自带媒体资产）。解析层宽一点没关系，
# 判据（编号连续、时间单调、正文非空）照旧是硬的。
TS = r"\d{1,2}:\d{2}:\d{2}[,.]\d{3}"
LINE = re.compile(rf"^({TS})\s*-->\s*({TS})\s*(?:\S.*)?$")


def to_seconds(ts: str) -> float:
    h, m, rest = ts.split(":")
    s, _, ms = rest.partition(",")
    if not ms:                      # WebVTT 用点，SRT 用逗号：两种都吃
        s, _, ms = rest.partition(".")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(text: str) -> tuple[list[dict], list[str]]:
    """按空行切块。返回 (块列表, 问题列表)；坏块不会被静默丢掉。"""
    problems: list[str] = []
    blocks: list[dict] = []
    if not text.strip():
        return blocks, ["内容是空的"]
    # 只按 \n 切行，再把行尾 \r 去掉：CRLF 的 SRT 是合法的，不能因为换行风格就判死。
    raw = [l.rstrip("\r") for l in text.split("\n")]
    i, n = 0, len(raw)
    while i < n:
        if not raw[i].strip():
            i += 1
            continue
        head = raw[i].strip()
        if head.upper() == "WEBVTT":        # 服务同时支持 vtt，误用 response_format=vtt 要能说清
            raise SystemExit("内容以 WEBVTT 开头 —— 这是 VTT 不是 SRT，判据要换")
        block = {"index": None, "start": None, "end": None, "text": []}
        if re.fullmatch(r"\d+", head):
            block["index"] = int(head)
            i += 1
            if i >= n:
                problems.append(f"编号 {head} 后面没有下文")
                break
        ts_line = raw[i].strip()
        m = LINE.match(ts_line)
        if not m:
            problems.append(f"编号 {block['index'] if block['index'] is not None else '（缺）'}"
                            f" 的第 2 行不是时间戳行：{ts_line[:80]!r}")
            i += 1
            while i < n and raw[i].strip():
                i += 1
            continue
        block["start"], block["end"] = m.group(1), m.group(2)
        i += 1
        while i < n and raw[i].strip():
            block["text"].append(raw[i].strip())
            i += 1
        blocks.append(block)
    return blocks, problems


def judge(blocks: list[dict], problems: list[str], *, min_blocks: int,
          expect_any: str | None) -> list[str]:
    out = list(problems)
    if len(blocks) < min_blocks:
        out.append(f"只解析出 {len(blocks)} 个完整块，少于要求的 {min_blocks}")
        return out
    if blocks[0]["index"] != 1:
        out.append(f"第一段编号是 {blocks[0]['index']}，不是 1 —— "
                   "server.cpp:1037 用的是 i+1+offset_n，而请求没发 offset_n")
    prev_start, prev_index = -1.0, 0
    for k, b in enumerate(blocks):
        if b["index"] is None:
            out.append(f"第 {k + 1} 块缺编号")
        elif k and b["index"] != prev_index + 1:
            out.append(f"编号不连续：{prev_index} 之后是 {b['index']}")
        if not b["text"] or not " ".join(b["text"]).strip():
            out.append(f"第 {b['index'] or k + 1} 块时间戳后面没有正文")
        try:
            s, e = to_seconds(b["start"]), to_seconds(b["end"])
        except (TypeError, ValueError) as exc:
            out.append(f"第 {k + 1} 块时间戳解析失败（{exc}）：{b['start']} / {b['end']}")
            continue
        if e <= s:
            out.append(f"第 {b['index'] or k + 1} 块时长非正：{b['start']} --> {b['end']}")
        if s < prev_start:
            out.append(f"第 {b['index'] or k + 1} 块起始时间比前一块还早：{b['start']}")
        prev_start, prev_index = s, b["index"] if b["index"] is not None else prev_index
    if expect_any:
        blob = " ".join(" ".join(b["text"]) for b in blocks).lower()
        hits = re.findall(expect_any, blob)
        if not hits:
            out.append(f"转写文本里没有 {expect_any!r} 里的任何一个词 —— 与已知样本对不上")
        else:
            print(f"命中的已知答案词 = {sorted(set(hits))[:6]}")
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="srt_proof.py")
    ap.add_argument("--file", required=True, help="要判的 SRT 文件")
    ap.add_argument("--min-blocks", type=int, default=4)
    ap.add_argument("--expect-any", default=None, help="小写正则，至少要命中一个词")
    ap.add_argument("--content-type", default=None,
                    help="服务端返回的 Content-Type；给了就要求它含 subrip")
    a = ap.parse_args(argv)

    # 以二进制读再自己解码：文本模式会把 CRLF 吃掉，打印出来的"字节数"就与文件实际长度不符
    # （samples 里那份 CRLF 的既有语料 401 B，文本模式读出 382 B —— 差值正是被翻译掉的 \r）。
    with open(a.file, "rb") as fh:
        raw = fh.read()
    text = raw.decode("utf-8", "replace")
    print(f"字节数 = {len(raw)}，行数 = {text.count(chr(10)) + 1}")
    bad: list[str] = []
    if a.content_type is not None and "subrip" not in a.content_type.lower():
        bad.append(f"Content-Type 不是 x-subrip（实为 {a.content_type!r}）—— "
                   "响应体可能是 HTML 错误页，先别看正文")
    blocks, problems = parse_srt(text)
    print(f"解析出 {len(blocks)} 块；首块 = {blocks[0] if blocks else '无'}")
    if blocks:
        print(f"末块 = {blocks[-1]}")
    bad += judge(blocks, problems, min_blocks=a.min_blocks, expect_any=a.expect_any)
    for x in bad:
        print("BAD:", x)
    if bad:
        return 1
    print(f"通过：{len(blocks)} 段字幕，编号连续、时间戳单调、正文非空")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
