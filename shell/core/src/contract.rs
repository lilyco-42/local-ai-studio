use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::error::CoreError;

/// 编译期嵌入：外壳读的就是提交在仓库里的那张表，不是运行时另找一份。
pub const CONTRACT_JSON: &str = include_str!("../../../pins/shell-contract.json");

#[derive(Debug, Clone, Deserialize)]
pub struct Contract {
    pub schema: u32,
    pub capabilities: BTreeMap<String, Capability>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Capability {
    pub engine: String,
    pub mode: String,
    pub bind: Option<String>,
    #[serde(default)]
    pub auth: Auth,
    pub cors: Option<Cors>,
    pub lifecycle: Lifecycle,
    pub readiness: Option<Readiness>,
    pub request: Option<Request>,
    pub version: Version,
    pub os: BTreeMap<String, OsCell>,
}

#[derive(Debug, Clone, Default, Deserialize)]
pub struct Auth {
    #[serde(default)]
    pub mode: String,
    pub flag: Option<String>,
    pub header: Option<String>,
    pub token_bytes: Option<usize>,
    #[serde(default)]
    pub persisted: bool,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Cors {
    pub flag: Option<String>,
    pub value: Option<String>,
    pub upstream_default: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Lifecycle {
    pub mode: String,
    pub idle_timeout_secs: Option<u64>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Readiness {
    pub method: String,
    pub path: String,
    pub expect_status: u16,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Request {
    pub method: Option<String>,
    pub path: Option<String>,
    pub multipart_field: Option<String>,
    pub poll: Option<String>,
    pub expect_status: Option<u16>,
    pub extra_fields: Option<BTreeMap<String, String>>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Version {
    pub pinned: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct OsCell {
    pub binary: String,
    #[serde(default)]
    pub argv_by_tier: BTreeMap<String, Vec<String>>,
    pub argv: Option<Vec<String>>,
    pub default_asset: Option<String>,
    pub version_dir: Option<String>,
    pub asset_dir: Option<String>,
}

impl Contract {
    pub fn parse(text: &str) -> Result<Self, CoreError> {
        let parsed: Self = serde_json::from_str(text)
            .map_err(|e| CoreError::Json(format!("外壳合同不是合法 JSON: {e}")))?;
        let problems = parsed.validate();
        if problems.is_empty() {
            Ok(parsed)
        } else {
            Err(CoreError::Contract(problems.join("; ")))
        }
    }

    pub fn embedded() -> Result<Self, CoreError> {
        Self::parse(CONTRACT_JSON)
    }

    pub fn capability(&self, name: &str) -> Result<&Capability, CoreError> {
        self.capabilities
            .get(name)
            .ok_or_else(|| CoreError::Contract(format!("合同里没有能力 {name:?}")))
    }

    /// 判据只检查合同**自己**是否自洽；上游事实由 Python 那张表负责。
    pub fn validate(&self) -> Vec<String> {
        let mut bad = Vec::new();
        if self.schema != 1 {
            bad.push(format!("合同 schema 是 {}，这里只认 1", self.schema));
        }
        // "什么算钥匙/什么算 CORS 旗标"也从表里取：写死旗标名等于把表抄了一半。
        let auth_flags: Vec<String> = self
            .capabilities
            .values()
            .filter_map(|c| c.auth.flag.clone())
            .collect();
        let cors_flags: Vec<String> = self
            .capabilities
            .values()
            .filter_map(|c| c.cors.as_ref().and_then(|x| x.flag.clone()))
            .collect();
        for (name, cap) in &self.capabilities {
            match cap.mode.as_str() {
                "server" => Self::validate_server(name, cap, &auth_flags, &cors_flags, &mut bad),
                "subprocess" => Self::validate_subprocess(name, cap, &mut bad),
                other => bad.push(format!("{name}: 未知 mode {other:?}")),
            }
            for (os, cell) in &cap.os {
                if cap.mode == "server" {
                    if cell.argv_by_tier.is_empty() {
                        bad.push(format!("{name}/{os}: 没有按档位的命令行"));
                    }
                    if cell.default_asset.is_none() || cell.asset_dir.is_none() {
                        bad.push(format!("{name}/{os}: 没有默认档或 asset 目录"));
                    }
                    for (tier, row) in &cell.argv_by_tier {
                        Self::validate_row(
                            name, os, tier, cap, cell, row, &auth_flags, &cors_flags, &mut bad,
                        );
                    }
                } else {
                    if cell.argv.is_none() {
                        bad.push(format!("{name}/{os}: subprocess 没有命令行模板"));
                    }
                    if cell.version_dir.is_none() {
                        bad.push(format!("{name}/{os}: subprocess 没有 runtime 目录"));
                    }
                    if let Some(row) = cell.argv.as_ref() {
                        Self::validate_row(
                            name, os, "-", cap, cell, row, &auth_flags, &cors_flags, &mut bad,
                        );
                    }
                }
            }
        }
        bad
    }

    fn validate_server(
        name: &str,
        cap: &Capability,
        auth_flags: &[String],
        cors_flags: &[String],
        bad: &mut Vec<String>,
    ) {
        let bind = cap.bind.as_deref().unwrap_or("");
        if bind != "127.0.0.1" {
            bad.push(format!("{name}: bind {bind:?} 不是逐字的 127.0.0.1"));
        }
        let keyed = match cap.auth.mode.as_str() {
            "api-key" => true,
            "none" => false,
            other => {
                bad.push(format!("{name}: 未知 auth.mode {other:?}"));
                return;
            }
        };
        if keyed {
            if cap.auth.flag.as_deref().unwrap_or("").is_empty() {
                bad.push(format!("{name}: api-key 模式没有旗标名"));
            }
            if !cap.auth.header.as_deref().unwrap_or("").contains("{token}") {
                bad.push(format!("{name}: 鉴权头模板里没有 {{token}}"));
            }
            if cap.auth.token_bytes.unwrap_or(0) == 0 {
                bad.push(format!("{name}: token 字节数是 0"));
            }
            if cap.auth.persisted {
                bad.push(format!("{name}: token 被标成落盘"));
            }
            if cap.cors.as_ref().and_then(|c| c.flag.as_ref()).is_none() {
                bad.push(format!("{name}: 有钥匙却没有收窄 CORS"));
            }
            if cap.lifecycle.mode != "resident" {
                bad.push(format!(
                    "{name}: 有钥匙引擎的生命周期是 {:?} 而不是 resident",
                    cap.lifecycle.mode
                ));
            }
        } else {
            if cap.auth.flag.is_some() {
                bad.push(format!("{name}: 没钥匙却写了旗标"));
            }
            if cap.cors.is_some() {
                bad.push(format!("{name}: 没钥匙却写了 CORS 收窄"));
            }
            if cap.lifecycle.mode != "on_demand" {
                bad.push(format!(
                    "{name}: 没钥匙引擎的生命周期是 {:?} 而不是 on_demand",
                    cap.lifecycle.mode
                ));
            }
            match cap.lifecycle.idle_timeout_secs {
                Some(s) if s > 0 && s <= 300 => {}
                _ => bad.push(format!("{name}: 没钥匙引擎的空闲超时必须 >0 且 <=300 秒")),
            }
        }
        match cap.readiness.as_ref() {
            Some(r) if r.path.starts_with('/') && !r.method.is_empty() && r.expect_status > 0 => {}
            _ => bad.push(format!("{name}: 没有可用的就绪探针（method/path/expect_status）")),
        }
        match cap.request.as_ref() {
            Some(req) if req.method.as_deref().unwrap_or("").is_empty() => {
                bad.push(format!("{name}: request 没有 method"))
            }
            Some(req) if req.path.as_deref().unwrap_or("").is_empty() => {
                bad.push(format!("{name}: request 没有 path"))
            }
            Some(_) => {}
            None => bad.push(format!("{name}: server 能力没有 request 描述")),
        }
    }

    fn validate_subprocess(name: &str, cap: &Capability, bad: &mut Vec<String>) {
        if cap.lifecycle.mode != "one_shot" {
            bad.push(format!(
                "{name}: subprocess 的生命周期是 {:?} 而不是 one_shot",
                cap.lifecycle.mode
            ));
        }
        if cap.auth.mode != "none" || cap.auth.flag.is_some() || cap.cors.is_some() {
            bad.push(format!("{name}: subprocess 不该有鉴权或 CORS"));
        }
        if cap.bind.is_some() {
            bad.push(format!("{name}: subprocess 不该有 bind"));
        }
        if cap.request.as_ref().and_then(|r| r.method.as_ref()).is_some() {
            bad.push(format!("{name}: subprocess 不该有 HTTP method"));
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn validate_row(
        name: &str,
        os: &str,
        tier: &str,
        cap: &Capability,
        cell: &OsCell,
        row: &[String],
        auth_flags: &[String],
        cors_flags: &[String],
        bad: &mut Vec<String>,
    ) {
        if row.is_empty() {
            bad.push(format!("{name}/{os}/{tier}: 命令行是空的"));
            return;
        }
        if cell.binary.is_empty() {
            bad.push(format!("{name}/{os}: 二进制名是空的"));
        }
        let bind = cap.bind.as_deref().unwrap_or("");
        if cap.mode == "server" && !row.iter().any(|t| t == bind) {
            bad.push(format!("{name}/{os}/{tier}: 命令行里没有逐字的 {bind}"));
        }
        for t in row {
            if t.contains('{') && !is_placeholder(t) {
                bad.push(format!(
                    "{name}/{os}/{tier}: {t:?} 不是完整的占位符（外壳只替换整段 {{name}}）"
                ));
            }
            if t.contains("0.0.0.0") || t.contains("localhost:") {
                bad.push(format!("{name}/{os}/{tier}: {t:?} 不是回环地址写法"));
            }
        }
        let keyed = cap.auth.mode == "api-key";
        if keyed {
            let flag = cap.auth.flag.as_deref().unwrap_or("");
            if !row.iter().any(|t| t == flag) {
                bad.push(format!("{name}/{os}/{tier}: 有钥匙却没有 {flag}"));
            }
            if !row.iter().any(|t| t == "{token}") {
                bad.push(format!("{name}/{os}/{tier}: 有钥匙却没有 {{token}} 占位符"));
            }
        } else {
            if let Some(hit) = row.iter().find(|t| auth_flags.iter().any(|f| f == *t)) {
                bad.push(format!(
                    "{name}/{os}/{tier}: 没钥匙的能力却带了鉴权旗标 {hit}"
                ));
            }
            if let Some(hit) = row.iter().find(|t| cors_flags.iter().any(|f| f == *t)) {
                bad.push(format!(
                    "{name}/{os}/{tier}: 没这条旗标的引擎却带了 {hit}"
                ));
            }
        }
    }
}

pub fn is_placeholder(token: &str) -> bool {
    if !token.starts_with('{') || !token.ends_with('}') || token.len() < 3 {
        return false;
    }
    let inner = &token[1..token.len() - 1];
    !inner.is_empty() && !inner.contains('{') && !inner.contains('}')
}

pub fn platform_key(os: &str, arch: &str) -> Result<&'static str, CoreError> {
    match (os, arch) {
        ("windows", "x86_64") => Ok("win-x64"),
        ("linux", "x86_64") => Ok("linux-x64"),
        ("macos", "aarch64") => Ok("macos-arm64"),
        _ => Err(CoreError::Platform(format!(
            "这台机器 {os}/{arch} 不在已钉的三档里"
        ))),
    }
}

pub fn host_platform_key() -> Result<&'static str, CoreError> {
    platform_key(std::env::consts::OS, std::env::consts::ARCH)
}

/// `<data>/runtime/...` → 真实的绝对路径；任何一段是 `.`/`..`/空都拒绝。
pub fn expand_dir(template: &str, data_root: &Path) -> Result<PathBuf, CoreError> {
    let rest = template.strip_prefix("<data>/").ok_or_else(|| {
        CoreError::Contract(format!("目录模板 {template:?} 不以 <data>/ 开头"))
    })?;
    let mut out = data_root.to_path_buf();
    for part in rest.split('/') {
        if part.is_empty() || part == "." || part == ".." {
            return Err(CoreError::Contract(format!(
                "目录模板 {template:?} 含不安全的段 {part:?}"
            )));
        }
        out.push(part);
    }
    Ok(out)
}

impl OsCell {
    /// server 按档位取；subprocess 只有一份模板，档位对它无意义。
    pub fn argv_for(&self, tier: &str) -> Result<&Vec<String>, CoreError> {
        if let Some(row) = self.argv_by_tier.get(tier) {
            return Ok(row);
        }
        if let Some(tpl) = self.argv.as_ref() {
            return Ok(tpl);
        }
        let tiers: Vec<&str> = self.argv_by_tier.keys().map(|s| s.as_str()).collect();
        Err(CoreError::Contract(format!(
            "没有档位 {tier:?} 的命令行（有: {tiers:?}）"
        )))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn contract() -> Contract {
        Contract::embedded().expect("提交在仓库里的合同必须能解析并通过自洽检查")
    }

    #[test]
    fn embedded_contract_is_self_consistent() {
        let c = contract();
        assert_eq!(c.capabilities.len(), 4);
        assert!(c.validate().is_empty());
    }

    #[test]
    fn only_llama_has_a_key_and_the_size_comes_from_the_table() {
        let c = contract();
        let llama = c.capability("对话").unwrap();
        assert_eq!(llama.auth.mode, "api-key");
        assert_eq!(llama.auth.token_bytes, Some(32));
        assert!(!llama.auth.persisted);
        assert_eq!(llama.lifecycle.mode, "resident");
        for name in ["字幕", "生图"] {
            let cap = c.capability(name).unwrap();
            assert_eq!(cap.auth.mode, "none", "{name}");
            assert_eq!(cap.lifecycle.mode, "on_demand", "{name}");
            assert_eq!(cap.lifecycle.idle_timeout_secs, Some(300), "{name}");
        }
        assert_eq!(c.capability("文字识别").unwrap().mode, "subprocess");
        assert_eq!(
            c.capability("字幕")
                .unwrap()
                .request
                .as_ref()
                .unwrap()
                .extra_fields
                .as_ref()
                .unwrap()
                .get("response_format")
                .map(|s| s.as_str()),
            Some("srt")
        );
    }

    #[test]
    fn bind_must_be_literal_loopback() {
        let mut c = contract();
        c.capabilities.get_mut("字幕").unwrap().bind = Some("0.0.0.0".into());
        assert!(c.validate().iter().any(|m| m.contains("127.0.0.1")));
    }

    #[test]
    fn keyed_engine_cannot_downgrade_to_on_demand() {
        let mut c = contract();
        let llama = c.capabilities.get_mut("对话").unwrap();
        llama.lifecycle.mode = "on_demand".into();
        llama.lifecycle.idle_timeout_secs = Some(300);
        assert!(c.validate().iter().any(|m| m.contains("resident")));
    }

    #[test]
    fn keyless_engine_cannot_be_resident() {
        let mut c = contract();
        let whisper = c.capabilities.get_mut("字幕").unwrap();
        whisper.lifecycle.mode = "resident".into();
        whisper.lifecycle.idle_timeout_secs = None;
        assert!(c.validate().iter().any(|m| m.contains("on_demand")));
    }

    #[test]
    fn a_server_without_a_readiness_probe_is_rejected() {
        let mut c = contract();
        c.capabilities.get_mut("字幕").unwrap().readiness = None;
        assert!(c.validate().iter().any(|m| m.contains("就绪")));
    }

    #[test]
    fn a_row_that_drops_the_key_placeholder_is_rejected() {
        let mut c = contract();
        let llama = c.capabilities.get_mut("对话").unwrap();
        let cell = llama.os.get_mut("win-x64").unwrap();
        let row = cell.argv_by_tier.get_mut("8GB").unwrap();
        let pos = row.iter().position(|t| t == "{token}").unwrap();
        row.remove(pos);
        assert!(c.validate().iter().any(|m| m.contains("{token}")));
    }

    #[test]
    fn a_keyless_row_that_adds_a_key_flag_is_rejected() {
        let mut c = contract();
        let whisper = c.capabilities.get_mut("字幕").unwrap();
        let cell = whisper.os.get_mut("win-x64").unwrap();
        let row = cell.argv_by_tier.get_mut("8GB").unwrap();
        row.push("--api-key".into());
        row.push("x".into());
        assert!(c.validate().iter().any(|m| m.contains("鉴权旗标")));
    }

    #[test]
    fn platform_mapping_is_exact() {
        assert_eq!(platform_key("windows", "x86_64").unwrap(), "win-x64");
        assert_eq!(platform_key("linux", "x86_64").unwrap(), "linux-x64");
        assert_eq!(platform_key("macos", "aarch64").unwrap(), "macos-arm64");
        assert!(platform_key("macos", "x86_64").is_err());
        assert!(platform_key("freebsd", "x86_64").is_err());
    }

    #[test]
    fn expand_dir_refuses_escaping_templates() {
        let root = Path::new("/data");
        assert_eq!(
            expand_dir("<data>/runtime/win-x64/llama/b11727", root).unwrap(),
            PathBuf::from("/data/runtime/win-x64/llama/b11727")
        );
        assert!(expand_dir("<data>/runtime/../../etc", root).is_err());
        assert!(expand_dir("runtime/win-x64", root).is_err());
        assert!(expand_dir("<data>/runtime//x", root).is_err());
    }

    #[test]
    fn tiers_resolve_and_subprocess_ignores_the_tier() {
        let c = contract();
        let llama = c.capability("对话").unwrap();
        let cell = llama.os.get("win-x64").unwrap();
        assert_ne!(cell.argv_for("4GB").unwrap(), cell.argv_for("8GB").unwrap());
        assert!(cell.argv_for("32GB").is_err());
        let ocr = c.capability("文字识别").unwrap();
        let cell = ocr.os.get("win-x64").unwrap();
        assert_eq!(cell.argv_for("8GB").unwrap(), cell.argv.as_ref().unwrap());
    }

    #[test]
    fn placeholders_are_whole_tokens_only() {
        assert!(is_placeholder("{bin}"));
        assert!(is_placeholder("{Qwen3-8B-Q4_K_M.gguf}"));
        assert!(!is_placeholder("--model={bin}"));
        assert!(!is_placeholder("{}"));
        assert!(!is_placeholder("{a{b}"));
        assert!(!is_placeholder("plain"));
    }
}
