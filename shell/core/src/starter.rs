//! 起始档权重：一张提交在仓库里的 pin，复用 `download.rs` 的 Range 续传、
//! 换源与 sha256 校验。运行时只认 `data_root/models/<file>` 这一只文件。
//!
//! 与 `payload_fetch.rs` 的分工：那边取"交付对象"（引擎文件，键带目录层级）；
//! 这里取一只扁平权重，落到 `models/`，随后由 `supervisor` 替换命令行里唯一的
//! 权重占位符。下载器本身不联网测试，CI 车道负责真下字节。

use std::fs;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::download::{download, Fetcher, Outcome, Policy, Source, Spec, Store};
use crate::error::CoreError;
use crate::manifest::sha256_file;

pub const STARTER_JSON: &str = include_str!("../../../pins/starter.json");

#[derive(Debug, Clone, Deserialize)]
pub struct StarterPin {
    pub schema: u32,
    pub model: StarterModel,
    #[serde(default)]
    pub fit: StarterFit,
}

#[derive(Debug, Clone, Deserialize)]
pub struct StarterModel {
    pub id: String,
    pub revision: String,
    pub file: String,
    pub bytes: u64,
    pub sha256: String,
    pub license: String,
    #[serde(default)]
    pub base_model: Option<String>,
    #[serde(default)]
    pub notes: Vec<String>,
    #[serde(default)]
    pub urls: Vec<String>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct StarterFit {
    #[serde(default)]
    pub ram_mib_min: u64,
    #[serde(default)]
    pub disk_mib: u64,
    #[serde(default)]
    pub context: u64,
}

fn is_hex(text: &str, len: usize) -> bool {
    text.len() == len && text.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

fn safe_file(name: &str) -> bool {
    !name.is_empty()
        && name != "."
        && name != ".."
        && !name.contains('/')
        && !name.contains('\\')
        && !name.bytes().any(|b| b < 0x20)
        && name.ends_with(".gguf")
}

impl StarterPin {
    pub fn parse(text: &str) -> Result<Self, CoreError> {
        let pin: Self = serde_json::from_str(text)
            .map_err(|e| CoreError::Json(format!("起始档 pin 不是合法 JSON: {e}")))?;
        pin.validate()?;
        Ok(pin)
    }

    pub fn embedded() -> Result<Self, CoreError> {
        Self::parse(STARTER_JSON)
    }

    pub fn validate(&self) -> Result<(), CoreError> {
        let mut bad: Vec<String> = Vec::new();
        if self.schema != 1 {
            bad.push(format!("schema 是 {}，只认 1", self.schema));
        }
        let m = &self.model;
        if !safe_file(&m.file) {
            bad.push("文件名必须是单层 .gguf".into());
        }
        if !is_hex(&m.revision, 40) {
            bad.push("revision 必须是 40 位小写十六进制（不许 main/latest）".into());
        }
        if !is_hex(&m.sha256, 64) {
            bad.push("sha256 必须是 64 位小写十六进制".into());
        }
        if m.bytes == 0 {
            bad.push("字节数必须 > 0".into());
        }
        if m.license.trim().is_empty() {
            bad.push("许可证不能为空".into());
        }
        if !m.id.contains('/') || m.id.chars().any(|c| c.is_whitespace()) {
            bad.push("模型 id 必须是 owner/name".into());
        }
        if m.urls.is_empty() {
            bad.push("没有候选下载地址".into());
        }
        for url in &m.urls {
            if !url.starts_with("https://") {
                bad.push(format!("候选地址不是 https: {url}"));
            }
            if !url.contains(&m.revision) {
                bad.push(format!("候选地址里没有钉住的 revision: {url}"));
            }
            if !url.ends_with(&format!("/{}", m.file)) {
                bad.push(format!("候选地址末尾不是钉住的文件名: {url}"));
            }
        }
        if self.fit.disk_mib > 0 && self.fit.disk_mib.saturating_mul(1024 * 1024) < m.bytes {
            bad.push(format!(
                "磁盘预算 {} MiB 装不下 {} 字节",
                self.fit.disk_mib, m.bytes
            ));
        }
        if bad.is_empty() {
            Ok(())
        } else {
            Err(CoreError::Contract(bad.join("; ")))
        }
    }

    pub fn spec(&self) -> Spec {
        Spec {
            file: self.model.file.clone(),
            bytes: self.model.bytes,
            sha256: self.model.sha256.clone(),
        }
    }

    /// 候选顺序就是 pin 里的顺序；吞吐未实测，`download()` 的稳定排序会保留它。
    pub fn sources(&self) -> Vec<Source> {
        self.model
            .urls
            .iter()
            .enumerate()
            .map(|(i, url)| Source {
                name: format!("候选 {}", i + 1),
                url: url.clone(),
                measured_mib_s: 0.0,
            })
            .collect()
    }

    pub fn size_mib(&self) -> u64 {
        self.model.bytes.div_ceil(1024 * 1024)
    }

    pub fn model_path(&self, data_root: &Path) -> PathBuf {
        data_root.join("models").join(&self.model.file)
    }
}

/// 幂等：目标文件哈希对得上就返回 `AlreadyPresent`，不联网。
pub fn install(
    pin: &StarterPin,
    data_root: &Path,
    fetcher: &mut dyn Fetcher,
    policy: Policy,
) -> Result<(PathBuf, Outcome), CoreError> {
    let models = data_root.join("models");
    fs::create_dir_all(&models)?;
    let store = Store::new(models);
    let outcome = download(&pin.spec(), &pin.sources(), &store, fetcher, policy)?;
    Ok((pin.model_path(data_root), outcome))
}

/// 启动时只看"在不在、够不够长"；哈希在下载那一步已经验过，状态轮询里不再重算。
pub fn installed_path(pin: &StarterPin, data_root: &Path) -> Option<PathBuf> {
    let path = pin.model_path(data_root);
    match fs::metadata(&path) {
        Ok(meta) if meta.is_file() && meta.len() == pin.model.bytes => Some(path),
        _ => None,
    }
}

/// 给下载完成后的一次复核用：长度 + sha256，两个都对才算数。
pub fn verify(path: &Path, bytes: u64, sha256: &str) -> Result<(), CoreError> {
    let meta = fs::metadata(path)?;
    if meta.len() != bytes {
        return Err(CoreError::Download(format!(
            "{}: 体积 {} ≠ 期望 {bytes}",
            path.display(),
            meta.len()
        )));
    }
    let got = sha256_file(path)?;
    if got != sha256 {
        return Err(CoreError::Download(format!(
            "{}: sha256 {got} ≠ 期望 {sha256}",
            path.display()
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            static N: AtomicU64 = AtomicU64::new(0);
            let path = std::env::temp_dir().join(format!(
                "las-starter-{}-{}-{}",
                std::process::id(),
                tag,
                N.fetch_add(1, Ordering::SeqCst)
            ));
            fs::create_dir_all(&path).unwrap();
            Self(path)
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

    struct Fake {
        body: Vec<u8>,
        calls: std::cell::Cell<usize>,
    }

    impl Fake {
        fn new(body: &[u8]) -> Self {
            Self {
                body: body.to_vec(),
                calls: std::cell::Cell::new(0),
            }
        }
    }

    impl Fetcher for Fake {
        fn fetch_range(
            &mut self,
            _url: &str,
            _from: u64,
            dest: &Path,
        ) -> Result<crate::download::FetchReply, CoreError> {
            self.calls.set(self.calls.get() + 1);
            fs::write(dest, &self.body)?;
            Ok(crate::download::FetchReply {
                status: 200,
                range_start: None,
                total: Some(self.body.len() as u64),
                bytes: self.body.len() as u64,
            })
        }
    }

    fn pin_for(body: &[u8], file: &str) -> StarterPin {
        use sha2::{Digest, Sha256};
        let sha = crate::hex_encode(Sha256::digest(body).as_slice());
        StarterPin {
            schema: 1,
            model: StarterModel {
                id: "owner/name".into(),
                revision: "a".repeat(40),
                file: file.into(),
                bytes: body.len() as u64,
                sha256: sha,
                license: "apache-2.0".into(),
                base_model: Some("owner/base".into()),
                notes: vec!["test".into()],
                urls: vec![format!(
                    "https://example.invalid/{}/{}",
                    "a".repeat(40),
                    file
                )],
            },
            fit: StarterFit {
                ram_mib_min: 512,
                disk_mib: 1,
                context: 2048,
            },
        }
    }

    #[test]
    fn the_embedded_pin_is_valid() {
        let pin = StarterPin::embedded().unwrap();
        assert_eq!(pin.model.file, "Qwen3.5-0.8B-finetuned-lora-uncensored-Q4_K_M.gguf");
        assert_eq!(pin.model.bytes, 527_495_776);
        assert!(pin.model.urls.len() >= 2);
    }

    #[test]
    fn a_floating_revision_is_refused() {
        let mut pin = pin_for(b"hello", "x.gguf");
        pin.model.revision = "main".into();
        assert!(pin.validate().is_err());
    }

    #[test]
    fn a_plain_http_url_is_refused() {
        let mut pin = pin_for(b"hello", "x.gguf");
        pin.model.urls[0] = pin.model.urls[0].replace("https://", "http://");
        assert!(pin.validate().is_err());
    }

    #[test]
    fn a_path_traversal_file_is_refused() {
        let mut pin = pin_for(b"hello", "x.gguf");
        pin.model.file = "../x.gguf".into();
        assert!(pin.validate().is_err());
    }

    #[test]
    fn install_writes_and_verifies_then_is_idempotent() {
        let root = TempDir::new("install");
        let pin = pin_for(b"hello starter", "x.gguf");
        let mut fetcher = Fake::new(b"hello starter");
        let (path, outcome) = install(&pin, root.path(), &mut fetcher, Policy::default()).unwrap();
        assert_eq!(outcome, Outcome::Downloaded {
            source: "候选 1".into(),
            resumed_from: 0,
            bytes: 13,
        });
        assert_eq!(fs::read(&path).unwrap(), b"hello starter");
        assert_eq!(fetcher.calls.get(), 1);
        assert_eq!(installed_path(&pin, root.path()), Some(path.clone()));

        let mut again = Fake::new(b"should not be called");
        let (_, outcome) = install(&pin, root.path(), &mut again, Policy::default()).unwrap();
        assert_eq!(outcome, Outcome::AlreadyPresent);
        assert_eq!(again.calls.get(), 0);
        verify(&path, pin.model.bytes, &pin.model.sha256).unwrap();
    }

    #[test]
    fn a_wrong_hash_never_becomes_the_target_file() {
        let root = TempDir::new("wrong");
        let pin = pin_for(b"good bytes", "x.gguf");
        let mut fetcher = Fake::new(b"bad bytes");
        assert!(install(&pin, root.path(), &mut fetcher, Policy::default()).is_err());
        assert!(!pin.model_path(root.path()).exists());
    }
}
