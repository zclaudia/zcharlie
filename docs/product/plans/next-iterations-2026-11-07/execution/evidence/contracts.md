# BASE-02：四方向接口冻结 v1

日期：2026-11-07。任务：BASE-02。状态：接口规范已冻结；不表示生产者、路由、resolver、迁移或功能已经实现。基线 HEAD=`1f62fa4ac3dd22be5ef65550ae6ac0444098b313`；实际 dirty 状态以 [BASE-01](baseline.md) 为准。本卡未修改业务代码、原设计、任务索引、simulation 或061迁移；不调用应用、数据库、模型、网络、通知或真实E2E。

规范依据：[共同约定](../../README.md)、[共享任务](../00-shared-tasks.md)、[质量](../../01-research-quality.md)、[收件箱](../../02-research-inbox.md)、[银行](../../03-bank-topic.md)、[组合](../../04-portfolio-stress.md)。本记录具体化尚未闭合的接缝；后续变更在文末登记版本及受影响任务，不在各方向私自兼容。

## 1. 交付物及验证层

机器合同位于 `tests/fixtures/next_iterations/contracts/`，均为**合成数据**，不证明来源存在或金融事实可信。

| 文件 | 合同 |
|---|---|
| `events.schema.json` | Draft2020-12闭合事件信封、13个event type及逐种payload、Subject/引用/RebuildState定义；无领域模块导入依赖 |
| `events.valid.json` | 每个事件一份完整输入/输出信封，真实计算payload_hash、确定性event_id；不是实际保存链产生的SV验收样本 |
| `events.invalid.json` | schema、跨字段、resolver及唯一键各层必须拒绝的反例；每例注明拒绝层 |
| `canonical.vectors.json` | canonical字节、SHA256、UUID命名空间、源身份及有类型Decimal/UTC等价样例 |
| `quality.json`、`inbox.json`、`bank.json`、`portfolio.json`、`g0.json` | 四域请求/响应及查询合同；领域read-model不冒充完整持久记录 |
| `projection-cursor.json`、`errors.json` | generation持久head/CAS和共享错误状态 |
| `manifest.json` | 接口版本、文件列表及状态 |

EV-01可直接以`common.Contract`、Python标准库与此schema实现：解析层不导入尚未创建的`bank_topics/research_quality/decision_observation`模块。来源schema与发布许可分离：**能解析预定义kind不等于能发布**。schema只能证明形状；字段相等、严格整数、引用存在、owner/subject、事实资格及事务由后续各层执行。schema里的format应启用format checker；Pydantic继续严格校验。

## 2. 已有能力核对与引用命名空间

核对 `investor/common.py`：Contract=`extra=forbid,frozen=True,validate_default=True`；Revision为strict int≥1；ObjectRef是闭合Literal，整数/UUID按kind分类；OwnerId当前仅`local_owner`。`BusinessError.current_refs`只接受ObjectRef。字段存在不构成解析授权。

| 引用 | ID/版本合同 | 持久回读及发布前责任 |
|---|---|---|
| 旧ObjectRef整数kind | listing、market_snapshot、financial_snapshot、earnings_forecast、disclosure_schedule、business_segment、candidate、watchlist_item、pipeline_run、research_dataset；strict正整数 | 原resolver逐种处理；不可用UUID字符串或数字字符串猜型 |
| 旧ObjectRef其余kind | UUID；revision≥1 | 保持原契约，不扩大publication/NumericResolver方法白名单 |
| QualityArtifactRef | quality_case/envelope/spec/result/report；UUID、revision≥1；无field_path | QT服务owner与hash回读；独立合同，不能转换成ObjectRef、EventSourceRef或正式current_refs |
| 新银行ObjectRef | topic_case/revision/condition/check；UUID | BT契约/引用任务提供领域解析，XA受控注册；case.revision为业务版本；独立topic_revision UUID的ref.revision=1；condition用稳定ID+内容revision；check UUID/ref.revision=1 |
| 新组合ObjectRef | portfolio_stress、decision_observation；UUID | PT提供owner/account/精确revision解析；calculation仍原kind，方法仅portfolio_stress或decision_fact_check的受限读取 |
| EventSourceRef整数分支 | review_round、review_comparison、research_change_set；strict正整数、revision=1 | EV-05初始只读resolver；IT生产者集成后按具体event开启发布 |
| EventSourceRef UUID分支 | review_cycle、case、recommendation_event、evidence、topic_check、decision_observation、calculation | 不接受field_path；revision≥1，其中recommendation_event/evidence/topic_check/calculation在本批事件固定1 |

