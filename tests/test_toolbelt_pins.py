"""toolbelt_pins 的行为测试。夹具是 2026-10-19 从 GitHub API 真抓的 release payload 与 sidecar 原文。

这些测试的价值全在"改坏一个字节必须变红"：正向对照（原样回放必须 12 条全立住）
和一批负向对照（缺 digest、缺产物、sidecar 被改、上游改名）成对出现，
否则一条永远绿的检查等于没有检查。
"""
import copy
import importlib.util
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "toolbelt_pins", os.path.join(HERE, "..", "tools", "toolbelt_pins.py"))
tb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tb)

FIX = os.path.join(HERE, "fixtures")
PINS = os.path.join(HERE, "..", "pins", "toolbelt.json")


def capture():
    return copy.deepcopy(tb.load_fixtures(FIX))


class TestReplay(unittest.TestCase):
    def test_fixture_replay_pins_everything(self) -> None:
        """正向对照：真实捕获回放必须 12 条全立住、零问题。"""
        releases, sidecars = capture()
        pins, bad = tb.build(releases, sidecars)
        self.assertEqual(bad, [])
        self.assertEqual(len(pins), 12)

    def test_two_mechanism_agreement_is_measured_not_claimed(self) -> None:
        """llmfit 的 .sha256 与 llama-swap 的 checksums.txt 各 4 条能对上 API digest；
        gguf-parser 上游没给第二套，必须如实记 None，不许混进"对照过"。"""
        releases, sidecars = capture()
        pins, _ = tb.build(releases, sidecars)
        agree = sum(1 for v in pins.values() if v["sidecar"] == "一致")
        alone = sum(1 for v in pins.values() if v["sidecar"] is None)
        self.assertEqual((agree, alone), (8, 4))
        self.assertEqual({v["tool"] for k, v in pins.items() if v["sidecar"] is None},
                         {"gguf-parser"})

    def test_committed_pin_table_matches_the_capture(self) -> None:
        """仓库里那份 pins/toolbelt.json 必须与捕获逐字段一致 —— 这条不联网也能挡漂移。"""
        releases, sidecars = capture()
        pins, _ = tb.build(releases, sidecars)
        self.assertEqual(tb.diff_against_file(pins, PINS), [])

    def test_pin_records_are_sha256_and_real_sizes(self) -> None:
        with open(PINS, encoding="utf-8") as fh:
            pins = json.load(fh)
        self.assertEqual(len(pins), 12)
        for key, v in pins.items():
            self.assertEqual(len(v["sha256"]), 64, key)
            self.assertGreater(v["size"], 1 << 20, key)  # 最小的也是 6 MiB 级
            self.assertTrue(v["url"].startswith("https://github.com/"), key)


