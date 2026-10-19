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
"只能靠 GitHub Actions 出包"这条硬约束本身的可行性（runner 缺什么、包该装什么）见
[docs/packaging.md](docs/packaging.md)；
选型/热切换/GGUF 估算这些**不自研**的部分见 [docs/toolbelt.md](docs/toolbelt.md)（llmfit、llama-swap、
gguf-parser-go、hf CLI，全部 MIT/Apache 且实测带活动度与产物表）。

## 现在有什么

- `tools/engine_probe.py`：吸收三家命名差异（`cuda-12.4` vs `cuda-12.4.0` vs 名字里带 `Ubuntu-24.04`，
  sd 的文件名只带 sha 不带 tag 里的 `975-`），按"引擎 + 平台 + 后端族 + 架构"选 target；
  同 target 冲突先比 release 构建号再比后端版本；**不看 `releases/latest`**（whisper.cpp 的 latest 是 0 产物）。
  名字里没写 OS 的（whisper 那两个）只**推断**成 win，并单列进 `manifest["inferred"]`。
- `tools/zip_peek.py`：用 HTTP Range 只读 zip 中央目录，不下载整包、不解压、不运行，
  把上面那个推断换成条目级证据；带一正一负对照，见 ADR。
- `tools/yaml_gate.py`：解析三条 workflow，核对 needs 链、step 输出引用、heredoc 闭合与**每段 PY 真编译**，
  以及车道里写死的 target 名确实存在于 manifest（已用假 target 验证它会红）。
  车道分段与预跑的**计数**由 `tests/test_lane_replay_report.py` 钉住：三条 workflow 共 17 段 `<<'PY'`、
  其中 12 段含判据（认 `check(`/`SystemExit`/`assert`/`sys.exit(`）、本地预跑引用 8 个
  (job, 步骤名) 组合 —— 重数：`python -W error::ResourceWarning -m unittest tests.test_lane_replay_report -v`。
  这几个数以前只活在文档里，漂过三次，所以现在是断言不是散文。
  还会把车道 env 里每条 64 位十六进制的值（**按值形状认，不按键名**，否则 OCR 车道的
  `DET_SHA/REC_SHA/DICT_SHA` 会漏）反查出处（`pins/*.json` 或上游 digest 夹具），
  查不到、或体积抄错、或哈希没加引号被 YAML 读成数字，都算 BAD（已用假哈希与错体积验证它会红）。
  另一条判据来自 `tools/shell_vars.py`：`set -u` 的车道里引用了本步骤从未定义、也不在 `env:`
  或 runner 内置白名单里的 `$VAR` 就点名行号 —— 这类写法 `bash -n` 查不出，只会在真跑那天当场死。
  它自己也被红过：注释里写一句"不要用 `:$LD_LIBRARY_PATH`"就把它惹红了，所以现在整行 `#` 注释
  不查（但 heredoc 正文里的 `#` **照查** —— 未加引号的 `<<PY` 会真展开 `$`，两种情况各有单测）。
- `tools/bash_gate.py`：把每一条 `run:` 写进临时文件跑 `bash -n`（只解析，绝不执行任何一行）。
  来历是一个假结论：本机 PATH 上的 `bash` 是 WSL 占位程序，对任何文件都返回非 0，
  第一遍跑出"42/42 步骤全部语法错"，看着像查得严其实一条真信息都没有。
  所以现在每个候选 bash 必须先过双对照（坏文件非 0、好文件 0），过不了就换下一个，
  全不过就报 `SKIP（unreliable/missing + 原因）` 而不是装成查过。2026-10-20 实测覆盖 53 段、0 问。
- `tools/png_proof.py` / `tools/srt_proof.py`：两条常驻服务车道与 CLI 车道**共用**的产物判据。
  前者读 PNG 的 IHDR 与 `tEXt/parameters`（也认 `result.images[0].b64_json` 那种塞在 JSON 里的图），
  后者把 SRT 解析成块再逐块要编号连续、时间单调、正文非空 —— 而不是 `grep '-->'` 一发过。
  语料里有别的工程留下的真 SRT 与 Pillow 写的真 PNG，判据不是在测自己拼的字节。
