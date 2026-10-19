# 模型目录：四项能力的权重来源、真实大小与 pin 规则

日期 2026-10-19 · 大小全部从镜像 API 的 `lfs.size` 实测（`tools/model_catalog_probe.py`），
不是README 里抄的估值；下载速率用同一天从国内出口量的 0.82–1.01 MiB/s（R2）与
0.10–1.01 MiB/s（GitHub/hf-mirror 波动）换算，所以时间一律给区间。

## 默认选择（按"普通人电脑 + 首次安装"排）

| 能力 | 默认权重 | 大小 | 下载耗时 | 备选 |
|---|---|---|---|---|
| 文字识别 | oar-ocr 移动端 det + rec | **4.6 + 7.4–15.8 MiB** | <1 分钟 | 服务端 det/rec 80–108 MiB；版面 `pp-doclayoutv3.onnx`；公式 `unimernet.onnx` **1,756 MiB，绝不默认** |
| 字幕 | `ggml-small-q5_1.bin` | **181.3 MiB** | 3–4 分钟 | `tiny-q5_1` 30.7 / `base-q5_1` 56.9 MiB（老机器首装）；`base` 141.1；`small` 465.0；`medium.en` 1,462.7；`large-v3` 2,951.7；`large-v3-turbo` 1,549.3 |
| 对话 | `Qwen2.5-0.5B-Instruct` q2_k | **395.9 MiB** | 7–8 分钟 | 同一仓库 q4_k_m 468.6 / q5_k_m 498.0 / q6_k 620.2 / q8_0 644.4 / fp16 1,207.8 MiB |
| 生图 | `stable-diffusion-v1-5-pruned-emaonly-Q4_0.gguf` | **1,494.2 MiB** | **25–30 分钟** | Q5_0 1,541.1 / Q5_1 1,564.6 / Q8_0 1,681.9 / f16 2,033.8 / f32 4,067.5 MiB（同仓库还有 .safetensors 4,067.6） |

来源仓库（名字是从镜像 API 搜出来的，不是猜的）：ASR 权重在 `ggerganov/whisper.cpp`
（47 个文件、整仓库 28.9 GiB，含 `-q5_1`/`-q8_0` 量化版与 `*-encoder.mlmodelc.zip` 的 CoreML 包）；
生图在 `second-state/stable-diffusion-v1-5-GGUF`（52,762 下载，同类还有 `gpustack/…`、
SD3.5-medium/large 与 `city96/…`）；对话在 `Qwen/Qwen2.5-0.5B-Instruct-GGUF`
（搜索里同时能看到 `lmstudio-community/` 的同一基座版本，说明它就是 LM Studio 用户实际在下的那批）。

## 生图这一行改变了默认策略

之前 requirements.md 写"只有 CPU 时降到量化 SD1.5 级"，量完大小才知道代价：**最省的量化版也要 1.5 GB**，
按实测速率是 25–30 分钟。所以：

1. 生图权重**不能进安装默认下载队列**，必须是用户显式点一次、且能看到进度与预计时间的后台任务；
   断点续传在这里不是加分项而是必需项（三家源都验过支持 Range/206）。
2. 对话与字幕的首装体量是可以接受的（395.9 + 181.3 MiB ≈ 8–11 分钟）；OCR 几乎免费。
3. 真要"装机就能玩"，能默认装的只有 OCR 和 tiny/base 级 ASR。

## pin 规则：GitHub 给、HF 与 R2 不给，而 HF 还多一个会咬人的假哈希

在**已知答案的样本**上比对（我们独立钉过 `qwen2.5-0.5b-instruct-q2_k.gguf` =
`9ee36184e616…f798cb8`、`415,182,688` 字节），2026-10-19 实测：

