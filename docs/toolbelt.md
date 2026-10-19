# 工具带（toolbelt）：部署本地模型的现成工具收集

日期 2026-10-19 · 全部条目为 `gh api` / 仓库源码实测，不是凭印象；星标与日期都写明测量日，因为会过期。

## 为什么要单独收集这一层

ADR-0001 只解决了"引擎从哪来"。但把普通人电脑用起来还差三件事：**这台机器能跑哪些模型**、
**去哪儿挑模型**、**怎么稳定地下载/切换**。这三件都有人做完了，而且做得比我们打算自研的好。

## 清单

| 工具 | 解决什么 | 许可 | 活跃度（2026-10-19 测） | 预编译产物 | 上游有没有给校验 |
|---|---|---|---|---|---|
| **AlexsJones/llmfit** (Rust) | 检测 RAM/CPU/GPU-VRAM/后端，对目录里每个模型按**显存拟合、预估速度**等四个维度打分，给"你这台机器能跑什么"的答案 | **MIT** | **38,108★**，push **2026-10-19**（今天） | 18 个产物＝9 平台包 ×2：**每个 tar.gz/zip 都配 `.sha256`**；darwin x64+arm64、win msvc x64+arm64、linux gnu+musl x64+arm64+riscv64 | **有**，格式是 `sha256sum -c` 直接能吃的 `<哈希>␣␣<文件名>` |
| **huggingface/huggingface_hub** (Python, `hf` CLI) | 模型/权重的搜索与下载，`HF_ENDPOINT` 换镜像端点 | **Apache-2.0** | 3,990★，push 2026-10-19 | PyPI 包（不是单文件二进制） | 源码里 `file_download.py` 有 `resume_size`（21 处命中）与 `_adjust_range_header` → **支持断点/Range** |
| **mostlygeek/llama-swap** (Go) | 给任意 OpenAI/Anthropic 兼容后端做**模型热切换与常驻代理** | **MIT** | 6,012★，push 2026-10-15 | 6 平台包（darwin amd64/arm64、linux amd64/arm64、freebsd、windows） | **有** `llama-swap_265_checksums.txt`，同样是 sha256sum 格式 |
| **gpustack/gguf-parser-go** (Go) | 读 GGUF 头部、估算内存占用与最大 context（**不用加载权重**） | **MIT** | 307★，push 2026-10-08，open issues **0** | 7 个按平台命名的裸二进制（含 darwin-universal） | 没有 sidecar，但 **API digest 7/7 全有** |
| **huggingface/hf_transfer** (Rust) | HF 下载加速扩展 | **Apache-2.0** | 582★，push 2026-09-29 | PyPI wheel | — |
| **huggingface/hf-agents** | HF 官方出的 CLI 扩展：**用 llmfit + llama.cpp 跑本地 agent** | Apache-2.0 | 440★，push 2026-03-25 | — | — |
| llama.cpp 自带 | `llama-fit-params`、`llama-quantize`、`llama-bench`、`llama-gguf-split`、`llama-server` | MIT | 已在 ADR-0001 实测（`llama-win-cpu-x64.zip` 51 条目里都在） | 是 | **有**：release 产物的 API `digest = sha256:…`，实测与下载后本地算的一致 |

`hf-agents` 值得单独看一眼：它是 HuggingFace 官方把 **llmfit 当选型层、llama.cpp 当执行层**拼起来的产物，
等于给我们指了同一条组合，而不是自己发明。

## 决定

1. **选型/拟合不自研，直接用 `llmfit`**。它已有 `--json`（README 原话："agent/script consumption"），
   还有 `/api/v1/system`、`/api/v1/models` 两个 JSON 端点，以及 `llmfit recommend --json` /
   `llmfit storage --keep 3 --selection largest --json`（后者正好是"磁盘不够时清哪些"的现成答案）。
   目录还允许**加本地自定义模型而不用重新构建**（`docs/custom-models.md`），这和 requirements.md 里
   "内置目录快照 + 用户可自己加"的设计对上了。二进制只有 **6 MiB**，可以直接进安装包。
