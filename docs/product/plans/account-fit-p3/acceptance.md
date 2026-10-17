# P3 工程验收记录（AF-P3-01—AF-P3-10）

日期：2026-10-17（Asia/Shanghai）。状态：P3 十项任务工程验收通过（含主代理最终验收）；生产保持关闭（三开关默认 false）、无生产迁移、未提交或推送。本记录只覆盖真实执行过的检查；数字以本轮实测为准，不与历史记录累加，专项与全量分区重叠不累加。

## 主代理最终验收（最终源码，2026-10-17）

全部数字对应同一份最终源码（其后仅文档改动）；集成带 `ZCLAUDIA_GATEWAY_DIR`（本机相邻网关仓库）与惰性 `API_KEY` 执行。

| 分区/门禁 | 结果 |
| --- | --- |
| 全量单元 `pytest -q tests/unit` | **2485 passed / 2 skipped / 6 network 默认排除**；256.88s；2 skip 为已知缺原始 captured 材料（Costco/HSBC），未伪造替代 |
| 全量集成 `pytest -q tests/integration`（含网关契约 4 项实跑） | **789 passed；无失败/错误/跳过**；590.40s |
| 根目录分区 `pytest -q tests/test_*.py` | **717 passed**；140.87s（带 `API_KEY`——`test_method_evolution` 等既有用例依赖惰性凭据环境，缺省时 Settings 校验失败属环境缺口非回归，已实测证实） |
| 合并专项（P3 全部新增 + P2 复审专项 + 迁移 050/051/052 + settings/job/ranking/import 图） | **287 passed**；59.16s；与完整分区重叠，不累加 |
| 真实浏览器 | research_closure / supplemental_rules / account_fit 三脚本串行，1440/390 全部通过（P3 含绑定溯源、逐规则状态、REVIEW_REQUIRED 中文原因、ready/limited 合成投影、legacy 无残留；本轮浏览器曾重写 P1/P2 既有探针截图，已两次恢复归档版本，仅保留 P3 自己的合成截图） |
| Ruff `src tests alembic` | All checks passed |
| mypy 全量源文件 | 430 文件 0 错误 |
| 迁移 | `alembic heads` = `052 (head)` 单 head；050/051 归档文件未改写 |
| 工作区 | `git diff --check` 通过 |

PB01—PB05 逐项均有真实链路反例（HTTP/SQLite/worker/浏览器至少其一，禁止仅 fixture 声明）：PB01 受理绑定与重试不追新（test_binding_admission）、PB02 新规则限制旧结论且零入队（test_rule_coverage_limits）、PB03 原子发布/发布期限制重查/财务分隔离/reduce-exit 不受阻（test_publication_closure、test_plan_readiness_split、test_suitability_integration）、PB04 评审 hash 绑定与无评审不闭合（test_publication_closure、test_coverage_prepare）、PB05 全新事件与新修订发现（test_rule_coverage_limits）。场景级映射见[复审记录](review.md)与各任务测试。

## 本轮（AF-P3-10）门禁实测数字

测试环境沿项目惯例：临时 `DATABASE_URL`/`DATA_DIR`、惰性 API_KEY，不访问 `.dev` 与真实库；临时变量使用非保留名（`task_test_exit`）。

| 门禁 | 命令 | 结果 |
| --- | --- | --- |
| 新增专项（真实 HTTP） | `pytest tests/integration/test_api_research_closure.py` | 8 passed |
| P2 API 旁路/收据回归 | `pytest tests/integration/test_supplemental_acceptance.py` | 38 passed（与新增专项、web、api_research 合并一次运行） |
| Web 页面专项 | `pytest tests/integration/test_web_supplemental_rules.py` | 7 passed |
| 通用 research runs | `pytest tests/integration/test_api_research.py` | 同上合并运行 |
| 接缝回归 | publication_closure / rule_coverage_limits / coverage_prepare / binding_admission / migration_052 / web_account_fit / api_account_fit / api_candidate_rankings / plan_readiness_split / route_input_validation / web_app | 81 passed |
| 浏览器（三脚本串行 1440/390） | `tests/browser/run.sh research_closure.mjs supplemental_rules.mjs account_fit.mjs` | 全部通过（P3 research-closure、P2 supplemental management、P1 account_fit 各自 1440px/390px） |
| Ruff | `ruff check src tests` | All checks passed |
| JS 语法 | `node --check supplemental_items.js account_fit.js` | 通过 |
| mypy 全量源文件 | `mypy src` | 430 文件无错误 |
| 迁移 head | `alembic heads` | `052 (head)` 单 head |
| 工作区 | `git diff --check` | 通过 |
| 全量三分区 | `pytest tests/`（unit+integration+根目录分区，network 默认排除） | 见文末补记 |

