# 架构评审报告：超大文件与耦合度

- 日期：2026-10-14
- 评审基线：`main` @ `63a2b4d`（工作区干净）
- 评审方式：只读。12 个并行分区评审（逐文件通读 + `wc -l`/AST 实测 + 调用点 grep）+ 1 次全仓机械普查（god function/class、重复窗口、import SCC）。**未修改任何代码、未运行 pytest**。
- 结论口径：所有 `file:line` 均来自本轮实际阅读；行号对应该 commit。

---

## 一、摘要

**规模现状**：`src/investment_agent` 330 个 Python 文件 / 99,412 行；测试 297 文件 / 约 82,000 行（测试:生产 ≈ 0.83:1）；前端手写资产 23 个 JS + 2 个 CSS / 13,489 行（无构建链）；mobile 1,205 行。源码中 **≥500 行的文件 71 个，≥800 行 35 个，≥1000 行 21 个**。

**五个最重要的结论：**

1. **最大的问题不是"文件行数"，是少数几个"巨型函数 + 巨型类"。** 全仓 52 个函数 ≥120 行；最严重的是 `portfolio/service.py:691 _project_decimal`——**342 行、圈复杂度 ~152，同时是全仓最大和分支最多的函数**，并且它的 23 个累加器共享可变状态、唯一的 DB 查询内联在持仓循环里（N+1）。其次是 `research/ifind_chain.py:497 canonicalize_ifind_results`（333 行）、`mcp_server/server.py:86 build_server`（320 行，10 个 tool 注册 + 全部 handler 主体 + 序列化挤在一个闭包体）、`evidence/store.py:131 save_evidence`（304 行，5 条来源分支各复制一份 79 行字段核对清单）。类方面 `PortfolioService` 935 行/20 方法、`PeerContextResolver` 883 行/30 方法、`PublicationService` 768 行/24 方法。

2. **持久层没有成为网关，这是耦合度最重要的单一事实。** **112 个 `storage/` 之外的模块直接构造 ORM 语句（338 处）**，另有 231 处直接 `session.execute/scalars`、271 处直接 import ORM model；而 import `storage.repositories` 的模块只有 96 个——两个集合并不重合。后果：没有任何单一位置能回答"谁在读写这张表"，改表结构只能全仓 grep。同时 `storage/repositories` 自身混入了业务算法（`universe.rebuild_history` 146 行 PIT 成员重算、`industry.rebuild_timelines` 93 行、`pipeline_run.py:171-178/226-236` 的 agent 队列准入策略），所以它既不是纯仓储也不是服务层。

3. **模块级"无环分层"已不成立，靠约 150 处函数内延迟 import 维持可导入性。** 全仓 import 图只有 3 个强连通分量，但其中之一是 **约 80 个模块的巨型 SCC**（覆盖 agent/evidence/investor/storage/industry_peers/settings/valuation）。已确证的**硬分层反转**三处：`storage/repositories/pipeline_run.py` ↔ `agent`（storage 执行 agent 队列策略）；`providers/ledger.py` → `storage`（IO 适配层依赖持久层）；`industry_peers/jobs.py:82` → `cli/commands/peer_annual.py`（域包反向依赖 Typer CLI）。另有 `verification ⇄ snapshots/research`、`investor ⇄ agent`、`config → providers/investor`、`settings.registry → scheduler.runner._parse_cron`（私有函数）四处包级环，全部靠函数内 import 拆开——**静态分析看不到这些边，任何一次"把惰性 import 提到模块级"的整理都可能变成启动期 ImportError**。

4. **重复实现的规模被低估了，因为它分散在 100+ 个文件里而不显眼。** 已量化的典型：11 份 UTC 转换器（3 个不同名字）、9 份 `_optional_*` 强转器、7 处 `is_trading_day` 兜底（"未复核年份按工作日"这一业务决策）、5 处以 04-30 为年报法定截止日、4 份 CNY 上市地作用域校验、3 份"至多一个活跃 opening_balance"、3 份 `_result` 工厂（其中两份 difflib 相似度 0.99）、2 份逐字相同的 `http_status_rejected`、2 份 `_number` 脏值解析（`verification/job.py:300` vs `snapshots/financial.py:121`——**同一字段在核验与采集两侧可能给出不同数值，直接翻转 verified/conflict 判定**）、5 个 provider 客户端各写一遍约 100 行的 throttle/熔断/账本/退避管道（合计 ~529 行）。净可删约 **800–1,200 行**，且多数是"同一知识两处实现"而非代码搬运。

5. **测试既是安全网，也是重构的主要阻力，而且 CI 上的保护力被高估了。** 91% 的生产模块被测试直接 import，规则/评分层有 26 个入仓 golden 作为强闸门——这是很好的基础。但：(a) **peer 黄金数据存放在被 `.gitignore` 忽略的 `/research/`（`git ls-files research/` = 0）且无 skipif 之外的守卫，干净检出/CI 上必然 FileNotFoundError**，即 industry_peers 最核心的一致性保证在 CI 上不存在；(b) 4 处"读生产源码文本 + grep 子串"的假测试（`test_peer_service.py:1339-1347`、`test_peer_wp13.py:1244-1251`）把实现细节钉死，等价重构也会变红；(c) 11 个测试模块互相 import，`test_peer_service.py` 事实上是 peer 簇的共享库；(d) 同一期望值（`0.3253` 等）散落在 8 个文件 26 处；(e) `tests/unit/` 中 34% 的用例要开 DB，"先跑单测"这一提速手段不存在；(f) 43 个测试文件 ≥500 行、8 个 ≥1000 行。

---

## 二、规模普查

### 2.1 源码最大文件（≥800 行，共 35 个）

| LOC | 文件 | 主要问题形态 |
|---|---|---|
| 1895 | `materials/us_sec.py` | 概念映射表 + 期间版本状态机 + 文档解析 + 草稿装配四类变更理由同处 |
| 1709 | `snapshots/health.py` | 26 个 Pydantic 响应类 + 告警规则 + 18 处裸 SQL 混居 |
| 1704 | `web/static/app.css` | 55 个分节、依赖物理顺序覆盖（不建议拆） |
| 1582 | `materials/metrics.py` | 6 个指标函数共享选择器（内聚，不建议拆）+ 共享词汇错位 |
| 1581 | `web/static/candidates.js` | 列表页 + 详情页 + 提名 + 参数编辑器 3 个 surface 合一 |
| 1499 | `research/ifind_chain.py` | HTTP 客户端 → 归一化 → LLM 解释 → 报告 → 编排落库 6 段 |
| 1473 | `industry_peers/jobs.py` | 制品存储 + 准入/入队 + handler + 重复编排 |
| 1441 | `providers/akshare_eastmoney.py` | transport shim + 摘要 + 事件三段 |
| 1422 | `industry_peers/calculations.py` | 纯计算（~1050 行）+ DB 读写（~187 行）混居 |
| 1414 | `web/static/settings.js` | 190 行静态目录 + 渲染逻辑 |
| 1312 | `industry_peers/resolver.py` | 880 行单体类 + 摘要格式化 + 事实校验 |
| 1254 | `snapshots/backfill.py` | 部件采集 + 续跑状态机 + 估算 |
| 1209 | `rules/zone_detection.py` | 判定 + 持久化 + 通知 outbox + 读取 |
| 1201 | `providers/eastmoney_annual.py` | 441 行 collector 类含重试/记账/解析 |
| 1178 | `factors/view.py` | god module：4 种职责 / 17 个导入方 |
| 1166 | `industry_peers/service.py` | 契约 + 作用域解析 + 读装配 |
| 1151 | `web/static/portfolio.js` | 台账展示 + 规则审阅段 |
| 1059 | `rules/drafting.py` | 20 个内部导入的交汇点 |
| 1032 | `portfolio/service.py` | 写侧编排 + 读侧投影 + 342 行巨型函数 |
| 1025 | `web/static/health.js` | 10 个只读分区 + 交易日历（唯一带写操作与轮询） |
| 1001 | `candidates/lifecycle.py` | 状态机 + screen 入库（函数粒度健康） |
| 956 | `cli/commands/peer_annual.py` | 约 780 行业务逻辑 + 约 150 行 Typer 壳 |
| 942 | `watchlist/drafts.py` | 请求-草稿-审批-同步状态机（函数粒度健康） |
| 926 | `verification/job.py` | 24 个内部导入 + 重造 snapshots 内部件 |
| 926 | `snapshots/eastmoney.py` | 采集 + 交叉校验（依赖 factors）+ verification 入队 |
| 918 | `investor/publication.py` | 契约 + 规则 + 持久化 + 读投影 + 编排 |
| 916 | `materials/contracts.py` | 单一冻结契约（不建议拆） |
| 911 | `materials/dividends.py` | 单一领域概念（不建议拆） |
| 896 | `research_kernel/scoring.py` | 186 行 `score_stock` + 35 个硬编码阈值/权重 |
| 870 | `scheduler/runner.py` | cron 层 + 11 个 job 入口 + 触发器工厂 + 6 个启动追赶 + 主循环 |
| 868 | `web/static/app.js` | 入口 + 3 个页面渲染器（约 770 行是页面实现） |
| 866 | `materials/hk_native.py` | 191 行 `parse`（九条结构检查）+ 逐发行人数据 |
| 840 | `materials/handlers.py` | 组合根（33 个内部导入，职责使然） |
| 836 | `providers/baostock_reference.py` | 382 行 client 类 |
| 821 | `cli/commands/snapshot.py` | 9 条命令 + 内联回填编排 |

### 2.2 最大函数（≥200 行，共 12 个）

