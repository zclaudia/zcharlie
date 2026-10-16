# 移动端技术路线（Android，供 review）

2026-10-16。本文是移动端实现的整体技术路线综述：架构、关键决策及依据、两条数据面、安全模型、
平台约束、构建发布与测试体系。逐项细节不在此重复：早期选型论证见 [decision.md](decision.md)，
API 全量清单见 [api-audit.md](api-audit.md)，协议 handler 设计见
[embedded-console-design.md](embedded-console-design.md)，验收历史见 [README.md](README.md)。

代码分布：App（Tauri/React）`mobile/`；后端内嵌模式 `src/investment_agent/web/routes.py` +
`static/api.js`；网关桥 `src/investment_agent/gateway/manager.py`。网关本体是独立仓库
`zclaudia-gateway`（Protocol v4），不在本仓库。

---

## 1. 总体架构

一个 Tauri 2 App，**单 WebView、两个源**：

```
┌─ Android WebView ──────────────────────────────────────────────┐
│                                                                │
│  源 A：tauri.localhost（打进 APK 的 React 原生 UI，窄面）        │
│    配对页 · AI 对话（会话/消息/任务卡） · 设置                   │
│    网络层：tauri-plugin-http（Rust reqwest），走 WebView 之外    │
│                                                                │
│  源 B：http://zcharlie.localhost（后端 v2 Web UI 完整研究台）    │
│    工具栏「台」按钮顶层导航进入；「设备与配对」行返回源 A          │
│    网络层：zcharlie:// 协议 handler（Rust，gateway_proxy.rs）    │
└───────────────┬────────────────────────────────────────────────┘
                │ 全部流量经网关（两个面同构）
                ▼
   网关 zclaudia-gateway  POST|GET /api/proxy/<backendId>/<path>
                │  鉴权：Authorization: Bearer zgd_…（设备凭证）
                ▼
   后端网关桥（gateway/manager.py）注入 X-API-Key
                ▼
   zcharlie 后端（JSON API / SSE+poll / 服务端渲染页面）
```

- **源 A（原生）**只做三件事：配对、AI 对话、设置——键盘优先的窄交互面，React + Vite，
  网络经 `plugin-http`（`mobile/src/transport.ts` 是唯一传输模块）。
- **源 B（研究台）**是后端渲染的完整 `/app` Web UI，内嵌模式渲染（无退出登录、配对行、
  轮询）。浏览器/桌面访问同一套页面，行为由 `data-embedded="android"` 开关分叉。
- 两条面走**同一网关代理路径、同一设备凭证、同一后端 API**，差别只在承载层
  （plugin-http vs 协议 handler）。

## 2. 关键决策及依据

| # | 决策 | 依据 |
| --- | --- | --- |
| D1 | 壳用 Tauri 2 而非原生 Android 或纯 WebView 套壳 | Rust 侧插件化（store/http/notification）+ 单 WebView 双源能力 + 现有 React 技术栈复用；论证见 decision.md |
| D2 | **永不直连后端**：手机只认识网关地址 + 设备凭证，后端 API key 永不下发 | 后端可不出公网；网关侧凭证可撤销/限定 scope/限期；手机失窃只泄 `zgd_`（设备范围、可吊销） |
| D3 | 双源而非全原生或纯 Web | 对话/配对/设置是高频窄交互，原生面可控且快；研究台 12+ 页面是宽信息面，内嵌 Web UI 零重复实现（UI v2 已是移动优先设计） |
| D4 | 任务事件双传输：SSE 优先、失败 3 次降级 2s 轮询，游标（`after_event_id`）贯穿 | 网关通道支持流式但手机网络不稳；轮询与 SSE 共享同一事件存储，降级零服务端改动；游标过期(410)读快照重建 |
| D5 | 凭证存 Tauri Store（App 私有目录 JSON），Rust/JS 共享同一实例 | 不进 localStorage、不进日志；`pm clear` 即彻底清除；Rust 协议 handler 每请求现读现用，免凭证桥接。**2026-10-16 起已加密落盘**（`tauri-plugin-secure-keystore`：Android Keystore AES-256-GCM / iOS Keychain），明文 Store 仅保留一次性迁移读取，迁后即删 |
| D6 | Debug 构建允许明文 HTTP（仅 debug manifest 注入 `usesCleartextTraffic`），Release 强制 HTTPS 网关 | 本机/模拟器联调零证书成本；`validateGatewayUrl` 在非 debug 构建拒绝 `http:` 网关 |

