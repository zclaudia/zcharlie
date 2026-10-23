# 个人投资 Agent 实现任务清单

> 2026-10-11 上下文与预算修复最新状态：实现实际请求预留、旧交互投影、强制收尾与前序证据恢复。有限真实验证累计十五次 HTTP，十四次已知用量共 209496 tokens，另一次未知；仍无合格终稿，最后阻塞为引用契约及供应商请求失败。详见[修复与真实验证](probes/ag20-context-budget-2026-10-11.md)。


日期：2026-10-10。本文将已评审方案拆为 30 个可分别实现、提交和验收的任务。AG01 至 AG20 交付首个公司观点闭环，AG21 至 AG24 加入真实组合，AG25 至 AG28 扩展取证与自主运营，AG29 至 AG30 交付多资金用途与方法改进。

独立任务意味着：前置接口可用后，可以单独开发并验收，不需要等待整个 feature 完成；它不意味着没有依赖。每项默认是一组可单独评审的变更，可包含必要的迁移、服务、接口和测试。任务完成与产品阶段开放分别判断，未通过发布门槛的能力保持关闭。

产品规则以[主设计](investor-agent-design.md)、[研究与运营设计](investor-agent-research-policy.md)、[领域契约](investor-agent-contracts.md)、[估值设计](investor-agent-valuation.md)为准；框架、阶段与探针记录以[技术设计](investor-agent-architecture.md)为准；页面依据[原型](investor-agent-ui-mockup.html)。本文只分配实现责任，不重新定义这些规则。

## 当前交付状态（2026-10-11）

30 张工程卡均由独立 gpt-6.1-sol 子 Agent 按授权顺序完成（AG14 先于 AG13），主 Agent 已完成逐项评审及最终独立核对；历史各卡记录和产品阶段门槛保留。当前迁移 head040，真实库仍021（360513536 bytes，mtime1791489715）；AG30 没有真实迁移、设置修改、付费模型调用、通知或 commit/reset。最终离线全量 **2195 passed、3 skipped、6 deselected，280.50s**；Ruff、mypy273、JS syntax、diff 和1440/390浏览器通过，3skip来自未配置的外部gateway checkout。

工程完成不代表产品发布。Agent/后台/组合/外部通知默认关闭；AG20实际6HTTP/75669tokens/0有效研究，之后身份修正只做离线验证；FCFE0/4，生产可信交易规则Unavailable。原文解析覆盖UTF-8 HTML和文本PDF，最多4MiB/100页/500片段/200k字符，无OCR或新增FCFE覆盖；可选摘要通知未实现。真实研究、人工理解与阶段质量门槛仍pending。AG30只允许有限验证步骤加强，离线重放不是前瞻证据，用户明确批准后才推进精确listing的方法头。

### AG20 后续修复与有限复验（2026-10-11）

[失败分析与结果](probes/ag20-followup-2026-10-11.md)：已对齐证券身份节点 schema、加入同 Task 的有界校验反馈、区分初始计划与最终结果，并缩短模型端进展保存回执（完整账本保留）。本轮新增 10 HTTP /159102 tokens，海康威视完成 4 次快照查询，国电南瑞完成 8 次查询及 6 次候选证据保存，仍无合格研究或正式观点；预算预留造成提前停止，最后短回执优化仅离线验证。阶段开放继续 pending，正式库和配置未改。最新全量回归 **2202 passed、3 skipped、6 deselected，366.12s**，Ruff/mypy273/diff 通过；此前各卡测试数字保留为历史记录。

## 任务总览

“前置”列给出完整验收要使用的关键任务依赖，未逐项展开全部传递依赖。接口固定后的纯计算、页面和 fixture 开发可以提前开始，但模拟接口不算联调完成。表列任务范围和依赖；实际实现状态以各任务完成记录为准，设计文档完成不等于代码完成。

| 编号 | 阶段 | 独立交付物 | 前置 |
|---|---|---|---|
| AG01 | A0.1 | 领域类型、服务接口与固定样本 | 无 |
| AG02 | A0.0 | 模型能力验证收口 V1 V2 | 无；复用已有探针 |
| AG03 | A0.0 | 现有估值字段覆盖报告 V3a | 无 |
| AG04 | A0.1 | 投资约定与研究档案基础服务 | AG01 |
| AG05 | A0.1 A1a.1 | 证据存取与现有数据查询工具 | AG04 |
| AG06 | A0.2 | 任务状态机与持久动作账本 | AG04 |
| AG07 | A0.3 | 独立模型配置与框架适配 | AG01 AG02 |
| AG08 | A0.2 A0.3 | 权限预算与真实执行器接通 | AG05 AG06 AG07 |
| AG09 | A0.2 A1a.3 | 会话消息 API 与可恢复事件流 | AG08 |
| AG10 | A1a.1 | 估值计算工具与计算记录 | AG03 AG05 |
| AG11 | A1a.1 | FCFE 字段补采与口径核验 V3b | AG10 |
| AG12 | A1a.2 | 公司意见发布与有效性服务 | AG06 AG10 |
| AG13 | A1a.2 | 分层研究与反证流程 | AG08 AG12 AG14 |
| AG14 | A1a.3 | 用户决定、操作权限与偏好记忆 | AG09 AG12 |
| AG15 | A1a.4 | 监测复核与站内提醒 | AG13 |
| AG16 | A1a.2 A1a.4 | 有限线索发现与研究队列 | AG13 |
| AG17 | A1a.3 | 桌面与手机对话工作台 | AG09 |
| AG18 | A1a.3 A1a.4 | 观点证据、线索比较与复盘界面 | AG15 AG16 AG17 |
| AG19 | A1a.3 | 启用、设置、控制与记忆界面 | AG07 AG14 AG17 |
| AG20 | A1a 验收 | 公司观点闭环验收与内部试用 | AG01 至 AG19 |
| AG21 | A1b.1 | 真实组合账本与快照 API | AG20 |
| AG22 | A1b.2 | 组合资金与约束计算 | AG21 |
| AG23 | A1b.2 | 组合方案与行动建议服务 | AG22 |
| AG24 | A1b 验收 | 组合交互与完整闭环验收 | AG23 |
| AG25 | A2.1 | 公告发现、下载与原件保存 | AG05 AG20 |
| AG26 | A2.1 | 原文解析、事实核验与更正传播 | AG25 |
| AG27 | A2.1 | 受控搜索与跨公司取证扩展 | AG16 AG26 |
| AG28 | A2.2 | 扩大后台运营与可选外部通知 | AG15 AG16 AG26 |
| AG29 | A3 | 多资金用途的方案比较 | AG24 |
| AG30 | A3 | 方法提案、评估、影子运行与回退 | AG20 |

## 共用执行约定

- 开始单项任务时阅读本文对应任务、前置任务完成记录及相关专题；先核对工作区已有改动，不重写他人正在进行的实现。已有 SDK 和框架探针继续复用。
- 第一版保持单主 Agent、Python 与 Pydantic AI、现有 FastAPI、SQLite、worker、Jinja 和原生 JS。模块路径下文均以 `src/investment_agent/` 为根；建议的新文件在实施时按实际结构落位，不预占迁移编号。
- 每项业务任务同时交付本项的服务、必要接口、错误处理和有意义的回归。测试围绕状态、授权、证据、幂等与数据边界，不留到 AG20 才补。AG20 和 AG24 负责跨任务集成与产品验收。
- 离线回归使用固定 fixture 和可控时间；真实端点、真实数据与人工研究质量评审分开记录。验收至少记录执行命令、结果、能力限制及尚未验证项，不虚构测试通过。
- 保留现有 `ruff check .`、`mypy src`、`pytest -q -m "not network"` 门禁，使用仓库环境及 CI 的惰性测试凭证；修改前端时另做 JS 语法、CSP 与实际桌面/手机交互检查。
- 新 Web 实现复用现有安全 DOM 和认证机制，不能把原型的内联样式直接复制进产品。新 Agent 页面允许设计内的投资意见，旧页面与规则草稿的禁用词约束仍保留；对相关守卫测试按模块范围调整，不能全局删除限制。
- 每个阶段有独立启用条件。A0 的关闭入口与停止执行能力由 AG08 实现；AG20 才决定开放公司研究入口，AG24 才开放组合建议。已有数据和解释链路持续可用。
- 任务完成后在对应条目追加简短记录：状态、变更/提交引用、验证证据、公开接口及已知限制。编号保持稳定；新增需求另列，不把它塞进已完成任务。

## A0 与 A1a 的任务边界

### AG01 领域类型与固定样本

- **目标与范围：** 将领域契约转为 Pydantic 类型和应用服务接口，固定 ID、revision、Decimal、时间、证据引用、公司意见、任务事件、错误与上下文绑定；为 A1b/A3 只保留必要的扩展边界。
- **交付与位置：** `investor/`、`agent/`、`evidence/` 的契约模块，以及 `tests/fixtures/agent/` 固定材料与事件序列。先落地 S01 至 S10、S16、S17 及 S14 偏好部分的输入和预期不变量；后续任务补可执行断言。
- **独立验收：** 有效/无效对象可解析与拒绝；所有枚举能映射界面文案；消息与工具引用可序列化；fixture 不包含未来信息或真实凭证。
- **边界：** 不建通用工作流，不提前建组合或方法审批表。无数据库、模型与页面实现也可以完成本任务。

#### AG01 完成记录（2026-10-10）

- **状态：完成（契约级验收）。** 新增 `investor/common.py`、`enums.py`、`contracts.py`、`services.py`，`evidence/contracts.py`、`services.py` 与 `agent/contracts.py`、`services.py`；没有数据库、执行器、模型调用或页面实现，也未提交 Git。
- **公开接口：** UTC/Decimal/显式未知值与版本引用；mandate、case、episode、plan、thesis、公司意见与有效性投影、发布检查、monitor、review、决定、偏好；证据/计算与泛型工具结果；会话/消息/任务、固定上下文与九种判别式公开事件。应用服务 Protocol 将查询与写入分开，服务实现及事务错误映射由后续责任任务完成。
- **身份接缝：** 现有 listing、financial/market snapshot、candidate、watchlist_item、pipeline_run 引用保留正整数主键；新领域对象使用 UUID，引用类型校验两种命名空间，不创建伪证券身份。服务沿用单用户 `local_owner` 边界，不建立用户表。旧快照的 revision 对应保存版本，来源适配由 AG05 固定。
- **固定材料：** `tests/fixtures/agent/` 含虚构公司财务字段、有限公司意见、消息受理响应、九种公开事件以及 S01–S10、S16、S17、S14 偏好部分的输入/顺序/预期不变量；新期间值只在后续事件出现。无真实凭证，未使用原型财务数字作为真实事实。
- **验证：** `.venv/bin/ruff check .` 通过；`.venv/bin/mypy src` 通过（201 source files）；`.venv/bin/pytest -q tests/test_agent_contracts.py` 通过（30 passed）。修改前完整离线基线由主任务执行，1537 passed、6 deselected；该基线不计作本次新增业务集成验收。
- **实际限制：** Pydantic 验证只覆盖对象形状、身份命名空间、UTC、版本、十进制、公司 scope/七项完整性、等待/终态结构与序列化；引用解析、事实支持、数值发布、权限、幂等、并发、续期、撤销传播及研究语义由后续服务实现。固定场景是验收输入和不变量，未冒充 S01–S17 的业务执行通过。A1b 只保留 snapshot/plan 引用边界，A3 只保留固定方法引用；组合行动和方法审批未提前实现。

### AG02 模型能力验证收口

- **目标与范围：** 复用已有两份 spike，补齐同一网关第二模型，以及错误参数、缺失、请求失败、输出错误和有限重试；逐模型记录输出模式、profile、流式事件及 usage。
- **交付与位置：** `tools/spike_tool_calling.py`、`tools/spike_pydantic_ai.py` 及技术设计中的脱敏 V1/V2 结果。保留已完成的单模型结果，按剩余缺口运行，不默认重新消耗全部验证预算。
- **独立验收：** 两模型各有可用配置或具体不支持项，失败可复现且不会无限重试；后续适配器可以直接使用经验证的配置。端点不可用时如实留下未完成项。
- **边界：** 不要求第二供应商、不验证应用崩溃恢复；不把单次注入样本通过当成完整权限保障。

#### AG02 完成记录（2026-10-10）

- **状态：完成（两模型 V1 / 有界 V2 验证，含明确不支持项）。** 复用两份未提交 spike，保留已有 `deepseek-flash` 结果；受控 models 列表选择同网关 `deepseek-v4-pro`，没有新增生产适配器、恢复执行器或 Git 提交。
- **变更与证据：** `tools/spike_tool_calling.py`、`tools/spike_pydantic_ai.py` 增加整项超时、整组 HTTP cap、SDK retries=0、脱敏错误与报告、未知 probe/漏测显式失败；框架新增缺失/ModelRetry 真探针与九项 FunctionModel/MockTransport 离线故障。`tests/test_model_spikes.py` 新增 10 个回归；逐项请求/事件/usage/profile 报告见 `docs/product/probes/ag02-model-probes-2026-10-10.json`，实际命令见技术设计 AG02 收口。
- **公开可用配置：** 两模型 `OpenAIChatModel` + 既有 tier-3 网关/凭证引用，thinking=`auto`，`PromptedOutput` 可用；`ToolOutput` 需显式 `openai_supports_tool_choice_required=False`；默认 required 与 NativeOutput/json_schema 在第二模型均 HTTP400，不启用该能力。DeepSeekProvider 对官方 v4-pro 名自动 forcing=False，对现有 flash 别名仍不能替代显式配置。框架/SDK 参数 CLI 为 `--model`、`--only`、`--timeout`、`--max-requests`、`--json`；框架额外 `--offline` 不读取设置或调用网关。
- **验证：** 第二模型框架补测 14 pass / 2 unsupported、SDK 2 pass；第一模型只补缺失/有限工具重试 2 pass。合计 35 聊天 HTTP 尝试、16,989 输入 / 2,239 输出，另 1 models 发现；未重跑旧单模型全矩阵。离线 9 项故障全部通过；`.venv/bin/pytest -q tests/test_model_spikes.py tests/test_agent_contracts.py`：40 passed；`.venv/bin/ruff check .`、`.venv/bin/mypy src`：通过（201 source files）。历史完整基线 1537 passed / 6 deselected 不冒充本次完整回归。
- **实际限制：** runtime settings 读取抛 `OperationalError`，未定位根因，本轮使用既有环境 bootstrap；错误参数/输出/HTTP503 等有界停止是确定性离线验证，不冒充真实供应商故障表现；实际价格与失败调用 usage 未知，不能记零费用。模型仍补证券身份/TTM口径，flash 再现 bn 换算错误；能力通过不代表可发布研究正确。生产权限、引用与数字校验、取消/恢复和审计结算由 AG05/AG07/AG08/AG12 接通。V3a 不在本任务内。

### AG03 现有数据覆盖审计

- **目标与范围：** 用已有快照核对市值含义、PE/PB 历史长度和时效、现金流金额字段与来源覆盖，产出 V3a。
- **交付与位置：** 可重复运行的覆盖统计入口、固定范围/as_of 的报告与缺口清单；复用 `storage/repositories/`、`factors/valuation.py` 与数据健康信息。
- **独立验收：** 分母、目标池版本、缺失原因和真实统计来源可查；不把缺列当成零，也不把结构化快照数量当成可用 FCFE 覆盖。
- **边界：** 不触发全池补采、不实现新估值公式；单项缺口只限制依赖该字段的能力。

#### AG03 完成记录（2026-10-10）

- **状态：完成（V3a现有字段审计，未启用FCFE）。** 新增 `investor/coverage.py`、`tools/audit_valuation_coverage.py`、`tests/test_valuation_coverage.py`；固定报告与逐证券来源/原因见 [AG03说明](probes/ag03-valuation-coverage-2026-10-10.md) 与 [JSON](probes/ag03-valuation-coverage-2026-10-10.json)。无采集、迁移、新估值公式或Git提交，保留全部原有未提交改动。
- **公开接口：** `audit_session(session, universe, as_of)` 与 `audit_database(path, universe, as_of)`；CLI要求显式 `--database` / `--as-of` / `--output`，可选 `--universe` 与 `--expected-pool-version`。SQLite `mode=ro` + `query_only=ON` + 显式 `BEGIN`固定一致读快照；缺文件不创建，缺schema/目标池明确失败，不静默切换库。
- **真实范围与结果：** 进程打开文件证实实际启用 `.dev/data/app.db`；默认 `data/app.db` 只有4096字节且无表，解释AG02 settings `OperationalError`的schema缺失，配置/恢复副本未替换。CSI300/as_of=2026-10-10分母300，成员版本 `265af9bd154b472feb4e11cf2776033ed886bd540caead86ca498915293228b8`。市值输入300；PE分位278（252完整、26近似、21负PE、1少于240），PB299（279完整、20近似、1少于240）。所有最新行情为10月8日，无超过14日的新鲜度标记；库中无开放report_overdue。CFO/capex原始金额各300，2023–2025三年度两金额齐全300；原始金额来源、版本、期间、availability与raw_ref逐项保留，库存行数不作分母。
- **口径与限制：** `market_cap`为未复权收盘价×总股数（非流通市值），尚未核验普通股归属与FCFE一致。分位直接复用已有5年/240正值日/31日容差与时效算法。现金流不将ratio推导或CFO-capex当FCFE；借还款、发债、现金利息、归属、币种/单位/合并范围和重大非经营/资本结构因素留AG11。首次捕获在10月6–7日，历史backfill按available_from重建不代表系统当时持有原件；没有后移as_of提高覆盖。V3b三层统一为未核验，未写成0或100%覆盖。
- **验证：** `.venv/bin/pytest -q tests/test_valuation_coverage.py tests/unit/test_factor_valuation.py`：29 passed（本项新增16）；`.venv/bin/ruff check .`通过；`.venv/bin/mypy src`通过（202 source files）；`.venv/bin/pytest -q -m 'not network'`：1593 passed、6 deselected。真实审计入口按报告命令重复执行，完整报告及 `data_version`一致。离线fixture只验证算法边界/PIT/去重/只读失败，不冒充真实覆盖；无网络验证。

### AG04 投资约定与档案基础

- **目标与范围：** 持久化 mandate 及修订、稳定 case、episode、计划/产物引用与用户固定标记；支持创建、查询、版本冲突和当前约定读取。
- **交付与位置：** `investor/` 服务，`storage/models/`、`storage/repositories/`、必要 Alembic 迁移及 `/investor/mandate`、`/investor/cases` 基础接口。现有候选/自选只建立可追溯关联。
- **独立验收：** 同证券不重复建 case；旧修订不可覆盖；未知投资信息保留未知；旧候选晋级和规则 API 回归通过。
- **边界：** 本项不发观点。归档撤回意见、暂停运行任务和具体操作提案的跨服务行为由 AG14 接通，不能先做看似成功的空操作。

#### AG04 完成记录（2026-10-10）

