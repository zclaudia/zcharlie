# 共享基础与执行准备任务

状态：待执行。本文只规划实现，不记录实现已完成。依赖[总执行计划](../execution-plan.md)；原设计依据为[共同约定](../README.md)与[收件箱设计](../02-research-inbox.md)。估算单位为人日，包含任务内开发自测；独立Review、回修与验收另外列卡。

## 写入边界与交付接口

共享事件代码统一放在`src/investment_agent/research_events/`：`contracts.py`、`canonical.py`、`resolvers.py`、`repository.py`、`delivery.py`、`maintenance.py`、`registry.py`、`health.py`。数据库模型为`storage/models/research_events.py`。收件箱的投影、分片目标选择、journal、用户操作和重建业务属于IT任务，不在此重复实现。

EV交付的最小接口是：接收调用方session的`append_event`；精确来源解析器；同session的receipt/cursor操作；白名单consumer注册；一次有界机械tick；健康摘要。所有写方法禁止自行提交外层事务。回调可产生领域投影，但不允许任意脚本、网络或模型调用。

维护进程只claim `research_event_maintenance`，不进入原Agent的研究维护或`deliver_one`。IT实现其收件箱业务handler；银行/决定方向继续复用现有pipeline lease，通过各自handler写结果及事件。

执行细化补充：`projection_cursor`增加`active_generation: UUID|null`及`rebuild_state: JSON|null`，只有`research_inbox.v1`使用。`InboxRebuildState`由IT契约定义，闭合字段为candidate_generation、domain_watermark、journal_watermark、last_replayed_sequence、state；其他consumer两列保持null。切换与cursor.revision共用CAS，不依靠内存active head。正常影子重建重放不可变journal（含用户操作和撤回效果）；不重新消费domain events制造新待办。迁移中不另加第四张收件箱业务表。

另外两项细化在BASE-02登记：质量实验新增只读`GET /investor/methods/quality-mutations/{mutation_id}`，在创建响应丢失且对象ID未知时查询原收据；404不证明无在途请求，继续使用原key/body。收件箱为现有ReviewCycle新增`cycle_event_revision`非空整数列，旧行从0开始；IT-002/003负责模型及迁移，IT-008通过CAS递增，不伪造旧事件历史。

来源失效提示的首期真实生产者限定为已有文档事实的可信更正：`save_candidates`实际插入verified数值证据且含非空correction_refs时，同事务产生`source.invalidated`，reason=`source_corrected`。source_ref为新SavedEvidence精确引用，invalidated_refs为核验后的旧引用，replacement_refs指向新引用，subject取已核对的listing引用，occurred_at固定新保存行的created_at。owner从当前grant及持久化SavedEvidence/DisclosureOriginal校验取得，不能从不含owner的EvidenceRecord或模型输入猜测。当前没有通用原件合法撤回写入口；缺少可信记录/无法读取保持source_unavailable，不能伪造withdrawn。

## 任务卡

以下表格中的路径除特别标注外均相对仓库根；括号`新`表示计划创建，不能在派发时误认为已有实现。共享热点任务的owner=集成；Review由未编写该批实现的Sol审查代理执行，主代理负责结论。

