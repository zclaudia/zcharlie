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
