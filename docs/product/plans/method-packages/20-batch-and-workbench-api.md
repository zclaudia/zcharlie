# 卡 20:批量发起候选影子 + 工作台 API 增补

设计依据:§9、§12 开放问题 4。前置:卡 19。全部为**新增分支**,config-only 与既有
包提案行为零回归(卡 17 测试钉死的 detail 键集合不得破坏)。

## 1. 批量发起(`POST /investor/methods/proposals/{proposal_id}/candidate-shadow-batch`)

- 契约(放 `investor/methods.py`,与 `CandidateShadowRequest` 并列):
  `CandidateShadowItem(prompt: Text(max 2000), expected: "pass"|"block")`;
  `CandidateShadowBatchRequest(mutation_id: UUID, proposal_id: UUID,
  items: tuple[CandidateShadowItem, ...] = Field(min_length=1, max_length=4))`,
  items 内 prompt 不得重复。
- 服务层 `start_candidate_shadows(request, *, base)`:
  - 从 `start_candidate_shadow` 中提取共享的准入校验 + grant 构建 + admit 逻辑为
    私有协程(逐条复用:状态/评估校验、候选冻结、shadow grant、`freeze_task`
    override、`admit`),单发起路径改为调用它,**行为与现 HEAD 一致**。
  - 一次 `method_transaction` 内逐条 admit:`input_key = candidate-shadow:{proposal_id}:{mutation_id}:{index}`;
    每条各自保存 `candidate_shadow_claim` 工件(proposal_revision、evaluation_id、
    task_id、expected、prompt、research_kind、quality 同单发)。
  - 预算上限(卡 19):已有 claim 数 + 本批条数 > `max_per_proposal` 即整批拒绝。
  - 幂等:整批一个 `InvestorMutation`(mutation_id),replay 返回原结果;
    receipt 形如 `{claims: [...], task_ids: [...], status: "queued"}`。
- 路由 body 用 `CandidateShadowBody` 同型 wrapper(path 绑 proposal_id),
  `ExecutionDeniedError → 409` 惯例与单发一致。

## 2. 工作台只读增补(`api/routes/methods.py`)

- `list_methods` 响应新增 `default_head`:`{"revision": r, "binding": payload}` 或
  `None`(读 `MethodService.default_head("company-research")`,照 heads 投影惯例);
  现有键全部保留。
- `proposal_detail` 对**包提案**在四块之后新增 `candidate_shadow_claims`:
  读该提案 `kind="candidate_shadow_claim"` 工件(按 artifact id 升序取最新
  `WORKBENCH_SHADOW_LIMIT` 条),每条投影 `{task_id, expected, prompt(截 200 字),
  created_at, registered}`;`registered` = 该 task_id 已出现在已登记的
  `candidate_shadow` 工件中。**config-only 提案 detail 键集合保持卡 17 断言不变**
  (`test_workbench_config_only_detail_shape_and_list_summary_stay_unchanged` 必须原样通过)。

## 3. 提案创建的 YAML 入口(路由层)

`POST /investor/methods/proposals` 改为接收路由级 body(新契约 `CreateProposalBody`,
字段同 `CreateProposal`,另加 `package_yaml: Text | None = None`,且 `config` 可选):

- `package_yaml` 缺省:与现 HEAD 完全一致(config-only 或 JSON package,零变化)。
- `package_yaml` 存在:`yaml.safe_load` 解析(失败→`InvestorError("invalid_reference")`,
  FastAPI 422 路径照 offline-shadow 惯例);`config` 省略时以解析出的 `discipline`
  填充;两者都给则走既有 config≡discipline 校验。随后 `CreateProposal.model_validate`
  进入既有 `svc.create`。服务层契约不动。

## 测试(`tests/unit/test_candidate_shadow_ops.py` 续)

1. 批量:2 正例 + 1 反例一次发起 → 3 个 task、3 个 claim、各自 input_key 不同;
   同 mutation replay 幂等;`candidate_shadow_result` 可逐条登记。
2. 批量预算:cap=3 时发起 4 条整批拒绝,库里无新增 claim。
3. `list_methods` 含 `default_head`(空库为 None;activate-global 后有值)。
4. 包提案 detail 含 `candidate_shadow_claims`(registered 标记正确、prompt 截断);
   config-only detail 键集合不变(复跑卡 17 回归断言)。
5. `package_yaml` 创建包提案成功(config 省略时由 discipline 填充);坏 YAML 422。

## 验收

三门禁 + 目标测试全绿;`tests/unit/test_method_replay.py` 既有断言零改动。

## 执行结果(2026-11-01)

完成并提交(d99e54b)。单发路径重构为共享 `_candidate_shadow_target` / `_admit_candidate_shadow`
(input_key 与行为逐字不变);`start_candidate_shadows` 一次事务整批 admit、整批计数上限、单 mutation
幂等。路由:批量端点(ExecutionDeniedError→409)、`list_methods.default_head`、包提案 detail 的
`candidate_shadow_claims`({items,total,truncated} 包装,config-only 键集合钉死不变)、
`CreateProposalBody` 的 `package_yaml` 入口(package 与 package_yaml 互斥;可解析但不符定义的 YAML
统一 422)。6 个新测试全过;既有测试零改动。
