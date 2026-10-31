# 卡 04:v2 receipt、固定渲染器与准入校验

设计依据:§4.3、§6.1。前置:卡 01–03。

## 新文件 `src/investment_agent/agent/method_receipt.py`

1. 常量:`RENDERER_VERSION: Literal["1"] = "1"`、`VALIDATOR_RENDER_ONLY = "render-only"`、
   `VALIDATOR_V1 = "1"`。
2. `TaskMethodReceipt(Contract)`:`schema_version: Literal[1]`、`binding: MethodBinding`(v2 形状)、
   `rendered_method: str`、`rendered_method_hash: str`、`renderer_version: str`、
   `validator_version: str`、`tool_family_members: dict[str, tuple[str, ...]]`
   (dict[str, list[str]] 的 JSON 兼容形态亦可,入库 JSON 化)。
3. `render_method(package: MethodPackageDefinition) -> str`:固定模板,按步骤顺序输出;每步包含
   步骤 id、instruction_kind 固定文案、depends_on(「must complete before this step」)、
   required_outputs 的确定性描述(min_count、dimension、minimum_status、calculation method、field 等)、
   blocking 与否、allow_missing;**不拼接** `description` 与 `title`。模板文案用英文(与现行指令一致)。
   开头一行固定:`Method package <package_key> (renderer 1):` + 步骤纪律说明
   (blocking steps must be planned first and satisfied or completed-with-gap before final answer)。
4. `build_receipt(binding, package_definition, *, validator_version) -> TaskMethodReceipt`:
   渲染 + sha256(rendered_method) + `tool_family_members`(对包内全部
   `required_capabilities` 并集,经 `resolve_family_members` 展开,按族分组)。
5. `parse_task_method_payload(payload: dict) -> tuple[MethodBinding, TaskMethodReceipt | None]`:
   payload 含 `"receipt"` 键 → v2;否则按现行 `MethodBinding.model_validate` 原样解析(向后兼容)。

## `freeze_task` 改造(`investor/methods.py`)

签名不变。解析 binding 改用第 5 节顺序(`resolve_binding`)。v2(即返回的 binding 带 `package_ref`)时:

1. `package_by_ref` 读包工件,重算 definition 的 `canonical_hash` 与 `binding.package_hash` 比对,
   并校验 `binding.config == package.discipline`;不一致抛 `InvestorError("method_package_corrupt", ...)`
   (fail closed,§4.3)。
2. `applicable_kinds` 含 research_kind、`validate_definition` 复验通过。
3. 能力前置:receipt 的全部工具名必须是本任务冻结 `ExecutionGrant.tool_names` 的子集
   (调用处在 `agent/execution.py:332`,grant 在作用域内;若不在,把 grant.tool_names 传入 freeze_task,
   改动最小化)。缺失抛 `ExecutionDeniedError("method_capability_unavailable")`(准入失败,不降级)。
4. `TaskMethodBinding` payload 存 `{"schema_version": 2, "receipt": receipt.model_dump(mode="json")}`。
   v1 路径 payload 形状与现行**完全一致**(直接 binding dump,不加新键)。

## 读取方

`task_method()` 改为经 `parse_task_method_payload`(v1 行为不变);新增
`task_method_receipt(session, task_id) -> TaskMethodReceipt | None`。
本轮 v2 validator_version 一律 `VALIDATOR_RENDER_ONLY`(M2 的卡 07 才引入 enforcing 分派)。

## 测试(`tests/unit/test_method_receipt.py` 新文件 + `test_method_evolution.py` 扩展)

- v1 任务 `TaskMethodBinding` payload 逐字节等于现行形状(构造 admission,断言 JSON 相等)。
- v2(手工把 default head 指向已 save_package 的基线包后走 admit):receipt 四要素齐全、
  rendered_method 含步骤 id 且不含 description/title;同一包渲染确定(两次相等)。
- 包哈希被篡改 / config 不一致 → fail closed;能力缺失(grant.tool_names 摘掉 valuation)→
  admission 拒绝;applicable_kinds 不符 → 拒绝。
- `parse_task_method_payload` 对旧 payload 的兼容。

## 验收

三门禁 + 目标测试全绿;`agent/execution.py` 的改动仅限 freeze_task 调用点传参。