`EventSourceRef`闭合kind限上表，不是任意ObjectRef透传。payload的`ExactRef`仅是上述正式/领域引用加明确的legacy event refs；QualityArtifactRef始终排除。正式ObjectRef的新增kind必须由原指定集成任务串行修改common；本卡不改白名单。

实际旧模型与设计简称的差别已经核对：

- `storage/models/review_panel.py`的ReviewRoundRecord/ReviewComparisonRecord均int PK，均无owner列；comparison.id与round.id独立，必须核对round_id及algorithm_version。当前只可在可信`local_owner`部署模式、授权listing范围下使用；不得声称有已有多租户owner行。review_round最终事件只绑定定稿所选comparison，若未来同round另一算法也要发通知须新event type/显式业务版本，不能改写同源payload。
- `ResearchChangeSet`无listing/owner列，必须联current_snapshot→pipeline_run→listing并核对snapshot证券身份；不能以symbol文本近似匹配，不把全局旧行当跨owner来源。
- `ReviewCycle`当前仅id/case_id/task_id/state/payload，无持久事件revision。IT-002/003新增`cycle_event_revision NOT NULL DEFAULT 0`，IT-008在每次真实变化事务中CAS递增后发布；旧0不是可发布revision。没有历史的可变旧cycle只允许一次migration_snapshot。
- `storage/models/publication.py`实际表是PublicationEvent，物理PK=`sequence:int`。`RecommendationEvent`是`investor/contracts.py`中的payload DTO，继承Record，有独立UUID id/revision=1。`recommendation_event` resolver按**payload.id**找唯一PublicationEvent并核验payload、owner、recommendation及case；不得把sequence或recommendation.id冒充源ID。当前没有该UUID实体列/索引；IT需在既定写范围内提供精确JSON查找并拒绝歧义，不暗增迁移。
- `SavedEvidence` owner在持久行，不在EvidenceRecord；新行created_at在payload。`save_candidates`当前返回fact_refs/facts并使用on_conflict_do_nothing，尚未以可信inserted结果发事件；owner目前硬编码local_owner。SV-01/02负责显式owner和inserted判定，不能靠现有先查猜新增。

## 3. 共同DTO、owner与时间/数值

所有新DTO继承Contract，拒绝未知字段；strict bool与strict int分开。Subject是discriminated union：`{kind:listing,listing_id:strict int≥1}`或`{kind:account,account_id:UUID}`，禁止混合、多余字段、空subject或客户端owner。账户事件不能自动扇出为持仓股票事件；account contribution的case_ref/episode强制null。

owner从认证上下文/服务端grant取；外部请求不含owner、actor、内部context、任意grant或任意方法/模型配置。当前OwnerId仅local_owner也必须校验持久owner及关系。回放顺序：认证→当前对象访问范围→owner/key请求收据及hash→对新请求检查开关/控制/精确引用/当前CAS→领域写入/事件/收据。原成功请求在head变化/开关关闭后仍可获权读取原收据；撤权不被旧收据绕过。

时间：内部aware datetime，统一UTC；规范JSON使用`YYYY-MM-DDTHH:MM:SS[.ffffff]Z`，小数秒去末尾0。拒绝naive datetime；接收合法时区偏移后归一UTC。业务期间date、published_at、acquired_at、available_from、occurred_at、recorded_at分别存，不用recorded证明历史可用性。界面Asia/Shanghai；sequence只排序，不能用时间戳充当cursor。recorded_at由append时钟取得，occurred_at由可信源固定。

