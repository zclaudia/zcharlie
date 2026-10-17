# 正式研究及整案闭环 P3 实施计划

日期：2026-10-17（Asia/Shanghai）。状态：本文件最初只编制/审查计划；用户随后授权同轮实现与验收。P3十项任务已实施并通过主代理工程验收，实际文件边界、复审与验收证据见[交付记录](account-fit-p3/execution.md)、[主代理复审](account-fit-p3/review.md)、[验收记录](account-fit-p3/acceptance.md)与[冻结记录](account-fit-p3/p3-freeze.md)；生产开关保持false、无生产迁移、未提交或推送。计划基线描述（迁移head 051、P2未提交状态）为开工前实情，实现后head为052单head。依据[分层设计](../account-fit-and-supplemental-rules-design.md)第5.5/10节、[P2实施计划](account-fit-p2-plan.md)、[P2交付索引](account-fit-p2/README.md)与[后续全面复审](account-fit-p2/followup-review-2026-10-16.md)。并行移动端改动原样保留，不属于本任务文件责任。

下文“新增/修改”表示任务责任；计划阶段代码对照审查记录见第10节。第2节“当前实情”记录开工前基线（含未提交P2代码），并非实施后现状。P3完成后才称“追加规则影响正式分析结论的闭环”；P4定时监测、外部采集与通知不在本轮。

## 1. 交付目标与阶段边界

P3把P2的独立补充事项接入正式研究与整案：研究任务受理时绑定精确规则定义（不可变`research_rule_binding`），取证后形成不可变prepared rule_coverage，正式语义审查绑定该manifest并授权逐规则闭环，发布与问题关闭原子提交，发布成功追加published/commit coverage与正式依赖；新激活的重要规则、新事件UUID或新修订能限制旧结论，即使旧结论没有显式依赖它们。

| 本阶段实现 | 留给后续阶段 |
| --- | --- |
| 受理绑定、prepared/published coverage、逐规则语义评审与原子闭合 | P4定时复核、外部事件采集、正式通知扩展与生产启用 |
| PublicationDependency读路径、_reference/_dependency_limits规则分支、maintain/续期重核 | 条件式账户约束、通用反向依赖投影表 |
| 账户适配supplemental_binding=bound/unknown、ready/limited bucket、company_alignment组合 | research视图完整候选排序（本轮仅加research_urgency表达） |
| 整案buy/add发布重核fit与coverage；reduce/exit不被买入fit阻断 | 减持/退出专用fit方法（沿用既有公司论据与constraint例外路径） |

P3工程完成不等于生产启用或真实研究质量验收；所有开关保持默认false。三值诚实边界继承P2：缺少绑定或覆盖必须输出unknown/incomplete/legacy_unbound，不能当作“没有规则”；ready只表示所需检查通过，不代表买入建议；用户报告不因审核通过升级为verified fact；账户变化不影响公司研究本体。

## 2. 已核对的代码接缝与必要增量

路径相对仓库根目录，表内源文件省略`src/investment_agent/`前缀；行号以2026-10-17工作区为准。

