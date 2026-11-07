# 银行财报专题完整实现任务

状态：执行计划主审完成、实施待执行，本轮仅编制计划。依据[已评审设计](../03-bank-topic.md)、[共同约定](../README.md)、[主审记录](../review.md)、[总执行计划](../execution-plan.md)及[共享任务](00-shared-tasks.md)。首期固定招行FY2025对FY2024，完整交付静态专题和全部条件检查，不将检查接线留作可选尾项。

## 范围、路径和派发纪律

下表共50张原子卡。路径以仓库根为准，缩写`S/`=`src/investment_agent/`，`B/`=`src/investment_agent/bank_topics/`，`T/`=`tests/next_iterations/`，`F/`=`tests/fixtures/bank_topics/`，`D/`=`docs/product/plans/next-iterations-2026-11-07/execution/`。括号列出的文件名均逐个展开，不授权整个目录。B、F及下表命名的专题测试/页面/证据文件均为计划新增；其余现有接缝在BASE-01核实后只修改指定片段。

一个agent每次领取一张卡；同文件后续卡串行，依赖完成不代替文件锁。类型为Review的代理不得为该批主要作者，只写指定审查记录。回修逐条关联发现、diff及回归证据，由原审查者复核；无发现时记录无需修改。回修超出估算必须追加有依赖的修复子卡，不能把预算当作问题豁免。

所有共享热点明确owner=集成、持锁串行：ObjectRef、Alembic、模型导出、API注册/错误码、设置、worker、scheduler、web入口。既有`simulation/`、`tests/simulation/`、`storage/models/simulation.py`和`061_simulation.py`属于并行用户工作，禁止覆盖或回滚；修改`storage/models/__init__.py`只合并专题导出片段并保留现有dirty内容。新迁移按当时真实head分配，不能预占062或修改任何已有迁移。

原件只能读取本地已保存的`research/industry-data-validation/evidence/run-02/raw/official/600036.SH_2025.pdf`及已审核本地元数据；不另下载PDF、安装依赖或访问真实provider。FY2026等未来/更正案例使用明确标为synthetic的局部fixture，不能伪装为已取得官方年报。已有PDF核验结果须记录bytes/hash和行列，不以模型答案或截图代替。

## 原子任务卡