Decimal：沿用DecimalValue，输入字符串/整数/Decimal，拒绝float、bool、NaN/Infinity。JSON数值以字符串传递；Quantity未知值value=null且unknown_reason非空；已知值unknown_reason=null。组合新数值输入再执行40位/最多18位小数及有限指数边界（PT契约负责在展开前拒绝巨大指数）；原DecimalValue本身**没有**40/18通用限制。金额精确计算不先float；显示量化不参与保存hash。

## 4. canonical与事件身份算法（EV-01冻结）

1. 先解析闭合DTO，在声明为Decimal/UUID/时间的字段按类型规范化；不把任意文本`"1.00"`猜成数字。Decimal用无指数十进制，去无意义尾0及小数点，负零→`"0"`；UUID小写标准形式；UTC按上一节。bool不可归成整数。可选字段使用合同默认值统一序列化；ObjectRef缺field_path与null统一为null。EventSourceRef没有field_path。仅本文schema新增的payload默认：backfill=false、manifest_hash=null；其余标required的nullable字段必须显式提供。
2. 字典键按Unicode码点排序；UTF-8、ensure_ascii=false、紧凑分隔符`,`和`:`、allow_nan=false，无缩进无BOM无末尾换行。数组保序，除明确集合：更正invalidated_refs、exact_source_refs、notify_reasons、quality、changed_reasons等无序集合先去重并按各元素canonical字节排序；业务有序数组、trial顺序、分片ordinal、version_refs清单不得擅自重排。哈希前默认补齐与集合化必须同一实现，不能用展示JSON直接哈希。
3. payload_hash=`sha256(canonical(payload).encode('utf-8')).hexdigest()`，只覆盖payload。输入event_id/hash如存在必须与重算一致；producer不能自行指定任意身份。schema_version与source_revision分别校验。
4. source_identity=`canonical({"kind":source_ref.kind,"id":source_ref.id})`，不含revision/field_path；数据库源唯一键=`(owner_id,event_type,source_identity,source_revision)`。
5. 固定UUID namespace=`30d5fd64-e30f-553b-930e-291bbad453ea`，生成方式uuid5(NAMESPACE_URL,`investment-agent/research-domain-event/v1`)。event_id=uuid5(namespace, canonical(`[owner_id,event_type,{kind,id},source_revision]`))。
6. source_ref.revision必须等于source_revision。同源同hash重放原事件，保留原recorded_at；异hash为显式source_conflict，不覆盖、不吞为幂等成功。事件内多重ref指同结果时必须相等。

**Fixture约束**：`events.valid.json`是规范化后的wire，包括ObjectRef默认null以及backfill默认字段；canonical vectors的`typed_inputs`是归一前输入，不直接按普通字典哈希。payload本身不允许浮点路由值；旧comparison可含float，只在其原记录回读，不复制整份result_json入事件。

## 5. 事件清单、闭合payload与注册责任

完整字段、required和枚举见events.schema.json；下面表格是来源、可信语义及落地责任。所有payload必须subject；无凭证、原文、用户账本备注、实验输出或任意HTML。安全摘要为受限纯文本，detail_url只能受控同源产品路径，不能外链。

