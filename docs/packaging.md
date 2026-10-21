# 出包可行性：「只能靠 GitHub Actions 出包」这句话拆开验

硬约束原文是"禁止本机编译与运行、只能靠 GitHub Actions 出包"。前一半（不编译不运行）已经由
`engine_probe` / `zip_peek` / `payload_plan` 这些只读元数据的工具守住了；后一半此前**只是假设**。
这一页把它拆成四个能回答的问题，每个答案都带 pin 住的出处（并已进 `pins/citations.json`，
由 `citation-drift` 车道逐条查）。2026-10-20 实测。

## ① runner 上有没有编译外壳的工具链？—— 有，而且比预想的齐

`actions/runner-images` 的 `ubuntu24/20261018.339`（→ commit `b4067e48bd9b…`，tag 里带斜杠，
raw 取文件必须换成 commit）那份 `images/ubuntu/Ubuntu2404-Readme.md` 第 140 行写着
**Rust 1.99.1**（第 142 行 rustup 1.29.1），第 26 行 **Node.js 22.23.3**。

顺带解掉 OCR 那个未知：`oar-ocr` 根 `Cargo.toml` 的 `rust-version = "1.95"`（同一张引用表里也有），
镜像自带的 1.99.1 已经高于它 —— 也就是说 OCR 车道的工具链前提在 runner 上是现成的。
车道里仍然显式钉 toolchain（`ocr-proof#0` 那条），因为"这次够"不等于"下个月那批镜像还够"。

## ② 那缺什么？—— 包表由工具从上游那一页读出来，不是一串手抄的名字

以前这一节把九颗包名抄成一段散文，而 `pins/citations.json` 只钉得住 `libwebkit2gtk-4.1-dev`
那一行（`:33`）—— 上游加一颗或把 4.1 换成 4.2，这段散文不会红。
现在它派生自 `tools/install_deps.py` → `pins/install-deps.json`：读
`tauri-apps/tauri-docs` @ `2e513e3f…` 的 `src/content/docs/start/prerequisites.mdx` 里
`<TabItem label="Debian">` 那一族（`:33-41`），只认**包管理器那条安装命令**的续行，
逐颗带上游行号。车道 `citation-drift` 每次都按 pin 现查并与已提交那张比漂移。

同一张表还逐字去镜像 readme 的包表里查在场情况（2026-10-20 实测）：

| 上游点名的 9 颗 | readme 里的情况 |
|---|---|
| `curl` `wget` `file` `libssl-dev` | **在表里**，版本分别 8.5.0-2ubuntu10.16 / 1.21.4-1ubuntu4.5 / 1:5.45-3build1 / 3.0.13-0ubuntu3.16 |
| `libwebkit2gtk-4.1-dev` `build-essential` `libxdo-dev` `libayatana-appindicator3-dev` `librsvg2-dev` | **readme 没提到** |

后一半这五个格子要说准：**readme 没提到 ≠ 机器上没装**（readme 是给人看的清单，不是 `dpkg -l`）。
所以"到底装没装"仍然只能由车道给：第一步 `apt-get install -y` 这九颗（多装在场的那四颗无害），
装完必须打印 `pkg-config --modversion webkit2gtk-4.1` —— 不打印就等于"装了但没人知道装对没"。
`webkit2gtk` 在整份 readme 里命中 0 次这条做成了 `absent` 型出处（连"没有"也查得到，命中就打行号）。

顺手记一处更正：此前任务与本文都写过"(+ gstreamer 插件族)"，**这一页 `2e513e3f…` 里 `gstreamer`
命中 0 次**（整份 prerequisites 页都搜不到），那是 Tauri 1 时代的清单留在记忆里的东西。
要加它得先给一个能查的出处，别凭印象写回这张表。

