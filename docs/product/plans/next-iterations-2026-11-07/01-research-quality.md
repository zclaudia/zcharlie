# 可信研究工作流与质量对照实施方案

日期：2026-11-07。状态：**设计主审完成；待实施与验收**。
代码基线：`1f62fa4`。本文只定义方案，不运行真实模型、采集、迁移或部署。
本文中的文件行号均指该基线；任务卡文件是拟修改位置，不代表已实现。

## 1. 用户问题与成功标准

用户需要回答：研究方法增加了反证、问题拆解等步骤后，真实研究是否更可靠，新增等待和 token 是否值得。
用户应能看到同一公司、同一问题和同一批材料上的两个完整研究结果，以及哪些失败仍未解决。
现有结构校验通过、模型意见一致、用户愿意采纳，都不能直接回答这个问题。

交付成功分为两层，独立记录：

- 工程成功：冻结材料、准入、运行、取消、盲评、报告可贯通，失败有明确分母，重放不重复收费。
- 研究质量：人工确认关键事实和反证达到既有量表要求；未达到时输出失败报告，不能通过删样本改善结果。
- 不设置“候选必须胜出”的工程退出条件；十二个情境只能提供案例级证据，不足以证明普遍优越或投资胜率。
- 输出始终为实验研究制品，不发布正式观点，不通知投资行动，不移动方法 head，不更新账本。

## 2. MVP 与后续边界

首期比较**同一投资者模型配置下的 baseline 方法与一个 candidate 方法包**。
一次实验只含一个公司、一个问题、一个冻结材料包和两臂；十二情境通过固定数据集索引组织。
数据集含三个公司的正常研究、既往失败、证据不足和反例；四例开发、八例冻结留出。
每情境默认每臂一次；需要重复时，在封存前明确次数，MVP 上限二次，不按结果临时追加。
跨公司使用各自适用的提案和授权，不能把单公司提案当全局方法授权。

MVP 包含：保存材料清单、两臂完整研究、真实失败记录、人工盲评、逐例报告与数据集汇总。
不包含：自由切换模型/供应商、在线找资料、动态扩充工具、自动 judge 签署、自动方法启用、收益回测。
后续模型对照需增加模型配置的独立准入边界；首期不借此改写现有执行器的模型选择路径。
评分回测另立阶段：先审计 observed-PIT/reconstructed 数据和历史池，再做单评分版本的分桶检验。该阶段的最小可行离线工具见 [`forward-validation-v1.md`](../../forward-validation-v1.md)（P1 第 7 项；只读、不训练、不改 `calibration_status`）。
不得以研究内容分数替代收益指标，也不得以历史收益抵消关键事实错误。

## 3. 当前能力与复用地图

| 当前代码／文档 | 基线行号 | 复用与新增边界 |
|---|---:|---|
| `src/investment_agent/investor/methods.py` | 373–440 | 已有精确方法 receipt、包 hash、能力前置校验；新增可信实验准入上下文，不开放客户端 override |
| 同上 | 577–625、677–701 | 结构评估与前瞻 gate-shadow 已有；语义质量报告另存，不改历史 evaluation 的含义 |
| 同上 | 706–787、790–814 | 真实候选影子与批次已有；复用 proposal 校验、cap 和 admit 接缝，不复制第二个付费调度器 |
| 同上 | 817–877、990 起 | 候选登记与批准已有；实验产物不能直接成为该路径的批准证据 |
| `src/investment_agent/investor/method_shadow.py` | 165–185 | FunctionModel 工程重放保留原义，不冒充真实内容实验 |
| `src/investment_agent/agent/execution.py` | 88–122、241–292 | immutable grant、live policy、queue admission 与 freeze_task 必须继续执行 |
| 同上 | 332–359 | 取消、授权、记忆变更及期限仍在每个模型／工具边界检查 |
| `src/investment_agent/agent/research.py` | 471–549、559–570 | 当前 assemble 会读记忆、身份、当前观点及恢复材料；实验需独立受限装配分支 |
| `src/investment_agent/agent/tools.py` | 85–120 | TrustedTools 的 scope、cutoff、check_boundary 保留；在其后接实验材料读取适配 |
| `src/investment_agent/agent/builtin_tools.py` | 59–70、164–169、213–231 | 当前查询和搜索会读库；实验查询必须限定 manifest，不能仅把 as_of 参数写死 |
| `src/investment_agent/agent/budget.py` | 102–129、141–205、249–256 | reserve/sent/settle、unknown 对账与恢复复用，实际用量不由实验报告重算 |
| `src/investment_agent/agent/tasks.py` | 327 起、651 起 | fence 与取消服务复用，实验编排不直接改任务状态 |
| `src/investment_agent/agent/executor.py` | 150–163、194 起 | 已有模型配置变化拒绝；MVP 保留同模型约束，不从实验请求传入 Model 实例 |
| `src/investment_agent/review_panel/service.py` | 110–140 | 复用 canonical hash、无凭证模型快照格式，避免另造 model_ref 规则 |
| `src/investment_agent/review_panel/job.py` | 177 起、327 起 | 借鉴 ReadRecorder 与受限读取；review_seat 不是方法任务，不借它绕开 AG30/admit |
| `src/investment_agent/storage/models/investor.py` | 108–114 | 既有 MethodArtifact 可保存不可变实验报告附件；新增查询索引表，不复制原件 |
| `docs/product/investor-agent-research-policy.md` | 224–236 | 五维人工量表及门槛直接沿用，模型辅助评审不能单独签署 |
| `src/investment_agent/storage/repositories/financial_snapshot.py` | 75–104 | 回补绕过采集时间且选最新版本；本实验标记材料模式，不宣称严格历史 PIT |

