# ADR-0001：引擎一律采用上游预编译产物，只自研外壳

日期：2026-10-18（2026-10-19 因 OCR 实测改写）· 状态：已采纳，**但还没有一条 CI 证据**（见文末"当前阻塞"）

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
| OCR | crates.io `oar-ocr`（GreatV/oar-ocr） | Apache-2.0 · 186★ · 最后 push 2026-10-17 · 0.10.0（2026-10-03）· 累计下载 323,157 | **没有上游预编译二进制**，只能由 CI `cargo build`；模型是 release 里的散装 `.onnx`（见下节） |

因许可排除（硬约束，不是偏好）：ComfyUI **GPL-3.0**（138,288★）、StabilityMatrix **AGPL-3.0** —— 不进安装包。

**更正一处**：本 ADR 早先把 Umi-OCR 写成"Windows 专有分发形态"而按许可排除，那是没核实的话。
实测 `hiroi-sora/Umi-OCR` 是 **MIT**、48,010★、最后 push 2025-11-20，所以许可根本不是理由。
它不进包的真实原因是形态：那是个 Python 桌面应用（自带便携运行时），不是一个能被外壳调用的引擎库。
反过来说，MIT 使它成为**可以直接读源码的参考实现**（截图取字、批量导入、排除水印这些交互正是要对标 LM Studio
体验的地方），按"有先例就克隆研究具体实现"处理，不当作竞争者。

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

## OCR 一项把"一律用上游预编译"打破了（2026-10-19 实测后改写）

原表里 OCR 那行写的是 `paddle-ocr-rs`，依据只是 crates.io 的一个下载数——不够。
按同一家族做横向对比后（全部为 API/registry 实测，非印象）：

| 候选 | 许可 | 活跃度 | 关键差异 |
|---|---|---|---|
| `oar-ocr` 0.10.0 | Apache-2.0 | 186★，**2026-10-17 仍在推**，323,157 下载 | 裁剪多边形用 `clipper2-rust`（纯 Rust）；GPU 后端除 CUDA 外还有 **DirectML**、CoreML、OpenVINO、TensorRT |
| `paddle-ocr-rs` 0.6.1 | 见仓库 | 最后发布 **2025-08-31**，25,261 下载，仓库 95★ | 依赖 `clipper-sys`（C++ 绑定，交叉编译要多装一套） |

两者都走 ONNX Runtime（`ort`），所以"避免引入 Python 运行时"这条原依据站不住——它们本来都不是 Python。
换成 `oar-ocr` 的真实理由是：**维护状态 + 纯 Rust 的几何库 + DirectML**。

DirectML 对这款产品不是锦上添花：目标用户是"普通人已有的电脑"，多数是带 Intel/AMD 集显的杂牌 Windows 本，
没有 CUDA。而 llama.cpp / whisper.cpp / sd.cpp 的上游预编译包只给 cuda / rocm / vulkan，
**OCR 这一路反而拿到了 DirectX 12 后端**。

### 打破的假设与代价

1. 对话/字幕/生图三项能直接下载上游二进制；**OCR 必须让 CI `cargo build --release` 三个 OS**。
   `oar-ocr-core` 依赖 `tokenizers` 且开了 `onig`（oniguruma，C 库），所以 runner 必须有 C 工具链——
   ubuntu/macos/windows 的标准镜像都有，但交叉编译目标（如 arm64 Linux）不会白来。
2. `oar-ocr-core` 把 ort 钉成 **`ort = "=2.0.0-rc.13"`（精确等号、且是 RC）**：上游一次 rc 版本变动就可能断构建。
   好处是 `download-binaries` 是默认 feature，CI 不用手工装 ONNX Runtime；
   它另开了 `copy-dylibs`，编出来的产物旁边就带着 dylib，正好适合安装包收集。
3. `auto-download` 默认从 **ModelScope** 拉模型（对中国用户比 Hugging Face 友好），
   但我们的设计仍然是"自己 pin 后从自家镜像下"，所以只把它当兜底，不作为 pin 的来源。

### EP 特性表与两条下载路（v0.10.0 的 Cargo.toml 实测，2026-10-19）

