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

## ② 那缺什么？—— Linux 侧缺 GTK/webkit 一族，而且必须自己装

同一份镜像 readme 里 **`webkit2gtk` 出现 0 次**（这条做成了 `absent` 型出处：连"没有"也查得到，
命中就把行号打出来）。而 Tauri 2 的 Linux 前提（`tauri-apps/tauri-docs` @ `2e513e3f…`，
`src/content/docs/start/prerequisites.mdx` 第 33-41 行）点名要：

```
libwebkit2gtk-4.1-dev  build-essential  curl  wget  file  libxdo-dev
libssl-dev  libayatana-appindicator3-dev  librsvg2-dev
```

后果很具体：**安装包车道的第一步是 `apt-get install` 这一族**，装完还要把
`pkg-config --modversion webkit2gtk-4.1` 打出来 —— 不打印就等于"装了但没人知道装对没"。
（Tauri 文档同时给的是 4.1 而不是 4.0，这条别凭印象写。）

## ③ 要不要用现成的？—— 有 MIT 的官方 action，但它的钉法不一样

`tauri-apps/tauri-action`：MIT、**1,619★**、最后 push 2026-10-16，描述就是"Build your Web
application as a Tauri binary for macOS, Linux and Windows"，最新 release 是 `action-v1.0.0` 且
**assets = 0**（它是 `uses:` 型 JS action，没有下载物）。

所以工具带那套"具体 release + 上游 sha256 digest"**对它不适用**，改成 **pin commit SHA**
（`uses: tauri-apps/tauri-action@<40hex>`）并把它记进引用表。按"有先例就克隆研究具体实现"处理：
外壳的安装包车道以它为骨架，只写我们多出来的那两步（塞引擎负载与 `payload-manifest.json`）。

同一次查询又撞回那个老坑：`releases/latest` 在 `actions/runner-images` 上给出的是
**`win25/20261013.288`**（windows 镜像的 tag）。这是本轮第二次证明
"latest 不是我们要的那个"（第一次是 whisper.cpp 的 latest 有 0 个产物）。

## ④ 包里到底该装什么？—— 只装外壳，引擎与权重一律首启下载

`pins/payload-plan.json` 实测的引擎**落地**负载（逐文件那部分只算得出 zip 的行）：

| OS 默认档 | 引擎压缩包相加 | 引擎落地 | 最省的一条权重 |
|---|---|---|---|
| win-x64 | 44.2 MiB | **115.6 MiB** | 对话 qwen2.5-0.5b-q2_k 415 MB |
| linux-x64 | 52.1 MiB | 65.0 MiB（llama/whisper 是 tar.gz，逐文件要 CI 解） | 同上 |
| macos-arm64 | 46.5 MiB | 93.9 MiB | 同上 |

结论不是审美问题而是算术：**引擎装进安装包就已经 >100 MiB，再叠 415 MB（对话）或
1,566 MB（生图）就不是安装包了** —— 而且权重本来就该按内容寻址校验后现下
（下载层契约见 [model-catalog.md](model-catalog.md) 与 `tools/file_index.py`）。
LM Studio 也是这个形态（壳先装好、模型首启下载），所以"安装包小、首启下载"不是妥协，是同类产品的共同选择。

## 还欠什么（都在等一个决定，不是技术问题）

- **CI 本身没跑过**：仓库 private，这个账号给 private 分不到 runner（现在表现为 job 永久停在
  `queued`、`runner_name: null`）。上面 ①②④ 都是本机读公共来源量的，**唯独"车道真跑一遍装出包"必须等解锁**（#19）。
- **签名**：macOS 要 Apple 证书 + notarization secrets；Windows 签名要代码签名证书，不签就吃 SmartScreen 提示。
  这两件都要花钱/要账号，由他定；不签名也能出包，只是首启有警告。
- **macOS 的字幕**：上游 whisper.cpp 没有 macOS 的**可执行**产物（`payload-plan` 里那行 `上游无产物`， 同排的 `asset_evidence` 记着 xcframework 的成员表与"里面没有 whisper-cli"这一事实），
  要么自编译（CI 里 cmake + Metal）、要么这个平台先不做字幕（#29）。
