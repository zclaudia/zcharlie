# AF-P4-08：S0 与首批真实样本执行方案

2026-10-17，Asia/Shanghai。**当前状态：08-P0已获后续独立工程授权并实施，最终工程验证见[08-P0验收](stage-b-p0-acceptance.md)；S0、真实profile装载及真实样本仍未执行。** 原计划审查只修订文档的历史结论保留在[计划复审](stage-b-source-plan-review.md)，不能覆盖后续工程授权。本文件第1节的“当前缺口”为计划审查时快照，实际工程契约及模块落点以[08-P0冻结](stage-b-p0-freeze.md)为准。主体、窗口及真实请求额度仍是待独立批准的提案，不是采集授权。

S0的当前逐项执行约束以[08-P0后S0-T/S0-P实际清单](stage-b-s0-execution-plan.md)及[本轮复审](stage-b-s0-plan-review.md)为准：补足精确body/配额/分包，并单列raw开关、暂停恢复与真实profile后绑定缺口；尚未授权或执行。

本文件细化[阶段B计划](stage-b-plan.md)的 AF-P4-08，具体执行边界以本文件为准；不改写[07工程验收](stage-b-acceptance.md)、[工程冻结](stage-b-freeze.md)和[工程复审](stage-b-engineering-review.md)。后续计划审查结论见[08计划复审](stage-b-source-plan-review.md)。

## 1. 历史基线与08-P0之前的三项前置缺口

本轮起始核对时HEAD为 `7df697aeeb951f9bf1b76de7b2194ca6a62c6e7a`；07是其后的未提交工作区。起始逐项核对其[源码快照](stage-b-source-snapshot.json)：43项自身变更匹配，1261个src/tests/alembic文件树摘要为 `7be6287cf9d77d60f5cf8920e2c8573cd42780eafdf38bceb9c03514c18db6dd`。源码迁移图054单head，未读取部署版本。07的集成证据是906项初次通过、3处旧head断言修正后迁移全组37通过，不冒称单次909全绿；其余证据以原验收为准，不在本轮重跑业务测试。

只读bootstrap的supplemental/updates/polling/originals/agent/background/external均false；实际部署runtime overlay未读取。本轮保全开始时移动端、银行域及P1探针等既有改动，不恢复、不混入08。收尾时并行银行域提交使HEAD变为`b75d5f28431222f3a56ab5c18d86ef2f9770dbef`，另有银行年度滚动工作进行中；07自身43项仍匹配，但当前全树不再冒称等于07历史摘要。此提交非本轮操作；差异与保全检查见[复审](stage-b-source-plan-review.md)。

| 已有能力 | 08实际缺口与裁决 |
| --- | --- |
| `DisclosurePageService`、普通`DisclosurePollingHandler`、逐hop授权账及V3 | 普通service的`protocol=None`，worker会`blocked_protocol_unvalidated`；只有`for_tests`能注入协议并标fixture。目前没有受审查的live profile装载入口。不能靠改对象字段、env、API/body、测试session旗标或假`live_validated`跑真实来源 |
| grant日配额、owner-source日配额、poll额度、storage、不可变authority/CAS | 当前V1没有`campaign_id`或跨日/跨grant累计请求与bytes帽。08所列一次性总预算必须补工程约束；不能拿日额度代替总许可 |
| 自动维护、近期/历史轮转、恢复 | 当前grant没有`manual_only`；正常maintenance会遍历active grant并排due。受限样本不得直接交给普通daemon。仅`kinds`过滤不足以禁止同kind的maintenance；需要手动授权模式、精确poll runner与recovery-only接缝 |

这些是08受控执行前置，不推翻07工程验收，也不表示本轮获准实施它们。不新增通用新闻、授权平台、队列或原件库；优先复用三窄表、PipelineRun租约和HTTP阶段账。必要新wire必须独立版本化；不向旧grant V1加默认字段后重算hash，不改V1/V2 coverage，不改050—054冻结迁移。若新wire的降级识别需要后继guard，先说明具体缺口另审独立迁移，不能假定054已识别未知tag。

## 2. 推荐主体、报告与绝对窗口

首源仍为 `cninfo.v1` / feed `cninfo_szse_v1`，限SZSE。**建议首个主体为五粮液 `000858.SZ`，2025年年度报告。** 选它是因已有可回读的本地选样线索，避免把并行银行域验收混为P4来源结论；不是投资推荐或已核实的当前官网状态。

