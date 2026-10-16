# 账户适配 P0 冻结

日期：2026-10-16（Asia/Shanghai）。范围：AF-P0-01—04；供 AF-P1-01 DTO、配置和合成 fixture 使用。此冻结定义工程边界，不代表交易规则、生产价格或研究判断已核验。

## 1. 基线与兼容边界

基线提交为 `245f95e`。开始工作时工作区已有未跟踪的 `.playwright-mcp/`、`.zcode/`、`docs/product/account-fit-and-supplemental-rules-design.md` 与 `docs/product/plans/account-fit-p0-p1-plan.md`；均是既有工作，本交付保留。迁移目录当前 head 为 `049`；本阶段不运行迁移。运行时为 Python 3.13.5；项目使用 `.venv`、pytest、Ruff、mypy。仓库未提供 `.venv/bin/node`，JS 检查不属于 P0/P1-01。

已核实接缝：

| 现成能力 | 符号/文件 | 冻结处理 |
| --- | --- | --- |
| 严格时间、Decimal、UUID引用 | `investor/common.py`: `Contract`, `DecimalValue`, `ObjectRef` | 仅在 ObjectRef.kind 增加 `account_fit`、`candidate_ranking`；旧 kind、整数 ID、JSON/hash 不动 |
| 账户及持仓快照 | `portfolio/contracts.py`: `Snapshot`, `SnapshotHolding`, `Holding` | 精确绑定账户和 snapshot；partial 缺持仓不推定为零 |
| 估值 | `portfolio/valuation.py`: `Valuation`, `ValuationHolding` | 精确绑定 `portfolio_valuation`；保存估值时点与单独的价格知识时点 |
| 约束及用途 | `investor/contracts.py`: `Mandate`; `portfolio/purposes.py`: `PurposeVersion` | 同账户 exact revision；用途状态另行冻结，不把缺失解释成空用途 |
| 纯组合计算 | `portfolio/calculations.py`: `calculate`, `reference_outputs`; `portfolio/purposes.py` | 新方法仅复用可表示的纯算术；不放宽 `ScenarioAction` 的必需参数，也不改 ag22-v1/v2 |
| 交易规则 | `portfolio/calculations.py`: `TradingRuleResolver`, `UnavailableTradingRules` | 当前默认供应返回 unavailable；测试合成规则不得冒充生产可信来源 |
| 候选来源 | `rules/screen.py`, `candidates/screen_intake.py`, candidate repository | 新读取器必须纳入 hit 与 truncated 全集并冻结源行；不改旧提名/冷却写路径 |
| 数值解析 | `portfolio/plans.py`: `PortfolioNumericResolver`; `investor/publication_numbers.py`: `NumericResolver` | 新 `AccountFitNumericResolver` 单独验证方法/路径；公司解析器继续拒绝账户事实 |
| 默认关闭配置 | `config.py`, `settings/registry.py`, `settings/loader.py` | Settings 与 RuntimeSettings 镜像，注册 SPECS，验证运行时 DB 覆盖；无显式 loader 映射缺口则不改 loader |
| 迁移与模型注册 | `storage/models/__init__.py`, `alembic/env.py` | 后续只注册两张表；P1-01 不改模型或迁移 |
| worker / 收据 | `research/worker.py`, `PipelineRunRepository`, `InvestorMutation` | 后续复用；handler显式保存终态，不能把 completed return 当成功；收据先于开关重放 |

只读边界：旧 `GET /portfolio/listing` 会 ensure 建档；`GET /portfolio/valuations/current` 可能入队估值任务。未来 fit-context 不调用这两条来补数据。缺 snapshot 的 GET 返回 typed missing，POST 422。历史估值/用途/分类不从当前行倒推。P1只新增两张表，不创建 P2 规则/事件表、批次表或通用依赖表。

## 2. 冻结契约

### 2.1 DTO、引用与结果

