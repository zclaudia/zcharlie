# 方法包实现计划(M4:提案、回放与影子)

日期:2026-10-31。依据:[方法包契约设计](../../investor-agent-method-packages.md) §7、§8(M1–M3 已交付)。
执行方式同 M1–M3:逐卡 GLM-5.3-Flash 子 Agent 实现,主 Agent 验收;门禁命令与既有失败处理照
[00-plan](00-plan.md) 全局约束(含 import graph 既有失败 --deselect)。

## 本轮范围

- **§8 包级加强偏序**(纯函数,含 v1↔包比较的 legacy 基线规则)。
- **§7 包级提案**:`CreateProposal` 可选 `package`,config≡discipline、§8 校验、候选内容随提案冻结。
- **§7.1 三态历史回放**:evaluate 按「可重算要求」逐项判 `pass | block | not_replayable`,
  实质要求规则与 `requires_candidate_shadow` 汇总;config-only 提案路径行为不变。
- **§7.2 候选包离线影子**:FunctionModel 输入含候选包渲染指令与输出 schema,只重放冻结内容。
- **§7.3 候选包前瞻影子**:`authenticated_user` 发起、绑定候选包的真实影子任务(shadow admission),
  无发布/通知/账本/用户操作副作用,不参与正式计划复用;completion 以包级校验为准。
- **§7.4 批准与回退**:evidence_hash 绑定包哈希 + 渲染哈希 + 版本 + 评估 + 最新前瞻影子;
  覆盖门槛(requires_candidate_shadow 全覆盖 + 一个预期失败候选被拒);回退支持 v2 包版本。

不做:候选影子的批量/调度运营默认值(§12 开放问题 4)、自动发起或自动批准、付费模型调用
(离线影子仍 FunctionModel;前瞻影子由用户显式发起并占用既有预算)。

## 卡片(顺序执行,续 M1–M3 编号)

| 卡 | 内容 | 设计节 | 主要文件 |
|---|---|---|---|
| 11 | §8 包级加强偏序纯函数 | 8 | `investor/method_packages.py` |
| 12 | 包提案创建与候选冻结 | 7 首段 | `investor/methods.py` |
| 13 | 三态历史回放(evaluate/check_sample) | 7.1 | `investor/methods.py`、`agent/method_validation.py` |
| 14 | 候选包离线影子 | 7.2 | `investor/methods.py`、`api/routes/methods.py` |
| 15 | 候选包前瞻影子(shadow admission) | 7.3 | `investor/methods.py`、`agent/execution.py`、`agent/context.py`、`api/routes/methods.py` |
| 16 | 批准与回退扩展 | 7.4 | `investor/methods.py` |
| 17 | 工作台展示(结构 diff、回放三态、影子覆盖) | 9 | `api/routes/methods.py` |
| 18 | 文档与全量门禁 | 11 | `README.md`、设计文档、00-plan/本文件 |

## 全局约束

同 [00-plan](00-plan.md) 五条,另加:

6. **受保护的并行工作扩大**:用户在途文件还包括 `config/field_sources.yaml`、
   `docs/product/investor-agent-research-policy.md`、`docs/product/investor-agent-valuation.md`、
   `src/investment_agent/evidence/{search,snapshots,source_tiers}.py`、`src/investment_agent/factors/library.py`、
   `src/investment_agent/valuation/**`、`tests/test_{saved_search,investor_publication,valuation_calculations}.py`、
   `tests/unit/test_{field_sources,source_tiers}.py`(只读可,改不行)。卡片确需与之冲突时先报 blocked。
7. **config-only 提案零回归**:`tests/test_method_evolution.py` 既有断言不改不删;
   包路径全部走新增分支。
8. M1–M3 交付的 receipt/validator 分派、v1 不变性测试是本轮的地基,不得改其语义
   (可新增,不可改既有断言;若确有冲突报 blocked 说明)。

## 执行结果(卡 18 收口,2026-10-31)

### 各卡完成情况

八张卡按依赖顺序执行完毕,交付物与测试均在本工作区(未提交);下表为卡 18 收口时对
工作区代码现实的核对:

| 卡 | 交付物 | 测试文件 | 状态 |
|---|---|---|---|
| 11 | `investor/method_packages.py`:`strengthening_violations` / `strictly_stronger_requirements` / `legacy_package_for`(§8 包级加强偏序纯函数) | `tests/unit/test_method_packages.py` | 完成 |
| 12 | `investor/methods.py` `create` / `_freeze_package_candidate`:config≡discipline、`validate_definition`、§8 判定(无违反且至少一项实质增强),包工件 ref / content_hash / rendered_method_hash / old_package_kind 随提案版本冻结 | `tests/test_method_evolution.py::test_package_proposal_*` | 完成 |
| 13 | `agent/method_validation.py` `replay_context` / `replay_requirements` / `replay_sample_state`(逐要求 pass / block / not_replayable)+ `evaluate` 包提案三态编排(失败样本须实质 block;范围内反例 block → failed;`requires_candidate_shadow` 累计) | `tests/unit/test_method_replay.py`、`tests/test_method_evolution.py::test_package_evaluate_*` | 完成 |
| 14 | `run_offline_shadow` 候选分支 + `api/routes/methods.py` 候选重放:prompt 增 `candidate_rendered_method` / `candidate_output_contract` / `frozen_actions`,只许引用冻结样本真实 id/ref(越界确定性拦截),逐样本包级 `validate_final` 判定,`quality=offline_FunctionModel_engineering_only` | `tests/unit/test_candidate_shadow.py::test_package_offline_shadow_*`、`::test_candidate_replay_boundary_rebuilds_frozen_content_and_intercepts`、`::test_route_candidate_replay_and_dispatch_cite_only_frozen_identities` | 完成 |
| 15 | `POST /investor/methods/proposals/{id}/candidate-shadow` + `start_candidate_shadow`(background grant 剥离发布与 portfolio 族)+ `freeze_task(override_binding=…)`(receipt 增 `shadow` / `candidate_base_method_ref`)+ `admit(method_override=…)` 透传 + `candidate_shadow_result`(包级校验登记正反例)+ 影子边界(`research.py` 影子完成分支禁发布、`company_plan.py` 正式计划复用排除影子) | `tests/unit/test_candidate_shadow.py` | 完成 |
| 16 | `decide` 包提案批准(追加 v2 version 工件、只动 per-listing head、同 head 旧 active retired、`MethodDefaultHead` 不触碰)、`_require_candidate_shadow` 覆盖门槛、`_candidate_binding` evidence_hash 绑定(包哈希 / 渲染哈希 / renderer / validator 版本 / 最新评估 / 最新前瞻影子)、`rollback` 复制 v2 目标 binding | `tests/test_method_evolution.py::test_package_approval_*`、`::test_package_rollback_*` | 完成 |
| 17 | `api/routes/methods.py` 工作台只读四块(`candidate` / `structure_diff` / `replay` / `candidate_shadows`,附加在 evidence_hash 之后不碰批准围栏)+ list 概要 `package_key` | `tests/unit/test_method_replay.py::test_workbench_*` | 完成 |
| 18 | README「方法包」小节与「完整专题」行、设计文档状态行 / §11 / §9 实现备注、本节;全量门禁 | 全量三门禁 | 完成(见下) |

### 全量门禁(卡 18 实测,2026-10-31)

- `.venv/bin/ruff check .`:**All checks passed!**(0 错误)。
- `env API_KEY=dummy-test-key-123456 .venv/bin/python -m mypy src/investment_agent`:
  **Success: no issues found in 480 source files**(0 错误)。
- `env API_KEY=dummy-test-key-123456 .venv/bin/python -m pytest -q --deselect
  tests/unit/test_import_graph.py::test_no_new_package_edges_beyond_baseline`:
  **4778 passed / 0 failed / 9 skipped / 7 deselected**,exit 0(44 分 47 秒)。

**既有失败(基线漂移,未修、未放开,按卡片指示 `--deselect` 跳过)**:
`tests/unit/test_import_graph.py::test_no_new_package_edges_beyond_baseline`——处置同 M1–M3
(00-plan 执行结果):既有包间边入 ratchet 基线需用户作为评审人确认,本轮未生成基线文件、
未改该测试;方法包 M4 未新增任何 `src` 模块,全部改动落在既有包(investor / agent / api)
内的既有文件上,未引入新的包间边。

### M4 关键正反例(全部在全量门禁内通过)

- **实质 block 正例**:`tests/test_method_evolution.py::test_package_evaluate_failure_block_requires_substantive_requirement`
  ——历史失败任务被候选实质增强的 `outputs:falsifiable_plan:hypotheses:min_count` block,
  失败要求与 `strictly_stronger_requirements` 有交集,evaluation passed 进 shadow。
- **非实质拒批反例**:`tests/test_method_evolution.py::test_package_evaluate_rejects_non_substantive_failure_evidence`
  ——失败任务恰好满足增强要求,只剩 not_replayable 结构缺失,不构成有效失败证据,
  evaluation failed;`::test_package_evaluate_counterexample_block_fails_evaluation`
  ——范围内反例(好任务)被候选 block,evaluation failed。
- **不可重放不当 pass**:`tests/test_method_evolution.py::test_package_evaluate_dependency_order_is_not_replayable`
  ——历史 action 无 `committed_sequence`,顺序要求判 `not_replayable` 并入
  `requires_candidate_shadow`,绝不当 pass。
