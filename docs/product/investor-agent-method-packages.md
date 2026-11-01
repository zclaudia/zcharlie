# 方法包：研究步骤的版本化契约

日期：2026-10-30。状态：M1–M3 已实现（2026-10-30）；M4 已实现（2026-10-31）。本文定义「方法包」（method package）：把公司研究
方法从「全局静态提示词 + 三个布尔加强开关」扩展为版本化、可审阅、可回放的结构化步骤定义，并给出与
现有方法生命周期、任务准入、研究结算和进度展示的集成契约。

产品规则以[主设计](investor-agent-design.md)、[研究与运营设计](investor-agent-research-policy.md)
（特别是「记忆与研究方法改进 → 从错误到方法版本」一节）、[领域契约](investor-agent-contracts.md)为准。
方法包是**方法版本（method version）的内容载体**；审批、影子、批准和回退仍复用现有生命周期，但包级提案
需要候选包实际参与影子执行，不能只用旧方法任务的输出冒充候选包证据。

## 1. 现状与问题

当前「方法」由三层组成：

| 层 | 实现 | 版本化 | 可审计性 |
|---|---|---|---|
| 全局研究指令 | `METHOD_INSTRUCTIONS`；公司研究实际由 `assemble` 切换到 `COMPANY_INSTRUCTIONS` 或 `COMPANY_REVIEW_INSTRUCTIONS` | 随代码发布 | 只能靠代码 diff 追溯 |
| 方法版本 | `MethodBinding` = `method_ref` + `MethodConfig`（三个布尔加强开关）+ 适用证券 | append-only 工件 + per-listing head | 完整：提案→评估→影子→批准→回退 |
| 研究计划 | 模型每次自拟 `evidence_steps` / `completion_criteria` / `stop_conditions`，仅形状校验 | 随任务保存 | 只保证「有计划」，不保证「计划符合方法」 |

问题：

1. **步骤性要求不可表达**。方法提案只能动三个开关；「价格维度评估前必须先尝试反向 DCF」「同业结论必须
   绑定同业上下文」这类步骤顺序与依赖，目前只能写进全局提示词或代码评审笔记。
2. **同一问题两次研究的步骤可以完全不同**。现有结算核对类型、引用与纪律开关，但不比较计划与方法。
3. **PlanView 与方法无关联**。现有投影按计划／完成文本的归一化相等判断状态，进度页无法可靠回答「这次
   研究按方法完成了哪些步骤、缺了哪步」。
4. **提示词事实上承载了方法定义**，却没有方法版本生命周期。方法改进想改变步骤，只能发版。

## 2. 目标与非目标

目标：

- 方法包是**数据**：结构化字段、内容哈希、版本化、人工审阅后生效；模型可以提出候选和映射计划，不能批准、
  替换或扩大方法权限。
- 复用现有闸门与生命周期：准入冻结、结算校验、提案—评估—影子—批准、回退。
- 确定性校验证明的是「指定计划、已提交工具动作和输出引用之间的结构关系」，不声称机械证明研究语义质量。
- **v1 任务迁移零行为变化**：有效指令、输出 schema、结算结果、计划复用和发布 provenance 均保持原样。
- v2 基线包只把现有研究要求显式化；它新增的 plan binding／step result 是新的提交契约，不冒充旧输出天然
  满足 v2。

非目标：

- 包不能授权工具、修改计算公式、发布门槛、预算或来源规则；这些仍属代码与配置发布。
- 包不携带任意自由文本提示词。包内说明文字只供用户审阅，模型指令只能由服务端固定模板按结构化字段渲染。
- 不做运行时自动选包；任务用哪个包由准入时解析的方法 head 决定，模型与用户消息都不能中途换包。
- 不用历史重放声称候选模型会主动遵守新方法，也不把离线 FunctionModel 输出当作语义质量或投资收益证据。

## 3. 权威与版本模型

方法版本和包内容分层，避免多个字段同时成为权威：

- **Method package**：不可变研究内容，回答「步骤和要求是什么」。
- **Method version**：不可变审批版本，回答「哪个证券／全局默认在什么批准后使用哪个包」。现有 `method_ref`
  继续指向 method version，发布 provenance、计划复用和回退仍使用它。
