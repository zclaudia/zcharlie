# 架构评审修复计划（2026-10-14）

> **执行状态：已执行四波次，共 25 个工作包提交（`1c04589..180a54f`）。**
> 门禁：基线 `3260 passed / 1 failed`（时间耦合用例）＋ `ruff` 13 错 → 终态 **`3291 passed / 0 failed`、`ruff` 0 错、`mypy` 0 错（360 文件）**。
> 逐项完成情况见文末「七、执行结果」；验证证据与需用户决策的事项见 `refactor-execution-2026-10-14.md`。

- 依据：`docs/review/architecture-review-2026-10-14.md`（P0×10 / P1×26 / P2×6）
- 分支：`refactor/arch-review-p0`（每个工作包一个提交；门禁全绿才提交）
- 门禁（三项，与 CI 一致）：

```bash
export DATA_DIR="$PWD/data" DATABASE_URL="sqlite+aiosqlite:///$PWD/data/app.db" API_KEY="gate-key" APP_ENV=dev
.venv/bin/pytest -q -m "not network"     # 全量
.venv/bin/ruff check .
.venv/bin/mypy src/investment_agent
```

> `tests/conftest.py` 的 `_no_dotenv` autouse fixture 会禁用 `.env`，所以三个环境变量必须显式导出，否则 Settings 解析失败。

## 一、执行原则

1. **先安全网、后结构改动**：P0 的黄金数据入仓、假测试改写、支持层抽取、import 守卫必须先落地，否则后续拆分缺少可信的回归信号。
2. **文件所有权互斥**：并行工作包按文件划分所有权，任一文件在任一波次只属于一个工作包。跨工作包需要的共享模块（`domain/time.py` 等）在第一波次先建。
3. **波次之间串行跑全量门禁**：每个波次内并行，波次末由主进程跑三项门禁；门禁绿才逐包提交。
4. **结构重构与语义变更严格分离**：不动 golden（`tests/fixtures/research_kernel/**`）、不改 scoring version、不改任何阈值/数值语义。任何工作包若发现需要改数值，停止并上报。
5. **不做"按行数搬家"**：只做报告里标注"建议拆/部分拆"的项；"不建议改动"清单里的对象一律不动。

## 二、波次 A：安全网与前提（P0）

| 包 | 内容 | 报告条目 | 文件所有权 | 量 |
|---|---|---|---|---|
| A1 | peer 测试基座：run-02 黄金数据入仓 + 抽 `tests/support/peer_world.py` + peer 侧源码文本断言改 AST | P0-1, P0-3, P0-2(peer) | `tests/unit/test_peer_*.py`、`tests/integration/test_peer_end_to_end.py`、`tests/integration/test_web_stock_peer_section.py`、新 `tests/support/peer_world.py`、新 `tests/fixtures/peer_run02/*` | M |
| A2 | materials 两处"读源码 grep"纯度断言改 AST 断言 | P0-2(materials) | `tests/unit/test_material_metrics.py`、`tests/unit/test_material_valuation.py` | S |
| A3 | 删死代码：`research_kernel/data_quality.py`(231)、`investor/services.py`(39)、`agent/services.py`(18)、`transport.ts:371-377`(7) | P0-5 | 上述文件 | S |
| A4 | import-graph 守卫 + 基线快照（新增允许清单，禁止新增违规边；绝对不变量单独断言） | P0-4 | 新 `tests/unit/test_import_graph.py`、新 `tests/fixtures/import_graph_baseline.json` | S |
| A5 | 共享基元：`domain/time.py`（UTC 归一化）、`domain/coerce.py`（可选值强转）+ 单测 | P0-6(基座) | 新 `src/investment_agent/domain/time.py`、新 `domain/coerce.py`、新单测文件 | S |
| A6 | 前端去重：`PROVIDER_LABELS` 两表合并（逐项判定文案）、`methods.js` 本地 HTTP 重写回归 `api.js` | P0-6(前端) | `web/static/{format,health,methods}.js`、`tests/integration/test_web_client_contract.py` | S-M |

## 三、波次 B：P0 结构收口 + P1 低风险拆分