当前 `review_panel` 已冻结 base pack、多模型席位与来源注解；本方案不再建设多席位评审平台。
AG20 历史试次质量欠账见 `investor-agent-implementation-plan.md:414,424`，不将旧失败记录改写为通过。

## 4. 用户流程与页面状态

### G0 首次可信研究引导（独立交付）

没有candidate提案的用户先进入“开始一次研究”，不要求了解方法实验。
选择已有覆盖证券后，从“商业驱动是什么／现金流为何背离／主要反证是什么”模板选问题并可编辑。
服务端只读预检现有材料，列明报告期、来源、可定位事实、当前缺口和可计算性；模板不自动声称覆盖完整。
用户明确选择现有`company_research`，看到当前已生效方法、模型和原任务预算后提交原认证消息入口。
G0只组织问题和展示已提交材料，不构造ExecutionGrant、不调用method_override、不创建实验Task或改变mandate。
任务按既有admission、取证权限、发布门槛和预算执行；G0不能给原研究新增来源/工具/后台授权。
结果页复用原任务/公司研究视图，解释“有观点／证据不足／未完成”的区别，展示理由、反证、缺口和下一步。
无材料时允许用户选择“先看缺口”而不运行；材料存在也不保证公司研究能通过，应显示真实停止原因。
候选方法与质量对照是单独入口；完成G0不会自动开启影子或付费对照，不将普通研究结果改标实验。
G0验收用现有真实任务路径的离线端到端fixture，真实模型质量仍需独立试运行，不由UI成功替代。
具体接线：证券身份复用`GET /agent/listings/{listing_id}`；新增只读计算的`POST /investor/research-entry/preflight`，输入listing_ref、template_key、question，返回材料refs、coverage、gaps、当前方法/模型摘要及预检时间，不写Task或预算。
预检是即时提示，不承诺后续状态不变；用户发送仍由原准入重新检查。会话创建复用`POST /agent/conversations`，发送复用`POST /agent/conversations/{id}/messages`的现有MessageInput及幂等键；结果读`GET /agent/tasks/{id}/research`。
G0不需要实验执行开关；它遵守现有Agent开关。预检缺材料时可以只展示缺口，不能以预检请求自动启动采集。

### G1 质量对照

1. `/app/methods` 增加“质量对照”入口；列出现有提案及其结构评估状态，缺少可用提案时解释前置条件。
2. 选择一个公司／情境，查看材料日期、范围、缺失和两臂方法差异；服务端解析身份，不要求输入内部 ID。
3. 预检材料包和能力，显示问题、模型、最多请求/token、重复次数及“实验不发布”的摘要。
4. 用户封存样本后，单独点击“开始对照”；这是本轮付费执行的具体授权，不由 GET 或封存自动启动。
5. 页面显示两臂 queued/running/blocked/failed 等实际状态、剩余额度和停止原因；可取消剩余工作。
6. 已完成结果进入盲评；随机呈现 A/B，展示相同材料和引用入口，不展示模型、方法名和预算暗示。
7. 提交量表后揭盲，显示两臂事实错误、研究深度、反证、成本和失败；进入原提案页面仅作材料参考。

空状态：没有材料则展示应补清单，没有真实产物则显示任务失败及诊断，不造示例成功卡。
partial：一臂失败或部分重复未完成时仍能查看已产生结果，但“可成对比较数”为零或实际完成数。
blocked：材料撤销、提案变化、预算不足和调用未知分别解释；不显示笼统“再试一次”。
error：403/409/422/503 保留用户输入，409 显示需刷新的版本，网络应答丢失先查询 mutation 收据。
失败和未评分条目始终显示；“只看已评分”筛选必须同步显示隐藏数量和总体分母。
盲评隐藏标签不是安全隔离：正文可能暴露方法，页面允许标记 `blinding_compromised`。

