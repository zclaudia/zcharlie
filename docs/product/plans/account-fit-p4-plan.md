# P4 持续更新与受控启用：实施计划

日期：2026-10-17（Asia/Shanghai）。状态：**实施计划经审查通过**（仅文档）；**未开始 P4 业务实现，未新增迁移，未操作生产、启用开关、提交或推送。** 本计划由主代理设计并交叉核对，三个 GPT‑6 Luna 子代理分别审查来源可信性、调度恢复、通知与启用门禁；审查裁决见第 12 节。后续实施须另获授权，生产启用再单独授权。

## 1. 基线与本轮范围

优先级：实际代码及最终验收 > 早期任务书中的拟议落点和待确认项。基线是当前工作区的 P3 最终源码，不是只含 P2 的 Git HEAD。

| 核对项 | 2026-10-17 的事实与证据 |
| --- | --- |
| 提交 | `de77ec5 feat(research): implement P2 supplemental rules with recovery safeguards` 为当前 HEAD；后续 P3 文件和修改尚未提交，不存在可指称的“P3 发布提交” |
| P3 最终版本 | [执行记录](account-fit-p3/execution.md)、[整合复审](account-fit-p3/review.md)、[主代理最终验收](account-fit-p3/acceptance.md)与[契约冻结](account-fit-p3/p3-freeze.md)共同定义；十项任务工程验收通过，不重复实施 |
| 工程验收证据 | P3 最终记录：unit 2485 passed / 2 skipped（缺原始材料），6 network 默认排除；integration 789 passed；根目录 717 passed；专项与全量重叠不累加。浏览器 1440/390 三脚本通过；ready/limited 部分为同渲染器合成投影。以上为历史验收，本轮没有重新执行或冒称实测 |
| 迁移 | 本轮只执行无数据库连接的 `alembic heads`，源码迁移图为 **052 单 head**；052 为工作区文件。未执行 `upgrade/current`，未据此推断部署数据库已经迁移至 052 |
| 配置 | 本轮只读解析 bootstrap Settings：supplemental/account_fit/candidate_ranking、investor_agent/background、disclosure_originals、external notification/idempotent 均 false。配置指向的本地 `data/app.db` 不存在；没有数据库 runtime overlay 可核。`settings/loader.py` 表明导入后 DB 设置优先于 env；Compose override 又可覆盖数据库路径，因此**部署库版本、导入状态和实际运行设置未核实**，须后续启用前独立只读核验 |
| 并行改动 | 开始时已有 P3 源码/测试/文档、`mobile/src-tauri/Cargo.lock`、`.playwright-mcp/`、`.zcode/` 等修改或未跟踪项。保留原状，不整理、回退、提交或混入 P4。本轮仅新增本文并给主设计补链接 |
| 继承基线 | [P2 冻结](account-fit-p2/p2-freeze.md)、[P2 追加复审](account-fit-p2/followup-review-2026-10-16.md)、[P3 计划](account-fit-p3-plan.md)。P3 早期“待确认”已由其冻结/最终代码裁决，不转抄成 P4 待实现 |

工程通过不代表真实来源、公司暴露、研究质量、生产交易规则或生产启用通过。当前没有这些门禁已通过的记录；首期仍保留 `UnavailableTradingRules`，没有通用可信政策事件流，不承诺全网新闻覆盖。

## 2. 现有能力、真实缺口与新增边界

以下路径相对 `src/investment_agent/`，均为实际代码挂点；新增文件标“拟新增”。

| 已有能力 | 现有挂点 | P4 真正增量 |
| --- | --- | --- |
| 事件不可变修订、user_report 写权限、规则 CAS/生命周期、generation | `supplemental_rules/{contracts,commands,service}.py`、`storage/repositories/supplemental_rules.py`、迁移 051 | 捕获**已提交变化**的持久收据与恢复消费；不重做事件版本库、不自动批准规则 |
| 本地冻结来源、三值评估、2000 pair / 4MiB、检查点与防隐藏校验；规则生命周期/事件变更已经原子入队本地重评 | `supplemental_rules/{sources,event_sources,admission,jobs}.py` | 认领 P2 已排原 run，只为原 manifest 未涵盖的成员、新资料/due/补偿排新批次；无 case 也能复核，不隐式模型/采集 |
| 受理绑定、prepared/published coverage、内容 hash 绑定评审、闭环原子发布 | `binding.py`、`coverage.py`、`investor/publication.py`、assessment 仓储 | 只接调度触发和新的来源覆盖限制；不另写发布器或自动解除流程 |
| 新 critical 规则、新 event UUID/修订、时间边界同步限制旧结论 | `publication_limits.py::published_manifest_limits/coverage_limit_reasons`、`investor/publication.py` | 保留零写入 current-limits；补“已声明必需来源覆盖不足”的同步检查，不靠 timer 更新安全状态 |
| durable PipelineRun、unique(kind,idempotency_key)、lease/heartbeat/attempt fencing、retry/stale recovery | `storage/repositories/pipeline_run.py`、`research/{worker,retry_policy}.py`（lease 定义同 PipelineRun 仓储） | 注册 P4 有界 handler；定时器只唤醒持久工作，不新增租约平台 |
| case 日历/快照扫描、收据合并、running dirty、授权研究 | `investor/monitoring.py::scan/_merge/dispatch/settle/maintain`、`ReviewCycle/MonitorReceipt` | 接收规则/事件/证据与本地复核批次；明确 dirty 归属、失败保留、无 case 待研究、运行中关闭 |
| 正式发布 outbox、站内通知、授权 webhook、未知 ACK 停止 | `storage/models/publication.py`、`investor/{monitoring,notifications,notification_http}.py` | 增加 P4 状态、容量恢复与审计；运行告警先只进本地运行面，不增加外部告警渠道 |
| CNInfo 有界 discovery/fetch、原件 blob/hash/收据、issuer 校验 | `evidence/disclosures.py`、`storage/models/disclosure.py` | 首个联网候选限**现有适配器支持的 SZSE A 股公司披露**；复用协议，补定时授权/修订/覆盖验收。当前不支持由此推导 SSE、BSE 或监管政策源 |
| 结构化材料、原始响应/采集 receipt、reviewed import | `materials/{store,handlers,reviewed_import}.py` | 接已保存资料变化；材料在线刷新默认禁用，SEC facts/Eastmoney 财务材料不能充当政策正文；不一并开启 |
| collector 回执推进 current task 知识窗 | `agent/observation.py::effective_cutoff/resume_collectors` | **P4 不扩展其源白名单**；新原件在新授权任务受理时进入可见窗口，旧任务不追新 |
| operations、单股补充事项与 coverage UI | `agent/operations.py`、`api/routes/investor.py`、`web/static/supplemental_items.js` | 按源/scope 的覆盖、最后成功、失败、due、积压、进度及恢复动作；不能把“无采集结果”画成“无风险” |

严格继承：不可变修订/精确 owner-subject-revision 引用/CAS/冻结清单/评审绑定/发布与 closure 原子性；暂停、退役、到期、关闭或采集失败均不能关闭旧问题。财务基础评分、Quality、DSL、账户硬约束不变；账户事实只使 fit/整案失效，不污染公司研究本体；buy/add 与 reduce/exit 边界保持 P3。不做自动交易、条件式账户约束、预约规则激活、泛源新闻目录/搜索、通用 Issue 平台或反向依赖平台。

## 3. 顺序与能力开关

按依赖分三阶段：**A 本地变化与复核恢复 → B 单一有限披露源 → C 真实样本与启用准备**。通知与观测从 A 起随事务交付，不能拖到最后补救。

拟新增两个默认 false 的 runtime 开关：`supplemental_updates_enabled`（自动本地复核和 P4 研究调度）与 `supplemental_disclosure_polling_enabled`（有限披露源定时联网）。它们不是授权凭证，也不修改 P2 三开关默认值。手动 P2 管理/评估仍沿 P2；官方原件链继续要求 `disclosure_originals_enabled` 和精确来源权限。P4 自动本地评估取 updates∧supplemental；自动研究再取 agent∧background∧当前 Mandate 范围/预算；定时联网再取 polling∧originals∧源/scope 明确许可与限额。fit/ranking 只沿原有各自开关，不由 P4 自动重排或建立账户交易情景。

