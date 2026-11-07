# 方向四实施任务：组合压力试验与决定事实核对

状态：执行计划主审完成；本轮仅写计划，未执行任何实现、测试、迁移、部署或真实E2E。
依据：[方向四已评审方案](../04-portfolio-stress.md)、[共同约定](../README.md)、[主审记录](../review.md)。
设计基线为`1f62fa4`；实施基线、实际Alembic head及已有并行改动由BASE-01重新登记。
本表覆盖S1与S2、原PS-01至PS-08及A01至A21；共55个原子任务，估算为单人有效工作日。
合计44.5人日，包含四轮独立Review/回修、分阶段集成和离线验收；BASE与EV共享基础不重复计算，不能直接换算并行日历承诺。
所有PT任务状态统一为“尚未执行”；表内退出条件是后续可验证要求，不是已取得的结果。

## 执行边界与派工规则

- 本轮不启动实现worker、不付费调用、不改原方案和业务代码；后续每次派工只给具体任务与必要上下文。
- 禁止真实浏览器全栈、真实provider行情、付费模型、真实账户E2E、生产数据库迁移与生产启用。
- 允许纯函数、临时文件型SQLite、进程内ASGI、mock-provider、组件Fake DOM、静态构建与局部故障注入。
- 不连接真实账户；AG22样本由原保存链在临时SQLite生成并读取，或采用已批准脱敏离线归档；禁止手写假记录冒充适配验证。
- 真实用户走查、真实材料/账户覆盖、生产时延、使用收益和真实端到端效果均记为“未验证”，不是本计划完成门槛。
- 当前`simulation/`、`storage/models/simulation.py`、`tests/simulation/`、`061_simulation.py`及其既有导出改动属于并行工作，不能编辑、重命名、覆盖或清理。
- 新迁移由集成人员从届时实际head分配；不预占061/062，不改已存在迁移，不从生产拷库做测试。
- 一个实现代理一次仅领取一个就绪任务；同一文件的任务按依赖串行，独立文件才能并行；必要时使用独立worktree。
- Review由未撰写该阶段主要实现的代理执行，只读业务源码，提交具体文件/行号、触发条件、复现与结论。
- 回修代理只能写本轮finding涉及且已在对应实现卡授权的文件；未知范围先由集成人员重新切卡，不能借回修扩大权限。
- 四轮Review、回修、验收分别记录；有未关闭阻断finding就不推进阶段验收，不能用测试数量替代契约核对。

外部前置使用BASE-01（基线、工作区及命令清单）、BASE-02（错误/引用/事件/时间契约）、BASE-03（离线隔离fixtures）、EV-12及下述PK资产任务。
主审补充PK-01/PK-02共享资产打包任务；S1阶段验收与最终验收分别检查实际S1/S2模板、JS和嵌套CSS，不等待质量方向的注册任务。
EV-12表示共享事件基础已完成离线验收，不表示收件箱页面完成。本方向不重复建outbox、收据表或maintenance进程。
S1在无EV基础时独立交付；S2契约/本地实现可用fixture先开发，S2运行接入与最终验收必须等待EV-12。
跨方向完整集成验收由主代理统一安排；本表验证本方向adapter及契约兼容，不领取其他方向业务实现。

路径缩写仅为表格节省空间：`S/`=`src/investment_agent/`；`T/`=`tests/portfolio_stress/`；`F/`=`tests/fixtures/portfolio_stress/`；`D/`=`docs/product/plans/next-iterations-2026-11-07/execution/portfolio-evidence/`。
表内未存在的T/F/D目录、stress/observation/fact-check模块均为明确的新文件范围；其余列出的共享文件是现有文件。
所有`api/deps.py`、ObjectRef枚举、API路由注册、settings注册、models导出、Alembic链、前端装配/导航、共享事件注册均由owner=集成人员串行修改；普通代理只提交建议，不直接写热点。
共享事件固定模块位于`S/research_events/`；本方向只在集成任务修改`registry.py`与`resolvers.py`的受限条目，不改EV公共事务语义。

## A. S1契约、输入与算术基础

