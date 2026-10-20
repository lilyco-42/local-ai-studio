#!/usr/bin/env python3
"""外壳的合同：三条验收 + 生命周期 + runtime 目录，全部从已有的表推导。

`launch_plan` 说的是"引擎该被怎么起"，这一层说的是**外壳自己**的四件事：

  ① 入口：外壳不新增监听口（UI 只走 Tauri IPC），引擎 HTTP 只由外壳进程发；
  ② 鉴权：llama 每次现生成 token 传 `--api-key`，外壳转发时带 Bearer；whisper/sd
     上游没有钥匙可给 —— 所以它们**不许常驻**，只能按需起、空闲超时停；
  ③ 起的正是下的那一颗：执行前逐文件对 `payload-manifest.json` 的 sha256，不符拒绝；
  ④ runtime 目录：`<data>/runtime/<os>/<engine>/<version>/<asset-stem>/`，
     version 取 `runtime-versions` 的钉值，换后端只是换 asset 目录，回退只是换 version。

三条验收里 ① 的 UI 那一半要等 UI 源码存在后才能静态扫；在此之前产物里如实写
`ui_checked: false`，不许把"规则写下来了"说成"扫过了"。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
PLAN = os.path.join(ROOT, "pins", "launch-plan.json")
CHOICE = os.path.join(ROOT, "pins", "backend-choice.json")
VERSIONS = os.path.join(ROOT, "pins", "runtime-versions.json")
PAYLOAD = os.path.join(ROOT, "pins", "payload-plan.json")
OCR_SURFACE = os.path.join(ROOT, "pins", "ocr-build-surface.json")
OUT = os.path.join(ROOT, "pins", "shell-contract.json")

TOKEN_BYTES = 32
IDLE_MAX = 300
RUNTIME_TEMPLATE = "<data>/runtime/<os>/<engine>/<version>/<asset-stem>/"
UI_FORBIDDEN = ["fetch(", "XMLHttpRequest", "WebSocket", "EventSource",
                "127.0.0.1", "localhost:"]
# 车道实测的上游字段：whisper 要 `response_format=srt` 才回 x-subrip，文件字段名叫 `file`。
# 外壳不许把这两个词写死在自己的源码里 —— 从这张表读。
MULTIPART_FIELD = {"字幕": "file"}
REQUEST_ADDENDA = {"字幕": {"response_format": "srt"}}

# 生命周期是**我们的决定**，理由必须能指回上游事实或验收条款。
LIFECYCLE = {
    "对话": {"mode": "resident", "idle_timeout_secs": None,
             "because": "引擎自带 --api-key，且 CORS 已从上游默认的 * 收窄到 localhost"},
    "字幕": {"mode": "on_demand", "idle_timeout_secs": IDLE_MAX,
             "because": "上游没有鉴权旗标且默认回 Access-Control-Allow-Origin: * —— "
                        "常驻等于对任意本地网页开放，只能缩短暴露窗口"},
    "生图": {"mode": "on_demand", "idle_timeout_secs": IDLE_MAX,
             "because": "同字幕：没有钥匙可给；生图是显式触发，任务结束就该停"},
    "文字识别": {"mode": "one_shot", "idle_timeout_secs": None,
             "because": "上游只有 CLI，没有服务形态，一次任务一个进程"},
}

def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def version_dir(tag: str) -> str:
    """tag 里可能带斜杠（runner-images 那种），目录名只留安全字符。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", tag)


def asset_stem(asset: str) -> str:
    for suffix in (".tar.gz", ".zip"):
        if asset.endswith(suffix):
            return asset[: -len(suffix)]
    return asset


def backends(choice: dict, engine: str, os_label: str) -> dict:
    cell = ((choice.get("engines") or {}).get(engine) or {}).get("os", {}).get(os_label)
    if not cell:
        return {"options": [], "default": None}
    opts = [{"asset": o["asset"], "backend": o["backend"], "is_default": o["is_default"]}
            for o in cell.get("options") or []]
    return {"options": opts,
            "default": next((o["asset"] for o in opts if o["is_default"]), None)}


