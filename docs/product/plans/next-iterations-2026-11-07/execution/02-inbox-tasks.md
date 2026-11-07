# 每日论点变化收件箱：sub-agent 原子执行计划

状态：执行计划主审完成；下表全部任务状态统一为“尚未执行”；本轮未派发实现、修改业务代码或执行验收。
依据：[设计](../02-research-inbox.md)、[共同约定](../README.md)、[主审记录](../review.md)的最终版本。
设计基线为 `1f62fa4`；实施基线以 BASE-01 的工作区清单为准，不能据旧 hash 覆盖后续用户工作。

## 1. 范围、前置与派发规则

- 目标是完整实现 IN-01b、IN-02 至 IN-08；共享 IN-01 的通用基础由 EV-01 至 EV-12 单独交付，不在本计划重复实现。
- 外部前置为BASE-01（基线/工作区）、BASE-02（错误/引用/事件/时间契约）、BASE-03（离线隔离/fixture）、EV-01至EV-12及下述PK资产任务。
- 主审补充PK-01/PK-02共享资产打包任务；最终UI验收依赖PK-02并检查实际inbox模板/JS/CSS，避免源码可用但发行包缺页。
- EV-12 是共享基础完成门槛，交付事件三表、publisher、白名单 EventSourceRef、receipt/顺序/重试、专用机械 worker 骨架和注册扩展点。
- 不重写 `research_domain_event`、`consumer_receipt`、`projection_cursor`；收件箱使用共享事务接口，补业务效果，不复制另一套 delivery。
- 本计划处理核心五类生产者与 IN-01b 后处理；银行/组合具体生产者属于其方向，本文只交付 adapter 注册接口、合约 fixture 和拒绝规则。
- 实验产物不能混入正式事件。账户结果保留 account subject，不自动拆成股票，不调用公司 manual。
- 每个任务派发前，主代理冻结允许写文件、前置产物 hash、离线命令和禁止目录；不满足前置不派发。
- 同一文件有多个任务时按依赖串行；不同文件的任务才可并行。不得把“文件不同”当成事务/契约独立的证明。
- 标有 `owner=集成串行` 的任务由唯一集成人员执行；其他 sub-agent 只能提出该热点补丁，不直接同时写。
- 每段 Review 必须由未实现该段代码的独立 sub-agent 完成；Review 只写报告，不顺手修代码。回修后由原 reviewer 或另一独立 reviewer 复核。
- 回修估算是预留，不能以预留耗尽为由放过问题；有未关闭阻塞项就追加同边界任务，禁止虚报验收通过。

路径缩写：`P/`=`src/investment_agent/`；`TI/`=`tests/inbox/`；`EI/`=`docs/product/plans/next-iterations-2026-11-07/execution/evidence/inbox/`。
表中 `+` 表示新文件；路径集合是上限，不授权删除或重写未归属代码；任务报告统一包含 diff、离线命令、结果与未验证项。

共享 EV 模块固定为 `P/research_events/{contracts,canonical,resolvers,repository,delivery,maintenance,registry,health}.py` 与 `P/storage/models/research_events.py`。
IT 不允许改其三表或运行骨架；注册补丁只在 IT-034/035 由集成人员修改 `research_events/registry.py`，共享 API 不兼容退回 EV 交付方修复。
`api/deps.py`、`api/app.py`、settings、models 导出、Alembic、worker/CLI/scheduler、Docker、全局 Web 导航均列为串行热点。
执行细化已确定：EV projection_cursor提供`active_generation:UUID|null`、`rebuild_state:JSON|null`，仅research_inbox.v1使用。IT-001定义闭合InboxRebuildState(candidate_generation、domain_watermark、journal_watermark、last_replayed_sequence、state)，通过EV cursor repository与cursor.revision CAS写入，不新增第四业务表。
正常影子重建只重放不可变journal（包含用户操作和完整撤回效果），不重新消费domain事件制造待办；切换前进行中分片必须全部结清，不能改冻结manifest或跨代搬剩余目标。

并行用户工作保护：`P/simulation/`、`P/storage/models/simulation.py`、`tests/simulation/`、`alembic/versions/061_simulation.py` 与模拟盘设计不在允许写范围。
`P/storage/models/__init__.py` 已有并行用户改动，只能由集成人员基于最新内容追加本方向导出并保留全部现有差异；不能还原、覆盖或夹带提交。
所有迁移使用 `next_revision` 占位，实施时按真实 head 串行分配；不占用/修改061，不硬改其他方向迁移或 current-head 测试掩盖冲突。

## 2. 离线验证边界

