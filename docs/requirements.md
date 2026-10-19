# 需求确认（对标 LM Studio）

日期 2026-10-19 · 状态：**需求与功能分层已定，构建验证仍被 CI runner 阻塞**（见 ADR-0001 文末）。

## 一句话需求

给**已经有电脑但不会配环境**的人一个桌面应用，把本机算力用起来做四件事：
对话、文字识别（OCR）、字幕生成、生图；引擎一律不自研，构建与验证只在 GitHub Actions。

用户已定的三条口径：高性能优先、本地 AI 优先 llama.cpp；交付要 i18n；
"无审查"指的是**用户在自己机器上选什么模型**，不是产品的功能卖点。

## LM Studio 的功能面（从 `https://lmstudio.ai/llms.txt` 抄，149 章节 / 214 页）

它自己把核心功能写成五条：桌面跑本地 LLM、聊天界面、**搜索并下载模型（via HF）**、
**本地 server 暴露 OpenAI 风格端点**、本地模型与配置管理。系统要求页写的是
Apple Silicon Mac、x64/ARM64 Windows、x64 Linux；引擎以 llama.cpp（GGUF）为主，Apple 上另支持 MLX，
且从 0.3.0 起 **LM Runtimes 是单独打包、可热切换的引擎**。

### 它官方给出的"离线/联网"边界（这一条对我们最关键）

| 不需要网络 | 需要网络 |
|---|---|
| 跑已下载的模型；聊天内容不出设备 | 在 Discover 里**搜索**模型（会打 huggingface.co） |
| 拖文档进来做 RAG / 全文入上下文 | **下载**新模型（官方要求"稳定且相当快"的连接） |
| 本地 server 供 localhost/局域网推理 | Discover 目录里每个模型的统计与下载选项 |
| — | **下载/热切换 LM Runtime**（引擎）；检查 app 更新 |

还有一条容易漏的：它支持 **sideload**（用 app 外部弄到的模型），并有 `lms import`。

## 据此定下的产品约束（有实测支撑，不是照抄）

1. **目录必须内置一份快照，联网只负责刷新。** LM Studio 自己就写"每个版本出厂内置一份 initial catalog，
   内容是发版那一刻的在线目录状态"。这不是偷懒，是因为我们的目标用户可能根本连不上源：
   实测国内出口 `huggingface.co` 直连 0 字节不可用，而 `hf-mirror.com/api/models?search=gguf`
   0.46 s 返回 20 条、20/20 带 `tags`+`downloads`（UI 要的许可/热度/大写字段都在），
   正向对照能按精确仓库名搜到 `Qwen/Qwen2.5-0.5B-Instruct-GGUF`，负向对照乱词返回 0 条
   （复现：`python3 tools/catalog_probe.py`，判据不满足就非零退出）。
   → 搜索源默认 hf-mirror，HF 只在能连时用；**离线时目录仍可浏览，只是"最新统计"降级**。
2. **sideload 是必须功能，不是高级选项。** 380–485 MB 一个权重，实测国内 R2 只有 0.82–1.01 MiB/s、
   GitHub 0.10–1.01 MiB/s 波动，一个下载要 6–20 分钟；必然有人选择从别处拿文件。
   所以"导入本地已有 GGUF/ONNX"要进 MVP，且导入后仍按 sha256 认领/提示。
3. **引擎按 runtime 打包、可替换**，与 ADR-0001 的"上游产物 + sha256 pin"一致，
   并且和 LM Studio 的 LM Runtimes 同形，便于后续对齐习惯。
4. **四项能力都起常驻 server，而不是一次一进程**：上游包里就有
   `llama-server` / `whisper-server` / `sd-server`（见 ADR-0001"包内到底有什么"）。
   400 MB 权重每次重载不可接受。`llama-bench` 也在包里，"这台机器多少 tok/s"不用自研。
5. **生图按检测到的后端分层**：有 Vulkan/DirectML/CUDA 时正常默认；只有 CPU 时保留入口但
   明示"一张约 5–10 分钟"并默认降到量化 SD1.5 级。依据是第三方数字与产物表实测的
   sd.cpp 每 release 都发 vulkan/rocm 包（详见 ADR-0001）。
   量完权重还要补一条：**最省的量化版也是 1,494 MiB**，按实测速率 25–30 分钟，
   所以生图权重**不进首装默认队列**，必须显式触发 + 后台 + 可续传；
   而 OCR（4.6+15.8 MiB）与字幕（181.3 MiB）是可以默认的。数字见
   [model-catalog.md](model-catalog.md)。

## MVP / MVP+ 切分

| 能力 | MVP（先能跑通、能出包） | MVP+ |
|---|---|---|
| 对话 | 内置目录搜索(镜像源) → 下载校验 → `llama-server` 常驻 → 聊天 UI | 工具调用、RAG/全文入上下文、预设库、speculative decoding |
| OCR | 图片→文字（`oar-ocr`，det+rec 移动端权重约 4.6+10 MiB） | 版面分析、公式、VLM 路线（`unimernet.onnx` 1.7 GiB，绝不默认下） |
| 字幕 | 视频/音频→SRT：`whisper-vad-speech-segments` + `-osrt` | 多语言批次、GPU EP 选择、实时流 |
| 生图 | SD1.5 级量化 + 后端分层 | SDXL/Flux、ControlNet、LoRA |
| 外壳 | 下载管理（续传 + sha256）、runtime 版本目录、i18n 文案层 | 热切换 runtime、多机 server 暴露 |