已有领域写入成功后，追加轻量变化收据不受这两个调度开关影响，避免关闭期漏记；调度关闭只阻止新评估/联网/研究的推进。启用新版本先建表后运行新 writer；旧版本仍在线写入的窗口用第 8 节补偿扫描弥补。不增加模型可写批准、核验、授权或启用工具。

## 4. 最小数据模型、身份与游标（拟议契约，A0 冻结）

### 4.1 三张窄表，沿用现有队列和原件库

1. **`supplemental_change_receipt`（拟新增，不可变）**：`sequence INTEGER PRIMARY KEY AUTOINCREMENT`、UUID id、owner、unique(owner,change_key)、schema_version、kind、精确 source_refs、source_hash/adapter_version、scope_hint、acquired_at、available_from、committed_at、previous_change_ref、payload_hash、payload。kind 仅 rule_revision/rule_lifecycle/research_event/evidence_revision/material_revision/source_page/source_failure/due_boundary/bootstrap/scope_member/source_authorization/source_attempt/evaluation_admission；另有 operation_id/request_hash、origin_run_ref（另有索引列origin_run_id）/source_manifest_hash、parent_sequence、listing_id/context（非成员行可空）。保存服务器生成的摘要及精确 refs；正文仍在既有证据/原件库。UPDATE/DELETE/重复身份插入拒绝；同 key 同摘要返回原收据，不同摘要冲突。仅用户命令根收据operation_id非空（成员/阶段审计用各自identity，关联root_request_id），以partial unique(owner,operation_id)核完整 command/path/body hash，跨命令复用 UUID 为409；采集 logical page 与 attempt operation 分开，不混淆请求幂等与业务去重。`previous_change_ref` 是本表内部 FK/身份，不新增通用 ObjectRef kind。
2. **`supplemental_source_state`（拟新增，可变 CAS）**：唯一(owner,feed_id,scope_key)，feed_id 固定本地补偿 feed 或 `cninfo_szse_v1`；revision、adapter/config hash、声明范围及其 hash、首次覆盖起点、最后尝试/成功、已提交 page cursor、complete_watermark、coverage_state、failure_code/count、retry_at、active_run_id、bootstrap/sweep 位置及健康阈值。原请求/成功页/失败尝试留不可变 receipt；本表仅加速投影，不能作为事实证据。配置变更用 receipt 冻结旧/新配置 hash，CAS 更新；旧 cursor 不跨版本复用。后台来源许可的当前状态/有效期/每轮与每日用量计数在本表CAS维护，许可授予/撤销、请求reserved/sent/result审计均留receipt。
3. **`supplemental_refresh_state`（拟新增，可变 CAS）**：唯一(owner,listing_id,context)，revision、pending_change_refs 的有界摘要/收据区间、冻结消费上界、active_batch/run、evaluation_ref、next_due_at/boundary_key、last_success、blocked_reason、dirty_high_watermark、待处理/人工处理计数。case 引用可空；不得把账户放入公司 scope。完整成员存在 receipt/批次冻结清单，投影有界、可重建；不能无限往 ReviewCycle.payload 塞 event/source_refs。

现有机制不足的具体原因：MonitorReceipt/ReviewCycle 强制 case FK，无法代表没有 case 的公司规则评估、源分页和覆盖；P2/P3 的 generation 只表示“有变化”，没有每条变化与持久消费确认；PipelineRun 已有 lease/尝试/请求清单，但缺源覆盖水位与跨批次 scope 合并槽位。故新增三张窄表；**不再新增工作队列、租约表、预算账本、原件表、通知 outbox、Issue 表或通用事件总线**。原件复用 DisclosureBlob/Original，资料复用 ResearchDataset/DocumentProvenance/EvidenceRecord；不同来源原件不硬塞 CNInfo 的固定身份结构。

后续只新增一条独立迁移（若仍以 052 为 head 则候选 053；实际编号实施时核查），不改 050/051/052。新表 owner/subject/ref/hash/CAS/唯一约束、必要索引与不可变触发器纳入 ORM/DDL 对照；存在 P4 receipt/state/关联任务时拒绝有损 downgrade。不在本轮建立文件。

### 4.2 三种身份不能互相替代

- **业务事件**：既有 ResearchEvent UUID/revision。来源文档键为 `(source,issuer/native_subject,native_document_id)`，不能用标题/hash 代替；同文档同内容重抓不增加事件 revision。修订正文、核验状态、撤回或冲突才追加 revision+previous_ref。公告更正若有新公告号，先保存另一文档，再以精确关系关联原事件；关系未核实不能凭相似标题合并。沿 P2 允许审计过的 event_key 变化，UUID 不变，不新增“业务键永久不可改”限制。
- **变化/采集尝试**：change_key 含 owner、输入身份+精确 revision/hash 或生命周期审计序号；due key 含目标版本+边界种类+绝对时刻；source page key 含固定 poll operation+窗口+page token+adapter/config hash。语义等价重抓可以有新的采集尝试，但不能重复事件/评估/付费/通知。
- **工作批次**：PipelineRun key 为 `p4:<kind>:H(owner,scope,policy_version,sorted change identities,batch generation)`，满足既有 128 字符上限；请求 hash 同时覆盖模式、冻结范围、成员、源健康与时间。研究 task 沿 `review:{cycle.id}`；付费任务和本地 run 是不同身份，重试不换 task/budget。

### 4.3 消费、扫描与分页游标

P2 的 `lifecycle.py/events.py::_enqueue_reassessment → admission.enqueue_manifest` 已把领域变更、mutation receipt 和本地 run 同事务保存。P4 hook 同事务记录 **origin_run_ref/source_manifest_hash**；原请求重放只能返回原收据。消费器核对原 run 冻结的 listing/context/rule/revision/输入 hash 与真实结果：已完整交给原 run 的成员记录交接，不能再排相同 P4 评估；原 run 在运行则等待原身份，失败/partial 按 P2 终态语义明确待人工或为未完成成员建立有审计的新恢复代次，不复活终态、不重复成功 pair。只有原 manifest 未包含的范围、明确未完成成员或新的 due/资料输入可补排，不能因原 run 存在/成功就宣称全范围完成。

P4新本地批次优先经现有 `SupplementalRuleAdmission.accept_evaluation` 的严格 EvaluationRequest/InvestorMutation 链受理，确定性UUID来自scope+冻结成员/hash，不自造未被 `jobs.py::_command_model/authorize_supplemental_run` 支持的命令。P4的evaluation_admission关联收据与该run同事务保存（origin_run_id索引）；worker仅对有该服务端P4关联的run增加updates开关复核，P2原生run保持原行为。B的candidate事件新写链使用单独的、仅服务端可生成的typed ingest命令与精确source授权/页回执证明，显式扩充受限authorizer映射与result身份判定；不冒用create_user_report的事件身份/核验语义，不接受任意新command，更不把journal本身当原P2授权收据。新内部事件命令与原件/事件revision必须通过相同owner/主体/哈希/operation校验。

内部事件消费按持久 `sequence`，不用 published_at、UUID 大小或全局 generation 当消息游标。每批在短事务冻结 high watermark 与成员；顺序只保证提交顺序，不推断业务发生顺序。接收收据与 fanout PipelineRun 同事务；队列容量不足时 receipt 保留且 `source_state` 的消费确认不得越过未排工作，启动恢复可重新确定性入队。

fanout 请求冻结实际 subject 集合及 hash，每页有持久 ordinal/最后处理成员，结果更新与 checkpoint 同事务、同 lease；全范围无法证明时 explicit unknown/partial，并保留继续位置。`affected_listings()` 当前是有界提示，不可直接视为全覆盖名单。类别规则遍历声明的本地上市主体集合并用 P2 matcher 判断；无暴露名单的广泛政策不能自动认为全市场适用。

