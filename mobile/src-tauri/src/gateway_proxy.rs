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

use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use serde::Deserialize;

// 凭证由 JS 侧经 tauri-plugin-secure-keystore 加密写入（Android Keystore
// AES-256-GCM / iOS Keychain），Rust 侧读同一键做每次代理请求的鉴权。
const STORE_KEY: &str = "gateway-pairing";
const MAX_REDIRECTS: usize = 5;
const REQUEST_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(20);

/// 静态资源缓存策略：WebView 每次导航都会重新请求 `/app/static`，而安卓
/// WebView 对拦截（shouldInterceptRequest）响应不走 HTTP 缓存，这些请求会
/// 全部落到网关。协议 handler 维护一个进程内小缓存：仅缓存 `/app/static/`
/// 下未跟随重定向的 GET 200，TTL 取后端 `Cache-Control` 的 `max-age`（缺省
/// 10 分钟，上限 1 天，`no-store/no-cache/private` 不缓存）。后端只给带内容
/// 版本的 `/app/static/v/<version>/…` 发长 `max-age`，无版本 URL 是 `no-cache`；
/// 错误响应（429/5xx 等）一律不缓存，否则一次限流会让页面坏 10 分钟。
const STATIC_PREFIX: &str = "/app/static/";
const DEFAULT_STATIC_TTL: Duration = Duration::from_secs(600);
const MAX_STATIC_TTL: Duration = Duration::from_secs(86_400);
const MAX_STATIC_ENTRIES: usize = 128;
const MAX_STATIC_BODY_BYTES: usize = 4 * 1024 * 1024;

type StaticCache = HashMap<String, (Instant, tauri::http::Response<Vec<u8>>)>;

fn static_cache() -> &'static Mutex<StaticCache> {
    static CACHE: OnceLock<Mutex<StaticCache>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

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

/// bridge.rs 复用的配对上下文：handler 每请求现读现用，桥同样。
pub(crate) struct PairingContext {
    pub gateway_origin: String,
    pub backend_id: String,
    pub device_token: String,
}

/// 读配对并分解；任何一步失败都是"未配对/配对损坏"。
pub(crate) fn load_pairing_context(app: &tauri::AppHandle) -> Result<PairingContext, &'static str> {
    let pairing = read_pairing(app).ok_or("pairing_required")?;
    let (gateway_origin, backend_id, _) = decompose_pairing(&pairing)?;
    Ok(PairingContext {
        gateway_origin,
        backend_id,
        device_token: pairing.device_token,
    })
}

/// 配对的进程内缓存：keystore 每次读取都要经 UI 主线程做一次 Android Keystore
/// 解密（模拟器实测 ~12ms/次），而每个 WebView 请求都要读配对。JS 侧写入/删除
/// 配对后调 `invalidate_pairing_cache` 清空；代次号防止失效前已开始的 keystore
/// 读取在失效之后把旧配对写回缓存。
#[derive(Default)]
struct PairingCache {
    generation: u64,
    pairing: Option<Pairing>,
}

impl PairingCache {
    fn invalidate(&mut self) {
        self.generation += 1;
        self.pairing = None;
    }

    /// 只有读取期间没有发生失效（代次未变）才回填。
    fn fill(&mut self, generation: u64, pairing: Pairing) {
        if self.generation == generation {
            self.pairing = Some(pairing);
        }
    }
}

fn pairing_cache() -> std::sync::MutexGuard<'static, PairingCache> {
    static CACHE: OnceLock<Mutex<PairingCache>> = OnceLock::new();
    CACHE
        .get_or_init(|| Mutex::new(PairingCache::default()))
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

#[tauri::command]
pub fn invalidate_pairing_cache() {
    pairing_cache().invalidate();
}