`[features]` 里执行后端是一等公民，全部转发给 ort：`cuda / tensorrt / directml / coreml / webgpu / openvino`；
依赖写死 `ort = { version = "=2.0.0-rc.13", default-features = false, features = ["std","ndarray","tracing","copy-dylibs"] }`。
三条后果：

1. **"没有 CUDA 的集显 Windows 本也有非 CPU 路径"这句现在有出处** —— `directml` 和 `webgpu` 都是可开特性。
   但这只证明**特性存在**，不证明"开了就快"；速度必须上设备矩阵，CI 的 Linux runner 给不出这个结论。
2. `default = ["download-binaries", "simd"]`，且 `download-binaries = ["ort/download-binaries","ort/tls-native"]`
   → **构建过程会去下载 ONNX Runtime 二进制**。好消息是取哪一版由 `=2.0.0-rc.13` 这个精确版本决定，
   所以我们钉 crate 版本 + `Cargo.lock` 就等于把 ORT 一起钉住了；坏消息是这条必须在 CI 里联网，
   OCR 车道不能假设离线可建。
3. `auto-download`（**不在** default 里）是运行时从 ModelScope 拉模型进 `$OAR_HOME`/`~/.oar`，
   而且**自己校 SHA-256**。这与我们的目录规则同源（内容寻址 + 下载后复核），
   所以 sideload 与自取两类可以并存；但对我们而言 pin 的权威仍然在 `pins/ocr.json` 这一侧，
   `auto-download` 只当兜底路径，不当哈希来源。

### OCR 模型的可钉性（实测）

`docs/models.md` 里链接的 **93 个模型 URL，逐个都能在 release 产物里找到，0 缺失**。
检查器是 `tools/ocr_catalog_check.py`（owner 大小写归一后比对；复现：
`python tools/ocr_catalog_check.py --models-md <models.md>`），并且验过它不是摆设：
往 models.md 里掺一条不存在的 URL，链接数 93→94、缺失 1、退出码 1。
产物共 119 个，其中 `.onnx` 94 个、合计 8.6 GiB：

- 移动端 det 只有 **4.6 MiB**，rec 7.4–15.8 MiB → OCR 是四项能力里最轻的，可以随首装直接下齐；
- 服务端 det/rec 80–108 MiB；最大的 `unimernet.onnx` **1,756 MiB**（公式识别，绝不默认下载）。

**更正（2026-10-19 晚）**：这一节原先写"GitHub release 只给 size、没有官方 sha256，所以 pin 必须 CI 自己算"，
那是错的。release 产物的 API 里有 `digest` 字段，形如 `sha256:<64 位十六进制>`，而且**实测就是内容哈希**：
`whisper-bin-x64.zip`（8,928,640 B）与 `sd-master-95b3090-bin-win-cpu-x64.zip`（17,706,677 B）下载后本地算
sha256，与 API 的 digest 逐字节相等。三家引擎每个产物都有（llama 216/216、whisper 36/36、sd 54/54），
OCR 权重同样全带（`v0.7.0` 8/8、`v0.6.0` 1/1，全部 119 个产物 0 个缺）。
所以引擎与 OCR 权重这一层的 pin **有上游来源**，CI 的角色变成"核对"而不是"首次计算"；
`engine_probe` 已把 digest 带进 manifest，缺任何一个就 `pin_missing` 点名并非零退出
——不能让它静默退回"不校验"。仍需自己算的是 R2 与 HF 那两侧（见"分发源可达性"）。

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

## 生图在"普通人电脑"上的现实（2026-10-19；依据是检索到的第三方数字，不是本机实测）

先说这条能力的证据质量：`stable-diffusion.cpp` 自己的 README **没有写任何显存/内存下限，也没有一条基准数字**，
只列了支持面（SD1/2、SDXL、SD3、Flux 1–3、MOSS/Qwen/Wan/LTX/Hunyuan；LoRA、ControlNet、TAESD、ESRGAN、
VAE tiling；GGUF/Safetensors/CKPT；后端 CPU(AVX/AVX512)、CUDA、Vulkan、Metal、OpenCL、SYCL），
可执行文件叫 `sd-cli`，并且明写 **"API and command-line option may change frequently"**
→ 外壳必须按精确 release 钉它，不能跟着 master 漂。