允许：临时文件 SQLite、进程内 ASGI、FunctionModel/mock-provider、组件 DOM、JS语法/静态构建、局部故障注入与纯本机虚拟时钟。
禁止：真实浏览器全栈、Playwright/CDP 联真实服务、Android 模拟器/设备 E2E、真实模型/行情/provider、付费请求、真实账户、生产设置/数据库与外发通知。
不得用 `-m not network` 单独证明隔离；BASE-03 的 socket/HTTP/send 拦截、无真实凭证和临时路径断言必须实际启用。
DOM统一使用BASE-03提供的bootstrap、Node内建`node:test`执行`.mjs`，从项目既有`mobile`依赖解析jsdom；本方向不安装包、下载浏览器或另建DOM运行器。
测试分层：producer+EV append事务、EV callback+投影事务、独立路由ASGI+服务替身、组件DOM+固定响应fixture分别验证；禁止用单个测试串通source→API→DOM全产品链路。
测试若意外请求外部地址、访问 `.dev/data` 或真实用户目录，立即失败；不能把该请求放行或把失败改为 skip。
原设计场景19的真实浏览器/Android与场景23的真实模型并发改为离线替代验证，明确保留真实效果未验证，不阻塞本计划工程验收。
真实两周试用、人工“值得查看”比例、真实视觉安全区、生产 p95、部署/上线均不属于本计划验收，不得借工程通过宣称已验证。

## 3. A 段：业务契约、存储与核心生产者

| ID | 类型 | 任务与交付物 | 允许写文件或新文件范围 | 依赖ID | 可验证退出条件 | 估算人日 |
|---|---|---|---|---|---|---|
| IT-001 | 实现 | 冻结InboxContribution、受限subject、三种revision、InboxRebuildState与adapter合同；接入共用离线guard | +`P/research_inbox/__init__.py,contracts.py`；+`TI/conftest.py,test_contracts.py`；+`EI/contracts.md` | BASE-01, BASE-02, BASE-03 | strict int/UUID、subject互斥、case仅listing、UTC/Decimal正确；rebuild闭合DTO完整，head只走EV既有cursor列；TI/conftest显式启用BASE-03共享guard，外连诱饵失败，无第二套guard | 0.5 |
| IT-002 | 实现 | 定义entry/journal/entry_source三模型，并为现有ReviewCycle新增显式标量cycle_event_revision | **owner=集成串行**：+`P/storage/models/research_inbox.py`；`P/storage/models/monitoring.py`仅ReviewCycle版本列/约束；+`TI/test_models.py` | IT-001 | journal唯一键含event+ordinal；cycle_event_revision为非空INTEGER且≥0、Python/服务端默认0；不把版本塞入可变payload JSON；不实现EV三表 | 0.75 |
| IT-003 | 集成 | 注册业务模型并生成本方向next_revision迁移：三业务表及既有cycle版本列 | **owner=集成串行**：`P/storage/models/__init__.py`；新分配的`alembic/versions/<next_revision>_research_inbox.py`；+`TI/test_migration.py` | IT-002, EV-12 | 既有investor_review_cycle行版本置0、payload/state/task_id不改、不伪造旧事件；首次真实变更发v1；fresh/升级/空库回退通过；061/simulation哈希与单head保持，有数据不删除回退 | 0.75 |
| IT-004 | 实现 | entry/source/journal仓储：原子写入、不可变完整entry_view、稳定UUID、同事件效果ordinal与原操作结果查询 | +`P/research_inbox/repository.py`；+`TI/test_repository.py` | IT-003 | 回滚不留半条；同generation/event/ordinal防重；owner/引用存在性由服务验证；不依赖SQLite FK开关保证安全 | 0.75 |
| IT-005 | 实现 | 核心事件→Contribution业务适配与精确关系核查；定义topic/decision扩展注册接口及合约fixture | +`P/research_inbox/adapters.py,adapter_registry.py`；+`TI/test_adapters.py`；+`TI/fixtures/adapter_contracts.json` | IT-001, EV-12 | 调用EV resolver而非自造ObjectRef；round/comparison独立整数ID；无reviewed_relation_ref只unlinked/context；银行/账户事件fixture可校验，未交付resolver不得注册为可用 | 0.75 |
| IT-006 | 实现 | 挂接研究快照事件生产者，覆盖ifind_chain success与已持久snapshot的partial；冻结原后处理eligibility和设置引用 | `P/storage/repositories/research_snapshot.py`、`P/research/ifind_chain.py`；+`TI/test_snapshot_producer.py` | IT-005, EV-12 | create/mark_partial/mark_success与append同session同提交；载荷不猜最终状态；backfill/缺原授权依据不能转为自动评审；恢复不重新调用解释模型 | 0.75 |
| IT-007 | 实现 | 挂接评审定稿事件，位于save_comparison后、旧best-effort提醒try前 | `P/review_panel/service.py`；+`TI/test_round_producer.py` | IT-005, EV-12 | 原子状态抢占唯一生产；append失败回滚定稿/结算；round已终态也能靠事件恢复投影，不重跑席位 | 0.5 |
| IT-008 | 实现 | 使用已迁移cycle_event_revision列挂接ReviewCycle合并/dispatch/dirty/settle事件 | `P/investor/monitoring.py`；+`TI/test_cycle_producer.py` | IT-003, IT-005, EV-12 | 每次实质变更按标量版本CAS递增，cycle更新+递增+事件同事务；v0既有行首次变更v1；纯重放不增版；同版本异载荷不能覆盖；原预算/授权不变，不写隐式JSON版本 | 0.75 |
| IT-009 | 实现 | 挂接正式意见有效性事件；源指向RecommendationEvent UUID与精确revision | `P/investor/publication.py`；+`TI/test_validity_producer.py` | IT-005, EV-12 | 限制/撤回/替代/续期同事务；不能恒指向recommendation revision=1掩盖事件差异；原发布白名单与外发outbox不扩权 | 0.5 |
| IT-010 | 实现 | 挂接case控制事件，与case CAS及原操作收据同提交；避免从通知反推事实 | `P/investor/controls.py`；+`TI/test_control_producer.py` | IT-005, EV-12 | archive/reopen/pause/resume/mute/unmute均有精确事件；失败原操作回滚；静音仍保留领域事件；不改变原authority入口 | 0.5 |
| IT-011 | 测试 | 汇总生产者故障矩阵：事务回滚、提交后退出、重复源、success/partial、不重做模型 | +`TI/test_producer_faults.py`；+`TI/fixtures/source_cases.json`；+`EI/a-tests.md` | IT-004, IT-006, IT-007, IT-008, IT-009, IT-010 | 临时SQLite与mock模型计数证明5类生产者原子性；EV重复/hash冲突语义在本方向接缝正确；无真实provider/send | 0.75 |
| IT-012 | Review | 独立审查A段diff：契约、三表、引用/owner、五生产者事务与原权限兼容 | 只读代码；+`EI/review-a.md` | IT-011 | 报告逐项标文件/证据/严重度，检查current模型/迁移用户差异，所有阻塞项给回修范围；未实现者担任reviewer | 0.5 |
| IT-013 | 回修 | 按A段Review逐条回修与补缺失反例；禁止趁机扩大范围 | 仅IT-001至011已登记精确文件；+`EI/fix-a.md`；热点仍**owner=集成串行** | IT-012 | 每个发现有修复diff/测试证据，或主代理明确接受且不违背设计的处理；阻塞项不得标为“以后再说” | 0.5 |
| IT-014 | 验收 | 独立复核A段，冻结生产者/仓储接口与source fixture版本 | +`EI/accept-a.md`；必要时仅新增`TI/test_a_regressions.py` | IT-013 | 定向离线套件通过，Review阻塞项0；源码与061/simulation保护清单一致；形成供下一段消费的精确接口清单 | 0.25 |