| ID | 类型 | 任务与交付物 | 允许写文件或明确新文件范围 | 依赖ID | 可验证退出条件 | 估算人日 |
|---|---|---|---|---|---|---|
| PT-001 | 实现 | 建离线输入清单与AG22-v2金样，复用BASE-03 guard/allowlist，记录保存字段与不支持分支 | 新建`F/manifest.json`、`F/ag22_v2/`、`T/conftest.py`、`T/sample_builders.py`；只读既有AG22测试/保存服务 | BASE-01, BASE-03 | 样本通过原save/get链；未读取真实账户/.env；全部输出能追到fixture与hash；不存在手写伪成功记录 | 0.75 |
| PT-002 | 实现 | 实现S1请求、冲击/路径联合、read-model、Decimal规范化hash与容量边界 | 新建`S/portfolio/stress_contracts.py`、`S/portfolio/stress_canonical.py` | BASE-02, PT-001 | 4路径/20冲击/50证券限制明确；float、bool、非有限数、超指数拒绝；两段S1示例补全真实fixture引用后通过DTO | 0.75 |
| PT-003 | 实现 | 实现S1 head/revision、幂等operation表模型与不可变边界 | 新建`S/storage/models/portfolio_stress.py`的S1模型 | PT-002 | 字段/唯一键/索引与方案一致；配置revision、control_revision、epoch分离；result_id预分配且不可改写 | 0.5 |
| PT-004 | 集成 | owner=集成人员；串行接入S1模型导出并分配实际迁移 | `S/storage/models/__init__.py`仅新增本方向导出；新建`alembic/versions/<实际分配号>_portfolio_stress.py`；`T/test_stress_migrations.py` | BASE-01, PT-003 | 临时SQLite从当前真实head升级；旧行不改写；单head且保留并行simulation/061原字节；索引与模型一致 | 0.75 |
| PT-005 | 集成 | owner=集成人员；受控注册两类领域引用并防止扩大正式方法采信 | `S/investor/common.py`；必要时仅守卫`S/investor/publication.py`、`S/investor/publication_numbers.py`、`S/investor/methods.py`、`S/portfolio/plans.py`、`S/evidence/services.py`；新建`T/test_method_isolation.py` | BASE-02, PT-002 | 新ref可寻址但不能作正式估值/执行/审批证明；原publication/AG22/审批方法白名单保持；A21反向注入被拒绝 | 0.75 |
| PT-006 | 实现 | 实现owner、账户及snapshot/valuation/mandate精确关联解析 | 新建`S/portfolio/stress_inputs.py`的基础解析器 | PT-002, PT-003 | 跨owner404；错账户/版本422；current与historical模式分开；账户变化不偷偷重绑定 | 0.75 |
| PT-007 | 实现 | 实现原AG22-v2计算和plan-option冻结适配器 | 新建`S/portfolio/stress_basis.py`；必要fixture追加仅`F/ag22_v2/` | PT-001, PT-006 | 复用原数量/现金/费用；费用不重复扣；同基准比较；v1与未知method_version明确unsupported，无当前值回落 | 1 |
| PT-008 | 实现 | 冻结价格时间/版本/可用性及零持仓、停牌诊断 | `S/portfolio/stress_inputs.py`仅价格解析部分 | PT-006, PT-007 | id/version/股号/未复权/知识截止全部验证；已过期或原估值未知值不被重新捞回；停牌不被标可成交 | 0.75 |
| PT-009 | 实现 | 冻结csrc时点行业副本与来源manifest，逐结果降级 | 新建`S/portfolio/stress_industry.py`；`S/portfolio/stress_inputs.py`仅装配调用 | PT-008 | 请求/查询/manifest均为csrc；半开区间/多解/缺码正确；源行变化不改旧hash；全局/逐股不被行业缺失阻塞 | 0.75 |
| PT-010 | 实现 | 实现冲击粒度覆盖与假设来源归一化 | 新建`S/portfolio/stress_shocks.py` | PT-002 | 逐股覆盖行业覆盖全局；同粒度重复目标拒绝；未命中显式0；行业未知不静默退回全局冲击 | 0.5 |
| PT-011 | 实现 | 实现独立Decimal压力纯内核与金额/比例精度规则 | 新建`S/portfolio/stress.py` | PT-010 | 与手算金样一致；动态金额精度与比例40位；-100%/零净值/极大有限值无除零或float污染 | 1 |
| PT-012 | 实现 | 实现partial、逐项约束、质量说明与SavedCalculation结果装配 | 新建`S/portfolio/stress_results.py`；`S/portfolio/stress.py`仅结果接口 | PT-007, PT-009, PT-011 | 缺现金/价/量不补0；未知保留Quantity原因；只有已知小计；非ok结果不带成功results；正式mandate只读 | 0.75 |
| PT-013 | 测试 | 以参数化与golden fixture验证冻结输入、覆盖、极端值和正式采信隔离 | 新建`T/test_stress_contracts.py`、`T/test_stress_inputs.py`、`T/test_stress_kernel.py`；补`T/test_method_isolation.py` | PT-004, PT-005, PT-012 | A03—A10及A21取得失败反例与通过证据；源行更改后重放结果不变；未触发网络/model | 1 |
| PT-014 | Review | 独立Review一：输入时点、算术、partial、schema与采信边界 | 仅新建`D/review-01-foundation.md`；业务源码只读 | PT-013 | 逐项核对PS-01/02/03与A03—A10/A21；finding分阻断与改进，含文件行号/触发条件 | 0.5 |
| PT-015 | 回修 | 关闭Review一阻断项并提交针对性复验 | 仅PT-001至PT-013中finding点名且原卡授权的文件；`D/review-01-fixes.md` | PT-014 | 独立reviewer复核阻断项全关；不改业务范围或用放宽校验消除失败 | 0.75 |