精确成员采用同一 journal 的 `scope_member` 行，不加第四张映射表：fanout 对每个 `(parent_change_sequence,listing_id,context)` 产生唯一成员，`id=UUIDv5(parent_receipt_id, listing_id/context)`，有 unique(parent_sequence,listing_id,context) 与 `(owner,listing_id,context,sequence)` 索引。成员行不递归生成 fanout；它引用冻结目标 manifest/hash 与 origin run。每个本地批次在 PipelineRun.request 中冻结有界成员 ID/序号列表和 pair/input hash，checkpoint 保存各成员的 result/交接/待人工证明。refresh_state 的确认水位只越过该 scope 已有持久交接证明的连续成员，不用一个全局 high watermark 代替成员集合；dirty 按该索引读取未确认成员。ReviewCycle 冻结本轮消费成员的 scope+序号范围/清单 hash/批次 refs，运行中新成员留独立未消费范围；成功后逐项确认，失败保留原集合。超出有界批次/评审窗口就 blocked/继续分页，不能截掉成员或把多次变化只留最后一条。真实 SQLite/EXPLAIN 验证不靠无索引 JSON 全扫还原成员。

HTTP cursor 绑定 owner/source/scope/config hash/query hash、snapshot high watermark、after ordinal，严格形状/长度和有界读取；与已冻结批次不符 409，非法 cursor 422，错 owner/不存在 404。不得从一个新集合继续旧 cursor。初始扫描/可变本地来源不是可靠单调 feed：需固定 sweep 成员和分段 hash、before/after generation 对账，写钩子补 sweep 期间的新变化；无法确认一致就继续补偿并显示覆盖不完整，不靠 max UUID 或 max captured_at 跳过迟到写入。

## 5. 来源、事实及时间语义

### 5.1 A/B 的最小来源合同

A 仅接本地已保存事件、证据及资料，受控人工导入入口接受**精确的已归档原件/证据 refs + manifest hash**，以及已有 user_report 事件入口。人工材料仅限现有能精确解析/回读的 DisclosureOriginal、EvidenceRecord（含其合法DocumentProvenance）、允许的ResearchDataset类型；逐类型adapter核owner/subject/revision/hash/available与当前更正状态。manifest_hash由服务端从这些精确对象重算。人工材料需先经现有材料/原件导入链归档，保留真实 bytes/hash/来源/取得时间，再提交接入；本阶段不建任意 URL 抓取或通用文件平台。未归档、主体不明、原件损坏或引用不可见时拒绝接收/显式待补，不因按下“导入”变 verified。

B 首个在线候选为现有 `DisclosureService` 的 **CNInfo、SZSE A 股、显式 listing 集合、明确起止窗口**。该代码已有在线 discovery/fetch 和原件留存，但不是持续监控能力，也不是政策原件/研究影响的可信性验收。P4 补周期收据、页恢复、源覆盖和修订关联；不扩大其市场或 host 白名单，不复用 annual reviewed import 去核验任意政策。若该源真实验收不通过，B 保持禁用，A 可独立验工程行为。监管政策线索可沿user_report保存；原件仅在现有受支持类型能真实表达主体/来源并精确回读时接入，否则 `unsupported_source_kind/blocked_policy_evidence`。普通文档、年报/SEC facts不得任意转换政策事实，不硬塞CNInfo；非公司监管原件合同/专用主体适配或监管联网源需另补范围与任务，不预建。

**后台来源授权是P4必要新接缝，不等同现有task权限。** 当前 `agent/disclosure_tools.py::check_permission` 的 DisclosurePermission 绑定task/listing/source/cutoff及有限discover/fetch/parse次数，不能被定时器循环复用。B新增类型化 `BackgroundDisclosureAuthorization`：用户经专门认证设置动作明确授予 source+listing/文档类别、起止窗口/频率、有效期、每轮/每日 discovery/fetch/byte上限、最大失联时间；授予/撤销记录actor=api/web及审计receipt，env/import/模型、普通登录、Mandate研究授权均不授予。配置/范围扩大需新授权revision，当前许可hash进入source_state和poll冻结请求。kind=source_authorization/source_attempt分别承载许可revision与请求阶段审计；一天按Asia/Shanghai计，用量不能随poll或retry代次重置。后台HTTP适配器在每次发送前原子预留用量并校验该许可/两开关/预算，在响应持久化前再次核对revision；unknown外部请求计入已用配额，不当未发送返还。每source/owner每日总配额跨scope/授权revision汇总：同表source_state保留专用quota scope行，短writer事务预留并留attempt receipt，不能靠拆scope或重授许可清零当天用量。复用现有HTTPS/issuer/MIME防线与同等上限语义，不假冒研究task或绕过task工具的check_permission；不新增付费数据供应商。

源合同必须冻结 source_id/version、原件/索引入口白名单、issuer/native document 身份规则、listing 映射、允许类别和窗口、发布时间精度/时区、更新/修订机制、分页与重叠回看策略、请求/字节/频率配额、使用条件、最大失联时间和未覆盖范围。官方网络规则的实际内容留待 B/C 从原站验证、记录，本文不声称已经完成核验。

### 5.2 四条可信性轴

| 轴 | 必需依据 | 不能推导的内容 |
| --- | --- | --- |
| 原件来源真实 | exact 原件、SHA256、来源/最终 URL、issuer/native id、取得回执、MIME/签名/主体核验 | “官方抓到”不等于正文事实已核实 |
| 正文事实核实 | 受限事件类型专用校验器或受控人工核验记录，精确原件页/段、内容 hash、核验者与范围；服务端验证 | user_report/candidate 不因用户确认、标题或模型输出升 verified |
| 公司暴露 | 精确 company/listing、产品/地区/订单/实体名单与版本、完整性声明、反证与未知 | 公司在某行业不等于政策/事件实际影响成立 |
| 影响判断/解除 | 既有正式研究、P3 coverage/proposal/review hash 绑定与原子闭合 | 原件可信、政策到期/撤回、机械 clear 均不自动解除旧问题 |

A的imports只建立变化/来源绑定，不创建或提升verified ResearchEvent；本期不凭客户端提交的reviewed/reviewer字段提供通用人工事实升级。若B核验任务未交付有效的专用服务端审查工件，verified门禁明确blocked。该工件至少绑定核验者权限、时间、原件ref/hash、页段、事件类型、issuer和核验范围，导入调用者不能自报。

B 默认仍产 candidate 的未解析原件及待核事件，复用 `EvidenceRecord` 和 P2 的语义角色。若要产 verified ResearchEvent，须为**明确一种事件事实**补专用 trusted adapter、核验记录和负例；不开放通用 verified POST、不放宽 EvidenceService 单公司检查。没有已验收核验器的受控人工导入保留 user_report/candidate；已保存且真实验收过的结构化事实可按原引用复用，不能顺带核验新主张。事件事实、公司暴露、影响推断分开保存。

### 5.3 B 的页协议与事件桥接

现有 `DiscoveryRequest` 无 page 字段，`discover()` 固定pageNum=1并以hasMore标truncated；`DisclosureService.execute` 的operation收据会缓存成功和error。B必须显式扩展受限 page/窗口协议（保留旧请求与收据形状），不可假设现有cursor已经支持续页。logical page key稳定，**每次实际外部attempt有独立operation UUID**；已提交成功页直接复用原receipt，不再请求；已缓存error不得以同operation重试，应由持久attempt序号派生新UUID，受原poll次数/字节限额约束。页响应保存原bytes或耐久归档ref、SHA256、status/content-type/final URL、取得时间、issuer目录hash、count/hasMore和前页链hash。相同logical page再次响应内容不同保留漂移收据，标partial并重新对账窗口，不能覆盖原成功页或直接当成功。`complete_watermark` 分开记录索引发现完成与所需原件下载完成；下载不全更不能声称正文已核实。