- **影子全覆盖门槛**:`tests/test_method_evolution.py::test_package_approval_binds_v2_head_and_freezes_candidate_receipts`
  ——无前瞻影子拒批并点名 `blocking:expectations_gap`;仅 expected=block 反例(无覆盖)拒批;
  正例覆盖集等于 `requires_candidate_shadow` 且 `evidence_hash` 与服务端重算一致才批;
  篡改 `evidence_hash` 拒批(`stale_revision`);批准后 per-listing head 指向提案冻结的 v2 包、
  `MethodDefaultHead` 不动、新任务 receipt 冻结同一包内容;
  `::test_package_approval_refuses_without_block_counterexample`——全覆盖但缺一个被包级校验
  拒绝的 expected=block 候选拒批。
- **影子无发布副作用**:`tests/unit/test_candidate_shadow.py::test_candidate_shadow_has_no_formal_side_effects_or_plan_reuse`
  ——影子任务运行前后 publication / InvestorMutation / InvestorCase / WatchlistItem 计数不变、
  case head 不变;正式计划复用不选取影子任务;正式任务引用影子保存的 evidence 仍走正常授权与
  cutoff 核验。
- **离线影子幻觉拦截**:`tests/unit/test_candidate_shadow.py::test_package_offline_shadow_intercepts_fabricated_identities`
  ——重放边界重建冻结内容,引用冻结样本之外的 id / ref 被确定性代码拦截。

### 已知偏差(工作区代码现实与卡片/设计文档的差异)

1. **卡 15 点名的 `agent/context.py` 未改**:影子排除落在 `agent/company_plan.py` 的
   `reuse_question_plan` 前驱选取处(`receipt.shadow` 非空即排除,
   `test_candidate_shadow_has_no_formal_side_effects_or_plan_reuse` 钉住)。
   `context.reusable_context_sources` 只解析信任构建器写入指令的 JSON——影子任务不产生
   preparation / review 工件、也无 parent 任务链(`recovered_evidence` 走 parent 链),
   结构上进不了正式 reusable_sources / recovered_evidence,无需过滤;设计 §7.3
   「影子产物不进入正式 reusable_sources / recovered_evidence」由结构保证并由上述测试覆盖。
2. **工作台展示为 API 响应块,静态页面未动**(卡 17 明确「不改前端」):结构 diff、回放三态与
   影子覆盖经 `GET /investor/methods/proposals/{id}` 的只读块交付,已在设计文档 §9 加
   「实现备注」,UI 呈现留给后续界面卡。
3. **环境基线**:pytest 必须带 `API_KEY` 环境变量(全局约束第 4 条;否则
   `tests/test_method_evolution.py` 两个既有测试因 Settings 缺字段失败)。本轮全量在带
   `API_KEY` 下运行,该文件全部通过。

### v1 与 config-only 不变性证据(测试名,全部在全量门禁内通过)

- `tests/unit/test_agent_company_context.py::test_v1_company_research_instructions_are_byte_identical`
- `tests/unit/test_agent_company_context.py::test_v1_review_reuse_candidate_instructions_are_byte_identical`
- `tests/unit/test_method_receipt.py::test_v1_admission_payload_stays_byte_identical`
- `tests/unit/test_method_receipt.py::test_v1_non_company_admission_keeps_plain_baseline`
- `tests/unit/test_method_output_schema.py::test_v1_company_study_output_schema_stays_frozen`
- `tests/unit/test_method_output_schema.py::test_v1_company_research_output_schema_stays_frozen`
- `tests/unit/test_method_output_schema.py::test_v1_json_deserializes_into_v1_types_unchanged`
- `tests/unit/test_method_packages_store.py::test_v1_binding_payload_stays_byte_identical`
- `tests/unit/test_method_validation.py::test_v1_task_and_render_only_receipt_skip_package_validation`
- `tests/unit/test_agent_progress.py::test_v1_plan_view_is_unchanged`
- `tests/unit/test_candidate_shadow.py::test_receipt_without_shadow_keeps_exact_payload_shape`
  (非影子 receipt 的两个新字段省略,既有 payload 形状不变)

config-only 零回归:`tests/test_method_evolution.py` 相对 HEAD 的 diff 为 **0 删除 / 539 新增**
(既有断言不改不删);config-only 提案的 create / evaluate / decide / run_offline_shadow 全部
走 `package_ref is None` 的原分支(`methods.py` 逐分支保留),既有用例如
`test_actual_failure_counterexamples_native_shadow_approval_binding_rollback`、
`test_incomplete_evaluation_no_counterexample_no_shadow_and_no_self_activation` 原样通过;
包提案均为新增分支与新增测试(`test_package_proposal_freezes_candidate_and_replays` 等)。

### 范围外(维持不做,如实标注)

按本文件「本轮范围」声明:候选影子的批量 / 调度运营默认值(设计 §12 开放问题 4)、自动发起或
自动批准、付费模型调用(离线影子仍 FunctionModel;前瞻影子由用户显式发起并占用既有预算)
不在本轮;生产默认仍 v1,per-listing 进入 v2 的唯一路径是包提案批准,全局默认仍只经
`ia methods activate-global` 显式启用。
