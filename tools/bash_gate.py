#!/usr/bin/env python3
"""用 `bash -n` 语法检查每一条 run（只让 bash 解析文本，绝不执行任何一行）。

`set -u` 那类问题 shell_vars.py 管，但引号没闭合、`${数组[@]}` 写错、`if` 忘了 `fi`
这种要交给真解析器。bash 是 runner 上一定有的东西，本机不一定 —— 而且本机的那个
`bash` 名字可能是骗人的：2026-10-20 在这台 Windows 机器上，PATH 里的 `bash` 是 WSL
的占位程序，它对**任何**文件都返回非 0 并吐一段 UTF-16 的"未安装"提示，
于是 42/42 步骤全部"语法错"，看着像查得严，实际一条真信息都没有。

所以这里先跑两个对照：故意写坏的文件必须非 0、故意写好的必须 0。两个都对，
这个 bash 才有资格参与判据；否则只报 SKIP（并说明为什么），绝不假装查过。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

GOOD_SRC = "if [ x ]; then\n  echo ok\nfi\n"
BAD_SRC = "if [ x ]\n"                    # 少了 then/fi：bash -n 必须报
CANDIDATES = ("bash", "/bin/bash",
              r"C:/Program Files/Git/bin/bash.exe",
              r"C:/Program Files/Git/usr/bin/bash.exe")


def _run(bash: str, path: str) -> tuple[int, str]:
    """bash -n 一个文件，返回 (退出码, stderr 文本)。单测从这里换进假 bash。"""
    p = subprocess.run([bash, "-n", path], capture_output=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stderr or "")


def probe_bash(run=_run, cands=CANDIDATES) -> tuple[str, str | None, str]:
    """返回 (状态, 命中的 bash 路径, 原因)。状态三选一：
    usable / unreliable（跑起来了但没过双对照）/ missing（一个都跑不起来）。

    每个候选都要过双对照，过不了就换下一个 —— 这样 WSL 占位程序会被跳过，
    而真 bash 即使在 PATH 第二位也能被用上。
    """
    fd, good = tempfile.mkstemp(suffix=".sh", text=True)
    os.write(fd, GOOD_SRC.encode()); os.close(fd)
    fd, bad = tempfile.mkstemp(suffix=".sh", text=True)
    os.write(fd, BAD_SRC.encode()); os.close(fd)
    tried, refused = [], []
    try:
        for c in cands:
            if c != "bash" and not os.path.exists(c):
                continue
            try:
                g, _ = run(c, good)
                b, _ = run(c, bad)
            except (OSError, ValueError) as e:      # 找不到可执行、不是应用等
                refused.append(f"{c}: {type(e).__name__}")
                continue
            if g == 0 and b != 0:
                return "usable", c, ""
            tried.append(f"{c}: 好文件 rc={g}（要 0），坏文件 rc={b}（要非 0）")
        # 分两种"没法用"：跑起来的候选没过对照（unreliable，值得知道是谁在骗人），
        # 还是一个都跑不起来（missing，本机没装而已）。混成一格就会把 WSL 占位程序
        # 和"没装 bash"说成同一件事，下一条车道就没人去查真因了。
        return ("unreliable" if tried else "missing"), None, "；".join(tried + refused) or "没有可试的候选"
    finally:
        for p in (good, bad):
            try:
                os.unlink(p)
            except OSError:
                pass


def check(bodies: list[tuple[str, str]], run=_run) -> tuple[list[str], str]:
    """bodies 是 (标签, 脚本正文)。返回 (问题列表, 一句状态说明)。"""
    state, bash, why = probe_bash(run)
    if state != "usable":
        return [], f"SKIP：没有可信的 bash 可做语法检查（{state}：{why}）"
    problems = []
    for label, src in bodies:
        fd, p = tempfile.mkstemp(suffix=".sh", text=True)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(src)
        try:
            rc, err = run(bash, p)
        finally:
            try:
                os.unlink(p)
            except OSError:
                pass
        if rc != 0:
            problems.append(f"{label} bash -n rc={rc}: "
                            f"{err.strip().replace(p, label)[:300]}")
    return problems, f"用 {bash} 语法检查了 {len(bodies)} 段 run（只解析，未执行）"


def step_bodies(files: list[str]) -> list[tuple[str, str]]:
    import yaml
    out = []
    for f in files:
        d = yaml.safe_load(open(f, encoding="utf-8"))
        for jn, job in (d.get("jobs") or {}).items():
            for i, st in enumerate(job.get("steps") or []):
                body = st.get("run")
                if body:
                    out.append((f"{os.path.basename(f)}:{jn}#{i}({st.get('name') or ''})", body))
    return out


def main(argv: list[str]) -> int:
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    files = argv or [os.path.join(root, ".github", "workflows", f)
                     for f in ("ci.yml", "engine-live.yml", "package.yml")]
    for f in files:
        if not os.path.exists(f):
            print(f"BAD: 要检查的 workflow 不存在：{f}")
            return 1
    bodies = step_bodies(files)
    problems, note = check(bodies)
    for x in problems:
        print("BAD:", x)
    print(f"---- bash 语法闸门 ----\n{note}")
    print("BAD 数量 =", len(problems))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