| 接缝 | 当前实情 | P3接入决策 |
| --- | --- | --- |
| `agent/execution.py::admit` | 受理唯一入口（conversations/discovery/monitoring/plans四处调用），首次受理同事务执行`freeze_task`（L287），同task重admission永不重绑（WP11先例，注释与authoritative分支L272-284） | 绑定写入点：首次admit、research_kind∈{company_research,review}时，与freeze_task同事务写research_rule_binding；重admission/worker重试原样复用。pipeline型`POST /research/single-stock`（api/routes/research.py:249-328）只产研究快照不发布观点，不入绑定范围并如实记录 |
| `investor/methods.py::freeze_task` | 只冻结MethodBinding（L195-204），不冻结as_of与规则定义 | 不修改；规则绑定由新admission binder写入，as_of沿admit时点，研究窗口仍由observation可信回执推进 |
| `agent/research_publication.py::prepare` | 材料从claim refs构造（L117-149），`material_hash`=fingerprint(materials)（L167-169）；摘要>32份或>64000字节→review_blocked且summaries清空（L184-186）；回执含candidate_hash/material_set_hash（L209-211） | prepare同事务构建prepared coverage；规则/事件/摘要经固定适配器进入模型评审窗口并受同一32/64k预算约束；coverage语义摘要独立成coverage_manifest_hash，不混入evidence_set_hash |
| `agent/research_publication.py::reviewed_candidate` | SemanticReview的candidate_hash/material_set_hash/method_ref取自prepare回执而非模型输入（L244-248） | coverage_manifest_ref/coverage_manifest_hash与逐规则评审项同样由服务端从prepare回执填入，模型不能自报 |
| `investor/publication.py::check` | 语义绑定校验L227-228；directional+unresolved拒绝L239-240；critical claim源必须是critical依赖L268-271；依赖fields白名单仅mandate L262-266 | 新增coverage绑定校验与逐规则评审完整性校验；rule_coverage依赖fields白名单独立定义（rules/open_issues/materials） |
| `investor/publication.py::publish` | `_reserve`物理BEGIN+case CAS（L117-128）；参数全等幂等L364-366；cutoff预检L372-374；`_dependency_limits`失败注入check L393-396；失败落已提交draft L404-409；成功写依赖行L429-430、superseded/ published事件L431-435、case CAS L436-440、outbox L441；整条链是单事务（ToolAction settle保存点） | 闭环追加插在check通过后、case CAS之前：保存review→追加closure application→写published coverage→附加规则依赖行，全部同一保存点；check未过或异常天然保持问题未关闭 |
| `investor/publication.py::_reference/_dependency_limits` | kind分支白名单，未知kind在L175-176/L604-605直接拒绝；critical依赖仅evidence/calculation/mandate/method/monitor可过；续期另限{evidence,calculation}（L833段） | 新增rule_coverage分支：_reference校验published行存在且fields合法；_dependency_limits做有界重匹配（规则语义集合+事件可见性+时间边界）；续期路径解除对rule_coverage的硬限改为强制重核 |
| `storage/models/publication.py` | PublicationDependency仅3列（L29-33），全库只有写（publication.py:429-430）没有读；不可变仅ORM监听 | P3新增按recommendation_id的读路径（limits/maintain/scan共用helper），不建反向投影表；表结构与不可变级别不改 |
| `storage/models/investment_assessment.py` | kind CHECK限三类（L30-33）、形状约束L34-44、series互斥L45-50、uq(owner,listing,series,seq) L51-54、部分表达式索引L72-77、不可变触发器L135-164 | 新增kind=rule_coverage：listing非空、account为空、series三列为空；manifest_hash列承载coverage清单内容hash；entry_count承载申报规则pair数；payload内stage判别prepared/published；052重建表加新CHECK分支与task_id部分表达式索引 |
| `alembic/versions/051_supplemental_rules.py` | 手式重建assessment（非batch）；降级守卫`_p2_business_data_exists`硬编码3个result_kind封套字面量（L369-373），已冻结不可改 | 052沿用051手式模式：重建assessment、新建research_rule_binding（不可变触发器+task_id唯一）；052自带完整降级检测（rule_coverage行、binding行、含coverage依赖的发布产物），不依赖051识别P3数据 |
| `supplemental_rules/contracts.py` | RuleApplication.application_identity硬禁reviewed/语义结论/closure_refs（L669-674）；`EvaluationResult.no_false_resolution`要求resolved必须有reviewed+精确评审ref+closure且无open（L464-498）；semantic_status枚举已含reviewed（L454） | 新增`authorization: Literal["mechanical","formal_review"]="mechanical"`（缺省兼容旧行）：mechanical保留全部P2禁令；formal_review要求reviewed+精确semantic_review_ref+closure纪律且禁replay；save_pair/worker路径只构造mechanical，formal_review仅publish路径构造 |
| `supplemental_rules/admission.py`+`jobs.py` | reserve_and_replay先收据后开关（L131/153）；逐pair“应用+检查点”同事务（jobs.py:321-384）；恢复验证saved集合==库中集合（L461-553）；partial/success终态L395-414 | 任务内coverage评估复用P2评估协议（同handler、同检查点纪律）；prepared/published清单不新建run或检查点：分别挂在prepare/publish既有ToolAction事务内，幂等由ToolAction承担 |
| `supplemental_rules/current_limits.py` | application_limits按时间边界+重冻结scope hash判stale（L24-67）；不直接比较generation；全新event UUID只在主动评估时经unbound_events浮现（service.py:230-232,395）；scope digest已预计算（P2-R10修复） | 发布侧重核复用同一重冻结+scope digest原语，外包有界上限（沿用2000/4MiB）；溢出输出显式limitation而非“无变化”；不新增主动投影表 |
| `portfolio/suitability_contracts.py` | supplemental_binding硬编码not_integrated（L373）、research_status/company_alignment恒not_assessed（L374-375）、ranking_bucket仅incomplete/blocked（L376）、blocked↔bucket互斥校验L417-421 | Literal扩展bound/unknown与current/review_required/unavailable、supportive/restrictive/undetermined、ready/limited，全部保留旧值为合法且缺省不变（旧行JSON回读不炸）；新增组合校验 |
| `candidates/ranking.py` | Bucket仅两值（L22）、entry无研究优先级字段、`order_ranking_entries`不含coverage因素（L150-243）；research_kernel的suggestion_for（scoring.py:584-599）与candidates完全脱节 | entry新增rule_application_refs与research_urgency（只展示、不参与排序键）；FILTERS与前端筛选同步ready/limited；排序政策v1不变 |
| `portfolio/plans.py` | directional=readiness门对buy/add/reduce/exit一体适用（L329-335）；`_company`要求current+PUBLISHED+无limitation（L177-184）；constraint例外仅reduce/exit/wait（L294-310） | 拆分：buy/add在门内追加“fit当前有效+coverage限制检查”；reduce/exit保持既有constraint例外与公司论据路径，不新增买入fit要求；wait/further_research/no_action不受影响 |
| `investor/monitoring.py` | `verify_unchanged_review`只确认evidence/calculation依赖新鲜（L374-475，L455）；maintain=plans+scan+settle+outbox（L323-334） | 续期重核加入rule_coverage依赖：有限制即拒绝机械续期并给reason；scan的reason聚合纳入规则类limitation |
| 功能开关 | config.py:97-99与settings/registry.py:217-219/337-341三开关默认false；admission/jobs按收据→开关顺序检查 | 不新增开关。P3发布链写入与限制分支由`supplemental_rules_enabled`门控；fit/排序组合沿用各自开关取交集；关闭时全部P3行为与当前逐字节一致 |

