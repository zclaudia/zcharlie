# Investment Agent · 设计文档

- **作者**：zhvala（与 Claudia 协同头脑风暴）
- **日期**：2026-06-07
- **状态**：设计已审定，等待实施计划
- **范围**：V1 (MVP)，含 V2 / V3 路线图

---

## 0. 目标与定位

构建一个面向**个人投资决策辅助**的 agent 系统。核心使命：

1. **发现机会（主线，每日定时）**：每个交易日收盘后，对设定的股票池（V1 限定 NASDAQ-100）跑多策略筛选，输出三类清单 —— 共识股、强分歧股、被忽略的优质股，辅助用户发现潜在投资机会。
2. **归因分析（辅线，按需触发）**：当用户对某只股票的最近波动好奇时，主动调用 agent，由系统分析"它为什么涨/跌" —— 量化分解（大盘 + 板块 + 个股特异性）+ 事件归因（新闻 / 财报 / 评级变动 / 上下游事件）。

系统**只提供分析与信号，不执行交易**（Phase A）。当用户对系统信任度建立后，未来扩展为推荐买入价 / 止损价 / 仓位建议（Phase B），但仍由用户手动下单。

## 1. 顶层架构

### 1.1 整体形态

单个 Python 应用，Docker 容器化部署。容器内三个入口共享同一份 Application Core：

- **FastAPI 服务**：对外 REST API + OpenAPI schema（为未来 Web/Mobile 端铺路）
- **APScheduler 内嵌调度**：美东 16:30 触发每日 Discovery
- **Typer CLI**：调试、一次性运行、数据库管理

```
┌────── investment-agent (Docker) ──────┐
│  FastAPI   APScheduler   CLI          │
│       │       │            │           │
│       └───────┴────────────┘           │
│                │                        │
│        Application Core                 │
│  ┌───────────────────────────────┐    │
│  │ Pipelines (Discovery / Attr)   │    │
│  │ Domain (Strategy / LLM /       │    │
│  │         Render / Notify /      │    │
│  │         DataSource)            │    │
│  └───────────────────────────────┘    │
│              │           │              │
│              ▼           ▼              │
│         SQLite     Local FS             │
└────────────────────────────────────────┘
```

### 1.2 关键技术决策

| 类别 | 选型 | 理由 |
|---|---|---|
| 编排范式 | 单进程 asyncio | 瓶颈在 I/O，无需多进程/多线程 |
| Web 框架 | FastAPI | OpenAPI 自动化 + Pydantic 集成 |
| 调度 | APScheduler 内嵌 | 单容器单进程，跨平台一致，无外部依赖 |
| CLI | Typer | 类型驱动、与 FastAPI 同生态 |
| ORM | SQLAlchemy 2.x async + alembic | SQLite→Postgres 平滑迁移 |
| HTTP 客户端 | httpx | async 原生、连接池复用 |
| Agent 框架 | Pydantic-AI | 类型安全、原生支持 OpenAI-compatible 与工具调用、零额外抽象惩罚 |
| LLM SDK | openai Python SDK | 通过自定义 `base_url` 兼容任意 OpenAI-compatible provider |
| 行情数据 | yfinance | 免费、覆盖广、V1 唯一数据源（DataSource Protocol 留扩展点） |
| 数据处理 | pandas + numpy | 行业事实标准 |
| 配置 | pydantic-settings + .env + YAML | 强类型 + 分层 |
| 日志 | structlog | 结构化、易查询 |
| 测试 | pytest + pytest-asyncio + respx | 标准组合 |
| 容器 | Docker + docker-compose | 简单稳定 |

### 1.3 部署形态

- **本地开发（Mac）**：`docker compose up`，挂载 source 目录以便热重载
- **生产（内网 Linux）**：相同 docker-compose.yml，通过 `.env` 切换 LLM provider / API key / 数据库路径；数据目录挂载到宿主机 volume

### 1.4 Agent 编排（Pydantic-AI）

我们的 "agent" 光谱有三类，Pydantic-AI 都能优雅覆盖：

- **结构化输出场景**（Tier 2 策略评分、Tier 3 深度分析）：定义 Pydantic 输出 schema，框架强制 LLM 返回该结构（带重试 / 校验）。
- **真 Agent 循环场景**（Attribution）：用 `@agent.tool` 装饰器注册 Python 函数为可调用工具，Agent 自主决定调用哪些、何时停。
- **纯算法场景**（Meta-Agent 共识识别）：不用 LLM，纯统计聚合。

