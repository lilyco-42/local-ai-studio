"""装依赖清单的测试：那张包表必须是从上游那一页**读出来**的，而且读法要能被改坏证明。

分工：本文件不联网（把取原文换成夹具），证的是判据本身；
上游那一页**现在**写着什么，由 `citation-drift` 那两步按 pin ref 现查。
"""
from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import citation_check as cc     # noqa: E402
import install_deps as idp      # noqa: E402

ART = os.path.join(ROOT, "pins", "install-deps.json")
CLI = os.path.join(ROOT, "tools", "install_deps.py")
# 真捕获的上游两份原文（2026-10-20 按 pin ref 取的，与 .cache/refs 里那两份逐字节相同）：
# "产物 == 重算"这条比的是**真文档**，不是我改小的假页。
MDX_FIX = os.path.join(HERE, "fixtures", "tauri_prerequisites_2e513e3f.mdx")
README_FIX = os.path.join(HERE, "fixtures", "runner_ubuntu2404_readme_b4067e48.md")
MDX_FIX_SHA = "2b72fc4fa86ff825"
README_FIX_SHA = "b34e8e11c5a3a4cd"

# 真捕获的**形状**（行内容与 Tauri 前提页一致，缩进/续行反斜杠都留着）：
# 解析器咬的是这些细节，所以这里不能用简化过的假页。
MDX = ('<Tabs syncKey="distro">\n'
       '  <TabItem label="Debian">\n\n'
       '```sh\n'
       'sudo apt update\n'
       'sudo apt install libwebkit2gtk-4.1-dev \\\n'
       '  build-essential \\\n'
       '  curl \\\n'
       '  librsvg2-dev\n'
       '```\n\n'
       '  </TabItem>\n'
       '  <TabItem label="Fedora">\n\n'
       '```sh\n'
       'sudo dnf install webkit2gtk4.1-devel \\\n'
       '  openssl-devel\n'
       '```\n  </TabItem>\n</Tabs>\n')
README = ("| package | version |\n|---|---|\n"
          "| curl | 8.5.0-2ubuntu10.16 |\n"
          "| libssl-dev | 3.0.13-0ubuntu3.16 |\n"
          "| file | 1:5.45-3build1 |\n"
          "- Node.js 22.23.3\n- Rust 1.99.1\n")


def SH(*lines: str) -> str:
    """按 chr(10) 拼测试页：源码里写反斜杠转义在这一轮被吃掉过五次。"""
    return chr(10).join(lines)


def artifact():
    with open(ART, encoding="utf-8") as fh:
        return json.load(fh)


def fake_fetch(mdx=None, readme=None):
    """默认喂真捕获的两份原文；传参时用改写过的版本（负向对照用）。"""
    if mdx is None:
        with open(MDX_FIX, encoding="utf-8") as fh:
            mdx = fh.read()
    if readme is None:
        with open(README_FIX, encoding="utf-8") as fh:
            readme = fh.read()

    def f(spec, cache="", fill=False):
        return mdx if spec[2].endswith(".mdx") else readme
    return f


def _fixture_bytes() -> None:
    """夹具要是被 git 归一化或被谁改小过，"产物==重算"就变成自证。按字节证一次。"""
    import hashlib
    for path, prefix in ((MDX_FIX, MDX_FIX_SHA), (README_FIX, README_FIX_SHA)):
        with open(path, "rb") as fh:
            b = fh.read()
        assert hashlib.sha256(b).hexdigest().startswith(prefix), path
        assert b"\r\n" not in b, path        # 上游发的就是 LF；混进 CRLF 说明被改写过


def recomputed():
    real = idp.fetch
    try:
        idp.fetch = fake_fetch()
        doc, problems = idp.build("", False)
    finally:
        idp.fetch = real
    assert not problems, problems
    return doc


