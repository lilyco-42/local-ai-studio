"""runtime 的版本轴：能升到哪一颗、能退回哪一颗、每颗能不能校验。

输入只有上游 release 列表的元数据（tag / published_at / asset 的 name+size+digest），
捕获在 `tests/fixtures/release-window.json`，所以没有 runner 也能离线重算。

这里钉住的三个数是量出来的、也是这张表存在的理由：
`llama` 钉住的 b11804 就是窗口里最新一颗带产物的版本，窗口 25 个 release 的覆盖面没变；
`whisper` 的 `releases/latest` 是 v1.9.5（0 产物），真正能装的 b5454 正是我们钉的那颗；
`whisper` 往回退的边界不是最老的标签，而是 v1.7.6 —— 再往前 13 个 release 上游没发 digest。
`sd` 钉住的 master-975 落后 6 个带产物的版本（最新 master-987），升级仍是一次显式动作。
"""
from __future__ import annotations

import ast
import contextlib
import copy
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import runtime_versions as rv      # noqa: E402

CLI = os.path.join(ROOT, "tools", "runtime_versions.py")
ART = os.path.join(ROOT, "pins", "runtime-versions.json")
CAP = os.path.join(ROOT, "tests", "fixtures", "release-window.json")
MATRIX = os.path.join(ROOT, "pins", "backend-matrix.json")
PLAN = os.path.join(ROOT, "pins", "payload-plan.json")

LPN = "b11804"
LLAMA = f"ggml-org/llama.cpp@{LPN}"
WHISPER = "ggml-org/whisper.cpp@b5454"
SD = "leejet/stable-diffusion.cpp@master-975-95b3090"


def pinned_tag() -> str:
    return next(r["release"] for r in plan()["rows"]
                if r.get("engine") == "llama" and r.get("status") == "ok")


def add_candidate(c: dict, tag: str = "b99999") -> str:
    """在窗口顶部合成一颗比钉值新的 release：判据不该只在"恰好落后"时才有正向对照。"""
    pin = pinned_tag()
    rel = copy.deepcopy(next(r for r in c[LLAMA]["releases"] if r["tag"] == pin))
    rel["tag"] = tag
    rel["published_at"] = "2099-01-01T00:00:00Z"
    rel["assets"] = [dict(a, name=a["name"].replace(pin, tag)) for a in rel["assets"]]
    c[LLAMA]["releases"].insert(0, rel)
    return tag


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def artifact() -> dict:
    return load(ART)


def captures() -> dict:
    return load(CAP)


def matrix() -> dict:
    return load(MATRIX)


def plan() -> dict:
    return load(PLAN)


def rebuild(c: dict | None = None, m: dict | None = None,
            p: dict | None = None) -> tuple[dict, list[str]]:
    return rv.build(c if c is not None else captures(),
                    m if m is not None else matrix(),
                    p if p is not None else plan())


