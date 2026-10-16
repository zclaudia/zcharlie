//! IPC 桥：内嵌研究台经 Tauri IPC 直达 Rust 的代理通道。
//!
//! 安卓 WebView 拦截层对页面 fetch"无进无出"（请求体拿不到、响应必须整包缓冲），
//! 头桥与轮询都是绕这两条限制的补丁；IPC 不经过拦截器，两条都根治。安全边界与
//! `zcharlie://` 协议 handler 完全一致：路径同一套校验（gateway_proxy::proxy_target）、
//! 设备凭证每请求现读现注入、页面永远拿不到 token。错误以稳定字符串码返回，
//! JS 侧（api.js）据此决定抛错或回退头桥。

use serde::Serialize;
use tauri::ipc::Channel;

use crate::gateway_proxy::{load_pairing_context, percent_encode_id, proxy_target};

const BRIDGE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(30);

/// 与协议 handler 分开的客户端：IPC 不受安卓 30s scheme 截止约束，
/// 可以用更长超时；重定向跟随默认策略（同源网关内，Authorization 保留）。
fn bridge_client() -> &'static reqwest::Client {
    static CLIENT: std::sync::OnceLock<reqwest::Client> = std::sync::OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::limited(5))
            .timeout(BRIDGE_TIMEOUT)
            .build()
            .expect("static bridge client config")
    })
}

#[derive(Serialize)]
pub struct BridgeResponse {
    pub status: u16,
    pub content_type: Option<String>,
    pub retry_after: Option<String>,
    pub body: String,
}

/// 纯函数：把最终响应映射为可序列化结构。体只承载 JSON/文本 API（二进制下载
/// 仍走协议 handler，那里的字节通道是完整的）。
fn bridge_response(
    status: u16,
    content_type: Option<String>,
    retry_after: Option<String>,
    body: Vec<u8>,
) -> BridgeResponse {
    BridgeResponse {
        status,
        content_type,
        retry_after,
        body: String::from_utf8_lossy(&body).into_owned(),
    }
}

async fn map_bridge_response(response: reqwest::Response) -> BridgeResponse {
    let status = response.status().as_u16();
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    let retry_after = response
        .headers()
        .get(reqwest::header::RETRY_AFTER)
        .and_then(|v| v.to_str().ok())
        .map(str::to_string);
    let bytes = response.bytes().await.unwrap_or_default().to_vec();
    bridge_response(status, content_type, retry_after, bytes)
}

fn parse_method(raw: &str) -> Result<reqwest::Method, String> {
    match raw.to_ascii_uppercase().as_str() {
        "GET" => Ok(reqwest::Method::GET),
        "POST" => Ok(reqwest::Method::POST),
        "PATCH" => Ok(reqwest::Method::PATCH),
        "PUT" => Ok(reqwest::Method::PUT),
        "DELETE" => Ok(reqwest::Method::DELETE),
        "HEAD" => Ok(reqwest::Method::HEAD),
        _ => Err("invalid_method".into()),
    }
}