原件变化本身不会自动成为P3事件限制。B需新增**候选事件桥接器**：以 `(cninfo.v1,issuer_id,disclosure_id)` 派生稳定ResearchEvent UUID，保持来源文档映射；同native文档新hash追加candidate事件revision/previous_ref/精确原件refs，新公告号默认另一事件，核实后才追加更正关系。不能用当前DisclosureItem.identity_hash替代自然键（它还含URL/发布时间等可变元数据）。原件/采集成功receipt/候选事件revision+events_generation/P2原run/P4变化收据/checkpoint在同页接收事务提交，保证HTTP页完成后即使P4 worker未跑，P3也能同步发现新事件。只为许可listing/issuer范围建立事件，不从标题推断产品暴露或影响；映射不明隔离并source_coverage_unknown。单页超P2容量就按文档原子子批持久checkpoint，未全部交接前页不确认完成。

### 5.4 cutoff、迟到、更正与失败

- `published_at/effective_from/to` 保存原精度，未知留空；`acquired_at/available_from/committed_at` 来自可信接收链。人工填写的旧发布时间不允许倒填知识可见性。日期级公告沿现有严格日期规则，不伪造午夜已知。
- P4 不修改 `observation.py` 的 collector 白名单。全局源成功水位、当前时间、定时 slot、排队/失败回执均不能推进任务 effective_cutoff。晚到资料限制旧结论并排下次授权研究；运行中任务仍使用原窗口，不能偷换 binding/prepared 清单。需要新研究时创建新周期要先通过授权与预算门；失败旧任务不能据此自动重付费。
- 本地 commit 重复/乱序/回填一律按精确版本保存；接收到旧版本不覆盖较新头部。来源修订写新原件/关系/事件 revision；P3 能发现新的 event UUID/修订，旧 event_ref 不遮住新问题。冲突不按“最新抓到的就对”静默解决。
- 采集每页落已验证原件/receipt/页 checkpoint 同事务；业务 DB 外文件沿既有 atomic rename+hash 机制先耐久化，DB 失败可遗留不可见孤儿，不能出现已提交 ref 指向不耐久文件。孤儿清理不得删已引用原件。
- `page_cursor` 只确认已持久化页；只有整个声明窗口完成并校验页链、计数/响应结构后才推进 `complete_watermark`。失败/partial/truncated 不推进完整水位，已存页可恢复；成功空结果也仅表示该源该窗口该范围查无新增。远端无稳定分页快照时用重叠窗口+文档键去重，并把覆盖标成 bounded，不能宣称站点无遗漏。
- transient 网络错误按现有 retry policy 限次退避；权限/身份/结构漂移/损坏为 blocked/quarantined 并显示原因。429 尊重可验证的 retry-after 及本地限额。来源恢复不能自动清旧风险；重复抓取失败不制造无限事件/告警。

## 6. 事务、调度与恢复

### 6.1 端到端事务边界

| 步骤 | 同一短事务内原子保存 | 事务外及恢复约束 |
| --- | --- | --- |
| T1 接收本地变化/采集页 | 领域事件/证据及既有收据 + change receipt + 确定性 fanout run（或未入队标记）；页 checkpoint/source state CAS | 网络不持锁；commit 前失败重试原 operation，commit 后丢响应返回原收据；不以返回 202 代替落库 |
| T2 影响识别 | lease 校验、冻结目标/输入 hash、scope_member与逐页 refresh_state pending 合并、fanout checkpoint | 主体不确定保留 unknown 与待人工；旧 worker 失租不能确认游标；每条 receipt 要么有处理证明要么仍待处理 |
| T3 本地评估调度 | scope CAS，冻结待消费成员上界、P2 admission/manifest、唯一评估 run、active_batch | 调用现有 P2 freeze/evaluate，进程外重评只读 frozen manifest；后到变化写 dirty，不改 in-flight 清单 |
| T4 评估完成与正式研究需求 | 校验真实 saved applications 与 checkpoint；完成批次/状态；有 case 时幂等 merge MonitorReceipt+ReviewCycle，无 case 存 `research_required_no_case` | 已消费=已交接到持久下一步，并非研究问题已关闭；无 case 不自动建 case/Mandate/付费任务。不可仅更新“成功游标”后再交接 |
| T5 研究 admission | 当前授权/范围/价格/队列检查，沿 admit，task/grant/binding/run 与 cycle.task_id/consumed_events 一起提交 | 沿 `review:{cycle.id}`；创建 task 后永不后台再 admission。同一事件/批次重跑不换 task |
| T6 模型与采集工具 | 沿 BudgetService 预留、sent、结算，ToolAction settle/fencing | 网络外部效应不可回滚；unknown 需对账，不能假定失败/未付费 |
| T7 prepare/review/publish | 完整复用 P3：check+当前限制重核+closure+published manifest+依赖+case CAS+PublicationOutbox | 并发新变化即使 T4 尚未跑也能拒绝发布；失败落 draft，旧 open issues 不变 |
| T8 通知与确认 | consume PublicationOutbox+站内投影+冻结 delivery，或精确源/运行告警的本地投影 | HTTP 发送前持久 sending；固定 delivery ID/bytes、授权再查；ACK unknown 沿现有策略停止或幂等限次重试 |

P4每次写source_state、成员/消费确认、refresh_state或checkpoint之前，必须在**同一writer事务**调用现有PipelineRun仓储fence，条件含 run_id/worker_id/attempt_count/status=running，再核相应state.revision；不只在生命周期finish时fence。失租失败须回滚全部新表和应用结果，旧attempt不得先提交业务再被拒绝更新run。

SQLite 使用本模块短 writer reservation/BEGIN IMMEDIATE 和已有 lease，不改全局事务策略。多查询 GET 复用 P2 `ensure_read_snapshot`，读路径不得写 receipt、入队或升级核验。

### 6.2 到期识别与同步防线

扫描对象包括规则 review_due/valid_until、research requirement check_by、event 发布/生效/失效边界、application/evidence/material 的已声明有效期、coverage/推荐 next_review/valid_until。无明确有效期的资料不能由 P4 自造“有效”结论。due identity 绑定具体 revision+边界；同边界重复 timer 只产生一个 receipt；新 revision 或新结果才生成新的后继 due。`next_due_at` 是最早未处理边界，逾期不靠滚动“now+interval”吞掉。

APScheduler 只唤醒单实例的 `supplemental_maintenance` PipelineRun；worker 启动与维护循环也补偿发现 pending receipt、expired lease、逾期 due 和 source checkpoint。维护 run 由持久工作代次去重；已有 run 时只标 dirty，不为每个漏掉的 cron slot 建任务。处理旧 backlog 与新变化采用每批固定 high watermark、稳定 scope 顺序，轮转给新 critical 变化保留份额，禁止无限历史扫描饿死新事件。critical 提高处理优先级，不授予费用或风险闭合权限。

P3 同步规则/事件/时间限制原样保留。P4 为声明使用持续源的 scope，新增共享 source coverage 限制谓词：按当前覆盖合同、最后完整成功水位、健康时限、partial/conflict/quarantine 状态判断，输出 `source_coverage_unknown/stale/incomplete`（具体 code A0 冻结）。读路径按 at 计算，即便扫描没跑也识别 stale；publish 与 maintain/续期在各自事务重查。资料不足表现为**无法确认覆盖**，不能说风险已发生或已解除。

新的 coverage prepared/published 使用 **schema_version=2** 的 `update_source_binding`（固定合同 version/hash、声明 source/scope、成功回执及原件 refs、覆盖窗口和完整性）；服务端填入并参与新 manifest/评审 hash。保留P3 schema_version=1 DTO及其完整model_dump/hash域不变，新增V2契约及按原payload schema显式分派的parse/hash入口，不能给V1加默认None再按完整dump验旧hash。V1读回显示not_assessed，不回填/重哈希；V2缺绑定或字段非法即拒绝。prepared/published同版本绑定、proposal/review引用与manifest hash一起校验，不混用V1/V2。启用 P4 source 策略的主体若仍无绑定，当前投影显示覆盖 unknown，方向性发布要求新研究；旧未进入该能力的主体不被全局源宕机误伤。来源健康的当前限制经 storage 仓储接缝进入 publication，**investor 层不 import supplemental_rules**。外部来源失败并不放宽 P3 的任何限制。

### 6.3 授权、预算与重复付费

