#!/usr/bin/env python3
"""外壳源码的门禁：合同说的那些事，源码里是不是真的那么做的。

不编译（本机禁止编译），只做**结构断言**，每条都能指回 `pins/shell-contract.json`
的一句规则：

  * 合同是编译期嵌入的（`include_str!` 指向那张提交在仓库里的表），不是运行时去找；
  * 生产代码里没有引擎名/旗标/默认端口 —— 这些只许出现在表里；
  * 只起一个进程：`Command::new` 的参数只能来自 `LaunchPlan`；
  * 起之前先校验载荷：`verify_payload` 必须排在 `spawn` 前面（server 与 OCR 两条路）；
  * 外壳自己不监听：`TcpListener::bind` 只能出现在取临时端口的函数里，且端口是 0；
  * token 只在内存里：启动/日志路径上不许出现它；
  * UI 存在时，不许出现 fetch/XHR/WS/硬编码回环地址。

`#[cfg(test)]` 之后的部分不算生产代码 —— 测试里点名 llama/oar 是故意的。
"""
from __future__ import annotations

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
CORE = os.path.join(ROOT, "shell", "core", "src")
CONTRACT = os.path.join(ROOT, "pins", "shell-contract.json")

ENGINE_WORDS = re.compile(r"(?i)\b(llama|whisper|sd-server|sd_server|oar-ocr|qwen|ggml)\b")
FLAGS = re.compile(r"--(api-key|cors-origins|port|host|listen)\b")
ENGINE_PORTS = re.compile(r"\b(8080|8081|7860|800[0-9]|9000|500[0-1])\b")
TEST_CUT = "\n#[cfg(test)]"

UI_FORBIDDEN = ["fetch(", "XMLHttpRequest", "WebSocket", "EventSource",
                "127.0.0.1", "localhost:"]
UI_SUFFIXES = (".js", ".mjs", ".html", ".htm", ".jsx", ".ts", ".tsx")


def production(text: str) -> str:
    idx = text.find(TEST_CUT)
    return text if idx < 0 else text[:idx]


def sources(core: str = CORE) -> dict[str, str]:
    out = {}
    for name in sorted(os.listdir(core)):
        if not name.endswith(".rs"):
            continue
        with open(os.path.join(core, name), encoding="utf-8") as fh:
            out[name] = production(fh.read())
    return out


def extract_fn(text: str, name: str) -> str:
    """按大括号配平取函数体；找不到就返回空串，让调用方报红。"""
    match = re.search(r"\bfn\s+" + re.escape(name) + r"\s*\(", text)
    if not match:
        return ""
    start = text.find("{", match.end())
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[match.start():i + 1]
    return ""


def scan(core: str = CORE, contract: str = CONTRACT) -> list[str]:
    bad: list[str] = []
    if not os.path.isfile(contract):
        return [f"合同不在: {contract}"]
    src = sources(core)
    if "contract.rs" not in src:
        return [f"{core} 里没有 contract.rs"]
    if 'include_str!("../../../pins/shell-contract.json")' not in src["contract.rs"]:
        bad.append("contract.rs 不是编译期嵌入 pins/shell-contract.json")

    for name, text in src.items():
        for m in ENGINE_WORDS.finditer(text):
            line = text[:m.start()].count("\n") + 1
            bad.append(f"{name}:{line}: 生产代码里写死了引擎名 {m.group(0)!r}")
        for m in FLAGS.finditer(text):
            line = text[:m.start()].count("\n") + 1
            bad.append(f"{name}:{line}: 生产代码里写死了引擎旗标 {m.group(0)!r}")
        for m in ENGINE_PORTS.finditer(text):
            line = text[:m.start()].count("\n") + 1
            bad.append(f"{name}:{line}: 生产代码里写死了端口 {m.group(0)}")

    sup = src.get("supervisor.rs", "")
    if sup:
        spawns = re.findall(r"Command::new\(([^)]*)\)", sup)
        if spawns != ["&plan.program"]:
            bad.append(f"起进程的参数必须只有 LaunchPlan: {spawns}")
        for fn in ("ensure_running", "ocr"):
            body = extract_fn(sup, fn)
            if not body:
                bad.append(f"supervisor.rs 里找不到 {fn}")
                continue
            verify = body.find("verify_payload")
            spawn = body.find("self.spawn")
            if verify < 0:
                bad.append(f"{fn} 没校验载荷清单")
            elif spawn < 0:
                bad.append(f"{fn} 没有起进程（结构变了？）")
            elif verify > spawn:
                bad.append(f"{fn}: 先起了进程再校验载荷 —— 第三条验收反了")
        binds = re.findall(r"TcpListener::bind\(([^)]*\)?[^)]*)\)", sup)
        if len(binds) != 1 or "0" not in binds[0]:
            bad.append(f"外壳只能在自己的临时端口上 bind 一次: {binds}")
        alloc = extract_fn(sup, "allocate_port")
        if "drop(listener)" not in alloc:
            bad.append("allocate_port 拿了端口却没立刻释放")
        spawn_body = extract_fn(sup, "spawn")
        for needle in ("token", "fs::write", ".args("):
            if needle == ".args(":
                continue
            if needle in spawn_body:
                bad.append(f"spawn 里出现了 {needle!r} —— token 不许靠近日志/磁盘")
        if ".token" in spawn_body or "plan.token" in spawn_body:
            bad.append("spawn 读了 token —— 它只该在内存的 Running 里")
    else:
        bad.append("没有 supervisor.rs")
    return bad


