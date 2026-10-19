"""本地静态闸门：解析 workflow YAML，核对结构、needs 链、step 输出引用、heredoc 闭合、
以及车道里写死的 target 键名确实存在于 manifest。只读文本，不编译不运行任何引擎。
用法：python tools/yaml_gate.py   （退出码非 0 = 有 BAD）"""
import importlib.util
import json
import os
import re
import sys
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("engine_probe", os.path.join(HERE, "engine_probe.py"))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)

ROOT = os.path.abspath(os.path.join(HERE, ".."))
# 默认查两条，但允许传路径：这样"闸门本身会不会红"能被单测用临时文件证明，
# 而不是只能靠我手工掺假再手工还原。
FILES = (sys.argv[1:] or
         [os.path.join(ROOT, ".github", "workflows", "ci.yml"),
          os.path.join(ROOT, ".github", "workflows", "engine-live.yml")])
rel = {e: json.load(open(os.path.join(HERE, "..", "tests", "fixtures", f"{e}.json"), encoding="utf-8"))
       for e in probe.UPSTREAM}
keys = set(probe.manifest_of(probe.plan(rel))["targets"])
bad = []
py_seen = 0

for f in FILES:
    raw = open(f, encoding="utf-8").read()
    if "\t" in raw:
        bad.append(f"{f} 含真实制表符，Actions 会拒收")
    d = yaml.safe_load(raw)
    print(f"{f}: jobs={list(d['jobs'])}")
    for jn, job in d["jobs"].items():
        if "runs-on" not in job or "steps" not in job:
            bad.append(f"{f}:{jn} 缺 runs-on 或 steps")
        need = job.get("needs") or []
        for nd in (need if isinstance(need, list) else [need]):
            if nd not in d["jobs"]:
                bad.append(f"{f}:{jn} needs 指向不存在的 {nd}")
        ids = {s.get("id") for s in job["steps"]}
        for k, v in (job.get("outputs") or {}).items():
            m = re.search(r"steps\.([A-Za-z0-9_-]+)\.", str(v))
            if m and m.group(1) not in ids:
                bad.append(f"{f}:{jn} 输出 {k} 引用了不存在的 step id {m.group(1)}")
        if "steps." in str(job.get("outputs") or "") and "id:" not in raw:
            bad.append(f"{f}:{jn} 用了 steps.* 但没有 step 声明 id")
        for i, st in enumerate(job["steps"]):
            body = st.get("run", "")
            if not body:
                continue
            if len(re.findall(r"<<'PY'", body)) != len(re.findall(r"(?m)^PY$", body)):
                bad.append(f"{f}:{jn}#{i}({st.get('name','')}) heredoc 没闭合")
            # heredoc 里的 Python 要真编译一遍：上一版我只查了闭合，
            # 结果一个多缩进两格的注释（Python 里就是 IndentationError）顺利溜过闸门。
            for n, code in enumerate(re.findall(r"<<'PY'[^\n]*\n(.*?)\nPY", body, re.S)):
                # 上面那个 [^\n]* 是必要的：discover 里写的是 `<<'PY' >> "$GITHUB_OUTPUT"`，
                # 要求紧跟换行会让那一段被静默跳过，闸门就变成只查一部分的假绿。
                py_seen += 1
                try:
                    compile(code, f"{f}:{jn}#{i} 第{n}段 PY", "exec")
                except SyntaxError as e:
                    bad.append(f"{f}:{jn}#{i}({st.get('name','')}) 第{n}段 PY 语法错：{e.msg} 行 {e.lineno}")
            for t in re.findall(r'"((?:llama|whisper|sd)-[a-z]+-[a-z0-9]+(?:-[a-z0-9]+){1,2})"', body):
                if t not in keys and not t.endswith("-cudart"):
                    bad.append(f"{f}:{jn}#{i} 引用了 manifest 里没有的 target：{t}")
        # needs 里引用的 outputs 名也要在对方 job 的 outputs 里
        for ref in re.findall(r"needs\.[A-Za-z0-9_-]+\.outputs\.([A-Za-z0-9_]+)", raw):
            src = [d["jobs"][j] for j in d["jobs"] if isinstance(job.get("needs"), (str, list))
                   and (job["needs"] == j or j in (job["needs"] or []))]
            if src and ref not in (src[0].get("outputs") or {}):
                bad.append(f"{f}:{jn} 引用了 needs.outputs.{ref}，上游没导出")

print("---- 结论 ----")
print(f"覆盖：编译了 {py_seen} 段 heredoc PY（0 段就意味着闸门什么都没查）")
for b in bad:
    print("BAD:", b)
print("BAD 数量 =", len(bad))
sys.exit(1 if bad else 0)