| ID | 类型 | 任务与交付物 | 允许写文件或明确新文件范围 | 依赖ID | 可验证退出条件 | 估算人日 |
|---|---|---|---|---|---|---|
| BT-001 | 实现 | 静态专题DTO、事实/解释/版本契约与固定模板标识 | `B/__init__.py`、`B/contracts.py`静态DTO；`T/test_bank_topic_contracts.py` | BASE-02, BASE-03 | Decimal字符串/null原因、期间/集团/CAS/CNY、稳定fact ID、封闭extra校验；示例合法且无float通路；不伪造现有bank-v2已支持新指标 | 0.5 |
| BT-002 | 实现 | 条件/检查DTO、有限metric及状态/manifest契约 | `B/contracts.py`条件DTO；`T/test_bank_condition_contracts.py` | BT-001 | condition内容/控制版本分离；检查outcome与队列状态分离；not_applicable仅叶解释；两种hash字段、subject和UTC合约闭合 | 0.5 |
| BT-003 | 实现 | 案例、修订、补充事实与mutation receipt模型 | `S/storage/models/bank_topics.py`静态四表部分；`T/test_bank_topic_models.py` | BT-001 | base_period、owner/listing、稳定字段身份和唯一键/索引字段齐全；revision/fact/receipt不可变，head受控可变；不依赖SQLite FK保完整性 | 0.75 |
| BT-004 | 实现 | condition revision/head及check revision模型 | `S/storage/models/bank_topics.py`条件三表部分；`T/test_bank_condition_models.py` | BT-002, BT-003 | condition_id/revision、owner/case、evaluator_version、execution_manifest_hash及semantic_input_hash实际落列；语义唯一键和due索引可查询；不增通用事件表 | 0.5 |
| BT-005 | 集成 | owner=集成；只注册四个topic ObjectRef kinds及命名空间 | `S/investor/common.py`精确Literal片段；`T/test_bank_topic_ref_kinds.py` | BT-001, BT-002 | topic_case/topic_revision/topic_condition/topic_check使用UUID；legacy整数语义不变；未扩大正式publication/AG22/evidence采信白名单 | 0.25 |
| BT-006 | 实现 | 只读仓储、历史版本/字段读取及immutable保护 | `S/storage/repositories/bank_topics.py`查询/保护部分；`S/storage/models/bank_topics.py`仅immutable DDL保护注册；`T/test_bank_topic_repository.py` | BT-004 | owner过滤、精确业务版本与UUID revision=1映射正确；不存在字段/跨父版本拒绝；ORM及直接SQL更新/删除受保护表均失败；BT-007复用同一保护定义确保create_all/迁移一致 | 0.75 |
| BT-007 | 集成 | owner=集成；注册专题模型与实际head后的迁移 | `S/storage/models/__init__.py`专题片段；`alembic/versions/<实际分配号>_bank_topics.py`（新）；`T/test_bank_topic_migration.py` | BT-004, BT-006 | 临时新库及含旧数据的前序库升级；字段/索引/trigger与create_all一致、单head；保留simulation现状，不写生产数据库 | 0.75 |
| BT-008 | 实现 | mutation receipt与head/control CAS写仓储 | `S/storage/repositories/bank_topics.py`写入部分；`T/test_bank_topic_mutations.py` | BT-005, BT-006, BT-007 | 同ID同参原收据、异参409；双session仅一个CAS成功；原子写及回滚无半份记录；先验当前访问再重放，不自动重放旧决定到新head | 0.75 |
| BT-009 | 测试 | 审核本地招行原件并建立最小金样本manifest | `F/manifest.json`、`F/SOURCE.md`、`F/expected.json`；`T/test_bank_topic_fixture_integrity.py` | BASE-01, BASE-03 | 校验PDF bytes/hash；PDF22/23对应印刷21/22；记录原件身份、已知公开/取得精度、两期表头与单位；只保存必要fixture/定位，不复制大PDF | 0.5 |
| BT-010 | 实现 | 新NII与NIM事实定位/校验规则 | `B/proofs.py`NII/NIM部分；`B/facts.py`标准化部分；`T/test_bank_nii_proofs.py` | BT-001, BT-009 | 两期NII215593/211277百万元及NIM1.87/1.98%正确回读；保留原文单位/列/集团/期间/hash；单位转换一次；错列/本行/季度/改hash拒绝 | 0.75 |
| BT-011 | 实现 | 官方量价表及交互项归属proof | `B/proofs.py`分解部分；`B/facts.py`分解部分；`T/test_bank_decomposition_proofs.py` | BT-010 | 16798/-12482/4316百万元及规模包含交互项脚注可核对；行列/正负号完整；新规则独立版本，不能套既有银行scope bridge冒认通过 | 0.75 |
| BT-012 | 实现 | Decimal分解、容差与缺数降级纯函数 | `B/decomposition.py`；`T/test_bank_decomposition.py` | BT-001, BT-010, BT-011 | precision40、增长展示2.04%、NIM-11bp、残差0；负债项反号、舍入区间明确；缺项不补平衡数，零基数/负基数null且原因正确 | 0.75 |
| BT-013 | 实现 | 精确引用resolver与现有peer/evidence/original绑定 | `B/references.py`；`B/facts.py`绑定部分；`T/test_bank_topic_references.py` | BT-005, BT-006, BT-010, BT-011 | owner/listing/kind/revision/field_path/hash回读；candidate及冲突状态不升级；既有peer事实不复制；批量限额/循环拒绝；正式财务路径仍拒绝直接采信topic解释 | 0.75 |
| BT-014 | Review | 独立Review一：契约、存储、迁移、原件和计算 | `D/evidence/review-bank-foundations.md`；业务代码只读 | BT-008, BT-012, BT-013 | 逐项审查事务/CAS/不可变/单位/期间/口径/原文列及采信边界；发现含位置、触发例、影响、关闭测试；没有发现仍记录实际检查范围 | 0.75 |
| BT-015 | 回修 | 关闭基础批Review问题并补反例 | 仅`B/contracts.py`、`B/proofs.py`、`B/facts.py`、`B/references.py`、`B/decomposition.py`、专题模型/仓储、上述对应专题测试与F最小fixture；共享文件及迁移仅owner=集成处理被指出片段；`D/evidence/fixes-bank-foundations.md` | BT-014 | 所有阻断及影响退出条件的问题修复；原审查者复核；回归不得触碰真实外部资源；无问题则记录无需改动 | 1 |
| BT-016 | 验收 | 独立验收一：基础与已有PDF金样本 | `D/evidence/acceptance-bank-foundations.md`；只读被验业务代码 | BT-015 | 本批非空测试清单通过；真实本地PDF核验与synthetic反例清楚分列；确认参考原规则未退化、无网络/生产库/真实凭证读取 | 0.5 |
| BT-017 | 实现 | 有界本地manifest导入并复用原件库 | `B/imports.py`；`T/test_bank_topic_imports.py` | BT-009, BT-013, BT-016 | 审核manifest/hash后复用原件blob，导入新事实草稿；重放幂等、路径越界/未知原件拒绝；无第二套PDF库、不自动armed、不发送历史观察事件 | 0.5 |
| BT-018 | 实现 | 固定问题模板及L1–L4解释/反证结构 | `B/templates.py`、`B/explanations.py`；`T/test_bank_topic_explanations.py` | BT-012, BT-016 | 披露陈述、恒等关系、机制假设、前瞻条件分层；支持/反证/缺口精确引用；缺反证标未完成，相关同向不转因果或买卖结论 | 0.5 |
| BT-019 | 实现 | 静态案例创建、修订、历史及更正提示服务 | `B/service.py`静态部分；`T/test_bank_topic_service.py` | BT-008, BT-013, BT-017, BT-018 | 模板/ticker受限；create/revision/archive与收据同事务；旧版本输入不随head漂移，正式更正追加supersedes；URL不可用与撤回分离；关闭仍读有权历史 | 0.75 |
| BT-020 | 实现 | 静态API模块和固定错误协议 | `S/api/routes/bank_topics.py`静态端点；`T/test_bank_topic_api.py` | BT-019 | 列表/创建/详情/修订返回schema/HTTP正确；API Key/同源会话/owner校验；BusinessError detail数组，缺值partial、空列表200、CAS409、无泄露 | 0.75 |
| BT-021 | 集成 | owner=集成；静态路由、设置开关、错误码及股票入口接线 | `S/api/app.py`、`S/api/routes/__init__.py`、`S/api/deps.py`、`S/config.py`、`S/settings/registry.py`、`S/web/routes.py`、`S/web/templates/stock.html`精确专题片段；`T/test_bank_topic_registration.py` | BT-002, BT-005, BT-007, BT-020 | 两开关均默认false；feature_disabled503及新增code显式映射，旧writes_disabled403不变；静态入口可装配且不依赖EV-12，未接事件时不能启用自动检查 | 1 |
| BT-022 | 实现 | 独立专题模板、事实/原页与披露分解UI | `S/web/templates/bank_topic.html`、`S/web/static/bank_topics.js`、`S/web/static/bank_topics.css`初始部分；`T/web/bank_topic_facts.test.mjs`（新） | BT-012, BT-020 | mock fetch/组件DOM覆盖加载/空/candidate/partial/错误；原页行列/单位/脚注、瀑布等值表和转义完备；Decimal无float回写，缺项不变0；使用BASE-03 Node/jsdom guard，不调用真实浏览器 | 0.75 |
| BT-023 | 实现 | 创建/版本切换、解释标签及静态交互状态 | `S/web/static/bank_topics.js`、`S/web/static/bank_topics.css`、`S/web/templates/bank_topic.html`交互部分；`T/web/bank_topic_history.test.mjs`（新） | BT-018, BT-021, BT-022 | 历史只读、stale/原件失败/未授权提示、修订冲突保留草稿；重试保持mutation UUID；键盘可访问、窄屏DOM结构完整；不宣称真实布局已通过 | 0.75 |
| BT-024 | Review | 独立Review二：静态服务/API/UI/入口 | `D/evidence/review-bank-static.md`；业务代码只读 | BT-021, BT-023 | 检查固定问题边界、授权/重放、旧引用、未核验披露、文本注入、默认关闭及不误触发研究；逐问题给出可验证关闭条件 | 0.75 |
| BT-025 | 回修 | 关闭静态批Review发现并补回归 | 仅`B/imports.py`、`B/templates.py`、`B/explanations.py`、`B/service.py`静态部分、`S/api/routes/bank_topics.py`静态部分、专题页面/JS/CSS及对应BT-017、BT-018、BT-019、BT-020、BT-021、BT-022、BT-023测试；共享接线只由owner=集成回修指出片段；`D/evidence/fixes-bank-static.md` | BT-024 | 阻断项关闭、原审查者复核；静态功能无需事件库即可装配，修复不扩大正式采信或修改原peer矩阵 | 1 |
| BT-026 | 验收 | 独立验收二：静态专题完整切片及发行资产 | `D/evidence/acceptance-bank-static.md`；只读业务代码 | BT-025, PK-02 | 分别在临时SQLite服务、ASGI及mock DOM验证创建/核对/历史/解释，不串完整旅程；场景1–8及18有证据；PK-02 helper检查实际bank_topic.html/bank_topics.js/bank_topics.css在wheel/ZIP中，不以源码目录存在替代；无EV-12依赖、无真实E2E | 0.5 |
| BT-027 | 实现 | 有限DSL解析器及metric依赖注册表 | `B/conditions.py`解析/依赖部分；`T/test_bank_condition_dsl.py` | BT-002, BT-016 | 根all/any、1–5叶、深度≤2及闭合metric/op/unit严格校验；拒绝脚本/SQL/URL/任意字段；依赖映射到明确财务指标，不以通用eval实现 | 0.5 |
| BT-028 | 实现 | 叶计算及三值根求值 | `B/conditions.py`求值部分；`T/test_bank_condition_logic.py` | BT-012, BT-027 | all false/unknown、any true/unknown正确；rate≥0叶not_applicable/no_rate_drag映射unknown，rate<0且volume≤0=false；根可判定仍保留partial/缺口 | 0.5 |
| BT-029 | 实现 | next annual选择、同年版本与时点过滤 | `B/reports.py`；`T/test_bank_report_selection.py` | BT-013, BT-017, BT-026 | 已核验财年日历选FY2026且不跳FY2027；不取Q1/H1/Q3或年化；公开上界和取得时间双过滤；未知日期保留unknown；正式更正链/不明重发区分 | 0.75 |
| BT-030 | 实现 | 冻结执行manifest与语义hash去重 | `B/manifests.py`；`T/test_bank_check_hashes.py` | BT-002, BT-029 | execution含as_of，semantic含refs/values/condition/evaluator/目标/可用性及pre_due/due/expired且排除扫描时钟；次序/Decimal编码稳定；相同语义返回原执行时间/收据 | 0.5 |
| BT-031 | 实现 | 条件草稿、显式启用与当前权限/控制CAS | `B/condition_service.py`；`T/test_bank_condition_service.py` | BT-008, BT-026, BT-027, BT-030 | 起始FY2025/24与resolver齐备即可armed，目标未出现正常等待；新内容版本不继承授权；原topic版本绑定无环；pause/resume/cancel CAS、先验权限后重放 | 0.75 |
| BT-032 | 实现 | 纯本地检查与due/expiry生命周期转换 | `B/checks.py`、`B/lifecycle.py`；`T/test_bank_check_lifecycle.py` | BT-028, BT-029, BT-030, BT-031 | awaiting_source只是armed原因；due/expiry阶段各至多一次不足结果；met/not_met完成，缺披露等至expiry；UTC比较/上海展示，不把作业失败当not_met | 0.75 |
| BT-033 | 实现 | 迟到资料、正式更正及原件失效处理 | `B/corrections.py`；`B/checks.py`更正调用片段；`T/test_bank_check_corrections.py` | BT-019, BT-029, BT-032 | 截止前补发新check链接prior；截止后迟到需显式动作且不自动armed；正式更正可追加late_correction/supersedes；旧check不可改，未知重发为冲突 | 0.75 |
| BT-034 | Review | 独立Review三：条件、时间、hash与更正核心 | `D/evidence/review-bank-conditions.md`；业务代码只读 | BT-033 | 穷举三值/无拖累边界、目标年和时点精度；审核双hash、阶段变化、起始/目标依赖区分、暂停后当前控制；发现逐项记录 | 0.75 |
| BT-035 | 回修 | 关闭条件核心Review问题 | 仅`B/conditions.py`、`B/reports.py`、`B/manifests.py`、`B/condition_service.py`、`B/checks.py`、`B/lifecycle.py`、`B/corrections.py`及BT-027、BT-028、BT-029、BT-030、BT-031、BT-032、BT-033对应测试；`D/evidence/fixes-bank-conditions.md` | BT-034 | 阻断项关闭且原审查者复核；语义重复与时间阶段测试同时通过，不以取消时点过滤规避重复问题 | 1 |
| BT-036 | 验收 | 独立验收三：全部条件纯服务核心 | `D/evidence/acceptance-bank-conditions.md`；只读业务代码 | BT-035 | 临时SQLite/假时钟核验场景9–14及19；synthetic FY2026/更正明确标识；此卡仅证明核心，事件/队列实际接通仍必须由后续卡验收 | 0.5 |
| BT-037 | 实现 | 单check观察事件生产与InboxContribution适配 | `B/events.py`；`T/test_bank_topic_events.py` | BT-036, EV-12 | 使用调用方session append_event；type=topic.observation_changed，source=immutable topic_check UUID/revision1；subject={kind:listing,listing_id}匹配源；一期一payload，引用/期间/口径/outcome/changed理由完整且无秘密原文 | 0.75 |
| BT-038 | 集成 | owner=集成；共享EventSourceRef/resolver及适配注册 | `S/research_events/contracts.py`、`S/research_events/resolvers.py`、`S/research_events/registry.py`的topic片段；`T/test_bank_topic_event_registration.py` | BT-037, EV-12 | 只开放topic_check精确来源，owner/listing/版本/hash拒绝伪造；按BASE-02交付贡献协议，无审核关系固定unlinked/context；适配未安装不冒认已接通 | 0.5 |
| BT-039 | 实现 | 检查任务准入、冻结输入、语义键复用 | `B/jobs.py`准入部分；`T/test_bank_check_admission.py` | BT-030, BT-031, BT-036, EV-12 | 原pipeline_run复用；相同语义活跃/终态只返回原run/check；当前权限、功能及事件可用性检查；新资料新任务；不向网络/模型请求材料 | 0.5 |
| BT-040 | 实现 | 单handler执行、lease fencing与同事务结账 | `B/jobs.py`handler部分；`T/test_bank_check_handler.py` | BT-032, BT-033, BT-037, BT-039 | 存check/更新head/事件/结账同短事务；提交前重查控制/开关/lease；取消映射pipeline.failed+reason=cancelled且不重试；无伪outcome或取消观察事件 | 0.75 |
| BT-041 | 实现 | 有界到期扫描、新本地资料唤醒与健康摘要 | `B/scheduling.py`、`B/health.py`；`T/test_bank_check_scheduling.py` | BT-029, BT-032, BT-039, BT-040 | 固定批量/明确水位、唯一键防双进程重复；暂停/归档/过期不再派发，正式更正例外按规则；本地新资料扫描可唤醒、无资料不刷事件；报告active/partial/retry原因 | 0.75 |
| BT-042 | 集成 | owner=集成；worker/scheduler及运行时开关接线 | `S/research/worker.py`、`S/scheduler/runner.py`、`S/settings/registry.py`专题片段；`T/test_bank_check_runtime_registration.py` | BT-021, BT-038, BT-040, BT-041, EV-12 | 新kind只由collection领取，单例定期有界扫描；条件开关需EV可用；关闭阻止新动作、在途机械结账和终态事件消费保留；不改pipeline状态枚举、不启动实际进程 | 0.75 |
| BT-043 | 实现 | 条件保存/启用/控制/检查及结果API | `S/api/routes/bank_topics.py`条件端点；`T/test_bank_condition_api.py` | BT-020, BT-031, BT-033, BT-038, BT-039, BT-040 | 201/200/202、run/check精确引用及409/422/503错误映射；请求身份/同源/owner/版本严格校验；目标缺报告允许armed；结果给逐叶未知、manifest和更正关系 | 0.75 |
| BT-044 | 实现 | 条件编辑预览、启停及结果/更正UI | `S/web/templates/bank_topic.html`、`S/web/static/bank_topics.js`、`S/web/static/bank_topics.css`条件部分；`T/web/bank_topic_conditions.test.mjs`（新） | BT-023, BT-028, BT-043 | DOM/mock API可操作保存草稿/显式armed/暂停取消/检查；依赖、下一年度、时区/日期精度/等待原因可见；对比结果及late_correction明确；已读不改领域事实 | 1 |
| BT-045 | 测试 | 独立事务、竞态与局部故障注入包 | `T/test_bank_topic_transactions.py`、`T/test_bank_topic_recovery.py`（新） | BT-038, BT-040, BT-041, BT-042 | 文件SQLite双session、提交前丢lease/提交后退出/重复消费验证全成全无；真实EV配局部测试consumer验证一次receipt；旧确认不吞更正新事件；失败均可恢复且外调计数0 | 1 |
| BT-046 | 测试 | 独立ASGI/组件状态及权限回归包 | `T/test_bank_topics_acceptance.py`、`T/web/bank_topic_acceptance.test.mjs`（新） | BT-026, BT-036, BT-042, BT-043, BT-044 | 覆盖19场景的API/状态联动；撤权后同mutation不能越权重放，开关关闭历史仍读；组件保留草稿/UUID、转义和假日期；只进程内API+BASE-03 guarded DOM，无真实布局断言 | 0.75 |
| BT-047 | 实现 | 操作说明、开关回退、材料及未验证项文档 | `docs/operations/bank-topics.md`、`D/evidence/bank-test-manifest.md`（新） | BT-017, BT-026, BT-036, BT-042, BT-043, BT-044 | 列明有界导入、规则版本、两开关、EV依赖、停止领取/机械结账/历史读取及无删除回退；测试allowlist/fixture来源明确；无真实部署/试用/安装步骤作为任务门槛 | 0.5 |
| BT-048 | Review | 独立Review四：事件/队列/API/UI及全方向收口 | `D/evidence/review-bank-complete.md`；业务代码只读 | BT-038, BT-042, BT-043, BT-044, BT-045, BT-046, BT-047 | 对照BT-01–08与19场景逐项审查真实接线/事务/取消/重试/权限/时间/说明；无占位模块/漏条件检查；明确真实IT投影交叉语义交XA且不冒称已验证 | 0.75 |
| BT-049 | 回修 | 关闭整线Review发现及所有阻断回归 | 仅`B/events.py`、`B/jobs.py`、`B/scheduling.py`、`B/health.py`、专题API/页面/JS/CSS、BT-037、BT-038、BT-039、BT-040、BT-041、BT-042、BT-043、BT-044、BT-045、BT-046对应测试和`docs/operations/bank-topics.md`；共享事件/worker/scheduler/settings仅owner=集成处理已指出片段；`D/evidence/fixes-bank-complete.md` | BT-048 | 每项问题有回归证据并经原审查者关闭；核心批发现返工先追加精确子卡，不越界顺手改前批/其他方向；无发现记录无需改动 | 1 |
| BT-050 | 验收 | 独立最终离线验收与方向交付清单 | `D/evidence/acceptance-bank-complete.md`；只读业务代码，运行允许清单 | BT-026, BT-036, BT-049, EV-12 | 全50卡交付证据、19场景及四次Review闭环；临时库迁移/ASGI/DOM/局部恢复及目标静态检查通过且非零测试；默认关闭但静态与条件完整接线，真实体验/模型/外部兼容未验证明确列出 | 0.75 |