| event type | source kind / revision | payload/跨字段约束与注册责任 |
|---|---|---|
| research_change.recorded | research_change_set:int /1 | run_ref、current/previous_snapshot_id、baseline、has_meaningful_changes、source_changed、postprocess_eligibility。IT同snapshot事务写；不猜最终run状态。eligibility=null明确拒绝自动付费后处理；非空含源提交时开关布尔、listing、accepted_at、settings_revision(无法取得时null)、settings_snapshot_hash，不是授权grant；IT-009/010生产/核验，消费者再次核当前权限/终态 |
| review_panel.finalized | review_round:int /1 | comparison_ref独立int、algorithm_version、notify_reasons仅high_divergence/stance_flip。EV-05提供精确round/comparison回读；IT定稿同事务绑定并发布，不能在best-effort块内 |
| review_cycle.changed | review_cycle:UUID /cycle_event_revision | case_ref、case_episode、phase、state、task_ref、reason_codes、exact_source_refs。IT递增并回读case owner/listing；phase与状态不同，dirty是phase，不伪造已有Cycle.state |
| recommendation.validity_changed | recommendation_event:UUID /1 | recommendation_ref、case_ref、validity、replacement_ref、review_ref、exact_source_refs；与PublicationEvent.payload完全吻合；EV基础回读协议，IT发布接缝 |
| case.control_changed | case:UUID /新case revision | case_ref同source、case_episode、archive/reopen/pause/resume/mute/unmute；IT控制CAS+原操作回执同事务 |
| source.invalidated | evidence:UUID /1 | **首期仅可信文档数值更正**，subject listing、reason固定source_corrected、invalidated_refs非空旧evidence引用集合、replacement_refs恰一个且等于source_ref的新evidence（忽略ObjectRef默认null的形状差异）。无backfill或任意withdrawn reason。EV-01先定义schema；SV-02后注册该event专用evidence发布resolver，不让EV默认把任何evidence当可信更正 |
| topic.observation_changed | topic_check:UUID /1 | topic_ref精确topic_revision、condition_ref、check_ref同source、prior_check_ref、target_period、baseline_period、scope、scope_changed、outcome、changed_reasons、exact_source_refs。采用方向03的精确check身份，取代方向02早期表格的泛称topic observation。BT事件/引用任务实现resolver并由指定集成卡注册 |
| portfolio_stress.completed/partial | calculation:UUID /1 | account subject、study_ref、calculation_ref同source、version_refs非空、quality、safe_summary、detail_url；PT限定method/version及账户，completed要求complete，partial要求结构化缺口；默认context且inbox receipt=ignored |
| decision_observation.due | decision_observation:UUID /精确观察revision | account、observation_ref同source、decision_ref、registered_at、due_at、not_before及共享安全字段；occurred_at=due_at，提交时检查未取消及授权epoch；PT领域实现后注册 |
| decision_fact_check.completed/partial | calculation:UUID /1 | account、observation_ref、decision_ref、calculation_ref同source、previous_check_ref、registered_at、due_at、outcome及共享安全字段。outcome为met/not_met/unknown/mixed，是predicate聚合事实状态，不是决定正确性。partial保留unknown；PT限定decision_fact_check方法/版本 |
| decision_fact_check.superseded | 新calculation:UUID /1 | 上述核对字段加superseded_ref，必须是旧结果且同owner/account/observation；旧事件/结果不改写。PT更正适配实现后注册 |

EV初始来源resolver只解决既有存储；未安装或未通过领域验收的topic_check/decision_observation以及新calculation方法，发布失败`invalid_reference`或未就绪依赖，不接受generic dict绕过。calculation/evidence已有ObjectRef不代表所有方法/事实自动具备新事件资格。消费者可以对已记录的未启用可选适配记ignored(`unsupported_optional_adapter`)；这与发布时拒绝虚假来源不同。

可信更正发布额外条件：save_candidates最终保存事务的DB inserted结果为true；新EvidenceRecord.verification=verified、source_kind=document、value及document_provenance非空、correction_refs非空；保存行及对应DisclosureOriginal/grant owner一致，listing/period/metric/unit/currency/reporting_scope及旧原件更正关系已核实，旧ref确实在新correction_refs中；occurred_at=**保存行payload.created_at**。重解析命中旧行、候选→未verified、普通文本/标题/GET/parse不发。append失败回滚新证据及原action receipt。首期无通用原件合法撤回入口；不可读只显示source_unavailable，不能制造source_withdrawn。

可选历史补入仅IT指定旧来源允许backfill=true+非空manifest_hash，保留原可证明occurred_at，缺时间在manifest记排除；默认新事件false/null。旧cycle只migration_snapshot。backfill不能触发付费/通知。source.invalidated不由手写fixture暗补历史。

## 6. 同session接口与事务边界（EV交付接口）

以下为待实现的Python接口合同，参数可增加静态类型但不得改变事务归属：