- **Task method receipt**：任务准入时的不可变执行快照，回答「这次任务实际看到什么指令、用哪版校验器和工具族
  映射」。
- **MethodHead / MethodDefaultHead**：分别保存 per-listing 和全局默认的当前 method version；head 可变且有
  revision，工件不可变。

`FIXED_METHOD` 永远保留为 legacy v1 基线，不改指到包工件。无 `TaskMethodBinding` 的历史任务、无全局默认
head 的 legacy 解析，以及多标的 / comparison / portfolio 任务继续使用 v1 基线。

## 4. 契约定义

### 4.1 MethodPackageDefinition 与 MethodPackage（schema v1）

YAML 和提案提交的是 `MethodPackageDefinition`；入库后形成 `MethodPackage` 工件：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | `Literal[1]` | 包 schema 版本 |
| `package_key` | 稳定短标识 | 同一逻辑包的版本链，如 `company-research`；包内唯一且不可借改名保留旧版本 |
| `applicable_kinds` | 非空 `ResearchKind` 子集 | 首版仅 `company_research` / `review`，不得重复 |
| `steps` | `tuple[MethodStep, ...]` | 至少一条 blocking 步骤；id 唯一；依赖无环 |
| `dimensions` | `tuple[DimensionRequirement, ...]` | 七个维度恰好各一条；省略时使用现状兼容默认值 |
| `discipline` | `MethodConfig` | 吸收现有三开关，语义不变 |
| `description` | 有界 Text | 仅供用户审阅；参与内容哈希，**不进入模型指令** |

入库工件另含 `artifact_id: UUID`、`version: int >= 1`、`content_hash: sha256`、来源、关联提案和
`evidence_hash`。版本按 `package_key` 单调分配；同 `(owner, package_key, content_hash)` 重复导入返回已有
工件，新内容才追加新版本。

`content_hash` 对 canonical definition 计算，包含 `description`，排除 `artifact_id`、`version`、创建时间、
来源、提案和审批等生命周期字段。YAML 中的 version 仅供参考，以存储分配为准，照 `rule_template` 的 canonical
hash / sync 模式。

`DimensionRequirement`：

| 字段 | 类型 | 现状兼容默认值 |
|---|---|---|
| `dimension` | `Dimension` | 七个维度恰好各一条 |
| `minimum_status` | `missing < qualitative < evidenced` | `missing`，即不新增最低状态要求 |
| `allow_not_applicable` | bool | `true`；仍须符合现有 reason 规则 |
| `allow_missing` | bool | `false`；仅在 `minimum_status > missing` 时有额外作用 |

现有「七维恰好各一次、状态三选一、不适用需要理由」始终由基础契约校验。`allow_missing=true` 只允许以明确
缺口关闭该要求，不能把 missing 当成 evidenced。

### 4.2 MethodStep

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | 稳定短标识 | 计划映射、执行结果和结算校验的键；包内唯一 |
| `title` | 有界 Text | 用户界面标题；不进入模型指令 |
| `instruction_kind` | 固定枚举 | `falsifiable_plan` / `counter_analysis` / `dimension_assessment` / `snapshot_capture` / `calculation` / `peer_context` / `expectations_gap`（renderer v2 新增）；选择服务端固定指令模板 |
| `blocking` | bool | true：首次计划必须覆盖，最终必须 satisfied 或符合条件的 completed_with_gap |
| `depends_on` | `tuple[str, ...]` | 必须先完成的同包步骤 id；组成 DAG，不得自依赖或循环 |
| `required_capabilities` | `tuple[ToolFamily, ...]` | 准入能力前置条件，不是工具授权，也不表示实际调用 |
| `required_outputs` | `tuple[OutputRequirement, ...]` | 有类型的输出要求，按下面的判别联合校验 |
| `allow_missing` | bool | 是否允许「带缺口完成」；默认 false |

工具族沿用固定枚举（`snapshot_read`、`evidence`、`valuation`、`peer`、`disclosure`、`document`、
`portfolio`），族划分随代码发布。`required_capabilities` 只表示「若执行此包，任务原本就必须已经拥有这些
工具」；它不扩大 `ExecutionGrant.tool_names`，也不是 allowed-tool 白名单。实际动作是否满足步骤由 action 和
output 校验决定。

