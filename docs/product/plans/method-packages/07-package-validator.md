# 卡 07:包级校验器(首次计划 + 最终结算)与 committed_sequence

设计依据:§6.2、§11 M2。前置:卡 01–06。这是核心卡,新逻辑集中在独立模块。

## 前置改动

1. **validator_version 冻结值切换**:`freeze_task`(卡 04)对新建 v2 任务一律
   `validator_version = VALIDATOR_V1`(enforcing)。此前 render-only receipt 只属于历史在途任务,
   代码发布后它们按 receipt 分派保持 render-only(§10)。
2. **committed_sequence 赋值**:在 `ToolAction` commit 路径(`agent/tools.py` 的 write/commit 与
   `agent/tasks.py` 相应函数;先读清楚唯一 commit 收口点)为 v2 任务(receipt 存在)在提交时赋
   `committed_sequence = 同任务现有最大值 + 1`(同事务内 `SELECT max`,SQLite 单写者下安全)。
   v1 任务不赋值(列保持 NULL),**赋值逻辑不得改变 v1 的任何行为**。
   兼容规则:后续校验读到 NULL 视为无序信息(§6.2 not_replayable 语义),不猜顺序。

## 新文件 `src/investment_agent/agent/method_validation.py`

`MethodValidationContext`(dataclass 或 Contract):`binding`、`package: MethodPackageDefinition`、
`receipt: TaskMethodReceipt`、`grant: ExecutionGrant`(冻结副本)、`first_plan: ResearchProgress | None`、
`committed_actions: tuple[dict, ...]`(按 committed_sequence 升序;元素含 operation_id、tool、
parameters、result 的必要字段)、`artifact_refs`。

公开两个入口,抛 `ResearchValidationError`(沿用现有 code/field/message 三元组):

### `validate_first_plan(progress)`

1. `plan_bindings` 的 index 合法(< len(evidence_steps))、method_step_id ∈ 包步骤、
   `(index, method_step_id)` 对唯一 → 违反 `method_plan_binding_invalid`。
2. 每个 blocking step 至少被一个 evidence step 覆盖 → `method_step_required`。
3. 首次计划不得携带 `step_results`(非空即 `method_step_result_premature`)。
4. 额外未映射步骤允许。

### `validate_final(progress)`

1. **计划保持**:最终 `evidence_steps` 文本与 `plan_bindings` 与 first_plan 完全一致
   → 违反 `method_plan_changed`;step result 的 `evidence_step_indices` ⊆ 该步骤在 first_plan
   的绑定 index 集合。
2. **blocking 关闭**:每个 blocking step 恰一个 result(`method_step_required`);
   non-blocking 缺失允许(不报错)。
3. **动作真实性**:result.action_ids 全部属于本任务且 state=committed 且工具名 ∈ 冻结
   grant.tool_names;action-backed 输出(calculation/snapshot_field/peer_context)的引用必须由
   对应工具动作生成或读取(对照 committed_actions 的 parameters/result)→ `method_action_invalid`。
4. **逐步骤输出落地**(不是全局找同类):按 OutputRequirement kind 校验该 result:
   - `plan`:evidence_steps 非空;require_completion_criteria → progress.completion_criteria 非空;
     require_stop_conditions → stop_conditions 非空(与现行 depth 校验等价,重复无害)。
   - `hypotheses`:hypothesis_indices 指向不同假设且数量 ≥ min_count。
   - `counter_refs`:output_refs 中 kind="evidence" 数量 ≥ min_count 且全部 ∈ progress.counter_refs;
     verified_only 时逐项走现行 enforce_steps 的核验路径(已验证、无未解冲突)。
   - `dimension`:progress.dimensions 中该维度达到 minimum_status;allow_missing 规则见 6。
   - `calculation`:output_refs 存在 kind="calculation" 且其方法 == 指定 method(读 SavedCalculation
     payload 的方法标识;先读 `valuation` 注册的 calculation payload 形状确定字段名),引用同时
     出现在 progress 的相应引用集合;action_ids 含生成动作。
   - `snapshot_field`:action_ids 含对该 field 的 committed `capture_snapshot`,且 output_refs 有
     对应已保存 evidence。
   - `peer_context`:action_ids 含 `get_industry_peer_context`;require_calculation 时 output_refs
     的 calculation 绑定任务冻结 peer context(读 calculation payload 的绑定标识比对 grant.peer_context)。
   违反 → `method_step_output_required`(field 定位到步骤 id 与 requirement)。
