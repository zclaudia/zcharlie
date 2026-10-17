# P4 分能力门禁与暂停说明

日期：2026-10-17。阶段 A 工程结论见 [acceptance.md](acceptance.md)；AF-P4-07 工程验收已通过，见[B工程验收](stage-b-acceptance.md)；AF-P4-08的[执行方案](stage-b-source-plan.md)已[交叉复审](stage-b-source-plan-review.md)，08-P0离线工程已实施，工程结果见[08-P0验收](stage-b-p0-acceptance.md)；S0/真实样本未执行。[阶段B计划](stage-b-plan.md)及[计划复核](stage-b-review.md)保留历史。本表不是运行授权，不由程序读取，不执行生产步骤。

| 能力 | 当前证据/缺口 | 下一次授权或验收要求 |
| --- | --- | --- |
| A 本地持续复核工程 | 代码与临时 SQLite/HTTP/worker/浏览器验证；真实来源和研究门禁分开 | 工程验收完成后仍保持默认关闭。若后续要运行，先独立核对部署版本/数据库/runtime 设置、备份恢复、明确本地 scope 和积压起点，再取实际启用授权 |
| B 有限披露来源工程 | 07 已实现受限 page/HTTP、后台 grant/额度、candidate 原子桥接、持久 poll/调度/恢复和 V3 证明；工程验收状态见独立记录。真实协议默认 unvalidated，fixture 不能正向认可生产覆盖 | CNInfo 指定 SZSE 发行人的年度报告全文及更正/补充候选；08 有限 live 仍须精确授权。S0 使用条件/字段/分类/限速及 S1—5 真实原件/重复/同号修订/更正关系/反例/空窗恢复分别签核；工程 fixture 不替代真实样本 |
| S0/首批受控样本 | 08-P0已补默认空的受审查profile/probe注册、V2累计账/manual-only/exact runner；未安装真实profile。000858全文/摘要的分类、页序、末页及许可仍未核 | 08-P0工程不能授真实请求；S0-T/P、profile、精确P1/P2仍分别取得许可并过门禁。提案campaign32 HTTP/192MiB含S0 8/40；第三poll服务端reserved，不因有余量放行。缺更正/同号修订/反例/空窗/恢复则08未过；不以计划通过授执行许可 |
| 可信事实 | A 只读回已保存 refs，user_report/原件/candidate 不升级 verified | 真实官方原件身份、hash、issuer、时间和修订链，专用适配核验范围与反例；普通手工导入不是核验授权 |
| 公司暴露 | 行业/分类只是候选范围；公司/证券映射工程存在，不等于公司真正受影响 | 明确受影响、不受影响、未知的真实主体样本，以及产品/地区/实体/订单证据；由独立人工逐项签核 |
| 正式研究质量与收费可用性 | P3/P4 工程闭环和预算拒绝已测；未进行真实付费研究或质量签核 | 先定样本、评审人和 per-task/daily 费用上限；验证真实 token/finish_only/费用未知对账、影响/解除反证；另获付费与背景研究范围授权 |
| 账户/交易规则 | 沿用 UnavailableTradingRules，不以常量交易单位充当规则供应 | 分市场/板块/生效区间的可信规则、价格/费用、例外与真实账户组合验收；不因 A/B 工程完成开放交易或账户 ready |
| 正式通知 | 正式 outbox 与 delivery 工程复用；没有新外发目的地或运行告警外发 | 后续单独确认站内/外部范围、endpoint、接收方幂等能力与未知 ACK 对账办法；现有 API/web 显式授权，不借 Mandate 自动授权消息 |
| 生产运行 | 未执行迁移、采集、付费、通知或启用 | 用户另行批准版本/DB head、具体能力/主体/历史起点、请求/字节/费用配额、时段/观察期、暂停负责人和回退版本 |