| 源 | 给不给权重 sha256 | 证据 |
|---|---|---|
| HF（含 hf-mirror） | **给**（2026-10-20 更正）：`GET /api/models/{repo}/tree/{rev}` 的每个文件带 `lfs.oid`，那就是内容 sha256；实测三颗独立 pin 逐字符相等。**但"两个主机"不等于"两家提供方"**：同一台 `hf-mirror` 从本机请求会 308 回 `huggingface.co`（真正回答的是 HF），从国内出口（lain42.top）请求则是它自己答（200、无跳转、3,145 B）—— 所以这是**同一分发方的两个入口**，独立的那一家仍然是 ModelScope。`--cross-hf` 会把"真正回答的主机"记进 `meta_hashes.answered_by`，级别名跟着它变 | 探针输出 `元数据是否带内容哈希：{'有'}`；控制样本 `9ee36184e616dfc7…` == pin。上一版写"不给"是**读者的盲点**：代码读的是 `lfs.oidsha`/`lfs.sha`，而 HF 的键叫 `lfs.oid`，读不存在的键就成了"上游没有" |
| HF resolve 响应的 `ETag` | **看着像但不是**。`e3545da2…f9deed9` 是 64 位十六进制，却不等于内容 sha256（它是 Xet/cas-bridge 的对象指纹） | 与已知 pin 直接比对不等 —— 这条**没有被更正**：要用的是 `lfs.oid`，不是 ETag |
| ModelScope | **给两条**：`repo/files` 元数据每个文件带 `Sha256`；CDN 路径又是 `lfs-objects/<sha[0:2]>/<sha[2:4]>/<sha[4:]>` 的内容寻址还原 | 本轮实测两者对 SD1.5 Q4_0 完全一致，且总长 == hf-mirror 的 `lfs.size` |
| GitHub release 产物 | **给**：每个产物的 API 里有 `digest = sha256:<64hex>`，实测就是内容哈希 | 下载 `whisper-bin-x64.zip`(8,928,640 B) 与 `sd-…-win-cpu-x64.zip`(17,706,677 B) 本地算 sha256，与 digest 逐字节相等；三家引擎 216/216、36/36、54/54 全带，OCR 权重 119 个产物 0 缺 |
| 自家 R2 | 不给，只有 `Content-Length` | 同上一轮 |

所以规则按源分三条，不是一句"全部自算"：**GitHub release 产物**（引擎、OCR 权重、工具）直接用上游
`digest`；**HF 上的权重**读 `tree` 的 `lfs.oid`，并去 ModelScope 查同名仓库的 `Sha256` 元数据 +
CDN 内容寻址路径 + 跨源总长 —— **两家各自独立给哈希且相等**时零下载就能立 pin（比单家强得多，
但仍只是"两家都说一样的哈希"，第 5 级要有一家给整包真字节）；**自家 R2 只有 `Content-Length`**，
入库时必须自算。三处都不许拿 HF 的 ETag 当校验值（它不是内容哈希，这条没被更正）。
大小可以三方互相印证：
元数据 415,182,688 == HEAD 415,182,688 == 我们的 pin。

> 2026-10-20 的更正为什么重要：这张表原本记着"HF 不给哈希"，于是下载层的设计就按"只能靠
> ModelScope 镜像 + 自算"来写；实际上 `lfs.oid` 一直是真 sha256。而且国内出口实测
> `hf-mirror.com/api/...` **是它自己回答**（200，不经跳转），所以在 CN 也能拿到这份元数据 ——
> 这不只是措辞：**目录里 84% 没有预置文件的条目，从此可以按"两家分发方元数据哈希一致 → 下载后
> 再自算一遍"来校验**，不必先要求那个仓库在 ModelScope 有镜像。
> 唯一的诚实边界：`hf-mirror` 与 `huggingface.co` 是同一家，别把"两个域名都给"数成两家。

## 已经立住的四个 pin

`tools/establish_pins.py` 处理的是"只有体积、哈希要自己凑"这一侧，做法是**跨主机逐字节对照后再立 pin**，
而不是"自己算完再自己断言相等"（那是自证）。2026-10-19 实测，结果落盘为
**`pins/models.json`**（含每条的 `primary` / `cross_checked_against` / `provenance`，
车道里写死的哈希必须与它对得上，改 pin 要走工具而不是直接改 yaml）。

