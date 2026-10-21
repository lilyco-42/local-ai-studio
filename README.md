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
- `tools/yaml_gate.py`：解析六条 workflow，核对 needs 链、step 输出引用、heredoc 闭合与**每段 PY 真编译**，
  以及车道里写死的 target 名确实存在于 manifest（已用假 target 验证它会红）。
  `needs.X.outputs.Y` 也按 **job 自己的文本**查三件事：X 这个 job 存在、本 job 真 `needs` 它、
  X 真导出 Y —— 引用没导出的输出在 Actions 里拿到的是空字符串而不是报错，而"扫整份文件"那一版
  会让任何带 `needs:` 的 job 为别人的引用背锅（见 `docs/requirements.md` 验收第 5 条）。
  车道分段与预跑的**计数**由 `tests/test_lane_replay_report.py` 钉住：六条 workflow 共 18 段 `<<'PY'`
  （`installer.yml` 与 `upstream-follow.yml` 没有 heredoc）、
  其中 13 段含判据（认 `check(`/`SystemExit`/`assert`/`sys.exit(`）、本地预跑引用 8 个
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
  全不过就报 `SKIP（unreliable/missing + 原因）` 而不是装成查过。2026-10-20 实测覆盖 108 段、0 问。
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
  变成能查的东西。82 条声明各记（2026-10-20 按 pin ref 实跑过一次，0 问题；`--cache .cache/refs --fill` 抓一次原文之后可离线复算） `repo/ref/path/line/expect`（还多一种 `absent`：连「这份文件里没有 X」也要查），逐条取回上游**核对那一行仍然含那个子串**
  （只看文件在不在没用：默认值改了文件照样在）。
  2026-10-20 加了一条硬判据：**相邻行也含同一子串 → 这条不会红**（行号漂一格就撞上隔壁那行，
  `/health` 与 `/v1/health` 两行注释一模一样，实测"行号 +1"复算时它是唯一没红的）。
  相隔很远的重复不拦，它不破坏灵敏度。总数由单测钉成字面量，所以"82"这个数改表时必须一起改（这一轮加 4 条时它就叫了一嗓子）。
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
- `tools/download_plan.py` + `pins/download-plan.json`：把"装哪颗、从哪下"接起来 —— 从装机预算的行
  出发，按 `repo + 文件 + sha256` 精确连到档位表里的候选源，生成 14 项下载计划（8 项默认安装）：
  源按国内实测吞吐降序（ModelScope 4.2 MiB/s → hf-mirror 0.25 → GitHub release 0.014 → HF 直连 0 兜底），
  whisper 的 ModelScope 源按 `distributor_absent` 删除，OCR 三颗各有 ModelScope（整包下载复核 sha256）
  与 GitHub release 两条源，默认安装至少两源，`(合计)` 行点名为聚合值、不下载。
  `--check` 重算逐键比对，`--selftest` 用十类变异证明会红；
  `--probe` 现探计划里的 37 个源 URL（Range 只读 1 字节 + `Content-Range` 总长对预算，
  2026-10-20T15:09:58Z 实测 36/37 可达、36/36 总长与预算相等，唯一失败的是 HF 直连
  `ggml-large-v1.bin` 的 SSL EOF —— 它本来就排在最后；可达 ≠ 吞吐，排序仍按实测速率）。
  `shell/core/src/download.rs` 是这条计划的消费者：系统 curl 传输（不引 TLS crate）、Range 续传、
  低速/断流换源、收满 sha256 才从 `blobs/<sha>.part` 落成 `blobs/<sha>` 并硬链接到文件名；
  这一组单测已随 `shell.yml` 的 `core` 在 3 个操作系统上真跑过（2026-10-21，各 70 条全过）。
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
  车道 `subtitle-tiers`：`--selftest`（含明知是错的必须认成错）→ 44 条单测（含"这张表算出的
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
  实测（win-x64）——**首装默认**（不含生图）4GB **3.80 GiB / 约 159.6 分钟**、
  8GB **7.61 / 约 268.4**、16GB **11.31 / 约 283.5**；**装齐四样**（生图按约束第 5 条要显式触发）
  4GB **5.79 GiB / 约 167.7 分钟**、8GB **11.59 / 约 284.6**、16GB **20.70 / 约 321.6**。
  等待时间逐颗按它自己的源相加：引擎只发 GitHub release（0.014 MiB/s）、whisper 只有 hf-mirror
  （0.25，它的 ModelScope 地址是上游 404）、对话/生图/OCR 有 ModelScope（4.2）；每颗再取可用源里
  最慢的一档给上界 —— 首装 **5.27 / 9.60 / 13.81 小时**、装齐 **7.53 / 14.12 / 24.49 小时**。
  旧版把全部字节按 4.2 除（漏了引擎与 whisper 的真实出口），写成 15.9 / 31.4 / 46.4 分钟，
  乐观约 10 倍；全 14 语言合计不是一个文件，`not_downloaded_bytes` 单列，不进字节也不进时间。
  这两组此前被并成一组报出去，而生图那部分（4GB 档就有 2,033.8 MiB）其实不该算进"点完安装要等的"——
  现在 `default_install` 那张政策表在产物里，`first_run_*` 与 `download_*` 各一套，两个数都得给用户，
  否则"约 24 分钟"就是拿最好的网速承诺最坏的体验，还是替用户决定了他没点的那件事。
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
  车道 `machine-budget` 三步全离线（输入是那五张已提交的表 + `pins/ocr.json` 的源地址 + 吞吐表）：
  `--selftest` → 21 条单测 → `--check`（重数：`python -m unittest tests.test_machine_budget`）。