第三方数字（各自的日期与强度都标出来，别当定律）：

| 来源 | 数字 | 强度 |
|---|---|---|
| HN 上 sd.cpp 作者早期报告（2023-08） | CPU 每 iteration 8.5–9 s | 代码早了三年，只能当量级 |
| 2026 本地生图硬件指南 | SD1.5 @512×512 CPU **5–10 分钟/张**，GPU 3–5 秒；SDXL CPU 更差 | 泛用指南，未指明 sd.cpp |
| 同指南 / thundercompute | SD1.5 ≥4 GB 显存、SDXL ≥8 GB | 常规经验值 |
| FastSD CPU（另一条 CPU 路线） | i7-12700 出 512×512，但**要 ~30 GB 系统内存** | 不同实现，不可混用 |

**但我们自己的产物表已经把最坏假设削掉一半**：`engine_probe` 实测 sd.cpp 每个 release 都发
`sd-win-vulkan-x64`、`sd-ubuntu-vulkan-x64`（还有 rocm 两个），也就是**带集显/DX12 的普通笔记本可以走
Vulkan，不必退回 CPU 的"几分钟一张"**。所以这条能力的设计不是"要不要砍掉生图"，而是：

1. **按检测到的后端分层**：有 Vulkan/DirectML/CUDA → 正常默认（含 SDXL 级）；只有 CPU → 保留入口但
   明确写"这张图预计 5–10 分钟"，默认降到量化后的 SD1.5 级模型。不要给用户一个"点了没反应"的按钮。
2. **CI 证明不了这件事**：ubuntu-latest 是 2 核无 GPU，跑出来的 CPU 数字既不代表集显也不代表独显。
   CI 只能证明"`sd-cli` 能起来、能出一张能校验的 PNG"（冒烟），真实性能必须靠设备矩阵，
   和"GUI 能不能打开"是同一类无法在 CI 里证完的东西。
3. 上面这些数字要在能跑 CI 之后由车道重新量一遍；现在它们只是**用来决定功能分层的依据**，不是承诺给用户的性能。

## 包内到底有什么（2026-10-19，`zip_peek` 读三个包的中央目录）

外壳怎么调引擎、装机要带哪些文件，下面五条结论全部来自条目表与钉住 ref 的源码，不是来自 README：

| 包 | 条目 | 关键事实 |
|---|---|---|
| `llama-win-cpu-x64.zip` | 51 | `llama-cli.exe`、**`llama-server.exe`**、`llama.exe`、`llama-completion.exe`、`llama-bench.exe`、`llama-tts.exe`、`llama-quantize.exe`、`llama-mtmd-cli.exe`/`llava`/`qwen2vl`（多模态）、`ggml-rpc-server.exe`。**没有 `llama-simple`，也没有 `llama-completions`** |
| `whisper-bin-x64.zip` | 41 | 全在 `Release/` 子目录；`whisper-cli.exe` **和** `main.exe` 同时存在，另有 **`whisper-server.exe`**、**`whisper-vad-speech-segments.exe`**、`whisper-quantize/bench/stream/talk-llama/lsp.exe`，以及 8 个 `test-*.exe`、`wchess.exe`、`parakeet-*.exe`、`SDL2.dll` |
| `sd-...-Linux-Ubuntu-24.04-x86_64.zip` | 35 | 平铺根目录：`sd-cli`、**`sd-server`**（无扩展名），外加一整层运行时闭包 `libggml{,-base}.so`（含 `.so.0.27.0` 版本名）、14 个 `libggml-cpu-<微架构>.so`、`libstable-diffusion.so`、`libwebp/libwebpmux/libsharpyuv/libwebm.so`，两个 `.txt` 出处说明 |
| `llama-b11727-bin-ubuntu-x64.tar.gz` | 61 | **Linux 包比 Windows 多一层同名目录** `llama-b11727/`：里面有 `llama-server`、`libllama.so`（+`.so.0.7.0`）、21 个 `libggml-cpu-*.so`。整包下回并算 sha256 == 上游 digest ✓ |
| `whisper-bin-ubuntu-x64.tar.gz` | 47 | 同样多一层目录 `whisper-bin-ubuntu-x64/`：`whisper-cli`、`whisper-server`、`libwhisper.so`（+`.so.1`）。整包 sha256 == 上游 digest ✓ —— 车道里 `find -maxdepth 2` 正是为这一层写的 |