本地匹配/复核是无模型操作；正式研究必须拥有当前 case/Mandate 的背景授权、scope、有效期、task/daily/review 限额及配置价格。复用 `MonitoringService.dispatch → live_policy/cost_limits/admit` 与 `BudgetService`。当前背景 review 范围沿 `authorization.exploration_scope_refs` 的既有语义核实，不能假定字段名不同就另造许可。P4 不扩展范围/放大预算/把研究意向当授权；没有授权明确 `blocked_authorization/scope/budget/price` 并可读。

三个检查点：①调度时低成本预检；②task admission 与 cycle 关联原子提交前再次核；③每次模型或付费工具的 reserve/sent 前沿 check_boundary 核最新设置、授权、pause、剩余额度及价格版本。队列等待不预留无限预算；跨上海日界只影响当日账本，不重置任务 lifetime。P3 记录的评审请求增大和测试 token 提额不是生产预算扩大依据；C 阶段须按真实形状测 token/费用与 finish_only 储备。

P4 发起的 grant 增可空、服务端填充的 update batch 溯源字段，用于 check_boundary 同步检查 updates/supplemental 开关与来源授权；旧 grant 缺省走原路径，客户端/模型不得伪造。已有通用任务不因 P4 开关被误停。字段缺省不重序列化改写旧收据/hash；admit已有grant重试比较应按保存版本/字段存在性核对，不能把旧JSON和补默认的新dump直接判不等。

**不承诺外部模型 exactly-once。**DB 内确保同批次单 admission、单正式发布和单通知身份；请求已发送但结果/费用未知时，沿 `BudgetEntry.unknown` 停止自动重发/新建任务，保留已有未知费用约束，使用既有 `agent/operations.py::reconcile_budget` 经真实供应商凭证人工对账。已知结算的 transient 失败可在原 task/预算内重试；失败、取消、预算终止、unknown 的 cycle 保留未决 dirty，自动置待人工，不自动换 cycle 再花钱。只有成功且尚有未覆盖 dirty、当前授权仍有效时才交接下一周期；以精确成员持久确认，禁止仅取 dirty[-1] 丢掉其余事件。

### 6.4 关闭、暂停、运行中关闭、恢复

| 场景 | 固定行为 |
| --- | --- |
| updates 关闭 | 新本地评估和 P4 付费 admission 停止；已提交变化继续留 receipt，源覆盖读面与 P3 限制仍计算；队列显式 paused，不删除收据/未决问题 |
| polling/originals 关闭或源授权撤销 | 不发新网络请求；响应持久化前重核授权/配置版本；已撤权时只保存安全尝试审计，正文隔离或丢弃，不写可见证据/事件、不推进页链/完整水位或fanout；已发送用量仍保留。单纯临时暂停不清旧覆盖/风险，健康如实显示 |
| 本地 worker 运行中关闭 | 下一 pair/checkpoint 前重查；已完成应用不撤销，剩余成员保留。复用 P2 停止语义，恢复用新调度代次承接未完成部分，不盲重试终态 partial/failed run |
| P4 研究运行中关闭/pause | 下一可信边界停止后续模型/写发布；已发送调用可结算或 unknown，不称“零费用”。publish 同事务再次核开关/授权；已提交正式产物不撤回，限制保持 |
| 恢复与来源升级 | 当前配置/授权/scope/预算全部重新核验；未受理工作可合并，已受理只能接回原 task，不复活终态 task。源 adapter/config 变化使旧 cursor 失配，建立显式回看/补偿批次，不重写历史 |
| dirty 与失败积压 | 成功才确认已覆盖成员；失败成员/未交接成员保留待处理原因。人工可在现有授权界面发新请求，需新请求身份并标明承接哪些旧成员；恢复开关本身不是追加付费授权 |
| 无历史覆盖/积压超额 | 标明 bootstrap_pending、未覆盖起点/范围/数量下界；按限额分批，不自动全历史抓取或逐条建付费任务；没完成时不能显示“已同步” |

## 7. 通知、告警与观测接口

### 7.1 通知策略

正式通知沿 **PublicationOutbox → MonitoringService.consume_outbox → InvestorNotification → NotificationDelivery**。仅对已提交正式观点/论据的重要变化、当前有效性转为待复核、研究需要用户补充，以及正式研究成功改变/确认结论产生通知；机械 run 成功、重复页、单纯全局 generation 变化不通知。待复核通知明确为“需复核/覆盖不足”，不伪装成已经评审的新结论。

业务去重沿 case/cycle 或正式发布引用+阶段/实质内容摘要；站内更新修订仅在实质内容变化时递增。dirty 合并不逐事件刷屏；同 case 的 outbox 与 settle 路径须落同一业务 key。外发沿现有 notification key/revision/config fingerprint 的 delivery ID，发送前重查 mute、pause、archive、渠道授权/配置；通知设置不能授予研究授权。配置变化或静音抑制后的历史通知不在解除静音时批量重放。

`capacity_blocked` 在容量释放后仅在原通知 revision/配置/权限仍一致时恢复 pending，固定 body/id；同时重查站内revision、case_revision、mute/pause/archive并在全局短writer锁下按剩余256容量逐个恢复；否则 suppressed/superseded 并给原因。已知 failed 沿现有语义为终态不自动重试；unknown ACK：接收方已明确幂等才按原 ID 限次重试，否则 unknown 待对账。非幂等或已耗尽重试的unknown为终态等待外部人工对账，本期没有重生/重发API；不能用重建通知或换 ID 绕过不确定状态。正式发布事务只写 outbox，不直接发 HTTP。

运行告警（抓取失败、结构漂移、积压、租约接管、预算阻断、unknown ACK）第一版仅展示在本地 operations，按 `(capability,source/scope,error_class,incident_generation)` 合并、恢复留痕，不进入正式观点 key/outbox，不新增外部运行告警。以后若要外发必须单独授权目的地、类别与频率；不能借用投资 case 的渠道权限。

### 7.2 拟议 HTTP 与页面（继承现有认证/owner 隔离）

| 接口/面 | 契约 |
| --- | --- |
| `POST /supplemental-updates/imports` | 受控人工接入已归档 exact refs+hash，operation UUID、expected scope/config revision；202 返回持久 receipt/run 或 blocked 状态；不接受 actor/verified/费用授权；无原件时 422 |
| `POST /supplemental-updates/sources/{source_id}/scopes` | 仅api/web显式来源授权/撤销，operation UUID、expected revision、typed source/listings/类别/窗口/频率/有效期/配额；source_state CAS+不可变授权receipt。模型无此工具；不授研究/外发权限 |
| `POST /supplemental-updates/polls` | 仅固定 source_id、显式 scope/窗口、operation UUID/配置 revision；认证不等于源权限，未获许可/关闭 403；只持久入队，不在请求中抓取 |
| `GET /supplemental-updates/sources`、`/scopes/{listing_id}` | 当前能力开关、覆盖合同/窗口、source state、due、待处理/人工状态、批次与准确进度；关闭态仍可读 |
| `GET /supplemental-updates/receipts/{id}`、`/batches/{run_id}` | exact owner 读取及分页成员，检查输入/hash/checkpoint/真实产物一致；运行失败给 safe code，不返回凭据或外部异常全文 |
| `POST /supplemental-updates/batches/{id}/resume` | 仅安全的未完成本地工作恢复：operation UUID、CAS、reason；只承接剩余成员、保留原审计；不能恢复 unknown 付费/外发，不重放成功项 |
| `GET /investor/operations` 扩展 | 现有是认证API，没有operations网页；首期运行告警以此API为本地读面，单股页另展示本scope状态。添加P4有界统计与明细链接，保持旧字段兼容 |
| 单股页/现有运行面 | 来源与覆盖、上次尝试/成功、到期、blocked 中文原因、处理/待处理数及恢复状态；1440/390 无溢出，无跨主体残留；显著区分“未检查/覆盖不完整/查无新增/有待核问题” |

403/404/409/422 沿 P2 错误语义；队列/采集容量达到上限用 422 带界限与可恢复位置，已有 durable receipt 的调度容量阻塞显示 `capacity_blocked`，不伪 accepted/完成；操作重放先核原请求再返回既有收据。所有 GET 零 DML、同一快照、不开模型。