- **状态：完成（基础业务验收）。** 新增 `investor/foundations.py`、`storage/models/investor.py`、`storage/repositories/investor.py`、`api/routes/investor.py` 与实际 Alembic `025_investor_foundations.py`（核对原 head 为 024）；入口注册、Alembic 模型导入、测试模型导入及 backup/funnel 的 head 断言同步更新。保留已有未提交设计、AG01–AG03、README 与本地启动配置；未提交 Git。
- **持久化与事务：** local_owner 的当前 mandate 指针与不可变修订；owner+listing 唯一稳定 case、不可变 case revision、首个 episode、不可变 episode/plan 修订和计划/产物引用；用户 pinned 独立保存。修改以 SQL 条件 revision CAS 更新当前对象，历史、引用与参数 hash 绑定的幂等回执同事务保存；服务保存点可被外层事务回滚。旧版本可明确读取。并发唯一冲突及 SQLite busy/locked 返回可重试 `revision_conflict`，同键不同参数为 `idempotency_conflict`；竞争中的请求可能先收到 409，重试同一完整请求读取已保存回执。
- **身份与旧功能：** 不新增用户表，复用整数 listing/candidate/watchlist 身份，新对象 UUID 由服务生成，用户写入 actor 与时间由服务确定。现有 listing、candidate、watchlist 无独立版本列，本项引用固定 revision=1；来源必须存在并属于相同 listing。重复创建 case 返回现有对象，不暗改 pinned 或来源；追加来源走显式 CAS 操作。候选晋级、规则批准及旧决定账本保持原接口，创建 case 不生成 thesis/recommendation、不改候选状态或规则。
- **公开服务：** `InvestorService.get_mandate(owner, revision=None)`、`get_case(owner, id, revision=None)`、`detail`、`create_mandate`、`revise_mandate`、`create_case`、`set_pinned`、`save_plan`、`attach_artifacts`、`link_source`；仓储提供分页查询和 CAS。AG01 的 `revise_mandate`/查询签名继续可用，历史查询的可选 revision 为兼容增量。写服务由调用方提交外层事务。
- **HTTP：** GET/POST `/investor/mandate`，POST `/investor/mandate/revisions`，GET/POST `/investor/cases`，GET `/investor/cases/{id}`，GET `/investor/cases/{id}/revisions/{revision}`，PATCH `/investor/cases/{id}/pinned`，POST `/investor/cases/{id}/plans`、`/artifacts`、`/sources`。创建 mandate 输入 `{candidate, idempotency_key}`，修订/plan 输入 `{candidate, mutation}`，pin 为 `{pinned, mutation}`，来源为 `{ref, mutation}`，产物为 `{refs, mutation}`；mutation 含 expected_revision 和 UUID 幂等键。case 创建输入 listing_ref/question/idempotency_key 与可选 pinned/source_refs。查询支持 listing_id/status/after(UUID 游标)/limit(1–100)，status 在 SQL 分页前过滤。所有接口复用 `verify_api_key_or_session` 与现有 cookie 同源写入校验；未找到为 404、无效引用为 422、版本/幂等冲突为 409，错误结构使用 AG01 BusinessError。
- **验证命令与结果：** `.venv/bin/ruff check .` 通过；`.venv/bin/mypy src` 通过（206 source files）；`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q tests/test_investor_foundations.py tests/integration/test_migration_025.py` 为 7 passed；backup/funnel 专项为 2 passed。最终固定代码执行 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m "not network"` 为 **1600 passed、6 deselected，90.72s**；`git diff --check` 通过。专项覆盖不可变旧约定/旧case、显式未知、同键重放/参数冲突、来源/预算引用、分页前状态过滤、cookie CSRF、两个独立会话并发同键建档和并发CAS修改、缓存刷新与外层回滚，以及临时库 024→025、fresh head/模型一致性、025→024 保留旧表。
- **实际限制与交接：** 本项仅提供基础 API/存储，未开放 Agent 工作台、创建任务或后台研究；归档/reopen/暂停/静音、意见发布及撤回仍由 AG12/AG14 接通，不提供空操作。首个 episode 已保存；重新研究增加 episode 的业务动作随 AG14 实现。产物关联当前只接受本 case 中可解析的已保存 plan；AG05/AG12 接入证据、计算、thesis 等真实存储后扩展解析，不能把任意 UUID 当已存在产物。plan budget 必须指向本 owner 的真实 mandate 修订；假设/取证步骤仍是 AG01 计划契约，本项不声称执行或核验证据。保留 AG01 完整 candidate 输入，新建对象的客户端 id/actor/created_at 不作为授权或实际生成元数据。
- **真实环境：** 全部数据库验收只使用临时库。AG03 已确认运行 API 指向 `.dev/data/app.db` revision 021、默认 `data/app.db` 为空；本项未升级或修改这些库，未替换配置、未打印真实凭证。运行实例要使用新代码和 025 schema 后才具备新基础 API，本项临时库 TestClient 验证不冒充真实实例已部署。

### AG05 证据与查询工具

- **目标与范围：** 将 PIT 字段、已有事件和用户文本封装为 evidence；实现证券身份、快照、数据健康、同口径比较及受控刷新请求的领域查询适配。
- **交付与位置：** `evidence/`、证据存储和 `/investor/evidence/{id}`；Agent 查询工具的普通 Python 服务实现。计算记录的引用与存储接口在此固定，计算方法由 AG10 提供。
- **独立验收：** 引用可回到保存值、主体、期间和版本；缺失、冲突、过期分别表达；用户文本保持 user_report；刷新走既有任务而非同步阻塞模型请求。
- **边界：** 不下载公告正文，不开放任意 URL。内部适配可复用只读服务，外部 MCP 不获得写入或入队权限。


#### AG05 完成记录（2026-10-10）

- **状态：** 已实现证据/计算存取与普通 Python 查询适配，工作区未提交；未实现模型执行器、估值算法或原件下载。
- **变更与存储：** `evidence/store.py`、`queries.py`、`snapshots.py`、`events.py`；新增不可变、owner 隔离的 `investor_evidence`/`investor_calculation` JSON payload 表与真实 head 025 后的迁移 `026_investor_evidence.py`。注册模型并更新现有 head 断言；临时库验证 025→026、fresh head/metadata 一致、026→025 保留 AG04/旧表。复用 AG04 owner/参数 hash 幂等收据、冲突映射与外层事务提交约定；新 ID、revision=1、actor、创建时间由应用生成。AG04 episode 产物现可关联同 case 主体的已保存 evidence/calculation；不存在、错主体与不可解析引用拒绝。
- **公开接口：** `EvidenceService.get_evidence/get_calculation/save_evidence/save_calculation` 实现 AG01 Protocol；查询还支持主体校验、新鲜度及 `visible_at`。`SnapshotQueryService.resolve_security/get_snapshot/get_event/compare_stocks/get_data_health/capture_snapshot` 复用真实 listing、PIT repositories、canonical provider 字段映射、field-source 登记、既有健康聚合。`RefreshService.request_data_refresh` 仅向既有 worker 入队 baostock `market_snapshot`/`financial_snapshot`，返回 pending、真实 pipeline_run ref 与 dependency_id；整项幂等绑定 ticker/scope/as_of/report_period/kind/payload，跨 kind 同键也拒绝不同参数。HTTP 新增认证 GET `/investor/evidence/{id}?revision=1&as_of=...&visible_at=...`，返回保存值/文本、主体、期间、来源版本、定位、hash、公开/取得/可用时间、核验和更正关系；不存在/不可见为 404，无时区查询时间为 422。未增加外部 MCP 写入/入队工具。
- **数据与时间边界：** 只读取已有证券身份，不自动创建证券；财务字段单位来自明确可信适配与登记，余额时点、TTM、期间累计分别保存期间口径，不暴露含义模糊的 net_profit/revenue。actual/approx availability 的日期精度保留在 warnings/text，不将日期冒充精确公告时刻；未知值保存原因并返回 missing，跨源冲突返回双方原始候选和 conflict，过期为 stale，不补零或由模型任选来源。计算写入会检查截止点可见输入并重新检查同主体/期间的跨源冲突。研究 `as_of` 限制来源公开/取得时间和计算输入截止，允许 t0 截止材料在 t1 被包装/计算后供同任务解析；显式 `visible_at` 另约束保存对象的创建时间，用于当时已保存对象的历史展示。默认不放开 backfill 的取得时间，不改旧 kernel 的历史重建政策。
- **来源与情景：** 快照/已有业绩预告、披露预约、主营构成证据重新校验真实记录的主体、期间、版本、字段、单位、值/hash/时间/冲突，路径存在不算来源核验；事件引用使用既有整数主键、单 capture revision=1，保留“预告/预约”语义，非已实现利润。用户提交保存原始值/文本为 user_report，并以应用捕获时刻和本记录内原文/值定位替换调用者来源/回填时间；报告不会被提升为 verified。计算事实拒绝 user_report；可选内嵌 `CalculationAssumption` 明确 method_default/user_scenario/agent_scenario，user_scenario 必须引用已保存用户提交。计算方法/input_hash/结果正确性及发布适用性由 AG10/AG12 核验；本项只存储和解析，不运行算法。计算非 OK 保留原 CalculationStatus，并映射 missing/stale/error，未将不可适用统一当成系统异常。
- **验证：** `.venv/bin/ruff check .`、`.venv/bin/mypy src`（211 source files）与 `git diff --check` 通过。`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q tests/test_evidence_services.py tests/integration/test_migration_026.py tests/integration/test_migration_025.py tests/test_investor_foundations.py tests/unit/test_mcp_tools.py` 为 **52 passed**；覆盖引用回溯/错主体/期间/单位/版本、未知/冲突/过期、未来与晚取得 backfill 不可见、用户回填时间覆盖和值-only定位、同键重放/参数冲突、研究 cutoff 与对象历史时点分离、选择单一冲突来源也不能写成功计算、仅入队刷新、API 认证和只读 MCP 数据库拒写。最终固定代码执行 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m "not network"` 为 **1610 passed、6 deselected，93.38s**。
- **限制与交接：** 已有 collector 支持 watchlist/universe 范围，本项只开放明确授权的 watchlist 整体 scope；`authorized_scope` 是可信执行器构造依赖，模型输入不能自行授予，AG08 必须校验整个 watchlist 授权/预算/lease，而非只校验一个 ticker。尚不支持任意证券单独补采、任意 URL/PDF 原件、财务 derived 字段/FCFE 或旧系统缺少单位的字段；预告/预约/结构化源证据仍保留 candidate/质量，不声称核验原件。`get_data_health` 是当前运维视图，不作历史事实来源。AG09 后接真实 message 来源时须保留本项原始提交。AG10/AG12 接方法白名单、数值/情景语义和发布门槛；AG15 接更正/失效依赖传播。本项仅验证临时库，未升级 `.dev/data/app.db`（运行 API revision 021）或空默认库，未换配置/输出凭证；新 API 尚未部署到运行实例。

### AG06 任务状态机与动作账本

- **目标与范围：** 在既有 `pipeline_run` 和 worker 上增加业务 task、运行尝试、动作意图与结果、简单依赖等待、deadline、取消标记和持久事件。
- **交付与位置：** `agent/` 任务仓储与状态服务、`research/worker.py` 的必要接缝及迁移。定义 claim、checkpoint、settle_action、resume、cancel 等执行接口。
- **独立验收：** 用确定性假工具覆盖 S08/S10：提交后回传前崩溃不重复写入，丢 lease 后不提交，依赖重复到达只续跑一次，等待超时进入真实终态；终态不能重开。
- **边界：** 此处用假执行器验证基础设施；Pydantic AI deferred tools 与模型调用在 AG08 接入，SSE HTTP 在 AG09 实现。恢复粒度是已提交的业务边界。

#### AG06 完成记录（2026-10-10）

- **状态：完成（确定性任务基础验收）。** 新增 `agent/tasks.py`、`agent/job.py`、`storage/models/agent.py`，实际接续核对后的 head 026 为 `027_agent_tasks.py`；注册模型及 backup/funnel/AG04 head 断言更新。保留全部已有未提交修改，未提交 Git，未升级运行 `.dev/data/app.db`（revision 021）或默认空库，也未替换配置。
- **任务与尝试：** owner+输入键唯一业务任务、固定上下文、revision CAS、版本化检查点；任务与 pipeline_run 生命周期分开，实际 attempt 对应 run+lease_attempt+worker。等待保存依赖/deadline并将当前run结算释放worker；续跑创建新run。等待代次 `wait_token` 和持久 `dependency_id:completion_event_id` 去重防止同结果在后次等待再次解锁；终态不可重开，晚结果经后续执行器授权/时效检查后才能用 parent_task_id 创建新任务。到期按已有产物进入 partial/failed。running取消先保存 cancel_requested 并阻止新动作，当前调用结束、执行器确认停止后 `ack_cancel` 才形成 cancelled；queued/waiting可立即取消，已提交结果保留。
- **动作与事务：** 可信应用提供稳定业务 intent_key 与完整参数（阶段/目标/预期版本也应包含在意图或参数），服务生成 operation_id 并绑定参数hash；准备动作须独立提交后再执行。恢复先 `recovery_actions` 读取账本，按原意图/参数读取已提交结果，不依赖新模型 tool-call ID。数据库 callback、AG04/05同session服务保存点、动作结果、可选 `ActionResult.artifact_refs` 和公开产物事件同事务保存，服务不commit；实际AG04服务及外层整体回滚已有专项。动作提交前后复核pipeline lease和真实attempt/task/取消条件，丢lease整体回滚。SQLite writer边界与task revision CAS处理竞争；并发唯一/CAS/busy冲突需由调用方重试，不能创建替代业务键绕过去重。
- **外部副作用：** `start_external(expected_revision)`先持久不确定状态并返回动作代次，调用方提交后才能调用供应商；供应商使用稳定operation_id，返回/崩溃均按该代次对账。`reconcile_external(expected_revision, outcome=committed|not_applied|unknown)`提供真实确认/未发生/仍未知接口，允许取消后保存确认结果但不执行新副作用。revision条件更新拒绝旧轮对账，已提交结果不可覆盖；不声称SQLite对网络副作用提供恰好一次。
- **公开接口：** `TaskService.create/get_task/claim/fence/get_checkpoint/checkpoint/prepare_action/settle_action/recovery_actions/start_external/reconcile_external/resume/expire/expire_due/cancel/ack_cancel/append_event/get_events`；`TaskClaim`、`ActionResult`与要求真实executor的 `AgentJobHandler`。沿用ResearchWorker的claim、heartbeat、lease fencing、retry与stale recovery，不复制worker框架；真实worker注入假executor覆盖成功/取消claim race/失败。公开事件通过TaskEvent判别式校验，只接收可信应用公开摘要，不映射隐藏推理、凭证或原始供应商事件；queued、未认领取消和续跑等任务级事件明确attempt_id=null，执行事件绑定实际attempt。event_id持久全局递增，任务内允许间隔。
- **验证：** `.venv/bin/ruff check .`通过；`.venv/bin/mypy src`通过（214 source files）；`git diff --check`通过；`.venv/bin/alembic heads`为027。`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q tests/test_agent_tasks.py tests/test_agent_contracts.py tests/integration/test_migration_027.py tests/integration/test_migration_026.py tests/integration/test_migration_025.py`为 **51 passed**，其中AG06新增18个任务/故障专项及1个迁移测试；临时库026→027→026→027与agent metadata一致。覆盖提交后回传前故障/新lease恢复不重复写入、提交边界lease丢失回滚、重复输入/依赖/跨等待旧完成事件、deadline partial/failed、终态、阻塞外部调用中的取消与ack、外部不确定结果和独立session陈旧对账拒绝。固定最终代码后完整 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m "not network"`为 **1629 passed、6 deselected，98.84s**。
- **限制与交接：** 无真实模型调用、PydanticAI deferred恢复、权限/feature flag/预算执行或HTTP/SSE，本项不开放产品入口。AG08必须在授权配置下注册AgentJobHandler，executor在调用模型前读账本/对账，工具新动作与正式发布复查授权/版本/预算，并在调用结束或安全边界ack取消。`expire_due(now, limit)`是可执行的有限批量维护接口；AG08须在现有worker轮询前的维护事务注册调用，当前默认worker尚未注册Agent handler或该维护钩子，不能称生产会自动到期。AG09提供消息原子受理、SSE游标保留/过期与HTTP错误映射。外部unknown不能凭模型猜测继续，需供应商对账或明确失败；本项S08/S10只验收基础设施涉及的故障/取消不变量，真实发布/通知、预算耗尽、浏览器断线验收仍属AG08/09/12等后续任务。

### AG07 独立模型配置与框架适配

- **目标与范围：** 新增 Agent 专用 provider/model/凭证引用、profile、参数和输出模式配置，组装 Pydantic AI Agent/toolsets 并提供一次 run 的适配接口。
- **交付与位置：** `agent/` 的模型工厂和运行适配，复用 `settings/` 加密存储、`config.py` 与调用审计。设置 API 同步支持新配置；界面归 AG19。
- **独立验收：** 使用 AG02 的配置完成类型化工具与结果回灌；记录配置版本、调用和用量；修改 Agent 配置不改变旧解释链路，历史消息格式带版本。
- **边界：** 不自建工具循环，不在运行中静默切供应商；不重复承担应用任务调度、动作账本或每日预算。

#### AG07 完成记录（2026-10-10）

- **状态：完成（独立配置与原生框架适配验收）。** 新增 `agent/model.py`、`agent/runtime.py` 与 `agent_model_run` 审计，接续实际 head 027 新增 `028_agent_model_runs.py`；同步设置 registry/runtime/env 镜像、加密 secret、loader、设置 API 元数据和当前 head 断言。保留全部 AG01–06 与设计未提交改动，未提交 Git、未迁移运行数据库、未读取/替换真实凭证配置。
- **配置：** Agent 独立 `agent_provider/base_url/model/api_key/credential_ref/profile/profile_model/output_mode` 与能力覆盖、thinking、timeout、temperature、max_tokens、校验 retries；默认空端点/模型/凭证明确未配置，不回退旧解释模型。凭证引用固定 `agent_api_key`（沿用 Fernet 加密与脱敏历史），`openai`/`deepseek` Provider 可显式替换，官方模型名供 profile 推断但请求仍用配置别名。json_object=True、json_schema=False、tool forcing=False 固化 AG02 两模型共同能力；PromptedOutput 为默认，ToolOutput 显式 auto，NativeOutput 需明确开启已验证能力。旧 `llm_tier3` 与规则草稿配置保持独立。配置是不可变序列化快照，版本含 settings revision 与非密钥配置 hash；loader 最多三次 revision 前后重查，刷新已有 ORM 行，持续竞争显式失败，不声称 SQLite SELECT 自动形成一致快照。
- **公开接口：** `load_agent_model_config(session, base)`；`AgentModelConfig.from_settings/validate_ready/profile/snapshot`；`create_model`、`build_agent`；`run_agent`、`RunOutcome`、`AgentRunError`、`PrivateHistory`。一次 run 使用原生 Agent/toolsets/类型校验/ModelRetry/输出校验，不建工具循环；原生 DeferredToolRequests 与输出 marker 组合，保留 `deps/toolsets`、`history/deferred_tool_results`、`UsageLimits`、`CancellationToken`、`event_stream_handler` 和每次实际 HTTP 发送前 `before_model_request` 接缝。AG08 在检查接口注入可信权限/预算/lease并实现动作恢复与事件映射，本项不注册执行器或开放入口。
- **私有历史与审计：** `PrivateHistory` 的 schema_version=1、framework/version= Pydantic AI 2.31.1、config_version 与原生 messages JSON 可存 AG06 checkpoint供延迟恢复；配置版本变化拒绝直接续聊，须按业务状态重新组装，框架升级须显式迁移。history 可能包含隐藏推理/工具材料，仅私有持久材料，不能作为公开 TaskEvent。审计保存配置版本/脱敏快照、call_id、可选 task/attempt/run 关联、框架成功响应 requests、实际 HTTP attempts/status及供应商 token usage，不保存原始供应商包/推理/凭证/端点。未知 usage/价格保持 null 与原因，失败响应带 usage 而框架解析失败仍保留实际计数；SDK重试=0，框架校验重试有界。错误只提供类别/call_id，调用方在捕获错误/取消后提交审计事务。本模块不 commit、不承担调度/账本/每日预算。
- **验证：** `.venv/bin/ruff check .`、`.venv/bin/mypy src`（216 files）、`git diff --check`、`.venv/bin/alembic heads`（028）通过。`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q tests/test_agent_model.py tests/unit/test_settings_registry.py tests/unit/test_settings_store.py tests/unit/test_settings_consumers.py tests/integration/test_api_settings.py tests/integration/test_migration_028.py tests/integration/test_migration_027.py tests/integration/test_migration_026.py tests/integration/test_migration_025.py` 为 **83 passed**。包含16项原生 Agent专项、两模型/两输出模式实际 MockTransport请求、类型化工具结果回灌、deferred suspend→私有history→resume不重复工具、401/429/503无SDK重试/脱敏、缺usage、解析失败仍计usage、实际发送前授权拒绝、UsageLimits、原生SSE增量不缓冲、配置隔离/加密/版本并发与临时库027→028→027→028。依赖声明固定 `pydantic-ai==2.31.1`，同步 lock 中既有 pyproject 的 mcp/pypinyin声明（原lock缺pypinyin，新增该包，未升级框架或其他包）；`uv lock --check --offline`通过。最终固定代码完整 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m "not network"` 为 **1647 passed、6 deselected，100.58s**；Web/API专项26 passed。首次全量因新增bool输入元数据不被旧settings.js支持而失败，已改为既有text元数据（服务端仍按bool校验），没有扩展AG19界面。修正后的首轮全量出现既有worker heartbeat测试一次 `no such table: pipeline_run`，独立复测1 passed、最后全量通过；内存库/取消并发是可疑fixture原因，未认定根因，交AG08以临时文件库故障回归继续关注。
- **限制与交接：** 复用 AG02 真模型结果，本项未再次调用网络模型。原生 SSE 可通过事件 handler消费，HTTP审计钩子不读取/缓冲SSE，流式供应商 usage 当前未独立观测时明确 unknown，不写默认0；AG08须在自身结算接入可信流式usage观察。供应商价格未验证，cost始终unknown；此审计不等同可结算费用。函数模型属于离线fixture，HTTP尝试数为null，不伪称网络调用。模型输出是待校验材料，AG02证券身份/TTM/单位错误仍须AG12可信渲染处理。AG08负责检查工具动作和发布授权、动作账本恢复、预算、取消与公开摘要；AG19负责设置界面。

### AG08 执行权限预算与框架接通

- **目标与范围：** 把 AG06 任务和 AG07 run 接通，注入受信任的身份、lease、权限、工具范围、单任务/每日预算；映射框架事件并结算用量。
- **交付与位置：** `agent/` 执行器、工具守卫与预算服务，worker 注册和 `investor_agent.enabled` 执行检查。既有 iFinD 额度继续独立生效。
- **独立验收：** 模型不能伪造授权或 operation_id；并发预算不重复使用，跨 run 不重置；deferred 等待可跨进程回灌；取消、暂停或关闭功能后无新的调用/发布，已发生费用可查。
- **边界：** token 事后超限不宣称为绝不超支；不凭用户材料里的指令批准操作。本项交付真实运行接缝，不包含投资推理策略。

**完成记录（2026-10-10）：**