重数：`python3 tools/install_deps.py --selftest`、
`python -W error::ResourceWarning -m unittest tests.test_install_deps -v`（36 条，含 8 个把工具源码
改坏的变异体）、`python3 tools/install_deps.py --check --fill --cache .cache/refs`（联网按 pin 重算）。
（Tauri 文档给的是 4.1 而不是 4.0，这条也别凭印象写 —— 表里那颗就是判据。）

## ③ 要不要用现成的？—— 有 MIT 的官方 action，但它的钉法不一样

`tauri-apps/tauri-action`：MIT、**1,619★**、最后 push 2026-10-16，描述就是"Build your Web
application as a Tauri binary for macOS, Linux and Windows"，最新 release 是 `action-v1.0.0` 且
**assets = 0**（它是 `uses:` 型 JS action，没有下载物）。

所以工具带那套"具体 release + 上游 sha256 digest"**对它不适用**，改成 **pin commit SHA**
（`uses: tauri-apps/tauri-action@1deb371b0cd8bd54025b384f1cd735e725c4060f`，即 tag `v1.0.0`）
并记进 `pins/actions.json`。它的入口是仓库里提交的 `dist/index.js`，所以钉 commit
**就等于钉住那份 bundle**；`runs.using = node24`、23 个输入也一并记进表里（换 runtime 或改输入会红）。
按"有先例就克隆研究具体实现"处理：安装包车道以它为骨架，但**不用它的 CLI 自动回退** ——
它在找不到 `@tauri-apps/cli` 时会 `npm install -g @tauri-apps/cli@v2`（一个浮动大版本），
所以车道显式传 `tauriScript: npx --yes @tauri-apps/cli@2.12.3` 把这个坐标钉死。
引擎与 `payload-manifest.json` 也**不进安装包**（见 ④/⑤），车道只多一步"产物必须非空"。

顺手把这张表存在的理由说完：**在这一轮之前，三个 workflow 里 4 个 `uses:` 全是浮动大版本 tag**
（`actions/checkout@v4`、`actions/setup-python@v5`、`actions/upload-artifact@v4`、
`actions/download-artifact@v4`，共 44 处；**现在这张表记的是 56 处 / 6 个 workflow / 5 个 action**
—— `launch-plan` 加了一处 `download-artifact` 去吃 `file-index` 那份首启队列报告，`backends` 那条
新车道又带进 checkout 与 setup-python 各一处，`installer.yml` 再带进 checkout 与 tauri-action 各一处，
`upstream-follow.yml` 带进 checkout / setup-python / upload-artifact 各一处；
**另有 4 处是补漏**：上一版扫描表没列 `shell.yml`，它从进仓库起就有 2 个 checkout + 2 个
upload-artifact 没被数过（"49 处"因此少算）。计数不靠这句话自证：
`python3 tools/action_pins.py --check` 会按五份 workflow 重数一遍）。仓库对引擎、权重、工具带都定了"release + sha256"，
唯独真正执行出包的那只手跟着别人随时会移动的 tag 走。现在：

| action | 钉住的 commit | 来自哪个 tag | runtime / 输入数 | 许可 |
|---|---|---|---|---|
| `actions/checkout` | `11d5960a…` | v4.4.0（= `v4` 当前所指） | node20 / 21 | MIT |
| `actions/setup-python` | `a26af69b…` | v5.6.0（= `v5`） | node20 / 10 | MIT |
| `actions/upload-artifact` | `ea165f8d…` | v4.6.2（= `v4`） | node20 / 7 | MIT |
| `actions/download-artifact` | `d3f86a10…` | v4.3.0（= `v4`） | node20 / 8 | MIT |
| `tauri-apps/tauri-action` | `1deb371b…` | v1.0.0（`installer.yml` 使用） | node24 / 23 | MIT |

判据与"tag 漂了只报不跟"由 `tools/action_pins.py` 守：车道每次重新解析尾注里那个 tag，
与本表不同就红（要显式重钉），本机重跑 `GITHUB_TOKEN=$(gh auth token) python3 tools/action_pins.py --check`。
两个容易踩的形状也一并记在表里，不再靠嘴说：**release 名与 tag 名不是一回事**
（它叫 `action-v1.0.0`，tag 叫 `v1.0.0` → `pin_is_a_release_tag: false`），
以及**匿名 GitHub API 只有 60 次/小时**（撞限流时报"被限流/没权限"，不会伪装成"上游没这个提交"）。

