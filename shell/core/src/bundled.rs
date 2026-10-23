//! 随安装包分发的运行组件：与远程源消费同一份 `delivery-manifest.json`。
//!
//! 安装包里放的是对象键布局（`<root>/<key>`）加一张**包根**的交付表
//! `delivery-manifest.json`（`bundle_payload.py` 最后写它）；这一层只负责发现它、
//! 把表再验一遍、按落点逐文件复制。校验与落盘顺序仍走 `delivery::install`，
//! 所以内置路径不会绕开哈希，也不会让根清单先于文件出现。

use std::fs;
use std::path::{Path, PathBuf};

use crate::delivery::{install, Delivery, Installed, TABLE_NAME};
use crate::error::CoreError;

pub const ENV_DIR: &str = "LOCAL_AI_BUNDLED_PAYLOAD";
pub const DIR_NAME: &str = "bundled";

fn has_table(dir: &Path) -> bool {
    dir.join(TABLE_NAME).is_file()
}

/// 测试与离线重放可以用环境变量指一棵树；正式安装走 Tauri 的资源目录。
pub fn from_env() -> Option<PathBuf> {
    let dir = PathBuf::from(std::env::var_os(ENV_DIR)?);
    has_table(&dir).then_some(dir)
}

pub fn in_resource_dir(resource_dir: Option<&Path>) -> Option<PathBuf> {
    let dir = resource_dir?.join(DIR_NAME);
    has_table(&dir).then_some(dir)
}

pub fn candidate(resource_dir: Option<&Path>) -> Option<PathBuf> {
    from_env().or_else(|| in_resource_dir(resource_dir))
}

pub fn load(root: &Path) -> Result<Delivery, CoreError> {
    Delivery::from_file(&root.join(TABLE_NAME))
}

