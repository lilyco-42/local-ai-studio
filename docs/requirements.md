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

1. `ci.yml`：189 个单测绿（含 `test_yaml_gate.py` 给闸门自己的 11 条、`test_toolbelt_pins.py` 给工具带
   钉表的 14 条、`test_catalog_snapshot.py` 给目录快照与其侧表的 16 条、`test_citation_check.py` 给出处闸门的 12 条、
   `test_ocr_build_surface.py` 给 OCR 依赖面分类器与比对的 15 条、`test_file_index.py` 给下载层五种状态的 20 条、
   `test_payload_plan.py` 给装机负载清单的 11 条、
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
   这条判据本身还有 6 条离线回放单测（合成合法/缺元数据/步数错/尺寸不一致/画布不是 256/非 PNG）。
   生图这条刻意分工：引擎不钉（上游每天出 nightly，钉死等于天天红且测的是旧版），
   权重钉死（1.5 GB，一次下载要 45 min 级 timeout）。
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
   验收动作是 `python tools/establish_pins.py --verify pins/models.json` 与
   `--verify pins/ocr.json`：字节与钉的哈希对不上就红；`catalog/defaults.json` 会把这条 pin 的
   强度级一起带出去，UI 才有东西可显示。第 6 级目前**没有任何一条到达** ——
   whisper/SD 权重实测只有一家发布，这点在交付说明里要照实写。
8. **车道里的 shell 也不能带 `set -u` 地雷**：`tools/shell_vars.py`（挂在 `yaml_gate.py` 里跑）把每条
   `run:` 步骤扫一遍，凡是 `set -u` 生效、而某个 `$VAR` 在本步骤从未赋值、也不在 job/step `env:` 或
   runner 内置白名单里的，就报 `文件:job#步骤 第 N 行`。来历是 2026-10-19 新加的 llama-server 步骤：
   原写 `export LD_LIBRARY_PATH="$(dirname "$SRV"):$LD_LIBRARY_PATH"`，而 runner 上这个变量根本不存在，
   步骤会在**起服务之前**死掉。`bash -n` 查不出（语法是对的），shellcheck 不在依赖里。
   三条刻意的边界，都有对应单测：`${X:-…}`/`${X:+…}` 是作者自己承认"可能没有"的写法，整段抹掉不查；
   `<<'PY'` 里是 Python 不是 shell，扫它会养出误报；`FOO=1 bar "$FOO"` **要报** —— 那个 FOO 只进 `bar`
   的环境，同一行的 `"$FOO"` 由当前 shell 展开，`set -u` 下确实未定义（这条是写完判据才发现自己
   原来的"对照用例"预期错了，判据比对照对）。
   证过它承重：把守卫去掉（临时副本，不动工作树），闸门红在第 5 行点名 `$LD_LIBRARY_PATH`。
9. **外壳必须是唯一的监听口，引擎端口不许直接给 UI**（出处是 2026-10-20 读三家 pin ref 的源码，
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
10. **每条 `file:line` 出处本身要能被查**：`pins/citations.json` 21 条声明，每条记
   `repo/ref/path/line/expect/claim`；`tools/citation_check.py` 按 pin 住的 ref 取回那份文件，
   核对**那一行仍然含着 expect 的子串**（不是只看文件在不在 —— 上游把 `cors_origins` 默认值改掉
   而文件照在，才是我们要知道的事），子串在文件里命中不唯一就不配进这张表（建表时按此筛过）。
   车道 `citation-drift` 先跑 `--selftest`（明知是错的必须被认成错），再逐条核对并守住
   "至少 20 条"的覆盖度地板。这一条是给第 9 条与整张判据出处表兜底的：本轮已经踩过六次
   "判据指向一个不存在的东西却照样跑完"（`llama-completions`、去 tag 里找 CLI、`-of jfk.srt`、
   去日志里找 `Steps:`、grep `tokens per second`…）。
   证过它承重：临时改一条 expect → 1 条红并打印上游实际那一行；行号整体 +1 → 21 条全红。
11. **OCR 的依赖图要真的被钉住**（这条目前是**未达成**状态，写在这里是为了不让它被"车道绿了"蒙过去）：
   上游 oar-ocr 不发 `Cargo.lock`（实测 404，正向对照 206），而 40 个外部包里只有 `ort` 是 `=` 精确钉的、
   其余 37 个是 caret → `cargo build` 会在构建时重新解析。所以：
   ① `ocr-proof` 第一次真跑生成锁并把 `Cargo.lock` + sha256 传成 artifact；
   ② 由人提交成 `pins/ocr-Cargo.lock`；③ 此后该步骤走 `cargo build --locked`，缺锁就红。
   在 ② 之前，OCR 那条只能算"编出来了"，**不能算"编的是钉住的那份依赖图"** —— 交付说明要照这句写。
   漂移由 `ocr-build-surface` 车道守：`tools/ocr_build_surface.py --check` 比对
   `pins/ocr-build-surface.json`（fingerprint + 锁文件状态），上游一旦开始发锁也会红（那就该改成取它的锁）。
   分类判据本身有 15 条离线单测，其中三条是给"曾经算错的样子"写的：`{ workspace = true }` 不是未钉、
   `[package]` 里的 `readme` 不是依赖、带 Range 的探测存在时回 206 而不是 200。
