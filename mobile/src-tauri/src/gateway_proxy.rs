//! Custom `zcharlie://` protocol: proxies every WebView request for the
//! embedded research console through the gateway.
//!
//! The WebView loads `zcharlie://localhost/app`; wry hands the handler
//! `zcharlie://localhost/<path>` and expects a full buffered body (Android
//! cannot stream scheme responses — the embedded web UI already polls
//! instead of SSE for exactly this reason). Two platform constraints shape
//! the design: Android aborts scheme requests after ~30s (client timeout is
//! 20s), and wry drops 3xx responses entirely, so redirects from the backend
//! are chased here and re-prefixed onto the gateway proxy path.

use std::sync::OnceLock;

use serde::Deserialize;

// 凭证由 JS 侧经 tauri-plugin-secure-keystore 加密写入（Android Keystore
// AES-256-GCM / iOS Keychain），Rust 侧读同一键做每次代理请求的鉴权。
const STORE_KEY: &str = "gateway-pairing";
const MAX_REDIRECTS: usize = 5;
const REQUEST_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(20);

/// WebView request headers forwarded to the gateway. `authorization` and
/// `cookie` are deliberately excluded: the device credential is injected per
/// hop and the app keeps no server session.
const FORWARD_HEADERS: &[&str] = &[
    "accept",
    "accept-language",
    "content-type",
    "if-modified-since",
    "if-none-match",
    "last-event-id",
    "range",
    "x-request-id",
    "x-requested-with",
];

/// Gateway response headers dropped when mapping back onto the scheme. Hop-
/// by-hop headers and content-length are re-derived by the stack, and
/// set-cookie must never land in the WebView's cookie jar.
const DROP_HEADERS: &[&str] = &[
    "connection",
    "content-length",
    "keep-alive",
    "set-cookie",
    "strict-transport-security",
    "transfer-encoding",
    "upgrade",
];

#[derive(Deserialize, Clone)]
struct Pairing {
    #[serde(rename = "gatewayUrl")]
    gateway_url: String,
    #[serde(rename = "backendId")]
    backend_id: String,
    #[serde(rename = "deviceToken")]
    device_token: String,
}

fn read_pairing(app: &tauri::AppHandle) -> Option<Pairing> {
    let response = tauri_plugin_secure_keystore::SecureKeystoreExt::secure_keystore(app)
        .get_item(tauri_plugin_secure_keystore::ItemKey {
            key: STORE_KEY.to_string(),
        })
        .ok()?;
    serde_json::from_str(&response.value?).ok()
}

fn client() -> &'static reqwest::Client {
    static CLIENT: OnceLock<reqwest::Client> = OnceLock::new();
    CLIENT.get_or_init(|| {
        reqwest::Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(REQUEST_TIMEOUT)
            .build()
            .expect("static reqwest client config")
    })
}

/// Handler registered via `register_asynchronous_uri_scheme_protocol`.
pub fn handle(
    ctx: tauri::UriSchemeContext<'_, tauri::Wry>,
    request: tauri::http::Request<Vec<u8>>,
    responder: tauri::UriSchemeResponder,
) {
    let app = ctx.app_handle().clone();
    tauri::async_runtime::spawn(async move {
        let response = proxy(&app, request).await;
        responder.respond(response);
    });
}