## 3. 开工前固定的契约与边界

### 3.1 引用、对象与存储

新增UUID命名空间`rule_coverage`、`research_rule_binding`（设计第4节），注册进`investor/common.py`的ObjectRef kind Literal，并同步assessment仓储kind白名单。company_id仍为整数，不新增company ObjectRef；`MutationReceipt.result_kind`不扩展——P3三条新写入都不经InvestorMutation：绑定由admission事务保证（task唯一+input_key幂等先例），prepared/published挂在prepare/publish的ToolAction事务（参数全等+settle幂等），理由记入本节避免“双幂等账本”蔓延。

新表`research_rule_binding`（不可变，revision=1）：owner_id、task_id（唯一）、listing_id、research_kind、method_ref、admission_as_of、rules_generation、events_generation、冻结候选规则集合（rule_id/revision/semantic_hash/selector hash的有序列表+集合hash）、匹配依据引用（分类实际行hash等P2 matching manifest先例）、事件可见性上界、input_hash、content_hash、created_at。准入事务只做有界本地枚举与写入，不执行模型、网络或大范围匹配；空规则集是合法绑定（记录“当时无生效规则”），与legacy_unbound严格区分。

investment_assessment新增kind=rule_coverage形状：listing_id非空、account_id/series三列/universe_hash为空、manifest_hash非空（清单内容hash）、entry_count非空且≥0（申报规则pair数，含not_applicable/unknown）。payload为严格DTO：schema_version、stage（prepared/published）、task_ref、binding_ref、listing_ref、case_ref、effective_cutoff、逐规则条目（rule_ref、semantic_hash、application_ref或not_applicable依据或unknown原因或unbound_new标记）、承接open_issue refs、closure_proposals（每项含exact旧问题refs、证据refs、提案hash）、scan诊断（上限/溢出/未覆盖计数）、prepared完整内容hash。published另含prepared_ref、prepared清单hash、semantic_review_ref、已提交closure application refs、thesis_ref/recommendation_ref（发布重映射后的精确ref）、依赖行定位、发布时current-limits重查结果。同一任务可因新证据产生多份prepared（新UUID），发布绑定本次评审的精确一份；published不覆写prepared，也不回写绑定。

052迁移：手式重建investment_assessment（保留全部既有约束/触发器/索引/部分表达式索引，新增rule_coverage CHECK分支与(owner, json_extract(payload,'$.task_id')) WHERE kind='rule_coverage'部分索引）；新建research_rule_binding含不可变UPDATE/DELETE/REPLACE触发器与task_id唯一守卫；ORM create_all与迁移DDL一致。降级：有任意rule_coverage行、binding行、或引用rule_coverage的PublicationDependency/发布产物时明确拒绝；无P3业务数据时052↔051往返保持P2/P1字节内容不变。SQLite NULL互异使既有uq对rule_coverage不提供唯一性，prepared重打以新UUID为准，发布以精确ref绑定，不依赖该约束。

### 3.2 受理绑定与重试语义

admit()首次受理（research_kind∈{company_research,review}）且`supplemental_rules_enabled=true`时，同事务写binding：枚举对该listing/company/类别当前active+review_due规则（沿P2 matching的有界路径，含历史未决series的存在性摘要）、当前events_generation与可见事件上界；未知匹配显式保留unknown条目。开关关闭时不写binding（任务按legacy路径走，与当前行为一致）。同task重admission、worker重试、崩溃恢复一律读原binding，不重枚举、不追新规则；任务开始后新增关键规则由发布期当前检查覆盖（3.5），原绑定仅作历史审计。绑定失败（如来源越界）使受理失败，不静默降级为无绑定。

### 3.3 prepared coverage、逐规则评审与closure提案

研究流程在prepare前确保绑定范围的规则评估存在：若binding之后无该listing/company_research context的完成评估，则按P2评估协议入队一次（受控exact范围=绑定规则集合+历史未决series+可见事件），由既有worker执行；prepare只读已完成application，不现场评估。prepare同事务构建prepared manifest：binding_ref、effective_cutoff（取`observation.effective_cutoff(session, task_id)`，与发布cutoff预检同源）、逐规则application_refs或not_applicable/unknown/unbound_new、承接的旧未决、closure_proposals及清单hash。评估缺失/过期/超限不阻断prepare，但manifest如实记coverage=unknown/不完整；方向性结论能否发布由check决定（3.4），非方向性可带记录在案的unknown发布。

模型评审窗口：规则定义摘要、事件版本、逐规则结果与closure提案经固定适配器进入summaries，共享现有32份/64k预算，超限review_blocked（明确受限，不出“部分通过”）。`CompanyViewInput`新增`closure_proposals`缺省空tuple；`SemanticInput`/`SemanticReview`新增`rule_reviews`（逐项：application_ref、处置no_action_required|resolved_with_closure|limitation_acknowledged、basis refs、提案hash）、`coverage_manifest_ref`、`coverage_manifest_hash`，全部带缺省值保证旧工件JSON回读兼容。reviewed_candidate从prepare回执服务端填入coverage绑定字段；check()校验：manifest存在且stage=prepared、hash一致、binding_ref==任务绑定、逐规则项覆盖manifest中全部需评审条目（open issue/closure提案/critical效果）、提案hash匹配、directional时coverage完整且无未评审critical事项。无有效评审不闭合、不发布方向性结论；现有L239-240等门槛全部保留，不放宽。

