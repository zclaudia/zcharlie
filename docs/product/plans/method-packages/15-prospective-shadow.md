# 卡 15:候选包前瞻影子(shadow admission 与真实影子任务)

设计依据:§7.3。前置:卡 12、13(卡 14 可并行但本卡不依赖它)。这是 M4 最重的卡;
先读 `agent/execution.py:admit`、`agent/job.py`(worker 如何取任务)、`agent/context.py`
`reusable_context_sources`、`agent/company_plan.py` 计划复用路径,再动手。

## 1. 发起(authenticated_user,新 route)

`POST /investor/methods/proposals/{proposal_id}/candidate-shadow`
body:`{mutation_id: UUID, prompt: Text(研究问题), expected: "pass" | "block"}`:

- 仅 `status="shadow"` 且 evaluation passed 的包提案;样本须同范围(proposal listing)
  且在评估后新建(校验影子任务 created_at > evaluation created_at,沿用 prospective 惯例)。
- 服务端构建 shadow grant:`mode="background"`、`source="authorized_background"`、
  `research_kind` 取候选包 applicable_kinds 与提案范围交集(company_research 或 review)、
  `scope_refs=(listing,)`、`tool_names` = 当前 registrations() 展开后的工具名
  **减去发布类**(`prepare_company_view`)与 `portfolio` 族(禁发布、禁账本);
  预算沿用既有 daily/task cost 配置。
- `input_key` 幂等(`candidate-shadow:{proposal_id}:{mutation_id}`);走 `admit()` 正常入队
  (worker 真实执行,FunctionModel 不可用于此路径)。

## 2. 冻结候选 receipt

`freeze_task` 增加 keyword-only `override_binding: MethodBinding | None = None`(默认 None,
现行为不变):候选影子任务传入候选 v2 binding(package_ref/hash 来自提案冻结),
receipt 增补两个可选字段(默认 None,v1/v2 普通任务不变):

- `shadow: Literal["candidate_package"] | None`
- `candidate_base_method_ref: ObjectRef | None`(= 提案 old_method,准入时校验相等)

`validator_version=VALIDATOR_V1`(enforcing:影子任务的完成就是包级校验通过)。
能力前置照常检查(影子 grant 的 tool_names 已含研究所需族)。

## 3. 影子边界(逐点核实现有机制,不新造权限系统)

- **无发布**:`prepare_company_view` 不在 tool_names → 模型无法调用;executor 的
  output 检查对影子任务要求最终 progress 过包级校验而非发布(若现有 completion 逻辑要求
  semantic_review/publication,为影子任务走 progress-only 分支——找到 `complete`/`output_check`
  的分派点,按 receipt.shadow 分支)。
- **无通知/账本/用户操作**:这些都不在 agent 工具集(通知由发布/事件触发,无发布即无);
  在测试中显式断言影子任务运行后无 publication 工件、无 InvestorMutation、无 watchlist/case 变化。
- **不参与正式计划复用**:`company_plan.reuse_question_plan` 与
  `context.reusable_context_sources` 排除 receipt.shadow 非空的任务(查这两个函数的选取条件,
  加过滤;用测试钉住)。SavedEvidence 本身可被后续正式任务按正式授权与 cutoff 独立重验——
  不需要额外改动,但测试要证明正式任务引用影子保存的 evidence 仍走正常核验。

## 4. 影子完成与结果登记

新 service 方法 `candidate_shadow_result(request)`(幂等 replay 惯例):读影子任务的
committed progress + actions,构建 replay_context(候选包、冻结 grant)跑
`validate_final`:expected="pass" → 校验须通过(通过=正例,覆盖其满足的全部
`requires_candidate_shadow` 项);expected="block" → 校验须拒绝(反例)。
结果写 `MethodArtifact kind="candidate_shadow"`(payload:proposal_id、task_id、expected、
outcome passed/blocked、覆盖的 requirement ids、渲染哈希),proposal 增补 `candidate_shadow_ids`。
状态机:提案批准前须逐项覆盖(卡 16)。

## 测试(`tests/unit/test_candidate_shadow.py`,复用现有 worker/executor 测试设施)

1. 端到端:造已通过评估的包提案 → 发起 expected=pass 影子 → worker 真实跑
   (FunctionModel 桩按候选输出契约返回合法 step_results,引用真实动作)→ 包级校验通过 →
   candidate_shadow 工件登记,覆盖 requirement ids 正确。
2. expected=block:影子任务最终 progress 缺 blocking 结果 → validate_final 拒 → 反例登记。
3. 边界断言:影子任务无 publication 工件 / InvestorMutation / case-head 变更;
   正式计划复用查询不返回影子任务;正式任务引用影子保存的 evidence 走正常核验。
4. 发起校验:非 shadow 状态提案拒;范围不符拒;重复 mutation 幂等;
   tool_names 确无发布/组合工具。
5. `freeze_task(override_binding=None)` 现行为不变(既有 receipt 测试回归)。

## 验收

三门禁 + 目标测试全绿;`agent/execution.py` 的改动限于 admit 调用链传参与 override 透传。
