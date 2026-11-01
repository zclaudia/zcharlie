# 任务 B:v2 基线启用验证记录(2026-11-01)

按「先验证后启用」执行。结论:**本轮不在生产启用 v2 全局默认 head**——本地真实运行
显示当前 tier3 模型(`deepseek-flash`)在既有反馈预算内无法稳定满足 v2 输出契约。
机制本身(sync、activate-global、receipt 冻结、包级校验、失败投影)全部按设计工作。

## 已执行步骤与证据

1. **sync**:本地库迁移至 056 后 `ia methods sync` 导入 `company-research@1`(基线)与
   `company-research@2`(expectations 加强候选),内容哈希幂等;`status` 确认 head 未被
   sync 移动(设计 §5)。
2. **测试环境激活**:本地 dev 库(`data/app.db`,空库种子 600519.SH 一只标的)
   `ia methods activate-global --package company-research@1 --expected-revision 0`,
   默认 head revision 1;per-listing head 保持为空。
   注意:要启用的是 `@1` 基线包(现行要求显式化);`@2` 是加强候选示例,不应全局激活。
3. **v2 receipt 冻结(生产入口)**:经会话消息(研究入口 `POST /agent/conversations/{id}/messages`,
   `research_kind="company_research"`)新建任务,`agent_task_method_binding` 冻结
   `schema_version 2` receipt(method_ref 指向 activate-global 的方法版本、package_ref 指向
   基线包工件)。解析顺序(per-listing → 默认 head → legacy)在生产路径生效。
4. **真实模型运行(4 次,deepseek-flash)**:
   - 运行 1:基础设施配置缺失(`agent_max_tokens` 未设 → `finite_output_token_bound_required`),
     非 v2 因素;设置后消除。
   - 运行 2:`method_step_result_premature`——模型把 step_results 混进**首次** save_research_progress,
     修正反馈 1 轮(agent_retries 默认 1)后仍失败。
   - 运行 3:`method_step_required`——直接输出最终 progress,plan_bindings 未覆盖全部 blocking 步骤。
   - 运行 4:`save_research_progress` **首次计划校验通过并提交**(plan_bindings 覆盖完整)、
     `prepare_company_view` 提交,随后因当日预算桶中历史 unknown 计费条目
     (`usage_unknown_reconciliation_required`,本地未配 token 单价所致,非 v2 因素)停止,终态 partial。
   - 运行 5(清理预算桶、配置本地单价、agent_retries=5 后):再次
     `method_step_result_premature` 失败——工具级修正轮被 `min(validation_retries, 2)` 硬顶,
     模型在两轮内未能改正。
5. **失败投影诚实**:每次失败/中断后助手消息明确「研究尚未完成」并复述确定性校验错误,
   无伪造完成、无发布副作用(partial 运行有 prepare_company_view 但未发布正式观点)。

## 通过率判定

契约相关运行 4 次(运行 2/3/4/5):完整走通「首次计划 → 结算」的 0 次;运行 4 通过首次
计划校验但未及结算(预算中断)。**模型侧 v2 首次接触通过率:0/4。** 不满足
「通过率可接受再 activate-global」的前置条件,生产保持 v1。

## 生产启用前需要(任选其一或组合,均为后续工作)

1. **换更强模型档位**重跑同样验证(tier3 为 flash 级;契约遵循对模型指令服从性敏感)。
2. **放宽工具修正轮**:`agent/tools.py` 的 `min(validation_retries, 2)` 硬顶把
   `agent_retries>2` 的增益截断在 2 轮;是否放宽属于代码发布,需单独评审。
3. **renderer v2 模板强化**:对「首次计划只带 evidence_steps+plan_bindings、step_results
   留给最终结算」在固定指令模板中前置强调(模板随代码版本发布,不影响在途 receipt)。

## 回退与在途任务

- 生产未激活,无需回退;本地测试环境的激活只影响本地 dev 库的新任务。
- 若未来激活后需回退:`ia methods activate-global --package company-research@<旧版本>
  --expected-revision <n> --reason …` 即可;历史任务 receipt 与发布 method_ref 不变
  (设计 §7.4/§10,已有测试钉住)。
