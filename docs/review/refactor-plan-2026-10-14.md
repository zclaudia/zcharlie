# 架构评审修复计划（2026-10-14）

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
