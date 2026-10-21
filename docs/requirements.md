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
   **这句话此前只在这里写着**：现在它是一张表（`pins/sideload-index.json`：354 串已钉哈希 →  420 行身份）与一个判据（`tools/sideload_lookup.py`），同名不同字节会被**拒绝认领** —— 见验收第 26 条。
3. **引擎按 runtime 打包、可替换**，与 ADR-0001 的"上游产物 + sha256 pin"一致，
   并且和 LM Studio 的 LM Runtimes 同形，便于后续对齐习惯。
   **可替换的对象现在有表可查了**（验收第 27 条）。那张表同时也说清了三处换不了的地方：
   macOS 的资产名不标后端、whisper 有两颗包名里不写平台、oar-ocr 没有预编译产物。
   **"换到哪一档、要多下多少"是另一张表**（验收第 28 条）：默认档从 `payload-plan` 搬、
   CUDA 档必须连 `cudart-` 伴生包一起算（实测伴生包 373–567 MiB，比本体还大），
   CPU 退路分四态算出来。
   **"换过去还起得来"同样有表**（验收第 29 条）：47 个 runtime 资产里 30 颗读到了成员表，
   每一颗都带着外壳要点名执行的那个 server（llama 的 Windows 包还带同名 impl 库成对），
   真装的 8 颗默认档全部证过；剩下 16 颗可选档的 `.tar.gz` 读不了中央目录，如实标未证，
   不冒充"已知没有"。
4. **对话、字幕、生图三项起常驻 server，而不是一次一进程**：上游包里就有
   `llama-server` / `whisper-server` / `sd-server`（见 ADR-0001"包内到底有什么"）。
   400 MB 权重每次重载不可接受。`llama-bench` 也在包里，"这台机器多少 tok/s"不用自研。
   **"包里有 server"这句现在是成员表级的证据**（验收第 29 条）：三档引擎真装的那 8 颗
   逐颗读到文件名、逐颗带着 server —— 其中 3 颗是 `.tar.gz`，取整包读成员表并把**字节数与
   上游 digest 都对上**才算数。剩下 16 颗可选档的 tar 没读（不为没人下载的那一颗付流量），
   标的是未证，不是"已知没有"。
   **OCR 是例外**：2026-10-20 递归列过 oar-ocr 在钉住 ref 的 464 个成员，
   只有 `oar-ocr-cli` / `oar-ocr-bench` / `examples`，没有任何 HTTP 服务面
   （`oar-ocr-vl/src/api` 是库的 Rust API 模块，不是网络服务）——
   上游只有 CLI，所以外壳对 OCR 只能按任务起进程，或在外面自己包一层常驻。
   原来这里写"四项能力都起常驻 server"是把例外说漏了。
5. **生图按检测到的后端分层**：有 Vulkan/DirectML/CUDA 时正常默认；只有 CPU 时保留入口但
   明示"一张约 5–10 分钟"并默认降到量化 SD1.5 级。依据是第三方数字与产物表实测的
   sd.cpp 每 release 都发 vulkan/rocm 包（详见 ADR-0001）。
   量完权重还要补一条：**最省的量化版也是 1,494 MiB**（ModelScope 4.2 MiB/s 约 6 分钟，
   hf-mirror 0.25 约 100 分钟），所以生图权重**不进首装默认队列**，必须显式触发 + 后台 + 可续传；
   而 OCR（4.6+15.8 MiB）与字幕（181.3 MiB）是可以默认的。数字见
   [model-catalog.md](model-catalog.md)。
   **这一条此前只是一句文档**：装机预算与首启队列都把生图算进"首装"，于是那句"装齐"的数里
   含着用户根本没点的 2,033.8 MiB（16GB 档是 9,612.6 MiB）。现在它是一张机器读的表
   （`machine-budget.json` 的 `default_install` + `first_run_*` / `download_*` 两组数，
   `pins/launch-plan.json` 每项能力的 `install_stage`），半装半不装直接红 —— 见验收第 20 条。
   **"有哪些后端可换"这一半也已经有产物**（`pins/backend-matrix.json`，验收第 27 条；
   换哪一档与安装量差值是 `pins/backend-choice.json`，验收第 28 条；
   能升到哪、能退回哪、每颗能不能校验是 `pins/runtime-versions.json`，验收第 30 条）；
   "这台机器检测到哪个后端"那一半仍属外壳（#25），要等 CI 能跑或真机 —— 这两句不许混着说。

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

1. `ci.yml`：946 个单测绿（重数：`python -W error::ResourceWarning -m unittest discover -s tests
   -p "test_*.py"`；按文件数就 `python -m unittest tests.<模块名>`。2026-10-20 实测逐文件：
   `test_yaml_gate.py` 闸门自己（含 workflow 文件清单与磁盘覆盖对照）17、`test_lane_argv.py` 车道 ↔ 启动表一致（含 15 个 workflow 文本变异）10、
   `test_sideload.py` sideload 按内容认领（6 条源码变异体 + 三个真上游文件）22、
   `test_backends.py` 后端矩阵（7 条源码变异体 + 真实 release 捕获）22、
   `test_backend_choice.py` 换档表（12 条源码变异体 × 16 条探针，输入是三份已钉产物）25、
   `test_runtime_parity.py` server 在场表（11 条源码变异体 × 15 条探针 + 捕获夹具字节复核）21、
   `test_runtime_versions.py` runtime 版本轴（18 条源码变异体 × 24 条探针 + 窗口元数据夹具）21、
   `test_follow_upstream.py` 上游跟随判定与 re-pin 流程（20 条，含"新版不红 / 钉值被撤或被重打包才红"）20、
   `test_shell_contract.py` 外壳合同（14 条源码变异体 × 24 条探针，输入是五张已钉产物 +
   serde 形状重放 12 个变异）19、
   `test_shell_source.py` 外壳源码结构闸门（7 条核心变异体 + 2 条 UI 负控 + 1 条包装层负控 +
   3 条 IPC 对表、Rust 分隔符平衡含 raw string、能力可用性 1 条正控 + 2 个变异、
   i18n 键集与引用 1 条正控 + 5 个变异）16、
   `test_toolbelt_pins.py` 工具带钉表 14、
   `test_catalog_snapshot.py` 目录快照与侧表 16、`test_citation_check.py` 出处闸门 22、
   `test_strength_derivation.py` 强度级从 pin 字段推导 19、
   `test_ocr_build_surface.py` OCR 依赖面 15、`test_ocr_language_pins.py` OCR 语言轴 31、
   `test_ocr_language_lane.py` 语言车道 9、`test_ocr_pins_augment.py` pin 增量写回 11、
   `test_file_index.py` 下载层五种状态与首启队列 35、
   `test_download_plan.py` 下载计划（重算逐键相等 + 源顺序/出处 + 生成器拒猜 + 镜像与 release 兜底 +
   探针与 policy 对表 + serde 形状重放）18、
   `test_payload_plan.py` 装机负载（含 tar 成员表与缺口点名）19、
   `test_pack_engine.py` 出包逻辑与 server-only 收窄（含符号链接闭包、只留一个可执行文件、
   清单 serde 形状 1 条正控 + 7 个变异）20、
   `test_stage_payload.py` 出包产物→外壳 runtime 摆位（含真实合同落点、soname 链接解真身与 11 条负向）12、
   `test_payload_intake.py` 首启交付清单（真实合同落点 / 两个数据根同一份字节 / 6 条源码变异体 / 消费端重放）21、
   `test_make_icons.py` 图标从 ICO 派生（PNG 尺寸 / ICNS chunk 覆盖 / 预乘缩放无黑边 /
   已提交图标可复算 + 篡改与缺件必须红 + 重压缩同一张图必须认、改一个像素必须拒）10、
   `test_establish_verify.py` 字节复核 16、`test_shell_vars.py` shell 变量判据 14、
   `test_bash_gate.py` `bash -n` 一好一坏双对照 9、
   `test_r2_manifest.py` 镜像清单 6、`test_lane_assertions.py` + `test_image_proof_block.py`
   车道判据 38 + 6 = 44、`test_png_proof.py` PNG 判据 25、`test_srt_proof.py` SRT 判据 20、
   `test_wd_ipc_probe.py` WebDriver 真驱动窗口（会话请求 / status 与 data_root 判据 / UI 渲染证据 /
   车道文本与已知坏对照）29、
   `test_recommend_image.py` 生图档位表 31、`test_machine_budget.py` 装机预算 21、
   `test_lane_replay_report.py` 给分段、预跑与 job 数计数，并把上面这份逐文件账按模块 import 复算
   （1 条正控 + 4 个计数变异 + 合写加法自洽），另按锚点复算 README 与两份文档里 7 处
   「车道 … N 条单测」（1 条正控 + 4 个变异）8、`test_engine_probe.py` 产物名解析与 skipped 点名 30、
   `test_recommend_subtitle.py` 字幕四表对账 44、`test_recommend_models.py` 对话档位表 46、
   `test_launch_plan.py` 启动计划（含 16 条源码变异体全部被抓）66、
   `test_install_deps.py` 装依赖清单（含 8 条源码变异体与真捕获夹具的字节校验）36、
   `test_action_pins.py` action 钉法（含 7 条源码变异体与形状对表复核）36），
   `yaml_gate.py` 0 BAD、
   离线发现器解析出全部必需 target 且每个都带上游 sha256；单测在 `-W error::ResourceWarning` 下跑。
   闸门还把**车道 env 里每一条 64 位十六进制的值**反查出处（必须在 `pins/*.json` 或
   上游 release digest 夹具里），查不到就红、体积抄错也红、哈希没加引号被 YAML 读成数字也红 ——
   这条是今天补的：那颗 415 MB 对话权重的哈希一直只写在 yaml 里，从没被任何工具复现过；
   而只按 `*_SHA256` 键名认又会漏掉 OCR 车道的 `DET_SHA/REC_SHA/DICT_SHA`（现在按值形状认，8 条全覆盖）。
2. `engine-live.yml`：`discover` 用真实 API 出 manifest（每个 target 都带上游 sha256）；`os-inference-proof` 对
   `manifest["inferred"]` 每一个产物解压要求 PE；`ocr-catalog` 93 条模型 URL 0 缺失且每条都有上游 digest；
   `model-catalog` 盯权重目录与"上游给不给哈希"的漂移；
   `toolbelt-pins` 盯工具带 12 条 pin 的漂移，并**真下载** llmfit 验 `--json` 旗标与 JSON 结构还在；
   `catalog-snapshot` 盯内置目录快照钉住的那份上游文件有没有漂（漂了要显式重钉），**并用
   `catalog/describe.sha256` 盯"重导出这张目录"的形状** —— 快照本身 .gitignore，所以这颗侧表哈希
   是仓库里对它的唯一记载；2026-10-20 已经真复现过一次（重下上游 28 s、整文件哈希等于钉的、
   重导出 5,808,032 B 与盘上那份逐字节相同），这条把那次一次性核对变成了每次都查；
   `headless-inference` 在 CPU runner 上对话真抓到 `tokens per second > 0`，**并且同一个包再起的
   常驻 `llama-server` 走 OpenAI 兼容 `/v1/chat/completions` 也真的产出非空文本**（ADR 的实现约束
   第 1 条"外壳起常驻服务而非一次一进程"原本一次都没被车道碰过）；
   `subtitle-proof` 用钉好的引擎+权重+已知答案样本跑出一份**带时间戳且内容对得上**的 SRT。
3. 目录可达性探针（`catalog_probe.py`）三条判据全过：泛词有结果、正向对照命中精确仓库、负向对照 0 条。
4. **车道覆盖度**：四项能力**都有执行车道了** —— 对话（`headless-inference` 抓 tokens/s）、
   字幕（`subtitle-proof` 出带时间戳且内容对得上的 SRT）、文字识别（`ocr-proof`：CI 里 `cargo build`
   出 `oar`，用钉住的 tiny det/rec/dict 认出一张自画合成图里的已知词）、
   生图（`image-proof`：引擎用 discover 实时发现的产物 + 上游 digest 复核，权重钉死
   `6ce01616…`/4,265,146,304 B —— SD1.5 **完整 checkpoint**（UNet+VAE+CLIP 同一文件，走 `-m`），
   3 步 256x256 出合法 PNG，并**读 PNG 自己的 IHDR 与
   `tEXt`/`parameters` 块**要求 `Steps: 3`、`Size: WxH` 与实际画布一致 —— 证明参数真被吃进去并写进产物，
   不是随便跑出一张图。判据读产物而非日志，是 2026-10-19 读上游源码定下的：
   `get_image_params()` 只 return，参数由 `stbi_write_png` 落成 tEXt，日志里根本没有那两行）。
   **2026-10-21 换过权重**：上一颗 `stable-diffusion-v1-5-pruned-emaonly-Q4_0.gguf` 只有 UNet
   （`metadata_kv_count=0`、1,131 个张量），sd.cpp 读不到版本字段，`-m` 报
   `get sd version from file failed: ''`；`--diffusion-model` 那条路又需要 VAE/CLIP 伴生件。
   完整 checkpoint 的 pin 在 `pins/proof-weights.json`：HF `tree.lfs.oid` 与 ModelScope
   `repo/files` 的 `Sha256` 同为 `6ce01616…`，两家的总长同为 4,265,146,304 B（零下载，未算整包字节）。
   这条判据本身还有 6 条离线回放单测（合成合法/缺元数据/步数错/尺寸不一致/画布不是 256/非 PNG），
   2026-10-20 又把同一段判据抽成 `tools/png_proof.py` 并加了 25 条单测（含"与车道那段内联判据
   逐案同生同死"的一致性用例），因为常驻服务那条路要把 b64 解开再判，两处必须说同一句话。
   生图这条刻意分工：引擎不钉（上游每天出 nightly，钉死等于天天红且测的是旧版），
   权重钉死（4,265,146,304 B，那一步的 curl 预算是 2,400 s，整条 job 现在是 60 min）。
   **并且对话/字幕/生图三项各有"真起常驻服务再调 HTTP"的一步了**（2026-10-20）：
   `headless-inference#4` 走 `/v1/chat/completions`，`subtitle-proof#5` 起 `whisper-server`
   打 `POST /inference`（multipart，`response_format=srt`）并要求响应是 `x-subrip`，
   `image-proof#4` 起 `sd-server` 走 `POST /sdcpp/v1/img_gen` → 轮询 `poll_url`
   → 解 `result.images[0].b64_json` → 同一套 PNG 判据。
   这三步的共同点是**服务启动时都不给生成旗标**（sd 那步尤其：上游默认 20 步/512x512，
   产物里却是 `Steps: 3`/256x256，才能证明参数是从请求体吃进去的）。
   OCR 没有服务形态可测（见"技术前提"第 4 条的例外说明）。
   但覆盖度说的是**车道存在**，不是跑过 —— 跑过与否要看 Actions 的真实记录，目前为零。
5. **车道里的 Python 片段要真编译**：`yaml_gate.py` 现在把六条 workflow 里的每段 `<<'PY'` 抽出来
   `compile()`（本轮 18 段 —— `upstream-follow.yml` 是纯命令车道、0 段；
   `python tools/yaml_gate.py` 自己会打印这个数，别信文档里的静态值），
   并验过它会红 —— 掺一段缩进错的 PY 进去，它按
   `文件:job#步骤 第N段` 报出语法错。这个能力是被我上一次写的车道逼出来的：
   一个多缩进两格的注释就是 IndentationError，而旧闸门只查 heredoc 是否闭合，放它过去了。
   闸门**自己**也有一条同类回归：它原本用整份文件的文本来核对 `needs.X.outputs.Y`，
   于是任何带 `needs:` 的 job 都要为别人写的引用背锅 —— 我给 `launch-plan` 加了一句
   `needs: file-index`（那个 job 不导出任何东西），它凭空背上 6 条"上游没导出"。
   现在按 job 自己的文本扫，并且三种情况分开报：引用的 job 不存在、用了 `needs.*.outputs.*`
   却没 `needs` 它（Actions 里那是空字符串，不报错）、上游确实没导出该输出。
   `tests/test_yaml_gate.py` 为这三种各钉一条，外加一条"别人写的引用不许算到旁观 job 头上"的正向对照。
6. **工具带也要钉**：`pins/toolbelt.json` 12 条 pin（llmfit v1.1.17 / llama-swap v265 / gguf-parser v0.26.4），
   8 条有 sidecar 与 API digest 两套机制互相对照、4 条只有 API digest（`sidecar: null` 如实记）。
   详见 [docs/toolbelt.md](toolbelt.md) 的"已钉住"一节。
