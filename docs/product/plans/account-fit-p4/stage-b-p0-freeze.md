# AF-P4-08 / 08-P0 离线工程冻结

2026-10-17，Asia/Shanghai。用户在08计划交叉复审后单独授权08-P0；本文件记工程接缝，最终源码/测试/复核状态以本轮验收为准。08-P0专属工程验收已通过；共享全库unit/Ruff存在单列并行银行域失败，见[验收](stage-b-p0-acceptance.md)，未执行S0、08-P1实际profile装载或真实样本，未操作生产、真实开关、提交或推送。

基线：[08方案](stage-b-source-plan.md)、[计划复审](stage-b-source-plan-review.md)、[07验收](stage-b-acceptance.md)。起始HEAD为`b75d5f28431222f3a56ab5c18d86ef2f9770dbef`；1757项tracked/nonignored起始文件摘要用于保全，并行银行域和移动端继续保留，不能用git diff相对HEAD代替本轮归属。开始时07自身43项源码均与历史快照匹配；050—054历史迁移和旧验收保持原字节。

## 严格授权与累计账

新增独立V2 body/command/snapshot/result；旧V1字段、literal和hash域不变。V2含campaign_id、sample_env_id、manual_only=true、valid_from，sample有效期≤72小时。V1解析器拒绝V2额外字段；amend不能在V1/V2间切换或更换campaign/env。旧V1接口不新增创建V2权限，内部受审查服务端命令使用严格V2。

campaign使用现有P4 source-state与immutable change journal，不新增表。CAS投影绑定权威收据、单主体/精确动作、绝对时段、真实SQLite规范路径及文件身份；跨日/新grant/修订不清累计HTTP/响应/逻辑存储。总帽32/192MiB/512MiB，S0子帽8/40MiB/128MiB，sample子帽24/152MiB，配置只能更严。每action唯一poll，第三slot首次reserved/disabled，不能靠余量受理；只有精确批准修订可解锁。`campaign_profile_hash`明确指批准profile内的`protocol.protocol_hash`；contract_hash另绑定完整协议/代码/URL策略。

native sample hop和campaign hop的reserve/sent/result/unknown/cancel在同一短写事务提交；sent提交后才允许一次HTTP。结果不明按上限计量，不退款；撤权/关闭不能阻止旧结果的安全计量。两个账本不能局部提交。raw probe同campaign/S0/action累计，并复用owner-source/day/cooldown/storage边界；不能独立脚本漏计。

## 两类批准与执行入口

S0 probe使用独立的精确operation批准，未批准来源协议也可在未来单独许可下观察raw；不依赖source profile自证。条款和数据动作分范围，每hop固定URL、方法、请求体摘要、响应/存储帽、绝对时段与campaign。默认批准registry为空，只能服务端显式装载；无API/env/model自动批准入口。原件、索引和条款仅进入隔离raw/audit，不生成正常SourceCoverage、Original、Evidence、Event、V3或公司fanout。

source profile registry也默认空。protocol_hash排除自身hash及validated派生标记；contract绑定protocol/hash域、schema、当前adapter build、URL policy、类型/时间/页序/覆盖语义；人工批准另绑定目标摘要、依据、具名不同执行/评审人、用途及时段。重复装载不能复活撤销批准。正常service只认可当前服务端批准，for_tests仍fixture；默认没有官方profile。

手动采样复用普通非测试handler与native lease/heartbeat，但仅exact runner向精确run/poll/action/env注入内部dispatch capability。scheduler/maintenance不枚举或恢复manual任务，普通worker领取也不能出网。run_once限定run_id；recovery-only不运行广域维护、不补排自动采集。响应回写、读取、prepare/publish/renew同步核当前批准和权限，不靠定时器失效。

## 恢复与兼容

旧V1自动轮转、旧fixture、V1/V2 saved refs、P2/P3知识窗、发布CAS/原子闭环保留。新profile不追认旧raw/fixture，不推进effective_cutoff。账户、Quality、DSL、财务基础评分、reduce/exit、付费研究和正式通知权限不扩展。

源码迁移图继续054：未增加表、列或索引，050—054不修改。054原有downgrade守卫在任一P4窄表非空时即拒绝，覆盖新增campaign/raw状态；严格V2使旧grant reader fail closed，因此不为这些JSON状态另加空迁移。若以后把新载荷放入未覆盖的旧表，必须另审守卫，不能借本结论豁免。