## 3. 数据面一：原生 UI（tauri.localhost）

### 3.1 配对（PairForm → `pairDevice`）

输入三元组：网关地址（仅裸 origin，http/https、无 userinfo、无路径）、服务 ID（**可选**）、
设备凭证（`zgd_` 前缀校验）。服务 ID 留空时调网关 `GET /api/discovery/backends`（设备凭证
Bearer，返回可达后端精简列表）自动发现：恰一个在线后端自动选定；零个报「范围内没有在线后端」；
多个报错并列出让用户填。验证 = 经代理链打一次 `GET /healthz`（401/403/502 分类报错），通过后
`savePairing` 落 Store（落盘的必是已解析的具体 backendId，Rust 侧协议 handler 因此不变）。
**Release 构建在此拒绝 http 网关**。依赖网关 ≥ db24f82（discovery 端点）；生产网关升级部署后
手机侧即可用空服务 ID 配对。

### 3.2 传输（transport.ts，全部请求的唯一出口）

- `proxyUrl()`：路径安全规则——单 `/` 开头、禁 `//`、禁 `\`、拼后 URL 规范化复查前缀包含
  （防 `..` 逃逸）；Rust 侧协议 handler 复刻同一套规则（双侧一致是安全前提）。
- 错误分类固定四类：`invalid_device_token`(401/403 → 引导重新配对)、`backend_offline`
  (502/504)、`gateway_unreachable`(网络/超时)、`cancelled`；30s 请求超时。
- 请求头只带 `Authorization: Bearer` + Accept/Content-Type；不依赖 cookie、
  `X-Requested-With`、`Last-Event-ID`（网关请求头白名单会剥掉，游标走查询参数——api-audit.md）。

### 3.3 任务事件（watchTask）

```
snapshot（GET /agent/tasks/<id>/snapshot）
  → SSE：GET /agent/tasks/<id>/events?after_event_id=<cursor>（ReadableStream 逐帧解析，
         60s idle 超时；task_state 事件触发 GET task 判终态）
  → 连续失败 <3 次：指数退避重连（0.5s→8s 封顶）
  → ≥3 次：降级轮询 GET …?transport=poll（2s 周期），UI 徽标「实时」→「轮询」
  → 游标过期（410 / cursor_expired 事件）：重读快照重建游标
  → 终态（succeeded/partial/failed/cancelled）结束
```

### 3.4 对话与设置

会话/消息走 `/agent/conversations*` CRUD；任务卡挂在触发消息之后（`task_ref` 关联）。
设置页 = 连接卡片（网关/服务 ID/凭证掩码 + 重新配对/解除配对）+ 运行参数表单
（`GET/PATCH /settings`，dirty 计数、密钥留空保持不变、422 inline 报错）+ 只读启动配置面板。

## 4. 数据面二：内嵌研究台（zcharlie.localhost）

进入：工具栏「台」按钮 → `location.href = "http://zcharlie.localhost/app"`（**必须用 workaround
形式**，`zcharlie://` 的 JS 导航不被 wry 拦截）。返回：内嵌「更多 → 设备与配对」
（`tauri.localhost/#pairing`，App 侧消费 hash 落设置页）或系统返回键。

### 4.1 协议 handler（gateway_proxy.rs，注册于 lib.rs）

每请求：读 Store 配对 → 路径校验（同 3.2 规则）→ 构造 `网关/api/proxy/<id><路径+query>` →
头白名单转发 + `Authorization: Bearer` 注入 → **手动追重定向**（wry 丢弃 3xx；≤5 跳，
301/302/303 降级 GET，`Location` 相对解析后重新加代理前缀）→ 响应头剔除 hop-by-hop 与
`set-cookie`、整包缓冲返回。错误映射：未配对 503、路径非法 400、网关不可达 502；总超时 20s
（Android scheme 请求 ~30s 硬截止）。模板的绝对资源路径在协议源上自然成立——**后端模板与
路由零改动**。