7. **pin 要标强度级，不许压成一句"已校验"**：六级阶梯 + 一条 2026-10-20 新增的 4.5 级
   （配置里写着 → 只有体积 → 上游 digest → 元数据两条机制 + 跨源体积 →
   **两家分发方各给哈希（ModelScope `Sha256` × HF `tree.lfs.oid`，零下载，`--cross-hf`）** →
   一家给哈希另一家给整包字节 → 两个独立发布方各下整包）
   与每级能说明什么，见 [model-catalog.md](model-catalog.md)。
   **4.5 级不止对话/生图那两条权重**（2026-10-20）：OCR 的 14 种语言包也到了 ——
   `tools/ocr_language_pins.py` 把 models.md、GitHub release 产物表、上游自己的
   ModelScope 镜像（`greatv/oar-ocr`）与 `registry.rs` 四张表叠起来，
   **28 条 rec/dict 里 25 条是"两家分发方给同一个 sha256"**，剩下 3 条（繁中/日/格鲁吉亚的
   v3 字典）两家都没有，只能按 PaddleOCR 的一个 commit 取 raw 自算哈希。
   合计 117.6 MiB（含字典）/ 122.2 MiB（含共享 det），落盘成 `pins/ocr-languages.json`。
   **同一个工具也复核默认档**：`--check-pins pins/ocr.json` 验到 tiny det/rec 与字典三条在
   GitHub `digest`、ModelScope 元数据、`registry.rs` 三处给的是同一个哈希、体积也相等
   → 那三条从 3 级升到 **4.5**，追加进 `pins/ocr.json` 的 `meta_hashes`（写文件前有一道
   "只许追加、原值动一下就拒写"的形状守卫，因为这张表是车道的输入，被顺手重写不会红，
   只会从此不再复核那件事）。顺序是刻意的：先有工具与 pin 字段，文档才写 4.5。
   这一轮还量出一个会坑死朴素实现的 fact：同一文件名在 `v0.1.0` 与 `v0.3.0` 里可以是
   **两个不同文件**（`ppocrv5_latin_dict.txt` 1,634 B/`3c0a8a79…` vs 2,616 B/`ccbcc457…`），
   所以查找必须按 models.md 那条链接里的 tag 定位 —— 第一版按名字找，`--live` 立刻报冲突。
   验收动作是 `python tools/establish_pins.py --verify pins/models.json` 与
   `--verify pins/ocr.json`，外加车道 `ocr-languages` 的三步（对照仓库快照 / 对照上游现状 /
   现场重立一张逐字段比）：字节与钉的哈希对不上就红。
   **`catalog/defaults.json` 现在真的带强度级了**（前一句此前是未兑现的承诺：表里只有
   `provenance` 与 `bytes_verified`，没有一个能显示的级数）：级数由 `strength_of()` 从 pin 字段
   推导，`python tools/catalog_snapshot.py --defaults-check` 逐条比对，手改一个 5 或加一条 pin
   忘了进表都会红。推导顺带改掉两句写过头的措辞，见 model-catalog 的《级数由字段推导》一节。第 6 级目前**没有任何一条到达** ——
   whisper/SD 权重实测只有一家发布，这点在交付说明里要照实写。
8. **车道里的 shell 也不能带 `set -u` 地雷**：`tools/shell_vars.py`（挂在 `yaml_gate.py` 里跑）把每条
   `run:` 步骤扫一遍，凡是 `set -u` 生效、而某个 `$VAR` 在本步骤从未赋值、也不在 job/step `env:` 或
   runner 内置白名单里的，就报 `文件:job#步骤 第 N 行`。来历是 2026-10-19 新加的 llama-server 步骤：
   原写 `export LD_LIBRARY_PATH="$(dirname "$SRV"):$LD_LIBRARY_PATH"`，而 runner 上这个变量根本不存在，
   步骤会在**起服务之前**死掉。`bash -n` 查不出（语法是对的），shellcheck 不在依赖里。
   四条刻意的边界，都有对应单测：`${X:-…}`/`${X:+…}` 是作者自己承认"可能没有"的写法，整段抹掉不查；
   `<<'PY'` 里是 Python 不是 shell，扫它会养出误报；**整行 `#` 注释根本不展开**，把它扫了就是假红
   （新加的 whisper-server 步骤在注释里写了一句"不要用 `:$LD_LIBRARY_PATH`"，闸门当场红了 ——
   红在自己的注释上，说明判据会把正确写法当 bug，这种闸门没人会再信）；
   但 **heredoc 正文里的 `#` 不是注释**：未加引号的 `<<PY` 会照常展开 `$`，所以洗注释必须先按
   heredoc 区间排除，否则就把真问题洗白（这条区分本身有一条单测守着，另有一条"注释里的
   `FOO=1` 不算 FOO 已定义"的反向对照）。
   `FOO=1 bar "$FOO"` **要报** —— 那个 FOO 只进 `bar`
   的环境，同一行的 `"$FOO"` 由当前 shell 展开，`set -u` 下确实未定义（这条是写完判据才发现自己
   原来的"对照用例"预期错了，判据比对照对）。
   证过它承重：把守卫去掉（临时副本，不动工作树），闸门红在第 5 行点名 `$LD_LIBRARY_PATH`。
9. **语法这一层交给真解析器，但先验证解析器本身**：`tools/bash_gate.py` 把六条 workflow 的每一段
   `run:` 写进临时文件跑 `bash -n`（只解析，绝不执行）。2026-10-20 实测覆盖 109 段、0 问
   （重数：`python3 tools/bash_gate.py` 最后一行的"语法检查了 N 段 run"）。
   这条工具是被一个假结论逼出来的：本机 PATH 上的 `bash` 是 WSL 的占位程序，它对**任何**文件都返回
   非 0 并吐一段 UTF-16 的"未安装"提示 —— 第一遍跑出的是"42/42 步骤全部语法错"，看着像查得很严，
   其实一条真信息都没有。所以现在每个候选 bash 都要先过双对照（故意写坏的文件必须非 0、
   故意写好的必须 0），过不了就换下一个，全都不过就报 `SKIP（unreliable/missing：原因）`
   而不是装成查过了。单测把"永远非 0""永远 0""跑不起来"三种假 bash 都钉住了。
10. **外壳必须是唯一的监听口，引擎端口不许直接给 UI**（出处是 2026-10-21 重读 llama `b11798`
   与 whisper/sd 的 pin ref 源码，
   见 [ADR-0001 的"三个 server 的接口与默认安全形状"](adr/0001-engines-from-upstream-binaries.md)）：
   三家默认都只绑 loopback（llama `common.h:652`、whisper `server.cpp:59/65`、sd `runtime.h:22-23`），
   这一条是安全的；但**鉴权与 CORS 不是** —— llama 有 `--api-key` 而默认为空（`arg.cpp:3533-3534`）且
   `cors_origins` 默认 `*`（`common.h:656`，`*` + 无 key 上游只打一句警告 `server.cpp:359`）；
   whisper 直接发 `Access-Control-Allow-Origin: *` 且**代码里没有任何 key 校验**（`server.cpp:738-740`）；
   sd 把请求的 `Origin` 原样反射进 ACAO 再配 `Allow-Credentials: true`（`main.cpp:123-131`）。
   也就是说：用户开着我们的应用再打开任意网页，那个网页就能驱动本机的模型 —— 对"本地自用的无审查对话"
   这项目标是必须防的。验收动作因此不是"能起服务"而是三条：① llama 起来时必须带 `--api-key` 且
   `--cors-origins` 收窄到应用来源；② whisper / sd **没有 key 可给**，只能按需起、用完停，或由外壳反代
   （外壳持有唯一端口并自己发 token）；③ UI 永不直连引擎端口。
   这一条现在有**源码级**与**表级**两层证据：`pins/launch-plan.json` 的每一行都写出了旗标
   （llama 那行是 `--cors-origins localhost --api-key {token}`），`pins/shell-contract.json`
   把"谁有钥匙、没钥匙的引擎怎么活"钉成合同，`engine-live` 车道也加了运行时探针
   （无钥匙请求 `/props` 必须 401；`Origin: http://localhost:1420` 必须被反射；`https://evil.example`
   必须没有 ACAO）。但**外壳本身还没写**，所以 UI 那一半 `ui_checked=false`；这些探针 2026-10-21
   真跑了：不带头访问 `/props` 得 401、本机 Origin 被反射、`evil.example` 无 ACAO
   （engine-live run 39633380193 的 `headless-inference`）—— 写了不等于验了，这次是验了。
11. **每条 `file:line` 出处本身要能被查**：`pins/citations.json` 82 条声明（其中 1 条是 `absent`
    型缺席断言：runner 镜像 readme 里没有 `webkit2gtk`），每条记
   `repo/ref/path/line/expect/claim`；`tools/citation_check.py` 按 pin 住的 ref 取回那份文件，
   核对**那一行仍然含着 expect 的子串**（不是只看文件在不在 —— 上游把 `cors_origins` 默认值改掉
   而文件照在，才是我们要知道的事）。
   2026-10-20 又加了一条硬判据：**相邻行不许也含同一个子串**。这张表以前只写"命中不唯一就不配进表"，
   而那句从来没有闸门守着 —— 实测 77 条行号型声明里有 7 条整文件命中不止一次（相隔很远，那些不破坏
   灵敏度），真正会出事的是相邻重复：本轮给 `llama-health-is-public` 定行号时，263 与 264 两行的
   注释一模一样，"行号整体 +1"复算时**它是唯一没红的一条**。现在 `check_one` 直接拦这个形状，
   并把那一条的 expect 加长到只命中 263 行（`ctx_http.get ("/health"` —— `/v1/health` 那行不含它）。
   其余 7 条相隔很远的重复**照旧允许**：它们不会因为挪一格而变绿，一并拦掉反而会把上游正常的
   重复代码行当成漂移。
   车道 `citation-drift` 先跑 `--selftest`（明知是错的必须被认成错，含"相邻重复必须红 /
   相隔两行以上的重复不拦"这一对正反对照），再逐条核对并守住"至少 20 条"的覆盖度地板；
   单测同时把总数钉成 82（加一条要一起改，删一条当场红 —— 这一轮加 4 条时它就叫了一嗓子）。这一条是给第 10 条与整张判据出处表兜底的：
   本轮已经踩过七次"判据指向一个不存在的东西却照样跑完"（`llama-completions`、去 tag 里找 CLI、
   `-of jfk.srt`、去日志里找 `Steps:`、grep `tokens per second`、启动计划里两个表上没有的 cite id、
   以及这条相邻重复）。
   证过它承重：临时改一条 expect → **1 条红**并打印上游实际那一行；
   行号整体 +1 → **81 条红**（82 条里除 1 条 `absent` 型之外全部，那条本来就不看行号），
   也就是说这张表里没有一条"永远绿"的摆设。这件事现在**不必联网也能重跑**：
   `python tools/citation_check.py --cache .cache/refs --fill` 抓一次原文（29 份、1,234,254 B，
   目录已 .gitignore），之后 `--cache .cache/refs` 离线复算就是"82 条真查了、0 条跳过"；
   跳过的那些会被点名说"不是通过，是没查"，这条本身有单测守着。
   重数这三句：`python tools/citation_check.py --cache .cache/refs`（计数行）与
   「把每条 line 加一再核对」那段（`tests/test_citation_check.py` 里有同形状的用例）。
   2026-10-20 清空缓存重抓 29 份原文，全部 82 条按 pin ref 联网实跑过一次（0 问题、0 跳过），
   车道 `citation-drift` 2026-10-21 也真跑了（engine-live run 39633380193，10 s，0 问题）。
12. **OCR 的依赖图现在已经钉住**：上游 oar-ocr 不发 `Cargo.lock`（实测 404，正向对照 206），
   而 40 个外部包里只有 `ort` 是 `=` 精确钉的、其余 37 个是 caret → `cargo build` 会在构建时重新解析。所以：
   ① `ocr-proof` 第一次真跑（run 39633380193）生成了锁并传成 `oar-cargo-lock` artifact；
   ② 已提交 `pins/ocr-Cargo.lock`（121,677 B，sha256 `ee01f0cb…`；495 个 `[[package]]`、
   445 个有名包、489 条 checksum、0 个 git 依赖，`ort = 2.0.0-rc.13`）；
   ③ 此后该步骤在锁存在时走 `cargo build --locked`；仓库里没有锁时才现场解析并打 `::warning::`
   点名"不能算钉住"（正常路径由已提交的锁兜住）。这一支在 run 39638448298 的 `ocr-proof`
   里真走进去过：notice「依赖图按仓库里的 pins/ocr-Cargo.lock 钉住」、lock 里 495 个包、
   编译 3m46s、识别出 `OARCI2291`。第一次 6653cfc 其实落到了 else 分支 —— 那个 job 没有
   checkout，`../pins/ocr-Cargo.lock` 根本不存在，warning 就是证据；46dfead 补了 checkout
   并把 `tools/action_pins.py` 的 uses 计数重算成 57（citation-drift 因此红过一次）。
   漂移由 `ocr-build-surface` 车道守：`tools/ocr_build_surface.py --check` 比对
   `pins/ocr-build-surface.json`（fingerprint + 锁文件状态），上游一旦开始发锁也会红（那就该改成取它的锁）。
   分类判据本身有 15 条离线单测，其中三条是给"曾经算错的样子"写的：`{ workspace = true }` 不是未钉、
   `[package]` 里的 `readme` 不是依赖、带 Range 的探测存在时回 206 而不是 200。
13. **下载层的校验规矩只有一条：两家分发方一致才可信**（`tools/file_index.py` + 车道 `file-index`）：
   按 `repo + 文件名` 同时读 HF `tree.lfs.oid` 与 ModelScope `repo/files` 的 `Sha256`，状态分五种，
   `agree` 才允许"下载前就信"；`single` 必须下载后自算一遍；`conflict` / `pin_conflict`
   **一个哈希都不许返回**（绝不"挑一个好看的"）。候选源按实测吞吐排序，
   `huggingface.co` 记 0（CN 实测 0 字节），不许排第一。
   正向对照 = 已钉的 pin；负向对照 = 故意造的假文件名 —— 它要是也被判成"有哈希"，
   就说明这套比对对任何输入都有答案；另有"至少 2 条被两家确认"的覆盖度地板，
   以及一条把"取数失败"与"这家没有这个仓库"分开的判据（前者必须红，后者是 `single`）。
   实测：qwen 与 SD `agree`（都等于 pin），`ggml-tiny.bin` `single`（ModelScope 未镜像，200 + 0 文件）。
14. **装机负载必须先量出来再设计打包**（`tools/payload_plan.py` → `pins/payload-plan.json`，
    车道 `payload-plan`；Range 读 zip 中央目录，不解压、不下整包、不运行任何东西）：
    按"普通人默认档"（没 CUDA 的 x64 / Apple silicon）选产物，引擎净负载
    win-x64 **44.2 MiB**、linux-x64 **52.1**、macos-arm64 **46.5**（都还没算权重）。
    逐文件侧：`llama-win-cpu-x64.zip` 51 条全收（解压 47.9 MiB）、
    `whisper-bin-x64.zip` 41 条**收 31 排除 10**（`test-*.exe` 那批 —— ADR 的条目表第一次变成可对账的数字）、
    `sd-win-cpu` 20 条 45.6 MiB、`sd-linux` 35 条 65.0 MiB、`sd-macOS` 只有 5 条却 93.9 MiB（`.dylib` 闭包肥）。
    三条判据：主二进制不许被过滤掉、排除规则的总命中数不许为 0（**不逐包要求** ——
    llama/sd 的包里本来就没有 `test-*`，上一版就是这么误红的）、win 的 `x-impl.dll` 不许成孤儿。
    两个真实缺口写清楚而不是藏起来：
    ① **whisper.cpp 的 release 不为 Apple 平台发可执行文件**：唯一相关附件是 `whisper-b5454-xcframework.zip`，成员表（200 条）里只有各 slice 的 framework/Headers、库本体与 dSYM， 数不出 `whisper-cli`/`whisper-server` —— 所以字幕在 Apple silicon 上要么 CI 自己编（可以链这份库）， 要么不做（产品决定，已立任务 #29）；这一条不由散文负责：`payload_plan.py --live` 会把 `same_engine_platform_assets` 与 `asset_evidence`（条目数、有没有主二进制、扩展名分布）写进产物；
    ② llama/whisper 的 Linux 与 macOS 包是 **tar.gz**，没有中央目录可读（格式决定）。
    这不再是缺口了：`--live` 把它取到临时文件、**只读 tar 成员表**（不解包、不执行），
    并且顺手要求"取回字节数 == release 声明的 size"与"整包 sha256 == 上游 digest"，
    任一条不符就当场抛错而不是交出一份看起来完整的清单。实测三颗 CPU 包全部相等
    （llama ubuntu 17,971,994 B `17bfcf22…`、whisper ubuntu 10,364,195 B `a72becf1…`
    —— 后者与字幕车道钉的那颗逐字符相同、llama macOS 12,292,958 B `46d40093…`）。
    只有"格式不认识"或"没跑 `--live`"的行才留在 `needs_extract`。
