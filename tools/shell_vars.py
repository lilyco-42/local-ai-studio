"""静态检查：`set -u` 的 shell 步骤里引用了本步骤从未定义、也不属于已知环境的变量。

动机是一次真事故：新加的 llama-server 步骤写 `export LD_LIBRARY_PATH="$(dirname
"$SRV"):$LD_LIBRARY_PATH"`，而 runner 上这个变量根本不存在 —— 步骤在起服务之前就死在
"unbound variable"，且 CI 只有真跑那天才会告诉你。`bash -n` 查不出来（语法是对的），
shellcheck 不在这个仓库的依赖里，所以自己写一条纯文本判据。

只认这三种"有出处"的名字：步骤里赋过值（`X=`/`export X=`/`local X=`/`for X in`/
`read X`/`declare X`）、job·step·workflow 的 `env:` 键、以及一份 GitHub runner
一定给的内置变量白名单。`${X:-…}`、`${X:+…}` 是"作者已经知道它可能没有"的写法，
整段先抹掉不查；`${X}` 与裸 `$X` 才是要报的。
两类文本根本不展开，也要先洗掉，否则判据会骗人：整行 `#` 注释（写一句
"不要用 :$LD_LIBRARY_PATH" 就报未定义，是把作者的正确写法当 bug）；以及
**不在** heredoc 里的判断 —— heredoc 正文中的 `#` 不是注释，未加引号的
`<<PY` 会照常展开 `$`，所以那段绝不能按注释洗（这条区分本身有单测守着）。
"""
from __future__ import annotations

import re

# runner / 镜像一定提供的环境，不需要在本仓库里定义
BUILTINS = {
    "CI", "HOME", "PATH", "SHELL", "TMPDIR", "TEMP", "TMP", "USER", "PWD", "OLDPWD",
    "IFS", "LANG", "LC_ALL", "TERM", "DEBIAN_FRONTEND", "GITHUB_ENV", "DISPLAY",
    "DBUS_SESSION_BUS_ADDRESS", "HOSTNAME", "LOGNAME", "OPTIND", "RANDOM", "SECONDS",
}
ASSIGN_RE = re.compile(r"^\s*(?:export\s+|local\s+|readonly\s+|declare(?:\s+-\w+)*\s+)?([A-Za-z_]\w*)=", re.M)
FOR_RE = re.compile(r"\bfor\s+([A-Za-z_]\w*)\s+in\b")
# `while IFS=$'\t' read -r target url` —— 只抓第一个变量的话，第二个（url）会被当成未定义，
# 这是一条真实的误报（写这个工具时在本仓库第一遍就撞上了）。
READ_RE = re.compile(r"\bread((?:\s+(?:-\w+|[A-Za-z_]\w*))+)\s*(?:[;|<&]|\n|$)")
GUARD_RE = re.compile(r"\$\{([A-Za-z_]\w*):[-+][=]?")
REF_RE = re.compile(r"\$\{?\s*([A-Za-z_]\w*)")
SET_U_RE = re.compile(r"set\s+-[a-zA-Z]*u")
LINEVAR_RE = re.compile(r"(?:^|[;&|]\s*)([A-Za-z_]\w*)=\S")
# heredoc 起始行：`<<'PY'`、`<<"PY"`、`<<PY`、`<<-PY` 四种写法都要认出来
HDOC_RE = re.compile(r"<<-?\s*(?:'([A-Za-z_]+)'|\"([A-Za-z_]+)\"|([A-Za-z_]\w*))")


def _strip_guards(body: str) -> str:
    """把 `${X:-…}` / `${X:+…}` 连同其内容一起抹成空格（支持一层嵌套花括号）。"""
    out, i = list(body), 0
    while i < len(body):
        m = GUARD_RE.match(body, i)
        if not m:
            i += 1
            continue
        depth, j = 1, m.end()
        while j < len(body) and depth:
            if body[j] == "{":
                depth += 1
            elif body[j] == "}":
                depth -= 1
            j += 1
        for k in range(i, j):
            out[k] = " "
        i = j
    return "".join(out)


