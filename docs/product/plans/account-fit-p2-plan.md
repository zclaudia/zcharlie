# 类别及个股追加规则 P2 实施计划

日期：2026-10-16（Asia/Shanghai）。状态：用户授权的15项已实施并通过主代理工程验收；执行和最终证据见[P2交付索引](account-fit-p2/README.md)。依据[分层设计](../account-fit-and-supplemental-rules-design.md)及已提交的P0/P1 `42a0b309e78aeb3477ba79268ea5dfbc6023955c`。P0/P1交付、修复及验收见[交付索引](account-fit-p0-p1/README.md)和[最终复审记录](account-fit-p0-p1/followup-review-2026-10-16.md)。本文件最初只编制/审查计划；用户随后授权实现。本轮实现与临时库迁移验证见交付索引，没有生产迁移、提交/推送或启用生产开关；并行移动端改动原样保留。

计划阶段代码对照审查及修订见第9节；以下保留当时任务、依赖与验收要求，实际实现和最终测试证据以P2交付记录为准。

下文“新增/修改”表示任务责任；建议测试文件名可按实际职责合并，映射见execution.md。计划基线迁移head为050；本轮复核后新增051，保留已提交050不变。第2节“当前实情”记录开工前基线，并非实施后的现状。

## 1. 交付目标与阶段边界

P2提供独立的“补充事项”：用户可以创建、校验和批准类别/公司/个股规则，登记事件线索，按已保存资料进行确定性匹配与逐规则评估，查看来源、未知、冲突、历史未决项及当前有效性。首批评估主体沿P1的人民币A股listing范围；公司事件可共享，应用仍逐listing保存，不把公司影响混成证券交易规则。

| 本阶段实现 | 留给后续阶段 |
| --- | --- |
| 三种selector、三种rule_type、四种显式effects；多条追加规则并存 | 不占用旧candidate/holding主规则槽位，不改基础分、不叠加统一总分 |
| 定义版本、用户决策、事件修订、类别匹配、逐规则应用及覆盖展示 | P3的任务准入绑定、prepared/published rule_coverage、逐规则语义评审与正式发布门槛 |
| 保留事实/公司暴露/影响推断的区别；继承未决问题并检查时效 | 不因用户批准变verified，不自动解释监管原文或推断财务损失 |
| 有界本地重评、同操作恢复、当前限制和管理/单股页面 | P4的定时复核、外部采集监听、全网新闻覆盖、正式通知和生产启用 |
| 原P1账户检查、排序和历史结果继续独立工作 | 不修改P1的not_integrated/not_assessed、不把ready/limited接入账户排序，不改整案发布 |

P2工程完成不等于“追加规则已影响正式分析结论”。补充事项页面必须明确这一点，不能用一个绿色状态覆盖公司结论或账户适配。context支持主设计的company_research/candidate/account_fit/holding_review，但在P2只是独立评估视角；不因此读取账户、提名候选或调用持仓/Agent流程，页面默认company_research。

## 2. 已核对的代码接缝与必要增量

路径相对仓库根目录，表内源文件省略`src/investment_agent/`前缀。

| 接缝 | 当前实情 | P2接入决策 |
| --- | --- | --- |
| `rules/schema.py`、`validation.py` | 已有ConditionGroup、all/any、参数及因子校验；旧DSL使用float数值和原canonical_hash | 复用条件语言及注册信息，通过薄适配处理P2数值/单位；不更改旧DSL、hash及模板序列化 |
| `rules/screen.py::evaluate_zone_outcomes` | missing叶子按未命中处理；全not_applicable才返回None，不能当作通用未知传播 | 新增P2三值求值；不调用screen的私有函数或把miss翻译成“未触发、已安全” |
| `factors/registry.py`、`catalog.py` | FactorValue含float、quality、flags、inputs_ref，Factor另有unit及适用性 | 冻结因子版本、来源、单位和质量；比较前显式适配，缺失/冲突/不允许的approx不能判通过 |
| `factors/view_db.py::load_snapshot_view` | 按上海业务日日终读，部分分支允许backfill；不接受评估时刻cutoff | P2新增专用严格读适配，按真实datetime与captured_at读取；不把整个旧loader的结果事后过滤当作完整PIT修复 |
| `storage/models/pit_snapshot.py::IndustryClassification` | 分类行有system/code/区间/captured_at，没有不可变revision；valid_to会变化 | 冻结实际行及hash，不发明分类revision；不得把SW/CSRC代码或名称混用 |
| `evidence/store.py::EvidenceService`、`evidence/documents.py` | 能精确读回已保存证据和当前限制；公司/证券主体及来源有约束 | 只读回可信来源，不借新规则放宽旧证据校验；未知公布时间沿既有精度表达 |
| `evidence/events.py` | 适配业绩预告/披露日程/分部数据，输出candidate | 不是ResearchEventRevision，也不是政策verified入口，不能直接当禁令核验器 |
| `materials/contracts.py`、`storage/models/research_material.py` | 材料限受控company/listing及已注册资料类型 | 不把现有库冒充通用监管原件库；无法证明主体/原件/范围时保留线索与缺口 |
| `investor/publication.py::SemanticReview` | 绑定candidate/material/method hash，尚不绑定rule/application/解除范围 | P2不伪造解除评审；现有评审不能直接作为逐规则resolved许可 |
| `storage/models/investment_assessment.py` | kind仅account_fit/candidate_ranking；account_id非空；无series/sequence字段 | 必须显式迁移形状和索引，不能只加ObjectRef枚举或JSON discriminator |
| 同名assessment repository | 只解析两类P1 DTO，listing解析和账户约束有明确假设 | 增加rule_application专用严格分支，原fit/ranking读写与全集校验不放宽 |
| `portfolio/suitability_jobs.py` | 收据授权、kind白名单及批次DTO均是P1专用 | 复用协议及底层表，不直接扩充其白名单；P2有自己的run读取和manifest校验 |
| `api/routes/research.py::get_research_run` | 通用研究run详情不限制kind，直接返回request_json | P2独立run接口之外，还须封住通用详情旁路；不把完整冻结清单暴露给旧run页面 |
| `storage/repositories/pipeline_run.py::record_metadata` | metadata为顶层浅合并，不自动合并成员字典；队列request也不是不可变制品 | P2显式恢复并保存完整成员检查点，逐次核对清单/收据，不能把通用队列当可信原件库 |
| `InvestorMutation`、PipelineRun、JobHandler | 已有owner/UUID收据、队列、心跳、租约和重试 | 复用；规则变更、generation、审计、收据及本地重评入队在同一事务 |
| `api/auth.py::verify_api_key_or_session` | 验证operator API key或同源cookie，不证明请求来自真人 | 服务端构造决策上下文，不信任body.actor/approved；不向模型工具注册批准入口 |
| Settings/RuntimeSettings/SPECS、Web设置 | P1两开关默认false，无supplemental开关 | 新增默认false的supplemental_rules_enabled及镜像；P1默认和开关语义不改 |

上述“新增专用适配”必须有真实输入与缺失输出，不能交付空壳后用fixture的成功值替代生产路径。

## 3. 开工前固定的契约与边界

