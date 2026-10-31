# 卡 05:assemble 注入 rendered_method 与 v1 指令快照

设计依据:§6.1 第 3–4 条、§11 M1 验收。前置:卡 04。

## 改动(`agent/research.py` `assemble`,指令切换点约 470–474 行)

- 现行:v1 company 路径 `instructions = COMPANY_INSTRUCTIONS`(review 复用候选时
  `COMPANY_REVIEW_INSTRUCTIONS`)。保持不动。
- 其后追加:v2(`task_method_receipt` 非 None)时
  `instructions = instructions + "\n\n" + receipt.rendered_method`。**v1 不加任何字符**。
- v2 时 `data` 字典可增键 `method_package`(package_key、version、package_hash、validator_version);
  **v1 不新增任何键**(data 序列化进上下文,保持逐字节稳定)。
- 本卡 validator_version 恒为 render-only:receipt 只影响指令,不影响输出类型与校验
  (executor 分派留到卡 06)。

## 测试(`tests/unit/test_agent_company_context.py` 扩展或新增文件)

1. **v1 指令快照逐字节不变**:构造 company_research 普通任务、review 复用候选任务两种 admission
   (照现有测试的 fixture/工厂),断言 `assemble` 返回的指令字符串分别与
   `COMPANY_INSTRUCTIONS` / `COMPANY_REVIEW_INSTRUCTIONS` **完全相等**(`==`,不是包含)。
   若现有测试已覆盖该断言,确认其仍然通过并在报告中指出测试名。
2. v2 任务:指令 == 基线指令 + `"\n\n" + receipt.rendered_method`;`data` 含 `method_package`。
3. 非 company 类型(explanation/local_check/comparison/portfolio)不注入(v2 包只适用 company 类,
   resolve 已挡;此处再加一道断言)。

## 验收

三门禁 + 目标测试全绿;`assemble` 对 v1 的输入输出无任何字节级变化(快照测试是硬断言)。