每个 source 至少显示声明范围 hash、历史覆盖起点、完整窗口/水位、最后尝试/成功、当前 stale 计算、失败分类/次数/重试时间、partial/unknown 原因、源/adapter 版本。每个 scope 至少显示 requested/processed generation 与 receipt 上界、next_due/overdue、queued/running/retry/blocked/failed、active run/task、完成/失败/待处理数、最老积压时长、最后 checkpoint/heartbeat、接管次数和恢复去向。计数未知给 null+下界，不伪 0；状态 success 也要并列显示 source coverage/研究可用性。

## 8. 容量、旧数据与升级兼容

A0 先冻结保守资源上限（实施实测只可降低或另行审查）：单维护页 50 scope（硬上限100），单批 2000 pair/4MiB 沿 P2，源每 poll ≤10页/200条新文档，单原件沿现有16MiB上限，单poll累计64MiB（包含索引与目录），poll遇上限立即partial并留可续位置；每日请求/字节cap必须显式授权且不由每轮重置。active source poll 每 scope 一个，P4 本地维护并发 1，不提升全局 native concurrency=4 或背景上限；每轮给新变更及 backlog 各留份额。控制列表、错误文本、cursor、dirty 摘要、重试次数均设硬上限，不因达到阈值丢原收据。

准入超限在 durable 接收前明确拒绝；已经接收的变化不得丢失，暂停后续采集/入队并保留可续页、水位及 storage/capacity_blocked。receipt/原件占用达到授权存储配额或可用磁盘低于配置硬阈值时停止新接收/联网，保持capacity_blocked；恢复需有审计的运维空间处理并重新检测阈值，不能删除已引用原件/收据腾空间。磁盘/DB 写失败不能 ACK 成功；上游数据不受本系统控制，因此“不漏事件”仅指**已接收持久事件与声明来源合同内的可恢复页**；未覆盖远端历史/下线数据必须可见，不能宣称互联网无遗漏。

升级：先在临时副本验迁移→备份/恢复演练方案→后续独立批准部署，调度默认关闭。旧业务表/hash/JSON/收据/manifest/task 不回填；coverage按第6.2节V1/V2显式分派；其他新增DTO/grant字段缺省兼容，原存储摘要按原格式核验，不能用带新默认字段的dump重建旧请求hash。初始 bootstrap 使用冻结的实际旧行集合/内容 hash，区分“旧行本次发现”与“今天新增事实”，只产生本地对账候选，不批量重发旧正式通知、不自动重研究全部历史。

升级期间旧 writer 没写 journal 的窗口由 bootstrap 与周期完整补偿 sweep 覆盖；P2 可变分类/既有候选事件行按实际内容 hash 变化发现，不能只看新行 ID。新 writer 对正式事件/证据/原件/规则生命周期采用同事务 hook；金融快照既有 monitor 扫描保留，不双重安排付费 review。每个 feed 有检测范围/能力声明，未接入的旧写路径显示 uncovered，不能给总体 full。

回退优先暂停 P4 两开关/源与相应 background 授权，保留历史及同步限制；不能 rollback 业务数据或删未决 receipt。已有 P4 数据时 DB downgrade 拒绝：守卫既查三新表，也查旧investment_assessment内schema=2/update_source_binding、既有PipelineRun/AgentTask/ExecutionGrant中的P4溯源/请求；即使三新表为空也不能漏检。旧二进制若不能识别新增 source binding/限制，不得直接回滚运行；需兼容版本或停写/只读维护模式并验证限制继续有效。备份须含 DB、原件/材料目录与必要设置密钥的独立保管，不能只恢复 DB 导致原件 dangling。

## 9. 任务、依赖、涉及文件与逐项验收

下列为实施任务，不是本轮已执行事项。共享表/DTO/事务由主代理冻结后再委派；子代理不得各自建立第二套 outbox/租约/cutoff 规则。

| 任务 | 依赖与拟涉及文件 | 逐项验收/退出标准 |
| --- | --- | --- |
| AF-P4-00 / A0 契约冻结 | 本计划；拟 `account-fit-p4/p4-freeze.md`、独立 expected fixtures | 三张表与必要索引/触发器、source contract、权限矩阵、cap、hash域、状态迁移、终态恢复、source binding 与同步限制签字；现有P3接缝再核，无悬空安全决策 |
| AF-P4-01 / A1 持久接入 | 00；拟 `supplemental_rules/update_contracts.py`、`storage/models/supplemental_updates.py`、repository、独立迁移、models注册 | fresh/052升级、已有P1–P3数据字节不变；唯一性/不可变/CAS/索引EXPLAIN；同事务hook、重复请求/坏hash/跨owner拒绝；含旧表P4载荷的downgrade数据守卫 |
| AF-P4-02 / A2 fanout与本地复核 | 01；拟 `supplemental_rules/{updates,update_jobs}.py`；扩展 `service/admission/jobs`，领域写入的 repository hook、`materials/store.py`、evidence保存接缝 | receipt→目标分页→冻结P2 run→结果→refresh_state真实SQLite闭环；无case、类别unknown、乱序/修订/回填、并发新旧批次、cap/dirty；P2原run全覆盖不重复排，capped/partial只补未完成成员；无模型/联网调用；账户变化不污染公司 |
| AF-P4-03 / A3 due/重启补偿 | 02；`scheduler/runner.py`、`research/worker.py`、update handler、既有PipelineRun仓储（仅必要接缝） | timer停摆后due同步受限；启动恢复所有未确认收据、租约过期/旧worker fencing；重复timer/两个worker不重复工作；历史sweep对账、队列满/磁盘失败不ACK |
| AF-P4-04 / A4 研究授权交接 | 02/03；`investor/monitoring.py`、`agent/execution.py`，必要的grant/check_boundary接缝、现有budget测试 | 无授权/价格/预算/范围零provider调用；task与cycle原子关联；dirty逐成员交接；已尝试失败/取消/unknown不再自动付费；关闭/恢复与跨日不换预算，旧grant兼容 |
| AF-P4-05 / A5 通知与运行读面 | 02–04；`investor/{monitoring,notifications}.py`、`agent/operations.py`、`api/routes/{supplemental_rules,investor}.py`、views；web模板/JS/settings | 正式通知单key，容量恢复固定body/id；未知ACK不盲重发；本地运行告警隔离；真实HTTP GET零DML/权限/cursor；1440/390覆盖缺口/恢复/无残留 |
| AF-P4-06 / A6 同步来源门槛 | 01/05；`supplemental_rules/{coverage,publication_limits}.py`、assessment仓储、`investor/publication.py`经仓储接缝、settings registry/config/UI | V1/V2显式解析，source binding服务端冻结进入评审hash；用归档V1 prepared/published原payload/hash逐项读回；计时器不运行仍stale；新规则/event限制继续生效；关闭/续期/发布竞争失败不闭合；reduce/exit不增阻断；import graph不放宽 |
| AF-P4-07 / B1 单源周期适配 | A退出；`evidence/disclosures.py`复用，拟 `supplemental_rules/disclosure_updates.py`、update jobs/source state；现有HTTP安全传输 | 只SZSE/CNInfo显式listing；独立后台source授权/用量预留、逻辑page与attempt身份、页协议扩展/原件→candidate事件桥接、429/漂移、索引/原件完整水位分离；不扩effective_cutoff；官方原件仍candidate，不放开通用verified |
| AF-P4-08 / B2 核验接缝与来源样本 | 07；专用事件事实adapter仅在定义明确时新增；证据精确读回，真实来源报告 | 最少一种明确文档类型的真实原件留存/身份/修订复核；若要verified，必须专用核验范围与反例。无法完成则保留candidate并将门禁标未通过，不阻挡A工程退出 |
| AF-P4-09 / C1 真实闭环与预算实测 | B、P3；拟 source-acceptance/research-quality/gates 文档与probes | 真实来源、暴露、影响/解除由人工分别签核；真实形状token/finish_only预算、失联/恢复、已核定交易规则另门禁；不足项逐项列出，不以fixture/skip代替 |
| AF-P4-10 / C2 整合验收与启用包 | 00–09；最终 execution/review/acceptance、分能力enablement-runbook | 第10节专项/必要全量通过；cap/退化/暂停/恢复演练；每项门禁证据、剩余缺口、允许范围、回退和用户待决清楚；仍不启用生产 |