计划中列出的 `tests/integration/test_api_supplemental_rules.py` 在现实仓库中不存在；P2 的 API 鉴权/收据/旁路断言实际位于 `tests/integration/test_supplemental_acceptance.py`，本轮按现实以该文件执行（计划与现实冲突，按现实实现并在此标记）。

## HTTP 读面（api/routes/supplemental_rules.py 扩展）

全部为认证只读 GET，挂在既有 `verify_api_key_or_session` 路由依赖下；错误映射沿 `api/deps.py` 的 InvestorError 表；无新增写接口，`registrations()` 未为 P3 增加模型可写工具。

| 路由 | 行为 |
| --- | --- |
| `GET /supplemental-rule-coverages/current?listing=<id>` | 该主体最新 published manifest + binding + 当前限制摘要（published 才计算 limits）；无绑定行 → `state=unknown, reasons=[rule_binding_missing]`；有绑定无 published → `[rule_coverage_unpublished]`；响应携带 `enabled`（开关状态只报告、不拦读）。静态路由先于动态 `{id}` 注册，真实 HTTP 断言其不被当作 UUID |
| `GET /supplemental-rule-coverages/{id}` | 精确 UUID/revision 的 prepared/published 清单：binding ref、逐规则条目（有界分页，cursor 绑定 `coverage_id+manifest_hash+after_ordinal`）、closure 记录（refs + 解析后的 formal_review application）、published 限制摘要；错 owner/不存在/revision≠1 → 404（`not_found`）；cursor 跨清单/篡改 → 422 `invalid_cursor`；不隐式重评估 |
| `GET /research-rule-bindings/{task_id}` | 任务绑定精确读取；无绑定行（含跨 owner）→ `state=legacy_unbound, reasons=[rule_binding_missing]`，绝不以空规则集冒充。挂载点选择理由：不扩展 `/agent/tasks/{task_id}`——该 Task 载荷是与模型和 agent UI 共享的冻结契约；本读取属 P3 补充面，独立路由继承 owner 校验与错误映射，最小侵入 |

关闭开关时全部 GET 仍可读（开关状态如实出现在 `enabled` 字段），POST 链语义不变（P2 acceptance 回归覆盖）。

## 页面（单股页）

- `web/templates/stock.html`：补充事项卡新增 `#supplemental-coverage-content` 容器；账户适配卡说明更新为「ready 只表示满足本次已检查条件，不构成买卖建议（工程功能，不代表生产启用）」。
- `web/static/supplemental_items.js`：新增 `renderRuleCoverage`（绑定溯源：bound 标记、规则/事件世代、绑定规则数、受理时点、绑定引用与规则集合/内容哈希；prepared/published 阶段区分；逐规则状态与 unknown 原因；当前限制 REVIEW_REQUIRED 依据），`reasonLabel` 镜像 `investor/enums.py::REASON_LABELS`（含 `:<identity>` 后缀按头匹配）；legacy 状态显示「legacy 标记：未覆盖新增规则…不回填、不追认」；空规则集绑定与 legacy 未绑定文案可区分；开关关闭显示「功能当前关闭；以下为既有只读数据」。P2 应用面板渲染逻辑未改动。
- `web/static/account_fit.js`：`renderFitView` 增加 ready/limited 桶文案（ready 携带「不构成买卖建议」）、`supplemental_binding`（bound 展示覆盖引用与双世代 / unknown 展示 REASON_LABELS 中文原因；`not_integrated` 旧行保持原展示）、`research_status`（含 REVIEW_REQUIRED）与 `company_alignment`；常驻文案声明工程边界。`reasonLabel` 经 `supplemental_items.js` 共享导出，避免两份文案漂移。