`expectations_gap` 步骤表达「变异认知」纪律：方向性价格判断前必须对比价格隐含预期（反向 DCF／历史分位
计算输出）与已取证的预期，说明差异及何种证据能收窄；说不出差异时价格判断保持 balanced／undetermined。
它的结构要求由现有 `calculation`（通常 `reverse_dcf_fcfe`）与 `dimension`（`price_expectations`）组合
表达，不新增输出要求种类；`allow_missing=true` 时反向 DCF `not_applicable` 可带缺口完成，即「降级而非
硬写」。指令模板属于 renderer v2；冻结 renderer v1 的在途 receipt 不受影响。

`OutputRequirement` 是带 `kind` 的判别联合，不允许只写无参数的 `calculation` 或 `snapshot_field`：

| kind | 参数 | 确定性含义 |
|---|---|---|
| `plan` | `require_completion_criteria` / `require_stop_conditions` | 复用现有完整研究计划必填校验 |
| `hypotheses` | `min_count` | 对应 step result 的 `hypothesis_indices` 指向足够数量的不同假设 |
| `counter_refs` | `min_count`、`verified_only` | 该步骤的 `output_refs` 中至少有指定数量的反证，且全部包含于 `progress.counter_refs`；需要时逐项已核、无未解冲突 |
| `dimension` | `dimension`、`minimum_status` | 指定维度达到最低状态；带缺口规则见 6.2 |
| `calculation` | `method: CalculationMethod` | `output_refs` 包含指定方法的 calculation，且引用同时进入相应 progress 引用集合；`action_ids` 包含生成它的已提交动作 |
| `snapshot_field` | `field: SnapshotField` | `action_ids` 包含对该字段的 committed `capture_snapshot`；satisfied 时还须有相应已保存 evidence ref |
| `peer_context` | `require_calculation` | `action_ids` 包含 `get_industry_peer_context`；需要计算时，output ref 必须绑定任务冻结的同一 peer context |

`CalculationMethod`、`SnapshotField` 和工具族成员均由代码注册表定义；包只能引用，不能扩展。

### 4.3 MethodBinding v2 与 TaskMethodReceipt

```python
class MethodBinding(Contract):
    schema_version: Literal[1, 2] = 1
    method_ref: ObjectRef = FIXED_METHOD  # 始终是 method version；不是 package ref
    config: MethodConfig                 # 从 package.discipline 派生的冻结副本
    listing_ref: ObjectRef | None = None
    package_ref: ObjectRef | None = None # v2 指向 kind="package" 的不可变工件
    package_hash: str | None = None
```

约束：

- v1：`package_ref` / `package_hash` 必须为空，payload 形状和现有行为不变。
- v2：两者必填；`method_ref` 指向 `MethodArtifact kind="version"`，version 工件保存该 binding、提案和批准证据。
- `config` 必须与包 `discipline` 完全一致。读取时同时校验包哈希和 config；不一致是存储损坏，fail closed，
  不能任选一个值继续。

v2 任务在 `TaskMethodBinding` 中保存 `TaskMethodReceipt`，v1 继续保存原 binding payload：

| receipt 字段 | 作用 |
|---|---|
| `binding` | 上述不可变 MethodBinding |
| `rendered_method` / `rendered_method_hash` | 准入时生成的固定方法指令段原文及哈希；后续每轮直接使用，不重新渲染 |
| `renderer_version` / `validator_version` | 明确分派到对应实现；M1 render-only 与 M2 enforcing 不混用 |
| `tool_family_members` | 准入时解析的族→工具名快照，后续校验不读取可能已经变化的全局映射 |

### 4.4 计划映射与步骤结果

不使用与 `evidence_steps` 平行、语义不明的 `step_refs`。`ResearchProgress` 在 v2 公司研究输出中增加：

```python
class MethodPlanBinding(Contract):
    evidence_step_index: int  # 从 0 开始；显式解决重复文本和多对多映射
    method_step_id: str

class MethodStepResult(Contract):
    method_step_id: str
    evidence_step_indices: tuple[int, ...]
    status: Literal["satisfied", "completed_with_gap"]
    action_ids: tuple[UUID, ...] = ()
    output_refs: tuple[ObjectRef, ...] = ()
    hypothesis_indices: tuple[int, ...] = ()
    gap_reason: Text | None = None
```