### 3.1 引用、状态与存储

P2仅新增UUID命名空间`supplemental_rule`、`research_event`、`rule_application`；新增的应用为UUID/revision=1，规则和事件沿稳定UUID追加revision。company_id仍为既有整数，不新增company ObjectRef。P3的`rule_coverage`与`research_rule_binding`本阶段不注册、不建表、不提供伪读取。

新增五表，复用既有assessment表。建议文件为`storage/models/supplemental_rules.py`、`storage/repositories/supplemental_rules.py`；事务编排归应用服务，repository不自行commit。

| 存储 | 必须落地的内容与约束 |
| --- | --- |
| supplemental_rule_revision | owner、UUID/revision、严格definition、content/semantic hash；不可变；内容不含生命周期状态 |
| supplemental_rule_head | owner/rule_id、active revision/state、latest draft revision/state、head_revision、最近校验定位；草案和active同时存在，计数器分开 |
| supplemental_rule_event | 每rule的有序append-only审计；校验结果、diagnostics、validation_hash、旧新状态与操作者；一次命令可产生多行，不把幂等键设为单行唯一 |
| supplemental_registry_head | 每owner的registry_revision、rules_generation/events_generation、语义探测信息；registry_revision覆盖所有有效规则/事件变更，专供并发及列表分页，不参与业务失效判断；GET缺行只返回零代视图 |
| research_event_revision | owner、event UUID/revision、previous_ref、事件业务键/别名、来源与核验/冲突/时间；不可变；不另建事件head表 |
| investment_assessment扩展 | 新增kind=rule_application，account_id允许该kind为空；新增仅该kind使用的series_key、application_sequence、application_mode。其他kind的新字段为空，原形状保持 |

rule_application要求listing非空、account为空、entry_count/manifest_hash/universe_hash为空；series_key、application_sequence、application_mode均显式非空，sequence≥1，mode仅live/historical_replay。company/context等在严格payload内，列/载荷身份一致。新增(owner,listing,series_key,application_sequence)唯一约束及live序列查询索引。旧account_fit/candidate_ranking仍要求原账户形状，原payload、input_hash、ordinal、引用和created_at不得回填或改写。候选entry父对象触发器继续只接受candidate_ranking。

SQLite重建assessment表时必须同时处理既有不可变触发器、entry外键和父对象保护，保留原数据及SQL约束；ORM create_all与迁移一致，不能以全局开启foreign_keys替代局部守卫。G1逐表列出SQL结构/父子身份守卫与服务层授权/载荷校验的责任，不把SQL约束当成完整业务授权；SQL CHECK须拒绝NULL绕过，P2不可变表须防REPLACE，head须核对引用属于同owner/规则。降级到050若已有任何P2定义、事件、审计、应用、P2 run或其收据应明确拒绝，包含零成员及failed run，不能静默删审计或留下旧代码无法解释的共享队列/收据。仅空初始化registry行可在无其他P2数据时删除；无P2业务数据的升级/降级/再升级应保持P1字节内容和行为。

### 3.2 定义与生命周期

- selector判别为listing/company/category；category只支持带来源的行业和显式产品/地区名单，以及最多两层all/any。名单覆盖不全时，“不在名单”不能证明not_applicable。
- rule_type为metric_condition/research_requirement/event_impact；effects仅annotate/require_evidence/require_review/limit_conclusion。禁止apply_constraint及任何写Mandate/账本/订单的字段或调用。
- head_revision是并发槽位版本，rule.revision是内容版本；新草案不挤掉active。approve只接受最新validated草案及其content_hash/validation_hash，批准时重新核对来源和时间。
- 校验业务失败保存诊断与收据、草案仍为draft；不是用户rejected。有效校验/失败校验均更新对应草案的最新校验定位，不能继续使用早先pass。
- validation_hash绑定精确rule_ref/content_hash、validator版本、依赖指纹、validated_at/有效期限及校验提交后的head_revision；批准核对最新校验事件及该head，不能复用其他版本、已失效或后续失败校验之前的pass。依赖指纹不包含本次写入会自行推进的registry_revision，避免校验刚提交就自失效。
- valid区间为[valid_from,valid_until)，review_due_at不得早于valid_from或晚于非空valid_until，review_due为派生限制。批准时必须已到valid_from且未到valid_until，review_due仍在未来；未来预约激活422。暂停、退役、到期、复核逾期均不关闭未决问题；恢复/续用创建新revision再校验批准。
- 草案、校验不推进生效规则generation；激活/替代/暂停/退役推进rules_generation。事件任何有效新revision推进events_generation，包括新event id和user_report；展示文字不冒充业务变化。

每条规则/事件命令成功提交只推进一次registry_revision；规则命令另推进一次head_revision，允许产生多行审计；审计sequence与定义revision/head_revision分别计数。显式评估、查询和收据重放不推进这些计数。新草案替代草案槽位并清除旧校验定位；approve清空草案槽位、原子替代active；reject清空草案槽位且只拒绝该草案。suspend/retire只作用于精确active_ref，保留该指针及状态用于历史/未决读取，不连带处理另一草案。重复目标状态的新key请求为409无效转换，同key则重放。

校验通过只说明定义可执行：合法的“尚缺运行时证据/分类/指标”可带诊断pass并在应用中unknown；错主体引用、非法DSL/单位、无效时间或不支持的效果属于校验fail。批准不能借此提升证据等级，也不要求先完成P3研究。

### 3.3 事件、证据与语义判断

公网HTTP只创建/修订user_report，不接受verification、verified_by或服务端actor等提权字段。相同事件追加revision，必须精确previous_ref及expected_event_revision；用户材料不能覆盖可信导入版本。无法确认同一事件则保持不同id，不按标题自动合并。

可信事件导入为内部适配服务：逐项读回允许的已存原件/证据，核对内容hash、定位、可用时间、主体及范围，再追加事件版本。source_ref存在、用户按了“批准”、材料自身自称verified均不构成证明。当前材料无法容纳/核验的通用监管原件保持candidate或user_report；本阶段不建设新监管采集平台、不伪造company/listing占位。默认缺可信适配器时如实返回pending/unsupported来源诊断。

规则event_ref是定义锚点；每次评估另选同event id在cutoff内最新可见revision，并冻结关系，同时探测主体相关的全新event id。没有规则承接的新相关事件在覆盖摘要中列为待核事项，不伪造rule/application引用，也不自动创建已批准规则。

事实已核验、公司暴露已核实、影响推断已评审分开表达。P2机械worker只能生成mechanical/pending或严格复用确有内容绑定的既存判断。当前SemanticReview没有逐规则解除绑定，首版不能据它输出reviewed/resolved、清空旧issue或授权正面公司结论；解除主张先显示为待评审，P3再接入完整评审。此限制须有负向测试，不能用仅测试可用的假评审读取器宣称P2已闭环。

### 3.4 冻结、时间与三值语义

live的as_of由接收端确定；冻结规则/事件内容及hash、分类实际行、company/listing映射、名单、因子版本/值/单位/quality/flags/来源、证据读回与缺口、前序应用/未决项、方法版本和generation。保存完整有界manifest，worker不重选latest。

