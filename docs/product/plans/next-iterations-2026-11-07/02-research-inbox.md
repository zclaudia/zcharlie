# 方向 2：每日论点变化收件箱

- 状态：设计主审完成；本文是实施设计，待实施与验收。
- 日期：2026-11-07；代码基线：`1f62fa4ac3dd22be5ef65550ae6ac0444098b313`。
- 用户结果：每天查看少量重要变化，知道它影响什么判断、依据在哪里、下一步需要做什么。
- 范围：同源 Web 与 Android 内嵌 Web；单用户、现有 A 股研究范围。
- 不修改本轮业务代码、数据库、真实设置，不发送外部通知。

## 1. 产品目标与交付边界

现有系统已经能保存变化、维护论点、安排复核并显示通知；新增价值是统一阅读与处置，不是再造研究执行器。
收件箱回答“发生了什么—关联哪项论点—证据是否可靠—接下来怎么办”，不产生买卖评分或盈利概率。
已读、待办处理、领域问题解决和正式观点有效性是四件不同的事，界面与存储都不能混用。

| 范围 | MVP | 后续或排除 |
|---|---|---|
| 输入 | 研究变化、评审定稿、复核周期、正式观点有效性、用户控制 | 专题与决定观察通过同一适配契约后接入 |
| 呈现 | 今日概览前三项、完整列表、证据详情、无 case 观察线索 | 个人化学习排序、跨公司宏观论点网络 |
| 操作 | 已读、已了解、延期、请求既有复核 | 批量确认、模型自动处置、自然语言直接改控制 |
| 归因 | 精确引用已有论点/监测条件；找不到则明确未关联 | 新模型语义归因、自动生成反证结论 |
| 调度 | 有界机械投影、重试、恢复、到期唤醒 | 新 Agent、新采集源、新外部推送平台 |
| 外部动作 | 无；请求复核仍过原授权和预算 | 不接入券商，不由收件箱改变实际账本 |

首期不把数据健康告警逐项搬入；只在其使具体研究依据失效时引用对应领域结果。
没有正式论点也可以显示观察线索；不能为了呈现提醒自动创建 `InvestorCase`。

## 2. 当前代码复用地图

以下行号针对基线，新增文件在任务卡中标明，不把拟新增能力写成现状。

| 当前路径与行号 | 已有职责 | 接入方式 |
|---|---|---|
| `storage/repositories/research_snapshot.py:51–110` | 快照与确定性变化同事务创建 | flush 变化后追加领域事件，不依赖 finalize |
| `research/ifind_chain.py:1483–1506,1520–1538` | snapshot+mark_partial 与 snapshot+mark_success 两条提交 | 两条都覆盖；消费重放不得回到解释/模型调用阶段 |
| `storage/models/analysis.py:158–179` | `ResearchChangeSet`、前后快照和 diff | 保留原件，事件仅带引用及有限路由摘要 |
| `research/worker.py:222–253` | finalize 尽力生成变化通知与触发评审 | 保留行为；新收件箱不依赖该路径交付 |
| `review_panel/service.py:539–595` | 原子定稿、比较、结算；通知异常被吞掉 | 比较保存后同事务 append 事件，位于 try/except 外 |
| `review_panel/notifications.py:206–270` | case 归属、静音过滤、尽力站内通知 | 继续服务旧通知；不把其写入成功当交付依据 |
| `investor/monitoring.py:131–150` | 事件合并、运行中 dirty、限制意见 | 在合并状态持久化时发事件，不重复扫描规则 |
| `investor/monitoring.py:332–364` | 手动复核、case 版本和幂等检查 | 请求复核动作复用此服务和原 UUID |
| `investor/monitoring.py:368–394` | 复核结算、partial 与需补充材料 | 结算同事务事件，保留原任务结果 |
| `investor/monitoring.py:478–518` | 发布 outbox 消费及维护 | 保留独立消费，不争用其 consumed 标志 |
| `investor/monitoring.py:456–475,520–554` | 通知更新 revision、按首次 sequence 分页、已读 CAS | 不照搬增量游标；旧行更新须产生新 journal sequence |
| `investor/controls.py:223–230,399–426` | 静音、归档、暂停及明确用户权限 | 控制事件同事务追加；读与写前再查当前控制 |
| `investor/publication.py:801–803,1259–1273` | 追加有效性事件与限制 | 同事务镜像事件，收件箱不得直接解除限制 |
| `storage/models/investor.py:26–43` | owner/listing 唯一 case 与历史版本 | case 可选关联，listing 是稳定证券身份 |
| `storage/models/reference.py:48–60` | 市场内唯一证券、内部 listing ID | 不用简称或六位代码充当跨市场主键 |
| `storage/models/monitoring.py:10–50` | 原通知、回执、复核、外发投递模型 | 不放宽现有 case 外键，不复用外发 outbox |
| `research/worker.py:397–405,473–494` | 单例职责按agent kinds区分；维护仍与执行串行 | 新隔离kind不加入原singleton集合，另设handler/进程 |
| `api/routes/investor.py:32–38,286–327` | 会话/API 认证、原通知/复核 API | 新路由复用认证/CSRF与错误风格，旧接口兼容 |
| `web/templates/overview.html:5–18`、`web/static/overview.js:55–97` | 今日概览与现有待办 | 新增研究事项卡，保留系统告警入口 |
| `mobile/src/ui/App.tsx:9–11,81–86` | 配对后进入内嵌 Web | 复用响应式页，不重做原生客户端 |