| LOC | cx | 位置 |
|---|---|---|
| 342 | 152 | `portfolio/service.py:691 _project_decimal` |
| 333 | ~ | `research/ifind_chain.py:497 canonicalize_ifind_results` |
| 320 | ~ | `mcp_server/server.py:86 build_server` |
| 307 | 39 | `research/ifind_chain.py:1193 run_ifind_research_chain`（14 参数） |
| 304 | 57 | `evidence/store.py:131 save_evidence` |
| 302 | ~ | `materials/valuation.py:154 annual_pe` |
| 234 | 40 | `snapshots/health.py:1307 derive_warnings`（12 参数） |
| 231 | ~ | `research/snapshot_source.py:390 build_snapshot_bundle` |
| 217 | ~ | `evidence/documents.py:327 save_candidates` |
| 215 | ~ | `materials/dividends.py:348 dividend_yield` |
| 207 | 70 | `evidence/search.py:45 search_saved` |
| 200 | 54 | `evidence/documents.py:58 numeric_rows` |

### 2.3 最大类（≥350 行，共 16 个）

`PortfolioService` 935/20 方法、`PeerContextResolver` 883/30、`PublicationService` 768/24、`TaskService` 609/26、`InvestorService` 538/13、`EvidenceService` 498/5、`PipelineRunRepository` 497/19、`PeerCollectHandler` 484/13、`ConversationService` 458/12、`EastmoneyAnnualCollector` 441/15、`DisclosureService` 440/11，以及 `ValuationService`（valuation）424、`UserControlService` 409、`ValuationService`（portfolio）402、`BaostockReferenceClient` 382、`PortfolioPlanService` 360。

---

## 三、耦合普查

### 3.1 扇入热点（被多少模块 import）

`storage.database` 71 · `config` 69 · `investor.common` 68 · `clock` 44 · `storage.repositories.pipeline_run` 41 · `storage.models.reference` 41 · `storage.models.analysis` 33 · `investor.foundations` 30 · `settings.loader` 29 · `storage.models.pit_snapshot` 29 · `investor.enums` 28 · `evidence.contracts` 28 · `storage.models.base` 27 · `agent.tasks` 26 · `providers.errors`（40 个文件）

**判读**：`clock`(44)、`providers.errors`(40)、`evidence.contracts`(28)、`storage.models`(27-41) 的高扇入是**正确复用**（纯函数 / 纯异常 / 纯契约 / 纯声明式模型），不应为了"降低 fan-in"而引入分层。真正错位的是 `investor.common`——见 4.2。

### 3.2 包间边（去重后）

`api→storage` 73 · `snapshots→storage` 72 · `agent→storage` 53 · `agent→investor` 52 · `investor→storage` 43 · `snapshots→providers` 34 · `cli→storage` 29 · `rules→storage` 29 · `investor→agent` 25 · `industry_peers→storage` 22 · `portfolio→investor` 21 · `rules→factors` 15 · `industry_peers→investor` 15

### 3.3 循环（SCC）

- **SCC #1，约 80 个模块**：agent、evidence、investor、portfolio、storage、industry_peers、settings、valuation 等尽在其中。已确证的真实双向环包括 `agent.research ↔ agent.conversations`、`agent.research ↔ investor.methods/monitoring/publication`、`agent.tools ↔ agent.disclosure_tools/document_tools`、`evidence.store ↔ evidence.queries`、`evidence.store ↔ investor.foundations`。
- **SCC #2**：`rules.drafting ↔ watchlist.drafts`（`watchlist/drafts.py:49` 模块级 →，`rules/drafting.py:521/951` 函数内反查）。
- **SCC #3**：`api.routes.dashboard ↔ api.routes.data_health`。
- 另一条独立的包级环：`verification.budget → snapshots.collect → research.ifind_chain → verification.budget`（`verification/budget.py:34-39` ↔ `research/ifind_chain.py:1321`）。

### 3.4 分层评估（api / storage 专项）

**方向是健康的，"everything imports everything"不成立**：`storage → api` = 0；`storage → domain` = 2 且 `domain/` 自身零项目内 import（真叶子）；`storage/models/*` 对 storage 之外的 import = 0，无任何 ≥60 行函数，27 个文件全部 ≤250 行；`api/schemas/*` 对 ORM 的 import = 0（JSON 契约干净）。**这是一个质量很高的模型层。**

**真正的结构问题是"读路径没有归属层"，量化如下：**

- 22 个路由模块共 5,652 行，函数体 3,642 行中约 **790 行（22%）是纯 HTTP 管道**，其余 **2,852 行（78%）** 是领域逻辑/响应组装/数据访问；**65 处原始 ORM 调用发生在 HTTP handler 内**（如 `candidates.py:597-635` 在 handler 里做模板分组/命中排序；`dashboard.py` 用 155 行模块级 helper 做 read-model 分桶）。
- 158 个端点中只有 5 个路由模块使用了 DI 服务（`investor`/`portfolio`/`agent`/`methods`），17 个没有；且"有服务"的模块也会旁路——`investor.py:103` 穿透 `svc.repository`、`:580-582` 直接 `svc.session.scalars(select(...))`。
- **`mcp_server/server.py` 反向 import 了 7 个 `api.routes.*` 函数**（`:124/210/254/271/286/365/387`）。这不是"复用路由代码"，而是**共享的展示/查询函数没有中立位置**，导致 MCP 协议层被 FastAPI 模块结构锁死——重构路由 = 同时改 MCP，且因为是惰性 import，改名只在运行期炸。
- ORM 类型虽未污染 schema，但泄漏进路由签名：`candidates.py:210 rule_detail_out(row: RuleInstanceRecord, ...)` 是被 MCP 复用的**公开函数**，等于把 ORM 类型暴露成跨模块接口。

**其余分层反转：**

| 反转 | 证据 | 后果 |
|---|---|---|
| storage 执行 agent 策略 | `storage/repositories/pipeline_run.py:171-178,226-236` 函数内 import `agent.operations` | 非 agent 的 enqueue/claim 也拖入 agent 包；队列规则变更要改 storage |
| IO 适配层依赖持久层 | `providers/ledger.py:30-32` import `storage.database/models/repositories` | providers 包脱离 DB 不可用；全仓唯一一处 providers→storage |
| 域包依赖 CLI 包 | `industry_peers/jobs.py:82` ← `cli/commands/peer_annual.py:81` | 导入 job 注册表即拉入整个 Typer 应用 |
| settings 依赖 scheduler 私有函数 | `settings/registry.py:102` ← `scheduler/runner.py:_parse_cron` | `import investment_agent.settings` 拖入 scheduler 全量依赖 |
| 全体模块依赖的 config 反向依赖上层 | `config.py:109` → `investor.notification_http`；`:344/378` → `providers.baostock_reference` | config 不是叶子；进程级 `_weak_key_warned` 造成测试顺序依赖 |
| 核验子系统双向依赖 | `verification/job.py:58-76` 取 `snapshots.collect/market/financial/jobs` 内部件；`budget.py:34-39` 取 `research.ifind_chain` | verification 无法独立开关/抽取 |

### 3.5 测试与前端

- **前端是无构建链的原生 ES module**（唯一例外 `nav.js` 是 classic IIFE），有共享 `api.js` 客户端（18 个模块中 15 个 import）——比预期好。但 `base.html:89` 每页静态加载 `app.js`，后者以静态 import 拉入全部 18 个页面模块，**每页下载并解析全部 11.7k 行 / 557KB**，无 GZip、无动态 import。
- 前端安全网存在（`test_web_client_contract.py` 745 行 + `node --check` + 13 个 Playwright 脚本 + `test_web_candidates.py` 的架构级 lint），但**断言形式是"文件清单 + 源码子串"**：把函数原样搬到新文件（行为不变）也会变红；反之把含 `score` 的代码移出 `candidates.js` 会**静默失效**那条禁用护栏。`/app/candidates`、`/app/health`、`/app/runs` 三个页面**零浏览器 E2E**——恰好覆盖两个最大文件。
- 前端重复：百分比 4 份（语义与精度不同）、键值渲染 4 份、表格构建 2 份、`redirectOn401` 2 份 + 1 处内联；**`PROVIDER_LABELS` 两个版本且值已分叉**（`format.js:434` 是 `akshare:"AkShare"`，`health.js:87` 是 `akshare:"AkShare（东财）"`）——同一 provider 在两个页面显示不同中文名；`methods.js:23-40` 自建了一整套 HTTP 客户端（自持 15s 超时 + 401 分支）与 `api.js` 并行。

---

## 四、重点发现（按严重度）

### 4.1 [高] 巨型函数：真正不可约的只有一部分

**`_project_decimal`（342 行 / cx 152，`portfolio/service.py:691-1032`）** 的结构实测：状态初始化 26 行（**23 个累加器**：`quantities/restricted/cash/cash_unknown/history/conflicts/comparisons/costs/realized_records/dividends/deficits/excluded/baseline_key…`）；主循环 218 行（含 51 个 `if` 中的 42 个）；持仓物化 56 行；快照装配 34 行（**0 个 `if`**，扁平构造）。

- **不可约的复杂度约 120-150 行**：库存/成本/现金三条线的推进是**顺序耦合**（如 `:854-860` 必须先算出 `new_quantity` 才能判周期关闭），不是可表驱动的纯矩阵。
- **约 190 行是"共享可变状态 + 追踪副作用 + 物化"的编排开销**，可以抽走。
- 附加缺陷：`:958` 是函数内唯一 `await`，在持仓循环里对每个 listing 单独 `select(MarketSnapshot)`——50 持仓账户单次导入预览触发 100 次单行 SELECT（`_import_plan:340-347` 还要 before/after 两次）；这也使投影逻辑**无法用无 DB 单测覆盖**。
- L999-1032 的装配段是健康的扁平构造，拆分时应作为整体搬移，不要进一步切碎。

