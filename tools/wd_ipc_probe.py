"""真驱动窗口里的 JS：开一个 WebDriver 会话，从页面内调用 status / data_root，再读回 UI 自己渲染的结果。

为什么窗口出现还不够：那一步只证明 GTK + Tauri 能建窗。`withGlobalTauri` 关掉、CSP 挡了 `ipc:`、
命令名改了、`Status` 序列化崩了，窗口都照样在。这里用 tauri-driver（crate 2.1.0）+ WebKitWebDriver
把窗口当浏览器连上，执行页面里的 `window.__TAURI__.core.invoke`，并核对 UI 真的把返回值画了出来。

只依赖标准库；Linux 专用（Windows 要 msedgedriver，macOS 上游还没有可用原生驱动）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# 与 shell/ui/main.js 的 TAB_CAP 一致：这四个名字是 UI 与后端的接缝，改名了这里必须跟着红。
EXPECTED_CAPABILITIES = ("对话", "字幕", "文字识别", "生图")
REQUIRED_STATUS_FIELDS = (
    "capability", "engine", "available", "running",
    "pid", "port", "idle_secs_left", "lifecycle",
)

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ProbeError(RuntimeError):
    pass


def say(line: str) -> None:
    print(line, flush=True)


def session_payload(app: str) -> dict:
    if not os.path.isabs(app):
        raise ValueError(f"--app 必须是绝对路径（原生驱动不解析工作目录）：{app!r}")
    return {"capabilities": {"alwaysMatch": {"browserName": "wry", "tauri:options": {"application": app}}}}


def driver_argv(binary: str, port: int, native_port: int, native_driver: str | None) -> list[str]:
    if port == native_port:
        raise ValueError(f"代理端口与原生驱动端口不能相同：{port}")
    argv = [binary, "--port", str(port), "--native-port", str(native_port)]
    if native_driver:
        argv += ["--native-driver", native_driver]
    return argv


def parse_driver_error(code: int, body: bytes) -> str:
    """WebDriver 的错误体是 {"value":{"error":…,"message":…}}；解析失败也要留下原文。"""
    text = body.decode("utf-8", "replace")
    try:
        value = json.loads(text).get("value")
    except (ValueError, AttributeError):
        value = None
    if isinstance(value, dict) and value.get("error"):
        return f"HTTP {code} {value.get('error')}: {value.get('message') or ''}".strip()
    return f"HTTP {code}: {text[:400]}"


def request(url: str, method: str = "GET", payload: dict | None = None, timeout: float = 30.0):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ProbeError(parse_driver_error(exc.code, exc.read())) from None
    except urllib.error.URLError as exc:
        raise ProbeError(f"{method} {url} 连不上：{exc.reason}") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ProbeError(f"{method} {url} 回的不是 JSON：{raw[:200]!r}") from None


def is_absolute_path(value: str) -> bool:
    return value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value) is not None


def check_status(rows, expected=EXPECTED_CAPABILITIES) -> None:
    if not isinstance(rows, list):
        raise ProbeError(f"status 应是数组，实际是 {type(rows).__name__}")
    if len(rows) != len(expected):
        raise ProbeError(f"status 应有 {len(expected)} 行，实际 {len(rows)} 行")
    names = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ProbeError(f"status[{index}] 不是对象：{row!r}")
        missing = [key for key in REQUIRED_STATUS_FIELDS if key not in row]
        if missing:
            raise ProbeError(f"status[{index}] 缺字段 {missing}")
        if not isinstance(row["capability"], str) or not row["capability"]:
            raise ProbeError(f"status[{index}].capability 不是非空字符串：{row['capability']!r}")
        for key in ("available", "running"):
            if not isinstance(row[key], bool):
                raise ProbeError(f"status[{index}].{key} 不是布尔值：{row[key]!r}")
        for key in ("engine", "lifecycle"):
            if not isinstance(row[key], str) or not row[key]:
                raise ProbeError(f"status[{index}].{key} 不是非空字符串：{row[key]!r}")
        for key in ("pid", "port", "idle_secs_left"):
            value = row[key]
            if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
                raise ProbeError(f"status[{index}].{key} 既不是 null 也不是整数：{value!r}")
        names.append(row["capability"])
    if len(set(names)) != len(names):
        raise ProbeError(f"status 有重复能力：{names}")
    if set(names) != set(expected):
        raise ProbeError(f"status 的能力集合是 {sorted(names)}，期望 {sorted(expected)}")


def check_data_root(value) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProbeError(f"data_root 应是非空字符串，实际 {value!r}")
    if not is_absolute_path(value):
        raise ProbeError(f"data_root 不是绝对路径：{value!r}")


def check_ui(state, data_root: str, expected_title: str = "Local AI Studio") -> None:
    if not isinstance(state, dict):
        raise ProbeError(f"UI 状态不是对象：{state!r}")
    if state.get("title") != expected_title:
        raise ProbeError(f"窗口标题是 {state.get('title')!r}，期望 {expected_title!r}")
    if state.get("tauri") is not True:
        raise ProbeError("页面里没有 window.__TAURI__.core.invoke")
    if state.get("boot_hidden") is not True:
        raise ProbeError("页面显示了“找不到 Tauri IPC”提示条（#boot 未隐藏）")
    chip = state.get("chip")
    if not isinstance(chip, str) or not chip.startswith("对话 · "):
        raise ProbeError(f"引擎状态条没有渲染对话能力：{chip!r}")
    if state.get("data_root_text") != data_root:
        raise ProbeError(f"UI 的数据目录 {state.get('data_root_text')!r} 与 data_root 返回值 {data_root!r} 不一致")


READY_JS = 'return !!(window.__TAURI__ && window.__TAURI__.core && typeof window.__TAURI__.core.invoke === "function");'

INVOKE_JS = """
const done = arguments[arguments.length - 1];
window.__TAURI__.core.invoke(%s).then(
  function (value) { done({ok: true, value: value}); },
  function (error) { done({ok: false, error: String((error && error.message) || error)}); }
);
"""

UI_STATE_JS = """
return {
  title: document.title,
  tauri: !!(window.__TAURI__ && window.__TAURI__.core && typeof window.__TAURI__.core.invoke === "function"),
  boot_hidden: document.getElementById('boot').hidden,
  chip: document.getElementById('engine-chip').textContent,
  data_root_text: document.getElementById('data-root').textContent
};
"""


class Driver:
    def __init__(self, argv: list[str], base: str, log_path: str):
        self.argv = argv
        self.base = base
        self.log_path = log_path
        self.proc: subprocess.Popen | None = None

    def start(self, timeout: float = 30.0) -> None:
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(self.argv, stdout=self._log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise ProbeError(f"tauri-driver 启动即退出（rc={self.proc.returncode}）\n{self.tail()}")
            try:
                request(self.base + "/status", timeout=2.0)
                return
            except ProbeError:
                time.sleep(0.3)
        raise ProbeError(f"tauri-driver 在 {timeout:.0f}s 内没有响应 /status\n{self.tail()}")

    def tail(self, lines: int = 40) -> str:
        try:
            with open(self.log_path, encoding="utf-8", errors="replace") as fh:
                return "".join(fh.readlines()[-lines:]).strip()
        except OSError:
            return "(没有日志)"

    def new_session(self, payload: dict, timeout: float = 180.0) -> str:
        value = request(self.base + "/session", "POST", payload, timeout=timeout).get("value")
        sid = (value or {}).get("sessionId")
        if not sid:
            raise ProbeError(f"建会话没拿到 sessionId：{value!r}\n{self.tail()}")
        return sid

    def execute(self, sid: str, script: str, async_: bool = False, timeout: float = 60.0):
        action = "execute/async" if async_ else "execute/sync"
        body = request(f"{self.base}/session/{sid}/{action}", "POST",
                       {"script": script, "args": []}, timeout=timeout)
        return body.get("value")

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        try:
            self._log.close()
        except Exception:
            pass


def poll_until(fn, deadline: float, what: str, interval: float = 0.5):
    last: ProbeError | None = None
    while time.monotonic() < deadline:
        try:
            value = fn()
        except ProbeError as exc:
            last = exc
        else:
            if value:
                return value
        time.sleep(interval)
    suffix = f"；最后一次错误：{last}" if last else ""
    raise ProbeError(f"超时没等到{what}{suffix}")


def invoke(driver: Driver, sid: str, command: str):
    script = INVOKE_JS % json.dumps(command)
    result = driver.execute(sid, script, async_=True)
    if not isinstance(result, dict):
        raise ProbeError(f"invoke({command!r}) 没回包装对象：{result!r}")
    if result.get("ok") is not True:
        raise ProbeError(f"invoke({command!r}) 被后端拒绝：{result.get('error')!r}")
    return result.get("value")


def run(args) -> int:
    if not os.path.isfile(args.app):
        raise ProbeError(f"--app 指向的二进制不存在：{args.app}")
    native = args.native_driver or shutil.which("WebKitWebDriver")
    if not native:
        raise ProbeError("找不到 WebKitWebDriver（Ubuntu 上是 webkit2gtk-driver 包）")
    base = f"http://127.0.0.1:{args.port}"
    driver = Driver(driver_argv(args.driver, args.port, args.native_port, native), base, args.log)
    sid = None
    try:
        driver.start()
        say(f"driver: {args.driver}（proxy :{args.port} → {native} :{args.native_port}）")
        sid = driver.new_session(session_payload(args.app), timeout=args.timeout)
        say(f"session: {sid}")
        deadline = time.monotonic() + args.timeout
        poll_until(lambda: driver.execute(sid, READY_JS) is True, deadline, "页面里的 window.__TAURI__")

        rows = invoke(driver, sid, "status")
        check_status(rows)
        say("status: " + " / ".join(
            f"{row['capability']}(available={row['available']},running={row['running']},lifecycle={row['lifecycle']})"
            for row in rows))

        root = invoke(driver, sid, "data_root")
        check_data_root(root)
        say(f"data_root: {root}")

        def settled():
            state = driver.execute(sid, UI_STATE_JS)
            if isinstance(state, dict) and state.get("chip") not in (None, "…") and state.get("data_root_text"):
                return state
            return None

        state = poll_until(settled, deadline, "UI 把 status / data_root 渲染出来")
        check_ui(state, root)
        say(f"ui: title={state['title']!r} boot_hidden={state['boot_hidden']} chip={state['chip']!r}")

        if args.negative_control:
            spoofed = EXPECTED_CAPABILITIES[:-1] + ("不存在的能力",)
            try:
                check_status(rows, expected=spoofed)
            except ProbeError as exc:
                say(f"negative-control: 掺假能力名被拒绝（{exc}）")
            else:
                raise ProbeError("negative-control: 掺假能力名居然通过了 —— 判据是空的")
        return 0
    finally:
        if sid is not None:
            try:
                request(f"{base}/session/{sid}", "DELETE", timeout=10.0)
            except ProbeError:
                pass
        driver.stop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="用 WebDriver 真驱动外壳窗口里的 JS 与 IPC")
    parser.add_argument("--app", required=True, help="外壳二进制绝对路径（target/debug/local-ai-studio-shell）")
    parser.add_argument("--driver", default=os.environ.get("TAURI_DRIVER") or shutil.which("tauri-driver") or "tauri-driver")
    parser.add_argument("--native-driver", default=None, help="默认取 PATH 里的 WebKitWebDriver")
    parser.add_argument("--port", type=int, default=4444)
    parser.add_argument("--native-port", type=int, default=4445)
    parser.add_argument("--timeout", type=float, default=180.0, help="建会话之后等页面与 UI 的总预算（秒）")
    parser.add_argument("--negative-control", action="store_true", help="同时证明这套判据能红")
    parser.add_argument("--log", default=os.path.join(tempfile.gettempdir(), "wd-ipc-probe.log"))
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (ProbeError, ValueError, OSError) as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