表内路径均相对 `src/investment_agent/`，唯 `mobile/` 相对仓库根目录。
`supplemental_rules.ResearchEventRevision`（`storage/models/supplemental_rules.py:116`）是投资事实版本，不是交付日志，禁止拿来承载本信封。

## 3. 完整用户流程与界面

1. 已有生产者提交领域结果及事件；未开启收件箱 UI 也完成已经接收事件的机械登记与结账。
2. 投影器验证精确来源、来源版本及控制，生成/更新事项；没有值得处理的变化则记录忽略原因。
3. 今日概览展示前三项及总数；列表可切“待处理/复核中/稍后/历史”，系统告警另列。
4. 用户点卡片进入详情，先看前后变化，再看论点/反证关系、证据状态和下一步。
5. 无语义评审支持的材料仅称“变化线索”；多模型分歧仅称“评审线索”，不当正式反证。
6. 已了解结束本次待办；延期仅改待办时间；请求复核显示原授权/预算结果及原任务身份。
7. 复核完成更新原事项并提示结果；若原任务运行中又收到新证据，沿原 dirty 机制继续处理。
8. 用户可回历史查看当时来源、处理版本、理由和后续变化，不改写当时判断。

```text
桌面：今日研究                         待处理 5 / 复核中 2 / 稍后 3
左列：事项列表                        右列：当前事项与证据
[需复核] 某公司现金转化假设             变化：本期现金流下降；来源存在冲突
  2 项新证据 · 上次处理后新增           关联：公司观点 v4 / 假设 claim-A
[观察线索] 某公司评审分歧               状态：尚待研究；不等于论点被推翻
                                      [前后值] [原文] [完整核验状态]
                                      [请求复核] [稍后] [已了解]
手机：今日研究 → 单列卡片 → 全屏详情 → 底部操作区
标题/证券 → 为什么现在看 → 一项关键变化 → 证据 2 → 操作
详情按顺序展开：变化 / 关联判断 / 证据与缺口 / 处理历史
返回恢复筛选与滚动位置；390px 不横向溢出，证据表转纵向字段。
```

空态分清“没有待办”“筛选无结果”“已接收但正在投影”；不能统一显示今日无事。
loading 显示骨架与上次刷新；离线保留当前页并禁用提交，恢复后取增量，不宣称支持完整离线资料库。
partial 显示“部分来源未能投影”、失败计数与最近成功时间；已有卡片仍可阅读，未知计数不写 0。
证据失效卡片保留原因与历史引用；危险动作禁用；403/409/429/503 在卡片内说明并保留输入。
存在结果不明的 mutation 时展示“确认是否已提交”，只能用原 UUID/请求体重取，不换 UUID 重做。

## 4. 共享领域事件：有界持久交付

IN-01 可先独立验收，方向 3/4 可依赖此任务；不要求先上线收件箱 UI。
仅向仓库内白名单消费者交付已有事务结果，不提供任意订阅、脚本回调、外部 broker 或通用消息平台。
公共信封如下，`sequence` 是数据库内部单调游标，不是业务时间或来源版本：

```json
{
  "event_id": "5e17c888-4fa1-5d70-83bc-a572ac4b7b11",
  "owner_id": "local_owner",
  "event_type": "review_panel.finalized",
  "source_ref": {"kind": "review_round", "id": 381, "revision": 1},
  "source_revision": 1,
  "occurred_at": "2026-11-07T09:30:00Z",
  "recorded_at": "2026-11-07T09:30:01Z",
  "schema_version": 1,
  "payload_hash": "<sha256-of-canonical-payload>",
  "payload": {"subject": {"kind": "listing", "listing_id": 18}, "comparison_ref": {"kind": "review_comparison", "id": 452, "revision": 1}, "notify_reasons": ["high_divergence"]}
}
```

`schema_version` 只描述信封/载荷契约，`source_revision` 描述业务对象；两者不能互相替代。
规范化 payload 采用固定 UTF-8 JSON、排序键、紧凑分隔、禁止 NaN；时间统一 UTC，Decimal 先按契约转字符串。
`payload_hash` 只覆盖规范化 payload；同源唯一键出现不同 hash 即冲突，不能覆盖或当重复成功。
源唯一键为 `(owner_id,event_type,source_identity,source_revision)`；identity 仅为 kind/id 的规范化值，不含 revision/field_path；子字段引用放 payload。
source_ref 若带 revision 必须等于 source_revision；引用不可变行没有版本字段时由适配器固定 revision=1 并登记语义。
event_id 从固定命名空间与源唯一键确定生成；同一次业务提交重试必须得出相同身份。
`source_ref`使用独立`EventSourceRef`闭合discriminated union，不直接扩展`ObjectRef`或接受任意kind/string ID。
现有`ObjectRef`闭合枚举与ID校验见`investor/common.py:45–113`，不支持review_round/comparison；不得靠伪装成review绕过。
resolver白名单：review_round→`ReviewRoundRecord.id` strict正整数（`storage/models/review_panel.py:55`）；review_comparison→`ReviewComparisonRecord.id` strict正整数（同文件:135），另验证round_id/algorithm_version一致。
research_change_set用strict正整数；review_cycle/case/recommendation_event用UUID；既有ObjectRef种类逐种映射其原ID规范与owner解析，不做字符串自动猜型。
上例comparison.id是独立行ID452，不能假设等于round.id381；comparison首次不可变行的source_revision=1，算法版本单列核验。
topic/decision新增来源在各方向领域模型实现后才注册UUID resolver，关闭适配器时不接受未实现来源；普通证据保留原ObjectRef校验。