class TestArtifact(unittest.TestCase):
    def test_check_zero(self) -> None:
        _doc, problems = rebuild()
        self.assertEqual(problems, [], problems)

    def test_committed_equals_recompute(self) -> None:
        fresh, _p = rebuild()
        self.assertEqual(rv.drift(artifact(), fresh), [])

    def test_stats_literals(self) -> None:
        self.assertEqual(artifact()["stats"], {
            "引擎": 3, "窗口内 release": 75, "带产物的版本": 66, "只有源码的标签": 9,
            "runtime 资产行": 1021, "可校验的候选版本": 66,
            "不可校验的版本（上游没发 digest）": 0, "附件（非产物）": 7,
            "认不出的词": 0, "latest 是空标签的引擎": 1,
            "窗口之外还看了的 release": 170,
            "窗口之外全没有 digest 的 release": 13})

    def test_capture_holds_metadata_only(self) -> None:
        """捕获里只能有元数据：一旦有人往里塞下载地址或内容，这张表就不再是"只看了一眼"。"""
        for entry in captures().values():
            for r in entry["releases"]:
                self.assertLessEqual(set(r), {"tag", "published_at", "name", "assets"})
                for a in r["assets"]:
                    self.assertEqual(set(a), {"name", "size", "sha256"})

    def test_capture_fixture_bytes_are_the_captured_ones(self) -> None:
        """夹具必须逐字节等于当初捕获的那份，否则这张表的输入就不是那次测量。"""
        meta = load(CAP + ".meta.json")
        with open(CAP, "rb") as fh:
            raw = fh.read()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), meta["sha256"])
        self.assertEqual(len(raw), meta["bytes"])
        self.assertNotIn(b"\r\n", raw)

    def test_window_contains_the_pin_with_room_on_both_sides(self) -> None:
        """窗口必须翻到钉住那一颗，而且它下面还有更老的；否则"能退回哪"是空的。"""
        want = {"llama": ("b11804", "b11777"), "sd": ("master-975-95b3090", "master-950-f2f168f"),
                "whisper": ("b5454", "v1.7.2")}
        for e, body in artifact()["engines"].items():
            tags = [r["tag"] for r in body["releases"]]
            self.assertEqual((body["pinned"], body["oldest_captured_tag"]), want[e])
            self.assertEqual(len(tags), 25, e)
            self.assertLess(tags.index(body["pinned"]), len(tags) - 1, e)
            self.assertEqual(body["per_page"], 100)
            self.assertEqual(body["pages_fetched"], 1)

    def test_whisper_latest_pointer_is_the_trap(self) -> None:
        """`releases/latest` 指向 0 产物的源标签：外壳按 latest 取 runtime 会"成功但装不上"。"""
        w = artifact()["engines"]["whisper"]
        self.assertEqual(w["latest_tag"], "v1.9.5")
        self.assertIs(w["latest_has_runtime"], False)
        self.assertEqual(w["latest_runtime_tag"], "b5454")
        self.assertEqual(w["latest_runtime_tag"], w["pinned"])
        self.assertEqual(w["newer_runtime_releases"], 0)
        self.assertEqual(w["source_only_tags"],
                         ["v1.9.5", "v1.9.4", "v1.9.3", "v1.7.5", "b2365", "v1.7.4",
                          "v1.7.3", "v1.7.3-pre", "v1.7.2"])

    def test_llama_pin_is_latest_in_window(self) -> None:
        e = artifact()["engines"]["llama"]
        self.assertEqual(e["pinned"], LPN)
        self.assertEqual(pinned_tag(), LPN)
        self.assertEqual(e["latest_runtime_tag"], LPN)
        self.assertEqual(e["newer_runtime_releases"], 0)
        self.assertEqual(len(e["verifiable_tags"]), 25)
        self.assertIn(e["pinned"], e["verifiable_tags"])
        self.assertEqual(e["older_without_digest_tags"], [])

    def test_sd_drift_is_reported_not_followed(self) -> None:
        e = artifact()["engines"]["sd"]
        self.assertEqual(e["pinned"], "master-975-95b3090")
        self.assertEqual(e["newer_runtime_releases"], 6)
        self.assertEqual(e["latest_runtime_tag"], "master-987-a77d362")

    def test_capability_delta_records_coverage_is_unchanged(self) -> None:
        d = artifact()["capability_delta"]
        self.assertEqual([x["engine"] for x in d], ["sd"])
        self.assertEqual((d[0]["from"], d[0]["to"]), ("master-975-95b3090", "master-987-a77d362"))
        self.assertEqual((d[0]["os_lost"], d[0]["os_added"]), ([], []))
        self.assertEqual(d[0]["runtime_releases_in_window"], 25)
        self.assertIn("只报不跟", d[0]["because"])

    def test_every_runtime_row_carries_a_sha256(self) -> None:
        rows = [x for e in artifact()["engines"].values()
                for r in e["releases"] for x in r["runtime"]]
        self.assertEqual(len(rows), 1021)
        bad = [x["asset"] for x in rows if not re.fullmatch(r"[0-9a-f]{64}", x["sha256"])]
        self.assertEqual(bad, [])

    def test_pinned_window_row_equals_matrix_row_for_row(self) -> None:
        """同一份 release，两张表必须点出同一批 runtime 包（47 颗）。"""
        m = matrix()
        total = 0
        for e, body in artifact()["engines"].items():
            pin = next(r for r in body["releases"] if r["tag"] == body["pinned"])
            got = sorted(x["asset"] for x in pin["runtime"])
            want = sorted(r["asset"] for r in m["engines"][e]["rows"])
            self.assertEqual(got, want, e)
            total += len(got)
        self.assertEqual(total, 47)
        self.assertEqual(total, m["stats"]["runtime 行"])

    def test_jar_attachments_are_counted_but_never_classified(self) -> None:
        """whispercpp.jar.zip 是上游附属文件：不能进 runtime 表，也不能冒充"认不出的后端词"。"""
        attachments = [(r["tag"], name) for e in artifact()["engines"].values()
                       for r in e["releases"] for name in r["attachments"]]
        self.assertEqual(len(attachments), 7)
        self.assertTrue(all(n == "whispercpp.jar.zip" for _t, n in attachments))
        self.assertEqual([t for t, _n in attachments],
                         ["v1.8.6", "v1.8.5", "v1.8.4", "v1.8.3", "v1.8.2", "v1.8.1", "v1.7.6"])
        names = {x["asset"] for e in artifact()["engines"].values()
                 for r in e["releases"] for x in r["runtime"]}
        self.assertNotIn("whispercpp.jar.zip", names)

    def test_whisper_rollback_depth_ends_at_the_digest_boundary(self) -> None:
        """回退不是"翻到最老"：v1.7.6 之前 13 个 release 上游没发 digest，不能当可校验候选。"""
        w = artifact()["engines"]["whisper"]
        self.assertEqual(w["oldest_verifiable_tag"], "v1.7.6")
        self.assertEqual(len(w["verifiable_tags"]), 16)
        self.assertEqual(w["older_without_digest_tags"],
                         ["v1.6.0", "v1.5.4", "v1.5.3", "v1.5.2", "v1.5.1", "v1.5.0",
                          "v1.4.0", "v1.3.0", "v1.2.1", "v1.2.0", "v1.1.1", "v1.1.0",
                          "v1.0.4"])
        self.assertIsNone(w["newest_older_release_with_digest"])
        self.assertEqual(artifact()["engines"]["llama"]["older_releases_seen"], 75)
        self.assertEqual(artifact()["engines"]["whisper"]["older_releases_seen"], 20)

    def test_source_never_retypes_an_upstream_repo(self) -> None:
        """仓库名只有一个来源（backend-matrix），代码里的字符串常量不许再写一遍。"""
        with open(CLI, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
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
                and ("ggml-org/" in n.value or "leejet/" in n.value)]
        self.assertEqual(hits, [], f"源码里出现了写死的上游仓库名：{hits}")

    def test_cli_check_exits_zero(self) -> None:
        r = subprocess.run([sys.executable, CLI, "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("问题 0 条", r.stdout)


class TestSelftest(unittest.TestCase):
    def test_selftest_exit_zero(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(rv.selftest(), 0)
        self.assertIn("BAD 数量 = 0", buf.getvalue())

    def test_window_policy_in_source_matches_artifact(self) -> None:
        """窗口政策是被写进产物的一列；源码改了常数而没重新抓取 + 落盘就必须红。"""
        self.assertEqual(artifact()["per_page_policy"],
                         {"per_page": rv.PER_PAGE, "max_pages": rv.MAX_PAGES})
        self.assertEqual(rv.KEEP_MIN, 25)


class TestSourceMutants(unittest.TestCase):
    @staticmethod
    def src() -> str:
        with open(CLI, encoding="utf-8") as fh:
            return fh.read()

    MUTANTS = [
        ("钉值不在窗口不报",
         "        if mine is None:", "        if False and mine is None:"),
        ("钉值缺 digest 不报",
         '        if mine and not mine["verifiable"]:',
         '        if False and mine and not mine["verifiable"]:'),
        ("认不出的后端词静默归类",
         '            if r["unknown_tokens"]:', '            if False and r["unknown_tokens"]:'),
        ("0 产物的源标签也当候选",
         '        cand = [r for r in rels if r["has_runtime"]]', "        cand = rels"),
        ("升级丢掉真装档不报",
         "            if hit_shipped:", "            if False and hit_shipped:"),
        ("latest 是空标签不计数",
         '        if rels and rels[0] is not None and not rels[0]["has_runtime"]:',
         '        if False and rels and rels[0] is not None and not rels[0]["has_runtime"]:'),
        ("附件被当成产物去分类",
         '        if ATTACHMENT.search(a["name"]):', "        if False and ATTACHMENT.search(a[\"name\"]):"),
        ("verifiable 不看 digest",
         '            "verifiable": bool(runtime) and not missing}',
         '            "verifiable": bool(runtime)}'),
        ("漂移不看只有源码的标签集合",
         '        if old.get("source_only_tags") != body["source_only_tags"]:',
         '        if False and old.get("source_only_tags") != body["source_only_tags"]:'),
        ("漂移不看同一颗资产的哈希",
         '                if x["sha256"] != y["sha256"] or x["size"] != y["size"]:',
         '                if False and x["sha256"] != y["sha256"] or x["size"] != y["size"]:'),
        ("钉值与矩阵不对表不报",
         "            if got != want:", "            if False and got != want:"),
        ("真装 OS 丢了不报",
         "            if lost:", "            if False and lost:"),
        ("可校验候选不足两个不报",
         "        if len(good) < 2:", "        if False and len(good) < 2:"),
        ("漂移不看 latest 有没有产物",
         '        for f in ("pinned", "latest_tag", "latest_has_runtime", "latest_runtime_tag",',
         '        for f in ("pinned", "latest_tag", "latest_runtime_tag",'),
        ("漂移不看 release 序列",
         '        if [r["tag"] for r in old["releases"]] != [r["tag"] for r in body["releases"]]:',
         '        if False and [r["tag"] for r in old["releases"]] != [r["tag"] for r in body["releases"]]:'),
        ("live 口径漏掉钉值重打包",
         "            if oldmap != newmap:", "            if False and oldmap != newmap:"),
        ("live 口径漏掉钉值消失",
         "        if np_ is None:", "        if False and np_ is None:"),
        ("live 口径被写成离线口径（把新版当红）",
         "    return pin_drift(committed, fresh) if live else drift(committed, fresh)",
         "    return drift(committed, fresh)"),
    ]

    def load(self, text: str):
        import types
        mod = types.ModuleType("runtime_versions_mutant")
        mod.__dict__["__file__"] = CLI
        mod.__dict__["__name__"] = "runtime_versions_mutant"
        exec(compile(text, CLI, "exec"), mod.__dict__)
        return mod

    def probes(self, mod) -> dict:
        out = {}
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out["a 离线对照"] = mod.selftest() == 0
        except Exception:                                  # noqa: BLE001
            out["a 离线对照"] = False
        try:
            caps, m, p = captures(), matrix(), plan()
            committed = artifact()
            fresh, problems = mod.build(caps, m, p)
            out["b 重算与漂移"] = problems == [] and mod.drift(committed, fresh) == []
            w = fresh["engines"]["whisper"]
            out["c whisper 的 latest 陷阱"] = (
                w["latest_tag"] == "v1.9.5" and w["latest_has_runtime"] is False
                and w["latest_runtime_tag"] == w["pinned"] == "b5454"
                and w["pinned"] in w["verifiable_tags"])
            out["d 钉值逐颗可校验"] = all(
                e["pinned"] in e["verifiable_tags"] for e in fresh["engines"].values())
            att = [n for e in fresh["engines"].values()
                   for r in e["releases"] for n in r["attachments"]]
            out["e 附件不进 runtime 表"] = (
                len(att) == 7 and set(att) == {"whispercpp.jar.zip"}
                and fresh["stats"]["认不出的词"] == 0)
            out["f 回退深度被 digest 边界限制"] = (
                w["oldest_verifiable_tag"] == "v1.7.6"
                and len(w["older_without_digest_tags"]) == 13
                and w["newest_older_release_with_digest"] is None)
            out["g 升级只报不跟"] = (
                len(fresh["capability_delta"]) == 1
                and fresh["capability_delta"][0]["engine"] == "sd"
                and fresh["engines"]["llama"]["newer_runtime_releases"] == 0)
            c1 = copy.deepcopy(committed)
            c1["engines"]["whisper"]["latest_has_runtime"] = True
            out["h 改 latest 指针被叫"] = any(
                "latest_has_runtime 变了" in x for x in mod.drift(committed, c1))
            c2 = copy.deepcopy(committed)
            c2["engines"]["llama"]["releases"][0]["runtime"][0]["sha256"] = "f" * 64
            out["i 换附件被叫"] = any("哈希或体积变了" in x for x in mod.drift(committed, c2))
            c3 = copy.deepcopy(committed)
            c3["engines"]["whisper"]["source_only_tags"].append("v9.9.9")
            out["j 源标签集合变了被叫"] = any(
                "只有源码的标签集合变了" in x for x in mod.drift(committed, c3))
            c4 = copy.deepcopy(committed)
            c4["engines"]["sd"]["releases"] = c4["engines"]["sd"]["releases"][:-1]
            out["k 序列变了被叫"] = any(
                "release 序列变了" in x for x in mod.drift(committed, c4))

            c5 = copy.deepcopy(caps)
            pin = pinned_tag()
            for r in c5[LLAMA]["releases"]:
                if r["tag"] == pin:
                    for a in r["assets"]:
                        if a["sha256"] and not rv.ATTACHMENT.search(a["name"]) \
                                and mod.bd.classify(a["name"], pin)["kind"] == "runtime":
                            a["sha256"] = ""
                            break
                    break
            out["l 钉值缺 digest 被报"] = any(
                "没有上游 digest" in x for x in mod.build(c5, m, p)[1])
            c6 = copy.deepcopy(caps)
            c6[LLAMA]["releases"] = [r for r in c6[LLAMA]["releases"] if r["tag"] != pin]
            out["m 钉值出窗口被报"] = any(
                "不在捕获的" in x for x in mod.build(c6, m, p)[1])
            c7 = copy.deepcopy(caps)
            tag7 = add_candidate(c7)
            newest = next(r for r in c7[LLAMA]["releases"] if r["tag"] == tag7)
            newest["assets"] = [a for a in newest["assets"]
                                if mod.bd.classify(a["name"], tag7).get("os") != "win-x64"]
            out["n 新版本丢真装档被报"] = any(
                "升级路" in x for x in mod.build(c7, m, p)[1])
            c8 = copy.deepcopy(caps)
            c8[LLAMA]["releases"][0]["assets"].append(
                {"name": "llama-b99999-bin-ubuntu-hip-x64.tar.gz", "size": 1, "sha256": "0" * 64})
            out["o 认不出的词被报"] = any(
                "认不出的名字" in x for x in mod.build(c8, m, p)[1])
            c9 = copy.deepcopy(caps)
            tag9 = add_candidate(c9)
            newest_l = next(r for r in c9[LLAMA]["releases"] if r["tag"] == tag9)
            for a in newest_l["assets"]:
                if mod.bd.classify(a["name"], tag9)["kind"] == "runtime":
                    a["sha256"] = ""
            fresh9, _p9 = mod.build(c9, m, p)
            out["q 缺 digest 的版本不进可校验候选"] = (
                tag9 in fresh9["engines"]["llama"]["unverifiable_tags"]
                and tag9 not in fresh9["engines"]["llama"]["verifiable_tags"]
                and pin in fresh9["engines"]["llama"]["verifiable_tags"])
            c10 = copy.deepcopy(caps)
            for r in c10[LLAMA]["releases"]:
                if r["tag"] == pin:
                    r["assets"] = [a for a in r["assets"]
                                   if a["name"] != f"llama-{pin}-bin-android-arm64.tar.gz"]
                    break
            out["r 钉值产物与矩阵对不上被报"] = any(
                "矩阵数出" in x for x in mod.build(c10, m, p)[1])
            c11 = copy.deepcopy(caps)
            m11 = copy.deepcopy(m)
            # 整档消失（不是某颗包消失）：win-x64 的所有 runtime 产物都从两张表里拿掉，
            # 这样产物名集合仍然一致，只有"我们真装的 OS 没了"这一条会红。
            pinned_c = next(r for r in c11[LLAMA]["releases"] if r["tag"] == pin)
            gone_assets = {a["name"] for a in pinned_c["assets"]
                           if mod.bd.classify(a["name"], pin)["os"] == "win-x64"}
            pinned_c["assets"] = [a for a in pinned_c["assets"]
                                  if a["name"] not in gone_assets]
            m11["engines"]["llama"]["rows"] = [
                r for r in m11["engines"]["llama"]["rows"] if r["asset"] not in gone_assets]
            out["s 真装 OS 丢失被报"] = any(
                "在这一版的产物名里找不到" in x for x in mod.build(c11, m11, p)[1])
            c12 = copy.deepcopy(caps)
            for r in c12[LLAMA]["releases"]:
                if r["tag"] != pin:
                    for a in r["assets"]:
                        a["sha256"] = ""
            out["t 可校验候选不足两个被报"] = any(
                "可校验的版本只有 1 个" in x for x in mod.build(c12, m, p)[1])
            out["u latest 空标签计数"] = fresh["stats"]["latest 是空标签的引擎"] == 1
            c13 = copy.deepcopy(committed)
            for e in c13["engines"].values():
                e["latest_tag"] = "更新的标签"
                e["newer_runtime_releases"] = 999
            advisory = []
            live_problems = mod.build(c7, m, p, live=True, advisory=advisory)[1]
            out["v live 口径不把新版当红"] = (
                mod.pin_drift(committed, c13) == []
                and not any("升级路" in x for x in live_problems)
                and any("升级路" in x for x in advisory))
            out["v2 两种口径不是同一种"] = (
                mod.compare(committed, c13, live=True) == []
                and mod.compare(committed, c13) != [])
            c14 = copy.deepcopy(committed)
            pin14 = next(r for r in c14["engines"]["llama"]["releases"]
                         if r["tag"] == c14["engines"]["llama"]["pinned"])
            pin14["runtime"][0]["sha256"] = "e" * 64
            out["w live 口径抓钉值重打包"] = any(
                "钉住那颗的产物变了" in x for x in mod.pin_drift(committed, c14))
            c15 = copy.deepcopy(committed)
            c15["engines"]["whisper"]["releases"] = [
                r for r in c15["engines"]["whisper"]["releases"]
                if r["tag"] != c15["engines"]["whisper"]["pinned"]]
            out["x live 口径抓钉值消失"] = any(
                "没有钉住的" in x for x in mod.pin_drift(committed, c15))
        except Exception as exc:                             # noqa: BLE001
            for k in ("b 重算与漂移", "c whisper 的 latest 陷阱", "d 钉值逐颗可校验",
                      "e 附件不进 runtime 表", "f 回退深度被 digest 边界限制",
                      "g 升级只报不跟", "h 改 latest 指针被叫", "i 换附件被叫",
                      "j 源标签集合变了被叫", "k 序列变了被叫", "l 钉值缺 digest 被报",
                      "m 钉值出窗口被报", "n 新版本丢真装档被报", "o 认不出的词被报",
                      "q 缺 digest 的版本不进可校验候选", "r 钉值产物与矩阵对不上被报",
                      "s 真装 OS 丢失被报", "t 可校验候选不足两个被报",
                      "u latest 空标签计数", "v live 口径不把新版当红",
                      "v2 两种口径不是同一种", "w live 口径抓钉值重打包",
                      "x live 口径抓钉值消失"):
                out.setdefault(k, False)
            out["_b 异常"] = str(exc)[:80]
        return out

    EXPECTED = {
        "a 离线对照": True, "b 重算与漂移": True, "c whisper 的 latest 陷阱": True,
        "d 钉值逐颗可校验": True, "e 附件不进 runtime 表": True,
        "f 回退深度被 digest 边界限制": True, "g 升级只报不跟": True,
        "h 改 latest 指针被叫": True, "i 换附件被叫": True,
        "j 源标签集合变了被叫": True, "k 序列变了被叫": True,
        "l 钉值缺 digest 被报": True, "m 钉值出窗口被报": True,
        "n 新版本丢真装档被报": True, "o 认不出的词被报": True,
        "q 缺 digest 的版本不进可校验候选": True,
        "r 钉值产物与矩阵对不上被报": True, "s 真装 OS 丢失被报": True,
        "t 可校验候选不足两个被报": True, "u latest 空标签计数": True,
        "v live 口径不把新版当红": True, "v2 两种口径不是同一种": True,
        "w live 口径抓钉值重打包": True, "x live 口径抓钉值消失": True,
    }

    def test_baseline_probes_all_hold(self) -> None:
        self.assertEqual({k: v for k, v in self.probes(rv).items()
                          if not k.startswith("_")}, self.EXPECTED)

    def test_every_mutant_is_caught(self) -> None:
        missed = []
        for name, old, new in self.MUTANTS:
            self.assertEqual(self.src().count(old), 1, f"锚点不唯一或已漂：{name}")
            got = {k: v for k, v in self.probes(
                self.load(self.src().replace(old, new, 1))).items()
                if not k.startswith("_")}
            if all(got.get(k, False) for k in self.EXPECTED):
                missed.append(f"{name} → {got}")
        self.assertEqual(missed, [], "这些变异没人报：对应判据是装饰\n" + "\n".join(missed))

    def test_mutant_count(self) -> None:
        self.assertEqual(len(self.MUTANTS), 18)


if __name__ == "__main__":
    unittest.main()