15. **出包车道已经有了，且它先校哈希再解包**（`tools/pack_engine.py` + `.github/workflows/package.yml`）：
    按 OS 矩阵（win-x64 / linux-x64 / macos-arm64）取 `payload-plan` 选中的产物 →
    **整包 sha256 必须等于上游 digest，对不上就停，不许"先解包再说"** →
    stdlib 解包（不调外部 unzip/tar）→ 按收集规则落成**扁平布局**
    （llama 的顶层同名目录与 whisper 的 `Release/` 都拉平，这是 `$ORIGIN` 找运行库的前提）→
    逐文件 sha256 + 体积写 `payload-manifest.json` 并上传为 artifact。
    **一个引擎只放一个可执行文件**：只收 `LAUNCH_PLAN` 点名的 server，CLI/bench/quantize 等
    工具与它们各自的 `-impl` 运行库（Windows 的 `x-impl.dll`、Linux 的 `libx-impl.so`）逐条点名
    排除；`.dll/.so/.dylib` 是共享闭包，`LICENSE`、`.metal` 这类非可执行数据也保留 ——
    判"是不是工具"靠归档的可执行位，不靠后缀白名单。macOS 的 `libx.0.dylib` 是链接，
    真身是 `libx.0.7.0.dylib`；链接本身必须落盘，否则 `@rpath` 找不到。
    自查：主二进制必须在位、server 必须在位、清单里不许残留被排除的东西、除 server 外不许有
    可执行文件、孤儿 `-impl.dll` 不许出现、符号链接必须能解到随包的真身、负载目录里不许留压缩包
    （留着就等于把二次解包推给用户首启）。同名不同目录的成员**不许静默覆盖**，
    要点名进排除表 —— 覆盖会让"装进去的文件"与"清单里的文件"不是同一份。
    收窄后 win 默认档 112 → 57 个文件、115.6 → **104.7 MiB**（`zip` 中央目录的 uncompressed
    size 求和，未下载整包；llama 51→24 / 42.8、whisper 41→16 / 16.9、sd 20→17 / 45.0；
    Linux/macOS 的 tar 引擎要等 CI 首次解流，zip 的 sd 已算出 65.0→63.6 与 93.9→90.4）。
    `--selfcheck` 18 条、`tests/test_pack_engine.py` 20 条（合成 zip + 合成 tar.gz，含 digest
    不符、下载失败、链式 dylib 链接与"手工塞回一个 CLI 必须红"；另按
    `shell/core/src/manifest.rs` 重放出包清单的 serde 形状，7 个变异含字符串化体积、
    带路径的文件名与同名重复），
    闸门现在同时解析六条 workflow（18 段 heredoc PY，`upstream-follow.yml` 0 段；重数：
    `python3 tools/yaml_gate.py` 的 "编译了 N 段"那行）。
    2026-10-21 出包车道（run 39632180385）在三档 OS 上真跑了这一步（bundle 自检与单测都过，
    三档 `engine-payload-<os>` 真的产出），所以这条现在既有判据、也有"包真的出出来"的证据；
    但"装得上、跑得起来"仍要等第 34 条的安装包与设备验证。
16. **"只能靠 GitHub Actions 出包"这句本身要被验，不能当审美前提**：拆开四问，答案全部来自
    pin 住的公共来源（已进 `pins/citations.json`，由 `citation-drift` 查），详见
    [docs/packaging.md](packaging.md)。要点：runner 镜像自带 Rust 1.99.1 与 Node 22.23.3
    （顺带解掉 OCR 的 `rust-version = 1.95` 那个未知）；**但 Linux 侧没有 `webkit2gtk`**
    （整份镜像 readme 命中 0 次 —— 这条做成 `absent` 型出处），而 Tauri 2 点名要
    `libwebkit2gtk-4.1-dev` 一族，所以安装包车道第一步是 apt 装 + 打印 `pkg-config --modversion`；
    官方 `tauri-apps/tauri-action`（MIT、1,619★、2026-10-16 仍在 push）是 `uses:` 型且
    release **没有产物**，所以它只能按 commit SHA 钉，工具带那套 digest sha256 对它不适用。
    最后是算术：**引擎落地就有 104.7 MiB（win-x64，只收 server 后；全量 115.6）**，再加 415 MB/1,566 MB 权重就不是安装包 ——
    所以"壳装好、引擎与权重首启按下载层契约拉"是决定，不是妥协（LM Studio 同形态）。
17. **给用户看的对话档 ≠ 车道最小样本，而这张表也是重算出来的，不是手挑的**：
    `catalog/defaults.json` 那颗 `qwen2.5-0.5b-instruct-q2_k`（415 MB）只为在 2 核无 GPU 的
    runner 上把引擎拉起来跑通一次；用户第一句问的是"我这台机器该装哪个"，那件事由
    `tools/recommend_models.py` → `catalog/recommendations.json` 负责，四条规则都进产物 `rule` 字段：
    **半档 < `recommended_ram_gb` ≤ 档位**（只用上游 llmfit 的拟合值；只写 `≤ 档位` 时 8GB/16GB 档的
    前两行和 4GB 完全一样，都是 0.6B 与 gpt2）、窗口内按上游 `hf_downloads` 排、仓库**依次**试
    `ggml-org → unsloth → mradermacher`（只取偏好第一名会拿到 `ggml-org/Qwen3-8B-GGUF` 的 Q8_0，
    8,709,518,656 字节 > 8 GB 机器的总内存）、文件要整只且塞得进本档（排除 `-of-` 分片与
    `dflash-*` 这类草稿模型 —— 后者 1.12 GB，按大小它最先合档，所以形状判据必须排在大小判据前面）。
    验收要看四件事：**每行文件大小 < 档位 × 1 GiB**、`single` 行必须标"下完自算"（那 7 行全在
    mradermacher 仓库上，ModelScope 对它们返回 0 条 `.gguf`，第二家根本不镜像重打包方）、
    某档凑不满 `per_tier` 要**报问题而不是自动放宽窗口**（放宽是人的决定）、
    解不出的候选进 `dropped` 而不是把车道判红（上游天天在漂，红要有意义）。
    重算路径与其余工具同构：`--fixtures` 用**同一次 `--live` 录下的真元数据切片**离线重算并比对，
    `--live --check` 盯上游漂移；而 `--fixtures` 那条路**永远不许落盘**（本轮被自己咬过：
    一次没带 `--check` 的 `--fixtures` 把整张产品表覆盖成切片算出的那张，行数照样 15，
    只有 `candidate_pool` 从 1,283 掉到 45 —— 现在闸门看的就是这个计数）。
18. **字幕这一档不能沿用对话那张表的来源，四张上游表必须互相对账**：llmfit 目录里
    **ASR 只有 5 条**（OCR/生图 0 条，已实测），所以"这台机器跑哪颗 whisper 模型"没有拟合值可用，
    `tools/recommend_subtitle.py` 只能回到 whisper.cpp 自己写的东西：README 的 `Model | Disk | Mem`
    （峰值内存 ~273 MB…~3.9 GB 是**上游给的**，不是我们按文件大小乘一个系数拍的）、
    `models/README.md` 的 `Model | Disk | SHA`（16 行）与三条语义规则（`.en` 只有英文、
    `-q5_0` 是量化、**`for-tests-` 前缀的模型文件是空的**——按"仓库里所有 .bin"收集就会把 CI 占位符
    当权重发给用户）、`models/download-ggml-model.sh` 的 30 条白名单与下载源（域名从脚本读）、
    宿主仓库 `ggerganov/whisper.cpp` 的文件表。**验收要看五件事**：
    ① `Mem` 只在上游两张表的 `Disk` 原文**精确相等**时才套用（`large-v3-turbo` 实测 1.5 GiB
    与 large 档的 2.9 GiB 不是同一档 → 它就没有内存数字，`fit=unknown`，**只列进候选表、不进档位推荐**；
    量化档同理 —— 上游只给未量化档的 Mem，宁缺毋滥）；
    ② 默认档另加我们自己的余量规则 `Mem <= 档位 × 0.5`，而"塞得进吗"另列一栏纯算术
    （4 GB 机器算术上装得下峰值 ~3.9 GB 的大档，但那是把系统挤死的算术）；
    ③ 上游三张表自己就互相不一致 —— 实测 `small.en-tdrz` 在脚本白名单里但宿主没有该文件，
    宿主有 4 个 `.en-q8_0` 而脚本没列。这类**上游内部的出入记成产物里的 `deltas`，
    由 `check()` 盯着集合变化**，而不是让车道天天红（红要有原因）；
    ④ 整轴哈希只有 HF 一家给（ModelScope 对该仓库实测返回 0 个文件），所以每一条都是
    `single`、都要标 `usable_before_download=false`，并把"实测最快那个源根本不存在"写进
    `distributor_absent` 让 UI 灰掉那条候选 —— **不许因为镜像站名字像就当成两家确认**；
    ⑤ 跨产物不变量：这张表算出的 `ggml-tiny.bin` sha256 必须逐字节等于
    `pins/models.json` 里字幕车道已经钉住的那颗 pin（否则"车道用的模型"与"推荐给用户的模型"
    就是两套真相）。语言这一轴也量出来了：`src/whisper.cpp:286` 的 `g_lang` 表 **100 种语言、
    id 从 0 连续到 99**（`en`=0、`zh`=1、`yue`=99），而 `.en` 档只有英文（上游 `models/README.md:63`）
    → **UI 选 `.en` 模型时语言下拉必须只剩英文**，下拉本身应由外壳运行时问引擎
    （`whisper_lang_str`），不把 100 个名字抄成会过期的副本。车道 `subtitle-tiers` 四步全在，
    前三步不联网也能红。

19. **生图档不给内存承诺，是因为上游没有这张表 —— 这件事要写死而不是糊过去**：
    `tools/recommend_image.py` → `catalog/image-tiers.json`。与前两张表的差别全在来源：
    llmfit 生图 0 条、`docs/sd.md:5` 只给 safetensors 出处（GGUF 都在社区仓库），
    而 `docs/performance.md:143/145` 说内存是**运行时量出来的**（分段执行留 128 MiB 余量、
    设备底线 512 MiB；README `:157` 就是把人推到这份文档）。所以产物里
    `rule.memory_claim = "none"`，档位判据只有算术：文件大小 < 档位 × 1 GiB × 0.5（与前一张表同一个常数）。
    验收要看六件事：
    ① **档位只认 `role=unet` 的主体权重** —— 实测 `Green-Sky/flux.1-schnell-GGUF` 同一仓库里
      `t5xxl_q3_k` / `clip_l-q8_0` / `ae-f16` 都比主体小，只按大小排会把文本编码器当成"出图的那个模型"；
    ② **配套件跟着行走**（FLUX 那类要 unet + clip_l + t5 + vae 才出得来图，只报主体文件名
      等于让用户下一次跑不动一次）；
    ③ 成人内容按**上游自己的** `not-for-all-audiences` 标签挡在档位外（这次捕获 2 个仓库带标记，
      仍留在 `weights` 里），不按仓库名猜 —— 名字里带 NSFW 不是判据，上游打了标记才是事实；
    ④ **许可缺失也要显示**：43 个候选仓库里 7 个没有 license 标签，`str(None)` 是 `"None"`，
      只看 `nc|other|unknown` 的正则会把"根本没有许可"当成"许可没问题"发出去；
      `creativeml-openrail-m` / `openrail++` 也进了标记（随包发的 SD1.5 就是这条）；
    ⑤ 哈希按两家分发方比对：**生图能到 4.5 级**（实测 338 行里 191 行两家同哈希），
      与字幕整轴只有 `single` 正好形成对照 —— 说明"能不能两家确认"是仓库的事，不是能力的事；
    ⑥ 一条反例钉在测试里：`leejet/FLUX.1-schnell-gguf` 的 `q4_0` 与 `q4_k`
      大小一模一样（6,884,606,880 B）而 sha256 不同 —— 按大小认文件必须被否掉。
    还有一件**仍未解决、必须留给真机**的事：跑一张图要多久、量化到多小会崩质量，
    CI 那台 2 核无 GPU 的 runner 证不了（见"第一次真跑要盯什么"），
    所以这张表只能回答"装得下哪个文件"，不能回答"跑得动跑不动"。
    车道 `image-tiers` 三步（判据单测 → 离线重算比对 → `--live --check`），15 个变异全被抓住。
20. **「装齐要下多少、要等多久」必须是算出来的，而且一个字节都不许估算**：三张档位表各自回答
    "这颗选哪个"，普通人问的是另一句。这个数字此前没有任何地方算过 —— `payload-plan` 只算引擎净负载，
    档位表只给单颗大小。`tools/machine_budget.py` 只加**已钉产物里有的字节**：引擎净负载 + 三张档位表
    该档第一行（默认档）+ OCR 默认档三条；`pins/ocr-languages.json` 的全 14 语言合计是**替代**默认档的
    另一个数，不许叠上去再加（它不是一个文件：`not_downloaded_bytes` 单列，不进字节也不进时间）。
    实测（win-x64）分两组：**首装默认** 4GB **3.80 GiB**、8GB **7.61**、16GB **11.31**，
    等待 **159.6 / 268.4 / 283.5 分钟**；**装齐四样** 才是 4GB **5.79 GiB**、8GB **11.59**、
    16GB **20.70**，等待 **167.7 / 284.6 / 321.6 分钟**。时间逐颗按它自己的候选源里**最快**的
    已量出口相加：引擎只发 GitHub release（0.014 MiB/s）、whisper 只有 hf-mirror（0.25，ModelScope
    地址是上游 404）、对话/生图/OCR 有 ModelScope（4.2）。每颗再取可用源里最慢的一档作为上界：
    首装 **5.27 / 9.60 / 13.81 小时**、装齐 **7.53 / 14.12 / 24.49 小时**。
    旧版把全部字节按 4.2 除（漏掉引擎与 whisper 的真实出口），得到 15.9 / 31.4 / 46.4 分钟 ——
    乐观约 10 倍。`ideal_all_modelscope_*`（首装 15.5 / 30.9 / 46.0 分钟）只留作
    "如果全部走 ModelScope"的网络差距参照，不是任何机器的承诺。
    这两组此前被并成一组：生图那部分（4GB 档就有 2,033.8 MiB）按**产品约束第 5 条**要显式触发，
    不该混进"点完安装要等多久"。现在政策在产物里（`default_install`：chat/subtitle/ocr 默认、image 不默认），
    `first_run_*` 与 `download_*` 各一套，`--check` 两个数都比；缺 `default_install` 字段的行会被报出来，
    同一族里半装半不装也直接红（下载层与启动表都不许猜）。
    三条口径由产物自己带着（`rule` / `ram_statement` / `measured_at_exit`），因为它们都会被人误用：
    ① 这是**磁盘与下载量，不是峰值内存** —— 四项能力共用一个常驻引擎、一次只跑一项（第 10 条），
       把权重相加当内存需求是错的，内存那一轴由三张表各自的判据负责；
    ② 吞吐是**一台机器一次量的**，而且每颗文件走哪个源不一样：逐颗取可用源里最快的一档，
       再逐颗取最慢的一档给上界，两个数都给用户；源缺席（`distributor_absent`，whisper 的
       ModelScope 404）不参与计时。只报最快那一列等于拿最好网速承诺最坏体验；查表的短针
       （`hf-mirror`）必须对得上表里的长键（`hf-mirror.com`），方向写反不报错、
       只会静默把慢吞吐读成 0 让整个最慢档消失，这条有独立用例；
    ③ **"没有产物"与"落地大小未知"是两件事**：macOS 的字幕是上游没发（`missing_capabilities`），
       某行格式读不出成员表时才标 `unknown_landed_sizes`（`needs_extract`）—— 把后者当缺失能力
       报出去等于谎称那项功能在这台机器上没得用。这一版之前 Linux/macOS 两档全靠它标"落地未知"，
       现在三颗 tar 包读了成员表，三个 OS 的落地大小都是实测值。
    加总这一步反过来把生图那条链改了三处，方向全是**少算**，这就是为什么要把它单独算一遍：
    ④ **原先三档选中的生图默认行，仓库里都没有配套件**，而上游 `docs/sd.md:16` 的 flux 例子要
       `--vae --clip_l --t5xxl`、`:17` 的 sd3.5_large 要 `--clip_l --clip_g --t5xxl`（连同 `:13/:14/:15`
       一共五条例子行进了出处表，由 `citation-drift` 逐行查）→ 档位表选默认行改成
       **先按"装完这一颗能不能直接出图"排**（每行带 `completeness`，`state ∈
       {self_contained, complete, incomplete, unknown_arch}`）：16GB 档从 `unsloth/FLUX.1-schnell-GGUF`
       （只有 unet）换成 `second-state/FLUX.1-schnell-GGUF`（带 clip_l / t5xxl / ae），16GB 的预算
       因此从 19.25 涨到 **20.82 GiB，其中配套件 1.77 GiB**。"这一档是不是下限"由
       `image_lower_bound` 说，UI 只有在这件事为假时才许说"装完就能出图"。
    ⑤ **"仓库里没有配套件"本身不是判据**：上游例子只给 `-m` 的家族（SD1.5，`:13`）是
       `self_contained`、不是下限；认不出家族的写 `unknown_arch`，**不许当成"不需要"** ——
       查不到与没有是两件事，这条与第 ③ 点是同一个教训的两次应用。
    ⑥ 配套件的分组键必须是**上游那个旗标对应的东西**（`clip_l` / `clip_g` / `t5xxl` / `VAE`），
       不是 `role`：`clip_l` 与 `t5xxl` 的 role 都是 `text_encoder`，按 role 分组会把 t5xxl 整个丢掉
       而档位表这时仍写着 `complete`（**这一轮就错过一次**）。现在两张表不一致会直接报问题
       （"档位表说这一族需要 X，但加法里没有它"），同键的十几个量化才是备选，取最小那颗，
       其余记 `group_alternates`。
    车道 `machine-budget` 三步**全离线**（输入就是那五张已提交的表加吞吐表；联网重算属于上面三条
    档位车道，那边一漂这边就跟着红）：`--selftest` → 21 条单测 → `--check`。
    一次性变异扫描 23 条全被抓，其中四条是这轮补的：分钟数少除一个 60、吞吐查表方向写反、
    OCR 的"下之前就能当真"写死成 `True`（默认档三条恰好全含 4.5 级，只有合成输入能证明那标记
    是读来的）、配套件按 `role` 分组（就是第 ⑥ 点那个错）。
    重数这几句：`python3 tools/machine_budget.py`（每档每 OS 的 GiB 与分钟）、
    `python3 tools/machine_budget.py --check`、`python -m unittest tests.test_machine_budget -v`。
    那句"23 条全被抓"来自本轮一次性的临时扫描（`.cache/` 已 gitignore，不在仓库里），
    能长期复现的是它逼出来的那批用例，不是扫描本身。