同一次查询又撞回那个老坑：`releases/latest` 在 `actions/runner-images` 上给出的是
**`win25/20261013.288`**（windows 镜像的 tag）。这是本轮第二次证明
"latest 不是我们要的那个"（第一次是 whisper.cpp 的 latest 有 0 个产物）。

## ④ 包里到底该装什么？—— 只装外壳，引擎与权重一律首启下载

`pins/payload-plan.json` 实测的引擎**落地**负载。出包现在只收 server 与共享运行库
（`pack_engine.py` 的 server-only 策略），所以"全量"与"只收 server"两列都给：
只有 zip 的逐文件大小可离线精确求和，tar 的收窄后体积要等 CI 首次解流。

| OS 默认档 | 引擎压缩包相加 | 引擎落地（全量 → 只收 server） | 最省的一条权重 |
|---|---|---|---|
| win-x64 | 44.2 MiB | 115.6 → **104.7 MiB**（三个包都是 zip，逐文件精确） | 对话 qwen2.5-0.5b-q2_k 415 MB |
| linux-x64 | 52.1 MiB | 136.1 MiB；sd 是 zip，65.0 → 63.6，llama/whisper 是 tar.gz 待 CI | 同上 |
| macos-arm64 | 46.5 MiB | 124.9 MiB；sd 是 zip，93.9 → 90.4（5 条收 2 条），llama/whisper 是 tar.gz 待 CI | 同上 |

（上一版这三格只有 sd 的数却标成整档落地 —— linux 65.0 / macOS 93.9 都漏掉了 llama 与 whisper。）

结论不是审美问题而是算术：**引擎装进安装包就已经 >100 MiB（只收 server 也还有 104.7 MiB），再叠 415 MB（对话）或
1,566 MB（生图）就不是安装包了** —— 而且权重本来就该按内容寻址校验后现下
（下载层契约见 [model-catalog.md](model-catalog.md) 与 `tools/file_index.py`）。
LM Studio 也是这个形态（壳先装好、模型首启下载），所以"安装包小、首启下载"不是妥协，是同类产品的共同选择。

## ⑤ 出包产物怎么变成外壳认的目录？—— 一步摆位，根清单最后写

`pack_engine.py` 的产物是扁平的 `dist/<os>/<engine>/` + `dist/<os>/payload-manifest.json`；
外壳（`shell/core/src/supervisor.rs` 的 `verify_payload`）要的却是
`<data>/runtime/<os>/<engine>/<version>/<asset-stem>/` 和**数据根**上的 `payload-manifest.json`。
两者之间原来没有工具，装出来外壳会报"清单不在"。`tools/stage_payload.py` 就补这一步，
规则全部可离线复算：

- **目标只从合同派生**：按 `pins/shell-contract.json` 的能力行（能力 × OS 指向哪个引擎、
  哪个目录模板、默认 asset）算落点，脚本里没有一条引擎/版本字面量；同一引擎在合同里
  出现两个不同模板直接报错。
- **进门前先对账**：源清单的 os 与 `--os` 不符、引擎不在合同里、asset 与合同的 `default_asset`
  不同 → 停。目标目录已存在时必须**恰好**是清单里那些文件，多一个都拒绝覆盖/删除（用户的东西不碰）。
