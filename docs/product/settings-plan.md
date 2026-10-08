# 运行时配置：显式清单 + DB 存储 + Web UI 管理

状态：Accepted 2026-10-08（决策见 §2）

## 1. 背景

现状：22 项配置写在 `.env`（pydantic-settings `Settings`），调度相关写在
`config/app.yaml`。问题：

- 改业务参数（候选过期天数、iFinD 预算等）要改文件并重启 api/worker/scheduler 三个进程；
- worker / scheduler 常驻进程只在启动时读一次配置；
- LLM / iFinD 密钥只能在文件里改，无法在 UI 里测试连通性；
- 配置的取值范围、说明散落在代码注释、`.env.example`、`app.yaml` 注释里。

## 2. 决策

| # | 决策 | 结论 |
|---|---|---|
| S-D1 | 分层 | **启动层**（连上 DB 之前、或登录本身需要的）留在 env；**运行时层**（外部连接、业务调参、调度）进 DB，由 Web UI 管理 |
| S-D2 | 优先级 | **DB 唯一真相**。env / `app.yaml` 中的运行时值只在首次导入时使用；导入后再改 env 不生效，启动时对"env 与 DB 不一致"打 warning |
| S-D3 | 密钥 | LLM / iFinD API key **加密存 DB**（Fernet），主密钥为新的启动层 env `SETTINGS_ENCRYPTION_KEY`；UI 只写不读，展示 `已设置 ···末4位` |
| S-D4 | 调度 | `app.yaml` 里的 cron / 股票池 / `hit_cap` / `calibration_count` 一并进 DB 与 UI；scheduler 检测到配置变化后重新排期，无需重启 |
| S-D5 | 显式清单 | 单一注册表声明每项：类型、范围、默认值、分组、中文标签与说明、是否密钥、生效方式。`.env.example`、API、UI 表单、校验全部由它派生 |

主密钥不复用 `WEB_SESSION_SECRET` / `API_KEY`：轮换它们不应让已存密钥失效。
主密钥也不放进 `DATA_DIR`（与 DB 同卷，备份泄露时等于明文）。

## 3. 配置清单

### 3.1 启动层（env，UI 只读展示）

| env | 说明 |
|---|---|
| `APP_ENV` `LOG_LEVEL` `DATA_DIR` `DATABASE_URL` | 进程启动 / 连库 |
| `API_KEY` `WEB_SESSION_SECRET` `WEB_SESSION_TTL_MINUTES` `WEB_COOKIE_SECURE` `WEB_PUBLIC_ORIGIN` | 认证与 Web 安全 |
| `SETTINGS_ENCRYPTION_KEY`（新） | 运行时密钥的加密主密钥；未设置时不能保存密钥类配置 |

`a_share_calendar_path` 保留在 `app.yaml`（部署资源路径，非业务参数）。

### 3.2 运行时层（DB，UI 可编辑）

生效方式：`live` = 下一次读取即生效；`next_job` = worker 下一个任务生效；
`reschedule` = scheduler 重新排期。

| 分组 | key | 默认 | 生效 | 来源 |
|---|---|---|---|---|
| llm | `llm_tier3_base_url` `llm_tier3_model` | 现有默认 | next_job | env |
| llm | `llm_tier3_api_key`（密钥） | — | next_job | env |
| llm | `llm_tier3_thinking` `llm_rule_draft_thinking` | auto | next_job | env |
| llm | `llm_request_timeout_seconds` | 120 | next_job | env |
| ifind | `ifind_proxy_url` / `ifind_proxy_api_key`（密钥） | 现有默认 | next_job | env |
| collection | `ifind_collect_rate_per_minute` `ifind_collect_batch_retries` `akshare_min_interval_seconds` `snapshot_completeness_threshold` | 现有默认 | next_job | env |
| verification | `ifind_budget_per_5h` `ifind_budget_window_hours` `ifind_verify_max_requests` | 现有默认 | next_job | env |
| candidates | `candidate_expiry_days` `candidate_dismiss_cooldown_days` | 14 / 30 | live | env |
| schedule | `watchlist_refresh_cron` / `_timezone` | yaml | reschedule | app.yaml |
| schedule | `snapshot.*`（universes、timezone、7 个 cron，含 `close_quote_cron` 15:10 / `close_quote_retry_cron` 16:00 的腾讯收盘快照） | yaml | reschedule | app.yaml |
| schedule | `screen.*`（universes、timezone、cron、hit_cap） | yaml | reschedule | app.yaml |
| schedule | `candidates.expiry_sweep_cron` / timezone | yaml | reschedule | app.yaml |
| schedule | `verification.*`（timezone、2 个 cron、calibration_count） | yaml | reschedule | app.yaml |

范围约束沿用现有 `Field(ge/le)` 与 `_known_universes` 校验；cron 额外用
APScheduler 解析校验（含"数字星期从周一=0"的坑：UI 提示用 `sun`/`mon` 名称）。

## 4. 设计

### 4.1 代码结构（新包 `investment_agent/settings/`）