接收只收listing/context、purpose及受控exact refs等用户意图，不接受客户端提供规则全集、因子结果、generation、前序sequence或manifest正文。live由服务端枚举全部适用候选规则及历史未决series并排序，冻结完整pair集合及扫描诊断；缺数据仍冻结unknown，非法exact引用拒绝。issue_review先核验原问题ref及其owner/listing/context、live身份，再绑定该series当前承接者，不能把回放产物当解除入口。

P2严格来源适配按datetime限定captured_at/available_from及实际取得时间，不使用旧loader的日终cutoff/backfill宽松分支。必要时从相同仓储以严格参数加载原行，再复用纯SnapshotView/因子算法；财务、市场、分类、分红覆盖和逾期标记均须检查时间，不能只过滤最终FactorValue。无法证明的输入标unknown，不修改旧因子读取默认值或黄金样本。

现有部分available_from为date，分类区间/逾期状态可变；各来源须记录时间精度与取得时刻，不能把date强转午夜冒充精确公布时间，不能声称重建了可变表任意过去时点。首版历史回放只复用已接收的冻结清单。规则/事件知识可见性另核对服务端created_at/批准时间，不允许客户端回填acquired_at/available_from使后来登记的数据倒流。

复用旧ConditionGroup的all/any、操作符和参数概念，但通过P2数值适配保留Decimal阈值/单位；旧DSL把普通数字归一为float的canonical_hash不得用于P2金额/比例契约。适配规则与等价样本在P2-01冻结，不复制一套无限表达式语言，不修改旧模板。float来源保留精度与quality信息，不能仅转成字符串便宣称精确；临界值无法证实时unknown。

三值规则：all有确定false则false，否则有unknown则unknown；any有确定true则true，否则有unknown则unknown。先按因子适用性确定not_applicable，不能靠删除缺叶子使条件成立；全叶子不适用不算triggered。missing/conflicted/不符合质量或时效策略的叶子本身不能成为not_triggered证据；整体可由独立已知分支判定，但仍保留其他未知、不能推广为公司“无风险”。已知命中和其他未知可并存并完整展示。

历史回放只接受本功能旧run/manifest/input_as_of的精确绑定，不接受整包客户端“历史原件”。复用原manifest及缺口，不从今日补齐；输入模式与授权请求一致，不能通过忽略mode绕过摘要检查。产物保留input_as_of和今天的evaluated_at，回放不进入live当前结果或未决事项承接链。

### 3.5 应用序列、去重、覆盖和失效

series_key固定为(owner,listing,rule_id,context)的规范摘要，声明pair也按此身份唯一，rule.revision、issue_key和evaluation_purpose不另起series。issue_key仅标识载荷中的问题及展示去重；修改它不能切断旧未决链，旧问题保留原始身份。application_sequence在短写事务按该series递增，UUID由operation及冻结pair稳定派生。同操作重试先核对并复用原UUID/输入，再检查新写入需要的前序CAS；不能因为自己已经提交的结果推进了序列就拒绝恢复。

live应用承接接收时冻结的最新live前序。两个操作并发写同series时，后者若前序已变且尚无自身结果，返回明确的item binding conflict，不静默rebase或丢失新issue。历史回放可占用审计序号，但不成为live predecessor；序号无需连续，live查询须过滤application_mode，不能只看最大sequence。

coverage包含全部声明pair的applicable/not_applicable/unknown及受控执行失败占位；匹配未知不是执行异常。当前生效规则集合之外，还必须读到已暂停/退役/到期/退出匹配的历史未决应用。coverage模式保留这些精确open_issue refs；issue_review以原问题范围为依据，不能因当前selector不命中判问题消失。clear只表示无新问题，不删除已有open_issue；缺逐规则评审时维持unresolved/inconclusive。

同公司/事件/issue_key/维度只合并展示一次影响，同时保留全部类别及个股application refs、条件与冲突；不同措施不按相似标题合并。去重不叠加severity成投资分数，不以个股例外覆盖类别风险，缺例外依据显示review_required。

P2覆盖摘要是run和应用集合的类型化读模型，不新增P3的rule_coverage制品或表。成功的空集合只表示声明范围没有生效规则；有历史未决项、新相关事件或扫描缺口时仍展示，不输出“无风险”。current-limits独立计算generation、主体semantic hash、来源/匹配变化及下一时间边界；无关全局变化可不使主体stale，扫描不足则显式coverage_pending，GET不写库或入队。

过期/暂停规则的“停止新要求”和“保留旧问题”分开输出；当前not_applicable只约束新条件结果，不覆盖已有open_issue_refs。当前集合是同一读取快照下的逐series最新live结果；来自不同run/as_of的结果明确mixed，缺任一要求或有failed/pending成员都不能称为一次完整覆盖。

### 3.6 命令、收据与重评触发矩阵

复用InvestorMutation存储，但P2定义自己的判别式结果（rule_mutation/event_mutation/evaluation_admission），不是把P1 AdmissionReceipt硬套全部命令。request_hash覆盖版本化命令、路由目标UUID及完整typed body；目标只在path时也必须入hash。同key先比较hash，异命令/目标/正文409，匹配后才按对应结果类型读取；重放不重新校验、递增或入队，保留原accepted_at及exact refs。

| 命令 | 原子写入与返回 | 重评 |
| --- | --- | --- |
| 创建/修改草案、用户reject | 定义/头/审计（依命令）、registry_revision、收据；同步200/201返回exact head/revision，run_ref=null | 不创建空重评run |
| validations pass或fail | 最新校验事件/头/registry_revision/收据；同步200返回业务outcome和diagnostics | 不重评；fail不抛回滚事务的HTTP错误 |
| approve/suspend/retire | head/审计/rules_generation/registry_revision/收据与有界run一起提交；同步200返回精确变更及run_ref | 有，允许明确的零目标run |
| user_report创建/修订、可信事件导入 | 新事件revision/events_generation/registry_revision/收据与有界run一起提交 | 有，允许明确的零目标run |
| 显式live/replay评估 | 冻结manifest/PipelineRun/收据；202 | 本run，不额外递增规则或事件generation |

鉴权和严格schema失败不写收据；合法校验命令的业务fail则必须留下诊断收据。expected_head_revision只用于规则槽位；事件创建expected_event_revision=0，后续≥1并附previous_ref，评估不携带无意义的head CAS。09先实现与具体生命周期无关的事务内enqueue协议，10/11调用它，禁止admission反向导入lifecycle/events。

## 4. 任务顺序、依赖与门禁