def _assign_positions(body: str) -> dict[str, list[int]]:
    """名字 → 它被赋值处令牌的字符位置。用位置而不是行号，是为了分清
    "`X=v; echo $X`（同一行但已定义）"和"`export X=$X:…`（同一行、右侧先求值）"。"""
    out: dict[str, list[int]] = {}

    def add(m: re.Match, g: int):
        out.setdefault(m.group(g), []).append(m.start(g))

    for rx in (ASSIGN_RE, FOR_RE, LINEVAR_RE):
        for m in rx.finditer(body):
            add(m, 1)
    for m in READ_RE.finditer(body):
        base = m.start(1)
        for off, t in enumerate(m.group(1).split()):
            if not t.startswith("-"):
                out.setdefault(t, []).append(base + body[base:].index(t))
    return out


def defined_names(body: str, env_keys=()) -> set[str]:
    names = set(env_keys) | set(BUILTINS)
    names |= set(_assign_positions(body))
    return names


def _strip_quoted_heredocs(body: str) -> str:
    """`<<'PY' … PY` 的内容由 Python 解释，不是 shell：里面的 `$xxx` 不该被当成变量。
    未加引号的 heredoc 会做展开，所以只剥引号形式。"""
    lines = body.split("\n")
    out, skip = [], None
    for ln in lines:
        if skip is not None:
            if ln.strip() == skip:
                skip = None
            out.append(" " * len(ln))   # 保持长度，位置才能对回原 body
            continue
        m = re.search(r"<<-?\s*'([A-Za-z_]+)'", ln)
        if m:
            skip = m.group(1)
            out.append(ln)   # 起始行本身是 shell（重定向、管道都在这行）
            continue
        out.append(ln)
    return "\n".join(out)


def _heredoc_spans(body: str) -> list[tuple[int, int]]:
    """每个 heredoc 的**正文**行区间（不含起始行）。`<<-` 的制表符缩进靠 strip() 兜。"""
    lines = body.split("\n")
    spans, i = [], 0
    while i < len(lines):
        m = HDOC_RE.search(lines[i])
        if m:
            term = m.group(1) or m.group(2) or m.group(3)
            j = i + 1
            while j < len(lines) and lines[j].strip() != term:
                j += 1
            spans.append((i + 1, j))
            i = j
        i += 1
    return spans


def _strip_line_comments(body: str) -> str:
    """整行 `#` 注释抹成空格（长度不变）。bash 里这种行根本不展开，
    写句"不要用 :$LD_LIBRARY_PATH"就被判成未定义引用，是判据在骗人。

    但 heredoc 正文里的 `#` **不是注释**（未加引号的 heredoc 会照常展开 `$`），
    所以行区间要按 heredoc 排除掉 —— 否则就把真问题洗白了。
    """
    spans = _heredoc_spans(body)
    lines = body.split("\n")
    for n, ln in enumerate(lines):
        if any(a <= n <= b for a, b in spans):
            continue
        if ln.lstrip().startswith("#"):
            lines[n] = " " * len(ln)
    return "\n".join(lines)


def unbound_refs(body: str, env_keys=()) -> list[tuple[int, str]]:
    """返回 (行号, 变量名)；行号按原始文本数，方便直接在 CI 日志里定位。"""
    if not SET_U_RE.search(body):
        return []
    core = _strip_line_comments(_strip_quoted_heredocs(body))
    positions = _assign_positions(core)
    static = set(env_keys) | set(BUILTINS)
    shielded = _strip_guards(core)
    hits = []
    for m in REF_RE.finditer(shielded):
        name = m.group(1)
        if name in static or name.startswith(("GITHUB_", "RUNNER_", "inputs.", "matrix.")):
            continue
        # 赋值只要在这个引用之前、且中间隔了语句边界（换行 ; & |），就算已经定义；
        # 同一条语句里"自己赋值给自己用"（export FOO=$FOO:x）在 set -u 下仍是未定义。
        ready = any(a < m.start() and re.search(r"[;&|\n]", body[a:m.start()])
                    for a in positions.get(name, ()))
        if not ready:
            hits.append((body[:m.start()].count("\n") + 1, name))
    return hits