21. **首启队列：每一项都要说出"我是谁、从哪家来、走哪条路线被确认"**：预算给了总量，
    外壳要的是**能下的清单**。`tools/file_index.py --queue catalog/machine-budget.json`
    把三档的默认权重行去重（15 项，同一颗文件跨档只排一次，按仓库缓存元数据），逐项检查：
    有仓库 id、有钉住的哈希、两家声明的哈希与钉值**逐字符相等**、体积与预算里一致、
    至少有一个候选源；并按 `resolve_via` 分流 ——
    `hf_metadata`（对话/字幕/生图）当场联网对表，`github_release_digest`（OCR 默认档三颗来自
    GitHub release，不是 HF 仓库）**如实标 `checked: false` 并写出路线**，
    `not_a_file`（全语言合计）跳过。**把"我没查"混进"查过了"是最容易骗过自己的一种错**，
    所以覆盖度地板按 live 确认的条数算（8），不是按条目总数。
    实测（本机一次）：**15 项去重、live 确认 11 项、其余 4 项标路线、BAD 0** ——
    含这轮换成的 FLUX 配套件 clip_l / t5xxl / ae 三颗（都 `agree`）。
    报告用 `--json queue-report.json` 落档并由车道传成 artifact `first-run-queue`，
    外壳实现（#25）直接拿它当下载顺序与校验输入。
    同一轮补的纪律：**"宿主从上游脚本里读"不能只写在 docstring 里**。
    `recommend_subtitle.py` 的 `parse_script()` 早就从 `download-ggml-model.sh` 读 `src=`/`pfx=`，
    可候选源与文件表那两处仍把 `ggerganov/whisper.cpp` 当字面量传进去 —— 上游换宿主时
    表里的 URL 会跟着脚本变、而解析还在查旧仓库。现在宿主由 `host_for_inputs()` 从脚本前缀里取，
    行里与 `tiers_rows` 都带 `repo`，`check()` 比对它，并且
    `test_host_literal_never_appears_in_code` 用 ast 扫全文（docstring 允许解释、代码里的
    字符串常量不允许）；`live_inputs()` 自己离线跑不到（要 curl），所以"读宿主"单独成函数，
    才能拿真捕获的脚本直接验 —— 换成另一家 `src=` 就得跟着变。
    重数：`python3 tools/file_index.py --queue catalog/machine-budget.json`、
    `python -m unittest tests.test_file_index tests.test_recommend_subtitle -v`。
    队列判据的一次性变异扫描 10 条全被抓（临时脚本，不在仓库里）：地板失效、
    GitHub 路线算成本车道查过、体积不符不报、负向对照撤掉、未命中也标确认、
    不去重、取数失败当空表、每文件重查元数据、状态不合格也算过、缺仓库/缺钉值也算能下。


22. **外壳该敲哪一行，也要有出处**：验收第 10 条那三句（外壳持有唯一监听口、自己发 token、
    UI 永不直连引擎端口）能不能成立，取决于**引擎自己支持什么开关**，这不是设计偏好而是事实。
    `tools/launch_plan.py` 把这四件事按钉住的 ref 读上游源码量清楚了，并落成 `pins/launch-plan.json`：

    | 能力 | 起法 | 引擎自己有没有鉴权 | 上游出处 |
    |---|---|---|---|
    | 对话 | `llama-server -m … --host 127.0.0.1 --port … --cors-origins localhost --api-key …` | **有** `--api-key`；CORS 可收窄 | `common/arg.cpp:3533`；`--cors-origins` = `common/arg.cpp:3372`、默认 `*` = `common/common.h:656`、`localhost` 只反射本机来源 = `tools/server/server-http.cpp:336-345`（`origin_is_localhost` 在 `:75`）；`/health` 那行明写 "public endpoint (no API key check)"（`tools/server/server.cpp:280`），所以带着钥匙也能探活 |
    | 字幕 | `whisper-server -m … --host 127.0.0.1 --port …` | **没有**（整份 server.cpp 里 `--api-key` 与 `api_key` 都查不到） | `examples/server/server.cpp:251/252`；默认就绑 127.0.0.1（`:59`） |
    | 生图 | `sd-server --diffusion-model … --clip_l … --t5xxl … --vae … --listen-ip 127.0.0.1 --listen-port …` | **没有** | `examples/server/runtime.cpp:193/198`；四份额外权重的旗标逐个在 `examples/common/common.cpp:407/411/421/471` 查到 |
    | 文字识别 | `oar ocr --det … --rec … --dict … --device … --format json --output <目录> <图片…>` | 没有服务形态，一次任务一个进程 | `oar-ocr-cli/src/args.rs:10/49/59/84/90`（这份文件里没有 `--port/--listen/--api-key`） |

    两条不显眼但要命的上游事实：① OCR 的 `--det/--rec/--dict` 是 `requires_all`，**三颗必须一起给**，
    而默认档 `--size` 与它们 `conflicts_with_all` —— 我们传自己钉住的权重，就一个 `--size` 都不能带
    （`build()` 里真有这条拦截：把 `--size` 塞进模板会当场红）；② whisper 在 Apple 侧**没有可执行产物**
    （只有 xcframework），所以 `by_tier` 里根本没有 mac 那一行 —— #29 那三种选择就是这个数据事实的形状。

    **生图那行的编码器不是手打的**：`--clip_l/--t5xxl/--vae` 从 `catalog/machine-budget.json` 里
    真算进预算的那几颗配套件读出来，主体文件名则与 `catalog/image-tiers.json` 的默认行**交叉核对** ——
    两张表对不上就报"下的与用的不是同一批文件"，绝不挑一张用。模板里写的是**文件名**
    （`{flux1-schnell-Q5_0.gguf}`）而不是一个谁都能填的 `{model}`，这样"起的正是下的那一颗"才可核对。
    对话与字幕同理：`-m {Qwen3-14B-Q4_K_M.gguf}`、`-m {ggml-large-v1.bin}` —— 这一档在预算里
    **必须正好有一颗**该能力的权重，两颗就报"实到 2 颗"、没有就报"实到 0 颗"，绝不替用户挑一颗
    （挑了，"表已经不一致"这件事就再也看不见）。四项能力一共点名 11 颗。

    点名之后还要与**另一个生产者**对表。`pins/launch-plan.json` 里那 11 颗是从 `machine-budget`
    算出来的，而首启队列由 `tools/file_index.py --queue` 现算、逐项做两家元数据 live 比对，
    产物叫 `first-run-queue`。`launch_plan.py --queue-report` 吃的就是这份报告：报告里没有这一颗、
    `status` 不是 agree、或这一行走的是"这条车道没做 live 比对"的路线（OCR 默认档那三颗来自 GitHub
    release，它们的复核在 `ocr-languages` 车道）—— 三种都拒绝起进程，而且**"没查"与"查了没对上"
    分开说**，不混成一句。没有报告时不假装核过：产物里逐项写着
    `queue_link = budget-only：没有 --queue-report，同源核对不算已验`（argv 与预算队列本就是同一个
    来源，拿它自证是空转）。这份报告的**真实捕获**钉在 `tests/fixtures/first-run-queue.json`
    （15 行、11 行 `matches_pin=true`，字节哈希记在同目录 `.meta.json`），所以这条跨产物的核对
    在还没有 runner 的今天也能离线重算。
    每项能力还标 `install_stage`（`first_run` / `on_demand`），读的是 `machine-budget` 那张
    `default_install` 政策表：对话/字幕/文字识别是首装就有，生图整族要显式触发 —— 外壳据此
    决定敲这条命令行之前要不要先问用户下载。字段缺失不许这一层猜（报），同一族里半装半不装
    也直接红：那是"命令敲下去缺一颗料"，不是一种取舍。
    起进程要点名的 server 名字现在也只有**一张表**：`launch_plan.SERVER_BASE` ⇄ `pack_engine.SERVER_BINARY`，
    出包那条因此会拒收"负载里只有 `whisper-cli`"的产物（只有 CLI 不算装了 server，装了也起不来）。

    判据的承重证明留在仓库里（不是又一次性扫描）：`tests/test_launch_plan.py` 的
    `TestSourceMutants` 把工具源码改坏 **16 个变异体**，每个都必须让六条探针（离线对照 /
    重算漂移 / 出处 id 闸门 / 缺席声明照扫 / 跨生产者核对 / 核对没有输入就红）至少一条不再"在位"，并同时跑一条正向对照
    （不改坏时六条全绿）。它当场抓出四个真 bug：`build()` 把 SPEC 里的 dict **直接塞进产物**
    （改产物就是改规格，现已 `deepcopy`）、`verify_spec` 里 `seen_absent` 初始化成了 list
    （第一跑就 `TypeError`）、探针之间**互相脏了数据** —— 出处 id 那条探针把 `SPEC` 里的 cite
    改掉后不还回去，紧接着的跨生产者探针拿到就是被污染的规格，报出来的"红"跟变异体毫无关系
    （现在每条改共享状态的探针都自带 `finally` 还原），以及**两条错误消息共用了一个说法** ——
    "缺件"与"读不动"都写着"核对没有输入"，于是把缺件那条分支删掉，探针照样绿；
    现在两条消息各用只有自己能命中的词。输出还会自报家门：
    `核对 28 条 needle / 8 条缺席声明，取自 8 份上游原文（缓存 N / 现抓 M）`，
    加 `--skip-provenance` 时打 NOTE 并报 0 —— 「没查」不能长得像「查过了」。
    这一轮也踩了一次**引用不存在的出处**：SPEC 里原先写 `headless-server-chat-completions`
    与 `subtitle-server-inference-field`，两个 id 在 `pins/citations.json` 里根本没有，
    而"引用了出处"这件事看起来与"核对过出处"一模一样。现在 `build()` 会逐个 id 对表，
    单测 `TestCiteIdsExist` 同时守正向（每条 id 都在表里）与负向（塞一个假 id 必须报）。
    重数：`python3 tools/launch_plan.py --selftest`、`python -m unittest tests.test_launch_plan -v`、
    `python3 tools/launch_plan.py --check --skip-provenance`（离线那层）、
    `python3 tools/launch_plan.py --check --fill --cache .cache/refs`（联网按 pin 查）、
    `python3 tools/launch_plan.py --check --fill --cache .cache/refs`（联网按 pin 查）、
    `python3 tools/launch_plan.py --cache .cache/refs --queue-report tests/fixtures/first-run-queue.json --out .cache/launch-plan-with-report.json`
    （跨生产者那层：写的是 scratch 那份，有 BAD 就不落盘，所以 rc 就是那句话本身）。
    **接线本身也踩了一次**：给 job 加 `needs` 就要同时加 `if: always()`。这一轮 `launch-plan`
    改成 `needs: file-index` 去吃它的报告，实测在 `3781953` 那次 push 上 —— file-index 因账单
    失败之后，launch-plan 的结论是 `skipped`、`steps: 0`，什么都没说出来；这正是本仓库已经写在
    `file-index` 注释里的那条规则（被挂住的 job 会被标 skipped，结论看不见）。所以现在那两步带
    `if: always()`，并且报告文件不在时由 `launch_plan.py` 自己报「不存在」再退出 1 ——
    `download-artifact` 缺件只警告、不红，不能指望它替这条核对把关。
    **仍然没有的部分**：这些是按上游源码与文档拼出来的模板，本机一台都没起过 ——
    argv 真能被引擎接受，2026-10-21 已在 CI 里验过关键那几条（`headless-inference` 用当时的
    最新版 b11797 真起了 server 并走完鉴权/CORS 探针）；等 #25 外壳落地，再把 `launch-plan`
    的每条命令逐条跑成探针（runner 2026-10-21 起已可用，不再是 #19 的阻塞）。

23. **安装包车道要装什么，也是读出来的**：`tools/install_deps.py` → `pins/install-deps.json`
    从 Tauri 前提页（`tauri-apps/tauri-docs` @ `2e513e3f…`）的 `<TabItem label="Debian">` 那一族
    抽出九颗包名并逐颗带上游行号（`:33-41`），再逐字去 ubuntu-24.04 镜像 readme 的包表里查在场情况。
    实测分两半：**在表里**的 `curl` / `wget` / `file` / `libssl-dev`（版本一并带出，如
    `libssl-dev 3.0.13-0ubuntu3.16`），**readme 没提到**的 `libwebkit2gtk-4.1-dev` /
    `build-essential` / `libxdo-dev` / `libayatana-appindicator3-dev` / `librsvg2-dev`。
    判语只能写成 `not_listed` 而不能写"没装"：**readme 是给人看的清单，不是 `dpkg -l`**，
    这一条写在产物 `verdict_legend` 里，不许哪天被顺手改成缺席证据；
    "到底装没装"由车道给 —— 装完必须打印 `pkg-config --modversion webkit2gtk-4.1`。
    两个上游事实也立成判据：**包管理器必须是 apt**（读到 dnf 就报，车道那行命令要跟着改）、
    **`libwebkit2gtk-4.1-dev` 必须在表里**（换成 4.2 就报，别默默照旧装 4.1）。
    `source.families` 这一栏是修 bug 修出来的：第一版把块里所有像包名的词都算进去，
    于是真页里 Windows/Linux/macOS 那几个 tab 的 `export JAVA_HOME=…` 和 `Most / apps / refresh`
    被当成包，产物里冒出 **10 个假家族**；现在只认包管理器那条安装命令的续行，
    `apt update` / `pacman -Syu` / `zypper up` / `dnf check-update` 都不算清单，剩 **6 个真家族**。
    顺带更正一处流传的假事实：此前任务与文档都写过"+ gstreamer 插件族"，
    而这份钉住的页里 `gstreamer` **命中 0 次** —— 那是 Tauri 1 时代清单的记忆，
    要加回去得先给一个能查的出处。
    重数：`python3 tools/install_deps.py --selftest`、
    `python -W error::ResourceWarning -m unittest tests.test_install_deps -v`（36 条：
    8 个源码变异体各被四条以上探针之一抓住，另有"不改坏时五条探针全在位"的正向对照，
    以及两份真捕获夹具的 sha256 前缀 + 无 CRLF 字节校验），
    `python3 tools/install_deps.py --check --fill --cache .cache/refs`。
    跑在 `citation-drift` 这条车道里（它本来就是"按 pin 读上游"的车道，job 数因此仍是 21）。


24. **出包那只手自己也要钉住**（`tools/action_pins.py` → `pins/actions.json`）：
    硬约束是"只能靠 GitHub Actions 出包"，那么 workflow 里每个 `uses:` 就是那条路径本身。
    这一轮之前它们全是**浮动大版本 tag**（4 个 action、开工时 44 处；现在这张表记 **56 处 /
    6 个 workflow / 5 个 action**：`launch-plan` 加了一处 `download-artifact` 去吃队列报告，
    `backends` 那条新车道又带进 `checkout` 与 `setup-python` 各一处，`installer.yml` 带进
    `checkout` 与 `tauri-action` 各一处，`upstream-follow.yml` 带进 `checkout` / `setup-python` /
    `upload-artifact` 各一处；**另有 4 处是补漏** —— 上一版扫描表没列 `shell.yml`，
    它从进仓库那天起就有 2 个 `checkout` + 2 个 `upload-artifact` 没被数过，"49 处"因此少算 —
    重数：`python3 tools/action_pins.py --check`），
    今天全部改成 40 位 commit SHA，
    尾注保留它来自哪个 tag；判据三件：形状必须是 SHA、尾注 tag 重新解析后**漂了就红但不自动跟**
    （重钉必须是显式动作）、`action.yml` 的 `runs.using` 与 `inputs` 键集合必须与表一致。
    形状这件事差点被自己骗过去：第一版用自写缩进正则，把 `actions/checkout` 的 21 个输入读成
    8 个（`description: |-` 折叠块让读取提前收尾），**基线读少了，上游真加输入时那次比对反而像"没变"**
    —— 现在改成用 pyyaml 复核并有一条用例逐行对表（取不到上游就明说跳过，不当通过）。
    403 与 404 也分开报：匿名 GitHub API 60 次/小时，撞限流时说"被限流/没权限"，
    不许伪装成"上游没这个提交"。`tauri-apps/tauri-action` 现在由 `installer.yml` 使用（#30），
    它的 release 名（`action-v1.0.0`）与 tag 名（`v1.0.0`）不同，两个名字都进表、机器复核。
    重数：`python3 tools/action_pins.py --selftest`、`--offline`（只查形状、**拒绝落盘**）、
    `GITHUB_TOKEN=$(gh auth token) python3 tools/action_pins.py --check`。

