# 卡 08:PlanView 方法覆盖投影

设计依据:§9。前置:卡 06、07。

## 契约(`agent/contracts.py`)

- `PlanStep` 增 `method_step_ids: tuple[str, ...] = ()`(多对多;默认空,v1 不变)。
- 新 `MethodCoverageStep(Contract)`:`method_step_id: str`、`blocking: bool`、
  `status: Literal["uncovered","planned","satisfied","completed_with_gap","missing"]`。
- 新 `MethodCoverage(Contract)`:`package_key: str`、`version: int`、`content_hash: str`、
  `discipline: MethodConfig`、`steps: tuple[MethodCoverageStep, ...]`(包内**全部**步骤)。
- `PlanView` 增 `method: MethodCoverage | None = None`。

## 投影(`agent/progress.py` `_plan_view`)

仅 v2 任务(receipt 存在):按 task artifact append order 找**首次** committed research_progress
(卡 07 已有同型遍历,抽公共助手复用)取 plan_bindings;最终结果取**最新** committed progress 的
step_results(含非终态任务最近一次)。覆盖状态推导(uncovered blocking 也要显示,§9):

- 有 result 且 satisfied / completed_with_gap → 对应状态。
- 无 result:在 first_plan bindings 中被覆盖 → `planned`;任务终态(succeeded/partial/failed)后
  仍无 result 的 blocking → `missing`;非 blocking 未覆盖 → 不强标 missing,按 planned/`uncovered`
  规则同样处理(blocking=false 且无绑定 → `uncovered`)。
- `PlanStep.method_step_ids`:渲染 steps 时,对每个 evidence step 收集映射到它的 method_step_id
  (从 plan_bindings);v1 恒空 tuple。
- v1 任务:`method=None`、`method_step_ids=()`,现有 step state 与 `extra_completed` 逻辑**一行不改**。

## 测试(`tests/unit/test_agent_progress.py` 扩展)

- v1 任务 PlanView 与现行完全一致(现有测试即回归;如无则补一个含 method=None 断言)。
- v2:构造 first_plan(覆盖部分步骤)+ 最终 step_results(部分 satisfied、一个 gap、
  一个 blocking 无 result 且任务 failed)→ 断言各步骤状态、method_step_ids、method 块字段
  (package_key/version/hash/discipline)正确;未进计划的 blocking 步骤显示 uncovered(任务运行中)。

## 验收

三门禁 + 目标测试全绿;v1 投影路径 diff 为零行为变化。