**Pydantic-AI 实例不直接暴露给业务层**，而是包在内部 `LLMClient` 抽象里 —— 业务层不直接依赖 Pydantic-AI，将来想换框架只改这一处。所有 LLM 调用统一过 `LLMCall` 表记录用于成本审计与调试。

---

## 2. 领域模型 + 数据库 Schema

### 2.1 实体分组

按"变化速度"分层：

1. **参考实体（慢变化）**：Market、Sector、Company、Listing、Universe、UniverseMembership
2. **行情/事件数据（每日变化）**：DailyPrice、FinancialReport、FundamentalSnapshot、NewsItem、AnalystRatingChange
3. **公司关系图**：CompanyRelationship（supplier_of / competes_with / subsidiary_of / partner_of）
4. **用户域**：WatchlistItem、HoldingItem（V1 留空表，未来 Phase B 启用）
5. **策略与分析**：Strategy、PipelineRun、StockScore、DiscoveryReport、AttributionReport
6. **运维**：LLMCall、DataFetchJob

### 2.2 关键设计决定

- **Company / Listing 分离**：同一家公司可在多市场有不同 Listing（AAPL/NASDAQ、TSMC ADR/NYSE + 原股/TWSE）。上下游关系建立在 **Company 层**，不绑定 Listing —— 这呼应"美股 X 的供应商是 A 股 Y"的跨市场场景。
- **`UniverseMembership` 带 `valid_from / valid_to`**：成份股调整有历史，避免未来回测产生幸存者偏差。
- **`FinancialReport.raw_data JSON`**：保留拉取的原始结构，未来想加字段不用回填。
- **`FundamentalSnapshot` 与 `FinancialReport` 分离**：财报是季度真实事件；PE/PB 是"今日价格 ÷ 最近 TTM 财报"派生指标，每日生成。
- **`Strategy.version` + `config_json`**：策略改 prompt 或阈值就升 version；历史评分可追溯哪个版本产生。
- **`PipelineRun` 是审计核心**：discovery 和 attribution 共用同一张运行表，所有产出（scores / reports）通过 `pipeline_run_id` 关联。
- **`LLMCall` 全量记录**：每次 LLM 调用都入库，用于成本统计、调试、未来回测复盘。
- **`HoldingItem` 表先建立但 V1 不使用**：避免未来 Phase B 时改 schema。

### 2.3 公司关系（Company Relationship）

单表 + `relationship_type` 字段表达多种关系；不为每种类型建独立表。

**关系类型与方向**：

| 类型 | 方向性 | 存储约定 |
|---|---|---|
| `supplier_of` | 有向（A → B：A 供应 B） | 只存 supplier_of 方向，反向（customer_of）通过查询表达 |
| `subsidiary_of` | 有向 | 仅存子→父 |
| `competes_with` | 对称 | 按较小 company_id 在前的约定，查询用 helper 双向匹配 |
| `partner_of` | 对称 | 同上 |

**关键字段**：`strength` (0-1)、`revenue_share_pct`、`source`（'llm_extracted' / 'fmp' / 'manual' / 'sec_10k'）、`confidence`、`evidence_url`、`valid_from / valid_to`、`last_verified_at`。

**数据来源（V1）**：完全靠 LLM 在 Tier 3 深度分析时提取，独立 structured-output 步骤产出 `ExtractedRelationship` 列表 → `relationship_resolver` 服务 fuzzy match counterparty 到已有 Company 或创建 placeholder。**新提取的关系初始 `confidence < 0.7`，CLI `relationship review` 让用户手动批准升为 `manual_verified`。**

**用法**：
1. 注入 Tier 3 深度分析 prompt（让 LLM 评估基于事实而非记忆）
2. Attribution 时扫描关联公司近期事件
3. 跨市场场景：即使 V1 不分析 A 股，关系图能告知"AAPL 的供应商 Luxshare 最近有新闻吗"
4. Meta-Agent 报告中标记单点依赖风险（"AAPL 和 NVDA 都依赖 TSMC"）

### 2.4 数据库 Schema（核心表概览）

主键统一整型自增；所有表含 `created_at / updated_at`；外键加索引。完整建表语句详见第 3 节 Phase 0 实施计划生成的 alembic 迁移。表清单：