- `tools/launch_plan.py` + `pins/launch-plan.json`：**外壳该敲哪一行**，按钉住的 ref 读上游源码查。
  验收第 10 条（外壳持有唯一监听口、自己发 token、UI 永不直连引擎端口）能不能成立，取决于引擎
  自己支持什么开关，这是事实不是设计选择。量清楚的四件事：`llama-server` **有** `--api-key`
  （`common/arg.cpp:3533`）且 `/health` 明写不查钥匙（`server.cpp:280`），CORS 默认 `*`
  （`common.h:656`）但可以 `--cors-origins localhost` 只反射本机来源（`server-http.cpp:75`）；
  `whisper-server` 只有
  `--host/--port`（`:251/252`），整份文件查不到 `--api-key`/`api_key`；`sd-server` 只有
  `--listen-ip/--listen-port`（`runtime.cpp:193/198`）；`oar` 根本没有服务形态，而且必须带子命令
  `oar ocr`，`--det/--rec/--dict` 是 `requires_all`（三颗一起给），默认档 `--size` 与它们
  `conflicts_with_all`（带了就被 clap 拒）。**所以那两项的隔离只能靠绑 127.0.0.1，不能指望引擎自带门禁。**
  生图那行的编码器从 `machine-budget` 真算进预算的配套件里读，并与 `image-tiers` 的默认行交叉核对 ——
  两张表对不上就报"下的与用的不是同一批文件"；模板里写的是文件名（`{flux1-schnell-Q5_0.gguf}`），
  对话与字幕也各自点名这一档那唯一一颗权重（`-m {Qwen3-14B-Q4_K_M.gguf}`），一共 11 颗。
  **点名之后还要与另一个生产者对表**：`file-index` 那份首启队列报告（`--queue-report`）里少一颗、
  `status` 不是 agree、或走的是这条车道没做 live 比对的 `github_release_digest` 路线，都不许起进程 ——
  "没查"与"查了没对上"是两句话，报告里分开说。没有报告时产物如实标 `budget-only`（argv 与预算队列
  同源，自证不算已验）。二进制名字与出包判据共用一张表（`pack_engine.SERVER_BINARY` == `launch_plan.binary_names`：
  负载里只有 `whisper-cli` 也算没装，外壳起不来）。
  每项能力还带 `install_stage`：读的是 `machine-budget` 的 `default_install` 政策，于是
  对话/字幕/文字识别 = `first_run`、生图 = `on_demand` —— 外壳据此决定"点这条命令之前要不要
  先问用户下载"，同一族里半装半不装是问题不是取舍。车道 `launch-plan` 前三步离线（判据自测 / 66 条单测 /
  与已提交那张比漂移），第四步联网按 pin 现查 28 条 needle 与 8 条缺席声明，第五、六步取 `file-index`
  的产物并按它对表（那两步带 `if: always()`：上游 job 挂掉时结论不能变成 skipped，而报告文件不在时
  工具自己报「不存在」再退出 1）；
  输出会自报"缓存 N / 现抓 M"，`--skip-provenance` 时报 0 ——「没查」不能长得像「查过了」。
  变异扫描留在仓库里：`tests/test_launch_plan.py::TestSourceMutants` 改坏源码 16 处，
  六条探针（离线对照 / 重算漂移 / 出处 id 闸门 / 缺席声明照扫 / 跨生产者核对 / 核对没有输入就红）
  每条至少被抓一次，并带一条正向对照。
- `tools/lane_argv.py`：**表说的那一行 = 车道真敲的那一行**。上面那张表是按上游源码核对的，可四条
  执行车道的命令是手写的，两边从没对过；这个工具用 pyyaml 取 `engine-live.yml` 每个步骤的 `run`，
  认出每项能力真起进程的那几步，再按表查四件事：二进制名对不对（`chmod/ldd` 那种"提到过"不算执行）、
  `--host`/`--listen-ip` 有没有逐字给 `127.0.0.1`（`$ADDR` 与"靠上游默认"都不算）、表里说上游查不到的
  旗标有没有混进来、`omit`/子命令对不对。只吃 shell 那一层（注释与 heredoc 正文先滤掉）。
  **验收第 10 条第①句在车道里是两截**：llama 那行必须真带 `--api-key`（变量必须在同一步被赋成
  非空，`"{token}"` 原样传进去算不合格）与 `--cors-origins localhost`；同一段还必须有
  **无钥匙访问 `/props` 得 401** 的断言和**两个不同 Origin**（本机被反射 / 非本机拿不到 ACAO）
  的对照 —— 旗标传对了不等于门禁生效。
  实测 4 项能力各一段、问题 0 条；15 种 workflow 文本变异全被报（含丢钥匙、占位符、变量未赋值、
  丢 CORS、401 被改成 200、只剩一个来源），正向对照是"0 问题必须建立在
  4 段都认出来之上"（重数：`python3 tools/lane_argv.py`，已挂在 `ci.yml` 静态门禁）。
