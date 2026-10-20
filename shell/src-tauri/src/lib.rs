mod commands;
mod state;

use tauri::Manager;

pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            let state = state::AppState::from_app(app).map_err(std::io::Error::other)?;
            app.manage(state);
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::status,
            commands::data_root,
            commands::start_engine,
            commands::stop_engine,
            commands::chat,
            commands::transcribe,
            commands::image,
            commands::ocr,
        ])
        .run(tauri::generate_context!())
        .expect("Tauri 外壳启动失败");
}
