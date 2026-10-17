# 港股与美股扩展实施计划 · 总计划

> 2026-10-17 HK2-06 准备阶段：三个 GPT-6 Luna 子任务完成来源范围核对、隔离 probe、离线验收工具及分来源三日计划，主代理集成验收见 [36](36-hk2-06-preparation-acceptance.md)，拆分见 [33](33-hk2-06-preparation-execution.md)，计划见 [35](35-hk2-06-observation-plan.md)。两源真实观测均为 **0/3**，许可仍 unknown、技术仍 not_accepted；在线刷新关闭，生产库及主 checkout 的 P4 未提交改动未操作。

> 2026-10-17：HK2-05 默认关闭的来源策略与可恢复采集代码已完成；本轮独立工作树验收见 [32](32-hk2-05-offline-acceptance.md)，子任务拆分见 [31](31-hk2-05-execution.md)。HK2-06 真实多日来源验收仍待开展，在线刷新保持关闭；本轮不操作生产库，不包含主 checkout 的 P4 未提交改动。

> 2026-10-16 后续阶段：HK2-08 M1 本地发布、配套备份恢复和恢复后的浏览器验收已通过，见 [29](29-hk2-08-m1-acceptance.md)。在线刷新保持关闭，生产环境尚未迁移、导入或部署。本总计划的首期波次、时间窗口及历史遗留表保留原日期；当前剩余工作以 [15](15-next-phase.md) 为准。

> 合入本地 main 的联合验收见 [30](30-main-integration-acceptance.md)；之前各阶段文档保留当时的分支和发布状态。

> 依据：[`research/market-data-validation/hk-us-expansion-design.md`](../../../../research/market-data-validation/hk-us-expansion-design.md)（v0.1，2026-10-12，供评审草案）。
> 本目录把该草案拆成 14 个**可独立验证**的子任务（01～12 为核心，13～14 为可选尾部），每个子任务交给一个独立 sub-agent 执行。
> 执行前先读本文件与 [`00-conventions.md`](00-conventions.md)，再读自己的子计划。

## 1. 首期交付边界（与设计一致）

| 做 | 不做 |
| --- | --- |
| 受控名单（中电 00002、汇丰 00005、腾讯 00700、AAPL、COST、JPM、XOM、TSM）的身份、来源绑定与不可变资料 | 全市场筛选、港美统一评分、行业排行、历史估值分位、回测、实时行情、多币种组合账本 |
| 港美年度财报资料采集（港股原币端点 + SEC CompanyFacts/Submissions）、字段契约与版本 | 严格历史时点查询、任意历史日期重建、全量不可依赖公告发现 |
| 确定性财务指标（同比、CAGR、平均权益 ROE、资产负债率、CFO/利润、简化 FCF）与适用性规则 | TTM、逆向 DCF、FCFE、PB/股息率默认开启 |
| 财务上下文只读接口、显式刷新接口、财务页面展示、研究档案与发布校验接入 | 把 A 股评分与阈值套到港美证券 |
| 条件启用的年度 PE、已核对分红事件（默认关闭，缺口可见） | 供应商付费权限、最新年报联网补验（见 §6 遗留） |

**离线优先**：首期所有验收都在禁网条件下成立。在线刷新默认关闭（能力位 `online_refresh=false`），只有联网验收通过后才打开；这一条与设计 §尚未验证能力 一致。

## 2. 波次与依赖

同一波次内的子任务**互不触碰同一文件**，各在自己的 worktree 内执行；波次结束后由控制 agent 合并到集成分支并跑一次全量门禁。

| 波次 | 子任务 | 依赖 |
| --- | --- | --- |
| W1 | 01 身份与市场注册表 ‖ 02 资料契约·模型·迁移·仓储 | 无 |
| W2 | 03 港股原币适配器 ‖ 04 美股 SEC 适配器 ‖ 05 不可变资料存储·原始制品·离线导入 | 02（W1）；05 只依赖 02（适配器经参数注入） |
| W3 | 06 单证券资料采集任务 ‖ 07 资料→证据适配分支 ‖ 08 确定性财务指标 | 06 需 03/04/05；07 只依赖 02（不 import store）；08 只依赖 02 |
| W4 | 09 财务上下文读模型·只读 API·刷新 API ‖ 10 研究档案与发布校验接入 | 09 需 06/07/08；10 需 07（+05 供测试造数） |
| W5 | 11 财务页面展示 ‖ 12 验收矩阵与文档收口 | 09（11）、全部（12） |
| W6（可选尾部） | 13 年度 PE 条件启用 ‖ 14 分红事件展示 | 09；剩余 ≥45 分钟才启动 |