| 项目 | 未验seed与后续锁定要求 |
| --- | --- |
| 本地主体 | 目标ticker `000858`、market `SZSE`；名称候选“宜宾五粮液股份有限公司”。未来在全新临时库解析精确`listing_ref(kind=listing, id=实际ID, revision=1)`；不填写fixture ID，不连接生产库找ID |
| 来源身份 | 本地归档orgId `gssz0000858`，全文native ID `1225273091`，URL seed为`https://static.cninfo.com.cn/finalpage/2026-04-30/1225273091.PDF`。未来须由授权S0目录与索引重新核实；有歧义或与seed不同则停，不按名称/标题强行映射 |
| 历史观察窗W0 | 上海自然日半开范围 `[2026-04-27T00:00:00+08:00, 2026-05-04T00:00:00+08:00)`，共7日；只允许这个具体窗口，不查全年/全公司池 |
| 与当前接口对齐 | 现parser按`start <= published <= end`过滤，wire end为闭端点。将W0固定为UTC `2026-04-26T16:00:00Z` 至 `2026-05-03T15:59:59.999Z`（毫秒源的最后1ms）；查询`seDate=2026-04-27~2026-05-03`。不直接把5月4日零点送作闭端点，不把窗口解释差异当迟到数据。若S0证明源时间精度不同，先修订合同和边界方案再发请求 |
| 原件seed | 已有本地PDF 3,085,658 bytes，SHA256 `09133e1f44b3bb4b2cebe211529ad68f5b04b6be10b43870f2a78c947d5910a4`；归档时间字段`1777544425000`对应`2026-04-30T18:20:25+08:00`。这些仅是本地文件事实，**不固定未来真实取得的预期hash/发布时间真值**；不同hash先隔离复核，不能据seed覆盖官网新版本 |

本地依据：[选样元数据](../../../../research/industry-data-validation/evidence/run-02/derived/filings/000858.SZ.json)、[原响应](../../../../research/industry-data-validation/evidence/run-02/raw/announcements/000858.SZ.json)、[已有页文本](../../../../research/industry-data-validation/evidence/run-02/derived/pages/000858.SZ_2025.json)、[历史manifest](../../../../research/industry-data-validation/evidence/run-02/manifest.json)。只引用路径/hash，不复制旧原件进本计划、不把历史取得时间改成现在。

### 已知S0阻塞线索

- 同主体摘要seed `1225273090` 与全文同一归档时间，`announcementType`都为 `01010503||010112||010301`，`columnId/pageColumn`也相同。现parser是字段值精确allowlist：若把这个值映为全文，会误收摘要。不能从标题“摘要”反向猜正文分类，也不能把拆token后的“年报”分类当全文。须有可核官方语义支持的区分字段/精确过滤合同；找不到则保持`classification_unknown`，阻断完整profile和正式S1 poll。
- 旧响应中的`hasMore=false`、2条记录与`totalpages=0`不能直接拼成页协议；一次同timestamp的公告号递减不证明稳定tie顺序。旧查询带keyword，而P4实际query是`searchkey=''`、`category=''`、`pageSize=20`；旧响应不证明相同查询合同。现请求只声明time降序，没有次排序参数。
- [旧annual-pilot结果](../../../../research/industry-data-validation/evidence/run-02/results/cninfo-annual-pilot.json)记录`count=0, announcements=null`；它不是P4成功空窗，当前parser也不接受null为合法空列表。
- [2026-10-11历史权利矩阵](../../../../research/industry-data-validation/evidence/run-02/rights-matrix.json)记录自动获取、长期保存/再分发许可未确认；归档服务页只有JS壳。该记录不是现行条款核验或许可，不能从公开可下载推导权限。

同主体摘要只作S4候选；另一离线对照为`000568.SZ`/`gssz0000568`全文`1225240660`与摘要`1225240659`（[归档](../../../../research/industry-data-validation/evidence/run-02/raw/announcements/000568.SZ.json)也有同类类型歧义）。**000568不加入首轮grant**；若要实时查询/下载反例，须另列精确许可，不因“反例”就扩主体。没有已定位的000858真实更正/同号历史版本，也没有已核实的空窗日期；这些不编造。

## 3. 执行顺序与各次授权