`plan_bindings` 和 `step_results` 均为 tuple。一个 evidence step 可映射多个方法步骤，一个方法步骤也可由多个
evidence step 共同覆盖；同一 `(evidence_step_index, method_step_id)` 不得重复。未映射的补充计划仍允许，
但不能抵消 blocking 覆盖。

现有 `completed_steps` 继续用于人类可读进度，不再作为方法步骤完成的权威。方法状态只由首次计划绑定、最终
step results 和服务端可核验的 actions / outputs 推导。

v1 输出 schema 不暴露这些字段；local check / explanation / comparison / portfolio schema 也不暴露。计划复用时
必须复制 `evidence_steps` 与 `plan_bindings`、清空 `step_results` 和旧结论，并要求完整执行身份
`(method_ref, package_ref, package_hash, renderer_version)` 相同。

## 5. 存储、同步与 head 解析

- **包工件**：`MethodArtifact kind="package"`，append-only，保存 canonical definition、package_key、version、
  content_hash 和来源。
- **方法版本**：`MethodArtifact kind="version"`，保存 MethodBinding、关联提案／回退和 evidence_hash，仍按现有
  方式不可变。
- **per-listing head**：沿用 `MethodHead`，只经现有提案批准或回退移动。
- **全局默认 head**：新增 `MethodDefaultHead(owner, channel, revision, binding)`，首版 channel 仅
  `company-research`。它只影响没有 per-listing override 的新 company research / review 任务。

单证券 company research / review 的解析顺序为：per-listing `MethodHead` → `MethodDefaultHead` → legacy
`FIXED_METHOD` v1。其他研究类型永远使用 v1 基线。

`ia methods validate <file>` 聚合全部 schema、枚举、依赖和引用错误；`ia methods sync` 只校验并导入包工件，
**不移动任何 head**。全局启用或回退使用单独的显式用户操作
`ia methods activate-global --package <key@version> --expected-revision <n>`，追加 method version 与审批事件并原子
移动默认 head；这与「全局方法启用需要用户批准」一致，不新增公开写端点。

## 6. 执行路径与确定性校验

### 6.1 准入与指令

1. `freeze_task` 按第 5 节解析 binding。v2 时验证包存在、hash / config 一致、kind 适用、步骤依赖合法。
2. 用 receipt 冻结的 `tool_family_members` 展开所有 `required_capabilities`，要求其工具名是冻结
   `ExecutionGrant.tool_names` 的子集；缺失即拒绝准入，不静默降级。
3. v1 完全沿用现有指令路径。v2 在公司研究的实际基线指令后追加 `rendered_method`：普通路径使用
   `COMPANY_INSTRUCTIONS`，复用准备候选时使用 `COMPANY_REVIEW_INSTRUCTIONS`；不是只修改未覆盖这些路径的
   `METHOD_INSTRUCTIONS`。
4. 方法段由 `instruction_kind`、depends_on、required_outputs 和 allow_missing 的固定模板生成，包含步骤 id，
   不拼接 `description` / `title`。基础纪律、工具权限和发布规则不被替换。
5. 运行中的 feature gate 仍可隐藏工具，现有权限检查不变；包不能绕过。若因此无法取得必需能力，任务按现有
   边界失败或保留缺口，不在结算时改用另一套实时工具映射。

### 6.2 首次计划与最终结算

首次 committed `save_research_progress` 是方法计划基线，按 task artifact append order 查找，不依赖 SQL 无序
查询。`ToolAction` 为 v2 任务增加任务内单调 `committed_sequence`；历史 action 没有该值时，顺序要求只能判为
`not_replayable`。

首次计划（`final=false`）追加校验：

- binding 的 index 合法、method_step_id 存在于冻结包、映射对唯一；
- 每个 blocking step 至少被一个 evidence step 覆盖；
- 初次计划不携带 step results；额外步骤允许。

最终提交先执行现有通用校验，再执行 v2 包级校验：

1. **计划保持**：最终 `evidence_steps` / `plan_bindings` 与首次计划一致；step result 的
   `evidence_step_indices` 必须是该步骤首次绑定集合的子集。
2. **blocking 关闭**：每个 blocking step 必须有且仅有一个 result；non-blocking 可缺失，缺失只进入覆盖展示和
   limitations，不抵消其他 blocking 要求。