## B. S1持久化、worker、API、界面与独立验收

| ID | 类型 | 任务与交付物 | 允许写文件或明确新文件范围 | 依赖ID | 可验证退出条件 | 估算人日 |
|---|---|---|---|---|---|---|
| PT-016 | 实现 | 实现S1创建/新revision/请求收据与冻结入队事务 | 新建`S/portfolio/stress_service.py`的写入服务 | PT-015 | 认证/范围校验先于重放；重放先于当前CAS；同key同参原收据、异参409；冻结+head+queue同事务 | 1 |
| PT-017 | 实现 | 实现S1列表/详情/历史、取消epoch及运行态映射 | `S/portfolio/stress_service.py`仅读/取消方法 | PT-016 | GET不重算；关闭功能仍可授权历史读取；取消推进epoch并保留旧记录；pipeline不新增cancelled状态 | 0.75 |
| PT-018 | 实现 | 实现S1 handler、租约/心跳、恢复、5秒本地预算与同session提交扩展点 | 新建`S/portfolio/stress_jobs.py`的S1 handler | PT-012, PT-016, PT-017 | 重用冻结input_hash/result_id；失效lease/epoch不能发布；缺项partial、取消failed+reason且不重试；事件sink可先缺省供S1独立执行 | 1 |
| PT-019 | 实现 | 实现S1专用HTTP schema、路由及BusinessError边界 | 新建`S/api/schemas/portfolio_stress.py`、`S/api/routes/portfolio_stress.py` | PT-017, PT-018 | create201/手动重试语义明确；404/409/422不泄露payload；Cookie同源；feature_disabled走503约定 | 0.75 |
| PT-020 | 集成 | owner=集成人员；串行接入S1路由、worker kind、设置与错误码 | `S/api/app.py`、`S/api/deps.py`、`S/research/worker.py`、`S/config.py`、`S/settings/registry.py`、必要`S/settings/loader.py`；`docker-compose.yml`仅离线配置中的kind列表 | PT-004, PT-005, PT-019 | 默认false；新增code显式映射；既有writes_disabled保持403；S1分派到本地采集worker；不依赖EV或Agent grant | 0.75 |
| PT-021 | 测试 | 临时SQLite/进程内ASGI与故障注入验证S1持久化链 | 新建`T/test_stress_service.py`、`T/test_stress_api.py`、`T/test_stress_jobs.py` | PT-020 | A01/A02/A03/A11/A12本地结果原子性/A17通过；双worker、取消竞争、commit前后中断不重绑或多写 | 1 |
| PT-022 | 实现 | S1独立页面与选择基准/路径/冲击表单，保存待确认请求 | 新建`S/web/templates/portfolio_stress.html`、`S/web/static/portfolio_stress_form.js`、`S/web/static/pages/portfolio_stress.css` | PT-002, PT-019 | 无账户/无估值/历史基准/参数错误有明确态；未确认模板不提交；超时重发保留原operation_id与body | 1 |
| PT-023 | 实现 | S1结果比较、逐数来源、partial/取消/重试/历史基准视图 | 新建`S/web/static/portfolio_stress_results.js`、`S/web/static/portfolio_stress.js`；仅续写`portfolio_stress.html`结果区/CSS | PT-021, PT-022 | 只消费服务器Decimal字符串；未知非0；不提供收益率/成交承诺；新基准创建新revision而非覆盖 | 0.75 |
| PT-024 | 集成 | owner=集成人员；串行接页面入口与模块，补ASGI/Fake DOM契约测试 | `S/web/routes.py`、`S/web/static/app.js`、必要`S/web/static/nav.js`、`S/web/templates/base.html`与`portfolio.html`入口；新建`T/test_stress_web.py`、`T/test_stress_dom.py` | PT-020, PT-023 | 入口/静态版本URL/只读历史可达；390/1440样式规则及Fake DOM状态转换通过；不启动浏览器或服务进程 | 1 |
| PT-025 | Review | 独立Review二：S1完整链、写入边界、取消恢复与页面语义 | 仅新建`D/review-02-s1.md`；源码只读 | PT-021, PT-024 | 审查请求重放、授权、队列归属、默认关闭、partial与执行分离；记录无真实E2E/生产性能结论 | 0.5 |
| PT-026 | 回修 | 修复Review二并对S1相关测试作最小复验 | 仅PT-016至PT-024中finding点名的授权文件；共享热点仅集成人员写；`D/review-02-fixes.md` | PT-025 | 阻断项独立复核关闭；S1不被S2/EV缺失阻塞，既有账户/AG22行为不回归 | 0.75 |
| PT-027 | 验收 | 独立S1离线验收与容量基准，产出阶段签收清单 | 仅新建`D/s1-acceptance.md`、`D/s1-results.json`及日志；业务只读 | PT-026, PK-02 | A01—A12的S1部分、A17/A18/A21通过；PK helper验证实际S1模板/JS/嵌套CSS入wheel；50×4×20本地P95和环境如实记录；未达目标列问题，不冒称已上线 | 1 |