- **实际接口：** 新增 `ExecutionGrant`/`admit`/`check_boundary`、`BudgetService`、`ToolRegistration`/`TrustedTools`、AG05 只读与整体 watchlist 刷新工具适配、`AgentExecutor`/`RunSpecification`/可信 `CompletionPolicy` 和 `deliver_deferred_result`。复用 AG06 lease/action/external ledger 与 AG07 原生 Agent；默认 worker 注册真实 Agent handler，并在默认轮询前有限批量 `expire_due`，注入 handlers 与既有非 Agent 路径保持兼容。原生运行成功只保存未发布草稿；默认状态为有已保存产物的 partial 或无产物的 failed，正式研究完成须由 AG13 可信策略判定。
- **权限边界：** 可信 grant 持久保存，模型仅提供严格白名单业务参数，不能指定 actor、operation_id 或审批。入口与每次实际模型发送、工具动作、结算前业务写入/发布接缝重新检查 flag、lease、当前 mandate/scope、取消、时间和预算。listing scope 同样检查关联 case；后台 pause 不阻止明确交互，archive 仍阻止执行。整体 watchlist 刷新须覆盖当前全部成员。运行中取消在安全边界 ack，已发生调用费用仍提交；iFinD 额度独立。
- **预算与审计：** 单任务跨日/跨 run 累计，owner 每日额度以独立短 session 的 SQLite 写入 fence 原子预留；模型请求、token 和每轮返回的全部工具尝试均持久计数，另传原生 `UsageLimits`。实际发送前重查调低的额度，事后超额诚实记录并停止后续动作。HTTPTrace 确认供应商 usage 是否存在，缺失或未观察的流式 usage 为 null；unknown 阻止后续调用，不用框架默认零冒充费用。已知 usage 的可重试 HTTP 故障经既有 worker 有界重试并累计；未知调用不盲重放。取消/timeout 中短结算事务使用 AnyIO shield，审计在调用方提交，无跨 HTTP 的数据库事务。
- **可信价格：** `investor_agent_*` 预算/开关与模型配置键隔离，默认 enabled/background 为 false，价格未配置则成本 unknown。显式 USD 输入/输出单价须同时绑定报价版本、model 和完整模型参数 fingerprint（含 provider/endpoint）；费用使用预留快照，24,12 精度保存微小非零费用。模型 config version 现为参数 fingerprint，settings revision 单独审计；修改暂停/预算不破坏私有 history。未联网猜价，非 USD 金额授权无可信汇率时拒绝执行。
- **恢复：** 原生 DeferredToolRequests、私有 history 和 typed results 保存于 checkpoint，框架版本/config fingerprint 校验后继续；等待 token/依赖完成事件代次阻止迟到结果解锁新等待，独立 session 并发两依赖回灌不丢结果。外部动作保持 start→commit→HTTP→expected_revision reconcile；sent/unknown 先读账本和可信对账，禁止盲重放。生产执行器使用非流式模型请求和公开阶段/工具/usage 摘要，不建立自定义工具循环或工作流设施。
- **验证：** AG08 专项及模型/任务/设置/迁移联合验证 **132 passed，16.19s**，包含原生 FunctionModel、MockTransport、真实 worker 已知失败重试、取消/关闭/后台暂停在途边界、timeout 未知结算、跨日与文件 SQLite 并发额度、报价修改和微小费用累计、两执行器实例持久 deferred 恢复/两依赖并发回灌、外部账本恢复与重复结果。`ruff check .`、`mypy src`（221 files）、`git diff --check`、`uv lock --check --offline`、`alembic heads`（029）通过；029 迁移与 head 断言同步，仅临时测试库验证。完整离线命令 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m "not network"` 为 **1678 passed、6 deselected，109.52s**；首轮唯一失败是既有 default registry 精确集合断言未包含新增三种 Agent kind，同步后模块 9 passed，最终全量通过。全量完成后仅新增既有 PIT 守卫的五项原生测试（未来/naive 时间拒绝、历史时间允许、上海市场 date 边界），`tests/test_agent_execution.py` **35 passed，8.40s**；Ruff/mypy/diff 再次通过，未重新运行无关全量。
- **限制与 AG09 交接：** 两执行器实例验证持久恢复，不冒充真实 OS 进程崩溃验收（AG20）。AG09 接可信消息来源、同事务 admission、公开事件/SSE 与 `deliver_deferred_result`，不得公开私有 prompt/history/reasoning 或把供应商 tool ID 用作授权；事件 ID 保留与 SSE 重连由 AG09 实现。AG12 接正式发布服务，AG13 接研究策略/完成判定，AG14 接正式审批与控制操作；本项提供可信接缝但未实现其业务。未操作真实模型网络、真实数据库或真实设置，未升级运行实例 `.dev/data/app.db` revision 021 或空默认库。

### AG09 会话消息与事件接口

- **目标与范围：** 存储多对象会话、消息与 context_refs；同事务保存消息、task 和排队引用；提供任务查询、取消、SSE 及轮询回退。
- **交付与位置：** `api/routes/` 中 Agent 路由、消息仓储及 `/agent/conversations`、`/agent/tasks` 接口。前端可按 AG01 的响应 fixture 开发。
- **独立验收：** 消息重发不重复入队，同键异参返回冲突；Last-Event-ID 可恢复且客户端能去重；旧游标明确要求加载快照；鉴权/CSRF 生效，事件不输出隐藏推理或密钥。
- **边界：** 不因浏览器断线取消任务；不把某次框架 run 成功当作整个研究完成；页面切换不修改已发送消息的对象。

#### AG09 完成记录（2026-10-10）

- **状态：完成（会话/API/持久事件离线验收）。** 新增 `agent/conversations.py`、`api/routes/agent.py`、`agent_conversation/agent_message` 与迁移030；扩展已有 task 来源、事件保留与取消边界，注册认证路由并接默认worker有限维护。保留全部前置dirty改动，未提交Git、未新建线程或子agent；未升级运行实例 `.dev/data/app.db` revision021、空默认库或真实设置。
- **实际接口：** `ConversationService.create/get/list/messages/submit`；`POST /agent/conversations`、`GET /agent/conversations`、`GET /agent/conversations/{id}`、`GET/POST /agent/conversations/{id}/messages`；`GET /agent/tasks/{id}`、`POST /agent/tasks/{id}/cancel`、`GET /agent/tasks/{id}/snapshot` 与 `/events`（默认SSE，`transport=poll`返回JSON）。序号与conversation+client_message_id唯一约束持久保存公开消息；NFC、换行与首尾空白正规化后，相同键相同内容/context/reply返回既有message和当前task，不重复排队，同键异参409。文件SQLite写预留串行化并发收据；消息、task、pipeline与grant共用调用方事务，task.create同时固定message_ref及其幂等hash。FastAPI依赖用function scope，提交成功才发202；外层rollback或提交失败全部撤销，feature关闭后仍可读历史并重取已成功收据。
- **对象与权限：** 只按真实DB对象/版本/owner/主体解析listing、case、episode、plan、mandate、evidence、calculation及已有快照/候选/自选引用；旧ID正整数，新ID UUID。binding固定发送时context、主体与当前mandate revision；grant由服务端读当前MandateHead并构造，只用解析出的listing身份授权有限只读工具，当前mandate更新在AG08边界复查，不由旧mandate context授予权限。跨会话reply、错版本/跨owner/不存在引用拒绝。空context无证券范围/工具权限，不猜代码或名字；整体watchlist刷新未由本接口授权，客户端actor/grant/tool allowlist/as_of等额外字段拒绝。页面后续发送不修改既有绑定。
- **事件与取消：** Header Last-Event-ID优先于query after_event_id，数字ID可去重；signed64范围与超前游标明确422。SSE及poll同样在旧游标时返回HTTP410 cursor_expired/reload_snapshot和snapshot_url；流中清理则发cursor_expired后结束。snapshot含公开task/messages/event_cursor，清空事件后cursor=0明确重读当前保留历史。snapshot与每批events/task用真实短SQLite BEGIN读快照，防止旧task+新cursor或旧event batch+新终态遗漏完成事件；读session在yield/等待前释放。显式SQLite AUTOINCREMENT与task.event_floor保证删除最大/全表后不复用ID，并仍能认定旧游标过期。默认worker每轮清理最多500条30天前事件，正式消息/task终态与审计继续保留。每进程最多8个SSE连接，poll回退可用；响应首迭代前断线也释放名额，断线不取消后台task。取消序列化队列状态：queued/wait/backoff无active lease直接取消并关闭queued run，在途running仅cancel_requested，安全边界ack后终止，终态幂等。
- **可信回答接缝：** `ConversationService.save_assistant(claim, text, content, artifact_refs)`仅供服务端 `RunSpecification.completion` 调用，按task唯一完成收据保存实际可交付回答。与executor后续授权/预算复查和terminal checkpoint处于同一事务；重复同内容不重写，异参冲突，回滚同时撤销消息与完成状态。真实原生FunctionModel/worker证明默认框架成功仍仅私有draft并failed/partial，不自动发assistant消息；可信completion明确保存回答并判定succeeded才公开。没有可伪造grant、依赖结果或审批的HTTP控制入口。
- **验证：** 最终联合命令 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q tests/test_agent_conversations.py tests/integration/test_api_agent.py tests/test_agent_tasks.py tests/test_agent_execution.py tests/integration/test_migration_030.py tests/integration/test_migration_029.py tests/integration/test_migration_028.py tests/integration/test_migration_027.py tests/integration/test_migration_025.py` 为 **80 passed，15.89s**；随后function scope提交时机修正及新增ASGI提交失败用例，`tests/integration/test_api_agent.py` **6 passed，2.52s**。覆盖五独立session并发相同消息、外层回滚、ref主体/版本/mandate撤权、五类取消、可信回答真实worker与事务回滚、Last-Event-ID重连/poll/全删保留、可控并发终态读取、真实ASGI断线/未开始迭代断线/连接上限、API key/session/CSRF。迁移仅临时库029→030→029→030及fresh head，与metadata一致，保留旧payload并删除全部旧事件后新ID>旧max；已有backup/funnel/迁移head断言同步030。`ruff check .`、`mypy src`（223 source files）、`git diff --check`、`uv lock --check --offline`、`alembic heads`（030）通过。首轮全量唯一失败为backup遗留“029 (head)”字符串，修后backup专项 **1 passed，6.60s**；旧代码收集中的一次回归停止后，固定最终代码完整 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m "not network"` 为 **1707 passed、6 deselected，117.50s**。
- **限制与AG10/13/17交接：** 本项未实现研究策略、证券文本消歧、正式观点发布、UI、整体watchlist刷新授权或真实collector结果接入。AG10计算记录可作为版本context，证券主体由已保存fact_refs推导；无事实主体的记录不扩大工具范围。AG13负责实际研究/普通回答/澄清的可信完成判定、内容与引用校验及正式发布后调用save_assistant，不能将framework output直接传入公开回答；默认executor仍不提供正式答案。AG17按上述API做去重、快照重载、SSE/poll回退及明确上下文提交，conversation默认refs不自动扩大空消息授权。`deliver_deferred_result`仍是受信任服务端接缝；将来collector完成回灌必须从真实关联DB job/result验证，不能开放客户端结果提交。8条SSE限制为进程内，部署多进程/真实OS崩溃与人工研究质量验收留AG20；仅离线fixture/临时库验证，无真实模型网络或真实设置变更。

### AG10 估值计算与记录

- **目标与范围：** 复用历史估值分位，实现 `reverse_dcf_fcfe.v1` 的 Decimal 求解、适用性、敏感性、终值占比、质量与缺失结果；计算输出与输入假设统一保存。
- **交付与位置：** 版本化估值计算服务和工具适配，调用已有 `factors/valuation.py`，写入 AG05 的 calculation 存储。
- **独立验收：** 估值专题列出的可知解、单调性、单位、缺失、负值、超区间和逐格失败回归通过；原型虚构计算可复算；分位结果与既有算法一致。
- **边界：** 用明确的已核验 FCFE 输入 fixture 即可完成求解器验收；不把该结果当成真实数据覆盖。真实 FCFE 来源启用由 AG11 决定。

#### AG10 完成记录（2026-10-10）

- **状态：完成（确定性计算与可信工具验收，真实 FCFE 尚未启用）。** 新增 `valuation/solver.py`、`contracts.py`、`service.py`、`tools.py` 与 `tests/test_valuation_calculations.py`；`CalculationAssumption` 向后兼容增加 `rationale`，`CalculationRecord` 增加 `details`，保持旧 payload、结果引用及 AG09 context 可解析，无数据库迁移、真实库操作、采集、模型调用或 Git 提交。
- **公开接口：** `solve(market_value, base, rate, terminal_growth)`；`ValuationService.reverse_dcf(owner, ReverseDCFRequest, operation)`、`percentile(owner, PercentileRequest, operation)`；`valuation_registrations(qualification)` 提供 `reverse_dcf_fcfe` 与 `historical_valuation_percentile` 原生受控数据库工具，AG13 须显式注册和授权，AG09 消息 grant 未扩大。请求只有真实证据引用和情景参数，拒绝模型传入金额事实、主体、核验/适用性断言与控制字段；最多六项额外折现情景。工具只返回计算引用、结果、假设/理由、逐格状态与窗口/质量摘要，完整窗口输入保存在计算记录，避免灌入模型上下文。
- **计算与适用性：** `reverse_dcf_fcfe.v1` 全部金额和比率以 Decimal 计算，求解精度 40 位、固定五年、区间 [-0.95,1]、最多 200 次，同时满足宽度 ≤1e-8 和相对价值残差 ≤1e-6；保存误差、次数、终值现值比及中位数/最近年度两种基数逐格结果，单格失败不补值。要求最近三个连续完整正年度、最大/最小 ≤3、同主体/货币/单位/年度口径；股权归属、净借款、现金利息、利息分类、融资科目重叠、重大差异及非经营资产处理由服务端资格解析器核验。现金流被推翻、报告过期、年度基数落后或超过既有14天行情策略时不运行正式情景；历史基数和等距年度/无存根简化显式记录。主折现率及终值增长都有来源和理由，敏感性至少一个更低和一个更高的 r。现金流对应普通股总价值，未重复扣净债务或加现金。
- **证据与记录：** 事实只经 AG05 解析实际可见的已保存证据；跨主体、缺失/用户口述/未核验/冲突资料不会提升为可用 FCFE。已知跨源冲突先转成缺口并排除冲突事实引用，失败记录仍保存请求引用及原因。全部计算调用 `EvidenceService.save_calculation`，保存方法版本、准确输入版本/单位/期间/质量与假设依据。服务自己的原请求收据在重读数据前重放，因此新行情或 overdue 不会改变同 operation 的历史结果；同键异请求冲突。短 SQLite 写预留建立一致快照，计算及收据服从调用方提交/回滚；文件 SQLite 两独立 session 并发验收。
- **历史分位：** 直接调用既有 `VALUATION_FACTORS` v1，不复制排名算法；保持五年左开右闭、正值过滤、240个有效样本、31天覆盖容差和原质量标记。额外按真实请求时间严格过滤 capture/public，拒绝未来回填和盘中未收盘价格；保存全部日输入快照版本、供应商、值/收盘价、财报版本时点、实际窗口和排除数。负 PE/PB、短样本、行情过期或 overdue 均保留原因，失败结果为空；少于五年覆盖保留 short_history，不称为五年低位。
- **验证：** AG10 专项 **37 passed，5.40s**；AG10/AG05/AG01/AG09/既有分位联合回归在新增两项前 **104 passed，7.39s**，涵盖可知解、原型复算、单调性、单位、缺年/负值/波动、未知借款/现金利息、股权口径、逐格失败、超区间、严格 PIT、负 PE、分位一致、源码冲突、收据重放/并发/外层 rollback、真实 TrustedTools 权限/主体/假设出处/未来时点守卫与有界工具输出。固定代码后完整离线命令 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 为 **1744 passed、6 deselected，122.03s**；最终 `.venv/bin/ruff check .`、`.venv/bin/mypy src`（228 files）、`git diff --check`、`uv lock --check --offline` 全部通过。
- **AG11/AG12/AG13 交接与真实限制：** `QualificationResolver.resolve(session, owner, request, evidence)` 是明确的服务端注入合同，返回每个确切引用版本的 `ValuationQualification`（derivation_version/accounting_basis/latest_complete_year 与各口径诊断），不承载模型金额。默认 `UnavailableQualification` 一律记录 `fcfe_source_mapping_unavailable`，fixture 专用已核验 FCFE 只证明求解和边界；未将 CFO-capex 或现有 FCF 改名为 FCFE，未宣称真实 DCF 覆盖交付。AG11 必须核验真实来源映射、最新可得年度、调整和新材料失效后才能显式注入启用；AG12 接发布门槛，AG13 接研究策略和假设/用户约束判断。当前只实现计算接缝，无正式发布、资料采集或新工作流；运行实例 `.dev/data/app.db` revision 021 和默认空库均未修改。

### AG11 FCFE 补采与真实适用范围

- **目标与范围：** 基于 AG03 缺口选取代表样本，核对借还款、发债、现金利息、股权归属、期间与非经营项目，按估值设计补列、保存原始证据并推导年度 FCFE。
- **交付与位置：** `providers/akshare_eastmoney.py`、必要快照/字段登记与适配服务；可重复的样本补采入口和 V3b 四层覆盖报告。
- **独立验收：** 字段无重叠、符号与单位正确，未来数据不可见；未知项不补零，利息费用不冒充现金利息。每只样本给出启用或明确不适用理由。
- **边界：** 不强求全池回填。若可用来源不能支持任何真实样本，交付可复现的缺口结果、关闭真实 DCF 与后续取证项；AG20 可按受限范围验收，但不能称真实反向 DCF 已交付。

#### AG11 完成记录（2026-10-10）

- **状态：完成（有界真实缺口核验，真实DCF启用0只）。** 东财现金流后端保留完整供应商raw行/null/未映射列，避免把旧库存过滤误报成公司零值；新增 `valuation/fcfe.py` 候选字段诊断和可信口径检查后的Decimal桥接、`tools/probe_fcfe_sources.py` 固定四样本采集/重放入口。原 `free_cash_flow` 不改含义，无迁移/运行库/设置写入、无模型、无全池采集、无Git提交，保留前序与用户dirty。
- **真实结果与工件：** 茅台/美的/长江电力三个非金融及工商银行排除对照，2023–2025完整年度，分母始终4。实际4次公共只读请求，20秒/次、零重试/分页，4/4 HTTP200、12/12年度行；三非金融发债null、茅台借还款null，共同无已批准现金利息拆分映射。五个桥接候选原始字段三年齐全0/4、口径可用0/4、方法适用0/4、数值成功0/4。原始响应/URL/参数/捕获时点/hash和逐证券全部原因/唯一主原因见 [V3b报告](probes/ag11-fcfe-coverage-2026-10-10.md)、[原始捕获](probes/ag11-fcfe-capture-2026-10-10.json)、[重放JSON](probes/ag11-fcfe-coverage-2026-10-10.json)；另实际查阅CFA/财政部主源2次。HTTP/年度行可得、列null、五个候选字段齐全、口径核验明确分开；样本不代表CSI300覆盖。
- **适配与边界：** 股利利润利息合并行、利润表应计利息、PAY_INTEREST_COMMISSION不冒充FCFE现金利息；借债未知不补0，利息已在CFO不再扣，非金融、普通股归属、融资无重叠、合并范围、单位及非经营/重大变化未核验即关闭。主体/真实日期/Shanghai日期/PIT捕获与披露修订/符号/null有确定性守卫。尚未取公司年报附注，供应商金额仅候选，未制造派生供应商事实或生产资格。AG10默认UnavailableQualification保持；未来可靠证据适配须有可重构来源链、精确版本及新材料失效验证；本项不提前构建公告/OCR系统。AG12可继续通用发布服务，但不能称真实反向DCF已交付。
- **验证：** 最终专项 `pytest -q tests/test_fcfe_sources.py tests/unit/test_akshare_eastmoney.py tests/unit/test_snapshot_eastmoney.py` **62 passed、1 deselected，2.85s**；涵盖桥接、重叠守卫、未知借债/利息、重复扣息、归属/非经营、符号/币种、主体/年度/修订/PIT/时区、raw保留、真实报告hash重放及错年度集合。最终ruff全仓、mypy src（229 files）、git diff --check通过。完整离线命令 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` **1762 passed、6 deselected，122.27s**；其启动后仅修正报告年度集合标记及五列定义并增回归，最终专项62项通过覆盖这些小改，未重复整轮全量。

### AG12 意见发布与有效性

- **目标与范围：** 持久化 thesis/recommendation、发布检查、当前指针、依赖和意见事件；实现数字节点服务端解析、身份引用、主张门槛、当前有效性、续期与替代。
- **交付与位置：** `investor/` 发布与查询服务、意见/监测规格/通知 outbox 存储以及意见查询接口。monitor 规格在发布事务中保存，实际调度与通知消费归 AG15。
- **独立验收：** S07/S09/S16 和发布部分 S08：错主体/单位/来源不能发布；并发不覆盖新观点；过期或受限意见不能续期；新旧版本、当前指针和 outbox 原子提交。规则检查和语义评审结果分开记录。
- **边界：** 接受符合契约的候选输出即可验收，不依赖真实模型生成；不建设通用自然语言数值解析器。没有可用 DCF 不阻止有充分依据的其他公司研究。

#### AG12 完成记录（2026-10-10）

- **状态：完成（公司发布与有效性离线验收）。** 新增 `investor/publication.py`、`publication_numbers.py`、`storage/models/publication.py` 与接续源码 head030 的迁移031；保存不可变 thesis/recommendation/monitor、修复草稿、publication_check、独立结构化语义审查、关键依赖、追加意见事件和通知 outbox。只操作临时测试库，保留全部前序 dirty 修改，未提交 Git、未迁移真实 `.dev/data/app.db`（仍021）或默认空库、未改真实设置/调用模型网络。
- **可信应用接缝：** `PublicationCandidate`、`SemanticReview/SemanticAssessment`、`PublicationResult`、`candidate_hash`、`PublicationPolicy`；`PublicationService.check/publish/get_thesis/get_recommendation/extend/restrict/artifact`。研究方法默认固定为 `FIXED_METHOD`（`company-research.v1` 的确定性 UUID/revision），可由服务端注入已知版本，不接受任意模型 method UUID。`dates(as_of)` 提供默认7日复核/14日有效期，发布拒绝未来/已到期材料、早于 cutoff 或不早于失效的复核日期、超配置的有效期限。
- **发布与事务：** `publish` 只接可信 `TaskClaim` 和 AG06 已持久准备的 database action；准备参数必须精确为 `{"candidate": candidate.model_dump(mode="json"), "semantic": semantic.model_dump(mode="json")}`，稳定 intent/key 由应用选择并先提交，模型不能生成 operation/actor/approval。服务在短 SQLite `BEGIN IMMEDIATE`/case 写预留后，从现有动作账本同事务 callback 复查 lease/取消/feature/当前 mandate/scope/发布权限及 case revision，并在提交边界再次复查。正式对象统一预分配服务端 ID，递归映射 thesis、monitor、条件、action 与 dependency 引用，旧非终态意见 superseded、当前指针/CAS、依赖与 outbox 原子保存；withdrawn/superseded 旧对象不追加新的替代迁移。重放 committed action 返回既有结果；同动作异参拒绝。规则失败（包括陈旧 case 与 cutoff 后关键更正）保留完整原候选和 field_errors 草稿，不删除错句继续发结论；非法 schema 仍是 Pydantic 错误。服务不 commit，外层 rollback 撤销全部写入及动作结果。
- **数值与研究门槛：** `NumericResolver` 从 immutable Evidence/Calculation 和真实 Listing 解析主体、原始单位、期间、角色、服务器精度及身份；元/万元/亿元等确定性归一与 Decimal HALF_UP，百分数/百分点不混用。固定 field_path/role 映射区分事实、计算、情景、日期和条件；同名计算假设必须用保存索引（如 `assumptions.2`），拒绝歧义名称。有限扫描阿拉伯/全角数字、中文数量+成/倍/年/天/元/股等模式，并覆盖正文、问题范围、理由、缺口、反证和假设条件；正常“一次性收益”不误伤，未建设通用 NLP。候选行情/供应商事实可带原 verification 作为有限背景节点展示，不升级 verified；方向性关键事实单独要求核验，支持和反对证据均须进入关键依赖及审查集合。计算非空 quality 或 `short_history` 不一概拒绝，按阻断状态/错误分类且返回 quality，真实 AG10 percentile/DCF 记录可解析。新更正记录指向旧被更正记录，按可见新记录反查失效，不反向拒绝新版本。
- **语义边界：** 公司观点两对象同七项状态，缺口/定性原因、fact/assumption/inference 来源与 impact_if_wrong 明确；局部核查不能改公司当前指针。insufficient 只允许暂不判断，undetermined 不附无依据 buy/sell；方向观点需相关反证、关键主张与逐主张语义审查。审查记录绑定确切 candidate/material/method 版本并保存具体事实关系、反证相关性、重要性和未决问题；模型 confidence 不是放行条件。规则检查与语义审查分别持久保存，确定性通过不证明投资判断正确。AG20 人工量表和真实质量门禁尚未通过，不要求每次正常研究重新人工批准。
- **读取、有效性与续期：** `get_recommendation(at=...)` 在物理短 SQLite 快照内投影当时可见事件/当前指针/mandate 版本及相关来源；精确到期边界、critical 更正/冲突/新期间、相关硬约束与监测条件立即限制，独立于 AG15 消费速度。复用 AG05 canonical 字段、跨源容差/冲突和严格 PIT；先选已公开交易日，提前捕获的当天收盘不遮蔽此前有效行情；新行情或无关字段不自动使观点受限。暂停/讲解/预算偏好不推翻原判断。`extend` 仅允许事务内仍 current/published/未过期未受限且 case CAS 和必要依赖一致，维持 review 必须列出全部确切关键依赖；旧历史价格超新鲜度时还须显式核查已可见的当前 snapshot 证据。保留原 as_of/正文/valid_until，追加 extended 及核查版本，历史期限不被未来续期回写；复核受限或过期须新对象。`restrict` 为 AG14 归档/撤回、AG15 变化提供可信同事务接缝，保留具体原因和终态。
- **查询接口与交接：** 认证 GET `/investor/recommendations/{id}?revision=&at=`、`/theses/{id}?revision=`、`/drafts/{id}`、`/publication-checks/{id}`；`PublicationResult.check_ref` 可查询检查原件。AG09 `resolve_context` 新增 thesis/recommendation 精确版本、owner、case/Listing 主体绑定，旧 refs 保持。没有公开发布/grant/审批入口，没有私有 checkpoint/history/密钥查询。AG09 grant 未扩大；AG13 须由可信策略正式授予 `publish_recommendation`/`extend_recommendation` 并接研究完成判定，继续使用 AG08 执行器预算边界；本项不把 framework run 成功当研究完成。AG15 接实际调度、到期/变化事件消费和 outbox 投递；AG14 先接用户决定/控制与偏好记忆。
- **验证：** 新 AG12 文件 SQLite 专项61项和031迁移1项；联合 AG09 context、AG10、AG05及025–031迁移 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q tests/test_investor_publication.py tests/test_agent_conversations.py tests/test_valuation_calculations.py tests/test_evidence_services.py tests/integration/test_migration_031.py tests/integration/test_migration_030.py tests/integration/test_migration_029.py tests/integration/test_migration_028.py tests/integration/test_migration_027.py tests/integration/test_migration_026.py tests/integration/test_migration_025.py`：**131 passed，20.83s**。覆盖独立 session 发布/续期竞争、外层回滚与动作重放、lease/取消/权限、失败保留原草稿、原子替代及撤回终态、数值单位/主体/角色/字段/有限扫描、候选质量/真实计算记录、未来与更正链、当前依赖竞态、七维/反证/关键反对来源、精确到期、历史续期/约定、当前行情核查、同 canonical 跨源容差、盘中 PIT 与并发一致读取。031临时库030→031→030→031和本项 metadata 一致；全库 metadata 已有 `ix_research_change_previous_snapshot` 单项历史差异不纳入本项新增表断言，没有泛化忽略。025/027–030 的当前 head断言同步031；026仍明确验证历史026，仅排除后续031新增表。`ruff check .`、`mypy src`（232 files）、`git diff --check`、`alembic heads`（031）通过。固定代码后的最终完整离线命令 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'`：**1825 passed、6 deselected，134.41s**。未重复运行无变化全量。

