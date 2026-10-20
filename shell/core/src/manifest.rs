use std::collections::BTreeMap;
use std::fs;
use std::io::Read;
use std::path::Path;

use serde::Deserialize;
use sha2::{Digest, Sha256};

use crate::error::CoreError;

/// `payload-manifest.json` 由出包车道写、随 runtime 一起装。
/// 外壳在 exec 前逐文件对这张表：缺一个、多一个、哈希不对都不许起。
#[derive(Debug, Clone, Deserialize)]
pub struct Manifest {
    pub os: String,
    pub engines: Vec<ManifestEngine>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ManifestEngine {
    pub engine: String,
    pub status: String,
    pub asset: Option<String>,
    #[serde(default)]
    pub files: Vec<ManifestFile>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct ManifestFile {
    pub file: String,
    pub bytes: u64,
    pub sha256: String,
}

impl Manifest {
    pub fn parse(text: &str) -> Result<Self, CoreError> {
        serde_json::from_str(text).map_err(|e| CoreError::Manifest(format!("清单不是合法 JSON: {e}")))
    }

    pub fn from_file(path: &Path) -> Result<Self, CoreError> {
        let text = fs::read_to_string(path).map_err(|e| {
            CoreError::Manifest(format!("读不到载荷清单 {}: {e}", path.display()))
        })?;
        Self::parse(&text)
    }

    pub fn for_engine(&self, engine: &str, asset: &str) -> Result<&ManifestEngine, CoreError> {
        self.engines
            .iter()
            .find(|e| e.engine == engine && e.asset.as_deref() == Some(asset))
            .ok_or_else(|| {
                CoreError::Manifest(format!("payload-manifest 里没有 {engine} 的 {asset}"))
            })
    }

    /// OCR 的产物是 CI 现编的，没有 release asset 名；同名条目多于一条就算歧义。
    pub fn for_engine_without_asset(&self, engine: &str) -> Result<&ManifestEngine, CoreError> {
        let hits: Vec<&ManifestEngine> = self.engines.iter().filter(|e| e.engine == engine).collect();
        match hits.as_slice() {
            [one] => Ok(one),
            [] => Err(CoreError::Manifest(format!(
                "payload-manifest 里没有 {engine}"
            ))),
            many => Err(CoreError::Manifest(format!(
                "payload-manifest 里 {engine} 有 {} 条，无法判断是哪一颗",
                many.len()
            ))),
        }
    }
}

/// 盘上目录与清单必须逐个文件对得上：只报"哈希不对"会漏掉多塞进来的 DLL。
pub fn verify_dir(entry: &ManifestEngine, dir: &Path) -> Result<usize, CoreError> {
    if entry.status != "ok" {
        return Err(CoreError::Manifest(format!(
            "{} 的出包状态是 {:?}，不是 ok",
            entry.engine, entry.status
        )));
    }
    if entry.files.is_empty() {
        return Err(CoreError::Manifest(format!("{} 的清单是空的", entry.engine)));
    }
    let mut want: BTreeMap<String, (u64, String)> = BTreeMap::new();
    for f in &entry.files {
        if f.file.is_empty()
            || f.file.contains('/')
            || f.file.contains('\\')
            || f.file == "."
            || f.file == ".."
        {
            return Err(CoreError::Manifest(format!(
                "{:?} 不是扁平文件名",
                f.file
            )));
        }
        if f.sha256.len() != 64 || !f.sha256.chars().all(|c| c.is_ascii_hexdigit()) {
            return Err(CoreError::Manifest(format!(
                "{} 的 sha256 不是 64 位十六进制",
                f.file
            )));
        }
        if want
            .insert(f.file.clone(), (f.bytes, f.sha256.to_ascii_lowercase()))
            .is_some()
        {
            return Err(CoreError::Manifest(format!("清单里 {} 出现两次", f.file)));
        }
    }
    if !dir.is_dir() {
        return Err(CoreError::Manifest(format!("{} 不是目录", dir.display())));
    }
    let mut seen = 0usize;
    for ent in fs::read_dir(dir)? {
        let ent = ent?;
        let name = ent.file_name().to_string_lossy().into_owned();
        if ent.file_type()?.is_dir() {
            return Err(CoreError::Manifest(format!(
                "{} 里多了一层目录 {name:?}",
                dir.display()
            )));
        }
        let (bytes, sha) = want.get(&name).ok_or_else(|| {
            CoreError::Manifest(format!("{name:?} 不在清单里（盘上多出来的文件）"))
        })?;
        let meta = ent.metadata()?;
        if meta.len() != *bytes {
            return Err(CoreError::Manifest(format!(
                "{name}: 清单写 {} 字节，盘上是 {}",
                bytes,
                meta.len()
            )));
        }
        let got = sha256_file(&ent.path())?;
        if &got != sha {
            return Err(CoreError::Manifest(format!("{name}: sha256 与清单不符")));
        }
        seen += 1;
    }
    if seen != want.len() {
        let missing: Vec<&str> = want
            .keys()
            .filter(|k| !dir.join(k.as_str()).exists())
            .map(|k| k.as_str())
            .collect();
        return Err(CoreError::Manifest(format!(
            "清单有 {} 个文件，盘上只对上 {seen} 个；缺 {missing:?}",
            want.len()
        )));
    }
    Ok(seen)
}

pub fn sha256_file(path: &Path) -> Result<String, CoreError> {
    let mut f = fs::File::open(path)?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 20];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    let digest = h.finalize();
    Ok(crate::hex_encode(digest.as_slice()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            static N: AtomicU64 = AtomicU64::new(0);
            let p = std::env::temp_dir().join(format!(
                "lasc-{}-{}-{}",
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

        fn write(&self, name: &str, bytes: &[u8]) {
            fs::write(self.0.join(name), bytes).unwrap();
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn file(name: &str, body: &[u8]) -> ManifestFile {
        ManifestFile {
            file: name.into(),
            bytes: body.len() as u64,
            sha256: crate::hex_encode(
                Sha256::digest(body).as_slice(),
            ),
        }
    }

    fn entry(rows: Vec<ManifestFile>) -> ManifestEngine {
        ManifestEngine {
            engine: "llama".into(),
            status: "ok".into(),
            asset: Some("llama-win.zip".into()),
            files: rows,
        }
    }

    fn two_files(dir: &TempDir) -> ManifestEngine {
        dir.write("a.exe", b"alpha");
        dir.write("b.dll", b"beta");
        entry(vec![file("a.exe", b"alpha"), file("b.dll", b"beta")])
    }

    #[test]
    fn a_matching_tree_passes() {
        let dir = TempDir::new("ok");
        assert_eq!(verify_dir(&two_files(&dir), dir.path()).unwrap(), 2);
    }

    #[test]
    fn a_tampered_byte_is_caught() {
        let dir = TempDir::new("tamper");
        let m = two_files(&dir);
        dir.write("a.exe", b"alphA");
        let err = verify_dir(&m, dir.path()).unwrap_err().to_string();
        assert!(err.contains("sha256"), "{err}");
    }

    #[test]
    fn an_extra_file_is_caught() {
        let dir = TempDir::new("extra");
        let m = two_files(&dir);
        dir.write("evil.dll", b"x");
        let err = verify_dir(&m, dir.path()).unwrap_err().to_string();
        assert!(err.contains("多出来"), "{err}");
    }

    #[test]
    fn a_missing_file_is_caught() {
        let dir = TempDir::new("missing");
        let m = two_files(&dir);
        fs::remove_file(dir.path().join("b.dll")).unwrap();
        let err = verify_dir(&m, dir.path()).unwrap_err().to_string();
        assert!(err.contains("缺"), "{err}");
    }

    #[test]
    fn a_size_mismatch_is_caught_even_without_hashing() {
        let dir = TempDir::new("size");
        let m = two_files(&dir);
        dir.write("a.exe", b"alphA!");
        let err = verify_dir(&m, dir.path()).unwrap_err().to_string();
        assert!(err.contains("字节"), "{err}");
    }

    #[test]
    fn nested_directories_are_refused() {
        let dir = TempDir::new("nested");
        let m = two_files(&dir);
        fs::create_dir(dir.path().join("lib")).unwrap();
        let err = verify_dir(&m, dir.path()).unwrap_err().to_string();
        assert!(err.contains("目录"), "{err}");
    }

    #[test]
    fn manifest_paths_must_be_flat_names() {
        let dir = TempDir::new("path");
        let m = entry(vec![ManifestFile {
            file: "../a.exe".into(),
            bytes: 1,
            sha256: "0".repeat(64),
        }]);
        assert!(verify_dir(&m, dir.path()).is_err());
    }

    #[test]
    fn a_failed_pack_status_is_not_a_usable_payload() {
        let dir = TempDir::new("status");
        let mut m = two_files(&dir);
        m.status = "digest_mismatch".into();
        assert!(verify_dir(&m, dir.path()).is_err());
    }

    #[test]
    fn lookup_is_by_engine_and_asset_not_by_position() {
        let text = format!(
            r#"{{"os":"win-x64","engines":[{{"engine":"llama","status":"ok","asset":"a.zip","files":[{{"file":"x","bytes":1,"sha256":"{}"}}]}}]}}"#,
            "0".repeat(64)
        );
        let m = Manifest::parse(&text).unwrap();
        assert_eq!(m.for_engine("llama", "a.zip").unwrap().engine, "llama");
        assert!(m.for_engine("llama", "b.zip").is_err());
        assert!(m.for_engine("whisper", "a.zip").is_err());
    }
}
