# 账户适配度 P0、P1 实施计划

日期：2026-10-16（Asia/Shanghai）。状态：P0四项、P1十一项已按用户后续授权实施；原计划及审查依据保留，执行分工见[实施与验收计划](account-fit-p0-p1/execution.md)，首次证据见[验收记录](account-fit-p0-p1/acceptance.md)，后续全面复审及修复见[复审记录](account-fit-p0-p1/review-2026-10-16.md)。仅在隔离测试库验证迁移，未启用生产配置。

完整文档导航与提交范围见[交付索引](account-fit-p0-p1/README.md)，十四组针对性复审及689项全量集成补验见[最终复审与补验记录](account-fit-p0-p1/followup-review-2026-10-16.md)。以下保留原任务顺序与验收要求，不把不同轮次的测试计数重复累计。

依据：[经代码对照修订的设计](../account-fit-and-supplemental-rules-design.md)，尤其第4—7、9—11、13—16节。本文将P0、P1细化为4项契约准备任务与11项实现任务；任务编号使用AF-P0/AF-P1，避免与仓库既有漏斗阶段P1混淆。下文“新增”描述相对基线的文件边界；实现后的实际文件和验证结果以验收记录为准。

## 1. 本轮计划覆盖的交付与边界

P0交付可执行的契约、样本预期、代码接缝清单与就绪判断；P1交付人民币A股账户的open/add/hold财务约束检查、本账户候选排序及只读展示。用户显式点击可以创建本地确定性检查任务，页面加载和GET查询不得隐式创建任务。

P1只增加`investment_assessment`和`candidate_ranking_entry`两张表，数值计算使用现有SavedCalculation，接收收据复用InvestorMutation，执行复用PipelineRun与ResearchWorker。交易规则供应默认仍可为UnavailableTradingRules，缺失时返回unknown，不能拿合成fixture代替生产来源。

P1不交付规则管理、事件核验、未决问题闭合、研究优先级调度、新Agent工具、正式公司观点发布或整案门槛改造；这些属于P2—P4。阳光电源类政策事项在P0只冻结输入/预期及后续接口边界，P1不得声称已研究或已核验其风险。

P1的`supplemental_binding=not_integrated`，不读取任何P2表；`research_status/company_alignment`固定not_assessed，不为此接入公司发布服务或构造买入支持。完整`ranking_bucket`只能为incomplete或blocked，ready/limited保留后续契约。财务状态独立展示和筛选；warning只有存在P0明确冻结的结构化软限制时才可产生，首版没有该来源就保留枚举、不虚构偏好解析或另建偏好系统。

P1不增加交易执行、真实资金预留或用户约束写入；最小单位检查通过不能替代更大数量方案的共享现金计算。原评分、主规则槽位、候选生命周期和ag22-v1/v2历史语义保持不变。

## 2. 已核实的代码接缝

| 接缝 | 现有文件 | 计划约束 |
| --- | --- | --- |
| 身份、十进制、引用 | [common.py](../../../src/investment_agent/investor/common.py)、[portfolio/contracts.py](../../../src/investment_agent/portfolio/contracts.py) | P1只新增account_fit/candidate_ranking引用；不提前注册规则命名空间，不放宽Decimal或单公司证据 |
| 组合算术与费用 | [calculations.py](../../../src/investment_agent/portfolio/calculations.py)、[calculation_contracts.py](../../../src/investment_agent/portfolio/calculation_contracts.py)、[purposes.py](../../../src/investment_agent/portfolio/purposes.py) | 复用纯calculate/check_scenarios/reference_outputs；不直接调用以估值时点统管全部资料的旧rebuild替代新来源重建 |
| 数值呈现 | [PortfolioNumericResolver](../../../src/investment_agent/portfolio/plans.py)、[NumericResolver](../../../src/investment_agent/investor/publication_numbers.py) | 新AccountFitNumericResolver验证新方法与路径；保持公司解析器拒绝账户fact_refs |
| 筛选与候选 | [screen.py](../../../src/investment_agent/rules/screen.py)、[screen_intake.py](../../../src/investment_agent/candidates/screen_intake.py)、[candidate repository](../../../src/investment_agent/storage/repositories/candidate.py) | 新读取hit+truncated全集；不改自动提名数量、不触发规则起草，不使用当前页内排序 |
| 不可变存储与收据 | [SavedCalculation](../../../src/investment_agent/storage/models/evidence.py)、[InvestorMutation](../../../src/investment_agent/storage/models/investor.py) | 新表不可变、旧表不重写；收据与结果身份分开，原请求重放先于关闭/CAS |
| SQLite关联约束 | [Base](../../../src/investment_agent/storage/models/base.py)、[database.py](../../../src/investment_agent/storage/database.py) | 当前未启用全局foreign_keys；新entry复合FK仅作声明，需新表专属INSERT约束与事务校验，不为此全局开启外键 |
| 模型注册与迁移 | [models/__init__.py](../../../src/investment_agent/storage/models/__init__.py)、[alembic/env.py](../../../alembic/env.py) | 新模型只加入统一注册清单；现有env会自动看到，不另建模型注册渠道 |
| 队列与租约 | [jobs/base.py](../../../src/investment_agent/jobs/base.py)、[worker.py](../../../src/investment_agent/research/worker.py)、[pipeline_run.py](../../../src/investment_agent/storage/repositories/pipeline_run.py) | 现有record_metadata/mark_success/mark_partial已支持lease；handler必须自行持久化终态，return completed不自动标成功；仅run状态fencing不能保护另一事务的制品 |
| 队列终态 | [pipeline_status.py](../../../src/investment_agent/domain/pipeline_status.py) | success/partial/failed；不能把AgentTask的cancelled状态写进PipelineRun |
| HTTP与配置 | [api/deps.py](../../../src/investment_agent/api/deps.py)、[config.py](../../../src/investment_agent/config.py)、[settings/registry.py](../../../src/investment_agent/settings/registry.py)、[loader.py](../../../src/investment_agent/settings/loader.py) | writes_disabled=403；新增设置同步Settings、RuntimeSettings、SPECS并验证运行时覆盖 |
| 页面 | [candidates.html](../../../src/investment_agent/web/templates/candidates.html)、[stock.html](../../../src/investment_agent/web/templates/stock.html)、[web/routes.py](../../../src/investment_agent/web/routes.py) | 独立区域与模块；保留原候选页面和操作流程，不修改原生mobile/Tauri |
| 隔离测试 | [tests/support/portfolio.py](../../../tests/support/portfolio.py)、[worker测试](../../../tests/unit/test_research_worker_robustness.py)、[迁移不可变性测试](../../../tests/integration/test_migration_047.py) | 合成来源明确标识；SQL UPDATE/DELETE/REPLACE也须检查，不能只测ORM监听器 |

两个必须在P0确认的读接口问题：现有`GET /portfolio/listing`会ensure建档；`GET /portfolio/valuations/current`可能入队估值更新。新页面不能为了准备只读展示而直接使用这些有副作用的路径。P1新增只读`GET /portfolio/fit-context`聚合已保存上下文，不改变旧接口行为。