12. **下载层的校验规矩只有一条：两家分发方一致才可信**（`tools/file_index.py` + 车道 `file-index`）：
   按 `repo + 文件名` 同时读 HF `tree.lfs.oid` 与 ModelScope `repo/files` 的 `Sha256`，状态分五种，
   `agree` 才允许"下载前就信"；`single` 必须下载后自算一遍；`conflict` / `pin_conflict`
   **一个哈希都不许返回**（绝不"挑一个好看的"）。候选源按实测吞吐排序，
   `huggingface.co` 记 0（CN 实测 0 字节），不许排第一。
   正向对照 = 已钉的 pin；负向对照 = 故意造的假文件名 —— 它要是也被判成"有哈希"，
   就说明这套比对对任何输入都有答案；另有"至少 2 条被两家确认"的覆盖度地板，
   以及一条把"取数失败"与"这家没有这个仓库"分开的判据（前者必须红，后者是 `single`）。
   实测：qwen 与 SD `agree`（都等于 pin），`ggml-tiny.bin` `single`（ModelScope 未镜像，200 + 0 文件）。
13. **装机负载必须先量出来再设计打包**（`tools/payload_plan.py` → `pins/payload-plan.json`，
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
    那几行如实标 `needs_extract`，逐文件清单只能由 CI 真打包时产出。

## 车道判据的出处（全部按**钉住的那个 ref** 核过，不是按 HEAD 记的）

| 车道 | 判据依赖的东西 | 出处 |
|---|---|---|
| `headless-inference` | 速率行 + 独立证明"真的吐了字"；关对话模式的真实拼法 | llama.cpp `b11727`：**库里** `src/llama-context.cpp:4657/4659` 会打 `%8.2f tokens per second`，但 **llama-cli 不调它** —— CLI 只在 `--show-timings`（`common/arg.cpp:1798`）下打 `[ Prompt: %.1f t/s | Generation: %.1f t/s ]`（`tools/cli/cli-context.cpp:655-660`）。判据因此两种写法都认，并另加一条与格式无关的证据：stdout 必须有非空白内容。抓不到速率时先怀疑格式（红字里顺带打 help 里的 timings/perf 开关），不再把结论写成"推理没跑起来"。关对话的真实拼法是 `-no-cnv`/`--no-conversation`（`arg.cpp:1907`），交互风险由 `< /dev/null` 兜死；`-t/-n/-ngl` 也都在 `common/arg.cpp`（**不在** `common/common.cpp`） |
| `headless-inference`（常驻 server） | 路由名、响应字段名、**服务二进制与 CLI 是不是同一个文件** | llama.cpp `b11727`：`GET /health` = `tools/server/server.cpp:263`（返回带 `"status"`）、`POST /v1/chat/completions` = `274`、`choices[].message.content` = `tools/server/server-task.cpp:422/434`（老 `/completion` 用 `choices[].text`，判据两种都吃）、`usage.tokens_predicted` = `340`。二进制是 `llama-server` **和** `libllama-server-impl.so` 成对的（zip 条目表），所以这一步自己 `find llama-server` 并带 `LD_LIBRARY_PATH`，不复用上一步挑中的 CLI。`tokens_predicted` 缺失时**只跳过不断言**（这个键历史上叫过 `completion_tokens`，写死会养出一条永远红的车道），非空文本那条才是硬证据 |
| `subtitle-proof` | `-osrt/--output-srt`、`-of/--output-file`、`-l`、`-m`、`-f`、`-t`；**产出文件叫什么** | whisper.cpp `b5454` 的 `examples/cli/cli.cpp:183/190/197/201/202/159`；不给 `-of` 时输出名从输入名推（同文件 `1136`）。**`-of` 要的是基名**：`fout_factory::open` 把扩展名直接拼在后面（`1165-1166`），写 `-of jfk.srt` 会得到 `jfk.srt.srt` —— 原车道就是这么写的，第一次跑必红且原因很怪。现在判据靠 `ls jfk*.srt` 发现真名，红字区分"命名变了"和"没产出字幕" |
| `image-proof` | 参数写在**产物的 tEXt** 里而不是日志里 | sd.cpp `master-975-95b3090`：`examples/common/common.cpp:3239/3269` 拼 `Steps:`/`Size:` → `examples/common/media_io.cpp` 交给 `stbi_write_png(..., parameters)` → `thirdparty/stb_image_write.h:1210` 落成 `tEXt`、关键字 `parameters` |
| `ocr-proof` | 包名 `oar-ocr-cli`、二进制名 `oar`、`--det/--rec/--dict` 必须一起给 | commit `84817f58…`（**不是** tag `v0.10.0` —— 那个 tag 里没有 CLI）：根 `Cargo.toml` members、`oar-ocr-cli/Cargo.toml` 的 `[[bin]] name = "oar"`、`src/args.rs` 的 `requires_all`/`conflicts_with_all` |
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

