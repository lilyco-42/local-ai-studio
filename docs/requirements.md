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
4. **对话、字幕、生图三项起常驻 server，而不是一次一进程**：上游包里就有
   `llama-server` / `whisper-server` / `sd-server`（见 ADR-0001"包内到底有什么"）。
   400 MB 权重每次重载不可接受。`llama-bench` 也在包里，"这台机器多少 tok/s"不用自研。
   **OCR 是例外**：2026-10-20 递归列过 oar-ocr 在钉住 ref 的 464 个成员，
   只有 `oar-ocr-cli` / `oar-ocr-bench` / `examples`，没有任何 HTTP 服务面
   （`oar-ocr-vl/src/api` 是库的 Rust API 模块，不是网络服务）——
   上游只有 CLI，所以外壳对 OCR 只能按任务起进程，或在外面自己包一层常驻。
   原来这里写"四项能力都起常驻 server"是把例外说漏了。
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

1. `ci.yml`：201 个单测绿（含 `test_yaml_gate.py` 给闸门自己的 11 条、`test_toolbelt_pins.py` 给工具带
   钉表的 14 条、`test_catalog_snapshot.py` 给目录快照与其侧表的 16 条、`test_citation_check.py` 给出处闸门的 14 条、
   `test_ocr_build_surface.py` 给 OCR 依赖面分类器与比对的 15 条、`test_file_index.py` 给下载层五种状态的 20 条、
   `test_payload_plan.py` 给装机负载清单的 11 条、`test_pack_engine.py` 给出包逻辑的 10 条、
   `test_establish_verify.py` 给字节复核与哈希对照的 16 条、`test_shell_vars.py` 给 shell 变量判据的 10 条、
   `test_r2_manifest.py` 给镜像清单的 6 条、`test_lane_assertions.py` + `test_image_proof_block.py`
   给车道判据的 32 条），
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
   `b8944e9f…`/1,566,768,416 B，3 步 256x256 出合法 PNG，并**读 PNG 自己的 IHDR 与
   `tEXt`/`parameters` 块**要求 `Steps: 3`、`Size: WxH` 与实际画布一致 —— 证明参数真被吃进去并写进产物，
   不是随便跑出一张图。判据读产物而非日志，是 2026-10-19 读上游源码定下的：
   `get_image_params()` 只 return，参数由 `stbi_write_png` 落成 tEXt，日志里根本没有那两行）。
   这条判据本身还有 6 条离线回放单测（合成合法/缺元数据/步数错/尺寸不一致/画布不是 256/非 PNG），
   2026-10-20 又把同一段判据抽成 `tools/png_proof.py` 并加了 25 条单测（含"与车道那段内联判据
   逐案同生同死"的一致性用例），因为常驻服务那条路要把 b64 解开再判，两处必须说同一句话。
   生图这条刻意分工：引擎不钉（上游每天出 nightly，钉死等于天天红且测的是旧版），
   权重钉死（1.5 GB，那一步的 curl 预算是 2,400 s，整条 job 现在是 60 min）。
   **并且对话/字幕/生图三项各有"真起常驻服务再调 HTTP"的一步了**（2026-10-20）：
   `headless-inference#4` 走 `/v1/chat/completions`，`subtitle-proof#5` 起 `whisper-server`
   打 `POST /inference`（multipart，`response_format=srt`）并要求响应是 `x-subrip`，
   `image-proof#4` 起 `sd-server` 走 `POST /sdcpp/v1/img_gen` → 轮询 `poll_url`
   → 解 `result.images[0].b64_json` → 同一套 PNG 判据。
   这三步的共同点是**服务启动时都不给生成旗标**（sd 那步尤其：上游默认 20 步/512x512，
   产物里却是 `Steps: 3`/256x256，才能证明参数是从请求体吃进去的）。
   OCR 没有服务形态可测（见"技术前提"第 4 条的例外说明）。
   但覆盖度说的是**车道存在**，不是跑过 —— 跑过与否要看 Actions 的真实记录，目前为零。
