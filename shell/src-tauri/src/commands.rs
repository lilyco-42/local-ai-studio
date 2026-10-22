use std::fs;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;
use serde_json::{json, Map, Value};
use tauri::{AppHandle, Emitter, State};

use local_ai_studio_core::base64;
use local_ai_studio_core::bundled;
use local_ai_studio_core::delivery::{install, Delivery};
use local_ai_studio_core::download::{CurlFetcher, Policy};
use local_ai_studio_core::hardware::Hardware;
use local_ai_studio_core::http::MAX_BODY_BYTES;
use local_ai_studio_core::hub::{self, HubView};
use local_ai_studio_core::payload_fetch::{
    fetch_manifest, fetch_object, load_source, SOURCE_NAME,
};
use local_ai_studio_core::starter::{self, StarterPin};
use local_ai_studio_core::{OcrRequest, Status};

use crate::state::AppState;

const OCR_PREVIEW_BYTES: usize = 200_000;

#[derive(Serialize)]
pub struct OcrOut {
    pub file: String,
    pub text: String,
}

#[derive(Serialize)]
pub struct ImageOut {
    pub data: String,
    pub path: String,
}

fn lock_broken() -> String {
    "进程表锁坏了（之前有一次 panic）".to_string()
}

fn stamp() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}

fn safe_name(name: &str, fallback: &str) -> String {
    let base = name
        .rsplit(|c| c == '/' || c == '\\')
        .next()
        .unwrap_or("");
    let cleaned: String = base
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '_' | '-' | '.'))
        .collect();
    if cleaned.is_empty() || cleaned == "." || cleaned == ".." {
        fallback.to_string()
    } else {
        cleaned
    }
}

#[tauri::command]
pub fn status(state: State<'_, AppState>) -> Result<Vec<Status>, String> {
    let mut sup = state.supervisor.lock().map_err(|_| lock_broken())?;
    Ok(sup.status())
}

#[tauri::command]
pub fn data_root(state: State<'_, AppState>) -> String {
    state.data_root.display().to_string()
}

#[tauri::command]
pub async fn start_engine(state: State<'_, AppState>, capability: String) -> Result<u16, String> {
    let sup = state.supervisor.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut guard = sup.lock().map_err(|_| lock_broken())?;
        guard
            .ensure_running(&capability)
            .map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| format!("启动任务失败: {e}"))?
}

#[tauri::command]
pub fn stop_engine(state: State<'_, AppState>, capability: String) -> Result<(), String> {
    let mut sup = state.supervisor.lock().map_err(|_| lock_broken())?;
    sup.stop(&capability);
    Ok(())
}

#[tauri::command]
pub async fn chat(state: State<'_, AppState>, body: Value) -> Result<Value, String> {
    let sup = state.supervisor.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut guard = sup.lock().map_err(|_| lock_broken())?;
        guard.chat(body).map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| format!("对话任务失败: {e}"))?
}

#[tauri::command]
pub async fn transcribe(
    state: State<'_, AppState>,
    filename: String,
    content_type: String,
    audio: Vec<u8>,
    language: Option<String>,
) -> Result<String, String> {
    if audio.len() > MAX_BODY_BYTES {
        return Err(format!(
            "音频 {} 字节，超过上限 {}",
            audio.len(),
            MAX_BODY_BYTES
        ));
    }
    let filename = safe_name(&filename, "audio.bin");
    let content_type = if content_type.trim().is_empty() {
        "application/octet-stream".to_string()
    } else {
        content_type
    };
    let sup = state.supervisor.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let mut guard = sup.lock().map_err(|_| lock_broken())?;
        guard
            .transcribe(&filename, &content_type, &audio, language.as_deref())
            .map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| format!("字幕任务失败: {e}"))?
}