**`build_server`（320 行，`mcp_server/server.py:86-405`）**：10 个注册点（9 tool + 1 resource）全部内联；闭包 `_settings/_session/_listing_id` 与序列化助手 `_json/_utc/_number` 被所有 handler 共享，因此 handler 无法移出函数而不传三个依赖；每个 tool 主体既调领域函数又做字段级 JSON 组装，还夹带 12 处函数内 import。结果：无法按领域单测（只能整 server 起再 `call_tool`）。

**`save_evidence`（304 行，`evidence/store.py:131-434`）**：按来源分 5 条分支，其中 4 段 `for name in (...)` 字段核对循环共 79 行、字段清单 12/14/12/14 项，**前 12 项完全同序**。它不做查询构建（全部 `session.get`），但混了三件事：持久化/回执幂等、"verified 只能来自再推导"的域规则、跨包适配器分派（硬编码 `industry_peers.evidence/originals`、`materials.evidence`、`factors.view`）。新增来源 = 改这个文件 + 复制第 5 份字段清单。

**`reverse_dcf`（190 行 / cx 83，`valuation/service.py:160-349`）**：**真正的数值求解只有 1 行**（`:325 solution = solve(...)`），其余 189 行是 7 段"追加 errors + 降级 status"的校验流水线。函数名让人以为复杂度在数学上，掩盖了真正的风险面（PIT 资格与状态降级顺序），也阻碍 `percentile`(`:351-442`) 复用同一套 gate。

### 4.2 [高] `investor/common.py` 是全应用共享词汇表，但所有权错位

148 行、**0 个项目内 import**（真叶子）、fan-in 68，其中 **58 个（85.3%）来自 `investor/` 之外**（agent 18、evidence 9、industry_peers 9、portfolio 8、materials 5、api 5、valuation 4）。

逐符号判定：**全部 12 个符号都是"真正的共享契约"，零 convenience helper**——`utc_timestamp`/`decimal_input`/`DecimalValue`/`Revision`/`Text`/`Contract` 是线格式基元（`Contract.normalize_timestamps:37-42` 是校验器），`ObjectRef:45-105` 是跨域引用（60 个导入方），`Record`/`Quantity`/`FieldError`/`BusinessError`/`Mutation`/`OwnerId` 同理。实测 `import investment_agent.investor.common` 后 `sys.modules` 只多出自身。同样的错位在 `investor/enums.py`（fan-in 28，21 个外部，含 agent 9）、`investor/contracts.py`（22 中 13 外部）、`investor/foundations.py`（30 中 23 外部）。

**结论：内容不该拆，位置必须动。** 这不是"杂物抽屉"，而是"目录名说它属于投资者域，实际它是全应用契约层"。唯一需要设计的是 `ObjectRef.identifier_namespace:90-105`（把 10 个 kind 绑到 legacy int ID 策略）。

### 4.3 [高] `factors/view.py` 是全仓最典的 god module

1178 行内并存四类职责：纯 dataclass（`:230-386`, `:522-674`）、纯消解算法（`:200-227`, `:389-519`, `:699-846`）、DB 读取（`:758-818`, `:849-1034`）、iFinD 观测路径（`:1041-1178`）。**17 个导入方各自只用其中一小片**：`factors/registry.py` 只要 `SnapshotView`，`rules/*`/`investor/coverage.py`/`mcp_server` 只要 `load_snapshot_view`，`evidence/*`/`verification/*` 只要 `factor_fields`/`resolve_period_fields`。

机制：唯一的对外"门面"把 sqlalchemy 与 6 个 repository 塞进**所有**消费者，包括纯因子定义层。后果：任何 repository 签名变化波及 17 个模块的 import 图；纯消解函数无法在没有 DB 依赖的上下文复用（`verification/compare.py`、`research/snapshot_source.py` 都受牵连）。

### 4.4 [高] 三处硬分层反转 + 约 150 处延迟 import 掩盖的巨型 SCC

见 3.3/3.4 表。补充两个最不显眼的：

- **`industry_peers/jobs.py:82` 模块级 import `cli.commands.peer_annual.SEMANTIC_HASH_SCHEMA`**，而 `peer_annual.py:58` 顶层 `import typer`。于是"导入 job 注册表"（agent 工具、research worker 都会做）就把整个 Typer CLI 应用拉进依赖图。修法是纯搬移，成本 S。
- **`snapshots/eastmoney.py` 直接依赖 `factors` 层注册表**（`:64-68` 导入 `field_sources`，`:276-289` 取交叉校验容差）+ `:611/622` 调 verification 入队（为避环用函数内 import `:164`）；反向 `verification/job.py:58-77` 又把 snapshots 的 `HistoryRequest`/`parse_bar`/`ProxyBatchCaller` 当库用。采集器因此无法脱离因子注册表测试，快照与 verification 形成事实双向依赖。

### 4.5 [高] 巨型 SCC 的代价：静态可审计性归零

全仓 52 个 ≥120 行函数、16 个 ≥350 行类、3 处分层反转、112 个模块直连 ORM——**这些问题都不是独立的技术债，而是同一个根因的可见症状：模块级依赖图已经是环，于是所有"跨包协作"都退化成"随便 import，出环就用函数内 import 打补丁"**。具体代价：(a) import-linter / 任何静态规则无法在此仓库上给出有效信号；(b) 每当有人把某个惰性 import 提升为模块级（这是最自然的"整理"动作），就可能变成启动期 ImportError 或新环；(c) 新人无法从 import 判断一个模块"可否无库测试"——`industry_peers/calculations.py:19-20` 的 docstring 自称 "Pure and deterministic. No IO"，但顶层 import `sqlalchemy.select`/`AsyncSession`/`EvidenceService`，纯段与 DB 段只靠 `:1190` 一行注释分隔。

**这是唯一需要"先建立守卫、再动结构"的判断**：建议在任何 P1 拆分开始前加一条 import-graph 守卫测试（或 import-linter 配置），否则"搬 import"类改动缺少自动回退信号。

### 4.6 [高] 测试侧：CI 保护力不可验证，且断言形式阻碍重构

- **peer 黄金数据不在版本控制内**：`.gitignore:46-47` 忽略 `/research/`，`git ls-files research/` = 0；而 `test_peer_service.py:124-128`、`test_peer_api.py:74-78`、`test_peer_calculations.py:91-95`、`test_peer_cohort_import.py:39-40`、`test_peer_end_to_end.py` 全部指向 `research/industry-data-validation/evidence/run-02/...`（共约 396KB）且**只有一处 node 可用性 skipif，没有文件存在性守卫**。CI (`pytest -q -m "not network"`) 检出后必然 FileNotFoundError。**industry_peers 最核心的一致性保证在 CI 上不存在。**
- **4 处"读源码 grep"假测试**：`test_peer_service.py:1339-1347`（`assert "init_engine" not in source`）、`test_peer_wp13.py:1244-1251`、`test_material_metrics.py:73-75/836-856`、`test_material_valuation.py:51-53/561-579`。它们断言的是"源文本子串"，零行为覆盖，等价重构即红。
- **11 个测试模块互相 import**：`test_peer_resolver.py:98` / `test_peer_agent_tool.py:83` / `test_peer_wp13.py:95,111` / `test_peer_review_regressions.py:37,48-51` 从 `test_peer_service`（及 `test_peer_jobs`）导入 fixture，而 `tests/support/` 已存在却未收纳（其 docstring 明确"测试模块之间不得互相 import"）。三级链 `wp13 → resolver → service`。
- **重复覆盖**：同一数值断言 26 处 / 8 个文件（`0.3253` 出现在 7 个文件 14 处）；构造器 `store_snapshot` 3 份、`dec` 3 份、`seed_listings` 3 份、`make_mapping` 2 份。改一条 peer 数值规则至少牵连 8 个测试文件。
- **`tests/unit/` 中 34%（520/1508）的用例要开 DB**，79/142 个 unit 文件触及 DB；41 个文件各自 `create_all` 118 张表；`peer_db` 这类 fixture 只在参数里收下 `db_session` 以"初始化全局引擎"然后 `del` 掉——测试之间通过进程全局状态隐式耦合，无法按文件并行。
- `tests/unit/` 不是单元测试层、`tests/*.py` 顶层 38 个文件（13,320 行）既不属于 unit 也不属于 integration——**层级语义不成立，"先跑单测"不存在**。

### 4.7 [中] 重复实现清单（可直接收敛）

