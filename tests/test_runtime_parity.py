"""runtime 包的 server 在场表：换后端那句的前提是不是真的。

捕获是 **HTTP Range 读 zip 中央目录**得到的成员文件名表（`tests/fixtures/parity-captures.json`，
`.meta.json` 记 sha256 并由测试复算字节），所以没有 runner 也能离线重算；
`.tar.gz` 一律不进这条链（gzip 不可随机寻址），由工具标 `not_read` 并写明该谁证。
"""
from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import runtime_parity as rp        # noqa: E402

CLI = os.path.join(ROOT, "tools", "runtime_parity.py")
ART = os.path.join(ROOT, "pins", "runtime-parity.json")
CAP = os.path.join(ROOT, "tests", "fixtures", "parity-captures.json")
MATRIX = os.path.join(ROOT, "pins", "backend-matrix.json")
PLAN = os.path.join(ROOT, "pins", "payload-plan.json")


def artifact() -> dict:
    with open(ART, encoding="utf-8") as fh:
        return json.load(fh)


def captures() -> dict:
    with open(CAP, encoding="utf-8") as fh:
        return json.load(fh)


def matrix() -> dict:
    with open(MATRIX, encoding="utf-8") as fh:
        return json.load(fh)


def plan() -> dict:
    with open(PLAN, encoding="utf-8") as fh:
        return json.load(fh)


def rebuild(c = None, m = None, p = None) -> tuple[dict, list[str]]:
    return rp.build(c if c is not None else captures(),
                    m if m is not None else matrix(),
                    p if p is not None else plan())


