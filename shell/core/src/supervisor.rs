use std::collections::BTreeMap;
use std::fs;
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

use serde_json::{json, Map, Value};

use crate::base64;
use crate::contract::{self, Capability, Contract, OsCell};
use crate::error::CoreError;
use crate::http::{self, FormFile, Response};
use crate::manifest::{self, Manifest};

pub const READY_TIMEOUT: Duration = Duration::from_secs(180);
pub const READY_POLL: Duration = Duration::from_millis(250);
pub const CHAT_TIMEOUT: Duration = Duration::from_secs(600);
pub const IMAGE_TIMEOUT: Duration = Duration::from_secs(600);
pub const OCR_TIMEOUT: Duration = Duration::from_secs(600);

const OCR_GAP: &str = "已知缺口 ocr-manifest：OCR 的二进制由 CI 现编，\
出包车道还没把它写进 payload-manifest.json";

pub struct LaunchPlan {
    pub program: PathBuf,
    pub args: Vec<String>,
    pub cwd: PathBuf,
    pub env: Vec<(String, String)>,
    pub port: u16,
    /// 要逐一对盘上存在性的非二进制输入（权重/图片等）。
    pub inputs: Vec<PathBuf>,
}

pub struct LaunchContext<'a> {
    pub data_root: &'a Path,
    pub tier: &'a str,
    pub port: u16,
    pub token: Option<&'a str>,
    pub extra: &'a BTreeMap<String, String>,
}

fn models_dir(data_root: &Path) -> PathBuf {
    data_root.join("models")
}

fn library_env(dir: &Path) -> Vec<(String, String)> {
    let dir = dir.to_string_lossy().into_owned();
    match std::env::consts::OS {
        "linux" => vec![("LD_LIBRARY_PATH".into(), dir)],
        "macos" => vec![("DYLD_LIBRARY_PATH".into(), dir)],
        _ => Vec::new(),
    }
}

/// 把合同里的那一行变成真正要敲的命令。任何一段 `{...}` 没被替换都算错，
/// 免得引擎把字面量 `{port}` 当成路径去打开。
pub fn render(
    cap: &Capability,
    cell: &OsCell,
    ctx: &LaunchContext<'_>,
) -> Result<LaunchPlan, CoreError> {
    let dir_template = cell
        .asset_dir
        .as_deref()
        .or(cell.version_dir.as_deref())
        .ok_or_else(|| CoreError::Contract(format!("{}: 没有 runtime 目录", cap.engine)))?;
    let cwd = contract::expand_dir(dir_template, ctx.data_root)?;
    let program = cwd.join(&cell.binary);
    let row = cell.argv_for(ctx.tier)?;
    let mut args = Vec::with_capacity(row.len());
    let mut inputs = Vec::new();
    for token in row {
        let resolved = match token.as_str() {
            "{bin}" => program.to_string_lossy().into_owned(),
            "{port}" => {
                if ctx.port == 0 {
                    return Err(CoreError::Contract(
                        "命令行要端口，但外壳没分配到端口".into(),
                    ));
                }
                ctx.port.to_string()
            }
            "{token}" => ctx.token.map(|t| t.to_string()).ok_or_else(|| {
                CoreError::Contract(format!("{}: 命令行要 {{token}}，但这个引擎没有钥匙", cap.engine))
            })?,
            other if contract::is_placeholder(other) => {
                let inner = &other[1..other.len() - 1];
                if let Some(v) = ctx.extra.get(inner) {
                    v.clone()
                } else if cap.mode == "server" {
                    let path = models_dir(ctx.data_root).join(inner);
                    inputs.push(path.clone());
                    path.to_string_lossy().into_owned()
                } else {
                    return Err(CoreError::Contract(format!(
                        "{}: 子进程参数 {other} 没有取值（外壳不会猜路径）",
                        cap.engine
                    )));
                }
            }
            other => {
                if other.contains('{') || other.contains('}') {
                    return Err(CoreError::Contract(format!(
                        "{}: {other:?} 里还有没替换的占位符",
                        cap.engine
                    )));
                }
                other.to_string()
            }
        };
        args.push(resolved);
    }
    Ok(LaunchPlan {
        env: library_env(&cwd),
        program,
        args,
        cwd,
        port: ctx.port,
        inputs,
    })
}

