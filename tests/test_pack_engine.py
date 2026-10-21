"""`tools/pack_engine.py` 的离线测试：出包这一步只在 CI 跑得动，所以它的判定逻辑必须在本地就被证明会红。

用现造的合成压缩包（zip 与 tar.gz）走同一条 `collect → extract_into → manifest_of → check` 路径，
再单独验一件最要紧的事：**整包哈希对不上时不许继续解包**（那是"我们装的东西和上游发的是不是同一份"
的唯一防线，也是三项预编译引擎与 OCR 唯一的共同约束）。
"""
import contextlib
import copy
import hashlib
import io
import json
import os
import sys
import tarfile
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
            self.assertEqual([e["dst"] for e in keep], ["m.gguf"])
            self.assertEqual(keep[0]["src"], "top/m.gguf")
            self.assertTrue(any("冲突" in x for x in dropped), dropped)

    def test_tar_gz_and_zip_share_the_interface(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "y.tar.gz")
            with tarfile.open(t, "w:gz") as tf:
                payload = b"cli"
                info = tarfile.TarInfo("pkg/Release/whisper-cli")
                info.size = len(payload)
                info.mode = 0o755
                tf.addfile(info, io.BytesIO(payload))
            self.assertEqual([e["dst"] for e in pe.collect(t)[0]], ["whisper-cli"])


class TestManifestAndChecks(unittest.TestCase):
    def test_manifest_has_hash_and_size_for_every_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            a = make_zip(os.path.join(d, "x.zip"), {"p/llama-server.exe": b"bin",
                                                    "p/llama-server-impl.dll": b"dll",
                                                    "p/test-ggml.exe": b"x"})
            keep, dropped = pe.collect(a)
            out = os.path.join(d, "out")
            pe.extract_into(a, keep, out)
            rows = pe.manifest_of(out, keep)
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


