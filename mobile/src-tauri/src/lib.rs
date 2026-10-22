mod bridge;
mod gateway_proxy;

/// Additive v1 contract. Keep these commands and argument/response fields
/// compatible with web releases delivered independently from this APK.
#[derive(serde::Serialize)]
struct BridgeCapabilities {
    bridge_version: u32,
    app_version: &'static str,
    capabilities: [&'static str; 3],
}

#[tauri::command]
fn bridge_capabilities() -> BridgeCapabilities {
    BridgeCapabilities {
        bridge_version: 1,
        app_version: env!("CARGO_PKG_VERSION"),
        capabilities: ["proxy_request", "stream_task_events", "move_task_to_back"],
    }
}

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
        .invoke_handler(tauri::generate_handler![
            bridge_capabilities,
            bridge::move_task_to_back,
            bridge::proxy_request,
            bridge::stream_task_events,
            gateway_proxy::invalidate_pairing_cache,
            is_debug_build,
        ])
        .register_asynchronous_uri_scheme_protocol("zcharlie", gateway_proxy::handle)
        .run(tauri::generate_context!())
        .expect("failed to run zcharlie mobile")
}