| 重复项 | 位置 | 份数 |
|---|---|---|
| UTC 转换器（3 个名字） | `api/routes/candidates.py:97,101`、`watchlist.py:339`、`rule_templates.py:48`、`storage/repositories/provider_call.py:21`、`candidate.py:35`、`pit.py:16`、`agent/conversations.py:85`、`snapshots/health.py:413`、`verification/budget.py:51`、`industry_peers/service.py:282`、`mcp_server/server.py:76` | 11-12 |
| `_optional_str/int/float/date` | `api/routes/research.py:452/456/460/464`、`watchlist.py:327/331`、`verification.py:46` | 9 |
| `is_trading_day` 兜底（未复核年按工作日） | `snapshots/health.py:444` == `earnings_events.py:130`；`scheduler/runner.py:198/237/579`、`portfolio/valuation_jobs.py:122`、`api/routes/portfolio.py:104` | 7 |
| 年报法定截止日 04-30 | 权威 `snapshots/disclosure.py:34-43`；重复 `verification/compare.py:232`、`scheduler/snapshot_jobs.py:105`、`valuation/service.py:248`、`candidates/promote.py:74`、`research_kernel/scoring.py:56` | 5 |
| 脏数值解析 `_number`（**语义已分叉**） | `verification/job.py:300-308` vs `snapshots/financial.py:121-129`（另 `breadth.py:349`、`market.py:152`、`eastmoney.py:239`==`health.py:427` 逐字相同） | 5 |
| CNY 上市地作用域校验 | `portfolio/service.py:239,308,627`、`portfolio/calculations.py:355` | 4 |
| 活跃 opening_balance 唯一性（写法各不相同） | `portfolio/service.py:216-221,312-327,611-615` | 3 |
| `_result` 工厂（`metrics.py:1472` 与 `valuation.py:627` 相似度 0.99） | `materials/metrics.py:1472-1494`、`valuation.py:627-648`、`dividends.py:880-898` | 3 |
| PIT 价格选取（`adjust=="none"` + 三重 order_by + limit 1） | `portfolio/service.py:958`、`portfolio/valuation.py:207-222`、`verification/compare.py:249` | 3 |
| `http_status_rejected`（**逐字相同**） | `materials/us_sec.py:862-867` == `hk_native.py:354-359` | 2 |
| provider 调用管道（throttle→分类→熔断→账本→退避） | 5 个客户端，plumbing 合计 ~529 行；`_throttle` 两份逐字相同（`akshare_eastmoney.py:1355` == `eastmoney_annual.py:978`） | 5 |
| 事件模型映射表 | `evidence/store.py:267-271`、`queries.py:237-241`、`agent/conversations.py:186-189`（`events.py:19` 已有 `EventRow` 可复用） | 3 |
| 终态集合 | `storage/repositories/pipeline_run.py:18` 有权威 `TERMINAL_STATUSES`；重写于 `research/watchlist_refresh.py:35`、`worker.py:583`、`storage/repositories/watchlist.py:109`、`investor/monitoring.py:42`、`discovery.py:43` | 5 |
| `local_owner` 租户字面量 | 146 处 / 41 个模块（唯一定义 `investor/common.py:108`） | 146 |
| 配额分类 | `research/retry_policy.py:35-58` vs `research/ifind_chain.py:174,189-209`（`_QUOTA_MARKERS` 逐字相同） | 2 |
| `portfolio` 两处 `prec = 40` 权重计算 + `_project_decimal`/`_compute_decimal` 四点同构 | `service.py:996-998`、`valuation.py:325-334`、`calculations.py:66-68` | 3 |
| `_write_json`（**行为不一致**：一处 `sort_keys=True`） | `research/ifind_chain.py:856-861` vs `research/kernel.py:116-120` | 2 |
| CLI 内联执行样板（`--now` 短路/断言/输出） | `snapshot.py:199-214`、`screen.py:95-110`、`verify.py:159-172` 等 8 处；`Settings() # type: ignore` 28 处 | 8 / 28 |

### 4.8 [中] 死代码 / 未接线抽象

| 位置 | 规模 | 证据 |
|---|---|---|
| `research_kernel/data_quality.py` | 231 行 | 全库 grep `compare_provider_values`/`DEFAULT_CONFLICT_THRESHOLDS`/`annotate_quality_fields` 零引用；且它是**第二套 provider 冲突阈值**（`pe 0.05/market_cap 0.03/dividend_yield 0.005`）与真正生效的 `config/field_sources.yaml:57 default_tolerance: 0.02` 互不知情 |
| `investor/services.py` | 39 行 | `InvestorQueries`/`InvestorWrites` Protocol，src/ 与 tests/ 零引用 |
| `agent/services.py` | 18 行 | `AgentService` Protocol，零引用 |
| `mobile/src/transport.ts:371-377 createConnectionConfig` | 7 行 | 对 `pairDevice` 的纯转发包装，零引用 |
| `snapshots/eastmoney.py` 的 `TransientBreaker`/`FailureBreaker`/`QuoteBreaker` | 3 个类 | 同一语义三种写法（相似度 0.76/0.43），且 provider 模块互相反向导入（`dividends.py:34` 从 breadth 取、`earnings_events.py:75` 从 eastmoney 取） |
| `config/rule_templates/*.yaml` ↔ `research_kernel/scoring.py` | 双份真理 | 模板注释明文承认出处，但**无交叉校验测试**：`roe_min 0.12`/`0.06≤growth<0.18`/`debt_ratio≤0.55` 各写两遍 |

### 4.9 [中] 前端

- `app.js:14-49` 静态 import 全部 18 个页面模块 + `:822-860 boot()` 无条件调 11 个 `boot*()` → **每页 557KB**（含登录页）。零 `import(` 动态导入、无 GZip。
- `candidates.js` 一个文件承载 3 个 surface（`CANDIDATE_REGIONS:46` 同时声明 list 与 detail），`listState:183` 与 `detailState:510` 两套模块级可变状态同居；详情页独占约 900 行。
- 跨页面 import 造成虚假依赖：`settings.js:9` 从 `health.js` 取 3 行透传的 `localTime`；`app.js:14,17` 从 `candidates.js`/`health.js` 取 `candidatesBucket`/`healthBucket`。
- 共享层已存在（`api.js`/`format.js`/`dom.js`/`ui.js`）但同义实现多份，且 `PROVIDER_LABELS` 已分叉（见 3.5）。
- 浏览器 E2E 只覆盖 5 条路径，`candidates.js`(1581) 与 `health.js`(1025) **零 E2E**；`settings.js`/`AgentScreen.tsx` 的部分逻辑（如 `transport.ts:307-369 watchTask` 的两层 SSE→退避→poll 状态机）零覆盖。

---

## 五、拆分/解耦建议（按优先级分批）

工作量：**S** <1 人日 ｜ **M** 1-3 人日 ｜ **L** >3 人日。风险指"改错会造成行为回归"的可能性，不代表回报。

### 批次 P0：先做这些——它们是后续所有重构的前提（低风险、机械性）

| # | 动作 | 规模 | 工作量 | 风险 | 测试保护 |
|---|---|---|---|---|---|
| P0-1 | **把 peer run-02 黄金数据入仓**（`tests/fixtures/peer_run02/`，约 396KB），四份重复路径常量收进一个模块；**不加 skipif**（否则又是静默跳过） | — | S | 低 | 恢复的不是覆盖率，而是"CI 能否作数"这一前提 |
| P0-2 | **四份"读源码 grep"假测试改为 AST 断言**（`test_peer_service.py:1339-1347`、`test_peer_wp13.py:1244-1251`、`test_material_metrics.py:73-75/836-856`、`test_material_valuation.py:51-53/561-579`） | — | S | 低 | 自身即守卫；解除等价重构的假警报 |
| P0-3 | **抽 `tests/support/peer_world.py`**（约 320 行，取自 `test_peer_service.py:124-412,1147-1174`），解除 11 个测试模块互相 import | — | M | 中（`golden`/`synthetic` 是 fixture，`store_snapshot` 有两个不同签名版本需区分） | `tests/support/__init__.py:1-6` 已定义策略并被 agent 簇验证，可直接复用为模板 |
| P0-4 | **加 import-graph 守卫**（import-linter 或一条 AST 测试），先把现状固化，再逐步收紧 | — | S | 低 | 这是 P1 全部"搬 import"动作的唯一自动回退信号 |
| P0-5 | **删死代码**：`research_kernel/data_quality.py`(231)、`investor/services.py`(39)、`agent/services.py`(18)、`createConnectionConfig`(7) | ~295 行 | S | 低 | `test_scoring_v13_regressions.py` + golden replay 可确认 |
| P0-6 | **重复工具收敛（第一批，无格式争议者）**：UTC 转换器→一份、`_optional_*`→一份、`is_trading_day` 兜底→一份（建议落 `domain/time.py`，`domain/` 是零依赖叶子）、终态集合→引用权威枚举、`_write_json` 统一语义、事件模型映射表→复用 `events.EventRow`、`PROVIDER_LABELS` 合并（**必须逐项人工判定目标文案，不可机械替换**） | ~250-350 行 | S-M | 低 | 各域 API/集成测试；`PROVIDER_LABELS` 需补一条"两页对同一 provider 渲染同一名称"断言 |
| P0-7 | **`storage/models/__init__.py` 承担全量注册**，删掉 `tests/conftest.py:7-34` 与 `alembic/env.py:13-40` 的两份手抄清单（26 个名字） | +30/-52 | S | 低（改错立刻在 `create_all` 暴露，而非静默） | 全部集成测试都依赖 `Base.metadata.create_all` |
| P0-8 | **收敛 5 份 `service()` DI 工厂 + 3 份不一致的 `InvestorError→HTTP` 映射表**为 `api/deps.py`（约 70 行）；顺手修 `candidates.py:121` 的裸 `raise exc`（丢 traceback，应 `raise`） | ~60 行重复 | S | 低 | 45 个 `tests/integration/test_api_*.py` |
| P0-9 | **`industry_peers/jobs.py:82` 的 `SEMANTIC_HASH_SCHEMA` 从 CLI 移到域包**（新建约 40 行 `industry_peers/hashing.py`） | 40 行 | S | 低 | 消除分层倒置，成本最低的分层修复 |
| P0-10 | **`verification/budget.py` 提到中立层**（如 `investment_agent/ifind_budget.py`），verification 退化为纯消费者，跨包环消失 | ~250 行搬迁 | M | 低-中 | `test_verification_budget.py`(155)、`test_ifind_quota.py`、`test_api_data_health.py` |

### 批次 P1：结构性拆分（收益最高的一批）