- `tools/sideload_lookup.py` + `pins/sideload-index.json`：**用户从别处弄来的权重，按内容认领**。
  六张已钉表摊平成 `sha256 → 身份`（实测 354 串哈希 / 420 行身份 / 30 个同名不同哈希的文件名），
  结论四种：`known_by_hash`（认领，带能力与证据等级）、`name_matches_hash_differs`（名字钉过但字节
  不是那颗 → **拒绝**）、`unknown`（只给带 `claim:false` 的形态提示）、`hash_known_size_differs`
  （哈希对、体积不对 → 先查 pin）。负向对照用真文件：`ppocrv5_latin_dict.txt` 在上游 v0.1.0 是
  1,634 B、v0.3.0 是 2,616 B —— 同名不同内容不是假想。文件头解读不在这一层（归 CI/外壳里的
  gguf-parser，本机禁止编译与运行），产物 `rule` 里写明了这条边界。
  6 条源码变异体 + 六条探针 + 三个真夹具；挂在 `ci.yml` 静态门禁
  （重数：`python3 tools/sideload_lookup.py --selftest`）。
- `tools/backends.py` + `pins/backend-matrix.json`：**分层与"runtime 可替换"这两句话的数据面**。
  按 `payload-plan` 钉的三个 ref 现取 release 的 asset 表（`name / size / digest`），分成
  引擎 × OS × 后端：实测 **57 个资产 = 47 个 runtime + 10 个不是 runtime**（7 个 `cudart-` 伴生包、
  2 个 xcframework、1 个 webUI），后端 10 种（cpu / cuda / vulkan / rocm / sycl / openvino /
  opencl-adreno / qnn-snapdragon / blas / `unknown_from_name`）。**一格是一列不是一行**：
  llama 在 win-x64 同时留着 CUDA **12.4 与 13.4** 两颗、whisper 在 win-x64 留着 11.8.0 与 12.4.0、
  ubuntu 的 sycl 留着 fp16 与 fp32 —— 上一版一格只装得下字典序最后那颗，而"36 个产物"照样数得出 36，被自己的测试抓回来了（少掉的那几颗恰恰是驱动较旧那台机器要的）。
  体积差是真的：
  sd 在 win-x64 是 CPU **16.9 MiB** 对 CUDA **327.7 MiB**，llama 在 linux-x64 是 CPU 17.1 对
  vulkan 29.2 —— 集显笔记本那条路有产物可换，不再是一句推测。
  三处**不许猜**的边界记在 `gaps` 里：macOS 的资产名不标后端（只能写 `unknown_from_name`，
  不断言 Metal）；whisper 的 `bin-x64` / `blas-bin-x64` 名字里不写平台，于是按名字挑后端
  对它做不到（`payload-plan` 那侧已标 `inferred_platform=true`，这里原样记账）；
  oar-ocr 没有预编译产物，它的加速轴是 Cargo feature，不在这张表里。
  缺口那一格点的是**颗**、不是点名次数：行侧与 payload-plan 都会点名同一颗，第一版 2 颗写成
  3 颗（格子数与总行数那几格都看不出来），现在按 `(引擎, 资产)` 去重。
  认不出的词（上游加一档 hip/mtl）一律报 `unknown_token` 并红，**不许静默归成 cpu**；
  7 条源码变异体 × 10 条探针 + 真实夹具字节校验；离线那层在 `ci.yml` 静态门禁，
  车道 `backends` 的现取步骤还回现取一次再对表（重数：`python3 tools/backends.py --selftest`、
  `python3 tools/backends.py --check`）。
- `tools/backend_choice.py` + `pins/backend-choice.json`：**"换哪一档、要多下多少、要多占多少盘"也是表**。
  矩阵说有哪些档，这张表说外壳该下哪一颗：默认档从 `payload-plan` 逐行搬（不许自己挑，有探针钉着）、
  体积差每次重算、落地体积从 `runtime-parity` 取、CUDA 档必须连 `cudart-` 伴生包一起算 —— 实测 llama 在 win-x64 换 CUDA 12.4 是
  **+236.3 MiB 本体 + 373.3 MiB 伴生包**（伴生包比本体还大），而换 **Vulkan 只 +12.0 MiB 且没有伴生包**：
  "有独显的普通笔记本最便宜的加速路是 Vulkan"这句话，此前没有任何产物撑得住。
  10 档 CUDA 对 7 份伴生包，剩下 whisper 那 3 档**没有**伴生包 → 写进 `cuda_rows_without_companion`
  并带 `because`，"没有"不是"不需要"。`cpu_fallback` 四态由格子算出来，不许手写：whisper 在 win-x64
  那颗 CPU 包名字里不写平台（`only_unnamed_asset`，而且它不在按名字分出的可换清单里）、
  macOS 两个引擎是 `name_does_not_say`（不断言 Metal）。sd 的伴生包写 `cu12` 而 runtime 写 `cuda12`，
  拼法不同也必须配上同一颗。**这张表不说"这台机器的驱动能吃到哪一档"** —— 那要运行时问 `nvidia-smi`，
  属外壳（#25）。10 条源码变异体 × 13 条探针，外加一条链式核对（release 夹具 → 矩阵 → 换档表）；
  **下载量小不等于占盘小**：llama 在 win-x64 的默认档落 47.9 MiB（整包全量；出包只收
  server 后默认档 42.8，可选档的收窄差值未逐档重算），换 Vulkan 87.5（+39.6）、
  换 CUDA 12.4 598.3（+550.4）、换 ROCm 1148.7（+1100.8 MiB）—— 45 个可选档里 29 个有数、
  16 个（没读整包的 tar）写 `null` + 说明，**不许写成 0**。
  12 条源码变异体 × 16 条探针（新增"落地体积未知被写成 0"与"默认档缺落地体积不报"）；
  离线那层在 `ci.yml` 静态门禁，车道 `backends` 最后一步拿现取那份矩阵重算
  （重数：`python3 tools/backend_choice.py --selftest`、`--check`）。