5. **车道里的 Python 片段要真编译**：`yaml_gate.py` 现在把两条 workflow 里的每段 `<<'PY'` 抽出来
   `compile()`（本轮 14 段；`python tools/yaml_gate.py` 自己会打印这个数，别信文档里的静态值），
   并验过它会红 —— 掺一段缩进错的 PY 进去，它按
   `文件:job#步骤 第N段` 报出语法错。这个能力是被我上一次写的车道逼出来的：
   一个多缩进两格的注释就是 IndentationError，而旧闸门只查 heredoc 是否闭合，放它过去了。
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
9. **语法这一层交给真解析器，但先验证解析器本身**：`tools/bash_gate.py` 把三条 workflow 的每一段
   `run:` 写进临时文件跑 `bash -n`（只解析，绝不执行）。2026-10-20 实测覆盖 53 段、0 问。
   这条工具是被一个假结论逼出来的：本机 PATH 上的 `bash` 是 WSL 的占位程序，它对**任何**文件都返回
   非 0 并吐一段 UTF-16 的"未安装"提示 —— 第一遍跑出的是"42/42 步骤全部语法错"，看着像查得很严，
   其实一条真信息都没有。所以现在每个候选 bash 都要先过双对照（故意写坏的文件必须非 0、
   故意写好的必须 0），过不了就换下一个，全都不过就报 `SKIP（unreliable/missing：原因）`
   而不是装成查过了。单测把"永远非 0""永远 0""跑不起来"三种假 bash 都钉住了。
10. **外壳必须是唯一的监听口，引擎端口不许直接给 UI**（出处是 2026-10-20 读三家 pin ref 的源码，
   见 [ADR-0001 的"三个 server 的接口与默认安全形状"](adr/0001-engines-from-upstream-binaries.md)）：
   三家默认都只绑 loopback（llama `common.h:648`、whisper `server.cpp:59/65`、sd `runtime.h:22-23`），
   这一条是安全的；但**鉴权与 CORS 不是** —— llama 有 `--api-key` 而默认为空（`arg.cpp:3513`）且
   `cors_origins` 默认 `*`（`common.h:652`，`*` + 无 key 上游只打一句警告 `server.cpp:340`）；
   whisper 直接发 `Access-Control-Allow-Origin: *` 且**代码里没有任何 key 校验**（`server.cpp:738-740`）；
   sd 把请求的 `Origin` 原样反射进 ACAO 再配 `Allow-Credentials: true`（`main.cpp:123-131`）。
   也就是说：用户开着我们的应用再打开任意网页，那个网页就能驱动本机的模型 —— 对"本地自用的无审查对话"
   这项目标是必须防的。验收动作因此不是"能起服务"而是三条：① llama 起来时必须带 `--api-key` 且
   `--cors-origins` 收窄到应用来源；② whisper / sd **没有 key 可给**，只能按需起、用完停，或由外壳反代
   （外壳持有唯一端口并自己发 token）；③ UI 永不直连引擎端口。
   这一条现在只有源码级证据，实现与验证都还没做 —— 列在验收里是为了别等写外壳时才发现架构选错了。
11. **每条 `file:line` 出处本身要能被查**：`pins/citations.json` 52 条声明（其中 1 条是 `absent`
    型缺席断言：runner 镜像 readme 里没有 `webkit2gtk`），每条记
   `repo/ref/path/line/expect/claim`；`tools/citation_check.py` 按 pin 住的 ref 取回那份文件，
   核对**那一行仍然含着 expect 的子串**（不是只看文件在不在 —— 上游把 `cors_origins` 默认值改掉
   而文件照在，才是我们要知道的事），子串在文件里命中不唯一就不配进这张表（建表时按此筛过）。
   车道 `citation-drift` 先跑 `--selftest`（明知是错的必须被认成错），再逐条核对并守住
   "至少 20 条"的覆盖度地板。这一条是给第 10 条与整张判据出处表兜底的：本轮已经踩过六次
   "判据指向一个不存在的东西却照样跑完"（`llama-completions`、去 tag 里找 CLI、`-of jfk.srt`、
   去日志里找 `Steps:`、grep `tokens per second`…）。
   证过它承重：临时改一条 expect → 1 条红并打印上游实际那一行；行号整体 +1 →
   行号整体 +1 → **51 条全红**（52 条里除 1 条 `absent` 型之外全部；那条本来就不看行号），
   也就是说这张表里没有一条"永远绿"的摆设。这件事现在**不必联网也能重跑**：
   `python tools/citation_check.py --cache .cache/refs --fill` 抓一次原文（21 份、828 KB，
   目录已 .gitignore），之后 `--cache .cache/refs` 离线复算就是"52 条真查了、0 条跳过"；
   跳过的那些会被点名说"不是通过，是没查"，这条本身有单测守着。
   2026-10-20 全部 52 条按 pin ref 联网实跑过一次（0 问题），车道 `citation-drift` 本身仍是 0 次执行。
