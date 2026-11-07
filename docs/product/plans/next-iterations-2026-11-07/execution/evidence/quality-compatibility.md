# QT-003 原方法准入兼容性门槛

日期：2026-11-07。结论：在本次合成材料、现行 v1 基线及 C1 候选上，原提案资格、结构评估、真实原准入、已存取证及方法结算的离线工程门槛通过。不是研究内容质量通过，也不是新实验执行器/read envelope 已完成。

本卡只新增 `tests/integration/test_quality_method_compatibility.py` 和本证据文件。未修改正式业务代码、能力白名单、原预算检查、默认方法包、公司方法 head 或数据库迁移。

## 先发现并修复的材料阻断

首次真正经原 NumericResolver 运行时，QT-002 v1 的字符串源值被原 snapshot materializer 判为缺失；`verified` 元数据并不能使 value=null 成为可用反证。原任务正确拒绝，安全错误为 `research_reference_validation_failed`。此前仅比较同一 materializer 输出的合同测试漏检了这个实质问题。

主代理授权的 QT-002 回修已将源值改为 strict 整数并封存 `research-quality.synthetic.v1.1`，更换 case/snapshot 精确身份，解决同公司/期间源版本唯一键冲突；原 save/get/NumericResolver 对54条材料逐项比值通过，三个反例确有负经营现金。旧失效 hash、修复边界及限制保留在 `dataset/README.md`。本卡最终直接使用修复后的冻结 recipe，没有在兼容性 harness 内转换或替换材料绕过此问题。

固定输入：

- dataset hash：`44b0bfef53cc7ede22eb23556b9dfc1976993126ff6896c54cbc5c2a782fc5c1`
- development hash：`124a2c70b01cdf08ffedab1a363d1ca10484370608a56dd0566e6a741da4fb1d`
- holdout hash：`2a03514b42e1ec7f21bcac4d816979154f3627e27f093574663dc40e681bbae2`
- C1 内容 hash：`9c522f374aeb428d893f1437c8a0120cf1a94d858611325c7e7f2f8909b538a4`
- C1 作者文件 SHA256：`7f97c19d116f67b5765cca10783966836ccfb1d2d33376f89d02572b0a0fc5a4`

## 实际调用链与证据

| 边界 | 本卡实际执行与断言 |
|---|---|
| 来源 | 临时文件 SQLite 内建立合成 Listing/FinancialSnapshot；通过原 EvidenceService.save_evidence 重新校验快照字段并保存，再 get_evidence 验证 status=ok、verified、数值已知。未手写 SavedEvidence/成功 Task/过程复盘。 |
| 旧失败资格 | 原 ConversationService.submit → admit → ResearchWorker/AgentExecutor；真实保存计划、读取证据后，未形成正式可交付观点，任务 partial/evidence_blocked。原 MethodService.process_review 冻结已提交输出与动作。 |
| 合格反例 | 同一范围的另一个原完整研究任务，经原准备/发布与 mock semantic 输入形成 succeeded/answered；另外执行一个范围外 succeeded explanation。其原 process_review 作为结构评估反例。设置中的三种任务均使用同一投资者模型配置。 |
| 提案/评估 | 原 MethodService.create 使用上述失败 review；不匹配 old_method 实际拒绝。未评估提案不能发起 shadow，Task 数不变。原 evaluate 验证失败、同范围反例及范围外反例后才进入 shadow。没有手填 evaluating/shadow 状态或提案授权证据。 |
| 真实准入 | 原 start_candidate_shadow → _admit_candidate_shadow → admit → freeze_task；同 mutation 重放返回原 Task。检查 v2 enforcing receipt、validator_version=1、C1 精确 package hash、原 mandate 与公司 scope；没有 mock admit。 |
| 计划与读证 | FunctionModel 经真实工具循环保存映射原三步骤的 MethodPlannedProgress，调用 get_evidence 读取同一条已核验已存证据；工具动作真实 committed。断言没有 capture_snapshot、刷新或发布动作。 |
| 正控结算 | 最终三条 satisfied step_results 只引用本 Task 已提交动作及已读证据；原最终方法验证与 completion:progress 结算后 succeeded/answered。candidate_shadow_result 再次从冻结 receipt/grant/动作构建上下文并验证 passed/matched。 |
| 缺反证反控 | 同样先保存计划并真实读证，最终故意删去 counter_refs。原错误码 method_verified_counter_required；任务实际 partial/failed，未有 completion:progress。原结果登记为 blocked/matched，不把错误输出记成功。 |
| capture 能力反控 | 原 save_package 保存另一个明确要求 snapshot_read/capture 的负控包；使用仅 save_research_progress/get_evidence 的服务端 grant 调用真实 admit，原 family 展开/receipt 检查以 method_capability_unavailable 拒绝。外层回滚后 Task 数不变；没有伪造 capture/action 或删包要求。该负控验证准入能力接缝，不冒充此负控包已获得提案资格。 |
| 费用与副作用 | 原 BudgetService 未被替换、释放、重置或绕过；所有实际观察用量在原条目 settled，没有 unknown/released。读取 AgentModelRun.config_version 验证五个 native mock 任务的模型配置一致。C1 正/反控前后 PublicationArtifact/Event/Outbox 计数不变，MethodHead/DefaultHead 始终0，原 resolve_binding 保持 FIXED_METHOD。 |