```
market, sector, company, listing, universe, universe_membership
daily_price, financial_report, fundamental_snapshot, news_item, analyst_rating_change
company_relationship
watchlist_item, holding_item
strategy, pipeline_run, stock_score, discovery_report, attribution_report
llm_call, data_fetch_job
deep_analysis_cache  (Tier 3 缓存)
```

### 2.5 为未来回测的预留

Schema 已经支持未来加严谨回测：所有 EOD 数据全量持久化、`universe_membership` 带历史有效期、`pipeline_run` + `stock_score` 全量保留、`strategy.version` 可追溯。**V1 仅做简化对账（"昨日推荐 vs 今日表现"hit-rate），严谨回测引擎留 V2。**

---

## 3. 模块边界（Protocol）+ 策略 Agent 设计

### 3.1 五个核心 Protocol

所有插件式扩展点都用 Python `typing.Protocol`：

```python
class DataSource(Protocol):
    name: str
    supported_markets: list[str]
    async def fetch_universe(self, code: str, as_of: date) -> list[ListingRef]: ...
    async def fetch_daily_prices(self, tickers: list[str], start: date, end: date) -> list[DailyPriceRecord]: ...
    async def fetch_financial_report(self, ticker: str, periods: list[str]) -> list[FinancialReportRecord]: ...
    async def fetch_fundamental_snapshot(self, ticker: str, as_of: date) -> FundamentalSnapshotRecord: ...
    async def fetch_news(self, ticker: str, days: int) -> list[NewsRecord]: ...
    async def fetch_analyst_ratings(self, ticker: str, days: int) -> list[AnalystRatingRecord]: ...

class LLMClient(Protocol):
    async def complete(self, messages, response_model=None, tools=None,
                       purpose: str, pipeline_run_id: int | None = None) -> CompletionResult: ...

class StrategyAgent(Protocol):
    code: str
    version: str
    async def screen(self, candidates: list[ListingSnapshot]) -> list[ListingSnapshot]: ...
    async def score(self, listing: ListingSnapshot, context: ScoringContext) -> StockScore: ...
    async def deep_analyze(self, listing: ListingSnapshot, context: DeepAnalysisContext) -> DeepAnalysis: ...

class Renderer(Protocol):
    async def render_discovery(self, report: DiscoveryReport, scores: list[StockScore]) -> RenderedContent: ...
    async def render_attribution(self, report: AttributionReport) -> RenderedContent: ...

class Notifier(Protocol):
    name: str
    async def send(self, content: RenderedContent, channel_metadata: dict) -> NotificationResult: ...
```

**V1 实现清单**：`YFinanceSource`, `OpenAICompatibleClient` (with tiered model config), `ValueAgent / GrowthAgent / RiskAggressiveAgent`, `MarkdownRenderer`, `FileNotifier`。

### 3.2 策略 Agent 的内部结构

`BaseStrategyAgent` 基类 + 子类只声明 **3 个声明式资产**：

1. `ScreeningRule`：声明式 DSL（required / excluded / preferred Conditions）
2. `scoring_prompt` 和 `deep_prompt`：Markdown / Jinja2 模板，存在 `prompts/strategies/<code>/` 目录
3. `scoring_schema` 和 `deep_schema`：Pydantic 模型

子类**不写实现代码**。新加策略 = 一个 `<code>.yaml` 配置 + 一个 Pydantic schema 类 + 两个 prompt 文件，约 100 行配置。

### 3.3 三个示例策略（V1 必做）

- **ValueAgent (`value` / v1.0)**：required `pe ∈ [0,25]`, `pb < 4.0`, `roe > 0.10`, `debt_to_equity < 2.0`；excluded `net_income < 0`；preferred `fcf_yield > 0.05`
- **GrowthAgent (`growth` / v1.0)**：required `revenue_growth_yoy > 25%`, `gross_margin > 40%`；PE 容忍上调到 60；prompts 强调 TAM / 市场扩张 / 技术护城河
- **RiskAggressiveAgent (`risk_aggressive` / v1.0)**：required `market_cap < 50B`, `beta > 1.3`；prompts 强调 catalyst / 空头比率 / 情绪信号 / 技术面

### 3.4 策略配置：YAML 真相源 + DB 镜像

- **真相源（source of truth）**：仓库内 `strategies/<code>.yaml`，含 ScreeningRule、prompt 路径、schema 引用、version、metadata
- **DB 中的 `strategy` 表**：YAML 的镜像 + 运行期数据；启动时同步入库；运行期不可直接编辑
- 改策略配置走 git review 流程；DB 用于外键关联和审计