12. **OCR 的依赖图要真的被钉住**（这条目前是**未达成**状态，写在这里是为了不让它被"车道绿了"蒙过去）：
   上游 oar-ocr 不发 `Cargo.lock`（实测 404，正向对照 206），而 40 个外部包里只有 `ort` 是 `=` 精确钉的、
   其余 37 个是 caret → `cargo build` 会在构建时重新解析。所以：
   ① `ocr-proof` 第一次真跑生成锁并把 `Cargo.lock` + sha256 传成 artifact；
   ② 由人提交成 `pins/ocr-Cargo.lock`；③ 此后该步骤走 `cargo build --locked`，缺锁就红。
   在 ② 之前，OCR 那条只能算"编出来了"，**不能算"编的是钉住的那份依赖图"** —— 交付说明要照这句写。
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
    ① **whisper.cpp 的 release 没有 macOS 产物** → 字幕在 Apple silicon 上只能自编译或不做（产品决定，已立任务）；
    ② llama/whisper 的 Linux 与 macOS 包是 **tar.gz**，中央目录读不了（格式决定），
    那几行如实标 `needs_extract`，逐文件清单由出包车道解开作答（见第 15 条）。
15. **出包车道已经有了，且它先校哈希再解包**（`tools/pack_engine.py` + `.github/workflows/package.yml`）：
    按 OS 矩阵（win-x64 / linux-x64 / macos-arm64）取 `payload-plan` 选中的产物 →
    **整包 sha256 必须等于上游 digest，对不上就停，不许"先解包再说"** →
    stdlib 解包（不调外部 unzip/tar）→ 按收集规则落成**扁平布局**
    （llama 的顶层同名目录与 whisper 的 `Release/` 都拉平，这是 `$ORIGIN` 找运行库的前提）→
    逐文件 sha256 + 体积写 `payload-manifest.json` 并上传为 artifact。
    自查三件：主二进制必须在位、清单里不许残留被排除的东西、负载目录里不许留压缩包
    （留着就等于把二次解包推给用户首启）。同名不同目录的成员**不许静默覆盖**，
    要点名进排除表 —— 覆盖会让"装进去的文件"与"清单里的文件"不是同一份。
    离线对照 10 条（合成 zip + 合成 tar.gz，含 digest 不符与下载失败两条），
    闸门现在同时解析三条 workflow（17 段 heredoc PY；重数：`python3 tools/yaml_gate.py` 的
    "编译了 N 段"那行）。
    车道仍 0 次执行，所以这一条给的是"出包这一步有路、有判据、有清单格式"，不是"包已经出出来"。
16. **"只能靠 GitHub Actions 出包"这句本身要被验，不能当审美前提**：拆开四问，答案全部来自
    pin 住的公共来源（已进 `pins/citations.json`，由 `citation-drift` 查），详见
    [docs/packaging.md](packaging.md)。要点：runner 镜像自带 Rust 1.99.1 与 Node 22.23.3
    （顺带解掉 OCR 的 `rust-version = 1.95` 那个未知）；**但 Linux 侧没有 `webkit2gtk`**
    （整份镜像 readme 命中 0 次 —— 这条做成 `absent` 型出处），而 Tauri 2 点名要
    `libwebkit2gtk-4.1-dev` 一族，所以安装包车道第一步是 apt 装 + 打印 `pkg-config --modversion`；
    官方 `tauri-apps/tauri-action`（MIT、1,619★、2026-10-16 仍在 push）是 `uses:` 型且
    release **没有产物**，所以它只能按 commit SHA 钉，工具带那套 digest sha256 对它不适用。
    最后是算术：**引擎落地就有 115.6 MiB（win-x64）**，再加 415 MB/1,566 MB 权重就不是安装包 ——
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

