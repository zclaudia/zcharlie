# zcharlie — 个人 A 股研究台

单用户、研究辅助型的个人投资研究系统：持续收集自选 A 股的证据、计算可复现的
确定性评分、发现"上次以来的变化"，并把 Agent 解释严格限制在已审计字段上。

**研究辅助，非投资建议。** 没有下单、模拟盘、自动调仓或任何券商连接路径。

- 数据链路：`ifind-proxy → canonical(ifind-canonical.v1) → 确定性内核(评分 v1.3) → Agent 解释 → 持久化`
- 长任务：数据库队列 + 独立 worker，支持幂等、原子认领、心跳、孤儿恢复与有限重试
- 持久化：SQLite + Alembic（迁移 `012`）；制品目录 `data/research-runs/<run_id>/`
- 界面：同源 Web UI（`/app`，会话 Cookie 登录）+ CLI + HTTP API

## 快速开始（空环境到第一次研究）

```bash
# 1. 安装（Python 3.13）
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. 配置
cp .env.example .env
# 编辑 .env：API_KEY、IFIND_PROXY_URL/IFIND_PROXY_API_KEY（数据网关）、
# LLM_TIER3_*（解释模型，任何 OpenAI 兼容端点均可）

# 3. 建库（alembic 迁移，尊重 DATABASE_URL）
ia migrate up

# 4. 三个进程：API、worker、scheduler
ia api serve          # 默认 127.0.0.1:8000；远程/Docker 需显式 --host 0.0.0.0
ia worker start       # 研究执行器；调试可用 ia worker start --once
ia scheduler start    # A 股日终自选股刷新（Asia/Shanghai，版本化交易日历）

# 5. 打开 Web UI 登录
open http://127.0.0.1:8000/app
# 用 .env 里的 API_KEY 登录；密钥只用于服务端验证，不写入 Cookie。
```

Web 内即可完成日常闭环：添加自选股（代码 + 行业 + 报告年度）→ 刷新全部 →
在 `/app/stocks/<ticker>` 阅读确定性评分、因子、指标、数据质量、缺失字段、
freshness、Agent 解释（引用可点击定位证据）与"自上次以来的变化"。
运行中心 `/app/runs` 展示 queued/running/success/partial/failed、阶段、
重试、错误摘要与受控制品下载。

## CLI

| 组 | 命令 | 用途 |
|---|---|---|
| `ia migrate` | `up/down/current/history` | alembic 迁移（尊重 `DATABASE_URL`） |
| `ia api` | `serve` | FastAPI 服务 |
| `ia worker` | `start [--once] [--kinds ...]` | 队列 worker（研究 + 快照采集，按 kind 分派） |
| `ia scheduler` | `start` | 日终刷新与快照采集调度 |
| `ia snapshot` | `sync-universe`、`sync-industry`、`collect-market`、`collect-financial`、`status` | 时点快照采集（见下节） |
| `ia research` | `ifind <ticker>` | 单股同步研究（诊断入口，制品同 API 路径） |
| `ia admin` | `cost`、`db-migrate` | LLM 成本、旧迁移入口 |

## 快照采集

时点（PIT）快照由 worker 执行、scheduler 入队。数据源分工（D6）：**baostock 负责目标池广度**
（日线、估值、季度财务、披露日），**iFinD 只服务观察池**（`--scope watchlist`，额度有限）。
日线与财报任务的 payload 带 `provider`（`baostock` 默认 / `ifind`）与 `scope`
（`universe` 目标池成分 / `watchlist` 启用的观察池条目）；iFinD 只接受 `watchlist`。
幂等键为 `kind:provider:scope:date(:period)`（成分 / 行业为 `kind:universe:date`）。

| 任务 kind | CLI | 调度（Asia/Shanghai） | 内容 |
|---|---|---|---|
| `universe_sync` | `ia snapshot sync-universe --universe CSI300 [--as-of]` | 每周一 08:00 | baostock 成分 → `universe_membership`；有进出发 `info` 通知 |
| `industry_sync` | `ia snapshot sync-industry [--universe CSI300 \| --all-market] [--as-of]` | 每月首个交易日 08:00 | baostock 证监会行业 → `industry_classification`；不带参数时默认配置的目标池（`snapshot.universes`），全市场需显式 `--all-market` |
| `market_snapshot` | `ia snapshot collect-market [--provider baostock\|ifind] [--scope universe\|watchlist] [--universe CSI300] [--date]` | 交易日 18:30 | 增量日线：不复权 `none`（估值）+ 后复权 `backward`（收益），不存前复权；baostock 行附 `pe_ttm`、`pb_mrq`、`ps_ttm`、`turnover`、`trade_status`、`is_st` |
| `financial_snapshot` | `ia snapshot collect-financial --period 2026-06-30 [--provider] [--scope] [--universe] [--as-of]` | 每周六 09:00 + 4/30、8/31、10/31 后首个交易日 19:00 | 最近已过法定截止期的报告期 + 披露季进行中的报告期，只采该数据源尚无该期的标的；截止日后顺带逾期检测 |