def build(plan: dict | None = None, choice: dict | None = None,
          versions: dict | None = None, payload: dict | None = None,
          ocr: dict | None = None, ui_dir: str = "") -> tuple[dict, list[str]]:
    plan = plan if plan is not None else load(PLAN)
    choice = choice if choice is not None else load(CHOICE)
    versions = versions if versions is not None else load(VERSIONS)
    payload = payload if payload is not None else load(PAYLOAD)
    ocr = ocr if ocr is not None else load(OCR_SURFACE)
    bad: list[str] = []
    bind = plan["bind_address"]
    defaults: dict[tuple[str, str], dict] = {}
    for os_label, rows in payload["os"].items():
        for r in rows or []:
            if r.get("engine"):
                defaults[(r["engine"], os_label)] = r
    caps: dict[str, dict] = {}
    for cap, spec in plan["capabilities"].items():
        engine = spec["engine"]
        auth = spec.get("engine_auth") or {}
        has_key = bool(auth.get("engine_supports_api_key"))
        cors = spec.get("cors") or {}
        life = LIFECYCLE[cap]
        if spec["mode"] == "server" and not has_key:
            if life["mode"] == "resident":
                bad.append(f"{cap}: 上游没有鉴权旗标却把生命周期设成常驻 —— "
                           "任意本地网页都能直接敲引擎端口")
            if not life.get("idle_timeout_secs") or life["idle_timeout_secs"] > IDLE_MAX:
                bad.append(f"{cap}: 无鉴权引擎的空闲超时必须 >0 且 <= {IDLE_MAX}s")
        if has_key and life["mode"] != "resident":
            bad.append(f"{cap}: llama 有钥匙(cors 已收窄)，生命周期不必降级成 {life['mode']}")
        if has_key and not cors:
            bad.append(f"{cap}: 有钥匙的引擎必须同时收窄 CORS —— 上游默认 * 等于对任意网页开放")
        if spec["mode"] == "subprocess":
            ref = str(ocr.get("ref") or "")
            if not re.fullmatch(r"[0-9a-f]{7,40}", ref):
                bad.append(f"{cap}: ocr-build-surface.json 的 ref={ref!r} 不是 commit —— "
                           "runtime 目录无版本可钉")
            if auth.get("argv") or cors:
                bad.append(f"{cap}: 上游只有 CLI，不应有鉴权/CORS 旗标")
            if life["mode"] != "one_shot":
                bad.append(f"{cap}: subprocess 能力只能是 one_shot，现在写的是 {life['mode']}")
            caps[cap] = {
                "engine": engine, "mode": spec["mode"], "bind": None,
                "auth": {"mode": "none", "flag": None, "header": None,
                         "token_bytes": None, "generated": None, "persisted": False},
                "cors": None,
                "lifecycle": life,
                "install_stage": spec.get("install_stage"),
                "readiness": spec.get("readiness"),
                "request": spec.get("request"),
                "version": {"pinned": ref or None, "oldest_verifiable": None,
                            "verifiable_count": None},
                "os": {os_label: {"binary": b, "argv": spec.get("argv_template"),
                                  "default_asset": None,
                                  "version_dir": f"<data>/runtime/{os_label}/{engine}/{version_dir(ref)}",
                                  "asset_dir": None,
                                  "backends": {"options": [], "default": None}}
                       for os_label, b in spec["binaries"].items() if b},
            }
            continue
        vbody = versions["engines"].get(engine)
        if vbody is None:
            bad.append(f"{cap}: 引擎 {engine} 不在版本轴里 —— runtime 目录无版本可钉")
            continue
        by_os: dict[str, dict] = {}
        for os_label, binary in spec["binaries"].items():
            if not binary:
                continue
            by_tier = {k.split("/", 1)[0]: list(v)
                       for k, v in (spec.get("by_tier") or {}).items()
                       if k.endswith("/" + os_label)}
            for row in by_tier.values():
                if spec["mode"] != "server":
                    continue
                if has_key:
                    if "--api-key" not in row or "{token}" not in row:
                        bad.append(f"{cap}/{os_label}: 有钥匙的引擎这一行没带 --api-key {{token}}")
                    if cors and not any(x in row for x in cors.get("argv") or []):
                        bad.append(f"{cap}/{os_label}: 这一行没带 --cors-origins")
                else:
                    if "--api-key" in row or "api_key" in " ".join(row):
                        bad.append(f"{cap}/{os_label}: 没钥匙的引擎这一行却带了鉴权旗标")
                    if "--cors-origins" in row:
                        bad.append(f"{cap}/{os_label}: 没这条旗标的引擎这一行却带了 --cors-origins")
            dflt = defaults.get((engine, os_label))
            if dflt is None:
                if cap != "文字识别":
                    bad.append(f"{cap}/{os_label}: payload-plan 里没有真装的默认档")
                by_os[os_label] = {"binary": binary, "argv_by_tier": by_tier,
                                   "default_asset": None,
                                   "version_dir": None, "asset_dir": None,
                                   "backends": backends(choice, engine, os_label)}
                continue
            asset = dflt["asset"]
            pinned = next((a for a in vbody["releases"] if a["tag"] == vbody["pinned"]), None)
            names = {a["asset"]: a.get("sha256") for a in (pinned or {}).get("runtime") or []}
            if asset not in names:
                bad.append(f"{cap}/{os_label}: 默认档 {asset} 不在钉住的 {vbody['pinned']} 的产物表里")
                continue
            digest = str(dflt.get("digest") or "")
            if digest.startswith("sha256:") and digest[7:] != names[asset]:
                bad.append(f"{cap}/{os_label}: {asset} 的 digest 与版本轴不一致 —— 两个生产者量的不是同一颗")
            by_os[os_label] = {
                "binary": binary,
                "argv_by_tier": by_tier,
                "default_asset": asset,
                "version_dir": f"<data>/runtime/{os_label}/{engine}/{version_dir(vbody['pinned'])}",
                "asset_dir": f"<data>/runtime/{os_label}/{engine}/{version_dir(vbody['pinned'])}/{asset_stem(asset)}",
                "backends": backends(choice, engine, os_label),
            }
        req = dict(spec.get("request") or {})
        if cap in MULTIPART_FIELD and req.get("multipart_field") != MULTIPART_FIELD[cap]:
            bad.append(f"{cap}: 上游的文件字段叫 {MULTIPART_FIELD[cap]!r}，"
                       f"launch-plan 写的是 {req.get('multipart_field')!r}")
        if cap in REQUEST_ADDENDA:
            req["extra_fields"] = dict(REQUEST_ADDENDA[cap])
        caps[cap] = {
            "engine": engine, "mode": spec["mode"], "bind": bind if spec["mode"] == "server" else None,
            "auth": {"mode": "api-key" if has_key else "none",
                     "flag": "--api-key" if has_key else None,
                     "header": "Authorization: Bearer {token}" if has_key else None,
                     "token_bytes": TOKEN_BYTES if has_key else None,
                     "generated": "per engine start" if has_key else None,
                     "persisted": False},
            "cors": {"flag": (cors.get("argv") or [None, None])[0], "value": (cors.get("argv") or [None, None])[1],
                     "upstream_default": "*" if cors else None} if cors else None,
            "lifecycle": life,
            "install_stage": spec.get("install_stage"),
            "readiness": spec.get("readiness"),
            "request": req or None,
            "version": {"pinned": vbody["pinned"],
                        "oldest_verifiable": vbody.get("oldest_verifiable_tag"),
                        "verifiable_count": len(vbody.get("verifiable_tags") or [])},
            "os": by_os,
        }
    ui_checked = bool(ui_dir) and os.path.isdir(ui_dir)
    doc = {
        "schema": 1,
        "rule": {
            "entry": "外壳自己不监听任何端口（0 个）；UI 只走 Tauri IPC；引擎的 HTTP 只由外壳进程发",
            "ui": f"UI 源码只许出现 invoke*；以下串一个都不许出现：{UI_FORBIDDEN}",
            "token": f"{TOKEN_BYTES} 字节 OS 随机数转 hex；每次起引擎现生成；只在内存里，不落日志",
            "spawn": "执行前把要 exec 的文件与同目录的运行库逐文件对 payload-manifest.json 的 sha256；不符拒绝启动",
            "manifest": "payload-manifest.json（出包车道产出，随 runtime 一起装）",
            "runtime_dir": RUNTIME_TEMPLATE,
            "version_sanitize": "[^A-Za-z0-9._-] → _",
            "port": "bind 127.0.0.1:0 取临时端口，立刻释放，再把端口交给引擎；不写死端口",
            "lifecycle": f"没有引擎鉴权的能力不许常驻：on_demand 且 idle_timeout_secs <= {IDLE_MAX}；关窗即停",
        },
        "ui_checked": ui_checked,
        "ui_dir": ui_dir or None,
        "capabilities": caps,
        "gaps": [
            {"id": "ocr-manifest",
             "what": "OCR 的二进制由 CI 现编，出包车道还没有把 oar 写进 payload-manifest.json",
             "why_it_matters": "第三条验收（起的正是下的那一颗）对 OCR 还只能靠构建 artifact 的哈希，"
                               "外壳拿不到随包清单",
             "until": "出包车道产出包含 OCR 的载荷清单"},
        ],
    }
    return doc, bad