## 3. 执行顺序、依赖与检查点

下列为原定先后约束；用户已另行授权实施和Luna子代理协作，生产启用仍不在本轮范围。每项完成后记录改动文件、验证命令/结果、剩余限制；未满足退出条件不能仅凭任务状态进入依赖它的下一项。

| 顺序 | 任务 | 直接依赖 | 可审阅交付 |
| --- | --- | --- | --- |
| 1 | AF-P0-01 接缝与兼容基线 | 修订设计 | 文件/契约基线与不可改语义 |
| 2 | AF-P0-02 P1契约冻结 | P0-01 | 状态、引用、HTTP、时间、开关与容量政策 |
| 3 | AF-P0-03 样本及验收预期 | P0-02 | 合成输入、预期结果、设计验收编号映射 |
| 4 | AF-P0-04 数据就绪与P0退出 | P0-01—03 | 生产依赖矩阵和P1进入检查点G0 |
| 5 | AF-P1-01 领域DTO、引用与配置基础 | G0 | 可独立运行的契约、默认关闭开关与设置覆盖测试 |
| 6 | AF-P1-02 两表存储与迁移 | P1-01 | 不可变存储、唯一性、fresh/往返迁移 |
| 7 | AF-P1-03 账户输入冻结 | P1-01—02 | 同账户、双时间、用途及缺失输入清单 |
| 8 | AF-P1-04 适配算术内核 | P1-01、03 | account_fit_checks-v1纯计算与状态投影 |
| 9 | AF-P1-05 适配应用服务与当前有效性 | P1-02—04 | 数值解析、保存/复用、缺失/过期查询；G1 |
| 10 | AF-P1-06 候选全集冻结 | P1-01—03 | 完整source_manifest及排除审计 |
| 11 | AF-P1-07 排序快照与分页 | P1-05—06 | 去重、有序全集、cursor、变化检测 |
| 12 | AF-P1-08 原子接收与worker | P1-01—07，含配置基础 | 两种本地job、幂等、恢复、租约提交；G2 |
| 13 | AF-P1-09 HTTP与只读context | P1-08；P1-01设置基础 | 受控路由、只读context、开关边界、CSRF；G3 |
| 14 | AF-P1-10 候选与单股展示 | P1-09 | 财务检查、用途/账户切换、稳定分页 |
| 15 | AF-P1-11 集成、浏览器与交接 | P1-01—10 | 实测验收、限制、启停记录；G4 |

必要依赖为：G0→DTO/配置→存储→输入；输入之后一支为内核→适配服务，另一支为候选全集，二者在排序任务汇合，再到队列→HTTP→UI→G4。上表是可串行执行的默认顺序，不把P1-06误写成依赖P1-05。G2的关停/恢复验收使用P1-01已交付的运行时开关，不再等待G3配置实现。

P0的四项交付集中于已交付的[契约冻结记录](account-fit-p0-p1/p0-freeze.md)，分基线、契约、样本、就绪四节；实施证据另存acceptance.md。每项任务只要求当时已有文件可独立验收，测试构造器不能提前import后续尚未实现的service/job。

## 4. P0具体任务

### AF-P0-01 接缝与兼容基线

目标：将当前源码对应的可复用能力、需新增能力和历史语义固定下来，防止实施时照抄旧验收结论。

涉及文件：读取第2节代码及设计第16节；未来新增`docs/product/plans/account-fit-p0-p1/p0-freeze.md`的“基线”节。不改源码。

任务：

1. 记录实际Git状态、迁移单head、Python/Node/测试入口；不固定猜测下一迁移号，不迁移开发库。
2. 核对snapshot、valuation、Mandate、用途版本的身份和时间字段；确认当前交易规则默认不可用。
3. 冻结旧候选GET/offset、ag22请求/哈希/result_paths、公司NumericResolver拒绝账户事实、模型/worker注册及设置覆盖的回归入口。
4. 确认新表的统一模型注册、SQLite约束和纯算术复用边界；标明不得为此重构整个组合或候选系统。

验收：每项依赖有真实文件及符号，区分“现成”与“需新增”；既有WIP保留；产物不把历史fixture通过等同当前生产数据可用。G0前不得留下不明的身份、时间或数值解析路径。

### AF-P0-02 P1契约冻结

涉及文件：补齐未来`p0-freeze.md`的“契约”节；必要时只对主设计作有依据的文字补正。源码DTO在P1-01实施。

必须冻结：

| 契约项 | P0必须落定的内容 |
| --- | --- |
| 动作与输入 | open=已确认0、add/hold=正持仓；partial缺标的不猜0；hold交易输入not_applicable |
| 用途 | selected/confirmed_empty/none_recorded/unselected/unknown各自形状；hold允许不关联用途但仍检查全账户保留 |
| P1状态 | financial四态；supplemental固定not_integrated；完整bucket仅incomplete/blocked；不借旧观点标签制造supportive |
| 来源时间 | assessment_as_of、price_basis_at、资料available/acquired、用途confirmed_at分别验证；live由服务端冻结，历史回放不混入当前 |
| 估值与起点缺失 | valuation_binding=available(exact ref)/missing(reason)，缺估值可形成有限unknown检查但不构造虚假完整NAV；账户尚无已确认snapshot则context显示缺失，POST拒收422，不伪造起始快照 |
| 计算方法 | account_fit_checks、method_version=1；fee=fixed_fee+q×execution_price×fee_rate，估值价与成交价分开 |
| 试算假设 | 独立fit逐股execution_price/fixed_fee/fee_rate；ranking显式传trade_assumptions：逐股假设，或确认覆盖本批的费用基础+valuation_price_as_scenario政策，服务端逐股展开并冻结；缺项为unknown，不猜0费或成交价 |
| 分类与比例 | 仓位行业检查首期使用既有CSRC口径；筛选来源SW分组仅用于来源展示，不能填补缺失CSRC。未复权价格、Decimal原值比较、ratio权重到percent的专属展示映射分别固定 |
| 引用/存储 | 仅account_fit/candidate_ranking新kind；新产物UUID/revision=1，重评新UUID；主体及旧引用校验不放宽 |
| API与查询 | 第7节的具体请求/返回、reason_code、整数run/UUID产物、只读context、静态路由优先 |
| 当前结果身份 | current查询另绑定calculation_basis_key，覆盖动作、费用/价格假设政策、方法及用途身份；exact来源变更由current-limits检查。不能将另一个价格/费用情景的最新结果冒充当前表单结果 |
| 失效/覆盖 | current_status与coverage独立；账户、估值、用途、约束、候选/筛选内容变更及时间过期的明确reason |
| 接收与重放 | 用户body的路由/命令hash与服务端冻结输入hash分开；同key重放原as_of/manifest/run，先查收据再看开关/当前版本 |
| 开关 | 本地fit写入需account_fit_enabled与portfolio_ledger_enabled；account排序还需candidate_ranking_enabled；无Agent调用不额外要求investor_agent_enabled，未来Agent入口另加其权限 |
| 开关关闭中途 | 接收后未完成job在claim及正式提交前重验；关闭则终结failed，reason=writes_disabled，不新增cancelled、不自动新建key重试；已完成结果和收据可读 |
| 容量与时效 | 单次fit≤100、单页≤100；总成员/manifest字节上限、分批、TTL与unknown缓存先用固定小样本测量后冻结初值及依据，G4记录是否满足资源预算。估值时效沿既有日历，未知年份不猜工作日；不为新方法另造投资阈值 |