class TestShipPolicy(unittest.TestCase):
    """一个引擎只放一个可执行文件；它的动态链接闭包（含链接）一个都不能少。

    上游的 Windows 包里有 13 个工具，Linux 还会给每个工具塞一份
    `libx-impl.so`；只按名字删 exe 会剩下一堆没人用的库。
    """

    WINDOWS = {
        "llama-b11727/llama-server.exe": b"server",
        "llama-b11727/llama-server-impl.dll": b"srv-impl",
        "llama-b11727/llama-cli.exe": b"cli",
        "llama-b11727/llama-cli-impl.dll": b"cli-impl",
        "llama-b11727/llama-bench.exe": b"bench",
        "llama-b11727/llama-common.dll": b"common",
        "llama-b11727/libggml.so.1": b"versioned",
        "llama-b11727/LICENSE-LLVM-OpenMP": b"license",
        "llama-b11727/README.md": b"docs",
    }

    def test_only_the_server_executable_and_its_libraries_ship(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            a = make_zip(os.path.join(d, "w.zip"), self.WINDOWS)
            keep, dropped = pe.collect_ship(a, "llama")
        self.assertEqual([e["dst"] for e in keep],
                         ["llama-server.exe", "llama-server-impl.dll", "llama-common.dll",
                          "libggml.so.1", "LICENSE-LLVM-OpenMP"])
        self.assertTrue(any("llama-cli-impl.dll" in x and "其它工具" in x for x in dropped), dropped)
        self.assertFalse(any("llama-server-impl.dll" in x for x in dropped), dropped)

    def test_other_tools_impl_libraries_on_linux_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "l.tar.gz")
            with tarfile.open(t, "w:gz") as tf:
                for name in ("llama-b11727/llama-server", "llama-b11727/llama-cli",
                             "llama-b11727/libllama-server-impl.so",
                             "llama-b11727/libllama-quantize-impl.so",
                             "llama-b11727/libggml.so.1"):
                    info = tarfile.TarInfo(name)
                    info.size = 3
                    info.mode = 0o755
                    tf.addfile(info, io.BytesIO(b"bin"))
            keep, dropped = pe.collect_ship(t, "llama")
        self.assertEqual([e["dst"] for e in keep],
                         ["llama-server", "libllama-server-impl.so", "libggml.so.1"])
        self.assertTrue(any("quantize-impl" in x for x in dropped), dropped)

    def test_macos_dylib_symlink_chain_is_part_of_the_closure(self) -> None:
        """真身叫 libllama.0.7.0.dylib，可执行文件按 libllama.0.dylib 找。"""
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "m.tar.gz")
            with tarfile.open(t, "w:gz") as tf:
                for name, payload in (("llama-b11727/llama-server", b"server"),
                                      ("llama-b11727/libllama.0.7.0.dylib", b"dylib"),
                                      ("llama-b11727/ggml-metal.metal", b"shader")):
                    info = tarfile.TarInfo(name)
                    info.size = len(payload)
                    info.mode = 0o755 if not name.endswith(".metal") else 0o644
                    tf.addfile(info, io.BytesIO(payload))
                for link, target in (("libllama.0.dylib", "libllama.0.7.0.dylib"),
                                     ("libllama.dylib", "libllama.0.dylib")):
                    info = tarfile.TarInfo("llama-b11727/" + link)
                    info.type = tarfile.SYMTYPE
                    info.linkname = target
                    tf.addfile(info)
            keep, dropped = pe.collect_ship(t, "llama")
            out = os.path.join(d, "out")
            failed = pe.extract_into(t, keep, out)
            rows = pe.manifest_of(out, keep)
        self.assertEqual(failed, [])
        self.assertIn("ggml-metal.metal", [e["dst"] for e in keep],
                      "非可执行的数据文件不能被当成工具删掉")
        hashes = {r["file"]: r["sha256"] for r in rows}
        self.assertEqual(hashes["libllama.dylib"], hashes["libllama.0.7.0.dylib"])
        self.assertEqual(pe.check("llama", rows), [])

    def test_a_symlink_to_a_dropped_tool_is_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            t = os.path.join(d, "s.tar.gz")
            with tarfile.open(t, "w:gz") as tf:
                for name in ("p/llama-server", "p/llama-cli"):
                    info = tarfile.TarInfo(name)
                    info.size = 3
                    info.mode = 0o755
                    tf.addfile(info, io.BytesIO(b"bin"))
                info = tarfile.TarInfo("p/llama")
                info.type = tarfile.SYMTYPE
                info.linkname = "llama-cli"
                tf.addfile(info)
            keep, dropped = pe.collect_ship(t, "llama")
        self.assertEqual([e["dst"] for e in keep], ["llama-server"])
        self.assertTrue(any("链接指向" in x for x in dropped), dropped)

    def test_macos_sd_zip_with_unix_modes_ships_only_server_and_lib(self) -> None:
        """照着真包的中央目录造：Darwin SD zip 只有 5 条，工具与库都是 0755、文档 0644。"""
        files = {
            "ggml.txt": (0o644, b"ggml"),
            "libstable-diffusion.dylib": (0o755, b"dylib"),
            "sd-cli": (0o755, b"cli"),
            "sd-server": (0o755, b"server"),
            "stable-diffusion.cpp.txt": (0o644, b"doc"),
        }
        with tempfile.TemporaryDirectory() as d:
            a = os.path.join(d, "sd-mac.zip")
            with zipfile.ZipFile(a, "w") as z:
                for name, (mode, payload) in files.items():
                    info = zipfile.ZipInfo(name)
                    info.external_attr = mode << 16
                    z.writestr(info, payload)
            keep, dropped = pe.collect_ship(a, "sd")
            out = os.path.join(d, "out")
            failed = pe.extract_into(a, keep, out)
            rows = pe.manifest_of(out, keep)
            self.assertTrue(os.access(os.path.join(out, "sd-server"), os.X_OK))
        self.assertEqual([e["dst"] for e in keep], ["libstable-diffusion.dylib", "sd-server"])
        self.assertTrue(any("sd-cli" in x for x in dropped), dropped)
        self.assertEqual(failed, [])
        self.assertEqual(pe.check("sd", rows), [])

    def test_check_red_on_an_extra_executable(self) -> None:
        rows = [dict(file="llama-server.exe", exec=True, sha256="a" * 64, bytes=1),
                dict(file="llama-cli.exe", exec=True, sha256="b" * 64, bytes=1),
                dict(file="ggml.dll", exec=False, sha256="c" * 64, bytes=1)]
        bad = pe.check("llama", rows)
        self.assertTrue(any("llama-cli.exe" in b and "只留一个" in b for b in bad), bad)

    def test_archive_is_removed_unless_asked_to_keep_it(self) -> None:
        real = pe.download
        with tempfile.TemporaryDirectory() as d:
            src = make_zip(os.path.join(d, "source.zip"),
                           {"p/llama-server.exe": b"bin", "p/llama-server-impl.dll": b"dll"})
            with open(src, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()

            def fake_download(url, dest, timeout=3600):
                import shutil
                shutil.copyfile(src, dest)
                return 0
            pe.download = fake_download
            try:
                row = {"engine": "llama", "asset": "llama.zip", "url": "https://x/llama.zip",
                       "digest": "sha256:" + digest}
                entry, bad = pe.pack_one(row, d, keep_archives=False)
                self.assertEqual(bad, [])
                self.assertEqual(entry["archive_bytes"], os.path.getsize(src))
                self.assertFalse(os.path.exists(os.path.join(d, "llama.zip")))
                entry, bad = pe.pack_one(row, d, keep_archives=True)
                self.assertTrue(os.path.exists(os.path.join(d, "llama.zip")))
                self.assertEqual(entry["kept_archive"], "llama.zip")
            finally:
                pe.download = real

    def test_pack_one_manifest_records_the_policy(self) -> None:
        """跑一遍完整路径：下载→整包哈希→策略→落盘→清单。"""
        real = pe.download
        with tempfile.TemporaryDirectory() as d:
            src = make_zip(os.path.join(d, "source.zip"), self.WINDOWS)
            with open(src, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()

            def fake_download(url, dest, timeout=3600):
                import shutil
                shutil.copyfile(src, dest)
                return 0
            pe.download = fake_download
            try:
                entry, bad = pe.pack_one(
                    {"engine": "llama", "asset": "weak.zip", "url": "https://x/weak.zip",
                     "digest": "sha256:" + digest}, d, keep_archives=False)
            finally:
                pe.download = real
        self.assertEqual(bad, [])
        self.assertEqual(entry["status"], "ok")
        self.assertEqual(entry["policy"], "server-only")
        self.assertEqual(entry["executables"], ["llama-server.exe"])
        self.assertEqual(entry["symlinks"], [])
        self.assertIn("llama-cli.exe", entry["excluded"])
        self.assertFalse(any(r["file"] == "llama-cli.exe" for r in entry["files"]))


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


def rust_manifest_findings(doc: dict) -> list[str]:
    """按 `shell/core/src/manifest.rs` 的 serde 形状重放这份出包清单。

    字段名或类型改了，Python 侧测试照样绿、Rust 侧要到运行时才炸（而且是在用户机器上）。
    这里只镜像 Rust 真会读的字段；`bytes` 在 Rust 是 u64、`asset` 是 Option<String>，
    所以字符串化的体积、数字化的资产名都算不兼容。
    """
    out = []
    if not isinstance(doc.get("os"), str) or not doc["os"]:
        out.append("os 不是非空字符串")
    engines = doc.get("engines")
    if not isinstance(engines, list):
        return out + ["engines 不是数组"]
    for i, e in enumerate(engines):
        if not isinstance(e, dict):
            out.append(f"engines[{i}] 不是对象")
            continue
        if not isinstance(e.get("engine"), str):
            out.append(f"engines[{i}].engine 不是字符串")
        if not isinstance(e.get("status"), str):
            out.append(f"engines[{i}].status 不是字符串")
        asset = e.get("asset")
        if asset is not None and not isinstance(asset, str):
            out.append(f"engines[{i}].asset 既不是 null 也不是字符串")
        files = e.get("files", [])
        if not isinstance(files, list):
            out.append(f"engines[{i}].files 不是数组")
            continue
        seen = set()
        for f in files:
            if not isinstance(f, dict):
                out.append(f"engines[{i}].files 里有非对象")
                continue
            name = f.get("file")
            if (not isinstance(name, str) or not name
                    or name in (".", "..") or any(c in name for c in "/\\\0")):
                out.append(f"engines[{i}] 的 file 不是扁平文件名: {name!r}")
                continue
            if name in seen:
                out.append(f"engines[{i}] 里 {name} 出现两次（Rust 的 verify_dir 会拒绝）")
            seen.add(name)
            n = f.get("bytes")
            if isinstance(n, bool) or not isinstance(n, int) or not 0 <= n < 2 ** 64:
                out.append(f"{name}: bytes 不是 u64")
            sha = f.get("sha256")
            if (not isinstance(sha, str) or len(sha) != 64
                    or any(c not in "0123456789abcdefABCDEF" for c in sha)):
                out.append(f"{name}: sha256 不是 64 位十六进制")
    return out


class TestManifestSerdeShape(unittest.TestCase):
    def packed(self) -> dict:
        real = pe.download
        with tempfile.TemporaryDirectory() as d:
            src = make_zip(os.path.join(d, "source.zip"),
                           {"p/llama-server.exe": b"bin",
                            "p/llama-server-impl.dll": b"dll",
                            "p/ggml.dll": b"ggml"})
            with open(src, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()

            def fake_download(url, dest, timeout=3600):
                import shutil
                shutil.copyfile(src, dest)
                return 0
            pe.download = fake_download
            try:
                entry, bad = pe.pack_one(
                    {"engine": "llama", "asset": "weak.zip", "url": "https://x/weak.zip",
                     "digest": "sha256:" + digest}, d, keep_archives=False)
            finally:
                pe.download = real
        self.assertEqual(bad, [])
        return {"os": "win-x64", "engines": [entry], "total_shipped_mib": 1.0}

    def test_packed_manifest_would_deserialize_in_the_rust_shell(self) -> None:
        self.assertEqual(rust_manifest_findings(self.packed()), [])

    def test_manifest_shape_mutations_are_caught(self) -> None:
        base = self.packed()
        entry = base["engines"][0]
        cases = {
            "bytes 变成字符串": lambda d: d["engines"][0]["files"][0].update(bytes="12"),
            "sha256 只有 63 位": lambda d: d["engines"][0]["files"][0].update(sha256="f" * 63),
            "file 带路径": lambda d: d["engines"][0]["files"][0].update(file="lib/a.exe"),
            "status 变成数字": lambda d: d["engines"][0].update(status=1),
            "asset 变成数字": lambda d: d["engines"][0].update(asset=7),
            "同一引擎两颗同名": lambda d: d["engines"][0]["files"].append(
                dict(d["engines"][0]["files"][0])),
            "engines 变成对象": lambda d: d.update(engines={"llama": entry}),
        }
        for label, mutate in cases.items():
            bad = copy.deepcopy(base)
            mutate(bad)
            with self.subTest(label=label):
                self.assertTrue(rust_manifest_findings(bad), label)


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