def ui_scan(ui_dir: str) -> list[str]:
    if not ui_dir or not os.path.isdir(ui_dir):
        return []
    bad = []
    for base, _dirs, files in os.walk(ui_dir):
        for name in files:
            if not name.endswith(UI_SUFFIXES):
                continue
            path = os.path.join(base, name)
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            for needle in UI_FORBIDDEN:
                if needle in text:
                    try:
                        rel = os.path.relpath(path, ROOT)
                    except ValueError:          # 另一块盘（测试用临时目录）
                        rel = path
                    bad.append(f"{rel}: UI 里出现了 {needle!r}（UI 只许走 Tauri IPC）")
    return bad


def selftest() -> int:
    """每条判据都要能被改坏；改不坏的就是装饰。"""
    import copy
    src = sources()
    base = scan()
    if base:
        for x in base:
            print("  BAD:", x)
        print("BAD 数量 =", len(base))
        return 1
    bad: list[str] = []

    def expect_red(label: str, mutate) -> None:
        files = copy.deepcopy(src)
        mutate(files)
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            for name, text in files.items():
                with open(os.path.join(d, name), "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
            got = scan(core=d)
        if not got:
            bad.append(f"负向对照「{label}」没被抓住")

    def put(files, name, old, new):
        assert old in files[name], f"锚点不在 {name}: {old[:40]}"
        files[name] = files[name].replace(old, new, 1)

    expect_red("合同改成运行时读取",
               lambda f: put(f, "contract.rs", 'include_str!("../../../pins/shell-contract.json")',
                             'std::fs::read_to_string("pins/shell-contract.json").unwrap()'))
    expect_red("生产代码写死 llama", lambda f: put(f, "supervisor.rs", "pub struct LaunchPlan {",
                                                  "pub const X: &str = \"llama\";\npub struct LaunchPlan {"))
    expect_red("生产代码写死 --api-key",
               lambda f: put(f, "supervisor.rs", "pub struct LaunchPlan {",
                             "pub const Y: &str = \"--api-key\";\npub struct LaunchPlan {"))
    expect_red("先起进程再校验",
               lambda f: put(f, "supervisor.rs",
                             "        self.verify_payload(&cap, &cell)?;\n        let token = match",
                             "        let token = match"))
    expect_red("bind 写死端口",
               lambda f: put(f, "supervisor.rs", 'TcpListener::bind((bind, 0))',
                             'TcpListener::bind((bind, 8080))'))
    expect_red("Command::new 用字面量",
               lambda f: put(f, "supervisor.rs", "Command::new(&plan.program)",
                             "Command::new(\"engine.exe\")"))
    expect_red("spawn 碰 token",
               lambda f: put(f, "supervisor.rs", "let log = fs::File::create(&log_path)?;",
                             "let _ = &plan.token;\n        let log = fs::File::create(&log_path)?;"))
    for label, body in [("fetch(", "fetch('/x')"), ("127.0.0.1", "http://127.0.0.1:1")]:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.js"), "w", encoding="utf-8") as fh:
                fh.write(f"const x = \"{body}\";\n")
            if not ui_scan(d):
                bad.append(f"负向对照「UI 出现 {label}」没被抓住")
    for x in bad:
        print("  BAD:", x)
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="shell_source.py")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--ui", default=os.path.join(ROOT, "shell", "ui"))
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    bad = scan() + ui_scan(a.ui)
    for x in bad:
        print("BAD:", x)
    print(f"外壳源码门禁：问题 {len(bad)} 条；UI 目录存在 = {os.path.isdir(a.ui)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