IT-006至010写集合分离，可在契约冻结后并行；迁移/导出始终串行。IT-008与后续monitoring相关修改不可重叠派发。
存储核对：当前`P/storage/models/monitoring.py:17–23`的ReviewCycle只有id/case_id/task_id/state/payload，没有标量版本；因此IT-002负责模型、IT-003负责实际列迁移，IT-008只使用已验明的列。

## 4. B 段：原后处理持久受理与投影/分片

| ID | 类型 | 任务与交付物 | 允许写文件或新文件范围 | 依赖ID | 可验证退出条件 | 估算人日 |
|---|---|---|---|---|---|---|
| IT-015 | 集成 | 抽取两个同session后处理内部函数；保留旧wrapper，提供typed accepted/ignored(reason) | **owner=集成串行**：`P/research/worker.py`、`P/review_panel/triggering.py`；+`TI/test_postprocess_sessions.py` | IT-014 | consumer调用内部服务不自开session；wrapper兼容；不在事务调用模型；原trigger成功/冷却/开关/额度策略不变 | 0.75 |
| IT-016 | 实现 | watchlist_sync.v1消费业务：原同步更新与EV receipt同事务，恢复只同步已有结果 | +`P/research_inbox/postprocess_watchlist.py`；+`TI/test_postprocess_watchlist.py` | IT-015, EV-12 | 旧run不能覆盖新watch head；源不存在/不匹配明确拒绝；失败回滚同步与回执；重放不建模型任务 | 0.5 |
| IT-017 | 实现 | review_admission.v1业务：原接受依据+当前控制双查，原入队与EV receipt原子 | +`P/research_inbox/postprocess_review.py`；+`TI/test_postprocess_review.py` | IT-015, EV-12 | 非backfill且原已同意才可受理；partial仍走原success-only限制；开关/预算拒绝给原因；重复只读原round/run，不再付费 | 0.75 |
| IT-018 | 集成 | 切换匹配新事件的finalize后处理至durable受理；历史无事件旧路径兼容 | **owner=集成串行**：`P/research/worker.py`；+`TI/test_finalize_dispatch.py` | IT-016, IT-017 | 不双写受理；原Markdown文件通知仍best-effort且测试/文档明确；未声称整个finalize已可靠 | 0.5 |
| IT-019 | 测试 | 后处理双writer/失联故障：已受理未回包、开关撤销、重启及partial边界 | +`TI/test_postprocess_faults.py`；+`EI/b-postprocess-tests.md` | IT-018 | 独立SQLite writers下同步/入队至多一次；原任务恢复不调用mock模型；原同意缺失不能因后来开关打开而补启动 | 0.75 |
| IT-020 | 实现 | 单subject事项归组与核心投影回调，使用EV事务接口写entry/source/journal | +`P/research_inbox/projector.py`；+`TI/test_projector.py` | IT-004, IT-005, IT-014, EV-12 | cycle/change/round/账户observation归组稳定；noisy/历史明确ignored；revision、content_revision、read_content_revision分开；无模型调用 | 0.75 |
| IT-021 | 实现 | source关系与实时有效性：精确反证关系、旧版本支配、撤回/不可读与新建卡验证 | +`P/research_inbox/sources.py`；+`TI/test_sources.py` | IT-020 | 无可信撤回记录只source_unavailable；单源失效不吞独立证据；全部失效withdrawn；后建卡不能重新挂旧失效来源 | 0.5 |
| IT-022 | 实现 | 冻结多事项effect_manifest：generation、排序目标、来源版本/hash及完整hash | +`P/research_inbox/effects.py`；+`TI/test_effect_manifest.py` | IT-021, EV-12 | 首次冻结后不可重新查询改目标；ordinal稳定；>10000目标显式隔离不截断；消费参数只用EV receipt既定字段不改三表 | 0.75 |
| IT-023 | 实现 | 分片效果应用：每片≤50，投影+journal+effect_cursor同事务，全部完成才applied | +`P/research_inbox/shards.py`；+`TI/test_shards.py` | IT-022 | 中断后沿冻结清单继续；同链后序阻塞、独立对象继续；唯一ordinal防重；scope控制journal不改写全部历史卡 | 0.75 |
| IT-024 | 测试 | 投影/分片竞争与故障注入：片中失败、片后丢响应、毒事件及多源交错 | +`TI/test_projection_faults.py`；+`TI/test_shard_concurrency.py`；+`EI/b-projection-tests.md` | IT-020, IT-021, IT-022, IT-023 | 证明投影/receipt/cursor原子，effect_manifest/hash不漂移，quarantined不假applied，同对象后序不会越过，独立对象有进展 | 0.75 |
| IT-025 | Review | 独立审查B段及A/B接缝：原授权、同session、冻结分片、journal效果唯一性 | 只读代码；+`EI/review-b.md` | IT-019, IT-024 | 专门核查已提交任务不重做模型、partial是否越界、片后回执一致性；所有发现可定位 | 0.5 |
| IT-026 | 回修 | 修复B段Review，补最小故障反例与兼容回归 | 仅IT-015至024登记文件；确需A段文件须主代理先给逐文件补充范围；+`EI/fix-b.md`；热点**owner=集成串行** | IT-025 | 阻塞项逐条关闭；不能通过改EV通用契约/放宽原授权修测试 | 0.5 |
| IT-027 | 验收 | 独立验收B段并冻结consumer callback/manifest接口 | +`EI/accept-b.md`；必要时仅新增`TI/test_b_regressions.py` | IT-026 | 离线故障与定向旧trigger回归通过；durable接受和Markdown限制分别报告；分片可恢复证据齐全 | 0.25 |

