# Android 通知设计提案

**状态：待确认；本期只提交设计，不改服务端发布逻辑，也不在 App 中启用通知。**

## 建议范围

先支持两种通道：App 前台通过 Gateway Topic 收到提示并显示应用内提示；需要后台送达时由网关的 ntfy 集成转发到用户配置的 ntfy 服务。Android 后台保活 WebSocket 不作为送达保证；若以后要求通知必须直接出现在 zcharlie 中，再单独评估 FCM 和持久化通知收件箱。

### 前台：Gateway Topic 与应用内提示

后端在少数有明确结果的时点（例如研究任务完成、失败或需要用户处理）发布 `hints` Topic。前台 App 使用 `@zclaudia/gateway-client` 订阅，并在收到消息时展示应用内提示；用户允许通知权限后，也可调用已安装的 Tauri notification 插件显示本地通知。通知点击后打开对应会话或任务，实际内容仍由 App 使用设备凭证经代理读取。

建议事件格式：

```json
{
  "schema_version": 1,
  "id": "<uuid>",
  "created_at": "<RFC 3339 timestamp>",
  "name": "agent.task.completed",
  "severity": "success",
  "title": "研究任务已完成",
  "body": "打开 zcharlie 查看结果",
  "deep_link": { "screen": "agent", "conversation_id": "<id>", "task_id": "<id>" }
}
```

每条事件都带唯一 ID，App 以 ID 去重；客户端只处理已知版本、允许的事件名和合理时效内的事件。首批只发完成、失败、需要用户处理等低频事件，不把 token 流、每个 Agent 步骤或普通心跳变成通知。

Topic 的 `retain` 语义是“新订阅者收到该 Topic 的最后一个 payload”，并非离线消息队列；Gateway 重启或 backend lease 结束时，retained 状态会清掉。因此建议单条通知事件使用 `retain=False`，避免 App 重连时把旧事件当成新通知。若需要呈现“当前未处理提示”的摘要，可另用一个 retained 快照；不要把它当成通知历史或已读状态的持久存储。可靠历史需要服务端持久化和带游标的读取/确认 API。

Topic 订阅需要 Gateway WebSocket 控制连接。建议复用 `@zclaudia/gateway-client` 的 Topic 能力，并在实现时验证 Tauri Android WebView 的 WebSocket 和 CSP `connect-src` 配置；手机凭证仍只放 Tauri Store 的应用私有存储，不输出日志。断线时退避重连，重连成功后刷新任务/提示的 API 当前状态，不假设 Topic 会补发断线期间的所有事件。

### 后台：网关 ntfy 集成

Gateway 已有独立的 ntfy 推送通道：backend 可发送 `push_notification_request`，事件包含 `name`、`severity`、`title`、`body`、`tags`、`clickUrl`；Gateway 根据 `NTFY_*` 配置转发，并可按事件 allowlist、denylist 和最低 severity 过滤。该功能默认关闭且未配置 Topic。当前 Python SDK 有 `GatewayBackend.send()` 和 `publish_topic()`，但没有专门的 `request_push_notification()` 类型化便捷方法；实现时应决定是补 SDK 方法，还是由服务端用 `send()` 发送协议事件并为 wire shape 加测试。

此路径适合让用户在 ntfy Android 客户端订阅其自管或信任的 ntfy 服务。若通知点击要回到 zcharlie，需先确定 HTTPS/App Link 路由；首版可以不带跳转链接，或只打开 zcharlie 首页。Gateway 的 `GET /api/notifications/config` 返回的 DTO 包含 ntfy 认证字段，不应由手机端读取或展示；这些配置应由网关管理员维护。

Android 会限制后台服务和网络活动；Doze 会推迟后台 CPU/网络活动。官方建议需要持久网络消息时优先使用 FCM。因此，ntfy/系统推送负责后台唤醒，Topic 连接只负责前台实时更新；不要让 Android 常驻 WebSocket 作为可靠后台推送方案。[Android 后台执行限制](https://developer.android.com/about/versions/oreo/background) · [Doze 与 App Standby](https://developer.android.com/training/monitoring-device-state/doze-standby)

## 隐私、权限与去重

- 默认关闭通知；用户在设置页单独启用前台提示或后台推送。Android 13 及以上在用户选择启用通知时再请求通知权限；拒绝权限时仍保留应用内提示。
- 通知正文只用通用摘要，不包含用户原始提问、完整模型输出、账户金额、证券明细、设备凭证或后端 API key。点击后再通过已认证 API 获取详情。
- 使用事件 ID 去重；前台订阅重连或 Gateway 重发时不重复弹出。通知无效、过期或目标会话不存在时忽略并重新拉取 API 状态。
- ntfy URL、Topic 和认证方式由网关管理员配置，默认不启用；设备凭证不兼任 ntfy 订阅口令。

## 待你确认

1. 接受“前台 Topic + 应用内提示；后台先走 Gateway ntfy”的通道划分吗？
2. 是否需要持久化通知收件箱和已读状态？若需要，需要另加后端存储与读取/确认 API。
3. 后台推送点击是否要直接打开指定会话？这需要确定 App Link/HTTPS 路由策略。

本提案提交后暂停通知实现，等你确认后再继续。