## 5. 表字段、约束与索引

新增六张小表；JSON 中的关键查询字段必须落列，迁移不得修改既有历史行。
当前 SQLite 不执行外键约束；表中 FK 表达模型关系，服务必须独立验证引用存在、owner、kind、revision与listing归属。

| 表 | 字段与约束 | 索引 |
|---|---|---|
| `research_domain_event` | sequence INTEGER 自增 PK；event_id UUID UNIQUE；owner_id；event_type；source_identity；source_ref JSON；source_revision≥1；occurred_at；recorded_at；schema_version≥1；payload_hash；payload JSON；全部只追加 | UNIQUE(owner,type,source_identity,source_revision)；(owner,sequence)；(type,sequence) |
| `consumer_receipt` | consumer、event_id 复合 PK/FK；state=applied/ignored/retry/blocked_predecessor/quarantined；ordering_key；effect_manifest JSON nullable；effect_manifest_hash nullable；effect_cursor nullable；attempts；next_retry_at；last_error_code；脱敏 error_summary；processed_at；projector_version；effect_refs JSON | (consumer,state,next_retry_at)；(consumer,ordering_key,state)；consumer名称固定research_inbox.v1，不随重建generation变化 |
| `projection_cursor` | consumer PK；scanned_sequence；revision；updated_at；last_success_at；retry_count；quarantined_count | revision CAS；单例扫描水位，不充当“全部成功”水位 |
| `research_inbox_entry` | generation、id UUID 复合 PK；owner_id；subject_kind=listing/account；listing_id nullable FK；account_id nullable UUID FK；case_id nullable FK；case_episode nullable；group_key；revision；content_revision；state；read_content_revision；priority_rank；snoozed_until；control_status；source_status；last_event_sequence；latest_journal_sequence；view JSON | CHECK listing/account恰一非空且符合kind；case只允许listing；UNIQUE(generation,owner,group_key)；(generation,owner,state,priority_rank,latest_journal_sequence,id)；(owner,listing_id)；(owner,account_id)；(case_id,case_episode) |
| `research_inbox_journal` | sequence 自增 PK；generation；entry_id nullable；kind=source/user/clock/control/repair；domain_event_id nullable；effect_ordinal≥0；owner_id；request_id nullable；request_hash nullable；result JSON；entry_revision nullable；entry_view JSON nullable；recorded_at | UNIQUE(generation,domain_event_id,effect_ordinal)；UNIQUE(owner,request_id)；(generation,entry_id,sequence)；(owner,sequence) |
| `research_inbox_entry_source` | generation、entry_id、event_id 复合 PK；exact_ref JSON；relation=unlinked/support/opposition/context；relation_review_ref nullable；active；invalidated_by nullable；group_member_ordinal | (event_id)；(generation,entry_id,active)；外键 journal/entry 不级联删除历史 |

普通来源首期只路由一个subject/事项；一次源撤回可影响多个已关联事项，每个效果写独立ordinal/journal，并与唯一receipt同事务。
case控制采用entry_id=null的scope控制journal，增量返回scope_remove/control，列表实时校验该scope；不为归档同步扫描改写全部历史事项。
适配器不开放任意fan-out；源撤回关联过多时按显式分片契约保存receipt effect_cursor，完成全部分片后才applied，中途保持retry且同链后序阻塞。
分片首次处理时在receipt保存不可变effect_manifest（generation、按entry_id排序的目标集合、来源版本与hash）及其规范化hash；effect_cursor为下一个ordinal。每事务至多50个效果，投影、journal与推进后的cursor同事务提交，唯一journal键防重；单事件超过10000个目标则隔离并报告容量原因，不截断成功。
影子generation切换前必须结清进行中的分片；未结清则延后切换并报告阻塞原因，不在切换时修改冻结manifest或跨代迁移剩余目标。影子投影按完整事件重放包含这些撤回。尚未更新的卡片在读取/动作前实时校验源有效性；后建卡也先核验，不能把旧失效来源重新挂入。
领域事件、journal 禁止 UPDATE/DELETE；receipt 是处理台账可更新；entry/source 是可重建投影。
request_id 为用户操作 UUID；result 保存原响应、原 domain refs 与原视图，重放不依赖当前 entry 状态。
revision是全行CAS版本，content_revision只在实质内容变化时增加；read_content_revision记录读过的内容版本。已读增加revision和journal，不增加content_revision；新证据后unread=true。
group_key 首期为 `listing:<id>:cycle:<cycle_id>`、`listing:<id>:change:<change_set_id>` 或 `listing:<id>:round:<round_id>`。
账户决定观察使用`account:<uuid>:observation:<uuid>`，不与同股listing卡归组；账户卡结果与due共用观察身份。
不按文字相似度跨周期合并；若周期事件已明确绑定 change_set/round，可将其标为 related，不偷偷移动既有处理身份。

## 6. 持久生产、消费与 finalize 缺口接法