**这一轮又翻掉本文早先一条结论**：ModelScope 不只 CDN 路径是内容寻址的，它
`api/v1/models/<ns>/<repo>/repo/files` 的元数据里**每个文件都带 `Sha256` 字段** ——
所以"HF 上的权重只能自己算"是错的（前提是同仓库在 ModelScope 上有镜像）。

| 文件 | sha256 | 大小 | 方法与凭据 |
|---|---|---|---|
| `stable-diffusion-v1-5-pruned-emaonly-Q4_0.gguf`（生图默认） | `b8944e9fe0b69b36ae1b5bb0185b3a7b8ef14347fe0fa9af6c64c4829022261f` | **1,566,768,416 B** | **零下载**：ModelScope 元数据 `Sha256` == 它 CDN 的 `lfs-objects/<2>/<2>/<60>` 还原值，总长又与**另一家**的 `lfs.size` 一致 |
| `qwen2.5-0.5b-instruct-q2_k.gguf`（对话车道默认） | `9ee36184e616dfc76df4f5dd66f908dbde6979524ae36e6cefb67f532f798cb8` | 415,182,688 B | 同上，**零下载**三条一致。这颗原先只是写进 yaml 的数字，今天才由工具复现出来 —— 复现前后才算钉过 |
| `ggml-tiny.bin`（字幕默认权重） | `be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21` | 77,691,713 B | 主源 hf-mirror 整包；总长 == 实收；**huggingface.co 直连的头 4 MiB 与尾 4 MiB 与主源逐字节相同** |
| `samples/jfk.wav`（车道样本，已知答案） | `59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e` | 352,078 B | raw.githubusercontent 整包 + github.com 同段对照；**另与 git 自己的 blob sha1 `3184d372cd2f…` 相等**（git 是内容寻址，这算独立一道旁证） |

**两条自己造的假阴性（2026-10-19 复测时抓到，都已改）**：
1. CDN 那条用 `curl -sIL`（HEAD）取 `url_effective` —— ModelScope 的 CDN 对 HEAD 不给这条重定向，
   于是"内容寻址还原 = None"，把两条本来能立的 pin 判成"少一条独立机制"。改成 GET 跟跳转后两条都还原成功。
2. 跨源总长那条用 `curl -sS` 没加 `-L`，而 **hf-mirror 对 `/api/...` 回 308 跳回 huggingface.co**
   （它只镜像文件端点），收到 0 字节 → "HF 侧没拿到总长"。加 `-L` 并把真正回答的主机打出来。
   所以本表那列措辞从"与 hf-mirror 一致"改成了"与另一家一致"，`pins/models.json` 里记的是
   `huggingface.co`：**这一侧的对照其实是 HF 本体，不是国内镜像**，国内取不到它。

生图与对话两条的诚实边界：**没有整包下载校验过**（不为立 pin 去下 1.5 GB / 415 MB），靠的是同一提供方两条
独立机制一致 + 跨提供方总长一致；所以真正下载时仍必须按这颗 pin 复核，对不上就是源被换过。
另外同一工具在 lain42.top 上跑时 whisper 那两条会红 —— 那台机器到 `huggingface.co` 与
`raw.githubusercontent.com` 的出站被重置（curl rc=35），是**出口限制不是文件问题**；
工具四条只要有一条立不住就如实给退出码 1，不糊成"通过"。

诚实地说清 download 那两条的强度：只证明**传输与镜像未改写**，provenance 仍然只有 HF / GitHub 一家。
原先设想的"两个独立发布方"对 `ggerganov/whisper.cpp` 实测不成立（ModelScope 404），
HF 上也没有 `ggml-org/whisper.cpp` 这个模型仓库（401，样本只在 GitHub 代码仓库里）。

另一个实测结论会影响目录设计：**whisper.cpp 的 GitHub release 里没有任何模型**
（30 个 release 的产物名去重 33 种，全是 xcframework/tar.gz/zip 二进制，`.bin` 命中 0）。
所以字幕这一路是"引擎走 GitHub（有 digest）+ 权重走 HF（HF 的 `tree.lfs.oid` 就是 sha256，2026-10-20 更正；另可再拿 ModelScope 的 `Sha256` 当第二家对照）"，
一个能力里混着两种凭据强度，代码里不能按同一套校验假设写。