fn read_pairing(app: &tauri::AppHandle) -> Option<Pairing> {
    let generation = {
        let cache = pairing_cache();
        if let Some(pairing) = &cache.pairing {
            return Some(pairing.clone());
        }
        cache.generation
    };
    let response = tauri_plugin_secure_keystore::SecureKeystoreExt::secure_keystore(app)
        .get_item(tauri_plugin_secure_keystore::ItemKey {
            key: STORE_KEY.to_string(),
        })
        .ok()?;
    let pairing: Pairing = serde_json::from_str(&response.value?).ok()?;
    pairing_cache().fill(generation, pairing.clone());
    Some(pairing)
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
    let (parts, body) = request.into_parts();
    let Some(pairing) = read_pairing(app) else {
        return error_response(
            tauri::http::StatusCode::SERVICE_UNAVAILABLE,
            &parts.headers,
            "尚未配对网关，请回到 Charlie 完成配对。",
        );
    };
    let path_and_query = parts
        .uri
        .path_and_query()
        .map(|pq| pq.as_str().to_string())
        .unwrap_or_else(|| "/".to_string());

    let (gateway_origin, backend_id, proxy_base) = match decompose_pairing(&pairing) {
        Ok(parts) => parts,
        Err(message) => {
            return error_response(tauri::http::StatusCode::BAD_GATEWAY, &parts.headers, message)
        }
    };

    let mut url = match proxy_target(&gateway_origin, &backend_id, &path_and_query) {
        Ok(url) => url,
        Err(message) => {
            return error_response(tauri::http::StatusCode::BAD_REQUEST, &parts.headers, message)
        }
    };

    let mut method = parts.method;
    let mut payload = body;

    let cache_key = url.to_string();
    let cacheable = method == tauri::http::Method::GET && is_cacheable_path(&path_and_query);
    if cacheable {
        let mut cache = static_cache().lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some(cached) = cache_lookup(&mut cache, &cache_key) {
            return cached;
        }
    }

    if method != tauri::http::Method::GET && method != tauri::http::Method::HEAD {
        // The Android WebView drops request bodies on intercepted requests; the
        // embedded web UI moves the JSON payload into this header (api.js).
        if let Some(encoded) = parts.headers.get("x-zcharlie-body").and_then(|v| v.to_str().ok()) {
            match decode_base64(encoded) {
                Some(decoded) => payload = decoded,
                None => {
                    return error_response(
                        tauri::http::StatusCode::BAD_REQUEST,
                        &parts.headers,
                        "请求体编码无效。",
                    )
                }
            }
        }
    }
    let mut first_leg = true;
    for _ in 0..=MAX_REDIRECTS {
        let outbound =
            build_outbound(&method, &url, &parts.headers, &pairing.device_token, payload.clone()).await;
        let response = match outbound {
            Ok(response) => response,
            Err(_) => {
                return error_response(
                    tauri::http::StatusCode::BAD_GATEWAY,
                    &parts.headers,
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
                return error_response(
                    tauri::http::StatusCode::BAD_GATEWAY,
                    &parts.headers,
                    "网关返回了无法跟随的重定向。",
                );
            };
            url = next;
            first_leg = false;
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
        let mapped = map_response(response).await;
        if cacheable && first_leg {
            let mut cache = static_cache().lock().unwrap_or_else(|poisoned| poisoned.into_inner());
            cache_store(&mut cache, &cache_key, &mapped, static_ttl(mapped.headers()));
        }
        return mapped;
    }
    error_response(
        tauri::http::StatusCode::BAD_GATEWAY,
        &parts.headers,
        "重定向次数过多。",
    )
}

type PairingParts = (String, String, String);

pub(crate) fn decompose_pairing(pairing: &Pairing) -> Result<PairingParts, &'static str> {
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
pub(crate) fn proxy_target(
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

pub(crate) fn percent_encode_id(value: &str) -> String {
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
    let is_html = headers
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.contains("text/html"));
    let body = response.bytes().await.unwrap_or_default();
    let builder = tauri::http::Response::builder()
        .status(tauri::http::StatusCode::from_u16(status.as_u16()).unwrap_or(tauri::http::StatusCode::BAD_GATEWAY));
    let mut builder = builder;
    for (name, value) in headers.iter() {
        let name = name.as_str();
        if DROP_HEADERS.contains(&name) {
            continue;
        }
        // 内嵌研究台要走 Tauri IPC 桥，而后端 CSP 没有 connect-src（回落
        // default-src 'self'），会拦掉 http://ipc.localhost 的 IPC 请求；
        // 文档响应把 connect-src 追加进既有策略，其余响应原样透传。
        if name == "content-security-policy" && is_html {
            let original = value.to_str().unwrap_or_default();
            builder = builder.header(
                name,
                format!("{original}; connect-src 'self' ipc: http://ipc.localhost"),
            );
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

fn is_cacheable_path(path_and_query: &str) -> bool {
    let path = path_and_query.split('?').next().unwrap_or("");
    path.starts_with(STATIC_PREFIX)
}

/// 响应头 → 缓存 TTL。`no-store`/`no-cache`/`private` 直接不缓存；否则取
/// `max-age` 并夹到上限，缺省 `DEFAULT_STATIC_TTL`。
fn static_ttl(headers: &tauri::http::HeaderMap) -> Duration {
    let mut ttl = DEFAULT_STATIC_TTL;
    for value in headers.get_all("cache-control") {
        let Ok(value) = value.to_str() else { continue };
        for directive in value.split(',') {
            let directive = directive.trim().to_ascii_lowercase();
            if directive == "no-store" || directive == "no-cache" || directive == "private" {
                return Duration::ZERO;
            }
            if let Some(age) = directive.strip_prefix("max-age=") {
                if let Ok(age) = age.trim().parse::<u64>() {
                    ttl = Duration::from_secs(age).min(MAX_STATIC_TTL);
                }
            }
        }
    }
    ttl
}

fn cache_lookup(cache: &mut StaticCache, key: &str) -> Option<tauri::http::Response<Vec<u8>>> {
    let now = Instant::now();
    if let Some((expires_at, response)) = cache.get(key) {
        if now < *expires_at {
            return Some(clone_response(response));
        }
    }
    cache.remove(key);
    None
}

fn cache_store(cache: &mut StaticCache, key: &str, response: &tauri::http::Response<Vec<u8>>, ttl: Duration) {
    if ttl.is_zero()
        || response.status() != tauri::http::StatusCode::OK
        || response.body().len() > MAX_STATIC_BODY_BYTES
    {
        return;
    }
    if !cache.contains_key(key) && cache.len() >= MAX_STATIC_ENTRIES {
        cache.clear();
    }
    cache.insert(key.to_string(), (Instant::now() + ttl, clone_response(response)));
}

fn clone_response(response: &tauri::http::Response<Vec<u8>>) -> tauri::http::Response<Vec<u8>> {
    let mut clone = tauri::http::Response::builder()
        .status(response.status())
        .body(response.body().clone())
        .expect("cloned response keeps valid status");
    *clone.headers_mut() = response.headers().clone();
    clone
}

/// 文档型导航（Accept 含 text/html）给可点的 HTML 错误页——研究台是主界面后，
/// 错误页必须留一条回原生设置的路；API/fetch 请求保持纯文本（错误信息会被
/// JS 原样展示，不能塞标签）。
fn wants_html(headers: &tauri::http::HeaderMap) -> bool {
    headers
        .get("accept")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| v.contains("text/html"))
}

fn error_response(
    status: tauri::http::StatusCode,
    headers: &tauri::http::HeaderMap,
    message: &str,
) -> tauri::http::Response<Vec<u8>> {
    if !wants_html(headers) {
        return text_response(status, "text/plain; charset=utf-8", message);
    }
    let page = format!(
        "<!doctype html><meta charset=\"utf-8\"><title>Charlie</title>\
         <p>{message}</p><p><a href=\"http://tauri.localhost/#pairing\">返回应用设置</a></p>"
    );
    tauri::http::Response::builder()
        .status(status)
        .header("content-type", "text/html; charset=utf-8")
        .header("cache-control", "no-store")
        .body(page.into_bytes())
        .expect("static error page")
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

    fn cache_control_headers(value: &str) -> tauri::http::HeaderMap {
        tauri::http::Response::builder()
            .header("cache-control", value)
            .body(Vec::<u8>::new())
            .unwrap()
            .headers()
            .clone()
    }

    #[test]
    fn error_pages_negotiate_html_for_document_navigation() {
        let mut headers = tauri::http::HeaderMap::new();
        assert!(!wants_html(&headers));
        headers.insert("accept", "text/html,application/xhtml+xml,application/xml;q=0.9".parse().unwrap());
        assert!(wants_html(&headers));

        // 文档导航拿可点的错误页，带回原生设置的出口。
        let page = error_response(tauri::http::StatusCode::BAD_GATEWAY, &headers, "网关不可达。");
        assert_eq!(page.status(), 502);
        assert_eq!(page.headers().get("content-type").unwrap(), "text/html; charset=utf-8");
        let body = String::from_utf8(page.body().clone()).unwrap();
        assert!(body.contains("网关不可达。"));
        assert!(body.contains("http://tauri.localhost/#pairing"));
        assert!(body.contains("返回应用设置"));

        // fetch/API 请求拿纯文本，JS 会原样展示错误信息。
        let plain = error_response(tauri::http::StatusCode::BAD_GATEWAY, &tauri::http::HeaderMap::new(), "网关不可达。");
        assert_eq!(plain.headers().get("content-type").unwrap(), "text/plain; charset=utf-8");
        assert_eq!(plain.body(), "网关不可达。".as_bytes());
    }

    #[test]
    fn static_ttl_honors_cache_control() {
        assert_eq!(static_ttl(&tauri::http::HeaderMap::new()), DEFAULT_STATIC_TTL);
        assert_eq!(static_ttl(&cache_control_headers("public, max-age=3600")), Duration::from_secs(3600));
        assert_eq!(static_ttl(&cache_control_headers("MAX-AGE=999999")), MAX_STATIC_TTL);
        assert_eq!(static_ttl(&cache_control_headers("no-store")), Duration::ZERO);
        assert_eq!(static_ttl(&cache_control_headers("private, max-age=60")), Duration::ZERO);
        assert_eq!(static_ttl(&cache_control_headers("no-cache")), Duration::ZERO);
    }

    #[test]
    fn cacheable_path_requires_static_prefix() {
        assert!(is_cacheable_path("/app/static/app.css"));
        assert!(is_cacheable_path("/app/static/app.css?v=2"));
        assert!(is_cacheable_path("/app/static/brand/icon-64.png"));
        assert!(!is_cacheable_path("/app"));
        assert!(!is_cacheable_path("/app/staticx/app.css"));
        assert!(!is_cacheable_path("/agent/tasks/abc/events"));
    }

    #[test]
    fn static_cache_roundtrips_and_expires() {
        let mut cache = StaticCache::new();
        let response = tauri::http::Response::builder()
            .status(200)
            .header("content-type", "text/css")
            .body(b"body{color:red}".to_vec())
            .unwrap();

        assert!(cache_lookup(&mut cache, "app.css").is_none());
        cache_store(&mut cache, "app.css", &response, DEFAULT_STATIC_TTL);
        let cached = cache_lookup(&mut cache, "app.css").expect("fresh entry survives");
        assert_eq!(cached.status(), 200);
        assert_eq!(cached.headers().get("content-type").unwrap(), "text/css");
        assert_eq!(cached.body(), b"body{color:red}");
        // 命中不消费条目：二次命中仍然可用。
        assert!(cache_lookup(&mut cache, "app.css").is_some());

        // TTL 为 0（no-store 等）不落缓存；过期条目在查找时清除。
        cache_store(&mut cache, "volatile", &response, Duration::ZERO);
        assert!(cache_lookup(&mut cache, "volatile").is_none());
        cache.insert("stale".to_string(), (Instant::now() - Duration::from_secs(1), response));
        assert!(cache_lookup(&mut cache, "stale").is_none());
        assert!(!cache.contains_key("stale"));
    }

    fn pairing(token: &str) -> Pairing {
        Pairing {
            gateway_url: GW.to_string(),
            backend_id: ID.to_string(),
            device_token: token.to_string(),
        }
    }

    #[test]
    fn pairing_cache_fills_and_invalidates() {
        let mut cache = PairingCache::default();
        let generation = cache.generation;
        cache.fill(generation, pairing("zgd_a"));
        assert_eq!(cache.pairing.as_ref().unwrap().device_token, "zgd_a");

        cache.invalidate();
        assert!(cache.pairing.is_none());
        cache.fill(cache.generation, pairing("zgd_b"));
        assert_eq!(cache.pairing.as_ref().unwrap().device_token, "zgd_b");
    }

    #[test]
    fn pairing_cache_drops_reads_that_raced_an_invalidation() {
        let mut cache = PairingCache::default();
        // 读取开始时记下代次；读取期间 JS 侧改写了配对并清缓存。
        let started_at = cache.generation;
        cache.invalidate();
        cache.fill(started_at, pairing("zgd_stale"));
        assert!(cache.pairing.is_none(), "stale keystore read must not repopulate the cache");
    }

    #[test]
    fn static_cache_skips_error_responses() {
        let mut cache = StaticCache::new();
        for status in [429u16, 404, 500, 502, 304] {
            let response = tauri::http::Response::builder()
                .status(status)
                .header("content-type", "application/json")
                .body(b"{}".to_vec())
                .unwrap();
            cache_store(&mut cache, "asset", &response, DEFAULT_STATIC_TTL);
            assert!(cache.is_empty(), "status {status} must not be cached");
        }
    }

    #[test]
    fn static_ttl_caps_immutable_versioned_assets() {
        // 后端给带版本的静态 URL 发一年 max-age；进程内缓存按上限 1 天收。
        let versioned = cache_control_headers("public, max-age=31536000, immutable");
        assert_eq!(static_ttl(&versioned), MAX_STATIC_TTL);
        assert!(is_cacheable_path("/app/static/v/0123456789ab/app.js"));
    }

    #[test]
    fn static_cache_is_bounded() {
        let mut cache = StaticCache::new();
        let response: tauri::http::Response<Vec<u8>> =
            tauri::http::Response::builder().body(Vec::new()).unwrap();
        for i in 0..(MAX_STATIC_ENTRIES * 2) {
            cache_store(&mut cache, &format!("asset-{i}"), &response, DEFAULT_STATIC_TTL);
        }
        assert!(cache.len() <= MAX_STATIC_ENTRIES);
        // 超大响应不进缓存。
        let huge = tauri::http::Response::builder().body(vec![0u8; MAX_STATIC_BODY_BYTES + 1]).unwrap();
        cache_store(&mut cache, "huge", &huge, DEFAULT_STATIC_TTL);
        assert!(!cache.contains_key("huge"));
    }
}