## 5. 冻结 read envelope

`FrozenReadEnvelope.v1` 是服务端构建、不可变、按 hash 绑定的**允许读取集合**，不是一个 as_of 日期。
封存事务读取已提交的精确 revision，生成 canonical JSON；存储 hash 和文件写入须原子可核验。
包含：证券身份投影、问题、cutoff、证据／原件／片段／财务字段／计算精确 refs 与内容 hash、单位、范围、缺口。
同时固定输入 schema、渲染器、检索算法、排序／分页规则、工具 profile、每次响应上限和引用到原件的映射。
大原件不复制，引用既有不可变 blob；仅保存被允许片段或表格的精确定位及内容 hash。
公开时间、取得时间、语义 available 时间全部不晚于 cutoff；只按报告期过滤不够。
模式固定为 `observed_materials` 或 `reconstructed_materials`，首期默认前者；重建样本单独汇总。

执行时新增服务端 `ExperimentTaskBinding`，与 Task、grant 和 envelope 一同绑定；模型不能提交此对象。
`assemble` 先经过原边界，再识别该绑定，只装配 envelope 身份／问题／材料和本臂 method receipt。
不自动读取当前公司观点、案头、持仓/资金用途、个人记忆、旧会话、公司上下文缓存或其他实验结果；恢复仅用本 task 的 checkpoint。
mandate只在服务端用于实时准入、预算和撤销，不将最新约定/用户偏好正文注入模型；样本确需约束时保存明确的冻结投影。
发送前对最终assembled payload作来源清单检查；非工具读取同样必须有envelope条目，不能只拦工具函数。
原记忆 revision 栅栏仍生效；记忆被撤销时停任务，不能以“实验不读记忆”为由绕开撤销。
模型系统指令、schema、方法、工具定义和第一次输入分别 hash；相同材料不要求方法指令相同。

工具处理顺序：原 TrustedTools scope/cutoff/check_boundary → 精确 membership/hash 校验 → envelope adapter。
`get_snapshot/get_evidence/get_event/read_disclosure_original` 只返回清单内固定内容，不能回落到当前库查询。
保存搜索只索引该包片段，固定排序与分页；未知 ref 返回 `outside_envelope`，不尝试实时搜索。
MVP 不开放跨公司比较、刷新、capture、发现／下载／解析新公告、外网搜索、发布、账本与其他写业务工具。
允许 `save_research_progress` 等实验内部过程提交，仍经现有校验与动作账本；不把它当业务发布。
实验用服务端RunSpecification保存最终研究/semantic_review到quality_result，并记录完整性；不调用PublicationService完成器。
该完成器保留计划、引用、方法步骤与scope校验，但不要求创建正式公司观点来证明完成；有缺口则明确partial。
阶段指令和finish_only提示也必须是实验版，不能仍提示prepare_company_view；工具返回不能暗示已发布。
产物只进入实验命名空间及本Task过程记录；不得写SavedEvidence、正式研究note、长期memory、publication/outbox或monitor。
实验ID与Task来源作为服务端污染栅栏，通用证据搜索、公司上下文复用、review_panel触发器、监测与通知都排除实验结果。
这一排除必须检查实际查询入口；仅在页面标“实验”或禁止发布工具，不能证明结果未被后台读入正式研究。
确定性计算首期只读预先保存的计算结果；动态估值、同行扩样留后续，避免运行期间引入新数据。

包声明的 required tool family 需要展开检查：包含被禁止能力时预检报 `method_capability_unavailable`。
不能把离线读取伪装成成功 `capture_snapshot`，不能删掉包要求或伪造 committed action 来使方法通过。
首批使用现有基线包及仅加强反证的兼容候选；没有兼容候选时返回真实阻塞，先走现有提案流程准备。
兼容性实查：`config/methods/company-research.v1.yaml:22–60`只有计划、假设、七维输出，没有capture/calculation要求；
`investor/method_packages.py:169–187`的required_capabilities默认空，故v1基线无需冒充动态采集。
可行首组设计为该基线与“C1反证加强包”：保留三步骤，counter_analysis改blocking，要求至少两个反向假设、一个既有verified反证ref。
C1不声明snapshot_field/calculation/peer_context要求，也不增加required_capabilities；证据必须已在envelope且能由get_evidence真实读取。
C1尚非本方案已存在的生产提案：Q01先通过现有包导入/提案和结构评估进入shadow，fixture证明可保存计划、读证、结算。
若当前old_method为另一套不兼容方法，不替换head来凑实验；先报告不兼容并选择相符情境或另行设计候选。
现有`company-research.v2.expectations.yaml`不能默认可用，需逐项检查其计算/动作来源条件；无兼容组合时Q04阻塞退出、G0仍可交付。
封存材料被合法撤销后停止读取；旧报告保留审计引用和受限原因，不能用复制的文字继续绕过遗忘。
资料更正发布后不静默改旧包，标记 `superseded_source` 并要求新实验；历史结果仍可解释所用版本。