| 包 | 内容 | 报告条目 | 文件所有权 | 量 |
|---|---|---|---|---|
| B1 | `api/deps.py`：统一 DI 工厂 + `InvestorError→HTTP` 映射表（消 5 份重复 + 3 份不一致）；修 `candidates.py:121` 裸 `raise exc`；api 侧 UTC/可选值改引 `domain/` | P0-8, P0-6(api) | `src/investment_agent/api/**` | M |
| B2 | iFinD 预算/台账提至中立层，切断 `verification ⇄ snapshots/research` 环；`_number` 脏值解析合并（核验与采集同源） | P0-10, P1-24 | 新 `src/investment_agent/ifind_budget.py`、`verification/**`、`snapshots/health.py`、`snapshots/handlers.py`、`snapshots/financial.py`、`research/ifind_chain.py`、`cli/commands/verify.py` | M |
| B3 | `SEMANTIC_HASH_SCHEMA` 从 CLI 迁入域包，消除"域包反向依赖 Typer CLI" | P0-9 | 新 `industry_peers/hashing.py`、`industry_peers/jobs.py`、`cli/commands/peer_annual.py` | S |
| B4 | `storage/models/__init__.py` 承担全量注册，删 conftest/alembic 两份手抄清单；storage/agent 侧 UTC 归一化改引 `domain/time.py` | P0-7, P0-6(storage) | `storage/models/__init__.py`、`tests/conftest.py`、`alembic/env.py`、`storage/repositories/{provider_call,candidate,pit}.py`、`agent/conversations.py`、`mcp_server/server.py`、`portfolio/{service,valuation}.py`、`valuation/{service,solver}.py` | M |
| B5 | `candidates/lifecycle.py` 抽出 screen intake（1001→约 790，re-export 保导入方） | P1-21 | `candidates/**` | S |
| B6 | CLI 抽 `cli/runtime.py` + `cli/parsing.py`（消 8 份内联执行样板 + 28 处 bootstrap 复制）；`verify.py` 除外（归 B2） | P1-23 | `cli/main.py`、`cli/runtime.py`(新)、`cli/parsing.py`(新)、`cli/commands/*.py` 除 `verify.py` | S-M |

## 四、波次 C：其余 P1（按剩余预算推进）

`providers/transport.py`（P1-4）、`settings` 三小步（P1-19）、`evidence/store` 适配器（P1-13）、`mcp_server` 拆分 + `api/views.py`（P1-7）、`is_trading_day`/终态集合收敛、`snapshots/health.py` 拆包（P1-3）。

## 五、明确不做（本轮）

- **P2 全部**：仓储网关边界、API 读路径下沉、测试 fixture 事务化——前者是全仓最贵重构（172 个测试文件直连 ORM），后者须排在所有生产重构之后。
- **报告"不建议改动"清单**里的对象：`materials/contracts.py`、`storage/models/`（模型本身）、`settings/registry.py` 的 SPECS 表、`rules/screen.py`、`clock.py`、`providers/{errors,circuit}.py`、`storage/repositories/base.py`、`storage/database.py` 的引擎单例语义、golden 语料、`app.css`、`mobile/src/transport.ts` 整体结构（仅删死函数）。
- **P0-6 中需要先固定输出契约的项**：`_decimal_text`(us_sec) 与 `_decimal_string`(hk_native) 的合并（会改已落库 `raw_value` 字符串，须先补格式测试）——留待专门工作包。
- **`rule_templates/*.yaml` 与 research_kernel 的一致性测试**（P2-5）：属新增测试，不与结构拆分混做。

## 六、验收标准

1. 波次 A/B 每包提交前：所属聚焦测试 + `ruff` + `mypy` 绿。
2. 每波次末：全量 `pytest -q -m "not network"` 绿（预期基线通过数在波次 A 后**上升**，因为 P0-1 会新增此前在干净检出下失败的用例）。
3. 最终：三项门禁全绿；`git log` 每包一个提交；本文件与评审报告的"已完成/未完成"状态更新。
4. 未完成项必须显式列出，不允许以"计划已写"充当完成。

## 七、执行结果

四波次，25 个工作包提交（`1c04589..180a54f`，未合入 `main`）。每个工作包在合并前后都跑所属聚焦测试集；每波次末由主进程串行跑全量三项门禁。

| 波次 | 工作包 | 提交 | 门禁（全量 pytest） |
|---|---|---|---|
| A | 8（7 计划 + 1 基线修复） | `141945b..cb6f3ea` | 3278 passed / 0 failed |
| B | 10（8 计划 + 2 基线修复） | `bd8b9b1..063712c` | 3281 passed / 0 failed |
| C | 4 | `e47c5e4..ae6edfc` | 3281 passed / 0 failed |
| D | 3（4 个包；其中 2 包必须同提交） | `c58cca3..180a54f` | 3291 passed / 0 failed |
| — | 基线 | `3260 passed / 1 failed`、`ruff` 13 错 | |

### P0（10/10 完成）

| 条目 | 状态 | 提交 |
|---|---|---|
| P0-1 peer 黄金数据入仓 | 完成（19 个小 metadata 入仓；37MB 原件改为显式 skip + 环境变量开关，见执行报告决策项） | `6a7c44a`、`bd8b9b1` |
| P0-2 读源码 grep 假测试改 AST | 完成（peer 2 处 + materials 2 处） | `6a7c44a`、`fa9b270` |
| P0-3 抽 `tests/support/peer_world.py` | 完成（消除 6 个测试模块互引） | `6a7c44a` |
| P0-4 import-graph 守卫 | 完成（不变量 4 条 + ratchet；后修正为不计 `TYPE_CHECKING`） | `2a68a3e`、`904bff9` |
| P0-5 删死代码 | 完成（296 行） | `038d2d3` |
| P0-6 重复实现收敛 | **部分**：UTC 归一化收敛完成（`29d98a0`、`6f4210f`）；前端 `PROVIDER_LABELS`/`methods.js` 完成（`88474fe`）。**未做**：`_optional_*`（bool 语义分歧）、`is_trading_day` 7 处兜底、终态集合 5 处、`_decimal_text` 合并 | — |
| P0-7 模型注册单点化 | 完成（106 表集合零差异） | `6f4210f` |
| P0-8 api DI 与错误映射收敛 | 完成 | `8a87b47` |
| P0-9 哈希 schema 迁出 CLI | 完成 | `4c33c70` |
| P0-10 iFinD 预算提至中立层 | 完成（落在 `providers/ifind_budget.py`，边集 204→204） | `6a9ce8d` |