IT-016/017可并行；IT-020可与015至019并行；021→022→023严格顺序。禁止在模型provider任务内执行消费回调。

## 5. C 段：控制、操作、API与恢复维护

| ID | 类型 | 任务与交付物 | 允许写文件或新文件范围 | 依赖ID | 可验证退出条件 | 估算人日 |
|---|---|---|---|---|---|---|
| IT-028 | 实现 | listing/account可见性与动作策略，case关联、episode、静音补看与观察池移除规则 | +`P/research_inbox/policy.py`；+`TI/test_policy.py` | IT-027 | listing无case不造case；同股已有case不能走listing绕静音；account不manual、不拆股；归档/重开不复活旧事项；当前控制拒绝不依赖投影追平 | 0.75 |
| IT-029 | 实现 | 读/已了解/延期操作服务：owner先查、原回执先重放、新动作再实时控制与CAS | +`P/research_inbox/service.py`；+`TI/test_mutations.py` | IT-028 | 原UUID同hash返回原结果、异参409；新证据撞延期一方409；已读不结束领域问题；account拒绝acknowledge；关闭功能仍可读授权历史回执 | 0.75 |
| IT-030 | 实现 | request_review事务接缝：原manual与in_review/journal原子、任务dirty关联 | +`P/research_inbox/review_actions.py`；+`TI/test_review_actions.py` | IT-029 | expected_case_ref/episode/source实时验证；拒绝不置in_review；同UUID不重复队列；运行中新证据不另建任务；无case/account拒绝 | 0.75 |
| IT-031 | 实现 | 延期到期与新内容竞争的机械时钟回调 | +`P/research_inbox/clock.py`；+`TI/test_snooze_clock.py` | IT-029 | 到期key确定、至多一次；新内容取消旧延期；读请求不写时钟；虚拟UTC覆盖跨日/过期/≤30日限制，无sleep | 0.5 |
| IT-032 | 实现 | 稳定水位列表、详情与journal增量读模型、opaque cursor | +`P/research_inbox/queries.py,cursors.py`；+`TI/test_queries.py` | IT-028, IT-004 | sequence≤W逐事项最新view分页；generation/owner/filter绑定；实时隐藏不误判空页；旧行更新、remove/scope控制占增量序号；旧cursor410 | 0.75 |
| IT-033 | 实现 | 新inbox路由与请求DTO：列表/详情/changes/actions/operations | +`P/api/routes/research_inbox.py`；+`TI/test_api.py` | IT-029, IT-030, IT-031, IT-032 | 进程内ASGI验证认证/CSRF/对象可见性/回执顺序；固定BusinessError detail；首屏health/partial/allowed_actions正确，无自造错误协议 | 0.75 |
| IT-034 | 集成 | 串行注册路由、错误码、默认关闭设置与白名单业务adapter/consumer | **owner=集成串行**：`P/api/app.py,api/deps.py,settings/registry.py,research_events/registry.py`；+`TI/test_registration.py` | IT-033, IT-016, IT-017, EV-12 | feature_disabled明确503；403/410/503新码入公共表，旧writes_disabled/权限码不变；开关只阻止新操作，历史/机械结账可用；未实现topic/decision来源不假注册 | 0.75 |
| IT-035 | 集成 | 实现inbox机械业务handler与注册：50新事件/20重试/20延期，复用EV隔离kind/lease；向既有调度tick注册本方向回调 | **owner=集成串行**：+`P/research_inbox/maintenance.py`；`P/research_events/registry.py`仅扩展注册；`P/scheduler/runner.py`仅inbox业务回调接线；+`TI/test_maintenance.py` | IT-023, IT-030, IT-031, IT-034, EV-12 | 不新增EV已有kind/CLI/worker/Compose服务；30秒单活与lease由EV提供，回调预算有界、失lease不提交；禁UI不停；无deliver_one/模型；阻塞mock研究不占机械容量 | 1 |
| IT-036 | 实现 | 不可变journal影子重放、bootstrap、双水位追平与cursor head原子切换 | +`P/research_inbox/rebuild.py`；+`TI/test_rebuild.py` | IT-023, IT-029, IT-032 | 不重消费domain事件、不复制request_id，receipt身份不变；InboxRebuildState通过EV cursor repo/CAS持久化，其他consumer列仍null；分片先结清再切换，旧cursor410，manifest不变 | 1 |
| IT-037 | 实现 | 有界manifest导入/对账/机械修复CLI服务，保持原时间和backfill标记 | +`P/research_inbox/backfill.py`；+`tools/research_inbox_backfill.py`；+`TI/test_backfill.py` | IT-021, IT-034 | 默认dry-run/显式manifest限定ID/期间/条数/水位；已有源键事件核对跳过，不能改其payload；未知原时间排除；旧cycle仅migration_snapshot；不外发/启模型/唤醒历史；只测临时库 | 0.75 |
| IT-038 | 实现 | inbox健康/lag/重试隔离/重建阻塞读模型，连现有健康页和今日聚合 | **owner=集成串行**：+`P/research_inbox/health.py`；`P/api/routes/data_health.py`、`P/web/static/health.js`、`P/web/templates/health.html`仅本卡区块；+`TI/test_health.py` | IT-035, IT-036, IT-037 | scanned≠全部成功；retry/quarantined/同链阻塞/未接adapter可见；无数据计数保持unknown；worker离线lag可见；健康接口只读 | 0.75 |
| IT-039 | Review | 独立审查C段：访问/重放顺序、错误/开关、稳定分页、影子代际、维护与有界导入 | 只读代码；+`EI/review-c.md` | IT-034, IT-035, IT-036, IT-037, IT-038 | 检查真实控制优先、先结清分片、固定manifest、历史不付费；无需浏览器/真实服务即可定位问题 | 0.5 |
| IT-040 | 回修 | 回修C段及新增离线反例，不触碰共用基础实现 | 仅IT-028至038登记文件；+`EI/fix-c.md`；api/deps/注册/设置/worker/scheduler等热点**owner=集成串行** | IT-039 | 阻塞问题0；generation失败不以清表掩盖；不修改EV三表、061或simulation；修改接口先更新被冻结契约证据 | 0.75 |
| IT-041 | 验收 | 独立C段离线验收：ASGI、临时迁移、重建/分片交错、导入/关闭恢复 | +`EI/accept-c.md`；+`TI/test_c_acceptance.py` | IT-040 | API/事务/时钟故障集通过；有效数据不删除；feature_disabled503/历史可读可证明；无网络尝试或真实路径写入 | 0.5 |