- `tools/catalog_probe.py`：目录"能不能搜"的探针（文件能下 ≠ 能浏览目录，而浏览目录才是这类的招牌交互）。
  三条判据一起成立才算过：泛词有结果、按精确仓库名能搜到它、乱词必须 0 条。
- `tools/establish_pins.py`：给权重立 pin，两条路子 —— `download`（整包算 sha256 + 第二主机头尾 4 MiB
  逐字节对照）与 `modelscope`（一个字节不下：元数据 `Sha256` == CDN 内容寻址路径 == 跨源总长）。
  第三条 `--verify` 是**降级用的**：拿另一家的整包真字节复核已钉的哈希，把 pin 从"元数据说一致"
  升到"两边逐字节实测一致"（对话 415 MB 与生图 1.5 GB 都验过，见 `pins/models.json`）。
- `tools/r2_manifest.py`：从 `pins/*.json` 推导"该镜像什么"，按源分组给实测吞吐换算，
  产出 `mirror/r2-intake.json`（只是清单，不含上传动作）。
- `tools/citation_check.py` + `pins/citations.json`：把"上游某 ref 的某一行写着某件事"这类结论
  变成能查的东西。78 条声明各记（2026-10-20 按 pin ref 实跑过一次，0 问题；`--cache .cache/refs --fill` 抓一次原文之后可离线复算） `repo/ref/path/line/expect`（还多一种 `absent`：连「这份文件里没有 X」也要查），逐条取回上游**核对那一行仍然含那个子串**
  （只看文件在不在没用：默认值改了文件照样在）。
  2026-10-20 加了一条硬判据：**相邻行也含同一子串 → 这条不会红**（行号漂一格就撞上隔壁那行，
  `/health` 与 `/v1/health` 两行注释一模一样，实测"行号 +1"复算时它是唯一没红的）。
  相隔很远的重复不拦，它不破坏灵敏度。总数由单测钉成字面量，所以"78"这个数改表时必须一起改。
  车道 `citation-drift` 先跑 `--selftest`（明知错的必须认成错）再逐条核对。
  来历：本轮踩过七次"判据指向一个不存在的东西却照样跑完"（`llama-completions`、去 tag 里找 CLI、
  `-of jfk.srt`、去日志里找 `Steps:`、grep `tokens per second`、启动计划引用了两个表里不存在的出处 id、
  以及这条相邻重复）。
- `tools/ocr_language_pins.py` + `pins/ocr-languages.json`：OCR「识别能力」那一轴的 pin。
  叠四张表（models.md 的两张字典表 + 语言权重表、GitHub 全部 release 产物、上游 ModelScope 镜像
  `greatv/oar-ocr`、`registry.rs` 登记表），**在场来源必须给同一个 sha256 才立 pin**，
  不一致就拒绝。结果：14 种语言、28 条 rec/dict 里 25 条两家一致（4.5 级），
  3 条（繁中/日/格鲁吉亚的 v3 字典）两家都没有 → 按 PaddleOCR 的一个 commit 取 raw 自算。
  查找按 models.md 链接里的 tag 定位 —— 同名文件在不同 tag 可以是不同内容。
- `tools/toolbelt_pins.py` + `pins/toolbelt.json`：把工具带（llmfit / llama-swap / gguf-parser）
  按 ADR-0001 同一条规矩钉成"具体 release + sha256"。12 条 pin 里 8 条有 sidecar 与 API digest
  两套机制互相对照，4 条只有 API digest（如实记 `sidecar: null`，不和前者混成一类）。
- `tools/catalog_snapshot.py` + `catalog/`：按 ADR 已定的两层结构生成内置目录快照。
  描述层从钉住的 commit 取（17,700 → 留 14,364 条 gguf，5.5 MiB，gzip-9 后 475 KB），
  默认档 `catalog/defaults.json` 的哈希与出处直接抄 `pins/models.json`，不重打数字。
  `describe.json` 不进仓库，但 `catalog/describe.sha256` 进 —— 2026-10-20 重下上游整文件（28 s）
  并重导出过一次，字节与盘上那份完全相同，所以这颗哈希是仓库对"出厂目录长什么样"的唯一记载，
  `--live --check` 每次都比对，`slim()` 少抄一个字段就会红。