## 车道判据的出处（全部按**钉住的那个 ref** 核过，不是按 HEAD 记的）

| 车道 | 判据依赖的东西 | 出处 |
|---|---|---|
| `headless-inference` | 速率行 + 独立证明"真的吐了字"；关对话模式的真实拼法 | llama.cpp `b11727`：**库里** `src/llama-context.cpp:4657/4659` 会打 `%8.2f tokens per second`，但 **llama-cli 不调它** —— CLI 只在 `--show-timings`（`common/arg.cpp:1798`）下打 `[ Prompt: %.1f t/s | Generation: %.1f t/s ]`（`tools/cli/cli-context.cpp:655-660`）。判据因此两种写法都认，并另加一条与格式无关的证据：stdout 必须有非空白内容。抓不到速率时先怀疑格式（红字里顺带打 help 里的 timings/perf 开关），不再把结论写成"推理没跑起来"。关对话的真实拼法是 `-no-cnv`/`--no-conversation`（`arg.cpp:1907`），交互风险由 `< /dev/null` 兜死；`-t/-n/-ngl` 也都在 `common/arg.cpp`（**不在** `common/common.cpp`） |
| `headless-inference`（常驻 server） | 路由名、响应字段名、**服务二进制与 CLI 是不是同一个文件** | llama.cpp `b11727`：`GET /health` = `tools/server/server.cpp:263`（返回带 `"status"`）、`POST /v1/chat/completions` = `274`、`choices[].message.content` = `tools/server/server-task.cpp:422/434`（老 `/completion` 用 `choices[].text`，判据两种都吃）、`usage.tokens_predicted` = `340`。二进制是 `llama-server` **和** `libllama-server-impl.so` 成对的（zip 条目表），所以这一步自己 `find llama-server` 并带 `LD_LIBRARY_PATH`，不复用上一步挑中的 CLI。`tokens_predicted` 缺失时**只跳过不断言**（这个键历史上叫过 `completion_tokens`，写死会养出一条永远红的车道），非空文本那条才是硬证据 |
| `subtitle-proof` | `-osrt/--output-srt`、`-of/--output-file`、`-l`、`-m`、`-f`、`-t`；**产出文件叫什么** | whisper.cpp `b5454` 的 `examples/cli/cli.cpp:183/190/197/201/202/159`；不给 `-of` 时输出名从输入名推（同文件 `1136`）。**`-of` 要的是基名**：`fout_factory::open` 把扩展名直接拼在后面（`1165-1166`），写 `-of jfk.srt` 会得到 `jfk.srt.srt` —— 原车道就是这么写的，第一次跑必红且原因很怪。现在判据靠 `ls jfk*.srt` 发现真名，红字区分"命名变了"和"没产出字幕" |
| `subtitle-proof`（常驻 server） | 服务二进制在不在包里、就绪态怎么认、multipart 字段名、SRT 由谁渲染 | whisper.cpp `b5454`：`whisper-server` **实测在** `whisper-bin-ubuntu-x64.tar.gz` 深度 1（2026-10-20 整包列条目，且整包 sha256 == 车道里钉的 `a72becf1…`）；`GET /health` 加载期回 **503** + `loading model`（`examples/server/server.cpp:1218-1224`），就绪回 `{"status":"ok"}`（`:1221`）—— 所以探测写成 `curl -fsS` **并且**要求正文含 `ok`，少一样都能把"还在加载"看成"服务不行/服务行了"；`POST /inference`（`:62/826`）的音频字段必须叫 `file`（`:831`，缺了直接 400），其余参数一律走 multipart 字段（`:482-622` 清一色 `has_file`）；`response_format=srt` 时服务端逐段拼 `序号/起 --> 止/正文` 并以 `application/x-subrip` 回吐（`:1037-1041`），编号是 `i+1+offset_n` 而请求不发 `offset_n`，所以判据要求从 1 连续。判据本体是 `tools/srt_proof.py`（20 条单测：本仓库拼的 spec 形状 + 另外两个项目留下的真 SRT 当独立生产者语料） |
| `image-proof` | 参数写在**产物的 tEXt** 里而不是日志里 | sd.cpp `master-975-95b3090`：`examples/common/common.cpp:3239/3269` 拼 `Steps:`/`Size:` → `examples/common/media_io.cpp` 交给 `stbi_write_png(..., parameters)` → `thirdparty/stb_image_write.h:1210` 落成 `tEXt`、关键字 `parameters` |
| `image-proof`（常驻 server） | 服务起不起得来、异步任务的状态机、图在响应的哪个字段、**请求体的参数有没有被吃进去** | sd.cpp `master-975-95b3090`：监听旗标是 `--listen-ip/--listen-port`（`examples/server/runtime.cpp:193/198`，默认 127.0.0.1:1234）；**模型先加载完才 `listen`**（`examples/server/main.cpp:92-98` 建上下文、`:152` 才 listen），所以与 whisper 相反——这里能答就是就绪，探测用 `GET /v1/models`（`routes_openai.cpp:253`）；`POST /sdcpp/v1/img_gen`（`routes_sdcpp.cpp:644`）成功回 **202**（`:695`）并给 `poll_url`（`:693`），车道**按服务端给的地址轮询**而不自己拼；状态名 `queued/generating/completed/failed/cancelled`（`async_jobs.cpp:73-88`），`failed/cancelled` 也要跳出轮询，否则红字看起来像超时而不像任务失败了；图在 `result.images[].b64_json`（`:190/192-194`，b64 由 `:284` 产出）。**这一步刻意不给 `sd-server` 任何生成旗标**：请求体是在 server 的 CLI 默认值之上覆盖的（`routes_sdcpp.cpp:417`），上游默认 20 步/512x512，产物元数据却是 `Steps: 3`、IHDR 是 256x256，才能证明参数从 HTTP 进去了。判据与 CLI 那条共用 `tools/png_proof.py`（25 条单测，含"两处判据逐案同生同死"的一致性用例），步数键是 `sample_params.sample_steps` 且逐键覆盖（`common.cpp:2204`）；元数据默认嵌进产物靠 `common.h:219` 的 `embed_image_metadata = true` |
| `ocr-proof` | 包名 `oar-ocr-cli`、二进制名 `oar`、`--det/--rec/--dict` 必须一起给 | commit `84817f58…`（**不是** tag `v0.10.0` —— 那个 tag 里没有 CLI）：根 `Cargo.toml` members、`oar-ocr-cli/Cargo.toml` 的 `[[bin]] name = "oar"`、`src/args.rs` 的 `requires_all`/`conflicts_with_all` |
| `ocr-languages` | OCR 的语言轴能不能「下载前就信」：产物在哪个 tag、两家分发方给不给同一个哈希、字典与权重是否成对 | oar-ocr `84817f58`：`oar-ocr-core/src/core/download/registry.rs:10` 写 `MODELSCOPE_REPO = "greatv/oar-ocr"`（第二个分发方就是上游自己的 ModelScope 镜像）、`:4` 声明条目按名字排序是为了二分查找（所以工具替上游守着这条排序，乱序当场抛）；`docs/models.md:116` 明写 v3 各语言字典**不在 auto-download 注册表内**，要另从 PaddleOCR 取 —— 那三份 pin 因此只能「按 commit 自算」。查找一律按 models.md 链接里的 **tag** 定位：`ppocrv5_latin_dict.txt` 在 v0.1.0 是 1,634 B / `3c0a8a79…`、在 v0.3.0 是 2,616 B / `ccbcc457…`，按名字找第一版就把拉丁语字典钉错了（`--live` 报「来源哈希不一致」才逼出来） |
| `recommendations` | 「文件要整只、要塞得进本档」这两条判据的依据不是 file:line，而是**同一次运行里两家给回来的文件清单本身**（切片入库，`--live --check` 每次都重测） | `ggml-org/Qwen3-8B-GGUF` 的 6 条 `.gguf` 只有 BF16（16,388,044,096 B）、Q8_0（8,709,518,656 B > 8 GB 机器总内存）和 `dflash-*`/`dspark-*` 草稿（1,120,250,336 B —— 按大小它反而最先合档，所以形状判据必须排在大小判据**前面**）；同一次捕获里 ModelScope 对 7 个 `mradermacher/*` 仓库各返回 **0** 条 `.gguf`，而对 unsloth 给 7–26 条，所以那 7 行只能是 `single`（第二家不镜像重打包方），UI 必须说"下完自己算" |
| 引擎与权重输入 | 上游 `digest` 就是内容哈希 | 五个包整包下回实算相等（llama b11727、whisper b5454、sd 95b3090、qwen 415 MB、SD1.5 1.5 GB），见 `pins/models.json` 的 `bytes_verified` |

