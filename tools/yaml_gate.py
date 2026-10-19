"""本地静态闸门：解析 workflow YAML，核对结构、needs 链、step 输出引用、heredoc 闭合、
以及车道里写死的 target 键名确实存在于 manifest。只读文本，不编译不运行任何引擎。
用法：python tools/yaml_gate.py   （退出码非 0 = 有 BAD）"""
import importlib.util
import glob
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
lane_pins = []

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
        jenv = job.get("env") or {}
        # 按**值的形状**认，不按键名认。上一版只认 `*_SHA256`，而 OCR 车道写的是
        # `DET_SHA / REC_SHA / DICT_SHA` —— 三条真 pin 就这样从覆盖度里漏掉，
        # 闸门还照样报"5 条已寻源"，是个典型的"读了一半却报全绿"。
        for k, v in jenv.items():
            s = str(v).strip().strip('"')
            if re.fullmatch(r"[0-9a-f]{64}", s):
                prefix = re.sub(r"_SHA(256)?$", "", k)
                lane_pins.append((f, jn, k, s, jenv.get(prefix + "_SIZE")))
            elif re.search(r"SHA", k, re.I):
                # YAML 会把"全是数字"的 64 位哈希解析成 int（测试里真踩到了），
                # 那样上面这条就永远不命中，闸门又变成"读了一半却报全绿"。
                bad.append(f"{f}:{jn} {k} 不像 64 位十六进制字符串（现在是 {type(v).__name__} "
                           f"{str(v)[:12]}…）：哈希要加引号，别让它被当成数字")
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

# 车道里写死的哈希必须能在"工具立住的 pin"或"上游 release digest 夹具"里找到出处。
# 这条是今天查出来的：那颗 415 MB 的对话权重哈希一直写在 yaml 里，但没有任何工具复现过它。
pin_shas = {}
for pf in sorted(glob.glob(os.path.join(ROOT, "pins", "*.json"))):
    with open(pf, encoding="utf-8") as fh:
        for name, v in json.load(fh).items():
            if isinstance(v, dict) and v.get("sha256"):
                pin_shas[v["sha256"]] = (os.path.basename(pf), name, v.get("size"))
upstream_shas = set()
for releases in rel.values():
    for r in (releases if isinstance(releases, list) else []):
        for a in (r.get("assets") or []):
            dg = a.get("digest") or ""
            if dg.startswith("sha256:"):
                upstream_shas.add(dg.split(":", 1)[1])
for f, jn, k, val, size in lane_pins:
    src = pin_shas.get(val)
    if not src and val not in upstream_shas:
        bad.append(f"{f}:{jn} {k} 这个哈希没有任何出处（既不在 pins/*.json，也不在夹具的上游 digest 里）")
        continue
    if src and size is not None and src[2] is not None and str(src[2]) != str(size).strip('"'):
        bad.append(f"{f}:{jn} {k} 的 {size} 与 pin {src[0]}:{src[1]} 记的 {src[2]} 体积不符")
    elif src:
        print(f"  pin 出处：{jn}.{k} → {src[0]}:{src[1]}")
    else:
        print(f"  pin 出处：{jn}.{k} → 上游 release digest（夹具里抓到的那颗）")

print("---- 结论 ----")
print(f"覆盖：编译了 {py_seen} 段 heredoc PY（0 段就意味着闸门什么都没查）；"
      f"车道写死的哈希 {len(lane_pins)} 条已寻源")
for b in bad:
    print("BAD:", b)
print("BAD 数量 =", len(bad))
sys.exit(1 if bad else 0)
