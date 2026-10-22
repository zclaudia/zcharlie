# Android 随后端发布更新

当前 APK 的研究台仍由后端提供。此次能力的启用只需部署后端；已打开的旧页面需要先导航或重新加载一次，取得包含发布检测的新页面，无须重新安装 APK。APK 中新增的 `bridge_capabilities` 命令用于以后精确声明能力，现有 APK 缺少该命令也可以继续使用。

## 发布与刷新契约

- `GET /app/release` 使用现有浏览器会话或网关注入的 API key 认证，返回 `schema_version: 1`、`release_id`、`supported_bridge_versions: [0, 1]`，响应禁止缓存。
- HTML 带 `meta[name=zcharlie-release]`，这是当前文档的发布基线。版本由 `APP_VERSION`、静态资源和模板指纹共同决定，纯后端部署、页面修改与回滚都能被检测。生产部署应继续使用 `tools/deploy.sh` / `tools/deploy-remote.sh`，确保镜像内 `APP_VERSION` 对应实际提交。
- 前台每 60 秒检查；回前台、网络恢复时立即检查。新版须间隔至少 5 秒被再次确认；刷新前再次查询服务，避免对已离线的服务刷新。
- 页面连续空闲 8 秒、没有正在请求/下载的数据、输入法组合输入、编辑中的表单、待确认消息或当前会话的研究任务，才会自动重新加载。活动的输入框和弹层会延迟自动刷新。安全刷新按钮也不绕过草稿、任务或提交保护。
- 断网、429、5xx、登录失效、旧服务器没有此端点、无效版本响应时保留当前页面。后台不刷新；同一标签页一分钟内最多自动刷新一次，避免发布切换时循环加载。
- AI 页保护恢复的草稿、待确认的原始请求和任务状态；设置页保护未挂载在当前区域中的编辑。其他表单采用保守策略：编辑过后先保存并导航，或还原输入。页面可以在确认保存成功后，从保存的控件或表单派发冒泡的 `release:form-saved` 事件解除对应 DOM 保护；不能因为任意一次 API 成功就清除整页编辑状态。
- 新页面若有额外内存状态，使用 `registerRefreshGuard(key, () => reasonOrEmptyString)`；短时异步操作用 `holdRefresh(reason)`，结束时调用其返回的释放函数。也可在对应元素设置 `data-refresh-blocked="true"`。未确定状态应阻止刷新。

## APK 兼容契约

`native_bridge.js` 是网页访问原生能力的协商入口。能力声明按页面缓存，查询最多等 1.5 秒：

| APK | 处理方式 |
| --- | --- |
| 有 `bridge_capabilities`，`bridge_version=1` | 仅使用声明的能力 |
| 没有声明命令（版本 0） | 对已知旧命令按需尝试一次；缺少命令后，该页面禁用该命令 |
| 没有 IPC 或声明无效/查询失败 | 使用已有 scheme/请求头传输；事件流降为轮询 |
| 后端声明不支持该桥版本 | 保留当前文档并提示升级应用，禁止自动刷新 |

v1 的声明结构为 `{bridge_version: 1, app_version, capabilities: [...]}`，现有能力是 `proxy_request`、`stream_task_events`、`move_task_to_back`。新增能力应保持 v1 和既有命令参数、返回字段及错误码兼容，通过能力列表扩展。网页必须有旧 APK 的降级路径；不要因为新增一个可选能力就抬高最低桥版本。

写请求只有在确认原生命令不存在时才切换到请求头通道。网络超时/结果不明不会自动换通道重发，应继续使用业务原请求与原幂等标识确认结果。旧请求头桥仍有约 12,000 字符 base64 上限；长输入需要具有 `proxy_request` 的 APK。系统权限、新原生通知渠道、凭证存储变更等仍可能需要更新 APK。

## 验证

```sh
.venv/bin/python -m pytest -q tests/integration/test_web_release.py tests/integration/test_native_bridge_contract.py tests/integration/test_web_static_assets.py tests/integration/test_web_app.py tests/integration/test_security_hardening.py tests/integration/test_web_client_contract.py
tests/browser/run.sh release_updates.mjs agent_workbench.mjs
cd mobile && pnpm typecheck && pnpm test
cd src-tauri && cargo test --locked
```

Chromium 用隔离后端和模拟旧/新 APK 命令面验证刷新与降级；它不替代真机的 WebView、网关及安装包验收。

## 2026-10-22 生产启用记录

- 功能提交 `db86f38667db` 已通过标准脚本部署到 ai-server；API、worker、scheduler 均 healthy，数据库迁移保持 `055`。
- 使用模拟器中 2026-10-21 09:32 已安装的 Debug APK（`com.zcharlie.mobile.debug`，版本 `0.1.1` / `1001`），未重新构建或安装。APK SHA-256 为 `4ab171d146d33e6b59f38c35daade16598ab2b97d51222f043cd8c2d82d57df2`。
- 通过真实生产网关 `http://192.168.2.1:3200` 接入后端，验收使用仅限 zcharlie 后端、有效期一天的临时设备凭证；原有配对单独保留以便验收后还原。
- 首次重新进入研究台后，旧 APK 已加载检测脚本；页面与 `/app/release` 的版本均为 `27c28eff3cccfcdc0b50fe02`，版本清单响应为 `200` / `Cache-Control: no-store`。
- 该 APK 调用 `bridge_capabilities` 返回命令不存在；网页正确协商为 `{bridge_version: 0, legacy: true}`。原有 `proxy_request` 仍成功读取生产 `/healthz`，返回功能提交版本。
- 本记录作为随后的一次文档发布，用于继续验收旧 APK 的周期检测、未提交输入保护和安全空闲后的自动刷新。该阶段保持 APK 与页面安装状态不变，不手动刷新页面。
- 后续文档发布 `baaeaad0470c` 后，旧 APK 自行提示有新版本并保留临时输入；处理完输入后自动重新加载。页面版本从 `27c28eff3cccfcdc0b50fe02` 变为 `a5711b9b09f6156061628813`，与生产清单一致，浏览器导航类型为 `reload`，本地刷新记录的目标版本也一致。
- 验收前后 APK SHA-256、版本号和安装时间一致。复验使用系统触控与输入，避免程序化填值未触发 Android WebView 焦点事件而影响初始值采集。
