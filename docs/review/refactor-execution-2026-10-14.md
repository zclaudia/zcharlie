# 架构评审修复执行报告（2026-10-14）

- 计划：`docs/review/refactor-plan-2026-10-14.md`（依据 `architecture-review-2026-10-14.md`）
- 分支：`refactor/arch-review-p0`，25 个工作包提交（`1c04589..180a54f`），**未合入 `main`**
- 执行方式：4 个波次，每波 4–8 个**文件所有权互斥**的工作包由 sub-agent 并行执行，波次末由主进程串行跑三项门禁

## 一、结果

| 指标 | 基线（`63a2b4d`） | 终态（`180a54f`） |
|---|---|---|
| 全量 `pytest -q -m "not network"` | **3260 passed / 1 failed** / 3 skipped | **3291 passed / 0 failed** / 3 skipped |
| `ruff check .` | **13 errors**（peer 工作包遗留 11 + 2 无归属） | **0** |
| `mypy src/investment_agent` | 0 errors | **0**（360 文件） |
| 文件改动 | — | 180 文件，+20,429 / −8,016 |

四波次门禁：A 3278 passed / B 3281 / C 3281 / D 3291，全部 0 failed。基线那 1 个失败是**时间耦合用例**（报告 §4.6 预判的类别），在本轮一并修掉。

交付内容：**P0 10/10 完成**；**P1 8/26 完成 + 2 项部分完成**；另修复 4 个计划外但在报告风险清单里的既有缺陷（lint 门禁、时间耦合、API 关停挂起、前端文案）。逐条状态见计划文档「七、执行结果」。

## 二、波次与提交

| 波次 | 工作包 | 提交区间 |
|---|---|---|
| A（安全网与前提） | 7 计划包 + 基线 lint 修复 | `141945b..cb6f3ea` |
| B（P0 结构收口 + 低风险 P1） | 8 计划包 + 2 计划外修复 | `bd8b9b1..063712c` |
| C（大文件拆分） | 4 包 | `e47c5e4..ae6edfc` |
| D（剩余高价值结构项） | 4 包（其中 factors 与 mcp 必须同提交，见下） | `c58cca3..180a54f` |

`refactor(factors,mcp)` 一条提交包含两个工作包：D2 删除了 `factors/view.py`，而当时的 `mcp_server/server.py` 正 import 它，D4 之后才改为 import `view_db`——拆成两条提交会产生无法导入的中间状态，故合并，并在提交信息里说明原因。

## 三、关键验证证据（每个工作包都有独立证据，此处只列最强的几条）

**可验证性（P0-1/P0-3）** — 在**无 `/research/` 目录**的 scratch worktree（干净检出仿真）中跑 peer 全簇 + import 守卫：**225 passed / 20 skipped / 0 failed**。skip 只覆盖确实需要 37MB 原件 PDF 的 20 个用例，理由串可执行：

```
run-02 filing originals unavailable: <path>/raw/official is missing or empty, so the
importer keeps every audited field 'candidate' instead of 'verified' and this test's
verified path cannot run. Enable it by pointing ZCHARLIE_PEER_ORIGINALS at an evidence
root containing raw/official/*.pdf (checkout default: ...)
```
本机（有原件）跑同一集合：226 passed / **0 skipped**——本地覆盖度未降低。改动前该集合在干净检出下是 206 passed / 20 **failed**。

**等价性（P1-1 投影引擎）** — 用 `git show HEAD:` 取出旧实现，与新的 `ProjectionEngine` 并排跑 16 个手工场景（开仓／买入／部分卖出／超基数卖出／含差额对账／基线前回填／同时刻后记录／reversal／拆股＋待确认／零持仓／未知现金／缺价／未知费用／受限标的／成本沿用／trace），逐项比较**整份快照 JSON、`confirmable_deficits`、trace、价格查询实参**——16/16 完全一致；新模块在该 harness 下 99% 行覆盖，另加 10 个无 DB 单测。

**结构性证明（P0-10 / P1-6 / P1-7）** — 都不是"看起来对"，而是可执行断言：
- 预算移出 verification：包对边集 204→204（零增零删），新模块在新解释器里加载 0 个 `verification`/`snapshots`/`research.ifind_chain` 模块。
- `factors/view.py` 拆分：新解释器 `import factors.snapshot_model` / `field_resolution` 后 `sys.modules` 中 sqlalchemy 与 storage.repositories **为空**；`import factors.view_db` 则加载 195 个——纯路径与 DB 路径彻底分开。
- MCP：`grep -rn "api.routes" src/investment_agent/mcp_server/` 无输出，且 `build_server()+list_tools()` 后 `sys.modules` 中 `investment_agent.api*` 为空；工具目录（name/description/inputSchema + resource）与 HEAD 逐字节一致。

