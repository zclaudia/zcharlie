# 方法包实现计划(M1–M3)

日期:2026-10-30。依据:[方法包契约设计](../../investor-agent-method-packages.md)(2026-10-30 修订版)。
执行方式:逐卡由 GLM-5.3-Flash 子 Agent 实现,主 Agent 验收;每卡过 `ruff check .`、
`mypy src/investment_agent` 与目标测试,末卡跑全量门禁。

## 本轮范围

- **实现 M1–M3**:包契约与注册表、基线包、存储与迁移、v2 receipt 与固定渲染器、v2 输出 schema、
  包级校验器(validator v1)、PlanView 方法覆盖、`ia methods` CLI 与全局默认 head。
- **不做 M4**:包级提案、三态历史回放、候选包离线/前瞻影子、包级批准与回退。设计第 7 节留给下一轮;
  本轮 per-listing head 无移动路径,全局启用只有 `ia methods activate-global`。
- 生产默认保持 v1:不写任何自动启用;`MethodDefaultHead` 初始为空,解析回落 legacy `FIXED_METHOD`。

## 卡片与依赖(顺序执行)

| 卡 | 内容 | 对应设计节 | 主要文件 |
|---|---|---|---|
| 01 | 包契约、输出要求判别联合、注册表、canonical hash、聚合校验 | 4.1/4.2/8 | `investor/method_packages.py`(新) |
| 02 | 基线包 YAML 与加载;基线=现行要求显式化 | 4.1 | `config/methods/company-research.v1.yaml`(新) |
| 03 | 存储:package 工件入库去重、`MethodDefaultHead`、`ToolAction.committed_sequence`、迁移 056、head 解析 | 3/5 | `storage/models/`、`investor/methods.py`、`alembic/versions/056_*` |
| 04 | v2 receipt:freeze_task 解析、固定渲染器、能力前置校验 | 4.3/6.1 | `investor/methods.py`、`agent/method_receipt.py`(新) |
| 05 | assemble 注入 rendered_method;v1 三类指令逐字节不变测试 | 6.1 | `agent/research.py` |
| 06 | v2 输出 schema:MethodPlanBinding / MethodStepResult、executor 输出类型分派 | 4.4 | `agent/research.py`、`agent/executor.py` |
| 07 | 包级校验器(首次计划 + 最终结算)、committed_sequence 赋值 | 6.2 | `agent/method_validation.py`(新)、`agent/tools.py` |
| 08 | PlanView 方法覆盖投影 | 9 | `agent/contracts.py`、`agent/progress.py` |
| 09 | `ia methods` CLI:validate/sync/list/show/status/activate-global | 5 | `cli/commands/methods.py`(新) |
| 10 | 文档更新与全量门禁 | 11 | `README.md`、设计文档状态行 |

## 全局约束(每卡必须遵守)

1. **不改动与卡片无关的未提交文件**:工作区已有 `mobile/`、`storage/repositories/listing.py`、
   两个测试文件的未提交修改,属于用户在途工作,禁止触碰、禁止回滚。
2. **v1 行为逐字节不变**:v1 的有效指令、输出 schema、`TaskMethodBinding` payload 形状、结算结果、
   golden 重放全部保持;新增校验只对 receipt 标记 enforcing 的 v2 任务生效。
3. **风格**:mypy strict、ruff 全绿;新契约放 `Contract` 基类(`investor/common.py`),带
   `schema_version` Literal;错误一次性聚合(照 `rules/validation.py` 模式)。
4. **命令**(venv 由旧路径迁入,console script shebang 失效,统一走 `python -m`;
   pytest 需 `API_KEY` 环境变量,否则 `tests/test_method_evolution.py` 两个既有测试因
   Settings 缺字段失败——这是环境基线,不是回归):`.venv/bin/ruff check .`、
   `env API_KEY=dummy-test-key-123456 .venv/bin/python -m mypy src/investment_agent`、
   `env API_KEY=dummy-test-key-123456 .venv/bin/python -m pytest -q <目标测试>`;
   末卡全量 `env API_KEY=dummy-test-key-123456 .venv/bin/python -m pytest -q`。
5. 每卡完成即提交卡内自测;发现设计文档与代码现实冲突时,**以代码现实为准并在报告中列出偏差**,
   不得擅自扩大契约。

## 验收协议(主 Agent)