1. `ResearchSnapshotRepository.create` 保存 change_set 并 flush 后，在同一 session 追加 `research_change.recorded`；与 ifind_chain 的 snapshot+mark_partial（1485/1501）或 snapshot+mark_success（1522/1538）一起提交。载荷只声明已保存快照，不在 create 时猜最终 run 状态；结果详情通过 run 精确引用读取。
2. `finalize_round` 保存 comparison 后、现有 best-effort try 之前追加 `review_panel.finalized`；append 失败回滚定稿。
3. `MonitoringService._merge/settle` 在领域状态改变事务内追加周期事件；使用持久递增的 cycle_event_revision，禁止只用可变 payload hash 当版本。
4. `PublicationService._event` 同事务追加有效性事件；其 source_ref 指向 RecommendationEvent.id、revision=1，不能只指向始终 revision=1 的 recommendation。
5. `UserControlService._apply` case CAS 成功后追加 `case.control_changed`；与操作回执一起回滚。
6. 不从 `_upsert` 或旧通知 outbox 反推源事实；静音不应抹掉领域事件的持久存在。
7. 独立mechanical worker通过`research_event_maintenance` kind调用消费者：每轮至多50新事件、20到期重试、20延期唤醒，无网络/模型调用；不复用执行长研究的singleton轮询保证延迟。
8. 每条消费事务原子完成 entry/source/journal 投影、receipt 和 cursor CAS；失败不得提前写 applied。
9. 发生异常回滚本条，再开短事务登记 retry 与脱敏错误，并推进 scanned_sequence；后续事件可以继续。
10. receipt 唯一键保证两 worker 重放不重复；扫描游标推进与 receipt 同事务，retry 不被误当交付完成。
11. 瞬时错误按 5秒/30秒/2分钟/10分钟重试，累计5次隔离；未知 schema/hash 冲突立即 quarantined。
12. retry/quarantined 独立列入健康状态与 partial；隔离事件保留原文引用、错误和人工重放入口，不静默跳过。
13. 人工机械重放不调用模型；使用审计操作恢复 receipt 并再消费，旧隔离历史保留在 journal repair 记录。
14. 控制事件是消费者优先处理的约束；即使其投影滞后，列表和操作仍实时检查 case 当前状态。
15. ordering_key由可信适配器给出，例如owner+case/episode、owner+round、owner+decision-observation；同链前序retry/quarantined时后序记blocked_predecessor，不可覆盖前序。独立对象继续消费；关联同一事项的多类事件使用同一归组链。
16. 安全收紧控制（静音/归档）实时检查，不依赖越过前序来“消费成功”；普通投影仍按链序。补偿/人工解除阻塞须记录repair及所依据源版本，不能把quarantined当已应用。
17. IN-01b单独交付既有后处理的durable接受：`watchlist_sync.v1`与`review_admission.v1`各用唯一receipt消费research_change事件；读取同事务提交的run终态，调用原同步/trigger_change_review，入队和receipt同事务。当前同意/额度/冷却不满足记ignored原因，不在之后偷偷重试成付费研究。
18. IN-01b启用后，匹配新事件的finalize不再直接走同一后处理；历史无事件路径保留。receipt只证明同步/原评审入队已接受，不证明模型研究完成；已受理模型任务由原队列恢复，消费者永不重做模型。旧best-effort缺口只有IN-01b验收后才可宣称已补。
19. `_sync_watchlist_refresh`与`_trigger_change_review`当前都自开session；IN-01b提取接收session的内部函数，旧wrapper保留兼容，consumer只调用新内部服务，使同步/入队与receipt原子。trigger返回typed accepted/ignored(reason)，不以None猜拒绝原因；原模型请求不进入此事务。
20. 自动评审仅接受非backfill、源提交时原开关/范围已允许的请求，payload持久化当时eligibility与设置引用；当前边界再次核验。该快照不授予权限；缺原接受依据记ignored，不能在稍后开启功能时补成付费请求。

现有评审提醒在 savepoint 中失败后直接返回 false，且 round 已终态，不能指望再次 finalize 补通知。
新路径以同事务事件成为恢复依据，旧通知可继续兼容；收件箱 UI 不再从旧通知重复渲染相同 review_round。
`_notify_meaningful_change`产生Markdown文件仍保留best-effort限制，IN-01b不覆盖文件通知交付；不能宣称整个finalize丢失风险已经修复。
生产者失败只重试原确定性事务边界，不重做已经结算的模型请求；若上层边界尚不能保证，先补 repository 级故障测试再接 UI。

## 7. 事件来源与语义路由清单

| event_type | 精确源身份/版本 | 事项处理 |
|---|---|---|
| `research_change.recorded` | change_set.id / 1 | meaningful 或 source_changed 才生成线索；baseline/noise receipt=ignored |
| `review_panel.finalized` | round.id / 1，绑定 comparison 算法版本 | high_divergence/stance_flip；失败或无重点仍登记 ignored |
| `review_cycle.changed` | cycle.id / 新 cycle_event_revision | 合并、dispatch、dirty、completed/stopped 的明确阶段 |
| `recommendation.validity_changed` | RecommendationEvent.id / 1 | 受限/撤回/替代/续期；不以提醒确认改变领域有效性 |
| `case.control_changed` | case.id / 新 case revision | archive/reopen/pause/resume/mute/unmute；不生成普通变化噪声卡 |
| `source.invalidated` | 后续适配器已注册的可信撤回记录typed ref / 原版本 | payload含exact invalidated_refs/理由/替代refs；MVP通过有效性事件处理正式意见撤回；无可信原记录不产生此事件，不新建任意字符串invalidation来源 |
| `topic.observation_changed` | topic observation.id / 版本 | 后续白名单，按下一节适配；不从原始抓取成功推定研究结论 |
| `decision_observation.due` | observation.id UUID / 精确观察revision | 后续账户待办；取消后不产生新的due |
| `decision_fact_check.completed` / `partial` | 核对calculation.id UUID / 1 | 结果绑定observation/decision/previous_check，partial保留缺口 |
| `decision_fact_check.superseded` | 新核对calculation.id UUID / 1 | 替代旧结果的精确ref，旧事件不改写 |
| `portfolio_stress.completed` / `partial` | stress calculation.id UUID / 1 | 默认仅主动结果可查，receipt=ignored，不自动入每日待办 |

