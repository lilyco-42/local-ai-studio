"""把剩下的三段车道判据在本地"预跑"一遍（第四段见 test_image_proof_block.py）。

这三段都是"对上/对下全靠字符串判断"的断言，CI 又跑不了；
不证明它们有承重，就等于把一条可能永远绿（或永远假红）的检查留在车道里。
每段都配一条正向（合成输入必须过），否则那几条"必须红"没有信息量。

用 (job, 步骤序号) 定位段落，并顺手核对步骤名 —— 步骤一重排，序号就会指到别的判据上，
那种指错必须在这里红，而不是悄悄测了别的东西。
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lane_replay  # noqa: E402  （discover -s tests 会自己加这条路径，单跑一个模块时不会）

WORD = "OARCI2291"


def files_of(d: dict) -> dict:
    return d


class TestOcrKnownWord(unittest.TestCase):
    JOB, NEEDLE = "ocr-proof", "认得出那个词"

    def code(self) -> str:
        return lane_replay.block(self.JOB, lane_replay.find_index(self.JOB, self.NEEDLE))

    def test_recognised_word_passes(self) -> None:
        """正向对照：带排版噪声的输出里有那个词，就必须过。"""
        out = f"## 第 1 页\n检测置信度: 0.98\n文本：【{WORD}】\n"
        rc, log = lane_replay.run(self.code(), files_of({"out.txt": out}), env={"WORD": WORD})
        self.assertEqual(rc, 0, log)

    def test_unrecognised_output_is_red(self) -> None:
        rc, log = lane_replay.run(self.code(), files_of({"out.txt": "hello world 123"}),
                                  env={"WORD": WORD})
        self.assertEqual(rc, 1, log)
        self.assertIn("没认出来", log)
        self.assertIn("tiny", log, "红的时候要告诉读的人这是模型质量底线，不是链路坏了")

    def test_empty_output_is_red_not_green(self) -> None:
        rc, _ = lane_replay.run(self.code(), files_of({"out.txt": ""}), env={"WORD": WORD})
        self.assertEqual(rc, 1)

    def test_normalisation_is_substring_by_design(self) -> None:
        """归一化后按子串比是刻意的（不假设上游怎么排版），钉住这个行为别被"顺手改严"。"""
        out = f"前缀{WORD}后缀"
        rc, log = lane_replay.run(self.code(), files_of({"out.txt": out}), env={"WORD": WORD})
        self.assertEqual(rc, 0, log)


class TestLlmfitJsonShape(unittest.TestCase):
    JOB, NEEDLE = "toolbelt-pins", "--json"

    def code(self) -> str:
        return lane_replay.block(self.JOB, lane_replay.find_index(self.JOB, self.NEEDLE))

    def test_valid_json_passes_even_if_empty(self) -> None:
        """runner 上一个模型都没有，"查无结果"是合法输出：这条必须过，否则车道会因无关原因红。"""
        rc, log = lane_replay.run(self.code(),
                                  files_of({"rec.json": '{"models": []}', "rec.err": "", "help.txt": "recommend"})
                                  , env={})
        self.assertEqual(rc, 0, log)

    def test_non_json_stdout_is_red(self) -> None:
        rc, log = lane_replay.run(self.code(),
                                  files_of({"rec.json": "error: no such subcommand",
                                            "rec.err": "usage: llmfit ...", "help.txt": "recommend"}), env={})
        self.assertEqual(rc, 1, log)
        self.assertIn("不是 JSON", log)

    def test_truncated_json_is_red(self) -> None:
        rc, log = lane_replay.run(self.code(),
                                  files_of({"rec.json": '{"models": [', "rec.err": "", "help.txt": "x"}), env={})
        self.assertEqual(rc, 1, log)


class TestOcrPinDrift(unittest.TestCase):
    JOB, NEEDLE = "ocr-catalog", "digest"

    def code(self) -> str:
        return lane_replay.block(self.JOB, lane_replay.find_index(self.JOB, self.NEEDLE))

    @staticmethod
    def pins(sha: str) -> str:
        return json.dumps({"pp-ocrv6_tiny_det.onnx": {"sha256": sha, "size": 1780590}},
                          ensure_ascii=False)

    def test_no_drift_passes(self) -> None:
        body = self.pins("a" * 64)
        rc, log = lane_replay.run(self.code(),
                                  files_of({"pins/ocr.json": body, "fresh-ocr.json": body}), env={})
        self.assertEqual(rc, 0, log)
        self.assertIn("漂移 0 条", log)

    def test_changed_digest_is_red_and_names_the_field(self) -> None:
        rc, log = lane_replay.run(self.code(),
                                  files_of({"pins/ocr.json": self.pins("a" * 64),
                                            "fresh-ocr.json": self.pins("b" * 64)}), env={})
        self.assertEqual(rc, 1, log)
        self.assertIn("pp-ocrv6_tiny_det.onnx", log)
        self.assertIn("sha256", log)

    def test_vanished_artifact_is_red(self) -> None:
        rc, log = lane_replay.run(self.code(),
                                  files_of({"pins/ocr.json": self.pins("a" * 64),
                                            "fresh-ocr.json": "{}"}), env={})
        self.assertEqual(rc, 1, log)
        self.assertIn("不再给", log)

    def test_new_default_is_reported_not_silently_accepted(self) -> None:
        rc, log = lane_replay.run(self.code(),
                                  files_of({"pins/ocr.json": "{}",
                                            "fresh-ocr.json": self.pins("a" * 64)}), env={})
        self.assertEqual(rc, 1, log)
        self.assertIn("多出一条", log)


class TestChatRatePattern(unittest.TestCase):
    """对话车道的速率判据：上游 CLI 打的是 `[ Prompt: 41.2 t/s | … ]`，
    库里那句 "tokens per second" 它根本不用（b11727 实测）。所以模式必须两种都吃，
    而且不能把"抓不到速率"和"没产出 token"混成一句话。"""

    @property
    def pattern(self) -> str:
        import re
        import yaml
        with open(lane_replay.WF, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        for st in doc["jobs"]["headless-inference"]["steps"]:
            m = re.search(r"grep -hoE '([^']+)'", st.get("run") or "")
            if m:
                return m.group(1)
        raise AssertionError("headless-inference 里找不到速率 grep 模式（被删了？）")

    def test_matches_both_spellings(self) -> None:
        import re
        for line in [
            "[ Prompt: 41.2 t/s | Generation: 8.9 t/s ]",
            "llama_perf_context_print:  eval time = 100.00 ms / 24 runs (4.17 ms per token, 240.00 tokens per second)",
        ]:
            self.assertRegex(line, self.pattern)

    def test_does_not_match_speedless_noise(self) -> None:
        import re
        self.assertFalse(re.search(self.pattern, "print_info: load time = 12.3 ms\n"))


class TestServerLane(unittest.TestCase):
    """常驻 llama-server 那条：ADR 说外壳要起服务，但四项能力车道原本全是 one-shot CLI，
    那个决定从没被验证过。这里的用例只测判据本身（真起服务要等 CI）。"""

    JOB, NEEDLE = "headless-inference", "llama-server"

    def code(self) -> str:
        return lane_replay.block(self.JOB, lane_replay.find_index(self.JOB, self.NEEDLE))

    @staticmethod
    def files(chat: str, props: str = '{"model_path":"model.gguf"}') -> dict:
        return {"chat.json": chat, "props.json": props}

    def test_openai_shape_passes(self) -> None:
        chat = json.dumps({"choices": [{"message": {"content": "hello"}}],
                           "usage": {"tokens_predicted": 3, "tokens_evaluated": 12}})
        rc, log = lane_replay.run(self.code(), self.files(chat))
        self.assertEqual(rc, 0, log)

    def test_legacy_text_field_also_passes(self) -> None:
        """/completion 的老形状用 choices[].text；两种都吃才不会因换端点而假红。"""
        chat = json.dumps({"choices": [{"text": "hi"}], "usage": {"tokens_predicted": 1}})
        rc, log = lane_replay.run(self.code(), self.files(chat))
        self.assertEqual(rc, 0, log)

    def test_error_body_is_red(self) -> None:
        rc, log = lane_replay.run(self.code(), self.files('{"error": {"code": 400, "message": "bad"}}'))
        self.assertEqual(rc, 1, log)
        self.assertIn("error", log)

    def test_empty_content_is_red_even_with_choices(self) -> None:
        """服务活着但一个字符没吐 —— 这条必须红，且要说清是常驻这条路的问题。"""
        chat = json.dumps({"choices": [{"message": {"content": "   "}}], "usage": {"tokens_predicted": 0}})
        rc, log = lane_replay.run(self.code(), self.files(chat))
        self.assertEqual(rc, 1, log)
        self.assertIn("没拿到任何文本", log)

    def test_missing_choices_key_is_not_a_crash(self) -> None:
        """响应形状变了应当报"没拿到文本"（rc=1），而不是 KeyError（rc=99）——
        崩掉的判据下次没人敢信它测过什么。"""
        rc, log = lane_replay.run(self.code(), self.files('{"detail":"unauthorized"}'))
        self.assertEqual(rc, 1, log)

    def test_non_json_response_names_the_payload_not_a_crash(self) -> None:
        """反代回 HTML 502 时，人要看见"哪个响应的哪个字节不是 JSON"，而不是只知道红了。
        rc=99 也算判据没写对：崩掉的段落下次没人知道它测过什么。"""
        rc, log = lane_replay.run(self.code(), self.files("<html>502 Bad Gateway</html>"))
        self.assertEqual(rc, 1, log)
        self.assertIn("chat.json 不是 JSON", log)
        self.assertIn("502 Bad Gateway", log)


class TestOcrLockShape(unittest.TestCase):
    """上游不发 Cargo.lock（实测 404，正向对照 Cargo.toml 206），所以 ocr 那条车道的依赖锁
    要么由仓库自己钉（pins/ocr-Cargo.lock + --locked），要么第一次跑生成后传回来。
    这段判据是在**没有 cargo 的情况下**也保证"锁形状认得出"。"""

    JOB, NEEDLE = "ocr-proof", "编译 CLI"

    def code(self) -> str:
        return lane_replay.block(self.JOB, lane_replay.find_index(self.JOB, self.NEEDLE))

    @staticmethod
    def lock(n: int = 60, ort: str = "2.0.0-rc.13", with_onig: bool = True,
             with_tok: bool = True) -> str:
        blocks = [f'[[package]]\nname = "ort"\nversion = "{ort}"\n']
        if with_onig:
            blocks.append('[[package]]\nname = "onig_sys"\nversion = "69.9.1"\n')
        if with_tok:
            blocks.append('[[package]]\nname = "tokenizers"\nversion = "0.23.1"\n')
        for i in range(n - len(blocks)):
            blocks.append(f'[[package]]\nname = "dep{i}"\nversion = "1.0.0"\n')
        return "".join(blocks)

    def files(self, text: str) -> dict:
        return {"Cargo.lock": text}

    def test_real_shaped_lock_passes(self) -> None:
        rc, log = lane_replay.run(self.code(), self.files(self.lock()))
        self.assertEqual(rc, 0, log)

    def test_drifted_ort_is_red_and_named(self) -> None:
        """ort 是上游唯一用 `=` 钉死的那个；锁里漂了就点名，而不是编出来才知道。"""
        rc, log = lane_replay.run(self.code(), self.files(self.lock(ort="2.0.0-rc.12")))
        self.assertEqual(rc, 1, log)
        self.assertIn("ort", log)

    def test_thin_lock_is_red(self) -> None:
        rc, log = lane_replay.run(self.code(), self.files(self.lock(n=8)))
        self.assertEqual(rc, 1, log)
        self.assertIn("不像一份完整的依赖锁", log)

    def test_missing_c_toolchain_dep_is_red(self) -> None:
        """onig_sys / tokenizers 缺席意味着 ADR 记的"C 工具链代价"变了，要重量而不是照旧跑。"""
        for kwargs in ({"with_onig": False}, {"with_tok": False}):
            rc, log = lane_replay.run(self.code(), self.files(self.lock(**kwargs)))
            self.assertEqual(rc, 1, f"{kwargs} → {log}")

    def test_absent_lock_is_a_verdict_not_a_crash(self) -> None:
        rc, log = lane_replay.run(self.code(), self.files(""))
        self.assertEqual(rc, 1, log)
        self.assertIn("只有 0 个包", log)


class TestHarnessItself(unittest.TestCase):
    def test_unexpected_exception_is_not_reported_as_a_legit_red(self) -> None:
        """判据段自己抛异常（比如忘了造某个文件）要报 99，不能和"判据认为不合格"混成一回事。"""
        rc, log = lane_replay.run("import os\nos.chdir('/nonexistent-zz')\n", files_of({}), env={})
        self.assertEqual(rc, 99, log)
        self.assertIn("FileNotFoundError", log)

    def test_step_lookup_must_be_unambiguous(self) -> None:
        """按名字定位时命中两步 = "不知道在测哪一段"，必须当场报错。
        写死序号的做法本轮就差点出事：往 ocr-proof 中间插一步"传依赖锁"之后，
        所有后续序号都被推位，测试会安静地指到别的判据上（这次靠步骤名 assert 才没指错）。"""
        with self.assertRaises(AssertionError):
            lane_replay.find_index("ocr-proof", "取")          # 取源码 / 取三个模型文件，两个都命中
        with self.assertRaises(AssertionError):
            lane_replay.find_index("ocr-proof", "这句名字不存在")
        idx = lane_replay.find_index("ocr-proof", "认得出那个词")
        self.assertIn("认得出那个词", lane_replay.step_name("ocr-proof", idx))


if __name__ == "__main__":
    unittest.main()