| # | 目标 | 新模块与预估 LOC | 接缝 | 量 | 风险 | 测试保护 | 结论 |
|---|---|---|---|---|---|---|---|
| P1-1 | **`portfolio/service.py:_project_decimal` 342 行 → 引擎 + 事件处理器 + 状态账本**（**最高优先**，同时切除唯一 DB 依赖） | `projection_state.py`~90（23 字段→dataclass + TraceWriter）、`projection_events.py`~190（`apply_balance/trade/cash/corporate_action` + `HANDLERS` 分发表替代 isinstance 链）、`projection.py`~160（引擎 + 物化 + `build_snapshot`）、`prices.py`~50（提取 PIT 价格查询，与 `portfolio/valuation.py:207` 共用）；`service.py` 1032→~730 | `_project:677-689` 保留为唯一入口与 decimal 上下文持有者；价格改为构造参数注入 `price_loader` | M | 中（unknowns/conflicts 字符串内容与排序、`baseline_key` 语义、价格截止点必须逐字节不变） | `test_portfolio_costs.py`(16) 正对成本语义、`test_portfolio_ledger.py`(15)、`test_portfolio_import.py`(9，含 trace 断言)、`test_portfolio_calculations.py:222` 直接调 `_project`；**建议补一条无 DB 的纯引擎测试** | **建议拆** |
| P1-2 | **`materials/us_sec.py` 1895 → 三分** | `us_sec_concepts.py`~400（概念映射表 183-437 + 静态选择器）、`us_sec_periods.py`~360（期间状态机 460-791）、`us_sec.py` 余~1110 | periods 是纯函数 + 5 个 frozen dataclass，只暴露 `select_period_state`；依赖方向天然单向 | M | 中（`test_material_us_sec.py:67-72` 直接 import 6 个私有符号） | `test_material_us_sec.py`(46)、`test_research_material_contracts.py`(30)、`test_material_collection_job.py`、`test_material_bootstrap_cli.py` | 建议拆（先状态机，再概念表） |
| P1-3 | **`snapshots/health.py` 1709 → 拆包** | `health/models.py`~340、`health/rules.py`~330（`derive_warnings` 的 12 参数收进 `WarningsInput`）、`health/queries.py`~480（18 处裸 SQL 集中）、`health/accumulation.py`~120、`health/__init__.py`~150（`build_data_health` + 全量再导出） | 只有 `queries.py` 持有 `AsyncSession`；`rules.py` 输入输出全是 models 类型，可零 DB 测试 | M | 低（纯读路径、无写事务） | `test_data_health.py:546`、`_health_support.py:271`、`test_provider_call_ledger.py`、`test_close_quote_job.py:44`、`test_web_health.py:148`；`__init__` 保留全部现名则这些导入零修改 | 建议拆 |
| P1-4 | **抽 `providers/transport.py`**（5 个客户端各写一遍的 throttle/熔断/账本/超时/退避管道） | `transport.py`~260（`RetryPolicy` + `ProviderCallRunner.run(operation, call, classify)`，支持异常式与 HTTP 状态式两种分类 + 429 Retry-After 钩子） | 五个客户端构造参数已同名同序（`timeout_seconds/max_attempts/retry_backoff_seconds/sleep/monotonic/circuit/on_call`），调用点几乎零改动 | M | 中（"check 在首次尝试前、record 在最终结果后、skipped_open 不拍熔断"被 3 个测试锁定；baostock 的 session/双锁与 tencent 的无 throttle 必须参数化，不能一刀切） | `test_provider_circuit.py:178-224`、`test_provider_call_ledger.py:124-158` 直接覆盖事件计数与熔断窗口；各 provider 单测只注入 fake backend，不受影响 | 建议拆（~400 行去重 + 语义统一，收益最大） |
| P1-5 | **`snapshots/backfill.py` + `snapshots/eastmoney.py` 的重复续跑状态机统一** | `backfill/plan.py`~330、`backfill/estimate.py`~230、`backfill/parts/*`~465；`eastmoney/crosscheck.py`~190（把唯一的 `snapshots→factors` 依赖收口成一个文件并改为入参注入）、`rows.py`~110、`summary.py`~210、`cash_flow.py`~150、`backfill.py`~130 | 键格式由 `jobs.idempotency_key:388-438` 单点定义，但"如何发现同族 run、如何续跑"两处各写一遍（一处正则 `backfill.py:136`、一处 SQL LIKE `eastmoney.py:876-889`） | M | 中（写路径：每个循环的 `fence_writes` 与 `LeaseLostError` 必须原样；元数据键不可改名） | `test_snapshot_backfill.py:472`（直接 import 11 个内部名 + 经 `ResearchWorker` 跑全量续跑）、`test_funnel_e2e.py`、`test_snapshot_eastmoney.py:503`、`test_provider_circuit.py:32-35` | 建议拆 |
| P1-6 | **`factors/view.py` 1178 → 四分**（god module，17 个导入方） | `factors/snapshot_model.py`~360（纯 dataclass）、`field_resolution.py`~300（纯消解）、`view_db.py`~250（DB 读取）、`ifind_view.py`~140；`view.py` 退化为再导出门面 | 文件内已有 banner 分段（"Database" 694、"iFinD research rows" 1037），同段函数无交叉调用 | M | 中低（风险点是重构后 `registry`/`library`/`valuation/service` 的 import 必须指向 `snapshot_model` 而非 `view_db`） | `test_factor_view_db.py`、`test_factor_library.py`、`test_factor_golden_parity.py`、`test_field_sources.py`（仅改 import 行，数值不变不动 golden） | 建议拆（只做一半时先抽 `snapshot_model.py`，收益/风险比最高） |
| P1-7 | **`mcp_server`：`build_server` 拆领域注册 + 消除 `mcp → api.routes` 的 7 条反向依赖** | `mcp_server/plumbing.py`~80（`ServerContext` 持 settings/session/序列化助手）、`mcp_server/tools/{watchlist,snapshot,bars,candidates,rules,earnings,health,stocks}.py`（各 30-70）；`api/views.py`~300（迁入双方共享的展示/查询函数）；`server.py` 收敛至 ~70 | **保留 `@server.tool()` 装饰器**——改成"name→handler+schema"的声明式表必须手工重写 JSON schema，属纯增负担，明确反对 | M | 低-中（`TOOL_NAMES:54-64` 顺序敏感） | `test_mcp_tools.py`(429，含 tool 目录三方锁定与逐 tool 调用)、`test_api_watchlist.py`/`test_api_candidates.py`/`test_api_market_bars.py`/`test_api_stock_search.py` | 建议拆 |
| P1-8 | **`agent/research.py` 拆枢纽**（36 个内部导入，全仓第一；22 处延迟 import 在压环） | `research_contracts.py`~60、`research_instructions.py`~90（含 `classify`）、`research_context.py`~140（`assemble`）、`research_validation.py`~200（`validate_progress`）、`research_policy.py`~200 | 按现有函数边界切；`registrations()` 迁出时改为声明式装配 | M | 中高（`assemble` 的提示尾串 `"Trusted current context:\n"` 被 `test_agent_research.py:343,708` 硬编码切分；`recovered_evidence:246-289` 的 4 层父任务环链校验行为敏感） | `test_agent_research.py`(24)、`test_agent_progress.py`、`test_peer_agent_tool.py`（覆盖 `check_progress_round`） | 建议拆（分两批：契约+指令+上下文先行） |
| P1-9 | **`investor/publication.py` 918 → 契约 / 规则 / 重检 / 事件 / 编排** | `publication_contracts.py`~120、`publication_rules.py`~240（`check` 变为接收"已解析依赖"的显式参数，可无 DB 单测）、`publication_recheck.py`~230、`publication_events.py`~70；`service.py` 余~330 | `check:180-273` 段是纯字典校验（无 `await`），L274 起才落 I/O——这是最干净的水平接缝 | L | 中（发布是最高风险路径：幂等键绑定、`BEGIN IMMEDIATE`、case revision 守恒） | `test_investor_publication.py`(708)、`tests/support/publication.py`(138)、`test_hk_us_publication_flow.py`；**拆分前先把 `test_investor_publication.py:506,511,520` 依赖的私有 `_dependency_limits`/`_reserve` 升为公开接口** | 建议拆 |
| P1-10 | **`research/ifind_chain.py` 1499 垂直分段 + 与 `snapshot_source.py` 的 canonical 形塑合流** | `ifind_client.py`~150、`canonical_items.py`~200（`_ItemBuilder` 等从 snapshot_source 上移，`_TICKER_PATTERN`/`_validate_ticker` 公开化）、`canonical_bundle.py`~260（`canonicalize_ifind_results` 333 行拆成 3 个 ~90 行 item builder）、`explanation.py`~130、`agent_report.py`~70、`chain.py`~330（307 行编排拆 5 个阶段函数）；`ifind_chain.py` 保留一周期 re-export shim | `canonicalize_ifind_results:497-829` 是纯函数（无 `await`），水平切最安全；`run_ifind_research_chain` 段落边界清晰 | L | 中（canonical 输出被 golden 逐字节断言，须保持 dict 键序与 `field_sources` 文案；`_write_json` 的 `sort_keys` 差异需先决策） | `test_ifind_chain.py`(288)、`test_ifind_contract.py`(353)、`test_ifind_quota.py`(295)、`test_snapshot_source.py`(266)；公共名不变即可零改测试 | 建议拆（顺序：agent_report/ifind_client → explanation → canonical → chain） |
| P1-11 | **`industry_peers`：`calculations.py` 纯/IO 分离 + `jobs._finalize` 与 `batch.submit_offline_batch` 编排去重 + 消除对 CLI 的反向依赖** | `metric_math.py`（`:140-1188`，~1050 行纯段）、`metric_records.py`（`:1190-1377`，~190 行 DB 段）；`batch.finalize_batch(...)` 新公开函数吸收 `jobs.py:1076-1168` 的重复编排（约省 90 行 + 删掉 4 处跨模块私有访问）；`jobs.py`→`collect_artifacts.py`~160 + `collect_admission.py`~450 | `:1188-1190` 段注释即现成缝隙；`batch` 的 4 个私有函数被 jobs 跨模块调用（`:1079/1114/1122/1147`），改名为公开即接缝 | M | 中（准入决策矩阵 6 种 outcome；`_finalize` 的 lease/事务边界不能动） | `test_peer_calculations.py:375-723` 的 **13 个无库纯测试**是黄金护栏；`test_peer_jobs.py`(18) 覆盖准入/代际/续跑/冷却 | 建议拆（四个大文件里收益/风险比最好的一个） |
| P1-12 | **`rules/zone_detection.py` 1209 → eval / store / notify** | `rules/zone_eval.py`~320（`evaluate_rule_zones` 已是纯函数）、`rules/zone_store.py`~300、`rules/zone_notify.py`~90；原文件留扫描/读取 ~450 | `evaluate_rule_zones` 是 `record_evaluations` 的唯一输入；`notify_zone_entry` 只被 `:669` 调用 | M | 中低（`zone_events/zone_summaries` 被 3 个模块依赖，保留原路径可避免大面积改 import） | `test_zone_detection.py`(21)；需把 `:910` 的 `zd._locked_state` 改指向 `zone_store` | 部分拆（至少 eval + notify） |
| P1-13 | **`evidence/store.py:save_evidence` 304 行 → 回执 + 分派 + 5 个来源适配器** | `evidence/adapters/{snapshot,peer,event,material,user_report}.py`（各 40-80 行，暴露 `materialize_expected(session, record)`）；共享常量 `EVIDENCE_IDENTITY_FIELDS`(12)/`EVIDENCE_FULL_FIELDS`(14) 放 `contracts.py`，消掉 79 行重复清单；`store.py` 保留 ~80 行 `save_evidence` | 分派表显式化；`else` 保持"未支持来源一律拒绝" | M | 中（"verified 只能来自再推导"是不变量，须保留逐字段严格相等；peer/material 适配器的函数内 import 位置需保持） | `test_evidence_services.py`、`test_document_native.py`、`test_disclosure_originals.py`、`test_peer_agent_tool.py` | 建议拆（先抽字段常量 + user_report/event 两条无外部依赖分支） |
| P1-14 | **`scheduler/runner.py` 870 → cron 层 + 声明式 JobSpec 表 + 追赶循环** | `scheduler/cron.py`~120（`parse_cron` 公开化，供 `settings/registry.py:102` 共用）、`scheduler/registry.py`~220（`JobSpec` 表）、`scheduler/catch_up.py`~150（替代 6 个手写 `_startup_*_check`）；`runner.py` 保留 11 个 `run_*` + 主循环 ~380 | 触发器在注册（613-705）与追赶（510-534/550-554/792/802/819-823）两处各自重新构造，`_parse_cron` 被调用 13 次——收敛为一处 | M | 中（注册顺序、job id、misfire/coalesce 参数必须不变） | `test_scheduler_runner.py`(291)、`test_scheduler_reschedule.py`(247)、`test_scheduler_crontab_weekdays.py`、`test_snapshot_schedule.py:174-301` | 建议拆（新增 job 从"改 5 处"变为"加 1 行 + 1 个 run_*"） |
| P1-15 | **前端：共享层收敛 + 按需加载 + 4 个页面文件拆分** | `methods.js:23-40` 回归 `api.js`（净删 ~18 行）；`settings_catalog.js`~230（settings.js 1414→~1180）；`decimal.js`~60（承载 BigInt 精确百分比并区分"精确 percent"与"SVG 比例 pct"）；`candidates.js` 1581→拆 `_shared`~140/`_list`~300/`_detail`~450（规则段并入既有 `rule_review.js`）；`app.js` 868→~120（迁出 `dashboard.js`/`runs.js`/`stock_detail.js`）；`health.js` 拆出 `health_calendar.js`~210；动态 `import()` 按需加载 | 文件内已有分段注释即接缝；**不要按 render/state/api 三层切**（会横切接缝并要求导出内部状态） | M | 中（`CANDIDATE_REGIONS:46` 拆散后必须与 `app.js:825-827` 同 commit 同步，否则区域被兜底渲染成原始 JSON；`test_web_candidates.py:186-188` 的 `score`/`strength` 禁用清单需扩展到新文件，否则护栏**静默失效**） | `test_web_client_contract.py`（但其中多条是源码子串断言，须同 commit 改写）、`test_web_candidates.py`、`test_web_health.py`、`test_web_settings.py`、`tests/browser/*.mjs`（13 个 Playwright 脚本） | 建议拆：先补 candidates/health 页最小 E2E，再拆；**不要引入 bundler**（裸 ESM 已足够，且 40+ 处断言按文件路径直读源码，产物化需全部重指向、收益为零成本为正） |
| P1-16 | **`portfolio/service.py:PortfolioService` 935 行 → 门面 + 写侧 + 读侧 + 校验** | `ledger_writes.py`~340（`append` 80 + `apply_import` 134 + `apply_correction` 113）、`ledger_reads.py`~70、`event_validation.py`~90（**合并 4 份 CNY 作用域校验、3 份 opening_balance 唯一性、2 份 `fact_refs` 内联副本**）；`PortfolioService` 保留 ~80 行门面 | `__init__(session, owner)` 不变；**`_reserve`/`_account`/`_project` 已被外部 3 个模块直调，必须保留同名委托** | M-L | 中（`storage_conflicts` 装饰器与 SQLite `BEGIN`/预留顺序是并发正确性所在，`_reserve:104-105` 的"DML 先于 SELECT"不能重排） | `test_portfolio_ledger.py`(15，含并发与 owner 隔离)、`test_portfolio_import.py`(9，含幂等重放与 revision CAS)、`test_holding_service.py` | 部分拆——**优先只做 `prices.py` + `event_validation.py` 两个纯抽取**（低风险、直接消除 4 条高风险领域规则的多副本），可拿到约 80% 收益 |
| P1-17 | **`portfolio/plans.py:check` 158 行 → 抽出 `verify_action`** | `plan_actions.py`~90（逐 action 语义 L257-313 + `constraint_improvement`）；`check` 收敛至 ~90（门禁清单 L189-235 留原位） | `check(candidate, semantic, *, as_of, task_id)` 签名与返回不变 | S-M | 中（`errors` 键命名 `options.{id}.actions.{id}` 是 API 契约；L312 捕获的异常类型集合含 `StopIteration`（来自 L276 `next(...)`），漏掉会穿透为 500） | `test_portfolio_plans.py`(517，本域最大测试文件)、`test_portfolio_purposes.py`(440) | 部分拆（只抽 `verify_action`；门禁清单是"一行一门禁"的扁平结构，管道化收益有限） |
| P1-18 | **`valuation/service.py:reverse_dcf` 190 行 → gate 管道** | `valuation/input_gates.py`~120（`evidence_gates`/`assumption_gates`/`qualification_gates`/`period_gates`，各返回 `(errors, status_suggestion, quality)`）；`reverse_dcf` 退化为 ~80 行编排 | `ValuationService.__init__` 的 `qualification` 注入参数已是现成扩展点；`percentile` 可复用同批 gate | M | 中（**状态降级顺序是行为契约**——`:192/222/278/285` 都依赖"不覆盖更严重状态"；`details` 键变化会让已落库 receipt 的 `input_hash` 不可复现） | `test_valuation_calculations.py`(18，17 处直调 `reverse_dcf`)、`test_valuation_coverage.py`、`test_fcfe_sources.py` | **部分拆**——只抽 `period_gates` + `qualification_gates`（现 L194-301，占 108 行且分支最密） |
| P1-19 | **`settings/` 三小步** | 三个逐字相同的调度模型（`registry.py:121-154`）合并为一个 `CronSchedule`（省 ~26 行）；`loader.py:233-319` 的日历定位整体移到 `settings/calendar_paths.py`~90 | 类定义 / "部署资源定位"函数群 / 公开私有可见性 | S | 低（**手写 dotenv 解析 `loader.py:258-283` 不要顺手动**） | `test_settings_registry.py`(131，含 specs 唯一性/ENV 镜像/app.yaml 对齐)、`test_settings_consumers.py`(235)、`test_api_settings.py`(335)、`test_calendar_resolution.py` | 建议拆 |
| P1-20 | **`storage/repositories/pipeline_run.py` 剥离 agent 队列策略**（消除 storage↔agent 硬环） | 策略函数加进 `agent/operations.py`（`queue_class_for`/`admit_agent_run`/`select_agent_run`，~40 行），`pipeline_run.py:171-178/226-236` 改为调用；`intervals.py` 增 `close_open_span_row`~25 供 `universe.py:173-198` 与 `industry.py:363-395` 共用；`universe.members_between/members_as_of` 合并（省 ~45 行，**必须保留两种排序**） | `admission`/`choose` 已是 agent 模块的公开函数；`close_open_span_row` 只操作 ORM 行的 `valid_from/valid_to` | S-M | 中高（`pipeline_run` fan-in 41；`claim_next` 的 `scalar_subquery()` 条件更新与 `recover_stale` 的 fencing 语义极敏感，**只移策略、绝不动 SQL 与租约**） | `test_pipeline_run_repo.py`、`test_pipeline_run_lifecycle.py`、`test_job_dispatch.py`、`test_agent_operations.py`、`test_pit_snapshot_repos.py`、`test_backfill_history.py` | 部分拆（策略外移 + 去重复做；`rebuild_history/rebuild_timelines` 本体不动） |
| P1-21 | **`candidates/lifecycle.py` 1001 → 抽出 screen intake** | `candidates/screen_intake.py`~210（`:169,515-714`，正对现 banner `:511`）；`lifecycle.py` 留 ~790 状态机 | 现有 banner 即切分线；**必须 re-export**（11 个外部导入方） | S | 低 | `test_candidates_lifecycle.py`(553/16)、`test_candidates_screen_job.py`、`test_candidates_promote.py` | 部分拆（只抽 screen intake） |
| P1-22 | **`verification/job.py` 只拆两个真接缝** | `verification/settle.py`~180（`settle_item` 已是独立签名）、`verification/report.py`~70 | `settle_item` 只依赖 `session/answers/registry/now/today/run_id` | M | 低 | `test_verification_job.py`(475，端到端覆盖 fold/batch/budget/quota/retry/no-data 七条路径) | 部分拆（`plan_units`/`fetch_unit`/`execute_verification` 属同一作业生命周期，按长度拆是纯搬运，不做） |
| P1-23 | **CLI：抽 `cli/runtime.py` + `cli/parsing.py`** | `runtime.py`~120（`bootstrap_settings`/`with_db`/`worker_identity`/`run_inline`）、`parsing.py`~80、`commands/snapshot_backfill.py`~230 | `snapshot.py:199-214`、`screen.py:95-110`、`verify.py:159-172` 的公共尾段逐行近似 | S-M | 低（`--now` 短路与输出文案有断言，搬迁须逐字保留） | `test_cli_snapshot.py`(288)、`test_cli_settings.py`、`test_cli_rules.py`、`test_cli_research.py`、`test_mcp_tools.py:114,416-425` | 建议拆（同一段业务语义实测 8 份） |
| P1-24 | **`verification` 两处"重造"收口** | 无新模块：`job.py:300-308` 改用 `snapshots.financial._number` 的公开化版本；`compare.py:255-274` 改为调 `FinancialSnapshotRepository.total_shares_as_of`；`compare.py:232` 改调 `snapshots/disclosure.statutory_deadline` | `total_shares_as_of` 在 src 内已无调用者——正是被核验路径绕过的证据 | S | 低-中（compare 的 SQL 语义与仓库方法不同，**先用测试钉住现有 verdict 输出再改**） | `test_verification_job.py:179-290` | 建议改 |
| P1-25 | **`portal`/证据重复收敛（第二批）** | 财年跨度常量三处归一（`contracts.py:598`/`metrics.py:120-121`/`us_sec.py:119`）、币种集合（`dividends.py:87` 复用 `contracts.py:144`）、`http_status_rejected`/`json_object`/十进制解析合并入 `materials/parsing.py`~120、`PEER_PERIOD_BASIS` 单点化、`_required_fact_keys` 保存侧与校验侧共用、`_latest_version` 下沉 repository、`_artifacts_root`/cohort-ref 构造归一 | 见"四、4.7" | S | 中（**`_decimal_text`(us_sec) 与 `_decimal_string`(hk_native) 输出格式不同，且 hk_native 的落库值被测试逐字断言 `test_material_hk_native.py:83-105`——合并前必须先固定输出契约并补测试**） | us_sec 46、hk_native 25、contracts 30、offline_import 11 | 建议拆（分两步：先合无格式争议者） |
| P1-26 | **测试结构重整（第二批）** | `tests/support/peer_golden.py`~80（集中 `0.3253/0.0739/0.5792/94.44/(18,18)/(15,18)` 与 `GOLDEN` 表）；8 个 ≥1000 行测试文件按行为族拆分（peer 簇拆前必先落 P0-3）；顶层 38 个 `tests/*.py` 归位 unit/integration | 现 docstring 里的分节注释即边界 | M | 低-中（路径计算与 conftest 作用域会变；`tests/browser/method_fixture_server.py:13` 直接 `from tests import conftest`） | 自身即测试 | 建议拆（顺序：P0-1 → P0-3 → P0-2 → P1-26 → 生产重构） |