- `tools/ocr_build_surface.py` + `pins/ocr-build-surface.json`：量 OCR 那条**构建输入**钉没钉住。
  结论是"没钉"：上游 `.gitignore` 明写 `Cargo.lock`、pin 住的 commit 里也没有（404，正向对照 206），
  40 个外部包里只有 `ort` 是 `=` 精确钉的，其余 37 个是 caret。处置写在 ADR 与 requirements 第 12 条：
  锁由我们自己出（第一次真跑生成 → 提交成 `pins/ocr-Cargo.lock` → 之后 `cargo build --locked`）。
- `tools/file_index.py`：下载层的文件级解析 —— `repo + 文件名` → 期望 sha256 + 按实测吞吐排序的候选源。
  同时读 HF `tree.lfs.oid` 与 ModelScope `repo/files` 的 `Sha256`，状态分 `agree / single / conflict /
  pin_conflict / absent`，**只有两家都给且相等才允许下载前就信**；两家不等时一个答案都不给。
  车道 `file-index` 跑离线对照（五种状态 + 顺序）与实跑对照（正向=已钉的 pin，负向=故意造的假文件名）。
  `--queue catalog/machine-budget.json` 是**首启队列**：把三档默认权重行去重（跨档同一颗只排一次、
  按仓库缓存元数据），逐项对表（钉值 == 声明哈希 == 体积 == 有候选源），并按 `resolve_via` 分流 ——
  OCR 默认档那三颗来自 GitHub release，这里**如实标 `checked: false` 并写出路线**，
  不混进"查过了"；覆盖度地板按 live 确认条数算（8）。实测 15 项去重 / live 确认 11 / BAD 0，
  报告传成 artifact `first-run-queue` 给外壳实现当下载与校验输入。
  同一轮修掉一处"嘴上读、手上写死"：`recommend_subtitle.py` 早已从
  `download-ggml-model.sh` 读 `src=`/`pfx=`，可候选源与文件表仍传字面量 `ggerganov/whisper.cpp`；
  现在宿主由 `host_for_inputs()` 从脚本前缀取，行里带 `repo`、`check()` 比对它，
  并由一条 ast 测试扫全文（docstring 里可以解释，代码里的字符串常量不行）。
- `tools/recommend_models.py` + `catalog/recommendations.json`：**给用户的**「我这台机器装哪个」表。
  `catalog/defaults.json` 那 6 条是车道最小样本（对话那颗 415 MB，只为跑通），不是推荐。
  四条规则都进产物、UI 要说得出为什么推荐它：半档 < `recommended_ram_gb` ≤ 档位（上游 llmfit 的数）、
  窗口内按上游下载量、仓库**依次**试 `ggml-org → unsloth → mradermacher`、文件要整只且塞得进本档
  （排除 `-of-` 分片与 `dflash-*`/`mmproj` 这类附带产物，大小 < 档位 × 1 GiB）。
  实测定过这两条为什么要存在：只按 `≤ 档位` 排，8GB/16GB 档的前两行和 4GB 一模一样（都是 0.6B）；
  只取偏好第一名，8GB 档拿到 `ggml-org/Qwen3-8B-GGUF` 的 Q8_0 —— 8,709,518,656 字节，比机器总内存还大。
  车道 `recommendations`：先跑离线判据（含"用真元数据切片重算已提交那张表"），再 `--live --check` 盯上游漂移。