合法提案资格准备需要一个真实成功的旧方法反例，因此隔离库的 setup 确实走了原正式发布路径；这是合成测试准备，未接生产库。C1 shadow 自身不产生正式观点、通知或账本动作。未调用 decide/activate，不把影子通过当作自动方法启用。

预算采用现有 `tests.support.conversations.base` 的固定测试配置（task 200000 tokens、daily 500000 tokens，原请求/工具/期限检查照常），仅为原背景 shadow 打开已有背景开关并在原 mandate 明确授权 scope。没有提高运行中的额度、修改预算规则或手动释放旧条目。FunctionModel usage 是合成审计量，不是实际付费成本。

## 验证命令与结果

```sh
NEXT_ITERATIONS_OFFLINE=1 PYTHONPATH=tests/support .venv/bin/python -m pytest -p next_iterations_offline tests/integration/test_quality_method_compatibility.py -m 'not network' -q
.venv/bin/python -m ruff check tests/integration/test_quality_method_compatibility.py
```

结果：1 个综合集成测试通过；内部实际执行5个原 native mock Task、原取证/结算与多个拒绝边界，未启动全应用或真实浏览器。ruff 通过。环境：pydantic-ai 2.31.1、pydantic 2.13.5。测试文件 SHA256：`5c7c336ee4a259ad27a3e48df1a4c3bc182f48bdf5a1d4341ed6b09cd7ef24d0`。

## 后续边界与未验证项

原 candidate shadow grant 仍使用原流程的工具集合；本卡通过 committed 动作证明未采集，并以单独原准入负控检验收紧工具授权时的能力拒绝。没有声称本卡已实现 envelope membership、禁止当前公司上下文注入、两臂原子 start、实验额外硬预算或独立 quality_result 完成器；这些由 QT-014 至 QT-035 验证。

缺反证错误当前被现有 executor 记录为 partial/failed，同时保留准确 method_verified_counter_required 错误码。本卡不改原分类语义，后续页面/报告必须同时展示具体错误和停止原因，不能将其简化成研究成功。

真实 old_method 如不是本次 v1 基线，仍须独立预检匹配与能力，不得移动 head 凑实验。真实提供商、原件完整人工核验、研究内容质量、付费成本、生产恢复与真实 E2E 均未验证。十二情境尚未执行真实研究评测；本次仅使用其中一个正常情境的合成来源做方法工程兼容验证。