模型预训练知识、提供商内部缓存、服务端模型更新和随机采样无法被 read envelope 消除。
提示模型只依证据，评审检查无引用事实；不把“无联网工具”称为“完全无先验”。
历史模型可能已知道后续事件，因此实验用于证据条件下的研究质量，不解释为历史预测真实性。

## 6. 方法绑定与准入

创建实验时保存 proposal_id/revision、其 old_method、候选 package_ref/hash、当前 baseline resolved binding。
要求 baseline 与提案 old_method 一致，精确公司 scope 和 company_research/review 适用性满足现有规则。
candidate 仍要求最新结构评估通过且提案处于 shadow；拒绝、修订、scope 变化均停止新准入。
baseline/candidate 不接收客户端配置 dict；只接收已存在且有权使用的制品 ref，由服务端复验 hash。

当前 `admit(method_override=...)` 只为候选影子设计，不能直接给 baseline 塞一个任意 binding。
新增内部 typed `ResearchExperimentAdmission`：包含封存 spec ref、arm、proposal fence 和 envelope ref。
由实验服务验证后交给 `admit`；admit 校验所属试次和唯一键，再由 MethodService 解析本臂精确 binding。
baseline 冻结已验证的 old_method；candidate 冻结已验证的候选包，继续复用 `_freeze_package_receipt`。
所有工具能力和 discipline 校验照常，不改 receipt 为 render-only，不允许模型选择 validator。
此 context 与 `method_override` 互斥；API、聊天、发现、组合准入不能构造或注入它。
实验任务标记 `experiment_only`，禁止进入 `candidate_shadow_result` 的批准覆盖计数；原 AG30 shadow 路径不变。
在同一短事务中写 trial、Task、grant、method receipt、ExperimentTaskBinding 和 mutation receipt。
已封存 spec 永远不跟随 head；新试次入场前如现行 baseline／提案已不匹配，停止并要求新实验版本。
重试/恢复使用原绑定；不能创建新 Task 重置预算或改换 method/cutoff。

## 7. 数据表、索引与不可变制品

| 表／制品 | 必要字段与约束 |
|---|---|
| `research_experiment` | UUID PK、owner_id、revision、state、spec_artifact_id、spec_hash、created_at；索引(owner_id,created_at,id) |
| `research_trial` | UUID PK、experiment_id FK、arm、repeat_index、state、task_id nullable UNIQUE、result_artifact_id、error_code；UNIQUE(experiment_id,arm,repeat_index)；索引(experiment_id,state) |
| `research_experiment_task` | task_id PK/FK、trial_id UNIQUE/FK、envelope_artifact_id/hash、method_binding_hash、model_config_hash、profile_version；不可变 |
| `research_assessment` | UUID PK、experiment_id/trial_id FK、reviewer_id、revision、payload_hash、payload、created_at；UNIQUE(trial_id,reviewer_id,revision)，追加不覆盖 |
| `research_experiment_receipt` | owner_id/mutation_id 联合PK、request_hash、result_ref、created_at；同键异参拒绝 |
| MethodArtifact 新 kind | `quality_case`、`quality_envelope`、`quality_spec`、`quality_result`、`quality_report`、`quality_blind_map`，名字均不超过现有长度24 |

`investor/common.py:45`的ObjectRef.kind是Literal；本方案采用独立`QualityArtifactRef`，不扩展正式ObjectRef枚举。
其kind仅允许`quality_case`、`quality_envelope`、`quality_spec`、`quality_result`、`quality_report`，id为UUID、revision为正整数；只在实验DTO和服务解析。
金融材料字段仍用原ObjectRef；两种ref不做隐式互转，EvidenceService/正式NumericResolver均拒绝QualityArtifactRef。
blind_map为内部制品，不进入QualityArtifactRef、公开artifact下载或URL白名单；未知kind一律拒绝。
case/envelope → sealed spec → trial/task → immutable result → assessment revisions → immutable report。
artifact 沿用 owner 检查、canonical hash 和追加保存；报告固定所用评分 revision、输出 hash 及算法版本。
可变状态表只作索引与生命周期投影；报告不能在读取时从最新任务／最新评分重新拼装。
盲映射只由服务端保存，不进入模型上下文、普通盲评响应、HTML data 属性或可猜测的下载列表。
唯一键负责并发去重；SQLite writer reservation 沿用现有短事务方式，模型／文件解析期间不持写锁。
不新增一个与 BudgetEntry 独立的费用事实账本；实验上限占用以 trial 绑定的原预算条目投影。