- `tools/recommend_subtitle.py` + `catalog/subtitle-tiers.json`：**字幕这一档**的同一件事，但换一个来源。
  对话那张靠 llmfit 的拟合值，而 **llmfit 目录里 ASR 只有 5 条**（OCR/生图 0 条）—— 字幕没有拟合值可用，
  只能回到 whisper.cpp 自己写的四张表：README 的 `Model | Disk | Mem`（峰值内存是上游给的，不是我拍的换算）、
  `models/README.md` 的 `Model | Disk | SHA` 与三条语义规则（`.en` 只有英文、`-q5_0` 是量化、
  `for-tests-` 前缀的文件是空壳）、`models/download-ggml-model.sh` 的白名单与下载源（域名从脚本读）、
  宿主仓库 `ggerganov/whisper.cpp` 的文件表。**Mem 只按上游两张表的 Disk 原文精确相等来接**，
  所以量化档与 `large-v3-turbo` 是 `fit=unknown`（只列进候选表、不进档位推荐 —— 上游没给数字就不假装知道）。
  默认档另加一条我们自己的余量规则：`Mem <= 档位 × 0.5`（4 GB 机器不该被推荐峰值 ~3.9 GB 的大档），
  纯算术的"塞得进"另列一栏。实测（b5454）：33 个可下载模型、22 个没有官方峰值内存；
  4GB → medium / 8GB、16GB → large-v3。整轴哈希只有 HF 一家给（ModelScope 返回 0 个文件，
  实测）→ 全部 `single`，且 `distributor_absent` 标出"实测最快那个源根本没有这个仓库"。
  语言这一轴也从源码量出来：`src/whisper.cpp:286` 的 `g_lang` 表实测 **100 种语言、id 从 0 连续到 99**
  （`en`=0、`zh`=1、`yue`=99 粤语）。UI 的下拉应当在运行时问 `whisper_lang_str()`，我们读这张表
  只是为了量出这一轴有多宽并盯住上游改动（表形状一变就该红，而不是下拉少了一项没人知道）。
  车道 `subtitle-tiers`：`--selftest`（含明知是错的必须认成错）→ 41 条单测（含"这张表算出的
  `ggml-tiny.bin` sha256 必须逐字节等于 `pins/models.json` 里字幕车道已钉那颗 pin"这条跨产物不变量）
  → `--fixtures --check` 离线重算 → `--live --check`。
- `tools/recommend_image.py` + `catalog/image-tiers.json`：**生图这一档的来源又不同**，
  所以它故意不写内存承诺。llmfit 里生图 0 条、sd.cpp 的 `docs/sd.md` 只给 safetensors 出处
  （GGUF 全在社区仓库），因此候选池由**上游搜索**按家族查询推导、按 `downloads` 排（查询串进产物，
  车道能发现多了一个家族），仓库顺序是 `leejet`（引擎作者自己也发 gguf）> 其余按下载量。
  三条实测出来的判据形状：**档位只认 `role=unet` 的主体权重**（`Green-Sky/flux.1-schnell-GGUF` 的清单里
  `t5xxl_q3_k` / `clip_l-q8_0` / `ae-f16` 都比主体小，只按大小排会把文本编码器推荐成"出图的那个模型"），
  配套件跟着每一行走（`companions`），而**默认行先按"装完这一颗能不能直接出图"排**
  （每行带 `completeness`：需要什么只从上游 `docs/sd.md` 的 txt2img 例子那五行读 —— `:13` SD1.5 只给
  `-m`、`:14` SDXL 要 `--vae`、`:15` sd3_medium 那颗自带 clips/t5xxl、`:16` flux 要 `--vae --clip_l --t5xxl`、
  `:17` sd3.5_large 要三串编码器；五条都进了出处表）。同仓库缺哪一样就叫 `incomplete` 并排后面，
  认不出家族是 `unknown_arch`，**不当成"不需要"**；**成人内容按上游自己的 `not-for-all-audiences` 标签挡**，
  不按仓库名猜（这次捕获里有 2 个仓库带这个标签，仍留在 `weights` 里但不进档位）；
  **许可缺失也算要显示**（43 个仓库里 7 个没 license 标签，`str(None)` 是 "None"，纯正则会把"没有许可"
  当成"许可没问题"）；`creativeml-openrail-m` / `openrail++` 也进标记（SD1.5 就是这条）。
  内存这一项写死为 `memory_claim: none`，依据是 `docs/performance.md` 说它是**运行时量出来的**
  （分段执行留 128 MiB 余量、设备底线 512 MiB、README `:157` 把人推到这份文档）—— 三行都进了出处表。
  实测：43 个候选仓库 / 338 行权重，**191 行两家分发方给同一个 sha256**（生图能到 4.5 级，
  与字幕整轴只有 single 形成对照）；`second-state` 那颗 Q4_0 与 `pins/models.json` 已钉的 pin 逐字节相同。
  还有一条反例钉在测试里：`leejet` 的 `q4_0` 与 `q4_k` **大小一模一样而哈希不同** —— 按大小认文件是假的。