### 4.2 请求体桥（平台硬限制的解法）

Android 拦截请求**不带 body**（wry 恒以空 body 重建）。内嵌模式下 `api.js` 把 JSON 载荷
base64(UTF-8) 编码进 `X-Zcharlie-Body` 头，handler 对非 GET/HEAD 解码还原；仅
`data-embedded="android"` 时启用，浏览器路径零变化。**限制**：头编码有大小上限（超长内容
回原生对话发送）；FormData 上传类不适用（当前全部写入均为 JSON）。

### 4.3 返回键（MainActivity.dispatchKeyEvent 接管）

两个平台事实：tauri Kotlin 侧 back 监听器表跨页面残留（React 注册后离开页面仍 `hasListener=true`，
BACK 被吞）；拦截协议文档的 pushState 条目被折叠出原生后退列表（`goBack()` 直接跳回上一文档）。
解法：WebView 停在研究台时，activity 层执行页面内 JS 分流——覆盖层开着走 `zcOverlay.close()`
（其 close 路径自行消费历史条目，配合 `shell.js` 的 `wireEmbeddedBack`），否则文档级
`history.back()`（研究台内逐级返回，根处回原生界面）；其余页面走原分发链（React
`onBackButtonPress` 优先级链不变）。

### 4.4 后端内嵌模式（routes.py，4edda31）

`WebSessionMiddleware` 接受 `X-API-Key`：有效 → `state["web_auth"]` → 模板渲染内嵌形态
（`data-embedded`、隐藏退出、配对行）；错误 key → 401 纯文本 + 登录节流（429）；带 key 访问
`/app/login` 302 回 `/app`。前端联动：`agent.js` 内嵌强制 `transport=poll`；CSP（`default-src
'self'`）在协议源上同源自洽，无改动。

### 4.5 网关限流预算（2026-10-16 实测收口）

网关对已认证代理请求按客户端 IP 限流（固定窗口 60s，默认 600，`GATEWAY_PROXY_RATE_LIMIT`
可配；429 带 `Retry-After`，见 zclaudia-gateway 05b6986）。内嵌研究台的常规开销很容易触顶：
一次页面导航 ~15 笔（HTML + ~10 静态资源 + 页面 API），AI 任务轮询 30 笔/分钟，App 探活
常驻。App 侧三项对策：handler 进程内缓存 `/app/static/` GET 200（TTL 取后端
`Cache-Control`，缺省 600s——Android WebView 对拦截响应不走 HTTP 缓存，这是唯一保准生效的
缓存层）；探活 8s → 30s；轮询/探活识别 429（`rate_limited`）退避 10s。组合后活跃使用
峰值 ~30–60 笔/分钟，余量充足。**注意**：将来网关置于 Caddy 之后必须开 `trustProxy`，否则
`req.ip` 是反代地址，所有设备共用一个桶。

## 5. 安全模型汇总

| 层 | 措施 |
| --- | --- |
| 凭证 | 设备凭证（`zgd_`，namespace+backend scope+TTL）存 App 私有 Store；后端 API key 不下发；网关 admin 可吊销/重签。**已加密落盘**（`tauri-plugin-keystore` → `tauri-plugin-secure-keystore`：Android Keystore AES-256-GCM、iOS Keychain；shared_prefs 中仅存密文+IV，Keystore 键不出硬件边界） |
| 传输 | Release 仅 HTTPS 网关；Debug 明文仅本机联调；网关侧请求/响应头白名单（cookie、set-cookie 双向隔离） |
| 路径 | TS 与 Rust 双侧相同的前缀包含校验，URL 规范化后复查（防 `..`/编码逃逸）；跨 namespace 代理按 BACKEND_OFFLINE 应答（不存在性盲） |
| WebView | App bundle CSP 仅 `ipc:`（原生面不能任意出网）；研究台页面 CSP 由后端下发（`default-src 'self'`），协议源同源自洽；`set-cookie` 在 handler 层剥离 |
| CSRF | API key 路径不依赖 Origin/X-Requested-With（网关桥注入 key 后端即信任）；cookie 路径的同源校验仅服务浏览器场景 |