**不变量（P1-4 / P1-2 / 模型注册）**
- 五客户端重试管道：三段被测试钉住的次序（熔断在首试**前** check、在末次**后** record、`skipped_open` 不记失败、每逻辑调用恰一条台账）由 10 个定向用例直接覆盖并通过。
- `us_sec.py` 三分：每个搬迁块断言"在新模块逐字存在、在旧模块不存在"，并用六个 fixture 并排 replay 新旧适配器——problems / draft 数量 / 每个 draft payload（深比较）/ period / raw_source_url / `requests()` 序列全部相同。
- 模型注册单点化：`Base.metadata` 表集合改前/改后 **106 张、零差异**，并在空库上实跑 `ia migrate up`（48 个迁移）+ 校验库表集合与 metadata 相等。
- golden 纪律：`tests/fixtures/research_kernel/`、`tests/fixtures/factors/` 全程未改；`test_factor_golden_parity.py`、`test_golden_corpus_replay.py` 均通过。

**守卫本身也被验证**：import-graph ratchet 通过"从 baseline 删掉一条真实运行期边（`providers -> storage`）"复现失败，确认仍会咬；基线重生成只删了一条边（`industry_peers -> cli`，正是 P0-9 修掉的分层倒置），无新增。

## 四、需要你决策的事项

1. **peer 原件 PDF（37MB）是否入仓**。现状（方案 A，已落地）：19 个小 metadata（35KB）入仓，20 个需要原件的用例在无原件时**显式 skip**——CI 不再红，但 CI 不覆盖 `verified` 原件的 20 个用例。备选：**B** 把 19 个 PDF 放进 `tests/fixtures/peer_run02/raw/official/`（仓库 +35.4MB，需考虑第三方年报再分发）；**C** CI 侧用 artifact/缓存/LFS 提供原件并设 `ZCHARLIE_PEER_ORIGINALS`。metadata 与解析逻辑已就位，B 只需补 PDF 并改默认回落顺序。
2. **前端 provider 文案分歧**：`akshare` 在候选/概览页显示「AkShare」、健康页显示「AkShare（东财）」。本轮把差异收拢为健康页一处**显式覆盖**（零文案变更），如需全站统一请指定取哪一个。
3. **`api/views.py` 的 763 行**：本轮按"单文件中立层"落地（重构后 `mcp_server` 不再 import `api.routes`）。若你希望它更薄，可再拆成 `api/views/` 包（导入路径可保持不变）。
4. **若干语义/契约判断**已在提交信息与下方"发现但未修"中逐条记录，均**刻意未改**：`_optional_float` 的 bool 语义分歧（`research.py` 返回 `1.0` vs `watchlist.py` 返回 `None`）、`max_attempts` vs `max_retries` 计数口径、`min_interval_seconds` 改为只读 property（外部赋值会 `AttributeError`）。

## 五、发现但未修（本轮只报告，已在各处留证据）

| 发现 | 位置 | 为何未改 |
|---|---|---|
| **`akshare` 熔断键被两个客户端共用**：`akshare_eastmoney` 与 `akshare_shenwan` 都定义 `PROVIDER="akshare"`，任一 `akshare:*` 任务熔断会拦住另一个 | `providers/akshare_{eastmoney,shenwan}.py` | 属行为修正（会影响故障域与健康页呈现），需单独评审 |
| `eastmoney_annual` 缓存命中把台账事件标成 `terminal`（看起来像 bug） | `providers/eastmoney_annual.py` reuse 路径 | 改标签会改台账口径 |
| 两组"同一语义两种数字"：`max_attempts=2`（=2 次）vs `max_retries=2`（=3 次）；节流打点位置不同（akshare 每次尝试后 / annual 仅在开始） | 五个客户端 | 已参数化保留，未统一数值 |
| 脏数值解析有**第三份**副本（前两份经穷举判定外延等价，报告里的"可能翻转 verdict"风险**不成立**） | `factors/metrics.py` `parse_number` | 修改即涉及比较口径 |
| `is_trading_day` 兜底逻辑 7 处、「未复核年份按工作日」这一业务决策散落 | `snapshots/health`、`scheduler/runner.py`、`portfolio/valuation_jobs.py`、`api/routes/portfolio.py` 等 | 跨多包，未纳入本轮文件所有权 |
| 终态集合 5 处（权威在 `storage/repositories/pipeline_run.py:18`） | `research/{worker,watchlist_refresh}.py`、`storage/repositories/watchlist.py`、`investor/{monitoring,discovery}.py` | 同上 |
| `rules.py` 仍**传递**加载 sqlalchemy（经 `models.py`→`snapshots.jobs` 的 kind 常量与 `rules.py`→`snapshots.collect` 的 `QUOTA_STOP_REASON`），故"规则层零 DB 依赖"只做到断言层面 | `snapshots/health/{models,rules}.py` | 需把那两组常量迁到无 DB 模块，超出该包文件集 |
| `compute_data_health` 仍函数级借用 `api/routes/dashboard.py` 的 `calendar_warnings`/`_calendar_path` | `api/views.py` | 建议把这两个 helper 下沉到 `settings/calendar_paths.py` 旁 |
| `settings/registry.py:102` import `scheduler/runner._parse_cron`（**私有**函数、跨层倒置） | settings ↔ scheduler | 属 P1-14（scheduler 拆分） |
| 空输入路径的异常类型变化：旧 `_project_decimal` 抛 `ValueError`（来自一行死代码），新路径抛 pydantic `ValidationError` | `portfolio/` | 所有调用点都保证非空；仅记录 |
| `dividends` 的 `_result` 包装因委托共享构建器而继承"空 basis 抛 ValueError"守卫 | `materials/dividends.py` | 分析确认该路径不可达；如要求零增量可回退为直接构造 |
| `factors/view.py` 被删除且**未留兼容门面**，仓外若有调用方会断（仓内仅历史文档提及） | — | 有意选择：保留门面会把 god 路径长期留着 |
| `providers/ledger.py:3` docstring、`verification/__init__.py` docstring、`docs/product/**` 的历史计划文档仍写旧路径 | 文档 | 历史计划文档是当时编辑记录，改写会篡改记录；新旧路径对照见下节 |