依赖图（→ 表示"需要先合并"）：

```
01 ─┐
    ├─→ 03 ─┬─→ 06 ─┐
02 ─┘       ├─→ 07 ─┼─→ 09 ─┬─→ 11
    └─→ 05 ─┘       │        └─→ 13, 14
            08 ─────┘  10 ───┴─→ 12
```

## 3. 冻结接口（跨任务不得修改）

以下接口在 W1 建立后即为**冻结契约**，后续任务只能使用、不能改签名。确需扩展时由控制 agent 在集成分支上改，并同步本文件。

| 契约 | 位置 | 冻结内容 |
| --- | --- | --- |
| 受控身份 | `src/investment_agent/domain/security.py` | `SecurityMarket` / `SecurityType` / `ControlledIdentity` / `load_pilot_identities` / `normalize_ticker` / `source_ticker` |
| 资料契约 | `src/investment_agent/materials/contracts.py` | `DatasetKind` / `SubjectKind` / `FactPeriod` / `FactSemantics` / `FactValue` / `FactProvenance` / `FactRecord` / `DatasetDraft` / `RawPayload` / `ParseResult` / `MaterialAdapter` / `SCHEMA_VERSION` / `fact_to_payload` / `fact_from_payload` / `payload_facts` |
| 资料存储 | `src/investment_agent/materials/store.py` | `MaterialStore.save(draft) -> ObjectRef`（幂等：内容相同返回既有版本）、`latest()`、`history()` |
| 离线导入 | `src/investment_agent/materials/offline_import.py` | `import_offline_evidence(..., adapters: Mapping[str, MaterialAdapter])`（适配器由参数注入，模块不 import 具体适配器） |
| 证据适配 | `src/investment_agent/materials/evidence.py` | `material_evidence(dataset, *, fields, subject_ref) -> tuple[EvidenceRecord, ...]` |
| 指标 | `src/investment_agent/materials/metrics.py` | `compute_metrics(dataset, *, profile) -> MetricSet` |
| 读模型 | `src/investment_agent/materials/context.py` | `financial_context(session, listing_id) -> FinancialContextView` |
| 任务 | `src/investment_agent/materials/jobs.py` | `MATERIAL_COLLECTION` kind、`MaterialCollectionPayload`、`material_idempotency_key` |

**来源特有字段一律进 `DatasetDraft.payload`（版本化 JSON），不进契约。** 契约只承载跨来源通用的语义与时间字段。

## 4. 文件所有权矩阵

一个文件在同一波次只能有一个 owner。矩阵外文件一律不改；确需改动时先报告控制 agent。

| 文件 | owner |
| --- | --- |
| `src/investment_agent/domain/security.py`、`config/research_material/pilot_list.yaml`、`src/investment_agent/storage/repositories/listing.py`、`tests/unit/_hk_us_support.py`、`tests/unit/test_security_identity.py` | 01 |
| `src/investment_agent/materials/__init__.py`、`materials/contracts.py`、`storage/models/research_material.py`、`storage/models/__init__.py`、`alembic/versions/046_*.py`、`alembic/env.py`、`storage/repositories/research_material.py`、`tests/conftest.py`、`tests/unit/test_research_material_contracts.py`、`tests/unit/test_research_material_repo.py`、`tests/integration/test_migration_046.py` | 02 |
| `materials/hk_native.py`、`tests/fixtures/materials/hk/**`、`tests/unit/test_material_hk_native.py` | 03 |
| `materials/us_sec.py`、`tests/fixtures/materials/us/**`、`tests/unit/test_material_us_sec.py` | 04 |
| `materials/store.py`、`materials/offline_import.py`、`tests/fixtures/materials/offline/**`、`tests/unit/test_material_store.py`、`tests/integration/test_material_offline_import.py` | 05 |
| `materials/jobs.py`、`materials/handlers.py`、`src/investment_agent/research/worker.py`、`tests/unit/test_material_jobs.py`、`tests/integration/test_material_collection_job.py` | 06 |
| `materials/evidence.py`、`tests/unit/test_material_evidence.py`、`evidence/contracts.py`、`evidence/store.py`、`investor/common.py` | 07 |
| `materials/metrics.py`、`tests/fixtures/materials/metrics/**`、`tests/unit/test_material_metrics.py` | 08 |
| `materials/context.py`、`materials/refresh.py`、`api/routes/research_materials.py`、`api/schemas/research_materials.py`、`src/investment_agent/api/app.py`、`tests/integration/test_api_research_materials.py` | 09 |
| `materials/investor_bridge.py`、`tests/integration/test_hk_us_publication_flow.py`、`investor/publication_numbers.py` | 10 |
| `src/investment_agent/web/routes.py`、`web/templates/**`、`web/static/**`、`tests/integration/test_web_stock_financials.py`、`tests/integration/test_web_client_contract.py` | 11 |
| `README.md`、`docs/product/hk-us-expansion.md`、`docs/product/plans/hk-us-expansion/12-acceptance.md` | 12 |
| `materials/valuation.py`、`tests/unit/test_material_valuation.py` | 13（可选） |
| `materials/dividends.py`、`tests/unit/test_material_dividends.py` | 14（可选） |

