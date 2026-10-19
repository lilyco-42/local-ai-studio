"""engine_probe 的行为测试。夹具是 2026-10-18 从 GitHub API 真抓的响应，不是照文档编的。"""
import copy
import importlib.util
import json
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("engine_probe", os.path.join(HERE, "..", "tools", "engine_probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def load(engine):
    with open(os.path.join(HERE, "fixtures", f"{engine}.json"), encoding="utf-8") as fh:
        return json.load(fh)


class TestNaming(unittest.TestCase):
    def test_llama_win_cpu(self) -> None:
        v = probe.parse_asset("llama", "llama-b11719-bin-win-cpu-x64.zip")
        self.assertEqual((v["platform"], v["backend"], v["arch"], v["bundle"]), ("win", "cpu", "x64", "main"))
        self.assertEqual(v["target"], "llama-win-cpu-x64")

    def test_whisper_cuda_version_is_three_segments(self) -> None:
        v = probe.parse_asset("whisper", "whisper-bin-win-cuda-12.4.0-x64.zip")
        self.assertEqual((v["platform"], v["backend"], v["backend_version"]), ("win", "cuda", "12.4.0"))

    def test_whisper_cublas_old_name_is_cuda_not_base(self) -> None:
        """whisper-cublas-12.4.0-bin-x64.zip：后端写在 bin 之前，rest 里只剩 arch。
        上一版只看 rest，把它当成 CPU 构建，和真 CPU 包抢同一个 target。"""
        v = probe.parse_asset("whisper", "whisper-cublas-12.4.0-bin-x64.zip")
        self.assertEqual((v["backend"], v["backend_version"], v["target"]),
                         ("cuda", "12.4.0", "whisper-win-cuda-x64"))

    def test_sd_cuda_is_written_cu12(self) -> None:
        v = probe.parse_asset("sd", "cudart-sd-bin-win-cu12-x64.zip")
        self.assertEqual(v["backend"], "cuda", "sd 把 CUDA 写作 cu12，必须归一到 cuda 族")
        self.assertEqual((v["platform"], v["backend_version"], v["bundle"]), ("win", "12", "cudart"))
        self.assertEqual(v["target"], "sd-win-cuda-x64-cudart")

    def test_sd_filename_carries_only_sha_not_tag(self) -> None:
        """tag 是 master-975-95b3090，文件名却是 sd-master-95b3090-…：
        上一版正则要求 975- 存在，整族 sd 静默消失，只剩一个 cudart 包。"""
        v = probe.parse_asset("sd", "sd-master-95b3090-bin-win-cpu-x64.zip")
        self.assertEqual((v["platform"], v["backend"], v["arch"]), ("win", "cpu", "x64"))

    def test_sd_linux_darwin_caps(self) -> None:
        lin = probe.parse_asset("sd", "sd-master-95b3090-bin-Linux-Ubuntu-24.04-x86_64.zip")
        mac = probe.parse_asset("sd", "sd-master-95b3090-bin-Darwin-macOS-26.6.2-arm64.zip")
        roc = probe.parse_asset("sd", "sd-master-95b3090-bin-Linux-Ubuntu-24.04-x86_64-rocm-10.1.0.zip")
        vul = probe.parse_asset("sd", "sd-master-95b3090-bin-Linux-Ubuntu-24.04-x86_64-vulkan.zip")
        self.assertEqual((lin["platform"], lin["backend"], lin["arch"]), ("ubuntu", "base", "x64"), "x86_64 要归一成 x64")
        self.assertEqual((mac["platform"], mac["arch"]), ("macos", "arm64"))
        self.assertEqual((roc["backend"], roc["backend_version"]), ("rocm", "10.1.0"))
        self.assertEqual(vul["backend"], "vulkan")

    def test_os_token_absent_is_inferred_win_and_flagged(self) -> None:
        """whisper-bin-x64.zip 不写 OS。这里只承认是推断，真凭据由 CI 解压看 .exe/.dll 给。"""
        v = probe.parse_asset("whisper", "whisper-bin-x64.zip")
        self.assertEqual(v["platform"], "win")
        self.assertTrue(v["inferred_platform"])
        w32 = probe.parse_asset("whisper", "whisper-blas-bin-Win32.zip")
        self.assertEqual((w32["platform"], w32["arch"], w32["backend"]), ("win", "x86", "blas"))
        self.assertFalse(w32["inferred_platform"], "写了 Win32 的不算推断")

    def test_unknown_os_is_dropped_for_other_engines(self) -> None:
        """推断规则只对 whisper 那几个已知形状成立；别把它当万能兜底。"""
        self.assertIsNone(probe.parse_asset("llama", "llama-b11719-bin-something-x64.zip"))

    def test_non_desktop_shapes_are_not_in_the_table(self) -> None:
        for n in ("llama-b11719-ui.tar.gz", "llama-b11719-xcframework.zip",
                  "llama-b11719-bin-ubuntu-s390x.tar.gz", "whisper-b5454-xcframework.zip"):
            self.assertIsNone(probe.parse_asset(n.split("-")[0], n), f"{n} 不该进 target 表")

    def test_cudart_target_never_collides_with_engine(self) -> None:
        main = probe.parse_asset("llama", "llama-b11719-bin-win-cuda-12.4-x64.zip")
        cudart = probe.parse_asset("llama", "cudart-llama-bin-win-cuda-12.4-x64.zip")
        self.assertIsNotNone(cudart, "cudart-llama-bin-…（没有构建号）是实测存在的另一种写法，上一版正则漏了它")
        self.assertNotEqual(main["target"], cudart["target"])
        self.assertEqual((main["target"], cudart["target"]), ("llama-win-cuda-x64", "llama-win-cuda-x64-cudart"))
        self.assertEqual(cudart["bundle"], "cudart")

    def test_whisper_blas_prefix_is_kept(self) -> None:
        """whisper-blas-bin-x64 的后端写在 bin 之前，rest 里只剩 arch：
        只解析 rest 会把它当成 base，和真 base 包抢同一个 target。"""
        b = probe.parse_asset("whisper", "whisper-blas-bin-x64.zip")
        self.assertEqual((b["backend"], b["target"]), ("blas", "whisper-win-blas-x64"))
        self.assertTrue(b["inferred_platform"])


class TestPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.releases = {e: load(e) for e in probe.UPSTREAM}
        cls.got = probe.plan(cls.releases)
        cls.keys = sorted(cls.got)

    def test_required_targets_resolve(self) -> None:
        self.assertEqual(probe.check(self.got, need_gpu=True), [], f"实际 target：{self.keys}")

    def test_workflow_key_contract(self) -> None:
        """engine-live.yml 按这些键取产物，键名一变车道就断，所以必须被测试钉住。"""
        for key in ("llama-ubuntu-base-x64", "whisper-ubuntu-base-x64", "sd-ubuntu-base-x64",
                    "llama-win-cpu-x64", "llama-macos-base-arm64", "sd-win-cpu-x64", "sd-macos-base-arm64"):
            self.assertTrue(key in self.got, f"缺 {key}，实际：{self.keys}")

    def test_targets_are_engine_scoped(self) -> None:
        """三家共用同名 target 会互相覆盖 —— 这正是上一版把 sd 整族吃掉的 bug。"""
        self.assertTrue(all(k.startswith((e + "-")) for k in self.got for e in probe.UPSTREAM if k.startswith(e)),
                        "target 必须以引擎名开头")
        engines = {k: self.got[k]["engine"] for k in ("llama-ubuntu-base-x64", "whisper-ubuntu-base-x64",
                                                      "sd-ubuntu-base-x64")}
        self.assertEqual(engines, {"llama-ubuntu-base-x64": "llama", "whisper-ubuntu-base-x64": "whisper",
                                   "sd-ubuntu-base-x64": "sd"})

    def test_engine_main_is_not_replaced_by_cudart(self) -> None:
        """上一版 llama-ubuntu-cuda-x64 命中的是 cudart 包，装上的是运行时不是引擎。"""
        for key, v in self.got.items():
            if v["bundle"] == "cudart":
                self.assertTrue(key.endswith("-cudart"), f"{key} 是 cudart 却没标明")
        self.assertEqual(self.got["llama-ubuntu-cuda-x64"]["bundle"], "main")

    def test_newest_release_wins_not_the_longer_cuda_tag(self) -> None:
        """whisper b5454 标 cuda-13.4、b5127 标 13.4.0：版本号段数不能把旧构建顶上来。"""
        v = self.got["whisper-win-cuda-arm64"]
        self.assertEqual((v["release"], v["backend_version"]), ("b5454", "13.4"),
                         f"挑到了 {v['release']} {v['backend_version']}")

    def test_whisper_skips_zero_asset_releases(self) -> None:
        zero = [r["tag_name"] for r in self.releases["whisper"] if not r["assets"]]
        self.assertIn("v1.9.5", zero, "夹具里那些 0 产物的 release 必须留着，否则这条测试失去意义")
        hits = [v for v in self.got.values() if v["engine"] == "whisper"]
        self.assertTrue(hits)
        self.assertTrue(all(str(v["release"]).startswith("b") for v in hits),
                        f"whisper 产物只在 b#### release 上，选中了 {[v['release'] for v in hits]}")

    def test_cuda_family_picks_highest_version(self) -> None:
        win = [v for v in self.got.values() if v["target"] == "llama-win-cuda-x64"]
        self.assertEqual(len(win), 1)
        self.assertEqual(win[0]["backend_version"], "13.4", "应挑最高 cuda 版本，而不是列表里第一个")

    def test_no_upstream_asset_is_left_unclassified(self) -> None:
        """全表遍历：除明确排除的形态，每个上游产物都必须被解析出来。
        上一版整族 sd 因正则没匹配而静默消失，就是靠这条藏不住。"""
        allow_none = {"llama": ("-ui.tar.gz", "xcframework", "s390x"),
                      "whisper": ("xcframework",), "sd": ()}
        for engine, rels in self.releases.items():
            for rel in rels:
                for a in rel["assets"]:
                    skip = any(tok in a["name"] for tok in allow_none[engine])
                    if skip:
                        self.assertIsNone(probe.parse_asset(engine, a["name"]),
                                          f"{a['name']} 本该排除却进了表")
                    else:
                        self.assertIsNotNone(probe.parse_asset(engine, a["name"]),
                                             f"{engine}:{a['name']} 没匹配上")

    def test_sd_ships_every_target_the_app_needs(self) -> None:
        """同 target 只留最新 release 的产物（上一条测的是解析，不是存活）。"""
        for key in ("sd-win-cpu-x64", "sd-win-cuda-x64", "sd-win-vulkan-x64", "sd-win-rocm-x64",
                    "sd-ubuntu-base-x64", "sd-ubuntu-vulkan-x64", "sd-ubuntu-rocm-x64",
                    "sd-macos-base-arm64", "sd-win-cuda-x64-cudart"):
            self.assertTrue(key in self.got, f"缺 {key}，实际：{self.keys}")
            self.assertEqual(self.got[key]["release"], "master-975-95b3090", f"{key} 该来自最新 release")

    def test_removing_an_asset_everywhere_is_reported(self) -> None:
        """负向对照：跨所有 release 删掉 win-cpu-x64 后必须报缺失，否则 check 是摆设。
        注意 arm64 的 win-cpu 还在——上一版只比后端不比架构，所以这条删干净也不报。"""
        cut = copy.deepcopy(self.releases)
        for rel in cut["llama"]:
            rel["assets"] = [a for a in rel["assets"] if not a["name"].endswith("-bin-win-cpu-x64.zip")]
        self.assertTrue(any(a["name"].endswith("-bin-win-cpu-arm64.zip")
                            for a in cut["llama"][0]["assets"]), "对照组里 arm64 必须还活着")
        bad = probe.check(probe.plan(cut), need_gpu=False)
        self.assertIn("llama/win-cpu-x64", bad, f"删干净了却没报缺失：{bad}")

    def test_removing_all_sd_is_reported(self) -> None:
        """负向对照 2：整族 sd 消失必须报 3 项缺失。"""
        cut = copy.deepcopy(self.releases)
        cut["sd"] = []
        bad = probe.check(probe.plan(cut), need_gpu=False)
        self.assertEqual(sorted(x for x in bad if x.startswith("sd/")),
                         ["sd/macos-base-arm64", "sd/ubuntu-base-x64", "sd/win-cpu-x64"], f"实际：{bad}")

    def test_manifest_exposes_inferred_targets(self) -> None:
        """车道按 manifest["inferred"] 决定解压哪些产物。上一版这条车道去解压
        llama-win-cpu-x64（名字里明写 win，从来不是推断），拿到的是假证据。"""
        m = probe.manifest_of(self.got)
        self.assertEqual(sorted(m), ["counts", "inferred", "no_pin", "targets"])
        self.assertEqual(m["inferred"], ["whisper-win-base-x64", "whisper-win-blas-x64"],
                         f"实际：{m['inferred']}")
        self.assertTrue(all("win" not in m["targets"][k]["asset"].lower().replace("win32", "")
                            for k in m["inferred"]), "inferred 里不许有名字已写明 OS 的产物")
        self.assertNotIn("llama-win-cpu-x64", m["inferred"])

    def test_upstream_gives_us_a_pin_for_every_target(self) -> None:
        """2026-10-19 实测：GitHub release 每个产物都有 digest，且两个真实包下载后
        本地算的 sha256 与 digest 逐字节相等——所以引擎这层的哈希来自上游。
        上游哪天不给了，必须是这条测试红，而不是下载端静默退回"不校验"。"""
        self.assertEqual(probe.pin_missing(self.got), [], f"缺 pin：{probe.pin_missing(self.got)}")
        for k, v in self.got.items():
            self.assertTrue(v["digest"].startswith("sha256:"), f"{k} digest 前缀不对")
            self.assertEqual(len(v["digest"]), 71, f"{k} digest 长度不是 sha256 的 64+7")

    def test_missing_digest_is_reported_not_silenced(self) -> None:
        """负向对照：抹掉一个 digest，pin_missing 必须点名它，否则 pin 检查是摆设。"""
        import copy
        tampered = copy.deepcopy(self.got)
        key = sorted(tampered)[0]
        tampered[key]["digest"] = None
        miss = probe.pin_missing(tampered)
        self.assertIn(key, miss, f"抹掉 {key} 的 digest 却没报")
        # 前缀对但被截断的（像是"有哈希"）也必须抓出来
        tampered[key]["digest"] = "sha256:deadbeef"
        self.assertIn(key, probe.pin_missing(tampered), "截断的假 digest 竟然通过了")

    def test_urls_match_asset_names_and_sizes_are_sane(self) -> None:
        for target, v in self.got.items():
            self.assertTrue(v["url"].endswith("/" + v["asset"]), f"{target} URL 与名字不符")
            self.assertGreater(v["size"], 1024 * 100, f"{target} 大小可疑")


if __name__ == "__main__":
    unittest.main()