- 新对象引用只有 `account_fit` 与 `candidate_ranking`，UUID、revision=1。每次重评创建新 UUID；账户/公司/Listing 引用校验保持不变。
- 动作是 `open | add | hold`。open 要求已确认目标持仓为 0；add/hold 要求正持仓。partial snapshot 缺目标标的时动作不能猜测，排序条目使用 `resolved_action=null`、`holding_presence_unknown`。
- 用途是五态：`selected` 精确用途版本、其中存在的 `purpose_id` 及 `confirmed_at`；`confirmed_empty` 精确绑定实际空用途版本及 `confirmed_at`；`none_recorded`（从未记录）；`unselected`（有非空版本但请求未选）；`unknown`（读取/时间边界无法判定并附原因）。hold 可不关联单项用途，但仍检查全账户已确认的现金保留；同一笔保留只扣一次。
- 估值是 `available`（精确 `portfolio_valuation` ref）或 `missing`（带原因）。没有估值可以计算独立于估值的检查，不能造完整 NAV；新估值到来使原 missing 结果 stale。
- `assessment_as_of` 是服务端冻结的决策知识截止；`price_basis_at` 独立记录价格基础的知识时点。所有时间必须带时区，统一存 UTC。来源分别保留 `available_at` 与 `acquired_at`；用途保留 `confirmed_at`。live 时间由服务端生成。
- 历史回放只接受本功能已存在的 run_ref、manifest hash 和原 `input_as_of`。任一绑定不符或无可信 manifest 返回 422；回放结果不进入 current。今天数据不能补历史缺项。
- 试算费用公式固定 `fee = fixed_fee + quantity × execution_price × fee_rate`，成交价 `e` 与估值价 `v` 分开。各费用/交易输入用 known 值或 unknown+reason；未知绝不补零。hold 不携带交易参数，也不查询最低交易单位。
- 适配方法是 `account_fit_checks`, version `1`。最低交易单位/数量未知时不造 0/1 股情景；若 `q` 已知，可把显式 nullable 的成交价/费率传给旧内核，由其给出精确结果或费用保守上界。缺价/费时现金上界不得显示为确切现金，依赖未知的检查仍为 unknown。完全没有可发布算术时 `calculation_refs=[]`，不制造 OK 计算。
- Financial 状态四态：`pass | warning | blocked | unknown`。先判断已证实违规，再依赖未知，再软限制，最后 pass。warning 只允许引用已冻结的结构化软限制；首版没有该来源，不得凭自由文本或模型偏好产生 warning。
- P1 固定 `supplemental_binding=not_integrated`、`research_status=not_assessed`、`company_alignment=not_assessed`；整体 bucket 只允许 `incomplete | blocked`。财务 pass 不得变成整体 ready/supportive。P2 尚未评估不能用空列表表达。
- 价格使用未复权市场快照。CSRC 用于仓位行业约束；筛选 SW 分组只作来源展示，不能填补 CSRC 缺失。权重用原 Decimal ratio 比较；仅新专属 resolver 可将 `weight`/`sector_weight` 展示为 percent。
- `current_status` 与 `coverage` 是独立轴，可以同时 stale 与 partial。current 查询还需 `calculation_basis_key`（动作、费用/价格假设政策、方法、用途身份）；另一价费情景的最新结果不能当作当前表单结果。

### 2.2 接口、幂等与错误

请求/响应按实施计划第7节冻结：fit-context 只读；fit admission 1–100 项；current 使用 account/listing/action/purpose key/basis key；单项结果用 UUID exact revision；ranking 只支持 account + consider_buy；ranking 查询固定 cursor/limit 与快照财务过滤；run ID 是正整数 PipelineRun，制品 ID 是 UUID。`/current` 等静态路由先于动态 UUID 路由。认证与 CSRF 沿用现有共享边界。

用户 request body 的 hash 与服务端冻结输入 hash 分开。重放先验证 owner/key/用户 body 收据，然后才检查开关或当前版本；同 key 同 body 返回原 as_of、manifest、run，异 body 409。未确认的 401/403 body 保留。writes_disabled 是 403；并发/旧版本/basis冲突是409；格式或缺少必需 snapshot/historical manifest 是422。unknown 是成功的有限结果，不等于执行失败；DB/IO/程序错误不能伪装成 partial。

fit 新写需 `account_fit_enabled && portfolio_ledger_enabled`；排序另需 `candidate_ranking_enabled`。本地确定性工作不要求 investor_agent_enabled。Settings/RuntimeSettings/SPECS 三处同步，两个新增开关默认 false，DB runtime 值在已导入后覆盖 env；已接收未完成工作 claim 和正式提交前重验。关闭后终结为 failed/writes_disabled，不创建新 key 自动重试；结果及收据仍可读。

### 2.3 容量及缓存初值

这些值只作资源保护，不代表投资限制。首版冻结如下：