备份为离线一致快照；独立`.p4-08-<campaign>.sent-anchor.json`不随旧SQLite备份回滚。建campaign即建立0600锚，sent提交前fsync推进最高不可变sent receipt；读取校验该收据仍在当前DB。SQLite backup写回同inode也不能抹掉已知发送；sent事务回滚留下超前锚时保守blocked，须人工离线对账，不自动删锚解锁。锚缺失、损坏、文件身份或路径变化默认拒绝继续发送，不能复制旧库清账。旧备份不能证明最高sent/result时保持unknown和停止，不自动重授未消费余额。原件/引用、旧风险和不可变账保留，关闭/失败/过期不解除问题。

## 工程退出

先过新契约/账本/受控HTTP/handler/runner/恢复/当前profile专项与交叉审查，完成unit、integration、roots、五个浏览器及Ruff/mypy/JS/迁移图/保全检查；P0专属工程签收与共享库全绿分开记录，并行域失败不得隐藏或擅自修改。各次失败与修复如实记录，不累加重叠测试，不把工程通过记为S0或真实来源通过。实际许可/来源合同、全文摘要区分、稳定tie/末页、真实样本及X/Q/T/N/P仍分别未验。

## 实际模块层次

基础URL/HTTPS、分页与批准profile在`evidence/disclosure_http.py`、`disclosure_pages.py`、`disclosure_profiles.py`；raw probe编排与真实账本适配在`supplemental_rules/disclosure_probe.py`，避免底层evidence反向依赖领域调度。精确worker运行入口在`research/disclosure_campaign_runner.py`，不让supplemental_rules依赖research。单位import graph守卫首先暴露这两条边后，实际移动新模块解决，没有增加本轮允许边；并行materials工作增加的`materials -> settings`保留并单列，不归属P0。

campaign状态/授权V2在`disclosure_campaign.py`和`disclosure_campaign_contracts.py`；共享raw日账与安全机械恢复在`disclosure_probe_quota.py`；独立sent锚在`disclosure_campaign_anchor.py`。这些位于supplemental_rules目录。旧V1 grants不开放live；新manual执行入口明确拒绝memory/SQLite URI，只接受文件数据库。未发送取消只终结permit并释放raw根，同一旧permit不能再sent，新明确批准的操作可在原未消费预算内恢复；已sent result/unknown始终one-shot终态。手动poll故障后不自动排队，独立recovery票只结算既有hop，不handoff新HTTP。

当前批准还覆盖两处读面：API scope读与operating metrics均从source行`config_hash`取得来源合同，传给只读批准检查，不回写历史payload。grant expiry/campaign pause及registry撤销均同步影响读/prepare/publish/renew。

## 显式服务端操作与恢复票

将执行入口限定为经审查服务端操作者的显式调用，无API/env/model批准入口或常驻worker循环：

- `create_disclosure_campaign` / `apply_authorization_command`保存严格V2及真实file SQLite/env绑定；来源与probe registry仍默认空。
- `DisclosureProbeExecutor.capture`仅一次精确operation；`CampaignProbeLedger.recover_hop`按permit/campaign/env/operation机械取消未sent或满额结算sent unknown，重启后新ledger仍沿原账，不调用capture/HTTP。
- `admit_disclosure_poll`绑定精确campaign action；`run_disclosure_campaign_once`只claim指定run及合法后继，不运行普通maintenance。
- `admit_disclosure_campaign_recovery` / `run_disclosure_campaign_recovery_once`需要独立精确恢复票，旧grant撤销/campaign暂停也允许结算原hop，结束为manual_recovery_required并清active_step，不排新请求。
- 失败后的采集resume是另一个明确命令：先由操作者暂停campaign，再调用`resume_disclosure_campaign_poll`保存绑定原grant revision/hash、profile、poll/action/env的resume票，CAS恢复同campaign并只排一步。没有新scope/deadline/预算或attempt额度；当前Retry-After、profile/原grant、quota、根状态和未决reservation均重核。generic API的V2 resume仍拒绝。

`approval_ref` / `resume_approval_ref`与operation ID是受信服务端操作者提供的独立批准证据引用，进入不可变审计；代码检查精确绑定、时限、CAS、去重与预算，不独立验证引用背后的人工签字或许可文本。未来实际运行仍须另获具名、具体范围的批准，不能把非空字符串或内部函数可调用性解释为授权。普通自动路径、API和模型不能据此造票。