- `tools/runtime_parity.py` + `pins/runtime-parity.json`：**换后端那句的前提也得有表**。
  "换个 asset 就换个后端"要求那一档的包里**有外壳要 exec 的那个文件**（约束第 3 条 × 第 4 条）。
  证法只用只读元数据：zip 用 `tools/zip_peek.py` 的 HTTP Range 读**中央目录**（每颗约 1 MiB 尾部，
  不解压、不运行），`.tar.gz` 取整包读成员表但**先要求字节数与上游 digest 都对上**。
  实测 **47 个 runtime 资产 / 30 颗读到成员表 / 30 颗都带着 server / 0 档缺 server**：llama 在 Windows 的 11 颗（cpu / cuda ×2 版本 / vulkan / rocm / sycl / openvino /
  opencl-adreno…）全都 `llama-server.exe` **与 `llama-server-impl.dll` 成对**，whisper 的 8 颗带
  `whisper-server.exe`，sd 的 8 颗带 `sd-server`。**三条边界不猜**：`.tar.gz` 只读真装的那 3 颗默认档
  （字节数与整包 sha256 都按 payload-plan 复核过才认），剩下 16 颗可选档不读 → 标 `not_read`
  并写明"该由出包车道证"，**绝不写进 server_gaps**（未知冒充缺席=谎称知道）；8 颗默认档全部证过；
  `whisper-blas-bin-x64.zip` 名字不写平台而
  plan 又没装它 → 停在未知，而 whisper 那颗 CPU 默认包的 OS 是**从 payload-plan 取的、不是猜的**
  （把它改成"猜 win-x64"的变异体被探针抓住）。`server_sibling_lib` 只对 llama 求值，whisper/sd 写
  `null` 不写 `false`。可执行名**只有一个来源**（`launch_plan.SERVER_BASE`），源码里出现字面量由
  一条 ast 扫描拒绝。同一份成员表还量出**落地体积**（各成员解压后大小之和），与 `payload-plan` 独立量过的
  `uncompressed_mib` 逐颗对表（8 颗默认档全在 0.2 MiB 内），并要求「落地 ≥ 压缩后」。
  11 条源码变异体 × 15 条探针 + 捕获夹具字节复核（`tests/fixtures/parity-captures.json`，
  31 颗 / 46,530 B）。顺带落下一条独立证据：**sd 在 macOS 有 `sd-server` + 1 个 `.dylib`**，
  而 whisper 在 macOS 只有 xcframework（#29）—— 生图在这台机器上有路，字幕没有。
  重数：`python3 tools/runtime_parity.py --selftest`、`--check`。
- `tools/runtime_versions.py` + `pins/runtime-versions.json`：**能升到哪、能退回哪、每颗能不能校验也是表**。
  矩阵只覆盖钉住的那一个 ref；外壳要给"更新 / 回退 runtime"，就得知道相邻版本发不发同样的包。
  窗口按"翻到钉住那颗为止"取（100/页 × 至多 5 页），不是写死 12 —— llama 一天发 6 个 release，
  固定窗口会装不下钉住那颗。**本次把 llama 从 b11727 重钉到 b11798 时（2026-10-21T17:44Z 现取），
  它是窗口里最新的带产物版本（0 个更新；上游随时会再发新版，这张表只报不跟）**；**sd 还差 1 个（master-975 → master-982），覆盖面没变**；**whisper 的 `releases/latest` 是 v1.9.5、0 个产物**，真正能装的 b5454 正是我们钉的那颗
  （9 个只有源码的标签单列，按 latest 取 runtime 会"成功但装不上"）；
  **回退边界是 digest 不是年龄**：v1.7.6 之前 13 个 release 上游没发 digest，可校验候选只有 16 个。
  窗口内 75 个 release / 66 个带产物 / 1021 行 runtime 资产 / 7 个附件 / 认不出的词 0；
  钉住那颗的 47 颗产物名与矩阵逐颗相等（同一个 release，两张表不许漂）。
  18 条源码变异体 × 24 条探针 + 窗口元数据夹具（`tests/fixtures/release-window.json`，259,775 B），
  漂移只报不跟。现取车道用 `--live` 口径：只核对钉住那颗还在、产物没被重打包；上游又发新版
  不算红（实测抓完半小时后 llama 又发了 5 个版本 —— 按"有新版本"红会让这条道永远红）。
  2026-10-21 车道 `backends` 末步原样跑了这条现取：三个仓库窗口内 101 个 release（带产物 92 个）、
  问题 0 条；同一条道还抓到一次"上游正在上传"的假红 —— b11797 的 release 16:01:17Z 发布、
  win 产物 16:03:57Z 才出现，16:03:24Z 那次捕获把它读成"升级会丢 win-x64"（run 39631173958），
  16:20Z 再读问题 0 条。这就是 `--live` 只把"升级路径覆盖变窄"当提示、只把钉住那颗的完整性当红的原因。
  重数：`python3 tools/runtime_versions.py --selftest`、`--check`。