| 范围 | 上限/初值 | 达限行为与依据 |
| --- | ---: | --- |
| 单次 fit listing | 100 | 计划和既有 calculation contracts 的批量上限；超过422 |
| response page | 100 | 单页上限；cursor按冻结快照推进 |
| 单账户 holding facts | 500 | 既有账户导入一次最多500行；不能截断为 complete，超过422 |
| ranking完整成员 | 2,000 unique listings | 资源上限；超过来源全集上限则 coverage=partial，报告实际范围及未处理数 |
| ranking canonical manifest | 4 MiB UTF-8 JSON | 哈希前的序列化硬上限；成员前缀超过时明确partial；必要排除审计自身超限则422容量错误，不丢弃审计来宣称全量 |
| 内部评估分批 | 100 listings | 与一次 fit 上限一致；manifest完整冻结后分批处理，不截候选 |
| 单项财务诊断 | 4,096 checks | fit和ranking entry共享上限，覆盖500持仓双情景及现有用途诊断；完整保留，不截到1000导致合法输入执行失败 |
| complete/known 有效窗口 | 5 min | 结果有效性上限；精确依赖/hash变化立即stale。当前读取仍检查依赖，未额外引入缓存系统 |
| 包含 unknown 的 current cache | 30 sec | 不确定/缺失来源较易变化，短期复用后重新读；unknown 仍是有效结果 |

最终实现通过真实source resolver读取2,001条合成screen结果，冻结2,000个唯一成员并明确记录第2,001项的未处理区间。canonical JSON为2,543,082 UTF-8 bytes（约2.43 MiB），本地冻结含读取和序列化耗时0.2968秒。每项保存原screen行快照；多模板最多100来源另加手动open来源，大context仍可能先达到4 MiB硬限，必须形成明确partial或受控容量错误。该测量仅是本机合成样本，不是生产延迟承诺。500条持仓沿用导入契约；100项批次和分页沿用现有计算契约。TTL是可撤销缓存政策而非数据有效期；精确依赖变化优先于TTL。G4实际查询数和100项批次测量见acceptance.md。

## 3. 合成验收样本及独立预期

以下所有数值均为合成输入，expected 由公式独立手算，不调用实现生成。`financial_status` 表示有限财务检查；完整 P1 bucket 若未 blocked 则为 incomplete，因为 supplemental/research 固定未集成。费用为 `fixed + q×e×rate`；持仓市值按估值价 `v`。

| 样本 | 独立输入与 expected | known / unknown、状态与引用边界 |
| --- | --- | --- |
| F01 最低建仓 | `q=100,e=v=1500,fixed=10,rate=0`，单股上限10%。交易后持仓市值是 `100×1500=150000`，fee只从NAV/现金扣除。NAV 200,000：`150000/(200000−10)=0.750037501875… > 0.10`，blocked；NAV 2,000,000：`150000/(2000000−10)=0.075000375001… ≤0.10`，必要输入齐时 pass。 | 合成交易规则。两者整体 bucket incomplete（除非财务 blocked）；无真实行情/交易单位声明。仅创建显式用户请求的 fit，不自动候选/研究任务。 |
| F02 双价格/open/add | `v=1500,e=1600,q=100,fee=10,NAV_before=1,000,000,cash_before=500,000`。两种情景都扣现金流 `100×1600+10=160010`，新增持仓估值 `100×1500=150000`，所以 `cash_after=339990`、`NAV_after=1,000,000−160010+150000=989990`。open: 原目标为0，目标市值150000、权重 `150000/989990=0.1515166819…`。add: 原持仓50股，目标市值 `(50+100)×1500=225000`、权重 `225000/989990=0.2272750229…`。 | 费用 `10+100×1600×0=10`；估值价v与成交价e分开。账户其它资产随情景分别调整以维持相同起始NAV；不套 `NAV_before−fee`。 |
| F03 未知与原始0 | 同F01，但 fee unknown 或 known fee=0；另设 cash/NAV/交易单位/CSRC unknown、partial snapshot 缺目标；非正NAV样本 `NAV=0`。 | known 0 与 unknown 区分；未知依赖项维持 unknown，不补零；缺目标不推定open/add；比值以 NAV≤0 时 unknown。若存在独立确证违规，可 blocked 与其他检查 unknown 并存。 |
| F04 用途与hold | 同账户 cash=100,000；用途A金额60,000、已分配预算40,000、required_cash=20,000；全账户现金下限50,000。 | 现金需留 `max(20,000,50,000)=50,000`，不能扣 `20,000+50,000`。用途绑定分别覆盖非空未选、确认空、无头、读取失败；hold无交易规则/费用/purpose_id仍进行持仓、NAV、现金留存检查。 |
| F05 全集与分页 | 2,001 unique listings（至少一项仅出现在 truncated hit），其中同股多模板；100页拼接。 | 1,000 hit + 1,001 truncated作为来源示意；去重后候选声明全集保留来源。受2,000成员 cap 时 coverage partial、明确处理前缀/未处理数，不能把仅 hit 的1,000冒称全量。无写入候选、无草稿/LLM任务。 |
| F06 并发、恢复与时点 | 同 key/body重放；同key异body；入队后 source hash变化；用途在 `price_basis_at` 后但不晚于 `assessment_as_of` 确认；worker失租；执行时关开关。 | 重放原 run/as_of/manifest；异body409；冻结输入变化依 current-limits stale，不换 latest；用途按两时点规则纳入；失租不可提交；关停 failed/writes_disabled，无半份排序/自动换key。 |
| F07 研究边界 | published观点的action为 avoid/sell 或条件未知；政策线索仅 `user_report` 且业务范围unknown。 | P1 `research_status/company_alignment=not_assessed`、supplemental not_integrated、bucket incomplete；不推断买入支持/硬违规，不核验政策，不写候选或研究任务。 |
| F08 提交与显示 | 全unknown；空候选；受控单项错误；系统DB错误；旧basis结果；比例weight=0.125；无snapshot/valuation；迟到旧页响应。 | 全unknown是正常成功；空全集是count=0成功；typed单项失败占位partial；系统故障执行失败；旧basis显示stale；0.125仅专属weight展示为12.5%；无snapshot POST422且不造快照；无估值有限unknown；epoch不符旧响应丢弃。 |