IT-031/032可分文件并行；IT-036/037可在其前置后并行；IT-034/035与其他方向同名热点必须由主代理统一串行排队。
IT-035只补业务注册，不重做EV-08/09；EV既有配置不足属于交付接口问题，退回共享基础而非在inbox旁建另一个worker。

## 6. D 段：Web组件、集成接线与最终离线验收

| ID | 类型 | 任务与交付物 | 允许写文件或新文件范围 | 依赖ID | 可验证退出条件 | 估算人日 |
|---|---|---|---|---|---|---|
| IT-042 | 实现 | 完整列表/筛选/计数/分页/增量状态的DOM组件与模板 | +`P/web/templates/inbox.html`；+`P/web/static/research_inbox_list.js`；+`TI/test_inbox_list_dom.mjs` | IT-032, IT-041 | mock fetch驱动loading/empty/error/partial、upsert/remove/scope_remove；保持过滤/滚动标识；不依赖真实HTTP服务或浏览器 | 0.75 |
| IT-043 | 实现 | 详情/证据/处理历史/原UUID恢复/响应式样式与页面控制器 | +`P/web/static/research_inbox_detail.js,research_inbox.js,pages/inbox.css`；+`TI/test_inbox_detail_dom.mjs` | IT-029, IT-030, IT-041 | account/listing按钮白名单、失效来源禁动作、403/409/429/503保留输入、未知提交只原UUID恢复；390/1440样式规则与布局约束静态检查，不宣称实际视觉通过 | 0.75 |
| IT-044 | 集成 | 串行接入Web路由/导航/今日前三项、健康入口与内嵌链接 | **owner=集成串行**：`P/web/routes.py,web/templates/base.html,web/templates/overview.html,web/static/overview.js,api/routes/dashboard.py`；+`TI/test_web_wiring.py` | IT-042, IT-043, IT-038 | ASGI模板渲染/转义、嵌入标识、feature关闭隐藏新入口不禁历史读取；今日事项与旧review通知不重复；不改mobile壳 | 0.75 |
| IT-045 | 测试 | UI组件联合fixture回放：所有状态、subject、分页/更新/断线/回执恢复 | +`TI/test_inbox_ui_contract.mjs`；+`TI/fixtures/ui_cases.json`；+`EI/d-ui-tests.md` | IT-044 | BASE-03 bootstrap+Node内建test执行.mjs，jsdom仅解析mobile既有依赖；无安装包/浏览器/服务端口；构建检查通过；DOM无布局计算，像素/触屏/系统返回未验证 | 0.75 |
| IT-046 | 测试 | 分层离线场景证据汇总与局部服务接缝故障注入，不建立全产品链路测试 | +`TI/test_offline_scenarios.py`；+`TI/test_rebuild_shard_faults.py`；+`EI/d-scenario-tests.md` | IT-041, IT-045 | 24场景分别由producer+append、callback+投影、路由ASGI+服务替身、DOM固定fixture证明；单个测试不得串source→API→DOM；重建/分片仅局部SQLite服务；topic/decision仅fixture，跨方向接线留后续 | 1 |
| IT-047 | Review | 最终独立审查D段及整条源→回执→投影→操作路径、所有覆盖证据与范围 | 只读本方向及EV公开接口；+`EI/review-d.md` | IT-046 | 复核四段发现闭环、状态机一致性、跨方向事件名、残留best-effort限制、禁网和用户工作保护；不得用测试数量代替语义审查 | 0.75 |
| IT-048 | 回修 | 回修最终Review，补离线反例与文档差异，不绕门槛或加真实E2E | 仅IT-001至046已登记文件的明确问题子集；+`EI/fix-d.md`；所有热点**owner=集成串行** | IT-047 | 每个问题有对应diff/测试/复核；若共享EV接口有问题交回其owner，不直接扩大inbox写集合；未关闭阻塞项禁止进入最终验收 | 0.75 |
| IT-049 | 验收 | 冻结代码后做完整离线工程验收与独立复核；按新变更选取必要旧回归 | +`EI/offline-acceptance.md`；必要时仅新增`TI/test_final_regressions.py` | IT-048, PK-02 | TI套件、相关原触发/监测/API/Web静态回归、Ruff/mypy/JS语法及已有构建入口通过；用PK helper检查实际inbox模板/JS/嵌套CSS均在wheel中；临时迁移单head与diff检查通过；0意外网络/真实路径写入 | 0.75 |
| IT-050 | 验收 | 主代理核对任务/场景矩阵、改动归属和未验证范围，形成可移交结果 | +`EI/handoff.md`；本计划仅完成记录区 | IT-049 | 50任务状态/依赖/证据齐全，4段独立Review与回修验收闭环；061/simulation/真实设置保持；跨方向集成、真实视觉/性能/试用明确未验证，不阻塞工程交付 | 0.25 |

