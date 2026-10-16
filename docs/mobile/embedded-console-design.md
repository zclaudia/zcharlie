# WebView 研究台（内嵌 Web UI）设计方案

2026-10-16 实施，第四轮 E2E 两个 P1 缺口的落地：App 侧无 web UI 入口（缺口 1）、代理前缀下
绝对资源路径 404（缺口 2）。方案：**Tauri 自定义协议 handler**——后端与模板零路由改动。
同日晚：研究台升级为 **App 唯一主界面**（原生对话页移除，配对完成即进入；入口从「台」按钮
改为启动跳转，BACK 根部等价退到后台，错误页附「返回应用设置」出口）。

## 架构

```
┌─ Android WebView ────────────────────────────┐
│ 原生 React（tauri.localhost）                │
│   配对完成 ──location.href（启动跳转）──────┐ │
│ ▼ 顶层导航（wry shouldInterceptRequest）     │ │
│ 研究台页面（http://zcharlie.localhost/app） │◀┘
└──────────────┬───────────────────────────────┘
               │ zcharlie:// scheme handler（Rust，
               │   gateway_proxy.rs，每请求现读配对）
               ▼
   网关 /api/proxy/<backendId><原路径>（Bearer zgd_）
               ▼
   后端桥注入 X-API-Key → 内嵌渲染（data-embedded="android"）
```

- **URL 映射只有一条规则**：scheme 源上的任意路径（`/app/*`、`/app/static/*`、裸 API 前缀
  `/agent` `/watchlist` `/settings` 等）→ `网关/api/proxy/<backendId><原路径+query>`。
  模板的绝对资源路径因此在协议源上自然成立（缺口 2 化解，后端模板零改动）。
- **凭证**：handler 经 `StoreExt::app.store("zcharlie-mobile.json")` 与 JS 侧共享同一 store
  实例，键 `gateway-pairing`，每请求现读现用；无桥接命令、无会话、无 cookie。
- **安全**：路径校验复刻 `transport.ts` proxyUrl 规则（单 `/`、禁 `//`、禁 `\`、URL 规范化后
  前缀包含性复查）；请求头白名单转发（注入的 `Authorization` 每跳重建）；响应头剔除
  hop-by-hop 与 `set-cookie`；后端 CSP（`default-src 'self'`）在协议源上同源自洽。

## 平台约束与对策（实测）

| 约束 | 对策 |
| --- | --- |
| Android 拦截请求**不带请求体**（wry `binding.rs` 恒以空 body 重建；`WebResourceRequest` 无 body） | 内嵌模式 `api.js` 把 JSON 载荷编码进 `X-Zcharlie-Body` 头（base64 UTF-8），handler 对非 GET/HEAD 解码还原。仅 `document.body.dataset.embedded==="android"` 时启用，浏览器路径不变 |
| wry Android **丢弃 3xx** | handler 关闭 reqwest 自动重定向，手动追跳（≤5 次），`Location` 相对解析后重新加代理前缀（301/302/303 降级为 GET） |
| 拦截协议文档里的 **pushState 条目在原生后退列表中被折叠**，`goBack()` 直接跳回上一文档 | BACK 在 activity 层接管（`MainActivity.dispatchKeyEvent`）：WebView 停在研究台时执行页面内 JS 分流——覆盖层开着走 `zcOverlay.close()`（其 close 路径自行消费历史条目），非根部走文档级 `history.back()`（研究台内逐级返回），根部 `moveTaskToBack`（原生壳会自动跳回研究台，走历史会打乒乓）；配对/设置页走原分发链（React `onBackButtonPress` 链不变） |
| 拦截协议响应**整包缓冲** | 内嵌 AI 页已按设计用 `transport=poll` 轮询（agent.js），实测 24 次轮询推进任务至终态 |
| Android scheme 请求 **~30s 硬截止** | handler 客户端总超时 20s；网关不可达返回 502 文本 |
| JS 侧导航**只能用 workaround 形式** `http://zcharlie.localhost/...`（`zcharlie://` 不会被拦截） | 启动跳转（App.tsx CONSOLE_URL）硬编码该形式并注释原因 |
| 拦截响应**不走 WebView HTTP 缓存**（shouldInterceptRequest 每次都问 handler） | handler 维护进程内静态资源缓存：仅 `/app/static/` 下未跟随重定向的 GET 200，TTL 取后端 `Cache-Control` 的 `max-age`（缺省 600s，上限 1 天，`no-store/no-cache/private` 不缓存），上限 64 条 / 单条 4MB。否则每次导航 ~10 笔静态请求全部计入网关按 IP 的限流桶（2026-10-16 实测触顶 200/min） |

## 已知限制（v1）

- **请求体经头编码有实际大小上限**（WebView/网关的头大小限制，未做压测）；超长写入
  （如 32000 字问题全文接近上限）建议回原生对话页发送。
- `target="_blank"` 链接在 WebView 内可能无效（如方法页的产物链接）。
- FormData 上传类表单若未来出现，不适用头编码桥（当前全部写入均为 JSON）。
- tauri 的 Kotlin 侧 back 监听器表在 WebView 离开 App 页面后残留（`hasListener` 恒真），
  这是研究台 BACK 必须在 activity 层接管的根因；tauri 上游问题，未修。

## 验证

- Rust 单测 10 个（路径映射/逃逸拒绝/Location 改写/凭证清理/base64 往返 + 静态缓存
  TTL/路径判定/命中过期/容量上限），`cargo test` 绿。
- 第五轮真机 E2E（2026-10-16，`docs/mobile/e2e-report-2026-10-16.md` §第五轮）：G6 内嵌页渲染、
  G7 概览真实数据、G8 配对行回程 + `#pairing` 落设置页、G9 返回键关覆盖层/回 App、
  G10 内嵌 AI 页发消息 → 轮询 → 已完成，全部通过；F1 凭证零泄漏、F2 出厂态收尾。