（这三行里 sd 那条与上表同源：早先用 `zip_peek` 只读中央目录；本轮改成**整包下回算 sha256**，
三个包的实算哈希都与上游 `digest` 逐字节相等 → 又添三条"GitHub 的 digest 确实是内容哈希"的证据，
而不只是那两个早期样本。）

由此定下五条实现约束：

1. **对话、字幕、生图三项上游都带 server 形态**（`llama-server` / `whisper-server` / `sd-server`）。
   外壳应当**起一个常驻服务**，而不是一次任务起一次进程：400 MB 权重每次重新加载，
   在 0.8–1 MiB/s 的下载与秒级加载面前是不可接受的。这也正好是 LM Studio 的形态（本地 OpenAI 兼容接口）。
   2026-10-19 补：这条决定原本四条能力车道**一条都没碰过**（全是 one-shot CLI），现在
   `headless-inference` 多了一步真起 `llama-server` 并走 `/v1/chat/completions` 拿非空文本 ——
   判据的路由与字段名按 `b11727` 源码核过（见 requirements.md 的出处表），但**这一步同样一次都没跑过**，
   所以它证明的是"这个形状有车道了"，不是"常驻服务在这台 runner 上起得来"。
   whisper / sd 两个 server 形态仍然只有包内条目表作证，没有车道。
2. **`llama-bench` 已经在包里** → "给你这台机器实测多少 tok/s"这类 LM Studio 式能力，
   不需要自己写基准，直接调上游产物即可。
3. **字幕这条不需要自研分段**：`whisper-cli` 的 `-osrt, --output-srt` 在
   `examples/cli/cli.cpp` 里能查到（`arg == "-osrt" || arg == "--output-srt"`），
   而 VAD 断句工具 `whisper-vad-speech-segments.exe` 既在 `examples/vad-speech-segments/` 里，
   也真的出现在 `whisper-bin-x64.zip` 的条目表里 → 出 SRT 的现成路径是"VAD 分段 + `-osrt`"，
   外壳只做编排与进度显示。
4. 打包**不能只拷可执行文件**：llama 是 `llama-cli.exe` + `llama-cli-impl.dll` 成对，whisper 的 `Release/` 里
   混着 8 个测试程序和 `SDL2.dll`，sd 的 linux 包依赖一整套 `.so` 闭包（还带版本号软链名）。
   所以按"整目录收集 + 显式排除 `test-*`"处理，运行时库目录要靠 `$ORIGIN`/`LD_LIBRARY_PATH`；
   而 CI 无头车道里那句 `find -name llama-completions` 找的是**不存在的文件名**，已按实测条目表改掉。
5. **生图产物自带生成参数**，这是一件白送的能力：上游把 `get_image_params()` 的结果
   经 `stbi_write_png(..., parameters)` 写成 PNG 的 `tEXt` 块（keyword=`parameters`，明文），
   内含 `Steps:`、`CFG scale:`、`Seed:`、`Size: WxH`、`Model:`、`Version:` 等
   （`common.cpp:3239/3269` 拼串，`thirdparty/stb_image_write.h:1210` 落成块；
   默认开，只有 `--disable-image-metadata` 会关）。
   所以"用户拿一张图回来想知道是怎么生成的"这种功能不必自己记会话，读图就行；
   生图车道的判据也据此改成读 IHDR + `tEXt`（并要求两者尺寸一致），
   而不是像我最初写的那样去 grep `-v` 日志 —— **日志里根本没有这两行**。

旗标面已按**钉住的那个 ref** 重读一遍，不是按 HEAD 记的：whisper `b5454` 的 `examples/cli/cli.cpp`
里 `-t/--threads`、`-of/--output-file`、`-osrt/--output-srt`、`-l/--language`、`-m`、`-f` 全在；
sd `master-975-95b3090` 的 `--steps/--width/--height/--sampling-method/--diffusion-model/-v` 全在。
（同一天 OCR 那条就是因为按 HEAD 读成员表、却钉了 tag，才发现 tag 里根本没有 CLI —— 见 `8b70b91`。）