源为可变对象时，必须在其自身事务中增加明确版本；同版本不同 payload 是实现错误。
receipt ignored 需明确 reason：noise、no_material_signal、unsupported_optional_adapter 或 control_suppressed；不等于执行失败。
原件撤回只停用相应 entry_source；其他独立证据仍在。只剩失效来源则 entry=withdrawn，并保留处理历史。MVP只接受有可信不可变撤回记录的适配器；无此记录时显示source_unavailable并禁止相关动作，不伪造撤回事实。
源被替代后，旧确认只对应旧 source revision；新版本有独立事件身份，不能沿用旧确认声称已核查。

## 8. 与专题、决定观察的适配契约

公共适配器输入为已持久事件信封；输出为 `InboxContribution.v1`，只在本地消费，不跨系统 HTTP 回调。
必填owner_id、subject、event_ref、exact_source_refs、materiality=attention/context、reason_code、evidence_status、display_text；subject为listing(listing_id严格正整数)或account(account_id UUID)的受限union。
可选 case_ref/episode、claim_ref、monitor_condition_ref、topic_ref、decision_ref、reviewed_relation_ref、replaces_refs。
claim/condition 必须指向精确父对象版本及稳定字段身份；没有稳定子 ID 时用 exact parent ref+field_path，禁止按文字找近似论点。
`reviewed_relation_ref` 必须可回读并匹配来源 hash；否则 relation 固定 unlinked/context，不能呈现支持/反对的已评审结论。
专题事件必须指出报告期、比较基准与口径变化；本卡不计算银行/白酒指标，不替专题授权抓取。
决定观察必须指出当时 decision_ref、观察窗口和 factual outcome；不能由价格上涨自动映射“决定正确”。
拒绝owner/subject与源对象不一致、来源不可见、未支持schema或任意HTML；整账户事件保留account subject，不按持仓自动拆成股票卡。
account卡只允许read/snooze/查看决定/回到原核对入口；公司manual永不可调用。账户/观察head控制、授权、取消与权限epoch由方向04原服务再次检查，卡片只投影，不另建一套授权。
account的due卡由原核对completed/superseded事件结束待核对状态，完成结果保持unread可看；partial仍提示缺口。用户已读只清“待查看结果”计数，不把尚未完成的核对标完成。
未交付的适配器在健康页显示“未接入”，不计为覆盖成功，也不阻塞核心四类事件。

## 9. listing 无 case 与用户控制

无 case 用 `(owner_id,listing_id)` 归属；沿证券身份、观察池有效状态校验，不按名称猜映射。
account subject用(owner_id,account_id)归属，case_ref强制null；与listing无case路径不同，不自动关联公司论点。
只允许查看评审/变化、已读、已了解、延期；“开始研究”跳既有明确建档/对象选择流程，不能由后台自动创建。
后续建档追加关联 journal，保留 entry.id 与原线索历史；关联不使旧材料自动成为正式观点证据。
观察池条目停用/移除时，该来源的观察线索从默认待办隐藏；历史可查；重加不自动重放旧噪声。
已有 case 的事项必须继承当前 case 控制，禁止通过 listing 路径绕过同股 case 的静音或归档。
归档：未决事项 control_status=archived、从待办排除；旧记录可查，复核拒绝；reopen 是新 episode，不复活旧 pending。
静音：从默认列表/计数排除，显式“已静音历史”可查；静音期间事件机械登记 suppressed，不自动触发收件箱研究。
取消静音不重放静音期间噪声；显示“期间有未审阅变化”汇总入口，用户明确打开后才创建一次当前补看事项。
暂停：仍可站内阅读变化，标“后台研究已暂停”；不由本卡改为 background grant；明确手动请求仍由原 manual 服务决定。
其他暂停/归档/静音开关保持各自语义；任何控制变化都不自动判定论点成立或解除硬约束。
读取详情与提交操作都实时校验owner、subject归属、case状态/episode或account观察控制、引用有效性，投影缓存不构成权限凭证。

## 10. 状态转换与冲突规则

| 输入 | 原状态 | 新状态/行为 |
|---|---|---|
| 新可处理来源 | 无事项 | open、unread |
| 已读 | 任意可见状态 | 更新read_content_revision与行revision，不结束待办 |
| 已了解（listing） | open | resolved，记录用户理由；不解除领域未决问题；account不提供此动作 |
| 延期至未来 UTC | open | snoozed；记录本次内容 revision |
| 到期时钟事件 | snoozed 且版本仍匹配 | open；相同到期事件至多一次 |
| 请求复核受理 | open | in_review，绑定原 cycle/task；预算拒绝保持 open |
| 复核已有可用结果 | in_review | open 显示结果待阅读；用户已了解后 resolved |
| 复核 partial/failed/cancelled | in_review | open，暴露具体停止原因，不写成已解决 |
| 新实质来源 | resolved/snoozed | open、unread，取消旧延期；显示“延期后新增证据” |
| 新来源但复核仍在运行 | in_review | 保持 in_review、unread，绑定 dirty，不另开任务 |
| 所有来源被撤回 | 非终止状态 | withdrawn；不可请求复核，保留历史 |
| 纯重放/时间戳变化 | 任意 | 不增加内容 revision、不唤醒 |

