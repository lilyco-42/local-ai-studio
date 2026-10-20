#!/usr/bin/env python3
"""车道里真敲的那一行，必须与 `pins/launch-plan.json` 说的一致。

`launch_plan` 那张表是按上游源码核对出来的（车道的第四步），但**车道自己手写的命令行**从来没被
拿来跟它对过。这一步补上：从 `engine-live.yml` 里把每一项能力"真起进程"的那一段抠出来，
用表里的四条规矩去量它 ——

  ① 它找的那个可执行文件，必须是表里那一颗（`whisper-cli` 与 `whisper-server` 是两个文件，
     上一轮 pack_engine 就栽在把这两者当"装了"）；
  ② 表里 `bind` 那个旗标必须出现，而且给的值必须逐字是 127.0.0.1 ——
     `$ADDR`、`0.0.0.0`、"没写所以靠上游默认" 三种都不算（验收第 10 条讲的是**我们传了什么**）；
  ③ 表里声明"上游没有"的旗标（whisper/sd 的 `--api-key`）不许出现在车道里 ——
     真出现了说明上游加了，或说明有人以为引擎有门禁；
  ④ 表里 `omit` 的旗标（OCR 的 `--size`）与 `subcommand`（`oar ocr`）按同一句话各查一遍。

读法刻意保守：heredoc 里的 Python **不进判据**（那是 HTTP 请求体，不是命令行），
`#` 注释行也不进 —— 上一轮就有"注释里写了被检查的字符串，于是判据钉着注释不放"的事故。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
WF = os.path.join(ROOT, ".github", "workflows", "engine-live.yml")
PLAN = os.path.join(ROOT, "pins", "launch-plan.json")
OS_RUN = "linux-x64"          # 车道跑在 ubuntu runner 上，取这一列的二进制名


def shell_lines(body: str) -> str:
    """只留 shell 那一层：heredoc 的正文与 `#` 注释行都去掉。"""
    out, heredoc, term = [], False, ""
    for line in body.split("\n"):
        s = line.strip()
        if heredoc:
            if s == term:
                heredoc = False
            continue
        m = re.search(r"<<-?\s*'?([A-Za-z_]+)'?", s)
        if m:
            term = m.group(1)
            out.append(re.sub(r"<<-?\s*'[A-Za-z_]+'.*$", " ", line))
            heredoc = True
            continue
        if s.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def step_bodies(wf_text: str) -> list[tuple[str, str]]:
    """每条 workflow 里的 (job#步骤名, run 正文)。

    用 pyyaml 读，不自写正则切块：上一版按 `- name:` 切，最后一步会一路吞到**下一个 job**，
    别的步骤的旗标就算到了这一段头上（读者读错，比判据写错更难查）。
    """
    import yaml
    d = yaml.safe_load(wf_text)
    out = []
    for jn, job in (d.get("jobs") or {}).items():
        for st in (job or {}).get("steps") or []:
            body = st.get("run")
            if body:
                out.append((f"{jn}#{st.get('name') or '(无名步骤)'}", body))
    return out


def launchers(doc: dict, wf_text: str) -> list[tuple[str, str, str]]:
    """从 workflow 文本里找出每一项能力"真起进程"的那些步骤。

    认的是**这段步骤里出现了表里那颗二进制的定位方式**（`-name <bin>` 或 `release/<bin> `），
    而不是步骤名 —— 步骤名给人看的，会漂；文件名是执行的一部分。
    """
    found = []
    for name, body in step_bodies(wf_text):
        text = shell_lines(body)
        for cap, spec in doc["capabilities"].items():
            b = spec["binaries"].get(OS_RUN)
            if not b:
                continue
            if f"-name {b}" in text or f"release/{b} " in text:
                found.append((cap, name, text))
    return found


def launches(b: str, text: str) -> bool:
    """这一步里有没有一行**真的执行**那颗二进制。

    只认"一行的开头"：`chmod +x "$SRV"`、`ldd "$SRV"` 也算提到过它，但都不是起进程。
    """
    for line in text.split("\n"):
        s = line.strip()
        if s.startswith('"$SRV"') or s.startswith('"$BIN"'):
            return True
        first = s.split(" ", 1)[0]
        if first.endswith("/" + b) or first.endswith("\\" + b):
            return True
    return False


def flag_values(text: str, flag: str) -> list[str]:
    """`--flag 值` / `--flag=值` 两种写法都收，去掉一层引号。"""
    return [v.strip("\"'") for v in
            re.findall(rf"(?<![\w-]){re.escape(flag)}(?:[\s=]+)([^\s;&]+)", text)]


def assigned(text: str, var: str) -> str | None:
    """这一段里这个变量有没有被赋成非空值；`$(...)` 只要不是空串就算。"""
    m = re.search(rf"(?m)^\s*(?:export\s+)?{re.escape(var)}=(.*)$", text)
    if not m:
        return None
    rhs = m.group(1).strip().strip("\"'")
    return rhs or None


def check_cap(cap: str, spec: dict, blocks: list[tuple[str, str]]) -> list[str]:
    bad = []
    b = spec["binaries"].get(OS_RUN)
    if not blocks:
        return [f"{cap}: 表里有这一项能力，但车道里没有任何一步按 `{b}` 起进程 —— "
                "这张表没被任何东西执行过"]
    for name, text in blocks:
        if not launches(b, text):
            bad.append(f"{cap}「{name}」: 只把 {b} 找出来了（chmod/ldd 也算提到），"
                       "没有任何一行执行它")
        if spec["bind"]:
            flag = spec["bind"][0]
            vals = re.findall(rf"{re.escape(flag)}[\s=]+([^\s;&]+)", text)
            if not vals:
                bad.append(f"{cap}「{name}」: 表里说绑地址要我们传（{flag}），车道这一段却一次没传 —— "
                           '"靠上游默认"不等于验收第 10 条成立')
            for v in vals:
                got = v.strip("\"'")
                if got != "127.0.0.1":
                    bad.append(f"{cap}「{name}」: {flag} 给的是 {got!r}，不是 127.0.0.1 —— "
                               "变量与 0.0.0.0 都算不合格")
        # 验收第 10 条第①句：llama 起来时必须带钥匙。表里写了不算，车道真敲出来才算。
        auth_argv = (spec.get("engine_auth") or {}).get("argv") or []
        for i in range(0, len(auth_argv) - 1, 2):
            flag, tpl = auth_argv[i], auth_argv[i + 1]
            vals = flag_values(text, flag)
            if not vals:
                bad.append(f"{cap}「{name}」: 表里说上游有 {flag}，这一段却没带 —— "
                           "引擎的门禁没被打开（/health 之外的路由会直接对本地进程开放）")
                continue
            for v in vals:
                if v == tpl:
                    bad.append(f"{cap}「{name}」: 把占位符 {tpl} 原样交给了引擎 —— "
                               "车道里必须换成一个真值，否则等于把钥匙写成字面量")
                elif v.startswith("$"):
                    var = v[2:].split("}", 1)[0] if v.startswith("${") else v[1:]
                    if not assigned(text, var):
                        bad.append(f"{cap}「{name}」: {flag} 给的是变量 {var}，"
                                   "可这一段里没有把它赋成非空值 —— 起进程时会传空钥匙")
        cors_argv = (spec.get("cors") or {}).get("argv") or []
        for i in range(0, len(cors_argv) - 1, 2):
            flag, want = cors_argv[i], cors_argv[i + 1]
            vals = flag_values(text, flag)
            if not vals:
                bad.append(f"{cap}「{name}」: 表里写了 {flag} {want}，车道没带 —— "
                           "上游默认 * 会让任意网页读本地端口")
                continue
            for v in vals:
                if v != want:
                    bad.append(f"{cap}「{name}」: {flag} 给的是 {v!r}，表里要求逐字 {want!r}")
        if auth_argv:
            # 旗标传对了 ≠ 门禁生效：必须有"不带钥匙被拒"与"非本机来源拿不到 ACAO"两个负向对照。
            if not re.search(r'test\s+"?\$[A-Za-z_]\w*"?\s*=\s*"?401"?', text):
                bad.append(f"{cap}「{name}」: 带钥匙之后没有 401 负向对照 —— "
                           "旗标传对了不等于门禁真的生效")
            origins = set(re.findall(r"Origin:\s*([^\s'\"]+)", text))
            if len(origins) < 2:
                bad.append(f"{cap}「{name}」: CORS 只测了 {sorted(origins)} 一个来源 —— "
                           "没有非本机来源的负向对照，收窄与否无法区分")
        for f in spec.get("flags_absent") or []:
            if re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", text):
                bad.append(f"{cap}「{name}」: 出现了 {f} —— 表里这条是按上游全文**查不到**才写的"
                           "（上游加了就重查那张表，别在车道里先撞上）")
        for f in spec.get("omit") or []:
            if f in text:
                bad.append(f"{cap}「{name}」: 带了 {f} —— 表里写了它与钉住权重的三颗互斥，"
                           "clap 会直接拒")
        sub = spec.get("subcommand")
        if sub and f"{b} {sub}" not in text:
            bad.append(f"{cap}「{name}」: 没带子命令 `{b} {sub}` —— 上游那个 CLI 不给子命令是报错的")
    return bad


def check(doc: dict, wf_text: str) -> tuple[list[tuple], list[str]]:
    blocks = launchers(doc, wf_text)
    by_cap: dict[str, list[tuple[str, str]]] = {}
    for cap, name, text in blocks:
        by_cap.setdefault(cap, []).append((name, text))
    bad = []
    for cap, spec in doc["capabilities"].items():
        bad += check_cap(cap, spec, by_cap.get(cap, []))
    return blocks, bad


def selftest() -> int:
    """每条判据都要"明知错的必须认成错"，并且各有一条能过的正向对照。"""
    bad = []
    with open(PLAN, encoding="utf-8") as fh:
        doc = json.load(fh)
    whisper = doc["capabilities"]["字幕"]

    good = ('SRV=$(find w -type f -name whisper-server | head -1)\n'
            '"$SRV" -m model.bin --host 127.0.0.1 --port 9009 > srv.log 2>&1 &\n')
    if check_cap("字幕", whisper, [("起常驻 whisper-server", good)]):
        bad.append("正向对照不过：合格的命令被判红了")
    for label, text in [
        ("绑到 0.0.0.0", good.replace("--host 127.0.0.1", "--host 0.0.0.0")),
        ("绑地址用变量", good.replace("--host 127.0.0.1", "--host $ADDR")),
        ("根本没传绑地址", good.replace(" --host 127.0.0.1", "")),
        ("带了 --api-key", good.replace("--port 9009", "--port 9009 --api-key t0p")),
    ]:
        if not check_cap("字幕", whisper, [("一步", text)]):
            bad.append(f"负向对照「{label}」没被认成错 —— 这条判据是装饰")

    # 注释里出现 --api-key 不许算（判据钉着注释 = 假红），heredoc 里的 python 也不算。
    # 注意喂给 check_cap 的必须是 shell_lines() 过滤**之后**的文本 —— 它就是干这件事的。
    commented = shell_lines(good + "# 上游没有 --api-key，所以只能绑 127.0.0.1\n")
    if check_cap("字幕", whisper, [("带注释", commented)]):
        bad.append("注释里的旗标被当成车道真的敲了它")

    # 验收第 10 条第①句的判据：llama 必须真带钥匙与收窄的 CORS。
    llama = doc["capabilities"]["对话"]
    llama_ok = ('TOK=$(python3 -c "import secrets;print(secrets.token_hex(16))")\n'
                'SRV=$(find w -type f -name llama-server | head -1)\n'
                '"$SRV" -m model.gguf --host 127.0.0.1 --port 9009 '
                '--cors-origins localhost --api-key "$TOK" > srv.log 2>&1 &\n'
                'noauth=$(curl -sS -o /dev/null -w "%{http_code}" http://127.0.0.1:9009/props)\n'
                'test "$noauth" = "401" || exit 1\n'
                "curl -sS -D - -o /dev/null -H 'Origin: http://localhost:1420' "
                "http://127.0.0.1:9009/health\n"
                "curl -sS -D - -o /dev/null -H 'Origin: https://evil.example' "
                "http://127.0.0.1:9009/health\n")
    if check_cap("对话", llama, [("起常驻 llama-server", llama_ok)]):
        bad.append(f"llama 正向对照不过：{check_cap('对话', llama, [('x', llama_ok)])}")
    for label, text in [
        ("没带 --api-key", llama_ok.replace(' --api-key "$TOK"', "")),
        ("钥匙是占位符", llama_ok.replace('"$TOK"', "{token}")),
        ("钥匙变量没赋值", llama_ok.replace(
            'TOK=$(python3 -c "import secrets;print(secrets.token_hex(16))")\n', "")),
        ("没带 --cors-origins", llama_ok.replace("--cors-origins localhost ", "")),
        ("CORS 还是 *", llama_ok.replace("--cors-origins localhost", "--cors-origins *")),
        ("CORS 给了变量", llama_ok.replace("--cors-origins localhost", "--cors-origins $ORIGIN")),
        ("没有 401 对照", llama_ok.replace('test "$noauth" = "401" || exit 1\n', "")),
        ("CORS 只有一个来源", llama_ok.replace("https://evil.example", "http://localhost:1420")),
    ]:
        if not check_cap("对话", llama, [("一步", text)]):
            bad.append(f"llama 负向对照「{label}」没被认成错 —— 验收第 10 条第①句是装饰")
    inpy = shell_lines(good + ("python3 - <<'PY'\n"
                               "print('--api-key 0.0.0.0 在 python 里只是字符串')\n"
                               "PY\n"))
    if check_cap("字幕", whisper, [("有 heredoc", inpy)]):
        bad.append("heredoc 正文被当成命令行了")
    if "--api-key" in commented or "--api-key" in inpy:
        bad.append("shell_lines 没把注释/heredoc 滤干净（上面两条对照就成了摆设）")

    ocr = doc["capabilities"]["文字识别"]
    ocr_ok = ("src/target/release/oar ocr --det d.onnx --rec r.onnx --dict t.txt "
              "--device auto --format json page.png\n")
    if check_cap("文字识别", ocr, [("跑 OCR", ocr_ok)]):
        bad.append("OCR 正向对照不过")
    if not check_cap("文字识别", ocr, [("带 --size", ocr_ok.replace("--device auto", "--size tiny"))]):
        bad.append("OCR 带了 --size 没被认成错")
    if not check_cap("文字识别", ocr, [("没有子命令", ocr_ok.replace("oar ocr", "oar"))]):
        bad.append("OCR 少了 ocr 子命令没被认成错")
    if not check_cap("字幕", whisper, []):
        bad.append("一项能力没有任何步骤起进程时没报 —— 那张表就没人执行过")

    # 检出这一层也要有对照：认不出步骤 = 什么都不查 = 一片绿。
    import textwrap
    mini = ("name: t\njobs:\n  j:\n    steps:\n      - name: 起常驻 whisper-server\n"
            "        run: |\n" + textwrap.indent(good, " " * 10))
    blocks, p0 = check({"capabilities": {"字幕": whisper}}, mini)
    if len(blocks) != 1 or p0:
        bad.append(f"最小 workflow 的正向对照没过：{blocks} / {p0}")
    _b1, p1 = check({"capabilities": {"字幕": whisper}},
                    mini.replace("-name whisper-server", "-name whisper-cli", 1))
    if not any("没有" in x for x in p1):
        bad.append("把定位改成品名不对的 CLI，检出层没报（表里那颗没人起）")
    _b2, p2 = check({"capabilities": {"字幕": whisper}},
                    mini.replace('"$SRV" ', ": ", 1))
    if not any("执行它" in x for x in p2):
        bad.append("只 find 不 exec 的那一步没被认出来（装了≠起了）")
    for x in bad:
        print("  BAD:", x)
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="lane_argv.py")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--plan", default=PLAN)
    ap.add_argument("--workflow", default=WF)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    with open(a.plan, encoding="utf-8") as fh:
        doc = json.load(fh)
    with open(a.workflow, encoding="utf-8") as fh:
        text = fh.read()
    blocks, bad = check(doc, text)
    caps = "、".join(sorted({c for c, _n, _t in blocks})) or "没有"
    print(f"在 {os.path.basename(a.workflow)} 里认出 {len(blocks)} 段真起进程的 shell（{caps}）")
    for x in bad:
        print("BAD:", x)
    print(f"比对了 {len(doc['capabilities'])} 项能力，问题 {len(bad)} 条")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