### 3.4 发布事务与published manifest、正式依赖

publish的write()内，check通过后依次：保存review→对每项resolved_with_closure构造closure application（purpose=issue_review、authorization=formal_review、semantic_status=reviewed、semantic_review_ref=本次review ref、closure_refs精确覆盖序列、previous_application_ref=manifest记录的series当前head）→写published coverage→把critical `Dependency(ref=rule_coverage published行, fields=rules/open_issues/materials)`服务端附加进recommendation.dependency_refs→既有thesis/recommendation/依赖行/事件/case CAS/outbox提交。全部同一保存点；check未过或异常整体回滚，未决问题保持原状，不留“风险已解除但观点未发布”半状态。

closure提交时逐series重新核对：目标series当前live head必须仍等于manifest记录值；并发新增评估（如生命周期重评先行提交）使该series closure拒绝→check失败→落draft，问题未关闭。发布中当前限制检查（L393-396）只应用本次已评审、范围匹配的闭合，不屏蔽并发新增事件（新增事件由3.5限制路径承接）。

### 3.5 依赖解析、current-limits与maintain

`_reference`新增rule_coverage分支：行存在、owner匹配、stage=published、fields⊆{rules,open_issues,materials}。`_dependency_limits`新增critical分支：读published manifest→有界重冻结当前规则/事件可见集（P2 scope digest原语，2000/4MiB上限）→比对binding语义集合与manifest覆盖：新激活且主体相关的critical规则/事件UUID/新修订未被覆盖→`rule_coverage_pending:*`/`unbound_event:*`限制；时间边界（valid_until/review_due/check_by/事件生效区间）跨界→限制；扫描超限→显式`rule_coverage_scan_incomplete`，不得当作无变化。错误折叠沿用`dependency_unavailable`。该分支使新风险自动经get_recommendation L500-508变REVIEW_REQUIRED、经发布L393-396阻断，无需旧结论显式依赖该规则；旧event_ref锚点不能遮蔽新revision（评估侧已绑最新可见版本，重核比对事件可见性）。

maintain：`extend()`对rule_coverage依赖执行同一重核，有限制即拒绝机械续期并给reason（解除对非{evidence,calculation}的硬限，改为“必须可重核且无限制”）；`verify_unchanged_review`同步加入规则依赖检查；monitoring scan的reason聚合纳入规则类limitation。legacy结论（无coverage依赖）：发布侧不加规则限制（从不宣称已检查，不追认）；消费层（fit组合、单股页）按binding/coverage缺失输出`rule_binding_missing`/“未覆盖新增规则”标记，原hash/原JSON不回填。

### 3.6 账户适配、候选排序与整案衔接

fit组合（受`supplemental_rules_enabled`∧`account_fit_enabled`）：新评估的supplemental_binding=bound（附coverage/binding ref、rules/events generation、主体集合hash、application refs）或unknown（附原因）；`not_integrated`保留为合法值（旧行与关闭态）。research_status由published观点视图状态+coverage限制组合（current/REVIEW_REQUIRED/unavailable/无观点not_assessed）；company_alignment按动作从business_view+可核验条件保守生成（负面但有效的观点不产生supportive；current不等于正面）。ranking_bucket新增ready/limited：financial blocked→blocked；必要未知、研究非current、alignment未明确或coverage不完整→incomplete；financial warning/restrictive/非阻断限制→limited；其余全过且supportive→ready。ready文案“满足本次已检查条件”；blocked↔financial互斥校验保留并扩展ready/limited要求financial∈{pass,warning}。

候选排序entry新增rule_application_refs与research_urgency（critical未决/coverage待核的有界投影，来源P2应用与published manifest；只展示与筛选，不改变order_ranking_entries v1顺序，不叠加成分数）——研究优先级与配置影响分别表达。整案：buy/add在directional readiness门内追加两项——所用fit的current-limits仍有效、公司/coverage限制未触发（`_company`已拒绝REVIEW_REQUIRED视图，coverage限制经3.5自动生效）；reduce/exit/wait/further_research/no_action不新增买入fit或coverage前置，既有公司论据与constraint改善例外路径原样回归。整案仍不信任客户端缓存结果，最小单位pass不授权更大数量。

## 4. 任务顺序、依赖与门禁