所有fixture标注 `synthetic`，不得冒充生产规则或报价。生产就绪依赖另行核验：四市场交易单位/版本和生效区间、未复权价格及交易日历、费用来源、账户覆盖、CSRC快照和用途确认时点。

## 4. 就绪检查与 G0

现有用户约定是 owner 级单一 `MandateHead`，其中 `portfolio_constraints` 一次只绑定一个账户。P1沿用该权威：其他账户没有适用的结构化约束时返回 `account_constraints_not_configured`/unknown，不从旧约定复活阈值，不按账户虚构第二套当前约定。两账户F01的真实浏览器样本须通过既有用户确认修订接口切换适用账户后分别生成；同时维护多账户独立约束属于后续数据模型工作，不由本功能暗中扩展。

| 生产依赖 | 当前检查结果 | P1边界/责任 |
| --- | --- | --- |
| 主板/创业板/科创板/北交所可信交易规则 | 未发现可作为默认可信 resolver 的来源；现有 `UnavailableTradingRules` 默认返回缺失 | 生产 open/add 交易单位与费用依赖保持 unknown；P1实现resolver协议接缝，不开发provider |
| 未复权价格、交易日历与时效 | 已有 `MarketSnapshot` adjust 字段、`latest_price_as_of`、组合估值时效配置；未知年份日历可返回不确定 | 使用已有数据并冻结知识/交易时点；未知日历不按工作日猜测 |
| 用户费用假设 | 旧 `ScenarioAction` 有 fixed_fee/fee_rate，但无账户fit确认来源 | 新 DTO 显式传入情景假设并记来源；缺项unknown，不拿旧scenario默认为用户确认 |
| 账户起点及snapshot覆盖 | `PortfolioService.snapshot`依赖已确认账本；缺失时not_found | context返回 typed missing；admission拒绝；不自动建档 |
| CSRC 分类 | `IndustryClassification` 有 interval/source/captured_at，但当前代码模型无不可变 revision | 冻结实际行内容/hash和可用时间；筛选 SW 标签不替代CSRC |
| 用途 | `PurposeVersion` 有 exact ref、account、snapshot、confirmed_at | 冻结 exact版本；首次新增用途使 `none_recorded` 结果 stale |

G0退出：P0四节可直接约束DTO/测试；状态、引用、时点、容量、TTL、错误映射已落定；P1限制为两表/本地确定性job/默认关闭；生产缺口有归属且不阻止unknown路径；既有候选、ag22、公司数值解析语义列为兼容门槛。G0不等于真实数据或研究质量验收通过。
