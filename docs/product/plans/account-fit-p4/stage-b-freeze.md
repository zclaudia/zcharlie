# AF-P4-07 工程契约冻结与实施裁决

2026-10-17，Asia/Shanghai。用户已授权AF-P4-07工程实施，**只用临时环境/可控HTTP**；AF-P4-08真实采集、生产操作/开关、提交和推送未授权。依据[经审查B计划](stage-b-plan.md)。本记录冻结共享约束；实际执行和测试见[执行记录](stage-b-execution.md)，工程完成前不得将本记录当验收。

## 基线与保全

HEAD `7df697aeeb951f9bf1b76de7b2194ca6a62c6e7a`；开始时1689个已跟踪/非忽略文件摘要保存在本机`/tmp/zcharlie-p4b-implementation-start.json`。A的60项源码变更与原snapshot全部匹配。A未提交源码为实现基线，不重置；mobile Cargo.lock、050—053归档、财务/Quality/DSL/账户约束/collector白名单和旧probes保全。P0—A probes已备份到本机临时目录，浏览器后恢复历史文件，仅归档B截图。

## 责任与共享接缝

- GPT‑6 Luna授权域：新增`disclosure_contracts.py`/`disclosure_authorization.py`，严格grant命令、owner-source policy/grant/day/poll的CAS配额及HTTP attempt阶段审计和测试。
- GPT‑6 Luna来源域：新增`evidence/disclosure_pages.py`/`supplemental_rules/disclosure_updates.py`，single-hop安全传输、页协议、candidate原子桥接、有界poll step/恢复/公平和测试；只限必要P2私有持久化/受限命令接缝。
- GPT‑6 Luna覆盖域：V3 DTO/coverage/current-limits、054后继降级guard及按EXPLAIN必要索引、旧hash兼容、同步发布/续期和UI测试。
- 主代理：API、operations/worker唤醒接缝、共享事务/授权/hash交叉审查、实际HTTP与browser整合、完整回归和最终验收。各域不得互相建立第二份source contract/hash算法或通用授权/队列平台。

## 冻结的不变量

1. 原件复用SQLite DisclosureBlob；三张A表承载来源状态/内部grant/quota/document映射，内部feed不进入声明来源统计。原生文档键为source+issuer+native公告号，owner隔离；blob可共享，Original/provenance不可跨公告冒用。
2. 每个实际HTTP hop均有独立reserve commit→sent commit→一次调用→result。sent为许可线性化点；redirect最多2次、每hop重新授权/计量；底层不得隐式follow/retry。关闭或撤权在途只计安全审计/额度，不接收可见正文/事件或推进水位。失租不能写任何业务或来源checkpoint。
3. 稳定attempt关联各阶段独立receipt；仅用户命令根operation_id非空。无sent的reservation在旧attempt fenced后才可释放；sent无result按响应上限消耗。跨日发送先原子迁移未sent预留；新grant/revision/operation不能清旧用量。实际owner-source日额度须显式授权并CAS，不以硬cap替代用户总额度。
4. poll根receipt与可轮换PipelineRun step分离，每quantum最多1HTTP hop或20local；recent/backfill各lane轮转，cooldown不占token/长租约。全部step持久化输入、序号/进度，通用worker重试不得透明重发未决HTTP。
5. 索引页与逐文档原件事务分开。单文档blob/Original/Evidence/candidate event revision/generation/P4 journal及checkpoint原子；公共user_report入口不放宽。candidate只说明该发行人披露文档，不产生verified/产品暴露/影响推断；更正/旧hash回现按B计划隔离。
6. V3为显式poll-window proof，V1/V2 DTO/hash不动；空结果必须有真实有界成功页。原件、桥接、分类、封口任一缺口均不能完整。source失联/撤权/关闭/到期不移除旧来源声明以解除限制。
7. cutoff内proof和原件才可进入prepare；now仅判断当前健康。S0的真实分类/tie-break/末页/条件未实证，生产协议保持未核实/unknown；工程正例只能由显式`for_tests`注入可控协议，认证API/模型不能选择测试协议或自授来源验收状态。
8. schema V3和B载荷需独立054（实际编号核head）降级guard，不改050—053。三表空但旧assessment/run/task/grant/DisclosureReceipt仍含B/V3时拒降；匹配明确schema/tag而非模糊键名。未知schema fail closed，旧二进制不能作为有V3数据的安全回退。
9. 来源grant不授研究/通知/交易权限；有独立A Mandate才可沿原A检查派发合法新研究，失败/cancel/unknown任务不重生。本轮工程和browser无真实provider/付费授权/外发终点；运行告警仅本地operations。