| 步骤 | 前提、动作与退出条件 | 产物及当前状态 |
| --- | --- | --- |
| 08-P0 离线工程准备 | 后续单独获工程实施授权；只临时DB/可控HTTP。实现raw probe权限边界、可信profile注册、campaign累计帽、manual-only grant/runner、精确recovery-only。先过第8节工程复测，才具备真实执行入口 | 已获后续独立授权并实施；源码/测试/版本/hash与最终结果见[08-P0验收](stage-b-p0-acceptance.md) |
| S0-T 条款核对 | 人明确批准最多2个精确官方条款/说明URL及只读/临时保存动作、执行绝对时段。只取得这些页面/人工提供的有来源文件；不是目录/查询/PDF授权。不清楚的条款/JS壳记缺口，不自行登录/联系网站/换数据源 | 逐用途结论：有限探测、原件保存/备份、界面查看、再分发、外部模型；缺项禁止对应动作。当前未授权/未执行 |
| S0-P 协议观察 | S0-T确认此次探测用途可执行，用户另明确批准目录、W0查询、1个已列明PDF及额度。仅one-shot raw捕获，不入正常source coverage、Original/Evidence/Event、P4变化fanout或V3 final | 原始请求/响应及审计manifest，profile仍unvalidated。当前未授权/未执行 |
| S0-R 独立评审 | 逐项审第4节；执行者与最终人工来源评审人不同。只观察一致不能代替可核来源合同；异议、分类/排序/末页不明则partial/blocked | 人工逐项decision、依据、日期、protocol/contract/adapter hash；不自动授后续采集 |
| 08-P1 精确profile装载 | 仅在S0所需项通过后，将唯一签核hash登记到隔离环境的服务端只读注册；默认无profile。装载的是批准内容及批准记录hash，不是调用者给`validated=true`。脱机重放与反例通过；无新网络 | 非测试入口、`_testing=False`；只能认可所签scope，不部署生产。真实profile当前未装载/未执行 |
| 08-S 首批手动样本 | 独立样本grant与campaign余量获批准，环境清单/精确refs/窗口/profile核对通过。只执行批准的P1/P2及可选P3，不启动普通worker daemon/自动维护 | S1—5逐栏manifest与独立人工复核；当前未授权/未执行 |
| 08-R 收尾 | 停止新受理，结算/保留unknown，撤销样本grant并终结campaign，冻结临时库/证据与备份，记录无活跃采集者和未完成项。暂停/归档不删旧声明或清风险 | 未来`stage-b-source-acceptance.md`，按子门禁签收；当前不创建假验收记录 |

授权票必须分别填绝对`valid_from/expires_at`、执行者、独立来源评审人、暂停负责人、用途/保留期限、确切URL/主体/窗口、hash与数值额度；未填为blocked。建议S0有效期≤24h，样本≤72h；日期尚未批准，不从当前时间自动滚动授权。单一campaign从首个批准动作至终止最多7日，过期不自动续期；后续任何修订仍计原总账，不能换UUID重置额度。

独立门禁不要求反复索取同一许可：可在同一批准票中分栏预授明确条件、范围与额度，已明确批准且条件满足的动作不重复申请。未列动作、未填必填字段或未通过的前置仍不得执行；本方案通过本身不构成任何执行许可。

## 4. S0核验工作单