## 三个 server 的接口与默认安全形状（2026-10-20 按 pin 住的 ref 读源码，不是按 OpenAI/A1111 文档猜的）

上面第 1 条约束说"外壳起常驻服务"，但当时只有条目表证明这三个二进制存在。这一节把"真起起来要调什么、
默认安不安全"读出来，出处都是钉住的 ref 的行号：

| 事项 | llama.cpp `b11727` | whisper.cpp `b5454` | sd.cpp `95b3090…` |
|---|---|---|---|
| 就绪探测 | `GET /health`（`tools/server/server.cpp:263`） | `GET /health`（`examples/server/server.cpp:1218-1227`）：**未就绪回 503** + `{"status":"loading model"}` | 没有 /health；`GET /sdapi/v1/progress`（`api.md:282-299`）能间接看忙闲 |
| 默认监听 | `hostnames = {"127.0.0.1"}`（`common/common.h:648`） | `127.0.0.1:8080`（`server.cpp:59/65`） | `127.0.0.1:1234`（`examples/server/runtime.h:22-23`） |
| 鉴权 | `--api-key`（`common/arg.cpp:3513-3514`），**默认为空**（`common.h:665`） | **没有任何 key/auth**（全文无此字段） | **同样没有** |
| CORS | `cors_origins = "*"` 是默认（`common.h:652`）；`*` + 无 key 时上游只**警告**（`server.cpp:340-342`） | `Access-Control-Allow-Origin: *`（`server.cpp:738-740`，allow-headers 里写着 `authorization` 但没有代码校验它） | pre-routing 把请求的 `Origin` **原样反射**进 ACAO，并配 `Allow-Credentials: true`、Methods/Headers `*`（`examples/server/main.cpp:123-131`） |
| 主调用 | `POST /v1/chat/completions`（`server.cpp:274`）→ `choices[].message.content`（`server-task.cpp:422/434`） | `POST /inference` **multipart**（字段 `file`，`server.cpp:826/839`） | `POST /sdcpp/v1/img_gen`、A1111 的 `POST /sdapi/v1/txt2img`、OpenAI 的 `POST /v1/images/generations` → `data[].b64_json`（`api.md:28/38/56/132`） |
| 白送的能力 | `/props`（运行时真值） | **SRT 由服务端渲染**：`response_format=srt` 逐段拼 `序号 / HH:MM:SS,mmm --> / 文本`，content-type `application/x-subrip`，支持 `offset_n` 与 diarize（`server.cpp:1022-1042`）；`verbose_json` 给 `segments[].text/from/to/words/no_speech_prob`（`1065-1156`）；`POST /load` 热切换权重（字段 `model` 是**文件路径**，`1175-1200`） | **job 模型 + 取消 + 实时预览**：`queued/generating/completed/failed/cancelled` + `queue_position`（`api.md:393-420`）、`GET /sdcpp/v1/jobs/{id}` 带 `preview.b64_json`（边采样边更新）、`POST /sdcpp/v1/jobs/{id}/cancel`、`GET /sdcpp/v1/capabilities` 的 `features_by_mode`（`lora/hires/vae_tiling/cache/cancel_*/preview`，`api.md:630-650`） |

四条设计后果：

1. **"默认安全"只在绑定这条轴上成立**。三家都只绑 loopback，但一个无 key + `*` CORS 的本地接口
   意味着用户随便打开的网页就能驱动这台机器上的模型 —— 对"无审查对话"这项目标尤其是要防的。
   所以外壳必须自己占住唯一监听口：llama 那条要显式给 `--api-key` 并把 `--cors-origins` 收窄到应用来源；
   whisper / sd **没有 key 可给**，只能"按需起、用完停"，或由外壳反代（外壳持有唯一端口并自己发 token）。
   这不是上游缺陷清单，是我们的实现约束，写进 requirements 的验收里。
