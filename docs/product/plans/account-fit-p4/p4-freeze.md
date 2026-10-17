# P4 阶段 A 契约冻结（AF-P4-00）

2026-10-17，Asia/Shanghai。用户已授权 AF-P4-00—06 实施与工程验收；不含 B 联网来源、真实付费研究、生产操作/启用、提交或推送。实际基线 HEAD `7df697a`（P3已提交），移动端 Cargo.lock 等并行内容保留。依据[经审查计划](../account-fit-p4-plan.md)及 P2/P3 最终冻结/验收；本记录的测试结果另在 acceptance.md 记载。

## 存储与身份

新增053（052之后），仅三窄表：不可变 supplemental_change_receipt、CAS supplemental_source_state、CAS supplemental_refresh_state。原件、PipelineRun lease/heartbeat/attempt、P2受理与检查点、BudgetEntry、ReviewCycle、PublicationOutbox和NotificationDelivery复用。

receipt UUID及sequence分别为稳定身份/提交顺序；unique(owner,change_key)，根命令operation_id非空时partial unique(owner,operation_id)，request_hash绑定命令/path/完整body。stage/member行不得复用根operation_id。hash统一P2canonical；重放比较原业务内容并沿用原时间，不能fresh now造成冲突。不可变payload/索引头部全部核对；损坏拒绝，不当unknown。

scope_member唯一(parent_sequence,listing_id,context)，有(owner,listing_id,context,sequence)索引；新增内部scope_outcome/scope_handoff/evaluation_admission证明行仍只属P4，不建立通用消息平台。source/refresh完整输入与未确认集合可由journal+冻结PipelineRun恢复，不能只保存dirty最高序号。新表业务写入同事务检查run_id/worker_id/attempt/running和state CAS。053降级拒绝三新表数据及旧assessment V2/source binding、run/task/grant P4溯源载荷；新表空仍需守卫。050/051/052文件不改。

P2 lifecycle/events已经原子排run，hook同事务只追加origin_run_id/source_manifest_hash的journal；不另排同批评估。P4认领原冻结pair及真实产物，运行中等待原run；capped/partial仅补未完成成员且不复活终态。P4新批次仍经accept_evaluation的现有严格命令收据链，P4关联收据控制附加开关边界，不放宽P2authorizer。

## 时间、调度与授权

current-limits读取/发布/续期同步计算，GET零DML并保持P2局部SQLite读快照。due绑定具体revision+边界种类+绝对时刻，覆盖规则/application/研究check_by/事件时间和正式结论到期。补偿扫描不得按UUID最大值/公布日期忽略迟到材料。

维护页最多50scope（硬上限100）；目标冻结段最多2000个listing ID、每派发页100个listing，超段保留原high-water/after_listing_id以续冻，不能把页大小当永久总范围cap。P2仍2000pair/4MiB，P4维护并发1；固定high watermark+成员集合，公平处理历史和新变化。未持久化不ACK，队列满保留receipt/游标；关闭仍记已有领域变化，禁止新P4评估/研究，恢复不复活终态付费任务。

updates/polling默认false；A只使用updates，polling仅冻结默认值，不注册联网任务或来源授权接口。P4背景研究grant附加可空的server batch UUID/hash，旧grant保存字段域比较/hash不改；新admission与cycle关联同事务，后续每次reserve/sent/publish前重核updates∧supplemental及既有agent/background/Mandate范围、价格/预算/暂停。未知费用停止自动重发并沿既有对账；失败/cancel/unknown不自动换cycle花钱。dirty逐成员持久交接，不能只保留dirty[-1]。

effective_cutoff/observation.py白名单不变，来源水位和墙钟均不能推进旧任务知识窗；新资料限制旧结论并等待授权内的新研究。暂停/退役/到期/缺来源不解除旧问题；财务基础分/Quality/DSL/硬约束、账户与公司研究隔离、reduce/exit边界保持。

## A 的受控导入与来源覆盖

POST imports仅接受精确listing/context及现有可回读的evidence/disclosure_original/research_dataset refs，最多64条；不接受任意URL、原始verified/reviewer或付费授权。按owner/subject/revision、原件bytes/hash/可见时间/当前更正状态验证；不支持的政策原件明确拒绝，不硬塞CNInfo。服务端重算manifest_hash=H({listing_ref,context,sources})，sources为按ref身份稳定排序的{ref,content_hash,status,acquired_at,available_from}。不创建verified事件。

本地来源固定controlled_local_v1，version=1，coverage=explicit_saved_refs，健康时限server86400秒；contract_hash=H({source_id:'controlled_local_v1',version:'1',allowed_kinds:['disclosure_original','evidence','research_dataset'],coverage:'explicit_saved_refs',health_limit_seconds:86400})。state key=listing:<id>:<context>，声明精确listing_ref/context。scope_hash=H({source_id,contract_version,contract_hash,scope_key,listing_ref})。完整仅代表该组已保存refs已检查，不承诺官方或历史全覆盖；冲突/更正状态保留partial，没采集到不得显示无风险。