### AG13 分层研究与反证流程

- **目标与范围：** 将解释、局部核查、完整研究、公司比较和观点复核接入主 Agent；组装上下文、按问题制定计划、核查关键假设与反证、保存进展或请求发布。
- **交付与位置：** `agent/` 研究策略、上下文和固定方法配置，接 AG05/AG10 工具与 AG12 发布服务；普通研究不逐步审批。
- **独立验收：** S01 至 S04、S17 的研究部分：局部回答不替换全局观点；事实/推断/假设可区分；证据不足不机械看空；只变用户喜好不无依据改变判断；预算或材料受阻时保存真实进展。
- **边界：** 不新增多个 Agent 角色，不自行改全局规则。完整公司观点需呈现七项取证状态；局部解释不强制完整计划。组合判断待 A1b。

#### AG13 完成记录（2026-10-10）

- **实际接通：** `agent/research.py` 的 `ResearchPolicy`/`ResearchOutput`、`research_publication.py` 的业务 DTO 与可信准备/审查、`observation.py` 的采集恢复和 `user_commands.py` 的明确指令控制器已接默认 worker 与 AG09 消息 API。继续使用 PydanticAI 2.31.1 单个原生主 Agent、AG06 worker/action ledger 和 AG08 预算/lease；没有自定义 tool loop、多角色或流程引擎。`research_kind` 可明确选择解释、局部核查、完整公司研究、同口径比较和观点复核，完整研究须先提交问题、可反驳假设、替代解释、按重要性取证计划及七维状态。没有 scope 不猜股票，不授予全池权限。
- **真实进展与完成：** `save_research_progress` 在工具提交阶段保存可读回答/计划、精确引用、已完成步骤与具体缺口；预算、取消和无进展停止前已提交产物保留。连续两轮没有新来源版本/哈希、实际完成步骤或可信依赖结果停止，单次运行重复相同参数版本也停止；时间戳与操作提案不算新证据。原生 run 成功只产生草稿，正式完整观点须经可信 completion/AG12 发布；局部问答不替换全局观点，关键反证先限制原意见并保存正式复核需求。比较拒绝不同期间或未知字段口径的硬排序，缺材料不机械产生买卖标签。
- **发布与复核：** 模型工具只收业务字段，服务器生成 UUID、actor、时间、授权、确切候选/材料哈希，模型随后审查同一原生 Agent 实际看到的候选与材料。语义失败只保存草稿；正文事实数字由可信 NumericResolver 校验。准备材料按可信摘要、必要字段及精确 refs 有界返回，超限明确阻断审查，完整审计保留 DB。正式假设、证伪条件和受控 field/calendar Monitor 输入随 thesis 生成并由 AG12 remap，日期复用 PublicationService policy。maintain 保存真实 review；续期只能在未过期/未受限的 current 对象及必要依赖核查后执行，过期或受限须新发布；withdraw 为终态撤回，同材料推断改变明确 `reasoning_revision`。
- **上下文与控制：** 每次可信装配使用 AG14 `context` 和 revision/期限 stamp；撤销和自然到期过滤原消息、同源偏好及派生回答，旧任务停止并结算已发送费用，只有新明确提交重建任务。历史任务过滤 cutoff 后消息/记忆，除本次用户问题外不送未来答案；不能可靠历史投影的当前 case/thesis/recommendation 正文省略并说明。有限完整语法“暂停/恢复这家公司的后台研究”“归档这家公司”“静音/取消这家公司的静音”“记住我的偏好：…”“把 <规范A股代码> 加入观察池”由可信控制器绑定精确消息与对象直接执行；其他模型解析只产生待批准提案，模型不能自批。偏好不成为公司证据或约定修订。
- **采集 PIT 闭环：** 原 Task binding、grant、授权与 lifetime 预算不变。仅 current research 可从已关联本任务 refresh 动作且成功提交真实采集记录的 PipelineRun 前移 observation cutoff，并追加不可重复审计；拒绝无关联、失败及未来收据，模型/deferred payload 不能自报完成时间。历史任务始终固定原 cutoff。AG08 工具与 AG12 候选/证据读取共用 `effective_cutoff`，真实 collector 完成后同 Task deferred 恢复可读取新证据，原 acquired_at/hash 不回填。
- **接口与 AG15 交接：** `research_artifacts` 与认证 GET `/agent/tasks/{task_id}/research` 提供保存的研究产物，不公开 checkpoint/history。AG15 用现有 `agent_review` 默认 handler（或 `ResearchPolicy.specification()`/`registrations()`）创建带确切 listing/case、mandate、admission as_of 的 `research_kind=review` 后台任务，grant 的 mode/source 为 background/authorized_background，依 live mandate 给有限研究工具与可信 publish/extend 权限；不得复用旧 grant 绕过记忆/权限或 lifetime 预算。按来源事件幂等 input_key 合并复核；等待采集继续同 Task。`critical_counter_evidence:formal_review_required` 是局部发现的正式复核需求，AG15 实现消费调度；calendar monitor 的 next_due、field 条件、有效 next_review/valid_until 及 AG12 outbox 由 AG15 消费，日历取证不伪造数值阈值。结果读取 Task artifact_refs、研究查询、PublicationService review 原件与有效观点投影；暂停/归档和通知静音须在调度/投递边界复查。
- **真实验证与限制：** 最终 AG13 原生 FunctionModel/MockTransport 专项 **19 passed，7.62s**；联合研究/会话/执行/发布/控制 **156 passed，30.18s**。覆盖实际 collector 数据晚于 admission 的恢复、历史上下文隔离、正式假设/calendar monitor、审查失败、局部反证、比较期间、持久预算进展、无进展、撤销/到期、有限明确指令/提案隔离与在途取消费用。紧化续期 cutoff 后仅修正旧发布测试：未来新材料应由新 admission 的 review Task 核查，而非给旧 Task 前移历史截止。固定代码全量离线 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` **1869 passed、6 deselected，157.89s**。`ruff check .`、`mypy src`（237 files）、`git diff --check`、`alembic heads`（032）通过。本项没有迁移、commit、真实模型/设置变更，真实库未迁移。配对 fixture 证明工程边界，不代表真实模型抗迎合或研究语义质量已通过，仍归 AG20；真实 FCFE qualification 仍 **0/4**、DCF 关闭，历史分位不等于便宜。UI 归 AG17–19，实际监测调度/通知归 AG15，不含持仓或真实成交。

#### 观察池模糊名入池（2026-10-23）

- **交互：** 用户对 agent 说模糊名称（如“茅台”）→ 模型用只读工具 `search_stock_directory`（本地目录，代码前缀/名称子串/拼音首字母，免 scope 白名单）列出候选与规范代码 → 用户回复精确指令“把 600519.SH 加入观察池”→ 可信控制器直接执行，不经模型、无第二次批准。写路径为 `WatchlistAddAction`（`investor/controls.py`）：listing 缺失时用目录公司名经 `ensure_a_share_listing` 补建，`WatchlistRepository.add` upsert 入池（provider=auto、不带模板，落 `needs_template` 走既有 UI 选模板流程），沿用既有 instruction 溯源与 InvestorMutation 回执（`uuid5(message.id, "clear-command")`）。代码不解析或目录/库中无此代码时指令不命中，消息照常走模型对话。
- **规则适配：** 方法校验 `research_numeric_text_requires_reference` 对规范 A 股代码（带 SH/SZ/BJ 后缀的身份令牌）窄豁免，裸数字仍禁；指令文本同步说明候选答复可含规范代码。非 research:v1 相位的精简 executor 白名单容忍未注册的 `search_stock_directory`（tools.py 既有跳过集合）。MCP server 保持只读；POST /watchlist 行为不变。

### AG14 用户决定操作与记忆

- **目标与范围：** 保存用户决定、明确偏好及待核观察；实现撤销偏好、归档/重开、暂停/恢复、静音，以及需要澄清的操作提案与明确用户指令执行。
- **交付与位置：** `investor/` 决定、控制和记忆服务，相关 `/investor/decisions`、`/actions`、`/memories`、`/proposals` API；提供上下文读取和撤销 revision 检查接口供 AG13 使用。
- **独立验收：** S06/S14 偏好部分/S17 控制部分：接受不生成持仓；用户归档不能自动重开；暂停只阻止相应后台后续动作；遗忘不会被旧消息或摘要重新提取；过期批准不可复用。
- **边界：** 已明确完整指令不重复确认；模型只准备待确认内容，不批准自己。真实成交工具与账本此阶段不开放。

#### AG14 完成记录（2026-10-10）

- **状态与交付：** 已实现、未提交；新增 `investor/controls.py`、032 临时库迁移与用户操作验收。`investor_user_object` 保存决定/记忆/提案/可信指令当前头，`investor_user_object_revision` 保留不可变版本，`investor_memory_state` 提供 owner 个性化栅栏；既有 dirty 全部保留。沿用 `accept/reject/defer/independent`，决定固定精确意见及档案版本、用户文本与可空理由；只保存决定，不生成持仓、交易或现金预留。
- **控制与事务：** 归档在同一调用方事务内通过 AG12 `PublicationService.restrict(withdrawn=True)` 撤回观点，CAS 更新 case 状态/当前指针、保留固定关注，并记录 `user_archived`。只有可信用户操作可重开，增加 episode；`automatic_reopen_allowed` 供 AG16 检查。暂停/恢复只变后台控制，AG08 下一边界停止该范围后台，交互问题仍可执行；静音/取消静音只变提醒控制，`notifications_allowed` 供 AG15 投递时复查，不使原观点受限。写入前 SQLite writer reservation、保存点、全局幂等收据与对象 CAS 共同保证并发和外层 rollback。
- **可信授权与提案：** `/actions` 直接执行完整认证请求，不追加确认。Pydantic discriminated 白名单为 `case_action`、`mandate_revision`、`decision`、`memory_save`、`memory_revoke`；不开放任意 dict 操作或真实账本。`UserAuthority` 是服务端能力，不是模型/HTTP schema；`authenticated_request` 仅由通过认证/CSRF 的 route 调用，`authenticated_message` 仅供可信应用策略将明确用户消息绑定到完整参数，不能把模型建议或 `actor=user` 自述当用户指令。提案绑定完整参数/版本、来源/期限和单次消费，异参、过期、旧 CAS 与重放拒绝；同 key 同参返回既有结果。模型 `prepare(..., claim=..., base=...)` 必须持可信 TaskClaim 和 `prepare_user_operation` 授权，不能 resolve 自批；用户批准后同事务执行。mandate 修订强制 actor/origin/authorization.source_refs 指向真实用户指令；批准/拒绝理由和消费结果保留。
- **记忆与遗忘：** 明确偏好只由用户操作保存；`save_observation(claim, ..., base=...)` 在 `save_observation` 授权内自动保存有未来期限、有精确原始消息/证据来源的 `unverified_observation`，拒绝升级偏好。撤销保留 tombstone、原因及原版本。`context` 是内部可信全量读取，按来源过滤撤销/到期记忆的整条原用户消息、同源偏好和全部派生 assistant 消息，保守排除失效前自动观察；旧消息不能再次提取失效偏好，新的明确认证指令可以重新保存。多偏好共用来源时整条消息和同源偏好会一起退出上下文，尚不提供片段级来源；所有派生 assistant 摘要保守失效，公开聊天审计历史保留。
- **AG13 执行接缝：** 使用 `UserControlService.context` 返回的 `MemoryContext`、`ContextStamp(revision,built_at,valid_until)` 装配上下文，私有历史传旧 stamp 验证；自然到期过滤原来源/摘要，不能只检查 revision。AG08 `admit` 从 DB 强制填 grant.memory_revision/最早记忆期限，模型参数不能选 revision；`check_boundary` 在后续模型、工具、发布和可信 `save_assistant` 检查变化/到期，私有 checkpoint 持久 revision/期限。用户记忆变更使原任务停止并要求新任务重建，明确 message 操作后也不能继续旧 grant；已发送模型请求仍由原 BudgetService 结算成本。自动观察不递增其生产任务的用户记忆栅栏，新增观察可能进入 tool history，其到期由下一边界确定性检查；AG13 应每次可信装配读取当前记忆与 stamp，不把 revision 当全部观察的缓存 hash。AG13 负责明确指令的可信语义解析、完成判定和重建任务，尚未把自然语言模型识别当授权，也未开放私有 checkpoint/history 查询。
- **公开接口：** POST/GET `/investor/decisions`、POST `/investor/actions` 与 `/cases/{id}/actions`、POST/GET `/investor/memories` 和 POST `/memories/{id}/revoke`、POST/GET `/investor/proposals`、GET `/proposals/{id}` 和 POST `/proposals/{id}/resolve`。提案详情默认当前 head，显式 revision 查历史；列表按 UUID 稳定 `after` 翻页，决定可筛 case/recommendation，记忆可筛 case/listing/status/active_only，提案可筛 status，先筛选再 limit。公开记忆自然到期投影 `expired`，不改不可变原件。沿用原认证/CSRF，Investor Service 全部 FastAPI 依赖改为 function scope，提交失败在成功响应前回滚。返回用户对象与审批来源，不暴露 raw checkpoint/history/凭证。
- **真实验证：** 前置与 AG14/迁移联合 **144 passed，26.52s**；最后固定实现后的 AG14+032 专项 **25 passed，6.81s**，包含明确用户/模型授权、精确提案/消费/过期、并发与 rollback、归档撤回/固定关注/新 episode、后台暂停与交互、静音、遗忘/同源/派生摘要/自然到期、101 条完整上下文与游标、Cookie CSRF、提交失败，以及真实 MockTransport 在途模型撤销后费用结算且工具未执行。全量离线 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 为 **1849 passed、1 failed、6 deselected，148.91s**：唯一失败是旧 migration026 仅升级到026却把新032元数据纳入比较；仅补其后续三表精确排除后，025–032 迁移专项 **8 passed，5.77s**。实现没有修改，没有重复全量，也不将修前全量声称全绿。`ruff check .`、`mypy src`（233 files）、`git diff --check`、`alembic heads`（032）通过。所有迁移仅临时测试库，真实 `.dev/data/app.db` 仍021、默认 `data/app.db` 无 Alembic schema；没有真实模型/配置变更。真实成交/资金及账本仍交 AG21，提醒实际投递交 AG15。

### AG15 监测复核与站内提醒

- **目标与范围：** 消费 snapshot_committed 与 calendar_due，按注册字段与假设条件识别依赖变化、使旧意见受限、合并复核并调用 AG13；消费通知 outbox。
- **交付与位置：** `investor/` 监测/复核服务、scheduler/worker 适配、手动复核 API 与站内提醒查询。支持 dirty 标记、到期计划、去重和无变化续期。
- **独立验收：** 完整 S05/S16：重复事件和 worker 重启不重复发观点/提醒；变化前后版本与用户决定保留；无变化仅留记录；同一变化的受限提示和研究结果更新同一提醒。
- **边界：** 不依赖后台及时消费才能显示过期；数据未更新或缺失不能判为无变化。不做公告原文、外部渠道与任意监测脚本。

#### AG15 完成记录（2026-10-10）

- **状态与位置：** 已实现 `investor/monitoring.py`、迁移033的 `investor_monitor_receipt` / `investor_review_cycle` / `investor_notification`，接既有 scheduler 设置轮询与默认 worker 的有界维护。采用架构允许的可信已提交扫描 adapter；按 case 单调 snapshot id 游标识别同期间更正、跨 provider 新版本和新期间，重查全部相关关键依赖/注册 monitor。每轮最多100 case、默认50，按最久未检查分页；历史批量补采按 case 合并，不逐行启动研究。游标、来源 hash/version 收据、限制和 dirty 在同一短写事务保存，不持事务跨模型/网络。
- **复核与期限：** 复用 AG08 `admit` / AG13 默认 `agent_review`，新授权精确 listing/case/当前 mandate 与 admission cutoff。后台要求显式启用、范围、已知 USD 额度及匹配模型配置的可信价格；缺失时保留 blocked 原因。已有 Task 的失败、取消、预算停止和不完整复核不自动新建付费任务、不跨日清账。运行中变化记录 dirty，完成后仅重查未消费当前依赖；已被新结果覆盖或无关变化不重复研究。明确手动新问题保存 reason、稳定幂等收据，运行中合并为待回答问题，完成后另一个 interactive 复核；暂停后的手动请求取消旧 background Task 并新建 interactive 授权，旧 grant/审计保留。
- **无变化与投影：** `RecommendationView.effective_next_review_at` 按可见 extension 投影，保留原正文/as_of/valid_until，历史读取不见未来延长。AG12 extension 把真实 review_ref 同事务绑定 Task。维持原有限观点只要求原 critical fact claims 的核验强度，不把 critical 时效依赖误提升为已核验事实；计算仍走 Calculation resolver。监测 maintain 必须有相关注册工具完成收据、明确核查确切版本；snapshot 必须晚于原 as_of/最后成功检查且当前查询完整、无冲突/质量降级。`capture_snapshot` 的 refs 经真实 SavedEvidence 解析，candidate 保持 candidate；旧 snapshot、缺字段、过期/受限均不能靠 maintain 延长。成功撤回是完成的复核；inconclusive 保留未完成/需材料。
- **站内提醒与接口：** 只写站内，普通来源检查和正常续期只留时间线。通知通过 `review:<cycle_id>` 合并受限与正式修订结果，AG12 独立 publication outbox 经真实 Task artifact_refs 归属该 cycle，独立初次发布仍独立一次。投递重新检查当前静音，suppressed outbox 永久消费，解除静音不重放；消费收据与 upsert 原子，业务内容不变不翻 revision/未读，新结果更新同条可未读。认证/CSRF/function-scope 的 POST `/investor/cases/{case_id}/reviews` 接 `mutation`、可选 `reason`；GET 同路径提供 UUID cursor 时间线；GET `/investor/notifications?after=<sequence>&limit=&unread=`、PATCH `/investor/notifications/{sequence}/read` 接 `read`/`mutation`，稳定分页、幂等和 revision CAS。Task 研究原件仍 GET `/agent/tasks/{id}/research` 与保存的 Publication review。
- **专项与门禁：** AG15+发布/研究/控制/033 联合 **132 passed，32.04s**，含真实默认 worker + 原生 FunctionModel、认证 API/CSRF、文件 SQLite 双 session 竞争、重复扫描/日历/重启、批量历史、dirty/新问题、现行静音、运行中暂停后手动、失败停止、financial 更正/新期间/跨源冲突/缺字段、fresh/old capture 成对、静默延长与历史日期、真实 prepare/semantic 修订同提醒、撤回不复活。`ruff check .`、`mypy src`（239 files）、`git diff --check`、`alembic heads`（033）通过；固定实现单次全量离线 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 为 **1889 passed、8 failed、6 deselected，165.00s**；八项均为 backup/restore、funnel 及升级到 HEAD 的025/027–031仍断言032，实际新 HEAD 为033。只把这些 HEAD 预期精确更新033，历史026/032固定断言不变；修后相关 backup/funnel/025–033 联合 **11 passed，29.59s**。该次全量没有业务失败，也未把修前全量描述为全绿；HEAD 修复仅测试预期变化，未重复全量。收尾生命周期核查把已归档 pending cycle 明确置为 stopped，防止旧请求在重开后重放；监测/033 专项此前为 **28 passed，13.06s**，新增归档回归独立 **1 passed，1.81s**（首轮失败为测试跨 session 读取未提交旧 revision，修正为同事务当前 revision）。
- **交接与限制：** AG16 可消费可信 `MonitorReceipt` 来源版本/变更记录，复用有限 admit 入口、live scope/预算与 controls；本项未做探索短名单；后续新增迁移时同步 backup_restore/funnel 和明确升级到 HEAD 的迁移测试预期，保留历史版本断言。AG18 用上述通知 sequence/revision/read、cycle/task/result_refs 和有效 next_review 日期，普通进展走 case/research 时间线。本项不下载公告、不执行任意 SQL/Python monitor、不做外部推送或新 workflow；正式核查质量及真实 OS 崩溃/多进程部署验收仍归 AG20。只有临时库迁移测试，真实 `.dev/data/app.db` 仍021、默认空库、真实设置/模型均未动，没有 commit/reset。

### AG16 有限线索发现与队列

- **目标与范围：** 从既有事件、模板差异与同业指标生成确定性短名单，Agent 在已授权范围与预算内选研究问题；用户提名可直接进入研究。
- **交付与位置：** `investor/` 探索服务、短名单/处理理由记录及查询接口；接既有 scheduler 和 AG13 研究入口。
- **独立验收：** 每批审阅/建任务上限生效，重复证券合并；未命中模板仍可研究；不制造买入数量指标，不重开用户归档项，不移除固定关注；探索和复核预算不互相越权挪用。
- **边界：** 只做 A1a 的有限范围；不逐只调用模型扫描目标池，不把研究顺序混进确定性评分或收益排名。

#### AG16 完成记录（2026-10-10）

- **状态：完成（有限发现与研究队列后端）。** 新增 `investor/discovery.py`、持久 `DiscoveryBatch` / `DiscoveryCursor`、迁移034及离线回归；默认 scheduler/worker 接通扫描、单次原生选择与 AG13 入研究队列。未提交 Git，保留前序未提交实现。
- **确定性短名单与原生选择：** 从精确授权 listing 的已提交 financial/market snapshot 最新来源版本，以及已成功 screen run 的模板结果/既有 peer context 生成线索；携带来源 ID、revision、hash、实际 captured_at、报告期间/交易日、provider 与单位语境，保留前次来源方便比较。按持久 least-checked listing 游标有限扫描，历史批量补采合并，每证券每批一条；自身处理记录不成为新来源。同源重启不创建新批/重复选择收费。应用默认及硬上限20条/3个新研究Task，与当前 mandate 和运行设置取最小值。一批只有一次单主 Agent 原生结构化审阅；准确校验所有 eligible lead ID/version/listing、入选问题、理由、再评估条件及 live 上限，允许选择零项，拒绝模型新增证券。未入选/未完成选择不称已研究；排序含义仅为研究问题价值，原确定性评分和收益排名未改。
- **持久队列与控制：** 初始扫描保留归档/暂停项的 blocked 原因，排除这些项的 selection grant，避免污染其他合法线索；在途单项撤权可保守停止整批。已有研究按可信 scope_identities（含 case-only scope）去重，入选新问题/来源版本保存 `pending_research` 和 `merged_task_id`。前序成功后，仅当真实 AG13 committed research_progress 的确切 question、answered 和全部来源版本的 verified SavedEvidence 构成覆盖证明时记 covered；否则有限补一个 exploration research Task，不重付选择费。失败/partial/取消的前序保留 stopped 原因，需要用户主动决定，不能自动重建清零。补任务保持原 batch/selection parent 关联，重新检查 live scope、授权、价格和上限。复用 owner/listing 唯一 case，保留 pinned/用户控制；本项没有自动归档或系统重开动作，用户归档绝不自动复活。用户精确提名继续走既有认证 MessageInput + company_research 入口，可绕模板，不能绕 mandate；归档重开仍用既有明确用户动作。
- **预算分账兼容：** 在唯一 `BudgetEntry` 账本增加服务端 `purpose`，保留原 owner 日总额/Task lifetime 及原子 reservation/settlement，选择和子研究均记 exploration，review 记 review；模型和 API 客户端不能选择 grant/bucket。034按可信 queue kind / immutable grant research_kind 分类旧 review 费用，未丢失旧账与总额。`ResearchAuthorization.exploration_daily_budget` 缺省 None 明确关闭付费探索；`review_daily_budget` 缺省 None 时仅在探索额度尚未设置的旧配置下兼容既有总额限制，一旦配置探索额度，复核也必须显式配置自己的 known USD 桶。两桶受同一日总额，互不借用；未知价格/费用不当零，当前额度在预留和实际发送边界复查，两 SQLite worker 竞争不能超额预留。运行时新增 `investor_agent_max_leads_per_batch`（0–20，默认20）和 `investor_agent_max_new_tasks_per_batch`（0–3，默认3），不放宽更小用户限额。
- **公开接口与 AG17/18 交接：** 认证 GET `/investor/discovery/batches?after=<sequence>&limit=<n>` 与 `/investor/discovery/batches/{batch_id}`；分页返回 `sequence/id/state/selection_task_id`、上限、来源、处理理由/问题/再评估条件、`research_task_id/merged_task_id`。详情的 `current_task_states` 从真实 Task 读取 status/stop_reason/revision，selection_completed 表示有限选择已回答，queued 不表示公司研究已成功；research_pending 表示已选问题等待现有研究，blocked/stopped 留明确原因。没有私有 prompt/history/checkpoint/凭证公开，也不为普通探索发重大提醒。AG18可按这些字段展示短名单与处理过程，实际研究产物沿 AG13 API。
- **AG19 交接：** 设置 UI 必须明确展示探索与复核两个日金额字段、共享每日总额、单任务终生金额和20/3上限；旧配置无探索额度不能静默开付费探索；一旦设置探索额度而未设置复核额度，复核明确返回 `review_budget_unavailable`。精确 listing 授权范围和后台开关继续沿当前 mandate / runtime 独立开关，不能通过一次交互提名开启后台付费发现。
- **真实验证与门禁顺序：** 联合研究/复核/执行/registry/025–034/backup/funnel首轮 **122 passed、1 failed，56.92s**，唯一失败为新034测试用全schema比较碰到既有 `ix_research_change_previous_snapshot` 元数据差异，随后精确比较本项两新表和 `agent_budget_entry`；历史026仅精确排除后续两新表，HEAD预期同步034而历史032/033固定断言保留。固定实现联合 **94 passed，25.27s**；最后新增真实 snapshot `materialize` → `EvidenceService.save_evidence` → AG13 `save_progress` committed action 的 covered 分支独立 **1 passed，1.86s**，初始fixture字符串利润未被数值 adapter 接受，改为数值后通过。该分支和034亦包含在最终全量。先完成 `.venv/bin/ruff check .`、`.venv/bin/mypy src`（241 files）、`git diff --check`、`.venv/bin/alembic heads`（034），全部通过；随后固定代码仅一次全量 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 为 **1921 passed、6 deselected，186.79s**。覆盖真实默认worker+原生FunctionModel有限选择→真实AG13队列、20/3与更小live限额、错误ID/version/subject/问题/越界拒绝、零入选、用户提名/越权、归档混合批次/pin、文件SQLite并发去重/预算、来源时点/批量历史、pending各终态与精确covered、取消、认证分页/当前任务状态和临时迁移。
- **限制：** 只使用既有授权数据和已保存模板/peer context，不补公告原文、组合或 A2 网络搜索，不逐证券调用模型。工程 fixture 验证不等于真实模型研究语义/抗迎合验收，仍归 AG20。只有临时数据库迁移，真实 `.dev/data/app.db` 未升级、默认空 `data/app.db` 未动；未改真实配置、未调用真实模型或发外部通知。没有持仓/真实成交动作、通用队列治理或 AG28 调度扩展。

### AG17 对话工作台

- **目标与范围：** 新建 `/app/agent` 桌面和手机壳，接会话列表、明确对象绑定、证券消歧、提问、进展、等待、取消、重连和已有观点更新提示。
- **交付与位置：** 独立的 `web/templates/` 与 `web/static/` Agent 模块，候选和单股页入口；依赖 AG09 的真实 API，其他卡片先按公开接口提供扩展位。
- **独立验收：** 用真实任务 API 加确定性执行 fixture 验证消息重发、切页、断线和取消；输入保留、状态准确；桌面/手机可操作，无隐藏内容与 XSS/CSP 退化。
- **边界：** 完整意见、来源和管理面板分别由 AG18/AG19 实现；不复制整套原型为静态成品，不提前替换默认首页。

#### AG17 完成记录（2026-10-10）

- **状态：完成（真实 API 与桌面/手机确定性浏览器验收）。** 新增独立 `web/templates/agent.html`、`web/static/agent.js` 及共用 CSS 的响应式壳，注册 `/app/agent`、主导航和旧候选/单股页入口；默认首页未替换，Agent 启用默认值未改。保留全部已有 dirty 工作，未提交 Git、未新增迁移、未访问/升级真实 `.dev/data/app.db` 或写真实设置，未调用真实模型。
- **实际交互与边界：** 分页读取真实会话/消息，证券名称与完整代码来自真实 Listing/Company；新增认证只读 `GET /agent/listings?q=`、`/agent/listings/{listing_id}` 与 `/agent/candidate-context/{candidate_id}`，按规范完整代码/交易所精确查询或列出消歧结果，不把 ticker 转整数或猜公司匹配。下一条对象明确可见，比较对象可追加并按真实 kind/id/revision 去重（API 上限32 refs）；已发送消息保留当时绑定。普通问题默认 `auto`，完整公司研究是明确范围选项。只安全展示已保存 `Message.text` 与公开进展，不在浏览器解释尚未解析的 NumericNode，不显示私有历史、思考过程、凭证或原始 provider 输出。
- **恢复与真实状态：** 每会话草稿和未确认请求保存于当前页签 sessionStorage；`client_message_id` 为持久 UUID，待确认请求的原 text/context_refs/research_kind 整体固定，重试复用原 body，独立提交锁防重复。网络/失去回执保留原请求；明确拒绝保留输入并解释实际错误。选择/创建更新 conversation URL，切会话及证券查找均有代际隔离，迟到结果不能进入另一会话。任务快照按 message id/sequence 合并，闭合“先读消息、随后任务已终态”的竞态；首次历史 Task 快照不再逐个重读整段消息。SSE 按全局 event_id 去重、Last-Event-ID/query cursor 恢复，真实 HTTP410 或流内 cursor_expired 重新加载快照，三次重连失败转真实 poll；poll 的410快照失败仍保留可见重试。断线/离页只断开客户端监听，不取消服务端；取消使用真实 API，running 的 cancel_requested 先显示“正在停止”，直到服务端 ack 才显示已取消。状态事件缺少等待细节/终态原因时读取真实 Task；终态恢复最终持久回答。引用 recommendation 的当前有效性和 replacement_ref 来自真实公开 API，旧回答保留并附更新/受限提示。
- **AG18/19 接缝：** `#agent-workbench` 实际派发 `agent:ready`（artifactHost=`#agent-artifact-extension`、controlsHost=`#agent-controls-extension`）、`agent:messages`（conversationId/messages）、`agent:task`（真实 Task/binding refs）、`agent:event`（去重后的公开事件）与 `agent:updates`（ref/RecommendationView 或读取错误）。AG18/19 可挂载这些真实容器/生命周期；多对象 refs 由当前明确选择进入 MessageInput，历史消息和 Task.binding 提供固定版本。此项未放完整意见卡、来源抽屉、设置/批准或假的成功按钮，完整组件仍归对应后续任务。
- **实际验证：** `tests/browser/agent_workbench.mjs` 使用 bundled Node Playwright 与本机已有 Chromium 1243，自建系统临时 SQLite/独立 FastAPI fixture server 并在 finally 关服删库；真实 AG09 API、原生 FunctionModel、真实 research tool registrations 与受信任 completion/worker 验证 **1440px 桌面、390px 手机均通过**。覆盖精确证券身份（id78 与 ticker600002.SH）、安全名称/输入/回答 XSS、草稿 reload、lost-ack 同 UUID/完整 body 重取及唯一收据、无收据请求 reload 后重发、原生执行最终回答、切换/迟到响应隔离、双对象比较与去重、running 取消/ack、等待材料、真实事件清理 HTTP410/快照/三次流失败/poll、首次消息与终态快照竞态、API/session/auth/异源 cookie CSRF403、无水平溢出；已查看 `/tmp/ag17-workbench-1440.png` 与 `/tmp/ag17-workbench-390.png`。复用命令：`AG17_CHROMIUM_EXECUTABLE=/Users/zhvala/Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell AG17_NODE_MODULES=/Users/zhvala/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules /Users/zhvala/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node tests/browser/agent_workbench.mjs`。API 专项加入真实身份精确读/认证/CSP 回归；固定最终代码完整 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 为 **1922 passed、6 deselected，190.35s**。`.venv/bin/ruff check .`、`.venv/bin/mypy src`（241 files）、`git diff --check`，Node 对 agent.js/app.js/browser fixture 的语法检查通过。
- **限制：** sessionStorage 只保证当前页签重载，关闭页签/跨设备的草稿同步未实现；已有会话/消息/任务仍持久保存在服务端，其他页签需重新明确下一条对象。事件过期后恢复当前快照，不虚构已经清理的进展。正式观点更新接口与安全提示已接入，但完整意见内容、来源/历史版本视图和管理面板归 AG18/19；真实模型投资语义、长时间多进程恢复与真实端点/数据验收归 AG20。浏览器离线 fixture 的成功不代表真实研究质量已经验收。