- **baostock（目标池）**：一次运行只登录一次，调用经进程级锁串行，无需令牌桶。每只股票每个序列从该数据源最后一个
  交易日 +1 增量取到 `--date`（无历史的从当日开始，深度历史由回补负责）。停牌日（`trade_status=0`，
  价格为前收、量为 0）原样入库并在运行元数据 `flags` 中列出，ST 同理。单只失败只影响该只；
  连续 5 次瞬时错误视为服务不可用，剩余标的不再调用、记为 `failed`，由 worker 稍后重试。
  财报：profit / balance / cash_flow / growth 四表合并为 canonical 字段（映射登记在
  `research/ifind_mapping.py` 的统一注册表，逐字段标明来源与单位；比率均为小数），
  `availability=actual`、`available_from`=baostock `pubDate`；未披露期为 `pending`。
  CSI300 日增量约 600 次查询、约 2.5 分钟；一个报告期全量约 1200 次查询、约 4 分钟（未披露的只查 1 表）。
- **总股本**：baostock 日线不带股本；因子计算市值时使用该日已可得的最新 baostock 财报
  `total_shares`（`FinancialSnapshotRepository.total_shares_as_of`）。iFinD 观察池采集把 `/stock/info`
  股本直接写在当日不复权行上，`raw_ref` 不再拼接后缀。
- **iFinD（观察池）**：每次 proxy 调用最多 3 只标的；全部调用经共享令牌桶节流（`IFIND_COLLECT_RATE_PER_MINUTE`，
  默认 40/分钟），瞬时错误按批重试（`IFIND_COLLECT_BATCH_RETRIES`，默认 2）。
- **iFinD 额度耗尽**：ifind-proxy 返回 `429`（或旧版本返回带 `usage limit` / `[HTTP 403]` 的 `502`）时归类为
  `QuotaExhaustedError`，不重试：采集任务立即停止剩余批次、保留已入库数据，以 `partial` 收尾
  （元数据 `stop_reason=quota`，未采标的标记 `quota`），并发一次 `action_required` 通知；下次调度自然续采。
  单股研究直接以 `failed` 结束并注明额度耗尽，不占用重试次数。
- **披露逾期**：目标池财报任务在 `as_of` 晚于法定截止日（Q1 4/30、半年报 8/31、Q3 10/31、年报次年 4/30）时，
  把仍无该期财报的成分股写入 `report_overdue`（每只每期一次）并发一次 `action_required` 通知；
  迟到的财报入库时以实际 `pubDate` 写 `resolved_on`，记录保留。
- 运行元数据记录完整率：应采 / 实采 / 缺失清单（`failed` 错误、`no_data` 无数据、`pending` 财报未披露、
  `quota` 额度耗尽未采）。有 `failed` 或 `quota` 为 `partial`，全部失败为 `failed`；完整率低于
  `SNAPSHOT_COMPLETENESS_THRESHOLD`（默认 0.95）、失败、额度耗尽或新增逾期时发 `action_required` 通知（每次运行至多一次）。
- 原始响应落盘到 `DATA_DIR/snapshot-runs/<run>/attempt-<n>/`，`raw_ref` 指向该文件。
- `--now` 在当前进程立即执行（调试用）；`ia snapshot status` 查看各类任务最近运行与完整率。
  调度配置见 `config/app.yaml` 的 `snapshot:` 段；日线 18:30 基于 baostock 晚间更新，需在正常交易日实测确认。

## HTTP API（`X-API-Key` 或 Web 会话）

| 端点 | 用途 |
|---|---|
| `GET /healthz` | 存活探针（无鉴权） |
| `POST /research/single-stock` | 入队单股研究（HTTP 202 + `run_id`，幂等键） |
| `GET /research/runs`、`/research/runs/{id}` | 运行列表 / 生命周期详情 + 版本化结果 |
| `GET /research/runs/{id}/changes`、`/change-notification` | 结构化 diff / 重要变化通知下载 |
| `GET /research/runs/{id}/artifacts[/{name}]` | 受控制品下载（限本 run 目录内） |
| `GET /research/stocks/{ticker}/history` | 分页快照历史 |
| `GET /dashboard/today` | 今日概览聚合（运行中/失败/partial/待复核/重要变化） |
| `GET/POST/PATCH/DELETE /watchlist`、`POST /watchlist/refresh` | 自选股管理与隔离型批量刷新 |
| `GET /admin/llm-calls`、`/admin/llm-calls/cost` | LLM 调用审计 / 月度成本 |