## 上游有一整张现成目录，但它给不了校验值（2026-10-19 实测）

ADR 里"模型目录来源"原本挂着不定。今天把 llmfit 自带的 HF 目录整表量了一遍，
它把这条决定需要的数字都补齐了：

**抓的是** `llmfit-core/data/hf_models.json` @ commit `fbde9aaf8de3abbc7cc0eb3bb7ea4f705227bb7a`，
**15,922,575 B**，sha256 `0d72c2eee81b0e3671c4a1ba374e987afb57d7eaa7075a9a4488c3ab0e3f4e83`
（按 commit 重抓逐字节相同，所以这组数字可复现，不是"我看了一眼"）。

| 量 | 值 | 对我们意味着什么 |
|---|---|---|
| 条目数 | **17,700**（format=gguf 14,364） | 对话这一项不用自己攒目录 |
| `min_ram_gb` / `recommended_ram_gb` / `min_vram_gb` 非空 | **100%** | "这台机器能跑什么"的拟合输入是现成的 |
| 字段数 | 38（含 capabilities / use_case / license / languages / architecture / 上下文长度 / MoE 参数） | 描述层够用 |
| **值形如 64 位十六进制的字段** | **0 个**（整表遍历，不是子串匹配） | **它给不了校验值**，与"HF 元数据无内容哈希"那条实测一致 |
| `gguf_sources` 里有什么 | 只有 `{repo, provider}` —— 没有文件名、没有大小、没有哈希 | 下载层必须我们自己落到"文件 + sha256" |
| 带 `gguf_sources` 的条目 | **2,297 / 17,700（13%）**，文件条目合计 2,921 | 八成七的条目连可下载的仓库都没指出来 |
| `quantization` 分布 | Q4_K_M **15,588（88%）** | 它是**模型级**索引，每条目一个默认量化，不是文件清单 |
| `pipeline_tag` 分布 | text-generation 10,079 / image-text-to-text 3,370 / TTS 1,338 / **ASR 5** / OCR **0** / 生图 **0** | 四项能力里**只有对话**被它覆盖 |
| `_revalidated` 为空 | **16,128 / 17,700（91%）** | 快照会漂，同步时不能当作"已复核" |
| `license` 分布 | apache-2.0 7,616 / **无 3,784** / **cc-by-nc-4.0 1,327** | 快照 21% 无许可、7.5% 禁商用，UI 必须把这个字段显示出来 |
| 压缩后体积 | gzip-9 **850,099 B（5.3%）** / lzma-9 607,988 B（3.8%） | "内置目录快照"完全可行 |

顺带一条实现参照：llmfit 是把这 15 MiB **以 `include_str!` 未压缩嵌进二进制的**
（`llmfit-core/src/models.rs:1719`），靠发布归档压缩 —— win msvc 包只有 6,222,681 B
（见 `pins/toolbelt.json`），比 JSON 本身小，就是这个办法的证据。

**由此定下的目录结构（写进 ADR）**：目录分两层，不许混。
**描述层**用 llmfit 快照，按 commit + sha256 钉住、联网只刷新；
**下载/校验层**自建 `repo + 文件名 + sha256 + 多候选源`，因为上游那层没有文件名也没有哈希 ——
这正是 `pins/models.json` 的四条已经做的事。字幕/OCR/生图三条能力的目录**只能自建**
（llmfit 对它们是 ASR 5 条 / OCR 0 / 生图 0），而这三条我们已经各自实测到文件级：
OCR 93 条 URL 全带上游 digest、whisper 权重与样本已钉、SD1.5 Q4_0 已钉。

### 已经按这个结构生成过一次（`tools/catalog_snapshot.py`）

```
python tools/catalog_snapshot.py --live --check     # 验上游快照还是钉住的那份 + 核对侧表
python tools/catalog_snapshot.py --live             # 重新生成 catalog/*.json 与侧表
python tools/catalog_snapshot.py --raw 整文件 --check  # 拿本地已下载的上游整文件复现校验
```

