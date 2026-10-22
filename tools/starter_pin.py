#!/usr/bin/env python3
"""入门模型那张 pin 的离线/在线校验与 CI 下载器。

三个动作互相独立：

  * `--check`   只读仓库里的 `pins/starter.json` 与 `pins/payload-plan.json`，
                不联网；URL、revision、sha256、体积、许可证、引擎版本对表。
  * `--live`    用 HF API 复核 revision / 文件体积 / LFS sha256 / 许可证，
                证明"提交在仓库里的那颗"就是"上游现在给的那颗"。
  * `--download` 按 pin 的候选 URL 真下字节，边下边算 sha256，对上才改名；
                这是给 GitHub Actions 用的，本机不跑。

`--selftest` 把每一种明知是错的改法都喂一遍，确认判据会红。
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIN = ROOT / "pins" / "starter.json"
PAYLOAD_PLAN = ROOT / "pins" / "payload-plan.json"

REVISION = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MODEL_ID = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ALLOWED_HOSTS = {"huggingface.co", "hf-mirror.com", "www.modelscope.cn", "modelscope.cn"}
ALLOWED_LICENSES = {"apache-2.0", "mit", "cc-by-4.0"}
MIB = 1024 * 1024
CHUNK = 1 << 20


def load(path: Path = PIN) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _payload_releases(root: Path) -> set[str]:
    plan = json.loads((root / "pins" / "payload-plan.json").read_text(encoding="utf-8"))
    out: set[str] = set()
    for rows in (plan.get("os") or {}).values():
        for row in rows:
            if row.get("engine") == "llama" and row.get("release"):
                out.add(row["release"])
    return out


def _safe_file(name: object) -> bool:
    if not isinstance(name, str) or not name:
        return False
    if name in {".", ".."}:
        return False
    if "/" in name or "\\" in name:
        return False
    if any(ord(c) < 32 for c in name):
        return False
    return name.endswith(".gguf")


def _url_problem(url: object, revision: str, file: str) -> str | None:
    if not isinstance(url, str) or not url:
        return "URL 不是非空字符串"
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        return f"URL 不是 https: {url}"
    if parts.username or parts.password or parts.port:
        return f"URL 不许带账号/端口: {url}"
    if parts.hostname not in ALLOWED_HOSTS:
        return f"URL 主机不在白名单: {parts.hostname}"
    if revision not in parts.path:
        return f"URL 里没有钉住的 revision: {url}"
    if not parts.path.endswith("/" + file):
        return f"URL 末尾不是钉住的文件名: {url}"
    return None


def problems(doc: dict, root: Path = ROOT) -> list[str]:
    bad: list[str] = []
    if doc.get("schema") != 1:
        bad.append(f"schema 是 {doc.get('schema')!r}，只认 1")
    requested = doc.get("requested") or {}
    if requested.get("size") != "0.6B":
        bad.append("requested.size 必须是用户点名的 0.6B（以便把档位差异留在表里）")
    if requested.get("resolved_size") != "0.8B":
        bad.append("requested.resolved_size 必须是 0.8B")
    resolution = requested.get("resolution")
    if not isinstance(resolution, str) or "0.6B" not in resolution or "0.8B" not in resolution:
        bad.append("requested.resolution 必须写明 0.6B 不存在、实际取 0.8B")
    if not isinstance(requested.get("uncensored"), bool):
        bad.append("requested.uncensored 必须是布尔值")

    model = doc.get("model") or {}
    if not MODEL_ID.match(str(model.get("id") or "")):
        bad.append("model.id 必须是 owner/name")
    revision = str(model.get("revision") or "")
    if not REVISION.match(revision):
        bad.append("model.revision 必须是 40 位小写十六进制，不能是 main/latest")
    file = str(model.get("file") or "")
    if not _safe_file(file):
        bad.append("model.file 必须是单层 .gguf 文件名")
    size = model.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        bad.append("model.bytes 必须是正整数")
    sha = str(model.get("sha256") or "")
    if not SHA256.match(sha):
        bad.append("model.sha256 必须是 64 位小写十六进制")
    elif len(set(sha)) == 1:
        bad.append("model.sha256 是单一字符重复（如全零），这是占位符而不是内容哈希")
    if model.get("license") not in ALLOWED_LICENSES:
        bad.append(f"model.license 不在允许清单 {sorted(ALLOWED_LICENSES)}")
    if not str(model.get("base_model") or ""):
        bad.append("model.base_model 不能为空 —— 无限制微调必须能追到基座")
    if not str(model.get("hash_source") or ""):
        bad.append("model.hash_source 不能为空")
    if not isinstance(model.get("notes"), list) or not model["notes"]:
        bad.append("model.notes 必须是非空列表（许可证/内容边界要写清楚）")

    urls = model.get("urls")
    if not isinstance(urls, list) or not urls:
        bad.append("model.urls 必须是非空列表")
    else:
        for url in urls:
            why = _url_problem(url, revision, file)
            if why:
                bad.append(why)

    engine = doc.get("engine") or {}
    releases = _payload_releases(root)
    if engine.get("release") not in releases:
        bad.append(
            f"engine.release {engine.get('release')!r} 不在 payload-plan 的 llama 钉值里: {sorted(releases)}"
        )
    if not str(engine.get("architecture") or ""):
        bad.append("engine.architecture 不能为空")
    if not str(engine.get("architecture_source") or ""):
        bad.append("engine.architecture_source 不能为空")

    fit = doc.get("fit") or {}
    ram = fit.get("ram_mib_min")
    disk = fit.get("disk_mib")
    if isinstance(ram, bool) or not isinstance(ram, int) or ram < 512:
        bad.append("fit.ram_mib_min 必须 >= 512")
    if isinstance(disk, bool) or not isinstance(disk, int) or isinstance(size, bool) or not isinstance(size, int):
        bad.append("fit.disk_mib / model.bytes 必须是整数")
    elif disk * MIB < size:
        bad.append(f"fit.disk_mib={disk} 装不下 {size} 字节")
    if not str(fit.get("because") or ""):
        bad.append("fit.because 不能为空")
    return bad


def _hf_api(model_id: str) -> dict:
    quoted = urllib.parse.quote(model_id, safe="/")
    url = f"https://huggingface.co/api/models/{quoted}?blobs=true"
    req = urllib.request.Request(url, headers={"User-Agent": "local-ai-studio-starter-pin/1"})
    with urllib.request.urlopen(req, timeout=60) as reply:
        return json.load(reply)


def live(doc: dict) -> tuple[list[str], dict]:
    model = doc.get("model") or {}
    bad: list[str] = []
    api = _hf_api(str(model.get("id") or ""))
    if api.get("sha") != model.get("revision"):
        bad.append(f"上游 revision 是 {api.get('sha')}，pin 是 {model.get('revision')}")
    if (api.get("cardData") or {}).get("license") != model.get("license"):
        bad.append(
            f"上游许可证是 {(api.get('cardData') or {}).get('license')!r}，pin 是 {model.get('license')!r}"
        )
    sibling = next(
        (row for row in api.get("siblings") or [] if row.get("rfilename") == model.get("file")),
        None,
    )
    report = {
        "id": model.get("id"),
        "revision": api.get("sha"),
        "file": model.get("file"),
        "bytes": None,
        "sha256": None,
        "license": (api.get("cardData") or {}).get("license"),
    }
    if sibling is None:
        bad.append(f"上游仓库里没有 {model.get('file')!r}")
        return bad, report
    lfs = sibling.get("lfs") or {}
    report["bytes"] = sibling.get("size")
    report["sha256"] = lfs.get("sha256")
    if sibling.get("size") != model.get("bytes"):
        bad.append(f"上游体积 {sibling.get('size')} ≠ pin {model.get('bytes')}")
    if lfs.get("sha256") != model.get("sha256"):
        bad.append(f"上游 LFS sha256 {lfs.get('sha256')} ≠ pin {model.get('sha256')}")
    return bad, report


def _verify(path: Path, size: int, sha: str) -> tuple[bool, str]:
    if not path.is_file():
        return False, "文件不在"
    if path.stat().st_size != size:
        return False, f"体积 {path.stat().st_size} ≠ {size}"
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    got = digest.hexdigest()
    return got == sha, f"sha256 {got}"


def download(doc: dict, dest_dir: Path) -> Path:
    model = doc["model"]
    size = int(model["bytes"])
    sha = str(model["sha256"])
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / model["file"]
    ok, why = _verify(target, size, sha)
    if ok:
        print(f"已存在且哈希对得上: {target}")
        return target
    if target.exists():
        aside = target.with_name(target.name + ".corrupt")
        os.replace(target, aside)
        print(f"同名文件哈希不符，挪到 {aside}")
    part = target.with_name(target.name + ".part")
    failures: list[str] = []
    for url in model["urls"]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "local-ai-studio-starter-pin/1"})
            digest = hashlib.sha256()
            total = 0
            with urllib.request.urlopen(req, timeout=180) as reply, open(part, "wb") as out:
                declared = reply.headers.get("Content-Length")
                if declared is not None and int(declared) not in (size, size + 1):
                    raise RuntimeError(f"Content-Length={declared}，期望 {size}")
                for block in iter(lambda: reply.read(CHUNK), b""):
                    out.write(block)
                    digest.update(block)
                    total += len(block)
                    if total % (64 * MIB) < CHUNK:
                        print(f"  {total // MIB} MiB / {size // MIB} MiB", file=sys.stderr)
            if total != size:
                raise RuntimeError(f"落地 {total} 字节 ≠ {size}")
            if digest.hexdigest() != sha:
                raise RuntimeError(f"sha256 {digest.hexdigest()} ≠ {sha}")
            os.replace(part, target)
            print(f"下载并校验通过: {target} 来源 {url}")
            return target
        except Exception as exc:  # noqa: BLE001 - 每个候选源都要换下一条
            failures.append(f"{url}: {exc}")
            try:
                part.unlink()
            except FileNotFoundError:
                pass
    raise SystemExit("所有候选源都没下成:\n  " + "\n  ".join(failures))


def selftest() -> int:
    base = load()
    base_bad = problems(base)
    if base_bad:
        print("已提交的 pin 自检不过：", file=sys.stderr)
        for item in base_bad:
            print("  BAD:", item, file=sys.stderr)
        return 1

    def mutate(name: str, fn) -> None:
        doc = copy.deepcopy(base)
        fn(doc)
        if not problems(doc):
            raise AssertionError(f"负向对照「{name}」没被抓住")

    mutate("sha256 改成全零", lambda d: d["model"].__setitem__("sha256", "0" * 64))
    mutate("revision 改成 main", lambda d: d["model"].__setitem__("revision", "main"))
    mutate("URL 改成 http", lambda d: d["model"]["urls"].__setitem__(0, d["model"]["urls"][0].replace("https://", "http://", 1)))
    mutate("URL 去掉 revision", lambda d: d["model"]["urls"].__setitem__(0, d["model"]["urls"][0].replace(d["model"]["revision"] + "/", "")))
    mutate("文件名带目录", lambda d: d["model"].__setitem__("file", "../x.gguf"))
    mutate("体积改成 0", lambda d: d["model"].__setitem__("bytes", 0))
    mutate("引擎版本改成 b0", lambda d: d["engine"].__setitem__("release", "b0"))
    mutate("许可证改成 gpl-3.0", lambda d: d["model"].__setitem__("license", "gpl-3.0"))
    mutate("删掉档位差异说明", lambda d: d["requested"].__setitem__("resolution", "0.6B 存在"))
    mutate("磁盘预算装不下", lambda d: d["fit"].__setitem__("disk_mib", 1))
    mutate("基座为空", lambda d: d["model"].__setitem__("base_model", ""))
    print(f"self-test ok（{len(base['model']['urls'])} 个候选源，{base['model']['bytes']} 字节）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="离线校验已提交的 pin")
    parser.add_argument("--live", action="store_true", help="用 HF API 复核 revision/体积/哈希/许可证")
    parser.add_argument("--download", metavar="DIR", help="按 pin 下载权重复算 sha256（CI 用）")
    parser.add_argument("--selftest", action="store_true", help="负向对照")
    args = parser.parse_args(argv)
    doc = load()
    rc = 0
    if args.check or not (args.live or args.download or args.selftest):
        bad = problems(doc)
        if bad:
            rc = 1
            for item in bad:
                print("BAD:", item, file=sys.stderr)
        else:
            print(f"pin ok: {doc['model']['file']} {doc['model']['bytes']} B {doc['model']['sha256']}")
    if args.selftest:
        try:
            rc |= selftest()
        except AssertionError as exc:
            print("BAD:", exc, file=sys.stderr)
            rc = 1
    if args.live:
        bad, report = live(doc)
        print(json.dumps(report, ensure_ascii=False))
        if bad:
            rc = 1
            for item in bad:
                print("BAD:", item, file=sys.stderr)
        else:
            print("live ok: 上游 revision/体积/LFS sha256/许可证与 pin 一致")
    if args.download:
        try:
            download(doc, Path(args.download))
        except SystemExit as exc:
            print(str(exc), file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
