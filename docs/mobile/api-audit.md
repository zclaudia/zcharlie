# 移动端 API 审计

审计对象是 `src/investment_agent/web/static/*.js` 实际发起的 HTTP 请求（含模板中的 `data-endpoint`）。以下 JSON API 路由均在路由器级使用 `verify_api_key_or_session`：可由网关注入 `X-API-Key` 认证，也可由 Web 端用会话 Cookie 认证。手机只走前者，服务端密钥不下发到手机。`/app/*` 是服务端渲染页面导航，不属于移动端 API；`/healthz` 是公开健康检查，本前端没有调用。

| 页面/模块 | 实际 API 端点与方法 | API key / Cookie | 网关会丢弃的请求头 |
| --- | --- | --- | --- |
| 首页与研究 | `GET /dashboard/today`; `GET /research/runs`, `/research/runs/{run_id}`, `/research/runs/{run_id}/changes`, `/research/stocks/{ticker}/history`, `GET /research/runs/{run_id}/artifacts/{artifact_name}`（详情响应中的下载 URL）; `POST /research/single-stock` | 均接受注入的 `X-API-Key`；浏览器默认 Cookie | 通用 `api.js` 带 `X-Requested-With: fetch`；仅用于 Cookie 写请求的 CSRF 证明。网关注入 key 后不依赖它。研究附件下载没有额外认证头要求。 |
| 数据健康与日历 | `GET /health/data`, `GET /calendar/status`; `POST /calendar/apply`, `/calendar/check` | 同上 | `X-Requested-With` 对移动端不需要。 |
| 自选与证券查找 | `GET /watchlist`, `/watchlist/template-suggestion?ticker=…`, `/stocks/search?q=…&limit=…`, `/stocks/{ticker}/industry`, `/stocks/{ticker}/report-year`, `/market/{ticker}/bars?days=…`; `POST /watchlist`, `/watchlist/refresh`, `/watchlist/{listing_id}/rule/{draft,edit,approve,reject}`; `PATCH`、`DELETE /watchlist/{listing_id}` | 同上 | `api.js` 包装调用带 `X-Requested-With`；`watchlist.js` 的直连 PATCH/DELETE 也显式带该头。`X-API-Key` 路径不要求该头。 |
| 候选与规则 | `GET /candidates`（含筛选查询）、`/candidates/{candidate_id}`, `/rules/{rule_id}`, `/market/{ticker}/bars`, `/verification/requests?limit=…`, `/verification/requests?listing_id=…&limit=…`; `POST /candidates`, `/candidates/{candidate_id}/{redraft,promote,dismiss}`, `/rules/{rule_id}/reject`; `PATCH /rules/{rule_id}` | 同上 | 只有通用 `api.js` 的 `X-Requested-With`，不构成 key 认证前提。 |
| 持仓、组合 | `GET /portfolio/accounts`, `/portfolio/listings?ids=…`, `/portfolio/listing?…`, `/portfolio/snapshot?account_id=…`, `/portfolio/events?account_id=…`, `/portfolio/valuations/current?account_id=…`, `/portfolio/purposes/current?account_id=…`, `/portfolio/plans/current?account_id=…`, `/portfolio/plans/{plan_id}?account_id=…`, `/portfolio/calculations/{calculation_id}?account_id=…`, `/portfolio/artifacts/{kind}/{artifact_id}?account_id=…`, `/portfolio/rule?listing_id=…`, `/rule-templates`, `/rule-templates/{template_id}`; `POST /portfolio/accounts`, `/portfolio/events`, `/portfolio/corrections`, `/portfolio/purposes`, `/portfolio/rule/draft`, `/portfolio/rule/edit`, `/portfolio/rule/approve`, `/portfolio/rule/reject` | 同上 | 通用包装调用带 `X-Requested-With`；移动端可省略。 |
| 投资者档案与 agent 面板 | `GET /investor/readiness`, `/investor/mandate`, `/investor/cases?limit=…&after=…`, `/investor/cases/{case_id}`, `/investor/cases/{case_id}/reviews?limit=…`, `/investor/cases/{case_id}/recommendations?limit=…`, `/investor/cases/{case_id}/research?limit=…`, `/investor/cases/{case_id}/publication-events?limit=…`, `/investor/decisions?case_id=…&limit=…`, `/investor/memories?limit=…&after=…`, `/investor/memories/{memory_id}?revision=…`, `/investor/proposals?status=pending&limit=…&after=…`, `/investor/recommendations/{id}?revision=…`, `/investor/recommendations/{id}/body?revision=…`, `/investor/evidence/{id}?revision=…`, `/investor/calculations/{id}?revision=…`, `/investor/reviews/{id}?revision=…`, `/investor/monitors/{id}?revision=…`, `/investor/theses/{id}?revision=…`, `/investor/disclosures/originals/{id}/fragments?…`, `/investor/disclosures/originals/{id}/download?…`, `/investor/discovery/batches?limit=…&after=…`, `/investor/discovery/batches/{batch_id}`, `/investor/notifications?limit=…&after=…`; `POST /investor/actions`, `/investor/decisions`, `/investor/mandate`, `/investor/memories`, `/investor/memories/{memory_id}/revoke`, `/investor/proposals/{proposal_id}/resolve`, `/investor/disclosures/search`, `/investor/cases/{case_id}/reviews`; `PATCH /investor/cases/{case_id}/pinned`, `/investor/notifications/{sequence}/read` | 同上 | `X-Requested-With` 仅由通用客户端携带；API key 路径不依赖。附件下载链接由站内 API 返回，移动端应请求该路径经代理下载。 |
| 方法工作台 | `GET /investor/methods?offset=…`, `/investor/methods/proposals/{proposal_id}`, `/investor/methods/proposals/{proposal_id}/history?offset=…`; `GET /investor/methods/artifacts/{kind}/{artifact_id}`（工作台生成的链接）；`POST /investor/methods/proposals`, `/investor/methods/evaluations`, `/investor/methods/decisions`, `/investor/methods/rollback`, `/investor/methods/offline-shadow`, `/investor/methods/prospective-shadow`, `/investor/methods/reviews/{task_id}` | 同上 | `methods.js` 的自定义 `fetch` 不添加 `X-Requested-With`；Web 写请求仍有同源 Origin/Referer，移动端传注入的 key 即可。 |
| Agent 对话和任务 | `GET /agent/listings?q=…`, `/agent/listings/{listing_id}`, `/agent/candidate-context/{candidate_id}`, `/agent/conversations?limit=…&after=…`, `/agent/conversations/{conversation_id}/messages?after=…&limit=100`, `/agent/tasks/{task_id}`, `/agent/tasks/{task_id}/research`, `/agent/tasks/{task_id}/snapshot`; `POST /agent/conversations`, `/agent/conversations/{conversation_id}/messages`, `/agent/tasks/{task_id}/cancel`; 事件流见下表 | 全部由 `/agent` 路由器 `verify_api_key_or_session` 保护，可接受注入的 `X-API-Key`。 | Web SSE 同时发 `X-Requested-With: fetch` 与 `Last-Event-ID`；两者都会被网关丢弃。移动端无需前者，游标通过查询参数发送。 |
| 设置与网关状态 | `GET /settings`, `/settings/history?limit=30`, `/gateway/status`; `PATCH /settings`; `POST /settings/test/llm`, `/settings/test/ifind` | 两个路由器都接受注入的 `X-API-Key`。设置页面可经 JSON API 读取、修改；Cookie 登录页和 `/app/*` HTML 不适用于网关代理。 | `X-Requested-With` 不需要。 |