2. **三家不是一个接口形状**。multipart 上传（whisper）与 JSON chat（llama）与 JSON job（sd）互不相同，
   "统一成一个 OpenAI 兼容网关"的想法只在对话那侧成立 —— UI/调度抽象要按"每能力一个本地服务"设计。
3. **功能分层有可探测的出处**：sd 的 `capabilities` 与 llama 的 `/props` 是运行时真值，
   "支持不支持 lora/hires/preview/取消"不许写死在代码里（此前按后端检测分层只解决了一半）。
4. **车道只覆盖了 llama 那条**（`headless-inference#4`）。whisper-server / sd-server 这一节**仍是源码级证据**，
   一次都没跑过；要不要给它们补车道，等 CI 解锁后看第一次真跑的耗时再定。
   但"这一节的行号还成不成立"不必等 CI：`pins/citations.json` 里三家共 21 条 `file:line` 声明由
   `citation-drift` 车道逐条核对**那一行的内容**（`tools/citation_check.py`，子串在文件里命中不唯一就不配进表）。
   上游挪行或改默认值会红，红字带上游实际那一行写了什么。

## 分发源可达性（2026-10-19 从 CN 服务器 lain42.top 实测：HEAD 之外再做区间 GET，2 MiB 一组、4 MiB 重复三轮）

第一次只发 HEAD 得到的结论有两处不能信：ModelScope 回 200 其实跳到了仓库页面，
`dl.lilyco42.top` 两条都是 404 分不清是键名错还是桶坏。所以第二版要求
**206 + Content-Range + 真收到 ≥1 MiB 字节**才算数（并加 `--max-filesize` 护栏，
免得遇到不支持分段的源就在别人线上游戏服务器上拉完整 415 MB）。

| 源 | 结果 | 备注 |
|---|---|---|
| `huggingface.co` 直连 | **不可用**：两次（20s HEAD / 45s GET）都 0 字节、rc=28 | 目标用户在国内时不能假定能连 HF |
| `hf-mirror.com` | 可用：206，`Content-Range: bytes 0-2097151/415182688`，最终落到 `cas-bridge.xethub.hf.co`（美区 IP） | 单 2 MiB 样本，不能据此排吞吐 |
| ModelScope `/resolve/master/` | 可用：206，同总长，CDN `cdn-lfs-cn-1` | |
| ModelScope `/api/v1/.../repo?...` | 可用：206，同总长，同一 CDN 对象、不同边缘 IP | 两次采样 0.4 与 5.5 MiB/s 差 13 倍，**这只能说明单样本不足以排序，不能说明某个端点更快**（很可能是缓存冷热） |
| GitHub release 产物 | 可用：206 + 真字节。但**同一个资产两次采样 0.10 / 0.28 MiB/s，另一个资产 1.01 MiB/s**（`x-cache: HIT, HIT`，后端是 Azure Blob） | 上一版本这行写成"0.1–0.2 MiB/s"，那是把方差当速率；三次采样说明它是**波动大**，不是恒定慢 |
| 自家 `dl.lilyco42.top`（R2） | 可用：206 + 真字节，3 次采样 **1.01 / 0.97 / 0.82 MiB/s**（`server: cloudflare`）；桶里已经有 GGUF：`downloads/models/Qwen3-0.6B-Q4_K_M.gguf`（总长 396,705,472）、`router_v13-Q4_K_M.gguf`（484,219,648） | 比 GitHub 稳，同尺寸对照下快约 4–10 倍；但这不是受控基准，样本各只有 3 次 |

**这条实测对产品有三个直接后果**（先说结论再说依据，避免把一次采样当定律）：

1. 镜像**不用新建**：同一个桶里已经有 `downloads/models/*.gguf` 命名空间。但现有键名是
   `downloads/models/<文件名>.gguf`，没有 org/repo/版本段，而且三个里有两个
   （`router_v13-Q4_K_M.gguf`、`UGround-V1-2B.Q2_K.gguf`）根本不是上游仓库的原生文件名——
   **光看键名回溯不出来源仓库**。新项目入库要用 `downloads/models/<org>/<repo>/<file>`，
   否则目录里会出现"知道是哪个量化、不知道是谁训的"的条目，sha256 也救不回出处。