## 5. 执行协议

1. **集成分支**：`/Users/zhvala/SourceCode/zcharlie-hkus`，分支 `feat/hk-us-expansion`，从 main 的"计划提交"建立。主 checkout `/Users/zhvala/SourceCode/zcharlie` 有 200+ 未提交的用户 WIP，**任何任务都不得在主 checkout 里改文件或提交**。
2. **每任务独立 worktree**：控制 agent 为每个子任务执行
   `git -C /Users/zhvala/SourceCode/zcharlie-hkus worktree add /tmp/zc-hkus-<task> -b hk-us/<task> HEAD`，
   把子计划路径交给一个 sub-agent（`general-purpose`，可写）。
3. **子 agent 交付**：按子计划的 Task 清单实现 → 跑该任务的定向测试 + 全量三门禁 → 只 `git add` 自己文件清单里的路径 → 一次提交 → 返回结构化报告（任务号 / 分支 / 提交哈希 / 门禁结果 / 偏差 / 未决）。
   **依赖边界**：一个子任务只允许 import 已合并波次的模块；同波次产物一律走参数注入或测试内假实现（见 05 的 `adapters=` 注入），不得 import 尚不存在的模块。
4. **控制 agent 合并**：一个波次的所有子 agent 结束后，在集成分支上依次 `git merge --no-ff hk-us/<task>`；冲突按"谁的文件谁改"回退给对应任务重做，不做无主合并。合并后跑一次全量门禁。
5. **状态记录**：每个子任务结束后更新 [`COMPLETION.md`](COMPLETION.md)（任务号、提交哈希、测试数、偏差、下一步）并提交。**任何时刻中断，COMPLETION.md 都反映已完成的真实状态。**
6. **死代码清理**：`git worktree remove --force /tmp/zc-hkus-<task>` 在合并后执行，避免残留。

## 6. 时钟与终止规则

- 实施窗口：**启动时刻 T0 起 4 小时**（硬截止 T0+4h）。
- 每波开始前检查 `date +%s` 与截止时间：剩余不足该波预估用时（W1～W3 各 40 分钟，W4～W5 各 35 分钟）时，**不再启动新波次**，改写 COMPLETION.md 记录状态后停止。
- 剩余时间 < 45 分钟时，W6（13/14）不启动，直接在 COMPLETION.md 里标为"未启动（时间预算不足）"。
- 到硬截止时：停止派发新子 agent，等待在跑的子 agent 结束（最多再等 15 分钟），合并已完成分支，写 COMPLETION.md 的"终止记录"（每任务状态、已合并提交、未完成任务、建议的恢复入口），然后**停止并等待人工确认**。不 push、不合并到 main。
- 06:00 有一个既有的"自动提交 + code review"定时任务会在主 checkout 上运行；本实施在 05:30 前收尾，避免与之交叉。

## 7. 遗留（不在本次实施范围，留给联网验收）

| 项 | 原因 | 恢复条件 |
| --- | --- | --- |
| 八家最新实际年报补验 | 需联网抓取最新财年，本地证据只覆盖 2021–2024 | 联网环境逐家核对最新年报、原件与实际期间 |
| 在线刷新开关打开 | 东财/Yahoo/新浪/HKMA 的使用许可未确认 | 确认展示/缓存/商业授权后置 `online_refresh=true` |
| 供应商付费权限（FMP/EODHD/HKEX） | 无真实账户端点证据 | 账户与端点逐项验收后作为可替换来源 |
| 港股公布/修订历史、跨日稳定性 | 无完整已验证时间链与多日保障 | 多日 fresh fetch 与官方披露关联验收 |
| 不引入 EdgarTools | 离线环境无该依赖，且设计把版本选择与资格判断归属应用 | 若引入须固定版本、重跑解析与版本案例 |

## 8. 任务状态

实时状态见 [`COMPLETION.md`](COMPLETION.md)。本文件不记录状态，避免两处不一致。