2026-10-19 从钉住的 commit 真跑一遍的结果：

| 输出 | 数字 |
|---|---|
| 上游 17,700 条 → 留下 | **14,364 条**（丢掉 3,336 条非 gguf，一条都没静默丢） |
| `catalog/describe.json` 体积 | **5,808,032 B**，gzip-9 后 **474,976 B（8.2%）** |
| 留下条目里有可下载仓库的 | **2,290 条（16%）** —— 其余只有描述，落到文件还得现查 |
| `license` 归一为 `unknown` | **3,404 条**；含 `nc` 子串的 **376 条**（这条是子串启发式，只当统计不当闸门） |
| `catalog/defaults.json` | **6 条**：`pins/models.json` 的 3 条权重 + `pins/ocr.json` 的 3 条 OCR 默认档；字段直接抄 pin 文件，不重打数字，另排除 1 条车道样本（`samples/jfk.wav` 不是给用户装的模型） |

**2026-10-20 复现验证**（这条补上了"派生物不进仓库"留下的那个洞）：重新从钉住的 commit 下载
上游整文件（15,922,575 B，28 s，sha256 实算 `0d72c2ee…` 与 `UPSTREAM["sha256"]` 相等），
用当前 `slim()` 重导出一遍，得到的 **5,808,032 B 与盘上那份逐字节相同**（`cmp` 无差异）。
所以"可复现"不再是一句设计意图，而是一次量过的结果。
因此新增 `catalog/describe.sha256`（**提交进仓库**，一行：`<64hex>␣␣<体积>␣␣路径`）：
`describe.json` 不进仓库，那它在仓库里唯一的身份就是这颗哈希；`--live --check` 与 `--raw --check`
都会重导出并比对，**任一边变了就红**（slim() 少抄一个字段、或上游漂了而哈希校验被绕过），
已用改一个 hex 的副本证它会红（`Ran 16 tests`，含侧表 8 条）。缺侧表也算红，不许静默跳过。

四个设计后果：
- 内置目录快照的代价量级定了 —— **不到 500 KB 压缩**，比 ADR 原先担心的"要自带一个大目录"小得多，
  而且只带 16 个字段（上游 38 个字段里的 `head_dim`/`moe_intermediate_size` 等是它自己拟合用的，
  全搬进来等于把别人的 schema 变更变成我们的破坏性变更）。
- `describe.json` **不进仓库**（已 gitignore），但 **`describe.sha256` 进**：它是按 commit+sha256
  可复现的派生物，要改就改 `UPSTREAM` 坐标，不把 5.5 MiB bulk 提交进历史 ——
  而"bulk 不进仓库"不等于"仓库对它没有任何记载"。
- 16% 有可下载仓库这件事说明**描述层不能当下载层用**：剩下 84% 想让用户点一下就下，
  必须先经文件级解析。这条路已经做成工具与车道（2026-10-20）：`tools/file_index.py`
  按 `repo + 文件名` 同时读 HF `tree.lfs.oid` 与 ModelScope `repo/files` 的 `Sha256`，
  五种状态 `agree / single / conflict / pin_conflict / absent`，
  **只有两家都给且相等才 `usable_before_download=True`**；两家不等时不给任何一个哈希当答案，
  只有一家时必须"下载后自算"；候选源按 lain42.top 那轮实测吞吐排
  （ModelScope 4.2 → hf-mirror 0.25 → huggingface.co 记 0，因为它从 CN 实测是 0 字节）。
  车道 `file-index` 跑 `--selfcheck`（五种状态 + 顺序的离线对照）与
  `--control pins/models.json`（正向 = 已钉的三条；负向 = 一根故意造的假文件名，
  它要是也被判成"有哈希"就说明这套比对对任何输入都有答案；覆盖度地板 = 2 条两家确认）。
  实测结果：qwen 与 SD 两条是 `agree`（两家都确认且等于 pin），`ggml-tiny.bin` 是 `single`
  （ModelScope 没镜像这个仓库，返回 200 + 0 个文件，不是取不到）。