2. **对话这一路的运行时管理用 `llama-swap`**，而不是我们写一套进程+端口管理：它做的正是
   LM Studio 的"选一个模型就换后端"行为，且 MIT、有 checksums。
3. **导入（sideload）的校验用 `gguf-parser-go`**：不加载权重就能读头部、估内存与最大 context，
   正好接住"用户从别处拿了一个 .gguf"之后的提示与拒绝逻辑。
4. **下载走 `hf` CLI 的语义但不依赖它**：`HF_ENDPOINT` 换镜像（源码里
   `ENDPOINT = os.getenv("HF_ENDPOINT", _HF_DEFAULT_ENDPOINT)`）与断点/Range 是我们要的**行为**；
   外壳自己实现更省事（R2/GitHub 都已实测支持 206），把 `hf` 当参考与兜底。
5. **许可闸门全部通过**：以上都是 MIT 或 Apache-2.0，可以进安装包；
   但**目录里的模型权重不由我们代为分发**，工具带与权重分发是两件事。

## 已知风险与还没证的

- 上游会动：llmfit 已到 v1.1.17 且今天还在 push，59 个 open issue。必须**钉具体 release + sha256**，
  不能跟 latest 漂（这正是 ADR-0001 对引擎定的同一条规矩）。
- **上游哈希这件事比原先写的乐观**：2026-10-19 更正——GitHub release 的**每个产物**都在 API 里带
  `digest = sha256:<64hex>`，且实测就是内容哈希（下载两个包本地算，逐字节相等）。
  所以 llmfit 的 `.sha256` sidecar、llama-swap 的 `checksums.txt` 只是"额外方便"，
  **连本来"没给 checksums"的 gguf-parser-go 也有 API digest（7/7 实测）**，
  凡是从 GitHub release 取的东西都不必自己先算一遍哈希；真正需要自算的只有 HF 与自家 R2 两侧。
- "HF 的 etag 是不是就是 sha256"：`file_download.py` 里有一条注释提到"检查 etag 是否是合法 sha256"，
  但我**没有证明**任何具体文件上 etag==权重 sha256，所以这条只当线索，别当 pin 的来源。
- 我搜 `gguf summary cli` 与 `llm downloader resume mirror` 两个词组返回空；`--sort stars` 那次还把
  "huggingface cli" 排成 11★ 打头 —— 这轮**所有星标与"存在性"结论都改成按具体仓库名 `gh api` 单查**，
  不靠搜索排序。

## 装这些工具时实测到的坑（写车道前必须知道的）

- **`cargo install oar-ocr-cli` 会直接失败。** crates.io 明确回
  `{"errors":[{"detail":"crate \`oar-ocr-cli\` does not exist"}]}`，而仓库的 workspace 成员里确实有它
  （`members = [".", "oar-ocr-derive", "oar-ocr-core", "oar-ocr-vl", "oar-ocr-bench", "oar-ocr-cli"]`）。
  README 那句安装命令是给仓库外的人看的门面话 —— 车道必须 `git clone` 后
  `cargo build --release -p oar-ocr-cli`。库本体 `oar-ocr` 是发布的，`.crate` 在 crates.io 上有校验和，
  所以"取源码"这一步仍然可以按上游哈希钉住，只是**可执行文件这一层没有预编译产物**（与 ADR 里那条一致）。
- **工具链版本是硬前提**：workspace 里写 `edition = "2024"`、`rust-version = "1.95"`。
  runner 上的默认 toolchain 不够新就会在依赖解析阶段失败，所以 OCR 车道要显式 `rustup toolchain install`。