### 3.5 Meta-Agent — 共识/分歧识别（纯算法）

输入：一次 pipeline_run 下所有 stock_score。输出：

- **共识（consensus）**：≥2 个策略评分 ≥ 70，平均分 ≥ 70，跨度 < 20
- **强分歧（divergent）**：max - min ≥ 40，至少一个 ≥ 70 且一个 ≤ 40
- **被忽略的优质股（overlooked）**：恰好一个策略 ≥ 80，其他都 < 50

阈值暴露在 `config/app.yaml`，用户可调；不用 LLM 保证可解释、可单测、零成本。

### 3.6 Tool 系统（为 Pydantic-AI Agent 提供）

工具是普通 Python 函数，按领域组织在 `src/tools/` 目录：

```
market_data.py    | news.py          | financials.py
relationships.py  | valuation.py     | beta_attribution.py
```

要求：完整类型注解 + docstring（Pydantic-AI 用这两个生成 tool schema）；先查 DB、缓存优先；不做副作用（DB 写入由 pipeline 层负责）。

**不暴露为 MCP server**（YAGNI），未来若需要再加包装。

### 3.7 模块依赖方向（单向）

```
入口层 (api / cli / scheduler)
    ↓
pipeline 层
    ↓
domain 层 (StrategyAgent / Renderer / Notifier)
    ↓
service 层 (LLMClient / DataSource / Tools)
    ↓
storage 层 (SQLAlchemy models + repository)
```

---

## 4. 数据流（Discovery + Attribution Pipeline）

### 4.1 Discovery Pipeline（每日 16:30 ET 后触发）

七个 Step 串联，单进程 asyncio 并发：

| Step | 内容 | 典型耗时 |
|---|---|---|
| 0 | 创建 PipelineRun 记录（status='running'） | < 1s |
| 1 | 数据准备：并发拉取 universe 所有 listing 当日 EOD / 基本面 / 新闻 / 评级 / 财报 | 1-3 分钟 |
| 2 | Tier 1 算法粗筛：每个策略并行，零 LLM 成本 | < 5s |
| 3 | Tier 2 LLM 评分：策略 × 候选笛卡尔积，asyncio.Semaphore(8) 控制并发 | 3-6 分钟 |
| 4 | Tier 3 深度分析：综合 Top 10 → 商业模式 + 风险 + 上下游 + 估值；同步触发 RelationshipExtractor | 3-7 分钟 |
| 5 | Meta-Agent 合成（纯算法） → DiscoveryReport | < 1s |
| 6 | 渲染 Markdown + 多 Notifier 分发 | < 3s |
| 7 | 收尾：更新 PipelineRun 状态、跑"昨日推荐 vs 今日表现"对账 | < 5s |

**总耗时**：日常 8-15 分钟；首日全量回填 30-60 分钟。

### 4.2 Attribution Pipeline（按需触发）

| Step | 内容 | 典型耗时 |
|---|---|---|
| 0 | 创建 PipelineRun 记录（kind='attribution'） | < 1s |
| 1 | 数据准备：近 30 日价格 / 板块/大盘对比 / 近 7 日新闻 / 财报 / 评级 / 关系图 | 5-10s |
| 2 | 算法归因：beta 回归分解 → market / sector / idiosyncratic | < 1s |
| 3 | LLM 归因 Agent（Pydantic-AI Agent loop）：自主调用工具，输出结构化 AttributionReport | 30-90s |
| 4 | 渲染 + 分发 → `attribution_{ticker}_{timestamp}.md` + API 同步返回 JSON | < 3s |

**总耗时**：45-120 秒；单次成本 0.05-0.3 USD。

### 4.3 缓存策略

| 数据 | 缓存策略 |
|---|---|
| `daily_price` / `fundamental_snapshot` | 当天写入后不再更新 |
| `financial_report` | 30 天才检查更新 |
| `company.description` | 180 天 refresh |
| `company_relationship` | 月度 re-verify |
| Tier 2 `stock_score` | 不缓存（每次 pipeline 重算） |
| **Tier 3 `deep_analysis`** | **同一财报周期内 90 天复用；触发失效条件：新财报发布 / 重大新闻 / 用户 `--no-cache`** |
| `attribution_report` | 不缓存（时效性强） |

`DeepAnalysisCache` 表的 key 为 `(listing_id, latest_financial_report_id)`。**月度 LLM 成本估计降到 5-20 USD**。

### 4.4 失败模式与降级