## 8. 字段契约与 API

全部 DTO 继承现有 Contract，`extra=forbid`；金融材料用ObjectRef，实验制品用上一节QualityArtifactRef，其他UUID/Quantity/UTC时间复用。
现有methods router前缀为`/investor/methods`（`api/routes/methods.py:39`）；下列均为完整API路径。
完整示例：`POST /investor/methods/quality-experiments`，认证/CSRF 后创建草稿，**不执行模型**。

```json
{
  "schema_version": 1,
  "mutation_id": "16987282-f862-42c8-87e9-a7a8d749ee55",
  "case_ref": {"kind": "quality_case", "id": "60468915-6c1b-483c-86ca-8c61d091c630", "revision": 1},
  "proposal_id": "36be88fc-a303-4d69-8957-077ecb88020f",
  "expected_proposal_revision": 4,
  "repeat_count": 1,
  "limits": {"max_requests": 8, "max_tokens": 160000},
  "rubric_version": "research-quality.v1"
}
```

```json
{
  "schema_version": 1,
  "experiment_id": "c19ca20b-f582-493f-8cea-309c5c62b4c6",
  "revision": 1,
  "state": "draft",
  "execution_started": false,
  "preflight": {"status": "pending", "errors": []},
  "model_policy": "same_authorized_investor_model",
  "limits": {"max_requests": 8, "max_tokens": 160000},
  "trial_plan": {"arms": ["baseline", "candidate"], "repeat_count": 1, "planned_trials": 2},
  "links": {"self": "/investor/methods/quality-experiments/c19ca20b-f582-493f-8cea-309c5c62b4c6"}
}
```

示例额度是请求上限，不是生产默认；页面需结合实际上下文预估，原任务/日限额更严时取更小者。
封存后响应补充 spec_hash、envelope_hash、模型无凭证快照、两臂方法 refs 及 token 预估，不返回密钥。

| API | 行为 |
|---|---|
| `POST /investor/methods/quality-cases` | 保存问题及精确材料 refs，校验身份、范围和时间；不采集 |
| `GET /investor/methods/quality-cases` | 分页列出可用／缺失样本和数据集split |
| `POST /investor/methods/quality-experiments` | 如示例创建草稿，服务端解析方法和模型 |
| `POST /investor/methods/quality-experiments/{id}/seal` | mutation_id+expected_revision；预检后固定 spec 与 envelope |
| `POST /investor/methods/quality-experiments/{id}/start` | mutation_id+expected_revision+spec_hash；明确执行，产生原子准入的两臂任务 |
| `POST /investor/methods/quality-experiments/{id}/cancel` | mutation_id+expected_revision；停止未来派发并调用现有任务取消 |
| `GET /investor/methods/quality-experiments/{id}` | 实际状态、分母、用量、原因与报告；GET 不调模型 |
| `GET /investor/methods/quality-experiments/{id}/blind` | 隐藏arm和敏感元数据的成对输出及材料；不隐藏失败计数 |
| `POST /investor/methods/quality-experiments/{id}/assessments` | mutation_id+expected_assessment_revision+output_hash+五维分数/理由；追加版本 |
| `POST /investor/methods/quality-experiments/{id}/reports` | 固定评分集生成报告，先提交评分再揭盲，后改评分记录已揭盲 |

统一错误沿用`api/deps.py`的BusinessError与HTTPException：`{"detail":{"code":"...","message":"...","field_errors":[],"retryable":false,"current_refs":[]}}`；不回传凭证或原始私有prompt。
实验版本差异写入field_errors数组的`{field:"expected_revision",message:"当前版本为..."}`；QualityArtifactRef不得塞入正式current_refs。客户端从已知实验详情地址刷新。新增code及下列状态在INVESTOR_ERROR_STATUS中显式注册并测试，不能依赖默认409映射。
401 未认证；403 quality_scope_forbidden或现有writes_disabled；404 含他人对象；409 stale_revision/idempotency_payload_mismatch/spec_changed。
422 envelope_incomplete/method_capability_unavailable/invalid_assessment；503 feature_disabled/model_unavailable/dependency_unavailable。既有授权错误沿原映射返回，不全局改为403。
先认证并核对对象访问范围，再在状态/CAS检查前读取mutation收据；同请求重放返回原结果，同键异参409，失败回滚不留半边入场结果。
评分 CAS 独立于实验状态 revision；同输出每评审者的下一 revision 必须连续且匹配前版。