容量上限是资源保护政策，候选来源达到上限必须显示partial及明确范围，不能先截前N又宣称完整。单账户持仓/现金事实不能截断后仍标为complete；此类输入超过安全容量应拒收422并说明原因。P0必须给上述政策确定值和边界预期，不能把阈值留给UI或测试各自定义；不凭经验增加仓位上限等用户投资约束。

验收：正例/反例DTO示例、状态真值表、引用矩阵和错误映射齐全；可区分403/409/422、unknown/执行失败、资料不全/entry不完整。未交付的P2字段必须有明确not_integrated状态，不允许空列表表示已评估。

### AF-P0-03 样本与验收预期

涉及文件：补齐未来`p0-freeze.md`的“样本”节；P1-01再创建`tests/fixtures/account_fit_v1/`与纯DTO构造器，后续任务按依赖扩展数据库/worker构造器。本任务不修改旧golden。

样本集至少包括：

| 样本组 | 合成输入/情形 | 必须预先写清的预期 |
| --- | --- | --- |
| F01 最低建仓 | 估值/成交1500、最小100、fixed_fee=10、fee_rate=0；账户20万/200万，单股上限10% | 前者blocked；后者必要条件齐备可financial pass，但P1整体仍incomplete；非真实茅台行情 |
| F02 双价格与已有持仓 | v=1500、e=1600，原有股票/现金明确；open与add分别构造 | 按现金流和v估值求NAV，不套NAV_before−fee；add计入原数量 |
| F03 未知与原始0 | 未知费用/现金/交易规则/行业/NAV、已确认0费、partial缺目标持仓、非正NAV | 未知不补零；0费与未知不同；不可证明的比例不通过 |
| F04 用途/hold | 非空未选、空确认、无头、读取失败；现金保留下限重叠；hold不含交易输入 | 绑定形状不同；保留不重复扣；hold无交易规则不因此失败 |
| F05 全集与分页 | 命中超过hit_cap、超过100；同股多模板；手动、dismiss冷却、expired与观察池 | truncated可读不自动提名；完整去重；页拼接等于全集，排除依据保留 |
| F06 并发/恢复/时点 | 同key异参、两SQLite写者、入队后改来源、晚确认用途、worker丢租约、关闭入口 | 409或stale符合各路径；重试仍用原manifest；无重复制品/半份排序 |
| F07 观点与后续风险边界 | published但avoid/sell/条件未知；阳光电源政策user_report和范围未知 | P1不推断支持配置、不把线索写成硬违规；事件核验/闭合明确留给P2/P3 |
| F08 提交与显示边界 | 全部unknown、空候选、单项错误/系统性DB错误、旧假设结果、ratio显示、无snapshot/valuation、迟到分页 | 正常未知不是job失败；空全集可完成；系统错误不伪装partial；缺起点拒收/缺估值有限检查；旧页面响应不覆盖当前，按原快照状态过滤时明确stale |

验收：逐样本列出input、known/unknown、financial_status、bucket、coverage、reason_code、是否创建候选/任务及预期引用；算术预期独立给出Decimal表达式，不能调用被测实现生成expected。P0已知生产缺口与合成供应明确区分。

### AF-P0-04 就绪检查与G0

涉及文件：补齐未来`p0-freeze.md`的“就绪与G0”节，汇总前三项；不安装依赖、不修改生产设置或启动采集。

检查生产供应：交易规则来源/版本/有效区间、未复权价格与交易日历、费用假设、已有账户覆盖、分类快照及用途确认时间。交易规则尚不可用时记录“生产前置未满足”，允许P1离线算法/unknown路径继续，不把provider开发悄悄扩大为P1必做范围。

G0退出标准：P0-01—03产物可审阅；DTO、状态、容量/TTL、读路径和错误映射无悬空决策；合成样本可复现；生产前置有逐项结果与责任边界；已确定P1只建两表、只接本地job、默认关闭。G0完成不表示真实来源或研究质量验收通过。

## 5. P1具体任务

### AF-P1-01 领域DTO、引用、配置与样本落盘

依赖：G0。

涉及文件：新增`portfolio/suitability_contracts.py`、`candidates/ranking_contracts.py`、`tests/fixtures/account_fit_v1/`、`tests/support/account_fit.py`、`tests/unit/test_account_fit_contracts.py`；修改`src/investment_agent/investor/common.py`仅添加两个引用kind；同步修改`config.py`、`settings/registry.py`并扩展`tests/unit/test_settings_registry.py`、设置覆盖专项。仅loader有实际显式映射缺口时修改`settings/loader.py`，不另造设置加载层。未加根目录的源码路径均相对`src/investment_agent/`。

交付：AccountFitRequest/Assessment、PurposeBinding、ValuationBinding、FrozenAccountInputs、TradeAssumptions、SourceManifest、RankingRequest/Snapshot/Entry及P1固定supplemental状态。Settings、RuntimeSettings、SPECS同步加入两个默认false开关，验证DB覆盖而非只读启动Settings；供后续服务和worker直接使用。API DTO在P1-09组合领域契约，不复制另一套模型。此时tests/support/account_fit.py只依赖已存在DTO/标准库，数据库和job工具随对应任务再添加，保证本任务可单独导入验证。

验收：拒绝float/NaN、客户端无时区、重复listing、结构非法的动作/用途/估值组合、客户端可信规则/通过状态；持仓相关业务合法性留P1-03。配置镜像与env/DB优先级专项通过，两个开关默认false；旧ObjectRef/JSON/hash不变。P2模型不提前引入。覆盖AF03、AF05—07、CP01—02及后续G2配置前置。

### AF-P1-02 不可变存储与迁移

依赖：P1-01。

涉及文件：新增`storage/models/investment_assessment.py`、`storage/repositories/investment_assessment.py`、`alembic/versions/<actual_next>_investment_assessment.py`、对应`tests/integration/test_migration_<actual_next>.py`、`tests/unit/test_investment_assessment_store.py`；修改统一`storage/models/__init__.py`；只在必要时更新现有current-head断言。

交付：assessment仅允许account_fit/candidate_ranking，PK(id,revision)、owner/kind/account/listing/as_of/input_hash索引及严格payload；ranking_entry复合FK、每revision的listing/ordinal唯一约束。完整成员映射在entry表，头部只存摘要/hash/count，不再重复一份全量名单当第二事实源。

当前SQLite关闭foreign_keys，不能把FK声明当作已实现关联保护。新entry的INSERT trigger须拒绝不存在/错误revision/非candidate_ranking的父对象及不匹配owner/account，ordinal范围按P0约定与父entry_count约束；不全局开启foreign_keys而影响旧表。父头+全部entry在一个事务提交，service校验exact成员集合、count、ordinal连续性和hash；读取再次验证kind与汇总一致性。完成后不允许追加额外entry；零成员快照是明确的count=0合法结果。