class TestGates(unittest.TestCase):
    def test_tampered_sidecar_is_red(self) -> None:
        """把 llmfit 的一个 sidecar 首字符改掉：两套机制对不上必须点名，不能挑好看的用。"""
        releases, sidecars = capture()
        key = "llmfit-v1.1.17-x86_64-pc-windows-msvc.zip.sha256"
        sidecars[key] = "f" + sidecars[key][1:]
        pins, bad = tb.build(releases, sidecars)
        self.assertEqual(len(bad), 1)
        self.assertIn("sidecar", bad[0])
        self.assertIn("!= API digest", bad[0])
        self.assertIsNone(pins["llmfit/x86_64-pc-windows-msvc"]["sidecar"])

    def test_sidecar_naming_itself_differently_is_red(self) -> None:
        """sidecar 里写的文件名与产物不一致（上游改了打包名）也要红，不能因哈希相同就放过。"""
        releases, sidecars = capture()
        key = "llmfit-v1.1.17-x86_64-apple-darwin.tar.gz.sha256"
        sidecars[key] = sidecars[key].replace("x86_64-apple-darwin", "x86_64-apple-darwin-old")
        pins, bad = tb.build(releases, sidecars)
        self.assertEqual(len(bad), 1)
        self.assertIn("没有这个文件的名字", bad[0])

    def test_missing_api_digest_is_reported_not_silenced(self) -> None:
        releases, sidecars = capture()
        for a in releases["gguf-parser"]["assets"]:
            if a["name"] == "gguf-parser-linux-amd64":
                a["digest"] = None
        pins, bad = tb.build(releases, sidecars)
        self.assertNotIn("gguf-parser/linux-amd64", pins)
        # 两条都要说：哈希这一层缺了，以及由此导致的"这个平台没钉上"
        self.assertEqual(bad, ["gguf-parser/linux-amd64: 上游 digest 缺失或不是 sha256（None）",
                               "gguf-parser: 钉在 v0.26.4 的产物表里没有 linux-amd64"])

    def test_dropped_asset_is_a_gap_not_a_shrink(self) -> None:
        """上游少发一个平台时，必须报缺失。少报一条比整条车道消失更难发现。"""
        releases, sidecars = capture()
        releases["llama-swap"]["assets"] = [
            a for a in releases["llama-swap"]["assets"] if a["name"] != "llama-swap_265_windows_amd64.zip"]
        pins, bad = tb.build(releases, sidecars)
        self.assertNotIn("llama-swap/windows_amd64", pins)
        self.assertEqual(bad, ["llama-swap: 钉在 v265 的产物表里没有 windows_amd64"])

    def test_asset_version_drift_is_red(self) -> None:
        """产物名里的版本与钉住的 tag 不一致 → 说明这个 tag 下混进了别的东西。"""
        releases, sidecars = capture()
        for a in releases["llmfit"]["assets"]:
            if a["name"] == "llmfit-v1.1.17-x86_64-unknown-linux-gnu.tar.gz":
                a["name"] = "llmfit-v9.9.9-x86_64-unknown-linux-gnu.tar.gz"
        pins, bad = tb.build(releases, sidecars)
        self.assertEqual(len(bad), 2, "版本不一致 + 该产物随之缺失，两条都要说")
        self.assertIn("llmfit/x86_64-unknown-linux-gnu: 产物名里的版本 v9.9.9 与钉的 v1.1.17 不一致", bad)
        self.assertIn("llmfit: 钉在 v1.1.17 的产物表里没有 x86_64-unknown-linux-gnu", bad)
        self.assertNotIn("llmfit/x86_64-unknown-linux-gnu", pins)

    def test_renamed_family_reports_rule_expiry(self) -> None:
        """整族改名时不能只报"缺 4 个平台"就完事，要指出是规则过期。"""
        releases, sidecars = capture()
        for a in releases["gguf-parser"]["assets"]:
            a["name"] = "ggufparser-" + a["name"][len("gguf-parser-"):]
        pins, bad = tb.build(releases, sidecars)
        self.assertTrue(any("一个都没匹配上" in b for b in bad), bad)
        self.assertEqual(len([k for k in pins if k.startswith("gguf-parser/")]), 0)

    def test_checksums_file_absent_is_red(self) -> None:
        releases, sidecars = capture()
        sidecars.pop("llama-swap_265_checksums.txt")
        pins, bad = tb.build(releases, sidecars)
        self.assertEqual(len(bad), 4)
        self.assertTrue(all("checksums.txt" in b for b in bad), bad)
        for key in [k for k in pins if k.startswith("llama-swap/")]:
            self.assertIsNone(pins[key]["sidecar"])


class TestSidecarFormat(unittest.TestCase):
    def test_binary_mode_marker(self) -> None:
        got = tb.parse_sidecar("ab" + "0" * 62 + " *pkg.tar.gz\n")
        self.assertEqual(got, {"pkg.tar.gz": "ab" + "0" * 62})

    def test_junk_lines_ignored(self) -> None:
        got = tb.parse_sidecar("not a checksum line\n" + "c" * 64 + "  two  spaces  name\n")
        self.assertEqual(list(got), ["two  spaces  name"])


if __name__ == "__main__":
    unittest.main()