实现sub-agent不得编造“移动布局通过”；DOM/样式检查只能证明组件和静态约束，实际Android/WebView呈现须以后单独授权验证。

## 7. 原 IN 任务到原子任务的覆盖

| 原任务 | 原子任务/外部依赖 | 覆盖说明 |
|---|---|---|
| IN-01 共享持久事件 | EV-01至EV-12；IT-001/005/006为消费约束与生产挂接 | 不重复三表/publisher/receipt/worker基础；EV-12通过才使用事务接口 |
| IN-01b 原后处理受理 | IT-015至019、025至027 | 同session、原同意与当前控制、幂等接受；Markdown通知仍best-effort |
| IN-02 源生产者/历史对账 | IT-006至011、037、046 | 五类源完整挂接、success/partial与有界backfill |
| IN-03 模型/归组 | IT-001至005、020至024 | 三表、两个subject、关系/来源有效性、多事项分片 |
| IN-04 操作/控制 | IT-028至031、033、041 | UUID/CAS/可见性/原manual/延期/当前控制 |
| IN-05 API/游标 | IT-032至034、041、044 | W快照分页、旧行增量、错误状态表与历史读取 |
| IN-06 维护/恢复 | EV-08/09骨架，IT-035至038、041 | inbox业务handler、注册、重建、分片结清、lag与安全补记 |
| IN-07 Web/移动 | IT-042至045 | 完整Web功能与内嵌标识；仅DOM/模板/静态样式/构建，真实E2E不执行 |
| IN-08 联合验收 | IT-011/019/024、四段Review/回修/验收、046至050 | 工程场景全覆盖；银行/组合实际生产者联合接线由主代理后续安排 |