- `registry.py`：`SettingSpec`（key、group、label、help、secret、apply、来源）与
  `RuntimeSettings`（pydantic 模型，字段即运行时层全集，含现有校验）。
- `crypto.py`：Fernet 加解密，主密钥缺失 / 错误时报可操作的错误。
- `store.py`：`SettingsStore`：读取（默认值 ⊕ DB 覆盖 → `RuntimeSettings`）、
  批量写（整体校验后一次事务写入 + 历史 + revision 自增）、`revision()`、首次导入。
- `loader.py`：`load_settings()` → 现有 `Settings`（启动层来自 env，运行时字段被
  DB 值覆盖）。保持 `settings.ifind_proxy_url` 等属性名不变，调用点改动最小。

`Settings` 里运行时字段保留（作为默认值与首次导入来源），但所有运行时消费方必须
经 `load_settings()` 取值；直接 `Settings()` 只用于启动层（连库前、CLI 帮助等）。

### 4.2 存储（migration 021）

- `app_setting(key PK, value_json, ciphertext, secret_hint, updated_at, updated_by)`
  ——密钥类只填 `ciphertext` + `secret_hint`（末 4 位）。
- `app_setting_change(id, key, old_value_json, new_value_json, changed_at, actor, note)`
  ——密钥类的 old/new 记为 `"***"` + hint。
- `app_setting_meta(id=1, revision, imported_at)`——revision 每次写入 +1。

### 4.3 首次导入

`ensure_imported()` 在 api / worker / scheduler 启动及 `ia settings import` 时调用：
`imported_at` 为空时，把 env 中**显式设置**的运行时项 + `app.yaml` 调度项写入 DB
（actor=`import`），并设置 `imported_at`。并发安全靠单事务 + meta 行条件更新。
导入后启动时若 env 中仍有与 DB 不同的运行时值，打一次 warning 提示可删除。

### 4.4 消费方

- API：`get_settings` 依赖改为 `load_settings()`（每请求一次 DB 读，单表小查询）。
- worker：每次 claim 前比较 revision，变化则重建 settings 与 handlers（含令牌桶）。
- scheduler：每 60 s 轮询 revision；变化则按新调度配置移除并重建所有 job，打日志。
- CLI：各命令改用 `load_settings()`；`ia snapshot` 的调度读取改走 store。
- dashboard / data_health 读 `app.yaml` 的地方改走 store。

### 4.5 API（全部需认证，写操作走现有 CSRF/会话校验）

- `GET /settings`：分组的清单 + 当前值（密钥只给 `configured` 与 hint）+ 是否默认值
  + 生效方式；附启动层只读视图（敏感项脱敏）。
- `PATCH /settings`：`{values: {key: value}, note?}`，整体校验，422 返回逐项 issues；
  密钥传空字符串 = 清除。
- `GET /settings/history?limit=`：变更历史。
- `POST /settings/test/llm`、`POST /settings/test/ifind`：可带未保存的候选值
  （密钥缺省时用已存值），返回成功 / 耗时 / 错误摘要；不落库。

### 4.6 Web UI（`/app/settings`，导航新增"设置"）

- 按分组卡片展示；每项：中文标签、说明、范围、默认值、生效方式徽标。
- 修改后显示"未保存 N 项"，统一保存；保存前客户端预校验，服务端 issues 逐项回显。
- 密钥输入框为空 = 不修改；"清除"单独按钮。
- LLM / iFinD 卡片各一个"测试连接"。
- 调度卡片显示 cron 的中文解释与下次触发时间（服务端计算）。
- 底部：最近变更历史；启动层只读卡片。
- 遵循现有约束：无内联 style（CSP）、DOM 只经 dom.js 构造。

### 4.7 CLI

`ia settings list | get KEY | set KEY VALUE | import [--force] | history`，
供无 UI 场景与 Docker 内运维。

## 5. 任务拆分

| 任务 | 内容 | 依赖 |
|---|---|---|
| S1 | settings 包：registry / crypto / store / loader、migration 021、首次导入、CLI；`cryptography` 加为直接依赖 | — |
| S2 | 消费方迁移：API 依赖、CLI 命令、worker 按 revision 重建；dashboard / data_health | S1 |
| S3 | scheduler 从 store 读调度并按 revision 重新排期 | S1 |
| S4 | settings API（含测试连接、历史） | S1 |
| S5 | Web 设置页 + 导航 | S4 |
| S6 | 文档：精简 `.env.example`、`operations.md`、README、docker 说明 | S2–S5 |

## 6. 验收

- 新库启动：`.env` 现有运行时值与 `app.yaml` 调度被导入，UI 显示一致；
- UI 改 `candidate_expiry_days` → 新候选立即使用新值；改 iFinD 预算 → worker 下一任务生效；
  改 screen cron → scheduler 日志出现重新排期且下次触发时间正确，三者均无需重启；
- DB 文件中搜索不到 LLM / iFinD 明文 key；缺主密钥时保存密钥给出明确错误；
- 全量测试通过；UI 文案不含交易用语（沿用现有测试）。