| 顺序/编号 | 交付 | 依赖 | 完成门禁 |
| --- | --- | --- | --- |
| 1 / AF-P2-01 | 契约、来源就绪矩阵、独立样本及容量冻结 | 已提交P0/P1、主设计 | G0：形状/阶段/时间/权限无悬空决策 |
| 2 / AF-P2-02 | DTO、引用、默认关闭设置 | 01 | 旧JSON/hash及设置镜像保持 |
| 3 / AF-P2-03 | 五新表、assessment迁移及类型化仓储 | 02 | G1：050升级与P1存储兼容 |
| 4 / AF-P2-04 | 精确来源读回与冻结 | 02、03 | 不依赖采集、按时刻可重放 |
| 5 / AF-P2-05 | 类别/公司/个股三值匹配 | 04 | 匹配真/假/未知独立预期 |
| 6 / AF-P2-06 | 指标DSL适配及三值求值 | 04 | 与旧筛选器差异明确；不改旧引擎 |
| 7 / AF-P2-07 | 研究要求/事件影响、未决承接与去重 | 05、06 | 不把消息、未知或未评审解除判成功 |
| 8 / AF-P2-08 | 应用保存、序列、当前有效性和覆盖读模型 | 03—07 | G2：单主体精确评估/读取独立可用 |
| 9 / AF-P2-09 | 接收收据、冻结批次、worker与恢复 | 08 | 原子接收、租约、模式及内容绑定；提供10/11使用的事务内enqueue协议 |
| 10 / AF-P2-10 | 规则草案/校验/用户决策生命周期 | 03、04、09 | head/审计/generation/收据/重评原子提交 |
| 11 / AF-P2-11 | 事件线索/可信导入及修订 | 03、04、09 | 事件CAS/核验边界及重评原子提交 |
| 12 / AF-P2-12 | 受控HTTP、独立run查询及错误映射 | 09—11 | G3：真实HTTP完整路径 |
| 13 / AF-P2-13 | 规则及事件管理Web | 12 | 显式批准与同操作恢复 |
| 14 / AF-P2-14 | 单股补充事项与来源/未决展示 | 08、12、13的共享样式/入口整合 | 与旧公司/账户状态分栏 |
| 15 / AF-P2-15 | 集成验收、兼容复审及交接 | 01—14 | G4：工程验收，保持生产关闭 |

05与06可以在共同输入冻结后并行，10与11可在09共享事务协议确定后并行。14的只读渲染逻辑可与13并行开发，但样式/入口整合及浏览器验收须在13相应文件稳定后进行；不把共享文件并行修改当成独立交付。公共ObjectRef、assessment模型/repository、迁移、app/worker/settings接缝各指定一名整合责任人。08的G2通过内部服务和真实仓储验收，不提前开放绕过09收据的用户写入口。每项必须有实现与真实验证，不通过空模块或模拟成功绕过依赖；API/Web在G3前不开放半套变更链路。

## 5. 逐项实施工作与文件边界

以下未另标包前缀的新增Python模块位于`src/investment_agent/supplemental_rules/`；contracts保持纯数据，不能反向导入service/jobs/store。存储仍归storage，页面/HTTP仍归现有包，不另建通用规则平台。04的event_sources只读03仓储及既有证据，不导入11的事件写服务；共享assessment仓储只增加严格DTO分支，不导入P2 service/jobs。

### AF-P2-01：冻结P2契约、样本和资源边界

新增`docs/product/plans/account-fit-p2/p2-freeze.md`，以及`tests/fixtures/supplemental_rules_v1/expected.json`、`tests/support/supplemental_rules.py`。读取本计划第2节接缝，固定三类selector/definition、时间、错误、mode、序列、事件核验与P3边界。样本只依赖当前DTO或独立JSON，不提前import后续service。

起始工程上限拟为：每批100个listing、每页100项、每批2000个声明pair、manifest 4MiB、selector两层/显式名单2000成员；registry/event/历史issue扫描也必须冻结有限行数与overflow语义。指标条件另限非空all/any、总叶子数、嵌套深度、参数数与in列表长度，不能只限制selector却允许任意递归DSL。G0先用独立JSON的序列化体积及既有只读路径探针定下契约常量，不依赖尚未编写的P2 worker；G2/G3再用真实全链路压限，若不满足则退回修订G0并重跑受影响验收，不能临时静默截断。不能直接继承P1的4096检查数。显式评估超容量在入队前422；current-limits扫描不足返回限制；变更自动重评采用明确的有界主体集合和未覆盖计数，不影响generation的即时可见性。

扫描受上限限制而无法得知精确总量时，count显式unknown并给已知下界，不写0或虚构精确未覆盖数；不为计数增加无界扫描或新的反向索引平台。自动重评可有界选择并标明未覆盖，不能因任意大类别的潜在全集而阻止暂停/退役提交。

验收：下文P2样本全有独立expected，含最坏合法组合；可信事件/类别/指标来源逐项标“已有/需适配/不可用”，没有来源仍能形成诚实unknown。G0前不得留未定义的数值精度、权限或“稍后做P3评审才能跑P2”的循环。

### AF-P2-02：契约、引用与默认关闭配置

新增`contracts.py`、`canonical.py`；修改`investor/common.py`、`config.py`、`settings/registry.py`、`web/static/settings.js`。按3.6节区分规则槽位CAS、事件CAS和评估请求，不套用旧Mutation。用户body仅一个幂等UUID `idempotency_key`，接收回执的operation_id映射同一值，不再制造第二个key。生命周期状态与定义内容分离，定义hash不因批准变动；冻结P2判别式收据、检查点、分页和feature/context响应。

新增`tests/unit/test_supplemental_contracts.py`、`test_supplemental_hashes.py`，扩展现有ref序列化及`test_settings_registry.py`、`test_settings_store.py`、`test_settings_consumers.py`。验收：未知字段、任意代码/效果、错引用、NaN、无时区和非法状态组合拒绝；P1用途/估值绑定、旧DSL/计算/收据序列化不变；新开关及镜像默认false。

### AF-P2-03：存储、迁移与不可变保护

新增`storage/models/supplemental_rules.py`、`storage/repositories/supplemental_rules.py`及`alembic/versions/<next>_supplemental_rules.py`；修改`storage/models/__init__.py`、既有assessment模型和repository。落实3.1节的列、索引、唯一性、判别式DTO及SQL有效JSON对象约束。无独立依赖表、事件head表或规则应用head表。

新增`tests/unit/test_supplemental_store.py`、`tests/integration/test_migration_<next>.py`，扩展`test_investment_assessment_store.py`必要兼容断言。`test_migration_050.py`保留050→049历史链和旧保护断言，仅调整“当前head=050”假设；`tests/unit/test_material_release_audit.py`的临时库版本标记随新head更新，不重写050迁移。验收：临时file SQLite从050带真实fit/ranking/entries升级、fresh、无P2行降级往返；按G1守卫矩阵在foreign_keys=ON/OFF下直接SQL验证结构、父子owner/主体、NULL、重复revision/sequence、孤儿和不可变UPDATE/DELETE/REPLACE，授权/语义错误另从服务验；有P2数据（含仅run/收据）降级明确阻止；旧payload/hash/entry顺序逐字一致。升级/失败回滚后核对外键目标、索引、全部触发器及foreign_key_check；不改全局database.py事务策略，不承诺旧worker可处理新kind。

### AF-P2-04：精确输入与来源适配

