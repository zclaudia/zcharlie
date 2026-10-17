# P3 执行记录（AF-P3-01—AF-P3-10）

日期：2026-10-17（Asia/Shanghai）。本文件记录 P3 十项任务的实际执行顺序、文件责任与本轮（AF-P3-10）的改动清单；验收数字见[验收记录](acceptance.md)。未提交或推送；生产开关保持默认 false。

## 任务与交付

| 顺序/编号 | 交付 | 主要文件（相对 `src/investment_agent/`） |
| --- | --- | --- |
| 01 / 冻结 | `docs/product/plans/account-fit-p3/p3-freeze.md`、`tests/fixtures/research_closure_v1/`、`tests/support/supplemental_rules.py` 扩展 | 契约、容量（2000/4MiB）、开关矩阵冻结 |
| 02 / DTO | `supplemental_rules/contracts.py`（binding/coverage/RuleReviewItem/authorization）、`agent/research_publication.py`、`investor/publication.py`、`investor/common.py` | 两个新 ObjectRef kind 与 hash 规范 |
| 03 / 存储 | `storage/models/investment_assessment.py`、`storage/models/supplemental_rules.py`、`alembic/versions/052_research_rule_binding.py`、assessment/binding 仓储 | 052 手式重建 + 不可变绑定表 + 降级守卫 |
| 04 / 受理绑定 | `supplemental_rules/binding.py`、`agent/execution.py` admit 钩子 | 首次受理同事务绑定，重试复用 |
| 05 / coverage 构建 | `supplemental_rules/coverage.py`、`agent/research_publication.py::prepare`、`supplemental_rules/admission.py` | prepare 同事务 prepared manifest |
| 06 / 发布闭环 | `investor/publication.py` check/write、closure application、published manifest、依赖附加 | 同一保存点原子提交 |
| 07 / 限制与 maintain | `supplemental_rules/publication_limits.py`、`investor/publication.py::_reference/_dependency_limits/extend`、`investor/monitoring.py`、assessment 仓储读路径（`published_rule_coverage_limits`/`latest_published_rule_coverage`/`rule_binding_state`/`RuleBindingState`） | 新规则/新事件限制旧结论 |
| 08 / fit 与排序 | `portfolio/suitability_contracts.py`、`suitability.py`/`suitability_calculations.py`（`compose_supplemental_fit`/`supplemental_bucket`）、`candidates/ranking*` | bound/unknown、ready/limited、research_urgency |
| 09 / 整案门槛 | `portfolio/plans.py` | buy/add 追加 fit/coverage 检查；reduce/exit 不受阻断 |
| 10 / HTTP、页面与验收 | 见下节；`tests/integration/test_api_research_closure.py`、`tests/browser/research_closure.mjs`+fixture、本文档与验收记录 | 本轮 |

## AF-P3-10 本轮改动清单

新增：

- `src/investment_agent/api/routes/supplemental_rules.py` 内三个只读 GET（`/supplemental-rule-coverages/current`、`/supplemental-rule-coverages/{coverage_id}`、`/research-rule-bindings/{task_id}`）与 cursor 编解码 helper（+188 行）。
- `src/investment_agent/supplemental_rules/views.py`：`CoverageLimitSummary`、`CurrentRuleCoverageView`、`RuleCoverageView`、`TaskRuleBindingView`（+77 行）。
- `tests/integration/test_api_research_closure.py`（8 个真实 HTTP 测试，真实发布链造数）。
- `tests/browser/research_closure.mjs` + `tests/browser/research_closure_fixture_server.py`（真实链夹具 + 1440/390 浏览器流程）。

修改：

- `web/templates/stock.html`：补充事项卡新增 `#supplemental-coverage-content`；适配卡 ready/工程边界文案。
- `web/static/supplemental_items.js`：REASON_LABELS 镜像与 `reasonLabel`、`renderRuleCoverage` 绑定/覆盖/限制渲染、boot 并行读取 coverage 视图（P2 应用面板逻辑未动）。
- `web/static/account_fit.js`：`renderFitView` 的 ready/limited 桶文案、supplemental_binding/research_status/company_alignment/unknown 原因展示、常驻边界文案；`renderFitView` 导出供浏览器探测（沿 P2 render 导出先例）。
- `tests/integration/test_web_supplemental_rules.py`：stock 页 coverage 容器与适配卡文案断言、Node 端 `reasonLabel` 行为与 import 图测试、fit 面板源文案检查。

理由记录（计划第 6 节待确认项「绑定读取挂载点」）：选择独立 `GET /research-rule-bindings/{task_id}` 而非扩展 `/agent/tasks/{task_id}`，因 agent Task 载荷是与模型和 agent UI 共享的冻结契约，P3 读取面收敛在 supplemental 路由内可继承 owner 校验与错误映射，改动面最小。

## 门禁执行方式

浏览器统一经 `tests/browser/run.sh`（自动探测 NODE_BIN / AG17_NODE_MODULES / AG17_CHROMIUM_EXECUTABLE，固定端口串行）。测试一律使用临时 `DATABASE_URL`/`DATA_DIR` 与惰性 API_KEY；临时变量使用非保留名（如 `task_test_exit`）。浏览器失败时保留原始输出并修脚本/夹具，不改断言凑绿（本轮经历 5 次真实失败迭代：夹具键名不一致、`innerText` 对折叠 details 的可见性语义、设置导入覆盖缺失导致 fit 面板关闭、断言用错 hash 域、移动档历史详情折叠——均以修正实现或脚本解决）。