class TestParser(unittest.TestCase):
    def test_debian_family_is_read_with_line_numbers(self) -> None:
        fam = idp.parse_families(MDX)["Debian"]
        self.assertEqual([p["package"] for p in fam["packages"]],
                         ["libwebkit2gtk-4.1-dev", "build-essential", "curl", "librsvg2-dev"])
        self.assertEqual([p["line"] for p in fam["packages"]], [6, 7, 8, 9])
        self.assertEqual((fam["pm"], fam["install_line"]), ("apt", 6))

    def test_other_families_are_kept_separate(self) -> None:
        fams = idp.parse_families(MDX)
        self.assertEqual(sorted(fams), ["Debian", "Fedora"])
        self.assertEqual([p["package"] for p in fams["Fedora"]["packages"]],
                         ["webkit2gtk4.1-devel", "openssl-devel"])
        self.assertEqual(fams["Fedora"]["pm"], "dnf")

    def test_install_verb_and_flags_are_not_packages(self) -> None:
        """「sudo apt install X」这一行里只有 X 是包 —— 动词与 -S/--needed 不许混进来。"""
        for p in idp.parse_families(MDX)["Debian"]["packages"]:
            self.assertNotIn(p["package"],
                             ("sudo", "apt", "install", "update", "-S", "--needed"))

    def test_refresh_lines_are_not_package_tables(self) -> None:
        """`apt update` / `pacman -Syu` / `zypper up` / `dnf check-update` 都只是刷新，不是清单。"""
        for body in ("sudo apt update", "sudo pacman -Syu", "sudo zypper up",
                     "sudo dnf check-update"):
            page = SH('<TabItem label="Debian">', '```sh', body, '```')
            self.assertEqual(idp.parse_families(page), {}, body)

    def test_prose_tab_is_not_a_package_table(self) -> None:
        """真页的 Windows/Linux tab 是 export 加一段散文。第一版把 Most / apps / refresh
        当成了包，产物里冒出 10 个假家族。"""
        page = SH('<TabItem label="Windows">', '```sh', 'export JAVA_HOME=/opt/jbr',
                  'Most apps dont refresh them', '```')
        self.assertEqual(idp.parse_families(page), {})

    def test_block_without_packages_is_not_a_family(self) -> None:
        self.assertEqual(idp.parse_families(SH('```sh', 'sudo apt update', '```')), {})

    def test_page_without_tabs_yields_no_family(self) -> None:
        """没有 TabItem 时不该凭空认出一族：否则上游改版会拿一张空表当依据还是绿的。"""
        self.assertNotIn("Debian", idp.parse_families(SH('```sh', 'sudo apt install curl', '```')))

    def test_real_page_yields_six_pm_families(self) -> None:
        with open(MDX_FIX, encoding="utf-8") as fh:
            fams = idp.parse_families(fh.read())
        self.assertEqual(sorted(fams),
                         ["Alpine", "Arch", "Debian", "Fedora", "OSTree", "openSUSE"])
        self.assertEqual(len(fams["Debian"]["packages"]), 9)

class TestReadmeProbe(unittest.TestCase):
    def test_exact_name_match_carries_the_version(self) -> None:
        tbl = idp.image_packages(README)
        got = idp.probe_readme("libssl-dev", tbl)
        self.assertEqual(got["verdict"], "listed")
        self.assertEqual(got["readme_version"], "3.0.13-0ubuntu3.16")
        self.assertEqual(got["probe"], "libssl-dev")     # 搜的就是包名本身

    def test_absent_name_is_not_listed_not_absent(self) -> None:
        got = idp.probe_readme("libwebkit2gtk-4.1-dev", idp.image_packages(README))
        self.assertEqual(got["verdict"], "not_listed")
        self.assertIsNone(got["readme_version"])

    def test_fuzzy_match_is_not_allowed(self) -> None:
        """readme 只有 profile 时 `file` 必须算没提到：模糊搜会造出假在场。"""
        self.assertEqual(idp.probe_readme("file", idp.image_packages("| profile | 1 |\n"))["verdict"],
                         "not_listed")

    def test_legend_says_readme_is_not_dpkg(self) -> None:
        doc = recomputed()
        self.assertIn("不等于机器上没装", json.dumps(doc["verdict_legend"], ensure_ascii=False))
        self.assertNotIn("absent", json.dumps(doc["verdict_legend"], ensure_ascii=False))

    def test_toolchain_versions_are_read_not_typed(self) -> None:
        self.assertEqual(idp.toolchain(README), {"rust": "1.99.1", "node": "22.23.3"})
        self.assertEqual(idp.toolchain("- Rust 1.99.1\n"), {"rust": "1.99.1", "node": None})