25. **表说的那一行，与车道真敲的那一行，必须是同一行**：`tools/lane_argv.py`（挂在 `ci.yml` 静态门禁）。
    第 22 条那张表是**按上游源码**核对出来的，可四条执行车道里的命令一直是手写的，两边从没对过 ——
    "表对了"与"跑的那行对了"是两句不同的话。现在读 `engine-live.yml`（用 pyyaml 取每个步骤的 `run`，
    不自写正则切块：上一版按 `- name:` 切，最后一步会一路吞进下一个 job，把别人的旗标算到这一段头上），
    认出每项能力真起进程的那几步，再用表里的六条规矩量：
    ① 定位到的二进制必须就是表里那颗（`whisper-cli` 与 `whisper-server` 是两个文件，而且
    `chmod/ldd` 那种"提到过"不算执行 —— 只看一行的开头）；
    ② `--host` / `--listen-ip` 必须出现，且值**逐字**是 `127.0.0.1` —— `$ADDR`、`0.0.0.0`、
    "没写所以靠上游默认"三种都算不合格（验收第 10 条讲的是我们传了什么，不是引擎默认什么）；
    ③ 表里声明"上游查不到"的旗标不许出现在车道里；④ `omit`（OCR 的 `--size`）与子命令 `oar ocr` 同样各查一遍；
    ⑤ 表里说引擎有钥匙的（llama），车道那行必须真带 `--api-key`：变量必须在同一步被赋成非空值，
    `"{token}"` 原样传进去算不合格；⑥ CORS 也不能只写在表里：必须带 `--cors-origins localhost`，
    同一段还要有**无钥匙访问得 401** 的断言和**两个不同 Origin**（本机被反射 / 非本机无 ACAO）——
    只带旗标、不做运行时对照，证明不了门禁真的生效。
    判据只吃 shell 那一层：`#` 注释与 heredoc 正文都先滤掉（本仓库真出过一次"判据钉着注释"的事故），
    并且注释里带 `--api-key`、heredoc 里打印 `--api-key` 都不许让它红 —— 这两条各有一组对照。
    实测（2026-10-20）：四项能力各认出一段常驻 server 的步骤，**问题 0 条**；把 workflow 文本改坏
    15 种（绑 0.0.0.0 / 绑变量 / 不传绑地址 / 给 whisper 或 sd 加 `--api-key` / 把 llama 的常驻那步改成
    找 CLI / 去掉 `oar` 子命令 / 带 `--size` / 只 find 不 exec / 改掉二进制名 / llama 丢钥匙 /
    钥匙传占位符 / 钥匙变量没赋值 / llama 丢 CORS / 401 被改成 200 / 非本机来源被换成同一来源）
    15 种全被报出来，正向对照"真实 workflow 必须 0 问题且认出 4 段"防止"0 问题"其实是"什么都没认出来"。
    重数：`python3 tools/lane_argv.py --selftest`、`python3 tools/lane_argv.py`、
    `python -m unittest tests.test_lane_argv`。

26. **sideload（用户从别处弄来的文件）按内容认领，不是按名字**：`tools/sideload_lookup.py`
    → `pins/sideload-index.json`（挂在 `ci.yml` 静态门禁）。约束第 2 条那句"导入后仍按 sha256
    认领/提示"此前没有任何产物。现在把六张已钉表（`pins/models.json`、`pins/ocr.json`、
    `pins/ocr-languages.json` 与三张档位表）摊平成 `sha256 → 身份`：**354 串独立哈希、420 行身份、
    30 个"同名不同哈希"的文件名**（`flux1-schnell-q2_k.gguf` 这类在同一份预研里被两个仓库发过，
    字节不同 —— 按名字认领一定会认错）。结论只有四种，每种都有反着来的对照：
    ① `known_by_hash`（认领，并带它属于哪项能力与证据等级）；② `name_matches_hash_differs`
    （名字钉过、字节不是那颗 → **拒绝认领**）；③ `unknown`（不认领，只给带 `claim:false` 的
    文件名形态提示）；④ `hash_known_size_differs`（哈希钉过但体积不符 → 先查 pin，不认领）。
    OCR 认出一颗时还要说清运行时得一起给哪几颗（上游 `requires_all`）。
    夹具是**三个真实下载的上游文件**（`tests/fixtures/sideload/…`，都 <30 KB）：两颗钉过的字典
    当正向，`ppocrv5_latin_dict.txt` 在 v0.3.0 是 2,616 B、在 v0.1.0 是 1,634 B —— 同名不同字节
    不是假想，是上游真的做过，所以 ② 有真负样本可测；三个 sha256 都取自 GitHub API 的
    `asset.digest`，测试再按 meta 复算一遍字节。
    **这一层不解读文件头**：`.gguf` 的 `general.architecture` 归 `gguf-parser-go`（CI 或外壳里跑），
    本机既不编译也不运行，所以这里只保证"按内容寻址"这一句是真的 —— 写在产物 `rule` 里。
    6 条源码变异体（不按哈希认、把同名闸门关掉、认领位翻掉、hint 当声明、体积不符仍认领、
    生图 337 行不进表）全被六条探针抓住，另有"不改坏时六条全绿"的正向对照。
    重数：`python3 tools/sideload_lookup.py --selftest`、`--check`、
    `python3 tools/sideload_lookup.py --lookup <你的文件>`。

27. **"有哪些后端可换"必须是一张表，不是 ADR 里的一句回忆**：`tools/backends.py`
    → `pins/backend-matrix.json`。产品约束第 5 条（按检测到的后端分层）与第 3 条（runtime 可替换）
    在此之前只在 ADR-0001 里写着"36 个产物含 win-cuda / ubuntu-vulkan …"—— 那句话这次被**逐 asset
    复核了**（按 `pins/payload-plan.json` 钉的三个 ref 现取 release 表，name / size / digest 三项照抄）。
    实测：三个 ref 共 **57 个资产 = 47 个 runtime + 10 个不是 runtime**（7 个 `cudart-` 伴生包、
    2 个 xcframework、1 个 webUI），后端 10 种：cpu / cuda / vulkan / rocm / sycl / openvino /
    opencl-adreno / qnn-snapdragon / blas / `unknown_from_name`。分层能用的证据是体积：
    sd 在 win-x64 是 CPU **16.9** vs CUDA **327.7** MiB、llama 在 linux-x64 是 CPU 17.1 vs
    vulkan 29.2 MiB —— 带集显的普通笔记本那条路确实有产物可换，不再靠推测。
    **三处不猜**（都写进 `gaps`，不是脚注）：① macOS 的资产名不标后端，所以 mac 上有没有加速
    这张表只能说 `unknown_from_name`（不许为了表格好看写 metal，也不许当 cpu）；
    ② whisper 的 `bin-x64` / `blas-bin-x64` 名字里不写平台，于是"按资产名挑后端"对它做不到 ——
    `payload-plan` 那侧按包内条目推断并标了 `inferred_platform=true`，这张表原样记账；
    没标就红（推断被写成事实是这一路最常犯的那类错）；③ oar-ocr 没有预编译产物，加速轴是
    Cargo feature / EP，不在这张表的范围里。
    两个真实的拼法坑被分类器吃下：同一个 CUDA 在 sd 那里既写 `cuda12` 又写 `cu12`（后者只在
    `cudart-` 伴生包里），`Darwin` 里含 `win` 子串 —— 不给词界就会把 macOS 的包分到 Windows 那一列。
    认不出的词（上游哪天加 hip / mtl）一律 `unknown_token` 并红，不许静默归成 cpu。
    **一格必须装着这一档后端的全部资产**（41 个格子 / 45 个格子条目 / 47 个 runtime 行——
    差的 2 个正是 whisper 那两颗名字里不写平台的包）。第一版写成 `cell[后端] = 一行`，
    `llama-…-win-cuda-12.4-x64.zip` 被同档的 `cuda-13.4` 静默覆盖 —— 而"57 个资产 = 47 个 runtime"
    那个总数照样数得对，所以**总数一致不代表格子没丢东西**。现在每格是按版本+资产名排序的列表，
    `格子条目` 单独计数并进了漂移判据（条目数变了就红）；实测 4 个格子各装着两颗：
    llama win-x64 cuda `12.4`+`13.4`、llama linux-x64 cuda `12.8`+`13.4`、
    llama linux-x64 sycl `fp16`+`fp32`、whisper win-x64 cuda `11.8.0`+`12.4.0` ——
    驱动只到 12.x 的那台机器需要的恰恰是被覆盖掉的那一颗。同一版内层循环还借用了 `rows`
    这个名字，把产物里的全表换成了一格（`rows` 从 47 行缩成 1 行）：现在工具里有一条
    "rows 必须逐个等于分类出来的 runtime 名字"的自洽检查（车道现取那一步的保险），
    产物形状另由单测钉住，两道不是同一件事，所以两道都要留。
    **缺口点的是颗，不是点名次数**：行侧（这张表里所有名字不写平台的 runtime）与 payload-plan
    那侧（选中且名字不写平台的那些）会点名同一颗，第一版把两次的点名直接拼起来，
    于是 2 颗写成 3 颗 —— 而格子数、总行数那几格都看不出来。现在按 `(引擎, 资产)` 去重，
    `test_gap_names_each_asset_once` 钉住产物里那一格只有两颗。
    判据的承重：7 条源码变异体（关掉哨兵、关掉与 payload-plan 的钉值比对、不要求标注推断、
    清空后端地板、一格只留最后一行、去掉词界、缺口点名不去重）× 10 条探针全被抓，另有"真实捕获必须 0 问题且格子逐个对得上"的正向对照；
    夹具是 GitHub API 的真实产物表（`tests/fixtures/backend_assets.json`，57 行 name/size/digest，
    sha256 记在 `.meta.json` 并由测试复算）。车道 `backends`（engine-live 第 22 个 job）第四步
    现取一次再对表：上游加档、撤档、改拼法都会红。
    重数：`python3 tools/backends.py --selftest`、`--check`、
    `python3 tools/backends.py --fetch --captures <路径> --check --captures <路径>`。

28. **"换哪一档、要多下多少、要多占多少盘"都得是表，不能等外壳里凭估计**：`tools/backend_choice.py`
    → `pins/backend-choice.json`。它有三份输入，全是**别家已经落盘的产物**（矩阵给有哪些档、
    `payload-plan` 给默认档、`runtime-parity` 给落地体积），所以这张表不许自己挑默认档、
    也不许自己估体积 —— 逐行等于上游那三张表才算对（探针「默认档搬对」钉这条，
    把"改成自己挑一颗"那个变异体当场抓住）。
    实测 **16 个 引擎×OS / 41 个格子 / 45 个可选档 / 10 档 CUDA / 7 份 `cudart-` 伴生包**。
    三条只有把两张表并起来才看得见的数：
    ① llama 在 win-x64 从 CPU 默认档换 CUDA 12.4 是 **+236.3 MiB 本体，另加 373.3 MiB 伴生包**
    （伴生包比本体还大），而换 **Vulkan 只 +12.0 MiB 且没有伴生包** —— "有独显的普通笔记本最便宜的
    那条加速路是 Vulkan"这句话，此前没有任何产物撑得住；
    ② whisper 的三档 CUDA（win-arm64 `13.4`、win-x64 `11.8.0` / `12.4.0`）**没有对应伴生包**
    （10 − 7 = 3，实测就是这三档）→ 进 `cuda_rows_without_companion` 并带 `because`：
    **"没有"与"不需要"是两句话**，与 `no_acceleration_named` / `missing_cell_default` 拆开记是同一条纪律；
    ③ sd 的伴生包写 `cu12` 而 runtime 写 `cuda12` —— 拼法不同但必须配上同一颗（单独一条用例钉住）。
    `cpu_fallback` 四态（`named_cpu` / `only_unnamed_asset` / `name_does_not_say` / `absent`）
    **由格子算出来，不许手写**：whisper 在 win-x64 那颗 CPU 包名字里不写平台 → `only_unnamed_asset`，
    同时它**不在按名字分出来的可换清单里**（`default_outside_cell` 实测 1 条，`default_in_cell: false`）；
    llama/sd 在 macOS 是 `name_does_not_say`（不断言 Metal）；`payload-plan` 里 whisper 在 mac 那一行
    **连 engine 都没有**（上游无产物）→ 记进 `missing_cell_default`（实测 1 条）。
    一张只说"有哪些档"的表还会骗人第二回：**4 个 引擎×OS**（llama linux-s390x、whisper
    linux-arm64 / linux-x64 / win-x86）按名字**分不出任何加速档** → 点名进 `no_acceleration_named`，
    而 llama 与 sd 在 win-x64 / linux-x64 有没有加速档是**硬地板**（把那一格的加速档撤光就红）。
    这张表**故意不说**的写在 `rule.driver`：这台机器的驱动能不能吃到某一档 CUDA，要运行时问
    `nvidia-smi`，属外壳 #25 —— 与"有哪些档可换"仍是两句话。
    **落地体积这一列是第四个会骗人的数**：下载量小不等于占盘小。逐颗从 `runtime-parity` 取
    （那颗包成员解压后大小相加；整包全量口径 —— 出包只收 server 后默认档 42.8，
    可选档的收窄差值未逐档重算），默认那颗作减法基准，实测 llama 在 win-x64：
    CPU 落 **47.9** MiB → Vulkan **87.5**（+39.6）→ CUDA 13.4 **190.7**（+142.8）→
    CUDA 12.4 **598.3**（+550.4）→ ROCm **1148.7**（+1100.8 MiB）—— 换 ROCm 是多占 1.1 GiB，
    不是"多下 228 MiB"那么简单；whisper 在 win-x64 换 CUDA 12.4.0 落 **1161.2** MiB（+1139.1）。
    45 个可选档里 **29 个有数、16 个未知**（都是没读整包的 tar.gz 可选档）：未知的写
    `landed_mib: null` + `landed_note`，**不许写成 0**，也不许出现第三种来源（既没登记读到、
    也没登记未读 = 两份产者对不上，直接红）；真装的默认档必须有落地体积，那是减法的基准。
    判据的承重：**12 条源码变异体**（配不上的伴生包被静默吸收、一档对上两颗、没哈希的档进表、
    默认档凭空消失、把推断写成事实、CPU 退路没了不报、默认档改成自己挑一颗、输入形状漂了不报、
    分层地板清空、体积差不进比对表、落地体积未知被写成 0、默认档缺落地体积不报）×
    **16 条探针**全被抓；另有一条链式核对：这张表的输入本身还能从重算的 release 夹具复现
    （夹具 → 矩阵 → 换档表，三环各自 0 问题）。
    车道 `backends`（engine-live 第 22 个 job，现在 **19 步**）里那四步换档步骤会拿**现取**那份矩阵重算一遍：
    上游补发或撤掉某个 CUDA 版本时，先红的会是伴生包配对而不是体积。
    重数：`python3 tools/backend_choice.py --selftest`、`--check`、
    `python3 tools/backend_choice.py --matrix <现取那份> --check`。