3. **动作真实性**：`action_ids` 全部属于当前任务且 committed；工具仍在冻结 grant 内。calculation、snapshot、
   peer 等 action-backed 输出必须由对应动作实际生成或读取，不能引用无关动作凑数。
4. **输出逐步骤落地**：按 4.2 的类型和参数校验该 result 的 refs / indices，而不是在全局 progress 中任意找
   一个同类输出。所有引用仍须通过现有范围、时点、质量和数值校验。
5. **依赖顺序**：对每条 `A -> B`，取 A result 的最大 action sequence 和 B result 的最小 action sequence，
   要求前者小于后者。参与依赖的步骤必须至少有一个 action-backed output；无动作证据不能声称满足顺序。
6. **带缺口完成**：仅当步骤 `allow_missing=true` 才接受 `completed_with_gap`；必须有 `gap_reason`，并在
   `gaps` 或 `limitations` 中有对应说明。action-backed 要求至少保留一次真实尝试动作；它表示「已尝试但材料／
   适用性不足」，不表示 required output 已产生，也不提升维度状态。
7. **能力复验**：只按 receipt 的冻结 grant 和工具族快照复验，不读取实时 head 或实时包版本。

`method_step_required`、`method_plan_binding_invalid`、`method_step_output_required` 和
`method_step_order_violation` 等模型可修正错误加入 `ResearchValidationError.repairable` 白名单。反馈次数继续
受现有 `agent_retries` 和有界重试机制约束，不新增固定一次或无限重试循环；未修正时沿用现有终态投影，有已
保存产物可进入 partial，无产物可 failed。

## 7. 评估、影子、批准与回退

`CreateProposal` 增加可选 `package: MethodPackageDefinition`，与现有 config-only 提案并存并复用同一生命周期。
包提案的 `config` 必须等于 `package.discipline`；创建时完成 schema 校验、canonical hash 和第 8 节加强判定，
候选内容随 proposal version 冻结。

包级 `check_sample` 改为接收显式 `MethodValidationContext`：候选 binding、包、renderer / validator 版本、
工具族快照、冻结 grant、首次计划、按提交顺序保存的 actions 和 artifact refs。它不得读取当前 head，也不得只
替换 `method_config` 后复用旧任务 binding。

### 7.1 历史确定性回放

历史输出没有新 plan bindings 是预期事实，不能据此判断候选研究要求有效或无效。回放结果增加
`pass | block | not_replayable`：

- 能从已保存 progress / actions / outputs 重算的要求，按候选包确定性校验；旧 action 无 sequence 或缺少必要
  元数据时，相关顺序／动作要求为 `not_replayable`，不得当成 pass。
- 原失败样本必须因提案针对的实质要求而 block；仅仅「旧输出没有新 step result」不算有效失败证据。
- 范围外反例继续使用其原方法并 pass；范围内反例只在全部相关要求可重算时才能 pass，未覆盖项明确列入
  `requires_candidate_shadow`。
- config-only 提案继续使用现有 pass / block 路径，行为不变。

### 7.2 候选包离线影子

离线 FunctionModel 输入必须包含候选包渲染指令、对应输出 schema、冻结 progress / actions 和候选上下文，
而不是只传三个布尔开关。它只能引用冻结样本中的真实 action ids / output refs，不调用工具、不发布、不产生
外部副作用。其结果仍只是工程证据，不能单独支持批准。

### 7.3 候选包前瞻影子

新增 blocking 步骤、plan binding、动作或顺序要求时，必须有**绑定候选包的新真实影子任务**；继续观察旧方法
下创建的任务不能替代它。

- 由 authenticated_user 显式发起，使用独立 shadow admission 和既有预算，模型不能自行发起或批准。
- 任务 receipt 直接冻结候选包和候选渲染指令，工具权限不超过本次用户授权。
- 可保存研究所需的 evidence / calculation / progress；禁止正式观点发布、notification、ledger、用户操作和
  对当前 case head 的修改，也不参与正式计划复用。影子产物带 shadow 标记，不进入正式 reusable_sources /
  recovered_evidence；后续正式任务如读取同一 evidence，仍须按正式授权和 cutoff 独立重验。