**阶段退出**：A=00–06工程行为与本地受控导入完成、source未接/失联诚实显示、P2/P3全部共享接缝回归通过；B=07工程+08来源门禁分别记录，来源门禁未过时只允许离线/临时环境且保留candidate；C=09–10形成分能力可审批启用包，任何真实来源/暴露/质量/交易规则缺口保留关闭。阶段完成不自动放开下一阶段生产运行，也不自动取得真实付费调用授权。

## 10. 验证矩阵与最终工程门禁

所有工程测试用临时 file-backed SQLite（含WAL多连接）、临时 DATA_DIR、惰性凭据与可控HTTP/provider；不使用 `.dev`、真实库、真实付费调用或已授权的外部通知终点。故障注入必须走真实 handler/SQL事务，不能只改fixture状态后断言标签。

| 编号 | 真实验证链与明确断言 |
| --- | --- |
| P4-F01 接收 | HTTP真实写→原件/事件/receipt/run：响应丢失后重放相同身份；key冲突409、非法verified/跨主体422或404；commit前失败零半状态，commit后重启找回一份工作 |
| P4-F02 变化与修订 | 重复/乱序/旧版迟到/旧公布日期回填/新event UUID/同UUID新revision/更正文档；稳定业务UUID、精确previous、头部不倒退；未知暴露不变verified，旧问题不自动解除 |
| P4-F03 消费游标 | 真worker多页fanout，中间kill、checkpoint前后失败、两worker抢占/失租旧worker回写拒绝；处理集合=索引scope_member冻结成员，旧attempt不能写任何新表/应用结果；P2已有run全覆盖不双排、capped/partial只补遗漏；局部成功不推进完整水位 |
| P4-F04 due与启动 | 假时钟越过每种边界、timer不运行、服务停机后重启；同步HTTP读/publish/maintain立即限制，due receipt仅一份；next_due不会越过遗漏边界 |
| P4-F05 合并与dirty | 无case本地评估；case一个活跃cycle；研究运行时多事件、旧backlog/新critical同时到达；全部成员有交接/待人工证明，失败/cancel/partial不丢dirty、不建第二付费任务 |
| P4-F06 费用 | fake provider+真实budget：授权缺失/撤销、scope/price/预算不足零调用；admit与cycle关联间kill不重复；sent后断线/崩溃unknown不重发；已知usage的transient仅原task限额内重试；跨日不重置lifetime |
| P4-F07 关闭恢复 | updates/polling/supplemental/agent背景、case pause/archive逐项在queued、claim、pair中、reserve、sent、publish前关闭；保存已提交成果/审计，禁止越权后续作用；恢复不自动复活终态付费任务、不清旧限制 |
| P4-F08 来源HTTP | 可控真实HTTP服务模拟分页乱序、重复页、远端修订、404/429/5xx、超时、HTML代PDF、hash不符、redirect/host/issuer不符、schema drift、缺页/超大原件；旧接口第一页兼容、新分页/成功页复用/error新attempt/元数据变动自然键稳定；原件→candidate事件后即使worker没跑旧观点仍受限；前后撤权零可见事件/水位推进，水位和coverage正确，错误安全，无凭据泄漏 |
| P4-F09 容量公平 | 2000pair/4MiB、source页/字节/队列/磁盘上限、超cap恢复、新旧积压交错；无事件丢弃/静默截断，无平方scope哈希/逐成员全表扫描；真实EXPLAIN命中索引，计数不把unknown作0 |
| P4-F10 来源同步守卫 | 最后成功后跨健康时限、采集partial/conflict/失败、关闭源，维护任务停跑；GET零DML显示unknown/stale；发布并发注入失败使draft不closure；旧未绑定显示not_assessed，局部无关源失败不误伤其他scope |
| P4-F11 通知 | 真实PublicationOutbox消费+站内+fake webhook；publish后kill、发送前后kill、同case两路径、容量释放/配置变更/mute/archive、ACK未知两种幂等配置；稳定key/id/body、无重复正式通知，运行告警不冒充结论 |
| P4-F12 兼容与迁移 | fresh/052升级/无P4数据downgrade往返/有P4数据拒绝；新表为空但旧表存P4载荷仍拒降；归档V1 prepared/published payload/hash/ref字节不变、V2形状与hash严格；旧JSON/grant缺省；关闭态读、旧cursor拒绝、bootstrap不倒填窗口或旧通知；恢复含原件备份 |
| P4-F13 浏览器 | 真实API+worker fixture，1440/390：覆盖/失败/最后成功/due/积压/blocked/恢复、错误cursor、关闭仍可读、切主体无残留；ready/limited合成证据与真实链分开标，不作为真实研究验收 |
| P4-F14 域隔离 | 财务基础评分/Quality/DSL逐字段/黄金不变；账户变化不改公司观点；buy/add仍查fit/coverage，reduce/exit不因缺买入适配阻断；新critical/new event在开关关闭仍限制已绑定旧结论；effective_cutoff仅原可信collector可推进 |

拟测试文件：`tests/integration/test_p4_{intake,recovery,due,dispatch,source_coverage,notifications,migration}.py`（最终按实际命名记录）、unit契约/三值/状态机、`tests/browser/supplemental_updates.mjs`+真实fixture server；必要复用测试支撑，expected结果不能由被测service自动生成。

每任务先跑针对性真实链与接缝测试。最终门禁：P2全部专项及复审反例，P3 binding/coverage/publication/plan readiness/API/browser，P1 fit/ranking，已有monitoring/notifications/agent budget/recovery/settings/import graph/material/disclosure、三迁移专项。触及共享publish/admit/budget/queue/HTTP/settings后必须完整复测两侧；最终unit/integration/根目录三个完整分区（network默认排除须明示；真实源另验），Ruff `src tests alembic`、mypy全部源文件、新JS语法、`git diff --check`、迁移单head、浏览器三旧脚本+新脚本串行。网关契约按P3惯例配置本机网关目录，惰性API_KEY不可省略；缺材料skip按事实列出不伪造。记录唯一case、环境、源码快照、日志与失败迭代，专项/全量不累加。截图只保留P4自己的归档，旧P1/P2/P3探针不得顺手覆盖。

本轮是文档计划，不为可逆文本修改跑全量业务测试；只核对引用、路径、迁移图和diff/工作区保全。工程矩阵全部是后续验收要求。

## 11. 真实门禁、暂停与生产审批包

| 独立能力/门禁 | 通过所需证据 | 当前缺口与允许范围 | 暂停/回退 |
| --- | --- | --- | --- |
| E 工程 | 第10节、源码快照、主代理交叉审查、临时SQLite迁移/备份恢复 | P3已过；P4尚未实现。只批准计划 | 保持P4默认关闭，保留同步读限制 |
| S 真实来源 | 固定source contract/使用条件与限速；真实URL/native id/issuer/原件SHA256/页段/时间精度/取得回执；重复、修订、失败与恢复复核；明确历史起点/漏项范围 | 无P4报告；即使原件能下载也只可称candidate，不开放连续生产源 | 停polling、保留水位/原件；source状态降unknown，不能以暂停清风险 |
| X 公司暴露 | 产品/地区/实体/订单范围与有来源名单、company/listing映射、版本与完整性、受影响/不受影响/未知反例人工复核 | 未验；只显示待核，不能因行业标签自动归因 | 停相关scope研究调度，保留未决项 |
| Q 研究质量 | 真实事实/暴露/推断分离；正式review hash、反证与解除范围；人工评审至少覆盖触发、未知、修订/解除；真实预算/finish_only可用 | P3只证明工程闭环；没有真实质量签核。需后续明确样本和研究费用授权 | 停背景research；保留已有正式结论及current-limits，不自动续期 |
| T 生产交易规则与账户 | 逐市场/板块规则及有效区间/版本/可信供应，价格/费用来源、交易单位/例外与真实组合验收 | 仍UnavailableTradingRules，不能用“100股”常量。S/Q通过不开放账户ready或交易 | 停相应fit/ranking自动任务，buy/add仍守门；reduce/exit边界不扩大 |
| N 外部正式通知 | 现有API/web显式设置授权、加密/终点校验、接收端幂等能力、容量/unknown ACK演练与收件范围核实 | 不因P4工程完成授权新终点；运行告警外发不在首期 | 关external；未发送抑制、未知不重发，站内历史保留 |
| P 生产运行授权 | 用户单独批准代码/DB版本、来源和主体清单、历史起点、每次/每日请求/字节/费用上限、任务与通知能力、执行时段、观察期、暂停负责人及回退版本 | **本轮未授权**；不能把实现、配置默认值或门禁表当启用批准 | 按能力关开关/授权；有数据禁有损降级、不运行不兼容旧二进制 |