批量刷新：`Idempotency-Key` 重放同一批运行；单只失败不影响其他标的；
只有 meaningful change 才生成一次 Markdown 通知，baseline 与噪声保持静默。

## 配置

`.env`：`API_KEY`、`DATABASE_URL`（默认 `sqlite+aiosqlite:///./data/app.db`）、
`DATA_DIR`、`IFIND_PROXY_URL` / `IFIND_PROXY_API_KEY`、
`IFIND_COLLECT_RATE_PER_MINUTE`、`IFIND_COLLECT_BATCH_RETRIES`、`SNAPSHOT_COMPLETENESS_THRESHOLD`、
`LLM_TIER3_*`、`LLM_REQUEST_TIMEOUT_SECONDS`（默认 120）、
`WEB_SESSION_TTL_MINUTES`（默认 720）、`WEB_SESSION_SECRET`、`WEB_COOKIE_SECURE`、
`WEB_PUBLIC_ORIGIN`（远程部署见 [`docs/product/security-deployment.md`](docs/product/security-deployment.md)）、
`LOG_LEVEL`。调度见 `config/app.yaml`；交易日历见
`config/calendars/a_share.yaml`（未知年份 fail-closed）。

评分成功但解释失败时运行以 `partial` 收尾：确定性分数与制品保留，
`explanation` 为 `null` 并注明原因，不重试非瞬时错误。

## 备份与恢复

见 [`docs/product/backup-and-recovery.md`](docs/product/backup-and-recovery.md)：
`tools/backup_research.py` 生成一致备份与 SHA-256 manifest，
`tools/verify_backup.py` 校验，恢复演练已自动化为集成测试。

## 测试与门禁

```bash
pytest -q -m "not network"  # 601 个测试（离线，fixture 全部脱敏）
ruff check .
mypy src/investment_agent  # strict
```

CI（`.github/workflows/ci.yml`）在 push/PR 上运行同样三门禁；默认测试不访问
网络、不使用真实 secrets。golden corpus（13 只不同特征 A 股）离线重放，
任何算法变化必须升级 scoring version 并附差异说明，不允许静默改 golden。

## 已知限制

- 严格单用户；无组织、权限组或订阅体系。
- SQLite 单机存储；并发写入成为瓶颈时才评估 PostgreSQL。
- 目标池广度数据只有 baostock 一个来源（D6）；iFinD 经 Kimi 网关有 5 小时滚动额度，只用于观察池。
- 交易日历仅审核到 2026 年；未审核年份 fail-closed。到期前 60 天起调度日志与
  `/dashboard/today` 会给出警告，交易所公布次年安排后需补入 `config/calendars/a_share.yaml`。
- 北交所行情（history/realtime）暂被上游网关阻塞，corpus 中北交所样本
  以沪深小盘替代并记录在
  [`tests/fixtures/research_kernel/manifest.yaml`](tests/fixtures/research_kernel/manifest.yaml)。
- 无任何交易执行路径；未来若考虑自动交易必须作为独立项目重新评审。

## Legacy

早期 NASDAQ-100 Discovery / Attribution 产品已从主干移除，代码保留在 tag
`legacy-nasdaq-final`（设计文档 `docs/superpowers/specs/2026-06-07-investment-agent-design.md`）。

## 文档

- 产品与工程基线：[`docs/product/engineering-baseline.md`](docs/product/engineering-baseline.md)
- 交付路线：[`docs/product/delivery-roadmap.md`](docs/product/delivery-roadmap.md)
- MVP 收口计划：[`docs/product/mvp-completion-plan.md`](docs/product/mvp-completion-plan.md)
- 发布清单：[`docs/product/mvp-release-checklist.md`](docs/product/mvp-release-checklist.md)
- 备份恢复：[`docs/product/backup-and-recovery.md`](docs/product/backup-and-recovery.md)
- 评分 v1.3 变更与回归差异：[`docs/product/scoring-v1.3-changes.md`](docs/product/scoring-v1.3-changes.md)
- 安全部署：[`docs/product/security-deployment.md`](docs/product/security-deployment.md)