#[tauri::command]
pub async fn proxy_request(
    app: tauri::AppHandle,
    method: String,
    path: String,
    body: Option<String>,
    idempotency_key: Option<String>,
) -> Result<BridgeResponse, String> {
    let verb = parse_method(&method)?;
    let pairing = load_pairing_context(&app)?;
    let url = proxy_target(&pairing.gateway_origin, &pairing.backend_id, &path)?;

    let has_body = body.is_some() && verb != reqwest::Method::GET && verb != reqwest::Method::HEAD;
    let mut request = bridge_client()
        .request(verb, url)
        .bearer_auth(&pairing.device_token);
    if let Some(payload) = body.filter(|_| has_body) {
        request = request
            .header(reqwest::header::CONTENT_TYPE, "application/json")
            .body(payload);
    }
    if let Some(key) = idempotency_key.filter(|_| has_body) {
        request = request.header("idempotency-key", key);
    }
    match request.send().await {
        Ok(response) => Ok(map_bridge_response(response).await),
        Err(_) => Err("gateway_unreachable".into()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_method_accepts_api_verbs_only() {
        assert!(matches!(parse_method("get"), Ok(reqwest::Method::GET)));
        assert!(matches!(parse_method("POST"), Ok(reqwest::Method::POST)));
        assert!(matches!(parse_method("patch"), Ok(reqwest::Method::PATCH)));
        assert!(matches!(parse_method("delete"), Ok(reqwest::Method::DELETE)));
        assert!(parse_method("TRACE").is_err());
        assert!(parse_method("CONNECT").is_err());
        assert!(parse_method("").is_err());
    }

    #[test]
    fn bridge_response_carries_status_and_headers() {
        let mapped = bridge_response(
            429,
            Some("application/json".into()),
            Some("10".into()),
            b"{\"error\":\"rate_limited\"}".to_vec(),
        );
        assert_eq!(mapped.status, 429);
        assert_eq!(mapped.content_type.as_deref(), Some("application/json"));
        assert_eq!(mapped.retry_after.as_deref(), Some("10"));
        assert_eq!(mapped.body, "{\"error\":\"rate_limited\"}");
    }

#[test]
    fn take_sse_frame_parses_and_consumes() {
        let mut buffer = String::from("id: 7\nevent: progress\ndata: {\"a\":1}\n\npartial");
        let frame = take_sse_frame(&mut buffer).expect("frame");
        assert_eq!(frame.id, Some(7));
        assert_eq!(frame.event.as_deref(), Some("progress"));
        assert_eq!(frame.data, "{\"a\":1}");
        assert_eq!(buffer, "partial");
    }

    #[test]
    fn take_sse_frame_handles_crlf_and_multiline_data() {
        let mut buffer = String::from("data: line1\r\ndata: line2\r\n\r\nno-full-yet\n");
        let frame = take_sse_frame(&mut buffer).expect("frame");
        assert_eq!(frame.data, "line1\nline2");
        assert_eq!(buffer, "no-full-yet\n");
        assert!(take_sse_frame(&mut buffer).is_none());
    }

    #[test]
    fn take_sse_frame_skips_comments_and_keeps_empty_data() {
        let mut buffer = String::from(": ping\n\n\n");
        let frame = take_sse_frame(&mut buffer).expect("frame");
        assert_eq!(frame.data, "");
        assert_eq!(frame.event, None);
    }

    #[test]
    fn channel_message_shapes_page_contract() {
        assert_eq!(channel_message("expired", ""), "{\"kind\":\"expired\"}");
        assert_eq!(
            channel_message("rate_limited", ",\"retry_after\":7"),
            "{\"kind\":\"rate_limited\",\"retry_after\":7}"
        );
    }

    #[test]
    fn bridge_response_handles_bare_responses() {
        let mapped = bridge_response(204, None, None, Vec::new());
        assert_eq!(mapped.status, 204);
        assert_eq!(mapped.content_type, None);
        assert_eq!(mapped.retry_after, None);
        assert_eq!(mapped.body, "");
    }
}

// ---- WP2：任务事件流（SSE → Channel）---------------------------------------
//
// Rust 侧读网关代理的 SSE 流，逐事件经 Channel 推给内嵌页；轮询降为兜底。
// 只做转发与边界信号：terminal 判定仍由页面既有 task_state 逻辑完成（事件里
// 带终态时页面会 finish），Rust 不重复实现状态机。429/410/连接失败以稳定
// 消息告知页面，由页面决定退避重连或落轮询。

/// 独立流客户端：IPC 不受安卓 scheme 截止约束，但流式请求不能用总超时
/// （会把长任务流掐死）——只限连接与单次读取间隔。
fn stream_client() -> &'static reqwest::Client {
    static CLIENT: std::sync::OnceLock<reqwest::Client> = std::sync::OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::limited(5))
            .connect_timeout(std::time::Duration::from_secs(10))
            .read_timeout(std::time::Duration::from_secs(60))
            .build()
            .expect("static stream client config")
    })
}

#[derive(Debug, PartialEq)]
pub(crate) struct SseFrame {
    pub id: Option<u64>,
    pub event: Option<String>,
    pub data: String,
}

/// 从缓冲头部取一个完整 SSE 帧（空行分隔，兼容 \n\n 与 \r\n\r\n），并从缓冲
/// 移除。没有完整帧返回 None；无 data 的帧（注释/空帧）返回 data 为空。
fn take_sse_frame(buffer: &mut String) -> Option<SseFrame> {
    let lf = buffer.find("\n\n");
    let crlf = buffer.find("\r\n\r\n");
    let (end, skip) = match (lf, crlf) {
        (Some(a), Some(b)) if b < a => (b, 4),
        (Some(a), _) => (a, 2),
        (None, Some(b)) => (b, 4),
        (None, None) => return None,
    };
    let frame: String = buffer[..end]
        .split('\n')
        .map(|line| line.trim_end_matches('\r'))
        .collect::<Vec<_>>()
        .join("\n");
    buffer.drain(..end + skip);
    let mut id = None;
    let mut event = None;
    let mut data: Vec<&str> = Vec::new();
    for line in frame.lines() {
        if line.is_empty() || line.starts_with(':') {
            continue;
        }
        let (field, value) = match line.split_once(':') {
            Some((field, value)) => (field, value.strip_prefix(' ').unwrap_or(value)),
            None => (line, ""),
        };
        match field {
            "id" => {
                if let Ok(parsed) = value.parse::<u64>() {
                    id = Some(parsed);
                }
            }
            "event" => event = Some(value.to_string()),
            "data" => data.push(value),
            _ => {}
        }
    }
    Some(SseFrame {
        id,
        event,
        data: data.join("\n"),
    })
}

/// 页面侧消费的消息封装。data 是后端事件的原始 JSON 字符串。
fn channel_message(kind: &str, extra: &str) -> String {
    format!("{{\"kind\":\"{kind}\"{extra}}}")
}