## C. S2观察、绑定与账户级并发控制

S2局部测试使用EV契约fixture，不伪造EV-12已完成；下列可离线开发，但不可据此启用S2。

| ID | 类型 | 任务与交付物 | 允许写文件或明确新文件范围 | 依赖ID | 可验证退出条件 | 估算人日 |
|---|---|---|---|---|---|---|
| PT-028 | 实现 | 实现观察predicate/deadline/authorization/binding异构联合及JSON样例验证 | 新建`S/investor/decision_observation_contracts.py`、`F/observations.json`、`T/test_observation_contracts.py` | BASE-02, PT-002 | 四谓词白名单、eq容差、固定日/交易日、三重CAS和首次0版本语义明确；任意表达式/owner覆盖拒绝 | 0.75 |
| PT-029 | 实现 | 实现观察head/revision、绑定、账户binding head模型与实体索引 | 新建`S/storage/models/decision_observation.py` | PT-028, PT-003 | event_id/account_id为实体列；撤销链有索引；immutable配置、运行态、权限epoch、完整声明分离 | 0.5 |
| PT-030 | 集成 | owner=集成人员；分配S2迁移并接模型导出，验证S1旧数据兼容 | `S/storage/models/__init__.py`仅新导出；新建`alembic/versions/<实际分配号>_decision_observation.py`；`T/test_observation_migrations.py` | PT-004, PT-029 | 当前实际head单链升级；S1数据不改；绑定唯一键/索引可验证；不占用或修改simulation迁移 | 0.5 |
| PT-031 | 实现 | 补精确决定读取与观察subject/账户授权解析，保留原决定流程 | `S/investor/controls.py`仅新增owner-scoped精确读方法；新建`S/investor/decision_observation_inputs.py` | PT-028 | 不从决定列表分页猜对象；plan/option或公司决定与账户研究范围显式绑定；跨owner/账户拒绝；原决定不改写 | 0.75 |
| PT-032 | 实现 | 实现deadline冻结、交易日覆盖校验、本地自动核对授权与期满规则 | 新建`S/investor/decision_observation_deadlines.py` | PT-028, PT-031 | 上海截止/下一交易日20:00/5交易日窗口按fixture日历；未知覆盖422；授权仅本地已存证据且不过期扩权 | 0.75 |
| PT-033 | 实现 | 实现观察登记、新revision、取消、历史read-model与台账CAS | 新建`S/investor/decision_observations.py`的配置/读取服务 | PT-030, PT-031, PT-032, PT-017 | 初始revision/control=1；晚于决定显式标记；截止后不改predicate；取消epoch即时生效；原决定/旧观察保留 | 1 |
| PT-034 | 实现 | 实现专用BEGIN IMMEDIATE事务、跨观察分配余量和三重CAS | 新建`S/investor/decision_event_bindings.py`的allocate/锁帮助函数 | PT-030, PT-033 | 先取得writer锁再读影响分配状态；账户ledger/binding/观察control同验；总有效分配不超源事件；收据同事务 | 1 |
| PT-035 | 实现 | 实现撤销绑定、declare_complete指纹、账本更正链与完整声明失效 | `S/investor/decision_event_bindings.py`仅revoke/complete方法；新建`S/investor/decision_binding_state.py` | PT-034 | 完整声明保存推进后binding revision；任何绑定/账本变化失效；reversal不抹旧绑定；replacement不自动继承关联 | 1 |
| PT-036 | 测试 | 文件型SQLite双连接/双任务验证锁、分配、取消及更正竞争 | 新建`T/test_binding_concurrency.py`、`T/test_binding_state.py`、`T/test_observation_service.py` | PT-033, PT-035 | A13/A14/A15/A19通过；最后100股只有一个观察成功；同operation重放先于CAS；无内存DB伪并发证明 | 1 |
| PT-037 | Review | 独立Review三：绑定事务与完整声明、授权/时间边界 | 仅新建`D/review-03-bindings.md`；源码只读 | PT-036 | 检查专用session与外层事务兼容、锁前SELECT、同账户全局上限、user_report标识及epoch；有可复现竞争证据 | 0.5 |
| PT-038 | 回修 | 修复Review三，按指定并发交错重跑 | 仅PT-028至PT-036中finding点名的授权文件；热点由集成人员写；`D/review-03-fixes.md` | PT-037 | 独立reviewer核对竞争/声明失效/更正不变量全关；没有通过扩大锁范围至网络调用来规避问题 | 0.75 |

