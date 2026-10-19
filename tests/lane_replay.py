"""车道判据的"预跑"工具（不是测试文件，discover 不会当用例收集）。

为什么需要它：CI 现在一次都跑不了，而车道里的判据本身可能是空的 ——
"要求 stdout 里出现某个词"这种断言，如果归一化写错，无论模型输出什么都永远绿或永远红。
做法：把 workflow 里那段 heredoc PY 原样抽出来，在临时目录里放好合成输入再 exec，
把 SystemExit 当成结论。这样判据的正确性与 CI 是否可用就脱钩了。

约定：只喂文件与环境变量，不 mock 任何东西 —— 所以**依赖外部命令的段**
（比如用 `rustc --version` 探工具链版本的那段）不适合走这里，那些只能等真跑。
"""
from __future__ import annotations

import contextlib
import io
import os
import re
import tempfile

import yaml

WF = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                  "..", ".github", "workflows", "engine-live.yml")


def block(job: str, step_idx: int, n: int = 0) -> str:
    """取某 job 某步骤里的第 n 段 `<<'PY' … PY`（与 tools/yaml_gate.py 同一套抽取规则）。"""
    with open(WF, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    steps = doc["jobs"][job]["steps"]
    body = steps[step_idx].get("run") or ""
    blocks = re.findall(r"<<'PY'[^\n]*\n(.*?)\nPY", body, re.S)
    assert len(blocks) > n, f"{job}#{step_idx} 没有第 {n} 段 heredoc PY（一共 {len(blocks)} 段）"
    return blocks[n]


def step_name(job: str, step_idx: int) -> str:
    with open(WF, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return doc["jobs"][job]["steps"][step_idx].get("name") or ""


def find_index(job: str, needle: str) -> int:
    """按步骤名片段定位，命中数必须正好是 1。

    为什么不用写死的序号：本轮往 ocr-proof 中间插了一步"传依赖锁"，把所有后续序号都推了一位，
    而写死序号的测试会**安静地指到别的判据上**（这里靠 assert 步骤名才没指错）。
    名字才是这一步存在的证据；步骤被改名或删掉时这里会当场报错，而不是去测另一段。
    """
    with open(WF, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    steps = doc["jobs"][job]["steps"]
    hits = [i for i, s in enumerate(steps) if needle in (s.get("name") or "")]
    assert len(hits) == 1, f"{job} 里 {needle!r} 命中 {len(hits)} 步 {hits}：判据不知道该指哪一步"
    return hits[0]


def body(job: str, needle: str) -> str:
    """按步骤名取整段 shell 正文。判据抽成 tools/ 里的脚本之后，车道剩下的就是
    "怎么起服务、怎么调工具"这部分 —— 它没有 heredoc PY 可回放，只能按文本钉住形状。"""
    with open(WF, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    return doc["jobs"][job]["steps"][find_index(job, needle)].get("run") or ""


def run(code: str, files: dict | None = None, env: dict | None = None) -> tuple[int, str]:
    """在临时目录里放好 files、设好 env，exec 这段判据。返回 (退出码, 合并输出)。

    退出码 0 = 判据认为通过；1 = 判据主动红；99 = 段本身抛了别的异常（也算不通过，
    但它说明这条判据在被测的东西之外还有问题，别当成"红得有道理"）。
    """
    out = io.StringIO()
    saved: dict[str, str | None] = {}
    with tempfile.TemporaryDirectory() as d:
        for rel, content in (files or {}).items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            mode = "wb" if isinstance(content, bytes) else "w"
            with open(path, mode, **({} if isinstance(content, bytes) else {"encoding": "utf-8"})) as fh:
                fh.write(content)
        for k, v in (env or {}).items():
            saved[k] = os.environ.get(k)
            os.environ[k] = v
        cwd = os.getcwd()
        os.chdir(d)
        rc = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                exec(compile(code, "车道判据", "exec"), {})
        except SystemExit as e:
            if isinstance(e.code, int):
                rc = e.code
            else:
                rc = 1
                out.write(f"{e.code}\n")
        except Exception as e:      # noqa: BLE001  判据段抛别的异常，也要能被看见而不是被跳过
            rc = 99
            out.write(f"{type(e).__name__}: {e}\n")
        finally:
            os.chdir(cwd)
            for k, prev in saved.items():
                if prev is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = prev
    return rc, out.getvalue()
