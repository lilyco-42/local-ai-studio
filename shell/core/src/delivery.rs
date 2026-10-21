//! 首启交付：把出包车道写的 `delivery-manifest.json` 落成外壳认的数据根。
//!
//! 这张表是 `tools/payload_intake.py` 的产物：每个文件一行 `(key, dest, bytes,
//! sha256)`，另有一条根清单 `payload-manifest.json`（`land: last`）。这一层只做
//! 纯逻辑：取字节的动作由调用方给（真实是 HTTPS，测试与 CI 重放是本地对象目录），
//! 所以不联网络也能证明映射与校验会红。
//!
//! 三条纪律：
//! ① `dest` 只能是数据根内的相对路径，`key` 必须等于 `<key_prefix>/<dest>`；
//! ② 每个文件先对长度、再对 sha256，全过才写到同目录临时文件后原子替换；
//! ③ 根清单**最后**落 —— 半套 runtime 不会配上一份"看起来完整"的清单。

use std::collections::BTreeSet;
use std::fs;
use std::path::Path;

use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::error::CoreError;
use crate::hex_encode;
use crate::manifest::sha256_file;

pub const MANIFEST_NAME: &str = "payload-manifest.json";

#[derive(Debug, Clone, Deserialize)]
pub struct Delivery {
    pub schema: u32,
    pub os: String,
    pub key_prefix: String,
    pub manifest: DeliveryFile,
    pub files: Vec<DeliveryFile>,
    #[serde(default)]
    pub totals: Option<Totals>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct DeliveryFile {
    pub dest: String,
    pub key: String,
    pub bytes: u64,
    pub sha256: String,
    /// 只有根清单带这个字段；值必须是 `last`。
    #[serde(default)]
    pub land: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Totals {
    pub files: u64,
    pub bytes: u64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Installed {
    pub written: usize,
    pub skipped: usize,
}

impl Delivery {
    pub fn parse(text: &str) -> Result<Self, CoreError> {
        serde_json::from_str(text)
            .map_err(|e| CoreError::Manifest(format!("交付清单不是合法 JSON: {e}")))
    }

    pub fn from_file(path: &Path) -> Result<Self, CoreError> {
        let text = fs::read_to_string(path).map_err(|e| {
            CoreError::Manifest(format!("读不到交付清单 {}: {e}", path.display()))
        })?;
        Self::parse(&text)
    }

    /// 按对象键反查清单里的期望值（取数的调用方用它决定要下多少字节、对哪个哈希）。
    pub fn expected(&self, key: &str) -> Result<(u64, &str), CoreError> {
        self.files
            .iter()
            .chain(std::iter::once(&self.manifest))
            .find(|row| row.key == key)
            .map(|row| (row.bytes, row.sha256.as_str()))
            .ok_or_else(|| CoreError::Manifest(format!("清单里没有对象键 {key}")))
    }

    /// 整张表先验一遍再碰盘：任何一条不合规都不许开始下载。
    pub fn check(&self, platform: &str) -> Result<(), CoreError> {
        if self.schema != 1 {
            return Err(CoreError::Manifest(format!(
                "交付清单 schema 是 {}，只认 1",
                self.schema
            )));
        }
        if self.os != platform {
            return Err(CoreError::Manifest(format!(
                "交付清单是 {} 的，这台机器是 {platform}",
                self.os
            )));
        }
        check_prefix(&self.key_prefix)?;
        if self.files.is_empty() {
            return Err(CoreError::Manifest("交付清单里一个文件都没有".into()));
        }
        if self.manifest.dest != MANIFEST_NAME {
            return Err(CoreError::Manifest(format!(
                "根清单的落点必须是 {MANIFEST_NAME}，清单写的是 {:?}",
                self.manifest.dest
            )));
        }
        if self.manifest.land.as_deref() != Some("last") {
            return Err(CoreError::Manifest(
                "根清单没有标 land=last —— 落盘顺序是它的前提".into(),
            ));
        }
        self.manifest_file()?;

        let mut dests = BTreeSet::new();
        let mut keys = BTreeSet::new();
        let mut bytes = 0u64;
        for row in &self.files {
            check_dest(&row.dest)?;
            if row.dest == MANIFEST_NAME {
                return Err(CoreError::Manifest(format!(
                    "{}: 根清单的名字不许出现在文件表里",
                    row.dest
                )));
            }
            let want_key = format!("{}/{}", self.key_prefix, row.dest);
            if row.key != want_key {
                return Err(CoreError::Manifest(format!(
                    "{}: 键是 {:?}，按前缀应当是 {want_key:?}",
                    row.dest, row.key
                )));
            }
            if row.land.is_some() {
                return Err(CoreError::Manifest(format!(
                    "{}: 只有根清单能带 land",
                    row.dest
                )));
            }
            if !dests.insert(row.dest.clone()) {
                return Err(CoreError::Manifest(format!(
                    "{}: 同一个落点出现两次",
                    row.dest
                )));
            }
            if !keys.insert(row.key.clone()) {
                return Err(CoreError::Manifest(format!(
                    "{}: 同一个键出现两次",
                    row.key
                )));
            }
            checked_sha(&row.sha256)?;
            if row.bytes == 0 {
                return Err(CoreError::Manifest(format!("{}: 字节数是 0", row.dest)));
            }
            bytes += row.bytes;
        }
        if let Some(t) = &self.totals {
            if t.files != self.files.len() as u64 {
                return Err(CoreError::Manifest(format!(
                    "合计写 {} 个文件，表里有 {}",
                    t.files,
                    self.files.len()
                )));
            }
            if t.bytes != bytes {
                return Err(CoreError::Manifest(format!(
                    "合计写 {} 字节，逐行相加是 {bytes}",
                    t.bytes
                )));
            }
        }
        Ok(())
    }

    fn manifest_file(&self) -> Result<(), CoreError> {
        if self.manifest.key != format!("{}/{}", self.key_prefix, self.manifest.dest) {
            return Err(CoreError::Manifest(format!(
                "根清单的键 {:?} 与前缀/落点不符",
                self.manifest.key
            )));
        }
        checked_sha(&self.manifest.sha256)?;
        if self.manifest.bytes == 0 {
            return Err(CoreError::Manifest("根清单的字节数是 0".into()));
        }
        Ok(())
    }
}

/// `fetch` 按清单里的键取字节（真实调用是 HTTPS GET，重放时是对象目录里的同名文件）。
/// 全部校验通过后才写；根清单最后写。
pub fn install<F>(
    delivery: &Delivery,
    platform: &str,
    data_root: &Path,
    mut fetch: F,
) -> Result<Installed, CoreError>
where
    F: FnMut(&str) -> Result<Vec<u8>, CoreError>,
{
    delivery.check(platform)?;
    let mut out = Installed {
        written: 0,
        skipped: 0,
    };
    for row in &delivery.files {
        let target = data_root.join(&row.dest);
        if already_good(&target, &row.sha256, row.bytes)? {
            out.skipped += 1;
            continue;
        }
        let body = fetch(&row.key).map_err(|e| {
            CoreError::Manifest(format!("取 {}（{}）失败: {e}", row.dest, row.key))
        })?;
        verify_body(&row.dest, &body, &row.sha256, row.bytes)?;
        write_atomic(&target, &body)?;
        out.written += 1;
    }
    let root_target = data_root.join(&delivery.manifest.dest);
    let m = &delivery.manifest;
    if !already_good(&root_target, &m.sha256, m.bytes)? {
        let body = fetch(&m.key)
            .map_err(|e| CoreError::Manifest(format!("取根清单（{}）失败: {e}", m.key)))?;
        verify_body(&m.dest, &body, &m.sha256, m.bytes)
            .map_err(|e| CoreError::Manifest(format!("根清单校验失败: {e}")))?;
        write_atomic(&root_target, &body)?;
        out.written += 1;
    } else {
        out.skipped += 1;
    }
    Ok(out)
}

fn already_good(target: &Path, sha: &str, bytes: u64) -> Result<bool, CoreError> {
    if !target.exists() {
        return Ok(false);
    }
    if !target.is_file() {
        return Err(CoreError::Manifest(format!(
            "{} 已经存在但不是普通文件",
            target.display()
        )));
    }
    if target.metadata()?.len() != bytes {
        return Ok(false);
    }
    Ok(sha256_file(target)? == sha.to_ascii_lowercase())
}

fn verify_body(dest: &str, body: &[u8], sha: &str, bytes: u64) -> Result<(), CoreError> {
    if body.len() as u64 != bytes {
        return Err(CoreError::Manifest(format!(
            "{dest}: 清单写 {bytes} 字节，取回的是 {}",
            body.len()
        )));
    }
    let got = hex_encode(Sha256::digest(body).as_slice());
    if got != sha.to_ascii_lowercase() {
        return Err(CoreError::Manifest(format!(
            "{dest}: sha256 与清单不符（取回 {got}）"
        )));
    }
    Ok(())
}

fn write_atomic(target: &Path, body: &[u8]) -> Result<(), CoreError> {
    let parent = target.parent().ok_or_else(|| {
        CoreError::Manifest(format!("{} 没有父目录", target.display()))
    })?;
    fs::create_dir_all(parent)?;
    let name = target
        .file_name()
        .map(|s| s.to_string_lossy().into_owned())
        .unwrap_or_default();
    let part = parent.join(format!(".{name}.part-{}", std::process::id()));
    fs::write(&part, body)
        .map_err(|e| CoreError::Manifest(format!("写 {} 失败: {e}", part.display())))?;
    if let Err(e) = fs::rename(&part, target) {
        let _ = fs::remove_file(&part);
        return Err(CoreError::Manifest(format!(
            "落位 {} 失败: {e}",
            target.display()
        )));
    }
    Ok(())
}

fn check_prefix(prefix: &str) -> Result<(), CoreError> {
    if prefix.is_empty() || prefix.contains('\\') || prefix.contains('\0') {
        return Err(CoreError::Manifest(format!("键前缀 {prefix:?} 不合法")));
    }
    if prefix.starts_with('/') || prefix.ends_with('/') {
        return Err(CoreError::Manifest(format!("键前缀 {prefix:?} 不合法")));
    }
    for part in prefix.split('/') {
        if part.is_empty() || part == "." || part == ".." {
            return Err(CoreError::Manifest(format!(
                "键前缀 {prefix:?} 含不安全的段 {part:?}"
            )));
        }
    }
    Ok(())
}

fn check_dest(dest: &str) -> Result<(), CoreError> {
    if dest.is_empty()
        || dest.contains('\\')
        || dest.contains(':')
        || dest.contains('\0')
        || dest.starts_with('/')
        || dest.ends_with('/')
    {
        return Err(CoreError::Manifest(format!("落点 {dest:?} 不是数据根内的相对路径")));
    }
    for part in dest.split('/') {
        if part.is_empty() || part == "." || part == ".." {
            return Err(CoreError::Manifest(format!(
                "落点 {dest:?} 含不安全的段 {part:?}"
            )));
        }
    }
    Ok(())
}

fn checked_sha(sha: &str) -> Result<(), CoreError> {
    if sha.len() != 64 || !sha.chars().all(|c| c.is_ascii_hexdigit()) {
        return Err(CoreError::Manifest(format!(
            "sha256 {sha:?} 不是 64 位十六进制"
        )));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            static N: AtomicU64 = AtomicU64::new(0);
            let p = std::env::temp_dir().join(format!(
                "lasdel-{}-{}-{}",
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
        hex_encode(Sha256::digest(body).as_slice())
    }

    fn row(dest: &str, body: &[u8]) -> DeliveryFile {
        DeliveryFile {
            dest: dest.into(),
            key: format!("payload/linux-x64/{dest}"),
            bytes: body.len() as u64,
            sha256: digest(body),
            land: None,
        }
    }

    fn sample() -> (Delivery, BTreeMap<String, Vec<u8>>) {
        let files = [
            ("runtime/linux-x64/llama/b9/llama-server", &b"server"[..]),
            ("runtime/linux-x64/llama/b9/libggml.so", &b"ggml"[..]),
            ("runtime/linux-x64/whisper/w1/whisper-cli", &b"whisper"[..]),
        ];
        let mut objects = BTreeMap::new();
        let rows: Vec<DeliveryFile> = files
            .iter()
            .map(|(dest, body)| {
                let r = row(dest, body);
                objects.insert(r.key.clone(), body.to_vec());
                r
            })
            .collect();
        let root = br#"{"os":"linux-x64","engines":[]}"#.to_vec();
        let manifest = DeliveryFile {
            dest: MANIFEST_NAME.into(),
            key: format!("payload/linux-x64/{MANIFEST_NAME}"),
            bytes: root.len() as u64,
            sha256: digest(&root),
            land: Some("last".into()),
        };
        objects.insert(manifest.key.clone(), root);
        let bytes: u64 = rows.iter().map(|r| r.bytes).sum();
        (
            Delivery {
                schema: 1,
                os: "linux-x64".into(),
                key_prefix: "payload/linux-x64".into(),
                manifest,
                totals: Some(Totals {
                    files: rows.len() as u64,
                    bytes,
                }),
                files: rows,
            },
            objects,
        )
    }

    struct Source {
        objects: BTreeMap<String, Vec<u8>>,
        calls: Vec<String>,
        drop_keys: BTreeSet<String>,
    }

    impl Source {
        fn new(objects: BTreeMap<String, Vec<u8>>) -> Self {
            Self {
                objects,
                calls: Vec::new(),
                drop_keys: BTreeSet::new(),
            }
        }

        fn fetch(&mut self, key: &str) -> Result<Vec<u8>, CoreError> {
            self.calls.push(key.to_string());
            if self.drop_keys.contains(key) {
                return Err(CoreError::Manifest("模拟断网".into()));
            }
            self.objects
                .get(key)
                .cloned()
                .ok_or_else(|| CoreError::Manifest(format!("对象目录里没有 {key}")))
        }
    }

    #[test]
    fn expected_looks_up_file_rows_and_the_root_manifest() {
        let (delivery, _) = sample();
        let row = &delivery.files[1];
        assert_eq!(delivery.expected(&row.key).unwrap(), (row.bytes, row.sha256.as_str()));
        let m = &delivery.manifest;
        assert_eq!(delivery.expected(&m.key).unwrap(), (m.bytes, m.sha256.as_str()));
        let err = delivery.expected("payload/linux-x64/nope").unwrap_err().to_string();
        assert!(err.contains("没有对象键"), "{err}");
    }

    #[test]
    fn a_delivery_lands_every_file_and_the_root_manifest_last() {
        let d = TempDir::new("ok");
        let (delivery, objects) = sample();
        let mut src = Source::new(objects);
        let out = install(&delivery, "linux-x64", d.path(), |k| src.fetch(k)).unwrap();
        assert_eq!(out, Installed { written: 4, skipped: 0 });
        assert_eq!(fs::read(d.path().join("runtime/linux-x64/llama/b9/llama-server")).unwrap(), b"server");
        assert!(d.path().join(MANIFEST_NAME).is_file());
        assert_eq!(src.calls.last().unwrap(), &delivery.manifest.key);
        assert_eq!(src.calls.len(), 4);
    }

    #[test]
    fn a_second_install_fetches_nothing() {
        let d = TempDir::new("idem");
        let (delivery, objects) = sample();
        install(&delivery, "linux-x64", d.path(), |k| Source::new(objects.clone()).fetch(k)).unwrap();
        let mut src = Source::new(objects);
        let out = install(&delivery, "linux-x64", d.path(), |k| src.fetch(k)).unwrap();
        assert_eq!(out, Installed { written: 0, skipped: 4 });
        assert!(src.calls.is_empty());
    }

    #[test]
    fn a_tampered_body_is_refused_before_it_is_written() {
        let d = TempDir::new("tamper");
        let (delivery, mut objects) = sample();
        let key = delivery.files[0].key.clone();
        objects.insert(key, b"Server".to_vec());
        let mut src = Source::new(objects);
        let err = install(&delivery, "linux-x64", d.path(), |k| src.fetch(k))
            .unwrap_err()
            .to_string();
        assert!(err.contains("sha256"), "{err}");
        assert!(!d.path().join(&delivery.files[0].dest).exists());
        assert!(!d.path().join(MANIFEST_NAME).exists());
    }

    #[test]
    fn a_short_body_is_refused() {
        let d = TempDir::new("short");
        let (delivery, mut objects) = sample();
        objects.insert(delivery.files[0].key.clone(), b"serv".to_vec());
        let mut src = Source::new(objects);
        let err = install(&delivery, "linux-x64", d.path(), |k| src.fetch(k))
            .unwrap_err()
            .to_string();
        assert!(err.contains("字节"), "{err}");
    }

    #[test]
    fn a_failed_file_never_fetches_the_root_manifest() {
        let d = TempDir::new("fail");
        let (delivery, objects) = sample();
        let mut src = Source::new(objects);
        src.drop_keys.insert(delivery.files[1].key.clone());
        let err = install(&delivery, "linux-x64", d.path(), |k| src.fetch(k))
            .unwrap_err()
            .to_string();
        assert!(err.contains("模拟断网"), "{err}");
        assert!(!src.calls.contains(&delivery.manifest.key));
        assert!(!d.path().join(MANIFEST_NAME).exists());
    }

    #[test]
    fn a_tampered_root_manifest_leaves_no_manifest_on_disk() {
        let d = TempDir::new("rootman");
        let (delivery, mut objects) = sample();
        objects.insert(delivery.manifest.key.clone(), b"{}".to_vec());
        let mut src = Source::new(objects);
        let err = install(&delivery, "linux-x64", d.path(), |k| src.fetch(k))
            .unwrap_err()
            .to_string();
        assert!(err.contains("根清单"), "{err}");
        assert!(!d.path().join(MANIFEST_NAME).exists());
        assert!(d.path().join(&delivery.files[0].dest).is_file());
    }

    #[test]
    fn an_existing_corrupt_file_is_replaced() {
        let d = TempDir::new("corrupt");
        let (delivery, objects) = sample();
        let target = d.path().join(&delivery.files[0].dest);
        fs::create_dir_all(target.parent().unwrap()).unwrap();
        fs::write(&target, b"old!").unwrap();
        let mut src = Source::new(objects);
        install(&delivery, "linux-x64", d.path(), |k| src.fetch(k)).unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"server");
    }

    fn check_err(delivery: &Delivery, wants: &str) {
        let err = delivery.check("linux-x64").unwrap_err().to_string();
        assert!(err.contains(wants), "{err}");
    }

    #[test]
    fn path_escapes_are_refused_before_any_fetch() {
        let mut delivery = sample().0;
        delivery.files[0].dest = "../evil".into();
        delivery.files[0].key = format!("payload/linux-x64/../evil");
        check_err(&delivery, "不安全");
        let mut delivery = sample().0;
        delivery.files[0].dest = "/etc/passwd".into();
        check_err(&delivery, "相对路径");
    }

    #[test]
    fn a_key_not_bound_to_the_dest_is_refused() {
        let mut delivery = sample().0;
        delivery.files[0].key = format!("payload/linux-x64/{}", delivery.files[1].dest);
        check_err(&delivery, "键是");
    }

    #[test]
    fn duplicate_destinations_and_wrong_prefixes_are_refused() {
        let mut delivery = sample().0;
        delivery.files[1].dest = delivery.files[0].dest.clone();
        delivery.files[1].key = delivery.files[0].key.clone();
        check_err(&delivery, "出现两次");
        let mut delivery = sample().0;
        delivery.key_prefix = "payload/../x".into();
        check_err(&delivery, "不安全的段");
        let mut delivery = sample().0;
        delivery.schema = 2;
        check_err(&delivery, "schema");
    }

    #[test]
    fn a_wrong_platform_or_bad_totals_is_refused() {
        let mut delivery = sample().0;
        delivery.os = "win-x64".into();
        check_err(&delivery, "这台机器");
        let mut delivery = sample().0;
        delivery.totals.as_mut().unwrap().bytes += 1;
        check_err(&delivery, "逐行相加");
        let mut delivery = sample().0;
        delivery.totals.as_mut().unwrap().files += 1;
        check_err(&delivery, "表里有");
    }

    #[test]
    fn a_root_manifest_that_is_not_last_or_misnamed_is_refused() {
        let mut delivery = sample().0;
        delivery.manifest.land = Some("first".into());
        check_err(&delivery, "land=last");
        let mut delivery = sample().0;
        delivery.manifest.dest = "manifest.json".into();
        check_err(&delivery, "payload-manifest.json");
        let mut delivery = sample().0;
        delivery.manifest.sha256 = "zz".into();
        check_err(&delivery, "64 位十六进制");
    }

    #[test]
    fn a_file_row_may_not_shadow_the_root_manifest() {
        let mut delivery = sample().0;
        delivery.files[0].dest = MANIFEST_NAME.into();
        delivery.files[0].key = format!("payload/linux-x64/{MANIFEST_NAME}");
        check_err(&delivery, "根清单的名字");
        let mut delivery = sample().0;
        delivery.files[0].land = Some("last".into());
        check_err(&delivery, "只有根清单");
    }
}