| 核验项 | 必留证据和通过条件 | 不足时 |
| --- | --- | --- |
| 用途/保存条件 | 版本化的第一手条款/合同/许可来源、取得日期/hash、适用主体/用途与期限；分别判断此次探测、原件临时留存/备份、私人查看、公开再分发、外部LLM。人工明确接受的许可范围，不由程序解析网页自动授权 | 缺许可不做对应数据动作。可在单独授权范围内只核条款；不发送询价/邮件，不扩大站点或借其他SDK绕过 |
| 身份 | directory原bytes/hash、唯一ticker→orgId，逐条secCode/orgId；PDF正文发行人/证券代码/报告年度交叉核对，名称变动留修订证据 | 歧义、跨issuer、重组/映射冲突停在quarantine，不能以旧seed纠正远端字段 |
| 文档类型 | 对全文、摘要、英文版、半年度/非目标及更正/补充逐一列真实字段、官方含义依据、unknown处理。复合type保留原串，不猜category，不默认类型全集完备 | 同值不能区分全文/摘要时该映射不通过；S0 PDF可辅助人工确认单件身份，不能解锁全窗口分类或verified |
| 时间 | 原始值/单位/时区/真实精度与含义、公布/修订/取得/可用/提交分别记录；核W0两端及上海日界。编码单位为ms不证明公布真值精确到ms | 不用采集时间补发布时间，不用timer/cutoff补窗口；源时间语义与现DTO不兼容则先记录工程缺口另审 |
| 页与顺序 | 原query bytes（stock/window/searchkey/category/sort/pageSize/pageNum）、每页原响应/hash、跨页native顺序、同timestamp tie、total字段/hasMore/终止语义及其第一手依据。首遍全页链后再走完整复核页链；20条/页，预算最多两页每遍 | 超两页/缺页/漂移/无法核次排序或末页即partial。只有一页则“多页边界未观察”，不能宣称已测。不得通过临时改pageSize=1或标题过滤把另一查询冒充正常20条协议 |
| 原件取得 | 精确原公告ID、索引locator、每hop URL/状态/安全headers/hash/长度/MIME，TLS/public DNS校验；最终PDF签名/可读性/主体；目录/索引与正文分别计量 | 不自动换HTML/代理/cookie/browser。跨白名单、非PDF、超过16MiB、hash/引用损坏即停；404/索引缺席不是撤回证据 |
| 覆盖结论 | 区分“该窗口在这两遍观察一致”与“有可核依据的该范围页协议”。S0从不产V3正向proof；只签明确范围的profile语义，仍不承诺全站/全网无遗漏 | 无法确认分类/排序/末页时只保留raw observation或单件身份的partial，live profile不注册；S1正常poll被阻断，不能降标准凑样本 |

S0-T的候选精确URL为`https://www.cninfo.com.cn/`与`https://webapi.cninfo.com.cn/`；仅据本地归档列为待批准候选，不确认当前可用或足以解释许可。历史后者只有JS壳，不能作为许可文本；旧`/new/disclaimer`为404，不安排盲目重试。若人工已提供适用的正式许可文件，可不消费这两个网络槽位。需要换条款URL时先将新的精确URL及依据列入修订票，不自动跟随页面链接、脚本或扩到数据API。

四个query槽位仅用于W0同一固定查询的两遍，每遍最多2页；实际只有1页时剩余槽位不改作搜更正、空窗或另一主体。若预算不足以形成所需证据，结束本次S0为未过，另行提交明确修订的方案/许可。**S0的成功标准允许得出“当前来源不满足本方案”，不是保证能够启用。**

## 5. 申请配额与累计规则

以下全部是推荐申请上限，未创建grant。1 MiB=1,048,576 bytes。实际预算取`min(批准帽、代码硬帽、剩余campaign/日/poll/存储额度)`，来源要求更慢/更少时从严。保留最坏响应和元数据空间后才sent，不保证上限以内的全部动作均能完成。

| 层级 | 推荐值 | 准确含义 |
| --- | --- | --- |
| campaign整体 | 1主体、≤7日；**32次实际HTTP、192MiB响应、512MiB逻辑留存**；单活跃采集者 | S0、失败、redirect、重取、全部样本、跨日/新grant/revision/resume/replacement共同累计。08-P0已实现该总帽，实际契约见工程冻结。不能以新campaign/可写备份克隆回补原许可 |
| S0子帽 | ≤24h、8 HTTP、40MiB响应、128MiB逻辑留存 | 包含S0-T与S0-P，不是各自8次。最多2个精确条款URL各1MiB、目录1×8MiB、query≤4×2MiB、固定PDF1×16MiB，名义最坏合计34MiB；redirect也占8次，因而可能缺后续槽位。没有自动重试；缺许可时PDF/查询槽位为0 |
| 样本阶段 | ≤72h、最多3个明确人工poll操作、样本子帽24 HTTP / 152MiB响应 | 152=192−S0最高40，避免暗加成232MiB。即使S0少用也不自动提高样本子帽；保留更严总帽。只有获批的2个poll可先执行，第三个为未授权保留名额，不是可自由支配查询 |
| 每上海自然日 | P4全owner共享日桶与样本grant日桶分别≤24 HTTP / 128MiB | S0、样本沿原owner日桶累计；跨日更新日桶，不回补campaign。非标准S0条款purpose也纳入同一站点审计总账，不能在外部脚本漏计 |
| 每样本poll | ≤8 HTTP、query≤4、fetch≤3、响应≤64MiB | 含第二遍、目录、redirect和retry。三次64MiB不是保证可用192MiB，样本总帽152可能使后续poll提前停；不为完成样本自动提额 |
| 频率/并发 | 新poll间隔≥12h；每hop≥10s或S0更严格约束；网络并发1 | 现硬下限分别6h/5s；建议更慢不是官网许可。无周期自动排队；等待Retry-After时yield，不持网络token或长事务 |
| 存储/磁盘 | campaign≤512MiB含S0≤128MiB；实际可用磁盘≥1GiB | raw目录/页/条款/PDF、阶段账、元数据、重复归档引用均保守归属；不能仅算唯一blob。每hop预留max_response+最多1MiB元数据；不够则不发送，备份物理空间另核，不删被引用资料 |
| 非来源能力 | 研究/模型/外发/交易预算0 | 全新临时库没有case/Mandate/provider key/外发终点；agent/background/external及自动交易相关能力关闭。不是在有既有授权的生产库里宣称“来源免费所以总费用0” |

