//! 权重下载：候选源按实测吞吐排序、Range 续传、过哈希才落成正式文件。
//!
//! 这一层不实现 TLS，也不把 URL 拼进 shell 命令：`CurlFetcher` 用系统 `curl`
//! （Windows 10+、macOS、Debian 系都自带）把一段响应写进临时文件，参数是一个个
//! 独立的 argv，URL 永远跟在 `--url` 后面。低速与断流由 `--speed-limit` /
//! `--speed-time` 交给 curl 判；决定「续传、换源、还是丢弃」的是本文件的纯逻辑，
//! 所以测试不需要网络。
//!
//! 落盘顺序：`<root>/blobs/<sha>.part` 收字节 → 长度与 sha256 都对 → 改名成
//! `<root>/blobs/<sha>`（按内容寻址）→ 硬链接到 `<root>/<file>`。目标文件在哈希
//! 通过之前不存在；已有的同名但哈希不符的文件会被挪到 `<file>.corrupt[-N]`，
//! 不删除。

use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde::Deserialize;

use crate::error::CoreError;
use crate::manifest::sha256_file;

#[derive(Debug, Clone)]
pub struct Spec {
    pub file: String,
    pub bytes: u64,
    pub sha256: String,
}

#[derive(Debug, Clone)]
pub struct Source {
    pub name: String,
    pub url: String,
    /// 国内出口的实测吞吐（MiB/s）。排序只认这个数，不认域名。
    pub measured_mib_s: f64,
}