async fn proxy(app: &tauri::AppHandle, request: tauri::http::Request<Vec<u8>>) -> tauri::http::Response<Vec<u8>> {
    let Some(pairing) = read_pairing(app) else {
        return text_response(
            tauri::http::StatusCode::SERVICE_UNAVAILABLE,
            "text/html; charset=utf-8",
            "<!doctype html><meta charset=\"utf-8\"><p>尚未配对网关，请回到 zcharlie 完成配对。</p>",
        );
    };
    let (parts, body) = request.into_parts();
    let path_and_query = parts
        .uri
        .path_and_query()
        .map(|pq| pq.as_str().to_string())
        .unwrap_or_else(|| "/".to_string());

    let (gateway_origin, backend_id, proxy_base) = match decompose_pairing(&pairing) {
        Ok(parts) => parts,
        Err(message) => {
            return text_response(tauri::http::StatusCode::BAD_GATEWAY, "text/plain; charset=utf-8", message)
        }
    };

    let mut url = match proxy_target(&gateway_origin, &backend_id, &path_and_query) {
        Ok(url) => url,
        Err(message) => {
            return text_response(tauri::http::StatusCode::BAD_REQUEST, "text/plain; charset=utf-8", message)
        }
    };

    let mut method = parts.method;
    let mut payload = body;
    if method != tauri::http::Method::GET && method != tauri::http::Method::HEAD {
        // The Android WebView drops request bodies on intercepted requests; the
        // embedded web UI moves the JSON payload into this header (api.js).
        if let Some(encoded) = parts.headers.get("x-zcharlie-body").and_then(|v| v.to_str().ok()) {
            match decode_base64(encoded) {
                Some(decoded) => payload = decoded,
                None => {
                    return text_response(
                        tauri::http::StatusCode::BAD_REQUEST,
                        "text/plain; charset=utf-8",
                        "请求体编码无效。",
                    )
                }
            }
        }
    }
    for _ in 0..=MAX_REDIRECTS {
        let outbound =
            build_outbound(&method, &url, &parts.headers, &pairing.device_token, payload.clone()).await;
        let response = match outbound {
            Ok(response) => response,
            Err(_) => {
                return text_response(
                    tauri::http::StatusCode::BAD_GATEWAY,
                    "text/plain; charset=utf-8",
                    "网关不可达或请求超时。",
                )
            }
        };
        let status = response.status();
        if status.is_redirection() {
            let Some(next) = response
                .headers()
                .get(reqwest::header::LOCATION)
                .and_then(|value| value.to_str().ok())
                .and_then(|value| rewrite_location(&url, &proxy_base, value))
            else {
                return text_response(
                    tauri::http::StatusCode::BAD_GATEWAY,
                    "text/plain; charset=utf-8",
                    "网关返回了无法跟随的重定向。",
                );
            };
            url = next;
            // 301/302/303 downgrade to GET; 307/308 keep method and body.
            if matches!(
                status,
                reqwest::StatusCode::MOVED_PERMANENTLY
                    | reqwest::StatusCode::FOUND
                    | reqwest::StatusCode::SEE_OTHER
            ) {
                method = tauri::http::Method::GET;
                payload = Vec::new();
            }
            continue;
        }
        return map_response(response).await;
    }
    text_response(
        tauri::http::StatusCode::BAD_GATEWAY,
        "text/plain; charset=utf-8",
        "重定向次数过多。",
    )
}

type PairingParts = (String, String, String);

fn decompose_pairing(pairing: &Pairing) -> Result<PairingParts, &'static str> {
    let gateway_origin = clean_gateway_url(&pairing.gateway_url)?;
    if pairing.backend_id.is_empty() {
        return Err("配对数据缺少服务 ID。");
    }
    if pairing.device_token.is_empty() {
        return Err("配对数据缺少设备凭证。");
    }
    let proxy_base = format!("/api/proxy/{}", percent_encode_id(&pairing.backend_id));
    Ok((gateway_origin, pairing.backend_id.clone(), proxy_base))
}

/// Mirrors `cleanGatewayUrl` in src/transport.ts: only a bare http(s) origin.
fn clean_gateway_url(raw: &str) -> Result<String, &'static str> {
    let parsed = url::Url::parse(raw.trim()).map_err(|_| "网关地址无效。")?;
    if parsed.scheme() != "http" && parsed.scheme() != "https" {
        return Err("网关地址必须使用 HTTP 或 HTTPS。");
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("网关地址不能包含账号密码。");
    }
    if parsed.path() != "/" || parsed.query().is_some() || parsed.fragment().is_some() {
        return Err("请输入网关主机地址，不要附加路径或查询参数。");
    }
    Ok(parsed.origin().ascii_serialization())
}

