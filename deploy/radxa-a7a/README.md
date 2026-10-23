# Radxa Cubie A7A：类 Ollama 的本地 AI API

本目录将 **上游预编译 llama.cpp** 作为推理引擎，暴露 OpenAI 兼容 API；
不自行实现推理内核，不在 SD 卡上编译，不修改 GPU/NPU 驱动、内核、MMC 时钟或设备树。

> 注意：这里提供的是 **Ollama 风格的本地模型服务**，不是 Ollama 本体；
> 接口是 `/v1/chat/completions` 等 OpenAI 兼容接口，不声称具有 `ollama pull`
> 或 `/api/generate` 的原生语义。管理 UI、模型切换以后由 local-ai-studio
> 或 tiny_lilyco 的外壳接入。

## 已在实机复现

2026-10-22，Debian 13 / aarch64 / 4 GiB 的 Radxa Cubie A7A：

| 指标 | 验证记录 |
|---|---|
| 版本 | `llama.cpp b11804`、commit `c7fc50941`、Linux ARM64 CPU |
| 模型 | Qwen2.5-0.5B-Instruct GGUF Q2_K，415,182,688 bytes |
| 服务 | `127.0.0.1:8081`，`--api-key-file`，`--no-webui` |
| 请求 | `POST /v1/chat/completions`，HTTP 200，非空自然语言 |
| 一次请求测得 | Prefill **71.58 t/s**，Decode **17.90 t/s**（36 prompt / 11 completion tokens）|
| systemd | `a7a-llama.service`（user service），`loginctl` linger enabled |
| 存储 | 部署后 `/sys/fs/ext4/mmcblk1p3/errors_count = 0` |
| GPU/NPU | 驱动节点存在，**此次推理仅走 CPU**，未声称 PowerVR/VIP9000 加速 |

数据只证明这次机器、模型、条件下真实跑通，不是五次重复 Benchmark。

### 上游文件与 SHA-256（使用仓库 pins）

- llama.cpp `b11804` Linux ARM64 CPU 官方包：
  `llama-b11804-bin-ubuntu-arm64.tar.gz`
  SHA-256：
  `2c2c2daf42db57fceca356e3440213f350dd800f74101d78c1dde04a14dca124`
- Qwen2.5-0.5B-Instruct Q2_K：
  `qwen2.5-0.5b-instruct-q2_k.gguf`
  SHA-256：
  `9ee36184e616dfc76df4f5dd66f908dbde6979524ae36e6cefb67f532f798cb8`

## 快速部署

在 A7A 上用 `radxa` 账户执行（整个操作无需 root 编译）：

```sh
git clone https://github.com/lilyco-42/local-ai-studio.git
cd local-ai-studio
bash deploy/radxa-a7a/install.sh
```

若使用尚未合并的分支，先切到 `feat/radxa-a7a-inference-api`。
安装脚本逐个下载、限制到 4 MiB/s、校验 SHA-256、只使用用户目录。
如果 GitHub Release 下载慢，可从 Windows 下载预编译包并经 SCP
放到 `/home/radxa/llama-b11804-bin-ubuntu-arm64.tar.gz`；安装脚本会复用并检验。

脚本遇到已有文件哈希不符、ext4 已有错误或已有 systemd unit 被本地修改，会停止，
**不会**删除不匹配文件、强行覆盖现有配置。

仅第一次需要在 A7A 上授予 user service 开机驻留：

```sh
sudo loginctl enable-linger radxa
```

检查：

```sh
systemctl --user status a7a-llama.service
bash deploy/radxa-a7a/health-check.sh
curl -fsS http://127.0.0.1:8081/health
```

API Key 文件：`/home/radxa/a7a-ai/run/api.key`（权限 600），不要提交进 GitHub。
请求示例：

```sh
TOKEN="$(cat /home/radxa/a7a-ai/run/api.key)"
curl -fsS http://127.0.0.1:8081/v1/chat/completions \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  --data '{"model":"qwen2.5-0.5b-instruct-q2_k.gguf","messages":[{"role":"user","content":"你好"}],"max_tokens":80}'
```

## 公网访问的边界

A7A 位于路由器私网（实机是 `192.168.10.165`），本机推理端口严格绑定
`127.0.0.1:8081`，**不要在小米路由器转发 8081，也不要对公网绑定 0.0.0.0**。

现有 `lain42.top` 由主服务器上的 Pingap 0.13.5 管理 80/443，
`ai.lain42.top` 已是另外一个 AI 网关；不要覆盖其现有路由。

拟使用以下受限结构（**当且仅当隧道与 Pingap 上游验证通过后才上线**）：

```text
https://lain42.top/edge-ai/v1/chat/completions
          | TLS / existing Pingap
          v
Lain42 server 127.0.0.1:19081
          | restricted, reverse SSH tunnel
          v
A7A 127.0.0.1:8081 /v1/chat/completions
          | API Key required
          v
Qwen2.5-0.5B on CPU
```

服务器隧道专用账号：`a7a-ai-relay`，不应使用 root 做隧道。
它的公钥授权应限制反向监听地址 `127.0.0.1:19081`；
本地服务仍要求 Bearer Token，即便隧道被错误公开也不应匿名生成。
公网路由只应匹配前缀 `/edge-ai/v1` 并重写为 `/v1`，
不要把免鉴权的 `/health` 暴露到互联网。

**部署状态须逐项实测**：本机推理和 user service 已验证；
公网隧道及 Pingap 路由尚不能标记为已完成。

## 停止 / 回滚

```sh
systemctl --user disable --now a7a-llama.service
# 不要执行 e2fsck 在线修复；不要自动重启/格式化 SD 卡。
# 如需彻底卸载，先备份模型与配置，再人工删除用户目录。
```

若根分区出现任何新 ext4 错误，应停止部署，禁止继续写模型、编译或修改驱动。
恢复文件系统应在备份后由未挂载根分区的恢复环境处理。