class TestDerivedArtifact(unittest.TestCase):
    """产物必须能被重算，而且与已提交那张逐字段等价。"""

    def test_committed_equals_replay_of_captured_docs(self) -> None:
        committed, fresh = artifact(), recomputed()
        self.assertEqual(idp.check(committed, fresh), [])

    def test_replay_is_deterministic(self) -> None:
        self.assertEqual(recomputed(), recomputed())

    def test_package_count_is_a_measured_literal(self) -> None:
        """9 颗是从上游那一页读出来的，也是写死的：删一行包名这里就红。"""
        doc = artifact()
        self.assertEqual(len(doc["apt_packages"]), 9)
        self.assertEqual(len(doc["runner_probe"]), 9)

    def test_readme_verdict_split_is_the_measured_one(self) -> None:
        doc = artifact()
        listed = [r["package"] for r in doc["runner_probe"] if r["verdict"] == "listed"]
        self.assertEqual(listed, ["curl", "wget", "file", "libssl-dev"])
        self.assertEqual(sorted(doc["missing_per_readme"]),
                         sorted(["build-essential", "librsvg2-dev", "libwebkit2gtk-4.1-dev",
                                 "libayatana-appindicator3-dev", "libxdo-dev"]))

    def test_apt_line_names_every_package(self) -> None:
        doc = artifact()
        names = [p["package"] for p in doc["apt_packages"]]
        self.assertEqual(doc["apt_get_line"], "sudo apt-get install -y " + " ".join(names))

    def test_webkit_requires_the_ci_probe_not_the_readme(self) -> None:
        """最容易犯的错是把"readme 没提到"当成"机器上没装"，然后就此下结论。
        产物里那句 must_verify_in_ci 必须还在，而且要点名 pkg-config。"""
        doc = artifact()
        self.assertIn("pkg-config --modversion webkit2gtk-4.1", doc["rule"]["must_verify_in_ci"])

    def test_source_ref_is_a_commit_not_a_branch(self) -> None:
        doc = artifact()
        self.assertRegex(doc["source"]["ref"], r"^[0-9a-f]{40}$")
        self.assertRegex(idp.IMAGE[1], r"^[0-9a-f]{40}$")

    def test_check_catches_a_dropped_package(self) -> None:
        fresh = recomputed()
        fresh["apt_packages"] = fresh["apt_packages"][:-1]
        self.assertTrue(any("apt_packages" in x for x in idp.check(artifact(), fresh)))

    def test_check_catches_a_probe_verdict_flip(self) -> None:
        fresh = recomputed()
        fresh["runner_probe"][0]["verdict"] = "listed"
        self.assertTrue(any("runner_probe" in x for x in idp.check(artifact(), fresh)))

    def test_positive_control_check_of_identical_docs_is_silent(self) -> None:
        self.assertEqual(idp.check(recomputed(), recomputed()), [])


class TestBuildGuards(unittest.TestCase):
    def test_unreachable_page_is_reported_not_empty_pass(self) -> None:
        real = idp.fetch
        try:
            idp.fetch = lambda *a, **k: None
            doc, problems = idp.build("", False)
        finally:
            idp.fetch = real
        self.assertEqual(doc, {})
        self.assertEqual(len(problems), 2, problems)

    def test_missing_debian_tab_is_a_problem(self) -> None:
        real = idp.fetch
        try:
            idp.fetch = lambda spec, cache="", fill=False: (
                MDX.replace('label="Debian"', 'label="Ubuntu"') if spec[2].endswith(".mdx")
                else README)
            _doc, problems = idp.build("", False)
        finally:
            idp.fetch = real
        self.assertTrue(any("Debian" in p for p in problems), problems)

    def test_webkit_version_change_is_a_problem(self) -> None:
        """上游把 4.1 换成 4.2 时不许默默照旧：文档与车道里的 4.1 必须一起改。"""
        real = idp.fetch
        try:
            idp.fetch = lambda spec, cache="", fill=False: (
                MDX.replace("libwebkit2gtk-4.1-dev", "libwebkit2gtk-4.2-dev")
                if spec[2].endswith(".mdx") else README)
            _doc, problems = idp.build("", False)
        finally:
            idp.fetch = real
        self.assertTrue(any("libwebkit2gtk-4.1-dev" in p for p in problems), problems)