```python
async def append_event(session, *, owner_id, event_type, source_ref,
                       source_revision, occurred_at, payload) -> StoredEvent: ...
async def resolve_source(session, *, owner_id, event_type, source_ref,
                         source_revision, payload) -> ResolvedSource: ...
# ResolvedSource闭合：owner_id, subject, source_ref, source_revision, occurred_at,
# ordering_key；来源回读及payload一致性已经完成，不能只回传输入。
register_source(kind, resolver, *, allowed_event_types, schema_versions=(1,))
register_consumer(name, handler, *, event_types, schema_versions=(1,))
```

StoredEvent=完整信封+数据库sequence；内部sequence不写进payload。registry闭合consumer至少包含research_inbox.v1、watchlist_sync.v1、review_admission.v1；QT来源失效消费者和BT/PT注册名称由其集成任务明确并保持稳定，不把generation放consumer名称。处理器只收session和可信StoredEvent，不接受脚本/URL。

append/resolve/receipt/cursor/投影回调都用调用方同AsyncSession，无commit/自开session。append前flush使本事务新行可回读；producer最终业务提交决定事件可见性。原源只查询失败不创建空记录。消费者一事务保存entry/source/journal、receipt、cursor CAS；回滚后可另开短事务只登记retry错误及扫描水位。入队/原后处理receipt亦同事务，模型请求不在事务内。原自开session helper由IT提取同session内部服务，旧wrapper兼容。

普通链ordering_key由可信源关系构造，owner+case/episode、round或account/observation；跨类型同事项用同链。前序retry/quarantined阻止同链后序，独立链继续。receipt state=applied/ignored/retry/blocked_predecessor/quarantined；忽略保留明确reason。5s/30s/2m/10m退避，累计第5次隔离；未知schema/hash冲突立即隔离。扫描水位不表示全部成功。

分片manifest闭合为generation、按entry_id排序的目标清单、精确源版本/hash；冻结后不可改，最多10000目标、每事务至多50效果，effect_cursor为下一个ordinal。投影/journal/ordinal推进原子，完成才applied。维护tick至多50新事件/20重试/20唤醒；lease失效不得提交。无模型、网络、任意通知；仅机械专用kind。

## 7. projection_cursor与重建持久head

EV-02/03增加`active_generation:UUID|null`和`rebuild_state:JSON|null`，与scanned_sequence/revision同一行；只research_inbox.v1可非null。IT首次初始化持久generation后才能投影；未初始化返回projection_unavailable，不能用进程内head。其他consumer两列固定null。

InboxRebuildState闭合字段：candidate_generation(UUID)、domain_watermark(int≥0)、journal_watermark(int≥0)、last_replayed_sequence(int≥0)、state=building/catching_up/ready/blocked。候选不等active，last_replayed_sequence不超过已知journal水位；追赶时以CAS更新水位，不能把冻结初值冒充最终值。EV只提供存储/CAS，IT定义并验证领域语义。

正常重建重放不可变journal（用户操作、clock/control及撤回效果全含），不重投domain events生成新待办；candidate bootstrap repair journal不复制原request_id/domain_event_id。捕获双水位、追赶后短写锁再核lease、cursor.revision和无在途分片，原子切active_generation并清rebuild_state；CAS失败保留原头并重读。切换前进行中的manifest必须结清，blocked不跨代搬运。旧cursor→410，取消候选只清受控rebuild_state，不删除源journal/收据。

## 8. 四域HTTP及收据合同

详见各域JSON请求/响应；这些是新增API，不表示当前已注册。共同错误体在下一节。

**G0**：POST `/investor/research-entry/preflight`只读输入listing_ref/template_key/question，不创建Task、预算、采集或实验制品；缺材料返回coverage/gaps，不承诺研究完成。A股材料通过SnapshotQueryService精确字段与captured/available截止读取，candidate快照不能冒称verified evidence；`materials.context.financial_context`是HK/US试点，不用于A股材料覆盖。方法摘要来自MethodService.resolve_binding(listing,`company_research`)的真实回退链；模型摘要只含可安全公开的标识/可用状态，不含values、base_url或credentials。发送复用MessageInput与`client_message_id`，不是新造幂等字段。G0遵守原Agent开关，不依赖实验开关。

