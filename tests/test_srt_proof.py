"""tools/srt_proof.py 的判据测试。

三份语料来自两个不同的生产者（`srt_foreign_*.srt` 不是本仓库任何代码写的），
加一份严格按 whisper.cpp b5454 `examples/server/server.cpp:1037-1039` 的写法拼的样本。
解析要能吃下别人写的时间戳（一位小时、点分毫秒、CRLF、多行正文、中文），
判据却必须只认自己车道那一条硬路：编号从 1 连续、时间单调、每块有正文。

来历（两个都是"别的工具写的文件"，不是本仓库为了测试造的）：
  · `fixtures/srt_foreign_producer.srt` —— gradio 自带媒体资产 `media_assets/data/s1.srt`
    （MIT 项目内文件，367 B，一位小时 + 点分毫秒 + 多行正文）；
  · `fixtures/srt_foreign_crlf.srt` —— 本机另一项目 `lyco_agent/smoke/dev_ops.srt`
    （401 B，CRLF，中文正文）。
两者的生产者都没在这台机器上复核过，这里用它们只为了"字节是别人写的"这一件事。
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import srt_proof  # noqa: E402

FX = os.path.join(HERE, "fixtures")
WHISPER = os.path.join(FX, "srt_whisper_shaped.srt")
FOREIGN = os.path.join(FX, "srt_foreign_producer.srt")
CRLF = os.path.join(FX, "srt_foreign_crlf.srt")

BLOCK = "1\n00:00:01,000 --> 00:00:02,000\nask not what your country can do\n\n"


def parsed(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return srt_proof.parse_srt(fh.read())


class TestTimeParsing(unittest.TestCase):
    def test_comma_and_dot_millisecond_separators(self) -> None:
        self.assertEqual(srt_proof.to_seconds("00:00:01,500"), 1.5)
        self.assertEqual(srt_proof.to_seconds("0:00:01.500"), 1.5)

    def test_hours_and_long_timestamps(self) -> None:
        self.assertEqual(srt_proof.to_seconds("01:02:03,004"), 3723.004)


class TestRealFiles(unittest.TestCase):
    def test_whisper_shaped_is_clean(self) -> None:
        blocks, problems = parsed(WHISPER)
        self.assertEqual(problems, [])
        self.assertEqual(len(blocks), 5)
        self.assertEqual([b["index"] for b in blocks], [1, 2, 3, 4, 5])
        self.assertEqual(srt_proof.judge(blocks, problems, min_blocks=4,
                                         expect_any="country|ask not"), [])

    def test_crlf_file_is_parsed_not_rejected(self) -> None:
        """CRLF 是合法的 SRT 换行风格；按行切却不剥 \\r 会把时间戳行判成"不是时间戳行"。"""
        with open(CRLF, "rb") as fh:
            self.assertIn(b"\r\n", fh.read(),
                          "这份语料的全部价值就是行尾带回车；哪天被归一成 LF，"
                          "这条测试就成了「拿 LF 测 CRLF 判据」，得红在这里")
        blocks, problems = parsed(CRLF)
        self.assertEqual(problems, [])
        self.assertGreaterEqual(len(blocks), 3)
        self.assertTrue(blocks[0]["text"][0].startswith("安装"))
        self.assertNotIn("\r", blocks[0]["end"])

    def test_foreign_single_digit_hours_are_understood(self) -> None:
        blocks, _ = parsed(FOREIGN)
        self.assertGreaterEqual(len(blocks), 5)
        self.assertEqual(blocks[0]["start"], "0:00:00.000")

    def test_multiline_text_block_survives(self) -> None:
        blocks, _ = parsed(FOREIGN)
        multi = [b for b in blocks if len(b["text"]) > 1]
        self.assertTrue(multi, "另一家的语料里有多行正文，解析器把它吞了就是判据的锅")

    def test_foreign_zero_based_index_is_flagged(self) -> None:
        """不是解析失败，是判据真的绑死了 whisper 的编号写法（i+1+offset_n）。
        记下这条，是为了将来换提供方时不会有人把它当"解析器坏了"。"""
        blocks, problems = parsed(FOREIGN)
        bad = srt_proof.judge(blocks, problems, min_blocks=4, expect_any=None)
        self.assertTrue(any("第一段编号是 0" in x for x in bad), bad)


class TestJudge(unittest.TestCase):
    def judge_of(self, body: str, **kw):
        kw.setdefault("min_blocks", 1)
        kw.setdefault("expect_any", None)
        blocks, problems = srt_proof.parse_srt(body)
        return srt_proof.judge(blocks, problems, **kw)

    def test_positive_control(self) -> None:
        self.assertEqual(self.judge_of(BLOCK, expect_any="country"), [])

    def test_empty_content(self) -> None:
        self.assertIn("内容是空的", " ".join(self.judge_of("   \n\n")))

    def test_timestamp_line_without_arrow(self) -> None:
        self.assertIn("不是时间戳行", " ".join(self.judge_of("1\n00:00:01,000 00:00:02,000\nx\n")))

    def test_block_without_text(self) -> None:
        self.assertIn("没有正文", " ".join(self.judge_of("1\n00:00:01,000 --> 00:00:02,000\n\n")))

    def test_reversed_timestamps(self) -> None:
        self.assertIn("时长非正", " ".join(
            self.judge_of("1\n00:00:05,000 --> 00:00:02,000\nx\n")))

    def test_non_monotonic_start(self) -> None:
        body = ("1\n00:00:05,000 --> 00:00:06,000\nx\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\ny\n")
        self.assertIn("比前一块还早", " ".join(self.judge_of(body)))

    def test_index_gap(self) -> None:
        body = ("1\n00:00:01,000 --> 00:00:02,000\nx\n\n"
                "3\n00:00:03,000 --> 00:00:04,000\ny\n")
        self.assertIn("编号不连续", " ".join(self.judge_of(body)))

    def test_too_few_blocks(self) -> None:
        self.assertIn("少于要求的", " ".join(self.judge_of(BLOCK, min_blocks=4)))

    def test_known_answer_miss_is_red(self) -> None:
        """这一段是"格式完全正确但内容不对"的产物：只有 grep '-->' 的判据会放它过。"""
        body = "".join(f"{i}\n00:00:{i:02d},000 --> 00:00:{i:02d},500\nhello world\n\n"
                       for i in range(1, 6))
        bad = self.judge_of(body, min_blocks=4, expect_any="country|ask not")
        self.assertTrue(any("已知样本对不上" in x for x in bad), bad)
        self.assertTrue(body.count("--> ") >= 4, "对照本身得是「很多个时间戳」的样子")

    def test_webvtt_is_refused_with_a_reason(self) -> None:
        with self.assertRaises(SystemExit):
            srt_proof.parse_srt("WEBVTT\n\n00:00.000 --> 00:01.000\nhi\n")


class TestMain(unittest.TestCase):
    def test_good_file_returns_zero(self) -> None:
        self.assertEqual(srt_proof.main(["--file", WHISPER, "--min-blocks", "4",
                                         "--expect-any", "country|ask not",
                                         "--content-type", "application/x-subrip"]), 0)

    def test_wrong_content_type_returns_one(self) -> None:
        self.assertEqual(srt_proof.main(["--file", WHISPER, "--min-blocks", "1",
                                         "--content-type", "text/html; charset=utf-8"]), 1)

    def test_byte_count_printed_is_the_real_file_size(self) -> None:
        """文本模式会把 CRLF 吃掉，打印"字节数"就与文件实际长度不符（差值正好是被翻译的 \\r）。"""
        size = os.path.getsize(CRLF)
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            srt_proof.main(["--file", CRLF, "--min-blocks", "1"])
        self.assertIn(f"字节数 = {size}", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
