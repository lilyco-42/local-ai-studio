"""`tools/pack_engine.py` 的离线测试：出包这一步只在 CI 跑得动，所以它的判定逻辑必须在本地就被证明会红。

用现造的合成压缩包（zip 与 tar.gz）走同一条 `collect → extract_into → manifest_of → check` 路径，
再单独验一件最要紧的事：**整包哈希对不上时不许继续解包**（那是"我们装的东西和上游发的是不是同一份"
的唯一防线，也是三项预编译引擎与 OCR 唯一的共同约束）。
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import pack_engine as pe  # noqa: E402


def make_zip(path: str, files: dict) -> str:
    with zipfile.ZipFile(path, "w") as z:
        for name, payload in files.items():
            z.writestr(name, payload)
    return path


class TestCollect(unittest.TestCase):
    def test_flatten_drops_directories(self) -> None:
        self.assertEqual(pe.flatten("llama-b11727/bin/llama-server.exe"), "llama-server.exe")
        self.assertEqual(pe.flatten("whisper-bin-ubuntu-x64/Release/whisper-cli"), "whisper-cli")

    def test_same_name_twice_is_dropped_not_overwritten(self) -> None:
        """静默覆盖会让"装进去的文件"和"清单里的文件"不是同一份 —— 必须点名并留在排除里。"""
        with tempfile.TemporaryDirectory() as d:
            a = make_zip(os.path.join(d, "x.zip"), {"top/m.gguf": b"first", "other/m.gguf": b"second"})
            keep, dropped = pe.collect(a)
            self.assertEqual([dst for _s, dst in keep], ["m.gguf"])
            self.assertEqual(keep[0][0], "top/m.gguf")
            self.assertTrue(any("冲突" in x for x in dropped), dropped)

    def test_tar_gz_and_zip_share_the_interface(self) -> None:
        import tarfile
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "y.tar.gz")
            with tarfile.open(t, "w:gz") as tf:
                payload = b"cli"
                info = tarfile.TarInfo("pkg/Release/whisper-cli")
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
            self.assertEqual([dst for _s, dst in pe.collect(t)[0]], ["whisper-cli"])


class TestManifestAndChecks(unittest.TestCase):
    def test_manifest_has_hash_and_size_for_every_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            a = make_zip(os.path.join(d, "x.zip"), {"p/llama-server.exe": b"bin",
                                                    "p/llama-server-impl.dll": b"dll",
                                                    "p/test-ggml.exe": b"x"})
            keep, dropped = pe.collect(a)
            out = os.path.join(d, "out")
            pe.extract_into(a, keep, out)
            rows = pe.manifest_of(out)
            self.assertEqual([r["file"] for r in rows], ["llama-server-impl.dll", "llama-server.exe"])
            self.assertTrue(all(len(r["sha256"]) == 64 and r["bytes"] > 0 for r in rows))
            self.assertEqual(pe.check("llama", rows), [])

    def test_extracted_files_keep_the_executable_bit(self) -> None:
        """zip 的权限位在解包时容易丢，Linux/macOS 上就会"文件在但跑不起来"。"""
        with tempfile.TemporaryDirectory() as d:
            a = make_zip(os.path.join(d, "x.zip"), {"p/llama-server": b"bin"})
            out = os.path.join(d, "out")
            pe.extract_into(a, pe.collect(a)[0], out)
            self.assertTrue(os.access(os.path.join(out, "llama-server"), os.X_OK))

    def test_missing_main_binary_is_red(self) -> None:
        bad = pe.check("sd", [dict(file="libstable-diffusion.so", sha256="f" * 64, bytes=3)])
        self.assertTrue(any("主二进制" in b for b in bad), bad)


class TestDigestGate(unittest.TestCase):
    def test_wrong_digest_stops_before_extracting(self) -> None:
        """上游 digest 对不上就停 —— 不停手就把别人的产物当成我们的引擎解出来，是最坏的一种"继续"。"""
        real = pe.download
        with tempfile.TemporaryDirectory() as d:
            a = make_zip(os.path.join(d, "src.zip"), {"p/llama-server.exe": b"bin"})

            def fake_download(url, dest, timeout=3600):
                import shutil
                shutil.copyfile(a, dest)
                return 0
            pe.download = fake_download
            try:
                row = {"engine": "llama", "asset": "llama.zip", "url": "https://x/llama.zip",
                       "digest": "sha256:" + "0" * 64}
                entry, bad = pe.pack_one(row, d, keep_archives=False)
            finally:
                pe.download = real
        self.assertEqual(entry["status"], "digest_mismatch")
        self.assertTrue(any("sha256 与上游 digest 不符" in b for b in bad), bad)
        self.assertFalse(os.path.isdir(os.path.join(d, "llama")), "对不上就不该解包")

    def test_download_failure_is_reported_not_swallowed(self) -> None:
        real = pe.download
        pe.download = lambda url, dest, timeout=3600: 22
        try:
            with tempfile.TemporaryDirectory() as d:
                entry, bad = pe.pack_one({"engine": "sd", "asset": "s.zip", "url": "u",
                                          "digest": "sha256:" + "0" * 64}, d, False)
        finally:
            pe.download = real
        self.assertEqual(entry["status"], "download_failed")
        self.assertTrue(any("下载失败" in b for b in bad), bad)


class TestSelfCheckRuns(unittest.TestCase):
    def test_offline_controls_pass_and_are_not_a_rubber_stamp(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = pe.selfcheck()
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertGreaterEqual(out.count("ok  "), 10, out)
        self.assertNotIn("FAIL", out)


class TestPlanContract(unittest.TestCase):
    def test_packer_reads_the_committed_plan_shape(self) -> None:
        """pack_engine 消费 payload_plan 的产物；这张表的形状一变，两边必须同时知道。"""
        p = os.path.join(HERE, "..", "pins", "payload-plan.json")
        with open(p, encoding="utf-8") as fh:
            doc = json.load(fh)
        need = {"engine", "asset", "url", "digest", "status"}
        seen = 0
        for os_tag, rows in doc["os"].items():
            for r in rows:
                if r["status"] in ("ok", "needs_extract"):
                    seen += 1
                    self.assertTrue(need <= set(r), (os_tag, sorted(set(r))))
                    self.assertTrue(r["digest"].startswith("sha256:"), (os_tag, r["asset"]))
        self.assertGreaterEqual(seen, 7, f"计划里可打包的行只剩 {seen} 条，覆盖度掉了")


if __name__ == "__main__":
    unittest.main()