### 批次 P2：大工程 / 需要先决策

| # | 议题 | 现状与风险 | 建议 |
|---|---|---|---|
| P2-1 | **仓储层是否应成为唯一 DB 网关** | 112 个非 storage 模块直接构造 ORM（338 处）；`storage/repositories/base.py` 只有 15 行/一个 `get()`，分页在 12 个 repository 各写一遍（另有 26 处分页参数、5 种上界取值）。这是全仓最贵的重构：**172/297 个测试文件直接引用 ORM 模型、102 个直接引用 repository**，阻力来自测试自身 | **不要一刀切**。分两步：先给 `repositories/query.py` 加显式小工具（`paginate(stmt, limit, offset)`）而不是把 `Repository` 扩成胖泛型基类（那会把 96 个导入者绑定到更易腐化的接口）；再按域逐步把读路径下沉为 `*/queries.py` |
| P2-2 | **API 读路径下沉 + 共享视图层** | 65 处 handler 内 ORM、78% 路由代码非管道、MCP 反向 import 路由。`candidates.py` 656 / `watchlist.py` 513 / `research.py` 472 是收益点；**明确拒绝**给 `stocks`/`dashboard`/`calendar` 造空转的 Service 类（它们是纯只读投影，应走 `*/queries.py`） | 4 个域补服务层（`candidates`/`watchlist`/`research`/`industry_peers`）：L 工作量、中高风险、保护网是 45 个 API 集成测试 + `api_client` fixture（真 app + 真磁盘 SQLite，重组内部不会误绿） |
| P2-3 | **测试 fixture 事务化（提速 + 可并行）** | 每个测试重建 118 张表；41 个文件各自 `create_all`；`peer_db` 类 fixture 靠"先初始化全局引擎再 `del`"的隐式约定。收益明确但影响 129 个文件 | **放在生产重构之后**——避免把"生产语义不确定性"与"测试基础设施不确定性"叠在同一次提交里 |
| P2-4 | **`config.py` 收窄 / api↔web 环** | `config` fan-in 69 但反向依赖 `investor`/`providers`；进程级 `_weak_key_warned` 造成测试顺序依赖；`api/app.py:180 → web/routes.py:29 → api/auth.py` 形成包级环，且 `api/auth.py:34` 的 `_default_throttle` 是进程级可变限流器（测试间计数泄漏） | 只做 4 处定向改动（`:109`/`:344`/`:378`/`:18-19`），不动 `Settings` 字段布局；`security/session_auth.py`~110 下沉传输无关部分解开环，顺带把 throttle 挂 `app.state` |
| P2-5 | **`rule_templates/*.yaml` 与 `research_kernel` 的双份真理** | 同一策略原型编码两次（Python 常量 + 模板参数），只有注释互指，无交叉校验 | **不要"数据驱动化" kernel**（那触发 scoring 版本升级 + 13 份 golden 重生成，收益是"可配置"、风险是打散算法可读性）。正确做法是**加一条一致性测试**比对模板参数与 archetype 常量 |
| P2-6 | **`local_owner` 租户字面量 146 处 / 41 模块** | 单租户系统把 owner 当字面量而非请求作用域对象；storage 层 22 个 repository 完全没有租户条件 | 现在是**假设成立**的（单租户），仅在多租户化时才有成本。记录在案，不做预防性改造 |