- **逐文件证据**：按源清单的 bytes + sha256 复制，复制后再算一次；目标文件名带 `..`、`/`、`\`
  或重名一律拒。
- **根清单最后写**：先写临时文件再 `os.replace`，任何中途失败都不会留下"看似齐全"的根清单；
  写完与源清单按字节比一次。`--check` 解析两份清单逐键相等，再把每个文件重算一遍。
- **缺席要点名，不是报错**：包里没有的合同能力（OCR；macOS 的 whisper 上游没有可执行产物）
  记进 `not_in_manifest`，装的时候看得到，不会假装装了。
- **首启交付清单只生成、不上传**：`tools/payload_intake.py` 读摆好的数据根，逐文件给出
  `(key, dest, sha256, bytes)`。键是 `payload/<os>/<数据根相对路径>`，根清单单独一行标
  `land: last`（文件先到位，清单最后落）；`--check` 用同一棵树重算并要求逐键相等。
  它复算每个文件的体积与 sha256，拒绝多余/缺失文件、键前缀越界、两个引擎指到同一个目标；
  输出不含 runner 的绝对路径 —— 两台机器摆同一棵树必须得到同一份字节。同一 OS 的根清单
  键固定（`payload/<os>/payload-manifest.json`），发布新版本时必须整版原子替换或给
  `--prefix` 加版本段；本次只生成清单，**没有发布动作**。

证据边界：`--selftest` 用合成合同/合成包证明 13 条判据会红（篡改、脏目录、路径逃逸、
asset/OS/引擎不符等）；`tests/test_stage_payload.py` 10 条离线对照，其中一条走**真实合同**
（whisper 落在 `runtime/win-x64/whisper/b5454/whisper-bin-x64`）。`package.yml` 里是
`--json` 出报告 + `--check` 复核两步。摆位之后同一 job 再跑 `payload_intake.py` 生成
`delivery-manifest.json` 并 `--check`，随 artifact 一起上传；**这份清单只描述"怎么送"，
没有任何写入桶或首启下载的动作**。`tests/test_payload_intake.py` 21 条（真实合同落点、
两个数据根同一份字节、13 条合成自测、6 条源码变异体，以及消费端重放：删掉原数据根，
只凭对象字典按清单还原，按外壳 `verify_payload` 的顺序逐文件验过；坏对象/缺对象/
越界或重复落点时根清单不落）。2026-10-21 出包车道（run 39632180385）在三档 OS 上真摆了位
并生成了 `delivery-manifest.json`（第 8、9 步都绿）。权重不在这一步，它们按
[model-catalog.md](model-catalog.md) 首启下载。

**外壳这一端现在也有消费者**：`shell/core/src/delivery.rs` 解析同一份清单（schema/os/键前缀/
键与落点绑定/哈希/总量逐项校验），再按行取字节、先比长度再比 sha256、同目录临时文件 + 重命名
落盘，根清单最后落；已正确的文件跳过（幂等）。它不认网络 —— 取字节是调用方给的 `fetch` 回调，
网络与 IPC 仍没接。CI 里的消费者重放（`examples/install_from_dir.rs`，仅 linux-x64）把
`/tmp/root-<os>` 复制成对象字典、**删掉原数据根**，只凭 `delivery-manifest.json` 装进空目录，
再从装好的树起 `llama-server --help`：run 39651683176 装好 94 个文件（0 跳过）、156.3 MiB，
`rc=0` 且输出里有 `--port`。三档都在各自原生 runner 上跑通了同一条路径
（run 39654853807：linux-x64 94 个文件 / 156.3 MiB、win-x64 58 / 105.0、macos-arm64 33 / 158.4，
三次 `--help` 都是 rc=0）—— "清单足以还原一棵能起引擎的树"现在三档都有 CI 证据。
这里的"真机"是 GitHub 托管的系统镜像，不是用户的个人电脑。

**还没接上的一段**：数据根仍只在 CI 的 `/tmp` 或 `.replay` 里；`delivery-manifest.json` 给出了逐文件
对象键与落点，外壳也能按它落盘，但**没有人往 R2 上传、也没有首启下载的 IPC 入口**。安装包车道只编壳（⑥），
`download.rs` 只处理权重的平铺文件。把负载送到数据根仍是任务 #57 的待定动作：
首启按这份清单逐文件从 R2 取（需他批准写桶；三档 105.0–158.4 MiB ≈ 117–176 s @ 0.90 MiB/s），
或随安装包分发（按同一份清单计，win 105.0 / linux 156.3 / mac 158.4 MiB）。

## ⑥ 安装包本身怎么出？—— 三平台各一个未签名包，图标由 ICO 派生

`.github/workflows/installer.yml`：`shell/**` 或它自己变动时触发（也留 `workflow_dispatch`），
矩阵三格 Windows `nsis`、macOS `dmg`、Linux `deb`；`projectPath: shell`，
`tauriScript: npx --yes @tauri-apps/cli@2.12.3`（钉死，绕开 tauri-action 在找不到 CLI 时
`npm install -g @tauri-apps/cli@v2` 的浮动回退），`tauri-action` 钉 `1deb371b…`，
产物用 `uploadWorkflowArtifacts` 上传。包里只有外壳，引擎与权重不在其中。

图标：打包器按平台读 `.ico` / `.icns` / PNG，仓库原来只有一张 32×32 的 `icon.ico`。
`tools/make_icons.py` 用纯 stdlib 解 ICO 的 32bpp BGRA（`biHeight` 是高度的两倍，下半是
AND 掩码），按**预乘 alpha** 做双线性放大，写出 `32x32.png`、`128x128.png`、`128x128@2x.png`
与 `icon.icns`（`icp4/icp5/icp6/ic07/ic08/ic09`，每块直接放 PNG）。同一解释器下输出确定；
`--check` **不能只逐字节比**：本机两个解释器的 zlib 不同（zlib-ng 1.3.1 vs zlib 1.3.2），
同一份像素压缩出 6,389 vs 6,393 B —— 所以字节不同时解压后逐像素比，CI 才不会假红。
10 条单测覆盖形状、块类型、预乘、坏 ICO、篡改与缺文件、以及"重压缩的同一张图必须认、
改一个像素必须拒"这对正负对照。
`bundle.active` 已打开，目标写在 `tauri.conf.json`，车道再用 `--bundles` 逐平台收窄。

证据边界：2026-10-21 三格都真出包了（run 39633525944）：Linux `deb` 3,237,370 B、Windows
`nsis` 2,076,484 B、macOS `dmg` 3,152,248 B；CLI 版本、图标与"产物必须非空"都由车道本身核过。
签名仍没做（macOS 无公证、Windows 无代码签名，首启有系统警告），证书仍需他决定。

## 还欠什么（都在等一个决定，不是技术问题）

- **CI 已经跑过了**：2026-10-21 仓库转 public 后，`ci`、`shell`、`engine-live`（22 job）、
  `package`（3 OS）全绿；`installer` 的 Linux `deb` 与 Windows `nsis` 已出包，macOS `dmg` 仍在
  队列/编译中。转 public 之前（2026-10-20 在 `4202ef0` 上）的形状是 run 建了、job 全登记但
  `runner_name` 空、`steps: 0`、2-3 秒 failure；原因只在 check-run annotation 里，写的是
  **账单/支出上限**（"recent account payments have failed or your spending limit needs to be
  increased"），不是"private 没有免费额度"一句话。逐条复现命令与完整观察记在 README 末段
  "这套 CI 能证明什么"。①②④⑤⑥ 现在都有 CI 证据；仍未证的是安装后的外壳真的能打开（GUI 设备验证）。
- **签名**：macOS 要 Apple 证书 + notarization secrets；Windows 签名要代码签名证书，不签就吃 SmartScreen 提示。
  这两件都要花钱/要账号，由他定；不签名也能出包，只是首启有警告。
- **macOS 的字幕**：上游 whisper.cpp 没有 macOS 的**可执行**产物（`payload-plan` 里那行 `上游无产物`， 同排的 `asset_evidence` 记着 xcframework 的成员表与"里面没有 whisper-cli"这一事实），
  要么自编译（CI 里 cmake + Metal）、要么这个平台先不做字幕（#29）。