写这张表的理由：CI 现在一次都跑不了，而"日志里会出现某串"这类假设一旦错，
车道第一次跑就会红在无关的地方 —— `image-proof` 原本就是这么错的（它去 grep 日志，
而参数其实只进 PNG）。所以**每条硬编码判据都要有 ref 级出处**，上游改动时按这张表逐条重量。

判据本身也不能只靠"看上去对"：`engine-live.yml` 里 12 段 `<<'PY'`（两条 workflow 合起来 14 段，
段数由 `yaml_gate.py` 打印）中有 6 段是**断言段**，
其中 5 段已在本地预跑过（`tests/lane_replay.py` 把那段原样抽出来、放合成输入、exec 一遍，
`SystemExit` 当结论；共 27 条用例，每段都配一条"必须过"的正向对照，另有一条
 mutation 检查证明它不是永远绿：同一份 OCR 输出把期望词换掉，rc 从 0 翻成 1）。
剩下 4 段是取数/打印，不含判据；而 `ocr-proof#0` 那段工具链版本闸依赖真 `rustc`，
预跑工具刻意不 mock 外部命令，所以它只能等 CI —— 这一条要如实留着，别当作已验。
预跑也顺手抓到一个"看起来在测、其实什么都没测"的形状：`test_lane_assertions.py` 与
`test_image_proof_block.py` 原来靠 `discover -s tests` 才 import 得到 `lane_replay`，
单跑一个模块时报的是 ImportError（`Ran 1 test … errors=1`），很容易被读成"这个模块只有 1 条用例"。
现在两个文件自己把目录加进 `sys.path`，单跑与整跑给出的用例数一致。