| 顺序/编号 | 交付 | 依赖 | 完成门禁 |
| --- | --- | --- | --- |
| 1 / AF-P3-01 | 契约冻结：`docs/product/plans/account-fit-p3/p3-freeze.md`、fixtures与support扩展、coverage/绑定容量上限与预算分摊 | 本计划、P2冻结 | G0：形状/权限/预算/超限行为无悬空决策 |
| 2 / AF-P3-02 | DTO与ObjectRef：binding/coverage契约、RuleApplication.authorization、SemanticInput/Review扩展、closure_proposals、依赖fields白名单 | 01 | 旧JSON回读逐字段兼容；hash规范冻结 |
| 3 / AF-P3-03 | 052迁移与仓储：assessment新kind分支、binding表、降级守卫、索引 | 02 | G1：051升级/fresh/无P3数据降级往返；P1/P2数据与守卫原样 |
| 4 / AF-P3-04 | 受理绑定：admit钩子、有界枚举冻结、重试/重admission复用、legacy路径 | 03 | G2a：真实SQLite双路径（开关开/关）与幂等 |
| 5 / AF-P3-05 | coverage构建与评估衔接：prepare前评估入队、prepare内manifest构建、digest与预算适配器 | 03、04 | G2b：真实worker评估→prepare→manifest精确读取 |
| 6 / AF-P3-06 | 语义评审与发布闭环：check扩展、closure application、published manifest、依赖附加、失败语义 | 05 | G3a：真实ToolAction全链，发布失败保持未决 |
| 7 / AF-P3-07 | 依赖解析与限制：PublicationDependency读路径、_reference/_dependency_limits分支、extend/verify_unchanged_review、legacy标记 | 06 | G3b：新规则/事件限制旧结论真实反例 |
| 8 / AF-P3-08 | fit/排序衔接：binding扩展、bucket/alignment组合、research_urgency、reason codes | 07 | ready/limited组合真值表与旧值兼容 |
| 9 / AF-P3-09 | 整案门槛拆分：buy/add追加检查、reduce/exit不受阻断回归 | 07、08 | 整案验收样本逐字段回归+新门反例 |
| 10 / AF-P3-10 | HTTP/页面/验收交接：GET binding/coverage、单股页展示、浏览器、PB01-05与全量回归、交付文档 | 02—09 | G4：工程验收，生产保持关闭 |

05与06可在03后并行开发但共享prepare接缝，整合与验收以06为准；07/08/09依次依赖06的发布产物，不得用fixture假manifest替代真实发布链验收。公共接缝（ObjectRef、assessment模型/仓储、publish事务）指定单一整合责任人。API/页面在G3a前不开放半套读取。

## 5. 逐项实施工作与文件边界

新增Python模块位于`src/investment_agent/supplemental_rules/`（binding/coverage服务）与既有包窄接缝；contracts保持纯数据。存储归storage，HTTP归api，页面归web，不另建通用平台。

### AF-P3-01：冻结契约、样本与容量

新增`docs/product/plans/account-fit-p3/p3-freeze.md`、`tests/fixtures/research_closure_v1/expected.json`、扩展`tests/support/supplemental_rules.py`。冻结：binding/coverage DTO形状、stage语义、digest规范（canonical_json+sha256沿P2）、规则摘要进评审窗口的预算分摊（首期共享32/64k，超限review_blocked）、逐规则评审项覆盖判定、closure提案hash内容、容量上限（单manifest规则pair沿用2000/4MiB，溢出显式受限）、开关矩阵（三开关组合×任务新旧）。样本含：空规则集绑定、legacy_unbound、prepared多版本、发布成功/失败、新规则/新事件限制、reduce/exit不受阻断。

### AF-P3-02：DTO、ObjectRef与哈希

修改`supplemental_rules/contracts.py`（RuleCoverageManifest、ResearchRuleBinding DTO、ClosureProposal、RuleReviewItem、RuleApplication.authorization及formal_review校验）、`agent/research_publication.py`（SemanticInput.rule_reviews/coverage字段、CompanyViewInput.closure_proposals）、`investor/publication.py`（SemanticReview扩展、依赖fields白名单）、`investor/common.py`（两个新kind）。新增`tests/unit/test_supplemental_binding_contracts.py`，扩展P2契约测试。验收：缺省字段旧行回读不变；mechanical仍拒绝reviewed/closure（P2负例保持绿）；formal_review缺评审ref/replay模式拒绝；unknown code与错误映射沿P2。

### AF-P3-03：存储、052与仓储

修改`storage/models/investment_assessment.py`、`storage/repositories/investment_assessment.py`、新增`storage/models/research_binding.py`（或并入supplemental_rules模型文件，以052 DDL一致为准）、`storage/repositories/research_binding.py`；新增`alembic/versions/052_research_rule_binding.py`；修改`storage/models/__init__.py`。实现3.1节形状/索引/触发器与降级守卫。新增`tests/integration/test_migration_052.py`，扩展assessment仓储测试。验收：051真实数据升级、fresh、无P3数据降级往返字节一致；有P3数据（含仅published manifest、仅binding、含coverage依赖的发布）降级明确拒绝；外键ON/OFF直接SQL验证NULL/孤儿/UPDATE/DELETE/REPLACE；P1/P2 payload、hash、entries、检查点逐字不变；`test_migration_050/051`历史断言不改写。

### AF-P3-04：受理绑定服务

新增`supplemental_rules/binding.py`（枚举冻结+写入+读取）；修改`agent/execution.py`（admit钩子，保持调用方拥有事务的契约）。复用P2 matching/inputs的有界只读路径，不导入lifecycle/events写服务。新增`tests/unit/test_supplemental_binding.py`、`tests/integration/test_binding_admission.py`。验收：开关开→company_research/review任务有绑定且集合hash确定；开关关→无绑定且行为与当前一致；同input_key重admission与worker重试绑定ref不变；两条受理路径（agent/pipeline）行为符合3.2；绑定读取owner校验404语义。