1. **timeout 够不够**：`image-proof` 是 45 min（1.5 GB 权重 + 3 步推理），`ocr-proof` 是 34 min
   （2 核 runner 上 `cargo build --release` 要过 ort / onig / tokenizers 的编译），
   `headless-inference` 2026-10-19 从 25 提到 35 min（同一步里多起一次常驻 server，权重要重新加载
   + 最长 2 分钟就绪轮询）。这三个数都是估的，第一次跑就要按实际耗时改回来，别把"超时"读成"能力不行"。
2. **OCR 的源码 ref**：钉的是 main 的 `84817f58…`，因为 **v0.10.0 那个 tag 里没有 `oar-ocr-cli`**
   （成员表实测只有 4 项）。断言已按 raw 文件在本地核过（成员含 CLI、`[[bin]] name = "oar"`），
   剩下的未知是真编译 —— 上游若发新 tag，应该换成新 tag 而不是继续跟 main。
3. **sd 的动态库闭包**：包内条目已确认（`sd-cli`/`sd-server` 平铺在根，31 个 `.so` 含
   `libggml-cpu-*.so` 与带版本号软链名），车道已按 `find` 不限深度 + `LD_LIBRARY_PATH=$(dirname)` 处理；
   **还没证的是运行时能不能真把这套 `.so` 起来** —— 若报 `cannot open shared object`，先查 RPATH 再怀疑后端。
4. **whisper 与 llama 的 Linux 包比 Windows 多一层同名目录**（`whisper-bin-ubuntu-x64/`、`llama-b11727/`），
   条目与二进制名都已用整包下载核对（`whisper-cli`、`llama-server` 都在深度 1）；
   整包 sha256 三个包都与上游 digest 相等。剩下的未知是它们跑出来的结果对不对。
5. **runner 侧的 HF 直连**：`headless-inference` 从 `huggingface.co` 直接下权重。国内出口不可用
   这件事**不代表 GitHub runner 不可用**，两件事别混；但备选源该按 ADR 的新层级重排
   （ModelScope 首选、hf-mirror 备选，见 [model-catalog.md](model-catalog.md)）。
6. **每条 pin 都会被车道自己再 `sha256sum -c` 一遍**。任何一条对不上都是"上游被换过或源在说谎"，
   要红着停下来，不许改成 warning 放行。
7. **常驻 server 那条的未知集中在"起不起来"而不是"字段对不对"**：`/health` 轮询给的是 60 次 × 2 s
   （≈2 分钟），2 核 runner 加载 415 MB 权重大概率够，但没测过；超时就先改这个预算，别改判据。
   `usage.tokens_predicted` 这个键名是按 `server-task.cpp:340` 核的，历史上它叫过 `completion_tokens`，
   所以判据写成"缺了就跳过、不硬要求"，硬证据是非空文本 —— 第一次跑要是 `/props` 有内容而
   `usage` 没有，那是键名换了，不是推理失败。`ldd` 那条只在**报 not found 时**才红（`grep` 找不到
   就退出 1，被 `||` 接住），所以它绿只说明"没缺失"，不说明库真的被加载了。

**CI 证明不了的**：GUI 是否真打开、集显/独显上的真实性能与兼容性、DirectML/Vulkan EP 是否真的加速。
这几项必须留到设备矩阵，交付说明里要写清，不拿 CI 绿当"能用"。

## 现成工具带（不自研的那部分）

"这台机器能跑什么"、"模型热切换"、"导入的 GGUF 怎么估内存"这三件事已经有 MIT/Apache 的成熟工具在做，
清单、依据与采纳决定见 [toolbelt.md](toolbelt.md)。其中 llmfit（38,108★，MIT，今天还在 push）
带 `--json` 与可扩目录，直接接上本文第 1 条"内置目录快照"的设计。