### AG18 观点证据线索与复盘界面

- **目标与范围：** 接入经营/价格/充分度卡片、七项取证状态、来源抽屉、估值详情、版本对比、线索与探索、比较产物、用户决定时间线和过程复盘；展示 AG15 提醒与复核结果和 AG16 短名单处理理由。
- **交付与位置：** Agent 独立的卡片、来源、档案与比较/复盘 Web 模块，复用 AG17 壳和结构化内容渲染。比较/复盘按设计首版提供桌面布局。
- **独立验收：** 新旧观点与当前有效性分开；局部好消息不显示全公司正面标签；数字节点定位到正确记录；过期卡片和计算失败不误导；手机长内容可滚动。
- **边界：** 复盘回答过程问题，不展示缺乏依据的真实收益或胜率；不增加 A1b 组合卡片。研究产物由 AG13 生成，页面不自行计算结论。

#### AG18 完成记录（2026-10-10）

- **真实挂载与阅读：** `agent_views.js` 在 AG17 boot 前挂载独立扩展，消费真实 messages/task/updates/binding 生命周期；空对话也可浏览公司档案、站内提醒与有限探索。经营、价格、问题充分度分开，七项取证状态、事实/推断/反证、假设证伪条件、缺口及条件性下一步可读；公司名/代码与发布时间区分不同观点对象，不把两个 UUID 各自的 revision=1 编造成同一对象的 v1/v2。来源、估值与存储元数据用原生页面抽屉和折叠审计详情；桌面观点并列，手机顺序长文，不设困住滚动的模态框。Agent footer 单独说明来源、假设、不确定性与用户交易决定，旧首页与旧页面措辞保持。
- **历史与精确数值：** 认证只读 `/investor/recommendations/{id}/body` 用 `NumericResolver` 在原 recommendation.as_of 解析主体、引用版本/字段、期间、角色、单位与服务端精度；返回原正文与独立当前 RecommendationView。新更正使当前观点待复核，历史值和原有效期/复核时间不改写；有效延长日期另列。没有增加 created_at 历史隐身条件。AG13 committed progress.result.text 已在当时可信 observation cutoff 解析并保存，页面复用该原始 canonical text 与精确 refs，避免用当前 Task cutoff 重算历史比较或局部回答。未解析节点明确不可用，不补零、不升级为公司正面判断。
- **真实档案与过程：** 新增 owner/case 作用域的只读分页 recommendations、publication-events 与 research 投影，分别查询真实 PublicationArtifact/Event 和 Task binding 下 committed save_research_progress；不依赖不会自动填充的 Episode.artifact_refs/ResearchPlan 表。档案展示当时问题、取证计划、完成步骤、缺失材料、用户决定与复核/观点变化时间线；比较保留保存的来源、期间与方法，页面不编新排名或收益/胜率。分页保留稳定游标，已显示记录数与未知全体分母分开。
- **来源、计算与后续状态：** 精确 revision 的只读 calculations/monitors/reviews 公开保存输入、假设、事实/结果/错误及适用性；计算失败、部分或未知保持原值与状态，不能冒充有效 DCF。AG15 通知按真实 category/stage/reasons/观点与结果 refs 展示公司及处理阶段；sequence/revision read CAS 只采用真实返回更新专用已读节点，内容不被覆盖。普通检查不冒充重大提醒。AG16 保留 selection_completed 与 queued/pending/covered/stopped/blocked 的区别、处理理由、再评估条件、来源与当前 Task 状态；merged_task_id 可进入现有研究。不同来源读取另有请求代次，换来源/关闭作废旧返回；对象或会话切换也隔离迟到数据。全程安全 DOM、既有 CSP/认证/CSRF，无 raw JSON 成品页。
- **旧约束与界面语义：** 按共用约定把旧页面禁用投资用语守卫限定旧模块，保留全部旧限制，并新增 Agent 无真实下单/伪成交入口守卫；条件性意见标签复用服务端唯一 CompanyAction.label，不削弱 buy/sell 语义。修正一个旧测试的时钟基准：真实 now 发布之后的查询不能再固定到已过去的 2026-10-10 10:00 UTC；读取用发布后的当前时刻，仍断言普通新价格不自动使意见失效，没有放宽 PIT。
- **浏览器与专项验证：** `tests/browser/agent_views.mjs` 复用隔离临时 SQLite/19847 fixture，真实两次正式发布、原生 FunctionModel→ResearchPolicy 保存 NumericNode 进展，然后更正证据。1440px 与 390px 均验证历史现金原值/当前限制分离、精确来源、失败计算与未知输入、真实通知字段/read CAS/唯一通知、探索受阻理由、真实研究/观点事件时间线与版本并列、HTTP503 重试、旧观点迟到响应隔离、A慢→B→A仍B与关闭来源不复活、安全 DOM/无横溢出；已查看 `/tmp/ag18-views-1440.png` 与 `/tmp/ag18-views-390.png`。复用命令：`AG17_CHROMIUM_EXECUTABLE=/Users/zhvala/Library/Caches/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-mac-arm64/chrome-headless-shell AG17_NODE_MODULES=/Users/zhvala/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules /Users/zhvala/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node tests/browser/agent_views.mjs`。相同环境运行 `tests/browser/agent_workbench.mjs`，AG17 桌面/手机完整回归通过。受影响 Python 专项 **87 passed，14.30s**；ruff、mypy（242 files）、diff check 与 Node JS 语法检查通过。第一轮全量真实结果 **1923 passed、1 failed、6 deselected，216.26s**，唯一失败是上述跨新旧模块的禁用词守卫，修复后最终固定代码全量结果另见下条。
- **最终固定代码全量：** `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` **1925 passed、6 deselected，217.08s**。最终代码通过上述全部专项、真实浏览器与静态验证，没有未收取的测试/fixture 进程。
- **限制与 AG19 交接：** 真实 FCFE qualification 仍 **0/4**、默认未启用，没有改变金融公式；工程 fixture 不证明真实模型投资语义或真实公司研究质量，归 AG20。没有迁移、commit/reset、真实模型请求、设置写入或外部通知；真实 `.dev/data/app.db` 与默认空 `data/app.db` 未动。Agent 默认关闭、首页不变，无组合/券商/下单界面。AG19 继续使用原 controlsHost，实现启用/模型配置/授权预算、档案控制、决定录入与记忆；本项仅阅读已有决定，不提前增加这些写操作。

### AG19 启用设置控制与记忆界面

- **目标与范围：** 接入独立模型配置、首次使用、后台范围/预算显式启用、投资约定、档案控制、决定录入、记忆撤销及操作提案。
- **交付与位置：** 复用现有设置页的密钥管理，新增 Agent 设置与操作面板，连接 AG07/AG14 API；后台开关与 AG15/AG16 的授权字段一致。
- **独立验收：** 未启用后台仍能提问；修改配置不影响旧链路；权限扩大来源明确；暂停/静音/归档含义准确；接受建议不伪造成交；失败不显示成功，输入错误只要求补充相关字段。
- **边界：** 不首次强迫填写完整画像，不把保存在浏览器的配置当成生效，也不开放无后端的真实成交或外部渠道按钮。

#### AG19 完成记录（2026-10-10）

- **状态：完成（设置与控制真实 API、桌面/手机工程验收），未提交。** 新增 `web/static/agent_controls.js`，挂载 AG17 原 controlsHost；复用 AG18 选中档案/观点的精确上下文，复用原设置页加密密钥表单，不复制配置页面。现有 dirty 全部保留，没有 Git commit/reset、新迁移、真实设置写入、真实模型调用或外部通知；真实 `.dev/data/app.db` 的 size/mtime 保持 **360513536 / 1791489715**，仍按既有021运行，工程 head034未变。默认 Agent/background 开关和首页均未改变。
- **启用与模型配置：** 原 `/app/settings` 的 Agent 独立 provider/base_url/model/api_key/profile/profile_model/output_mode 可替换；密钥只写不读、掩码显示、加密存储，未初始化加密主密钥时使用原有明确错误与禁用输入。原模型参数、凭证和解释链路保持独立，没有 legacy fallback。布尔设置使用明确 checkbox，高级模型参数/请求-token-时间/20与3等执行限额收起；PATCH 仅发 changed fields 与 note，未虚构 settings CAS。未新增假连接测试，也未把旧 `/settings/test/llm` 当 Agent 验证。页面反复说明保存/本地检查不代表网络或研究质量已验证。
- **首次约定与预算：** 通过名称/完整证券代码查找并明确选择证券，显示公司及完整代码，不要求用户填写内部 listing ID；初次约定允许只补本次相关目标、明确约束和 unknowns。精确交互/后台范围、USD 每日共享总额、单任务全生命周期上限、独立 review/exploration 日金额及每批20线索/3新任务清楚展示；空 exploration 保持 None、未启用探索；配置探索后未配置 review 明确显示 review_budget_unavailable。运行后台开关与约定 background_enabled 分别显示真实回读，不由提问自动开启；配置/约定取较小探索限额。约定变更先呈现中文范围/金额/版本摘要，再明确保存；首次创建走 foundation，后续走 UserControlService mandate_revision 与 expected_revision。CAS 失败阻止原按钮再提交，刷新必须重新审阅新内容。
- **只读公开接口：** 认证 `GET /investor/readiness` 使用 load_settings，不惰性导入、不联系供应商；只白名单输出实际开关、模型参数 fingerprint（排除密钥，可复制用于价格绑定）、输出能力声明、runtime/effective 探索限额和独立 interactive/review/exploration 缺项。报价必须绑定版本/模型/完整配置指纹，未知不当0，不猜汇率；各用途检查 known positive USD 与0探索限额；复核未授权探索不会被误标为同一失败。该投影是本地诊断，实际执行仍检查实时范围、费用和剩余额度，不承诺准入或网络成功。新增认证 `GET /investor/memories/{uuid}?revision=` 供撤销提案审阅确切旧版本内容与范围；UUID 路由转换器保留私有 `/memories/context` 的404，不开放上下文、private history、推理或凭证。
- **控制与明确对象：** 暂停/恢复仅控制后台，静音/解除仅控制通知，归档撤下观点不自动重开，显式重开进入新 Episode，固定关注独立保留；所有按钮连接原认证 execute 或 pin CAS API。决定必须从当前明确绑定/AG18 用户选中观点或档案中明确选择一个，历史 Message 不自动选择对象，多公司/多版本不取第一条；真实引用与版本在折叠审计中保留。切会话或实际改变对象使旧决定/记忆表单停止提交；AG17 重复同 binding/恢复名称不会覆盖 AG18 明确选中的观点。采纳仅写 UserDecision，无持仓、成交、现金预占、券商或外部渠道动作。
- **记忆与提案：** 显式偏好和有明确未来有效截止的未核实观察分别保存，不自动升格为事实或 mandate。撤销使用确切 memory_ref 与理由，界面说明未来原文/派生上下文排除、审计保留，具体遗忘语义复用 AG14。提案以中文变更、证券/范围、版本、有效期限和来源摘要审阅，撤销提案展示具体原记忆内容/范围；原参数与完整版本保留折叠审计，resolve 只发送 parameters、mutation、approve、reason。过期/旧 CAS 409 诚实显示，拒绝不会执行操作，不替换参数自动批准。档案/记忆/待审提案按50条稳定 UUID cursor 翻页。所有新 DOM 使用 textContent/原生控件，沿既有 CSP/cookie CSRF；输入错误和 API 失败不显示成功。
- **丢回执恢复：** sessionStorage 仅保存不含配置密钥的未确认控制请求，固定 path/method/完整 body/稳定 UUID，跨当前页签 reload 可重取；HTTP0/5xx 留待确认请求，明确拒绝清除待确认并展示失败；原请求未确认前阻止其他写操作。连续两次丢回执均复用各自原 UUID/body，retry 状态每条新请求重置；明确放弃重取提示先核对服务器，未声称回滚已经保存的操作。
- **验证：** `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q tests/integration/test_api_agent_controls.py tests/test_investor_controls.py tests/integration/test_api_settings.py tests/integration/test_api_agent.py tests/integration/test_web_candidates.py` **76 passed，14.38s**，涵盖首次最小约定、真实 CAS、默认关闭/认证/无 secret/无惰性导入、独立桶/币种与0上限，复用既有决定无账本、原文与派生记忆撤销、安全/密钥加密回归。初轮53项有1项新 UUID 路由把旧 context404变成422，已用 path UUID converter 修复并包含最终联合回归。`.venv/bin/ruff check .`、`.venv/bin/mypy src`（242 files）、Node 对 controls/settings/agent_views/app/browser脚本语法、`git diff --check` 通过。
- **真实浏览器：** 沿 AG17 完成记录的 bundled Node/Chromium 环境，运行 `node tests/browser/agent_controls.mjs`，每个1440px/390px viewport独立系统临时 SQLite/19849 server，完成后关服删库，**均通过**。涵盖 defaultoff→明确启用、changed-fields与加密key脱敏/旧模型隔离、最小约定/后台off真实交互接受、名称代码授权范围、双历史观点明确选择、相同binding重复广播不换对象/真实切换旧表单失效、暂停与静音区别、连续两次lost-ack含reload唯一保存、记忆撤销、真实提案过期409与拒绝、真实并发CAS/刷新重审、503不假成功、安全DOM与无横向溢出。已查看 `/tmp/ag19-setup-1440.png`、`/tmp/ag19-setup-390.png` 主要表单及 `/tmp/ag19-controls-390.png` 错误态。`agent_workbench.mjs` 与 `agent_views.mjs` 串行桌面/手机回归通过；首轮误并行因二者共用19847冲突，已关服并串行重跑，没有放松断言。
- **完整离线结果：** 单次完整 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 为 **1928 passed、6 deselected，184.89s**，结果已收取。收尾仅再加“AG17重复同binding不能覆盖AG18明确选中观点”的前端守卫及对应真实浏览器用例，最终AG19桌面/手机再次通过，Ruff/mypy/JS/diff再次通过；后端/Python未再变化，未重复无关全量。
- **AG20 交接与限制：** Agent 保持默认关闭，启用配置不等于阶段开放。仅临时数据和原生 FunctionModel/离线工具证明工程闭环，未证明真实端点、投资推理、不迎合或研究质量；真实 worker/多进程部署/故障恢复、真实模型成本、人工量表和用户理解验收仍待 AG20，不能由工程冒签。按已记录的招商银行/海康威视/国电南瑞开展三家公司验证，不再询问样本、不预定研究结论；真实 FCFE qualification 仍0/4，未验证 DCF继续关闭。没有提前开始 AG20、真实账本、交易执行或外部渠道。