/// Mirrors the path rules of `proxyUrl` in src/transport.ts, then composes the
/// gateway proxy URL and re-checks containment after URL normalization (so a
/// crafted `..` cannot climb out of `/api/proxy/<id>/`).
fn proxy_target(
    gateway_origin: &str,
    backend_id: &str,
    path_and_query: &str,
) -> Result<url::Url, &'static str> {
    if !path_and_query.starts_with('/')
        || path_and_query.starts_with("//")
        || path_and_query.contains('\\')
    {
        return Err("路径格式无效。");
    }
    let proxy_base = format!("/api/proxy/{}", percent_encode_id(backend_id));
    let composed = format!("{gateway_origin}{proxy_base}{path_and_query}");
    let parsed = url::Url::parse(&composed).map_err(|_| "路径格式无效。")?;
    let expected_origin = url::Url::parse(gateway_origin)
        .expect("gateway origin validated earlier")
        .origin();
    let in_scope = parsed.path() == proxy_base || parsed.path().starts_with(&format!("{proxy_base}/"));
    if parsed.origin() != expected_origin || !in_scope {
        return Err("路径超出当前服务范围。");
    }
    Ok(parsed)
}

/// Resolves a backend `Location` against the current proxy URL and re-prefixes
/// it onto the proxy base. The backend only ever emits same-host root-relative
/// redirects (`/app`, `/app/login`, …); any other host component is discarded.
fn rewrite_location(current: &url::Url, proxy_base: &str, location: &str) -> Option<url::Url> {
    let resolved = current.join(location).ok()?;
    let mut rewritten = url::Url::parse(&current.origin().ascii_serialization()).ok()?;
    let path = resolved.path();
    let in_scope = path == proxy_base || path.starts_with(&format!("{proxy_base}/"));
    let target = if in_scope {
        path.to_string()
    } else {
        format!("{proxy_base}{path}")
    };
    rewritten.set_path(&target);
    rewritten.set_query(resolved.query());
    rewritten.set_fragment(None);
    Some(rewritten)
}

fn decode_base64(encoded: &str) -> Option<Vec<u8>> {
    use base64::Engine as _;
    base64::engine::general_purpose::STANDARD.decode(encoded.trim()).ok()
}

fn percent_encode_id(value: &str) -> String {
    let mut encoded = String::with_capacity(value.len());
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => {
                encoded.push(byte as char)
            }
            _ => encoded.push_str(&format!("%{byte:02X}")),
        }
    }
    encoded
}

async fn build_outbound(
    method: &tauri::http::Method,
    url: &url::Url,
    forward: &tauri::http::HeaderMap,
    device_token: &str,
    body: Vec<u8>,
) -> Result<reqwest::Response, reqwest::Error> {
    let mut request = client().request(method.clone(), url.clone());
    for name in FORWARD_HEADERS {
        if let Some(value) = forward.get(*name) {
            request = request.header(*name, value);
        }
    }
    request = request.bearer_auth(device_token);
    if !body.is_empty() || method != tauri::http::Method::GET {
        request = request.body(body);
    }
    request.send().await
}

async fn map_response(response: reqwest::Response) -> tauri::http::Response<Vec<u8>> {
    let status = response.status();
    let headers = response.headers().clone();
    let body = response.bytes().await.unwrap_or_default();
    let builder = tauri::http::Response::builder()
        .status(tauri::http::StatusCode::from_u16(status.as_u16()).unwrap_or(tauri::http::StatusCode::BAD_GATEWAY));
    let mut builder = builder;
    for (name, value) in headers.iter() {
        let name = name.as_str();
        if DROP_HEADERS.contains(&name) {
            continue;
        }
        builder = builder.header(name, value);
    }
    builder.body(body.to_vec()).unwrap_or_else(|_| {
        text_response(
            tauri::http::StatusCode::INTERNAL_SERVER_ERROR,
            "text/plain; charset=utf-8",
            "响应映射失败。",
        )
    })
}

fn text_response(
    status: tauri::http::StatusCode,
    content_type: &str,
    message: &str,
) -> tauri::http::Response<Vec<u8>> {
    tauri::http::Response::builder()
        .status(status)
        .header("content-type", content_type)
        .header("cache-control", "no-store")
        .body(message.as_bytes().to_vec())
        .expect("static text response")
}

#[cfg(test)]
mod tests {
    use super::*;

    const GW: &str = "http://10.0.2.2:3400";
    const ID: &str = "c5e70e4d-1368-47ca-9a16-130aa53ba411";

    fn target(path: &str) -> Result<String, String> {
        proxy_target(GW, ID, path).map_err(str::to_string).map(|url| url.to_string())
    }