## 8. 设计24场景覆盖与本轮替代方式

| 场景 | 主要实现任务 | 测试/验收任务 | 离线证明与边界 |
|---|---|---|---|
| 1 源事务失败 | IT-006至010 | IT-011, IT-014 | 事务注入失败，源与事件共同回滚 |
| 2 定稿后进程终止 | IT-007, IT-020 | IT-011, IT-024 | 本地session/进程中断与恢复，只消费已有事件，mock模型计数不增 |
| 3 重复/异参源 | IT-005至010 | IT-011, IT-024 | 使用EV唯一键/hash合同，不覆盖旧源 |
| 4 消费中断/双writer | IT-020, IT-023 | IT-024, IT-027 | 两个独立临时SQLite session，投影/journal/receipt/cursor原子 |
| 5 毒事件/瞬时失败 | IT-020, IT-023, IT-035 | IT-024, IT-041 | 同链阻塞、独立对象进展、重试隔离可见 |
| 6 无case观察 | IT-005, IT-028 | IT-041, IT-045 | 无自动建档、无manual越权 |
| 7 旧行新增内容 | IT-020, IT-032 | IT-041, IT-045 | journal增量upsert提升内容revision，DOM可接收 |
| 8 新证据撞延期 | IT-029, IT-031 | IT-041, IT-046 | CAS冲突及确定性虚拟时钟，无sleep |
| 9 旧确认/丢回执 | IT-029, IT-030 | IT-041, IT-045 | 同UUID原结果、异hash拒绝、不重复队列 |
| 10 静音/取消静音 | IT-010, IT-028 | IT-041, IT-045 | scope过滤、suppressed历史、显式补看不暗重放 |
| 11 归档/重开 | IT-010, IT-028 | IT-041, IT-046 | 旧episode不复活、当前控制优先 |
| 12 暂停/预算不足 | IT-017, IT-030 | IT-019, IT-041 | mock准入拒绝保持open、不改grant |
| 13 源撤回/部分失效 | IT-021至023 | IT-024, IT-046 | 独立来源保留、全部失效withdrawn、不可读不伪造撤回 |
| 14 partial复核 | IT-008, IT-020, IT-030 | IT-041, IT-045 | typed部分结果，界面不写已解决 |
| 15 稳定分页/控制变化 | IT-028, IT-032 | IT-041, IT-046 | 固定W、筛选hash、实时隐藏与增量补全 |
| 16 影子重建 | IT-036 | IT-041, IT-046 | bootstrap/双水位/分片先结清/旧cursor410；不扩张为新算法迁移 |
| 17 禁UI/回滚升级 | IT-034至037 | IT-041, IT-049 | 临时开关/临时库，不删历史；旧代码间隙有manifest；不生产部署 |
| 18 恶意专题/决定适配 | IT-005, IT-028 | IT-041, IT-046 | fixture拒owner/subject/未知schema/虚假已评审关系 |
| 19 浏览器/Android内嵌 | IT-042至044 | IT-045, IT-049 | **替代**：ASGI模板+组件DOM+响应式静态约束；真实浏览器/设备视觉和返回手势未验证 |
| 20 不越权 | IT-006至010, IT-017, IT-028至030 | IT-046, IT-049 | 全局拦截模型/HTTP/send与真实路径；不得启动生产或外发 |
| 21 整账户决定卡 | IT-005, IT-028, IT-043 | IT-041, IT-045 | account保留身份、不拆股、不manual、原控制contract fixture |
| 22 多事项撤回 | IT-022, IT-023, IT-036 | IT-024, IT-046 | 50效果分片、>10000隔离、固定manifest/hash、片与切代交错 |
| 23 长研究/错误协议 | IT-034, IT-035 | IT-041, IT-046 | **替代**：阻塞mock任务与独立机械回调、虚拟调度和ASGI错误；不宣称真实模型负载p95 |
| 24 原后处理恢复 | IT-015至018, IT-037 | IT-019, IT-027, IT-046 | 原受理至多一次，partial不放宽success-only，backfill不付费/不外发 |