## 6. 构建、发布与运行约束

- 构建：`mobile/scripts/tauri.sh` 是唯一入口（`pnpm tauri:android:debug`）——每次构建恢复
  `android-overlay/`（MainActivity insets + BACK 接管）与图标到 `gen/android/`，并向 debug
  manifest 注入明文许可；`gen/android` 不入库。
- 工具链三件套必须显式 export：`ANDROID_HOME=/opt/homebrew/share/android-commandlinetools`、
  `NDK_HOME=$ANDROID_HOME/ndk/28.0.13004108`、`JAVA_HOME=$(/usr/libexec/java_home -v 17)`。
- 门禁：`pnpm typecheck`（0 错）+ `pnpm test`（vitest 11：传输层单测）+ `cargo test`
  （gateway_proxy 纯函数 10 测）+ 仓库 Python 全量（api.js 改动经浏览器契约测试覆盖）。
- 运行依赖：手机 ↔ 网关 ↔ 后端；后端 research worker 必须与 API 同库成套运行（否则任务停排队）。

## 7. 测试与验收体系

五轮真机 E2E 沉淀（2026-10-12 两轮手动 → 10-15 全自动化 31 例 → 10-16 第四轮 18 例回归 +
第五轮 5 例内嵌验收），harness 落库 `tools/mobile-e2e.sh` + `tools/mobile-e2e/cdp.mjs`
（CDP 读 DOM + 系统级注入动作）。已固化的环境坑表在 e2e-plan §12（20+ 条，含模拟器 NAT
失效冷启动、`/health` vs `/healthz`、M2E_SHOTS 落点、force-stop 等）。服务端断言双写：
`X-API-Key` curl + sqlite 只读。

## 8. 已知限制与技术债

1. **凭证已加密落盘**（2026-10-16）：`storage.ts` 经 `tauri-plugin-secure-keystore` 读写
   `zgd_`（Android Keystore AES-256-GCM / iOS Keychain）；升级路径保留一次性迁移（明文 Store
   读出→加密写入→删明文键，真机已验证），`gateway_proxy.rs` 同步改走 Rust API。剩余边界：
   Keystore 加密（不挂用户认证要求时）防静态提取，不防已 root 设备上以 App 身份运行；如需更强
   可用插件的 `requireAuth: "os"`（每次读取弹生物识别）或 "password" 门，代价是交互成本。
2. **body 头桥大小上限**未压测（WebView/网关头大小限制），超长写入回原生对话。
3. **tauri Kotlin back 监听器残留**是上游问题（`hasListener` 跨页面恒真），我们的 activity 层
   接管绕开了它，但监听器表随进出研究台次数缓慢增长（每条一个 channel 对象，量级无害）。
4. `target="_blank"` 链接在 WebView 内可能无效（如方法页产物链接）。
5. Release 构建与签名已就绪（2026-10-16：keystore 在 `~/.android/`、R8 keep 规则、
   `validateGatewayUrl` 的 HTTPS 强制已实战——Release 拒 HTTP 网关）；剩余 HTTPS 网关侧
   （Caddy + DNS-01）待搭，搭好后真机配对验证证书链。
6. 任务卡重复渲染（P3，10-15 起）；「＋」预建会话不回填标题（P3，10-16 发现）。
7. 通知投递未实现（notifications.md 提案：前台 Gateway Topic + ntfy 后台）。

## 9. 演进方向（按优先级建议）

1. ~~凭证加密升级~~（**已完成**，2026-10-16：secure-keystore 接入 + 迁移真机验证）。
2. **通知投递**：按 notifications.md 提案落地前台 Topic + 后台 ntfy（设计已评审待确认）。
3. **Release 构建 + HTTPS 网关联调**：补签名、验证 D6 的强制逻辑与真机证书链。
4. **iOS 复刻**：两条数据面的 Rust 层（协议 handler、insets/BACK 接管）需按 WKWebView
   约束重做，transport.ts 与 URL 映射设计可复用。
5. **P3 修缮**：任务卡去重、会话标题回填、`M2E_SHOTS` 缺省告警进 harness。