**QT**：创建POST `/investor/methods/quality-experiments`→201 draft，不执行模型。seal mutation_id+expected_revision固定spec/envelope，不创建Task；start另需spec_hash，才做两臂原子准入；cancel同CAS。Financial refs仍ObjectRef，spec/result/report为QualityArtifactRef。repeat_count=1..2；两臂同一授权模型，limits不是新增授权。既有MethodService.freeze_task/_freeze_package_receipt确实校验immutable包hash、applicable_kinds和tool_family成员可用；现有admit只有candidate shadow的内部method_override，**不存在实验context/两臂API**，QT-018..020新增typed内部admission且与override互斥，不接受客户端方法dict；候选仍shadow，实验不增加方法批准覆盖。两臂Task/grant/receipt/trial/binding/原mutation同事务，任一结构准入失败整笔回滚，之后预算原reserve/sent/settle边界保留unknown。

`GET /investor/methods/quality-mutations/{mutation_id}`→200闭合 `{schema_version,mutation_id,operation,experiment_id|null,result_refs:QualityArtifactRef[],response_status,response,committed_at}`；response是该operation已验证的原响应联合，不是开放任意dict；operation枚举create_case/create_experiment/seal/start/cancel/assess/report。无实验ID也可按owner查询；认证及已提交对象当前访问权限先行，feature关闭仍只读。404 not_found仅表示当前未见可访问的已提交收据，**不证明无在途请求**；客户端保留原key/body轮询或重放，不换UUID。BusinessError.current_refs禁止质量ref，刷新已知详情或原receipt提供的地址。

**IT**：GET `/investor/inbox` limit1..100，返回snapshot水位W、opaque分页cursor、changes_cursor及projection_health；cursor绑定generation/owner/filter_hash/W/排序末键。GET `/investor/inbox/changes`按journal sequence upsert/remove；过滤也推进实际读取序号；GET不顺手唤醒。POST `/{id}/actions`的request_id、expected_revision必填；snooze until须未来且≤30日；request_review另带expected_case_ref，服务实时核case/episode并调用同session原manual受理，不能先置in_review再enqueue。GET `/investor/inbox/operations/{request_id}`读取原响应，不以当前entry重建原收据。account只read/snooze/查看决定/原核对入口，禁止公司manual/acknowledge。

InboxContribution.v1必填owner_id、Subject、event_ref=`{event_id:UUID,sequence:int≥1}`、exact_source_refs、materiality(attention/context)、reason_code、evidence_status(current/source_unavailable/source_invalidated/superseded/historical)、display_text(纯文本≤300字)。可选case_ref、episode、claim_ref、monitor_condition_ref、topic_ref、decision_ref、reviewed_relation_ref、replaces_refs；没有已验证reviewed_relation_ref只能unlinked/context，不猜支持/反对。topic_ref=精确topic_revision，monitor_condition_ref=topic_condition；账户case/episode=null。可选适配器未交付显示未接入，不计覆盖成功。

**BT**：POST `/bank-topics`→201，idempotency_key是mutation ID；request_hash包含API/schema/object/body但去key。POST revisions/conditions用expected_revision，condition激活同事务追加新TopicRevision并CAS head；冻结topic_revision_ref仍指激活前材料，不环引。POST checks→202 run_ref，不直接伪成功check。condition/check读取精确历史；control用expected_control_revision。固定年度next_comparable_annual，met/not_met/insufficient_evidence/not_comparable/source_invalidated与运行态分开。source rows/原件hash、期间与单位都须核验；case只能archive不删证据。

**PT S1/S2**：operation_id作用域owner+ID，action进入hash，同键异action冲突；重放优先于当前CAS。S1创建→201 study_ref/run_id/result_id/input_hash，冻结snapshot/valuation/旧计算情景/价格/行业副本后入队；GET只读，不用当前价重算。最多3 alternatives、20冲击、csrc存储值；逐股>行业>全局，缺分类受影响行unknown，不偷偷退回全局。SavedCalculation.status=ok只是算术完成，质量complete/partial在details；无可计算项missing_inputs且results={}。

