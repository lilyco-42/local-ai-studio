"""核对"文档里写的 file:line 出处"上游还认不认。

为什么需要：这一轮预研的结论大量建立在"上游某个 ref 的某一行写着某件事"上
（默认绑 loopback、CORS 默认 `*`、SRT 由服务端渲染、`--api-key` 默认为空…）。
本轮已经踩过六次同一类坑：`find -name llama-completions`（不存在）、给 v0.10.0 找 CLI
（tag 里没有）、`-of jfk.srt` 会得到 `jfk.srt.srt`、去 grep 日志里的 `Steps:`（其实在 PNG 的
tEXt）、grep `tokens per second`（那是库里的话，CLI 不打）。每一次都是**判据指向一个不存在的东西，
而它照样跑完**。所以把"出处"本身变成可查的：每条声明记 repo/ref/path/行号/该行必须含的子串。

比对的是**那一行的内容**，不是文件在不在 —— 上游把 cors 默认值改掉而文件仍在，才是我们要知道的事。
`--selftest` 不联网（合成文本喂进同一个判据函数），用来证明这套判据不是永远绿：
行号超范围要红、子串对不上要红、指错文件要红。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SPEC = os.path.join(ROOT, "pins", "citations.json")
RAW = "https://raw.githubusercontent.com/{repo}/{ref}/{path}"
# 覆盖度地板。文档里"至少 20 条"那句说的就是这个数，单测把它与两张表绑在一起查，
# 免得代码是 10、文档写 20，而红字看起来像"表被删过"其实从来没被删过。
MIN_CITATIONS = 20


def url_of(c: dict) -> str:
    return RAW.format(repo=c["repo"], ref=c["ref"], path=c["path"])


def check_one(c: dict, text: str | None) -> list[str]:
    """返回问题列表（空 = 这条出处仍然成立）。text=None 表示取不到文件。"""
    # 先要字段再干活：少一个键的声明不该"看起来查过了"，而该当场炸在缺的那个键上。
    for k in ("id", "repo", "ref", "path", "expect", "claim"):
        if k not in c:
            raise KeyError(f"出处声明 {c.get('id', '?')} 缺字段 {k!r}")
    absent = bool(c.get("absent"))
    if not absent and "line" not in c:
        raise KeyError(f"出处声明 {c.get('id', '?')} 缺字段 'line'")
    if text is None:
        return [f"{c['id']}: 取不到 {url_of(c)}"]
    if absent:
        # "这份文件里没有 X"也是一种断言，而且是最容易悄悄失效的那种：
        # 出包车道要靠它知道 runner 少了什么（Tauri 要 webkit2gtk-4.1，镜像里没有 → 得 apt）。
        # 只数整文件命中次数，不看行号；命中了就把行号打出来，方便一眼看出是什么东西挤进来的。
        hits = [i + 1 for i, l in enumerate(text.split("\n")) if c["expect"] in l]
        if hits:
            return [f"{c['id']}: 声明「{c['expect']!r} 不该出现」，但它在第 {hits[:5]} 行出现了"
                    f"（声明：{c['claim']}）"]
        return []
    lines = text.split("\n")
    ln = c["line"]
    if not 1 <= ln <= len(lines):
        return [f"{c['id']}: 行号 {ln} 超出范围，{c['path']} 只有 {len(lines)} 行 —— 上游挪过了"]
    got = lines[ln - 1]
    want = c["expect"]
    if want not in got:
        return [f"{c['id']}: 第 {ln} 行不含 {want!r}，实际是 {got.strip()[:90]!r}"
                f"（声明：{c['claim']}）"]
    return []


def fetch(c: dict, timeout: int = 40) -> str | None:
    req = urllib.request.Request(url_of(c), headers={"User-Agent": "citation-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return r.read().decode("utf-8", errors="replace")
    except Exception:      # noqa: BLE001  404/网络错都要变成一行"取不到"，不能抛死整轮
        return None


def cache_name(c: dict) -> str:
    """缓存文件名 = repo/ref/path 全把斜杠换掉。

    `repo` 本身就带斜杠（`ggml-org/llama.cpp`），只换 path 的话文件名里还剩一层目录，
    写的时候直接 FileNotFoundError —— 第一次 --fill 就撞上了。
    同 path 在不同 repo/ref 下靠这段前缀区分，不会互相盖掉。
    """
    def flat(s: str) -> str:
        return str(s).replace("/", "-").replace("\\", "-")

    return f"{flat(c['repo'])}__{flat(c['ref'])}__" + c["path"].replace("/", "_")


def read_cache(c: dict, d: str) -> str | None:
    p = os.path.join(d, cache_name(c))
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def write_cache(c: dict, d: str, text: str) -> None:
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, cache_name(c)), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def selftest() -> int:
    """离线对照：明知是错的必须被认成错，否则这套判据是装饰。"""
    text = 'line one\n  cors_origins = "*";\nline three'
    good = {"id": "t", "repo": "a/b", "ref": "r", "path": "p", "line": 2,
            'expect': 'cors_origins = "*"', "claim": "CORS 默认 *"}
    cases = [
        ("正确声明要过", check_one(good, text), False),
        ("上游改了默认值必须红", check_one(dict(good, expect='cors_origins = "localhost"'), text), True),
        ("行号超出必须红", check_one(dict(good, line=99), text), True),
        ("行号 0 必须红", check_one(dict(good, line=0), text), True),
        ("取不到文件必须红", check_one(good, None), True),
    ]
    rc = 0
    for name, got, want_problem in cases:
        ok = bool(got) == want_problem
        rc |= 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {name} → {got}")
    try:
        check_one({k: v for k, v in good.items() if k != "line"}, text)
        print("  FAIL 缺 line 键应当抛 KeyError")
        rc = 1
    except KeyError:
        print("  ok   缺 line 键当场抛（不许静默默认成 0 行）")
    return rc


def main(argv) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--spec", default=SPEC)
    ap.add_argument("--json", default="", help="把逐条结果写出去（给 CI 存 artifact）")
    ap.add_argument("--cache", default="",
                    help="在这个目录里放/取上游原文（本机复算用；文档里那句「N 条可离线核对」就靠它）")
    ap.add_argument("--fill", action="store_true", help="配合 --cache：缺的原文先抓下来存进缓存")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    with open(args.spec, encoding="utf-8") as fh:
        spec = json.load(fh)
    if len(spec) < MIN_CITATIONS:
        print(f"BAD：夹具只有 {len(spec)} 条出处声明（地板 {MIN_CITATIONS}），"
              f"这张表应该覆盖三家 server + 四条车道判据")
        return 1
    problems: list[str] = []
    rows = []
    skipped = []
    for c in spec:
        if args.cache:
            text = read_cache(c, args.cache)
            if text is None and args.fill:
                text = fetch(c)
                if text is not None:
                    write_cache(c, args.cache, text)
            if text is None:
                # 缓存里没有 ≠ 出处不成立。这一类只报"没核到"，绝不当成通过，
                # 也不能当成 BAD —— 否则换台机器跑就会红成一片假警报。
                skipped.append(c["id"])
                rows.append({"id": c["id"], "url": url_of(c), "line": c.get("line"),
                             "expect": c["expect"], "ok": None, "problems": []})
                continue
        else:
            text = fetch(c)
        bad = check_one(c, text)
        problems += bad
        rows.append({"id": c["id"], "url": url_of(c), "line": c.get("line"),
                     "expect": c["expect"], "ok": not bad, "problems": bad})
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=1)
    print(f"核对 {len(spec)} 条 file:line 出处，问题 {len(problems)} 条"
          + (f"；真查了 {len(spec) - len(skipped)} 条，缓存里没有原文而跳过 {len(skipped)} 条"
             if args.cache else ""))
    if skipped:
        print("  跳过的 id（不是通过，是没查）：", ", ".join(skipped[:12])
              + ("…" if len(skipped) > 12 else ""))
    for p in problems:
        print("  ✗", p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