fn allocate_port(bind: &str) -> Result<u16, CoreError> {
    let listener = TcpListener::bind((bind, 0))
        .map_err(|e| CoreError::Protocol(format!("在 {bind}:0 取临时端口失败: {e}")))?;
    let port = listener
        .local_addr()
        .map_err(|e| CoreError::Protocol(format!("读临时端口失败: {e}")))?
        .port();
    drop(listener);
    Ok(port)
}

fn log_tail(path: &Path, max_bytes: usize) -> String {
    match fs::read(path) {
        Ok(bytes) => {
            let start = bytes.len().saturating_sub(max_bytes);
            String::from_utf8_lossy(&bytes[start..]).trim().to_string()
        }
        Err(_) => String::new(),
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct Status {
    pub capability: String,
    pub engine: String,
    pub available: bool,
    pub running: bool,
    pub pid: Option<u32>,
    pub port: Option<u16>,
    pub idle_secs_left: Option<u64>,
    pub lifecycle: String,
}

struct Running {
    child: Child,
    port: u16,
    token: Option<String>,
    last_used: Instant,
    idle: Option<Duration>,
}

impl Running {
    fn touch(&mut self) {
        self.last_used = Instant::now();
    }
}

pub fn idle_expired(idle: Option<Duration>, last_used: Instant, now: Instant) -> bool {
    match idle {
        Some(limit) => now.saturating_duration_since(last_used) >= limit,
        None => false,
    }
}

pub struct OcrRequest<'a> {
    pub image: &'a Path,
    pub out_dir: &'a Path,
    pub det: &'a Path,
    pub rec: &'a Path,
    pub dict: &'a Path,
}

pub struct Supervisor {
    contract: Contract,
    platform: &'static str,
    tier: String,
    data_root: PathBuf,
    device: String,
    readiness_timeout: Duration,
    running: BTreeMap<String, Running>,
}

impl Supervisor {
    pub fn new(
        contract: Contract,
        platform: &'static str,
        tier: &str,
        data_root: PathBuf,
        device: &str,
    ) -> Result<Self, CoreError> {
        for (name, cap) in &contract.capabilities {
            if cap.mode != "server" {
                continue;
            }
            let cell = cap.os.get(platform).ok_or_else(|| {
                CoreError::Platform(format!("{name}: 合同里没有 {platform} 这一格"))
            })?;
            cell.argv_for(tier).map_err(|_| {
                CoreError::Contract(format!(
                    "{name}: 没有档位 {tier:?} 的命令行（有: {:?}）",
                    cell.argv_by_tier.keys().collect::<Vec<_>>()
                ))
            })?;
        }
        Ok(Self {
            contract,
            platform,
            tier: tier.to_string(),
            data_root,
            device: device.to_string(),
            readiness_timeout: READY_TIMEOUT,
            running: BTreeMap::new(),
        })
    }

    pub fn with_readiness_timeout(mut self, timeout: Duration) -> Self {
        self.readiness_timeout = timeout;
        self
    }

    pub fn data_root(&self) -> &Path {
        &self.data_root
    }

    pub fn tier(&self) -> &str {
        &self.tier
    }

    pub fn status(&mut self) -> Vec<Status> {
        let mut out = Vec::new();
        for (name, cap) in &self.contract.capabilities {
            let available = cap.os.contains_key(self.platform);
            let mut running = false;
            let mut pid = None;
            let mut port = None;
            let mut left = None;
            if let Some(r) = self.running.get_mut(name) {
                match r.child.try_wait() {
                    Ok(None) => {
                        running = true;
                        pid = Some(r.child.id());
                        port = Some(r.port);
                        left = r.idle.map(|limit| {
                            let used = Instant::now().saturating_duration_since(r.last_used);
                            limit.saturating_sub(used).as_secs()
                        });
                    }
                    _ => {
                        let _ = self.running.remove(name);
                    }
                }
            }
            out.push(Status {
                capability: name.clone(),
                engine: cap.engine.clone(),
                available,
                running,
                pid,
                port,
                idle_secs_left: left,
                lifecycle: cap.lifecycle.mode.clone(),
            });
        }
        out
    }

    fn manifest_path(&self) -> PathBuf {
        self.data_root.join("payload-manifest.json")
    }

    fn verify_payload(&self, cap: &Capability, cell: &OsCell) -> Result<PathBuf, CoreError> {
        let path = self.manifest_path();
        if !path.is_file() {
            let extra = if cap.mode == "subprocess" { format!("；{OCR_GAP}") } else { String::new() };
            return Err(CoreError::Manifest(format!(
                "载荷清单不在 {} —— 没有它就不能保证起的正是下的那一颗{extra}",
                path.display()
            )));
        }
        let manifest = Manifest::from_file(&path)?;
        if manifest.os != self.platform {
            return Err(CoreError::Manifest(format!(
                "载荷清单是 {} 的，这台机器是 {}",
                manifest.os, self.platform
            )));
        }
        let entry = if cap.mode == "server" {
            let asset = cell.default_asset.as_deref().ok_or_else(|| {
                CoreError::Contract(format!("{}: 没有默认产物", cap.engine))
            })?;
            manifest.for_engine(&cap.engine, asset)?
        } else {
            manifest.for_engine_without_asset(&cap.engine).map_err(|e| {
                CoreError::Manifest(format!("{e}；{OCR_GAP}"))
            })?
        };
        let dir_template = cell
            .asset_dir
            .as_deref()
            .or(cell.version_dir.as_deref())
            .ok_or_else(|| CoreError::Contract(format!("{}: 没有 runtime 目录", cap.engine)))?;
        let dir = contract::expand_dir(dir_template, &self.data_root)?;
        manifest::verify_dir(entry, &dir)?;
        Ok(dir)
    }

    fn spawn(&self, cap: &Capability, plan: &LaunchPlan) -> Result<(Child, PathBuf), CoreError> {
        for input in &plan.inputs {
            if !input.is_file() {
                return Err(CoreError::Manifest(format!(
                    "{}: 输入不在盘上: {}",
                    cap.engine,
                    input.display()
                )));
            }
        }
        let log_dir = self.data_root.join("logs");
        fs::create_dir_all(&log_dir)?;
        let log_path = log_dir.join(format!("{}.log", cap.engine));
        let log = fs::File::create(&log_path)?;
        let mut cmd = Command::new(&plan.program);
        cmd.args(&plan.args)
            .current_dir(&plan.cwd)
            .envs(plan.env.iter().map(|(k, v)| (k, v)))
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::from(log));
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            cmd.creation_flags(CREATE_NO_WINDOW);
        }
        let child = cmd.spawn().map_err(|e| {
            CoreError::Spawn(format!("起 {} 失败（{}）: {e}", cap.engine, plan.program.display()))
        })?;
        Ok((child, log_path))
    }

    fn wait_ready(
        &self,
        cap: &Capability,
        child: &mut Child,
        port: u16,
        log_path: &Path,
    ) -> Result<(), CoreError> {
        let readiness = cap.readiness.as_ref().ok_or_else(|| {
            CoreError::Contract(format!("{}: 没有就绪探针，不能瞎等", cap.engine))
        })?;
        let bind = cap.bind.as_deref().unwrap_or("127.0.0.1");
        let deadline = Instant::now() + self.readiness_timeout;
        loop {
            if let Some(status) = child.try_wait()? {
                return Err(CoreError::Spawn(format!(
                    "{} 在就绪前退出（{status}）—— 日志尾巴: {}",
                    cap.engine,
                    log_tail(log_path, 2048)
                )));
            }
            match http::request(
                bind,
                port,
                &readiness.method,
                &readiness.path,
                &[],
                None,
                Duration::from_secs(5),
            ) {
                Ok(r) if r.status == readiness.expect_status => return Ok(()),
                Ok(_) | Err(_) => {}
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                return Err(CoreError::Spawn(format!(
                    "{} 在 {:?} 内没就绪（探针 {} {}）—— 日志尾巴: {}",
                    cap.engine,
                    self.readiness_timeout,
                    readiness.method,
                    readiness.path,
                    log_tail(log_path, 2048)
                )));
            }
            std::thread::sleep(READY_POLL);
        }
    }

    pub fn ensure_running(&mut self, name: &str) -> Result<u16, CoreError> {
        let cap = self.contract.capability(name)?.clone();
        if cap.mode != "server" {
            return Err(CoreError::Contract(format!(
                "{name}: 上游只有 CLI、没有服务形态 —— 一次任务一个进程，不监听端口"
            )));
        }
        if let Some(r) = self.running.get_mut(name) {
            match r.child.try_wait() {
                Ok(None) => {
                    r.touch();
                    return Ok(r.port);
                }
                _ => {
                    self.running.remove(name);
                }
            }
        }
        let cell = cap
            .os
            .get(self.platform)
            .ok_or_else(|| CoreError::Platform(format!("{name}: 合同里没有 {self_platform}", self_platform = self.platform)))?
            .clone();
        self.verify_payload(&cap, &cell)?;
        let token = match cap.auth.mode.as_str() {
            "api-key" => {
                let bytes = cap.auth.token_bytes.unwrap_or(0);
                if bytes == 0 {
                    return Err(CoreError::Contract(format!(
                        "{name}: 钥匙模式却没写字节数"
                    )));
                }
                Some(crate::token_hex(bytes))
            }
            _ => None,
        };
        let bind = cap.bind.as_deref().unwrap_or("127.0.0.1");
        let port = allocate_port(bind)?;
        let extra = BTreeMap::new();
        let plan = render(
            &cap,
            &cell,
            &LaunchContext {
                data_root: &self.data_root,
                tier: &self.tier,
                port,
                token: token.as_deref(),
                extra: &extra,
            },
        )?;
        let (mut child, log_path) = self.spawn(&cap, &plan)?;
        if let Err(err) = self.wait_ready(&cap, &mut child, port, &log_path) {
            return Err(err);
        }
        let idle = cap.lifecycle.idle_timeout_secs.map(Duration::from_secs);
        self.running.insert(
            name.to_string(),
            Running {
                child,
                port,
                token,
                last_used: Instant::now(),
                idle,
            },
        );
        Ok(port)
    }

    fn auth_header(&self, name: &str) -> Result<Option<(String, String)>, CoreError> {
        let cap = self.contract.capability(name)?;
        if cap.auth.mode != "api-key" {
            return Ok(None);
        }
        let template = cap
            .auth
            .header
            .as_deref()
            .ok_or_else(|| CoreError::Contract(format!("{name}: 没有鉴权头模板")))?;
        let token = self
            .running
            .get(name)
            .and_then(|r| r.token.clone())
            .ok_or_else(|| CoreError::Contract(format!("{name}: 引擎在跑，但外壳没有它的钥匙")))?;
        let (header, value) = template
            .split_once(':')
            .ok_or_else(|| CoreError::Contract(format!("{name}: 鉴权头模板 {template:?} 没有冒号")))?;
        Ok(Some((
            header.trim().to_string(),
            value.trim().replace("{token}", &token),
        )))
    }

    fn send(
        &mut self,
        name: &str,
        method: &str,
        path: &str,
        mut headers: Vec<(String, String)>,
        body: Option<&[u8]>,
        timeout: Duration,
    ) -> Result<Response, CoreError> {
        let port = self.ensure_running(name)?;
        if let Some(h) = self.auth_header(name)? {
            headers.push(h);
        }
        let bind = self
            .contract
            .capability(name)?
            .bind
            .clone()
            .unwrap_or_else(|| "127.0.0.1".into());
        let response = http::request(&bind, port, method, path, &headers, body, timeout)?;
        if let Some(r) = self.running.get_mut(name) {
            r.touch();
        }
        Ok(response)
    }

    pub fn get(&mut self, name: &str, path: &str) -> Result<Response, CoreError> {
        self.send(name, "GET", path, Vec::new(), None, Duration::from_secs(60))
    }

    pub fn chat(&mut self, body: Value) -> Result<Value, CoreError> {
        let name = "对话";
        let cap = self.contract.capability(name)?.clone();
        let request = cap
            .request
            .as_ref()
            .ok_or_else(|| CoreError::Contract(format!("{name}: 没有请求描述")))?;
        let path = request
            .path
            .clone()
            .ok_or_else(|| CoreError::Contract(format!("{name}: 没有请求路径")))?;
        if !body.is_object() {
            return Err(CoreError::Protocol("对话请求体必须是 JSON 对象".into()));
        }
        let mut body = body;
        if body.get("stream").is_none() {
            body["stream"] = json!(false);
        }
        let bytes = serde_json::to_vec(&body)
            .map_err(|e| CoreError::Json(format!("序列化对话请求失败: {e}")))?;
        let response = self.send(
            name,
            request.method.as_deref().unwrap_or("POST"),
            &path,
            vec![("Content-Type".into(), "application/json".into())],
            Some(&bytes),
            CHAT_TIMEOUT,
        )?;
        http::expect_2xx(response, "对话").and_then(|r| r.json())
    }

    pub fn transcribe(
        &mut self,
        filename: &str,
        content_type: &str,
        audio: &[u8],
        language: Option<&str>,
    ) -> Result<String, CoreError> {
        let name = "字幕";
        let cap = self.contract.capability(name)?.clone();
        let request = cap
            .request
            .as_ref()
            .ok_or_else(|| CoreError::Contract(format!("{name}: 没有请求描述")))?;
        let path = request
            .path
            .clone()
            .ok_or_else(|| CoreError::Contract(format!("{name}: 没有请求路径")))?;
        let field = request
            .multipart_field
            .clone()
            .ok_or_else(|| CoreError::Contract(format!("{name}: 没有文件字段名")))?;
        let mut fields = request.extra_fields.clone().unwrap_or_default();
        if let Some(lang) = language {
            fields.insert("language".into(), lang.to_string());
        }
        let boundary = http::new_boundary();
        let body = http::multipart_body(
            &boundary,
            &[FormFile {
                field: &field,
                filename,
                content_type,
                bytes: audio,
            }],
            &fields,
        );
        let response = self.send(
            name,
            request.method.as_deref().unwrap_or("POST"),
            &path,
            vec![(
                "Content-Type".into(),
                format!("multipart/form-data; boundary={boundary}"),
            )],
            Some(&body),
            CHAT_TIMEOUT,
        )?;
        let response = http::expect_2xx(response, "字幕")?;
        Ok(response.text()?.to_string())
    }

    pub fn image(&mut self, prompt: &str, extra: Map<String, Value>) -> Result<Vec<u8>, CoreError> {
        let name = "生图";
        let cap = self.contract.capability(name)?.clone();
        let request = cap
            .request
            .as_ref()
            .ok_or_else(|| CoreError::Contract(format!("{name}: 没有请求描述")))?
            .clone();
        let path = request
            .path
            .clone()
            .ok_or_else(|| CoreError::Contract(format!("{name}: 没有请求路径")))?;
        let mut body = extra;
        body.insert("prompt".into(), json!(prompt));
        let bytes = serde_json::to_vec(&Value::Object(body))
            .map_err(|e| CoreError::Json(format!("序列化生图请求失败: {e}")))?;
        let response = self.send(
            name,
            request.method.as_deref().unwrap_or("POST"),
            &path,
            vec![("Content-Type".into(), "application/json".into())],
            Some(&bytes),
            IMAGE_TIMEOUT,
        )?;
        let expect = request.expect_status.unwrap_or(200);
        if response.status != expect {
            return Err(CoreError::Protocol(format!(
                "生图: 上游回 {}，合同写的是 {expect}",
                response.status
            )));
        }
        let mut value = response.json()?;
        if let Some(found) = find_b64(&value) {
            return base64::decode(found);
        }
        let poll_field = request.poll.clone().unwrap_or_else(|| "poll_url".into());
        let poll_url = value
            .get(&poll_field)
            .and_then(Value::as_str)
            .ok_or_else(|| {
                CoreError::Protocol(format!(
                    "生图: 响应里既没有图也没有 {poll_field}: {}",
                    truncate(&value.to_string(), 200)
                ))
            })?
            .to_string();
        let poll_path = loopback_path(&poll_url, &cap)?;
        let bind = cap.bind.clone().unwrap_or_else(|| "127.0.0.1".into());
        let port = self.ensure_running(name)?;
        let deadline = Instant::now() + IMAGE_TIMEOUT;
        loop {
            std::thread::sleep(Duration::from_millis(500));
            if Instant::now() >= deadline {
                return Err(CoreError::Protocol(format!(
                    "生图: 轮询 {poll_path} 超时（{:?}）",
                    IMAGE_TIMEOUT
                )));
            }
            let headers = self
                .auth_header(name)?
                .map(|h| vec![h])
                .unwrap_or_default();
            let r = http::request(
                &bind,
                port,
                "GET",
                &poll_path,
                &headers,
                None,
                Duration::from_secs(30),
            )?;
            let r = http::expect_2xx(r, "生图轮询")?;
            value = r.json()?;
            if let Some(found) = find_b64(&value) {
                if let Some(r) = self.running.get_mut(name) {
                    r.touch();
                }
                return base64::decode(found);
            }
            if let Some(status) = value.get("status").and_then(Value::as_str) {
                if matches!(status, "failed" | "error" | "cancelled") {
                    return Err(CoreError::Protocol(format!(
                        "生图: 任务 {status} —— {}",
                        truncate(&value.to_string(), 300)
                    )));
                }
            }
        }
    }

    /// OCR 没有服务形态：一次任务一个进程。载荷清单缺 OCR 时必须拒绝，
    /// 不能"先跑起来再说"—— 那会把第三条验收变成一句口号。
    pub fn ocr(&mut self, req: &OcrRequest<'_>) -> Result<Vec<PathBuf>, CoreError> {
        let name = "文字识别";
        let cap = self.contract.capability(name)?.clone();
        if cap.mode != "subprocess" {
            return Err(CoreError::Contract(format!("{name}: 合同说它不是子进程")));
        }
        let cell = cap
            .os
            .get(self.platform)
            .ok_or_else(|| CoreError::Platform(format!("{name}: 合同里没有 {}", self.platform)))?
            .clone();
        self.verify_payload(&cap, &cell)?;
        for input in [req.image, req.det, req.rec, req.dict] {
            if !input.is_file() {
                return Err(CoreError::Manifest(format!(
                    "{}: 输入不在盘上: {}",
                    cap.engine,
                    input.display()
                )));
            }
        }
        fs::create_dir_all(req.out_dir)?;
        let mut extra = BTreeMap::new();
        for (key, path) in [
            ("det", req.det),
            ("rec", req.rec),
            ("dict", req.dict),
            ("image", req.image),
            ("out_dir", req.out_dir),
        ] {
            extra.insert(key.to_string(), path.to_string_lossy().into_owned());
        }
        extra.insert("device".into(), self.device.clone());
        let plan = render(
            &cap,
            &cell,
            &LaunchContext {
                data_root: &self.data_root,
                tier: &self.tier,
                port: 0,
                token: None,
                extra: &extra,
            },
        )?;
        let (mut child, log_path) = self.spawn(&cap, &plan)?;
        wait_child(&mut child, OCR_TIMEOUT).map_err(|e| {
            CoreError::Spawn(format!(
                "{}: {e} —— 日志尾巴: {}",
                cap.engine,
                log_tail(&log_path, 2048)
            ))
        })?;
        let mut produced = Vec::new();
        for entry in fs::read_dir(req.out_dir)? {
            let entry = entry?;
            if entry.file_type()?.is_file() {
                produced.push(entry.path());
            }
        }
        produced.sort();
        Ok(produced)
    }

    pub fn touch(&mut self, name: &str) {
        if let Some(r) = self.running.get_mut(name) {
            r.touch();
        }
    }

    pub fn reap_idle(&mut self) {
        let now = Instant::now();
        let expired: Vec<String> = self
            .running
            .iter()
            .filter(|(_, r)| idle_expired(r.idle, r.last_used, now))
            .map(|(name, _)| name.clone())
            .collect();
        for name in expired {
            self.stop(&name);
        }
    }

    pub fn stop(&mut self, name: &str) {
        if let Some(mut r) = self.running.remove(name) {
            let _ = r.child.kill();
            let _ = r.child.wait();
        }
    }

    pub fn stop_all(&mut self) {
        let names: Vec<String> = self.running.keys().cloned().collect();
        for name in names {
            self.stop(&name);
        }
    }
}