5. **依赖顺序**:每条 A→B(A in depends_on(B)):A result 的 max(committed_sequence) <
   B result 的 min(committed_sequence);任一侧 action 序列含 NULL 或为空 → 该条判
   `method_step_order_unknown`(计为失败,不得当 pass)。→ `method_step_order_violation`。
6. **带缺口完成**:`completed_with_gap` 仅当步骤 allow_missing=True;必须有 gap_reason,且
   progress.gaps 或 limitations 中存在对应说明;action-backed 要求至少保留一次真实尝试动作。
   缺口不提升维度状态(dimension 仍按实际 status 判)→ `method_step_gap_invalid`。
7. **能力复验**:仅用 receipt.tool_family_members 与冻结 grant,不读实时 head/包。

错误码加入 repairable 白名单:先读 `ResearchValidationError` 定义与 executor 的重试机制,
`method_plan_binding_invalid / method_step_required / method_step_output_required /
method_step_order_violation / method_plan_changed / method_step_gap_invalid / method_step_result_premature`
标记可修复(模型可修正),反馈次数沿用现有 agent_retries 有界机制;`method_action_invalid /
method_capability_unavailable` 不可修复。若 ResearchValidationError 尚无 repairable 概念,加可选
`repairable: bool = True` 字段并让 executor 的反馈路径沿用现状(不新增重试循环)。

## 接入 `validate_progress`(research.py)

现有通用校验之后:`receipt = await task_method_receipt(...)`;若 receipt 且
`validator_version == VALIDATOR_V1`:`final=False` 时(保存路径)首次 committed 计划作为基线
(按 task artifact append order 取最早的 research_progress action,照 `progress.py` 的遍历方式),
若本次即首次 → `validate_first_plan`;`final=True` 时 → `validate_final`(含 first_plan 基线读取;
无 first_plan 的历史在途任务 → 跳过包级校验并记日志,不判失败——它们是 render-only 或
带旧 receipt 的任务)。构建 context 时 committed_actions 查询 `ToolAction where task_id 且
state="committed"` 按 committed_sequence 排序。

## 测试(`tests/unit/test_method_validation.py`)

用现有测试的内存库 + 假 grant 工厂(参考 `test_agent_research.py` 造 admission / grant 的方式)。
必备正反例(§11 M2 验收):

1. 无关 calculation(如 historical_valuation_percentile 的引用)不能满足 reverse_dcf_fcfe 要求 → 拒。
2. 自报 satisfied 无 action_ids / output_refs → 拒。
3. 依赖顺序:A 的动作 sequence 全部 ≥ B 的最小 sequence → `method_step_order_violation`。
4. allow_missing=True 步骤走 completed_with_gap + gap_reason + gaps 说明 + 至少一次尝试动作 → 过;
   allow_missing=False 却交 gap → 拒;gap 无 gap_reason → 拒。
5. 首次计划:blocking 未覆盖 → 拒;携带 step_results → 拒;合法首计划 → 过。
6. 最终计划改了 evidence_steps 文本 → `method_plan_changed`。
7. counter_refs verified_only 路径:未核验引用 → 拒(可复用 enforce_steps 现有测试的造数方式)。
8. v1 任务(无 receipt)与 render-only receipt:包级校验完全不触发(现有测试回归即证)。

## 验收

三门禁 + 新增测试 + `env API_KEY=dummy-test-key-123456 .venv/bin/python -m pytest -q tests/unit/test_agent_research.py tests/unit/test_method_evolution.py`
全绿;v1 行为零变化(现有测试不改断言即通过)。