#[tauri::command]
pub async fn image(
    state: State<'_, AppState>,
    prompt: String,
    steps: Option<u32>,
    width: Option<u32>,
    height: Option<u32>,
) -> Result<ImageOut, String> {
    let prompt = prompt.trim().to_string();
    if prompt.is_empty() {
        return Err("提示词是空的".to_string());
    }
    let mut extra = Map::new();
    if let Some(v) = steps {
        extra.insert("steps".into(), json!(v));
    }
    if let Some(v) = width {
        extra.insert("width".into(), json!(v));
    }
    if let Some(v) = height {
        extra.insert("height".into(), json!(v));
    }
    let sup = state.supervisor.clone();
    let png = tauri::async_runtime::spawn_blocking(move || {
        let mut guard = sup.lock().map_err(|_| lock_broken())?;
        guard.image(&prompt, extra).map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| format!("生图任务失败: {e}"))??;
    let dir = state.data_root.join("outputs");
    fs::create_dir_all(&dir).map_err(|e| format!("建不了输出目录 {}: {e}", dir.display()))?;
    let path = dir.join(format!("image-{}.png", stamp()));
    fs::write(&path, &png).map_err(|e| format!("写不了 {}: {e}", path.display()))?;
    Ok(ImageOut {
        data: format!("data:image/png;base64,{}", base64::encode(&png)),
        path: path.display().to_string(),
    })
}

#[tauri::command]
pub async fn ocr(
    state: State<'_, AppState>,
    name: String,
    image: Vec<u8>,
    det: Vec<u8>,
    rec: Vec<u8>,
    dict: Vec<u8>,
) -> Result<Vec<OcrOut>, String> {
    for (label, bytes) in [
        ("image", &image),
        ("det", &det),
        ("rec", &rec),
        ("dict", &dict),
    ] {
        if bytes.is_empty() {
            return Err(format!("{label} 是空文件"));
        }
        if bytes.len() > MAX_BODY_BYTES {
            return Err(format!("{label} 超过单文件上限 {MAX_BODY_BYTES}"));
        }
    }
    let run = state
        .data_root
        .join("inputs")
        .join(format!("run-{}", stamp()));
    fs::create_dir_all(&run).map_err(|e| format!("建不了输入目录 {}: {e}", run.display()))?;
    let image_path = run.join(safe_name(&name, "image.png"));
    let det_path = run.join(safe_name("det.onnx", "det.onnx"));
    let rec_path = run.join(safe_name("rec.onnx", "rec.onnx"));
    let dict_path = run.join(safe_name("dict.txt", "dict.txt"));
    for (path, bytes) in [
        (&image_path, &image),
        (&det_path, &det),
        (&rec_path, &rec),
        (&dict_path, &dict),
    ] {
        fs::write(path, bytes).map_err(|e| format!("写不了 {}: {e}", path.display()))?;
    }
    let out_dir = state.data_root.join("outputs").join(format!("ocr-{}", stamp()));
    let sup = state.supervisor.clone();
    let paths = tauri::async_runtime::spawn_blocking(move || {
        let mut guard = sup.lock().map_err(|_| lock_broken())?;
        guard
            .ocr(&OcrRequest {
                image: &image_path,
                out_dir: &out_dir,
                det: &det_path,
                rec: &rec_path,
                dict: &dict_path,
            })
            .map_err(|e| e.to_string())
    })
    .await
    .map_err(|e| format!("识别任务失败: {e}"))??;
    let mut rows = Vec::new();
    for path in paths {
        let bytes = fs::read(&path).map_err(|e| format!("读不了 {}: {e}", path.display()))?;
        let cut = bytes.len().min(OCR_PREVIEW_BYTES);
        rows.push(OcrOut {
            file: path.display().to_string(),
            text: String::from_utf8_lossy(&bytes[..cut]).into_owned(),
        });
    }
    Ok(rows)
}

#[derive(Serialize)]
pub struct PayloadSourceOut {
    /// `remote` = 数据根里写了源配置；`bundled` = 安装包自带运行组件。
    pub mode: String,
    pub manifest_url: String,
    pub bases: Vec<String>,
}

#[derive(Serialize)]
pub struct InstallOut {
    pub platform: String,
    pub written: usize,
    pub skipped: usize,
    pub mode: String,
}

/// 远程源是用户的显式配置，优先；没配才用包内资源。
fn source_mode(has_remote: bool, has_bundled: bool) -> Option<&'static str> {
    if has_remote {
        Some("remote")
    } else if has_bundled {
        Some("bundled")
    } else {
        None
    }
}

