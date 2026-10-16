# P2 全面复审、修复与追加验收

日期：2026-10-16（Asia/Shanghai）。状态：14组问题/边界修复已闭环，通过主代理最终验收；生产仍关闭。

用户授权继续采用 GPT‑6 Luna 子代理审查并修复 P2，由主代理验收；不推进 P3/P4、不启用生产开关。复审依据为[分层设计](../../account-fit-and-supplemental-rules-design.md)、[P2计划](../account-fit-p2-plan.md)及首次[验收记录](acceptance.md)。首次验收数字保留，本文件单独记录后续发现与时间边界。

三个 Luna 子代理分别审查模型/生命周期/迁移、匹配/来源、收据/恢复/界面；主代理独立审查应用服务、时间限制、读取快照、容量分页、查询成本和整体验收。以下列出本轮可复现的问题及实际修复，不把代码阅读意见或不合法fixture当成缺陷。

## 问题与闭环

| 编号 | 问题、影响及修复 | 追加反例 |
| --- | --- | --- |
| P2-R01 | 规则重存只比较payload，未重新验证存储hash列/schema_version；事件详情/列表未核重复的业务键、前版和时间列；head最新校验可指另一草案。统一精确解析与header核对，仓储/ORM/051约束最新验证必须当前同owner/rule草案；损坏是完整性错误，不伪unknown | test_p2_review_model：实际SQLite损坏rule hash/schema、7类event header、详情/可见列表；migration/create_all错误validation ref |
| P2-R02 | 051降级检查使用旧根形状，漏掉实际p2_schema_version+receipt.result_kind封套，可能仅留P2收据时降级。修为识别真实封套并保留旧形状守卫 | test_downgrade_detects_actual_p2_receipt_envelope；既有零成员/failed run、P1往返测试 |
| P2-R03 | 数据契约允许mechanical/pending声称supported/mitigated/resolved，reviewed无exact评审引用，闭合引用可自相矛盾。通用结果要求reviewed及精确ref、resolved有closure且无open，P2应用仍禁止伪造语义评审/闭合 | 参数化DTO反例；P1 publication回归。未来激活、pass后fail再approve、依赖版本变化后的HTTP批准额外补测 |
| P2-R04 | 仅过期行业分类区间被当作不适用；指标参数在绕过校验的旧/内部输入中会静默套用默认值。分类时间覆盖不足unknown；参数适配防御性unknown并保留参数审计，公网定义校验原本就拒绝非空参数 | test_p2_review_sources：过期SW分类；两参数叶子不复用默认结果；原匹配与三值/DSL回归 |
| P2-R05 | 服务把不匹配B证券规则的来源/因子混入A，可能使A报非法主体或无关变化stale；B的event锚点还能遮住A未绑定事件。按可确定身份筛选输入依赖，仍保留B的not_applicable声明pair；跨主体已存来源诊断排除，默认直接冻结的严格行为保留 | 两个真实listing/规则及相关新事件；真实EvidenceService保存→freeze→match，严格模式拒绝，服务模式unknown |
| P2-R06 | EvidenceService对被更正旧材料返回conflict/source_corrected，但冻结消费者只看payload中的verified/none，旧材料仍可充当正向依据。清单保留read_warnings；类别及research_requirement均核精确read_status/warnings；效果basis排除受限旧引用 | 真实research_dataset/DocumentProvenance更正链，经save_evidence→freeze→match/evaluate；旧证据冲突，新更正证据仍可用 |
| P2-R07 | current限制未处理research check_by与事件公布/生效/失效时刻，跨界后旧机械结果仍valid。比较冻结as_of与当前时间，跨界要求刷新；刷新后不重复误判同一旧边界 | 真实应用在check_by前/后求值并刷新；event effective_from/to分段限制 |
| P2-R08 | SQLite legacy SELECT不自动BEGIN，P2多查询GET可能混registry/source/应用代次，违反同一读取快照。新增P2局部ensure_read_snapshot，仅未开启SQLite事务时BEGIN，caller结束；已保留writer锁的命令/worker不重启事务，不改全局database策略 | 两个独立file SQLite WAL连接：GET读取registry后另一writer提交退役，GET仍完整读旧snapshot，第三连接确认退役；既有GET零DML |
| P2-R09 | source cap fallback只返回第一页并忽略cursor，历史未决项无法继续读取。降级视图保留unknown/coverage_pending并分页有界已存集合；cursor严格形状422、集合绑定改变409 | 真实两应用、受控source cap、两页不重复/不遗漏、坏cursor拒绝 |
| P2-R10 | current对每条应用重复规范化/哈希整个规则scope并扫描全部最新refs，成本随成员数平方增长。每scope预计算一次hash与latest ref集合，复用做成员限制 | 多规则真实保存后计数，禁止逐成员scope_hash调用；GET零写入回归 |
| P2-R11 | immutable收据与可变队列中的nested admission.operation_id/accepted_at及manifest.evaluated_at未全部对齐。授权核对完整身份、原时间和mode/purpose；live清单与接受时间一致，replay保持原清单时刻 | 真实HTTP已接收run，分别篡改nested admission、execution purpose、evaluated_at、accepted_at并重算自hash，仍明确拒绝 |
| P2-R12 | 将saved检查点改为合法item_failed/pending可隐藏已提交应用；损坏检查点可能漏掉终态一致性。起止/读取验证实际操作应用集合等于saved refs，校验确定性ID及完整输入；success必须全saved，partial不能留pending或无失败；已终结失败不重试 | 真实worker完成后改检查点，HTTP和worker均拒绝隐藏产物；原崩溃、失租、关闭和item_failed接管回归 |
| P2-R13 | 新增防隐藏校验若逐pair查询或按无索引JSON全扫，会破坏有界恢复。改为起止验证、500成员PK批量读取、pair_count+1操作集合上限，增加一条仅rule_application的owner/operation_id表达式索引；ORM/051固定DDL一致 | 用实际固定literal JSON路径查询作EXPLAIN，命中owner与operation；升级/降级与schema一致；两成员worker操作集合扫描为2次 |
| P2-R14 | 前端遇本地相同run编号时直接声称已持久化，可擦除身份不符/实际未存好的accepted回执。核对操作、path、完整body、manifest及exact run引用，并重新确认持久化；冲突保留回执 | Node身份负例；真实浏览器注入同run编号错误本地记录并刷新，accepted标记仍保留；双存储失败/ABA/P1兼容继续通过 |