新内容与延期同事务 CAS：先提交者成功，后者409；用户必须看到新增证据后才能重新延期。
取消延期只有显式操作/新实质事件；不能在读页面时顺手写入。批量已了解不在 MVP。
迟到旧来源版本保留审计，但若被已知更新版本支配，不改变当前内容或唤醒；依赖乱序会进入 retry，不能猜补事实。

## 11. API、JSON、CAS 与游标

新增 `api/routes/research_inbox.py`，使用现有认证/CSRF；owner 固定从可信会话取，客户端不得指定。
GET `/investor/inbox?state=open&limit=20` 返回快照水位 W 与 opaque next_cursor；limit 1–100。
列表使用 journal 中 sequence≤W 的每事项最近 entry_view，按 `(priority_rank ASC,latest_journal_sequence DESC,id ASC)` 分页。
cursor 绑定 generation、owner、筛选 hash、W 和末行排序键；后续页不能按当前 entry 状态直接过滤而漏行。
实时控制可能隐藏快照中的卡片，返回 suppressed_count；扫描直到凑够页或耗尽有界扫描额度，不能靠空页宣布结束。
GET `/investor/inbox/changes?after=<cursor>&limit=100` 按 journal sequence 返回 upsert/remove，不能只按 entry 创建序号。
change cursor 含 generation/owner/last_sequence；下一次只推进到实际读到的最后记录，过滤后的移除也占序号。
首屏和详情返回 projection_health、pending/retry/quarantined 数、as_of；不把扫描水位解释成已处理完整。

```json
{"items":[{"id":"inbox-uuid","revision":4,"state":"open","unread":true,
  "subject":{"kind":"listing","listing_id":18},"case_ref":null,"scope":"watchlist_lead","reason_code":"review_divergence",
  "source_status":"current","claim_ref":null,"relation":"unlinked",
  "title":"多视角评审出现分歧","allowed_actions":["read","acknowledge","snooze"],
  "source_refs":[{"kind":"review_round","id":381,"revision":1}]}],
 "next_cursor":null,"changes_cursor":"opaque-g1-57","as_of":"2026-11-07T09:31:00Z",
 "projection_health":{"status":"partial","retry":1,"quarantined":0}}
```

POST `/investor/inbox/{id}/actions`：所有写操作带 request_id 与 expected_revision。
request_review 另带 expected_case_ref；服务端复核当前 case/episode 后在同事务调用 manual，以 request_id 作为原幂等 key。

```json
{"request_id":"855a75bb-bdfe-48fd-a2ac-b8ca608a8c20","expected_revision":4,
 "action":"snooze","until":"2026-11-09T01:00:00Z","reason":"等待周末核对"}
```

```json
{"request_id":"855a75bb-bdfe-48fd-a2ac-b8ca608a8c20","replayed":false,
 "item":{"id":"inbox-uuid","revision":5,"state":"snoozed","snoozed_until":"2026-11-09T01:00:00Z"},
 "changes_cursor":"opaque-g1-58","operation_status":"committed"}
```

先认证并检查原操作对象的owner/可见性，再按owner/request_id查回执；同请求hash返回原结果，异参409。归档/feature关闭仍可读取有权限的原回执，不能绕过访问撤销。
新操作再检查开关、引用、控制和 CAS；成功事务必须同时写 entry/source、journal 回执及原复核受理，不允许“先置复核中再 enqueue”。
错误一律复用`api/deps.py:54–92`的BusinessError包裹：`{"detail":{"code":"revision_conflict","message":"内容已更新","field_errors":[],"retryable":false,"current_refs":[]}}`，不另造错误体。
已有401/认证与429/节流保持原依赖语义；404 not_found、422 invalid_reference/invalid_cursor/invalid_action、409 revision_conflict/idempotency_conflict复用原映射；延期须未来且≤30日。
新增code必须显式加入`INVESTOR_ERROR_STATUS`：inbox_access_denied→403、cursor_expired→410、projection_unavailable/feature_disabled/review_unavailable→503、source_stale→409；否则原fallback是409，不得只在文档写503。
原复核预算/授权错误保留原code与已定义状态，不全局改它们的HTTP语义；不伪装成功、不改grant、不自动跨日付费重试。
410 返回新快照入口；请求体与草稿保留。503/超时后查询 GET `/investor/inbox/operations/{request_id}`，不存在时才按原请求重试。

## 12. 重建、补记与保留