## D. S2核对执行、事件适配、界面与最终离线验收

| ID | 类型 | 任务与交付物 | 允许写文件或明确新文件范围 | 依赖ID | 可验证退出条件 | 估算人日 |
|---|---|---|---|---|---|---|
| PT-039 | 实现 | 实现交易份额、现金流和历史确认持仓三类事实谓词 | 新建`S/portfolio/fact_check_ledger.py` | PT-038 | 只取显式绑定且窗口有效事件；未声明完整保持unknown；零值有user_report来源；不拿当前snapshot充历史确认 | 0.75 |
| PT-040 | 实现 | 实现财务白名单resolver与两种timing_mode、冲突/重述/迟到诊断 | 新建`S/portfolio/fact_check_evidence.py`；新增`F/financial_timing/` | PT-028, PT-031, PT-038 | 经济期次<=due；当时可知的三时间均<=due；事后确认可使用截止后公开资料且late_data明确；非verified不采信 | 1 |
| PT-041 | 实现 | 实现核对纯装配、manifest/hash、不可变报告与previous_check链 | 新建`S/portfolio/fact_check.py`、`S/portfolio/fact_check_service.py` | PT-039, PT-040 | pass/fail/unknown与盈亏无关；同semantic input不重复报告；新数据新cutoff新calculation；旧结果保持且能标basis_changed | 0.75 |
| PT-042 | 实现 | 实现S2 dispatcher/handler、本地预算、租约、授权撤销、取消及有限补查；预留同session事件sink注入点 | 新建`S/portfolio/fact_check_jobs.py` | PT-032, PT-033, PT-041 | 50条批取/200队列上限；到期延迟、5交易日最多3补查；10秒/10谓词/200事件；lease/epoch失效不发结果；due及结果事件sink接收业务session，PT-045可通过注册注入而非另起事务 | 1 |
| PT-043 | 实现 | 实现S2观察、绑定、核对、取消及历史HTTP接口 | 新建`S/api/routes/decision_observations.py`、`S/api/schemas/decision_observations.py` | PT-035, PT-041, PT-042 | 设计登记与绑定样例通过DTO；手动核对202；原BusinessError detail协议；无模型/外部采集/交易执行入口 | 0.75 |
| PT-044 | 实现 | 实现账户级事件来源及InboxContribution.v1适配器，用EV契约fixture验证 | 新建`S/portfolio/study_events.py`、`T/test_study_event_contract.py`、`F/inbox_contributions.json`；只读EV contracts | BASE-02, PT-018, PT-028, PT-041 | subject严格account且核owner；exact_source_refs与source_revision一致；压力默认context；决定到期/结果attention按契约；无账本全文/备注泄露 | 0.75 |
| PT-045 | 集成 | owner=集成人员；等待EV-12后串行接S1/S2全部事件、设置/错误/调度/注册 | `S/api/app.py`、`S/api/deps.py`、`S/config.py`、`S/settings/registry.py`、必要`S/settings/loader.py`、`S/research/worker.py`、`S/scheduler/runner.py`、`S/research_events/registry.py`和`resolvers.py`仅本方向条目；`docker-compose.yml`仅kind列表 | EV-12, PT-020, PT-027, PT-042, PT-043, PT-044 | S2硬依赖EV就绪；S1/S2实际事件sink全部接入同session，结果+终态+事件同事务；无第二outbox；feature_disabled503；账户卡不触发公司复核 | 1 |
| PT-046 | 实现 | S2登记/修改/取消、deadline授权表单与显式事件分配/完整声明组件 | 新建`S/web/static/decision_observation_form.js`、`S/web/templates/decision_observations.html`、`S/web/static/pages/decision_observations.css` | PT-028, PT-036, PT-043 | 表单展示三重CAS与当时可知/事后确认；绑定后要求重新确认；无默认已执行/默认授权；pending请求稳定重放 | 1 |
| PT-047 | 实现 | S2核对详情/报告切换/更正链/缺口/到期状态组件 | 新建`S/web/static/decision_observation_results.js`、`S/web/static/decision_observations.js`；仅续写PT-046页面结果区/CSS | PT-041, PT-046 | late_data/公开与获取时间/完整声明失效/previous_check可见；旧决定和旧报告不被新数据覆盖；不展示收益排名 | 0.75 |
| PT-048 | 测试 | S2谓词/时间模式/worker与EV原子提交的局部故障注入 | 新建`T/test_fact_check.py`、`T/test_fact_check_evidence.py`、`T/test_fact_check_jobs.py`、`T/test_study_event_delivery.py` | PT-040, PT-041, PT-045 | A11/A12/A13/A15/A16/A17/A20通过；模拟迟到/重述/commit前后退出/丢回执/重复投递；相同输入无新报告或通知 | 1 |
| PT-049 | 集成 | owner=集成人员；串行接S2页面、回跳与API/Fake DOM测试 | `S/web/routes.py`、`S/web/static/app.js`、必要`S/web/static/nav.js`、`S/web/templates/base.html`及`portfolio_stress.html`链接；新建`T/test_observation_api.py`、`T/test_observation_web.py`、`T/test_observation_dom.py` | PT-024, PT-043, PT-045, PT-047 | 进程内ASGI测试认证/同源/503与历史读；Fake DOM验证声明失效、冲突保留表单、迟到标签；不用真实浏览器 | 1 |
| PT-050 | Review | 独立Review四：S1+S2发布前代码/契约与完整范围审查 | 仅新建`D/review-04-final.md`；业务源码只读 | PT-027, PT-038, PT-048, PT-049 | 审核A01—A21映射及EV事务、正式采信白名单、账户subject、所有开关和不可用降级；明确未做真实E2E | 0.75 |
| PT-051 | 回修 | 关闭最终Review阻断项及相邻回归 | 仅PT-001至PT-049中finding点名的授权文件；热点由集成人员串行写；`D/review-04-fixes.md` | PT-050 | 独立reviewer逐条签回；schema/接口变化同步fixture；未加入真实E2E、收益率或反事实收益来扩围 | 1 |
| PT-052 | 实现 | 补本地方向运维/权限/迁移/回退/已知限制与命令清单 | 新建`D/operations.md`、`D/offline-commands.md`、`D/unverified.md`；不改原已评审方案 | PT-027, PT-045, PT-051 | 区分取消、关开关、机械结账、历史读；迁移号引用实际值；真实使用/生产/性能外推均列未验证 | 0.75 |
| PT-053 | 测试 | 独立跑精确变更静态检查、已有相关回归、迁移与新模块检查 | 仅新建`D/static-regression.json`及对应日志；源码/测试只读 | PT-051, PT-052 | 所选pytest、ruff、mypy与Node语法/DOM命令有退出码；无相关测试skip替代通过；不触碰simulation和生产数据 | 0.75 |
| PT-054 | 验收 | 独立离线最终验收，逐项签收PS01—08与A01—21 | 仅新建`D/final-acceptance.md`、`D/final-results.json`、复现实验日志 | EV-12, PT-027, PT-048, PT-049, PT-053, PK-02 | 21场景各有样本/命令/结果；fixture事件与真实EV临时库兼容；PK helper验证实际S1/S2全部资产入wheel；源码无临时旁路；全部阻断项关闭，未宣称真实E2E | 1 |
| PT-055 | 验收 | 整理主代理交接包、实现边界、未验证事项与分阶段完成结论 | 仅新建`D/handoff.md`、`D/artifact-manifest.json` | PT-054 | 记录commit/diff范围/hash/迁移号/开关默认值/Review签回；分开S1、S2、跨方向待集成与真实使用未验证 | 0.5 |