### AG20 公司观点闭环验收与试用

- **目标与范围：** 串通 AG01 至 AG19，跑完公司研究到决定、变化、复核与修订的闭环，验证部署、迁移、功能关闭及旧功能兼容；记录模型质量与首批成本。
- **交付：** 可演示的 A1a 版本、跨任务回归、阶段验收记录和当前启用范围。真实三家公司按产品设计在试用前选定，选择工作不阻塞前面任务。
- **独立验收：** S01 至 S10、S16、S17 及 S14 偏好部分通过；在真实 worker/API 上验证关键故障，而不只测内存假实现；人工质量量表达标，用户能复述立场、依据、反证和下一步。
- **退出条件：** 启用至少一条有真实证据支撑的公司研究及后续复核路径；清楚披露估值支持范围。未验证的 DCF 保持关闭。满足后才开放 A1a 入口并开始 A1b。


#### AG20 完成记录（2026-10-10）

- **状态：engineering complete / product gate pending。** 完成跨AG01–19工程集成验收、真实API/worker/fileSQLite进程故障证据与三公司隔离资料包；真实模型/人工量表/用户理解门槛尚未通过，A1a入口未正式开放。Agent/background继续默认关闭，FCFE资格0/4、未验证DCF关闭；未commit/reset、未改真实配置或迁移真实库。三家公司选择不是质量验收。
- **交付：** [阶段验收记录](ag20-company-acceptance.md)列S01–10/S14偏好/S16/S17实际测试名、证据层级、部署/关闭/兼容/恢复runbook及AG21接口；[真实资料包](probes/ag20-company-materials-2026-10-10.md)与JSON冻结招商银行/海康威视/国电南瑞的identity/provider/period/unit/time/locator/hash和缺口；[人工评审模板](probes/ag20-human-review-template.json)保持未签署。`tools/agent_company_trial.py prepare/run`：只读backup、隔离copy034、清空副本runtime settings、明确独立配置/环境凭据、显式company_research+冻结cutoff、8requests/Task/24总、50k/Task/150k总token、4096output/请求、45s请求/360sTask、无后台/自动重复付费；记录实际AgentModelRun usage/http/延迟/失败、实际材料receipt/hash和cost=null，资料包hash不冒充实际用料集。
- **关键故障：** `tests/integration/test_agent_process_acceptance.py`在真实TCP FastAPI消息事务提交后、返回确认前os._exit(86)，重启恢复精确UUID/body且关闭后仍保留原回执；实际ResearchWorker在发布/发件箱消费/通知事务提交后被父测试SIGKILL该PID，再由新OS进程recover_stale，旧lease写被拒、替代attempt复用同operation_id、committed动作/发布事件/outbox/notification各一次。外部unknown仍须对账，不宣称任意外部恰好一次。扩展AG15 native修订用例跨用户defer决定→受限→正式复核→版本/一次通知，决定继续指旧版本且持仓数量不变。S03对立态度native配对仅证明fixture工程边界，真实模型不迎合仍待评审。
- **验证：** focused200 passed/62.37s；新增配对与最终试用离线专项2 passed/2.56s；Ruff、mypy src（247 source files）、三份browser JS语法、diff检查通过；AG17/18/19真实Chromium1440/390分别通过。一次完整 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 为**1943 passed、3 skipped、6 deselected，194.37s**；三skip是缺外部ZCLAUDIA_GATEWAY_DIR实际Node checkout，未冒签。真实021只读copy034成功，原库复核revision021/size360513536/mtime_ns1791489715347475627/SHA25685c29a22e6ece11efff8f25d76f7baf886cbf8d10efeb6a4082460c112a32e3b不变。
- **真实试用/阶段限制：** 正式独立配置缺失；用户后续明确授权临时复用旧供应商/模型，首轮真实3 HTTP200、35949tokens，三项failed；repair-1保守schema预留0HTTP/0tokens；最小预留/阶段schema修复后的repair-2真实3 HTTP200、新39720tokens，三项仍progress.question门槛拒绝。三轮累计6 HTTP/75669tokens、各公司24790/24613/26266，均在原8/50k及24/150k累计限额内，无合格公司研究产物。立场/依据/反证/下一步尚无真实可评审Agent答案，不能请人工硬打分。人工事实关系2分/方向反证2分/其他适用>=1/零关键事实错零硬约束违规及用户复述仍pending。资料包与旧v1.3解释不升级为新观点；无真实已发布历史假设被新材料推翻的对照。工程任务完成与产品“AG20通过后A1b”条件分别判断，本卡不改阶段条件、不自动启动AG21；后续默认关闭工程交父任务明确处理。UserDecision不能映射真实持仓/成交。



- **AG20首轮真实试次追加（用户后续明确授权，失败保留）：** 临时复用实际effective旧供应商+deepseek-flash模型到隔离独立Agent配置，AG02兼容DeepSeek reasoning profile/prompted json_object/required=false/schema=false；正式设置不写、硬限额和冻结cutoff不变。[真实结果](probes/ag20-real-trial-2026-10-10.md)及JSON记录3次HTTP200、33144输入/2805输出token、模型延迟2362/2201/8688ms、cost=None。三个Task全部failed：前两项错把listing当get_evidence证据ref（海康还错subject），InvestorError；第三项native解析成功但completion ExecutionDeniedError/evidence_blocked，未保留可核字段级拒绝原因。无公开assistant/正式观点/committed材料receipt，不能称真实公司研究路径已验收；原始“未调用”资料包保留冻结版本并用真实试次补充，不覆盖失败，后续唯一修复验证轮见下。原库hash/stat不变，人工质量/理解与A1a开放继续pending；只更新报告，未重复full。


- **真实暴露边界修复：** evidence UUID/listing正整数窄schema、用途说明、原生最多2次参数修复；无权限/预算错误仍failclosed、无伪造Evidence。原国电parsed draft在完成事务回滚丢失，具体字段不可恢复不猜；新增私有rejected_completion与安全公开fieldpath/code/下一步。native错误引用→get_snapshot→capture候选→get_evidence→明确不足partial5请求回归与1请求预算拒绝通过。修复 focused104 passed28.30s，最终研究/工具24 passed11.74s，跨日试用/真实进程3 passed17.58s；必要单次全量 **1946 passed、3 skipped、6 deselected，192.33s**，Ruff/mypy247/diff通过；无UI变更，前序真实browser证据仍有效。
- **零HTTP前次修复验证轮：** [实际记录](probes/ag20-real-repair-2026-10-10.md)/JSON，`--retry-existing`同目录只容许一份追加，不清ledger/覆盖初轮，parent_task_id关联失败Task；真实settled BudgetEntry lifetime逐公司和总额扣减，跨日不reset，unknown/sent/reserved拒绝。各公司剩7请求及38461/38437/37153token，全21/114051；实际三项均HTTP前保守schema字节预留拒绝，不放宽预算/估算、不新增付费轮。真实修复语义未验收，成本未知None，人工/A1a门槛pending。隔离开关关闭、临时key及setting change清除，源021 hash/stat不变。

- **最终有HTTP修复验证与技术收口：** [三轮统一报告](probes/ag20-real-trial-2026-10-10.md)/JSON保留首轮原始归档、0HTTP前次、最后真实轮完整审计；同目录repair-2 predecessor链，累计额度不reset。structured instruction_parts存在才剔除消息instructions重复预留，fallback保留；native tool.prepare对齐既有committed七维计划前置，保存后同native run自动恢复。affected108 passed29.51s、该版本full1950 passed/3 skipped/6 deselected185.48s；最后tool异常ResearchValidationError.code/field_errors安全透传及可执行Text提示专项8 passed4.46s、Ruff/mypy247/diff通过，最终必要full **1951 passed、3 skipped、6 deselected，183.69s**；三skip仍是未提供外部Node gateway checkout，不计通过。最后真实3公司可调用却没有研究产物，三项具体progress.question数值/证券身份文本门槛未过；海康safe字段经只读prepared动作确定并修复未来包装，历史不改写。Provider发送max_tokens4096但reported4356/4156/4520输出超请求边界，实际usage结算、累计公司及总额未突破，未知money None。余18requests/74331tokens不再使用，临时key清除、原库021 hash/stat未变、默认off/DCF off。试用identity正文/可信assembly缺公司名/canonical ticker，仅listing IDs且无identity发现工具，曾是上下文覆盖工程bug；最终assemble已按grant/binding交集主键注入canonical名称/完整ticker/exact listing_ref，明示当前身份元数据不是金融证据或历史核验；无新工具/扩大权限，缺身份unavailable不guess。默认native worker离线SDK/SQLite四项通过，同名、错scope、历史binding及缺失身份覆盖；修复后真实模型仍未验证。直接拒绝原因与历史额外限制分开，不将全部归模型。真实研究/方法/模型质量、人工量表/理解/A1a仍pending；无正式结果要求用户硬打分。后续关闭工程由父明确解释与决定，不自动产品阶段通过。

## A1b 真实组合的任务

### AG21 真实账本与快照接口

- **目标与范围：** 用户确认的 opening_balance、成交、现金变动、分红、公司行动、对账点与冲销；构建完整/部分范围的可追溯组合快照。
- **交付与位置：** `portfolio/`、账本/快照存储与迁移、`/portfolio/events` 和 snapshot API。复用现有证券与价格引用。
- **独立验收：** S12 中的缺费用、部分资产、重复导入、历史回填、冲销和公司行动正确；旧快照保持；违反约定的真实交易仍入账，不编造现金和历史收益。
- **边界：** 不连接券商，不因建议或接受生成事件；不拿现有 HoldingItem 当未经确认的完整起点。

#### AG21 完成记录（2026-10-10，工程能力默认关闭）

- **交付与边界：** `portfolio/contracts.py`、`portfolio/service.py`、四张 `portfolio_account/event/snapshot/request` 表与035迁移；认证 `POST /portfolio/accounts`、`GET /portfolio/accounts`、`POST /portfolio/events`、认证 `GET /portfolio/events?account_id&after_revision&limit` 稳定顺序分页与 `GET /portfolio/events/{event_id}?account_id` 原事件读取、`GET /portfolio/snapshot`（current、固定UUID或revision）。`portfolio_ledger_enabled=false` 默认禁止新录入，历史只读保留。服务端固定认证 `local_owner`，请求不能自授actor/owner；没有模型写工具、券商、执行按钮或UI，也没有从旧HoldingItem、建议或UserDecision自动生成事实。AG20产品研究/人工理解门槛仍pending，不宣称A1a发布或A1b开放。
- **事实语义：** 用户确认的opening含明确账户、complete/partial资产范围、持仓、现金、UTC基准与来源；仅已发生事实可追加，首版只收CNY及本地人民币listing，不猜FX。trade用精确listing、真实方向/股数/价格和known/unknown费用；真实odd lot、约定违规、负现金/持仓成交均保留，冲突与未知显式返回。公司行动只实现确认split（明确ratio/quantity范围）或pending（明确影响quantity/cash范围），其他行动保持待核并经冲销再录确认结果，未冒称通用自动处理。
- **版本与投影：** `(occurred_at, recorded_revision)` 确定性排序；opening的`prior_history=included_in_confirmed_balance`只把早于该边界的历史保留审计且不重复计入，同时间晚录成交会计入。reversal只能指本账户原事实、同原事件至多一次，不能冲销reversal；错误opening可冲销，当前变为`no_active_opening`受限，再确认新opening，active opening至多一个。历史补录重建新snapshot，旧event/snapshot不改。reconciliation仅重置其后余额，保存用户解释及前值/确认值/可算delta；原值受限时delta未知并说明原因，不补费用、不造差额现金事件，不抹历史unknown；零差异不新增冲突。
- **AG22接口：** `Snapshot.reference`固定portfolio_snapshot UUID/revision；`known_cash`仅当前精确现金，`cash_subtotal`为已记录金额投影（不能当可用现金）；`holdings.confirmed_quantity`可为null，`quantity_subtotal`不是对未知起点的猜测；`coverage/asset_scope/unknowns/historical_unknowns/conflicts/reconciliations/source_event_ids/excluded_prior_event_ids`明确范围与血缘。`fact_as_of`是已录事实发生时间，`valuation_at`是快照记录时的行情知识/估值时点，逐持仓冻结未复权`market_snapshot`ref/version/date；缺价格或总资产不完整时`total_assets/weight=null`，`known_subtotal`绝不作完整分母。确认零数量无需行情，其市值为零；未知quantity仍不估值。GET只读取已保存快照，不隐含更新行情。无成本历史的收益始终null。资金/约束计算与方案优化交AG22/23。
- **并发和恢复：** SQLite写前DML reservation；事件追加、新快照、current指针、revision CAS和认证请求receipt同事务，function-scope dependency使commit在HTTP ACK前。lost ACK先校验key/exact validated body hash并重放旧结果，再检查CAS。batch/row唯一；导入重放的另一请求key也保存receipt，禁止之后换body重用；相同时间/数量不同真实手工key分别记账。账号间不抵扣未知现金。035仅测试库升级/降级，并更新current-head断言到035，历史固定032/033测试保留；备份恢复fixture现在验证账本事实、请求receipt、current指针和不可变快照一起恢复。
- **验证记录：** 固定专项命令 `.venv/bin/pytest -q tests/test_portfolio_ledger.py tests/integration/test_migration_025.py tests/integration/test_migration_026.py tests/integration/test_migration_027.py tests/integration/test_migration_028.py tests/integration/test_migration_029.py tests/integration/test_migration_030.py tests/integration/test_migration_031.py tests/integration/test_migration_032.py tests/integration/test_migration_033.py tests/integration/test_migration_034.py tests/integration/test_migration_035.py tests/integration/test_backup_restore.py` 为 **25 passed，15.26s**，覆盖S12、未来事实拒绝、相同时间补录、opening冲销/重建、并发CAS/冲销/lost-ACK、真实奇数股及负余额、对账已知差额/旧值未知、现金/持仓/partial历史限制、零数量不需价格、跨账户、API认证/default-off/commit-ACK/409、固定行情/旧快照、035 metadata roundtrip与账本备份。补原事件只读审计API与遗漏funnel current-head035后专项 `.venv/bin/pytest -q tests/test_portfolio_ledger.py tests/integration/test_migration_035.py tests/integration/test_backup_restore.py tests/integration/test_funnel_e2e.py` 为 **17 passed，20.79s**，含14项账本测试、分页/原事实固定读取/认证/owner及账户隔离/错误UUID和分页范围拒绝。首轮全量 **1968 passed、1 failed、3 skipped、6 deselected，188.02s** 的唯一失败为funnel旧head034断言，明确修为035；不把该轮声称全绿。最后接口固定后 `ruff check .`、`mypy src`（252 files）、`git diff --check`、`alembic heads`（035）通过；最终固定接口的全量离线 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 为 **1970 passed、3 skipped、6 deselected，198.01s**；3skip仍为未配置`ZCLAUDIA_GATEWAY_DIR`外部Node checkout，不计通过。真实 `.dev/data/app.db` stat仍360513536/1791489715、head仍021，未写真实库/密钥/设置、未调用真实模型或网络、未commit/reset。

### AG22 组合资金与约束计算

- **目标与范围：** 从同一 snapshot/价格基准计算不行动和各情景的资金、费用、净资产、单股/行业暴露及交易单位取整；检测必要未知项。
- **交付与位置：** `portfolio/` 确定性计算工具，复用 calculation 引用方式和 mandate 约束；增加 S11/S12 计算 fixture。
- **独立验收：** 多项行动共享资金；费用影响现金和净资产分母；未知费用不称全部通过；交易单位缺失不生成股数，部分组合不冒充完整分母。
- **边界：** 不求全市场最优组合、不预测校准收益率；目标暴露区间是明确的方案假设，不能声称由公式求得最优值。

#### AG22 完成记录（2026-10-10，工程能力默认关闭）

- **交付：** `portfolio/calculation_contracts.py`、`portfolio/calculations.py` 提供 Decimal 组合情景工具；复用 immutable `SavedCalculation` / `CalculationRecord`，无新表或迁移，head仍035。`POST /portfolio/calculations` 创建/按operation_id精确重放；`GET /portfolio/calculations/{calculation_id}?account_id=...` 认证、owner及账户隔离的只读历史。写入继续受默认false的`portfolio_ledger_enabled`控制，关闭后仍可读取。计算不追加事件、不替换快照/推荐/当前计划、不预留现金、不产生真实成交。
- **计算契约：** 必须固定account、portfolio_snapshot UUID/revision和mandate UUID/revision；保存完整快照、正式约定、情景/费用/成交价格假设、来源、价格日期/捕获时点、method_version及hash。不行动和互斥情景分别从同一baseline开始；单情景按明确行动顺序共享现金/持仓，不能未卖先花卖款。仅`known_cash`用于精确资金；`cash_subtotal`、`known_subtotal`不能变为可用现金或完整NAV。买卖费用及成交/估值价格差异共同影响现金与NAV；未知非负费用保留cash=null，必要时cash_upper_bound仍可证明超支。partial范围允许局部现金检查，但NAV/权重未知；零数量无需行情。违规与未知分别保存，不把missing当pass。
- **正式约束与假设：** `Mandate.portfolio_constraints`可选typed字段（默认None兼容旧记录）绑定一个明确CNY账户，含单股/行业上限和最低现金/现金比例；用户通过既有认证创建/修订确认版本。模型情景约束单列`checks`，正式约束单列`mandate_checks`，模型不能放宽正式约定。自由文本`hard_constraints`不解析，存在时返回未结构化限制；缺typed约束/错账户仍needs_data。publication依赖允许精确`portfolio_constraints`字段，用户修订使相应已发布意见待复核；AG19既有candidate对象展开保留该字段，无新增组合UI。目标/限制均是用户约定或明示情景假设，不是公式求得的最优目标。
- **行情与规则：** 既有持仓只使用snapshot冻结未复权行情，新标的只能精确选已有market_snapshot并由服务端重建listing/version/close/provider/date/captured_at、校验CNY/截止；不能通过任填价格+ref伪造事实，不能刷新旧持仓，超过显式日龄政策即未知。行业只取CSRC有效区间与捕获截止满足的唯一分类并冻结来源；未知/冲突不归“其他”桶。缺规则不生成股数；显式假设规则包含minimum+N*increment及full odd-lot sell，预算按含固定/比例费用向下取整。请求规则只产生scenario_rule_simulation，保持needs_data；服务端`TradingRuleResolver`接缝默认Unavailable，只有受控resolver输出经证券/市场/生效/可见时间校验的`TrustedTradingRule`可产生verified_rule_simulation。未实现交易所官方规则抓取或生产适配，fixture verified不代表真实证券核验。
- **AG23数值交接：** `CalculationRecord.status=ok`仅表示确定性算术完成，绝不等于约束通过或产品可发布；各情景`status`、`violations`、`unknowns`、`publication_ready`是必要资料/约束计算门槛，仍需AG23完成当前版本、权限、研究、lease等发布检查。已知cash/nav/fees、持仓quantity/value/weight、行动quantity/gross/fee、行业weight保存为稳定`results.scenario_N_...` Quantity路径，未知不写零；`details.result_paths`固定场景索引、主体、角色、门槛及规则性质，费用/成交假设另存CalculationAssumption。现有公司NumericResolver仍拒绝组合主体，AG23须单独适配这些字段和exact snapshot/mandate依赖，不能直接读取details数字当已核验主张。无计划服务/优化/期望收益/组合UI。
- **精度与验证：** 金额按有限Decimal输入位数设置足够精度，暴露比率固定40位；AG21仅weight除法改为40位localcontext，不改精确金额及旧快照。最终专项 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q tests/test_portfolio_calculations.py tests/test_portfolio_ledger.py tests/test_investor_foundations.py tests/test_investor_publication.py` 为 **102 passed，14.98s**，含S11共享现金/独立互斥、不行动、费用分母、预算minimum/increment/q0/奇股清仓、未知费用已知超支、partial/未知数量、行业未知、价格PIT/伪造/币种、可信规则接缝、正式约束与模型假设隔离/用户修订失效、精确source/replay/immutable历史/API认证/default-off/无事件副作用、长金额精确与eventcount比例不爆长。`ruff check .`、`mypy src`（254 files）、`git diff --check`、`alembic heads`（035）通过。最终固定全量离线 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 为 **1989 passed、3 skipped、6 deselected，210.70s**；3skip是未配置`ZCLAUDIA_GATEWAY_DIR`外部Node checkout，不计通过。
- **限制与状态：** AG20真实模型研究/人工验收仍pending，AG22只交付默认关闭工程，不宣称A1a/A1b开放。全部数据库验证为临时库，真实`.dev/data/app.db`仍021，size/mtime保持360513536/1791489715；未调用真实模型/供应商、未写真实设置/库、未commit/reset或启动AG23。

### AG23 组合方案与行动服务

- **目标与范围：** 生成 portfolio_plan 与互斥选项，将 company_thesis 和 portfolio_constraint 两类依据接入研究与发布；同一组合只保留一份当前正式方案。
- **交付与位置：** `portfolio/` 方案服务、组合研究工具、计划查询与发布接口，以及与 AG15 的组合变化复核集成。
- **独立验收：** 两会话同时发布不能重复分配现金；新方案原子替代旧方案；部分真实成交后剩余行动重新检查；失效公司观点不妨碍有依据的硬约束减仓分析，但不能据此买入。
- **边界：** 采纳不锁定真实资金、不提交订单；hold/add/reduce/exit 必须与真实持仓匹配。

#### AG23 完成记录（2026-10-10，工程能力默认关闭）

- **实现与原子性：** 新增 `portfolio/plan_contracts.py` 的 `PortfolioAction`、不可变 whole-account `PortfolioPlan/PlanOption/PlanView`，及 `portfolio/plans.py`。组合不虚构公司 Case，036 用专属 artifact/event/outbox 与每账户唯一 CAS head；同一次原子事务保存整份不行动对照、互斥选项、原案引用、语义审查与 publication_check、替代旧方案并追加状态/outbox。沿用 AG12 的 prepared ToolAction 参数指纹、TaskClaim/lease、权限/截止点边界、`settle_action` 和 rollback/replay 防线；两个真实 SQLite 会话不能各自发布独立可花费方案，旧案引用错误直接冲突。
- **算术、研究与行动门槛：** AG22 增加无写入 `rebuild`；正式方案必须匹配 exact account/snapshot/mandate 与共同价格基础，全部已选情景结果由服务器重建并从稳定 Quantity 路径复制，专用 `PortfolioNumericResolver` 不放宽公司 NumericResolver。目标仅 exact scenario/listing action quantity；buy 要已确认不存在，hold/add/reduce/exit 要已确认正持仓，exit 与全量、reduce 与剩余持仓吻合；hold/wait/further_research 不能藏成交。company_thesis 要正确 listing 的当前有效、未过期未受限公司观点。portfolio_constraint 仅已确认持仓上的 reduce/exit/wait，绑定 exact 用户确认账户 typed hard constraint、已知违规与明确公司研究缺口；方向性减仓须改善相关约束且不恶化其他必要指标，仍有的已知违规显式保留。无数量 wait 可在已知违规下保持不增风险而等待；此例外不能授权买入/加仓。现金/费用/覆盖/行情/可信交易规则缺失不能变成 all-pass sizing；默认可信规则解析器仍 unavailable，模型场景规则绝不升级为执行权限。
- **版本与复核：** 认证只读 `/portfolio/plans/current`、`/portfolio/plans/{id}?account_id=...` 返回原正文、当前状态/限制/refs。新实际成交、补录、冲销/核对使旧 snapshot 方案在读取时立即 review_required；公司观点受限/替代与相关 mandate 的 portfolio_constraints/hard_constraints/scope/goals 同样即时失效，解释偏好/预算变化不推翻旧正文。逐实际 price_date 在发布和读取检查新鲜度，仅依赖 no_action 必要持仓与已选情景的标的。AG15 `maintain` 接入有界 least-checked head/outbox 协调，追加精确 current refs 状态事件；同一失效方案最多一个持久 review Task，更多实际变化不能自动重置预算/付费重试。默认关闭与暂停/归档后台权限仍生效；仅显式 interactive 已确认持仓的风险分析可绕过归档公司研究入口，公司观点仍不可复用。
- **原生与用户权限：** Conversation 显式 `portfolio_action` 加 exact account/snapshot trusted ExecutionGrant；原生工具 `get_portfolio_context`、`calculate_portfolio_options`、`prepare_portfolio_plan` 进入既有 durable action ledger。工具阶段与准备前 progress 门槛受服务端控制，完成策略做 exact semantic review 后发布，无手写模型循环；原 no-progress 与预算限制保留，新增 exact snapshot/calculation 材料签名。用户决定兼容旧 company ref，新增 exact portfolio_plan_ref/option_id/可选 action_id，只有认证用户可执行；接受方案不锁现金、不改变账本、不提交订单。
- **验证：** 扩展组合/公司发布/用户控制/AG15/原生研究/036 临时迁移专项 **196 passed，43.32s**；最后选中情景新鲜度/截止点修订后的 AG23+036 **30 passed，8.42s**。包含真实两个 file-SQLite 会话整案 CAS、rollback/idempotence/body-conflict、部分实际成交即时失效、约束减仓无公司观点与买入拒绝、数量/unknown/violation 门槛、相关 mandate/公司限制、原生 FunctionModel 的真实 Conversation→grant→工具→semantic→发布及引用数字路径、归档持仓交互允许与后台 archive/pause 拒绝、单一 review Task 无重置、price_date 与未选情景隔离。`ruff check .`、`mypy src`（258 files）、`git diff --check`、`alembic heads`（036）通过。首轮全量离线 **2018 passed、1 failed、3 skipped、6 deselected，224.11s**；唯一失败为 AG20 OS 子进程 fixture 在 `Base.metadata.create_all` 前未导入 portfolio 模型，AG15 查询组合表时发现临时测试表缺失。仅补 fixture 的 portfolio/portfolio_plan 模型导入，没有修改实现；修后真实 OS kill/recovery+036 临时迁移专项 **3 passed，16.74s**，static/head/diff 再次通过。因已发现失败进行必要的固定修后全量复验，`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 最终为 **2019 passed、3 skipped、6 deselected，239.68s**。3 skip 仍是未配置 `ZCLAUDIA_GATEWAY_DIR` 的外部 Node checkout，不计通过；首轮失败记录保留，修后版本明确验证。
- **AG24交接与限制：** UI 未实现，精确 action 绑定由不可变 enclosing plan/option/snapshot/calculation 提供，决定仍需完整 plan_ref+option_id+action_id（可选）；历史整案沿 previous_ref 回看。当前专属 draft/publication_check/review 没有独立 HTTP 读取；AG24 若需展示失败字段/审计须补最小认证 account+kind exact artifact read API。AG24 展示同一 calculation_ref 的费用/成交价格 CalculationAssumption 与 nature，不能把情景支出当真实成交事实。当前工具支持 no_action/互斥研究与限制，未增加可信交易规则供应、自动执行/预留、实时报价更新或新平台发布。全部数据库验证仅临时库；真实 `.dev/data/app.db` 仍021、size/mtime 360513536/1791489715；没有真实模型、设置、网络/付费调用或 commit/reset。AG20 的6 HTTP/75669 tokens 无有效研究与人工质量验收仍 pending，本卡不声明 A1a/A1b 开放，不启动 AG24。