新增`inputs.py`、`sources.py`、`event_sources.py`。复用现有只读仓储、EvidenceService和纯因子计算，不调用ensure、采集器、估值更新或模型。为P2构造严格cutoff读取，复用仓储若缺严格参数只作向后兼容的keyword增量，涉及`storage/repositories/financial_snapshot.py`、`market_snapshot.py`、`industry.py`、`dividend.py`、`report_overdue.py`时逐项证明必要，不改旧默认；必要的FinancialSnapshot原行读取归P2来源适配，不经旧loader的宽松分支；避免修改整个`factors/view_db.py`默认行为。

新增`tests/unit/test_supplemental_inputs.py`、`tests/integration/test_supplemental_source_boundaries.py`。验收：同日未来捕获/backfill不可偷看、未知发布时间不伪造、分类和公司映射实际内容冻结；源删除/篡改让旧结果stale而不改旧payload，历史缺manifest为422；DB/IO/程序错误不伪装unknown。可信导入支持的来源必须有真实读回反例，其余明确不可用。

### AF-P2-05：selector匹配

新增`matching.py`，通过冻结来源解析listing/company/category及浅层all/any；只使用明确system/版本语义的行业、受控名单和例外依据。匹配输出保留所有叶子依据和unknown，不以名称相似或自由文本猜业务类别。

新增`tests/unit/test_supplemental_matching.py`。验收：类别+个股同时命中、公司多listing、不同分类体系代码相同、名单非完整覆盖、缺来源/未来来源、无依据例外及退出匹配；相同输入结果/hash确定，不产生来源查询或副作用。

### AF-P2-06：指标条件三值求值

新增`metric_adapter.py`、`metric_evaluation.py`，复用旧ConditionGroup结构概念、操作符和FactorRegistry元数据，实施3.4节的Decimal/单位/质量策略；不调用私有_Checker/_evaluate，不写基础分或strength。

新增`tests/unit/test_supplemental_metrics.py`，复跑`test_rules_schema.py`、`test_rules_screen.py`、因子黄金样本。验收：all/any三值真值表、全不适用、空组/深度/叶子上限、参数范围/单位不符、约等值和阈值边界、stale/missing/conflicted/approx策略；missing不变未触发，原筛选输出/hash逐字段不变。FactorValue.quality现仅ok/missing/not_applicable/approx；conflicted和stale在P2输入诊断中派生，不向旧Quality新增枚举，不把行业缺失时的fallback误作已证实的不适用。

### AF-P2-07：研究要求、事件影响及未决事项

新增`evaluation.py`、`issues.py`；三类rule_type共用严格应用输出。research_requirement以保存证据与完成/反证条件评估；event_impact分开event_fact/company_exposure/impact_assessment。承接历史未决应用、按问题身份去重展示，并实现issue_review的范围与评审核验。

新增`tests/unit/test_supplemental_evaluation.py`、`test_supplemental_issues.py`。验收：用户消息不能verified、政策verified而暴露未知、两个公司影响不同、正反证冲突、类别/个股同事件不重复影响、不同措施不合并；暂停/到期/退出匹配/clear不清issue；旧SemanticReview不能授权逐规则解除，缺评审继续unresolved/inconclusive。不得依赖未实施的P3发布路径才能完成这些机械评估。

### AF-P2-08：应用服务、精确读取和current-limits

新增`service.py`、`current_limits.py`、`views.py`，调用专用仓储保存稳定UUID应用。实现3.5节序列/前序CAS、摘要验证、覆盖读模型和只读当前状态；批量加载一次共同registry及来源，不逐条重复扫描全部规则。只对P2范围提供限制，不修改P1fit/ranking DTO或公司RecommendationView。

新增`tests/unit/test_supplemental_service.py`、`tests/integration/test_supplemental_current_limits.py`。验收：错owner/kind在解析payload前拒绝，exact结果始终保留；首次新增规则/新event id/新revision能被发现，无关generation和改名不滥失效；边界到刻即stale且GET零写入；同操作先读保存结果，不拿重算的created_at比较旧payload；回放不能成为live前序；修订issue_key、purpose变化不另起链，并发前序冲突不静默承接；mixed集合和不完整覆盖明确标记。

### AF-P2-09：接收、worker、恢复与重评协议

新增`admission.py`、`jobs.py`、`run_views.py`；修改`research/worker.py`注册本地`supplemental_rule_evaluate` handler。用户评估与生命周期触发的有界重评共用该执行协议，不新增scheduler、任务表或失效outbox。

writer reservation从P2 registry自身的INSERT OR IGNORE/不推进计数的条件UPDATE开始，不为规则写操作依赖已有账户；事务第一条需写SQL，禁止先读业务快照后升级SQLite写锁。取得写者资格后先查owner/key原收据，再检查开关/CAS；关闭或冲突失败回滚初始化，不提交空registry行。重放不改变任何计数。纯本地有界freeze在此事务内读取一致来源，限制文件读回体积/耗时，纯评估交给worker；不在锁内做网络或LLM。

冻结manifest和run/收据一次提交；变更命令返回的run需绑定其原命令收据、冻结manifest hash及操作角色，不能把同一key重复写进InvestorMutation。09的enqueue只接受已由事务内服务构造的snapshot参数，不自行调用10/11。重评manifest表示变更提交后的规则/事件集合与generation；pause/retire仍枚举旧未决series。创建草案/校验/reject不创建run。

本地变更只选择已保存应用主体及selector明确主体的有界集合，freeze中保存处理范围、已知总数/下界及未覆盖计数的精度。新类别潜在其他主体由generation+current-limits标coverage_pending，不能宣称已经全市场重评。没有目标时可提交零成员run，不隐式扩张到研究全集或启动付费任务。

新增`tests/unit/test_supplemental_jobs.py`、`tests/integration/test_supplemental_recovery.py`，扩展`tests/unit/test_job_dispatch.py`。验收：收据请求摘要重算、manifest自身与收据绑定、mode一致、派生成员不吞重复；单项应用/检查点在同一lease写事务，最终汇总及终态同事务；失租约、SQLite锁、已提交后崩溃均可恢复且不重复制品。claim、成员间及最终写事务均复核运行时开关；关闭后以failed/writes_disabled终止后续写入，保留已提交应用和原收据，不伪造完整coverage。受控item失败与数据unknown分开，IO/程序错误全局失败/按既有策略重试。

每个pair以固定ordinal记录pending/saved/item_failed状态，受控失败也单独落lease检查点；恢复先读取并验证完整已保存成员，再跳过saved/item_failed，防止浅合并metadata覆盖旧成员或让既判失败的pair重跑后改变终态。保存结果与检查点分裂、错ref/hash、未知ordinal均拒绝继续；零pair仍保存未绑定事件/扫描范围摘要。success表示声明pair均保存，partial表示有终结item失败；failed时保留进度、未执行成员及原因，不把终态等同完整覆盖。关闭后允许写同lease的失败终态，禁止新增应用，避免“禁写”导致run永久running。

source_manifest_hash只标识冻结事实/规则/前序及方法版本；live/replay的执行envelope、原run/hash/input_as_of关系单独绑定收据和request_hash，不能靠删除mode再比较整包摘要。重放仅限原已授权清单及支持的方法版本；来源今天删除不阻止重放已完整冻结的内容，但标记当前限制，缺必需原件/方法版本不可用则422，不从今日补齐。失败终态后同key仅取回原run；再次执行需新key并显式选择新live或原清单replay，不能复活终态run。

