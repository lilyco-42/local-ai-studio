"""车道 ↔ 启动表 的一致性测试：表说的那一行，与车道真敲的那一行，必须是同一行。

`pins/launch-plan.json` 是按上游源码核对出来的（`launch-plan` 车道第四步），但四条执行车道里的
命令一直是**手写的**，从没与这张表对过。这里补的正是那一格：
读真实 workflow 文本 → 认出每项能力真起进程的那段 shell → 用表里的四条规矩量它。

判据本身也要能红：所以每个变异都是**改 workflow 文本**（不是改工具），
每种坏形状各一条，外加一条"真实 workflow 必须 0 问题且认出 4 段"的正向对照
（否则"0 问题"可能只是"什么都没认出来"）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import lane_argv as la        # noqa: E402

WF = os.path.join(ROOT, ".github", "workflows", "engine-live.yml")
CLI = os.path.join(ROOT, "tools", "lane_argv.py")


def wf_text() -> str:
    with open(WF, encoding="utf-8") as fh:
        return fh.read()


def plan() -> dict:
    with open(os.path.join(ROOT, "pins", "launch-plan.json"), encoding="utf-8") as fh:
        return json.load(fh)


def problems(text: str) -> list[str]:
    return la.check(plan(), text)[1]


class TestRealWorkflow(unittest.TestCase):
    def test_the_lanes_agree_with_the_table(self) -> None:
        self.assertEqual(problems(wf_text()), [])

    def test_four_capabilities_are_actually_recognised(self) -> None:
        """正向对照之二：0 问题必须建立在"四段都认出来了"之上。"""
        blocks, bad = la.check(plan(), wf_text())
        self.assertEqual(bad, [])
        self.assertEqual({c for c, _n, _t in blocks},
                         {"对话", "字幕", "生图", "文字识别"}, blocks)

    def test_a_capability_with_no_launcher_is_reported(self) -> None:
        """删掉字幕那段的二进制名 → 必须报"这张表没被任何东西执行过"，而不是安静地少一项。"""
        got = problems(wf_text().replace("-name whisper-server", "-name whisper-OTHER", 1))
        self.assertTrue(any("没有" in p and "字幕" in p for p in got), got)

    def test_filter_drops_comments_and_heredoc_bodies(self) -> None:
        blocks = la.launchers(plan(), wf_text())
        self.assertTrue(blocks)
        for _c, _n, text in blocks:
            for line in text.split("\n"):
                self.assertFalse(line.strip().startswith("#"), line)

    def test_a_block_never_reaches_past_its_own_step(self) -> None:
        """上一版按 `- name:` 正则切块，最后一步会一路吞进**下一个 job**：
        别的步骤的旗标就算到这一段头上。现在用 pyyaml 读，job 级的键不可能出现在步骤体里。"""
        for _c, _n, text in la.launchers(plan(), wf_text()):
            for key in ("timeout-minutes:", "runs-on:", "- uses:"):
                self.assertNotIn(key, text, f"步骤体里出现了 {key} —— 切块切过头了")


class TestMutants(unittest.TestCase):
    MUTANTS = [
        ("绑到 0.0.0.0", '"$SRV" -m model.bin --host 127.0.0.1 --port 9009',
         '"$SRV" -m model.bin --host 0.0.0.0 --port 9009'),
        ("绑地址写成变量", '"$SRV" -m model.bin --host 127.0.0.1 --port 9009',
         '"$SRV" -m model.bin --host $ADDR --port 9009'),
        ("根本不传绑地址", '"$SRV" -m model.bin --host 127.0.0.1 --port 9009',
         '"$SRV" -m model.bin -m model.bin --port 9009'),
        ("给 whisper 传了 --api-key", '"$SRV" -m model.bin --host 127.0.0.1 --port 9009',
         '"$SRV" -m model.bin --host 127.0.0.1 --port 9009 --api-key t0p'),
        ("给 sd-server 传了 --api-key", '"$SRV" "$MODEL_ARG" sd.gguf',
         '"$SRV" "$MODEL_ARG" sd.gguf --api-key t0p'),
        ("llama 常驻那步找的是 CLI", "-name llama-server", "-name llama-cli"),
        ("OCR 少了子命令", "release/oar ocr --det", "release/oar --det"),
        ("OCR 带了 --size", "--dict dict.txt page.png", "--dict dict.txt --size tiny page.png"),
        ("只 find 不 exec", '"$SRV" -m model.bin --host 127.0.0.1 --port 9009',
         'echo "找到了但没起" && : -m model.bin --host 127.0.0.1 --port 9009'),
        ("llama 没带 --api-key",
         '--cors-origins localhost --api-key "$TOK" > server.log 2>&1 &',
         '--cors-origins localhost > server.log 2>&1 &'),
        ("llama 把占位符原样传进去",
         '--api-key "$TOK" > server.log 2>&1 &',
         "--api-key '{token}' > server.log 2>&1 &"),
        ("llama 的钥匙变量没赋值",
         "TOK=$(python3 -c 'import secrets; print(secrets.token_hex(16))')",
         'TOK=""'),
        ("llama 丢了 --cors-origins",
         '--cors-origins localhost --api-key "$TOK" > server.log 2>&1 &',
         '--api-key "$TOK" > server.log 2>&1 &'),
        ("llama 不再验 401",
         'test "$noauth" = "401" ||', 'test "$noauth" = "200" ||'),
        ("CORS 只剩一个来源",
         "-H 'Origin: https://evil.example' \\",
         "-H 'Origin: http://localhost:1420' \\"),
    ]

    def test_baseline_has_no_mutant_applied(self) -> None:
        s = wf_text()
        for _n, old, _new in self.MUTANTS:
            self.assertEqual(s.count(old), 1, f"锚点不唯一或已漂：{_n}")

    def test_every_mutant_is_caught(self) -> None:
        missed = []
        for name, old, new in self.MUTANTS:
            got = problems(wf_text().replace(old, new, 1))
            if not got:
                missed.append(name)
        self.assertEqual(missed, [], "这些坏形状没人报：对应判据是装饰")

    def test_mutant_count(self) -> None:
        self.assertEqual(len(self.MUTANTS), 15)


class TestCli(unittest.TestCase):
    def test_selftest_exits_zero(self) -> None:
        r = subprocess.run([sys.executable, CLI, "--selftest"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("BAD 数量 = 0", r.stdout)

    def test_check_prints_what_it_recognised(self) -> None:
        r = subprocess.run([sys.executable, CLI], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout[-600:] + r.stderr[-300:])
        self.assertIn("问题 0 条", r.stdout)
        self.assertIn("4 段", r.stdout)


if __name__ == "__main__":
    unittest.main()