### P1（8/26 完成，其中 2 项部分）

| 条目 | 状态 | 提交 |
|---|---|---|
| P1-1 `_project_decimal` 342 行分解 | 完成（16 场景新旧并排等价证明） | `c58cca3` |
| P1-2 `materials/us_sec.py` 三分 | 完成（1895→1143） | `20688cc` |
| P1-3 `snapshots/health.py` 拆包 | 完成（1704→包） | `e47c5e4` |
| P1-4 `providers/transport.py` | 完成（五客户端全部采纳） | `180a54f` |
| P1-6 `factors/view.py` 拆分 | 完成（删除 god module，33 处导入改指向） | `7a1d069` |
| P1-7 `mcp_server` 拆分 + 共享视图层 | 完成（405→94；消除 7 条 mcp→api.routes） | `7a1d069` |
| P1-19 `settings` 三小步 | 完成（合并三个相同调度模型 + 日历定位外移；crontab 倒置留给 P1-14） | `ae6edfc` |
| P1-21 `candidates/lifecycle.py` 抽 screen intake | 完成（1001→787） | `4ce4d23` |
| P1-23 CLI runtime/parsing 抽取 | 完成（`verify.py`/`peer_annual.py` 归其他包，仍有余量） | `1e45108` |
| P1-24 verification 两处"重造"收口 | **部分**：`_number` 经穷举判定为外延等价（报告风险不成立），保留三份副本；`total_shares`/`statutory_deadline` 未改 | `6a9ce8d` |
| P1-25 materials 重复收敛 | **部分**：词汇下沉 `metric_core.py` 完成（`b50b5c9`）；`http_status_rejected`/`_decimal_text` 未做（需先固定输出契约） | — |
| P1-其余（5/8/9/10/11/12/13/14/15/16/17/18/20/22/26） | **未做**，仍按原计划有效 | — |

### 计划外但已完成（均源于报告已记录的风险类别）

| 修复 | 提交 | 说明 |
|---|---|---|
| 恢复 ruff 门禁 | `141945b` | 上一提交基线 `ruff check .` 有 13 错（peer 工作包遗留 11 + 2 未归属） |
| 时间耦合用例解耦 | `cb6f3ea` | 报告 §4.6 预测的类别在 `test_market_monitor_...` 实际发生：基线唯一失败 |
| API 关停无界等待 | `063712c` | 目录同步在途时关停 19.03s vs 15s 预算，是 `test_clean_environment_smoke` 在 3 次全量中失败 2 次的原因 |
| 前端超时文案保真 | `cfeb8db` | 上一包合并 HTTP 客户端时把超时文案并成通用文案 |
| 守卫不计 `TYPE_CHECKING` | `904bff9` | 注解专用导入不构成运行时依赖；baseline 204→203 |

### 未做（明确列出，不视为完成）

- **P2 全部 6 项**：仓储网关边界、API 读路径下沉、测试 fixture 事务化（执行报告给出为什么最后做）、`config.py` 收窄 / api↔web 环、`rule_templates` 与 kernel 的一致性测试、`local_owner` 多租户化。
- **P1 剩余 15 项**：`snapshots/backfill.py`(P1-5)、`agent/research.py`(P1-8)、`investor/publication.py`(P1-9)、`research/ifind_chain.py`(P1-10)、`industry_peers`(P1-11)、`rules/zone_detection.py`(P1-12)、`evidence/store.py`(P1-13)、`scheduler/runner.py`(P1-14)、前端按需加载与页面拆分(P1-15)、`PortfolioService` 类拆分(P1-16)、`plans.check`(P1-17)、`reverse_dcf`(P1-18)、`storage/repositories/pipeline_run.py`(P1-20)、`verification/job.py`(P1-22)、测试结构重整(P1-26)。
- **P0-6 剩余子项**与 **P1-24/P1-25 剩余子项**（见上表）。
- 评审报告与各波次子报告提出的"发现但不改"清单（如 `akshare` 熔断键冲突、`eastmoney_annual` 缓存命中记为 `terminal`、`api/views.py` 的 `compute_data_health` 借用边、`providers/ledger.py:3` docstring 指向旧路径）均未动手，**已在执行报告中逐条列出**。
