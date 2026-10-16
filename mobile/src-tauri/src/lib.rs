mod gateway_proxy;

#[tauri::command]
fn is_debug_build() -> bool {
    cfg!(debug_assertions)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_http::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_store::Builder::new().build())
        .plugin(tauri_plugin_secure_keystore::init())
        .invoke_handler(tauri::generate_handler![is_debug_build])
        .register_asynchronous_uri_scheme_protocol("zcharlie", gateway_proxy::handle)
        .run(tauri::generate_context!())
        .expect("failed to run zcharlie mobile")
}