29. **"换个 asset 就换个后端"的前提是那一档里真有外壳要 exec 的那个文件**：`tools/runtime_parity.py`
    → `pins/runtime-parity.json`。约束第 3 条（runtime 可替换）与第 4 条（三项起常驻 server）
    叠在一起才成立：如果 CUDA 那一档只发了 CLI，换过去就不是"换个文件"而是另一种活法。
    **这一层只用只读元数据证**：zip 用 `tools/zip_peek.py` 的 HTTP Range 读中央目录
    （每颗约 1 MiB 尾部，不整包下载、不解压、不运行 —— 硬约束没被绕开，也没被顺手扩大成"连看清上游发了什么都不许"）。
    实测 **47 个 runtime 资产 / 30 颗读到成员表 / 30 颗都带着 server / 0 档缺 server**：
    llama 在 Windows 的 11 颗（cpu / cuda 12.4 / cuda 13.4 ×x64+arm64 / vulkan ×2 / rocm / sycl / openvino / opencl-adreno）
    都同时有 `llama-server.exe` **和** `llama-server-impl.dll` 成对，whisper 的 8 颗都有 `whisper-server.exe`，
    sd 的 8 颗都有 `sd-server`（Windows 带 .exe）。**每一档都能起常驻服务**这句不再是推测。
    三条边界的形状：
    ① `.tar.gz` 读不了中央目录（gzip 流不可随机寻址），只能取整包读成员表 —— 这一层**只为真装的
    默认档付那份流量**，且字节数与整包 sha256 都按 `payload-plan` 已经量过的数复核（3 颗全对上）；
    剩下 16 颗可选档不读 → `read: false` + `not_read` 点名"该由出包车道证"，
    **绝不写进 server_gaps**（未知冒充缺席就是谎称知道）；
    ② 8 颗默认档 **全部证过**：5 颗走 zip 中央目录，3 颗走整包读并复核 digest
    （llama 的 linux-x64 与 macos-arm64、whisper 的 linux-x64）—— 之前那句"3 颗要等出包车道"
    在这一轮被证掉了，代价是 40,629,147 B（38.75 MiB）的一次性读取，而不是等一条从没跑过的 CI；
    ③ `whisper-blas-bin-x64.zip` 名字里不写平台、payload-plan 又没装它 → `read_via: zip_unnamed_os`
    并停在未知；同一颗 whisper 的 CPU 默认包 `whisper-bin-x64.zip` 的 OS 是**从 payload-plan 取的**，
    不是猜 win-x64（把这条兜底改成猜的变异体被探针「平台从 plan 取不猜」抓住）。
    读到的 tar 还顺带量到库闭包：llama linux 28 个 `.so`、llama macOS 35 个 `.dylib`、
    whisper linux 18 个 `.so` —— "$ORIGIN 找运行库"那句现在有数可依。
    `server_sibling_lib`（exe 与同名 impl 库成对）**只对 llama 求值**，whisper/sd 写 `null` 不写 `false` ——
    那个包形状只在 llama 上被量过，套上去就是替上游编判据。
    另一条独立证据顺手落进表里：**sd 在 macOS 有 `sd-server` + 1 个 `.dylib`**（Darwin 包 5 条成员），
    与"whisper 在 macOS 只有 xcframework、没有可执行产物"（#29）形成对照 —— 生图在这台机器上有路，字幕没有。
    同一份成员表还量出**落地体积**（`landed_mib` = 各成员解压后大小之和，不含目录项）——
    换档要多占多少盘就取这个数。它与 `payload-plan` 独立量过的 `uncompressed_mib` 逐颗对表
    （8 颗真装的默认档全在 0.2 MiB 内）：**两个生产者各量一遍再对表**，比只信自己强；
    读到的 zip 还要满足"落地 ≥ 压缩后"，反过来就是读法坏了。
    可执行名**只有一个来源**（`launch_plan.SERVER_BASE`，Windows 加 `.exe`），源码里出现字面量
    由一条 ast 扫描拒绝（docstring 允许解释，代码常量不允许）；中央目录压缩后合计与矩阵声明的
    体积逐颗对表（30 颗全在 1.5 MiB 内），差太远=量的不是同一份东西。
    判据的承重：**11 条源码变异体**（捕获少一颗不报 / 空成员表不报 / 体积不符不报 / 默认档缺 server 不报 /
    默认档缺 server 降级成备注 / tar 被当成读过 / 默认档整颗消失不点名 / 真装的 tar 没读也不报 /
    tar 的整包 digest 不符不报 / 平台改成猜 win-x64 / `has_server` 不进漂移比对表）×
    **15 条探针**全被抓，另有"可选档少 server 只点名不报警"这条防止判据过度报警的正向对照。
    捕获钉成 `tests/fixtures/parity-captures.json`（31 颗 / 46,530 B，sha256 记在 `.meta.json`
    并由测试复算字节），所以没有 runner 也能离线重算。
    车道 `backends` 随后四步跑它：判据本身 / 单测 / 离线对表 / **现读 28 颗 zip 的中央目录与真装默认档那 3 颗 tar 的成员表，再对一次**
    —— 上游重打包、把某档的 server 换掉或删掉，就在这一步红，而不是等外壳去 exec 一个不存在的文件。
    重数：`python3 tools/runtime_parity.py --selftest`、`--check`、
    `python3 tools/runtime_parity.py --fetch --captures <路径> --check --captures <路径>`。

30. **runtime 的版本轴也得是表：能升到哪、能退回哪、每颗能不能校验**：`tools/runtime_versions.py`
    → `pins/runtime-versions.json`。约束第 3 条写"引擎按 runtime 打包、可替换"，矩阵却只覆盖
    **钉住的那一个 ref**，版本轴一句都没有 —— 外壳的"更新 runtime / 回退 runtime"就靠这张表。
    **窗口是翻出来的，不是写死的**：每个仓库从最新往下翻，翻到钉住那颗为止（100/页 × 至多 5 页，
    再裁到至少 25 个）。llama 一天发 6 个 release，固定 12 个的窗口会装不下钉住那颗 ——
    窗口不含钉值时"能退回哪"这句就不成立，而表看起来仍然完整。
    实测三件事：
    ① **本次把 llama 从 b11727 重钉到 b11798 时（2026-10-21T17:44Z 现取），它是窗口里最新的
    带产物版本（0 个更新；上游随时会再发新版，这张表只报不跟）**；
    **sd 还差 1 个（master-975 → master-982），覆盖面没变**（`os_lost`/`os_added` 都为空）——
    "能升"与"升完还发同样的包"是两个数，都得在表里；
    ② **whisper 的 `releases/latest` 是 v1.9.5，0 个产物**，真正能装的 b5454 正是我们钉的那颗；
    窗口里 9 个只有源码的标签，`latest_runtime_tag` 与 `pinned` 相同。外壳按 `releases/latest`
    取 runtime 会"检查更新成功、装不上任何东西"；
    ③ **回退不是翻到最老**：v1.7.6 之前 13 个 release 上游根本没发 digest（v1.6.0、v1.5.4…），
    所以 whisper 的可校验候选只有 16 个、`oldest_verifiable_tag` = v1.7.6。没有 digest 的版本
    单列在 `unverifiable_tags` 里，不冒充候选，也不当噪音删掉。
    窗口内 75 个 release / 带产物 66 / runtime 资产行 1021 / 附件 7（`whispercpp.jar.zip` 这类
    附属文件按扩展名先排掉，否则会被 `classify` 当成"认不出的后端词"）/ 认不出的词 0 / 可校验 66。
    钉住那颗的 47 颗产物名与 `backend-matrix` 逐颗相等（同一个 release，两张表不许漂）。
    **漂移只报不跟**：`capability_delta` 记"从钉住升到最新"的 OS 增减，并写明这不是自动动作；
    新版本若丢掉我们真装的 OS 直接红。
    18 条源码变异体 × 24 条探针（含"0 产物的标签也当候选"、"verifiable 不看 digest"、
    "latest 是否带产物不进漂移比对"、"真装 OS 丢了不报"、"live 口径把新版当红"）。捕获
    `tests/fixtures/release-window.json`（259,775 B，sha256 记在 `.meta.json` 并由测试复算字节，
    只有 tag / published_at / asset 的 name+size+digest 三种元数据），没有 runner 也能离线重算。
    车道 `backends` 最后四步：判据本身 / 单测 / 离线对表 / **现取三个仓库的窗口核对钉值**。
    最后一步用 `--live` 口径：**只核对钉住那颗还在、产物没被重打包、真装 OS 没变**；
    上游在钉住之上又发新版本**不算红** —— 这条不是假设：抓完这张表约半小时后 llama 又发了
    5 个版本（b11748 → b11753）。把"有新版本"当红，这条车道会永远红，红就不再是信号；
    新版本只在下一次显式重抓时进表（漂移只报不跟）。
    重数：`python3 tools/runtime_versions.py --selftest`、`--check`、
    `python3 tools/runtime_versions.py --fetch --captures <路径> --check --live --captures <路径>`。

31. **外壳的合同也得是表：谁有钥匙、没钥匙的引擎怎么活、runtime 放哪**：`tools/shell_contract.py`
    → `pins/shell-contract.json`。验收第 10 条的"三条"如果只停在上一条的启动表里，写外壳的人
    仍然要自己决定"字幕要不要常驻""端口谁来挑""目录里放版本还是放 latest"——于是同一件事
    会有第二种答案。这张表只从**已提交的五张表**推导（launch-plan / backend-choice /
    runtime-versions / payload-plan / ocr-build-surface），不读外壳源码，也不引入新事实：
    ① **入口**：外壳自己不监听任何端口（0 个），UI 只走 Tauri IPC，引擎 HTTP 只由外壳进程发；
    ② **鉴权**：只有 llama 有 `--api-key`；token 每次起引擎现生成（32 字节 OS 随机数转 hex），
    只存内存；whisper / sd 上游没有钥匙可给，所以**不许常驻** ——
    `on_demand` 且 `idle_timeout_secs=300`，关窗即停；OCR 是 `one_shot`（一次任务一个进程）；
    ③ **起的正是下的那一颗**：exec 前逐文件对 `payload-manifest.json` 的 sha256，不符拒绝启动；
    ④ **runtime 目录**：`<data>/runtime/<os>/<engine>/<version>/<asset-stem>/`，版本取版本轴的钉值，
    换后端只是换 asset 目录，回退只是换 version，标签里的非安全字符统一替换成 `_`。
    默认档不是抄来的：三个 server 能力 × 各自 OS 格 = **8 颗**，逐颗与 `payload-plan` 的行、
    钉住 release 的产物名与 digest 三方对表；后端选项逐格来自 `backend-choice`；OCR 的版本
    取自 `ocr-build-surface.json` 的 `ref`（`84817f58`），而不是另写一个常量。
    **没做的照实写**：`shell/ui` 已经存在，所以产物记 `ui_checked=true`、`ui_dir="shell/ui"`，
    扫描器会拒绝 fetch/XHR/WS/硬编码回环地址，并核对 UI 调用的每条 IPC 都注册在
    `generate_handler!` 里；OCR 的二进制由 CI 现编，
    出包车道还没把它写进 `payload-manifest.json`，这条缺口以 `ocr-manifest` 记在产物里，
    不删也不当成已覆盖。14 条源码变异体 × 24 条探针（每条判据都能被单独改坏：常驻不带钥匙、
    llama 丢 CORS、OCR ref 不是 commit、默认档不在 release、digest 对不上、check 不看鉴权/缺口/
    UI 扫描位置、runtime 目录不写版本、whisper 的多段字段被改名……），19 条单测，CI 静态门禁跑
    `--selftest` 与 `--check`。
32. **这张合同的每一条都有代码了，但代码一行都还没被编译过**：`shell/core`（Tauri-free，可离线测）
    把上面 ①–④ 写成可执行逻辑 ——
    ① 生产代码里只有一次 `127.0.0.1:0` 探测，绑定后立刻 `drop`，不监听端口；
    ② token 字节数读合同、由 OS 随机数生成，只进请求头与（llama 的）argv，不进环境变量；
    whisper/sd 只能是 `on_demand` 且 300 s 空闲回收，调用时会话先验 `inputs` 文件在场；
    ③ `ensure_running`/`ocr` 都必须先过 `verify_payload`（清单存在、OS 相符、逐文件字节+sha256）
    才轮到 `spawn`，且全文只有一处 `Command::new(&plan.program)`；
    ④ argv 的占位符（`{bin}/{port}/{token}`）与模型路径全部由合同 + 启动表现渲染，
    生产源码里不许出现引擎名、引擎旗标或引擎端口（`tools/shell_source.py` 按词表扫，连
    `llama|whisper|sd-server|oar-ocr|ggml` 都算）；HTTP 层自己实现（chunked/trailers/512 MiB 上限）、
    multipart、base64；对话请求强制 `stream=false`，字幕的 `file` 字段与 `response_format=srt`
    都从合同读，生图的 `poll_url` 只许回环地址。
    `shell/src-tauri`（Tauri 2）只做装配：`AppState` 持有 supervisor 与数据根，8 条 IPC 命令
    （status / data_root / start_engine / stop_engine / chat / transcribe / image / ocr）全部把
    阻塞调用丢进 `spawn_blocking`，返回结构一律 `serde`；`status` 每行带 `available`
    （合同里有没有当前平台这一格），UI 据此禁用按钮并写"本平台暂不提供"，而不是点了才报错；
    UI（`shell/ui`）只调
    `window.__TAURI__.core.invoke`，没有网络 API，中英两套文案；`icons/icon.ico` 是
    Windows 资源段要的那一颗（`tauri-build` 缺它会直接报错，而不是跳过）。
    **证据的边界**：`.github/workflows/shell.yml` 现在是两个 job —— `core` 与 `app` 各在 3 OS 跑
    `cargo test --all-targets`（`app` 在 Ubuntu 先装 WebKitGTK；两个 job 都上传首轮的 `Cargo.lock`）。
    2026-10-21 真跑的结果：`core` 三档各 **70 条**全过，`app` 三档各 **3 条**全过（Ubuntu 首编 1m30s）；
    首轮 `Cargo.lock` 已作为 `shell-core-Cargo.lock` / `shell-app-Cargo.lock` artifact 产出，
    但还没提交进仓库。离线另证 `python tools/shell_source.py --check`（问题 0 条）与 14 条 Python 单测。
    这次不把"写了"说成"过了"：2026-10-21 起 `app (ubuntu-latest)` 在 Xvfb 里真起二进制并等到
    窗口（run 39658522639：标题 `Local AI Studio`、980×720），随后用 `tauri-driver` 2.1.0 +
    `WebKitWebDriver` 连上窗口执行页面 JS（run 39663865685，commit `bb68f29`）：`invoke('status')`
    回 4 行（对话 resident / 字幕 on_demand / 文字识别 one_shot / 生图 on_demand，Linux 四格
    `available=true`），`invoke('data_root')` 回 `/home/runner/.local/share/top.lilyco42.local-ai-studio`；
    页面隐藏了 IPC 缺失提示、状态条是 `对话 · 未启动`、`#data-root` 与返回值逐字相同，另有一组
    “假能力名必须被拒”的已知坏对照。边界：只有 Ubuntu，没起引擎、没联网，证的是 IPC 桥与首屏渲染。