## 9. Review门禁、并行切片与移交

A门禁 IT-012→013→014：先证明源/存储/引用正确，再进入新动作；B门禁 IT-025→026→027：先证明受理/分片正确，再接用户操作。
C门禁 IT-039→040→041：先证明控制/分页/恢复正确，再接UI；D门禁 IT-047→048→049→050：整条离线路径最终独立复核。
Review报告至少包含：允许写范围遵守、具体事务边界、当前owner/权限检查、版本/时间/ID一致性、失败恢复、scope排除与测试证据。
无阻塞发现时回修任务仍需记录“无代码回修”及复核结论，不可省略验收；有问题时回修后重跑受影响检查，不机械重复已无变化的全量套件。

可并行切片：A段五个producer；B段watch/review消费者与单卡投影；C段查询/延期/恢复导入的不同文件；D段list/detail组件。
必须串行：共享注册/错误/设置/导出/Alembic/worker/CLI/scheduler/Docker/全局导航；各段回修与同文件后继任务；generation切换与未结清分片。
跨银行/组合不写其源文件或强依赖其任务ID；以schema fixture和可注入resolver交付适配合同，真实生产接线属于主代理跨方向集成清单。

最终交付包含代码diff、离线命令与日志、24场景证据索引、四段Review/回修/验收、迁移兼容说明、用户工作保护对账、残留限制。
特别声明：未修复的Markdown文件通知best-effort、真实浏览器/WebView视觉、真实provider研究质量、真实吞吐/p95及生产试用均独立列出。
上线、真实采集/模型/账户和试用不是本计划验收阻塞，也不因本文工程验收得到授权。

## 10. 计划自检与估算

共50个连续IT任务；所有依赖只能指向前序IT或允许的BASE/EV外部ID，无环；估算均为单个数值，包含4段Review与明确回修/验收。
共享EV基础只计一次，不计入本表人日；银行/组合生产者和主代理跨方向最终联合接线也不计入本表。
本表合计33.25人日，包含细粒度接口/故障验证、四段独立Review、回修预留与验收；EV基础不重复计入。该详细容量高于早期概念估算，不能隐去回修/Review让总数看似一致。
任务可并行不等于日历工期等比例缩短；实际全批工期还包含共享EV与其他方向接口等待，热点串行和未通过门禁优先。
本轮只验证计划ID/依赖/覆盖/写范围；尚未实现任何新DTO、表、API、消费者、测试或真实产品效果。