#[derive(Clone, Serialize)]
struct PayloadProgress {
    seen: usize,
    total: usize,
    key: String,
}

/// 载荷从哪来：数据根里的源配置优先，没配就看安装包带没带；都没有就是 `null`，
/// UI 据此决定显不显示安装入口。
#[tauri::command]
pub fn payload_source(state: State<'_, AppState>) -> Result<Option<PayloadSourceOut>, String> {
    let path = state.data_root.join(SOURCE_NAME);
    let source = load_source(&path).map_err(|e| e.to_string())?;
    Ok(match source_mode(source.is_some(), state.bundled.is_some()) {
        Some("remote") => source.map(|s| PayloadSourceOut {
            mode: "remote".into(),
            manifest_url: s.manifest_url,
            bases: s.bases,
        }),
        Some(_) => Some(PayloadSourceOut {
            mode: "bundled".into(),
            manifest_url: String::new(),
            bases: Vec::new(),
        }),
        None => None,
    })
}

/// 首启安装：有 `<数据根>/payload-source.json` 就按它下载；没有就用安装包自带的
/// 对象键布局。两条路都走 `delivery::install`，逐文件验哈希、根清单最后写。
/// `.objects` 只属于远程路径，是断点续传的缓存；装完才删，失败时留着下一次接着取。
#[tauri::command]
pub async fn install_payload(
    app: AppHandle,
    state: State<'_, AppState>,
) -> Result<InstallOut, String> {
    let data_root = state.data_root.clone();
    let bundled_root = state.bundled.clone();
    tauri::async_runtime::spawn_blocking(move || {
        let platform = crate::state::platform_label()
            .ok_or_else(|| format!("合同里没有 {} 这一格", std::env::consts::OS))?;
        let source_path = data_root.join(SOURCE_NAME);
        let source = load_source(&source_path).map_err(|e| e.to_string())?;
        if let Some(source) = source {
            let bases = source.checked_bases().map_err(|e| e.to_string())?;
            let mut fetcher = CurlFetcher::new();
            let objects = data_root.join(".objects");
            let scratch = objects.join("delivery-manifest.json.part");
            let body = fetch_manifest(&source.manifest_url, &scratch, &mut fetcher)
                .map_err(|e| e.to_string())?;
            let text = String::from_utf8(body).map_err(|e| format!("交付清单不是 UTF-8: {e}"))?;
            let delivery = Delivery::parse(&text).map_err(|e| e.to_string())?;
            delivery.check(platform).map_err(|e| e.to_string())?;

            let total = delivery.files.len() + 1;
            let mut seen = 0usize;
            let report = install(&delivery, platform, &data_root, |key| {
                let (bytes, sha) = delivery.expected(key)?;
                let fetched = fetch_object(
                    &bases,
                    key,
                    bytes,
                    sha,
                    &objects,
                    &mut fetcher,
                    Policy::default(),
                );
                seen += 1;
                let _ = app.emit(
                    "payload-progress",
                    PayloadProgress {
                        seen,
                        total,
                        key: key.to_string(),
                    },
                );
                fetched
            })
            .map_err(|e| e.to_string())?;
            let _ = fs::remove_dir_all(&objects);
            return Ok(InstallOut {
                platform: platform.to_string(),
                written: report.written,
                skipped: report.skipped,
                mode: "remote".into(),
            });
        }
        let root = bundled_root.ok_or_else(|| {
            format!(
                "没有载荷来源：数据根里没有 {}，安装包里也没有 bundled/{}",
                source_path.display(),
                local_ai_studio_core::delivery::MANIFEST_NAME
            )
        })?;
        let report = bundled::install_from(&root, platform, &data_root)
            .map_err(|e| e.to_string())?;
        let total = report.written + report.skipped;
        let _ = app.emit(
            "payload-progress",
            PayloadProgress {
                seen: total,
                total,
                key: "bundled".to_string(),
            },
        );
        Ok(InstallOut {
            platform: platform.to_string(),
            written: report.written,
            skipped: report.skipped,
            mode: "bundled".into(),
        })
    })
    .await
    .map_err(|e| format!("安装任务失败: {e}"))?
}