四层错误隔离：全局 → Pipeline Step → 单股票 → LLM 调用。每层独立 `try/except`，失败标 partial 后继续。

**降级模式核心**：即使 LLM 全瘫，Tier 1 + Meta-Agent 都不依赖 LLM，仍能产出"纯指标排名 + 公告事件提醒"日报。

**幂等性**：所有 DB 写入是 upsert，重复跑安全；CLI 支持 `--rerun` 或 `--new-run`。

**断点续跑**：`pipeline_run.metadata.last_completed_step` 记录进度；`pipeline resume <run_id>` 从断点继续。

### 4.5 调度时间

APScheduler 使用 `America/New_York` 时区 cron `"30 16 * * 1-5"`，自动适应夏令时。北京时间约 04:30 / 05:30 触发，04:45 / 05:45 完成。

---

## 5. API / CLI / 配置 / 错误处理 / 测试 / 范围

### 5.1 HTTP API（FastAPI）

API 是核心 —— CLI 是 API 的薄包装。所有端点以 Pydantic 模型定义请求/响应 → OpenAPI schema 自动生成 → 未来 Tauri Mobile / Web 用 `openapi-typescript-codegen` 一键生成强类型客户端。

**端点清单（节选）**：

```
POST   /pipelines/discovery/run             | GET /pipelines/{run_id}
POST   /pipelines/{run_id}/resume           | GET /pipelines/{run_id}/scores
POST   /attribute/{ticker}                  | GET /attribute/{ticker}/latest
GET    /reports/discovery, /reports/discovery/{date}, /reports/attribution
GET    /listings/{ticker}, /listings/{ticker}/scores, /listings/{ticker}/relationships
POST   /listings/{ticker}/relationships
GET    /watchlist, POST/DELETE /watchlist
GET    /strategies, /strategies/{code}
GET    /universes
GET    /admin/llm-calls, /admin/llm-calls/cost
GET    /healthz
```

**认证**：API Key（HTTP Header `X-API-Key`），单 key 配置（V1）；V2 升级 JWT。

**异步任务**：长任务支持 `?async=true`，立即返回 `run_id`，客户端轮询 `GET /pipelines/{run_id}`。

### 5.2 CLI（Typer）

命令分组：`pipeline`、`attribute`、`report`、`watchlist`、`strategy`、`relationship`、`data`、`admin`。详细列表见 `src/investment_agent/cli/`。

### 5.3 配置分层

- `.env`：机密信息（API key、LLM endpoint 配置、数据库 URL、并发上限、日志级别）
- `config/app.yaml`：universe 列表、enabled_strategies、pipeline 阈值、scheduler cron
- `strategies/*.yaml`：每个策略的 ScreeningRule + prompt 路径 + schema + version

### 5.4 错误处理与重试

- **全局**：未捕获异常 → 写 PipelineRun.error_summary + 日志告警 + `/data/alerts/` 文件
- **重试**：yfinance 5xx/网络 → 最多 3 次（1s/2s/4s）；LLM 429 → 3 次（5s/15s/45s）；4xx 不重试
- **日志**：structlog JSON 输出；关键事件 INFO+；LLM 输入输出 DEBUG（生产关闭）
- **告警 V1**：本地文件；V2 接 Telegram / 邮件

### 5.5 测试策略

```
tests/
  unit/         算法逻辑 100% 行覆盖（screening / meta-agent / beta / DCF）
  integration/  Pipeline 流程关键路径 + 关键失败模式
  fixtures/     录制的 yfinance / LLM 响应 + 代表股票财报样本
```

**目标**：算法 100% + Pipeline 流程 + API 每端点至少一对 happy/fail path。

**CI 检查**：`pytest -m "not slow"` + `mypy --strict src/` + `ruff check .`。

### 5.6 项目结构

```
investment-agent/
├── docker-compose.yml, Dockerfile, pyproject.toml, .env.example
├── alembic/, alembic.ini
├── config/app.yaml, config/universes/
├── strategies/{value,growth,risk_aggressive}.yaml
├── prompts/{strategies,attribution,relationship_extraction}/
├── src/investment_agent/
│   ├── api/, cli/, domain/, pipelines/, strategies/
│   ├── llm/, sources/, tools/, renderers/, notifiers/
│   ├── storage/, scheduler/, config.py
├── tests/
├── data/  (gitignored: app.db, reports/, cache/, logs/)
└── docs/superpowers/specs/2026-06-07-investment-agent-design.md
```

### 5.7 V1 范围