- `tools/follow_upstream.py` + `.github/workflows/upstream-follow.yml`：**跟随上游 llama.cpp，
  但新版只报不跟**。上游一天发好几个包，把"有新版本"当红，红就不再是信号；这条每天 03:17 UTC
  现取窗口，只有钉住那颗被撤、被重打包、或与 `backend-matrix` 不是同一颗才红（退出码 1）。
  新版本身退出码 0，候选缺我们真装的 target / 有认不出的后端词 / 没 digest 只报 `blocked`
  （`--strict` 才红）。升级是显式动作：`--update --tag bNNNNN` 用 `payload_plan.rows_for` 的
  真路径量出三行（zip 读中央目录、tar 读成员表，不执行），`--apply` 只替换该引擎的行；十份派生
  文件（3 份捕获 + 7 张表）只要还带旧 tag 就退出码 2 并逐张打印重出命令，半套重钉不许说完成。
  8 类变异自检 + `tests/test_follow_upstream.py` 20 条；2026-10-21T17:44Z 重钉后本地只读实跑读到
  b11798（钉 b11798、落后 0、已最新、rc=0）。**证据边界**：这条车道的 `schedule` 至今
  没有被真正触发过（这台账号的 cron 实测晚 1–3 小时且会整槽丢）；同一套现取逻辑已在 `backends`
  车道末步跑过（101 个 release / 92 个带产物 / 0 问题），但"定时会不会落地"仍未知。
- `tools/shell_contract.py` + `pins/shell-contract.json`：**把外壳必须遵守的合同钉成表**。
  只从已提交的五张表推导（launch-plan / backend-choice / runtime-versions / payload-plan /
  ocr-build-surface），不读外壳源码：入口（外壳 0 个监听口，UI 只走 Tauri IPC）、
  鉴权（只有 llama 有 `--api-key`，token 每次现生成、只存内存）、
  **生命周期**（没钥匙的 whisper / sd 不许常驻：`on_demand` + 空闲 300s 即停；OCR 一次任务一个进程）、
  spawn（exec 前逐文件对 `payload-manifest.json` 的 sha256）、
  runtime 目录（`<data>/runtime/<os>/<engine>/<version>/<asset-stem>/`）。
  三个 server 能力 × 各自 OS 格 = 8 颗默认档，逐颗与 payload-plan 的行、钉住 release 的产物名
  与 digest 三方对表。**UI 存在就真扫**：产物记 `ui_checked=true`、`ui_dir="shell/ui"`，
  fetch/XHR/WS/硬编码回环地址一律拒绝，且 UI 调用的每条 IPC 都要挂在 `generate_handler!` 上；
  OCR 的二进制还没进出包清单，这条缺口以 `ocr-manifest` 留在产物里。
  14 条源码变异体 × 24 条探针、19 条单测。
  重数：`python3 tools/shell_contract.py --selftest`、`--check`。
- `shell/core`（Rust，不依赖 Tauri）：把上面那张合同写成可测逻辑 —— 生产代码只做一次
  `127.0.0.1:0` 探测并立刻 drop（自己不监听）、token 长度读合同且只进内存、没钥匙的引擎
  `on_demand` + 300 s 回收、`spawn` 前必须过 `payload-manifest.json` 的逐文件 sha256、
  argv 全由合同/启动表渲染；HTTP 客户端自己实现（chunked/trailers/512 MiB 上限）并带
  chat / transcribe / image / ocr 四个调用。`tools/shell_source.py` 扫生产源码：引擎名、
  引擎旗标、引擎端口一律不许出现，`Command::new` 只有一处、`TcpListener::bind` 只有一处。
  `shell/src-tauri`（Tauri 2）只做装配：`AppState` + 8 条 IPC 命令全部 `spawn_blocking`
  调 core，`shell/ui` 只有 IPC、没有网络 API、中英两套文案；`icons/icon.ico` 是 Windows
  资源段要的那一颗。
  **这些代码现在真编过、真跑过了**：2026-10-21 `shell.yml` 的 `core` 在 3 个操作系统上各 70 条
  单测全过，`app` 在 3 个操作系统上真的编出 Tauri 层并跑 3 条单测（Ubuntu 首编 1m30s，先装 WebKitGTK）；
  两个 crate 的首轮 `Cargo.lock` 已作为 artifact 产出（`shell-core-Cargo.lock`、`shell-app-Cargo.lock`），
  还没提交进仓库。Windows 第一次跑红在假服务器的 RST 竞态，修法是读到完整请求（头 + Content-Length
  正文）再回包 —— 只有真跑才会发现。**窗口里的 JS 与 IPC 也真驱动过了**：2026-10-21 起
  `app (ubuntu-latest)` 先起一次窗口（run 39658522639：标题 `Local AI Studio`、980×720），
  再用 `tauri-driver` 2.1.0（`cargo install --locked --version 2.1.0`）+
  `webkit2gtk-driver` 2.52.6 的 `/usr/bin/WebKitWebDriver` 连上窗口执行页面 JS
  （run 39663865685，commit `bb68f29`）：`invoke('status')` 回 4 行（对话 resident / 字幕 on_demand /
  文字识别 one_shot / 生图 on_demand，Linux 四格 `available=true`），`invoke('data_root')` 回
  `/home/runner/.local/share/top.lilyco42.local-ai-studio`；页面标题是 `Local AI Studio`、
  “找不到 Tauri IPC”提示条保持隐藏、状态条渲染成 `对话 · 未启动`，`#data-root` 与返回值逐字相同；
  同一步还把能力名换成假的做已知坏对照，必须被拒。**边界**：只有 Ubuntu 这一档，没有点“启动引擎”、
  没有联网 —— 证的是 IPC 桥与首屏渲染，不是四条能力的端到端链路；macOS/Windows 的 app job 跳过这两步。