2. R2 与 GitHub 都回 **206 + Content-Range**，所以**断点续传可以做**。在 0.8–1 MiB/s 的实际速率下，
   一个 400 MB 权重要 7–8 分钟，不给续传就是让用户白等一次网络抖动。
3. R2 只暴露 `Content-Length`，**没有 sha256 元数据**；HF 的 tree/siblings 也只给文件名+大小，
   而 resolve 响应那个 64 位十六进制的 `ETag` **不是**内容哈希（详见 model-catalog.md 的对照表）。
   但 **HF 的权重未必要自算**：同一仓库若在 ModelScope 有镜像，它 `repo/files` 元数据直接给每个文件的
   `Sha256`，且 CDN 路径本身就是内容寻址的 `lfs-objects/<sha[0:2]>/<sha[2:4]>/<sha[4:]>`，
   两条机制一致 + 总长与 HF 相符，就能零下载立 pin（SD1.5 Q4_0 1,566,768,416 B 就是这么立住的）；
   查不到镜像的仓库才退回"下载后自己算"。自家 R2 只能自算。
   GitHub release 这一侧则最简单：`digest` 实测就是内容 sha256，可直接当上游 pin 来源。

**能省掉一整类校验的设计事实**：ModelScope 的 CDN 对象路径是
`lfs-objects/9e/e3/6184e616dfc76df4f5dd66f908dbde6979524ae36e6cefb67f532f798cb8`，
按 `sha[0:2]/sha[2:4]/sha[4:]` 还原后 **正好等于** `engine-live.yml` 里钉的
`9ee36184e616dfc76df4f5dd66f908dbde6979524ae36e6cefb67f532f798cb8`（64 位十六进制，整串重建比对，不是子串匹配）。
HF 与 ModelScope 共用这套 LFS 布局，所以**同一个 sha256 pin 可以跨源复用**：
目录按内容寻址，源只是候选。落地仍然要对下载结果算哈希——路径一致只证明"它声称给的是这份字节"。

## 后果

- 安装包不含引擎与权重（CUDA 包 146–566MB），运行时下载 + sha256 pin；离线首装需要网络。
- 上游改名或换布局会在 CI 里直接变红，而不是等用户报 404 —— 这是把风险换成构建负担的自觉交易。
- 无头链路能被 CI 证明；**GUI 是否真能打开无法由 CI 证明**，交付说明里必须写清这一点。

## 当前阻塞（不是技术问题）

**本 ADR 的所有"待 CI 实测确认"部分目前一条都没被 CI 证实过。**
仓库 `lilyco-42/local-ai-studio` 建成 private 后，账号拿不到 runner：job 4 秒失败，
`runner_name:""`、`steps:[]`，`gh run view --log` 报 `log not found`，
真实原因只在 check-run annotation 里 —— *"recent account payments have failed or your
spending limit needs to be increased"*。同账号的 public 仓库当天早些时候还能正常跑，
所以这是 private 的计量分钟问题，不是车道写错。

二选一，由用户定：把仓库改 public（车道立刻能跑，但仓库名与 README/ADR 公开），
或在 Settings→Billing 加付款方式 / 提高 Actions 支出上限。
在此之前，"47 个 target / 94 个单测 / 93 个模型 URL 全部命中 / 47 个 target 都有上游 sha256 /
工具带 12 条 pin 其中 8 条有两套机制对照"都只是本机 python 证据。
（这些数字每次改动都会漂，复核命令是同一条：`python tools/yaml_gate.py &&
python -W error::ResourceWarning -m unittest discover -s tests -p "test_*.py"`。）

## 已定：模型目录分两层（2026-10-19 补齐证据，原先挂在待定里）

**描述层**直接采用 llmfit 自带的 HF 目录快照（`llmfit-core/data/hf_models.json`，
17,700 条 / 38 字段 / `min_ram_gb` 等拟合输入 100% 非空 / gzip-9 后 850 KB），
按 commit + sha256 钉住，联网只刷新；**下载与校验层自建**
`repo + 文件名 + sha256 + 多候选源`。理由是实测而不是偏好：