#[derive(Serialize)]
pub struct StarterOut {
    pub installed: bool,
    pub id: String,
    pub file: String,
    pub bytes: u64,
    pub size_mib: u64,
    pub license: String,
    pub ram_mib_min: u64,
    pub path: Option<String>,
}

fn starter_view(pin: &StarterPin, data_root: &std::path::Path) -> StarterOut {
    let path = starter::installed_path(pin, data_root);
    StarterOut {
        installed: path.is_some(),
        id: pin.model.id.clone(),
        file: pin.model.file.clone(),
        bytes: pin.model.bytes,
        size_mib: pin.size_mib(),
        license: pin.model.license.clone(),
        ram_mib_min: pin.fit.ram_mib_min,
        path: path.map(|p| p.display().to_string()),
    }
}

#[tauri::command]
pub fn hardware_profile(state: State<'_, AppState>) -> Hardware {
    state.hardware.clone()
}

#[tauri::command]
pub fn starter_status(state: State<'_, AppState>) -> StarterOut {
    starter_view(&state.starter, &state.data_root)
}

/// 只下这只权重，不碰引擎载荷；装好之后把"对话"停一次，下一次启动就换用它。
#[tauri::command]
pub async fn starter_install(state: State<'_, AppState>) -> Result<StarterOut, String> {
    let data_root = state.data_root.clone();
    let supervisor = state.supervisor.clone();
    let pin = state.starter.clone();
    let task_pin = pin.clone();
    let path = tauri::async_runtime::spawn_blocking(move || {
        let mut fetcher = CurlFetcher::new();
        let (path, _) = starter::install(&task_pin, &data_root, &mut fetcher, Policy::default())
            .map_err(|e| e.to_string())?;
        // 527 MB 的整文件复核也放在阻塞线程里，别堵住 async worker。
        starter::verify(&path, task_pin.model.bytes, &task_pin.model.sha256)
            .map_err(|e| e.to_string())?;
        Ok::<_, String>(path)
    })
    .await
    .map_err(|e| format!("入门模型下载任务失败: {e}"))??;
    {
        let mut guard = supervisor.lock().map_err(|_| lock_broken())?;
        guard.stop("对话");
        guard.set_starter_model(Some(path));
    }
    Ok(starter_view(&pin, &state.data_root))
}

#[tauri::command]
pub fn hub_config(state: State<'_, AppState>) -> Result<HubView, String> {
    hub::load(&state.data_root)
        .map(|config| config.view())
        .map_err(|e| e.to_string())
}

/// 只写开关与展示用的命令；不连接、不登录、不碰积分。
#[tauri::command]
pub fn hub_save(
    state: State<'_, AppState>,
    enabled: bool,
    ssh_user: String,
    ssh_key: String,
) -> Result<HubView, String> {
    // 配置文件坏了也要能靠一次显式保存恢复，而不是把用户锁在报错页。
    let mut config = hub::load(&state.data_root).unwrap_or_default();
    config.enabled = enabled;
    config.ssh_user = ssh_user.trim().to_string();
    config.ssh_key = ssh_key.trim().to_string();
    hub::save(&state.data_root, config)
        .map(|saved| saved.view())
        .map_err(|e| e.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn names_cannot_walk_out_of_the_run_dir() {
        assert_eq!(safe_name("../a.wav", "x"), "a.wav");
        assert_eq!(safe_name("C:\\tmp\\b.png", "x"), "b.png");
        assert_eq!(safe_name("..", "fallback"), "fallback");
        assert_eq!(safe_name("", "fallback"), "fallback");
    }

    #[test]
    fn a_stamp_is_monotonic_enough_to_be_nonzero() {
        assert!(stamp() > 0);
    }

    #[test]
    fn an_explicit_remote_source_wins_over_the_bundled_one() {
        assert_eq!(source_mode(true, true), Some("remote"));
        assert_eq!(source_mode(true, false), Some("remote"));
        assert_eq!(source_mode(false, true), Some("bundled"));
        assert_eq!(source_mode(false, false), None);
    }
}