---

## 六、不建议改动清单（大而内聚）

明确写下这些，是为了避免后续有人"按行数"去拆它们——按行数拆是净损失。

| 对象 | 规模 | 保留理由 |
|---|---|---|
| `materials/contracts.py` | 916 | 单一冻结契约（19 个枚举/数据类 + 对称的 `fact_to_payload`/`fact_from_payload` + 8 条校验规则 + 内容哈希）。体量来自"键集 × 三个方向（写/读/校验）"的机械展开，只有一个变更理由。按序列化/校验切开只会让同一键集分布到两个文件 |
| `materials/metrics.py` 的 6 个指标函数 | ~690 | 共享同一套机制（`_FactIndex.one` 绝不在冲突事实上取值、`_pick`、`_currency_problem`、`_comparability_notes`、`_result`）。每个函数里 5-8 个早返回分支是本模块的**核心产品规则**（"缺失不是零，每条拒绝都要点名理由"），不是结构问题。按指标拆成 6 个文件会产生 6 份 import 同一组选择器的样板，耦合不降反升。可接受的只有词汇下沉 |
| `materials/dividends.py` / `valuation.py` / `hk_native.py` | 911 / 648 / 866 | 单一领域概念；长函数是"每一种缺口都有独立语义"的要求（时段不可判定、覆盖未声明、特殊股息、ADS 不可合并、汇率未声明）；`annual_pe` 每一步的返回文本都被 24 个测试逐条断言；`hk_native` 的逐发行人数据留在适配器内是**正确位置**（数据随来源走），`parse` 的九条结构检查抽成函数列表只提高可读性、不改变依赖方向 |
| `storage/models/` | 1,935 / 62 类 | **本仓分层最好的部分**：对外零 import、无 ≥60 行函数、每文件 ≤250 行。`models/reference.py` fan-in 41 / `analysis.py` 33 的原因是它们持有外键枢纽与运行记录中心——拆开只会迫使所有调用方跨多模块拼装 |
| `settings/registry.py` | 682 | 主体是 248 行声明式 `SPECS` 表；全文件仅 11 个函数、最长 46 行。拆一张按 key 索引的配置表只会让"新增一个设置项"从改 1 处变成改 3 处。该修的是它对 `providers.INDEX_CODES` 的依赖，不是拆文件 |
| `clock.py` / `providers/errors.py` / `providers/circuit.py` | 24 / 79 / 249 | fan-in 高**恰恰是正确复用**：纯函数 / 纯异常数据 / 已被 21 个 src 复用的唯一状态机。为"降 fan-in"引入分层是负收益 |
| `storage/repositories/base.py` | 15 | 只有 `model` 声明与 `get()`，无隐式 session、无反射、类型干净。**不要扩成胖泛型基类**——那会把 96 个导入者绑定到更宽更易腐化的接口 |
| `storage/database.py` 的全局引擎 | ~210 | docstring 记录了真实故障模式（aiosqlite 连接绑定 loop，未 dispose 会在 worker 线程抛 "Event loop is closed"）；`session_scope()` 是集中式管理，调用方从不自建 engine（`create_async_engine` 全仓仅出现于 `:49`）。改成 DI 注入要牵动 71 个导入者并重写 `test_engine_lifecycle.py` 的 7 个并发语义测试。只做"收窄公开面"（等价切分） |
| `scheduler/snapshot_jobs.py` / `calendar_refresh.py` / `trading_calendar.py` | 396 / 267 / 161 | 纯决策层与零内部依赖的纯函数；`snapshot_payloads` 是"日期→入队负载"的纯函数且被 runner 与 CLI 复用。它是 `runner.py` 拆分的**样板**，不是被拆对象 |
| `scheduler` 的 lease/heartbeat/fencing、`pipeline_run` 的租约机制 | — | 并发正确性代码。迁移时**整体搬运、逐字不改**；"顺手重构"会破坏竞态语义，而 `test_research_worker_robustness.py`(660) 的价值正建立在这些精确行为上 |
| `rules/screen.py` | 590 | 全仓最干净的模块：0 DB、0 LLM、25 个纯函数，一套求值内核同时服务 screen 与 zone detection。是"统一而非拆分"的范本 |
| `factors/library.py` | 726 | 22/28 个函数是"一个 Factor 一份声明"（含中文口径说明与 inputs），本质是目录数据。规模来自声明密度而非逻辑纠缠 |
| `research_kernel/` 的算法与 golden 语料 | 896 + 26 个 golden | 被定义为冻结的离线回归 kernel；golden manifest 明文"永不为了通过测试而更新"。**任何结构重构必须做到输出逐字节不变**；阈值/权重语义变更必须与结构重构严格分离 |
| `tests/fixtures/research_kernel/`、`tests/conftest.py`、`tests/support/*` | 93 / 129 / 1,466 | 有文档纪律的只读 oracle；conftest 的 3 个 autouse 守卫体量小职责单一（按区域拆会把"离线守卫"复制多份）；`tests/support/*` 已是正确形状，应作为 peer 簇改造的**模板**而非被重构对象 |
| `web/static/app.css` | 1704 | 单文件是刻意的：无构建链、无 `@import` 请求瀑布，且 `:1590` 注释记录覆盖关系依赖物理顺序。按页面拆会引入瀑布并破坏覆盖顺序。只做一次分节去重 |
| `mobile/src/transport.ts` | 377 | 内部分层已清晰（URL 校验/错误分类/请求管线/公共 API/SSE 纯解析/流式循环/编排），拆成 7 个文件会碎片化并削弱"传输语义集中一处"的优势。只删 `createConnectionConfig` 死代码 + 补 `watchTask` 测试 |
| `investor/foundations.py` / `publication_numbers.py` / `notifications.py` / `discovery.py` | 619 / 207 / 268 / 387 | 同构的"校验+单聚合持久化+receipt"闭环（共享 session 与 revision 语义）；外发投递状态机是不可分割的协议实现；discovery 的策略/机制分离**已经做过**（`DiscoveryPolicy`）。继续拆会把一次事务切成跨模块协作 |
| `agent/runtime.py` / `budget.py` / `operations.py` | 368 / 301 / 283 | 围绕"预算确定性 + HTTP 审计"单一不变量；18 个关键字参数是刻意的"可信旋钮清单"；存在的问题是"接缝"（与 `check_boundary` 互调、私访 `TaskService._row`）而非文件过大 |
| `evidence/documents.py` / `disclosures.py` / `contracts.py` / `queries` 的大函数 | 724 / 588 / 139 | 前者是"确定性交叉校验 + 原文字节证据"的唯一实现；后者是 CNInfo 有界读的单一入口；`contracts.py` 是真正窄而稳的内核（28 个 src 依赖）。风险高于收益 |
| `mcp_server/readonly.py`、`cli/main.py`、`settings/store.py`、`settings/crypto.py`、`verification/queue.py`、`storage/repositories/intervals.py` | 各 <350 | "只读"安全属性的最小闭环 / 唯一装配点 / 单一事务单元 / 语义定义处（`TRIGGER_PRIORITY` + assert 与 `VERIFICATION_TRIGGERS` 同步是"单一来源+守卫"的正面例子）/ 已被两个仓库与测试共用的泛型纯函数层——**它就是"把业务算法移出仓库"的答案** |