## Agent 事件流核验

| 请求 | 行为 | 移动端用法 |
| --- | --- | --- |
| `GET /agent/tasks/{task_id}/events?after_event_id={cursor}` | 默认 `transport=sse`。服务端每轮最多读取 200 条事件，空闲时发送 SSE 注释心跳 `: heartbeat`，随后等待 1 秒；响应类型为 `text/event-stream`，设置 `Cache-Control: no-store` 与 `X-Accel-Buffering: no`。 | SSE 连接只发 `after_event_id`，成功解析带 `id:` 的事件后更新游标并重连。心跳间隔远低于网关的 60 秒相邻数据块上限。 |
| `GET /agent/tasks/{task_id}/events?transport=poll&after_event_id={cursor}` | 返回 `events`、`next_cursor`、`task` 和 `snapshot_url` JSON；与 SSE 共享同一事件存储和游标。 | 流式能力不可用或连续失败时轮询；使用响应中的 `next_cursor`。 |
| `GET /agent/tasks/{task_id}/snapshot` | 返回任务快照及事件游标；过期游标会以 HTTP 410 指示重新加载快照。 | 收到 410 时先读快照，再从新游标续传。 |

浏览器会同时设置 `Last-Event-ID` 和同值 `after_event_id`；后端在 `Last-Event-ID` 存在时优先取该值。网关丢弃该头后，查询参数仍被后端正常读取，因此游标续传等价可用，无需服务端改动。SSE 与 poll 都经过相同 `/agent` API key 认证。任务快照、取消、消息提交与对话列表均为普通 JSON 请求，未发现 cookie 专属接口。

## 网关注意事项

- 移动端所有 API 请求都走 `https://<gateway>/api/proxy/<backendId>/<path>`，携带设备凭证 `Authorization: Bearer zgd_…`。网关验证设备凭证后向后端注入 `X-API-Key`；客户端不持有或发送后端 key，也不依赖 Cookie、`Set-Cookie`、`X-Requested-With`。
- Android 端必须使用 Tauri `plugin-http` 的 Rust 侧请求，不能用 WebView `fetch`；网关 CORS 不允许本方案所需之外的浏览器请求头。
- `/healthz` 是未认证健康检查，但经代理请求可用作连通性检查。用 `/gateway/status` 可额外确认后端 gateway 状态，它需要 API key。
- 认证错误应显示凭证无效/过期；网关上游不可用对应离线/超时提示。JSON API 超时策略可独立于事件流：首响应不超过 30 秒，SSE 活跃连接依靠服务端 1 秒心跳满足 60 秒相邻数据限制。

## 覆盖范围说明

静态 JS 中未发现需要 Cookie 才能访问的 JSON API。`api.js`、`watchlist.js` 直连、`agent.js` SSE 以及 `methods.js` 自定义请求的差异仅涉及 `X-Requested-With`；服务端 API key 分支认证成功后立即返回，不执行 Cookie 写请求的 CSRF 校验。个别页面导航链接 `/app/*` 仍由 Web session 保护，应由手机原生页面取代，而不是代理这些 HTML。