### AF-P3-05：coverage构建与评估衔接

新增`supplemental_rules/coverage.py`（manifest构建、digest、读模型）；修改`agent/research_publication.py::prepare`（入队衔接+manifest写入+摘要适配器）、`supplemental_rules/admission.py`（允许task来源的受控评估受理，origin绑定任务）。新增`tests/unit/test_supplemental_coverage.py`、`tests/integration/test_coverage_prepare.py`。验收：评估完成后prepare产出prepared manifest且逐规则ref精确；评估缺失/超限→coverage如实unknown且摘要显式；同task多prepared互不覆写；manifest hash复算一致；32/64k超限review_blocked且不出部分通过；prepare幂等重放不产生新manifest。

### AF-P3-06：语义评审与发布闭环

修改`investor/publication.py`（check覆盖校验、write()闭环段、published manifest写入、依赖附加）、`agent/research_publication.py::reviewed_candidate`、`supplemental_rules/service.py`（formal_review构造入口）。新增`tests/integration/test_publication_closure.py`。验收：真实HTTP任务→prepare→评审→publish全链；评审缺逐规则项/提案hash不符→check fail落draft且未决不变；closure与published manifest与依赖行同事务；发布异常savepoint回滚后open_issue_refs不变；并发新评估使series head变化→该closure拒绝；发布时current-limits注入新限制→draft；non-directional带unknown coverage发布成功且manifest如实记录。

### AF-P3-07：依赖解析与maintain

修改`investor/publication.py`（_reference/_dependency_limits/extend、新增依赖读helper）、`investor/monitoring.py`（verify_unchanged_review/scan）；新增`supplemental_rules/publication_limits.py`（有界重核，复用scope digest，禁止逐成员重复hash——P2-R10教训）。新增`tests/integration/test_rule_coverage_limits.py`、`tests/unit/test_publication_dependency_reader.py`。验收：激活新critical规则→旧观点get_recommendation立即REVIEW_REQUIRED带精确原因；确认主体无关（确定性匹配）→限制解除且无研究入队；全新event UUID/新修订同样限制；旧event_ref不遮蔽新revision；扫描超限输出显式限制；extend带限制拒绝；legacy结论发布侧不受影响且消费层有标记；GET零写入回归。

### AF-P3-08：账户适配与候选排序衔接

修改`portfolio/suitability_contracts.py`、`portfolio/suitability.py`/`suitability_calculations.py`（组合逻辑）、`candidates/ranking.py`/`ranking_contracts.py`（bucket/research_urgency）、`web/static/app.js`等展示接缝与reason codes文案。新增`tests/unit/test_suitability_integration.py`、扩展ranking测试。验收：Literal旧值全兼容；bound要求精确coverage引用、unknown要求原因；ready仅当全部通过且supportive；published且avoid/sell不进ready；coverage未知→incomplete不冒充通过；research_urgency不改变排序顺序；三开关组合行为矩阵（含关闭时与当前逐字节一致）。

### AF-P3-09：整案与动作门槛

修改`portfolio/plans.py`（check拆分directional门、buy/add追加fit/coverage重核）。新增`tests/integration/test_plan_readiness_split.py`。验收：buy/add在fit过期/coverage限制时拒绝并给精确reason；fit缺失（旧评估）按unknown处理不虚构通过；reduce/exit在无买入fit时沿既有路径完成（constraint例外与公司论据回归逐字段保持）；wait/further_research/no_action不变；价格基线与publication_ready既有语义不放宽；账户变化不触发公司观点限制（只影响fit/整案）。

### AF-P3-10：HTTP、页面与验收交接

修改`api/routes/research.py`或新增受控GET（binding/coverage精确读取，静态路由先于动态注册）、`web/routes.py`、`web/templates/stock.html`、`web/static/supplemental_items.js`（绑定/覆盖/限制展示）；新增`tests/integration/test_api_research_closure.py`、`tests/browser/research_closure.mjs`；新增`docs/product/plans/account-fit-p3/acceptance.md`与`execution.md`。验收：GET只读且owner校验；run整数ID与产物UUID分开；通用research runs不泄露P3清单（沿P2旁路守卫回归）；真实浏览器1440/390覆盖绑定溯源、逐规则结果、REVIEW_REQUIRED原因、ready/limited分组、丢回执恢复与409；全量回归与静态门禁按P2标准记录，专项与全量不累加。

## 6. 接口与运行时权限矩阵

无新增写接口；新增均为认证只读。发布/闭合能力不经HTTP或模型工具暴露，沿用prepare→显式评审→受控settle链；`registrations()`不为P3新增模型可写工具，模型读取规则/覆盖经现有只读工具模式另按10节授权注册（不把内部写接口暴露给模型或MCP，MCP保持只读）。

