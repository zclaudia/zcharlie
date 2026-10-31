# 卡 06:v2 输出 schema:plan bindings 与 step results

设计依据:§4.4。前置:卡 04(v1 输出 schema 必须不动,故用子类型而非扩展现有模型)。

## 契约(`agent/research.py`,紧邻 `LocalResearchProgress` 的现有模式)

```python
class MethodPlanBinding(Contract):
    evidence_step_index: int  # ge=0
    method_step_id: str

class MethodStepResult(Contract):
    method_step_id: str
    evidence_step_indices: tuple[int, ...] = ()
    status: Literal["satisfied", "completed_with_gap"]
    action_ids: tuple[UUID, ...] = ()
    output_refs: tuple[ObjectRef, ...] = ()
    hypothesis_indices: tuple[int, ...] = ()
    gap_reason: Text | None = None
```

- 新 progress 子类型:继承现行 company 研究 progress 类型(先读 `executor.py:255-266` 确认
  `CompanyStudyOutput`/`CompanyResearchOutput` 的 progress 字段类型,继承**那个**类型),新增
  `plan_bindings: tuple[MethodPlanBinding, ...] = ()`、`step_results: tuple[MethodStepResult, ...] = ()`
  (普通字段,进 JSON schema;不使用 SkipJsonSchema)。
- 对应 output 子类型(progress 字段换成上述子类型;其余字段照抄父类,注意 `CompanyStudyOutput` 与
  `CompanyResearchOutput` 若都需要则分别子类化,或仅对 review 路径需要的类型子类化——以
  executor 现行分派为准,两路都要能走 v2)。

## executor 分派(`agent/executor.py:255-266`)

在现行 research_kind 分派之后:取 `task_method_receipt`;若 receipt 存在且
`receipt.validator_version == VALIDATOR_V1`(enforcing)且 research_kind ∈ {company_research, review}
→ output_type 换为 v2 子类型。receipt 为 render-only 或不存在 → **维持现行分派完全不变**。
(本轮卡 04 建 receipt 时恒为 render-only,本卡只是把分派机制与常量接好;卡 07 起将新 v2 任务
标为 enforcing——由卡 07 决定冻结时 validator_version 的取值改为 VALIDATOR_V1。)

## validate_progress 兼容(`agent/research.py`)

`validate_progress` 接收的 progress 可能是 v2 子类型实例;本卡只保证**现有校验对 v2 实例照常工作**
(子类型多出的字段被忽略),包级校验在卡 07 接入。`save_progress` / `ResearchProgress.model_validate`
对 v1 输出行为不变(v1 JSON 无新字段,校验结果不变)。

## 测试

- v1 输出 schema 快照:`CompanyStudyOutput`/`CompanyResearchOutput` 的
  `model_json_schema()` 在本卡前后不变——直接把 schema JSON 写进测试断言(golden 字符串),
  防止子类化无意改动父类。
- v2 子类型 schema 含 plan_bindings/step_results;v1 JSON 反序列化进 v1 类型不受影响。
- executor 分派:render-only receipt → 现行类型;enforcing receipt(测试内手工造)→ v2 类型。
  照 `test_agent_research.py` 现有 executor 测试模式。

## 验收

三门禁 + 目标测试全绿;`git diff` 中现有输出契约类型只有新增文件旁路的子类,无父类字段改动。
