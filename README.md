# local-ai-studio（占位名）

把普通人自己的电脑用起来：在一台机器上跑本地 AI 能力 —— 对话、文字识别、字幕生成、生图。
对标 LM Studio，但引擎一律不自研。

## 两条硬约束

1. **开发者机器不编译、不运行**：所有构建与验证只在 GitHub Actions。
2. **引擎不自研**：对话/字幕/生图直接下载上游预编译产物 —— llama.cpp、whisper.cpp、stable-diffusion.cpp（都 MIT）；
   OCR 用 `oar-ocr`（Apache-2.0），它是四项能力里**唯一没有上游二进制**的，只能由 CI `cargo build`。
   权重运行时按需下载并做 sha256 校验，安装包不塞权重。

依据与取舍见 [docs/adr/0001-engines-from-upstream-binaries.md](docs/adr/0001-engines-from-upstream-binaries.md)。

## 现在有什么

- `tools/engine_probe.py`：吸收三家命名差异（`cuda-12.4` vs `cuda-12.4.0` vs 名字里带 `Ubuntu-24.04`，
  sd 的文件名只带 sha 不带 tag 里的 `975-`），按"引擎 + 平台 + 后端族 + 架构"选 target；
  同 target 冲突先比 release 构建号再比后端版本；**不看 `releases/latest`**（whisper.cpp 的 latest 是 0 产物）。
  名字里没写 OS 的（whisper 那两个）只**推断**成 win，并单列进 `manifest["inferred"]`。
- `tools/zip_peek.py`：用 HTTP Range 只读 zip 中央目录，不下载整包、不解压、不运行，
  把上面那个推断换成条目级证据；带一正一负对照，见 ADR。
- `tools/yaml_gate.py`：解析两条 workflow，核对 needs 链、step 输出引用、heredoc 闭合，
  以及车道里写死的 target 名确实存在于 manifest（已用假 target 验证它会红）。
- `.github/workflows/ci.yml`：静态门禁（含 yaml_gate）+ 单测（夹具是 2026-10-18 真实 API 捕获）+ 离线跑发现器。
- `.github/workflows/engine-live.yml`：打真实 API 发现产物 → 下载 linux 引擎（不编译）→ 下载 pin 住的
  0.5B GGUF 并校 sha256 → 无头推理，要求真的抓到 `tokens per second > 0`；
  另有 os-inference-proof 逐条解压 `manifest["inferred"]` 里的产物，要求里面有 `.exe/.dll`。

## 这套 CI 能证明什么、不能证明什么

能证明：上游产物可发现、可下载、校验对得上、在无 GPU 的 runner 上真能产出 token。
**不能证明：图形界面真的打得开。** 桌面外壳交付时必须另找设备验证，不拿 CI 绿当"能用"。


## 待定

仓库名 / public-private / 外壳许可（AGPL-3.0-or-later 还是 MIT）/ 模型目录来源 / i18n 语言集。