ORM监听加新表SQL UPDATE/DELETE/REPLACE保护，包含REPLACE经唯一键替换不同主键的情况；收据仍用InvestorMutation，计算仍用SavedCalculation，不借此改全部旧表。metadata import一次注册，不改alembic/env.py另建名单。迁移不预占编号，不创建第三张批次表或规则/事件/outbox表。

验收：临时file SQLite fresh、当前head升级/降级/再升级；旧行不改；在PRAGMA foreign_keys=OFF下直接SQL插入孤儿/错kind/错owner/revision/越界ordinal均失败，重复entry/REPLACE不能越过保护；count/hash错误不能被查询当完整快照。计算+fit及完整排序保存可整体回滚。覆盖MG01、AU01、CP01—02，并补新增表触发器专项。

### AF-P1-03 账户上下文与输入冻结

依赖：P1-01—02。

涉及文件：新增`portfolio/suitability_inputs.py`、`tests/unit/test_account_fit_inputs.py`；读取现有portfolio service/valuation/purposes及Mandate、Listing、IndustryClassification；如有必要，仅抽取不改变旧默认的只读校验helper。

交付：按exact owner/account/subject解析快照、估值、约定、用途；分开assessment_as_of与price_basis_at；冻结来源值/hash/取得时间、ValuationBinding、CSRC分类行实际内容、TradeAssumptions和所有缺失状态。交易规则通过现有TradingRuleResolver协议注入，默认Unavailable，fixture供应只在隔离测试入口注入；类型构造器的verified字面值不等于生产来源已核验。

只读context与admission冻结分开。先验证账户存在且属于当前owner，错owner/账户保持404；仅对已授权账户缺snapshot的not_found映射snapshot_missing。GET可用snapshot的只读BEGIN，不创建事实，POST无起点以snapshot_required拒收。缺估值则冻结missing绑定及当时不存在的头状态，可检查独立现金/约束、比例unknown；后来新增估值令旧结果stale。

POST须先在同一session取得writer reservation并查收据，之后才调用会显式BEGIN读取的snapshot等服务；不能先开读事务再尝试升级为BEGIN IMMEDIATE。预览后输入变更409，不自动替换。来源冻结、原子接收和worker重建均须调用同一个只读物化逻辑，不各自实现latest查询。

历史模式首期只回放已有本功能run_ref+source_manifest_hash对应的冻结输入，不建设通用历史账户/分类重建器；验证owner、允许kind及原input_as_of。缺可信旧manifest直接422 historical_manifest_unavailable，不接收客户端自称历史原件的整包JSON。已冻结输入内部本来缺失的项仍可unknown，不从今天的行补齐。

验收：partial缺持仓不当0；缺交易规则unknown，hold不需该来源；无snapshot与无估值分支不同；用途状态不混同；晚于价格基础且不晚于评估截止的用途可绑定；错账户/未来输入拒绝。SQLite模拟预览与写入竞争，验证POST不发生读后锁升级；GET与历史回放不建档/采集/导入设置。覆盖AF03、AF06—07、TM01、AU01。

### AF-P1-04 适配算术与财务状态内核

依赖：P1-01、03。

涉及文件：新增`portfolio/suitability_calculations.py`、`tests/unit/test_account_fit_calculations.py`；按需复用或最小抽取`portfolio/calculations.py`、`portfolio/purposes.py`纯函数；对应旧回归为`tests/test_portfolio_calculations.py`、`tests/test_portfolio_purposes.py`。

交付：account_fit_checks-v1的method-owned details、result_paths、assumptions、input_hash。open/add按最小增量试算，hold用无交易情景。既有ScenarioAction要求positive quantity或budget二选一，不能表示“未知最低单位”；新适配DTO独立表达缺口，只有该子检查必要输入完整时才构造旧ScenarioAction并复用算术。其余保存unknown，不以0、1股或虚构价费凑齐旧请求。完全无可发布算术时fit可calculation_refs=[]，不生成status=OK的伪计算。

先保存no_action基线，再标明每项违规是已有账户违规还是本次试算出现/加重；不因既有超限把它解释为目标公司的经营缺陷。所有比例按未舍入Decimal比较；费用未知时可用的保守上界与完整现金值分开引用，不能把cash_upper_bound当确切余额。CSRC缺失不能用筛选SW标签补齐。

按设计计算cash/NAV/权重、现金与用途保留、单股/行业上限；先证实违规，再必要未知，再soft limitation，最后pass。所有软阈值有来源；没有偏好就not_configured，不擅自设投资阈值。缺失不生成伪成功数值，CalculationStatus.OK仅表示实际已产出的算术正确，不等于财务通过。

验收：F01—04及F08独立预期；v≠e、非正NAV、未知费用/最低单位、独立违规与未知共存、hold无规则、已有超限基线、用途不双扣均覆盖。空计算不伪造结果；阈值附近舍入不改变判定，旧ag22/hash/result_paths保持。覆盖AF01—07、CP01。

### AF-P1-05 适配应用服务、数值解析和current-limits

依赖：P1-02—04。

涉及文件：新增`portfolio/suitability.py`、`tests/unit/test_account_fit_service.py`；使用新repository和SavedCalculation，以PortfolioNumericResolver为校验参考并复用纯format_quantity。默认不import整案发布模块或为此重构plans.py，不扩大公司NumericResolver的证据/单位范围；确需抽取时另列最小差异和旧调用回归。

交付：构建/读取不可变fit，独立AccountFitNumericResolver验证method/version、account/subject、result_paths、metric/单位与诊断；缺计算时不调用resolver假装有数值。现有reference_outputs的权重单位为ratio，而通用format_quantity不把任意ratio解释成percent；仅在新resolver已验证的weight/sector_weight路径上做专属比例显示，不能放宽全局ratio语义。金额/原值/舍入仍服务端处理。

read_exact/read_current返回payload与current-limits分栏，缺结果200 missing、过期stale。purpose_binding_key包含状态/版本/id；current还按calculation_basis_key区分价费假设与动作。更换表单假设后旧结果只能标历史情景，不能回显为新情景通过。首次新增用途或估值使相应missing/none_recorded结果过期。首期仅要求同一operation的稳定UUID/输入hash重试复用，不另做跨操作内容寻址缓存；不重写created_at。

同步检查快照、估值及其缺失绑定、Mandate语义、用途语义、来源hash/有效期、交易规则时效和日历边界，无新依赖表或规则generation。unknown结果有有限TTL；批量读取先取一次账户共同状态再逐条比较，不能每页每股重复加载全账户。P1不查公司发布服务，始终not_integrated/not_assessed；财务pass不产生整体ready。

G1验收：单股检查→保存计算/fit→精确读取→依赖变化stale可独立完成；不存在P2表也可运行；读取无副作用。覆盖AF01—07、TM01、AU01、CP01—02。新fit引用不能作为旧整案目标数量，也不能被公司NumericResolver接收。

### AF-P1-06 完整候选source_manifest

依赖：P1-01—03。

