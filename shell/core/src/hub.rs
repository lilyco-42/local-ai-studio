//! 可选算力共享：只保存开关和一条给人复制到终端的 SSH 命令。
//!
//! 这一层**不联网、不自动登录、不开监听**。真正把本机算力交给中心服务器，
//! 需要用户自己拿公钥去 `lain42.top` 登记；积分与结算都在服务端，客户端不碰支付。
//! 默认关闭，关掉时不会生成任何出站流量。

use std::fs;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

use crate::error::CoreError;

pub const HUB_HOST: &str = "lain42.top";
pub const CONFIG_NAME: &str = "hub.json";
pub const DEFAULT_KEY: &str = "~/.ssh/lain42_ed25519";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HubConfig {
    pub enabled: bool,
    pub host: String,
    #[serde(default)]
    pub ssh_user: String,
    pub ssh_key: String,
    #[serde(default)]
    pub node_id: String,
}

impl Default for HubConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            host: HUB_HOST.to_string(),
            ssh_user: String::new(),
            ssh_key: DEFAULT_KEY.to_string(),
            node_id: String::new(),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct HubView {
    pub enabled: bool,
    pub host: String,
    pub ssh_user: String,
    pub ssh_key: String,
    pub node_id: String,
    pub ssh_command: String,
}