    #[test]
    fn clean_gateway_url_accepts_bare_origins_only() {
        assert_eq!(clean_gateway_url("http://10.0.2.2:3400/").unwrap(), GW);
        assert_eq!(
            clean_gateway_url("  https://gw.example.com  ").unwrap(),
            "https://gw.example.com"
        );
        assert!(clean_gateway_url("ftp://gw").is_err());
        assert!(clean_gateway_url("http://u:p@gw").is_err());
        assert!(clean_gateway_url("http://gw/path").is_err());
        assert!(clean_gateway_url("http://gw/?x=1").is_err());
        assert!(clean_gateway_url("not a url").is_err());
    }

    #[test]
    fn proxy_target_maps_paths_under_proxy_prefix() {
        assert_eq!(target("/app").unwrap(), format!("{GW}/api/proxy/{ID}/app"));
        assert_eq!(
            target("/app/static/app.css").unwrap(),
            format!("{GW}/api/proxy/{ID}/app/static/app.css")
        );
        assert_eq!(
            target("/agent/tasks/abc/events?transport=poll&after_event_id=7").unwrap(),
            format!("{GW}/api/proxy/{ID}/agent/tasks/abc/events?transport=poll&after_event_id=7")
        );
        assert_eq!(target("/").unwrap(), format!("{GW}/api/proxy/{ID}/"));
    }

    #[test]
    fn proxy_target_rejects_escape_attempts() {
        assert!(target("app").is_err()); // no leading slash
        assert!(target("//evil.example/app").is_err()); // scheme-relative
        assert!(target("/app\\..\\x").is_err()); // backslash
        // Dot segments that normalize out of the proxy prefix are refused…
        assert!(target("/../../other/app").is_err());
        assert!(target("/a/../../etc/passwd").is_err());
        // …while anything staying inside the prefix is in scope (the backend
        // owns 404s for nested-looking paths).
        assert!(target("/api/proxy/other/app").is_ok());
    }

    #[test]
    fn percent_encode_id_escapes_non_unreserved() {
        assert_eq!(percent_encode_id(ID), ID);
        assert_eq!(percent_encode_id("a b/c"), "a%20b%2Fc");
    }

    #[test]
    fn decode_base64_roundtrips_utf8_json() {
        let original = "请介绍一下确定性研究的概念 {\"a\":1}";
        let encoded = "6K+35LuL57uN5LiA5LiL56Gu5a6a5oCn56CU56m255qE5qaC5b+1IHsiYSI6MX0=";
        assert_eq!(
            decode_base64(encoded).map(|bytes| String::from_utf8(bytes).unwrap()),
            Some(original.to_string())
        );
        assert_eq!(decode_base64("not!valid@base64"), None);
    }

    #[test]
    fn rewrite_location_prefixes_backend_redirects() {
        let current = url::Url::parse(&format!("{GW}/api/proxy/{ID}/app/login")).unwrap();
        let base = format!("/api/proxy/{ID}");
        let rewritten = rewrite_location(&current, &base, "/app").unwrap().to_string();
        assert_eq!(rewritten, format!("{GW}/api/proxy/{ID}/app"));
        // Already-prefixed locations pass through unchanged.
        let again = rewrite_location(&current, &base, &format!("/api/proxy/{ID}/app"))
            .unwrap()
            .to_string();
        assert_eq!(again, format!("{GW}/api/proxy/{ID}/app"));
        // Relative locations resolve against the current directory (join
        // replaces the last path segment).
        let relative = rewrite_location(&current, &base, "static/app.css")
            .unwrap()
            .to_string();
        assert_eq!(relative, format!("{GW}/api/proxy/{ID}/app/static/app.css"));
        // Queries survive; fragments are dropped.
        let queried = rewrite_location(&current, &base, "/app?next=%2Fapp%2Fruns")
            .unwrap()
            .to_string();
        assert_eq!(queried, format!("{GW}/api/proxy/{ID}/app?next=%2Fapp%2Fruns"));
        let fragment = rewrite_location(&current, &base, "/app#main").unwrap().to_string();
        assert_eq!(fragment, format!("{GW}/api/proxy/{ID}/app"));
    }
}