### AF-P2-10：规则生命周期应用服务

新增`validation.py`、`lifecycle.py`，复用rules的公开注册数据和验证概念，不调用旧RuleStore审批或主规则槽位。实现草案、确定性校验、approve/suspend/retire/reject；校验结果写append-only事件，不新建validation表。时间过期通过查询派生，恢复仅新revision。

写事务按3.6节完成head CAS、生命周期多行审计、generation、InvestorMutation及需要时的09任务入队，任何基础设施失败整体回滚；业务校验fail按已接收结果提交诊断。批准服务需要服务端认证的operator决策上下文，不接受caller自报角色。P2不新增任何模型/MCP写工具；验收“模型不能批准”限于工具注册/调用通道没有该能力，现有API key无法区分持同一凭据的人与自动客户端，不虚构真人认证，也不新增通用权限系统。

新增`tests/unit/test_supplemental_lifecycle.py`、`tests/integration/test_supplemental_concurrency.py`。验收：两独立SQLite写者同head仅一个成功；失败校验留draft且可重放诊断，旧validated草案/错hash/过期validation拒绝；替代旧active与激活新active一次提交；按3.6矩阵核对run是否存在、代数与审计数，不能为草案操作无谓重评；相同key关闭后仍重放、异命令/目标/正文409；未来激活422；审批与事件核验互不授权。

### AF-P2-11：事件登记、可信导入与修订

新增`events.py`，调用04可信来源适配及09重评协议。公网user_report与内部受控导入分开；事件revision按同id在短写事务分配，previous_ref和expected_event_revision共同CAS，不建设新事件head。事件版本、events_generation、收据和重评入队原子提交。

新增`tests/unit/test_supplemental_events.py`、`tests/integration/test_supplemental_event_flow.py`。验收：客户端不能提升核验级别，用户报告不能覆盖可信原件；同事件并发修订一成功一409；晚取得版本不倒灌旧cutoff，未来政策不能标已生效；原件hash/主体/范围错误拒绝，真实适配缺失输出pending而非伪verified；新event id不能被旧rule.event_ref遮蔽。

### AF-P2-12：HTTP及运行批次读取

新增`api/routes/supplemental_rules.py`、`api/schemas/supplemental_rules.py`；修改`api/app.py`、`api/deps.py`注册与422映射，`api/routes/research.py`仅增加P2 run的旁路保护。复用现有API key/同源cookie+CSRF；决策上下文由服务端生成，不修改旧auth函数的返回契约。独立context读取返回功能状态、已存listing/company身份、允许的类型/因子元数据及来源能力；关闭时仍可读，不借用要求账户或创建对象的P1入口。

新增`tests/integration/test_api_supplemental_rules.py`、`test_api_research_events.py`、`test_api_supplemental_evaluations.py`，扩展现有research run HTTP专项。验收按第6节逐路由真实HTTP执行，静态路由在UUID动态路由之前；鉴权失败/错owner/关闭/历史错误均走明确映射。P1的`/investment-assessment-runs`仍仅认原两类job；通用`/research/runs`列表（含kind=all/显式P2 kind）排除P2 run，通用详情对P2返回404，完整清单仅留服务内，P2经专用受控视图读取。旧kind的详情/列表语义不改，不借此重构全局run权限或修其他历史接口。

### AF-P2-13：规则与事件管理页面

新增`web/static/supplemental_rules.js`、`web/static/pages/supplemental_rules.css`、`web/templates/supplemental_rules.html`；修改`web/routes.py`、`web/static/app.js`、`web/templates/base.html`加入独立入口。提供结构化编辑、最新草案/active并列、校验诊断、精确版本批准确认、暂停/退役理由、历史及线索登记；不提供“手动勾选verified”。

新增`tests/integration/test_web_supplemental_rules.py`及`tests/browser/supplemental_rules.mjs`、`supplemental_rules_fixture_server.py`；DOM/状态机验证沿`tests/integration/test_web_account_fit.py`的Node驱动方式组织，公共client契约回归`test_web_client_contract.py`。验收：完整pending正文+稳定UUID及目标path先落会话存储，双击不双发；同步200/201变更和异步202分别处理，只有非空run_ref才轮询。丢响应时同key/原body/原path重发以取回回执，已有run时只读跟踪；提交前存储失败则不发送，接收后存储失败保留内存回执并显示恢复信息。迟到响应、主体切换、规则版本变化及ABA不覆盖新选择；401/403保留原命令，409/422要求显式刷新/更正，新意图用新key，不自动改body重试同key。使用现有api.js时传idempotency:false，避免它每次自动生成另一header key；不修改共享client或并行移动端传输。1440/390无溢出，关闭仍读历史，编辑不停止已接收任务跟踪。

### AF-P2-14：单股补充事项只读展示

新增`web/static/supplemental_items.js`，复用13样式；修改`web/templates/stock.html`、`web/static/app.js`，必要时扩展`web/routes.py`只读身份数据。展示已评估范围、分类/名单依据、事实/暴露/推断、冲突、旧未决项和实时限制；合并展示保留逐规则审计入口。

扩展Web/browser专项。验收：P1单股账户表单、账户排序和主规则操作不受影响；GET不评估/提名/采集；不同主体无残留，回放明显标历史，unknown不着色为通过；明确“尚未接正式结论/账户排序”，没有P3服务也可工作。新应用不能被旧公司NumericResolver当事实，也不能作为组合目标数量。

### AF-P2-15：兼容审查、工程验收与交接

新增`docs/product/plans/account-fit-p2/acceptance.md`、`review.md`和`docs/product/probes/account-fit-p2/`证据，更新交付导航。复核本计划所有文件接缝、依赖方向、P3边界和生产数据缺口。`tests/fixtures/import_graph_baseline.json`只在实际审阅每条新增边后更新，contracts不引入service/jobs循环，不借放宽门禁掩盖问题。

先跑P2专项和P1/旧主规则/证据/发布/迁移回归，再串行真实浏览器，最后静态与必要全量离线分区。按最终代码记录命令、原始日志/JUnit、收集清单、skip及先后时间，不累计重复用例，不把fixture等同生产来源。临时shell使用`task_test_exit`等非保留变量；暂存前检查新增文件的空白和提交范围，避免重现未跟踪文件漏检。

G4退出：五表及assessment扩展安全、类别/个股叠加与未知/冲突行为正确、生命周期/事件原子性及worker恢复有真实反例、旧结果和开关兼容、界面范围真实、P3依赖不被提前宣称完成。生产开关仍false；可信政策/业务暴露来源尚缺的，按能力明确受限，不阻塞诚实unknown的工程验收，也不以此批准启用。

## 6. P2接口与运行时权限矩阵

无额外`/api`前缀。同步生命周期返回精确head/revision、校验/审计定位及可空重评run_ref，状态码依3.6节；显式评估返回202收据。全部读取无副作用。