enum StreamEnd {
    /// 页面已离开（Channel 关闭），线程收尾。
    Closed,
    /// 游标过期：页面应重读快照。
    Expired,
    /// 网关限流：页面按 retry_after 退避后重连。
    RateLimited(u64),
    /// 可重试失败（网络/非 410 HTTP/流中断）：累计 3 次转 degrade。
    Retry,
}

fn run_event_stream(app: tauri::AppHandle, task_id: String, mut cursor: i64, on_event: Channel<String>) {
    let ctx = match load_pairing_context(&app) {
        Ok(ctx) => ctx,
        Err(code) => {
            let _ = on_event.send(channel_message("error", &format!(",\"code\":\"{code}\"")));
            return;
        }
    };
    let mut buffer = String::new();
    let mut failures: u32 = 0;
    loop {
        let path = format!(
            "/agent/tasks/{}/events?after_event_id={}",
            percent_encode_id(&task_id),
            cursor
        );
        let url = match proxy_target(&ctx.gateway_origin, &ctx.backend_id, &path) {
            Ok(url) => url,
            Err(code) => {
                let _ = on_event.send(channel_message("error", &format!(",\"code\":\"{code}\"")));
                return;
            }
        };
        let request = stream_client()
            .get(url)
            .bearer_auth(&ctx.device_token)
            .header(reqwest::header::ACCEPT, "text/event-stream");
        // 整个流式循环包在同一个 block_on 里：tokio 不允许从 runtime 线程内再起
        // block_on（嵌套会 panic 且线程无声死亡，页面侧表现为 0 事件）。
        let outcome = tauri::async_runtime::block_on(async {
            let mut response = match request.send().await {
                Ok(response) => response,
                Err(_) => return StreamEnd::Retry,
            };
            let status = response.status().as_u16();
            if status == 410 {
                return StreamEnd::Expired;
            }
            if status == 429 {
                let retry_after = response
                    .headers()
                    .get(reqwest::header::RETRY_AFTER)
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.parse::<u64>().ok())
                    .unwrap_or(10);
                return StreamEnd::RateLimited(retry_after);
            }
            if !response.status().is_success() {
                return StreamEnd::Retry;
            }
            loop {
                let chunk = match response.chunk().await {
                    Ok(Some(chunk)) => chunk,
                    Ok(None) => return StreamEnd::Retry,
                    Err(_) => return StreamEnd::Retry,
                };
                buffer.push_str(&String::from_utf8_lossy(&chunk));
                while let Some(frame) = take_sse_frame(&mut buffer) {
                    if frame.event.as_deref() == Some("cursor_expired") {
                        return StreamEnd::Expired;
                    }
                    if frame.data.is_empty() {
                        continue;
                    }
                    if let Some(id) = frame.id {
                        cursor = cursor.max(id as i64);
                    }
                    let payload = format!(
                        "{{\"kind\":\"event\",\"data\":{}}}",
                        serde_json::Value::String(frame.data)
                    );
                    if on_event.send(payload).is_err() {
                        return StreamEnd::Closed;
                    }
                }
            }
        });
        match outcome {
            StreamEnd::Closed => return,
            StreamEnd::Expired => {
                let _ = on_event.send(channel_message("expired", ""));
                return;
            }
            StreamEnd::RateLimited(secs) => {
                let _ = on_event.send(channel_message("rate_limited", &format!(",\"retry_after\":{secs}")));
                return;
            }
            StreamEnd::Retry => {
                failures += 1;
                if failures >= 3 {
                    let _ = on_event.send(channel_message("degrade", ""));
                    return;
                }
                let backoff = std::time::Duration::from_millis(500u64 * 2u64.pow(failures - 1));
                std::thread::sleep(backoff);
            }
        }
    }
}


/// 根部 BACK 的原生最小化：AppPlugin 管线下 Kotlin 已消费 BACK，JS 无法自行退栈，
/// 经桥在 WebView 线程调 activity.moveTaskToBack（jni_handle().exec 与 tauri 自家
/// zoomBy 同一条通路）。desktop 下无操作。
#[tauri::command]
pub fn move_task_to_back(app: tauri::AppHandle) -> Result<(), String> {
    #[cfg(target_os = "android")]
    {
        use tauri::Manager;
        let window = app.get_webview_window("main").ok_or("no_webview")?;
        window.with_webview(|platform| {
            platform.jni_handle().exec(
                |env: &mut jni::JNIEnv, activity: &jni::objects::JObject, _webview: &jni::objects::JObject| {
                    let _ = env.call_method(activity, "moveTaskToBack", "(Z)Z", &[jni::objects::JValue::from(true)]);
                },
            );
        });
    }
    #[cfg(not(target_os = "android"))]
    let _ = &app;
    Ok(())
}

#[tauri::command]
pub fn stream_task_events(
    app: tauri::AppHandle,
    task_id: String,
    cursor: i64,
    on_event: Channel<String>,
) -> Result<(), String> {
    if task_id.is_empty() || !task_id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_') {
        return Err("invalid_path".into());
    }
    std::thread::spawn(move || run_event_stream(app, task_id, cursor, on_event));
    Ok(())
}