## 主代理验收纠偏与兼容边界

- 撤回了“事件event_key必须不可变”的建议：设计以UUID稳定标识事件，并未禁止修订业务键/别名，擅加限制会收窄合法生命周期；没有保留该限制。
- 撤回了“EvidenceRecord没有id/revision，需只凭manifest元数据放行”的建议：实际继承Record具备这两个字段；删除了不合法手造字典的假反例和宽松授权。最终来源修复依据合法DTO、真实保存及更正链。
- 非空因子参数在原定义validation已拒绝，因此适配端unknown是防御改进，不把它夸大为公网已批准规则的绕过。受控能力缺失继续unknown/pending，没有新增可信政策采集或P3解除评审。
- SQLite表达式索引不能用绑定JSON路径的等价写法冒充已命中；实际worker与EXPLAIN共用固定literal路径。SQLAlchemy不能反射表达式索引，metadata diff不足以证明一致，另查实际DDL与执行计划。
- 第一次整合测试出现1 failed/114 passed，来自新索引测试未排除仓库既有research_change历史索引差异；按既有迁移审查边界修正测试。另一次子代理并行编辑中漏导入InvestmentAssessment导致临时NameError，已修正并由后续完整专项覆盖；不作为最终通过结果。
- 没有修改旧Factor Quality/DSL、P1产物/排序语义或全局SQLite事务策略。新索引属于共享assessment的P2 partial分支；没有新表、额外业务列或通用索引平台。三个功能开关仍默认false，测试仅用临时库/惰性key。

## 验证结果

| 主代理验证 | 最终结果 | 日志/JUnit |
| --- | --- | --- |
| 全量unit | 2435 passed，2 skipped，6 network默认排除；331.28秒；2437个唯一case，180模块 | `/tmp/zcharlie-p2-review-unit.dttdEf/unit.log`、`unit.xml` |
| 全量integration | 732 passed；无失败、错误、skip或排除；656.10秒；732个唯一case，131模块；含本地gateway契约 | `/tmp/zcharlie-p2-review-integration.MhsMCz/integration.log`、`integration.xml` |
| 根目录完整分区 | 717 passed；182.00秒；717个唯一case，37模块 | `/tmp/zcharlie-p2-review-root.P7IBO6/root.log`、`root.xml` |
| 最终P2/迁移/HTTP/Web/导入图专项 | 132 passed，8 warnings；33.20秒；132个唯一case，20模块 | `/tmp/zcharlie-p2-review-final.log`、`/tmp/zcharlie-p2-review-final.xml` |
| 真实浏览器 | P2/P1脚本串行，1440/390均通过；含相同run编号本地冲突、双存储失败reload、ABA、旧105成员排序/表单恢复 | `/tmp/zcharlie-p2-review-browser.log`；exit=0 |
| 静态/迁移门禁 | Ruff `src tests alembic`、mypy全部427源文件、两份P2 JS语法、diff检查通过；051单head | 最终代码执行 |

2项skip仍分别缺clean checkout不携带的Costco/HSBC原始材料，未伪造替代；6项network由仓库默认配置排除。集成109条警告、专项8条警告全部是SQLAlchemy/Alembic无法反射这条表达式索引的提示，未隐去或用metadata diff替代索引验收；实际DDL、部分条件、升级降级和带真实绑定参数的EXPLAIN已另验。没有新增绝对分层豁免或更改旧Quality/DSL黄金结果。

代码在完整分区收集前已整合，最终核对1187个source/test/migration文件组成的快照未再变化（`c4bbadb970eaa40d3725dc455f020ee7bcc02368a3760d27fefb1088be69c589`）。新增根代理service/snapshot组合20 passed；子代理专项仅用于定位，与完整分区重叠，不累加。

完整分区与专项互有重叠，后补用例及收集时间会独立记载。全量JUnit、实际case身份、日志hash及源码快照见[本轮验证摘要](review-validation-summary.json)；首次validation-summary.json保留原验收时点，不改写其测试数字。

生产数据库未运行迁移，未启用生产开关，未推进P3/P4；本轮未提交/推送。工作区并行移动端/文档修改及提交原样保留；浏览器自动重写的P1旧截图恢复到复跑前备份，P2合成截图单独更新。