四次验收分别对应基础、静态切片、条件核心、完整功能；BT-036不能替代BT-050。静态切片BT-026没有EV外部依赖，BT-037起接共享基础；EV-12同时是事件基础已完成独立Review/回修/验收的门槛。

## 原方案任务覆盖

| 原任务 | 具体实现卡 | 独立Review、回修、验收 |
|---|---|---|
| BT-01 合约与引用 | BT-001, BT-002, BT-005, BT-013 | BT-014, BT-015, BT-016 |
| BT-02 持久化/CAS | BT-003, BT-004, BT-006, BT-007, BT-008 | BT-014, BT-015, BT-016 |
| BT-03 样本定位与计算 | BT-009, BT-010, BT-011, BT-012, BT-017 | BT-014, BT-015, BT-016, BT-024, BT-025, BT-026 |
| BT-04 静态服务与API | BT-018, BT-019, BT-020, BT-021 | BT-024, BT-025, BT-026 |
| BT-05 页面 | BT-022, BT-023, BT-044, BT-046 | BT-024, BT-025, BT-026, BT-048, BT-049, BT-050 |
| BT-06 DSL与选择器 | BT-027, BT-028, BT-029, BT-030, BT-031, BT-032, BT-033 | BT-034, BT-035, BT-036 |
| BT-07 队列与事件 | BT-037, BT-038, BT-039, BT-040, BT-041, BT-042, BT-043, BT-045 | BT-048, BT-049, BT-050 |
| BT-08 启用与验收 | BT-021, BT-042, BT-045, BT-046, BT-047 | BT-014, BT-015, BT-016, BT-024, BT-025, BT-026, BT-034, BT-035, BT-036, BT-048, BT-049, BT-050 |