## 六、模块路径变更对照（供阅读旧文档时对照）

| 旧路径 | 新位置 |
|---|---|
| `snapshots/health.py`（1,704 行） | `snapshots/health/{__init__,models,rules,queries,accumulation}.py`（旧 import 路径全部保持可用） |
| `verification/budget.py`（实现） | `providers/ifind_budget.py`；`verification/budget.py` 保留为再导出 shim |
| `verification/job.py` 的 iFinD 预算 import | `providers/ifind_budget.py` |
| `industry_peers/jobs.py` 的 `SEMANTIC_HASH_SCHEMA` 来源 | `industry_peers/hashing.py`（原位置在 `cli/commands/peer_annual.py`） |
| `factors/view.py`（1,178 行，已删除） | `factors/{snapshot_model,field_resolution,view_db,ifind_view}.py` |
| `portfolio/service.py` 的 `_project_decimal` | `portfolio/{projection,projection_events,projection_state,prices}.py` |
| `materials/us_sec.py` 的状态机／概念表 | `materials/us_sec_periods.py` / `materials/us_sec_concepts.py` |
| `materials/metrics.py` 的结果词汇 | `materials/metric_core.py`（metrics 显式再导出） |
| `mcp_server/server.py` 的 `build_server` | `mcp_server/plumbing.py` + `mcp_server/tools/*.py` + 94 行 `server.py` |
| 七个 api 路由 helper（MCP 复用） | `api/views.py`（路由模块再导出同名） |
| `settings/loader.py` 的日历定位函数 | `settings/calendar_paths.py`（loader 再导出） |
| 五客户端的重试/熔断/台账管道 | `providers/transport.py` |
| 新增：UTC 归一化 | `domain/time.py`（`clock.py` 仍管业务日期） |
| 新增：CLI 样板 | `cli/runtime.py` + `cli/parsing.py` |
| 新增：candidates screen intake | `candidates/screen_intake.py`（lifecycle 再导出） |
| 新增：测试基座／守卫 | `tests/support/peer_world.py`、`tests/support/purity.py`、`tests/unit/test_import_graph.py` + `tests/fixtures/import_graph_baseline.json` |

## 七、后续

- **未完成项的清单与状态**在计划文档「七、执行结果」逐条列出（P0-6 剩余子项、P1 剩余 15 项、P2 全部 6 项），路线图与排序原则仍然有效。
- 下一批建议顺序：P1-11（`industry_peers`，含 `jobs._finalize` 与 `batch` 编排去重）→ P1-3 已完成的 `health` 包基础上做 `snapshots/backfill.py`(P1-5) → P1-13（`evidence/store.py` 适配器）→ P1-14（scheduler，顺带清掉 `_parse_cron` 倒置）→ P1-16/P1-17/P1-18（portfolio 与 valuation 剩余巨型函数/类）。
- **P2 一律排在最后**：仓储网关边界影响 172 个直连 ORM 的测试文件，测试 fixture 事务化影响 129 个文件，二者都会同时动摇"生产语义"与"测试基础设施"，不应与结构拆分叠加在同一批提交里。
