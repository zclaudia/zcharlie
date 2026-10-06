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
| `ia worker` | `start [--once]` | 研究队列 worker |
| `ia scheduler` | `start` | 日终刷新调度 |
| `ia research` | `ifind <ticker>` | 单股同步研究（诊断入口，制品同 API 路径） |
| `ia admin` | `cost`、`db-migrate` | LLM 成本、旧迁移入口 |

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
| 其余 `/pipelines`、`/attribute` 等 | legacy（见下节） |

批量刷新：`Idempotency-Key` 重放同一批运行；单只失败不影响其他标的；
只有 meaningful change 才生成一次 Markdown 通知，baseline 与噪声保持静默。

## 配置

`.env`：`API_KEY`、`DATABASE_URL`（默认 `sqlite+aiosqlite:///./data/app.db`）、
`DATA_DIR`、`IFIND_PROXY_URL` / `IFIND_PROXY_API_KEY`、
`LLM_TIER2_*` / `LLM_TIER3_*`、`LLM_REQUEST_TIMEOUT_SECONDS`（默认 120）、
`WEB_SESSION_TTL_MINUTES`（默认 720）、`WEB_SESSION_SECRET`、`WEB_COOKIE_SECURE`、
`WEB_PUBLIC_ORIGIN`（远程部署见 [`docs/product/security-deployment.md`](docs/product/security-deployment.md)）、
`LOG_LEVEL`。调度与 pipeline 见 `config/app.yaml`；交易日历见
`config/calendars/a_share.yaml`（未知年份 fail-closed）。

评分成功但解释失败时运行以 `partial` 收尾：确定性分数与制品保留，
`explanation` 为 `null` 并注明原因，不重试非瞬时错误。

## 备份与恢复

见 [`docs/product/backup-and-recovery.md`](docs/product/backup-and-recovery.md)：
`tools/backup_research.py` 生成一致备份与 SHA-256 manifest，
`tools/verify_backup.py` 校验，恢复演练已自动化为集成测试。

## 测试与门禁

```bash
pytest -q                  # 578 个测试（离线，fixture 全部脱敏）
ruff check .
mypy src/investment_agent  # strict
```

CI（`.github/workflows/ci.yml`）在 push/PR 上运行同样三门禁；默认测试不访问
网络、不使用真实 secrets。golden corpus（13 只不同特征 A 股）离线重放，
任何算法变化必须升级 scoring version 并附差异说明，不允许静默改 golden。

## 已知限制

- 严格单用户；无组织、权限组或订阅体系。
- SQLite 单机存储；并发写入成为瓶颈时才评估 PostgreSQL。
- 数据源仅有 iFinD（经 ifind-proxy）；无第二数据源容灾。
- 交易日历仅审核到 2026 年；未审核年份 fail-closed。到期前 60 天起调度日志与
  `/dashboard/today` 会给出警告，交易所公布次年安排后需补入 `config/calendars/a_share.yaml`。
- 北交所行情（history/realtime）暂被上游网关阻塞，corpus 中北交所样本
  以沪深小盘替代并记录在
  [`tests/fixtures/research_kernel/manifest.yaml`](tests/fixtures/research_kernel/manifest.yaml)。
- 无任何交易执行路径；未来若考虑自动交易必须作为独立项目重新评审。

## Legacy：NASDAQ Discovery / Attribution（冻结维护）

系统早期面向 NASDAQ-100 的多策略 Discovery（Tier2 全量打分 → Tier3 深度分析）
与单票 Attribution（β 分解 + LLM 叙述）保留为 legacy：`ia pipeline discover`、
`ia attribute run <TICKER>`、`ia auxiliary ...` 及对应 HTTP 端点仍可用，
但不扩展、不阻塞 A 股 MVP。相关文档见
`docs/superpowers/specs/2026-06-07-investment-agent-design.md`。

## 文档

- 产品与工程基线：[`docs/product/engineering-baseline.md`](docs/product/engineering-baseline.md)
- 交付路线：[`docs/product/delivery-roadmap.md`](docs/product/delivery-roadmap.md)
- MVP 收口计划：[`docs/product/mvp-completion-plan.md`](docs/product/mvp-completion-plan.md)
- 发布清单：[`docs/product/mvp-release-checklist.md`](docs/product/mvp-release-checklist.md)
- 备份恢复：[`docs/product/backup-and-recovery.md`](docs/product/backup-and-recovery.md)
- 评分 v1.3 变更与回归差异：[`docs/product/scoring-v1.3-changes.md`](docs/product/scoring-v1.3-changes.md)
- 安全部署：[`docs/product/security-deployment.md`](docs/product/security-deployment.md)