- 逐卡 review diff 与卡片验收点;末卡后独立运行三门禁全量。
- 重点核验:v1 指令快照测试真实断言逐字节相等;v2 校验器对「无关 calculation 冒充反向 DCF」「自报
  完成无动作」「依赖顺序违反」「带缺口路径」四个正反例的行为;`activate-global` 不动 per-listing head、
  sync 不动任何 head;迁移 056 对空库与既有库都可执行。
- M4 缺口在设计文档与 README 中如实标注。

## 执行结果(卡 10 收口,2026-10-31)

### 各卡完成情况

十张卡按依赖顺序执行完毕,交付物与测试均在本工作区(未提交),主 Agent 逐卡验收;下表为卡 10
在收口时对工作区代码现实的核对:

| 卡 | 交付物 | 测试文件 | 状态 |
|---|---|---|---|
| 01 | `investor/method_packages.py`(契约、判别联合、注册表、canonical hash、聚合校验) | `tests/unit/test_method_packages.py` | 完成 |
| 02 | `config/methods/company-research.v1.yaml` + `load_package_file` / `load_packages` | 同上 | 完成 |
| 03 | `MethodDefaultHead`、`ToolAction.committed_sequence`、`alembic/versions/056_method_package_storage.py`、三级 head 解析(`investor/methods.py`) | `tests/unit/test_method_packages_store.py` | 完成 |
| 04 | `agent/method_receipt.py`(`TaskMethodReceipt`、`RENDERER_VERSION`、固定渲染器、准入能力前置校验) | `tests/unit/test_method_receipt.py` | 完成 |
| 05 | `agent/research.py` assemble 注入 rendered_method | `tests/unit/test_agent_company_context.py` | 完成 |
| 06 | `MethodPlanBinding` / `MethodStepResult` 与 executor 输出类型分派 | `tests/unit/test_method_output_schema.py` | 完成 |
| 07 | `agent/method_validation.py`(首次计划 + 最终结算)与 committed_sequence 赋值 | `tests/unit/test_method_validation.py` | 完成 |
| 08 | PlanView 方法覆盖投影(`agent/contracts.py`、`agent/progress.py`) | `tests/unit/test_agent_progress.py` | 完成 |
| 09 | `cli/commands/methods.py`(validate/sync/list/show/status/activate-global)与 CLI 注册 | `tests/unit/test_methods_cli.py` | 完成 |
| 10 | `README.md`、设计文档状态行与 §11 完成标记、本节;全量门禁 | 全量三门禁 | 完成(见下) |

### 全量门禁(卡 10 实测,2026-10-31)

- `env API_KEY=dummy-test-key-123456 .venv/bin/python -m pytest -q --deselect
  tests/unit/test_import_graph.py::test_no_new_package_edges_beyond_baseline`:
  **4722 passed / 0 failed / 9 skipped / 7 deselected**,exit 0(34 分 12 秒)。
  按卡 10 指示以 `--deselect` 跳过的既有失败见下。
- `.venv/bin/ruff check .`:**All checks passed!**(0 错误)。
- `env API_KEY=dummy-test-key-123456 .venv/bin/python -m mypy src/investment_agent`:
  **no issues found in 480 source files**(0 错误)。卡10本身未改动方法包的 `src/` 文件,
  方法包各卡新增/修改的全部 src 模块在最终运行下通过 strict 检查。

**已知既有失败(基线漂移,未修、未放开,按卡 10 指示以 `--deselect` 跳过)**:
`tests/unit/test_import_graph.py::test_no_new_package_edges_beyond_baseline`——既有边
`candidates -> snapshots`、`rules -> snapshots` 来自更早提交、未入 ratchet 基线;接受新边需用户
作为评审人确认,故**未**生成 `import_graph_baseline.json`,**未**改该测试。本特性新增模块全部
位于既有包内(investor / agent / cli),全量运行未发现方法包代码引入新的包间边。