| 接口 | 输入/结果与阶段限定 |
| --- | --- |
| GET /supplemental-rule-context | 认证只读功能状态、允许context/selector/effect/因子元数据、已存主体及来源能力；无账户依赖，不ensure/采集 |
| GET /supplemental-rules；GET /supplemental-rules/{id}；GET /{id}/history | 筛选/固定cursor分页、exact revision及当前头/时间限制；无结果明确missing/404语义，不返回未授权载荷 |
| POST /supplemental-rules；POST /{id}/revisions | 幂等UUID、创建时expected_head_revision=0/后续CAS、严格新定义；仅draft |
| POST /{id}/validations | 精确rule_ref/content_hash、head CAS；返回pass/fail及diagnostics/validation_hash，失败不等于rejected |
| POST /{id}/decisions | 精确rule_ref/content_hash、head CAS、approve另含validation_hash；suspend/retire/reject有理由；服务端operator上下文 |
| POST /research-events；POST /research-events/{id}/revisions | 公网仅user_report；创建expected_event_revision=0，修订含previous_ref及expected_event_revision≥1，禁止提交核验等级及服务端取得时间 |
| GET /research-events；GET /research-events/{id} | 管理页的有界检索、精确历史revision、最新更正/冲突；不可自动抓取来源 |
| POST /supplemental-rule-evaluations | idempotency_key、mode、listing_refs、context、evaluation_purpose=coverage/issue_review及受控exact refs；完整规则/来源/前序由服务端枚举冻结，客户端不能缩小覆盖或提供结果；issue_review另含prior_application_refs/解除资料，缺有效评审不解除；replay绑定原run/manifest/input_as_of |
| GET /supplemental-rule-evaluations/runs/{run_id} | P2专用整数run查询，核对允许kind、owner、原命令收据与manifest；结果refs有界分页，不泄露raw request |
| GET /supplemental-rule-applications/{id} | 精确UUID/revision、原应用/前序/未决及current-limits，原payload不变 |
| GET /supplemental-rule-applications/current | listing/context下逐series的最新live集合、已保存批次coverage、旧未决和未绑定事件待核诊断；不把一次listing的最新单条当全部覆盖，不现场生成应用；在动态UUID路由前注册 |
| GET /supplemental-rule-coverages/{id} | 不在P2提供；P3才引入正式制品与发布绑定 |

规则/事件列表cursor绑定owner/筛选/稳定排序键/registry_revision，不能只绑定rules_generation，也不逐页扫描全库head来算hash；任意已提交变更使旧列表cursor明确409并刷新。历史审计cursor绑定rule/上界sequence。current集合cursor绑定listing/context及本次最新live集合hash，成员推进时409刷新，避免混读；该集合受01扫描上限约束。应用批次cursor绑定run/manifest/过滤，并按冻结pair ordinal稳定分页；运行中用pending/error占位覆盖声明pair，状态推进不改变pair顺序或删除成员，返回observed_at，不承诺不同请求的进度同一时刻；终态后成员结果稳定。全部分页只支持身份/ordinal等稳定过滤，首版不提供会随状态迁移的“只看失败”分页；客户端可对已取页本地筛选并注明范围。cursor形状坏为422、绑定不符/快照改变为409；最终原子终态之前不宣称完整完成。

`supplemental_rules_enabled=false`拒绝全部新写入/评估，返回403 writes_disabled；认证后的同key原body先重放收据，已保存对象和未决事项仍可读。P2独立本地管理/机械评估不要求打开Agent或账户账本开关；上位设计中“与组合/Agent取交集”仅在以后实际进入对应流程时应用，不为本阶段凭空启用它们。进入P3整案/Agent流程时再同时核验其原授权与开关。P2不新增付费授权或模型工具。

404用于不存在/错owner，409用于CAS/幂等/绑定冲突，422用于形状、时间、来源/历史manifest非法及显式请求超容量。新增错误逐一注册，不能落默认409；503只用于确实无法接收的临时服务故障。数据缺失是unknown，不是503。保留P1现有错误映射。

## 7. 样本与验收追踪

所有输入为明确合成离线数据或有原件的冻结资料；阳光电源仅作说明场景，不从本计划断言真实禁令或公司暴露已成立。

| 编号 | 独立情景/可判定结果 | 任务与设计验收 |
| --- | --- | --- |
| P2-F01 | 同股同时命中行业与个股；全部应用可读，展示去重，不改旧分数/槽位 | 05、07、14；SR01/SR05/CP01 |
| P2-F02 | 分类缺失、不同体系同code、名单非完整及例外无据；匹配unknown，不伪not_applicable | 04、05；SR01/TM01 |
| P2-F03 | missing/approx/conflict、all/any及全不适用、单位/临界阈值；未知传播和旧筛选语义各自保持 | 02、06；SR01/CP01 |
| P2-F04 | active与新draft并存、失败校验、旧validated批准、错/过期hash；active不被误替换 | 10；SR02/SR07 |
| P2-F05 | 两真实SQLite写者批准/事件修订；仅一成功，generation/审计/收据/run无半状态 | 09—11；SR02/AU03 |
| P2-F06 | review_due/valid_until到刻、暂停/退役/退出匹配；历史未决仍在，不能自动resolved | 07、08、10；SR03/SR06/TM01 |
| P2-F07 | 用户线索→事实核验→暴露未知；两个公司暴露不同、监管/公司回应冲突 | 04、07、11；SR04/AU01 |
| P2-F08 | 旧事件锚点后有新revision/全新event id；发现相关待核，无关事件不滥失效 | 04、08、11；SR04，PB05的P2读模型部分 |
| P2-F09 | 有解除材料但无逐规则内容绑定评审；继续待核，旧SemanticReview/回放不能消除live issue | 07、08、12；SR06，PB04正式闭合留P3 |
| P2-F10 | 下午接收但同日未来捕获/回填、未来政策、未知发布时间；历史不倒填 | 04、09、11；TM01，TM02准入/发布部分留P3 |
| P2-F11 | 同key重放/异命令、模式/manifest/派生pair篡改、历史缺原件；明确拒绝且无制品 | 09、12；AU02/AU03 |
| P2-F12 | 部分提交后崩溃、锁重试、最终失租、两个run同series、较晚回放；稳定复用且不漏issue/live链不串 | 08、09；SR02/SR06/MG01 |
| P2-F13 | 空规则但有旧issue/新未绑定事件；来源超cap、单项执行失败；不显示完整安全 | 01、07—09；SR01/AU02 |
| P2-F14 | 无P2业务数据升级/降级往返，有P2审计或仅有零成员/failed run及收据均拒绝降级；外键/触发器及旧payload/hash/entries不变 | 03、15；MG01/CP02 |
| P2-F15 | 模型工具通道无批准能力，operator API不虚称真人认证；公网不能verified/写Mandate；关闭原收据和历史仍可读 | 02、10—13；AU01/AU02 |
| P2-F16 | 真浏览器1440/390，同步200/201及202丢响应、context/终态GET迟到、ABA、编辑、存储失败、409和分页；主体/操作身份不串 | 13、14；UI01/AU03 |
| P2-F17 | 修改rule revision/issue_key、coverage切issue_review仍承接同series；旧问题保留原身份，mixed run/as_of不冒充完整批次 | 02、07—09；SR06/CP01 |
| P2-F18 | 3.6所有命令逐项核对响应/计数/审计/run；校验fail诊断提交；同key异path目标、与P1命令撞key均409且不误解析收据 | 02、09—12；SR02/SR07/AU03 |
| P2-F19 | 受控失败检查点已提交后崩溃、metadata浅合并、saved之后其他run推进、已失租旧worker；恢复不漏成员、不重试终结失败、不覆盖新issue | 08、09；SR02/MG01 |
| P2-F20 | 草案/校验/事件改动令列表cursor失效；current集合成员推进409；批次pair固定分页跨进度无漏项，终态稳定；不扫全库hash | 03、08、09、12；AU03 |
| P2-F21 | 通用research runs的all/显式P2 kind/详情无法读取P2清单；专用run和context读鉴权正确、零写入、关闭仍可用；旧kind保持 | 12—14；AU01/AU03/CP01 |
| P2-F22 | 空条件组、深嵌套、大in列表被有界拒绝；旧Quality枚举/DSL/hash不变；分类fallback未知不误NA，冲突只在P2派生 | 01、02、04—06；SR01/CP01 |
| P2-F23 | date精度、可变分类/逾期行与后来登记的旧日期事件；服务端取得/批准时间阻断倒流，不虚称可重建任意过去时点 | 04、10、11；TM01 |
| P2-F24 | replay envelope改mode/指向其他run、方法版本不支持、冻结原件完整但现源删除；按授权/冻结范围判断；failed同key不重启 | 04、08、09、12；AU02/AU03/MG01 |