正常重建从不可变 journal 按 sequence 重放到新 generation 的影子 entry/source；用户处置不依赖模型重算。
影子初始化为每个entry追加新generation的bootstrap repair journal（不复制request_id/原事件ID），以供稳定快照分页；原用户回执保留在原journal，不重复插入。
receipt消费者身份及待重试台账不因重建变化；影子追平后续journal，切换后新的源效果只写当前generation，不能把已消费事件重新生成一次待办。
MVP保持归组算法不变；未来从domain events重新归组须另行设计版本迁移，按原source refs映射确认，无法一一对应标“需重看”。
切换 generation 前记录 domain/journal 双水位，短写锁下追平并原子切头；操作期间 CAS，旧 cursor 返回410。
历史 source 事件只补记原可证明结果：旧 review_round/comparison、change_set、PublicationEvent；必须有有界manifest（精确源ID、期间、条数上限、截止水位与执行摘要），payload标backfill=true并引用manifest_hash；保持可证明的原occurred_at，recorded_at为本次补记时间，无法证明原发生时间的行在manifest记排除原因，不伪造时间补事件。
旧可变 ReviewCycle 没有历史版本时只记一次 migration_snapshot，不能声称重建了每一次阶段变化。
首次启用默认仅新事件进入待办；历史补记标 historical，不主动唤醒，不重新解释、不外发通知、不启动付费复核；显式“检查既有未决事项”才建当前基线，后续付费仍走原授权。
无来源可回读、hash冲突、未知schema均隔离；提供计数与单条诊断，不清空投影掩盖缺口。
MVP不自动删除 domain/journal/用户回执；快照分页 W 保留至少7日，过旧 cursor 410；磁盘增长纳入健康指标。
独立维护kind每30秒由scheduler幂等入队，最多一个queued/running，专用`ia worker start --kinds research_event_maintenance`只跑机械任务；用原lease/心跳/孤儿恢复，批次≤2秒目标，超时留cursor续跑。
不让该kind触发原singleton的deliver_one/研究维护；queue设置与Docker进程增加独立容量。延迟由真实长研究并发测试验证，scheduler/专用worker离线须显示lag，不把60秒当离线承诺。

## 13. 迁移、开关、部署与回退

迁移使用`next_revision`占位，编号由实施时的 Alembic 单 head 与四方向实施顺序统一分配，不抢占061；先新增共享三表，再新增收件箱三表。
增加 `research_inbox_enabled=false` 只控制 UI 与新用户操作；不会关闭已接收事件的机械投影/重试/隔离结账。
共享持久事件写入随已迁移代码生效，无模型/网络副作用；不把原 Agent、评审、后台执行开关一起打开。
部署顺序：备份/临时库迁移演练→代码+迁移→生产者双写→机械消费者验证→显式开启UI；本设计不授权执行这些操作。
回退首选关UI并保留表与机械消费者；不降级删除含用户处置的表，不修改原通知/外发开关。
必须回滚旧代码时记录最后事件水位；旧代码期间不会写新信封，重启新版后先有界源对账补记，显式显示覆盖间隙。
旧代码兼容验证失败则停止发布，不能以drop表或重跑研究消除差异；生产迁移与功能启用需另走既有部署流程。

## 14. 实施任务卡与退出条件

| 卡 | 文件边界（新增标+） | 退出条件 | 估算 |
|---|---|---|---|
| IN-01 共享持久事件 | +`research_events/contracts.py,service.py`、+`storage/models/research_events.py`、+`next_revision`迁移、models注册；research_snapshot/ifind_chain提交边界 | 信封/唯一键/hash冲突；success与持久snapshot的partial均同事务；receipt+cursor原子、双writer、按链阻塞及隔离通过；重放不调用模型；无UI依赖 | 3–4人日 |
| IN-01b 原后处理持久受理 | `research/worker.py`、`review_panel/triggering.py`、+共享消费者适配器；提取同session同步函数 | watch同步/评审入队与receipt原子，源与当前同意双查，backfill不付费，返回拒绝原因；原模型任务不重做 | 2–3人日 |
| IN-02 源生产者与历史对账 | `research_snapshot.py` repository、`review_panel/service.py`、`investor/monitoring.py,publication.py,controls.py`、+对账工具 | 四核心来源同事务交付；finalize后故障不丢；历史阶段不可证明明确标记 | 3–4人日 |
| IN-03 收件箱模型/归组 | +`storage/models/research_inbox.py`、+迁移、+`research_inbox/contracts.py,projector.py` | listing无case、relation约束、source撤回、内容版本与read分离、无模型投影 | 3–4人日 |
| IN-04 操作与控制 | +`research_inbox/service.py`、复用controls/manual边界 | UUID重放、CAS、归档/静音/暂停、延期并发、复核受理原子 | 2–3人日 |
| IN-05 API与游标 | +`api/routes/research_inbox.py`、`api/deps.py`映射、router注册、dashboard聚合 | 稳定快照分页、增量旧行更新、410、BusinessError状态表、操作查询 | 2–3人日 |
| IN-06 隔离维护与恢复 | +`research_inbox/maintenance.py`、`research/worker.py` registry/kinds、scheduler、Docker服务与健康页 | 专用kind/lease/30秒单活入队；长研究并发仍机械结账，禁UI不停，超时续跑与lag可见，不调用deliver_one | 3–4人日 |
| IN-07 Web与内嵌移动 | `web/routes.py`、+`templates/inbox.html`、+`static/research_inbox.js,pages/inbox.css`、overview | 1440/390真实浏览器主流程；输入保留、空/错/partial、无横向溢出 | 3–4人日 |
| IN-08 联合验收/适配冻结 | +领域事件/收件箱单元和集成测试、+浏览器fixture、本文补验收记录 | ≥16场景、故障恢复、topic/decision契约样例、迁移往返；真实数据试用另计 | 2–3人日 |