实现中的具体DTO/API落点和纠偏均在执行/复审中追加，以可运行代码和反例测试证明；不得删减B计划的必要门禁来凑完成。

## 整合裁决

- 收据证明使用无环链：原始 `poll_index_v1` → `poll_document_v1` 叶子 → `poll_page_v1` 桥接 → `poll_final_v1`。第二遍索引独立存原响应、HTTP阶段链并由桥接精确引用；final不在自己的payload中嵌入自己的hash，读取时补header ref。
- R3直接沿现有不可变ResearchEventRevision/registry CAS/P4 journal接收candidate；不为仅接收一份原件创建一个立即执行的P2评估run。A coordinator随后沿已有`accept_evaluation`授权受理本地重评/研究交接。这样仍保留原件/事件/来源语义代次/journal/checkpoint同事务，来源grant不能绕过A的研究授权。
- quota投影采用现有journal内的typed不可变authority receipt绑定CAS revision和完整计数；缺authority、hash不一致或revision不一致立即阻断，不能用投影自hash作为权威。每次更新同事务追加，不在HTTP请求路径无界扫描全部历史。离线审计可分页重放阶段账；不会自动减免unknown用量。
- A本地更新和B来源poll共用128个queued/running容量。新命令在受理前满额可明确拒绝；已受理工作持久保存capacity_blocked续点，不能丢journal/cursor或伪完成。终态run不占额度。
- 工程测试协议持久标记`protocol_profile=fixture`、`protocol_status=fixture_validated`。正常prepare/read/publish路径不接受其正向覆盖证明；唯一正例测试接缝是显式session.info，HTTP/API/模型/环境开关均不能申请该认可。生产默认协议仍unvalidated，S0门禁保持未验。
- 新poll重置任务游标，resume保留原游标；已完成poll的同事实健康续查保留epoch/anchor/semantic_generation。单窗的三种水位与累计连续三水位分别冻结；自动补查的目标时间在root受理时固定，完成一个早期子窗不算整段历史完成。
- 手动替换未完成poll必须显式`replace_poll_id`及原因、精确scope CAS、旧运行已终止；先保存不可变checkpoint snapshot及有界待处理集合。新root/final绑定精确snapshot及pending hash，旧HTTP用量与未完成logical attempt计数继承；新窗口未覆盖旧窗口/待处理文档时只能partial，不能以替换清缺口。旧poll详情可读归档进度与superseded_by。普通已完成健康轮次不继承为一次失败重试。
- maintenance在当前租约下机械清理已fenced旧hop：可证未sent者沿原取消机制返未用预留，有sent无result者按unknown结算；不要求旧grant仍有效，不请求网络或接收新资料。仍存活的旧worker不被清理。维护唤醒合并并节流，不能无进度时高速制造新run。
- 关闭/撤权发生在HTTP在途时，实际次数/响应bytes及安全hash审计仍结算，未获当前许可的正文不持久化，不建立可重放响应。已存在且被旧资料引用的共享blob不删除。
- 同basis的健康续查以`previous_final_ref`精确绑定上一成功不可变final及累计三水位；缺失/篡改或与source投影不一致则停止封口，不从可变投影继承未经证明的历史。loader直接核前一代header/hash/basis/epoch/generation/anchor及连续性，不递归全历史。首次或新epoch只有窗口从history_start开始才能独立建立累计水位。

- 语义代次与历史basis分开。root冻结`semantic_generation_base`及精确base final；已失败poll通过≤20个checkpoint snapshot中的≤200个唯一叶引用证明继承增量，本轮按原子`semantic_change`叶证明当前增量。收集本页、primary pages及已桥接的叶并去重，不能漏掉封口前已提交的candidate。新事实即时限制旧绑定，但不阻断其后合法新final；同事实重取增量为0。连续水位全null的旧审计final不妨碍后续从history_start完整回填建立前沿，非连续窗口/pending仍不能建立完整覆盖。