impl HubConfig {
    pub fn validate(&self) -> Result<(), CoreError> {
        let mut bad: Vec<String> = Vec::new();
        if !host_ok(&self.host) {
            bad.push(format!(
                "主机只认 {HUB_HOST} 或其子域（不带协议/端口/路径）"
            ));
        }
        if !self.ssh_user.is_empty()
            && (self.ssh_user.len() > 32
                || !self
                    .ssh_user
                    .chars()
                    .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.')))
        {
            bad.push("ssh 用户名只能是 1–32 个字母/数字/._-".into());
        }
        if self.ssh_key.is_empty() || self.ssh_key.len() > 256 {
            bad.push("ssh 私钥路径不能为空且不超过 256 字符".into());
        }
        if self.ssh_key.contains("..")
            || self
                .ssh_key
                .chars()
                .any(|c| c.is_control() || !(c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.' | '~' | '/')))
        {
            bad.push("ssh 私钥路径含不允许的字符".into());
        }
        if !self.ssh_key.starts_with("~/") && !self.ssh_key.starts_with('/') {
            bad.push("ssh 私钥路径必须以 ~/ 或 / 开头".into());
        }
        if !self.node_id.is_empty()
            && (self.node_id.len() != 32
                || !self.node_id.bytes().all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()))
        {
            bad.push("node_id 必须是 32 位小写十六进制".into());
        }
        if bad.is_empty() {
            Ok(())
        } else {
            Err(CoreError::Contract(bad.join("; ")))
        }
    }

    /// 只给人复制，不由外壳执行；`enabled=false` 时也照样能预览命令。
    pub fn ssh_command(&self) -> String {
        let target = if self.ssh_user.is_empty() {
            self.host.clone()
        } else {
            format!("{}@{}", self.ssh_user, self.host)
        };
        format!(
            "ssh -i {} -o IdentitiesOnly=yes {}",
            shell_quote(&self.ssh_key),
            target
        )
    }

    pub fn view(&self) -> HubView {
        HubView {
            enabled: self.enabled,
            host: self.host.clone(),
            ssh_user: self.ssh_user.clone(),
            ssh_key: self.ssh_key.clone(),
            node_id: self.node_id.clone(),
            ssh_command: self.ssh_command(),
        }
    }
}

fn host_ok(host: &str) -> bool {
    if host == HUB_HOST {
        return true;
    }
    match host.strip_suffix(HUB_HOST) {
        Some(prefix) => prefix.ends_with('.') && prefix.len() > 1,
        None => false,
    }
}

fn shell_quote(value: &str) -> String {
    if !value.is_empty()
        && value
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.' | '~' | '/'))
    {
        value.to_string()
    } else {
        format!("'{}'", value.replace('\'', "'\\''"))
    }
}

pub fn path(data_root: &Path) -> PathBuf {
    data_root.join(CONFIG_NAME)
}

pub fn load(data_root: &Path) -> Result<HubConfig, CoreError> {
    match fs::read_to_string(path(data_root)) {
        Ok(text) => {
            let config: HubConfig = serde_json::from_str(&text)
                .map_err(|e| CoreError::Json(format!("{CONFIG_NAME} 不是合法 JSON: {e}")))?;
            config.validate()?;
            Ok(config)
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(HubConfig::default()),
        Err(e) => Err(CoreError::Io(format!(
            "读 {} 失败: {e}",
            path(data_root).display()
        ))),
    }
}

/// 第一次保存时生成 node_id；之后保持稳定，不让"换一台机器"被误当成同一个节点。
pub fn save(data_root: &Path, mut config: HubConfig) -> Result<HubConfig, CoreError> {
    if config.node_id.is_empty() {
        config.node_id = crate::token_hex(16)?;
    }
    config.validate()?;
    fs::create_dir_all(data_root)?;
    let text = serde_json::to_string_pretty(&config)
        .map_err(|e| CoreError::Json(format!("序列化 {CONFIG_NAME} 失败: {e}")))?;
    fs::write(path(data_root), text)?;
    Ok(config)
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
                "las-hub-{}-{}-{}",
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

    #[test]
    fn default_is_off_and_never_contacts_anything() {
        let config = HubConfig::default();
        assert!(!config.enabled);
        assert_eq!(config.host, HUB_HOST);
        assert!(config.node_id.is_empty());
        assert!(config.ssh_command().starts_with("ssh -i"));
    }

    #[test]
    fn save_roundtrip_generates_a_stable_node_id() {
        let root = TempDir::new("roundtrip");
        let saved = save(root.path(), HubConfig::default()).unwrap();
        assert_eq!(saved.node_id.len(), 32);
        assert!(!saved.enabled);
        let loaded = load(root.path()).unwrap();
        assert_eq!(loaded, saved);
        let again = save(root.path(), loaded.clone()).unwrap();
        assert_eq!(again.node_id, saved.node_id);
    }

    #[test]
    fn enabling_persists_and_the_command_is_display_only() {
        let root = TempDir::new("enable");
        let config = HubConfig {
            enabled: true,
            ssh_user: "join".into(),
            ..HubConfig::default()
        };
        let saved = save(root.path(), config).unwrap();
        assert!(saved.enabled);
        let view = saved.view();
        assert!(view.ssh_command.contains("-i"));
        assert!(view.ssh_command.contains("-o IdentitiesOnly=yes"));
        assert!(view.ssh_command.contains("join@lain42.top"));
    }

    #[test]
    fn foreign_hosts_are_refused() {
        for host in [
            "https://lain42.top",
            "lain42.top:22",
            "evil.com",
            "lain42.top.evil.com",
            "evil-lain42.top",
        ] {
            let config = HubConfig {
                host: host.into(),
                ..HubConfig::default()
            };
            assert!(config.validate().is_err(), "{host}");
        }
        let config = HubConfig {
            host: "node.lain42.top".into(),
            ..HubConfig::default()
        };
        assert!(config.validate().is_ok());
    }

    #[test]
    fn a_key_path_cannot_smuggle_a_command() {
        for key in [
            "~/.ssh/key; rm -rf /",
            "~/.ssh/../secret",
            "~/.ssh/key\nssh other",
            "relative/key",
        ] {
            let config = HubConfig {
                ssh_key: key.into(),
                ..HubConfig::default()
            };
            assert!(config.validate().is_err(), "{key}");
        }
    }

    #[test]
    fn a_bad_node_id_is_refused() {
        let config = HubConfig {
            node_id: "not-hex".into(),
            ..HubConfig::default()
        };
        assert!(config.validate().is_err());
    }
}