- `tools/action_pins.py` + `pins/actions.json`：**出包那只手自己也要钉住**。开工时三个
  workflow 里 4 个 `uses:` 全是浮动大版本 tag（`checkout@v4`、`setup-python@v5`、
  `upload-artifact@v4`、`download-artifact@v4`）—— 仓库对引擎/权重/工具带都定了
  "release + sha256"，唯独真正跑包的东西跟着别人随时会移动的 tag 走。
  现在六条 workflow / 5 个 action / **56 处 uses** 全部是 40 位 commit SHA，尾注留着记它来自哪个
  tag（重数：`python3 tools/action_pins.py --check`；上一版表漏了 `shell.yml` 的 4 处，
  是这轮把它补进扫描表时才数出来的）：`checkout 11d5960a…`(v4.4.0)、`setup-python a26af69b…`(v5.6.0)、
  `upload-artifact ea165f8d…`(v4.6.2)、`download-artifact d3f86a10…`(v4.3.0)，都是 MIT、`node20`。
  判据不只是"形状对不对"：每次重新解析尾注那个 tag，**漂了就红但绝不自动跟**（重钉必须是显式动作）；
  还取那份 `action.yml` 复核 `runs.using` 与 `inputs` 键集合 —— 上游换 runtime 或加输入会点名。
  `tauri-apps/tauri-action` 现在由 `installer.yml` 使用：`v1.0.0` → `1deb371b…`、
  `node24`、23 个输入、入口是提交进仓库的 `dist/index.js`（所以钉 commit 等于钉住那份 bundle）；
  它还有个容易看错的形状：**release 叫 `action-v1.0.0` 而 tag 叫 `v1.0.0`**，两个名字都进表，
  谁把 `releases/latest` 当 tag 用就会红。形状用 pyyaml 复核而不是自写正则 ——
  第一版的缩进正则把 checkout 的 21 个输入读成 8 个，那张漂移基线就成了假的（现在有单测对表）。
  匿名 GitHub API 只有 60 次/小时：撞限流时报"被限流/没权限"，**不会伪装成"上游没这个提交"**。
  本机重跑：`GITHUB_TOKEN=$(gh auth token) python3 tools/action_pins.py --check`。
- `tools/install_deps.py` + `pins/install-deps.json`：**安装包车道第一步该 apt 装什么**，从 Tauri
  官方前提页读出来。只认 `<TabItem label="Debian">` 那一族里**包管理器那条安装命令**的续行
  （`prerequisites.mdx:33-41`，九颗各带上游行号）；其余 tab 里的 `export JAVA_HOME=…` 与散文不算清单
  （第一版就是把它们读了进来，产物里冒出 10 个假家族），`apt update` / `pacman -Syu` / `zypper up`
  这类刷新行也不算。再逐字去 ubuntu-24.04 镜像 readme 的包表查在场：实测
  **`curl` / `wget` / `file` / `libssl-dev` 在表里**（版本一起带出，如 `libssl-dev 3.0.13-0ubuntu3.16`），
  **`libwebkit2gtk-4.1-dev` / `build-essential` / `libxdo-dev` / `libayatana-appindicator3-dev` /
  `librsvg2-dev` 没提到**。判语只写 `not_listed` 不写"没装" —— **readme 不是 `dpkg -l`**，
  这句留在产物 `verdict_legend` 里；装没装要由车道打印 `pkg-config --modversion webkit2gtk-4.1` 才算。
  两根哨兵：包管理器不是 apt 就报、`libwebkit2gtk-4.1-dev` 不在表里就报（上游换 4.2 不许默默照旧）。
  顺带删掉一处查不到出处的旧说法：那份页里 `gstreamer` 命中 0 次。
  跑在 `citation-drift` 车道里；`tests/test_install_deps.py` 36 条，含 8 个把工具源码改坏的变异体
  与"不改坏时五条探针全在位"的正向对照。
- `tools/payload_plan.py` + `pins/payload-plan.json`：装机负载计划（出包前必须先有的数字）。
  按"没 CUDA 的 x64 / Apple silicon"选默认产物，zip 用 Range 读中央目录、tar.gz 读成员表
  （都只读名字与大小，不解包、不执行）拿逐文件大小：引擎净负载 win 44.2 / linux 52.1 /
  macOS 46.5 MiB，**全量落地** win 115.6 / linux 136.1 / macOS 124.9 MiB（都不含权重；
  出包只收 server 后 win 实为 104.7 MiB，见下条）；
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
  **一个引擎只放一个可执行文件**：只收 `LAUNCH_PLAN` 点名的 server，CLI/bench/quantize 等
  工具及其 `-impl` 库（含 Linux 的 `libx-impl.so`）逐条点名排除；`.dll/.so/.dylib` 闭包与
  `LICENSE`、`.metal` 这类非可执行数据原样保留。macOS 的 `libx.0.dylib` 是指向
  `libx.0.7.0.dylib` 的符号链接，链接本身也要落盘（否则 `@rpath` 那一环断了）。
  同名不同目录的成员不许静默覆盖；主二进制缺席、清单里残留被排除的东西、目录里留着压缩包都会红。
  收窄后 win 默认档 112 → 57 个文件、115.6 → **104.7 MiB**（中央目录 uncompressed size 求和，
  未下载整包；llama 51→24 / 42.8，whisper 41→16 / 16.9，sd 20→17 / 45.0）。
  `--selfcheck` 18 条、`tests/test_pack_engine.py` 20 条（合成 zip/tar.gz，含 digest 不符、
  下载失败、链路链接与"塞回 CLI 必须红"）。