S2创建首次expected_revision/control_revision=0为create-only；新ref实际revision≥1，0不是通用Revision放宽。四predicate及deadline类型、eq绝对容差/其他比较禁止容差、注册早于due、冻结日历均遵守设计。binding allocate/revoke/declare_complete都核观察control、账户ledger、账户binding_head三重CAS；同账户竞争持同锁、有效分配不超源量，完整声明冻结集合hash/revision，后续变化失效。due与结果保留account归属；精确决定读取尚需PT新增，不能假设已有单决定GET。

各域取消只是领域展示；沿用pipeline_run既有状态，机械落failed+reason=cancelled且不重试，不新增全局cancelled枚举。

## 9. 错误合同及显式HTTP映射

唯一响应：`{"detail":{"code":"...","message":"...","field_errors":[{"field":"...","message":"..."}],"retryable":false,"current_refs":[]}}`。复用BusinessError/InvestorError/raise_http_for_investor_error；不新增四套异常协议。当前api/deps.py未注册feature_disabled等新增码，未知fallback=409。下表和errors.json是**待集成映射**，每个使用它的集成卡须显式登记并测状态，不能说已返回503。

| HTTP | code |
|---|---|
| 403 | 原writes_disabled；新增quality_scope_forbidden、inbox_access_denied |
| 404 | not_found（不存在/跨owner/不可访问，无他人ref泄漏） |
| 409 | revision_conflict、idempotency_conflict；stale_revision、idempotency_payload_mismatch、spec_changed、source_stale、idempotency_mismatch、source_conflict、basis_changed |
| 410 | cursor_expired（重新获取首屏，保留草稿） |
| 422 | 原invalid_reference/invalid_cursor/invalid_action；envelope_incomplete、method_capability_unavailable、invalid_assessment、unsupported_scope、invalid_dsl、unit_mismatch、incomplete_dependencies、unsupported_basis、calendar_unavailable、invalid_parameter、invalid_binding |
| 503 | feature_disabled、model_unavailable、dependency_unavailable、projection_unavailable、review_unavailable、event_delivery_unavailable |

401/认证/会话来源/429按原依赖，不强行映射为InvestorError；原授权/预算code不全局改HTTP语义。409通常retryable=false需刷新决定；同请求临时DB竞争/不可用可用dependency_unavailable/503 retryable=true，保持原key/body，不改为新付费请求。配置关闭feature_disabled=503、retryable=false；writes_disabled始终403。current_refs仅当前有权限的正式ObjectRef，新topic/PT ref须common受控扩展后才能进；质量引用用field_errors及质量详情/receipt。

## 10. 默认开关及关停语义

| 配置（全默认false） | 新动作/依赖 | 关闭后的机械处理与历史 |
|---|---|---|
| research_quality_enabled | 新case/草稿/封存/评分等新写 | 历史/receipt可获权读取；运行停止新业务写入，已发生用量/unknown结算继续 |
| research_quality_execution_enabled | start及每次发送/工具/完成边界；需quality_enabled及原授权 | 不再发送，取消/迟到usage照常机械结账，迟到结果不得评分；不替代模型 |
| research_inbox_enabled | UI及新用户操作 | 已获事件机械投影/重试/隔离/重建不停止；历史/操作收据保留 |
| bank_topics.enabled | 新建/编辑/启用/新检查 | 已入队未提交取消，提交前重查；机械结账和授权历史可读 |
| bank_topics.condition_checks_enabled | 条件调度/检查，需bank_topics及EV验收 | 静态页面保留，未提交计算取消；已终态check/outbox仍机械消费 |
| portfolio_stress_enabled | S1新创建/修订/调度/提交边界 | 推进epoch停止新排队，未提交工作终止，机械结账/历史可读；S1不依赖EV |
| decision_observation_enabled | S2新配置/自动检查，需S1+ledger+EV | 推进epoch取消未来工作，已持久结果/事件可消费，历史不删除 |

共享持久事件本身不设“随UI关闭而停写”的开关，随已迁移代码生效；不带模型/网络副作用，也不打开Agent/自动评审/外发原开关。持久后处理接受仍服从原开关、源eligibility及当前授权。读取与必要机械取消/结算不能被新写开关拦死；访问撤销始终优先。

