# 卡 14:候选包离线影子

设计依据:§7.2。前置:卡 12、13。改动:`investor/methods.py` 的 `run_offline_shadow`、
`api/routes/methods.py` 的 `_frozen_progress_replay`(或并排新增候选版 replay 函数)。

## 要求

对包提案(status=shadow)的离线影子:

1. FunctionModel 输入(`agent.run` 的 prompt JSON)在现有 `frozen_progress` 基础上增加:
   `candidate_rendered_method`(候选包经 `render_method` 的渲染原文)、
   `candidate_output_contract`(plan_bindings/step_results 字段说明——服务端固定模板生成,一段
   确定性文字)、`frozen_actions`(该样本的 committed action 摘要:id、tool、committed_sequence)。
   **不是只传三个布尔开关**。
2. replay 函数(路由侧 `_frozen_progress_replay` 的扩展)返回的 progress JSON 仍必须是
   冻结样本里真实存在的内容 + 允许模型补的仅限新元数据字段(plan_bindings/step_results),
   且其中的 action_ids / output_refs 只能引用 `frozen_actions`/冻结 progress 里出现过的
   id/ref(超出即视为模型幻觉,重放函数直接抛 `InvestorError`——由确定性代码拦截,不靠模型自觉)。
3. 不调用工具、不发布、不产生外部副作用、无付费调用(仍 FunctionModel);
   `usage_limits=request_limit=1` 每样本一次(现状保留)。
4. 结果判定:对 FunctionModel 返回的候选 progress 跑**包级 validate_final**(用卡 13 的
   replay_context——frozen actions 即 context.committed_actions,first_plan 取冻结 progress
   首个)。`passed` = 每个样本的包级校验结论与其角色期望一致(failure→被拒、counterexample→通过
   或其不可重算项不涉及);工件 payload 增补 `candidate_shadow: true`、渲染哈希与判定明细,
   `quality` 字段如实标注 offline_FunctionModel_engineering_only。
5. config-only 提案的离线影子路径行为不变。

## 测试(`tests/unit/test_method_replay.py` 或新文件,复用现有 shadow 测试造数)

- 包提案离线影子:输入含渲染指令与动作摘要;模型(桩)返回引用真实冻结 action id 的
  step_results → 判定路径正确;桩返回编造 action id → 确定性拦截,不入库通过。
- config-only 回归:现有 run_offline_shadow 测试原样通过。

## 验收

三门禁 + 目标测试全绿。