### AG24 组合交互与阶段验收

- **目标与范围：** 实现起点、已发生成交和修正录入、未知字段补问、组合方案、不行动对照与决定记录，完成 A1b 的桌面/手机闭环。
- **交付：** 组合 Web/API 联调、授权语义与完整 S11/S12 回归、A1b 验收记录；复用已有决定和操作面板。
- **独立验收：** 用户“接受”“以后买”和“已经买”进入不同记录；输入缺失不猜测；同一现金不重复花；费用与覆盖限制清楚可见；旧公司研究与真实账本互不混淆。
- **退出条件：** 从组合起点到建议、决定、实际事实、重新评估完整可演示，权限和并发不变量通过后开放组合建议。

#### AG24 完成记录（2026-10-10，工程能力默认关闭）

- **交付：** Agent 页集成独立全宽真实组合卡片，桌面并排互斥选项、手机单列；开户、完整/部分起点、canonical name/full ticker 查找后的确认持仓、实际买卖、资金进出/分红、对账与原事实冲销、分页历史、单账户 typed mandate 数值约束、整案/不行动与 accept/defer/independent 及显式重新研究。表单按按钮一次展开一项；实际事实使用原生日期时间+明确北京时间/UTC及保存预览，不能把计划数量/价格/费用预填为成交；现金/费用空白保留未知，显式0单独确认。补最小认证 exact owner/account/kind `draft/publication_check/review` 读取与readiness账本开关字段；公司Agent流程保持。
- **事实、绑定与可靠性：** UserDecision 与真实账本保持独立，接受不预留资金/提交订单；数值约束经existing auth/CAS revision且保留其他fields/free text。选账户后主动绑定exact不可变组合记录到下一条`portfolio_action`，新绑定清旧plan/snapshot，非持仓证券仍明确选择；读取/账户切换不调用模型。账户选择随reload恢复，pending优先exact account。所有写请求发送前持久化同UUID/exactbody/path/method，存储失败不发送；输入锁、busy与异步epoch守卫防重复/串账户。丢回执后401/403保留原请求，权限恢复后重取；明确422/409拒绝后解除，409刷新当前记录并保留原请求审阅，不自动写新事实。
- **展示与完成门槛：** 现金/总资产/本次交易数量/方案后持仓/单股及行业占比区分；金额和数量使用server Decimal序列化后的exact字符串，不在浏览器计算现金/NAV；占比仅字符位移，支持1E-18/负数/重复小数/有界指数，完整精度在审计。费用、执行价假设、性质/来源及版本普通展示，约束违规与缺资料单列。原option.executable flag不可变，当前PlanView.status/reasons限制接受；published等待/不行动可记录接受，即使不生成方向性数量。补研究完成边界：`portfolio_action`必须真正published才能succeeded，仅progress prose或发布失败保持partial/evidence_blocked；中间进度改“模型输出已保存，正在核验研究与发布条件”，不留终态草稿错觉。
- **真实浏览器：** 独立19853/临时file SQLite/真实HTTP/原生FunctionModel→get context→calculate→prepare→completion→worker→plan，1440/390均最终通过。fixture-only显式TradingRuleResolver注入，生产Unavailable不变；同1000现金/10价格，两互斥减仓10/20股、费用1/2、现金1099/1198、总资产1999/1998、最终持仓90/80，接受后账本仍100股/1000现金。实际成交字段空白另填未知费→原案受限；丢回执→真实403→reload/原UUID重取只一事件；失败重算真实partial/check；原事件冲销后用户明确第二会话/latest snapshot替代整案/previous_ref旧史；partial未知现金、真实并发409、default-off、XSS和零横向溢出。每视口最终15个本地FunctionModel请求、usage450tokens，无外部网络/付费。截图与[独立验收记录](ag24-portfolio-acceptance.md)已保存并view_image检查，手机viewport可直接读字。
- **验证与修前记录：** 前置受影响专项65 passed/11.68s；新增read API/auth/native“无发布不能成功”回归后research/execution/portfolio **85 passed/26.37s**；legacy页面guard最小范围适配 **23 passed/5.25s**；JS syntax/percentage案例通过。修前全量因必要研究门槛修订主动中断，真实为384 passed、1 failed、3 skipped、6 deselected、129.38s/exit2；唯一失败是legacy禁买卖词guard尚未限定豁免AG24实际事实模块，保留legacy其他页限制后专项已绿，不把中断轮冒称完成。最终固定 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` **2021 passed、3 skipped、6 deselected，298.14s/exit0**；3skip仍外部ZCLAUDIA_GATEWAY_DIR未配置，不计通过。Ruff/mypy258/diff/JS/head036通过。AG17、AG18、AG19真实旧浏览器桌面/手机兼容通过；full后AG18 views与AG19 controls再次串行补验，两视口均exit0，结果见验收记录。
- **预算与产品边界：** 首轮fixture env未启用被默认关闭、旧notice等待提前、409刷新覆盖错误和第二账户reload误选均定位修复。真实100k预留门槛样本（第三次长会话复核/方向性完整工具上下文）停止为budget_exhausted，未放宽AG23；最终fixture从启动用300k/task、1.5m/owner/day，有明确用户新会话，累计owner日预算与每Task生命周期不重置，生产预算不改。AG20仍6HTTP/75669tokens、零有效研究与人工质量pending；FCFE0/4、生产可信交易规则defaultUnavailable，AG24工程闭环不授权A1a/A1b产品开放。真实`.dev/data/app.db`仍021/360513536/1791489715；没有真实迁移/设置/模型/网络/commit/reset，全部dirty/gateway工作保留，停在父review，不启动AG25。

## A2 扩展取证与运营的任务

### AG25 公告发现下载与原件保存

- **目标与范围：** 从受控公告来源校验证券、披露标识和时间，下载并保存不可变 PDF/HTML 原件、hash 与更正关系。
- **交付与位置：** `evidence/` 来源适配、下载任务、受控 artifact 与发现/获取工具；既有公告标题入口继续可用。
- **独立验收：** 来源协议/主机、大小、超时与重定向受控；重复公告不重复存储，身份歧义不自动归属；失败保留真实原因，原文可回看。
- **边界：** 下载成功不等于事实核验；不引入任意浏览器自动操作或无限抓取。技术上不依赖组合账本，默认排在 A1b 交付后。

### AG25 完成记录（2026-10-10）

- **工程交付与默认关闭：** 新增 `evidence/disclosure_http.py`、`evidence/disclosures.py`、`agent/disclosure_tools.py`、认证 `/investor/disclosures` API 与临时库迁移037。`DISCLOSURE_ORIGINALS_ENABLED=false` 默认禁止新外网发现/获取；已保存的认证元数据、原件附件、精确 operation/message 回放继续可读。未改真实设置、真实库、模型配置、gateway或既有公告标题/日历入口。
- **实际来源范围：** `cninfo.v1` 仅覆盖已存在的深圳 A 股 listing，按完整代码+SZSE、官方目录唯一 `orgId` 与官方 `column=szse` 查询交叉约束；公告 ID 与毫秒 publication time 必须来自官方响应，无名称/短代码猜测。仅单页最多20项、最多366日窗口；不实现上交所/北交所直连、任意发行人网址或任意 URL。官方当前前端公开协议见[巨潮公告检索](https://www.cninfo.com.cn/new/commonUrl/pageOfSearch?url=disclosure/list/search)及[官方 history-notice.js](https://www.cninfo.com.cn/new/js/app/disclosure/notice/history-notice.js?v=20260710082532)，2026-10-10窄范围只读核对，JS SHA256 `ef36c3fb82af6b4176c2b4fcf92c1c7eb58ff03beba7931fa48bc1b2b417213a`（70937 bytes）。只读主页/公告检索页/公开脚本以确认目录及表单协议，没有调用公告检索数据 endpoint、股票目录下载或真实 PDF/HTML 原件采集；未调用真实/付费模型。生产协议无公开稳定 SLA，现场公告发现/原件 TLS、响应 MIME及HTML覆盖仍未经 live 验证，schema变化/挑战页须失败并保存真实原因。
- **HTTP与原件边界：** 每跳重新检查 HTTPS/443/无userinfo、固定官方主机与路径，拒绝私网、保留地址、混合DNS、映射IPv6和越界redirect。经过验证的数值IP直接 socket connect，保留官方 Host、TLS证书校验和SNI，不先检查DNS再不可控解析；无环境proxy、凭据、cookie、压缩解码或浏览器执行。最多2次redirect、每个响应15秒总deadline、上限16MiB，连接/TLS/头部/流式正文均有deadline与主动socket abort。libc DNS resolver无法取消时仅可能留有有限待结束解析工作，超时后不能继续打开连接。PDF需MIME+签名/EOF；HTML字节原样存储，读回只能attachment、nosniff、CSP sandbox/default-src none。
- **身份、版本与PIT：** SQLite不可变blob/hash、owner元数据、原件、candidate SavedEvidence及获取receipt事务保存；SQL写保留在完整受控下载/校验后，短writer reservation后做重复/hash检查，独立session并发及同operation来源变字节只复用赢家，回滚/重连不留被引用但缺失的文件。相同hash共享原件，不作为独立佐证；同披露ID变字节保存新原件，保留旧原件和 `prior_original_refs`。更正/修订标题仅 `correction_lead`，当前官方协议没有可信更正目标字段，`verified_correction_refs` 保持空，不发明关系。原件来源/首次公开/实际取得/available时间都保存；普通读取同时限制公开与取得/available，历史重建必须显式标记，不能回填当时已知证据。当前和历史任务均固定入场cutoff，不在获取后偷偷推进；fetch结果显式返回 `evidence_availability`、`acquired_at`、`readable_after` 与 `requires_new_message`，新取得原件必须在后续明确消息的较晚cutoff才能进入证据上下文。
- **真正接通权限路径：** 认证消息显式 `disclosure_access` 指定listing/source/count/response size；服务校验已绑定真实深圳listing、开关、重复和硬上限，再从消息research cutoff派生不可变grant。默认消息/后台任务不会取得此权限。模型工具只接业务ID，不接URL/owner/权限；source原文/标题保持不可信数据，不能扩权。native PreparedToolAction/external_unknown/receipt/reconcile接通；网络前释放fence事务，下载后在同一保存事务重新检查权限、停止、lease及开关，失效则original/blob/evidence/receipt全回滚。GET可能物理重复，但精确已保存动作幂等，不宣称物理 exactly-once。原始bytes不进入模型消息、ToolAction或公共事件。
- **验收：** AG25来源/传输/存储/API/真实 FunctionModel离线fixture共56项；连同runtime settings和既有监测revision回归为 **73 passed，9.54s**。最终固定代码 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 为 **2077 passed、3 skipped、6 deselected，260.32s**；3项gateway跳过因未配置独立built checkout。Ruff、mypy（263 source files）、diff --check通过，Alembic head037。AG25未修改dependencies/uv.lock（既有dirty lock保留）；最终SHA256 `ea3b9ec3183fd1a96563fbdb51be045598803784294c6cec0e8c5ce34330b804`。迁移只对temp fresh/036→037→036→037 roundtrip；历史固定迁移目标保留，当前head断言跟随037。真实 `.dev/data/app.db` 仍021、360513536 bytes、mtime1791489715；AG20真实质量仍6HTTP/75669tokens/零有效研究与人工量表pending，FCFE0/4，不授权A1a/A1b/A2产品开放。
- **AG26专门交接：** 从 `DisclosureOriginal` owner记录与 `DisclosureBlob` 精确hash读原始字节，保留版本/时间/原始source ID；不要重写HTML、执行脚本或将标题/URL/下载成功升级为财务事实。解析产物需另有页/段/单元格locator、period/单位/合并范围、parser版本和verified事实核验；原件取得晚于历史cutoff只能显式重建。AG26必须沿用固定cutoff与fetch availability原因；当前研究同样不能在同一已入场任务消费新取得原件，需后续明确消息绑定较晚cutoff，历史任务不得自动推进。`prior_original_refs` 是字节版本关系，不是已核验语义更正；必须取得可信更正目标关系后再形成证据更正链/关键结论限制与复核。当前HTML、OCR/表格解析、跨公告纠错传播和现场来源coverage均未交付，AG26未启动，停在父review。

### AG26 原文解析核验与更正传播

- **目标与范围：** PDF/HTML 解析、页码/段落/表格定位、候选事实、单位/期间/合并口径核验、冲突与更正传播；OCR 结果默认待核。
- **交付与位置：** `evidence/` 解析器及核验管线、原文引用界面，接 AG05 证据服务和 AG15 依赖复核。
- **独立验收：** S13：表格错位、材料注入、更正公告及解析失败不会制造事实；撤回/更正关键证据让旧意见受限；旧原件、提取版本与定位仍可查。
- **边界：** 首批选择可维护的格式范围；不要求 OCR 覆盖所有扫描件。不支持的材料明确受限，不用模型记忆补正文。

### AG26 完成记录（2026-10-10）

- **工程状态：** 已交付限定格式的原文解析、确定性核验、更正及冲突传播；实际支持范围、资源限制、依赖和 AG27 交接见 [AG26 原文解析说明](ag26-original-parsing.md)。完整长年报、复杂表格、扫描 OCR 和广泛自然语言更正仍 needs_data，不声称全量报告覆盖。
- **实现：** `evidence/original_parser.py` 的有界真实 PDF/安全 HTML 子进程；`evidence/documents.py` 原件 hash/locator/header provenance、Decimal 精确换算、独立 Snapshot 精确核验及 immutable parse manifest。中文单表年度披露要求表格之前连续明确 header block；晚出现或无关章节 header 不能赋予旧表格口径。候选与已核验事实不覆盖，归一化冲突身份独立于物理 locator；更正必须有明确旧公告关系和独立旧新核验。
- **应用收口：** 认证 conversation→queue→ResearchPolicy→FunctionModel 使用实际授权保存原件；两个 native 解析/读取工具共用计数，解析期间无数据库长事务，取消/关闭后不落证据或 receipt。认证只读片段 API 与安全来源抽屉支持原文、候选、当前冲突/待核差异及附件。新解析产物保留真实 created/available 时间，晚于固定 cutoff 要求后续明确消息，旧任务不自动前进。
- **意见传播：** NumericResolver、即时意见读取及直接 get_evidence 都投影当前冲突/更正/待核差异；旧正文、旧 cutoff 和 visible_at 不改变。AG15 receipt 幂等复核保留双方依据，不把 candidate 提升为事实。
- **验证：** 核心 focused 140 passed（最终 header-order 补充后核心 41 passed）；Ruff、mypy 266、diff 和 JavaScript 语法通过。实际生成中文 PDF/HTML/扫描/加密/超限、跨源核验、真实 native 路径及取消/关闭、历史及当前视图、候选/已核验冲突/更正、生产子进程并发与回收均有离线测试。1440/390 浏览器实际打开新来源抽屉，验证注入文本不执行、候选 provenance、分页及认证 sandbox 附件；截图位于 `probes/ag26-original-1440.png` 与 `probes/ag26-original-390.png`。
- **中断审计：** 第一次全量在发现直接证据读取缺少当前冲突投影后主动中断：130 passed / 6 deselected / 68.72s / exit 2；第二次在发现晚出现 header 可以错误赋予表格期间后主动中断：542 passed / 3 skipped / 6 deselected / 174.05s / exit 2。均非通过的完整验收。最终固定命令 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 完整通过：2118 passed / 3 skipped / 6 deselected / 305.91s / exit 0；3 skip 仅外部 `ZCLAUDIA_GATEWAY_DIR` 未配置。
- **运行边界：** 无新迁移，head 037；真实 `.dev/data/app.db` 仍 021、360513536 字节、mtime 1791489715。无真实模型/网络采集/设置更改，保持 default off。AG20 6 HTTP / 75669 tokens / 0 有效研究、真实质量 gate pending、FCFE 0/4 和 trading rules unavailable 不变。
- **AG27 交接：** 复用 immutable 原字节及 `parsed_fragments` SavedEvidence manifest 做有界索引，保留 parser version/hash/availability；搜索命中不代表 verified，不能扩大来源、授权或旧 cutoff。本 agent 停在 AG26，不启动下一卡。

### AG27 受控搜索与跨公司取证

- **目标与范围：** 通过来源清单发现材料，按证券、期间、字段和关键词检索已保存片段，并在需要时读取相邻内容；扩大有同口径依据的跨公司比较。
- **交付与位置：** `evidence/` 检索工具和 `agent/` 取证适配，接既有比较产物与界面；搜索调用计入范围和预算。
- **独立验收：** 搜索摘要只作线索，关键主张可回原件；转载不算独立证据；比较时期不一致时不硬排结论；范围外来源不能绕过权限。
- **边界：** 先用字段与全文检索，不默认建向量库。扩展研究覆盖不能放宽发布门槛。

- **工程状态（2026-10-11）：** 已实现保存材料字面搜索与 exact saved-source 比较；范围、资源、时间及 AG28 交接见 [AG27 受控搜索说明](ag27-controlled-search.md)。复用 AG26 immutable manifest，无新数据库表或解析重跑。搜索结果始终 discovery_only，相关性不是事实认证。
- **真实入口：** 认证消息所选 listing → 服务端不可变查询 grant → queue → ResearchPolicy → native FunctionModel 的 `search_saved_sources` / `compare_saved_sources`。范围、lease、取消、当前开关、固定 cutoff 和预算复核沿用既有路径；query grant 不扩充原件 discover/fetch/parse 权限。认证历史搜索 API 与工作台入口在 feature off 仍可读。
- **有界查询与语义：** owner/listing/field/period_end/字面关键词及 published/acquired/available/created 先过滤再 limit；UTC 六位小数精确时间，解析晚于入场不可见。100 条保存记录及 220 个片段分别按请求证券公平分配，相同相关性跨证券交错；覆盖窗口、截短、审计条数和 hash 明示。同 hash/locator 合并但不同证券仍独立展示，转载不能增加独立支持。相邻内容只取 exact original/parser/fragment 的唯一可见保存 manifest；更正、候选、冲突状态保留。比较需完全相同期间/basis、metric、unit、currency 与显式 scope，并要求当前 verified 事实，否则仅并列 not_comparable / ranking=null。
- **专项与浏览器：** 最终集中 focused **44 passed / 19.43s**；Ruff、mypy（267 source files）、JS 语法、diff 通过。实际 PDF/HTML 保存 manifest、晚解析及微秒 cutoff、字面 Unicode/引号/反斜线/%/_、属主与证券授权、范围外 native 拒绝、同原文审计、公平记录/片段窗口、期间不一致与当前更正传播有回归。1440/390 实际 HTTP / 临时文件 SQLite 的搜索、分页、来源定位、邻文校验、安全 DOM 和无横溢出通过；已查看手机截图。原生授权测试使用真实认证消息准入，不是手工 grant-only 测试。
- **修复审计：** 初始 focused 暴露原件标题实际存在于 DisclosureItem、搜索缺 untrusted_source_data 标识，以及公平窗口测试的 fixture 取得时间晚于 cutoff；均据实际服务约束修复。后续回归要求相同相关性第一页跨公司交错，加入公平片段预算回归。不把失败轮次算通过；无提前全量重启。
- **全量验证：** 单次冻结代码的固定命令 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 完整通过：**2127 passed / 3 skipped / 6 deselected / 282.69s / exit 0**。三项 skip 为未配置外部 `ZCLAUDIA_GATEWAY_DIR`，未伪造 gateway 成功。
- **运行边界与 AG28 交接：** migration head 仍 037，无新迁移；真实 `.dev/data/app.db` 仍 021 / 360513536 字节 / mtime 1791489715。无真实设置写入、模型/付费调用、外部采集或通知，default off；AG20 6 HTTP / 75669 tokens / 0 有效研究、质量 gate pending、FCFE 0/4、trading rules unavailable 保持。AG28 可复用上述已保存取证工具及 caps，但不得扩大 grant/来源/旧 cutoff，也不得把命中数当支持强度。本 agent 完成后停在 AG27，不自动启动 AG28。

### AG28 扩大后台运营与可选通知

- **目标与范围：** 在首版队列上扩大多 case 调度，处理拥塞、公平性、过期复核、暂停恢复和成本；实现用户显式启用的外部渠道与可选摘要。
- **交付与位置：** scheduler/worker 容量配置、运营指标、通知投递适配与设置；复用站内 outbox 和稳定去重身份。
- **独立验收：** 交互任务不被探索饿死；无变化不提醒；外部投递失败只重试投递，不重新生成观点；暂停期间恢复后合并当前变化，预算不会跨日自动重置同一任务上限。
- **边界：** 默认站内渠道；没有配置和授权不发送外部内容。只有测得现有基础设施不足才另评估新队列服务。A2 开放前合并验收 AG25 至 AG28 及 S13。

#### AG28 完成记录（2026-10-11）

- **工程状态：** 已实现、未提交。复用既有 PipelineRun/TaskService/AG15 station outbox，新增 `agent/operations.py`、delivery-only `investor/notifications.py` 与 `notification_http.py`，038 只新增持久调度 cursor 和 optional delivery 表。详见 [AG28 运营与通知](ag28-operations-notifications.md)，所有既有 dirty/user/gateway 修改保留。
- **有界调度：** persisted 3 interactive : 1 review : 1 exploration : 1 legacy 加权槽，least-served account/case 与 300 秒 aging；默认 queued/running purpose caps 128/96/32、总 running 4、review 2、exploration 1，全部非交互含 legacy 最多 3。四项 runtime settings 只能降低安全上限；短 SQLite writer reservation 后 CAS，独立 writers 与 explicit native run_id 不能绕过。AG23 least-checked 不变；单 worker 不抢占在途工作。legacy-only 自定义/CLI 与无 kinds/run_id 的内部 FIFO 消费者保留历史行为；可占用 global running 延迟 managed interactive，不声称任意消费者受默认 native registry 的 shared cap 保证。
- **预算与恢复：** 未更改 native lease、settings revision、unknown usage、取消、授权、day/purpose/lifetime cost guards；重试/等待恢复/过午夜仍同一 Task。terminal failed/partial/cancelled 没有自动新付费任务。恢复无变化静默；后台暂停继续合并当前 dirty，历史通知不逐条重放。
- **外部渠道：** 默认 off；只有 actual API/web positive opt-in、用户地址和可解密加密设置才 ready。新增设置组和认证 `/investor/operations` purpose/ready/running/oldest-wait/budget-blocked/delivery metrics。四项 notification 字段不 env 导入；地址/token Fernet 加密及掩码。只冻结实际新站内变化的状态/公开 refs；不导出 private prompts、credentials、原文 HTML。purpose/outbox 容量满保留可见 blocked 状态，不另建预算。
- **投递安全：** 真实 bounded HTTPS adapter 验证公网 DNS、数字地址 pin 和原 host TLS，拒绝 redirects/ambient credentials，DNS5s+HTTP10s、request16KiB/response4KiB。同 stable id/frozen bytes retry-only；unknown ack 默认停止，只有用户确认接收端 idempotency 才最多 3 attempts，不能声称物理 exactly once。当前 station/case revision、mute/pause/archive、consent era 和 channel fingerprint 在发送前复查；disable→enable/mute→unmute 未经过 polling 也不复活旧 backlog。新 revision 后 old sending recovery 不重试旧内容，在途无法撤回的限制明确。
- **A2/S13 工程与产品状态：** AG25 fixture acquisition→AG26 actual parser conflict/correction→AG27 saved search→AG28 native maintenance 联合证明无正式观点时不产生虚假提醒或模型任务；既有正式观点复核、AG08 跨日 lifetime、AG23 单 review Task 回归继续通过。AG25–28 工程可合并验收；A2 产品开放仍 pending，AG20 真实质量 6HTTP/75669tokens/0有效研究、FCFE0/4、trading rules unavailable 与 AG25现场来源覆盖缺口保持，不作 release claim。
- **验证及修复：** 早期 helper/required reason、exact secret-key registry、038 compare 误含既有旧索引、SPECS 分组排序 failures 均在 [运营记录](ag28-operations-notifications.md) 保留；只修本卡实现/测试，没有更改旧索引或假冒跳过通过。集中 AG06/08/15/23/25–28/settings/migration focused **274 passed，61.13s**；独立真实 HTTP/file SQLite Chromium **1440/390** positive consent、invalid URL、encrypted save/reload/disable 通过。最终冻结前专项 **39 passed，9.36s**。首轮全量 **2164 passed、2 failed、3 skipped、6 deselected，287.52s**：固定033 substring metadata 误包含 later038 notification delivery；AG16 budget stress fixture 同时 claim 两个探索被正确的 managed exploration=1 cap 拒绝。只精确排除 later 表，并将 ledger fixture 标明用历史内部 FIFO seam 独立压力验证 budget（真实 lease/CAS 与预算断言保留），生产容量未放宽；修后集中 **62 passed，16.63s**，Ruff/mypy270/JS/diff/head038通过。最终固定 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 为 **2166 passed、3 skipped、6 deselected，278.00s，exit 0**；三 skip 仍是外部 `ZCLAUDIA_GATEWAY_DIR` 未配置，不计通过。测试和浏览器子进程全部结束，未提前重启全量。
- **运行边界及 AG29 交接：** 临时 fresh/037→038→037→038 通过；固定历史037不改，仅当前head断言更新。真实 `.dev/data/app.db` 仍021/360513536bytes/mtime1791489715，没有真实设置写入、模型/付费/外部采集或真实通知。optional digest 未实现。AG29 继续精确组合 snapshot/约束与未成交计划语义，不能换 Task 重置预算、扩大 grant 或把通知当投资效果。父任务依据既有全部30卡授权顺序启动 AG29，本子 agent 完成后停在 AG28。

## A3 长期能力的任务

### AG29 多资金用途的方案比较

- **目标与范围：** 用户明确划分资金用途、可用额度和期限，在已有组合事实之上比较用途之间的机会成本与现金保留；不重复创造账户现金。
- **交付与位置：** `portfolio/` 资金用途约束与分配情景、计划服务和对应界面，复用 AG22 的资金检查。
- **独立验收：** 同一实际现金划分后的总额不超已确认范围；未知账户不跨账户抵扣；短期资金要求不能被长期研究观点覆盖；变更用途后相关方案待复核。
- **边界：** 不新增杠杆、衍生品或交易执行。新增具体估值方法须另有方法规格与数据验证，不能以“更多方法”为无限范围塞入此任务。


#### AG29 完成记录（2026-10-11）

- **工程完成，产品开放仍 pending。** 039 增加精确账户的 purpose CAS head 与不可变版本；用户确认分配/预算/期限/日期/必留现金/来源/未知项，合计不超过 known_cash，未分配对账，空版本仅释放规划，接受/分配不写真实现金事件。默认沿用 portfolio_ledger_enabled=false；没有模型批准/改用途工具。
- **原生与组合闭环：** AG22 同一现金、fee-inclusive 用途预算/净支出与必留现金检查接 AG23 互斥整案/no_action，稳定 Quantity 数字路径及精确 provenance；用途/实际现金变动即时限制相关旧案，并复用 AG15/23 outbox，不自动付费重试或重置 Task 预算。名称改动不让公司观点或语义相同的整案失效。Task 知识时点前未知的新确认不能倒填，须主动新消息；旧 AG22 无用途序列化/hash与原生 checkpoint 参数兼容，无旧行重写。
- **页面与验证：** 资金用途表单、现金/保留摘要及同案比较，1440/390 真实 HTTP/SQLite/native worker 覆盖双用途、丢回执/reload/关闭后同UUID重取、CAS409、接受无实际事件、真实转出超额和 label/semantic 变更；既有 AG24 完整浏览器也通过。最终 focused65 passed/13.63s；Ruff、mypy271、JS syntax、diff/head039通过；临时 fresh/038 roundtrip通过。首次 full2180 pass/1 fail（遗漏034 current-head038断言）/3 skip/6 deselect已记录并修正；最终固定离线 **2182 passed、3 skipped、6 deselected，263.77s，exit0**。3skip仅外部 gateway未配置，不计通过。
- **公开接口/边界及 AG30：** POST /portfolio/purposes（用户确认、同UUID重放先于关闭/CAS）、GET /portfolio/purposes/current（exact owner/account）；原生 get_portfolio_context/calculation/plan 接缝使用用途。详见 [资金用途验收](ag29-funding-purposes.md) 与 probes/ag29-ui。真实库仍021/360513536bytes/mtime1791489715，无真实迁移/设置/模型/付费/采集/通知或 commit/reset。AG20仍6HTTP/75669tokens/0有效研究，FCFE0/4，生产可信交易规则Unavailable；本卡不构成产品发布。AG30继续显式用户批准的方法评估/影子/回退，不能以用途、采纳率或通知ack替代质量证明；本子agent完成AG29后停止，不启动下一卡。

### AG30 方法提案评估与回退

- **目标与范围：** 将已完成复盘中的错误模式转为带适用范围的 method_change_proposal；实现 evaluating、shadow、active、rejected、retired 与显式批准、版本回退。
- **交付与位置：** `investor/` 方法版本服务、离线评估与前瞻影子记录、提案查看和批准界面；接既有过程复盘及固定材料集。
- **独立验收：** S14 方法部分/S15：评估有原失败样本和反例；影子输出不改正式观点、不发提醒；启用有明确批准；新任务固定方法版本；回退不删历史。
- **边界：** 只发布支持的配置化研究步骤变更；公式、权限和程序修改仍走正常开发。单次收益和用户采纳率不能直接触发全局方法生效。

#### AG30 完成记录（2026-10-11）

- **工程交付：** actual completed Task 过程复盘与失败模式、精确 scope/old method 的版本提案；历史失败/同范围及范围外反例评估；SDK 原生离线重放与评估后新 Task 的前瞻 gate-shadow 分开存储，离线重放不能批准。仅支持 schema_version=1 的三个加强步骤，语义独立性仍需研究评审。
- **启用和任务绑定：** 用户 auth/CSRF、精确 revision/scope/冻结证据 hash、CAS/idempotence 批准；模型不能自批。admission 原子冻结 method receipt，重试/恢复不跟随头；旧无 receipt Task 永久 FIXED_METHOD，旧完整 grant/Task/hash/history 兼容。多 listing/组合/解释明确 baseline；当前权限、取消、归档与预算继续实时执行。退役读取与回退追加历史，正式发布/maintain/续期不能以 unanswered 部分停止绕过反证。
- **页面与验证：** `/app/methods` 中文查看/评估/批准/拒绝/回退，分页和精确 artifact/history；1440/390 真实 HTTP/SQLite/FunctionModel/browser 检查 XSS、401、CAS409、lost ack/reload/idempotent replay，并保存 [截图](probes/ag30-ui)。迁移重点 54 passed，方法/发布/复核重点 104 passed，最终方法 12 passed；Ruff/mypy273/JS/diff 通过。最终固定全量 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` 为 **2195 passed、3 skipped、6 deselected，280.50s**；3 skip 仍为外部 gateway checkout 未配置。早期 pinned026 排除问题及 fixture/浏览器问题记录保留，修正后的检查通过。
- **交付边界：** [方法演化验收](ag30-method-evolution.md) 记录支持范围和限制。临时库迁移 head040；真实021/360513536bytes/mtime1791489715 未变。没有真实迁移/设置/付费模型/通知或 commit/reset。所有30卡为工程交付，产品质量仍 pending；本 agent 完成最后一卡后停止，父 review 做整体收口。