## 浏览器（tests/browser/research_closure.mjs + research_closure_fixture_server.py）

沿 `supplemental_rules_fixture_server.py` 模式（OS 临时目录守卫、惰性凭据、真实 create_app + 真 worker）。夹具用真实发布链造数：激活两条规则 → admit 绑定 → 真评估 worker → prepare（含 closure 提案）→ 语义评审 → publish → 再激活一条 critical 规则并真评估，使当前限制重查输出 `rule_coverage_pending:<id>`。覆盖：绑定溯源哈希与世代、逐规则状态、REVIEW_REQUIRED 中文原因、真实提交 fit 后 bound/REVIEW_REQUIRED/incomplete 组合、prepared/published 区分与关闭开关合成投影、ready/limited 桶合成投影（本环境无法真实成排 ready/limited 场景，以与线上同一 renderFitView/renderRuleCoverage 渲染器驱动）、legacy 主体切换无残留（无 bound 标记、无对方 manifest 哈希）、legacy fit 的 unknown 原因、1440/390 两档无横向溢出与截图（`docs/product/probes/account-fit-p3/`）、全页面文案不含「建议买入」。

## 验收追踪（P3-F15/P3-F16 及接缝）

| 验收点 | 证据 |
| --- | --- |
| F15 旁路：客户端不能提交 binding/coverage；通用 runs 不泄露 P3 清单；GET 零写入 | test_api_research_closure：`/research/runs?kind=all` 与逐 run 详情、`/investment-assessment-runs/{id}` 断言 manifest_hash/覆盖 id/绑定 id 不出现、评估 run 404；POST 面由 P2 acceptance 回归 |
| F15 鉴权/404/current 不被动态吞 | 三路由无钥 401；不存在/跨 owner 404；`current?listing=` 200 且 `reasons=[rule_binding_missing]`（真实 HTTP，非 422） |
| 分页 | limit=1 两页走完 entry_total=2；跨清单 cursor 与垃圾 cursor 均 422 `invalid_cursor` |
| 关闭开关可读 | `set_runtime(False)` 后 current/exact/binding 全部 200 且 `enabled=false` |
| F16 浏览器 | 见上节；两档视口全过 |
| 页面不改 P1/P2 语义 | web_supplemental_rules 7 passed（含 stock 面板既有断言逐字保留 + 新增 P3 断言）；P2/P1 浏览器脚本回归通过 |

## 边界与遗留风险

0. 计划要求的页面文案原文为「不构成买入建议」；既有守卫 `test_web_candidates.py::test_ui_has_no_trading_language_or_scores` 禁止 account_fit.js/模板等出现「买入」二字（该守卫未被削弱）。按现实实现，上线文案改为语义更强的不构成「买卖」建议，六处 UI 文案与全部断言同步；全量回归证实守卫与文案兼容。

1. `test_api_supplemental_rules.py` 名称与计划不符，按现实执行 `test_supplemental_acceptance.py`（见上）。
2. ready/limited 桶的浏览器证据来自与线上相同的渲染器合成投影：真实成排需要 supportive/restrictive 公司论据与 warning 财务的完整组合，超出本任务夹具范围；真值表本身由 P3-08 的单元/集成测试固定。
3. coverage 条目分页按 payload 内元组切片实现（≤2000 有界），页大小沿 P2 上限 100；超 50 条时页面只预览 50 条并提示经接口分页读取。
4. `GET /research-rule-bindings/{task_id}` 对不存在的任务同样返回 `legacy_unbound`（不做任务存在性探测，避免跨面依赖 agent 存储；owner 隔离语义不受影响）。
5. 全量三分区、浏览器与静态门禁的最终数字见下文补记；专项与全量重叠，不累加宣称。

## 全量补记（AF-P3-10 当时的分区运行，被文首主代理最终验收取代）

T10 界面子代理曾以自身门禁记录本节；主代理最终验收（文首）在同一最终源码上以完整分区（含网关契约实跑与惰性凭据）覆盖并取代以下占位结果，两者不累加。

P3 工程完成不等于生产启用或真实研究质量验收；所有开关保持默认 false，未写入生产设置、未迁移生产库、未提交或推送。