- `tools/stage_payload.py` + `package.yml` 里的摆位步：把上面那份**扁平**的 `dist/<os>/<engine>/`
  摆成外壳真会去读的 `runtime/<os>/<engine>/<version>/<asset-stem>/`，并在数据根写根清单
  （外壳的 `verify_payload` 要的就是这个）。目标只从 `pins/shell-contract.json` 的能力行派生，
  脚本里没有引擎/版本字面量；OS 不符、引擎不在合同里、asset 与合同默认档不同都直接停；
  逐文件按清单的 bytes + sha256 复制并复核；目标目录里多一个清单外文件也停（不覆盖、不删除）。
  根清单**最后写**（临时文件 + `os.replace`），中途任何失败都不会留下"看似齐全"的根清单；
  `--check` 再解析两份清单逐键相等并重算每个文件。包里没有的能力（OCR、macOS 字幕）记进
  `not_in_manifest` 点名，不当作失败。13 条合成自测 + 10 条离线对照。2026-10-21 出包车道
  （run 39632180385）在三档 OS 上真跑了这一步：摆位后 `--check` 通过，`not_in_manifest` 点名
  OCR 与 macOS 字幕。
- `tools/payload_intake.py` + `package.yml` 里的交付清单步：读摆好的数据根，为每个文件生成
  `payload/<os>/<数据根相对路径>` 的对象键、落点、sha256 与字节数；根清单单独一行标
  `land: last`。它重新复核每个文件，拒绝多余/缺失、键前缀越界与两个引擎同目标；输出不含
  绝对路径，两个数据根得到同一份字节。`--check` 重算并逐键相等。只生成清单，**不上传、
  不下载、不执行**；`delivery-manifest.json` 随 `engine-payload-<os>` artifact 上传。
  21 条离线对照（含真实合同落点、6 条源码变异体，以及删掉原树后只凭清单还原的
  消费端重放；坏对象或越界落点不许落根清单），2026-10-21 同一轮出包车道在三档 OS 上都生成了
  `delivery-manifest.json`（run 39632180385 第 9 步）。
- `shell/core/src/delivery.rs` + `shell/core/src/payload_fetch.rs` + `examples/install_from_dir.rs`：
  外壳侧的清单消费者。逐项校验 schema/os/键前缀/键与落点绑定/哈希/总量，按调用方给的 `fetch`
  取字节，长度与 sha256 都对了才用同目录临时文件 + 重命名落盘，根清单最后落，已正确的文件跳过；
  25 条离线测试（清单消费 14 + 取数 11）。`payload_fetch` 负责真正取数：多个基址按顺序换源、按对象键拼 URL
  （非安全字节转义）、断流后用 `.part` 的长度发 Range 续传、哈希不符丢掉重下；基址只收
  https，`http://127.0.0.1` 只给重放用（公网 http 会被拒）。CI 的消费者重放先走目录路径
  （run 39654853807：linux-x64 94 个文件 / 156.3 MiB、win-x64 58 / 105.0、macos-arm64 33 / 158.4），
  再用 `python -m http.server` 把对象目录当远端、走一遍 HTTP 取数路径（run 39656642754 三档同数、
  均 0 跳过，`--help` 三次 rc=0 且输出含 `--port`）。IPC/UI 入口与
  发布落点（R2 写）仍未接。
- `.github/workflows/installer.yml` + `tools/make_icons.py`：**安装包本身**，三平台各一个未签名包，
  只装壳、不带引擎与权重（引擎 server-only 后 104.7 MiB + 权重 415 MB 起，装不进安装器）。矩阵 Windows `nsis` /
  macOS `dmg` / Linux `deb`，用钉住的 `tauri-apps/tauri-action`；CLI 显式传
  `npx --yes @tauri-apps/cli@2.12.3` —— 不传时 action 会自己 `npm install -g @tauri-apps/cli@v2`，
  浮在一个大版本上。`tauri.conf.json` 的 `bundle.active` 打开、targets 三档、图标五件；
  图标不是手画的：`make_icons.py` 只从仓库里已有的 `icon.ico` 用纯 stdlib 派生 32/128/256 PNG
  与 ICNS（预乘 alpha 双线性缩放，避免透明边缘发黑）。`--check` 先比字节，不同就**解压后逐像素比**
  —— 压缩流随 zlib 实现而变（本机 python3 的 zlib-ng 1.3.1 与 Anaconda python 的 zlib 1.3.2
  对同一份像素给出 6,389 vs 6,393 B），逐字节比会在 CI 的另一种 zlib 上假红；
  `tests/test_make_icons.py` 10 条（含"重新压缩但图没变"的阳性对照与改一个像素的阴性对照）。车道最后一步断言 glob 真找到包且非空
  （`uploadWorkflowArtifacts` 成功不代表编出了东西）。**2026-10-21 三格全出了**（run 39633525944）：
  Linux `deb` 3,237,370 B、Windows `nsis` 2,076,484 B、macOS `dmg` 3,152,248 B（`darwin-aarch64-dmg`，
  16:28Z 才排到 runner、约 3 分钟编完）。包未签名，`tauriScript` 与 deb maintainer 仍只是静态核对。
