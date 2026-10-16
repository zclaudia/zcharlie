//! IPC 桥：内嵌研究台经 Tauri IPC 直达 Rust 的代理通道。
//!
//! 安卓 WebView 拦截层对页面 fetch"无进无出"（请求体拿不到、响应必须整包缓冲），
//! 头桥与轮询都是绕这两条限制的补丁；IPC 不经过拦截器，两条都根治。安全边界与
//! `zcharlie://` 协议 handler 完全一致：路径同一套校验（gateway_proxy::proxy_target）、
//! 设备凭证每请求现读现注入、页面永远拿不到 token。错误以稳定字符串码返回，
//! JS 侧（api.js）据此决定抛错或回退头桥。

use serde::Serialize;

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
    let pairing = crate::gateway_proxy::load_pairing_context(&app)?;
    let url = crate::gateway_proxy::proxy_target(&pairing.gateway_origin, &pairing.backend_id, &path)?;

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
    fn bridge_response_handles_bare_responses() {
        let mapped = bridge_response(204, None, None, Vec::new());
        assert_eq!(mapped.status, 204);
        assert_eq!(mapped.content_type, None);
        assert_eq!(mapped.retry_after, None);
        assert_eq!(mapped.body, "");
    }
}