| 接口/入口 | 行为与阶段限定 |
| --- | --- |
| GET /research-tasks/{task_id}/rule-binding（或沿现有任务读取扩展） | 精确绑定、规则集合hash、generation与未知匹配诊断；无绑定返回明确legacy_unbound而非空集 |
| GET /supplemental-rule-coverages/{id} | 精确coverage清单（prepared/published）、binding ref、逐规则覆盖与closure记录；不隐式重评估 |
| GET /supplemental-rule-coverages/current?listing= | 该主体最新published manifest与当前限制摘要；静态路由先注册 |
| 既有POST链 | binding/coverage/published全部由服务端在受理/prepare/publish事务内派生，客户端不能提交或改写 |
| RecommendationView / fit / ranking / 整案读取 | 按3.5-3.6返回限制原因；REVIEW_REQUIRED/limited/incomplete都携带可读reason code |

关闭态：全部P3新GET仍可读历史（无数据返回明确missing/legacy语义），POST链行为与当前一致；认证、CSRF、401/403/404/409/422映射沿P2，新增422 code显式注册。

## 7. 并发、恢复与兼容行为

- 重复提交：受理幂等由input_key/task唯一承担（重放返回原任务与绑定）；prepare/publish由ToolAction参数全等承担（换manifest/content为idempotency_conflict）；不新增InvestorMutation键。
- worker重试：评估沿P2检查点协议；发布闭环不经worker重试通道（ToolAction settle自身幂等），崩溃后重调publish重放已committed action结果，不二次写。
- 进程崩溃/部分持久化：发布链单事务+保存点，不存在“closure已提交、观点未发布”或反向中间态；prepared manifest随prepare事务原子落库；评估“应用+检查点”原子性沿P2-R12/R13纪律，任何新阶段不得拆开这两者。
- 并发规则/事件变化：受理后新规则不改变绑定（审计用）；发布/读取期由3.5重核承接；closure的series head CAS拒绝并发先行提交的评估覆盖。
- 开关切换：任务受理时的开关状态决定有无绑定，之后不回溯；已发布结论的限制检查读取不受开关影响（P2“关闭仍可读、不免除依赖检查”语义延续）；关闭期间新任务走legacy路径。
- 老任务/老结论/缺绑定缺覆盖：legacy_unbound与unknown/incomplete显式输出；不回填、不追认、不把缺失当“无规则”；`supplemental_binding=not_integrated`旧行保持合法。
- 元数据写法：PipelineRun metadata整键替换（浅合并规避），延续P2写法；read_snapshot只用于多读一致GET，不与writer reservation混用。

## 8. 样本与PB01—PB05验收追踪

所有输入为合成/冻结数据；不产生真实公司或政策的核验结论。专项与全量测试重叠，不累加宣称总数。

PB01→F01/F02/F13（受理绑定、重试不追新、legacy显式）；PB02→F06/F12（新规则限制旧结论、maintain拒绝机械续期）；PB03→F05/F08/F09/F10/F11（原子发布、发布期限制重查、财务分与账户隔离、负面不进ready、reduce/exit不受阻）；PB04→F03/F04/F05（评审hash绑定、无评审不闭合、发布失败保持未决、窗口超限受限）；PB05→F07（全新事件/新修订发现、旧锚点不遮蔽）。每项至少一条真实链路（HTTP/SQLite/worker/浏览器）反例，禁止仅fixture声明。

| 编号 | 场景与可判定结果 | 任务与设计验收 |
| --- | --- | --- |
| P3-F01 | 开关开受理：绑定冻结精确规则集合+未知匹配显式；开关关：行为与当前一致；重试/重admission绑定不变 | 04；PB01/AU02/CP01 |
| P3-F02 | 空规则集绑定≠legacy_unbound；缺绑定老任务标记明确；两者读取语义可区分 | 04、10；PB01/CP02 |
| P3-F03 | 评估完成→prepared manifest逐规则精确；评估缺失→unknown显式；多prepared互不覆写；发布绑定本次评审那份 | 05、06；PB04/TM02 |
| P3-F04 | 评审缺项/提案hash不符→check fail落draft；32/64k超限review_blocked；无有效评审不闭合不方向性发布 | 06；PB04/SR06遗留边界 |
| P3-F05 | 发布成功：closure+published manifest+依赖行+outbox同事务；失败/异常：全部回滚、open_issue不变、draft可修复 | 06；PB04/PB03 |
| P3-F06 | 激活新critical规则→旧观点立即REVIEW_REQUIRED（rule_coverage_pending）；无关规则确认后解除且零入队 | 07；PB02 |
| P3-F07 | 全新event UUID/新修订限制未依赖它的旧结论；旧event_ref锚点不遮蔽新revision；无关事件仅探测 | 07；PB05 |
| P3-F08 | 发布时当前限制重查：并发新限制注入→draft；已评审范围匹配的闭合才可应用 | 06、07；PB03 |
| P3-F09 | 事件更新公司观点而财务基础分逐字段不变；账户/估值变化只影响fit与整案，公司观点payload不变 | 06、08；PB03/DR03边界 |
| P3-F10 | fit组合真值表：blocked/ready/limited/incomplete×research_status×alignment；published且avoid/sell不进ready；coverage未知→incomplete | 08；PB03/RK04 |
| P3-F11 | buy/add：fit过期或coverage限制→拒绝；reduce/exit：无买入fit不被阻断，constraint例外与公司论据回归保持；wait/no_action不受影响 | 09；PB03/设计10.2 |
| P3-F12 | maintain/续期：带限制的rule_coverage拒绝机械延长并给reason；scan聚合规则类reason | 07；PB02 |
| P3-F13 | legacy结论：发布侧不加规则限制、消费层显示未覆盖标记、原hash/JSON不回填；历史重放不追认覆盖 | 07、08；PB01/CP02 |
| P3-F14 | 052迁移：051升级/fresh/降级往返；有P3数据拒绝降级；P1/P2数据字节不变；触发器/索引/外键完整 | 03；MG01/CP01 |
| P3-F15 | 权限与旁路：客户端不能提交binding/coverage；模型工具无批准/闭合通道；通用research runs不泄露P3清单；GET零写入 | 06、10；AU01/AU03 |
| P3-F16 | 真浏览器1440/390：绑定溯源、逐规则结果、REVIEW_REQUIRED原因、ready/limited分组、丢回执恢复、409/422、主体无残留 | 08、10；UI01/AU03 |
| P3-F17 | 性能与容量：限制重核每scope一次digest；扫描超限显式受限；manifest受2000/4MiB双上限；无逐成员重复hash | 05、07；P2-R10教训 |