33. **出包产物到外壳目录之间的那一步补上了**：出包车道的产物是平铺的 `dist/<os>/<engine>/`，
    外壳只认 `<data>/runtime/<os>/<engine>/<version>/<asset-stem>/` + 根部的 `payload-manifest.json`，
    此前没有工具把两者接起来。`tools/stage_payload.py` 只从 `pins/shell-contract.json` 的
    `asset_dir`（没有就 `version_dir`）推导落点，逐文件复核体积与 sha256 后复制，
    目标目录已有清单外文件时**拒绝而不是覆盖**，文件名带 `/` `\` `..` 一律拒绝，
    `payload-manifest.json` **最后**才落盘（半套目录不会配上一份完整清单）；`--check` 复核已摆放目录，
    `--selftest` 用合成合同/合成包证明 13 条判据会红，`tests/test_stage_payload.py` 12 条
    （含真实合同里 whisper 的落点、改合同落点必须跟着改、第二条引擎失败时根部无清单）。
    CI：`package.yml` 在三档负载解包后、上传前各摆一次并 `--check`（不执行任何二进制）。
    同一 job 接着跑 `tools/payload_intake.py`：读摆好的数据根，逐文件给出
    `payload/<os>/<数据根相对路径>` 对象键、dest、sha256、bytes，根清单标 `land: last`；
    `--check` 重算并逐键相等，拒绝多余/缺失、两个引擎同目标、越界前缀，输出不含绝对路径。
    `tests/test_payload_intake.py` 21 条（真实合同落点、两个数据根同一份字节、13 条合成自测、
    6 条源码变异体，以及消费端重放：删掉原数据根后只凭对象字典按清单还原，按外壳
    `verify_payload` 的顺序逐文件验过；坏对象/缺对象/越界或重复落点时根清单不许落）。这份
    `delivery-manifest.json` 随 artifact 上传，**没有写桶或生产发布动作** —— 取数目前只发生在
    CI 重放；首启路径的发布落点仍是 #57 的 A/B。
    外壳侧新增 `shell/core/src/delivery.rs`：解析同一份清单（schema/os/前缀/键与落点绑定/哈希/
    总量），按 `fetch` 回调取字节、长度与 sha256 双验、同目录临时文件 + 重命名、根清单最后落、
    已正确的文件跳过；清单消费侧 14 条离线测试覆盖篡改、短缺、坏根清单和路径逃逸。
    取数字节由 `shell/core/src/payload_fetch.rs` 接上：多个基址按顺序换源、按对象键拼 URL、
    断流后用 `.part` 长度发 Range 续传、哈希不符丢掉重下；基址只收 https（`127.0.0.1`/`localhost`
    的 http 仅给 CI 重放，公网 http 在解析基址时就拒）。取数侧 11 条离线单测用假 Fetcher 覆盖换源、
    续传、服务端忽略 Range、截断后继续和路径逃逸，不需要网络。
    **证据边界**：2026-10-21 `package.yml`（run 39632180385）在三档 OS 上真跑了第 8 步（摆位 + `--check`）
    与第 9 步（交付清单），三档 `engine-payload-<os>` 都上传成功；同日 run 39654853807 在**三档原生 runner**
    上做了消费者重放：删掉摆好的数据根，只凭清单装进空目录（linux-x64 94 个文件 / 156.3 MiB、
    win-x64 58 / 105.0、macos-arm64 33 / 158.4，均 0 跳过），再从装好的树起 `llama-server --help`
    （三次 rc=0，输出含 `--port`）。所以"真实上游包能按合同落位"与"清单足以还原能起引擎的树"都有 CI 证据；
    出包车道现在还会把对象目录当 HTTP 远端再取一遍（`--base-url`；run 39656642754 三档均 0 跳过、
    同数落盘、`--help` 三次 rc=0，但这段是回环明文，真 HTTPS 基址仍未在重放里跑过），
    仍没有的是**发布落点（R2 写）与 IPC/UI 入口**（#57），以及用户个人电脑上的"装完真能跑"。
    OCR 仍不在出包清单里，`not_in_manifest` 会点名 `文字识别/oar-ocr` —— 已知缺口，不是这一步的失败。
34. **安装包本身：三平台各一个未签名包，只装壳**（`.github/workflows/installer.yml` + `tools/make_icons.py`）：
    矩阵 Windows `nsis` / macOS `dmg` / Linux `deb`；触发是 push main 上 `shell/**` 或自身 +
    `workflow_dispatch`；`permissions: contents: read`、并发组、checkout 与 tauri-action 全按 SHA。
    CLI 坐标写死 `tauriScript: npx --yes @tauri-apps/cli@2.12.3` —— 读过 action 的 `dist/index.js`：
    检测不到 CLI 时它会 `npm install -g @tauri-apps/cli@v2`，浮在一个大版本上，不能留给它。
    `tauri.conf.json` 的 bundle 打开、targets 三档、图标 5 件（32/128/128@2x PNG + `.icns` + `.ico`）。
    图标不是手画的：`make_icons.py` 从仓库已有的 `icon.ico` 用纯 stdlib 派生 —— 解析 DIB
    （`biHeight` 可能是 2×h，多出来的是 AND 掩码）、**预乘 alpha** 双线性缩放（不预乘会把透明边缘拉黑）、
    zlib 写 PNG、ICNS 用 `icp4/icp5/icp6/ic07/ic08/ic09` 六块 PNG。
    `--check` **不许只逐字节比**：压缩流随 zlib 实现变（本机实测 zlib-ng 1.3.1 与 zlib 1.3.2
    对同一份像素给出 6,389 vs 6,393 B），所以字节不同时解压后逐像素比；CI 上假红的正是这条。
    `tests/test_make_icons.py` 10 条（PNG 尺寸、ICNS chunk 覆盖、确定性、透明边缘无黑边、坏 ICO 拒绝、
    已提交图标可复算、篡改与缺件必须红、重压缩的 PNG/ICNS 必须认作同一张图、改一个像素必须拒）。
    车道最后一步断言矩阵 glob 真找到安装包且非空 ——
    `uploadWorkflowArtifacts: true` 成功不代表编出了东西。
    **证据边界**：2026-10-21 三格都真出包了（run 39633525944）：Linux `deb` 3,237,370 B、
    Windows `nsis` 2,076,484 B、macOS `dmg` 3,152,248 B；包未签名、未公证；CLI 版本与 deb maintainer
    （tauri-bundler 取 identifier 第二段 `lilyco42`）仍只是静态核对。安装包只装外壳：引擎落地
    win-x64 就是 104.7 MiB（只收 server；全量 115.6），权重 415 MB / 1.5 GB，都不进安装器。
35. **国内下载层：慢在网络，别慢在设计与重试**（`shell/core/src/download.rs` + `tools/download_plan.py` +
    `pins/download-plan.json`）：传输不引 TLS 依赖，也不把 URL 拼进 shell 字符串 —— `CurlFetcher`
    调系统 `curl`，每个参数一个 argv，URL 只出现在 `--url` 后面；`--speed-limit 65536 --speed-time 30`
    把"30 秒低于 64 KiB/s"判成断流，`--proto-redir =https` 禁止跳转降级。续不续传、换不换源是纯逻辑，
    由 `Fetcher` trait 注入，测试不需要网络。顺序只认实测吞吐（ModelScope 4.2 MiB/s、hf-mirror 0.25、
    GitHub release 0.014、huggingface.co 直连 0 仅作最后兜底），不认域名或模板；每源 2 次机会，
    206 追加、200 丢弃 `.part` 重下、416/404 保留进度换源、收满后 sha256 不符删 `.part` 从 0 重来；
    全源失败时保留已下字节并把每个源的失败原因拼进错误。落盘只在哈希通过后发生：
    `blobs/<sha>.part` → 复核长度与 sha256 → `blobs/<sha>` → 硬链接到 `<file>`；已存在但哈希不符的
    目标挪成 `<file>.corrupt[-N]`，不删除。
    `tools/download_plan.py` 从 `catalog/machine-budget.json` 的行出发，按 `repo + file + sha256`
    精确连到三张档位表里的候选源，生成 `pins/download-plan.json`：14 项、8 项默认安装、1 行 `(合计)`
    被点名为聚合值（123,297,373 B = OCR 全部语言 rec+dict，不是文件，不下载）；逐档默认权重
    4GB 3850.3 / 8GB 7752.1 / 16GB 11542.0 MiB，三档并集 20181.2 MiB **不是一台机器的数**。
    whisper 的 ModelScope 源按 `distributor_absent` 删掉（不再制造一个必 404 的候选），HF 直连排最后；
    OCR 三颗各有两条源：ModelScope（`pins/ocr.json` 的 `mirrors`，2026-10-20 整包下载后 sha256
    与 pin 相等）与 GitHub release，镜像的哈希/体积只要和 pin 差一位 build 就报问题；默认安装
    不允许单源（`default_install_min_sources: 2`）。`--check` 用同一份输入重算并逐键比对，
    `--selftest` 用十类变异证明会红（换源顺序 / HF 提前 / 聚合行混入 / OCR 丢 release 兜底 /
    默认安装单源 / 计划少一项 / OCR pin 哈希不符 / 档位表缺行 / 镜像哈希不符 / 镜像 URL 与
    repo+revision 推不出来）。`--probe` 是报告式的现探：URL 逐字取自计划（`known_absent` 没有 URL
    就不探，不按模板补），Range `bytes=0-0` 只读 1 字节，200/206 且真拿到字节才算通，并拿
    `Content-Range` 的总长与预算字节对表（206 的 `Content-Length` 是片段长度，不冒充总长）；
    2026-10-20T15:09:58Z 实测 37 个 URL 里 36 个通、36/36 总长与预算相等（modelscope 12、
    hf-mirror 11、huggingface.co 10、GitHub 3），唯一失败的是 HF 直连 `ggml-large-v1.bin`
    的 `SSL: UNEXPECTED_EOF_WHILE_READING` —— 它本来就排在最后。落地主机是
    cdn-lfs-cn-1.modelscope.cn 11 / www.modelscope.cn 1 / us.aws.cdn.hf.co 21 /
    release-assets.githubusercontent.com 3。可达 ≠ 吞吐：huggingface.co 直连整份仍是 0 B/s，
    排序只认 `RATES_MIB_S`。`tests/test_download_plan.py` 18 条（探针四条钉住"URL 逐字来自计划 /
    200 空正文不算通 / 总长不符算红 / 空列表不算全绿"，另一条把 policy 的 65536/30/2 与
    `download.rs` 的 curl 参数对表；另一条按 serde 语义重放整份计划 —— `schema` 是 u32、
    `bytes` 是 u64、速率有限、`file` 扁平且不重名，6 个变异各自必须红）。Rust 侧 `Plan::parse/from_file/item` 读同一份 JSON
    （重名必须报错），`download.rs` 20 条单测。
    **证据边界**：这组 Rust 单测已随 `core` 在 3 个操作系统上真跑（各 70 条全过，2026-10-21）；
    curl 在真实网络上的重试/续传行为仍只有 Fake fetcher 的逻辑证据；R2 镜像仍是唯一缺口（#23）。
36. **跟随上游 llama.cpp：定时只报不跟，重钉是一次显式动作**（`tools/follow_upstream.py` +
    `.github/workflows/upstream-follow.yml`）：上游一天发好几个包（b11727 → b11798 只隔了几天），
    把"有新版本"当红，红就不再是信号。车道每天 03:17 UTC 现取窗口，只有钉值坏了
    才红：钉住那颗从窗口里消失、产物被重打包（逐颗 name+size+sha256 对已提交捕获）、
    `backend-matrix` 与 `payload-plan` 钉的不是同一颗。新版本身退出码 0；候选丢我们真装的 target /
    出现认不出的后端词 / 没有上游 digest 只报 `blocked`，`--strict` 才当红。
    升级路径是显式命令：`--update --tag bNNNNN` 走 `payload_plan.rows_for` 的真路径量出三行
    （zip 读中央目录、tar 读成员表；只下载与读取，不执行），`--apply` 只替换 payload-plan 里
    该引擎的行。之后十份派生文件（3 份捕获 + 7 张表）只要还有旧 tag，退出码 2 并逐张打印重出
    命令 —— 半套重钉不许说成完成；自检用 8 类变异证明（含"换 tag 后别的引擎一个字节不许动"、
    "同 tag/回退必须拒绝"），`tests/test_follow_upstream.py` 20 条。
    **证据边界**：这条车道的 `schedule` 至今没有被真正触发过（这台账号的 cron 会晚 1–3 小时、整槽丢）；
    重钉前 2026-10-21T16:28Z 本地只读跑过一次 `--check --live`，读到 b11797、钉住 b11727 落后 50 个；
    重钉到 b11798 后 17:44Z 再跑一次：钉 b11798、落后 0、已最新、退出码 0 —— 证的是工具与
    GitHub API 的形状，不是 schedule 真会触发。
    同一套现取逻辑已在 `engine-live` 的 `backends` 末步真跑（101 个 release / 92 个带产物 / 0 问题）。

## 车道判据的出处（全部按**钉住的那个 ref** 核过，不是按 HEAD 记的）

| 车道 | 判据依赖的东西 | 出处 |
|---|---|---|
| `headless-inference` | 速率行 + 独立证明"真的吐了字"；关对话模式的真实拼法 | llama.cpp `b11798`：**库里** `src/llama-context.cpp:4657/4659` 会打 `%8.2f tokens per second`，但 **llama-cli 不调它** —— CLI 只在 `--show-timings`（`common/arg.cpp:1797`）下打 `[ Prompt: %.1f t/s | Generation: %.1f t/s ]`（`tools/cli/cli-context.cpp:655-660`）。判据因此两种写法都认，并另加一条与格式无关的证据：stdout 必须有非空白内容。抓不到速率时先怀疑格式（红字里顺带打 help 里的 timings/perf 开关），不再把结论写成"推理没跑起来"。关对话的真实拼法是 `-no-cnv`/`--no-conversation`（`arg.cpp:1907`），交互风险由 `< /dev/null` 兜死；`-t/-n/-ngl` 也都在 `common/arg.cpp`（**不在** `common/common.cpp`） |
| `headless-inference`（常驻 server） | 路由名、响应字段名、**服务二进制与 CLI 是不是同一个文件** | llama.cpp `b11798`：`GET /health` = `tools/server/server.cpp:280`（返回带 `"status"`）、`POST /v1/chat/completions` = `291`、`choices[].message.content` = `tools/server/server-task.cpp:422`（老 `/completion` 用 `choices[].text`，判据两种都吃）、`usage.tokens_predicted` = `340`。二进制是 `llama-server` **和** `libllama-server-impl.so` 成对的（zip/tar 条目表都核过），所以这一步自己 `find llama-server` 并带 `LD_LIBRARY_PATH`，不复用上一步挑中的 CLI。`tokens_predicted` 缺失时**只跳过不断言**（这个键历史上叫过 `completion_tokens`，写死会养出一条永远红的车道），非空文本那条才是硬证据 |
| `subtitle-proof` | `-osrt/--output-srt`、`-of/--output-file`、`-l`、`-m`、`-f`、`-t`；**产出文件叫什么** | whisper.cpp `b5454` 的 `examples/cli/cli.cpp:183/190/197/201/202/159`；不给 `-of` 时输出名从输入名推（同文件 `1136`）。**`-of` 要的是基名**：`fout_factory::open` 把扩展名直接拼在后面（`1165-1166`），写 `-of jfk.srt` 会得到 `jfk.srt.srt` —— 原车道就是这么写的，第一次跑必红且原因很怪。现在判据靠 `ls jfk*.srt` 发现真名，红字区分"命名变了"和"没产出字幕" |
| `subtitle-proof`（常驻 server） | 服务二进制在不在包里、就绪态怎么认、multipart 字段名、SRT 由谁渲染 | whisper.cpp `b5454`：`whisper-server` **实测在** `whisper-bin-ubuntu-x64.tar.gz` 深度 1（2026-10-20 整包列条目，且整包 sha256 == 车道里钉的 `a72becf1…`）；`GET /health` 加载期回 **503** + `loading model`（`examples/server/server.cpp:1218-1224`），就绪回 `{"status":"ok"}`（`:1221`）—— 所以探测写成 `curl -fsS` **并且**要求正文含 `ok`，少一样都能把"还在加载"看成"服务不行/服务行了"；`POST /inference`（`:62/826`）的音频字段必须叫 `file`（`:831`，缺了直接 400），其余参数一律走 multipart 字段（`:482-622` 清一色 `has_file`）；`response_format=srt` 时服务端逐段拼 `序号/起 --> 止/正文` 并以 `application/x-subrip` 回吐（`:1037-1041`），编号是 `i+1+offset_n` 而请求不发 `offset_n`，所以判据要求从 1 连续。判据本体是 `tools/srt_proof.py`（20 条单测：本仓库拼的 spec 形状 + 另外两个项目留下的真 SRT 当独立生产者语料） |
| `image-proof` | 参数写在**产物的 tEXt** 里而不是日志里 | sd.cpp `master-975-95b3090`：`examples/common/common.cpp:3239/3269` 拼 `Steps:`/`Size:` → `examples/common/media_io.cpp` 交给 `stbi_write_png(..., parameters)` → `thirdparty/stb_image_write.h:1210` 落成 `tEXt`、关键字 `parameters` |
| `image-proof`（常驻 server） | 服务起不起得来、异步任务的状态机、图在响应的哪个字段、**请求体的参数有没有被吃进去** | sd.cpp `master-975-95b3090`：监听旗标是 `--listen-ip/--listen-port`（`examples/server/runtime.cpp:193/198`，默认 127.0.0.1:1234）；**模型先加载完才 `listen`**（`examples/server/main.cpp:92-98` 建上下文、`:152` 才 listen），所以与 whisper 相反——这里能答就是就绪，探测用 `GET /v1/models`（`routes_openai.cpp:253`）；`POST /sdcpp/v1/img_gen`（`routes_sdcpp.cpp:644`）成功回 **202**（`:695`）并给 `poll_url`（`:693`），车道**按服务端给的地址轮询**而不自己拼；状态名 `queued/generating/completed/failed/cancelled`（`async_jobs.cpp:73-88`），`failed/cancelled` 也要跳出轮询，否则红字看起来像超时而不像任务失败了；图在 `result.images[].b64_json`（`:190/192-194`，b64 由 `:284` 产出）。**这一步刻意不给 `sd-server` 任何生成旗标**：请求体是在 server 的 CLI 默认值之上覆盖的（`routes_sdcpp.cpp:417`），上游默认 20 步/512x512，产物元数据却是 `Steps: 3`、IHDR 是 256x256，才能证明参数从 HTTP 进去了。判据与 CLI 那条共用 `tools/png_proof.py`（25 条单测，含"两处判据逐案同生同死"的一致性用例），步数键是 `sample_params.sample_steps` 且逐键覆盖（`common.cpp:2204`）；元数据默认嵌进产物靠 `common.h:219` 的 `embed_image_metadata = true` |
| `ocr-proof` | 包名 `oar-ocr-cli`、二进制名 `oar`、`--det/--rec/--dict` 必须一起给 | commit `84817f58…`（**不是** tag `v0.10.0` —— 那个 tag 里没有 CLI）：根 `Cargo.toml` members、`oar-ocr-cli/Cargo.toml` 的 `[[bin]] name = "oar"`、`src/args.rs` 的 `requires_all`/`conflicts_with_all` |
| `ocr-languages` | OCR 的语言轴能不能「下载前就信」：产物在哪个 tag、两家分发方给不给同一个哈希、字典与权重是否成对 | oar-ocr `84817f58`：`oar-ocr-core/src/core/download/registry.rs:10` 写 `MODELSCOPE_REPO = "greatv/oar-ocr"`（第二个分发方就是上游自己的 ModelScope 镜像）、`:4` 声明条目按名字排序是为了二分查找（所以工具替上游守着这条排序，乱序当场抛）；`docs/models.md:116` 明写 v3 各语言字典**不在 auto-download 注册表内**，要另从 PaddleOCR 取 —— 那三份 pin 因此只能「按 commit 自算」。查找一律按 models.md 链接里的 **tag** 定位：`ppocrv5_latin_dict.txt` 在 v0.1.0 是 1,634 B / `3c0a8a79…`、在 v0.3.0 是 2,616 B / `ccbcc457…`，按名字找第一版就把拉丁语字典钉错了（`--live` 报「来源哈希不一致」才逼出来） |
| `recommendations` | 「文件要整只、要塞得进本档」这两条判据的依据不是 file:line，而是**同一次运行里两家给回来的文件清单本身**（切片入库，`--live --check` 每次都重测） | `ggml-org/Qwen3-8B-GGUF` 的 6 条 `.gguf` 只有 BF16（16,388,044,096 B）、Q8_0（8,709,518,656 B > 8 GB 机器总内存）和 `dflash-*`/`dspark-*` 草稿（1,120,250,336 B —— 按大小它反而最先合档，所以形状判据必须排在大小判据**前面**）；同一次捕获里 ModelScope 对 7 个 `mradermacher/*` 仓库各返回 **0** 条 `.gguf`，而对 unsloth 给 7–26 条，所以那 7 行只能是 `single`（第二家不镜像重打包方），UI 必须说"下完自己算" |
| `subtitle-tiers` | 字幕档位表的四个来源与"哪一条不许我们替上游补数字" | whisper.cpp `b5454`：`README.md:138-144` 是 `Model \| Disk \| Mem` 表（`Mem` 列 tiny `~273 MB` → large `~3.9 GB`，`:140` 已进 `pins/citations.json`）；`models/README.md:63` 明写 **Models are multilingual unless the model name includes `.en`**（顺序按这条排）、同一文件 `:67` 明写 **`for-tests-` 前缀的模型文件是空的**（收集清单按名字排掉）；`models/download-ggml-model.sh:9` 写死宿主 `src="https://huggingface.co/ggerganov/whisper.cpp"`（下载源从脚本读出来，不硬编码在我们代码里）。哈希只有一家给：同一次捕获里 HF `tree.lfs.oid` 有 47 条、ModelScope 对该仓库 **0 条** → 整轴 `single`，`ggml-tiny.bin` 的 oid 与 `pins/models.json` 那颗 pin 逐字节相同（正向对照）。上游自己不一致的两处也记进产物 `upstream_deltas`：白名单有 `small.en-tdrz` 而宿主无此文件；宿主有 4 个 `.en-q8_0` 而白名单没列 |
| `image-tiers` | 生图这一档**为什么只能给算术下限、不能给内存承诺**，以及三条来源各自的位置 | sd.cpp `95b3090`：`docs/performance.md:143` 分段执行的容量检查留 `128 MiB of headroom`、`:145` 保住 `512 MiB device` 底线、`:102` 的 `--offload-to-cpu`（那一行 `--offload-to-cpu` 在文件里命中 6 次，所以进表的是 "saving VRAM without reducing generation speed" —— 子串不唯一就不配进这张表）、`README.md:157` 把内存/显存这件事推到这份文档（**上游给的是运行时策略，不是一张静态表**）；`docs/sd.md:5` 明写 SD1.5 的出处是 `huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5` 的 **safetensors** —— GGUF 量化全在社区仓库，所以候选池只能由上游搜索推导。文件级哈希与字幕那行同一套机制（HF `tree.lfs.oid` × ModelScope `Sha256`），实测这次 338 行里 191 行两家一致 |
| `machine-budget` | 这一条几乎不依赖上游 file:line —— 它的输入是**已提交那五张表的数字**加一张实测吞吐表；唯一的外部依据是「每个家族在主体之外还要不要外挂权重」 | sd.cpp `95b3090`：`docs/sd.md` 的 txt2img 例子五行 —— `:13` SD1.5 只给 `-m`（`sd-example-sd15-only-model`）、`:14` SDXL 要 `--vae`（`sd-example-sdxl-needs-vae`）、`:15` sd3_medium 那颗文件名自带 clips/t5xxl（`sd-example-sd3-medium-selfcontained`）、`:16` flux 要 `--vae --clip_l --t5xxl`（`sd-flux-needs-companions`）、`:17` sd3.5_large 要 `--clip_l --clip_g --t5xxl`（`sd-sd35-needs-encoders`），五条都由 `citation-drift` 逐行查；吞吐 `4.2 / 0.25 MiB/s` 不是上游声明，是 `tools/r2_manifest.py` 里那次 lain42.top 实测，所以产物里 `measured_at_exit` 写明是哪台机器量的 |
| `launch-plan` | 外壳起每一项能力该敲哪些旗标、引擎自己有没有鉴权 | llama.cpp `b11798`：`--api-key` = `common/arg.cpp:3533`，`/health` 那行注释 `public endpoint (no API key check)` = `tools/server/server.cpp:280`（相邻的 `/v1/health` :281 注释一模一样，所以 expect 加长到只命中 280）。whisper.cpp `b5454`：只有 `--host`/`--port`（`examples/server/server.cpp:252/251`），默认绑 127.0.0.1（`:59`），整份文件查不到 `--api-key`/`api_key` —— 缺席也是一条被扫出来的判据。stable-diffusion.cpp `95b3090`：`--listen-ip`/`--listen-port` = `examples/server/runtime.cpp:193/198`，四份编码器旗标 = `examples/common/common.cpp:407/411/421/471`。oar-ocr `84817f5`：`name = "oar"` `oar-ocr-cli/src/args.rs:10`、子命令 `Ocr(Ocr),` `:49`、`requires_all = ["rec", "dict"]` `:59`、`conflicts_with_all = ["det", "rec", "dict"]` `:84`、位置参数 `INPUTS` `:90` —— 这份文件里没有 `--port`/`--listen`/`--api-key`。以上九条都进了 `pins/citations.json`（现 82 条），并由 `tools/launch_plan.py` 的 28 条 needle 与 8 条缺席声明在车道里逐条现查 |
| `install-deps` | 安装包车道第一步该 `apt-get install` 什么、镜像 readme 里在不在 | Tauri `2e513e3f`：`<TabItem label="Debian">` = `src/content/docs/start/prerequisites.mdx:29`，族里最后一颗点名的 `libayatana-appindicator3-dev` = `:40`（整页唯一命中），安装命令首行 `sudo apt install libwebkit2gtk-4.1-dev` = `:33`；**整页搜 `gstreamer` 命中 0 次** —— 旧文档里那句"+ gstreamer 插件族"没有出处，已删。runner-images `b4067e48`：包表形状靠 `| libssl-dev             | 3.0.13-0ubuntu3.16 |` = `images/ubuntu/Ubuntu2404-Readme.md:289` 证明（不然逐字查全是 `not_listed` 只可能是读错了表），工具链 `- Node.js 22.23.3` = `:26`、`- Rust 1.99.1` = `:140`。在场判语只说 `listed / not_listed`，产物 `verdict_legend` 明写"readme 没提到 ≠ 机器上没装"，真要证明装没装得看车道打印 `pkg-config --modversion webkit2gtk-4.1` |
| `lane-argv` | 车道真敲的那一行是否与 `pins/launch-plan.json` 一致 | **不是上游声明，是本仓库两份产物互相核对**：读 `.github/workflows/engine-live.yml`（pyyaml），按表里的 `binaries` / `bind` / `flags_absent` / `omit` / `subcommand` 查。九种改坏 workflow 文本的对照全被报（含 `$SRV` 换成 `:` 的"只 find 不 exec"）；判据不吃注释与 heredoc 正文，这条本身也有正反对照。上游那五处旗标事实的出处仍归 `launch-plan` 那一行 |
| `sideload` | 用户手上的文件能不能按内容认领 | 三个**真实下载的上游文件**（`tests/fixtures/sideload/`，都 <30 KB）：两颗钉过的字典 + `ppocrv5_latin_dict.txt` 的另一颗同名不同字节者。同一串 sha256 取自 GitHub API 的 `asset.digest`（复现命令连同两颗 tag 的体积与 digest 一起记在 `tests/fixtures/sideload.meta.json` 的 reproduce 字段 —— 那条 jq 里有竖线，写进这张表会打断一行），meta 记完整串、测试按 meta 复算字节。`.gguf` 文件头不在这一层解读（归 CI/外壳里的 gguf-parser），产物 `rule.header` 写明了这条边界 |
| `backends` | 分层与 runtime 可替换这两句有多少可选面 | 三个 ref 的 release 表按 `pins/payload-plan.json` 钉的那一个现取（`gh api repos/<repo>/releases/tags/<tag>` 的 name / size / digest）：llama `b11798` 36 个、whisper `b5454` 12 个、sd `master-975-95b3090` 9 个 = 57，与 ADR-0001 那句"36 个产物"逐条对得上。真实捕获钉在 `tests/fixtures/backend_assets.json`（sha256 记在 `.meta.json`，测试复算字节），车道 `backends` 的现取步骤再现取一次对表；同一 job 后四步是换档表（`--selftest` / 单测 / 离线对表 / 拿现取那份矩阵重算） |
| `backends`（server 在场那四步） | "每一档包里都有外壳要 exec 的那个文件"这句话的前提 | zip **中央目录的成员文件名表**（`tools/zip_peek.py` 用 HTTP Range 读尾部，每颗约 1 MiB，不解压不运行）+ 真装默认档那 3 颗 `.tar.gz` 的**成员表**（`payload_plan.read_tar_entries`：取整包后先要求"字节数 == release 声明"与"整包 sha256 == 上游 digest"，才读 `tarfile.getmembers()`）；31 颗钉在 `tests/fixtures/parity-captures.json`（44,548 B，sha256 记 `.meta.json` 并由测试复算字节）；可执行名不在这张表里定义，只从 `launch_plan.SERVER_BASE` 取（那条出处见上面两行），源码里出现字面量由一条 ast 扫描拒绝 |
| 引擎与权重输入 | 上游 `digest` 就是内容哈希 | 五个包整包下回实算相等（llama b11798、whisper b5454、sd 95b3090、qwen 415 MB、SD1.5 1.5 GB），见 `pins/models.json` 的 `bytes_verified` |

写这张表的理由：CI 现在一次都跑不了，而"日志里会出现某串"这类假设一旦错，
车道第一次跑就会红在无关的地方 —— `image-proof` 原本就是这么错的（它去 grep 日志，
而参数其实只进 PNG）。所以**每条硬编码判据都要有 ref 级出处**，上游改动时按这张表逐条重量。

判据本身也不能只靠"看上去对"，而且**这几个计数不许只活在散文里**：
`tests/test_lane_replay_report.py` 把它们钉成断言，重数只要一条命令
（`python -W error::ResourceWarning -m unittest tests.test_lane_replay_report -v`）。
2026-10-21 实测：六条 workflow 合起来 **18 段** `<<'PY'`（`engine-live.yml` 14、`ci.yml` 2、
`package.yml` 2、`shell.yml` 0、`installer.yml` 0、`upstream-follow.yml` 0；`yaml_gate.py` 会打同一份编译计数，
段数打 0 就意味着闸门什么都没查），
其中 **13 段含判据**（认 `check(` / `SystemExit` / `assert` / `sys.exit(` —— 最后这个是这轮补的：
`ocr-proof` 那段工具链版本闸就是靠 `sys.exit("工具链低于…")` 红的，只找前三个会漏），
5 段是取数与打印。被本地预跑引用到的是 **8 个 (job, 步骤名) 组合**：
`headless-inference 起常驻 llama-server`、`ocr-proof 认得出那个词`、`ocr-proof 编译 CLI`、
`ocr-catalog digest`、`toolbelt-pins --json`、`subtitle-proof 常驻 whisper-server`、
`image-proof 常驻 sd-server`、`image-proof 生成一张小图`；这两份测试文件合起来 **44 条用例**
（`python -m unittest tests.test_lane_assertions tests.test_image_proof_block`）。
预跑的做法是 `tests/lane_replay.py` 把那段原样抽出来、在临时目录里放合成输入再 exec，
`SystemExit` 当结论；每段都配一条"必须过"的正向对照，另有一条 mutation 检查证明它不是永远绿
（同一份 OCR 输出把期望词换掉，rc 从 0 翻成 1）。其中 `subtitle-proof` / `image-proof`
那两步的判据本体已经抽进 `tools/srt_proof.py` 与 `tools/png_proof.py`，
走的是 `lane_replay.body`（钉整段 shell 文本的形状）而不是 `lane_replay.block`。
**刻意不在这里验的也留在账上**：`ocr-proof` 开头那段工具链版本闸要真 `rustc`，
预跑工具不 mock 外部命令，所以它只能等 CI；`ci.yml` / `package.yml` / `ocr-languages` /
`citation-drift` 那几段断言段的判据其实住在 `tools/` 里，由各自的离线单测负责，
车道里剩下的只是"怎么调它" —— 这两种"没在本地跑"要分开说，别混成一句"都验过了"。
预跑也顺手抓到一个"看起来在测、其实什么都没测"的形状：`test_lane_assertions.py` 与
`test_image_proof_block.py` 原来靠 `discover -s tests` 才 import 得到 `lane_replay`，
单跑一个模块时报的是 ImportError（`Ran 1 test … errors=1`），很容易被读成"这个模块只有 1 条用例"。
现在两个文件自己把目录加进 `sys.path`，单跑与整跑给出的用例数一致。

## 第一次真跑实测（2026-10-21；引擎车道用例取的是当时的**最新版**，不是钉值）

1. **timeout 预算 vs 实测**：`image-proof` 3m43s（60 min 预算；4,265,146,304 B 权重下载 2m43s、
   两次模型加载）、`subtitle-proof` 9 s（30）、`ocr-proof` 编译 6m06s + 识别（34）、
   `headless-inference` 12 s（35）、`os-inference-proof` 5 s。四个预算都留得很宽，最紧的仍是
   OCR（占 18%），暂不按实测收紧 —— 2 核 runner 的缓存命中率会变。
2. **OCR**：真编译过了（6m06s），识别输出命中 `OARCI2291`；锁已提交（第 12 条），
   `--locked` 已在 run 39638448298 的 `ocr-proof` 里真走过（notice「依赖图按…钉住」、495 个包、
   编译 3m46s、同一句识别）；首轮 6653cfc 因 job 没 checkout 落进 else 分支，46dfead 补后才算钉住。
3. **sd 的动态库闭包**：`sd-server` 真起来了并出图；这次的坑不在 `.so`，而在权重 ——
   上一版用 UNet-only 的 Q4_0，diffusion 模型缺失直接出不了图；换成完整 checkpoint
   （4,265,146,304 B）后一次通过。以后修生图先看权重完整性，再怀疑 `.so`。
4. **whisper 与 llama 的目录/产物名**：`find` 不限深度在 Linux 包上都找到了 `whisper-server` /
   `llama-server`；subtitle 第一次红是短音频被 `--min-blocks` 门槛挡掉（改成 1），不是路径或 CLI 名。
   whisper 用钉住的 b5454，llama 用 discover 现挑的 b11797。同一晚的 b11798 把 `llama`
   变成了分发器（help 里只有 serve/cli/download/version）：`find | head -1` 选中它后，
   两个开关都不在 help 里，整步在跑模型之前就被 `grep &&` 的 `-e` 杀掉（run 39638448298）。
   实例定位改成只认 `llama-cli`、开关探测改成 `if` 之后，b11798 跑出 53.9 tok/s
   （run 39641510711）—— 这是"用最新版"车道该抓的上游漂移，不是钉值坏了。
5. **runner 侧的 HF 直连**：可用 —— 415 MB 的 0.5B GGUF 与 4.27 GB 的 SD checkpoint 都从
   `huggingface.co` 直接下成功并按 pin 校过 sha256。国内出口不可用仍是真的，所以产品侧仍按
   ModelScope 首选、hf-mirror 备选（`pins/download-plan.json`）。
6. **每条 pin 的 sha256 复核**：引擎与权重下载步骤都按 pin 复核通过；任何一条对不上仍会红着停下。
   这次另有一条"假红"值得记住：上游正在上传新 release 时资产表可能只有一半（b11797 的 win 包
   晚于 release 2 分 40 秒），`runtime_versions --live` 已把"升级路径覆盖变窄"降级为提示（5a601c7），
   钉住那颗的完整性仍是硬红。
7. **常驻 server 三步的"起不起来"**：都起来了 —— llama 第 2 次探测就绪（401/CORS 探针通过）、
   whisper 加载期 503 后转 ok、sd 能答就是就绪；`usage.tokens_predicted` 是否出现没有单独记录，
   判据本来就允许缺（缺了跳过），硬证据是非空文本。

**仍没测的（下一轮盯这些，别把上面的绿读成"都验过了"）**：

- GUI 是否真打开、集显/独显性能与 DirectML/Vulkan 是否真加速 —— 只能等设备矩阵。
- **钉住的 llama/sd 二进制本身没被执行过**：执行车道跑的是最新版；要证钉值能跑，得重钉（#58）
  或在执行车道加一条"按 pin 下载再跑"。
- `upstream-follow` 的 `schedule` 会不会真触发（这台账号 cron 会丢槽）。
- macOS 字幕（#29）、写 R2（#23）、把负载送到数据根（#57）。

**CI 证明不了的**：GUI 是否真打开、集显/独显上的真实性能与兼容性、DirectML/Vulkan EP 是否真的加速。
这几项必须留到设备矩阵，交付说明里要写清，不拿 CI 绿当"能用"。

## 现成工具带（不自研的那部分）

"这台机器能跑什么"、"模型热切换"、"导入的 GGUF 怎么估内存"这三件事已经有 MIT/Apache 的成熟工具在做，
清单、依据与采纳决定见 [toolbelt.md](toolbelt.md)。其中 llmfit（38,108★，MIT，今天还在 push）
带 `--json` 与可扩目录，直接接上本文第 1 条"内置目录快照"的设计。