工程原有硬帽仍为owner-source日64 HTTP/256MiB、poll32 HTTP/64MiB、原件16MiB、存储512MiB；硬帽不是批准额。S0耗满8/40后，campaign最多剩24/152；未用保留不构成追加许可。每hop错误body、redirect、重复页/原件、unknown均计量。known result结算实际body并释放未用预留；sent未知消耗该hop最大预留bytes，不退款；可证未sent且旧lease已fenced才取消预留。S0 raw reserve与sent必须同上海日，跨午夜未sent停止并机械取消后重核许可，不自动迁移发送；跨日不回补campaign。样本沿native账规则，不把样本恢复套入raw。

## 6. 首批样本清单与不能混淆的结论

| 操作/样本 | 具体范围、核对与退出 | 当前/缺样时状态 |
| --- | --- | --- |
| P1 / S1正常 | 仅S0通过后，在已签profile下用W0、000858精确listing受理一次manual poll。必须由真实索引识别目标native ID，再取允许PDF；独立人工核发行人/报告年度/源时间/取得hash/精确refs。与seed hash不同先停复核，不覆盖旧版 | 未执行；S0分类阻塞未解除前不能跑。即使成功，Event/Evidence仍candidate |
| P2 / S2重复 | P1完成至少12h后，授权期限内，对同W0/同native重复一轮；计新HTTP与bytes，核相同hash时同文档/Original/Event revision与semantic_generation不增、无重复研究/通知；profile/basis变化则不称“健康重取” | 未执行；若自然出现不同hash，停为真实修订待核，不能伪造“重复成功” |
| 可选P3 / S3更正 | 先离线定位同issuer明确关联原报告的**具体**更正/补充ID、PDF关系页与绝对Wc，再单独批准授权修订/剩余额度后最多一poll。当前无具体样本/窗口，故P3不可执行，也不允许用第三轮遍历全历史寻找 | S-correction未验；同号两真实版本另列S-inplace-revision未验，不制造bytes或借本地替换冒充 |
| S4类型/主体反例 | 同主体摘要1225273090是候选，可能只需原响应+独立已授权材料核对；不能因为grant仅列annual_full就把同type摘要当已安全排除。另一issuer反例优先离线真实归档，在隔离重放中测试拒绝；标清“真实来源材料/离线重放”，不称实时链路已验 | live反例未执行。若需实时获取负例PDF或另一主体，先列精确URL/ID/用途和quota修订；现grant不授该请求，不能把它伪装成正常fetch |
| S5空窗/恢复 | 由授权后的真实索引+人工清单确定候选空窗We，固定绝对起止另行批准。必须合法schema/空列表/页终止/0 eligible，unknown分类不能算0。恢复以同root已保存进度实测；真实故障仅自然发生时记录 | 目前We未定位、未授权、未验。前三poll预算不预授空窗/新窗口；需要第四poll或更多材料先修订计划/许可，不自动扩总帽 |

第三个poll槽位在服务端不可变campaign动作清单中必须为`reserved/disabled`；仅有独立批准修订将该精确动作转为`approved`后才能受理。campaign还有余量、grant仍有效或`manual_only=true`均不能放行。批准修订绑定更正ID、Wc及原总账，不赋予查找样本的额外请求。

P3窗口若在W0之外，不因已有grant就可查。先批准明确Wc与候选native；必要grant history包络扩展不能授权runner访问包络中未列窗口，runner另核W0/Wc精确白名单。包络内未采集间隙保持history_gap，不能因更正单件取得而报累计历史完整；改变history_start/contract等仍按07 epoch规则，保留旧限制。没有来源提供的可信版本序列时A→B→A回现隔离，不擅自倒退头。

