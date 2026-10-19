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
| HF（含 hf-mirror） | **不给**。`tree` 与 `siblings` 两个端点都只有文件名与大小，三个仓库一致 | 探针输出 `元数据是否带内容哈希：{'无'}` ×3 |
| HF resolve 响应的 `ETag` | **看着像但不是**。`e3545da2…f9deed9` 是 64 位十六进制，却不等于内容 sha256（它是 Xet/cas-bridge 的对象指纹） | 与已知 pin 直接比对不等 |
| ModelScope | **给**：LFS 对象路径按 `sha[0:2]/sha[2:4]/sha[4:]` 重建后**正好等于**同一个 pin | 上一轮整串重建验过（不是子串匹配） |
| GitHub release 产物 | **给**：每个产物的 API 里有 `digest = sha256:<64hex>`，实测就是内容哈希 | 下载 `whisper-bin-x64.zip`(8,928,640 B) 与 `sd-…-win-cpu-x64.zip`(17,706,677 B) 本地算 sha256，与 digest 逐字节相等；三家引擎 216/216、36/36、54/54 全带，OCR 权重 119 个产物 0 缺 |
| 自家 R2 | 不给，只有 `Content-Length` | 同上一轮 |

所以规则按源分两条，不是笼统一条：**引擎二进制与 OCR 权重（都在 GitHub release 上）直接用上游 `digest` 当 pin**，
CI 负责核对而不是首次计算；**HF 权重与自家 R2 镜像仍必须入库时自算 sha256**（HF 元数据不给，且 ETag 是假哈希）。
ModelScope 的对象路径可当独立旁证，HF ETag **禁止**被当校验值使用。三处大小互相印证可以放心用：
元数据 415,182,688 == HEAD 415,182,688 == 我们的 pin。

## 已经立住的两个 pin（无上游哈希的那一侧，先做一个样例）

`tools/establish_pins.py` 专门处理"源不给哈希"这一侧，做法是**跨主机逐字节对照后再立 pin**，
而不是"自己算完再自己断言相等"（那是自证）。2026-10-19 实测：

| 文件 | sha256 | 大小 | 凭据 |
|---|---|---|---|
| `ggml-tiny.bin`（字幕默认权重） | `be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21` | 77,691,713 B | 主源 hf-mirror 整包；HEAD 声明总长 == 实收；**huggingface.co 直连的头 4 MiB 与尾 4 MiB 与主源逐字节相同** |
| `samples/jfk.wav`（车道样本，已知答案） | `59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e` | 352,078 B | raw.githubusercontent 整包 + github.com 同段对照；**另与 git 自己的 blob sha1 `3184d372cd2f…` 相等**（git 是内容寻址，这算独立一道旁证） |

诚实地说清强度：这两条只证明**传输与镜像未改写**，provenance 仍然只有 HF / GitHub 一家。
原先设想的"两个独立发布方"实测不成立——ModelScope 上没有 `ggerganov/whisper.cpp`（404），
HF 上也没有 `ggml-org/whisper.cpp` 这个模型仓库（401，样本只在 GitHub 代码仓库里）。

另一个实测结论会影响目录设计：**whisper.cpp 的 GitHub release 里没有任何模型**
（30 个 release 的产物名去重 33 种，全是 xcframework/tar.gz/zip 二进制，`.bin` 命中 0）。
所以字幕这一路只有"引擎走 GitHub（有 digest）+ 权重走 HF（无 digest，须自算）"这一种组合，
一个能力里混着两种凭据强度，代码里不能按同一套校验假设写。

## 还没证的

- 上面所有"下载耗时"是从我们自己那台出口机的一次换算，不同城市/运营商必然不同，只能当量级。
- 目录里该收哪些"无审查/abliterated"模型：**不策展**。工具带与目录只按内容寻址列源，
  用户自己搜、自己导入（与 requirements.md 的"明确不做"一致）。
- 量化版的实际质量/速度差异需要真实设备矩阵，CI 的 2 核无 GPU runner 给不出结论。