- 车道：`engine-live.yml` 的 `catalog-snapshot` 用 `--live --check` 同时盯两件事 —— 上游漂移
  （整文件哈希）与**目录形状漂移**（重导出 vs 侧表）；
  `ci.yml` 用真实切片夹具离线跑同一套规则。

## pin 强度不是一条线，是六级（`establish_pins.py --verify` 用来把低升到高）

"这个权重钉住了没有"不是非黑即白。按**证据能走到哪一步**分六级，写目录/交付说明时必须标出级数：

| 级 | 凭据 | 能说明什么 | 说不清什么 |
|---|---|---|---|
| 1 | 哈希只写在 workflow env 里 | 什么也不能说明 | 没人复现过它 —— 本轮之前那颗 415 MB 对话权重就在这一级 |
| 2 | 只给体积（自家 R2；HF 的 `tree` 已从这一级挪走，见下方 4.5） | 文件还在、长度没变 | 内容对不对 |
| 3 | 上游给 `digest = sha256:`（GitHub release 全部产物） | 内容与发布方算过的一致 | 发布方只有一家；来源独立性没有 |
| 4 | 元数据 `Sha256` + CDN 内容寻址路径 + 跨源总长（ModelScope 零下载） | 同一提供方**两条独立机制**一致，另一家体积也一致 | 整包字节一次都没下过 |
| 4.5 | 两家分发方**各自独立给同一个内容哈希**，零下载。现已量到两对：对话/生图权重走 `--cross-hf`（ModelScope `Sha256` × HF `tree.lfs.oid`）；OCR 语言包走 GitHub release 的 `digest` × 上游自己的 ModelScope 镜像 `greatv/oar-ocr` 元数据（`tools/ocr_language_pins.py`，28 条 rec/dict 里 25 条到这一级） | 两家都说同一个哈希 —— 单家说谎已经不够了 | 仍没人下过整包；而且两家都是**分发方**，不是原作者 |
| 5 | `--verify`：从**另一家**下整包算 sha256，与已钉的哈希逐字节相等 | 跨提供方 + 真字节，两边都不许说谎 | 仍是两个镜像，不是两个原作者 |
| 6 | 两个独立发布方各下整包且哈希一致 | 真正的来源独立 | 现实中几乎不存在：whisper/SD 权重实测只有一家发布 |

```
python tools/establish_pins.py --verify pins/models.json                 # 全部元数据来的 pin 都升一级
python tools/establish_pins.py --cross-hf pins/models.json                 # 第二家给哈希：只比元数据，不下载
python tools/establish_pins.py --verify pins/models.json --only qwen2.5   # 只升某一条（1.5 GB 那条要单独挑）
python tools/ocr_language_pins.py --check-pins pins/ocr.json --live       # OCR 默认档：GitHub × ModelScope × registry.rs 三处对齐
```

三条硬规矩，都是这轮踩出来的：
- **只对"哈希来自元数据"的那几条有意义**。download 路线的哈希本来就是自己算的，再"复核"一遍是自证。
- 换提供方是这条检查的全部价值所在：所以 URL 只能从 `TARGETS` 推（`sources()`），
  `TARGETS` 里认不出仓库的键**直接拒绝**，绝不拼一个猜出来的地址。
- **一次车道运行就是一次第 5 级复核**：`subtitle-proof` / `image-proof` / `headless-inference` 都会
  `sha256sum -c` 真下载的文件。换句话说 CI 跑绿的那天，这些 pin 会被自动重验一遍 ——
  而 CI 现在一次都没跑过，所以这一级目前只有本机证据。

### 每条 pin 现在处在哪一级（2026-10-19 全部实测）

