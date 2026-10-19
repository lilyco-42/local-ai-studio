# local-ai-studio（占位名）

把普通人自己的电脑用起来：在一台机器上跑本地 AI 能力 —— 对话、文字识别、字幕生成、生图。
对标 LM Studio，但引擎一律不自研。

## 两条硬约束

1. **开发者机器不编译、不运行**：所有构建与验证只在 GitHub Actions。
2. **引擎不自研**：对话/字幕/生图直接下载上游预编译产物 —— llama.cpp、whisper.cpp、stable-diffusion.cpp（都 MIT）；
   OCR 用 `oar-ocr`（Apache-2.0），它是四项能力里**唯一没有上游二进制**的，只能由 CI `cargo build`。
   权重运行时按需下载并做 sha256 校验，安装包不塞权重。

依据与取舍见 [docs/adr/0001-engines-from-upstream-binaries.md](docs/adr/0001-engines-from-upstream-binaries.md)；
功能分层、离线/联网边界与 MVP 切分见 [docs/requirements.md](docs/requirements.md)；
选型/热切换/GGUF 估算这些**不自研**的部分见 [docs/toolbelt.md](docs/toolbelt.md)（llmfit、llama-swap、
gguf-parser-go、hf CLI，全部 MIT/Apache 且实测带活动度与产物表）。

## 现在有什么

- `tools/engine_probe.py`：吸收三家命名差异（`cuda-12.4` vs `cuda-12.4.0` vs 名字里带 `Ubuntu-24.04`，
  sd 的文件名只带 sha 不带 tag 里的 `975-`），按"引擎 + 平台 + 后端族 + 架构"选 target；
  同 target 冲突先比 release 构建号再比后端版本；**不看 `releases/latest`**（whisper.cpp 的 latest 是 0 产物）。
  名字里没写 OS 的（whisper 那两个）只**推断**成 win，并单列进 `manifest["inferred"]`。
- `tools/zip_peek.py`：用 HTTP Range 只读 zip 中央目录，不下载整包、不解压、不运行，
  把上面那个推断换成条目级证据；带一正一负对照，见 ADR。
- `tools/yaml_gate.py`：解析两条 workflow，核对 needs 链、step 输出引用、heredoc 闭合与**每段 PY 真编译**，
  以及车道里写死的 target 名确实存在于 manifest（已用假 target 验证它会红）。
  还会把车道 env 里每条 64 位十六进制的值（**按值形状认，不按键名**，否则 OCR 车道的
  `DET_SHA/REC_SHA/DICT_SHA` 会漏）反查出处（`pins/*.json` 或上游 digest 夹具），
  查不到、或体积抄错、或哈希没加引号被 YAML 读成数字，都算 BAD（已用假哈希与错体积验证它会红）。
  另一条判据来自 `tools/shell_vars.py`：`set -u` 的车道里引用了本步骤从未定义、也不在 `env:`
  或 runner 内置白名单里的 `$VAR` 就点名行号 —— 这类写法 `bash -n` 查不出，只会在真跑那天当场死。
- `tools/catalog_probe.py`：目录"能不能搜"的探针（文件能下 ≠ 能浏览目录，而浏览目录才是这类的招牌交互）。
  三条判据一起成立才算过：泛词有结果、按精确仓库名能搜到它、乱词必须 0 条。
- `tools/establish_pins.py`：给权重立 pin，两条路子 —— `download`（整包算 sha256 + 第二主机头尾 4 MiB
  逐字节对照）与 `modelscope`（一个字节不下：元数据 `Sha256` == CDN 内容寻址路径 == 跨源总长）。
  第三条 `--verify` 是**降级用的**：拿另一家的整包真字节复核已钉的哈希，把 pin 从"元数据说一致"
  升到"两边逐字节实测一致"（对话 415 MB 与生图 1.5 GB 都验过，见 `pins/models.json`）。
- `tools/r2_manifest.py`：从 `pins/*.json` 推导"该镜像什么"，按源分组给实测吞吐换算，
  产出 `mirror/r2-intake.json`（只是清单，不含上传动作）。
- `tools/toolbelt_pins.py` + `pins/toolbelt.json`：把工具带（llmfit / llama-swap / gguf-parser）
  按 ADR-0001 同一条规矩钉成"具体 release + sha256"。12 条 pin 里 8 条有 sidecar 与 API digest
  两套机制互相对照，4 条只有 API digest（如实记 `sidecar: null`，不和前者混成一类）。
- `tools/catalog_snapshot.py` + `catalog/`：按 ADR 已定的两层结构生成内置目录快照。
  描述层从钉住的 commit 取（17,700 → 留 14,364 条 gguf，5.5 MiB，gzip-9 后 475 KB），
  默认档 `catalog/defaults.json` 的哈希与出处直接抄 `pins/models.json`，不重打数字。
- `.github/workflows/ci.yml`：静态门禁（含 yaml_gate）+ 单测（夹具是真实 API 捕获）+ 离线跑发现器
  与离线回放工具带钉表。
- `.github/workflows/engine-live.yml`：打真实 API 发现产物 → 下载 linux 引擎（不编译）→ 下载 pin 住的
  0.5B GGUF 并校 sha256 → 无头推理，要求真的抓到 `tokens per second > 0`；
  另有 os-inference-proof 逐条解压 `manifest["inferred"]` 里的产物，要求里面有 `.exe/.dll`。

## 这套 CI 能证明什么、不能证明什么

能证明：上游产物可发现、可下载、校验对得上、在无 GPU 的 runner 上真能产出 token。
**注意：这些车道至今一次都没跑过** —— 仓库是 private，这个账号给 private 仓库分不到 runner。
观察到的形状有两种，都不执行任何一步：早先是 job 两秒失败、`steps: []`（原因只在 check-run
annotation 里）；2026-10-19 这两次 push 则是**一直停在 queued**（`gh run view <id> --json jobs`
给 `runner_name: null`、`steps: 0`，annotations 接口 404），21 分钟没动过。所以别把"queued"
读成"在排队、等等就有"。所以下面"能证明"目前是设计意图，不是结果。
**也不能证明：图形界面真的打得开。** 桌面外壳交付时必须另找设备验证，不拿 CI 绿当"能用"。


## 待定

仓库名（`local-ai-studio` 是占位）与外壳许可（AGPL-3.0-or-later 还是 MIT，所以现在**故意没放 LICENSE**）。
可见性已经定了 private，代价就是跑不了 CI —— 三条出路都归你选：
① `gh repo edit lilyco-42/local-ai-studio --visibility public`；
② Settings→Billing 加付款方式 / 提 Actions 支出上限；
③ 在 lain42.top 上挂一个 self-hosted runner（仍然算"靠 GitHub Actions 出包"，
但要在这台跑着线上对局的机器上做 `cargo build` 与 1.5 GB 下载，我不代你决定）。

**已经定了的**：模型目录来源 —— 分两层，描述层用 llmfit 自带的 HF 快照（17,700 条，压缩后 850 KB），
下载/校验层自建 `repo + 文件名 + sha256 + 多候选源`，因为那张表里一个内容哈希都没有。
证据与复现见 [docs/model-catalog.md](docs/model-catalog.md)；四条已立住的权重 pin 在
[pins/models.json](pins/models.json)，OCR 默认档 3 条在 [pins/ocr.json](pins/ocr.json)，
工具带 12 条在 [pins/toolbelt.json](pins/toolbelt.json)。
i18n 已经按实测拆成"识别语言"与"界面语言"两条轴并给出默认包，见
[docs/i18n.md](docs/i18n.md)。