def check(committed: dict, fresh: dict) -> list[str]:
    """只报漂移，不追着改；UI 扫描状态变了也要说，免得 'rule 写了' 被读成 '扫过了'。"""
    bad = []
    if committed.get("rule") != fresh.get("rule"):
        diff = [k for k in fresh["rule"] if committed.get("rule", {}).get(k) != fresh["rule"][k]]
        bad.append(f"合同口径变了：{diff} —— 外壳实现与验收证据要一起重看")
    if committed.get("ui_checked") != fresh.get("ui_checked"):
        bad.append(f"UI 扫描状态从 {committed.get('ui_checked')} 变成 {fresh.get('ui_checked')}")
    for cap, spec in fresh["capabilities"].items():
        old = (committed.get("capabilities") or {}).get(cap)
        if not old:
            bad.append(f"{cap}: 新增能力，合同里没有")
            continue
        for key in ("engine", "mode", "bind", "auth", "cors", "lifecycle", "install_stage",
                    "version", "os"):
            if old.get(key) != spec.get(key):
                bad.append(f"{cap}.{key}: {str(old.get(key))[:70]!r} → {str(spec.get(key))[:70]!r}")
    if committed.get("gaps") != fresh.get("gaps"):
        bad.append("已知缺口清单变了 —— 别在没补上的时候把它删掉")
    return bad


