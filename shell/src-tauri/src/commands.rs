use std::fs;
use std::time::{SystemTime, UNIX_EPOCH};

use serde::Serialize;
use serde_json::{json, Map, Value};
use tauri::State;

use local_ai_studio_core::base64;
use local_ai_studio_core::http::MAX_BODY_BYTES;
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
}