## 9. 状态、预算与恢复

experiment：draft → sealed → running → awaiting_review → reported；可追加新 report revision，不覆盖旧报告。
draft 预检失败留 draft；sealed 失败不变更已封存内容，修复材料需新 case/spec 版本。
running 中的额度/授权/调用未知可进入 blocked；取消进入 cancelling，全部任务停止或结算后为 cancelled。
trial：planned → queued → running → completed/failed/cancelled/blocked；调用unknown为独立usage状态，不冒充failed可重试。
正常终止且任何一臂缺产物仍进 awaiting_review，report 的 completeness=partial；不能成对评分但可记录失败原因。

两臂派发顺序按封存随机种子交错；MVP 同时最多两个任务，先比较相同重复序号，避免某臂长期抢占。
封存公平规则：总requests/tokens整除`2×repeat_count`，所得为每trial硬上限；余数不分配，禁止臂间借额。
示例8请求/160000tokens、一次重复时，每臂4请求/80000tokens；两次重复则每trial2请求/40000tokens。
每trial的结论预留策略、输出上限和超时相同；材料/方法长度导致实际用量不同如实报告，不调整冻结份额。
unknown占用所属trial份额及实验总额；耗尽的一臂不能挪用另一臂或下一repeat额度继续修复。
owner日预算为实时共享约束，预检不是实际全组日额度锁定；其他任务抢占造成partial时单列external_budget_contention。
这类不对称结果不计为候选胜/负，但保留planned分母；不能重新开实验隐去该轮，也不承诺两臂一定都能发出。
start 在同一事务校验两臂准入条件并建立任务；队列/结构准入有一臂失败则整次回滚，模型未发送。
发出后两臂独立失败，不能回滚已发生费用；每次HTTP仍由原 BudgetService reserve/sent/settle。
实验上限是附加收紧：跨所有trial/attempt求已用+reserved+unknown，重建Task、跨日、取消都不能重置。
每Task预留仍受原task lifetime及owner日额度约束；实验预算检查与原预留在同一短事务，防并发超额。
unknown继续占用估算，显示实际token未知、费用未知；不能把null写0，不因更换实验ID释放未知日额度。
sent后中断不自动重发；恢复先复用Task和checkpoint，原recover将未知请求转unknown，需现有可信对账后才能继续。
取消只承诺禁止后续发送/提交；已发送请求可能完成，实际费用仍结算，迟到输出记fenced且不得进入正式评分。
模型配置hash变化、候选撤回、材料撤销和功能关闭均在发送前/工具前/输出提交前检查；不自动换模型。
原任务已完成且输出hash固定后才可评分；未结算费用允许显示内容但报告 cost_status 保持unknown。

## 10. 失败分母与人工盲评

报告固定 planned/admitted/sent/completed/usable_output/paired/scored 七个计数，不丢弃失败或取消。
工程完成率=usable_output/planned；语义合格率同时报 qualified/scored 与 qualified/planned，未知不记为不合格事实。
拒答和证据不足按案例预期判断，不因没有多空观点而自动失败；有方向观点不得滥用“不适用”。
五维沿用事实关系、研究深度、反证质量、不迎合与修订、可理解与继续；每项0/1/2或有理由的N/A。
关键事实错误、范围/授权违规单列零容忍；事实关系必须2，有方向观点反证必须2，其余适用项至少1。
模型辅助标注仅作待核问题，不默认填分，不自动签署；评审者身份来自认证上下文，不来自请求actor字段。
随机化A/B映射按实验固定；评分提交前不提供揭盲结果，提交后改分必须注明已知标签，不混入原盲评分组。
单用户可隔日复评并报告分歧，不能冒充双人独立评审；多公司案例按公司聚合，重复试次不当独立公司样本。
开发集和留出集hash固定；留出集使用次数、事后修改和重复请求全部记录，避免边看答案边调方法。
报告逐例展示事实错、缺口、胜/平/负与成本，无统一投资胜率；小样本不作显著性结论。

## 11. 迁移、开关、回退与方向接口

当前迁移head为060；实施时先复查实际head，由主审按落地顺序分配next_revision，不预占061、不改已发布060。
只新增表/索引/制品kind，grant新增可选实验绑定版本兼容字段或同等不可变关联，旧grant序列化保持兼容。
迁移验收含空库、当前head升级、downgrade/upgrade roundtrip；仅临时库运行，不自动迁移真实库。
新增 `research_quality_enabled=false` 和 `research_quality_execution_enabled=false`，前者控制新建入口及新写入，后者控制付费准入；有权限的既有报告与操作收据读取不因关闭功能而消失。
已开始任务执行每边界看execution开关；关闭后拒绝发送，保留只读报告；紧急关闭不等待schema回退。
部署回退必须先停实验派发并处理在途/unknown；旧应用忽略新增表，不能运行不识别实验profile的任务。
降级不删除已有实验历史；有数据时禁止无导出的破坏性downgrade，回退代码优先于删表。