真实样本最小集：一个真实官方公司披露原件及重复取得；一个真实更正/撤回关联（找不到可核样本则该能力门禁待验，工程故障用fixture另测）；同事件明确暴露/明确不适用/未知主体；至少一条真实到期与来源失联恢复；一条经独立人工复核的正式研究及带证据解除或拒绝解除；真实账户交易规则验收另记，不以这些公司样本替代。来源许可、live请求和任何付费研究在后续授权范围内执行，工程测试不能擅自触网花费。

生产后续 runbook 必须逐步列出：只读核部署DB/runtime/镜像和门禁→备份与恢复可用性→先限定源/主体和历史窗口→开启本地能力→按授权再开单源/背景研究/外发→观察 source coverage、最老积压、unknown费用、通知unknown/失败→触发暂停条件与负责人。暂停条件至少包括身份/hash/页链完整性错误、重复付费迹象、unknown费用、来源超过合同失联时限、不可恢复积压、越权或限制被绕过；动作是停止相应新工作并保留限制/审计，不自动撤销观点或做交易。本轮仅定义该runbook要求，不执行任何步骤。

门禁与动作明确对应：E→仅P4本地updates∧supplemental；S→polling∧originals及专用来源授权；X/Q→P4背景研究再取agent/background+Mandate；T→account_fit/candidate_ranking及真实交易规则来源；N→external notification显式设置；P→实际部署/迁移/启用动作。任一项都还须P的独立生产批准，代码不会读取本表文字作为运行授权。

需要用户在后续决定（本轮不阻塞计划）：首个联网源是否按推荐的现有CNInfo/SZSE范围、具体listing/文档类别与历史起点；真实样本与独立人工评审人；背景研究每次/每日费用和已授权范围；是否开外部正式通知及目的地。默认按最窄范围设计，未决定部分保持blocked/关闭。SSE/BSE/监管联网源、独立外部运行告警、交易规则供应实施如需纳入，应先补范围与门禁，不能混入P4隐式建设。

## 12. 计划审查与主代理裁决

三名 GPT‑6 Luna 子代理已分别完成来源可信性、调度/授权/恢复、通知/观测/启用门禁的只读审查；主代理亲自核对共享接缝后形成以下裁决。首轮草稿均获条件通过；主代理将条件落实成下表具体方案后，原三名子代理再次逐章复核，均结论“计划可通过、未发现剩余阻塞”。未把审查意见冒称已实现能力。

| 编号 | 发现与最终计划裁决 |
| --- | --- |
| R01 P3状态 | HEAD为P2、工作区为已验收P3；以最终验收/执行/冻结覆盖早期“P3待做”文字，P4不重复binding/closure/新事件限制 |
| R02 来源能力纠偏 | 代码已有 `DisclosureService.discover/fetch` 的CNInfo/SZSE在线能力；不能写成“完全没有公告采集器”。缺的是持续运行、覆盖/修订合同和真实来源/事实验收；按A本地→B有限单源补齐 |
| R03 cutoff | 不采纳“源poll成功即可更新任务cutoff”的宽泛建议；P4不扩展现有collector白名单，晚到资料用新授权任务，避免绕过可信任务回执 |
| R04 数据量最小化 | ReviewCycle/MonitorReceipt的case FK无法承接无case与源游标；只增三张窄表，队列/租约/预算/原件/outbox复用；任何额外表需证明现有三表和PipelineRun不足 |
| R05 不漏与外部exactly-once | journal同事务+durable fanout+交接确认；queue满保留待处理。外部请求未知停自动重发/对账，不虚称绝对无重复收费或互联网无遗漏 |
| R06 dirty共享接缝 | 现有settle仅取dirty[-1]且失败停止，P4不能据此声称逐事件已处理；补完整成员持久交接，失败保留待人工，不自动重付费 |
| R07 来源健康 | 独立于timer按at同步算stale，与prepared来源绑定及发布/续期共享谓词；没有采集到≠没有风险；不把全局源故障强加无关主体 |
| R08 通知授权 | 沿正式outbox/冻结delivery；运行告警先只进本地operations，不扩大正式外发权限；补capacity_blocked恢复而不换ID/重生未知通知 |
| R09 旧数据与回退 | 不回填旧hash/window，不追认旧来源覆盖；bootstrap与可变行完整补偿扫描；不能用不识别新限制的旧二进制回退绕过守卫 |
| R10 分能力门禁 | 工程、来源真实、暴露、研究质量、交易规则、外发、生产授权分别记录。P3的合成浏览器桶和P4工程fixture不算真实验收；生产另批 |
| R11 P2重复排队 | Luna与主代理共同确认lifecycle/events已排原run。journal保存origin_run_ref/hash，逐pair认领冻结范围及真实产物；只补遗漏/due/新资料，P2原生关闭行为保留 |
| R12 成员与fencing | 单一journal补scope_member内部行与关系索引，PipelineRun冻结精确成员/证明；所有新表写入同事务验证worker/attempt/running+CAS，不只fence最终run状态 |
| R13 后台来源权限 | 原task DisclosurePermission不能供cron循环使用；新增明确api/web许可revision、窗口/有效期/日配额、请求前预留与落库前撤权重查，不假冒任务或借Mandate授源权限 |
| R14 分页与事件桥 | B明确扩受限page协议，逻辑page与实际attempt分离，error收据用新attempt；保存页链，索引与原件水位分开；native文档→稳定candidate事件修订，events_generation同事务让P3立即发现 |
| R15 hash与降级 | 不向V1 DTO加默认字段；coverage新增V2且parse/hash按原schema分派。降级守卫检查三新表及旧assessment/run/task/grant的P4载荷，包含新表空反例 |
| R16 通知恢复 | failed终态；unknown仅已授权幂等有限重试、终态等待外部对账无重生入口；容量恢复全局锁下重核station/case/config/权限，operations明确为现有认证API |

主代理最终验收采用文档级检查：引用/实际路径、决策闭合、P2/P3接缝与计划内互相一致、无业务/迁移改动、原工作区内容保全；后续实现与真实验收仍须第9–11节门禁。

最终审查结论（2026-10-17）：来源子代理确认支持类型/人工核验边界、后台许可、页重试与candidate事件桥接相容；调度子代理确认原run防双排、精确dirty集合、全写入fencing、撤权与容量恢复边界闭合；通知/门禁子代理确认V1/V2 hash兼容策略、旧表载荷降级守卫和通知终态策略闭合。主代理接受三份复核结论，**P4实施计划通过；P4工程、真实来源、研究质量及生产启用均未通过或启动。**

本轮文档验收：本计划及主设计的本地Markdown引用全部存在；`alembic heads`仍052单head；`git diff --check`通过。对本轮开始时1647个已跟踪/非忽略未跟踪文件的SHA256逐项比对，仅主设计文档变化，新增文件仅本P4计划，无文件移除；P3源码/测试/052迁移、移动端和其他并行文件内容均未改变。无业务测试重跑、无新迁移文件、无生产命令、无开关写入、无提交/推送。