- **CI 没有"小而快"的扩散模型可用**（本轮实测 hf-mirror 的 `tree` 大小）：
  `Green-Sky/SD-Turbo-GGUF` 最小档 `sd_turbo-f16-q8_0.gguf` 是 **1,930.0 MiB**，
  比 `second-state/stable-diffusion-v1-5-GGUF` 的 `Q4_0` **1,494.2 MiB** 还大。
  也就是说想给生图做冒烟，1.5 GB 下载省不掉。
  （2026-10-19 更正：原先写"HF 无 digest → pin 只能首算回填，所以这条车道先不写"，
  前半句仍成立，但 **ModelScope 的 `repo/files` 元数据每个文件带 `Sha256`**，
  与它 CDN 内容寻址路径、HF 的 `lfs.size` 两条机制互相对得上 → 一个字节不下就能立钉
  （`tools/establish_pins.py` 的 `modelscope` 路线）。生图车道已经写了：
  权重钉 `b8944e9f…2261f` / **1,566,768,416 B**，见 `engine-live.yml` 的 `image-proof`。）
- `sd-cli` 的参数以源码为准实测过：`-m/--model`、`-p`、`-o/--output`（main.cpp 的选项表里就是
  `{"-o", "--output"`)，长参数另有 `--diffusion-model`、`--vae`、`--clip_l`、`--t5xxl`、
  `--cfg-scale`、`--sampling-method`、`--steps`、`-M convert`、`--type`、`-v`
  （后几组来自项目自己的 `docs/flux.md`，最小命令是 README 里的 `./bin/sd-cli -m <模型> -p "a lovely cat"`）。
  它同时有 `-h` 打 usage，所以车道可以照对话那条路的办法：**先抓 help 再决定用哪些旗标**，
  不把我记住的名字当事实。

## 已钉住：12 条，其中 8 条有两套机制对照（2026-10-19）

`pins/toolbelt.json` 是权威落盘物，由 `tools/toolbelt_pins.py` 生成；立钉与复核是同一条命令的两个开关：

```
python tools/toolbelt_pins.py --live --json pins/toolbelt.json    # 立钉 / 换版本
python tools/toolbelt_pins.py --live --check pins/toolbelt.json   # 漂移即红（CI 用这条）
python tools/toolbelt_pins.py --fixtures tests/fixtures           # 离线回放，同一张表
```

钉住的 release 与哈希来源（下面是 `--live` 的实测输出，不是手填的）：

| 工具 | 钉的 release | 平台数 | 哈希机制 |
|---|---|---|---|
| llmfit | v1.1.17 | 4（win msvc x64、mac x64 + arm64、linux gnu x64） | API digest + 每个包的 `.sha256`，**对得上** |
| llama-swap | v265 | 4（win/linux/darwin amd64 + darwin arm64） | API digest + `checksums.txt`，**对得上** |
| gguf-parser | v0.26.4 | 4（win/darwin amd64+arm64/linux amd64） | **只有 API digest**，上游没给第二套 |

- sidecar 与 API digest 是两套独立机制，`sha256sum` 格式 `<64hex>␣␣<文件名>`；实测文件名与哈希都对得上。
  任何一条对不上就进 `bad` 且 rc=1，**不许挑一个好看的用**（改一个字符即红，见 `test_tampered_sidecar_is_red`）。
- 半张钉表不落盘：`bad` 非空时 `--json` 直接拒绝写文件。`tests/test_toolbelt_pins.py` 13 条里有
  7 条是负向对照（缺 digest、缺产物、上游改名、sidecar 被改、checksums 缺失），
  专门防"检查永远绿等于没检查"。
- `gguf-parser` 那 4 条只有单一机制，强度低于另外 8 条 —— 这句写进 pin 表的 `sidecar: null`，
  别在交付说明里把它们和"两边都对得上"混成一类。
- 车道 `engine-live.yml` 的 `toolbelt-pins` 除了逐字段比对，还会**真下载**钉住的 llmfit 跑 `--help`
  与 `recommend --json`：哈希只证明字节没变，不证明文档里记的旗标还在。
  退出码不作判据（runner 上一个模型都没有），判据是 stdout 必须能解析成 JSON、help 里必须有 `--json`。
- 仍未证的是执行：这条车道和其余九条一样，一次都没跑过（private 仓库分不到 runner）。