涉及文件：新增`candidates/ranking_sources.py`、`tests/unit/test_candidate_ranking_sources.py`；读取现有ScreenResultRecord、CandidateRecord、WatchlistItem及CandidatePolicy，原则上不修改筛选/提名写服务。

交付：账户视图读取声明批次的hit+truncated、open与已有手动提名。批量读取后按现有语义判断：启用观察池优先排除；有效且未到期的当前open保留，包括用户在旧dismiss冷却内经既有nominate_candidate手动重新提名的open；只有没有有效open的自动筛选线索才受dismiss冷却约束。open但expires_at已到按逻辑到期处理，不等待scheduler改行，也不在GET修改状态。

旧expired/dismissed对象的旧命中不复活；无有效open、冷却结束且有晚于关闭/到期的新完整筛选批次时，可作为新只读线索入列，不重开旧CandidateRecord。新手动提名仍走既有流程，不新增快捷状态写入；不能把“自动提名冷却”错误扩大为禁止既有手动提名结果展示。

冻结run状态、完整行内容、模板version和上述状态依据，worker不重选latest。全体候选/筛选查询批处理，不逐股调用会重复查批次的latest_screen_results。不触发draft/research/提名；source_cap排除和业务排除分开统计。

超过100条分批是内部评估策略，不是候选截断；达到P0资源上限时coverage=partial并给出实际范围/未处理数。历史状态没有旧manifest时不从今天状态倒推。

验收：truncated不漏；跨模板全来源；逻辑到期、冷却内手动重新提名的有效open、冷却结束无新批次/有新批次、观察池覆盖open、空集合/缺批次/资源partial分别单测。冻结后不重选输入，候选/草稿/LLM任务数不变。覆盖RK01、RK05、CP01。

### AF-P1-07 排序、快照、cursor和差异

依赖：P1-05—06。

涉及文件：新增`candidates/ranking.py`、`tests/unit/test_candidate_ranking.py`；使用ranking_contracts及assessment repository。

交付：先按完整bucket分组，再按固定模板ID/version做round-robin；同模板沿原rank，手动队列明确，无跨模板strength相加；同股一entry。完整成员与稳定ordinal先确定，之后才分页。单项执行失败以incomplete/evaluation_failed占位，不删除成员。

P1保留完整bucket语义，另支持financial_status筛选（作为分组内过滤，不重算顺序）；cursor绑定ranking_ref、bucket/financial_status过滤hash及ordinal。这样可以查看财务pass标的，但不把全部尚未接规则的incomplete重新命名为ready。

过滤只针对快照时financial_status；另返回当前stale限制，不能把过期pass显示成当前通过。partial持仓导致consider_buy无法解析动作时，entry.resolved_action=null、financial_status=unknown、fit_ref=null、reason=holding_presence_unknown；这属于已完成的有限评估，不冒造open/add、也不放入执行failed_items。

快照保留previous_ref、universe_hash、变更理由；current_status与coverage独立，可同时partial和stale。已冻结排序不因依赖变化原地重排，新批次只进入下一快照。research视图的政策风险优先级留P3，不构造空风险规则或假数据。

验收：分页拼接=全集，无重复遗漏；跨快照/筛选cursor422；相同输入确定；空集合生成合法count=0快照；未知动作与单项执行错误分开；资源partial与中止分开；迟到分页不改变当前列表。覆盖RK01—03、RK05、CP01—02。

### AF-P1-08 幂等admission、worker与恢复

依赖：P1-02—07。

涉及文件：新增`portfolio/suitability_jobs.py`、`candidates/ranking_jobs.py`、`tests/unit/test_account_fit_jobs.py`、`tests/unit/test_candidate_ranking_jobs.py`；修改`research/worker.py`的default_job_handlers注册。现有PipelineRunRepository已提供record_metadata及带lease的mark_success/partial/failed，默认不改worker框架、pipeline repo或retry_policy；若集成证明有真实缺口，先记录具体缺口再作最小变更并回归。

交付两个kind：account_fit_evaluate和candidate_rank。admission复用同一收据辅助函数，按owner/路由/用户body查InvestorMutation，原收据优先返回；获得writer reservation后核验冻结来源，再同事务写run+收据。服务端新生成的时间、UUID、manifest不进入客户端body比较，否则同请求重放会因重新解析latest而冲突。不在事务中跑算术；ranking不为每股调用公共POST、不创建子AgentTask、不启动候选/估值等follow-up job。

checkpoint存PipelineRun.metadata_json的有界明细（完成listing、result ref/input_hash、failed_items），不另建任务表。单项结果ID由operation+listing+角色稳定派生，重试按ID/hash核验复用，不能生成新UUID重复保存；同操作的计算/fit/checkpoint一次提交。跨不同操作的内容缓存不在P1必做范围。

租约必须与制品处于同一session/事务：先用带lease的record_metadata取得写入fence，再写计算/fit/检查点，失败整体回滚；最终写ranking头、完整entry及mark_success/mark_partial同事务提交。不能先SELECT验证租约、另一个事务写制品，再由worker异步更新状态。handler必须调用终态持久化方法，return completed只表示execute结束，不自动标success。失租约后禁止提交，之前已提交单项保留为合法检查点但不能冒充完整排序。

数据库锁竞争、IO/存储故障和编程错误必须传播给现有worker的重试/失败分类，禁止except Exception逐股吞掉并伪装partial。只有明确的FitItemEvaluationError类受控局部失败才占位/failed_items；输入缺失/冲突/未知动作属于成功完成的有限检查。空全集也正常success。全局输入失效仍用冻结输入生成可审计stale结果，而不是偷偷重绑定。

使用P1-01配置基础，在execute内及提交事务内读取有效开关，不能把可能抛错的关停检查放在现有try/settle之外的on_claimed中。writes_disabled为带code的非重试异常，由worker落failed并保存安全诊断；终态仍只有success/partial/failed。异常和丢租约不得写正式ranking_ref，重新开启不会自动换key重建任务。

G2验收：真实file SQLite+worker：相同run重放、异参409、并发一收据、部分已提交后崩溃、旧worker提交与新attempt竞争、SQLite锁可重试、IO/程序错误不伪装partial、unknown和空全集success、受控单项错误partial、handler终态确实落库。两开关在claim前/处理中/提交前关闭都能终结且不发布排序，原收据可读。记录所有kind的新增run计数，确认没有候选、估值、LLM或通知副作用。覆盖RK03、RK05、AU02—03、CP02。

### AF-P1-09 HTTP、只读context和配置边界

依赖：P1-08。

涉及文件：新增`api/schemas/investment_assessments.py`、`api/routes/investment_assessments.py`、`tests/integration/test_api_account_fit.py`、`tests/integration/test_api_candidate_rankings.py`；修改`api/app.py`注册router，必要时修改`api/deps.py`显式注册新422类code。配置模型/加载器由P1-01交付，本任务仅接HTTP权限与运行时读取，避免多任务分别定义开关。

交付第7节接口。静态fit-context/current/run在动态ID之前；认证+CSRF按现有共享边界。run查询核实owner、允许kind及命令收据，只返回受控进度/产物映射，不沿用ResearchRunDetail暴露raw request。新写路由的开关检查放在服务收据查询之后，不能由Depends(enabled)提前挡住原请求重放。

