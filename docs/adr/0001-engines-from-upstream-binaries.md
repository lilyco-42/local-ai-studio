# ADR-0001：引擎一律采用上游预编译产物，只自研外壳

日期：2026-10-18 · 状态：已采纳（待 CI 实测确认）

## 背景与硬约束

产品是"普通人电脑上的本地 AI 工具箱"（对话 / OCR / 字幕 / 生图），对标 LM Studio。
约束：**开发者机器不编译、不运行**，一切构建与验证只在 GitHub Actions。

## 决策

自研的只有 Tauri 2 外壳（i18n、任务编排、模型下载与校验）；推理引擎一律使用上游发布的
**预编译二进制**，运行时按需下载并做 sha256 校验。自己一行推理代码都不写。

## 依据（2026-10-18 用 gh/crates/HF API 实测，非印象）

| 引擎 | 仓库 | 许可 / 星数 | 预编译产物 |
|---|---|---|---|
| 对话 | ggml-org/llama.cpp | MIT · 131,593★ | 每个 nightly **36 个**产物，含 win-cpu-x64、win-cuda-12.4/13.4、ubuntu-x64、ubuntu-vulkan、macos-arm64、android、rocm、sycl、openvino |
| 字幕 | ggml-org/whisper.cpp | MIT · 54,465★ | 12 个产物，**但只挂在 `b5454` 这类 release 上；`releases/latest`（v1.9.5）产物数为 0** |
| 生图 | leejet/stable-diffusion.cpp | MIT · 7,719★（当天仍在推） | 9 个产物：win-cpu 16.9MB、Linux 25.1MB、macOS-arm64 34.8MB、cuda/rocm/vulkan |
| OCR | crates.io `paddle-ocr-rs` | 0.6.1，累计下载 25,261 | 纯 Rust 路线，避免为 OCR 引入 Python 运行时 |

排除项（许可是硬约束，不是偏好）：ComfyUI **GPL-3.0**（138,288★）、StabilityMatrix **AGPL-3.0**、
Umi-OCR 为 Windows 专有分发形态 —— 不进安装包。

现成壳覆盖度：LM Studio 本体闭源（开源的只有 `lms` 5,355★ CLI 与 `lmstudio-js`），Jan 44,995★ 许可为
`other`，GPT4All 77,349★ 已停滞 17 个月；都不覆盖"四合一 + i18n + 纯 MIT/Apache 引擎"，约 70%。

## 命名漂移的处置（这是本决策最大的风险）

三家命名规则互不相同，且版本号写进文件名：llama 用 `cuda-12.4` / `cuda-13.4`（两段），
whisper 用 `cuda-12.4.0`（三段），sd.cpp 连 OS 版本都写进名字（`Linux-Ubuntu-24.04`、
`Darwin-macOS-26.6.2`）。因此：

1. 禁止写死完整 URL 或完整文件名；只按"引擎 + 平台 + 后端族 + 架构"选择 target。
2. 冲突时的取谁：**先比 release 构建号（新→旧），再比后端版本**。
   实测反例：whisper `b5454` 写 `cuda-13.4`、`b5127` 写 `cuda-13.4.0`，
   只比版本段数会让旧构建顶掉新构建。
3. 永远遍历 release 列表并跳过零产物的 release，不看 `latest`。
4. `cudart-*` 运行时支持包必须单独成 target：实测 `llama-ubuntu-cuda-x64` 会被
   `cudart-llama-b11719-…` 抢先命中，装上的是 CUDA 运行时而不是引擎本体。
5. 名字里不写 OS 的产物（`whisper-bin-x64.zip`、`whisper-blas-bin-x64.zip`）只**推断**成 win，
   并写进 `manifest["inferred"]`；证据由 `engine-live.yml` 的 os-inference-proof 逐条解压给。
   本机不下载整包也能拿到一半证据：`tools/zip_peek.py` 用 Range 只读 zip 中央目录，
   看条目里是 `.exe/.dll` 还是 `.so`。
6. 明确不进表的形态：`*-ui.tar.gz`（非平台二进制）、`*-xcframework.zip`（给 Xcode 嵌入用，
   不能起进程）、`*-s390x.tar.gz`（IBM 主机）。**whisper 因此没有 macOS 可用产物**，
   mac 侧字幕只能由 CI 自编，不假装上游给了。