## 9. 回归、最终验收与不实施清单

最低回归：P2全部专项（生命周期/事件/并发恢复/迁移051/Web/浏览器）、P1 fit/排序/迁移050、发布与监控既有链（publication/monitoring/ag23整案样本）、旧rules/factors/证据读回、settings与导入图。共享文件（ObjectRef、assessment模型/仓储、publish事务、plans.check、admit）改动后必须完整复测受影响两侧，不只跑新测试。

最终验收门槛：G0—G4全过；全量unit/integration/根目录三分区绿（数字以最终记录为准，不与专项累加）；Ruff、mypy全量源文件、JS语法、`git diff --check`通过；052单head；真实浏览器1440/390通过；PB01—PB05各有真实链路反例（HTTP/SQLite/worker/浏览器至少其一，禁止仅fixture声明）；生产开关保持false、无生产迁移。测试环境沿项目惯例：临时DATABASE_URL/DATA_DIR、惰性凭据，不访问`.dev`与真实库。

明确不做：P4定时复核/外部采集/通知扩展；生产启用与真实交易规则供应；research视图完整候选排序；条件式账户约束与预约规则激活；通用Issue平台与反向依赖投影表；回填老任务绑定；新增功能开关或InvestorMutation result_kind；修改P1/P2既有验收记录与迁移文件；修改全局SQLite事务策略与旧Quality/DSL/黄金样本；对真实公司/政策给出新结论。

## 10. 本轮计划审查记录

2026-10-17。三个子代理分项检查数据/接口、生命周期/并发恢复、发布/账户排序兼容，主代理逐接缝交叉核对（research_publication.py、publication.py check/publish/_reference/_dependency_limits、investment_assessment模型、suitability_contracts、plans.check、admit等均亲自复读确认）。计划已据以下核实修正：

| 编号 | 核实结论与计划修正 |
| --- | --- |
| R1 | PublicationDependency只有写没有读路径（全库唯一写入publication.py:429-430）——P3把“依赖读路径”列为07的显式交付，而非假想已存在 |
| R2 | `_reference`/`_dependency_limits`未知kind直接拒绝且只在publication.py——不新增ObjectRef就算完成是假象；07必须加分支，且计划明示续期硬限{evidence,calculation}需解除 |
| R3 | ranking_bucket只有incomplete/blocked，research_status/company_alignment恒not_assessed且无组合逻辑——“负面有效结论不进ready”没有现成挂点；08按设计5.4新建组合与Literal扩展，旧值保合法 |
| R4 | plans.py的directional readiness门对buy/add/reduce/exit一体适用（L329-335）——09拆分门槛并加“reduce/exit不被新增阻断”回归，防止P3把阻断扩大化 |
| R5 | freeze_task只冻结方法不冻结规则/as_of；正式受理有agent与pipeline两条路径——04把绑定收敛在admit()（覆盖全部可发布调用方），pipeline路径如实记录为不在范围 |
| R6 | publish已是单事务（ToolAction settle保存点）且失败落已提交draft——闭环直接挂入该事务，不新建run/检查点/收据体系；发布失败语义有现成机制承接 |
| R7 | 051降级守卫硬编码P2封套字面量且已冻结——052自带完整P3降级检测，不修改051 |
| R8 | P2对reviewed/closure的DTO级硬禁令（contracts.py:669-674）——以authorization字段条件放宽，mechanical路径禁令原样保留并有负例回归 |
| R9 | 全新event UUID当前只在主动评估时浮现、application_limits不比较generation——3.5采用发布侧重核（有界重冻结+scope digest），不做主动投影表 |
| R10 | persist工件extra=forbid回读——SemanticReview/fit契约全部新增字段带缺省值，旧行JSON回读纳入G1/G2验收 |

待确认问题（不阻塞开工，G0前落定）：规则摘要与closure提案在32/64k评审窗口内的分摊是否需要独立预算；`_dependency_limits`重核在高频读取下的耗时上限与是否需要有界缓存（先实测再决定，禁止无界缓存）；整案readyiness拆分对既有ag23验收样本断言的影响面；父任务/子任务绑定继承的精确语义；`GET /research-tasks/{id}/rule-binding`挂载在现有任务读取还是独立路由。