## 原设计任务卡与原验收场景映射

| 原卡 | 本表实现与验证任务 | 阶段出口 |
|---|---|---|
| PS-01 契约/引用 | PT-001, PT-002, PT-005, PT-013 | PT-014, PT-015 |
| PS-02 模型/迁移/持久化 | PT-003, PT-004, PT-016, PT-017, PT-021 | PT-025, PT-026, PT-027 |
| PS-03 输入/AG22/行业/内核 | PT-006—PT-013 | PT-014, PT-015, PT-027 |
| PS-04 worker/API/局部事件 | PT-018—PT-021, PT-044, PT-045, PT-048 | S1 PT-027；EV接入PT-054 |
| PS-05 S1页面 | PT-022—PT-024 | PT-025, PT-026, PT-027 |
| PS-06 观察/绑定/并发 | PT-028—PT-038, PT-043, PT-046 | PT-037, PT-038, PT-054 |
| PS-07 核对/调度/事件/报告 | PT-039—PT-049 | PT-050, PT-051, PT-054 |
| PS-08 回归/运维/验收 | PT-013, PT-021, PT-024, PT-036, PT-048—PT-055 | PT-054, PT-055 |

| 原验收 | 主要验证任务 | 要保存的证据 |
|---|---|---|
| A01 同参重试/异参冲突 | PT-021, PT-036, PT-049 | 收据、run/result计数、409 detail |
| A02 head变化后的重放顺序 | PT-021, PT-036 | 旧成功收据与新operation旧CAS冲突 |
| A03 owner/账户/引用错配 | PT-013, PT-021, PT-049 | 404/422与无入队/无泄露断言 |
| A04 未来/陈旧价格和旧版本 | PT-013 | 冻结manifest、unsupported与partial样例 |
| A05 行业区间/多解/源改变 | PT-013 | csrc查询、副本hash、改变前后重放一致 |
| A06 分层覆盖/重复/未命中 | PT-013 | 手算金样与逐行冲击来源 |
| A07 极端/非法数值 | PT-013 | Decimal边界与拒绝超指数证据 |
| A08 缺现金/价/量与部分账户 | PT-013, PT-024 | Quantity null原因、DOM未补零断言 |
| A09 精度/费用/零净值 | PT-013, PT-027 | 未量化结果、费用一次、无除零、基准环境 |
| A10 停牌/历史方案/执行语义 | PT-013, PT-024 | 停牌诊断与DOM模型边界文本 |
| A11 取消/失效租约 | PT-021, PT-048 | 可控交错与失效提交被拒证据 |
| A12 崩溃/重复事件 | PT-021, PT-048 | S1本地原子性、EV同事务回滚及consumer去重 |
| A13 假日/日历/时区 | PT-036, PT-048 | 冻结calendar、UTC与上海边界样例 |
| A14 无/部分/错误绑定 | PT-036, PT-049 | unknown与分配拒绝、用户完整声明来源 |
| A15 更正与撤销 | PT-036, PT-048, PT-049 | 原事件/reversal/replacement、旧报告保留 |
| A16 迟到/重述/无效证据 | PT-048 | 三时间manifest、新cutoff、同hash无新增结果 |
| A17 权限/开关/容量 | PT-021, PT-048, PT-049 | epoch、授权过期、503/403区分、历史读取 |
| A18 不扩收益率/反事实 | PT-024, PT-049, PT-050 | 新API/DOM/依赖与计算method审查，不新增收益路径 |
| A19 全局超配与声明失效 | PT-036 | 文件型SQLite双连接锁交错、CAS及指纹失效 |
| A20 两种timing_mode | PT-048, PT-049 | 同报告两种模式结果、late_data与说明 |
| A21 正式采信边界 | PT-005, PT-013, PT-050 | publication/AG22/审批反向注入被拒 |