首批normal+repeat可形成**部分来源验收**，不能宣布AF-P4-08整体完成。S1、S2、S-correction、S-inplace-revision、S4类型、S4主体、S5空窗、S5恢复分别记录`pass/fail/not_observed/quarantined/not_executed`；必需子门禁非pass时整体保持未过。真实429/断连等未自然出现记not_observed；工程故障注入仍可离线完成，不对官网造故障。真实同号历史版如果确无可得样本，长期保持该能力未验/隔离，不换合成材料“完成08”。

## 7. 停止、恢复与备份

下表中的native lease、poll replacement与sample resume仅适用于已有样本poll。S0 raw没有native worker lease，且暂停后无采集resume；普通polling开关不挡raw。S0以[实际清单第7节](stage-b-s0-execution-plan.md#7-停止机械恢复与备份)为准，不由本表泛化出重新采集权限。

| 情况 | 立即动作 | 恢复所需证据 |
| --- | --- | --- |
| 无/撤销/到期许可、开关关闭、lease/hash/owner/listing/root/campaign不符 | 停新HTTP、桥接和新root；sent前0发送；sent后不能撤回在途，只存安全hash/实际用量，正文不成为可见资料 | 原操作、当前许可与scope/CAS、原lease终态或新合法lease、cap余量、同环境身份。过期不自动延长；撤权不自行恢复 |
| 条款/issuer/type/tie/末页/时间/URL/MIME/schema不明，raw/引用/hash损坏，旧hash回现 | quarantine或partial；S0不注册profile，样本不封完整final，不再探索别端点/主体 | 独立人工给精确新依据和修订范围；需要代码变化先另行离线工程审查，再取得新执行授权 |
| 429/5xx/超时/unknown | 结算原hop；首批runner停止并显示原因/Retry-After，不自动消费native max_attempts重试；不能假造成功、降低频率等待或换operation清次数 | 明确批准同campaign恢复，时间≥Retry-After/最小间隔，原attempt账核对、未知按上限计入，逻辑次数≤原上限。授权到期前无法等待则blocked |
| 日/阶段/campaign/poll额度、磁盘/存储/队列上限 | 停在持久续点，不删数据、不ACK越过未完项。一天到限不能跨日清campaign余量；grant修订也不清旧用量 | 同campaign及原总帽内、当前日可用、完整authority/ledger一致且有持久空间；需要提额须另行明确审查授权，不是“恢复” |
| 进程崩溃/失租、孤立hop | 旧worker不能写。精确recovery-only在native lease下结算可证未sent取消或sent unknown；核旧poll/attempt/hop后清旧attempt，保留logical计数；已知安全raw只重放业务，不重新HTTP | 原DB/WAL/收据完好、原root/窗口/grant/profile、checkpoint及partial候选增量可证；替换未完成poll需`replace_poll_id`+原因+不可变快照，继承旧用量/未决集合，不能缩窗清缺口 |
| 发现普通maintenance、自动due/deep、其他scope/poll、第二采集者或外部provider/消息任务 | fail closed终止runner、暂停campaign并保留审计；不以grant仍有效继续跑 | 定位隔离失败原因，经离线反例验证、独立复审和明确恢复授权 |

有限runner只用普通非测试handler和native heartbeat/lease，显式`run_once(run_id=...)`，每步核批准poll及其唯一后继step。`handlers=[明确handler]`可避普通worker全局维护，但不是充分授权边界；不得使用`run_worker()`的广域stale恢复，不得运行`poll_maintenance_v1`（它把orphan恢复、active扫描和due排队耦合）。08-P0必须提供绑定本campaign精确poll的recovery-only；不修改handler lease/candidate原子性来方便脚本。

全新临时DB/DATA_DIR、唯一路径和单写者绑定`sample_env_id/campaign_id`。保留原receipts/refs和hash；备份SQLite+WAL以SQLite一致快照完成，正文blob在DB内，额外证据文件及manifest另校hash。备份演练只离线，不运行复制库的联网worker。若从旧备份恢复且可能遗漏backup后sent/result，**禁止自动联网**；核对最高已知收据/外部捕获审计，缺失用量无法可靠重建则campaign终态unknown，未消费余额也不得自动重授。不得同时运行原库与可写克隆，或靠重建临时库/新grant逃累计帽。

## 8. 原08-P0工程任务依据与后续验收清单

本表保留08-P0实施前的任务依据，不能重复当待实现。实际16源码/6测试及落点以[08-P0验收](stage-b-p0-acceptance.md)为准；本轮发现的真实profile同campaign后绑定、raw暂停续采限制另见S0实际清单，不在本轮改代码。模块路径以`src/investment_agent/`为根。

| 工作/涉及文件（未来，非本轮修改） | 复用及新增边界 | 必须通过的验证 |
| --- | --- | --- |
| raw S0 probe | 拟窄`evidence/disclosure_probe.py`与服务端命令，复用`src/investment_agent/evidence/disclosure_http.py`的TLS/public-DNS/单hop限长，扩展的条款purpose只容最多2个经批准精确官方URL/redirect目标；不接受任意URL/浏览器/cookie | 受控HTTP中每hop reserve→sent→结果、强制cap、401/403/429/timeout/未知、坏MIME/URL/DNS。只存quarantine raw+audit，不能生成正常source覆盖、candidate或V3 |
| campaign与grant模式 | `src/investment_agent/supplemental_rules/disclosure_contracts.py`、`disclosure_authorization.py`及既有journal/source-state；新严格版本的08授权合同绑定campaign、manual_only、环境、绝对时段/精确动作；内部累计行authority receipt+CAS，复用owner日/poll账和租约，不另造预算平台/表 | S0→S1、跨日/新grant/revision、redirect/重试/unknown、并发reserve/重启/备份不能重置campaign；旧grant V1字节/hash不变，新版不识别则拒绝；新样本grant必须manual_only，scheduler不会枚举它；第三poll仍reserved时即便grant有效/总帽有余，受理与HTTP均为0，只有精确批准修订能解锁 |
| profile注册/装载 | `evidence/disclosure_pages.py`、`supplemental_rules/disclosure_updates.py`窄构造接缝与只读服务端registry；绑定批准记录hash、官方证据hash、protocol字段/分类策略、adapter build及URL policy/contract hash | API/env/model不能注册/置validated；改映射/证据/hash需新审批。未知/撤销profile零HTTP，当前读/prepare/publish/renew也同步检查registry批准状态，不能等下一poll才失效；for_tests、fixture proof不得冒live；raw S0不能注册自己 |
| 有限runner/recovery-only | `research/worker.py`现claim/heartbeat、`supplemental_rules/disclosure_updates.py`、`disclosure_schedule.py`精确mode guard；提取必要恢复函数，08服务端唯一入口不接受广域maintenance | 真实临时SQLite+可控HTTP验证manual root后继白名单、P1完成不自动排P2、maintenance/stale别scope拒绝、任一额度到限停止、逐检查点关闭/撤权/失租、正向重启及unknown保守结算 |
| 真实采集与验收 | 以后获准的隔离环境、S0 pack与`stage-b-source-acceptance.md`；原件和raw只进批准临时库/备份，不直接入Git或上传外部模型 | 独立人工核身份/页段/类型/时间/hash/关系，真实样本逐栏决定；DB+blob备份hash回读、1440/390诚实显示partial/未知/进度，无provider任务/外发/交易 |

profile的hash域须先冻结以免信任自签：`protocol_hash`取规范字段映射/策略（排除自身hash及validated派生标记）；`contract_hash`绑定source/feed、schema、protocol_hash、adapter build、URL policy、类型/时间/页序/覆盖语义；人工approval另以这些目标hash及用途/证据/评审人/时段生成不可变批准ref/hash，不能循环包含自己的hash。受理和每hop必须核grant.contract_hash==服务端已批准contract，修改profile/代码/URL策略时不能沿用旧合同hash。读取历史proof仍受原cutoff，当前认可还须注册批准有效；新批准不能把S0 raw/旧fixture或未验证资料追认为已验证来源，不推进旧任务知识窗。

08-P0先跑契约、授权/配额、HTTP、poll/恢复、V3/同步guard、API零DML、migration050—054及旧P2/P3/A兼容专项。改shared grant/worker/HTTP/coverage时按影响完整回归unit/integration/roots、五browser、Ruff/mypy/JS/迁移单head；若新增head，联查所有历史head断言。P0退出须有独立工程审查、source snapshot和原始日志；没过就不申请S0实际执行。不能为了真实源分类失败在live时临时改代码，改动先回到此工程门禁。

## 9. 执行产物与独立签核

未来每次执行包应有下面可审计的内容；本轮只定义格式，不生成“已采集”manifest。

- **授权/环境**：campaign/sample_env与不可变批准ref/hash；S0-T、S0-P、profile安装、每个sample poll各自许可；真实执行绝对时段；执行者/来源评审人/暂停负责人；源、精确listing/issuer/native/动作/窗口/用途/保留期限/URL allowlist；代码/adapter/profile/contract hash、源码head与054或后继实际临时head、runtime配置快照、无case/Mandate/provider/外发证据。
- **捕获/用量**：request canonical bytes/hash、page/pass/顺序、每hop permit/sent/result/unknown/cancel refs、原响应安全headers/final URL/body hash/长度、取得/可用/提交时刻；S0子帽、sample子帽、owner/grant/day/poll/campaign消耗和未决预留；不可变连续审计，不允许日志另存后漏算body或元数据。
- **样本**：原生公告ID/原type串/分类依据、原PDF hash/MIME/长度、PDF核对页码与文字依据、原件/证据/事件精确refs及previous；candidate、correction_lead、人工核关系依据分栏。人工关系评审不写`verified_correction_refs`或verification，当前真实关系持久化核验入口未实现，需要的话另审，不能直改数据库。
- **结论**：S0各项和S1—5各子门禁的status、能力范围、依据ref/hash/页码、执行者、具名独立人工评审人、reviewed_at、异议及处理、未观察/遗漏/未知原因、quarantine映射、允许与禁止动作；AI/子代理复核只能辅助，不能替代独立人工来源签核。
- **恢复/收尾**：停止点、原run/root/lease/attempt和checkpoint、retry_at/unknown/未完集合、备份seal/hash与最高收据、恢复批准及结果、终态grant/campaign、无残留采集进程。导出/分享仅限批准的脱敏metadata/允许摘录；整包PDF/raw/许可文件不默认进Git、外发或送模型。

S0通过仅支持**申请**该精确profile/用途的有限样本，不是sample执行批准；S1/S2通过仅是部分来源验收。S-correction/inplace/S4/S5仍分别守门；X公司暴露、Q真实研究质量与费用、T交易规则、N正式外发、P生产运行均未因此通过。P4不改变财务基础评分/Quality/DSL/账户硬约束，账户变化不污染公司研究，reduce/exit不因买入适配缺失被阻断，effective_cutoff只沿原可信collector回执；新事件和新修订仍限制旧结论，暂停/采集失败/开关关闭不解除旧问题。

## 10. 后续需要用户确定的事项

1. 选择先补同campaign真实profile后绑定等离线接缝（推荐），还是仅明确授权S0-only来源观察并接受后续样本blocked；当前没有S0执行许可。是否采用上述000858/FY2025/W0候选；S3真实更正及We空窗尚无精确样本，不能一并预授不确定范围。
2. 08-P0离线工程已获单独授权；后续仍须分别批准S0-T/S0-P的精确URL、用途/保存权限、实际绝对执行时段和上述数值额度。当前没有S0、真实profile装载或真实样本执行授权。
3. 具名来源评审人（与采集执行者不同）、许可核对责任人、暂停负责人及证据保留/导出范围。可由用户日后明确承担来源评审；本轮不默认指定或视为已签字。
4. normal+repeat之后是否追加更正/负例/空窗所需具体范围及预算；若来源合同本身无法可靠区分全文/摘要或确认分页，是否接受保持blocked、仅形成S0失败/部分记录。不得自动换源扩大平台以凑完成。

2026-10-17后续细化：S0-T/S0-P具体URL、192字节POST及hash、8个动作、互斥单/双页分包、具名责任/绝对时段/保留期限、42MiB最坏存储预留、raw已知错误与unknown结算及独立退出门禁见[实际清单](stage-b-s0-execution-plan.md)。初始八slot票必须明确条件性P范围；只签T不能先建approved P actions再期待后加。S0中真实分类/页序不明仍blocked；后续样本profile绑定缺口须另行工程，不能换campaign清账。

2026-10-17连续性工程计划接续：[同campaign后绑定与raw暂停受控恢复离线修复](stage-b-continuity-repair-plan.md)及[复审](stage-b-continuity-repair-review.md)已细化。拟严格CampaignV3原UUID提升、profile-ready/样本范围额度/lane/grant分权、原包raw suffix单次票与epoch、独立control锚；原sent锚与全部累计不重置。当前未实现，S0/样本/生产仍blocked；不因计划审查通过增加任何执行授权。