- `tools/machine_budget.py` + `catalog/machine-budget.json`：**把已钉的五张表加成一个数** ——
  普通人问的不是"这颗选哪个"，而是「装齐四样要占多少盘、要等多久」，而这个数此前没有任何地方
  算过（`payload-plan` 只算引擎净负载，三张档位表只给单颗大小）。只加已钉产物里有的字节：
  引擎净负载 + 三张档位表**该档第一行**（默认档）+ OCR 默认档三条；全 14 种语言合计是**替代**
  默认档的另一个数，不许叠上去再加。
  实测（win-x64，按 lain42.top 那台 CN 出口量到的 ModelScope 4.2 MiB/s）：
  4GB **5.90 GiB / 约 24.0 分钟**、8GB **11.70 / 约 47.5**、16GB **20.82 / 约 84.6**；
  同一批字节按最慢的那个已量出口（hf-mirror 0.25 MiB/s）是 **6.72 / 13.31 / 23.68 小时**。
  两个数都得给用户，否则"约 24 分钟"就是拿最好的网速承诺最坏的体验。
  产物自己带口径：`ram_statement` 写死"这是磁盘与下载量、不是峰值内存"（四项能力共用一个常驻
  引擎、一次只跑一项），内存那一轴由各档位表自己的判据负责；两类"算不出来"也分开记：
  **没有产物**（macOS 的字幕：whisper 为 Apple 平台只发 xcframework 库、没有可执行文件）与  **落地大小未知**（某个包格式我们
  读不出成员表时才标 `needs_extract`）—— 混成一个就等于谎称那项功能在这台机器上没得用。
  这张表一算出来就把生图那条链改了三处，方向全是"少算"：
  ① 三档原本选中的默认行**仓库里都没有配套件**，而上游 `docs/sd.md:16` 的 flux 例子要
  `--vae --clip_l --t5xxl`、`:17` 的 sd3.5_large 要 `--clip_l --clip_g --t5xxl`
  （连同 `:13/:14/:15` 一共五条例子行都进了出处表）→ 档位表选默认行改成**先按"装完能不能直接
  出图"排**，16GB 档因此从 `unsloth/FLUX.1-schnell-GGUF`（只有 unet）换成
  `second-state/FLUX.1-schnell-GGUF`（带 clip_l / t5xxl / ae），16GB 的数字从 19.25 涨到
  **20.82 GiB（其中配套件 1.77 GiB）**；
  ② 上游例子只给 `-m` 的家族（SD1.5，`:13`）判成 `self_contained`，**不是**下限 ——
  "仓库里没有配套件"本身不是判据，得先问"这一族要不要"；认不出家族的写 `unknown_arch`，
  不许当成"不需要"；需要但仓库没有的才标 `image_lower_bound`，UI 那时才不许说"装完就能出图"；
  ③ 配套件的分组键必须是**上游那个旗标对应的东西**（`clip_l` / `clip_g` / `t5xxl` / `VAE`），
  不是 `role`：`clip_l` 与 `t5xxl` 的 role 都是 `text_encoder`，按 role 分组会把 t5xxl 整个丢掉
  而档位表这时候还写着 `complete`（这一版就错过一次）→ 现在两张表不一致会直接报问题。
  同一个键下面的十几个量化才是备选，取最小那颗，其余记 `group_alternates`。
  车道 `machine-budget` 三步全离线（它的输入就是那五张已提交的表加吞吐表）：`--selftest` →
  20 条单测 → `--check`（重数：`python -m unittest tests.test_machine_budget`）。