| ID | 类型 | 任务与交付物 | 允许写文件或新文件范围 | 依赖ID | 可验证退出条件 | 估算人日 |
|---|---|---|---|---|---|---|
| BASE-01 | 集成 | owner=集成；冻结执行基线、工作区改动清单、真实迁移head、共享文件锁和命令清单 | `execution/evidence/baseline.md`（新文档）；只读Git、pyproject、lockfile、CI及现有测试 | — | 记录HEAD和dirty内容；标记并行simulation及061迁移归属；不重置工作区；明确目标测试allowlist与基线失败 | 0.5 |
| BASE-02 | 集成 | owner=集成；冻结四方向DTO/错误码/事件名/引用/时间与事务契约，输出版本化接口清单 | `execution/evidence/contracts.md`、`tests/fixtures/next_iterations/contracts/`（新）；不修改正式采信白名单 | BASE-01 | feature_disabled=503；listing/account union、QualityArtifactRef隔离、EventSourceRef整数/UUID、generation head落点及canonical hash规则无冲突；接口变更必须登记影响任务 | 1 |
| BASE-03 | 测试 | 建立本次离线隔离与测试运行入口，复用已有工具；提供网页静态模块DOM测试bootstrap | `tests/next_iterations/conftest.py`、`tests/next_iterations/test_offline_guard.py`、`tests/support/next_iterations_offline.py`、`tests/support/next_iterations_dom.mjs`、`scripts/check_next_iterations.py`（新）；owner=集成修改必要的测试配置 | BASE-01 | wrapper强制为所有选定pytest目录加载共享plugin，不能只靠子目录conftest；unit、integration及Node/Python子进程诱饵均证明外连失败；临时SQLite/时钟/惰性凭证；只收集allowlist且排除browser/E2E/network，零测试失败；网页.mjs用Node内建test并从mobile现有依赖解析jsdom，不安装新依赖或浏览器 | 1 |
| PK-01 | 集成 | owner=集成；统一新增网页资产的package-data规则与可复用离线包装检查器 | `pyproject.toml`仅package-data；`tests/support/next_iterations_package.py`、`tests/fixtures/next_iterations/package-assets.json`（新）；必要时`src/investment_agent/web/static_assets.py`仅支持已冻结路径 | BASE-02, BASE-03 | 显式包含static/pages/*.css及确实使用的新增目录，不宽泛打包数据/凭证；不升级依赖；通用helper可按功能资产清单检查wheel中精确路径 | 0.5 |
| PK-02 | 验收 | 独立检查离线wheel资产及可复用helper，交付各功能阶段验收使用 | `tests/next_iterations/test_package_assets.py`、`execution/evidence/acceptance-packaging.md`（新） | PK-01 | 临时源副本用现有setuptools后端构建，ZIP检查既有资产和受控嵌套CSS探针；不安装/发布包，不启动应用；之后各功能验收必须用helper核验实际新增资产，不能仅引用本通用验证 | 0.5 |
| EV-01 | 实现 | 定义共享事件信封、闭合EventSourceRef、source identity及规范化hash，预定义本批事件payload合同 | `src/investment_agent/research_events/contracts.py`、`src/investment_agent/research_events/canonical.py`；对应新unit测试 | BASE-02, BASE-03 | int/UUID严格校验、revision一致、UTC/Decimal稳定、NaN拒绝；同语义同hash；source.invalidated闭合schema含精确invalidated_refs/replacement_refs、source_corrected、subject与版本，SV验收前即可校验；未装领域resolver的kind不可发布，不能靠generic dict绕过 | 0.75 |
| EV-02 | 实现 | 定义事件、receipt、cursor三表，含分片manifest与generation head列 | `src/investment_agent/storage/models/research_events.py`；独立模型测试 | EV-01 | 唯一键/索引完整；immutable事件、可变receipt分离；owner与引用由服务校验；cursor新增列及受限使用写入契约 | 0.5 |
| EV-03 | 集成 | owner=集成；注册模型并按实际head生成共享迁移 | `src/investment_agent/storage/models/__init__.py`、`alembic/versions/<分配号>_research_events.py`；隔离迁移测试 | EV-02 | 新临时库与实际前序head升级成功；已有数据保留；只有一个head；不修改061_simulation或其他历史迁移 | 0.75 |
| EV-04 | 实现 | 同session事件append、确定性event_id、同源同hash重放与异hash冲突 | `src/investment_agent/research_events/repository.py`中的事件写入；对应unit/SQLite测试 | EV-03 | 业务回滚同时撤销事件；重复提交一次；不同hash显式失败；无commit或新开session逃逸 | 0.75 |
| EV-05 | 实现 | 已有来源只读resolver与白名单注册协议 | `src/investment_agent/research_events/resolvers.py`、`src/investment_agent/research_events/registry.py`初始定义；对应测试 | EV-04 | review round/comparison正整数与各UUID来源精确回读；缺失/跨owner/未知kind拒绝；新topic/decision未安装时不冒认有效 | 0.75 |
| EV-06 | 实现 | receipt/cursor的事务API、CAS、同链阻塞与有界分片进度原语 | `src/investment_agent/research_events/repository.py`的receipt/cursor部分；对应SQLite竞争测试 | EV-04, EV-05 | 同一consumer/event唯一；投影回调与收据/游标同事务；manifest不可换、cursor只能推进；扫描水位不伪装交付成功水位；无领域分片目标选择 | 1 |
| EV-07 | 实现 | 有界消费者调度、重试隔离与健康只读摘要 | `src/investment_agent/research_events/delivery.py`、`src/investment_agent/research_events/health.py`；对应测试 | EV-06 | 独立链继续、同链阻塞；5秒/30秒/2分钟/10分钟退避与5次隔离；未知schema立即隔离；显式记录错误不吞异常 | 1 |
| EV-08 | 实现 | 专用机械worker循环与tick/lease隔离 | `src/investment_agent/research_events/maintenance.py`；worker假队列/故障注入测试 | EV-07 | 每轮50新事件/20重试/20唤醒的扩展点有界；lease丢失停止提交；不调用研究维护、provider或通知；慢假研究不阻塞独立tick | 1 |
| EV-09 | 集成 | owner=集成；接入kind、CLI、调度与独立进程配置，交付注册文档 | `src/investment_agent/research/worker.py`、`src/investment_agent/cli/commands/worker.py`、`src/investment_agent/scheduler/runner.py`；实际Compose文件（BASE-01定位）；`docs/operations/research-events.md`（新）；必要设置注册精确片段 | EV-08 | CLI可限定kinds；30秒单活入队、仅机械进程领取；配置解析与单元装配通过；不启动生产服务；IT可注册业务handler | 1 |
| EV-10 | 测试 | 独立故障/并发测试包：提交前后中断、双writer、cursor冲突、异hash、跨链隔离 | `tests/next_iterations/test_event_transactions.py`、`test_event_delivery.py`、`test_event_worker.py`（新） | EV-09 | 文件SQLite及两个session实际验证全成/全无；假时钟验证恢复顺序；外连计数为0；不启动全应用或真实服务 | 1 |
| EV-11 | Review | 独立代码审查共享事件基础，按diff及事务调用链给出分级问题 | `execution/evidence/review-events.md`（新）；业务文件只读 | EV-10 | 检查原子性、重复处理、链序、kind隔离、migration和权限；每项问题含位置/触发条件/影响/修复验收；不得作者自批通过 | 0.75 |
| FX-01 | 回修 | 修复EV-11问题并提交逐项证据；无发现时记录无需改动 | 仅EV-01至EV-10原允许文件中被指出的部分；共享热点仍由集成人员处理 | EV-11 | 严重/阻断问题归零；相关回归通过；原审查代理复核关闭；新增问题继续生成FX-01子卡，不跳过 | 1 |
| EV-12 | 验收 | 独立验收共享基础及交付API，冻结供三个方向使用的版本 | `execution/evidence/acceptance-events.md`（新）；运行allowlist测试不修改业务代码 | FX-01 | EV-01至EV-10退出条件有证据；Review问题已关闭；注册/API/运行文档可读；形成可依赖的离线工程验收结果 | 0.5 |

## 可信来源更正生产者

这些任务补齐QT-046与收件箱来源失效处理的实际生产者，不与IN-01b的观察池/评审受理重复。只读解析接口不保存证据，因此不能从GET或纯parse路径发送事件。

| ID | 类型 | 任务与交付物 | 允许写文件或新文件范围 | 依赖ID | 可验证退出条件 | 估算人日 |
|---|---|---|---|---|---|---|
| SV-01 | 集成 | owner=集成；向文档最终保存链显式传递可信OwnerId并保留旧调用兼容 | `src/investment_agent/evidence/documents.py`仅save_candidates/verified_corrections关联owner读取；`src/investment_agent/agent/document_tools.py`最终permission与保存接缝；`tests/next_iterations/test_source_owner.py`（新） | BASE-02, BASE-03 | owner取grant并与原件/证据持久行一致；不接受模型/OriginalRequest指定owner；兼容local_owner默认调用；GET/纯parse不写事件；越权发生在保存前 | 0.75 |
| SV-02 | 集成 | owner=集成；新增verified correction事件生产者并接到真实新插入证据的同session边界 | `src/investment_agent/research_events/source_corrections.py`（新）；`src/investment_agent/evidence/documents.py`保存后窄接线；`src/investment_agent/research_events/registry.py`、`src/investment_agent/research_events/resolvers.py`仅该事件；对应新unit测试 | SV-01, EV-12 | 以数据库可信inserted结果判定新增而非先查后猜；verified且非空correction_refs才发；核owner/listing/原件/旧ref；使用EV-01已定义schema及真实append校验；规范化refs/固定created_at/稳定源键；publisher不commit；候选/普通文本/标题线索不发 | 0.75 |
| SV-03 | 测试 | 验证真实保存接缝的并发、重复解析、追加事件失败和候选升级 | `tests/next_iterations/test_source_correction_events.py`（新） | SV-02 | 调用实际save_candidates及DocumentExternal最终保存边界，仅stub纯parse/外传；证据+事件+原action receipt原子；两writer/重解析不重发；跨owner拒绝；外连0，历史记录不暗补事件 | 1 |
| SV-04 | Review | 独立审查更正事件的事实语义、owner来源、事务与回读完整性 | `execution/evidence/review-source-corrections.md`（新） | SV-03 | 不把原件不可读当撤回；不从EvidenceRecord猜owner；事件精确回读持久事实；审查报告含反例及关闭条件 | 0.5 |
| SV-05 | 回修 | 按来源更正审查回修并交独立复核 | 仅SV-01至SV-03被指出文件；所有共享接线owner=集成 | SV-04 | 阻断项关闭；真实保存接缝反例通过；不新增通用原件撤回平台或外部自动采集 | 0.5 |
| SV-06 | 验收 | 离线验收更正生产者，冻结供QT/IT消费的实际事件样本 | `execution/evidence/acceptance-source-corrections.md`、`tests/fixtures/next_iterations/source-corrections.json`（新） | SV-05 | 从临时库实际保存结果导出样本；源唯一键/owner/subject/schema可回读；核心更正事件已生产，不只是手写fixture；旧历史仍靠直接有效性检查，未支持撤回类型明确标注 | 0.5 |

## 原设计覆盖与消费边界

| 原范围 | 本文负责 | 其他任务负责 |
|---|---|---|
| IN-01三表、信封、引用、hash、事务原语 | EV-01至EV-07 | IT源生产者将事件写进真实业务事务，不能由EV测试替代 |
| IN-01队列隔离与共享交付 | EV-08至EV-12 | IT维护handler、健康页面、重建及backfill；BT/PT具体生产者与消费者适配 |
| IN-01b原后处理恢复 | 只提供共享receipt/session API | IT提取旧helper、同事务受理及权限检查 |
| 多事项来源撤回 | EV-06提供冻结manifest和原子进度接口 | IT选择目标、最多50效果一片、最多10000目标、逐片journal及generation切换阻塞 |
| 共享配置与路由 | BASE-02约定、EV-09机械进程接线 | 每方向自己的集成任务串行注册；最终XA任务做组合检查 |

## 离线验收限制

本批不运行真实E2E、不启动完整产品链路、不调用真实LLM/行情/账户/通知。故障注入只针对事件存储、消费服务或单个worker handler，用临时数据库及假适配器；进程隔离可用测试子进程，不能借此启动全套服务。真实部署时延、连续运行稳定性和模型产出质量不在本计划验证范围内。

表中路径缩写`execution/`指本目录；证据文件在任务执行时创建，本轮不预填通过记录。Review发现超出回修预算的工作需新增有依赖的修复子卡，再更新任务索引与估算；不能为了维持原任务数量标记通过。
