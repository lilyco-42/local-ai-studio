use std::fs;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use local_ai_studio_core::hardware::{detect, Hardware};
use local_ai_studio_core::starter::{installed_path, StarterPin};
use local_ai_studio_core::{bundled, Contract, Supervisor};
use tauri::Manager;

pub struct AppState {
    pub supervisor: Arc<Mutex<Supervisor>>,
    pub data_root: PathBuf,
    /// 启动时读一次；`nvidia-smi` 之类只读探测不会每 3 秒重跑。
    pub hardware: Hardware,
    pub starter: StarterPin,
    /// 安装包自带的运行组件目录；没有就是 `None`，远程源仍然可用。
    pub bundled: Option<PathBuf>,
}

impl AppState {
    pub fn from_app<R: tauri::Runtime>(app: &tauri::App<R>) -> Result<Self, String> {
        let data_root = app
            .path()
            .app_data_dir()
            .map_err(|e| format!("拿不到应用数据目录: {e}"))?;
        fs::create_dir_all(&data_root)
            .map_err(|e| format!("建不了数据目录 {}: {e}", data_root.display()))?;
        let contract = Contract::embedded().map_err(|e| e.to_string())?;
        let platform = platform_label()
            .ok_or_else(|| format!("合同里没有 {} 这一格", std::env::consts::OS))?;
        let tier = std::env::var("LOCAL_AI_TIER").unwrap_or_else(|_| "8GB".to_string());
        let device = std::env::var("LOCAL_AI_OCR_DEVICE").unwrap_or_else(|_| "auto".to_string());
        let starter = StarterPin::embedded().map_err(|e| e.to_string())?;
        let starter_model = installed_path(&starter, &data_root);
        let supervisor = Supervisor::new(contract, platform, &tier, data_root.clone(), &device)
            .map_err(|e| e.to_string())?
            .with_starter_model(starter_model);
        let resource_dir = app.path().resource_dir().ok();
        Ok(Self {
            supervisor: Arc::new(Mutex::new(supervisor)),
            data_root,
            hardware: detect(),
            starter,
            bundled: bundled::candidate(resource_dir.as_deref()),
        })
    }
}

pub fn platform_label() -> Option<&'static str> {
    if cfg!(all(target_os = "windows", target_arch = "x86_64")) {
        Some("win-x64")
    } else if cfg!(all(target_os = "macos", target_arch = "aarch64")) {
        Some("macos-arm64")
    } else if cfg!(all(target_os = "linux", target_arch = "x86_64")) {
        Some("linux-x64")
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_running_platform_is_a_contract_key() {
        let platform = platform_label().expect("CI 跑在合同覆盖的三格之一");
        let contract = Contract::embedded().unwrap();
        let cap = contract.capability("对话").unwrap();
        assert!(cap.os.contains_key(platform), "{platform}");
    }
}