读取P1-01的有效设置，不只用启动值。已完成结果不因关闭消失；本地功能不要求启用Agent，不新增scheduler。配置UI若通用SPECS已支持可直接展示，否则只增必要说明，不建新设置页。401/403保留未确认body；明确409/422后解除自动重放并展示原输入，不自动换新key或改版本重发。

GET fit-context返回已有account/snapshot、ValuationBinding、用途绑定、可用listing、CSRC/价格缺口及支持的试算政策；无snapshot时只读说明起点缺失，不调用PurposeService.current连带抛错或创建起点。price/fee只能来自已存引用或请求明确的场景值，不为准备DTO调用ensure或估值更新路由。异步批次查询同时核验run参数中服务端owner/来源hash与命令收据，不能仅凭客户端给的run_id返回制品。

G3验收：真实HTTP验证auth/CSRF、wrong owner/account/kind/revision、403/409/422、same-key重放、静态路由、整数run/UUID产物、不同价费basis的current查询、回放不进current、未知/执行错误区分。应用启动和一次性设置导入完成后建立基线，GET期间零新增行/任务/网络；零写入断言不能把lifespan初始化混在其中。P1-01配置镜像及DB覆盖回归继续通过，旧API形状不变。覆盖AU01—03、CP01—02。

### AF-P1-10 候选与单股页面接入

依赖：P1-09。

涉及文件：新增`web/static/candidate_ranking.js`、`web/static/account_fit.js`、`web/static/pages/account_fit.css`；修改`web/templates/candidates.html`、`web/templates/stock.html`、`web/static/app.js`、必要的`web/static/pages/candidates.css`；仅需要server只读身份元数据时改`web/routes.py`。扩展`tests/integration/test_web_candidates.py`、`test_web_client_contract.py`，新增`tests/integration/test_web_account_fit.py`。

交付：保留旧入口；新增本账户检查区域、账户/用途选择、TradeAssumptions表单、显式生成按钮、financial筛选、批次状态与固定快照分页。ranking可明确选择“以冻结估值价作为成交价情景”，并填写/明确未知两项费用；页面不自动勾选免费，也不把情景称为报价保证。单股支持逐股执行价和hold；缺snapshot禁用提交并说明起点缺失，缺估值仍可请求有限检查。未注册listing不ensure，原建档流程处理。

使用api.js/dom.js，不在前端算现金/权重。发送前保存完整UUID/body；无可用浏览器存储则不发送。POST使用已保存body key并禁用api.js自动生成另一幂等key。202是接收完成，不是评估完成：清除“结果未知的POST”锁后保存run_ref、accepted_as_of、上下文/basis与上次ranking_ref，轮询受控run接口；reload恢复同run而不重发新操作，终态后按制品ref取结果。页面不可见/离开或epoch改变停止本地轮询，不创建任何后台自动化。

账户/用途/假设/排序过滤或页码变化均有epoch guard；一页的迟到响应也不能覆盖新页。假设改变使当前展示标为旧情景，不以旧basis判新输入通过；过滤快照pass时stale标记仍可见。401/403保留待确认请求，409/422展示原输入供重新审阅，不能自动换key；P1不显示政策已核验、风险已闭合或完整ready。

验收：旧提名/晋级/忽略保持；无case/无snapshot/无估值、unknown/stale/partial/failed均有文案；明确费用输入能驱动F01，留空不免费。真实导入新模块和浏览器CSS加载成功；扩展test_shipped_js_modules_parse的实际参数清单，不能只grep文件名。覆盖202后reload、存储失败零发送、双击一次接收、跨页迟到响应、价费basis改变与关闭后收据重放。覆盖UI01的P1部分、AF03、RK02—03、AU02—03、CP01。

### AF-P1-11 集成验收、浏览器和交接

依赖：P1-01—10。

涉及文件：新增`tests/browser/account_fit.mjs`、`tests/browser/account_fit_fixture_server.py`、必要的`tests/integration/test_account_fit_flow.py`；新增交付记录`docs/product/plans/account-fit-p0-p1/acceptance.md`及`docs/product/probes/account-fit-p1/`；扩展相关旧回归。浏览器fixture使用独立端口与临时file SQLite，不修改生产库。

交付：实际HTTP→admission→ResearchWorker→SavedCalculation/fit→排序快照→页面的离线链路。P1无需FunctionModel或真实模型演示；若旧回归使用FunctionModel，仍保留原控制，不把它包装成本阶段研究质量证据。

桌面1440与手机390验证：两个账户、用途状态、单股hold、显式价费假设驱动F01、缺snapshot/估值、缺交易规则、空全集、truncated与多页、partial+stale、POST丢回执与202后reload两条恢复路径、中途关闭、真实409、运行失败/恢复、账户/假设/页码切换迟到响应、旧候选操作、XSS/无溢出。模拟丢响应可以用传输故障，成功API及worker产物不作替身。fixture标识合成来源，截图为可读viewport。

G4验收：第8节阶段映射及PC01—PC12增补验收全部给出结果，G0—G3证据齐全；专项/必要全量/静态门禁通过；记录最大测试成员、manifest字节、共同上下文读取次数与分批耗时，不能只有“性能正常”。来源未满足项可见，新开关默认关闭，不改真实库/设置。P1完成后交接P2，不自动实施后续功能。

## 6. 文件与数据边界汇总

| 范围 | P1允许变更 | 禁止顺带扩大 |
| --- | --- | --- |
| 领域与存储 | suitability_*、ranking_*、单一assessment model/repository、两引用kind、两新表迁移 | 规则/事件/issue通用平台、第三张批次表、通用依赖图 |
| 旧计算 | 默认只读复用calculate/check_scenarios/reference_outputs，新方法与resolver独立 | 为适配unknown放宽ScenarioAction、改ag22时间/hash、重构plans或公司NumericResolver |
| 队列 | 注册两个handler，使用现成record_metadata/mark_*，新增本地admission/回执组合 | 新租约框架/全局重试策略、AgentTask、follow-up采集/研究/通知、换key重置运行 |
| HTTP与设置 | 单独router/DTO、只读context、两开关与镜像 | 放宽旧权限、改writes_disabled状态码、关闭时删历史 |
| Web | 旧页独立区域、新JS/CSS、共享客户端/DOM | 重写候选页、原生mobile/Tauri开发、前端金融算术 |
| 测试与文档 | 小合成样本、新专项、受影响回归、阶段证据 | 修改旧golden掩盖回归、把fixture标为生产可信 |

默认不修改或调用`agent/research_publication.py`、`investor/publication.py`、`investor/monitoring.py`及整案发布服务；P1研究状态固定not_assessed，旧观点仅作P0边界样本。若声称必须接这些服务才能完成P1，应先记录越界依赖并调整设计，不以兼容补丁提前实施P3。