## 11. 影响任务与变更登记

v1冻结：EV-01..12使用事件schema/canonical/resolver/事务与cursor；SV-01..06使用唯一可信更正合同；QT-001/010/013/018..020/039/040/042/045/046使用引用/方法/G0/quality-mutations及错误；IT-002/003/008及领域生产者/适配/重建任务使用Cycle与generation合同；BT合同/事件/集成任务使用check源身份；PT合同/事件/适配/配置使用account/方法隔离；XA引用/错误/配置集成负责组合验证。编号不取代任务卡原依赖。

已消解的设计简称冲突：topic observation→topic_check；RecommendationEvent UUID→PublicationEvent payload.id而非物理sequence；cycle revision→新列0起；migration号必须按实际head分配，绝不假定061可用。接口自身无待裁决冲突；未实现的能力均在上文明确，不是本卡完成证明。

验证范围：仅本目录JSON语法、Draft2020-12 schema结构与13个合成事件形状、规范hash/UUID/反例、字段覆盖；没有运行应用或真实E2E。准确命令与结果在本卡交付回报中记录。

### 本卡本地验证结果

实际使用`.venv/bin/python`执行仅标准库/jsonschema的内联检查，退出码均0：12个JSON以拒绝NaN的json.loads读取通过；Draft202012Validator.check_schema通过；启用FormatChecker的13个完整事件通过；13个payload SHA256/event UUID/source revision重算相等；3个canonical vector相等；9个标为schema层的反例全部被拒绝；manifest文件集合一致。其余7个反例属于尚待实现的跨字段/持久resolver/事务层，未声称已测试通过。fixture文件SHA256记录在manifest.json（排除其自身）。未导入应用模块、读取真实配置/账户/数据库或发网络请求。

### v1.1 银行精确内部事实绑定与分解DTO补充（2026-11-07）

主代理在BT-001/002独立主审发现：原FactBinding只有通用source_ref，无法冻结新增topic_fact_revision行的UUID和业务revision；TopicRevision DTO缺失设计已有的decomposition。经主代理明确授权，补齐银行本域闭合合同，不新增ObjectRef/EventSourceRef kind，不改变冻结事件信封或已有JSON示例。

- `FactBinding={stable_fact_id,metric,source}`；source以binding_kind判别：`existing_fact`仅允许已有peer_annual_snapshot的revision1与精确`fields.<field>`，或已有evidence的精确revision与null/value路径；`topic_fact`仅允许本域内部`fact_ref={id:UUID,revision:strict正整数}`。内部ref没有通用kind，不开放全局resolver。已有引用解析成功仍不等于事实已verified。
- 父TopicRevision冻结每个内部fact row UUID及其业务revision；BT-006/013回读owner、case、stable_fact_id、metric、row.revision及原件证据，不按稳定ID查“最新”。既有source分支不能绑定topic_revision、calculation或解释，避免事实绑定自循环。对外引用仍限父topic_revision UUID/ref.revision1与`facts.<stable_fact_id>`。
- TopicRevision新增必需nullable `decomposition`。非null时采用闭合Decomposition与金额CNY／fraction／bp数量DTO；所有数值沿用DecimalValue，null必须有unknown_reason，已知值必须有source_fact_ids且能在该父revision冻结bindings中找到。输入含两期NII/NIM及披露规模／利率／净变动；派生变化、增长、bp、残差、容差独立保留。缺输入不能complete，两effect均缺为unavailable，单项缺失保留partial，无法核对残差时rounding_consistent=null。BT-012随后实现计算，DTO解析不产生核验资格。

影响：BT-006/008精确冻结及回读；BT-010/011事实定位与原件审核；BT-012/013分解与resolver；BT-019修订/历史服务；条件dependency_manifest使用同一冻结FactBinding，影响后续BT-027至BT-033及检查服务。已有topic_revision.fact_bindings／decomposition和condition dependency_manifest均为JSON列，此补充不增加表或泛化授权。