## 19场景覆盖与证据落点

| 设计场景 | 验证卡 | 核心断言/证据 |
|---|---|---|
| 1 招行金样本 | BT-009, BT-010, BT-011, BT-012, BT-016, BT-026 | 原字节/hash、原页/印刷页及行列；2.04%、-11bp、残差0 |
| 2 母行混集团 | BT-010, BT-011, BT-013, BT-016, BT-046 | scope_conflict/422，无可比派生量 |
| 3 单位/百分比重复转换 | BT-001, BT-010, BT-012, BT-016 | 标准化一次、原单位保留、错单位拒绝 |
| 4 错列/季度 | BT-010, BT-011, BT-016, BT-029 | 行列/期间反例被拒，不取最近数字补齐 |
| 5 分解缺项 | BT-012, BT-022, BT-026, BT-046 | partial/unavailable，无补零/平衡数/绿色完整标记 |
| 6 原件零/前期零 | BT-012, BT-016, BT-046 | 0是值；增长率null/zero_denominator |
| 7 owner/ref/field_path伪造 | BT-005, BT-006, BT-013, BT-020, BT-038, BT-046 | 精确身份与当前权限检查，拒绝且不泄露 |
| 8 重放/异参/CAS | BT-008, BT-019, BT-031, BT-045, BT-046 | 双session只一个head，同ID异参409、撤权不能重放 |
| 9 H1/Q3不能替代年度 | BT-029, BT-036, BT-046 | FY2026未取得正常等待，目标不跳FY2027 |
| 10 due/expiry与日期精度 | BT-029, BT-032, BT-036, BT-046 | 公开上界+取得时间不越as_of、UTC/上海边界 |
| 11 无资料重复tick | BT-030, BT-032, BT-041, BT-045, BT-050 | 阶段内去重；due/expiry各至多一次，不把新扫描时刻改成原执行时刻 |
| 12 正式更正/不明重发 | BT-019, BT-029, BT-033, BT-036, BT-045 | 正式链新check/supersedes；不明重发冲突；旧manifest不漂移 |
| 13 过期后迟到/更正 | BT-033, BT-036, BT-044, BT-046 | 迟到需显式动作；late_correction可更正历史、不重armed |
| 14 三值组合 | BT-027, BT-028, BT-036, BT-046 | all/any穷举与partial覆盖，无叶被静默忽略 |
| 15 lease/退出/消费两次 | BT-038, BT-040, BT-045, BT-050 | 局部故障下结果/事件全成全无；同语义恢复同check，receipt一次 |
| 16 暂停/撤权/关闭与在途 | BT-031, BT-040, BT-042, BT-045, BT-046 | 提交前重查，pipeline.failed取消且不重试；历史/机械结账继续 |
| 17 确认后又更正 | BT-037, BT-038, BT-045, BT-050 | 真实EV+局部测试consumer旧receipt不吞新事件ID；实际IT投影/已读UI跨功能测试由XA负责，不借此声称已验收收件箱 |
| 18 未审解释/相关同向 | BT-018, BT-023, BT-024, BT-026, BT-046 | L1–L4及反证/缺口标签保持，topic不自动进入正式采信 |
| 19 无利率拖累/规模非正 | BT-028, BT-036, BT-044, BT-046 | no_rate_drag叶映射unknown；rate<0且volume≤0=false，根按三值逻辑 |