阶段 A 中的新开关 `supplemental_updates_enabled`、`supplemental_disclosure_polling_enabled` 均默认 false；当前 bootstrap 只读核对也为 false。测试中的开关操作全部在临时数据库。生产 DB runtime overlay、容器 env、实际迁移版本并未核实，不能由源码 head 054 或测试配置推断已经部署。

暂停语义已经落实：关闭 updates 阻止新 P4 本地受理、已有 P4 本地 worker 后续检查点和 P4 背景研究付费边界；原生 P2 保持其原 supplemental 开关语义。领域变化仍保留 journal；读取和发布同步限制继续生效。不得因暂停、到期、退役、采集失败或关闭开关解除旧未决事项。恢复沿原收据/预算/精确成员，新代次只处理 pending，不复活终态付费任务或未知通知。

若后续生产发现来源身份/hash 错误、越权、重复付费迹象、费用/ACK 未知或不可恢复积压，应暂停相应新工作并保留审计/限制。实际操作仍须当次授权；本轮没有执行 runbook。053 三新表或旧表 P4 载荷非空时拒绝有损降级；054 进一步守住 B/V3 载荷；不得用不理解新限制的旧二进制绕过守卫。

阶段B计划按最窄推荐值形成，不要求先授权采集才能讨论。实际执行前仍需用户明确源/精确主体与文档、绝对窗口、有限live的有效期/频率/请求字节存储额度及独立来源评审人；建议额度不是授权。B工程、S真实来源、X公司暴露、Q研究质量/费用、T交易规则、N外发及P生产运行分别守门。B来源grant不新增研究/外发/交易授权；若A已有独立Mandate，新事实仍可能沿A产生新研究费用，真实样本用无付费权限的隔离环境。SSE/BSE、通用监管政策流、全网新闻覆盖和自动交易不扩入。

AF-P4-07 的暂停/恢复补充：关闭或撤权在 sent commit 之后不能撤回在途 HTTP，但仅结算次数/bytes/安全 hash，正文不进入可见资料；未知按上限计量。新 poll 不清日用量，显式替换未完成 poll 须保留不可变进度、逻辑重试次数及旧缺口。维护可机械结算已失租的旧 hop，不能借此授新请求。历史三水位与当前单窗进度分开显示；尚未补齐历史时，近期成功也不报完整覆盖。B 不增加研究或消息外发权限；没有既有独立 A 授权时新 candidate 仍待处理。

08-P0恢复边界：raw orphan只通过精确permit/campaign/env的机械结算服务取消未sent或满额结算sent unknown，不发HTTP；sample recovery-only仅结算旧hop。重新采集必须另有绑定原grant/hash/profile/poll/action/env的显式resume票，原campaign绝对期限、quota及logical attempts全部继承，已完成、unknown或terminated不能重生。备份外sent锚缺失/超前/与SQLite最高收据不符则停止并离线对账，不删锚或换库清账。

08-P0后S0具体执行以[实际清单](stage-b-s0-execution-plan.md)和[复审](stage-b-s0-plan-review.md)为准。当前新增门禁：初始票必须明确P精确条件授权才可建八slot campaign；raw普通poll开关无kill能力，需撤销probe批准并暂停；paused raw只结账、无采集resume；未知真实profile不能后绑定到原campaign的P1/P2，先独立补离线工程才可连续样本。明确S0-only可只观察来源，但样本子帽0，不称08整体过关，不另起campaign重置已用量。具名人工、时段、许可/留存和执行模式均尚待决定，未执行S0。

连续性修复已有[离线计划](stage-b-continuity-repair-plan.md)/[交叉复审](stage-b-continuity-repair-review.md)，尚未实施。新增工程门禁包括纯S0的显式V2→V3提升、旧V2 raw发送封闭与旧进程隔离、原UUID/账/总帽/期限/sent锚保全、control锚保护全部V3 authority/额度状态、一次样本分配与独立授权、原hash原包未消费suffix恢复和每票epoch fence。raw已sent/已取消/unknown/过期/撤销不复活。工程通过后仍要另核S0签票与真实来源门禁，不能提前采集。