impl Drop for Supervisor {
    fn drop(&mut self) {
        self.stop_all();
    }
}

fn wait_child(child: &mut Child, timeout: Duration) -> Result<ExitStatus, CoreError> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(status) = child.try_wait()? {
            if status.success() {
                return Ok(status);
            }
            return Err(CoreError::Spawn(format!("子进程退出码不是 0: {status}")));
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(CoreError::Spawn(format!("子进程 {timeout:?} 内没结束")));
        }
        std::thread::sleep(Duration::from_millis(100));
    }
}

fn truncate(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

fn find_b64(value: &Value) -> Option<&str> {
    value
        .pointer("/result/images/0/b64_json")
        .or_else(|| value.pointer("/images/0/b64_json"))
        .and_then(Value::as_str)
}

/// 外壳只追自己那台引擎给的轮询地址：别的绝对 URL 一律拒绝。
fn loopback_path(url: &str, cap: &Capability) -> Result<String, CoreError> {
    if url.starts_with('/') {
        return Ok(url.to_string());
    }
    let bind = cap.bind.clone().unwrap_or_else(|| "127.0.0.1".into());
    for prefix in [format!("http://{bind}:"), "http://localhost:".to_string()] {
        if let Some(rest) = url.strip_prefix(&prefix) {
            if let Some((_port, path)) = rest.split_once('/') {
                return Ok(format!("/{path}"));
            }
        }
    }
    Err(CoreError::Protocol(format!(
        "生图: 轮询地址 {url:?} 不是本机引擎的相对路径"
    )))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn embedded() -> Contract {
        Contract::embedded().unwrap()
    }

    fn ctx(root: &Path) -> LaunchContext<'_> {
        LaunchContext {
            data_root: root,
            tier: "8GB",
            port: 51234,
            token: Some("deadbeef"),
            extra: &BTreeMap::new(),
        }
    }

    #[test]
    fn llama_row_gets_model_port_and_token_from_the_paths() {
        let c = embedded();
        let cap = c.capability("对话").unwrap();
        let cell = cap.os.get("win-x64").unwrap();
        let root = Path::new("/data");
        let plan = render(cap, cell, &ctx(root)).unwrap();
        assert!(plan.program.ends_with("llama-server.exe"), "{:?}", plan.program);
        assert!(plan.args.contains(&"51234".to_string()));
        assert!(plan.args.contains(&"deadbeef".to_string()));
        let want = models_dir(root).join("Qwen3-8B-Q4_K_M.gguf");
        assert!(
            plan.args.iter().any(|a| Path::new(a) == want),
            "{:?} 里没有 {:?}",
            plan.args,
            want
        );
        assert!(plan.args.contains(&"127.0.0.1".to_string()));
        assert!(plan.inputs.iter().any(|p| p.ends_with("Qwen3-8B-Q4_K_M.gguf")));
        assert!(!plan.args.iter().any(|a| a.contains('{')), "{:?}", plan.args);
    }

    #[test]
    fn keyless_rows_never_get_a_token() {
        let c = embedded();
        let cap = c.capability("字幕").unwrap();
        let cell = cap.os.get("win-x64").unwrap();
        let root = Path::new("/data");
        let plan = render(cap, cell, &ctx(root)).unwrap();
        assert!(plan.args.iter().all(|a| a != "deadbeef"), "{:?}", plan.args);
        let want = models_dir(root).join("ggml-large-v1.bin");
        assert!(
            plan.args.iter().any(|a| Path::new(a) == want),
            "{:?} 里没有 {:?}",
            plan.args,
            want
        );
    }

    #[test]
    fn a_token_placeholder_on_a_keyless_engine_is_refused() {
        let c = embedded();
        let mut cap = c.capability("字幕").unwrap().clone();
        cap.os
            .get_mut("win-x64")
            .unwrap()
            .argv_by_tier
            .get_mut("8GB")
            .unwrap()
            .push("{token}".into());
        let cell = cap.os.get("win-x64").unwrap();
        let err = render(
            &cap,
            cell,
            &LaunchContext {
                data_root: Path::new("/data"),
                tier: "8GB",
                port: 1234,
                token: None,
                extra: &BTreeMap::new(),
            },
        )
        .unwrap_err()
        .to_string();
        assert!(err.contains("没有钥匙"), "{err}");
    }

    #[test]
    fn the_16gb_sd_row_resolves_all_companions() {
        let c = embedded();
        let cap = c.capability("生图").unwrap();
        let cell = cap.os.get("win-x64").unwrap();
        let mut context = ctx(Path::new("/data"));
        context.tier = "16GB";
        let plan = render(cap, cell, &context).unwrap();
        for name in ["flux1-schnell-Q5_0.gguf", "t5xxl-Q2_K.gguf", "clip_l-Q8_0.gguf", "ae-f16.gguf"] {
            assert!(
                plan.args.iter().any(|a| a.ends_with(name)),
                "{name} 没进命令行: {:?}",
                plan.args
            );
        }
        assert_eq!(plan.inputs.len(), 4);
    }

    #[test]
    fn ocr_requires_every_subprocess_value() {
        let c = embedded();
        let cap = c.capability("文字识别").unwrap();
        let cell = cap.os.get("win-x64").unwrap();
        let root = Path::new("/data");
        let err = render(
            cap,
            cell,
            &LaunchContext {
                data_root: root,
                tier: "8GB",
                port: 0,
                token: None,
                extra: &BTreeMap::new(),
            },
        )
        .unwrap_err()
        .to_string();
        assert!(err.contains("{det}") || err.contains("{image}"), "{err}");

        let mut extra = BTreeMap::new();
        for (k, v) in [
            ("det", "/w/det.onnx"),
            ("rec", "/w/rec.onnx"),
            ("dict", "/w/dict.txt"),
            ("image", "/tmp/a.png"),
            ("out_dir", "/tmp/out"),
            ("device", "auto"),
        ] {
            extra.insert(k.to_string(), v.to_string());
        }
        let plan = render(
            cap,
            cell,
            &LaunchContext {
                data_root: root,
                tier: "8GB",
                port: 0,
                token: None,
                extra: &extra,
            },
        )
        .unwrap();
        assert!(plan.program.ends_with("oar.exe"), "{:?}", plan.program);
        assert!(plan.args.contains(&"/tmp/a.png".to_string()));
        assert!(!plan.args.iter().any(|a| a.contains('{')), "{:?}", plan.args);
    }

    #[test]
    fn an_unresolved_brace_is_an_error_not_a_literal_argument() {
        let c = embedded();
        let mut cap = c.capability("文字识别").unwrap().clone();
        cap.os.get_mut("win-x64").unwrap().argv = Some(vec![
            "{bin}".into(),
            "{not-supplied}".into(),
        ]);
        let cell = cap.os.get("win-x64").unwrap();
        let err = render(
            &cap,
            cell,
            &LaunchContext {
                data_root: Path::new("/data"),
                tier: "8GB",
                port: 0,
                token: None,
                extra: &BTreeMap::new(),
            },
        )
        .unwrap_err()
        .to_string();
        assert!(err.contains("not-supplied"), "{err}");
    }

    #[test]
    fn idle_expiry_only_applies_to_on_demand() {
        let now = Instant::now();
        assert!(idle_expired(Some(Duration::from_secs(300)), now - Duration::from_secs(301), now));
        assert!(!idle_expired(Some(Duration::from_secs(300)), now - Duration::from_secs(299), now));
        assert!(!idle_expired(None, now - Duration::from_secs(10_000), now));
    }

    #[test]
    fn an_unknown_tier_is_refused_at_construction() {
        let err = Supervisor::new(embedded(), "win-x64", "6GB", PathBuf::from("/data"), "auto")
            .err()
            .expect("6GB 不在合同的档位里")
            .to_string();
        assert!(err.contains("6GB"), "{err}");
    }

    #[test]
    fn status_reports_a_capability_the_platform_has_no_build_for() {
        let mut s = Supervisor::new(embedded(), "macos-arm64", "8GB", PathBuf::from("/data"), "auto")
            .unwrap();
        let rows = s.status();
        let sub = rows.iter().find(|r| r.capability == "字幕").unwrap();
        let chat = rows.iter().find(|r| r.capability == "对话").unwrap();
        assert!(!sub.available, "macOS 的合同里没有字幕这一格");
        assert!(chat.available, "macOS 的合同里有对话这一格");
    }

    #[test]
    fn ocr_cannot_be_started_as_a_server() {
        let mut s = Supervisor::new(embedded(), "win-x64", "8GB", PathBuf::from("/data"), "auto").unwrap();
        let err = s.ensure_running("文字识别").unwrap_err().to_string();
        assert!(err.contains("没有服务形态"), "{err}");
    }

    #[test]
    fn a_missing_manifest_refuses_to_start_anything() {
        let dir = std::env::temp_dir().join(format!("lasc-sup-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let mut s = Supervisor::new(embedded(), "win-x64", "8GB", dir.clone(), "auto").unwrap();
        let err = s.ensure_running("对话").unwrap_err().to_string();
        assert!(err.contains("载荷清单"), "{err}");
        let err = s
            .ocr(&OcrRequest {
                image: Path::new("/tmp/a.png"),
                out_dir: &dir.join("out"),
                det: Path::new("/tmp/det.onnx"),
                rec: Path::new("/tmp/rec.onnx"),
                dict: Path::new("/tmp/dict.txt"),
            })
            .unwrap_err()
            .to_string();
        assert!(err.contains("载荷清单") && err.contains("ocr-manifest"), "{err}");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn loopback_poll_urls_only() {
        let c = embedded();
        let cap = c.capability("生图").unwrap();
        assert_eq!(loopback_path("/sdcpp/v1/img_gen/q/1", cap).unwrap(), "/sdcpp/v1/img_gen/q/1");
        assert_eq!(
            loopback_path("http://127.0.0.1:8080/x/y", cap).unwrap(),
            "/x/y"
        );
        assert!(loopback_path("http://evil.example/x", cap).is_err());
    }

    #[test]
    fn image_b64_is_found_in_either_shape() {
        let a = json!({"result": {"images": [{"b64_json": "AA=="}]}});
        let b = json!({"images": [{"b64_json": "AA=="}]});
        assert_eq!(find_b64(&a), Some("AA=="));
        assert_eq!(find_b64(&b), Some("AA=="));
        assert_eq!(find_b64(&json!({"status": "queued"})), None);
    }

    #[test]
    fn a_port_is_allocated_and_released() {
        let p = allocate_port("127.0.0.1").unwrap();
        assert!(p > 0);
        TcpListener::bind(("127.0.0.1", p)).expect("取端口后应立刻释放");
    }
}