路径除迁移/Web模板等外相对 `src/investment_agent/`；repository 接缝为前文准确路径，不新建重复 research persistence 层。
顺序 IN-01→01b/02/03→04/05→06/07→08；IN-01可被其他方向单独依赖，IN-02中共享生产者由单一writer统一集成。
完整工程约23–32人日，即5–7人周，包含新增后处理持久受理与执行隔离；只读核心UI可2–3人周，但不代表完整闭环。

## 15. 验收矩阵与产品目标

| 场景 | 必须观测的结果 |
|---|---|
| 1 源事务失败 | change_set/round变更与事件一起回滚，无孤立通知 |
| 2 定稿后进程终止 | 重启仅消费原事件，不重新调用模型、不重复结算 |
| 3 重复/异参源 | 同唯一键同hash幂等；异hash隔离报警，旧事件不覆盖 |
| 4 消费中断/双writer | 投影、receipt、cursor全成或全无；无重复事项 |
| 5 毒事件/瞬时失败 | 独立对象继续；同对象后序blocked_predecessor，不越过覆盖；retry/隔离/解除阻塞可见 |
| 6 无case观察条目 | 分歧卡可查看/延期；不建case、不允许无授权复核 |
| 7 旧行新增内容 | 手机按changes cursor必能看到revision更新 |
| 8 新证据撞延期 | CAS一方成功；另一方409；新增证据取消旧延期 |
| 9 旧确认与丢回执 | 原UUID返回原结果；不得确认新revision或重复建任务 |
| 10 静音/取消静音 | 默认列表受控；期间事件不丢、旧噪声不自动重放 |
| 11 归档/重开 | 旧episode事项不可请求研究；重开不复活旧待办 |
| 12 暂停/预算不足 | 阅读保留；复核走原权限，拒绝不置in_review |
| 13 来源撤回/部分失效 | 单源停用；其他来源保留；全部撤回明确withdrawn |
| 14 partial复核 | 显示未知与停止原因，不显示论点已解决 |
| 15 快照分页期间新增/控制变化 | 无排序重复遗漏；当前静音可隐藏，计数解释；增量补新事件 |
| 16 影子重建/算法升级 | 用户处置保留；generation切换原子；旧cursor410 |
| 17 禁用UI/回滚后升级 | 机械结账继续；覆盖间隙可见；历史表不删除 |
| 18 专题/决定恶意适配 | owner/listing不符、无评审关系、未知schema均拒绝或隔离 |
| 19 真实浏览器/Android内嵌 | 详情返回、原UUID恢复、错误/空/partial、390px可操作 |
| 20 不越权 | 无新增模型调用、外发、账本变更或权限扩大；trace可证明 |
| 21 整账户决定卡 | subject账户身份核验；不拆股票、不关联case、不调用公司manual；原控制撤销即停止新动作 |
| 22 多事项源撤回 | 有界分片与effect_cursor恢复；已完成效果不重复、未完成不漏，同链后序阻塞 |
| 23 长研究与错误协议 | 并发长模型请求时独立机械worker仍满足lag目标；403/410/503经共享状态表与固定detail包裹 |
| 24 既有后处理恢复 | 原已同意评审至多受理一次；partial快照进入收件箱但不越过原success-only评审条件；backfill不外发/不付费 |

工程目标：源引用可回读100%；重复投影0；旧版本误确认0；恢复漏更新0；采集提交至投影p95≤60秒（正常worker下）。
试用目标：连续两周、人工标注≥30事项，值得查看比例≥70%、每日处理用时中位数≤10分钟；样本不足标未验证。
不把点击/采纳率或事后股价表现作为研究正确性指标；每日无重要变化允许空收件箱。

## 16. 风险、外部资料与取舍

主要风险：事件日志扩成平台、来源引用不可回读、状态与权限漂移、事项合并遮住反证、共享生产者并发改写。
控制办法：白名单消费者/固定适配契约、精确refs、实时控制校验、原子回执、逐源展开、共享接缝单writer。
主审已确认：IN-01作为三方向共同基础；新增cycle_event_revision契约；保留静音补看入口，并区分行revision与已读内容revision。上述规则仍须在IN-01至IN-08中验证。
这些是设计决策，不以历史10月探针推断当前生产研究质量；真实运行时配置本轮未读取。

- [changedetection.io API](https://changedetection.io/docs/api_v1/)：已打开官方文档；采纳历史快照diff与过滤失效可见，不引入通用抓取服务。
- [Feedly内容去重](https://docs.feedly.com/article/218-how-does-deduplication-work)：采纳归组阅读；拒绝照搬85%近重复阈值及跨版本自动已读，财报更正不能被吞。
- [Novu工作流](https://docs.novu.co/platform/workflow/add-and-configure-steps)：采纳digest/delay/throttle分开表达；已有队列/outbox足够，不部署整个平台。
- [Anthropic thesis-tracker](https://github.com/anthropics/financial-services/blob/main/plugins/vertical-plugins/equity-research/skills/thesis-tracker/SKILL.md)：采纳可证伪论点与更新日志；不引入目标价/止损或未校准信心等级。
- [Relay](https://github.com/GawainTheCoder/project-relay)：采纳证据、论点评估、用户决定分离；公开历史有限且本轮未运行验证，不采用其代码或任意数值门槛。

本轮已检索并打开以上页面，仅作为交互/架构取舍参考；方案适配本仓库既有契约，未安装或调用这些项目。