- shadow completion 以包级校验和候选输出为结果，不调用正式发布来完成任务。
- 批准前至少有一个正例覆盖全部 `requires_candidate_shadow` 项，并有一个预期失败的候选任务被相应校验拒绝；
  样本仍须同范围且在评估后新建。影子 receipt 另存 `candidate_base_method_ref`，必须等于提案 old_method；
  不再要求影子任务自身仍绑定旧包，因为它的目的就是实际执行候选包。

### 7.4 批准与回退

`evidence_hash` 必须绑定候选 package content hash、渲染指令 hash、renderer / validator 版本、历史评估和
最新前瞻影子。仅 authenticated_user 可批准；批准时追加 method version 并 move head，同 head 旧 active 提案
retired。回退追加新 method version 指向目标包，历史任务 receipt 和发布 method_ref 不变。

## 8. 包级加强偏序

新包 B 相对旧包 A 为「加强」，当且仅当全部成立，且至少一项**研究要求**严格增强；版本号、哈希和展示文案
变化本身不算增强：

| 维度 | 判定 |
|---|---|
| applicable_kinds | B ⊇ A，不得缩小适用范围 |
| discipline | 逐开关不得 true→false |
| 步骤集合 | A 的每个 step id 在 B 中保留；non-blocking 可升为 blocking，不得删除或改名 |
| 固定语义 | 同 id 的 instruction_kind 不变；description / title 的快速路径变更不允许 |
| blocking 与 outputs | 同 id 的 required_outputs 只增不减；min_count 不降低，字段集合不缩小，calculation method 不变 |
| 依赖 | 同 id 的 depends_on 只增不减，且全图仍为 DAG |
| 缺口 | allow_missing、维度 allow_missing / allow_not_applicable 不得 false→true；最低维度状态不降低 |
| 能力前置 | 既有步骤的 required_capabilities 必须不变；它是适用性约束，不当作质量偏序。新步骤可声明能力并经受准入与影子验证 |

重命名／删除步骤、改写固定语义或展示文案、放松要求、修改既有能力前置，均不能走「加强提案」快速路径；只能
作为新的全局包经 repo sync、人工评审和显式启用，或先回退再加强。旧 v1 与包比较时使用虚拟 legacy baseline
定义；新增 v2 元数据本身不算研究增强。

## 9. API 与界面

- `PlanStep` 增加可选 `method_step_ids: tuple[str, ...]`，支持多对多，不使用单个歧义 id。
- `PlanView` 增加可选 `method` 块：package_key、version、完整 content_hash、discipline，以及**包内全部步骤**的
  coverage。状态至少包括 `uncovered` / `planned` / `satisfied` / `completed_with_gap` / `missing`，因此未进入
  计划的 blocking 步骤也能显示。
- 现有按计划／完成文本计算的 step state 和 `extra_completed` 保持兼容；方法 coverage 独立由 6.2 的权威数据
  投影，不能靠文本猜测。
- 设置页显示全局默认与 per-listing 覆盖；提案工作台展示步骤、依赖、输出要求、能力和 discipline 的结构化
  diff，以及评估中的 `not_replayable` 和影子覆盖情况。
- 不新增公开写端点；包导入走 CLI，全局启用需要独立用户操作，per-listing 批准沿用现有接口。

> 实现备注（2026-10-31，M4；2026-11-01 收尾轮更新）：提案工作台的「结构化 diff、`not_replayable`
> 与影子覆盖」展示以 API 响应块交付——`GET /investor/methods/proposals/{id}` 对包提案增补只读的
> `candidate`（提案冻结的包身份）、`structure_diff`（按 §8 增强项分组的新旧对比）、`replay`（逐样本
> 三态与累计 `requires_candidate_shadow`）和 `candidate_shadows`（前瞻影子登记）四块，均有界截断且
> 不复制冻结工件；`list_methods` 概要增补 `package_key`，并增补只读 `default_head`（全局默认 head
> 投影，空为 null）。收尾轮（卡 19–21）补齐界面与运营层：`/app/methods` 页面渲染上述展示块与
> `candidate_shadow_claims`（发起与登记之间的队列视图），提供候选影子单个 / 批量发起、结果登记与
> 批准入口，提案创建支持粘贴候选包 YAML（服务端解析并以 `discipline` 填充 config）；配置与形态验证
> 以 `tests/unit/test_method_replay.py`（工作台四块）与 `tests/unit/test_candidate_shadow_ops.py`
> （运营默认值、批量、claims、YAML 入口）为准。