- 那张表里**值形如 64 位十六进制的字段有 0 个**（整表遍历，不是子串匹配）→ 给不了校验值；
- `gguf_sources` 只有 `{repo, provider}`，**没有文件名也没有大小**，且只有 2,297/17,700 条带 sources；
- `quantization` 88% 是 Q4_K_M → 它是**模型级**索引，不是文件清单，落到"下哪个文件"还得我们自己解；
- `pipeline_tag` 分布里 **ASR 只有 5 条、OCR 0、生图 0** → 四项能力只有对话能被它覆盖，
  其余三条我们已经各自量到文件级（OCR 93 条带上游 digest、whisper 权重与样本已钉、SD1.5 已钉）；
- `_revalidated` 为空占 91% → 同步来的快照不能当"上游已复核"，我们自己下过并校过哈希才算复核过；
- `license` 有 3,784 条为空、1,327 条是 `cc-by-nc-4.0` → **许可字段必须透到 UI**，
  不能替用户默认"随便用"。

数字、复现命令与逐条解释见 [../model-catalog.md](../model-catalog.md)。

## 待定（不阻塞本车道）

- **外壳许可**：AGPL-3.0-or-later 还是 MIT。所以仓库里现在**故意没有 LICENSE 文件**，
  定了再补，先不放一个会改变别人义务的默认值。
- **仓库名**：`local-ai-studio` 仍是占位（改名一条 `gh repo rename` 就行，不影响已推的车道）。
- **分发源**（是上面"已定"那节的第三层，不是待定）：对话权重 ModelScope 与 hf-mirror 都拿到过真字节
  （HF 直连国内不可用）；引擎与 OCR 权重
  GitHub 可用但波动大（同一资产 0.10 / 0.28 MiB/s），自家 R2 三次采样稳定在 0.82–1.01 MiB/s，
  所以引擎也应进 R2。HF 索引只当同步用的上游，不当用户侧下载地址。
  **工具带同理，且这次量到了更差的一端**：2026-10-19 用 `tools/gh_release_reach.sh` 从同一台 CN 服务器
  复测，llmfit / llama-swap / gguf-parser 的 linux x64 产物都可达且支持 Range（206 + `content-range`
  总长与钉表逐字节一致，负向对照 404），但 1 MiB 档吞吐只有 **0.012–0.017 MiB/s** ——
  三件合计约 37.7 MiB，走 GitHub 是 45 分钟量级，走 R2（0.82–1.01 MiB/s）是 40 秒量级。
  同一 URL 从本机（Windows+代理）是 1.350 MiB/s，所以慢的是出口不是产物。
  数字来自单主机单时段，只用于决定分发层级，不当用户侧承诺。
  **但"权重也一律进 R2"这句要按实测收窄**：把 pin 复核做完整后（ModelScope 给哈希、
  hf-mirror 给整包字节，两条都对上）顺手量了 ModelScope 的下载速度，同一台 CN 出口
  1 / 16 / 32 MiB 三档是 **4.2 / 22.9 / 12.1 MiB/s**，比自家 R2 的 0.82–1.01 MiB/s 快一到两个数量级。
  于是层级变成：**GitHub release 那侧（工具带 + OCR，154.2 MiB，0.014 MiB/s ≈ 184 min）值得镜像进 R2；
  大权重不镜像，把 ModelScope 列为首选候选源、hf-mirror 作备选**，
  清单与逐条结论由 `tools/r2_manifest.py` 从 `pins/*.json` 推导（`mirror/r2-intake.json`）。
  注意这条决定的成立**依赖于先做过跨提供方字节复核** —— 只是因为快就把某家列首选，
  等于把校验责任推给速度。
- **i18n 语言集**：已按实测拆成两条轴并给出默认包，见 [../i18n.md](../i18n.md)。
  识别侧 14 种语言共 23 个 rec 权重（单个 7.4–12.8 MiB），全装约 **122 MiB** ——
  OCR 的多语言便宜到不该做成高级功能；但**繁中、日文、格鲁吉亚语只有 PP-OCRv3 的语言包**，
  给这三个 locale 的承诺不能与中英同级。rec 与 `*_dict.txt` 必须成对下载。
  UI 文案的语言集是另一条轴，加一种界面语言不动任何权重。
