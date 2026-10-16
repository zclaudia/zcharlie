# Android 客户端本地验收

2026-10-12 在 `hermes_api34`（Android 14 / API 34，arm64）模拟器上完成端到端联调。联调只使用 `/Users/zhvala/SourceCode/zclaudia-gateway/dist` 启动的本地临时网关、临时 API 数据库和临时签发的设备凭证；没有连接生产网关或使用真实凭证。模拟器通过 `10.0.2.2` 访问宿主机网关。

## 场景记录

| 验收场景 | 结果 | 截图 |
| --- | --- | --- |
| 未配对时显示网关地址、服务 ID、设备凭证输入表单 | 通过 | [01-pairing.png](screenshots/01-pairing.png) |
| 配对成功，空会话页显示已连接 | 通过 | [02-agent-empty.png](screenshots/02-agent-empty.png) |
| 对话成功保存，能在会话列表重新打开 | 通过 | [03-sessions.png](screenshots/03-sessions.png) |
| 发送消息并收到实时任务状态 | 通过 | [04-message-task.png](screenshots/04-message-task.png) |
| 取消任务后，通过事件流显示取消结果 | 通过 | [05-task-cancelled.png](screenshots/05-task-cancelled.png) |
| 设置页读取本地网关配置和运行参数 | 通过 | [06-settings-top.png](screenshots/06-settings-top.png) |
| 把 Agent 最大并发从 4 改为 3；设置页记录变更，另经本机 API `GET /settings` 确认服务端值为 3 | 通过 | [07-settings-saved.png](screenshots/07-settings-saved.png) |
| 停止临时网关后出现离线提示 | 通过 | [08-gateway-offline.png](screenshots/08-gateway-offline.png) |
| 重启同一临时网关后自动恢复连接 | 通过 | [09-gateway-recovered.png](screenshots/09-gateway-recovered.png) |

配对凭证只用于本次本机测试，没有保存到仓库或截图。调试完成后已从模拟器应用存储清除该凭证；本机临时 API 数据目录、数据库和网关数据目录已删除。

## 构建与自动化检查

按干净环境流程构建 Android Debug APK：

```bash
cd mobile && pnpm install --frozen-lockfile && pnpm tauri:android:debug
```

本次执行结果：`pnpm typecheck` 通过；`pnpm test` 的 6 个传输层测试通过；`pnpm tauri:android:debug` 构建成功。APK 输出在 `mobile/src-tauri/gen/android/app/build/outputs/apk/universal/debug/app-universal-debug.apk`。安装要求、环境变量与 Debug 明文 HTTP 限制见 [mobile/README.md](../../mobile/README.md)。

Python 全量套件本次为 `2390 passed, 6 deselected, 1 failed`；唯一失败是已知不稳定的 `tests/unit/test_engine_lifecycle.py::test_api_lifespan_disposes_the_engine_on_shutdown`，单独重跑通过。改动文件的 Ruff 和 mypy 检查均通过。

## 2026-10-12 晚间复验（第二轮）

同日稍晚从干净构建目录重打 Debug APK（`pnpm typecheck`、`pnpm test` 6 例、`pnpm tauri:android:debug` 全绿；注意 `scripts/env.sh` 在本机会回退到 `~/Library/Android/sdk`，需显式 `ANDROID_HOME=/opt/homebrew/share/android-commandlinetools` 及对应 NDK_HOME）并重跑端到端验收。本轮改用独立临时网关（临时数据目录 + 临时签发的 `zgb_`/`zgd_` 凭证）与无 `--reload` 的隔离后端实例：共享 dev API 带 `--reload`，并行开发保存文件会触发整进程重启、网关链路反复重建，不能作为联调基线。

全部场景再次通过：配对表单、配对成功空会话已连接、发送消息收到实时任务状态（LLM 真实响应，问候语按设计以 `evidence_blocked` 部分完成收尾）、运行中取消任务经事件流显示已取消、会话列表重开、设置页读取网关与运行参数、修改 Agent 最大并发 4→3 后经服务端 `GET /settings` 确认为 3、停止网关出现离线横幅、重启网关自动恢复、设置页"重新配对"换绑新服务 ID。调试后临时凭证、临时数据目录与模拟器应用配对均已清除；dev 库运行设置已还原，仅"注册凭证"无法复原（历史只存掩码），需从签发记录重新粘贴。

联调中发现的两条环境事实：dev API 进程对局域网 LLM 的访问可能被 macOS 本地网络隐私静默拒绝（重启 dev API 即恢复）；agent 对话需运行时设置 `investor_agent_enabled=true` 及 `agent_base_url` / `agent_model` / `agent_api_key` / `agent_max_tokens` 齐备，缺前者每条消息 409 `execution_denied`，缺后者任务以配置错误失败。

## 2026-10-15 第三轮（全自动化）

计划 [e2e-plan-2026-10-14.md](e2e-plan-2026-10-14.md)、执行报告
[e2e-report-2026-10-15.md](e2e-report-2026-10-15.md)。31 例：29 通过 / 0 失败 / 1 环境跳过。
首次由 CDP 自动化 harness（[tools/mobile-e2e.sh](../../tools/mobile-e2e.sh)）完成全程，
新增生命周期、设置写拒绝分支覆盖；发现任务卡片在 user/assistant 消息上重复渲染（P3）。

## 2026-10-16 第四轮（内嵌模式 + 最新 APK 回归）

计划 [e2e-plan-2026-10-16.md](e2e-plan-2026-10-16.md)、执行报告
[e2e-report-2026-10-16.md](e2e-report-2026-10-16.md)。18 例：17 通过 / 0 失败 / 1 环境失效未计。
验证内嵌模式（4edda31）经真实代理链路（WebView → 网关 → WS 通道 → 后端桥注入 X-API-Key）
的 API 级行为（G1–G5）+ 原生链路 11 例回归。关键结论：**页面级内嵌体验不可达**——App 侧无
web UI 入口、代理前缀下 HTML 绝对资源路径 404，需下一工作包（App 自定义协议/导航入口 +
资源路径策略）。另记 P3×2（任务卡重复渲染仍开放；＋预建会话不回填标题）。

## Web UI 内嵌模式（2026-10-16）

`WebSessionMiddleware` 在会话 cookie 之外接受 `X-API-Key`（手机网关剥离 cookie 后注入的凭证）：

- 有效 key：请求标记 `state["web_auth"]`，模板经 `is_embedded(request)` 渲染内嵌形态——`<body data-embedded="android">`；隐藏「退出登录」（App 走设备凭证，无会话可退）；「更多」弹层提供「设备与配对」，链接指向 `tauri.localhost/#pairing` 由 App 壳接管。带有效 key 访问 `/app/login` 直接 302 回 `/app`。
- 错误 key：401 纯文本（header 客户端用不了登录表单，不给重定向），复用登录失败节流，超限 429。
- 前端配套（`shell.js` / `agent.js`）：内嵌模式 AI 页改用轮询（安卓自定义协议传输会缓冲整响应，SSE 不流）；系统返回键先关覆盖层（`wireEmbeddedBack`，覆盖层打开时压一条历史记录）。

验证：`tests/integration/test_web_app.py` 的 embedded 测试组（渲染 / 302 / 401 / 429 / cookie 会话不受影响）+ 临时真实服务的 curl 验证。返回键与轮询行为属 WebView 内交互，待后续接入手机 E2E harness 覆盖。