- `tools/launch_plan.py` + `pins/launch-plan.json`：**外壳该敲哪一行**，按钉住的 ref 读上游源码查。
  验收第 10 条（外壳持有唯一监听口、自己发 token、UI 永不直连引擎端口）能不能成立，取决于引擎
  自己支持什么开关，这是事实不是设计选择。量清楚的四件事：`llama-server` **有** `--api-key`
  （`common/arg.cpp:3513`）且 `/health` 明写不查钥匙（`server.cpp:263`）；`whisper-server` 只有
  `--host/--port`（`:251/252`），整份文件查不到 `--api-key`/`api_key`；`sd-server` 只有
  `--listen-ip/--listen-port`（`runtime.cpp:193/198`）；`oar` 根本没有服务形态，而且必须带子命令
  `oar ocr`，`--det/--rec/--dict` 是 `requires_all`（三颗一起给），默认档 `--size` 与它们
  `conflicts_with_all`（带了就被 clap 拒）。**所以那两项的隔离只能靠绑 127.0.0.1，不能指望引擎自带门禁。**
  生图那行的编码器从 `machine-budget` 真算进预算的配套件里读，并与 `image-tiers` 的默认行交叉核对 ——
  两张表对不上就报"下的与用的不是同一批文件"；模板里写的是文件名（`{flux1-schnell-Q5_0.gguf}`），
  所以"起的正是下的那一颗"可核对。车道 `launch-plan` 前三步离线（判据自测 / 46 条单测 / 与已提交那张
  比漂移），第四步才联网按 pin 现查 28 条 needle 与 8 条缺席声明；输出会自报"缓存 N / 现抓 M"，
  `--skip-provenance` 时报 0 ——「没查」不能长得像「查过了」。
  变异扫描留在仓库里：`tests/test_launch_plan.py::TestSourceMutants` 改坏源码 10 处，
  四处探针（离线对照 / 重算漂移 / 出处 id 闸门 / 缺席声明照扫）每条至少被抓一次，并带一条正向对照。
- `tools/payload_plan.py` + `pins/payload-plan.json`：装机负载计划（出包前必须先有的数字）。
  按"没 CUDA 的 x64 / Apple silicon"选默认产物，zip 用 Range 读中央目录、tar.gz 读成员表
  （都只读名字与大小，不解包、不执行）拿逐文件大小：引擎净负载 win 44.2 / linux 52.1 /
  macOS 46.5 MiB，**落地** win 115.6 / linux 136.1 / macOS 124.9 MiB（都不含权重）；
  `whisper-bin-x64.zip` 41 条里收 31、排除 10 个 `test-*.exe`，Linux 那个 tar 包同样数出 10 个测试程序。
  tar.gz 那三行是**取到临时文件后整包算过 sha256 并等于上游 release digest** 才算读到的
  （体积也要等于 release 声明，否则当截断）；zip 那几行只读了尾部中央目录，本来就没有整包字节，
  所以两种行的证据强度不同，产物里只有前者带 `verified_bytes`。
  **whisper 上游根本没有 macOS 的可执行产物** —— b5454 唯一的 Apple 附件是
  `whisper-b5454-xcframework.zip`（200 条：framework/Headers + 各 slice 的库与 dSYM，
  没有 `whisper-cli`/`whisper-server`），所以字幕在 Apple silicon 上要么 CI 自己编、要么不做。
- `tools/pack_engine.py` + `.github/workflows/package.yml`：**出包这一步**。按 OS 矩阵取装机计划选中的产物 →
  整包 sha256 必须等于上游 digest（对不上就停，不解包）→ stdlib 解包并把顶层目录与 `Release/` 拉平
  （`$ORIGIN` 找运行库的前提）→ 逐文件哈希与体积写 `payload-manifest.json` 上传为 artifact。
  同名不同目录的成员不许静默覆盖；主二进制缺席、清单里残留被排除的东西、目录里留着压缩包都会红。
  10 条离线对照（合成 zip 与 tar.gz，含"digest 不符"与"下载失败"）。
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