## 离线验收边界和工时

所有任务测试均使用BASE-03隔离，选择已核实的pytest、ASGI、临时文件SQLite、假时钟、Node内建test及BASE-03对mobile现有jsdom的guarded bootstrap。直接组合一个生产者/handler/消费者服务允许；不启动完整服务栈、不运行`tests/browser/run.sh`、Playwright/Selenium、真实设备、真实provider/模型/行情/账户/付费或外发链路。前端专题是现有服务端模板/原生JS，组件只导入指定静态模块，不改造成React页面，不新增依赖安装任务。PK-01由集成人员统一补package-data，PK-02提供临时构建/ZIP helper；银行只依赖共享PK交付，不依赖QT资产注册。

执行前展开本文件测试清单，由BASE-03入口检查只收集允许集合且计数非零；局部测试不得以`not network`标签代替实际网络阻断。迁移只跑临时库，命令与退出码记录在对应验收证据中。Ruff/mypy只查本方向改动Python与受影响导入，模板/API schema检查和已有前端typecheck/build按BASE-01锁定环境运行；其他用户改动引起的基线失败单独标记，不扩范围修复。

DOM结构、CSS规则及键盘行为检查不证明390px/1440px真实布局无溢出；实际浏览器/设备视觉体验、真实模型研究质量、外部新年度资料兼容性、生产吞吐和连续运行稳定性均标为未验证。本计划不安排这些验证，也不以真实试用或生产启用作为离线交付门槛。功能、接线、错误/partial及全部条件检查仍必须完整实现。

上表合计50卡、35人日，包含卡内自测及四次独立Review/回修/验收；不含BASE/EV/PK共享投入及其他方向/XA工时。这是工程容量估算，不是并行后日历承诺。任务未执行，不预填任何通过日志；实际发现导致新增卡时同步更新索引、依赖和总量。