class TestArtifact(unittest.TestCase):
    def test_check_zero(self) -> None:
        _doc, problems = rebuild()
        self.assertEqual(problems, [], problems)

    def test_committed_equals_recompute(self) -> None:
        fresh, _p = rebuild()
        self.assertEqual(rp.drift(artifact(), fresh), [])

    def test_capture_fixture_bytes_are_the_captured_ones(self) -> None:
        """夹具必须逐字节等于当初捕获的那份，否则这张表的输入就不是那次测量。"""
        with open(CAP + ".meta.json", encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(CAP, "rb") as fh:
            raw = fh.read()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), meta["sha256"])
        self.assertEqual(len(raw), meta["bytes"])
        self.assertNotIn(b"\r\n", raw)

    def test_stats_literals(self) -> None:
        s = artifact()["stats"]
        self.assertEqual(s, {"runtime 资产": 47, "读到成员表": 30, "没读要走出包车道": 17,
                             "有 server": 30, "缺 server 的可选档": 0, "默认档": 8,
                             "包名不写平台": 2})

    def test_assets_equal_matrix_runtime_rows(self) -> None:
        self.assertEqual(len(artifact()["assets"]), matrix()["stats"]["runtime 行"])

    def test_every_read_asset_has_the_server(self) -> None:
        read = [a for a in artifact()["assets"] if a["read"]]
        self.assertEqual(len(read), 30)
        self.assertTrue(all(a["has_server"] for a in read),
                        [a["asset"] for a in read if not a["has_server"]])
        self.assertEqual(artifact()["server_gaps"], [])

    def test_all_eight_defaults_are_proven_to_carry_the_server(self) -> None:
        """真装的 8 颗必须逐颗证过。3 颗 tar.gz 是取整包读成员表证的，
        而取整包只有在字节与上游 digest 都对得上时才算证据。"""
        df = [a for a in artifact()["assets"] if a["is_default"]]
        self.assertEqual(len(df), 8)
        self.assertEqual([a["asset"] for a in df if not a["read"]], [])
        self.assertTrue(all(a["has_server"] for a in df),
                        [a["asset"] for a in df if not a["has_server"]])

    def test_tar_read_is_pinned_to_payload_plan_bytes(self) -> None:
        """tar 那一族的字节数与哈希都从 payload-plan 已量过的数来，不是这张表自己说的。"""
        verified = {r["asset"]: (r["verified_bytes"], r["digest"])
                    for rows in plan()["os"].values() for r in rows if r.get("verified_bytes")}
        read_tar = [a for a in artifact()["assets"]
                    if a.get("read_via") == "tar_stream" and a["read"]]
        self.assertEqual(len(read_tar), 3)
        for a in read_tar:
            self.assertTrue(a["sha256_verified"], a["asset"])
            vb, digest = verified[a["asset"]]
            self.assertEqual(a["archive_bytes"], vb["bytes"], a["asset"])
            self.assertEqual(digest, "sha256:" + a["sha256"], a["asset"])

    def test_sixteen_optional_tars_are_not_paid_for(self) -> None:
        """16 颗可选档的 tar 不读整包：不为没人会下载的那一颗付流量，也不假装知道。"""
        unread = [a for a in artifact()["assets"]
                  if a["asset"].endswith(".tar.gz") and not a["read"]]
        self.assertEqual(len(unread), 16)
        self.assertEqual(artifact()["server_gaps"], [])
        for a in unread:
            self.assertIn("整包", a["why_not_read"])

    def test_server_name_rule_is_visible_in_the_table(self) -> None:
        """Windows 要带 .exe、Linux/macOS 不带：这条规则必须能在产物里被看见，
        而且要同时出现"带 .exe"和"不带 .exe"两种，否则后缀规则坏了也没人知道。"""
        want = {a["expect_server"] for a in artifact()["assets"] if a["read"]}
        self.assertEqual(want, {"llama-server.exe", "llama-server", "whisper-server.exe",
                                "whisper-server", "sd-server.exe", "sd-server"})
        import launch_plan as lp
        self.assertEqual(rp.server_name("llama", "win-x64"), lp.SERVER_BASE["llama"] + ".exe")
        self.assertEqual(rp.server_name("sd", "linux-x64"), lp.SERVER_BASE["sd"])
        self.assertEqual(rp.server_name("sd", "macos-arm64"), lp.SERVER_BASE["sd"])

    def test_unnamed_two_and_os_comes_from_plan(self) -> None:
        unnamed = {a["asset"]: a for a in artifact()["assets"] if not a["os_named"]}
        self.assertEqual(sorted(unnamed), ["whisper-bin-x64.zip",
                                           "whisper-blas-bin-x64.zip"])
        self.assertEqual(unnamed["whisper-bin-x64.zip"]["os_resolved"], "win-x64")
        self.assertTrue(unnamed["whisper-bin-x64.zip"]["read"])
        self.assertFalse(unnamed["whisper-blas-bin-x64.zip"]["read"])
        self.assertEqual(unnamed["whisper-blas-bin-x64.zip"]["read_via"], "zip_unnamed_os")

    def test_llama_win_exe_is_paired_with_impl_lib(self) -> None:
        with open(CLI, encoding="utf-8") as fh:
            src = fh.read()
        llama = [a for a in artifact()["assets"]
                 if a["engine"] == "llama" and str(a["os_resolved"]).startswith("win")]
        self.assertEqual(len(llama), 11)
        self.assertTrue(all(a["server_sibling_lib"] for a in llama))
        # whisper/sd 不套这条判据：那个形状只对 llama 量过，这里必须是 null 而不是 false。
        for a in artifact()["assets"]:
            if a["engine"] in ("whisper", "sd") and a["read"]:
                self.assertIsNone(a.get("server_sibling_lib"), a["asset"])
        # 没读到的那颗连这个字段都不该有（字段存在=假装读过）。
        self.assertTrue(all("server_sibling_lib" not in a for a in artifact()["assets"]
                            if not a["read"]))
        self.assertIn('if engine == "llama" else None', src)

    def test_every_read_package_carries_a_shared_library_closure(self) -> None:
        """解包后不是一个 exe 就能跑：每个已读包都带着共享库闭包（pack_engine 的 $ORIGIN 那句）。"""
        for a in artifact()["assets"]:
            if a["read"]:
                self.assertGreaterEqual(sum(a["libs"].values()), 1, a["asset"])

    def test_size_agrees_with_the_matrix_for_every_read_asset(self) -> None:
        """zip 比中央目录压缩后合计，tar 比整包字节 —— 两条路都得与矩阵声明对上，
        否则"读到的成员表"来自另一个包，证据不算数。"""
        for a in artifact()["assets"]:
            if not a["read"] or a["mib"] is None:
                continue
            got = a["packed_sum_mib"] if a["read_via"] == "zip_central_directory" else \
                round(a["archive_bytes"] / 1048576, 1)
            self.assertLessEqual(abs(got - a["mib"]), 1.5, (a["asset"], got, a["mib"]))

    def test_server_names_are_never_retyped_in_the_source(self) -> None:
        """可执行名只有一个来源（launch_plan.SERVER_BASE）。代码里的字符串常量出现它就是第二个定义；
        docstring 允许解释这件事（与前一条 ast 判据同一个口径）。"""
        with open(CLI, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        doc_nodes = {id(n.value) for n in tree.body
                     if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if (fn.body and isinstance(fn.body[0], ast.Expr)
                        and isinstance(fn.body[0].value, ast.Constant)):
                    doc_nodes.add(id(fn.body[0].value))
        hits = [n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and id(n) not in doc_nodes
                and any(k in n.value for k in ("llama-server", "whisper-server", "sd-server"))]
        self.assertEqual(hits, [], f"源码里出现了写死的 server 名：{hits}")        # 而且必须真的从那张表取：三个引擎的名字与 launch_plan 一致。
        import launch_plan as lp
        self.assertEqual(rp.server_name("llama", "win-x64"), lp.SERVER_BASE["llama"] + ".exe")
        self.assertEqual(rp.server_name("sd", "linux-x64"), lp.SERVER_BASE["sd"])

    def test_cli_check_exits_zero(self) -> None:
        r = subprocess.run([sys.executable, CLI, "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("问题 0 条", r.stdout)


class TestSelftest(unittest.TestCase):
    def test_selftest_exit_zero(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(rp.selftest(), 0)
        self.assertIn("BAD 数量 = 0", buf.getvalue())

    def test_fetch_writes_inside_the_loop(self) -> None:
        """这个 CN 出口会在半路断 TLS（实测过一次），一次抓取十几分钟：
        全攒在内存里最后才写 = 一断就前功尽弃。落盘必须在 for 循环体内。"""
        with open(CLI, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef) and n.name == "fetch")
        loops = [n for n in ast.walk(fn) if isinstance(n, ast.For)]
        def names(node):
            out = set()
            for f in ast.walk(node):
                if isinstance(f, ast.Call):
                    if isinstance(f.func, ast.Name):
                        out.add(f.func.id)
                    elif isinstance(f.func, ast.Attribute):
                        out.add(f.func.attr)      # zp.entries(...) / json.dump(...) 都是属性调用
            return out
        self.assertTrue(any("entries" in names(l) and "dump" in names(l) for l in loops),
                        "捕获必须每读一颗就写一次")


OLD_TAR = '        is_tar = not r["asset"].endswith(".zip")'
OLD_DEFAULT_FLOOR = '        if a is None:'
OLD_DRIFT_TUPLE = '\n'.join([
    '        for f in ("expect_server", "has_server", "read", "read_via", "entries",',
    '                  "server_sibling_lib", "packed_sum_mib", "archive_bytes", "sha256_verified",',
    '                  "landed_mib", "libs", "is_default", "os_resolved"):'])
NEW_DRIFT_TUPLE = '\n'.join([
    '        for f in ("expect_server", "read", "read_via", "entries",',
    '                  "packed_sum_mib", "archive_bytes", "sha256_verified",',
    '                  "landed_mib", "libs", "is_default", "os_resolved"):'])


class TestSourceMutants(unittest.TestCase):
    @staticmethod
    def src() -> str:
        with open(CLI, encoding="utf-8") as fh:
            return fh.read()

    MUTANTS = [
        ("捕获少一颗不报", "        if cap is None:", "        if False and cap is None:"),
        ("成员表 0 条不报", '        if s["entries"] == 0:', '        if False and s["entries"] == 0:'),
        ("体积与矩阵不符不报",
         '        if r["mib"] is not None and abs(size_mib - r["mib"]) > 1.5:',
         '        if False and r["mib"] is not None and abs(size_mib - r["mib"]) > 1.5:'),
        ("默认档缺 server 不报",
         '        if not s["has_server"] and item["is_default"]:',
         '        if False and not s["has_server"] and item["is_default"]:'),
        ("默认档缺 server 也塞进 gaps（把硬约束降格成备注）",
         '        if not s["has_server"] and item["is_default"]:',
         '        if not s["has_server"] or item["is_default"]:'),
        ("tar 被当成读过了（未知冒充缺席）", OLD_TAR, "        is_tar = False"),
        ("默认档整颗消失时不点名", OLD_DEFAULT_FLOOR, "        if False and a is None:"),
        ("tar 的整包 digest 不符不报", '            if not cap["digest_matches"]:',
         '            if False and not cap["digest_matches"]:'),
        ("真装的 tar 没读也不报", '            if item["is_default"]:',
         '            if False and item["is_default"]:'),
        ("不写平台那颗改成猜 win-x64",
         '        os_key = r["os"] if r["os"] is not None else os_of_asset.get(r["asset"])',
         '        os_key = r["os"] or "win-x64"'),
        ("has_server 不进漂移比对表", OLD_DRIFT_TUPLE, NEW_DRIFT_TUPLE),
    ]

    def load(self, text: str):
        import types
        mod = types.ModuleType("runtime_parity_mutant")
        mod.__dict__["__file__"] = CLI
        mod.__dict__["__name__"] = "runtime_parity_mutant"
        exec(compile(text, CLI, "exec"), mod.__dict__)
        return mod

    def probes(self, mod) -> dict:
        out = {}
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out["a 离线对照"] = mod.selftest() == 0
        except Exception:                            # noqa: BLE001
            out["a 离线对照"] = False
        try:
            m, p, caps = matrix(), plan(), captures()
            committed = artifact()
            fresh, _pr = mod.build(caps, m, p)
            out["b 重算与漂移"] = mod.drift(committed, fresh) == []
            dflt = next(a for a in fresh["assets"] if a.get("read") and a["is_default"]
                        and a["asset"].endswith(".zip"))
            c1 = copy.deepcopy(caps)
            c1[dflt["asset"]]["names"] = [n for n in c1[dflt["asset"]]["names"]
                                          if not n.endswith(dflt["expect_server"])]
            out["c 默认档缺 server 被报"] = any(
                "装上起不来" in x for x in mod.build(c1, m, p)[1])
            alt = next(a for a in fresh["assets"] if a.get("read") and not a["is_default"])
            c2 = copy.deepcopy(caps)
            c2[alt["asset"]]["names"] = [n for n in c2[alt["asset"]]["names"]
                                         if not n.endswith(alt["expect_server"])]
            d2, p2 = mod.build(c2, m, p)
            out["d 可选档只点名不报警"] = (
                p2 == [] and any(g["asset"] == alt["asset"] for g in d2["server_gaps"]))
            c3 = copy.deepcopy(caps)
            c3.pop(alt["asset"])
            out["e 捕获少一颗被报"] = any("是 zip 却没有捕获" in x for x in mod.build(c3, m, p)[1])
            c4 = copy.deepcopy(caps)
            c4[alt["asset"]]["packed_total"] = 10
            out["f 体积对不上被报"] = any("差过 1.5" in x for x in mod.build(c4, m, p)[1])
            c5 = copy.deepcopy(caps)
            c5[alt["asset"]]["names"] = []
            out["g 空成员表被报"] = any("解析出 0 条" in x for x in mod.build(c5, m, p)[1])
            unread_tar = {a["asset"] for a in fresh["assets"]
                          if a["asset"].endswith(".tar.gz") and not a.get("read")}
            out["h tar 不被当成读过"] = (
                len(unread_tar) == 16 and len(fresh["not_read"]) == 17
                and not (unread_tar & {g["asset"] for g in fresh["server_gaps"]})
                and not any("是 zip 却没有捕获" in x for x in _pr))
            off = {a["asset"]: a for a in fresh["assets"] if not a["os_named"]}
            out["i 平台从 plan 取不猜"] = (
                off["whisper-bin-x64.zip"].get("os_resolved") == "win-x64"
                and off["whisper-blas-bin-x64.zip"].get("read") is False)
            c6 = copy.deepcopy(caps)
            c6.pop(dflt["asset"])
            out["j 默认档消失单独点名"] = any(
                "在捕获后不见了" in x for x in mod.build(c6, m, p)[1])
            c7 = copy.deepcopy(committed)
            for a in c7["assets"]:
                if a.get("read") and not a["is_default"]:
                    a["has_server"] = not a["has_server"]
                    break
            out["k has_server 被手改要叫"] = mod.drift(c7, fresh) != []
            c8 = copy.deepcopy(caps)
            for v in c8.values():
                if v.get("reader") == "tar_stream":
                    v["digest_matches"] = False
            out["l tar 的 digest 不符被报"] = any(
                "成员表是从另一个包读的" in x for x in mod.build(c8, m, p)[1])
            c9 = copy.deepcopy(caps)
            for v in c9.values():
                if v.get("reader") == "tar_stream":
                    v["bytes"] = 1000
            out["m tar 的体积对不上被报"] = any(
                "差过 1.5" in x for x in mod.build(c9, m, p)[1])
            dtar = next(a for a in fresh["assets"]
                        if a["is_default"] and a["asset"].endswith(".tar.gz"))
            c10 = copy.deepcopy(caps)
            c10.pop(dtar["asset"])
            out["n 默认档 tar 没捕获被报"] = any(
                "没人证过" in x for x in mod.build(c10, m, p)[1])
            out["o tar 字节与哈希搬自 plan"] = all(
                a["sha256_verified"] for a in fresh["assets"]
                if a.get("read_via") == "tar_stream" and a["read"])
        except Exception as exc:                     # noqa: BLE001
            for k in ("b 重算与漂移", "c 默认档缺 server 被报", "d 可选档只点名不报警",
                      "e 捕获少一颗被报", "f 体积对不上被报", "g 空成员表被报",
                      "h tar 不被当成读过", "i 平台从 plan 取不猜", "j 默认档消失单独点名",
                      "k has_server 被手改要叫", "l tar 的 digest 不符被报",
                      "m tar 的体积对不上被报", "n 默认档 tar 没捕获被报",
                      "o tar 字节与哈希搬自 plan"):
                out.setdefault(k, False)
            out["_b 异常"] = str(exc)[:60]
        return out

    def test_baseline_probes_all_hold(self) -> None:
        self.assertEqual({k: v for k, v in self.probes(rp).items() if not k.startswith("_")},
                         {"a 离线对照": True, "b 重算与漂移": True, "c 默认档缺 server 被报": True,
                          "d 可选档只点名不报警": True, "e 捕获少一颗被报": True,
                          "f 体积对不上被报": True, "g 空成员表被报": True,
                          "h tar 不被当成读过": True, "i 平台从 plan 取不猜": True,
                          "j 默认档消失单独点名": True, "k has_server 被手改要叫": True,
                          "l tar 的 digest 不符被报": True, "m tar 的体积对不上被报": True,
                          "n 默认档 tar 没捕获被报": True, "o tar 字节与哈希搬自 plan": True})

    def test_every_mutant_is_caught(self) -> None:
        missed = []
        for name, old, new in self.MUTANTS:
            self.assertEqual(self.src().count(old), 1, f"锚点不唯一或已漂：{name}")
            got = {k: v for k, v in self.probes(
                self.load(self.src().replace(old, new, 1))).items() if not k.startswith("_")}
            if all(got.values()):
                missed.append(f"{name} → {got}")
        self.assertEqual(missed, [], "这些变异没人报：对应判据是装饰\n" + "\n".join(missed))

    def test_mutant_count(self) -> None:
        self.assertEqual(len(self.MUTANTS), 11)


if __name__ == "__main__":
    unittest.main()