class TestFixturesAreRealCaptures(unittest.TestCase):
    def test_both_fixtures_match_the_live_capture_bytes(self) -> None:
        _fixture_bytes()

    def test_real_readme_lists_four_of_the_nine(self) -> None:
        with open(README_FIX, encoding="utf-8") as fh:
            tbl = idp.image_packages(fh.read())
        for pkg in ("curl", "wget", "file", "libssl-dev"):
            self.assertEqual(idp.probe_readme(pkg, tbl)["verdict"], "listed", pkg)
        for pkg in ("libwebkit2gtk-4.1-dev", "build-essential", "libxdo-dev",
                    "libayatana-appindicator3-dev", "librsvg2-dev"):
            self.assertEqual(idp.probe_readme(pkg, tbl)["verdict"], "not_listed", pkg)


class TestCli(unittest.TestCase):
    def test_selftest_exits_zero(self) -> None:
        r = subprocess.run([sys.executable, CLI, "--selftest"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("BAD 数量 = 0", r.stdout)

    def test_check_offline_against_committed_artifact(self) -> None:
        """--check 的比对层不联网也能跑（缓存命中时）；这里用桩代替缓存。"""
        real = idp.fetch
        buf = io.StringIO()
        try:
            idp.fetch = fake_fetch()
            with contextlib.redirect_stdout(buf):
                rc = idp.main(["--check", "--out", ART])
        finally:
            idp.fetch = real
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("问题 0 条", buf.getvalue())

    def test_bad_table_is_never_landed(self) -> None:
        real = idp.fetch
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "deps.json")
            buf = io.StringIO()
            try:
                idp.fetch = lambda spec, cache="", fill=False: None
                with contextlib.redirect_stdout(buf):
                    rc = idp.main(["--out", out])
            finally:
                idp.fetch = real
            self.assertEqual(rc, 1, buf.getvalue())
            self.assertIn("不落盘", buf.getvalue())
            self.assertFalse(os.path.exists(out))

    def test_stdout_prints_the_derived_apt_line(self) -> None:
        real = idp.fetch
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            try:
                idp.fetch = fake_fetch()
                with contextlib.redirect_stdout(buf):
                    rc = idp.main(["--out", os.path.join(d, "deps.json")])
            finally:
                idp.fetch = real
            self.assertEqual(rc, 0, buf.getvalue())
            self.assertIn("sudo apt-get install -y", buf.getvalue())


class TestSourceMutants(unittest.TestCase):
    """把工具源码改坏，四条探针必须各抓至少一次；正向对照保证不是"永远红"。"""

    MUTANTS = [
        ("安装动词闸门关掉：`sudo apt update` 会被当成包表",
         "            if not any(w in INSTALL_WORDS for w in head):",
         "            if False:"),
        ("续行走到第二行就停：包表只剩同一行的那颗",
         "                if not cont or j + 1 >= len(body):",
         "                if True or j + 1 >= len(body):"),
        ("逐字查改成模糊查：profile 也会算 file 在场",
         "ver = table.get(pkg)",
         "ver = next((v for k, v in table.items() if pkg in k), None)"),
        ("没有 TabItem 也照样产出一族（空表当依据）",
         '    marks = [(m.group(1), m.start()) for m in TAB_RX.finditer(text)]',
         '    marks = [(OS_FAMILY, 0)] if OS_FAMILY not in text else []'),
        ("读不到版本号时不再给 None 而是猜一个",
         '        out[name] = m.group(1) if m else None',
         '        out[name] = m.group(1) if m else "unknown"'),
        ("apt 行只列 readme 里没提到的（少装 4 颗照样绿）",
         '        "apt_get_line": "sudo apt-get install -y " + " ".join(names),',
         '        "apt_get_line": "sudo apt-get install -y " + " ".join('
         '[r["package"] for r in rows if r["verdict"] == "not_listed"]),'),
        ("pm 不是 apt 时不再报（上游换成 dnf 就悄悄装错包）",
         '    if fam and fam["pm"] not in ("apt", "apt-get"):',
         '    if False and fam and fam["pm"] not in ("apt", "apt-get"):'),
        ("4.1 那根哨兵被拆掉：上游换版本不再报",
         '    if "libwebkit2gtk-4.1-dev" not in names:',
         '    if False and "libwebkit2gtk-4.1-dev" not in names:'),
    ]

    def src(self) -> str:
        with open(CLI, encoding="utf-8") as fh:
            return fh.read()

    def load(self, src: str):
        import types
        mod = types.ModuleType("install_deps_mutant")
        mod.__dict__["__file__"] = CLI
        mod.__dict__["__name__"] = "install_deps_mutant"
        exec(compile(src, CLI, "exec"), mod.__dict__)
        mod.fetch = fake_fetch()
        return mod

    def probes(self, mod) -> dict:
        out = {}
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out["a 离线对照"] = mod.selftest() == 0
        except Exception:                     # noqa: BLE001  炸了也算"没在位"
            out["a 离线对照"] = False
        try:
            doc, problems = mod.build("", False)
            out["b 重算与漂移"] = (not problems) and mod.check(artifact(), doc) == []
            out["c 逐字查的在场判语"] = (
                mod.probe_readme("curl", mod.image_packages(README))["verdict"] == "listed")
        except Exception:                     # noqa: BLE001
            out["c 逐字查的在场判语"] = False
        try:
            real = mod.fetch
            mod.fetch = lambda spec, cache="", fill=False: (
                MDX.replace("libwebkit2gtk-4.1-dev", "libwebkit2gtk-4.2-dev")
                if spec[2].endswith(".mdx") else README)
            _d, problems = mod.build("", False)
            mod.fetch = real
            out["d 版本哨兵"] = any("libwebkit2gtk-4.1-dev" in p for p in problems)
        except Exception:                     # noqa: BLE001
            out["d 版本哨兵"] = False
        try:
            real = mod.fetch
            mod.fetch = lambda spec, cache="", fill=False: (
                MDX.replace("sudo apt install", "sudo dnf install")
                if spec[2].endswith(".mdx") else README)
            _d, problems = mod.build("", False)
            mod.fetch = real
            out["e 包管理器哨兵"] = any("包管理器不再是 apt" in p for p in problems)
        except Exception:                     # noqa: BLE001
            out["e 包管理器哨兵"] = False
        return out

    def test_baseline_probes_all_hold(self) -> None:
        self.assertEqual({k: v for k, v in self.probes(idp).items()},
                         {"a 离线对照": True, "b 重算与漂移": True,
                          "c 逐字查的在场判语": True, "d 版本哨兵": True,
                          "e 包管理器哨兵": True})

    def test_every_mutant_is_caught(self) -> None:
        missed = []
        s = self.src()
        for name, old, new in self.MUTANTS:
            self.assertEqual(s.count(old), 1, f"锚点不唯一或已漂：{name}")
            got = {k: v for k, v in self.probes(self.load(s.replace(old, new, 1))).items()}
            if all(got.values()):
                missed.append(f"{name} → {got}")
        self.assertEqual(missed, [], "这些变异没人报：对应判据是装饰\n" + "\n".join(missed))

    def test_mutant_count(self) -> None:
        self.assertEqual(len(self.MUTANTS), 8)


class TestPackageFilesAreValidPython(unittest.TestCase):
    def test_emitted_source_parses(self) -> None:
        with open(CLI, encoding="utf-8") as fh:
            ast.parse(fh.read())


if __name__ == "__main__":
    unittest.main()