PT-054汇总全部映射，不能仅引用“pytest全绿”省略对应样本与assertion位置；不重复计入EV-12的基础建设工作量。

## 验证命令与证据格式

BASE-01确认可用解释器/Node及现有测试命令后，记录确切命令；本计划不立即执行下列候选命令。
新范围主要命令为`python -m pytest tests/portfolio_stress -m 'not network'`；必须启用BASE-03统一离线guard/allowlist，拦截真实provider/付费模型/外网/.env读取，不能只靠marker。
相邻回归包括`tests/test_portfolio_calculations.py`、`test_portfolio_plans.py`、`test_portfolio_valuation_binding.py`、`test_investor_controls.py`与有关publication/方法/设置测试；BASE-01逐路径确认后交集成人员执行。
`ruff check`与`mypy`只针对新改Python模块及必要相邻模块；全仓既有问题需单列基线，不可把新增失败归入历史问题。
前端沿现有Node Fake DOM测试方式执行ES模块导入、交互与`node --check`；Node不可用应阻塞该验收项，不能skip后称通过。
ASGI测试须显式注入临时库与安全Settings并mock启动初始化、目录同步和网关；不启动uvicorn/Docker/浏览器，不访问行情源。
迁移、双连接竞争和故障注入均使用临时目录与文件型SQLite；取消/lease/commit交错以barrier/hook控制，不靠长sleep碰运气。
证据记录`task_id,code_revision,fixture_hash,command,exit_code,assertion_path,result,limitations`；日志不含真实账户、API key或用户备注。
P95基准记录硬件、解释器、样本规模、冷/热口径及重复数，仅评价本地内核，不把ASGI或mock结果解释为生产性能。