## 第一次真跑要盯什么（车道至今 0 次执行，这些是静态审计之后剩下的未知）

1. **timeout 够不够**：`image-proof` 2026-10-20 从 45 提到 60 min（1.5 GB 权重 + 一次 CLI 出图
   + 一次常驻服务出图），`subtitle-proof` 从 20 提到 30 min（多起一次 whisper-server），
   `ocr-proof` 是 34 min（2 核 runner 上 `cargo build --release` 要过 ort / onig / tokenizers 的编译），
   `headless-inference` 是 35 min。这四个数全是估的，第一次跑就要按实际耗时改回来，
   别把"超时"读成"能力不行"。
2. **OCR 的源码 ref**：钉的是 main 的 `84817f58…`，因为 **v0.10.0 那个 tag 里没有 `oar-ocr-cli`**
   （成员表实测只有 4 项）。断言已按 raw 文件在本地核过（成员含 CLI、`[[bin]] name = "oar"`），
   剩下的未知是真编译 —— 上游若发新 tag，应该换成新 tag 而不是继续跟 main。
3. **sd 的动态库闭包**：包内条目已确认（`sd-cli`/`sd-server` 平铺在根，31 个 `.so` 含
   `libggml-cpu-*.so` 与带版本号软链名），车道已按 `find` 不限深度 + `LD_LIBRARY_PATH=$(dirname)` 处理；
   **还没证的是运行时能不能真把这套 `.so` 起来** —— 若报 `cannot open shared object`，先查 RPATH 再怀疑后端。