**做**：

- NASDAQ-100 单一 universe
- 3 个策略：value, growth, risk_aggressive
- Discovery pipeline（Tier 1+2+3+ Meta）
- Attribution pipeline（按需）
- Markdown 报告 + 文件归档
- FastAPI + CLI 双入口
- APScheduler 内嵌调度
- SQLite 持久化（含公司关系图）
- LLMCall 审计 + 月度成本汇总
- Docker 容器化（Mac 开发 + Linux 部署）
- 全栈测试 + 关键路径单测

**不做**：

- 任何交易执行（Phase A only）
- Web UI / Mobile 端（API-ready，前端零开发）
- A 股 / 港股数据源（DataSource Protocol 留扩展点）
- 严谨回测引擎（仅简化对账）
- FMP / 付费数据源接入
- 邮件 / Telegram / Notion 推送
- 多用户 / JWT
- MCP server 形式暴露工具

### 5.8 V2 / V3 路线图

- **V1.5**：HSI universe + Telegram 通知
- **V2.0**：Phase B（推荐买入价 / 止损 / 仓位）、FMP 数据源、严谨回测引擎
- **V2.5**：Web Dashboard（React + 复用 OpenAPI schema）
- **V3.0**：Tauri Mobile、多用户 + JWT、扩到 S&P 500

### 5.9 实施 Phase 与工作量估计

总 80-120 小时，10 个 Phase：

| Phase | 内容 | 工作量 |
|---|---|---|
| 0 | 项目脚手架 + Docker + DB schema + alembic + 配置层 | 4-6h |
| 1 | DataSource Protocol + YFinanceSource + 数据回填 CLI | 10-15h |
| 2 | LLMClient + Pydantic-AI 集成 + LLMCall 审计 | 8-12h |
| 3 | 策略框架 + 3 个策略实现（含 prompt 调试） | 15-20h |
| 4 | Discovery pipeline 完整流程 | 10-15h |
| 5 | Attribution pipeline + tools + 归因 Agent | 10-15h |
| 6 | Meta-Agent + Renderer + Notifier | 5-8h |
| 7 | FastAPI + CLI 包装 | 5-8h |
| 8 | APScheduler 集成 + Docker 部署 | 5-8h |
| 9 | 测试覆盖 + 文档 + 首日调试 | 10-15h |

每 Phase 完成后跑端到端 smoke test 再进下一阶段。

---

## 附录 A：已审定的关键决策记录

供未来重新评估时回看：

| 决策 | 选择 | 理由摘要 |
|---|---|---|
| 主要投资风格 | 多策略，价值优先 | 用户主诉求 |
| 市场范围 | NASDAQ-100 起步 | 数据成熟、API 免费、迭代成本低 |
| 跨市场实体模型 | Company / Listing 分离 | 上下游分析跨市场（如 AAPL ← TSMC） |
| 自动化级别 | Phase A only（仅分析） | 用户掌控交易；A → B 后续过渡 |
| 多 Agent 协作 | 分歧驱动 Meta-Agent | 分歧 = alpha 来源；平均掩盖关键信号 |
| 交付方式 V1 | Markdown 文件归档 | 简单、可靠、易扩展 Notifier |
| 运行环境 | Mac 开发 + 内网 Linux 生产 | Docker 化保证一致性 |
| 编程语言 | Python | 金融生态成熟、LLM SDK 成熟 |
| LLM 接入 | OpenAI-compatible 通用 | 用户多订阅、灵活切换 |
| LLM 分层 | Tier 1 算法 / Tier 2 中端 / Tier 3 顶配 | 漏斗式控制成本 |
| 数据源 V1 | yfinance | 免费、覆盖广；DataSource Protocol 留扩展点 |
| 回测 V1 | 简化对账（hit-rate） | 严谨回测留 V2，schema 已铺路 |
| Agent 框架 | Pydantic-AI | 轻量、类型安全、覆盖三类场景 |
| 公司关系存储 | 单表 + relationship_type + LLM 提取 + 手动复核 | YAGNI；FMP 接入留 V2 |
| 策略配置 | YAML 真相源 + DB 镜像 | git review + 运行期审计兼得 |
| Meta-Agent 类型 | 纯算法不用 LLM | 可解释、可单测、零成本 |
| 工具暴露形式 | Python 函数（不走 MCP） | YAGNI |
| API-first | 所有功能先 API，CLI 为薄包装 | 未来 Tauri Mobile / Web 零后端改动 |