## 10. 兼容与回放不变项

- 模型不能铸造方法权威：候选影子发起、批准与回退仅限 authenticated_user；现有模型不能批准的断言保留。
- 包是数据不是代码：不授权工具、不改公式／门槛／预算／来源规则；固定指令模板和注册表随代码发布。
- append-only：package / version / event 工件不可改删；canonical hash 去重；head 只追加 revision。
- v1 binding、历史 TaskMethodBinding、无 receipt 任务和 `FIXED_METHOD` 完全走旧路径；v1 有效指令和输出
  schema 不变，旧任务重放结果不因新增 v2 校验而改变。
- v2 任务使用冻结 rendered_method、validator_version 和工具族成员快照；代码发布后仍分派到对应版本，不能在
  任务中途把 render-only 升级为 enforcing。
- 旧任务可以因缺少新元数据得到 `not_replayable`，不得伪造 pass；任何方法变更不追溯改写历史输出与发布。
- 离线影子只是工程证据；候选包前瞻影子与正式研究隔离，不能发布或修改当前观点。

## 11. 交付分期

| 阶段 | 状态 | 内容 | 验收 |
|---|---|---|---|
| M1 | ✅ 已实现（2026-10-30） | 契约、基线包 YAML、v2 receipt、固定渲染器；默认仍 v1 | v1 三类有效指令快照（普通公司研究／review／复用候选）逐字节不变，v1 输出 schema 与全量 golden 重放不变；v2 仅 render-only，receipt 保存渲染原文 |
| M2 | ✅ 已实现（2026-10-30） | plan bindings、step results、committed_sequence、包级校验与 PlanView coverage | 仅 validator v1 的新 v2 任务启用；无关 calculation 不能满足反向 DCF，自报完成无动作不能通过，依赖顺序和带缺口路径有正反例；M1 在途任务仍 render-only |
| M3 | ✅ 已实现（2026-10-30） | validate/sync、MethodDefaultHead 与显式全局启用 | hash 去重、旧版本不改、错误一次汇总；sync 不动 head；无 override 新任务按默认解析，历史与 per-listing binding 不变 |
| M4 | ✅ 已实现（2026-10-31） | 包提案、三态历史回放、候选包离线与前瞻影子、批准回退 | 原失败因实质要求 block；缺失元数据标 not_replayable；候选正例覆盖全部新增要求、反例被 block；shadow 无正式发布副作用；evidence_hash 变化拒绝批准 |

每阶段独立可交付。生产默认保持 v1，直到 M3 后由用户显式启用 v2 基线；不存在 M1–M2 之间对在途任务突然
增加校验的灰度窗口。

## 12. 开放问题

1. comparison / portfolio_action 是否需要独立包形态？首版不做，保持 v1 基线，观察到真实需要再评估。
2. 固定指令模板的语言与版本维护：包展示文案可中文，模型指令由 renderer version 决定，不允许每个包自行选择
   或注入语言版本。
3. 包是否允许携带估值参数（如反向 DCF 敏感性档位）？首版不做，参数仍归估值契约；`calculation.method`
   只能选择已发布方法，不能覆盖其参数边界。
4. 候选包前瞻影子的最小样本数、预算和界面批量操作需要运营默认值；它们只影响证据收集成本，不得降低第 7
   节对新增非回放要求的覆盖门槛。
   **已落地（2026-11-01，卡 19–21）**：`Settings.method_candidate_shadow_min_positives`（默认 1，
   只允许调高）要求批准前有相应数量的 matched 正例影子；`Settings.method_candidate_shadow_max_per_proposal`
   （默认 8）是每提案发起候选影子任务的硬上限；`POST …/candidate-shadow-batch` 支持一次发起 1–4 条。
   全部覆盖门槛（最新正例全覆盖 + 一个被拒反例）保持不变。
5. 跨步骤条件约束的表达：某步骤未满足时限制最终结论强度（如「答不出预期差则价格判断强制非方向性」）
   目前 `OutputRequirement` 词汇表无法表达，只能用维度 `minimum_status` 近似或由发布门槛代码实现；是否为
   包 schema 引入结论级条件，待第一个真实候选包（expectations_gap）运营后再评估。