---

## 七、建议路线图

排序原则：**先建立"让后续编辑安全"的前提，再做收益无争议的整理，最后做会动摇测试基础设施的改造。** 不把两层不确定性叠在同一次提交里。

1. **P0（1-2 周，低风险）**：P0-1 黄金数据入仓 → P0-4 import-graph 守卫 → P0-2 假测试改 AST 断言 → P0-3 抽 `peer_world` → P0-5 删死代码 → P0-6/P0-7 重复与注册收敛 → P0-8/P0-9/P0-10 三个分层小修复。
   理由：P0-1 恢复的不是覆盖率而是"CI 能否作数"；P0-4 是 P1 全部"搬 import"动作的唯一回退信号；P0-2/P0-3 解除的是等价重构的假警报与测试互相 import 的锁定。

2. **P1 第一批（收益/风险比最高的 6 项）**：P1-1 `_project_decimal`（全仓最大函数，且顺带切除 DB 耦合）→ P1-2 `us_sec.py` → P1-3 `health.py` → P1-4 `providers/transport.py` → P1-11 `industry_peers`（含消除反向依赖）→ P1-6 `factors/view.py`（god module）。

3. **P1 第二批**：P1-7 `mcp_server` + 共享视图层 → P1-8 `agent/research.py` → P1-12 zone_detection → P1-13 evidence/store → P1-14 scheduler → P1-20 storage 策略外移（含 3 处分层反转收口）。

4. **P1 第三批（高风险）**：P1-9 `investor/publication.py` → P1-10 `ifind_chain.py` → P1-16/17/18 portfolio & valuation 巨型函数族 → P1-15 前端（先补 E2E 再拆）→ P1-19/21/22/23/24/25/26 收尾。

5. **P2（需先决策）**：仓储网关边界、API 读路径下沉、测试 fixture 事务化。**P2-3 放在所有生产重构之后。**

贯穿原则（本项目已有明确约定，继续遵守）：
- **结构重构与数值/阈值变更严格分离**：golden（`tests/fixtures/research_kernel/`）永不为通过测试而更新；结构重构必须做到输出逐字节不变，否则走"版本号 bump + 重生成 + 书面 diff 说明"。
- **不静默改 golden**；每个工作包独立提交；提交前跑三门禁。
- 拆分时**保留 re-export 门面**一至两个提交周期（保护 8-17 个导入方），但**不要长期保留**——长期 facade 就是纯搬运。

---

## 八、方法与局限

**方法**：12 个分区评审各自由一个独立 reviewer 逐文件通读（`portfolio/service.py`、`snapshots/health.py`、`research/ifind_chain.py`、`materials/us_sec.py`、`providers/akshare_eastmoney.py`、`mcp_server/server.py`、`scheduler/runner.py`、`agent/research.py`、`investor/common.py`、`factors/view.py`、最大的 8 个测试文件等均全文阅读）+ 一次全仓 AST 普查（god function/class、圈复杂度、10 行窗口跨文件重复、Tarjan SCC、扇入扇出）。所有非平凡结论要求引用实际代码位置，禁止基于文件名推测。

**局限**（请在据此排期时考虑）：

1. **未运行测试**。本报告未执行 pytest（全量约 3,100 例，超出只读评审的时间预算）。因此"测试保护"判断来自**阅读测试文件**（导入关系、断言形式、是否经公共缝驱动），不等于"重构后测试必绿"。
2. **未实际验证拆分可行性**。所有工作量（S/M/L）与预测风险是评审判断，不是试做结果。P1-1（`_project_decimal`）与 P1-16（`PortfolioService`）涉及账本正确性，实施前建议先补一条**无 DB 的纯投影测试**。
3. **行号对应 `63a2b4d`**。任何后续提交都会使行号漂移；引用时应以符号名为主、行号为辅。
4. **圈复杂度为近似值**（`if/for/while/except/with/boolop/comprehension` 计数，不同评审者是否展开 `BoolOp` 操作数会造成约 ±10% 差异）。排名一致，绝对值仅供相对比较。
5. **"重复 LOC 可删量"是区间估计**（约 800-1,200 行），因为部分重复（如 5 个 provider 调用管道）合并时需要参数化差异（baostock 的 session/双锁、tencent 的无 throttle、eastmoney_annual 的 429 分支），实际净删会低于算术差。
6. **未覆盖**：`.github/workflows/ci.yml` 的门禁配置（仅从测试评审侧读到 `pytest -q -m "not network"` 与 15 分钟 timeout）、`tools/` 下的 16 个脚本、`alembic/` 迁移、`Dockerfile`/`docker-compose*`、`research/`（被 gitignore 的本地数据）。前端评审覆盖了 `web/static`、`web/templates`、`mobile/src`，未评估 `mobile/src-tauri` 的 Rust/Android 构建链。
7. **正面结论同样重要**：`storage/models/` 的干净分层、`rules/screen.py` 的纯求值内核、`clock.py` 的统一入口、`clock`/`errors`/`contracts` 类的高扇入复用、`evidence/contracts.py` 的窄接口、前端已有 `api.js`/`format.js`/`dom.js`/`ui.js` 共享层、`tests/support/*` 的正确形状、agent 簇已完成的支持层抽取、规则层的 golden 门禁——这些是本仓的**结构性资产**，报告中标注"不建议改动"的部分应与待办项同等对待。