def selftest() -> int:
    """判据必须能在内存里被改坏：改哪一条，红哪一条。"""
    plan, choice, versions, payload = load(PLAN), load(CHOICE), load(VERSIONS), load(PAYLOAD)
    ocr = load(OCR_SURFACE)
    base, p = build(plan, choice, versions, payload, ocr)
    bad = list(p)
    if bad:
        for x in bad:
            print("  BAD:", x)
        print("BAD 数量 =", len(bad))
        return 1

    def expect_red(label: str, mutate) -> None:
        import copy
        pl, ch, ve, pa, oc = (copy.deepcopy(x) for x in (plan, choice, versions, payload, ocr))
        mutate(pl, ch, ve, pa, oc)
        _doc, pr = build(pl, ch, ve, pa, oc)
        if not pr:
            bad.append(f"负向对照「{label}」没被抓住 —— 这条判据是装饰")

    def whisper_key(pl, *_):
        pl["capabilities"]["字幕"]["engine_auth"]["argv"] = ["--api-key", "{token}"]
        pl["capabilities"]["字幕"]["engine_auth"]["engine_supports_api_key"] = True

    expect_red("whisper 被写成有钥匙", whisper_key)
    expect_red("whisper 的文件字段被改名",
               lambda pl, *_: pl["capabilities"]["字幕"]["request"].update(multipart_field="audio"))
    expect_red("llama 丢了 CORS",
               lambda pl, *_: pl["capabilities"]["对话"].update(cors=None))
    expect_red("llama 的默认档不在钉住的 release 里",
               lambda _pl, _ch, _ve, pa, _oc: pa["os"]["win-x64"][0].update(asset="llama-另一个.zip"))
    expect_red("默认档 digest 与版本轴不一致",
               lambda _pl, _ch, _ve, pa, _oc: pa["os"]["win-x64"][0].update(digest="sha256:" + "0" * 64))
    expect_red("OCR 的 ref 不是 commit",
               lambda _pl, _ch, _ve, _pa, oc: oc.update(ref="main"))
    expect_red("OCR 被塞进鉴权旗标",
               lambda pl, *_: pl["capabilities"]["文字识别"]["engine_auth"]["argv"].append("--api-key"))
    if base["capabilities"]["字幕"]["lifecycle"]["mode"] != "on_demand":
        bad.append("字幕被设成非常驻以外的样子 —— 没有钥匙的引擎生命周期决定丢了")
    if base["capabilities"]["对话"]["auth"]["mode"] != "api-key":
        bad.append("llama 的钥匙没进合同")
    if base["capabilities"]["文字识别"]["version"]["pinned"] != ocr["ref"]:
        bad.append("OCR 的 runtime 目录没钉在 ocr-build-surface.json 的 ref 上")
    if base["ui_checked"] is not False:
        bad.append("UI 还没写就把 ui_checked 说成真了")
    for x in bad:
        print("  BAD:", x)
    print("BAD 数量 =", len(bad))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="shell_contract.py")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--ui", default="", help="UI 源码目录；不存在就如实标 ui_checked=false")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args(argv)
    if a.selftest:
        return selftest()
    fresh, problems = build(ui_dir=a.ui)
    if a.check:
        committed = load(a.out)
        bad = check(committed, fresh) + problems
        for x in bad:
            print("BAD:", x)
        print(f"比对了 {len(fresh['capabilities'])} 项能力，问题 {len(bad)} 条")
        return 1 if bad else 0
    for x in problems:
        print("BAD:", x)
    if problems:
        print("有 BAD → 不落盘")
        return 1
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(fresh, fh, ensure_ascii=False, indent=1, sort_keys=True)
        fh.write("\n")
    print("落盘", a.out, "ui_checked =", fresh["ui_checked"],
          "gaps =", len(fresh["gaps"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