/// `pins/download-plan.json` 的 Rust 侧形状。计划由 `tools/download_plan.py` 从
/// 预算与三张档位表生成；这里只读 `download()` 要的四个字段，多出来的行字段
/// （tiers / known_absent / route）由 serde 忽略，不在一处再抄一遍。
#[derive(Debug, Clone, Deserialize)]
pub struct Plan {
    pub schema: u32,
    pub items: Vec<PlanItem>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PlanItem {
    pub file: String,
    pub bytes: u64,
    pub sha256: String,
    pub kind: String,
    #[serde(default)]
    pub repo: Option<String>,
    #[serde(default)]
    pub default_install: bool,
    pub sources: Vec<PlanSource>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct PlanSource {
    pub name: String,
    pub url: String,
    pub measured_mib_s: f64,
}

impl Plan {
    pub fn parse(text: &str) -> Result<Self, CoreError> {
        serde_json::from_str(text)
            .map_err(|e| CoreError::Download(format!("下载计划不是合法 JSON: {e}")))
    }

    pub fn from_file(path: &Path) -> Result<Self, CoreError> {
        let text = fs::read_to_string(path).map_err(|e| {
            CoreError::Download(format!("读不到下载计划 {}: {e}", path.display()))
        })?;
        Self::parse(&text)
    }

    /// 按文件名取一颗；同名出现多次就是计划有歧义，不许挑第一条。
    pub fn item(&self, file: &str) -> Result<&PlanItem, CoreError> {
        let hits: Vec<&PlanItem> = self.items.iter().filter(|x| x.file == file).collect();
        match hits.as_slice() {
            [one] => Ok(one),
            [] => Err(CoreError::Download(format!("下载计划里没有 {file}"))),
            many => Err(CoreError::Download(format!(
                "下载计划里 {file} 有 {} 条，无法判断是哪一颗",
                many.len()
            ))),
        }
    }
}

impl PlanItem {
    pub fn spec(&self) -> Spec {
        Spec {
            file: self.file.clone(),
            bytes: self.bytes,
            sha256: self.sha256.clone(),
        }
    }

    pub fn sources(&self) -> Vec<Source> {
        self.sources
            .iter()
            .map(|s| Source {
                name: s.name.clone(),
                url: s.url.clone(),
                measured_mib_s: s.measured_mib_s,
            })
            .collect()
    }
}

#[derive(Debug, Clone)]
pub struct Store {
    pub root: PathBuf,
}

impl Store {
    pub fn new(root: PathBuf) -> Self {
        Self { root }
    }
}

#[derive(Debug, Clone, Copy)]
pub struct Policy {
    pub attempts_per_source: u32,
}

impl Default for Policy {
    fn default() -> Self {
        Self {
            attempts_per_source: 2,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    AlreadyPresent,
    Downloaded {
        source: String,
        resumed_from: u64,
        bytes: u64,
    },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FetchReply {
    pub status: u16,
    /// 206 的 `Content-Range` 起点；200/416 为 None。
    pub range_start: Option<u64>,
    /// `Content-Range` 里的总长；没有该头时为 None。
    pub total: Option<u64>,
    /// 这次真正写进临时文件的字节数。
    pub bytes: u64,
}

pub trait Fetcher {
    fn fetch_range(&mut self, url: &str, from: u64, dest: &Path) -> Result<FetchReply, CoreError>;
}

pub struct CurlFetcher {
    pub program: String,
}

impl Default for CurlFetcher {
    fn default() -> Self {
        Self {
            program: "curl".to_string(),
        }
    }
}

impl CurlFetcher {
    pub fn new() -> Self {
        Self::default()
    }
}

impl Fetcher for CurlFetcher {
    fn fetch_range(&mut self, url: &str, from: u64, dest: &Path) -> Result<FetchReply, CoreError> {
        let header = header_path(dest);
        let _ = fs::remove_file(dest);
        let _ = fs::remove_file(&header);
        let mut cmd = Command::new(&self.program);
        cmd.args(curl_args(url, from, dest, &header));
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            let _ = cmd.creation_flags(0x0800_0000);
        }
        let out = cmd.output().map_err(|e| {
            CoreError::Download(format!(
                "跑不了 {}（Windows 10+/macOS/Debian 系自带 curl）: {e}",
                self.program
            ))
        })?;
        let text = fs::read_to_string(&header).unwrap_or_default();
        let _ = fs::remove_file(&header);
        let code = out.status.code().unwrap_or(-1);
        let (status, range_start, total) = parse_headers(&text);
        if status == 0 {
            let err = String::from_utf8_lossy(&out.stderr);
            return Err(CoreError::Download(format!(
                "curl 退出码 {code}，没有读到响应头：{}",
                err.trim()
            )));
        }
        Ok(FetchReply {
            status,
            range_start,
            total,
            bytes: len_if_file(dest).unwrap_or(0),
        })
    }
}

fn curl_args(url: &str, from: u64, dest: &Path, header: &Path) -> Vec<String> {
    let mut args = [
        "--location",
        "--fail",
        "--silent",
        "--show-error",
        "--connect-timeout",
        "15",
        // 低于 64 KiB/s 持续 30 秒就放弃这个源；已收到的字节仍留在 .part 里。
        "--speed-limit",
        "65536",
        "--speed-time",
        "30",
        "--proto",
        "=https,http",
        "--proto-redir",
        "=https",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect::<Vec<String>>();
    if from > 0 {
        args.push("--range".to_string());
        args.push(format!("bytes={from}-"));
    }
    args.push("--dump-header".to_string());
    args.push(header.to_string_lossy().into_owned());
    args.push("--output".to_string());
    args.push(dest.to_string_lossy().into_owned());
    args.push("--url".to_string());
    args.push(url.to_string());
    args
}

fn header_path(dest: &Path) -> PathBuf {
    let mut s = dest.as_os_str().to_os_string();
    s.push(".hdr");
    PathBuf::from(s)
}

/// 取最后一组响应头（`--location` 会留下多组）；`HTTP/2 200` 也认。
fn parse_headers(text: &str) -> (u16, Option<u64>, Option<u64>) {
    let mut status = 0u16;
    let mut start = None;
    let mut total = None;
    for raw in text.lines() {
        let line = raw.trim_end_matches('\r');
        if line.starts_with("HTTP/") {
            status = line
                .split_whitespace()
                .nth(1)
                .and_then(|s| s.parse().ok())
                .unwrap_or(0);
            start = None;
            total = None;
            continue;
        }
        if let Some((name, value)) = line.split_once(':') {
            if !name.trim().eq_ignore_ascii_case("content-range") {
                continue;
            }
            let v = value.trim();
            let v = v
                .strip_prefix("bytes ")
                .or_else(|| v.strip_prefix("bytes="))
                .unwrap_or(v);
            if let Some((range, tot)) = v.split_once('/') {
                total = tot.trim().parse::<u64>().ok();
                let range = range.trim();
                if range != "*" {
                    start = range
                        .split_once('-')
                        .and_then(|(a, _)| a.trim().parse::<u64>().ok());
                }
            }
        }
    }
    (status, start, total)
}

fn checked_sha(spec: &Spec) -> Result<String, CoreError> {
    if spec.bytes == 0 {
        return Err(CoreError::Download(format!("{}: 期望字节数是 0", spec.file)));
    }
    if spec.sha256.len() != 64 || !spec.sha256.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(CoreError::Download(format!(
            "{}: sha256 不是 64 位十六进制",
            spec.file
        )));
    }
    if !flat_name(&spec.file) {
        return Err(CoreError::Download(format!(
            "{:?} 不是扁平文件名（不许带目录或 ..）",
            spec.file
        )));
    }
    Ok(spec.sha256.to_ascii_lowercase())
}

fn checked_sources(sources: &[Source]) -> Result<(), CoreError> {
    if sources.is_empty() {
        return Err(CoreError::Download("一个候选源都没有".into()));
    }
    for s in sources {
        if s.name.trim().is_empty() {
            return Err(CoreError::Download("候选源没有名字".into()));
        }
        if !s.url.starts_with("https://") {
            return Err(CoreError::Download(format!(
                "{}: 候选源不是 https: {}",
                s.name, s.url
            )));
        }
        if !s.measured_mib_s.is_finite() || s.measured_mib_s < 0.0 {
            return Err(CoreError::Download(format!(
                "{}: 实测吞吐 {:?} 不是非负有限数",
                s.name, s.measured_mib_s
            )));
        }
    }
    Ok(())
}

fn flat_name(name: &str) -> bool {
    !name.is_empty()
        && name != "."
        && name != ".."
        && !name.contains('/')
        && !name.contains('\\')
        && !name.contains('\0')
}

fn len_if_file(path: &Path) -> Option<u64> {
    fs::metadata(path)
        .ok()
        .filter(|m| m.is_file())
        .map(|m| m.len())
}

fn aside_path(target: &Path) -> PathBuf {
    let name = target
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    for n in 0..10_000u32 {
        let cand = if n == 0 {
            format!("{name}.corrupt")
        } else {
            format!("{name}.corrupt-{n}")
        };
        let p = target.with_file_name(cand);
        if !p.exists() {
            return p;
        }
    }
    target.with_file_name(format!("{name}.corrupt-full"))
}

fn append(tmp: &Path, part: &Path) -> Result<(), CoreError> {
    let mut src = fs::File::open(tmp)?;
    let mut dst = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(part)?;
    std::io::copy(&mut src, &mut dst)?;
    dst.flush()?;
    dst.sync_all()?;
    Ok(())
}

fn link_into_place(blob: &Path, target: &Path) -> Result<(), CoreError> {
    if target.exists() {
        return Err(CoreError::Download(format!(
            "目标在链接前出现了，不覆盖: {}",
            target.display()
        )));
    }
    match fs::hard_link(blob, target) {
        Ok(()) => Ok(()),
        Err(_) => {
            // 同一个 Store 根下一般能硬链接；文件系统不支持时把 blob 改名过去。
            if target.exists() {
                return Err(CoreError::Download(format!(
                    "目标在改名落位前出现了，不覆盖: {}",
                    target.display()
                )));
            }
            fs::rename(blob, target).map_err(|e| {
                CoreError::Download(format!("落位到 {} 失败: {e}", target.display()))
            })
        }
    }
}

fn finalize(part: &Path, blob: &Path, target: &Path) -> Result<(), CoreError> {
    if blob.exists() {
        fs::remove_file(blob)?;
    }
    fs::rename(part, blob)
        .map_err(|e| CoreError::Download(format!("把 .part 收进 blobs 失败: {e}")))?;
    link_into_place(blob, target)
}

pub fn download(
    spec: &Spec,
    sources: &[Source],
    store: &Store,
    fetcher: &mut dyn Fetcher,
    policy: Policy,
) -> Result<Outcome, CoreError> {
    let sha = checked_sha(spec)?;
    checked_sources(sources)?;
    if policy.attempts_per_source == 0 {
        return Err(CoreError::Download("每源尝试次数不能是 0".into()));
    }

    let blobs = store.root.join("blobs");
    fs::create_dir_all(&blobs)?;
    let target = store.root.join(&spec.file);
    let blob = blobs.join(&sha);
    let part = blobs.join(format!("{sha}.part"));

    if target.is_dir() {
        return Err(CoreError::Download(format!(
            "{} 是目录，不是文件",
            target.display()
        )));
    }
    if target.is_file() {
        if sha256_file(&target)? == sha {
            return Ok(Outcome::AlreadyPresent);
        }
        let aside = aside_path(&target);
        fs::rename(&target, &aside).map_err(|e| {
            CoreError::Download(format!(
                "把哈希不符的旧文件挪到 {} 失败: {e}",
                aside.display()
            ))
        })?;
    }
    if blob.is_file() {
        if sha256_file(&blob)? == sha {
            link_into_place(&blob, &target)?;
            let _ = fs::remove_file(&part);
            return Ok(Outcome::AlreadyPresent);
        }
        fs::remove_file(&blob)?;
    }
    if let Some(len) = len_if_file(&part) {
        if len > spec.bytes {
            fs::remove_file(&part)?;
        } else if len == spec.bytes {
            if sha256_file(&part)? == sha {
                finalize(&part, &blob, &target)?;
                return Ok(Outcome::AlreadyPresent);
            }
            fs::remove_file(&part)?;
        }
    }

    let mut ordered = sources.to_vec();
    ordered.sort_by(|a, b| {
        b.measured_mib_s
            .partial_cmp(&a.measured_mib_s)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    let mut failures: Vec<String> = Vec::new();
    for (si, source) in ordered.iter().enumerate() {
        for _ in 0..policy.attempts_per_source {
            let have = len_if_file(&part).unwrap_or(0);
            let attempt = blobs.join(format!("{sha}.{si}.attempt"));
            let _ = fs::remove_file(&attempt);
            let reply = match fetcher.fetch_range(&source.url, have, &attempt) {
                Ok(r) => r,
                Err(e) => {
                    let _ = fs::remove_file(&attempt);
                    failures.push(format!("{}: {}", source.name, e));
                    continue;
                }
            };
            if let Some(total) = reply.total {
                if total != spec.bytes {
                    let _ = fs::remove_file(&attempt);
                    failures.push(format!(
                        "{}: 上游总长 {total} ≠ 期望 {}",
                        source.name, spec.bytes
                    ));
                    break;
                }
            }
            let resumed_from;
            match reply.status {
                206 => {
                    if reply.range_start != Some(have) {
                        let _ = fs::remove_file(&attempt);
                        failures.push(format!(
                            "{}: 206 的起点 {:?} ≠ 本地 {have}",
                            source.name, reply.range_start
                        ));
                        break;
                    }
                    append(&attempt, &part)?;
                    resumed_from = have;
                }
                200 => {
                    if reply.bytes > spec.bytes {
                        let _ = fs::remove_file(&attempt);
                        failures.push(format!(
                            "{}: 200 的 {} 字节超过期望 {}",
                            source.name, reply.bytes, spec.bytes
                        ));
                        break;
                    }
                    let _ = fs::remove_file(&part);
                    fs::rename(&attempt, &part).map_err(|e| {
                        CoreError::Download(format!("把完整响应挪成 .part 失败: {e}"))
                    })?;
                    resumed_from = 0;
                }
                416 => {
                    let _ = fs::remove_file(&attempt);
                    failures.push(format!("{}: 416（本地已有 {have} 字节）", source.name));
                    break;
                }
                other => {
                    let _ = fs::remove_file(&attempt);
                    failures.push(format!("{}: HTTP {other}", source.name));
                    continue;
                }
            }
            let _ = fs::remove_file(&attempt);
            let now = len_if_file(&part).unwrap_or(0);
            if now > spec.bytes {
                fs::remove_file(&part)?;
                failures.push(format!(
                    "{}: 落地 {now} 字节超过期望 {}",
                    source.name, spec.bytes
                ));
                break;
            }
            if now == spec.bytes {
                if sha256_file(&part)? == sha {
                    finalize(&part, &blob, &target)?;
                    return Ok(Outcome::Downloaded {
                        source: source.name.clone(),
                        resumed_from,
                        bytes: now,
                    });
                }
                fs::remove_file(&part)?;
                failures.push(format!("{}: 字节齐了但 sha256 不符", source.name));
                break;
            }
        }
    }

    let kept = len_if_file(&part).unwrap_or(0);
    Err(CoreError::Download(format!(
        "{}: {} 个候选源都没下成（已保留 {kept} 字节供续传）：{}",
        spec.file,
        ordered.len(),
        failures.join("；")
    )))
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};
    use std::sync::atomic::{AtomicU64, Ordering};

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            static N: AtomicU64 = AtomicU64::new(0);
            let p = std::env::temp_dir().join(format!(
                "lasd-{}-{}-{}",
                std::process::id(),
                tag,
                N.fetch_add(1, Ordering::SeqCst)
            ));
            fs::create_dir_all(&p).unwrap();
            Self(p)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn digest(body: &[u8]) -> String {
        crate::hex_encode(Sha256::digest(body).as_slice())
    }

    fn spec_for(file: &str, body: &[u8]) -> Spec {
        Spec {
            file: file.to_string(),
            bytes: body.len() as u64,
            sha256: digest(body),
        }
    }

    fn source(name: &str, rate: f64) -> Source {
        Source {
            name: name.to_string(),
            url: format!("https://{name}.example/file"),
            measured_mib_s: rate,
        }
    }

    #[derive(Clone)]
    enum Step {
        Range,
        Full,
        WrongStart,
        WrongTotal,
        Corrupt,
        Status(u16),
        Transport,
        Short(usize),
    }

    struct Fake {
        body: Vec<u8>,
        steps: Vec<Step>,
        calls: Vec<(String, u64)>,
    }

    impl Fake {
        fn new(body: &[u8], steps: Vec<Step>) -> Self {
            Self {
                body: body.to_vec(),
                steps,
                calls: Vec::new(),
            }
        }
    }

    impl Fetcher for Fake {
        fn fetch_range(
            &mut self,
            url: &str,
            from: u64,
            dest: &Path,
        ) -> Result<FetchReply, CoreError> {
            self.calls.push((url.to_string(), from));
            let step = if self.steps.is_empty() {
                Step::Range
            } else {
                self.steps.remove(0)
            };
            let total = self.body.len() as u64;
            let start = from.min(total) as usize;
            let (status, range_start, range_total, bytes) = match step {
                Step::Range => {
                    if from >= total {
                        (416, None, Some(total), 0)
                    } else {
                        fs::write(dest, &self.body[start..])?;
                        (206, Some(from), Some(total), total - from)
                    }
                }
                Step::Full => {
                    fs::write(dest, &self.body)?;
                    (200, None, Some(total), total)
                }
                Step::WrongStart => {
                    fs::write(dest, b"")?;
                    (206, Some(from + 1), Some(total), 0)
                }
                Step::WrongTotal => {
                    fs::write(dest, &self.body[start..])?;
                    (206, Some(from), Some(total + 1), total - from)
                }
                Step::Corrupt => {
                    let junk = vec![0xFFu8; total.saturating_sub(from) as usize];
                    fs::write(dest, &junk)?;
                    let n = junk.len() as u64;
                    (206, Some(from), Some(total), n)
                }
                Step::Status(code) => (code, None, None, 0),
                Step::Transport => {
                    return Err(CoreError::Download("模拟连接失败".into()));
                }
                Step::Short(n) => {
                    let end = (start + n).min(self.body.len());
                    fs::write(dest, &self.body[start..end])?;
                    (206, Some(from), Some(total), (end - start) as u64)
                }
            };
            Ok(FetchReply {
                status,
                range_start,
                total: range_total,
                bytes,
            })
        }
    }

    fn store(d: &TempDir) -> Store {
        Store::new(d.path().to_path_buf())
    }

    #[test]
    fn a_fresh_download_picks_the_fastest_source_and_lands_after_hashing() {
        let d = TempDir::new("fresh");
        let body = b"hello world".to_vec();
        let slow = source("hf-mirror", 0.25);
        let fast = source("modelscope", 4.2);
        let mut f = Fake::new(&body, vec![Step::Range]);
        let out = download(
            &spec_for("a.gguf", &body),
            &[slow, fast.clone()],
            &store(&d),
            &mut f,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(
            out,
            Outcome::Downloaded {
                source: "modelscope".into(),
                resumed_from: 0,
                bytes: 11
            }
        );
        assert_eq!(f.calls[0].0, fast.url);
        assert_eq!(fs::read(d.path().join("a.gguf")).unwrap(), body);
        assert!(d.path().join("blobs").join(digest(&body)).is_file());
    }

    #[test]
    fn an_already_good_target_needs_no_network() {
        let d = TempDir::new("present");
        let body = b"abc".to_vec();
        fs::write(d.path().join("a.bin"), &body).unwrap();
        let mut f = Fake::new(&body, vec![]);
        let out = download(
            &spec_for("a.bin", &body),
            &[source("modelscope", 4.2)],
            &store(&d),
            &mut f,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(out, Outcome::AlreadyPresent);
        assert!(f.calls.is_empty());
    }

    #[test]
    fn a_corrupt_target_is_moved_aside_not_deleted() {
        let d = TempDir::new("aside");
        let body = b"good".to_vec();
        fs::write(d.path().join("a.bin"), b"bad").unwrap();
        let mut f = Fake::new(&body, vec![Step::Range]);
        download(
            &spec_for("a.bin", &body),
            &[source("modelscope", 4.2)],
            &store(&d),
            &mut f,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(fs::read(d.path().join("a.bin")).unwrap(), body);
        assert_eq!(fs::read(d.path().join("a.bin.corrupt")).unwrap(), b"bad");
    }

    #[test]
    fn a_partial_blob_is_resumed_from_its_length() {
        let d = TempDir::new("resume");
        let body = b"0123456789".to_vec();
        fs::create_dir_all(d.path().join("blobs")).unwrap();
        fs::write(
            d.path().join("blobs").join(format!("{}.part", digest(&body))),
            &body[..4],
        )
        .unwrap();
        let mut f = Fake::new(&body, vec![Step::Range]);
        let out = download(
            &spec_for("a.bin", &body),
            &[source("modelscope", 4.2)],
            &store(&d),
            &mut f,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(f.calls[0].1, 4);
        assert_eq!(
            out,
            Outcome::Downloaded {
                source: "modelscope".into(),
                resumed_from: 4,
                bytes: 10
            }
        );
        assert_eq!(fs::read(d.path().join("a.bin")).unwrap(), body);
    }

    #[test]
    fn a_server_that_ignores_range_restarts_from_zero() {
        let d = TempDir::new("ignore");
        let body = b"0123456789".to_vec();
        fs::create_dir_all(d.path().join("blobs")).unwrap();
        fs::write(
            d.path().join("blobs").join(format!("{}.part", digest(&body))),
            &body[..4],
        )
        .unwrap();
        let mut f = Fake::new(&body, vec![Step::Full]);
        let out = download(
            &spec_for("a.bin", &body),
            &[source("modelscope", 4.2)],
            &store(&d),
            &mut f,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(f.calls[0].1, 4);
        assert_eq!(
            out,
            Outcome::Downloaded {
                source: "modelscope".into(),
                resumed_from: 0,
                bytes: 10
            }
        );
        assert_eq!(fs::read(d.path().join("a.bin")).unwrap(), body);
    }

    #[test]
    fn a_wrong_content_range_start_switches_source() {
        let d = TempDir::new("wrongstart");
        let body = b"abcdef".to_vec();
        let fast = source("modelscope", 4.2);
        let slow = source("hf-mirror", 0.25);
        let mut f = Fake::new(&body, vec![Step::WrongStart, Step::Range]);
        let out = download(
            &spec_for("a.bin", &body),
            &[fast.clone(), slow.clone()],
            &store(&d),
            &mut f,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(f.calls.len(), 2);
        assert_eq!(f.calls[0].0, fast.url);
        assert_eq!(f.calls[1].0, slow.url);
        assert_eq!(f.calls[1].1, 0);
        assert_eq!(out, Outcome::Downloaded {
            source: "hf-mirror".into(),
            resumed_from: 0,
            bytes: 6
        });
    }

    #[test]
    fn an_http_error_switches_source_and_keeps_progress() {
        let d = TempDir::new("http");
        let body = b"abcdef".to_vec();
        fs::create_dir_all(d.path().join("blobs")).unwrap();
        fs::write(
            d.path().join("blobs").join(format!("{}.part", digest(&body))),
            &body[..2],
        )
        .unwrap();
        let mut f = Fake::new(&body, vec![Step::Status(404), Step::Range]);
        download(
            &spec_for("a.bin", &body),
            &[source("modelscope", 4.2), source("hf-mirror", 0.25)],
            &store(&d),
            &mut f,
            Policy { attempts_per_source: 1 },
        )
        .unwrap();
        assert_eq!(f.calls.len(), 2);
        assert_eq!(f.calls[1].1, 2);
        assert_eq!(fs::read(d.path().join("a.bin")).unwrap(), body);
    }

    #[test]
    fn a_short_response_is_resumed_on_the_same_source() {
        let d = TempDir::new("short");
        let body = b"0123456789".to_vec();
        let mut f = Fake::new(&body, vec![Step::Short(3), Step::Range]);
        let out = download(
            &spec_for("a.bin", &body),
            &[source("modelscope", 4.2)],
            &store(&d),
            &mut f,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(f.calls.len(), 2);
        assert_eq!(f.calls[0].1, 0);
        assert_eq!(f.calls[1].1, 3);
        assert_eq!(out, Outcome::Downloaded {
            source: "modelscope".into(),
            resumed_from: 3,
            bytes: 10
        });
    }

    #[test]
    fn a_transport_error_is_a_source_failure_not_a_dead_end() {
        let d = TempDir::new("transport");
        let body = b"abcdef".to_vec();
        let mut f = Fake::new(&body, vec![Step::Transport, Step::Range]);
        let out = download(
            &spec_for("a.bin", &body),
            &[source("modelscope", 4.2), source("hf-mirror", 0.25)],
            &store(&d),
            &mut f,
            Policy { attempts_per_source: 1 },
        )
        .unwrap();
        assert_eq!(out, Outcome::Downloaded {
            source: "hf-mirror".into(),
            resumed_from: 0,
            bytes: 6
        });
    }

    #[test]
    fn a_hash_mismatch_drops_the_part_and_restarts_on_the_next_source() {
        let d = TempDir::new("badhash");
        let body = b"abcdef".to_vec();
        let mut f = Fake::new(&body, vec![Step::Corrupt, Step::Range]);
        let out = download(
            &spec_for("a.bin", &body),
            &[source("modelscope", 4.2), source("hf-mirror", 0.25)],
            &store(&d),
            &mut f,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(f.calls[0].1, 0);
        assert_eq!(f.calls[1].1, 0);
        assert_eq!(out, Outcome::Downloaded {
            source: "hf-mirror".into(),
            resumed_from: 0,
            bytes: 6
        });
        assert_eq!(fs::read(d.path().join("a.bin")).unwrap(), body);
    }

    #[test]
    fn all_sources_failing_keeps_the_part_for_the_next_run() {
        let d = TempDir::new("allfail");
        let body = b"0123456789".to_vec();
        fs::create_dir_all(d.path().join("blobs")).unwrap();
        fs::write(
            d.path().join("blobs").join(format!("{}.part", digest(&body))),
            &body[..3],
        )
        .unwrap();
        let mut f = Fake::new(&body, vec![Step::Transport, Step::Transport]);
        let err = download(
            &spec_for("a.bin", &body),
            &[source("modelscope", 4.2)],
            &store(&d),
            &mut f,
            Policy::default(),
        )
        .unwrap_err()
        .to_string();
        assert!(err.contains("已保留 3 字节"), "{err}");
        assert!(d
            .path()
            .join("blobs")
            .join(format!("{}.part", digest(&body)))
            .is_file());
        assert!(!d.path().join("a.bin").exists());
    }

    #[test]
    fn a_416_keeps_progress_and_the_next_source_resumes_it() {
        let d = TempDir::new("416");
        let body = b"abcdef".to_vec();
        fs::create_dir_all(d.path().join("blobs")).unwrap();
        fs::write(
            d.path().join("blobs").join(format!("{}.part", digest(&body))),
            &body[..3],
        )
        .unwrap();
        let mut f = Fake::new(&body, vec![Step::Status(416), Step::Range]);
        let out = download(
            &spec_for("a.bin", &body),
            &[source("modelscope", 4.2), source("hf-mirror", 0.25)],
            &store(&d),
            &mut f,
            Policy { attempts_per_source: 1 },
        )
        .unwrap();
        assert_eq!(f.calls.len(), 2);
        assert_eq!(f.calls[1].1, 3);
        assert_eq!(out, Outcome::Downloaded {
            source: "hf-mirror".into(),
            resumed_from: 3,
            bytes: 6
        });
    }

    #[test]
    fn a_content_range_total_that_disagrees_is_not_appended() {
        let d = TempDir::new("total");
        let body = b"abcdef".to_vec();
        let mut f = Fake::new(&body, vec![Step::WrongTotal, Step::Range]);
        download(
            &spec_for("a.bin", &body),
            &[source("modelscope", 4.2), source("hf-mirror", 0.25)],
            &store(&d),
            &mut f,
            Policy { attempts_per_source: 1 },
        )
        .unwrap();
        assert_eq!(f.calls[0].0, "https://modelscope.example/file");
        assert_eq!(f.calls[1].1, 0);
        assert_eq!(fs::read(d.path().join("a.bin")).unwrap(), body);
    }

    #[test]
    fn a_cached_blob_is_linked_without_fetching() {
        let d = TempDir::new("blob");
        let body = b"cached".to_vec();
        fs::create_dir_all(d.path().join("blobs")).unwrap();
        fs::write(d.path().join("blobs").join(digest(&body)), &body).unwrap();
        let mut f = Fake::new(&body, vec![]);
        let out = download(
            &spec_for("a.bin", &body),
            &[source("modelscope", 4.2)],
            &store(&d),
            &mut f,
            Policy::default(),
        )
        .unwrap();
        assert_eq!(out, Outcome::AlreadyPresent);
        assert!(f.calls.is_empty());
        assert_eq!(fs::read(d.path().join("a.bin")).unwrap(), body);
    }

    #[test]
    fn a_path_in_the_file_name_is_refused_before_any_fetch() {
        let d = TempDir::new("path");
        let body = b"x".to_vec();
        let mut spec = spec_for("a.bin", &body);
        spec.file = "../a.bin".into();
        let mut f = Fake::new(&body, vec![]);
        assert!(download(
            &spec,
            &[source("modelscope", 4.2)],
            &store(&d),
            &mut f,
            Policy::default()
        )
        .is_err());
        assert!(f.calls.is_empty());
    }

    #[test]
    fn a_non_https_source_is_refused() {
        let d = TempDir::new("httponly");
        let body = b"x".to_vec();
        let mut s = source("modelscope", 4.2);
        s.url = "http://modelscope.example/file".into();
        let mut f = Fake::new(&body, vec![]);
        assert!(download(
            &spec_for("a.bin", &body),
            &[s],
            &store(&d),
            &mut f,
            Policy::default()
        )
        .is_err());
        assert!(f.calls.is_empty());
    }

    #[test]
    fn the_header_parser_takes_the_last_response_and_both_content_range_forms() {
        let text = "HTTP/1.1 302 Found\r\nLocation: https://a.example/x\r\n\
                    Content-Range: bytes 0-1/2\r\n\r\n\
                    HTTP/2 206\r\ncontent-range: bytes 10-19/100\r\n\r\n";
        assert_eq!(parse_headers(text), (206, Some(10), Some(100)));
        assert_eq!(
            parse_headers("HTTP/1.1 416 Range Not Satisfiable\r\nContent-Range: bytes */100\r\n\r\n"),
            (416, None, Some(100))
        );
        assert_eq!(parse_headers("SSH-2.0-OpenSSH\r\n"), (0, None, None));
    }

    #[test]
    fn curl_arguments_keep_the_url_in_one_slot_and_only_send_range_when_resuming() {
        let dest = PathBuf::from("a.bin");
        let header = PathBuf::from("a.bin.hdr");
        let url = "https://a.example/x.gguf;touch /tmp/pwned";
        let fresh = curl_args(url, 0, &dest, &header);
        let at = fresh.iter().position(|x| x == "--url").unwrap();
        assert_eq!(fresh[at + 1], url);
        assert!(!fresh.iter().any(|x| x.starts_with("bytes=")));
        assert!(fresh.iter().any(|x| x == "65536"));
        assert!(fresh.iter().any(|x| x == "30"));
        let resumed = curl_args(url, 7, &dest, &header);
        assert!(resumed.iter().any(|x| x == "bytes=7-"));
    }

    const PLAN_JSON: &str = r#"{
      "schema": 1,
      "items": [
        {"sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
         "file": "a.gguf", "bytes": 3, "kind": "chat", "repo": "o/r",
         "default_install": true, "tiers": ["4GB"], "route": "hf_metadata",
         "known_absent": [], "single_source": false,
         "sources": [{"name": "modelscope", "url": "https://ms.example/a.gguf",
                      "measured_mib_s": 4.2, "note": "x"},
                     {"name": "hf-mirror.com", "url": "https://hf.example/a.gguf",
                      "measured_mib_s": 0.25}]}
      ]
    }"#;

    #[test]
    fn a_download_plan_parses_and_maps_to_the_fetcher_inputs() {
        let plan = Plan::parse(PLAN_JSON).unwrap();
        let item = plan.item("a.gguf").unwrap();
        assert_eq!(item.kind, "chat");
        assert_eq!(item.spec().bytes, 3);
        assert_eq!(item.spec().sha256, "a".repeat(64));
        let sources = item.sources();
        assert_eq!(sources.len(), 2);
        assert_eq!(sources[0].name, "modelscope");
        assert_eq!(sources[1].measured_mib_s, 0.25);
    }

    #[test]
    fn a_plan_with_a_missing_or_ambiguous_name_is_refused() {
        let plan = Plan::parse(PLAN_JSON).unwrap();
        assert!(plan.item("b.gguf").is_err());
        let dup = Plan::parse(
            r#"{"schema":1,"items":[
                 {"file":"a","bytes":1,"sha256":"a","kind":"chat","sources":[]},
                 {"file":"a","bytes":2,"sha256":"b","kind":"chat","sources":[]}]}"#,
        )
        .unwrap();
        assert!(dup.item("a").is_err());
        assert!(Plan::parse("not json").is_err());
    }
}