P3专属的PB01—04、正式观点对新风险的限制、整案buy/reduce/exit门槛和研究优先级本阶段只记录接缝，不列“P2已通过”。P2 current-limits/coverage与P3正式rule_coverage必须分开验收。

## 8. 回归、交接与不实施清单

最低回归：旧rules schema/store/screen/drafting、factor golden/PIT、evidence/materials精确读回、publication与portfolio plan数值授权边界；P1所有contracts/inputs/calculations/jobs/store/HTTP/current-limits、050迁移、两类Web模块与恢复浏览器；settings/worker注册、导入图及新迁移门禁。共享assessment表或ObjectRef改动后必须完整复测P1，不只跑新P2测试。

验收环境沿项目`.venv`，每条测试显式临时`DATABASE_URL`与`DATA_DIR`、惰性凭据，不访问`.dev`或真实库。全量unit/integration/根目录测试分区分别记录最终代码时点；网关等可用的本地隔离依赖应实际执行，环境确实缺失才如实skip，不改断言凑数。部署/生产启用不是工程退出动作。

本段保留原计划阶段的授权边界：当时只交付本计划，不开启生产开关、不新增迁移文件或修改业务源码、不修改P1产物或排序、不研究或核验真实政策事件、不自动提交或推送。用户后续已单独授权P2实现、复审修复及2026-10-17的归档提交；实际交付与验收见[P2索引](account-fit-p2/README.md)。这些后续授权不包含推送、生产迁移、启用开关或推进P3/P4。

## 9. 本轮代码对照审查记录

2026-10-16。结论：原15项任务方向及五新表规模可以保留，但以下14组问题需要在实施前修正；本轮已修订计划，涉及跨文档契约的同时修订主设计。下表“闭合”指计划明确了责任和反例验收，不表示实现已经通过测试。

| 编号/级别 | 代码或设计依据与问题 | 本轮修订及验收 |
| --- | --- | --- |
| PR01 高：序列身份 | 主设计5.3与原计划把可改issue_key放入series；修订定义可能另起链，旧问题无人承接 | series固定owner/listing/rule/context，issue身份独立保存；同步主设计；F17 |
| PR02 高：生命周期与run | 原09/10要求变更一律入队，第6节又把所有同步结果写成必有run；草案/校验不改生效集合 | 3.2槽位转换、3.6触发矩阵；失败校验200提交诊断，无意义重评取消；F04/F18 |
| PR03 高：全局幂等身份 | InvestorMutation键只有owner/UUID，原hash未明确path目标及P1/P2不同结果类型 | 版本化command+目标+body hash，先比较后解析判别式收据；F11/F18 |
| PR04 中：列表分页成本 | 草案/校验不推进rules_generation；原有界head集合hash无法说明大集合跨页一致性 | 五表内加registry_revision并与业务generation分开，cursor变更409；不加快照表或全库扫描；F20 |
| PR05 高：恢复检查点 | PipelineRunRepository.record_metadata仅浅合并，原计划未定义失败成员落点和恢复跳过规则 | 完整typed成员状态、失败也落检查点、单项/检查点原子、重放先复用原产物；F12/F19 |
| PR06 高：冻结及回放 | 队列request可变；“精确输入清单”可能被解释为接受客户端自报规则/前序；mode摘要含混 | 服务端枚举全部pair、原收据绑定；source与执行envelope分层，方法版本可用性校验；F11/F24 |
| PR07 高：时间精度 | FinancialSnapshot.available_from是date，分类valid_to/逾期状态会变；仅按datetime过滤不足以重建历史 | 分源精度与server-created/approved约束，只回放已冻结历史；不新建通用双时间库；F10/F23 |
| PR08 高：迁移/降级 | 050共享assessment有父触发器；新kind的NULL形状及仅run/收据会越过原降级检查 | SQL/服务守卫矩阵、ON/OFF及重建完整性验证、P2队列/收据阻止降级；F14 |
| PR09 高：通用run旁路 | api/routes/research.py详情直接返回任意kind的request_json；只保护P2新接口不足 | 12窄改旧research路由排除P2，不重构旧kind；F21 |
| PR10 中：依赖/导入方向 | 10/11依赖09，但09若反向调用生命周期会形成环；14又复用13样式 | 09独立事务内enqueue，04只读仓储；14整合依赖13，G2不先开放裸写入口；F16及导入图 |
| PR11 中：前端恢复 | api.js默认每次自动生成header key；原计划只讨论丢202，遗漏同步命令和存储失败前后 | P2稳定body key+path、禁自动header key、200/201/202区分、提交前持久化失败不发送；F16/F18 |
| PR12 中：旧因子边界/资源 | FactorValue没有conflicted/stale质量；旧ConditionGroup结构允许空组，原只限制selector深度 | 冲突/时效在P2派生；空组/叶子/参数/列表均限量，不改Quality/旧DSL；F03/F22 |
| PR13 中：权限表述 | verify_api_key_or_session返回None，仅确认operator凭据，不能证明真人 | 区分工具通道禁止与operator权限，服务端上下文不靠body声明；不新增通用身份系统；F15 |
| PR14 中：只读覆盖及关闭 | 最新series集合可能混run/as_of；“禁止后续写入”若含失败结算会遗留running | mixed/partial/pending明确、批次分页固定pair，关闭仍允许同lease失败结算；F13/F17/F19/F20 |

文档验收仅核对代码接缝、任务依赖/共享文件、主设计一致性、本地链接及格式；本轮不运行P2业务测试，不创建fixture或迁移。后续G0仍须用实测冻结容量和来源能力；这属于明确的第一项交付，不是把未决实现假设藏进后续任务。