## 明确不做

- 不把"无审查"做成卖点、不做 jailbreak 模型精选清单：模型由用户自己搜/自己导入（与用户口径一致）。
  目录按内容寻址、源只是候选，所以"用户搜到什么"由源决定，不由我们策展。
- 不在本机编译或运行任何东西；性能数字必须由车道或真实设备给，不拿 2 核无 GPU runner 的数当承诺。

## 验收标准（在当前唯一可用验证手段 = GitHub Actions 下）

1. `ci.yml`：57 个单测绿（含 `test_yaml_gate.py` 给闸门自己的 9 条、`test_toolbelt_pins.py` 给工具带
   钉表的 14 条、`test_catalog_snapshot.py` 给目录快照的 8 条，其中负向对照合计 10+ 条）、
   `yaml_gate.py` 0 BAD、
   离线发现器解析出全部必需 target 且每个都带上游 sha256；单测在 `-W error::ResourceWarning` 下跑。
   闸门还把**车道 env 里写死的每一条 `*_SHA256` 反查出处**（必须在 `pins/models.json` /
   `pins/toolbelt.json` 或上游 release digest 夹具里），查不到就红、体积抄错也红 ——
   这条是今天补的：那颗 415 MB 对话权重的哈希一直只写在 yaml 里，从没被任何工具复现过。
2. `engine-live.yml`：`discover` 用真实 API 出 manifest（每个 target 都带上游 sha256）；`os-inference-proof` 对
   `manifest["inferred"]` 每一个产物解压要求 PE；`ocr-catalog` 93 条模型 URL 0 缺失且每条都有上游 digest；
   `model-catalog` 盯权重目录与"上游给不给哈希"的漂移；
   `toolbelt-pins` 盯工具带 12 条 pin 的漂移，并**真下载** llmfit 验 `--json` 旗标与 JSON 结构还在；
   `catalog-snapshot` 盯内置目录快照钉住的那份上游文件有没有漂（漂了要显式重钉）；
   `headless-inference` 在 CPU runner 上对话真抓到 `tokens per second > 0`；
   `subtitle-proof` 用钉好的引擎+权重+已知答案样本跑出一份**带时间戳且内容对得上**的 SRT。
3. 目录可达性探针（`catalog_probe.py`）三条判据全过：泛词有结果、正向对照命中精确仓库、负向对照 0 条。
4. **车道覆盖度**：四项能力**都有执行车道了** —— 对话（`headless-inference` 抓 tokens/s）、
   字幕（`subtitle-proof` 出带时间戳且内容对得上的 SRT）、文字识别（`ocr-proof`：CI 里 `cargo build`
   出 `oar`，用钉住的 tiny det/rec/dict 认出一张自画合成图里的已知词）、
   生图（`image-proof`：引擎用 discover 实时发现的产物 + 上游 digest 复核，权重钉死
   `b8944e9f…`/1,566,768,416 B，3 步 256x256 出合法 PNG，并**要求 `-v` 日志里的
   `Steps: 3` 与 `Size: 256x256` 证明参数真被吃进去**，不是随便跑出一张图）。
   生图这条刻意分工：引擎不钉（上游每天出 nightly，钉死等于天天红且测的是旧版），
   权重钉死（1.5 GB，一次下载要 45 min 级 timeout）。
   但覆盖度说的是**车道存在**，不是跑过 —— 跑过与否要看 Actions 的真实记录，目前为零。
5. **车道里的 Python 片段要真编译**：`yaml_gate.py` 现在把两条 workflow 里的每段 `<<'PY'` 抽出来
   `compile()`（本轮 10 段；`python tools/yaml_gate.py` 自己会打印这个数，别信文档里的静态值），
   并验过它会红 —— 掺一段缩进错的 PY 进去，它按
   `文件:job#步骤 第N段` 报出语法错。这个能力是被我上一次写的车道逼出来的：
   一个多缩进两格的注释就是 IndentationError，而旧闸门只查 heredoc 是否闭合，放它过去了。
6. **工具带也要钉**：`pins/toolbelt.json` 12 条 pin（llmfit v1.1.17 / llama-swap v265 / gguf-parser v0.26.4），
   8 条有 sidecar 与 API digest 两套机制互相对照、4 条只有 API digest（`sidecar: null` 如实记）。
   详见 [docs/toolbelt.md](toolbelt.md) 的"已钉住"一节。

**CI 证明不了的**：GUI 是否真打开、集显/独显上的真实性能与兼容性、DirectML/Vulkan EP 是否真的加速。
这几项必须留到设备矩阵，交付说明里要写清，不拿 CI 绿当"能用"。

## 现成工具带（不自研的那部分）

"这台机器能跑什么"、"模型热切换"、"导入的 GGUF 怎么估内存"这三件事已经有 MIT/Apache 的成熟工具在做，
清单、依据与采纳决定见 [toolbelt.md](toolbelt.md)。其中 llmfit（38,108★，MIT，今天还在 push）
带 `--json` 与可扩目录，直接接上本文第 1 条"内置目录快照"的设计。