其他方向：[研究收件箱](02-research-inbox.md)、[组合压力与事实核对](04-portfolio-stress.md)、[银行财报专题](03-bank-topic.md)。
本方向不依赖其UI交付；完整共享约定见[总览](README.md)。
共享事件由02的IN-01独立基础任务提供，使用research_domain_event及consumer_receipt/projection_cursor。
信封为`event_id/event_type/owner_id/source_ref/source_revision/occurred_at/recorded_at/schema_version/payload_hash/payload`。
payload_hash覆盖规范化payload，内部sequence用于消费排序；重复event_id幂等，不按到达顺序覆盖source_revision。
IN-01接入后只消费`source.invalidated`白名单事件及其更正/撤销理由；事件不得直接授权付费任务，occurred/recorded时间不赋予历史可用性。
IN-01交付前，在每个发送/工具/完成边界对精确源版本实时查询撤销和更正状态；无法核验则停止，不能因为没有事件链就省略有效性检查。因此G0和实验核心没有隐含的IN-01硬依赖。
首期实验结果不进入日常收件箱，不发布投资论点/正式证据变化事件；实验失败留在质量页面，避免与投资论点混淆。

## 12. 按依赖拆分的任务卡

| 卡 | 依赖／拟修改文件 | 退出条件 |
|---|---|---|
| G0 首次可信研究 | 无；`web/templates/agent.html`、对应静态脚本、已有认证消息API和只读coverage接口 | 无candidate仍能问题模板→材料覆盖→明确选择company_research→原admit→结果/缺口；无实验权限与自动调用 |
| Q01 契约与可执行组合 | 无；`research_quality/contracts.py`、新C1候选包/fixtures、实验ref解析白名单 | 十二情境索引/split、QualityArtifactRef冻结；v1+C1用实际方法校验跑通，当前不兼容则记录阻塞，不虚构可运行case |
| Q02 存储与迁移 | Q01；`storage/models/research_quality.py`、`storage/repositories/research_quality.py`、下一迁移 | 唯一键/CAS/owner/追加制品与fresh/roundtrip测试通过 |
| Q03 envelope封存 | Q02；`research_quality/envelope.py`、`evidence/`受控读取接缝 | 来源时间/单位/hash/撤销验证；可重现相同包；不读当前头或漏出包外数据 |
| Q04 可信两臂准入 | Q03；`investor/methods.py`、`agent/execution.py`、新admission服务 | baseline/candidate精确绑定，同事务admit，无任意override，能力不兼容预拒绝 |
| Q05 执行与预算 | Q04；`agent/research.py`、`agent/tools.py`、`agent/executor.py`、`agent/budget.py` | 冻结装配/工具适配/同模型fence/跨trial预算/取消unknown完整，旧流程回归通过 |
| Q06 报告与盲评 | Q02、Q05；`research_quality/reporting.py`、`assessment.py` | 七分母、output hash、评分CAS、盲映射与已揭盲后改分可追溯 |
| Q07 API与页面 | Q03–Q06；`api/routes/research_quality.py`、对应schemas/template/static | 认证CSRF、丢ack重放、空/partial/blocked/409、390/1440页面真实HTTP验收 |
| Q08 联合验收 | Q01–Q07；对应unit/integration/browser、README与验收记录 | 下表覆盖；真实试运行单独授权与限额后再做，未做时明确pending，不冒充通过 |

文件路径均相对 `src/investment_agent/`，tests/config/docs/alembic明确写出的除外。
Q04/Q05须顺序合并以免原Task准入与模型栅栏分叉；先测离线FunctionModel，不自动启动真实试次。

## 13. 验收矩阵