- `.github/workflows/ci.yml`：静态门禁（含 yaml_gate）+ 单测（夹具是真实 API 捕获）+ 离线跑发现器
  与离线回放工具带钉表。
- `.github/workflows/engine-live.yml`：打真实 API 发现产物 → 下载 linux 引擎（不编译）→ 下载 pin 住的
  0.5B GGUF 并校 sha256 → 无头推理，要求真的抓到 `tokens per second > 0`；
  另有 os-inference-proof 逐条解压 `manifest["inferred"]` 里的产物，要求里面有 `.exe/.dll`。
  **口径**：llama 与 sd 用 discover 现挑的**最新**产物（2026-10-21 夜里是 b11798 / master-982），
  不是 `payload-plan` 钉住的那两颗；whisper 与 OCR 才固定用钉值。所以这条车道证明的是
  "上游最新版 + 按钉值推导的 argv 仍然能跑"，钉住那颗的字节完整由 `backends` / 版本轴守。
  b11798 顺手抓到一次真实漂移：包里新出现的 `llama` 是分发器（`serve/cli/download/version`），
  `find | head -1` 选中它后 help 里没有任何补全参数 —— 实例定位因此改成只认 `llama-cli`。
- `.github/workflows/upstream-follow.yml`：每天 03:17 UTC 现取 llama.cpp 的 release 窗口，只核对
  "钉住那颗还在不在、被没被重打包"与"候选还能不能覆盖三条 OS"；有新版本退出码仍是 0，
  钉值坏了才 1，报告 JSON 无论红绿都作为 artifact 留下。升级不在这条车道里自动做。

## 这套 CI 能证明什么、不能证明什么

能证明：上游产物可发现、可下载、校验对得上、在无 GPU 的 runner 上真能产出 token。
**2026-10-21 起这些车道真的跑过了**（仓库转 public 后）：`engine-live` 的 22 个 job 全绿
（run 39633380193）、`package` 三档 OS 全绿（run 39632180385）、`shell` 的 `core` / `app` × 3 OS
全绿（run 39633525782）、`ci` 绿（run 39633525885）；安装包三格（`deb` / `nsis` / `dmg`）都已产出。
下面是转 public **之前**（2026-10-20 在 `4202ef0` 上）量到的形状，留作记录：
run 建出来了、`engine-live.yml` 的 **21 个 job 全部登记**（现在是 22 个，新加了 `backends` 那条），
可每一个都是 `runner_name` 为空、`steps: 0`、2-3 秒 conclusion=failure。
`3781953` 上再量一次还是 21 个 job，但多了一种形状：**带 `needs:` 的那个 job 是 `skipped`**
（`launch-plan` 等的是失败的 `file-index`）—— skipped 什么都不说，所以吃别人产物的那两步现在
带 `if: always()`，缺报告由工具自己报「不存在」再退出 1。
真正的原因只在 check-run annotation 里，而且这次读到了原文：
**"The job was not started because recent account payments have failed or your spending limit
needs to be increased"** —— 是**账单/支出上限**，不是"private 仓库没有免费额度"这一句就完事。
复现：`gh api repos/lilyco-42/local-ai-studio/actions/runs/<id>/jobs`（注意要 run **id**，
用 run number 会 404），再 `gh api repos/.../check-runs/<job id>/annotations`。
更早两次 push 停在 queued 二十分钟不动，别把"queued"读成"在排队、等等就有" —— 2026-10-21 的
macOS `dmg` 也排了 10 分钟才起，队列是真实的。
**也不能证明：图形界面真的打得开。** 桌面外壳交付时必须另找设备验证，不拿 CI 绿当"能用"。


## 待定

仓库名（`local-ai-studio` 是占位）与外壳许可（AGPL-3.0-or-later 还是 MIT，所以现在**故意没放 LICENSE**）。
可见性已按你的决定转 **public**（2026-10-21，选项 A），CI 因此真的跑起来了；当初 private 的三条出路
留作记录：① 已执行；② 加付款方式 / 提支出上限；③ 在 lain42.top 挂 self-hosted runner（仍算"靠 GitHub
Actions 出包"，但要在这台跑着线上对局的机器上做 `cargo build` 与 1.5 GB 下载，已不需要）。

**已经定了的**：模型目录来源 —— 分两层，描述层用 llmfit 自带的 HF 快照（17,700 条，压缩后 850 KB），
下载/校验层自建 `repo + 文件名 + sha256 + 多候选源`，因为那张表里一个内容哈希都没有。
证据与复现见 [docs/model-catalog.md](docs/model-catalog.md)；四条已立住的权重 pin 在
[pins/models.json](pins/models.json)，OCR 默认档 3 条在 [pins/ocr.json](pins/ocr.json)，
工具带 12 条在 [pins/toolbelt.json](pins/toolbelt.json)。
i18n 已经按实测拆成"识别语言"与"界面语言"两条轴并给出默认包，见
[docs/i18n.md](docs/i18n.md)。