7. `tools/engine_probe.py` 的夹具是当天真实 API 捕获，`tests/` 含**三条负向对照**
   （删产物必须报缺失、整族消失必须报缺失、`check()` 已加架构维度）和一条
   **全表遍历**（除上面第 6 条列明的形态外，任何上游产物都必须被解析出来）。
   `tools/_yaml_gate.py` 会核对车道里写死的 target 名确实存在于 manifest，
   并已用 `llama-ubuntu-base-x99` 验证这条闸门会红。

## 本地测试暴露的五个映射缺陷（2026-10-18，全部有对应回归测试）

| 缺陷 | 后果 | 修法 |
|---|---|---|
| sd 文件名只带 sha（`sd-master-95b3090-…`），tag 却是 `master-975-95b3090` | 整族 sd 静默消失，表里只剩一个 cudart 包 | `975-` 改成可选 |
| `cudart-llama-bin-win-…`（没有构建号） | 正则把连字符写在可选组外面，这类全漏 | `(?:-b\d+)?` |
| whisper 的 `cublas-<ver>-bin-` / `blas-bin-` 把后端写在 `bin` 之前 | 解析成 base，和真 CPU 包抢同一 target，CUDA 构建被当成 CPU 构建 | 各加一条前置模式并强制后端 |
| `check()` 只比到后端，不比架构 | 删光 win-**x64** 仍不报缺失（arm64 顶着），负向对照形同摆设 | REQUIRED/GPU 改成 (后端, 架构) |
| unknown OS 一律兜底成 win | `whisper-*-xcframework.zip` 变成 `whisper-win-xcframework-?` | 兜底只对 whisper 生效并打 `inferred_platform`，其余丢弃 |

## OS 推断的证据（2026-10-19，`tools/zip_peek.py` Range 读中央目录）

本机不能跑二进制，但可以只读 zip 的中央目录（每个包只花两次小请求、≤1 MB），
所以推断在提交给 CI 之前就已经有一条独立证据，而且带一正一负两个对照。
复现：`python tools/engine_probe.py --fixtures tests/fixtures --json m.json` 取 URL，再
`python tools/zip_peek.py "<URL>"`（每个包只发两个 Range 请求：`0-0` 换总长、尾段读中央目录）。

| target | 文件名 | 条目 | 判定 |
|---|---|---|---|
| `whisper-win-base-x86`（名字明写 Win32） | whisper-bin-Win32.zip | 33 | win，PE 33、无 .so/.dylib ← **正向对照** |
| `sd-ubuntu-base-x64`（名字明写 Linux） | sd-master-95b3090-bin-Linux-Ubuntu-24.04-x86_64.zip | 35 | linux，.so 31、无 PE ← **负向对照：分类器不是永远说 win** |
| `llama-win-cpu-x64`（名字明写 win） | llama-b11719-bin-win-cpu-x64.zip | 51 | win，PE 50 |
| `whisper-win-base-x64`（**名字没写 OS**） | whisper-bin-x64.zip | 41 | win，PE 41 → 推断成立 |
| `whisper-win-blas-x64`（**名字没写 OS**） | whisper-blas-bin-x64.zip | 43 | win，PE 43 → 推断成立 |

顺带量到一个会影响外壳取路径的布局差异：**whisper 的包把可执行文件全放在 `Release/` 子目录里**
（`Release/main.exe`、`Release/ggml-base.dll`），而 llama.cpp 与 sd.cpp 的包是平铺在根目录
（`ggml-base.dll`、`libggml-base.so.0.27.0`）。CI 无头车道与外壳定位引擎都必须按深度 1–2 glob，
不能假设 `解压根/可执行名`。

## 后果

- 安装包不含引擎与权重（CUDA 包 146–566MB），运行时下载 + sha256 pin；离线首装需要网络。
- 上游改名或换布局会在 CI 里直接变红，而不是等用户报 404 —— 这是把风险换成构建负担的自觉交易。
- 无头链路能被 CI 证明；**GUI 是否真能打开无法由 CI 证明**，交付说明里必须写清这一点。

## 待定（不阻塞本车道）

外壳许可（AGPL-3.0-or-later 还是 MIT）、仓库名与 public/private、模型目录来源（HF 索引 vs 自建清单）、
i18n 语言集。