| 场景 | 期望结果／证据 |
|---|---|
| 正常两臂研究 | 相同envelope hash、不同精确方法receipt、两个真实Task refs及不可变输出 |
| 无candidate首次研究 | G0走原company_research准入，coverage缺口如实，无实验Task或grant扩大 |
| 草稿／封存GET | 无Task、BudgetEntry、源网络请求；GET只读 |
| 同mutation丢ack重放 | 返回原experiment/start receipt；Task和sent计数不增加 |
| 同mutation异参 | 409，原spec与费用不变 |
| 并发start／最后额度 | 至多一组trial；原子上限不超，不留一臂半准入 |
| 先执行臂耗尽／unknown | 不能借另一臂份额，冻结公平规则一致，外部日额度争用单列partial |
| baseline head已变化 | 新入场拒绝；已绑定Task仍保留原receipt，不能随头漂移 |
| 提案修订／拒绝 | 新任务停止，迟到结果fenced；实验不成为AG30批准覆盖 |
| 包要求capture或外网 | preflight capability错误，无HTTP；不能假capture成功 |
| 取得时间晚于cutoff | 封存拒绝或明确缺口；报告期较早不构成豁免 |
| 查询包外ref／搜索新材料 | outside_envelope，无当前库回落，无源网络调用 |
| 当前观点／记忆／恢复缓存变化 | 两臂首轮材料投影不漂移；撤销仍按原栅栏停；不跨Task恢复 |
| 持仓／案头／mandate正文变化 | 最终prompt无清单外非工具材料；实时预算/撤销仍生效 |
| 材料更正／撤销 | 更正标记并要求新版本，撤销停止读；旧制品不静默改写 |
| 模型配置／服务不可用 | 拒绝后续发送，不替代模型、不把错误当空成功 |
| sent后进程终止 | 原BudgetEntry unknown占用；不自动重发，原可信对账留痕 |
| 发出后取消／关开关 | 后续发送为零，实际迟到usage结算，输出不进入有效盲评分 |
| 一臂失败／预算耗尽 | partial报告保留planned分母和错误，paired数量正确 |
| 评分冲突／错output hash | CAS409或422，原评分不覆盖；重新打开不会泄漏隐藏arm |
| 揭盲后改分 | 新revision注明unblinded，旧盲评汇总可重现 |
| 证据不足／反例 | 合理拒答可合格；方向结论缺反证不可用N/A掩盖 |
| 旧Agent／review_panel | 无实验绑定任务的指令、工具、收费及方法receipt保持既有行为 |
| 实验输出被正式搜索／监测读取 | 来源栅栏排除，不新增SavedEvidence/观点/通知/monitor或方法批准覆盖 |
| QualityArtifactRef与blind_map | 专用ref按白名单可解析，正式ObjectRef/EvidenceService拒作金融证据，blind_map不可公开下载 |

测试包括真实SQLite/HTTP与原准入路径；禁止只mock admit后断言传参。渗出材料测试检查模型实际收到的prompt/工具返回。
按卡运行必要单元/集成检查；末卡按当前CI做ruff、mypy、not-network pytest、浏览器，不在CI运行真实付费评测。
产品质量验收独立追加真实样本、模型配置hash、人工评分、预算及失败报告；未取得该证据就保持pending。

## 14. 工作量、风险与官方依据

估计工程4–6人周：冻结读取与准入约2周，存储/API约1周，盲评页面与报告约1周，联合故障验证1–2周。
材料整理与人工复核另约1人周；真实模型费用不包含在人周中，按封存的请求/token上限预估。
最大风险是现有方法步骤需要动态取证，与冻结能力不兼容；先用兼容反证候选验证，不能为赶交付放宽方法门槛。
其次是原始材料撤销、模型先验、留出集污染和小样本；分别通过执行栅栏、限制结论、版本审计和逐例披露处理。
实施验证重点：实验typed admission不扩散权限、原模型配置fence覆盖所有发送、额外预算占用与原账本原子提交；设计约束已纳入主审，仍须取得集成验证证据。

- [Pydantic Evals Dataset API](https://pydantic.dev/docs/ai/api/pydantic_evals/dataset/)：typed cases/evaluator/report可作薄适配；其retry/repeat不能直接包住付费Task以绕过预算/幂等。
- [Pydantic Evals开源库](https://github.com/pydantic/pydantic-ai/tree/main/pydantic_evals)：可评估任意Python函数；先对保存结果做适配spike，当前固定Pydantic AI 2.31.1兼容性未验证，不预先承诺依赖升级。
- [MLflow Evaluation Datasets](https://mlflow.org/docs/latest/genai/datasets/)：借鉴真实失败轨迹整理固定样本；其数据集要求Tracking Server SQL后端，首期不另设权威存储和平台。
- [Qlib Workflow](https://qlib.readthedocs.io/en/latest/component/workflow.html)及[RollingGen源码](https://github.com/microsoft/qlib/blob/main/qlib/workflow/task/gen.py)：后续借鉴滚动分段与trunc_days；不引入整套训练交易平台，不声称它修复本项目重建PIT问题。

上述官方资料于本轮方案调研打开核查；开源能力只作取舍依据，未在本仓库安装、执行或验证兼容。