4. **whisper 与 llama 的 Linux 包比 Windows 多一层同名目录**（`whisper-bin-ubuntu-x64/`、`llama-b11727/`），
   条目与二进制名都已用整包下载核对（`whisper-cli`、`whisper-server`、`llama-server` 都在深度 1；
   2026-10-20 又整包核过一次 whisper 那份，47 个条目里 `whisper-server` 确实在，整包 sha256
   与车道钉的 `a72becf1…` 相等）；整包 sha256 三个包都与上游 digest 相等。
   剩下的未知是它们跑出来的结果对不对。
5. **runner 侧的 HF 直连**：`headless-inference` 从 `huggingface.co` 直接下权重。国内出口不可用
   这件事**不代表 GitHub runner 不可用**，两件事别混；但备选源该按 ADR 的新层级重排
   （ModelScope 首选、hf-mirror 备选，见 [model-catalog.md](model-catalog.md)）。
6. **每条 pin 都会被车道自己再 `sha256sum -c` 一遍**。任何一条对不上都是"上游被换过或源在说谎"，
   要红着停下来，不许改成 warning 放行。
7. **常驻 server 那三步的未知集中在"起不起来"而不是"字段对不对"**：`/health` 轮询给的是 60 次 × 2 s
   （≈2 分钟），2 核 runner 加载 415 MB 权重大概率够，但没测过；超时就先改这个预算，别改判据。
   `usage.tokens_predicted` 这个键名是按 `server-task.cpp:340` 核的，历史上它叫过 `completion_tokens`，
   所以判据写成"缺了就跳过、不硬要求"，硬证据是非空文本 —— 第一次跑要是 `/props` 有内容而
   `usage` 没有，那是键名换了，不是推理失败。`ldd` 那条只在**报 not found 时**才红（`grep` 找不到
   就退出 1，被 `||` 接住），所以它绿只说明"没缺失"，不说明库真的被加载了。
   新加的两步各自还压着三个没测过的假设，红的时候先按这里查：
   · **whisper** —— 就绪判据要求 `/health` 正文含 `ok`（`:1221`）。上游哪天把就绪体写成别的，
     探测会一直 503/200-但没-ok 直到预算用完，红字是"没到就绪态"而不是"转写失败"；
     multipart 里 `language=en` 是按 `:558` 的 `has_file("language")` 给的，哪天 cpp-httplib 不再把
     普通表单字段放进 `files`，那这些参数会**静默不生效**（SRT 仍然出，只是按默认语言赌）。
   · **sd** —— 请求体只给了 `sample_params.sample_steps`，靠的是 `common.cpp:2204` 那种
     "逐键 contains 再覆盖"的写法；哪天改成整块替换，scheduler/sampler 会跟着变默认值，
     红点仍然是 `Steps:` 或产物尺寸，不会告诉你"块被换了"。
     就绪探测用 `GET /v1/models`（`:253`）并要求正文含 `data` —— 若上游把这个 OpenAI 端点改名，
     表现为"90 次探测没响应"，那时先查端点表再查权重加载。
   · **两条共同的**：`find … -name whisper-server` / `-name sd-server` 命中 0 就是上游改了产物名，
     红字里已经带上整包条目表，不必再跑一次才知道少了什么。

**CI 证明不了的**：GUI 是否真打开、集显/独显上的真实性能与兼容性、DirectML/Vulkan EP 是否真的加速。
这几项必须留到设备矩阵，交付说明里要写清，不拿 CI 绿当"能用"。

## 现成工具带（不自研的那部分）

"这台机器能跑什么"、"模型热切换"、"导入的 GGUF 怎么估内存"这三件事已经有 MIT/Apache 的成熟工具在做，
清单、依据与采纳决定见 [toolbelt.md](toolbelt.md)。其中 llmfit（38,108★，MIT，今天还在 push）
带 `--json` 与可扩目录，直接接上本文第 1 条"内置目录快照"的设计。