## 推荐实施顺序与并行边界

先完成 AG01、AG02、AG03；三项互不阻塞。AG02 只补已有验证的缺口，AG03 不调用全池补采。此后按下列可交付节点推进，每完成一个节点都能独立演示：

| 节点 | 需要完成 | 可以演示的结果 |
|---|---|---|
| 执行基础 | AG04 至 AG09 | 持久任务经配置模型调用受控工具，能等待、停止、恢复和重连；此时只开放内部调试 |
| 研究与页面并行 | AG10 至 AG14、AG17 | 有证据与计算的公司判断可以发布；局部追问不覆盖观点；工作台显示真实任务状态 |
| 公司闭环 | AG15、AG16、AG18、AG19、AG20 | 有限机会发现、对话、决定、持续复核与修订完整可用 |
| 真实组合 | AG21 至 AG24 | 从真实账本到互斥方案，再到用户事实反馈与重新评估 |
| 扩大覆盖 | AG25 至 AG28 | 工程：正文取证、受控保存搜索、持久运营/可选通知；产品开放仍 pending |
| 方法与用途 | AG29、AG30 | 多资金用途比较、可审核的方法迭代 |

以上节点是演示与发布顺序，不是额外的全局开发锁。主要并行窗口：

- AG04 和 AG07 可并行；AG04 后，AG05 与 AG06 可并行。
- AG10 与执行链 AG06 至 AG09 可并行；AG10 后，AG11 补采与 AG12 发布服务可并行。真实 DCF 覆盖不阻塞通用发布服务开发。
- AG09 后即可开展 AG17；后端继续 AG12、AG14、AG13。AG13 依赖 AG14 的正式记忆和操作服务，编号不代表全局执行顺序。
- AG13 后 AG15 与 AG16 可并行；AG17 完成后按接口就绪情况分别开展 AG18、AG19，AG18 的完整验收同时需要 AG15/AG16。
- A1b 在 AG20 通过后开始。AG25 的技术依赖不含账本，但默认把资源集中在 AG21 至 AG24，随后再扩大取证。AG27 与 AG28 可在 AG26 后并行；AG29 与 AG30 在各自前置具备后可并行，默认安排在 A2 之后。

任务可并行不等于所有文件可同时改。共享位置采用以下集成约定：

| 共享位置 | 处理方式 |
|---|---|
| `alembic/versions/`、模型注册与迁移 head 断言 | 每项只迁移自己负责的对象，按合并顺序串行分配 revision；不预写编号或产生未处理多 head |
| `config.py`、`settings/registry.py`、设置加载 | AG07 负责模型键，AG08 负责执行预算/开关；后者基于前者合并后的版本扩展，AG19 只接已存在的配置接口 |
| `research/worker.py`、scheduler 注册 | 各任务把处理器放各自模块；注册入口按前置合并后逐项接入，禁止复制一套 worker |
| API 路由注册与共用 schema | AG01 固定契约；各任务拥有独立路由模块，入口注册只做必要增量 |
| `web/static/app.js`、共用 CSS 与导航 | AG17 先建立独立 Agent 模块入口；AG18/AG19 各自实现组件，公共入口变更串行集成 |

## 阶段验收责任

| 契约中的场景 | 主要实现责任 | 集成收口 |
|---|---|---|
| S01 至 S04 研究内容与不迎合 | AG10 AG12 AG13 | AG20 |
| S05 变化到复核闭环 | AG12 AG13 AG15 | AG20 |
| S06 决定不等于成交 | AG14 AG19 | AG20；AG24 再验证真实账本隔离 |
| S07 引用与数值 | AG05 AG10 AG12 | AG20 |
| S08 崩溃与重复提交 | AG06 AG08 AG12 AG15 | AG20 |
| S09 并发与跨会话 | AG09 AG12 AG17 AG18 | AG20 |
| S10 取消预算断线 | AG06 AG08 AG09 AG17 | AG20 |
| S11 S12 组合一致性 | AG21 AG22 AG23 | AG24 |
| S13 原文更正与注入 | AG25 AG26 AG27 | AG28 的 A2 阶段检查 |
| S14 偏好遗忘与方法回退 | AG14 AG19；AG30 | AG20；AG30 |
| S15 方法不只适用于成功案例 | AG30 | AG30 |
| S16 到期与续期并发 | AG12 AG15 | AG20 |
| S17 局部问答与用户控制 | AG13 AG14 AG19 | AG20 |

下一项开发可以直接指定编号，例如“实现 AG01，按任务范围完成并验收”。开始后只推进该任务及其明确范围内的必要配套；前置缺失时先报告具体依赖，不用假接口冒充已完成业务。

- **AG20最后身份bug工程收口：** grant/binding共同授权listing按主键注入服务端现有canonical_name/full ticker/exact listing_ref；身份是当前元数据而非金融证据/历史名称核验，缺失unavailable无guess，无新工具/权限扩大。默认worker真实native SDK→MockTransport→SQLite来源覆盖同名/错scope/历史binding及缺失身份。affected122 passed31.69s，Ruff/mypy247/diff通过；最终固定全量1955 passed、3 skipped、6 deselected195.63s。3skip仍为外部gateway未配置。历史首轮/0HTTP/最终真实轮审计保留，总6HTTP/75669tokens；最后工程版本无真实模型再调用，不声称旧失败已通过。隔离密文凭据全部清除，原021 stat/hash不变、Agent/backgroundoff/DCFoff；工程complete，产品真实研究/人工理解/量表/A1a gates pending；AG21由父review决定，未启动。

## AI 页后端完成记录（AIB，2026-10-11）

依据[AI 页后端实现方案](ai-page-backend-plan.md)交付 AIB1、AIB2；工作区混有未提交的 investor-agent 基线，本轮全部改动留在工作区未提交，由用户决定提交边界。

### AIB1 完成记录（2026-10-11）

- **状态：** 完成。迁移 `041_agent_conversation_activity`：`agent_conversation` 新增 `last_activity_at`（可空，仅 DDL 兼容）、`title_revision`（server default 1）、`subject_refs`（JSON，默认 `[]`）与索引 `(owner_id, last_activity_at, id)`。因 pysqlite 传统模式对 DDL 自动提交，回填采用**先全量校验计算、后 DDL 与写入**的顺序：payload 时间戳必须带时区，统一转 UTC 写成与 ORM 相同的六位微秒格式，非法时间在任何 DDL 之前使迁移失败、库保持原状；对象投影按主键关系回填（listing/candidate/watchlist_item/快照/预告/业务分段/case/正式观点→case），不可解析保留空投影。
- **行为：** `create` 写入活动时间、标题修订与创建对象投影；`submit`/`save_assistant` 仅在真实新增消息的会话写锁内以 max 更新活动时间，幂等重放、任务事件、取消与重命名不动它。`GET /agent/conversation-summaries`：attention 由全部关联任务聚合（needs_you > running > none，owner 限定、EXISTS 筛选在分页前），`latest_task` 取 sequence 最大的用户消息任务，subjects 优先最新任务 `binding.base_refs` 的去重 listing、无消息回退创建投影，身份按主键批量解析、缺失为 `unavailable`；键集分页（limit+1、不透明版本化游标、筛选不一致 422 `invalid_cursor`），整页固定次数查询。`PATCH /agent/conversations/{id}` 独立标题 CAS：NFC+去空白、1–256、409 `conversation_title_conflict`、跨 owner 404、无变化不递增、不触碰 `Conversation.revision` 与消息引用。
- **验证：** `tests/integration/test_api_agent_summaries.py` 5 例（排序/字段/取消中、attention 筛选与优先级及终态不遮蔽、同时间分页不重不漏与游标校验、重命名修订/冲突/清除/NFC/历史不变、subjects 跟随最新消息）+ `tests/integration/test_migration_041.py` 3 例（混合偏移/微秒回填与投影、非法时间失败保持 040、fresh head）全部通过；既有 head 断言（迁移 025/027–031/034/035、funnel、backup_restore）随 head 040→041 同步。ORM 与迁移经 `compare_metadata` 无 agent_conversation 差异。
- **边界与未验证项：** 真实库仍在 021，上线须先按运维流程过 022–040 再 041（备份后在副本验证）；并发仅覆盖既有写锁路径，未做专门压测与查询计划验证；旧列表接口形状未变。

### AIB2 完成记录（2026-10-11）

- **状态：** 完成。新增 `agent/progress.py` 只读投影 `task_progress`：最新 `research_progress` 按 `Task.artifact_refs` 追加序逆向选取已提交项（不依赖 SQL 返回顺序与随机 UUID），`saved_at` 取对应 `artifact_saved` 保留事件；`evidence_steps` 与 `completed_steps` NFC+空白规整后精确匹配，首个未完成项仅运行中且未取消时为 `next`，措辞不同项进 `extra_completed`；排队/等待/终态无 `next`。`limits` 为当前策略参考：设置四项限额 + 只读美元上限投影（复用 `cost_limits` 的最小值与 USD 校验规则，mandate 缺失/替换或口径非法降级为 null+稳定原因，无约束为 `unbounded`）；`finish_only` 复用从 `research_headroom` 抽出的纯函数 `BudgetService.headroom`（执行语义不变），终态恒 false；`usage` 直取 `Task.usage`；活动任务计时含排队等待，终态从终态 `task_state` 事件固定、事件清理后 null+`event_history_pruned`。无研究授权任务：独立 `GET /agent/tasks/{id}/progress` 404，`GET /agent/tasks/{id}/snapshot?include_progress=true` 返回任务且 `progress=null`；默认快照形状不变，投影与 task/messages/cursor 同一真实读事务。不构造 `TaskClaim`、不调用 fence/check_boundary/live_policy、不写预算记录，Agent 关闭后历史可读。
- **验证：** `tests/test_agent_progress.py` 6 例（plan 空态/next-done-pending/extra/回放不重复发事件、最新计划选取与事件清理降级及关闭 Agent 可读、等待无 next、finish_only 与 research_headroom 一致且 comparison 恒 false 及 usage 未知保持 null、mandate USD/清除降级、路由契约与缺授权 404）全部通过；执行路径未改（`budget.py` 仅抽取，`agent/tasks.py`/`tools.py` 未重复发事件）。
- **门禁：** `ruff check .` 通过、`mypy src` 275 文件无错误、`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 pytest -q -m "not network"` 2237 passed、3 skipped、6 deselected。仅剩 2 个与本轮无关的既有失败：`test_ui_has_no_trading_language_or_scores`（工作区未提交的 `candidates.js` 含"减仓"，用户 WIP）与 `test_api_lifespan_disposes_the_engine_on_shutdown`（顺序性连接池计数竞态，用既有 `test_agent_tasks.py+test_agent_conversations.py` 组合即可复现，与本次改动无关）。
- **边界与未验证项：** AIF 前端接入、浏览器用例扩展（agent_workbench.mjs）与 AIB3/AIB4 未做；真实库迁移同 AIB1。