**他人估值在途改动的门禁残留(按主 Agent 复跑指示由卡 10 修复)**:
卡 10 会话期间,工作区出现他人(非方法包各卡)在途的估值改动(`src/investment_agent/valuation/`、
`tests/test_valuation_calculations.py`),带 2 个 ruff F401 与 1 个 mypy strict 错误;卡 10 首轮
收口时按「不触碰在途文件」处理并如实报告,主 Agent 复跑门禁后指示修复,遂以最小改动收口:
删除 `tests/test_valuation_calculations.py:5` 未用的 `UTC`、`datetime` 导入;
`src/investment_agent/valuation/service.py:520` 的空值守卫对齐本文件既有模式
(同文件 422–424 行 `evidence.value is not None and evidence.value.value is not None`),
运行时行为不变(`materialize` 恒构造 Quantity)。两处修复后 ruff/mypy 全绿,
`tests/test_valuation_calculations.py` 40 个测试全部通过。
另:本轮门禁执行期间,工作区曾被并发的在途改动与全量测试反复重塑(迁移 056 文件与
`tests/test_method_evolution.py` 等在一次全量运行中途被改写,导致该轮迁移类失败;磁盘曾被
并发测试写满引发 ENOSPC),那两轮受污染的全量结果作废;受影响测试(迁移链、`test_alembic_env`、
`test_method_evolution`、`tests/test_agent_company_context`、`test_material_release_audit`、
`test_bank_release_rehearsal`)均已单独复跑验证为通过。

### 已知偏差(工作区代码现实与卡片/设计文档的差异)

1. **`tests/unit/test_cli_methods.py` 为遗留草稿,已由卡 10 改写**。该文件未在任何卡片中命名,
   假定了与卡 09 规格及实现不符的契约(`--directory` 参数、JSON 输出 payload、对
   `typer.BadParameter` 期望 exit 1),卡 10 之前就会让全量门禁失败(修复前首轮全量实测:6 失败
   中 5 个来自它)。卡 10 将其改写为与实现一致的 rich 输出契约,保留其独有场景:仓库级
   `config/methods/` 双包 validate、一次 sync 将同 key 的基线与 expectations 包导入为 v1/v2、
   显式激活 `company-research@2`、激活后 sync 不动 head、同命令幂等 replay。卡 09 指定的
   `tests/unit/test_methods_cli.py` 未改动。
2. **`config/methods/company-research.v2.expectations.yaml` 未在卡片中点名**:该「基线的加强
   候选」示例包已在工作区存在,并被
   `tests/unit/test_method_packages.py::test_expectations_package_extends_baseline_with_a_strengthening_step`
   断言(相对基线仅追加 blocking 的 expectations_gap 步骤,符合 §8 加强偏序)。卡 10 如实记录,
   未另立文件。
3. **环境基线**:pytest 必须带 `API_KEY` 环境变量(全局约束第 4 条;否则
   `tests/test_method_evolution.py` 两个既有测试因 Settings 缺字段失败)。本轮全量在带
   `API_KEY` 下运行,该文件全部通过。

### v1 不变性证据(测试名,全部在全量门禁内通过)

- `tests/unit/test_agent_company_context.py::test_v1_company_research_instructions_are_byte_identical`
  (真实准入路径上 `head == COMPANY_INSTRUCTIONS` 硬相等断言)
- `tests/unit/test_agent_company_context.py::test_v1_review_reuse_candidate_instructions_are_byte_identical`
- `tests/unit/test_method_receipt.py::test_v1_admission_payload_stays_byte_identical`
  (`TaskMethodBinding` payload 形状)
- `tests/unit/test_method_receipt.py::test_v1_non_company_admission_keeps_plain_baseline`
- `tests/unit/test_method_output_schema.py::test_v1_company_study_output_schema_stays_frozen`
- `tests/unit/test_method_output_schema.py::test_v1_company_research_output_schema_stays_frozen`
- `tests/unit/test_method_output_schema.py::test_v1_json_deserializes_into_v1_types_unchanged`
- `tests/unit/test_method_packages_store.py::test_v1_binding_payload_stays_byte_identical`
- `tests/unit/test_method_validation.py::test_v1_task_and_render_only_receipt_skip_package_validation`
- `tests/unit/test_agent_progress.py::test_v1_plan_view_is_unchanged`

### M4 缺口(未实现,如实标注)

包级提案、三态历史回放、候选包离线/前瞻影子、包级批准与回退(设计第 7 节)未实现;
README「完整专题」行、「方法包」小节与设计文档状态行 / §11 均已标注。生产默认仍 v1:
全局默认 head 初始为空,解析回落 legacy `FIXED_METHOD`;进入生产的唯一路径是显式
`ia methods activate-global`(追加 method version 与审批事件,只移动全局默认 head)。