## 阶段关卡、并行与失效处理

1. 基础关卡：PT-014 Review → PT-015回修；之后允许S1服务/界面分文件并行。
2. S1关卡：PT-025 Review → PT-026回修 → PT-027独立验收；EV未完成不影响S1本地交付。
3. S2绑定关卡：PT-037 Review → PT-038回修；PT-039/PT-040及独立EV契约adapter可分文件并行。
4. 完整关卡：EV-12 → PT-045实际共享接入；PT-050 Review → PT-051回修 → PT-053检查 → PT-054验收 → PT-055交接。

Review或验收发现阻断项，重新打开本阶段已有回修卡、记录新revision并重跑受影响检查；这是同卡迭代，不给静态依赖图增加反向边。
若回修超过1人日或需要未授权文件，主代理把finding拆成新增原子任务后更新依赖；不得默认让验收代理直接修业务代码。
共享热点不得由多个代理同时写；PT-004/PT-005/PT-020/PT-024/PT-030/PT-045/PT-049统一交同一集成人员排队，与其他方向的热点任务互斥。
S1与S2界面组件测试可以独立证明DOM契约和CSS规则，但不声称390/1440真实设备视觉验收或真实使用走查已经完成。
390/1440仅检查响应式样式、DOM结构和键盘交互；DOM/Fake DOM无真实布局计算，真实浏览器溢出和设备体验保留未验证，不列隐含补做任务。
S1阶段验收可先于事件接入；完整交付必须完成PT-045/PT-048/PT-054的S1/S2事件适配与契约验收，不留下“可选事件”尾项。
最终状态只可写“离线工程验收完成”及明确未验证清单；真实账户演练、真实数据质量与生产启用需后续独立任务，不作为本表隐含补做事项。