source state保存contract/scope/hash、source_refs、window_start/end、complete_watermark、coverage_state、success_sequence/receipt_hash、last_attempt/success、failure/retry和固定健康时限。覆盖绑定采用V2：UpdateSourceBinding(listing_ref,context,scope_key,source_policy_hash,sources[1..16],observed_at)，每entry含source_id/contract_version/hash/scope_hash、success_sequence/receipt_hash、source_refs、窗口/水位/完整性、last_success_at/health_limit_seconds。V1DTO及完整model_dump/hash域不变；按原schema显式parse/hash分派V1/V2，prepared/published同版，绑定服务端构建并进入manifest/语义评审hash。旧行不回填/重哈希，缺新覆盖显示not_assessed；已声明P4scope则同步unknown/stale/incomplete并限制新方向性发布，不误伤无关scope。

## 通知与观测

正式通知仍沿发布outbox/站内投影/已授权delivery，运维告警仅本地认证operations API和单股scope状态。capacity_blocked恢复沿原ID/body，writer锁下受256总容量限制，重核station/case revision、config fingerprint、mute/pause/archive。failed终态；unknown只有已明确幂等接收方可沿原ID限次重试，终态无重生入口。不开新外发目的地。

HTTP明确403关闭/404隔离/409CAS或幂等冲突/422形状来源容量错误；关闭仍可读/重放原收据。浏览器1440/390展示来源覆盖、最后成功、失败、due、积压、处理进度/待人工，不以0冒unknown；切主体无残留。实际验证矩阵沿计划F01—07、F09—14（F08联网留B），真实SQLite/HTTP/worker故障注入及必要全量回归，所有测试临时库/目录+惰性key/可控provider，不访问.dev/生产。

## 整合后的具体裁决

- `scope_member.id` 在首次持久化时生成 UUID，由唯一 parent/listing/context 身份与相同内容重放返回原 UUID；不以 UUID 大小表示提交顺序。确定性 change_key/运行 key 和冻结请求 hash 承担幂等，不要求 UUID 本身可重新推算。协调器 key 使用源 sequence + 段/页/恢复代次；源 UUID/payload hash 同时进入冻结请求。
- 目标段的 listing ID 是冻结集合。每个 listing 的本地 P2 受理控制 pair/byte 上限，实际 scope/pair 必须被完整授权 manifest/checkpoint 证明；页/段 complete 与单成员评估成功分别表达。原 P2 manifest 完整受理的成员认领原身份，其他成员不冒充成功。
- 恢复命令为仅服务端可构造的 `supplemental_rule.update_recovery.v1`；原 EvaluationRequest、用户 API 形状和旧 hash 不增加默认字段。新 body 精确绑定原 run/hash、P4 source/member、pending ordinals、原 saved refs；新 sidecar/变更收据与 run 同事务。新清单只重建 pending pair，重 ordinal/input/scope/manifest hash，保持原 as_of/evaluated_at/generation/方法版本。authorizer 独立重建，普通 live 的受理时间等式不变。原 run/checkpoint/成功应用保持原状；item_failed 不重复执行，若同 scope 有失败，pending 可局部完成但原 scope 继续 needs_manual、不确认、不收费。恢复子代再次中断可继续新代次，沿原清单证明链核对；最多 16 代，超过后在 resume 接收前明确拒绝并待人工。
- outcome 里的 source_manifest_hash 是本次实际 P2 清单证明；member 的同名字段保留原领域 origin hash，不能修改或以 None 充当结果证明。正式研究批次按每成员成功 outcome 的 run/hash 冻结，付费边界重新核对真实授权与 checkpoint。合法零规则 scope 可以通过声明范围与空成功检查点证明本地无规则工作，不代表没有风险。
- Bootstrap 初始历史只进入本地机械复核，不自动触发研究收费；后续遗漏变化按真实提交处理。证据/资料/事件/rule 审计及可变分类摘要有界扫完后每 300 秒重新审计，迟到旧时间/UUID 不会被永久跳过。读取安全限制不等这次审计。
- 成功来源 receipt 还须含 source_id/contract_version/contract_hash/scope_hash/source_refs_hash。prepare 以 effective_cutoff 限制 receipt committed/acquired/available 及每条精确资料的可见时间；当前 operational health 以 now 检查。无 cutoff 内匹配 proof 则 V1/unknown，后到资料不进入旧任务 V2 或正向摘要。历史 proof 按精确 scope/合同/refs hash 索引有界选取，不在应用内拉取全史。
- 相同精确 refs/content/合同的较新健康证明可延续旧 manifest 的健康，但不改其原 proof/hash/知识窗；窗口不能收窄，未来时间、partial/failed、内容或合同变化仍限制。健康到期边界使用 `at >= deadline`。来源重核不视为新经济事实，不因取得时间变化重复收费。
- 无授权/无价格/无预算形成未尝试的 blocked cycle；授权改变可重核同 cycle/batch。已尝试任务失败、取消、unknown 不自动创建新任务。运行中的新成员独立 dirty，成功后按完整成员集合接续；同一批次重放不把已消费成员再加入 dirty。
- scope GET 从已保存来源健康期限、最新 live application 边界及当前结论 effective_review/validity 只读计算 due，不等定时器写状态；尚无依据显示尚未确定。内部补偿/调度游标单列在 operations.maintenance_feeds，不计作声明来源覆盖。