| pin | 级 | 实测凭据 |
|---|---|---|
| `qwen2.5-0.5b-instruct-q2_k.gguf`（对话） | **5 + 4.5** | ModelScope 元数据给哈希，**hf-mirror 下满 415,182,688 B 真字节**，实算 `9ee36184e616…` 与钉值相等；2026-10-20 另加 `--cross-hf`：HF 两主机 `lfs.oid` 也等于它 |
| `stable-diffusion-v1-5-pruned-emaonly-Q4_0.gguf`（生图） | **5 + 4.5** | 同上，整包 1,566,768,416 B 流式算完，`b8944e9fe0b6…` 相等；`--cross-hf` 下 HF 两主机 `lfs.oid` 同值 |
| OCR `pp-ocrv6_tiny_det/rec` 与 `ppocrv6_tiny_dict` | **5 + 4.5** | 上游 release digest 给哈希，同一条 URL 整包下回并算一致（1,780,590 / 4,462,639 / 27,156）= 第 5 级的"同家真字节"那半；**另外** `python tools/ocr_language_pins.py --check-pins pins/ocr.json` 验到 GitHub 的 `digest`、上游 ModelScope 镜像 `greatv/oar-ocr` 的元数据 `Sha256` 与 `registry.rs` 三处给的是同一个哈希、体积也相等 → 4.5 级。写进 `pins/ocr.json` 的 `meta_hashes`，车道 `ocr-languages` 每次都查。提供方仍然只有一家，所以来源独立性没涨 |
| OCR 语言轴 14 种 × (rec + dict) + 共享 det | **4.5（25/28 条）** | `tools/ocr_language_pins.py` 叠四张表（models.md 两张字典表 + 语言权重表、GitHub 全部 release、ModelScope 镜像、`registry.rs`），在场来源必须给同一个哈希才立 pin。合计 117.6 MiB / 含 det 122.2 MiB，落盘 `pins/ocr-languages.json`。剩下 3 条是繁中/日/格鲁吉亚的 v3 字典：**两家都没有**，按 PaddleOCR 的一个 commit 取 raw 自算（`models.md:116` 明写它们不在 auto-download 注册表内）。查找一律按链接里的 **tag** 定位，因为同名文件在不同 tag 可以是不同内容（`ppocrv5_latin_dict.txt`：v0.1.0 是 1,634 B，v0.3.0 是 2,616 B） |
| `ggml-tiny.bin` | 3 + 同族两主机头尾 | 整包自算 + huggingface.co 头尾 4 MiB 与 hf-mirror 逐字节一致；ModelScope 无该仓库 → **升不到第 5 级** |
| `samples/jfk.wav` | 3 + git 旁证 | 整包自算 + 同族对照 + 与 git blob sha1 相等（内容寻址的第三方旁证） |
| 工具带 12 条 | 3（8 条双机制） | sidecar × API digest 两套机制一致，但**都出自 GitHub 一家** |

**没有任何一条到第 6 级**，而且大概率永远到不了：这些权重的原始发布方各只有一家，
ModelScope 与 HF 是两个**分发渠道**而不是两个**发布者**。交付说明里"两个来源都验过"必须写成
"两家分发渠道给的字节一致"，不能说成"来源独立"。

顺带一条会改变镜像决策的实测（同一次复核带出来的）：
从这台 CN 出口拉 ModelScope 的同一颗权重，1 / 16 / 32 MiB 三档分别给出
**4.2 / 22.9 / 12.1 MiB/s**，而早轮自家 R2 只有 **0.82–1.01 MiB/s** ——
也就是说**大权重不该镜像到 R2，而该把 ModelScope 列为首选候选源**；
GitHub release 那侧（工具带 + OCR，合计 154.2 MiB，实测 0.014 MiB/s ≈ 184 min）才真的值得镜像。
清单由 `tools/r2_manifest.py` 从 `pins/*.json` 推导（`mirror/r2-intake.json`，
含每条的候选源与按源分组的结论），一个数字都不手打。前提是先有上面的第 5 级：
**正因为字节与哈希跨提供方对得上，才敢把更快的那家列为首选。**

## 还没证的

- 上面所有"下载耗时"是从我们自己那台出口机的一次换算，不同城市/运营商必然不同，只能当量级。
- 目录里该收哪些"无审查/abliterated"模型：**不策展**。工具带与目录只按内容寻址列源，
  用户自己搜、自己导入（与 requirements.md 的"明确不做"一致）。
- 量化版的实际质量/速度差异需要真实设备矩阵，CI 的 2 核无 GPU runner 给不出结论。