共享文件的任务责任固定：P1-01负责common.py与Settings/RuntimeSettings/SPECS及必要loader，P1-02负责模型注册/迁移，P1-08仅负责worker handler注册，P1-09负责api/app.py与新路由/错误边界，P1-10负责Web启动和模板。P1-03—07默认只新增各自模块并扩展同一assessment repository，不分别修改旧portfolio/candidate写路径。若需要改同一文件，由后续任务基于前置已完成版本最小追加，不能提前写跨任务空壳或复制业务实现。

## 7. P1接口交付清单

| 接口 | P1明确行为 | 归属任务 |
| --- | --- | --- |
| GET /portfolio/fit-context | 认证读取已有snapshot、ValuationBinding、约定/用途、可选listing、开关和支持的试算政策；无snapshot typed missing，不创建/采集/入队 | P1-03/09 |
| POST /portfolio/fit-assessments | 1..100唯一条目、动作与TradeAssumptions、exact snapshot/约定、ValuationBinding和用途；202原子接收，返回operation_id/run_ref/accepted_as_of/request_hash/source_manifest_hash/replayed；无snapshot422 | P1-08/09 |
| GET /portfolio/fit-assessments/current | account/listing/action/purpose_binding_key/calculation_basis_key；200 missing或payload+current-limits，不重算；未知或不匹配basis不能冒充最新通过 | P1-05/09 |
| GET /portfolio/fit-assessments/{id} | UUID、exact revision与account；不可变内容和当前失效分栏 | P1-05/09 |
| POST /candidates/rankings | P1仅account/consider_buy，明确来源政策和TradeAssumptions（逐股或本批价费政策）；服务端逐股展开并冻结；research视图422 unsupported | P1-06—09 |
| GET /candidates/rankings/{id} | revision/cursor/limit、bucket及新增financial_status过滤；冻结顺序、coverage、current_status和refresh_required | P1-07/09 |
| GET /candidates/rankings/{id}/entries/{listing_id} | exact冻结条目、全部命中来源、fit或失败占位，无case也可读 | P1-07/09 |
| GET /investment-assessment-runs/{run_id} | 仅两个P1kind的整数ID，精确owner及收据核验；状态/进度/失败项/产物清单 | P1-08/09 |

GET fit-context和financial_status过滤是为落实主设计只读准备与P1独立财务展示而补充的实施接缝，不改变主设计的整体bucket含义。缺失上下文留缺口，不能自动调用现有ensure/估值入队GET补齐。

basis_key由服务端生成并随完成的fit/entry返回；页面保留已接受run/结果对应key，不自行复刻hash规范。首次没有已知key时显示尚未生成，不发空key查询并挑取另一情景的“最新pass”。ranking共享假设按source_manifest中的成员展开，数据不足的成员unknown；被业务排除项不能因假设输入被重新纳入。

模式策略：live由服务端冻结as_of；historical_replay仅接受本功能已有run_ref+source_manifest_hash及原input_as_of，验证owner/kind和原数据可见性，回放结果不进current。没有可信历史manifest返回422；原manifest内部资料本来缺失仍保留unknown。首期不支持任意历史日期的通用重建，也不增加专门回放页面；这是一条有明确输入的审计重放能力，不得用今天分类或账户状态补过去。

## 8. 设计验收编号与阶段覆盖

| 主设计验收编号 | P0交付 | P1执行/证据归属 |
| --- | --- | --- |
| AF01—AF07 | F01—04输入与独立预期 | P1-03—05算术/用途/动作/状态；P1-11真实链路 |
| RK01—RK03、RK05 | F05—06全集、分页和恢复预期 | P1-06—08及浏览器，不自动提名/起草 |
| RK04 | 负面有效观点和无open持仓风险的边界样本 | P1仅保证current不推出ready；完整research全集/政策优先级留P3，不计全项通过 |
| TM01、TM02 | 时间轴与迟到输入/回放样本 | P1执行账户/来源/回放部分；准入绑定与可信采集窗口改造留P3 |
| AU01—AU03 | 权限、收据与静态路由真值表 | P1-02、08—11；模型批准/事件verified部分留P2/P3 |
| CP01—CP02 | 旧JSON/hash/引用/阶段边界基线 | P1-01—11受影响回归；不提前创建P2表或改变整案动作 |
| UI01、MG01 | 页面状态及迁移清单 | P1-02/10/11实际迁移和浏览器 |
| SR01—SR07、PB01—PB05 | 只冻结后续字段和F07语义边界 | 不在P1实现，不将样本存在计为这些项目通过 |

本轮审查补充下列计划级验收，不改主设计原编号的含义：

| 编号 | 必须验证的行为 | 责任任务/检查点 |
| --- | --- | --- |
| PC01 | 两个开关在worker验收前已进入Settings/RuntimeSettings/SPECS；实际DB配置改变影响执行，不只读启动值 | P1-01、08；G2 |
| PC02 | foreign_keys=OFF时孤儿、错父kind/revision/owner、越界ordinal、REPLACE被拒；空集合合法，count/hash校验不靠FK声明 | P1-02、07；G1/G2 |
| PC03 | source/fit/checkpoint提交与lease CAS同事务，旧attempt丢租约无新提交，handler显式落success/partial终态 | P1-08；G2 |
| PC04 | admission先writer reservation再调用snapshot等BEGIN读服务；预览后的并发写入409，不读后锁升级或重选latest | P1-03、08；G2 |
| PC05 | 无snapshot不收任务；缺估值可有限unknown；首次新增估值令旧missing结果stale；未知动作不伪造open | P1-03—07、09；G1/G3 |
| PC06 | 两个不同价费basis不会互相替代；ranking价费政策逐股冻结，空费不免费；ratio权重只在专属路径转percent | P1-01、04—05、09—10 |
| PC07 | 全unknown/空全集正常完成，受控单项错误partial占位，SQLite锁重试，IO/编程错误不被except Exception吞成partial | P1-07—08；G2 |
| PC08 | 存储失败零发送，双击一次接收，POST丢回执及202后reload均恢复原run；页码/账户/假设迟到响应不覆盖新状态 | P1-10—11；G4 |
| PC09 | 未知q/价费不构造虚假ScenarioAction；完全无算术则无OK计算；基线违规可区分，cash_upper_bound不作确切现金 | P1-04—05；G1 |
| PC10 | 逻辑到期、冷却内手动新open、冷却结束无/有新批次、观察池覆盖、truncated与cap排除分别测试，保留手动提名的既有优先级 | P1-06—07；G2 |
| PC11 | 回放仅使用已存本功能manifest；错owner/hash/时点或无manifest422，缺失原数据不补齐，回放不进current | P1-03、08—09；G3 |
| PC12 | 资源预算有测量记录；超过单账户安全容量拒收、候选cap明确partial，批量共享上下文与SQL查询数不随每股重复整账户加载 | P0-02、P1-05—08、11；G4 |

证据按条目记录expected/actual、命令、环境与来源、run/制品引用、失败和修复前后结果。unknown是有效结果，不能为提高“通过率”补数据；skip、合成fixture、静态检查和真实业务质量分别记录。

## 9. 验证、启停与交接门槛