pub fn install_from(root: &Path, platform: &str, data_root: &Path) -> Result<Installed, CoreError> {
    let delivery = load(root)?;
    delivery.check(platform)?;
    install(&delivery, platform, data_root, |key| {
        let path = root.join(key);
        fs::read(&path)
            .map_err(|e| CoreError::Manifest(format!("读内置对象 {} 失败: {e}", path.display())))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::delivery::MANIFEST_NAME;
    use crate::hex_encode;
    use serde_json::json;
    use sha2::{Digest, Sha256};
    use std::sync::atomic::{AtomicU64, Ordering};

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            static N: AtomicU64 = AtomicU64::new(0);
            let path = std::env::temp_dir().join(format!(
                "las-bundled-{}-{}-{}",
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

    fn digest(body: &[u8]) -> String {
        hex_encode(Sha256::digest(body).as_slice())
    }

    fn write_bundle(root: &Path, platform: &str) -> serde_json::Value {
        let prefix = format!("payload/{platform}");
        let file_rows = [
            (
                format!("runtime/{platform}/llama/b9/llama-server"),
                b"server".to_vec(),
            ),
            (
                format!("runtime/{platform}/llama/b9/libggml.so"),
                b"ggml".to_vec(),
            ),
        ];
        let mut files = Vec::new();
        let mut total = 0u64;
        for (dest, body) in &file_rows {
            let key = format!("{prefix}/{dest}");
            let path = root.join(&key);
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(&path, body).unwrap();
            files.push(json!({
                "dest": dest,
                "key": key,
                "bytes": body.len(),
                "sha256": digest(body),
            }));
            total += body.len() as u64;
        }
        let root_body = br#"{"os":"linux-x64","engines":[]}"#.to_vec();
        let manifest_key = format!("{prefix}/{MANIFEST_NAME}");
        let manifest_path = root.join(&manifest_key);
        fs::write(&manifest_path, &root_body).unwrap();
        let delivery = json!({
            "schema": 1,
            "os": platform,
            "key_prefix": prefix,
            "manifest": {
                "dest": MANIFEST_NAME,
                "key": manifest_key,
                "bytes": root_body.len(),
                "sha256": digest(&root_body),
                "land": "last",
            },
            "files": files,
            "totals": {"files": file_rows.len(), "bytes": total},
        });
        fs::write(
            root.join(TABLE_NAME),
            serde_json::to_string_pretty(&delivery).unwrap(),
        )
        .unwrap();
        delivery
    }

    #[test]
    fn installs_bundled_objects_and_reinstall_skips_all() {
        let tmp = TempDir::new("install");
        let bundle = tmp.path().join("bundle");
        let data = tmp.path().join("data");
        fs::create_dir_all(&bundle).unwrap();
        write_bundle(&bundle, "linux-x64");

        let first = install_from(&bundle, "linux-x64", &data).unwrap();
        assert_eq!(
            first,
            Installed {
                written: 3,
                skipped: 0
            }
        );
        assert_eq!(
            fs::read(data.join("runtime/linux-x64/llama/b9/llama-server")).unwrap(),
            b"server"
        );
        assert!(data.join(MANIFEST_NAME).is_file());

        let again = install_from(&bundle, "linux-x64", &data).unwrap();
        assert_eq!(
            again,
            Installed {
                written: 0,
                skipped: 3
            }
        );
    }

    #[test]
    fn a_tampered_bundled_object_is_refused_before_it_lands() {
        let tmp = TempDir::new("tamper");
        let bundle = tmp.path().join("bundle");
        let data = tmp.path().join("data");
        fs::create_dir_all(&bundle).unwrap();
        write_bundle(&bundle, "linux-x64");
        fs::write(
            bundle.join("payload/linux-x64/runtime/linux-x64/llama/b9/libggml.so"),
            b"evil",
        )
        .unwrap();

        let err = install_from(&bundle, "linux-x64", &data)
            .unwrap_err()
            .to_string();
        assert!(err.contains("sha256") || err.contains("字节"), "{err}");
        assert!(!data.join("runtime/linux-x64/llama/b9/libggml.so").exists());
        assert!(!data.join(MANIFEST_NAME).exists());
    }

    #[test]
    fn a_missing_bundled_object_is_named() {
        let tmp = TempDir::new("missing");
        let bundle = tmp.path().join("bundle");
        let data = tmp.path().join("data");
        fs::create_dir_all(&bundle).unwrap();
        write_bundle(&bundle, "linux-x64");
        fs::remove_file(
            bundle.join("payload/linux-x64/runtime/linux-x64/llama/b9/llama-server"),
        )
        .unwrap();

        let err = install_from(&bundle, "linux-x64", &data)
            .unwrap_err()
            .to_string();
        assert!(err.contains("读内置对象"), "{err}");
        assert!(!data.join(MANIFEST_NAME).exists());
    }

    #[test]
    fn a_foreign_platform_bundle_is_refused_before_writing() {
        let tmp = TempDir::new("platform");
        let bundle = tmp.path().join("bundle");
        let data = tmp.path().join("data");
        fs::create_dir_all(&bundle).unwrap();
        write_bundle(&bundle, "linux-x64");

        let err = install_from(&bundle, "win-x64", &data)
            .unwrap_err()
            .to_string();
        assert!(err.contains("win-x64"), "{err}");
        assert!(!data.exists() || fs::read_dir(&data).unwrap().next().is_none());
    }

    #[test]
    fn resource_dir_is_only_used_when_it_contains_a_manifest() {
        let tmp = TempDir::new("discover");
        let resources = tmp.path().join("resources");
        let bundled = resources.join(DIR_NAME);
        fs::create_dir_all(&bundled).unwrap();
        assert_eq!(in_resource_dir(Some(&resources)), None);
        assert_eq!(in_resource_dir(None), None);

        write_bundle(&bundled, "linux-x64");
        assert_eq!(in_resource_dir(Some(&resources)), Some(bundled));
    }
}