实施时使用项目`.venv`，测试`DATABASE_URL`和`DATA_DIR`必须显式指向本次临时目录。测试app启动也要隔离directory/provider后台能力，不能因启动lifespan而访问真实数据源。禁止在真实`.dev/data/app.db`或现有DATA_DIR运行迁移/全量测试。

验证顺序：

1. 每任务先跑对应纯函数、契约或存储专项；源码变化后再加最小受影响旧回归。
2. G2/G3跑真实文件SQLite、worker、HTTP与设置覆盖；验证lease失效、收据优先于开关、零隐式模型/采集。
3. G4串行跑真实浏览器1440/390及旧候选/组合相关回归；不并发修改共用端口或数据库。
4. 最终完成Ruff、mypy、实际新增JS模块检查、git diff --check、迁移单head与离线全量。所有失败如实记录，修复后按影响重跑，不以已有基线失败数掩盖新增失败。

最低受影响回归集合：`tests/test_portfolio_calculations.py`、`test_portfolio_purposes.py`、`test_portfolio_valuation_binding.py`、`test_portfolio_plans.py`、候选生命周期/筛选专项、worker/pipeline lease专项、`test_settings_registry.py`与API设置专项、旧Web候选/单股模块契约。实际符号和迁移head在实施前重新核对，不把本计划的文件名示例当当前已新增测试。

启停约定：新开关默认false，G4不自动开启生产。关闭只拒绝新接收/未提交运行，旧结果、收据和stale解释可读；恢复不自动重启failed任务。技术回退优先关闭新写入口并保留表和审计数据，不通过自动downgrade删历史。降级迁移只在隔离测试/明确备份恢复流程执行。

交接记录至少包含：实际任务完成情况、文件差异、迁移编号、样本/方法/排序政策版本、专项/全量/浏览器结果、输入条数与manifest大小/耗时/查询数、未满足的生产交易规则/资料依赖、默认开关状态及P2接入点。未接规则的P1标签继续not_integrated。

## 10. P0/P1完成判定

P0完成：单份p0-freeze四节与F01—F08预期可直接用于DTO/测试；所有时间、用途/估值缺失、假设、容量与开关政策有明确决定；供应缺口有记录，P1无需猜P2行为。

P1工程完成：两表、独立方法与解析器、冻结输入、完整候选来源、稳定排序、幂等worker、受控HTTP及实际页面闭环均有证据，主设计的本阶段验收项通过，历史契约不变。缺生产可信交易规则时open/add诚实unknown，不能宣称生产适配能力已全量可用。

P1生产启用条件单列：可信交易规则及必要价格/日历/费用基础得到来源核验，明确账户数据覆盖与用户约定，相关产品质量门槛通过后再由已有配置路径启用。生产数据未就绪不等于P1离线工程未完成，也不允许以工程完成代替生产就绪。

用户后续授权后已交付P0/P1，详见验收记录；P2及以后阶段未实施，生产启用另按上述条件进行。

## 11. 2026-10-16 计划代码对照审查记录

本节保留实施前的计划审查记录；各项实际实现与PC01—PC12结果见验收记录。

| 编号/影响 | 原计划问题及代码依据 | 文档修订 |
| --- | --- | --- |
| PR01 高：任务依赖倒置 | [load_settings](../../../src/investment_agent/settings/loader.py)读取RuntimeSettings覆盖，原P1-08却要在P1-09添加开关之前验收关停 | 配置模型/镜像/覆盖前移P1-01，P1-09仅接HTTP权限；明确共享文件归属 |
| PR02 高：关联保证不足 | [Base](../../../src/investment_agent/storage/models/base.py)说明SQLite未启用foreign_keys，原计划只列复合FK | 新表INSERT保护、parent/kind/owner/ordinal及整份集合/hash检查，foreign_keys=OFF直SQL验收，不改全局行为 |
| PR03 高：制品与run不原子 | [PipelineRunRepository](../../../src/investment_agent/storage/repositories/pipeline_run.py)已有lease支持，但只约束它自身SQL；[JobHandler](../../../src/investment_agent/jobs/base.py)要求handler持久化终态 | 制品与record_metadata/mark_*同事务，稳定单项ID/检查点，禁止仅return completed；不重建队列框架 |
| PR04 高：接收事务锁升级 | [PortfolioService.snapshot](../../../src/investment_agent/portfolio/service.py)会显式BEGIN，只读事务后不能直接BEGIN IMMEDIATE | admission先reserve/收据再冻结；GET与POST会话分开，补并发验收 |
| PR05 高：输入/验收不一致 | [snapshot](../../../src/investment_agent/portfolio/service.py)无起点抛not_found；[ValuationService.current](../../../src/investment_agent/portfolio/valuation.py)可为None，原请求却要求exact估值 | typed ValuationBinding，缺估值有限unknown、缺snapshot拒收；补未知动作entry，无假起始事实 |
| PR06 中：价费与当前结果缺协议 | [ScenarioAction](../../../src/investment_agent/portfolio/calculation_contracts.py)依赖执行价和两项费用，原ranking请求/UI未规定假设入口 | TradeAssumptions、逐股/本批政策、current的basis key、显式场景表单与对应测试 |
| PR07 中：异常被误计为partial | [worker._settle_failure](../../../src/investment_agent/research/worker.py)、[retry_policy](../../../src/investment_agent/research/retry_policy.py)已有系统故障分类，on_claimed在execute异常处理外 | 局部typed错误与未知/全局故障分开，关停检查置execute，禁止逐股吞所有异常 |
| PR08 中：202后恢复与页面竞争遗漏 | [api.js](../../../src/investment_agent/web/static/api.js)通用POST只代表HTTP响应，不能负责领域run恢复 | pending请求与已接收run分开存，reload跟踪原run，价费/页码epoch、存储失败零发送和真实浏览器验收 |
| PR09 中：算术复用前提不足 | [ScenarioAction](../../../src/investment_agent/portfolio/calculation_contracts.py)不接受未知quantity；[reference_outputs](../../../src/investment_agent/portfolio/calculations.py)权重为ratio，通用格式化不接受任意ratio→percent | 只在输入充分的子检查调用旧算术；独立bounds/无计算分支和metric限定显示，不扩大旧DTO/解析器 |
| PR10 中：全集过滤与时效模糊 | [screen_intake](../../../src/investment_agent/candidates/screen_intake.py)按观察池/open/冷却处理，[nominate_candidate](../../../src/investment_agent/candidates/lifecycle.py)的手动提名不受自动冷却限制 | 明确手动新open优先级、逻辑到期/新批次与旧状态隔离、source cap、批量查询及空全集验收 |
| PR11 中：范围及制品过多 | 原计划四份P0规格、可选公司发布读取、通用历史重建/跨操作缓存会扩张P1，且不提高本阶段财务检查可信度 | P0合并单份冻结记录，P1固定not_assessed；回放只用旧manifest，缓存只保证同操作重试；保留4+11任务结构 |

审查结论：任务仍为4项P0+11项P1，依赖已消除配置后置；新增12项计划级验收覆盖提交、缺失、假设、恢复和资源边界。本轮仅修订本文，不修改业务代码或主设计的金融语义；实际供应缺口和后续P2/P3能力仍按原设计保留。
