# AG20 公司观点闭环验收与内部试用

> 2026-10-11 上下文与预算修复最新状态：实现实际请求预留、旧交互投影、强制收尾与前序证据恢复。有限真实验证累计十五次 HTTP，十四次已知用量共 209496 tokens，另一次未知；仍无合格终稿，最后阻塞为引用契约及供应商请求失败。详见[修复与真实验证](probes/ag20-context-budget-2026-10-11.md)。


日期：2026-10-10。状态：**engineering complete / product gate pending**。代码无提交；本记录不能代替用户批准或人工语义评审。Agent 和 background 的生产默认值继续为 false，A1a 入口尚未正式开放。三样本选择也不代表质量通过。


最新真实试用：[三轮汇总](probes/ag20-real-trial-2026-10-10.md)及统一JSON，累计6HTTP/75669tokens；schema修复可调用，但模型三样本均未完成研究，人工无正式结果可评分。Provider输出超过请求4096上界、试用identity上下文缺公司名/ticker等限制如实记录；身份缺口已最小修复并仅离线验证；不再付费，真实修复版本待未来验证，方法/模型质量仍pending。
最新后续验证（2026-10-11）：[失败分析、修复与有限复验](probes/ag20-followup-2026-10-11.md)。本轮新增10 HTTP /159102 tokens，已进入实际查询和证据保存，但仍无合格研究或正式观点；新旧试次分开记账，以下历史记录保留。

## 实际交付与证据分层

公司研究、可信准备/审查/发布、用户决定、材料/期限触发、人工或后台复核、意见版本和站内通知已有完整接缝。AG20 复用前置强回归，并扩展 AG15 的实际 native worker 修订用例：发布后用户暂缓采纳、观点受限、请求复核、原生 prepare/semantic/publish、旧意见 superseded、一次变化通知、旧决定继续引用旧版本，HoldingItem 数量保持不变。

新增 `tests/integration/agent_process_fixture.py` 与 `test_agent_process_acceptance.py` 在独立 OS 子进程、真实 TCP FastAPI、ResearchWorker 和文件 SQLite 上执行：

- API 消息事务已提交，ASGI `http.response.start` 发送前 `os._exit(86)`；客户端实际收到 HTTP 断线。重启后相同 client_message_id 和精确正文恢复同一回执；改正文 409。关闭新 admission 后旧回执仍能读/重放，新 UUID 拒绝，healthz 继续可用。
- worker 在 PublicationService 发布事务、MonitoringService 发件箱消费事务分别提交后写阶段标记，然后父测试 **SIGKILL 该唯一 PID**。重启进程由 `ResearchWorker.recover_stale` 失效旧租约，拒绝旧 Claim 的新写，替代 attempt 复用相同 operation_id。动作、发布事件、发件箱和站内通知各一次，TaskAttempt 保留 abandoned 并最终 succeeded。此处“恰好一次”限于这些已提交的 SQLite 业务对象；外部未知效果仍须查询和对账，不能推为任意外部 exactly once。

虚构候选和 FunctionModel 只用于工程验证，不证明投资语义正确。真实模型路径使用正常 AgentExecutor、ResearchPolicy 和 `agent_job_handlers`，不使用上述测试进程的自定义执行器。

## 场景矩阵

表内文件/测试名对应实际执行回归。工程通过只说明不变量；涉及结论含义、真实反证和用户理解的部分均未由 fixture 代签。

|场景|实际工程证据|真实模型/人工门槛|
|---|---|---|
|S01 研究价值/持续性|[native preparation](../../tests/test_agent_research.py) `test_native_full_preparation_and_exact_semantic_publication`；[trial runner](../../tests/integration/test_agent_company_trial.py) 有限三任务/七维缺口/partial|待真实输出、事实关系2分|
|S02 缺失/冲突|[publication](../../tests/test_investor_publication.py) `test_failed_publication_retains_repairable_exact_draft`；[monitoring](../../tests/test_investor_monitoring.py) `test_real_financial_new_version_or_period_and_unknown_restrict_merge`|待核公司特定缺口与结论范围|
|S03 喜欢/讨厌|[paired native boundary](../../tests/integration/test_agent_company_acceptance.py) `test_same_saved_material_opposite_attitudes_preserve_fact_scope_and_gap`：相同来源与缺口，真实两输入进入固定政策|FunctionModel不能证明不迎合，真实配对评审待办|
|S04 价格变化|[publication](../../tests/test_investor_publication.py) `test_no_new_price_automatic_invalidation_without_triggered_condition`、`test_extension_retains_old_historical_price_requires_explicit_current_check`|真实经营判断不能据价格漂移；待模型评审|
|S05 材料→受限→复核→版本/通知|[monitoring](../../tests/test_investor_monitoring.py) `test_real_financial_new_version_or_period_and_unknown_restrict_merge`、`test_real_revision_updates_same_change_and_outbox_does_not_duplicate`|暂无真实历史假设被新材料推翻的已发布版本对照；工程变化是虚构演示|
|S06 决定≠成交|上述AG20扩展的native修订闭环；[controls](../../tests/test_investor_controls.py) `test_decisions_are_exact_history_not_transactions`|用户暂缓不是反证/成交，仍需用户理解反馈|
|S07 来源/数值/主体|[publication](../../tests/test_investor_publication.py) failed publication、numeric resolver、foreign主体及单位用例；`test_ag18_historical_body_keeps_original_numeric_source_after_correction`|真实关键事实仍需原件核验；候选不升级verified|
|S08 崩溃/重复提交|[actual OS/TCP](../../tests/integration/test_agent_process_acceptance.py) 两项；[external recovery](../../tests/test_agent_execution.py) `test_external_uncertain_recovery_reconciles_before_any_model_or_replay`|已验证API/worker关键进程边界；未声称真实上游外部写恰好一次|
|S09 CAS/多会话|[publication](../../tests/test_investor_publication.py) `test_two_independent_sessions_cannot_overwrite_new_view`；[conversation](../../tests/test_agent_conversations.py) 固定多对象/跨会话绑定；[AG18 browser](../../tests/browser/agent_views.mjs) 旧回答已更新/历史版本|人工复述当前与历史观点区别待办|
|S10 取消/断线/预算|[execution](../../tests/test_agent_execution.py) in-flight HTTP、任务跨日 lifetime、shared/purpose caps、timeout unknown charge；[API](../../tests/integration/test_api_agent.py) SSE断线/410/poll；[AG17 browser](../../tests/browser/agent_workbench.mjs)|真实usage/延迟/费用待独立配置；未知费用保留null|
|S14 偏好遗忘|[controls](../../tests/test_investor_controls.py) `test_revocation_filters_source_summary_sibling_and_prevents_reextraction`、inflight memory revoke；[research](../../tests/test_agent_research.py) expiry/source/derived过滤|仅偏好部分A1a；方法回退归AG30|
|S16 到期/续期|[publication](../../tests/test_investor_publication.py) exact expiry、cannot extend expired/limited、concurrent CAS及历史失效区间|不以新期限回填失效区间|
|S17 局部/暂停/归档|[research](../../tests/test_agent_research.py) `test_local_never_replaces_global_and_critical_counter_restricts`；[monitoring](../../tests/test_investor_monitoring.py) paused task/manual、archived pending cycle；[controls](../../tests/test_investor_controls.py) archive/pause|局部回答不替换全局，归档不自动重开|

## 真实三公司资料包与覆盖理由

[冻结 JSON](probes/ag20-company-materials-2026-10-10.json)和[可读资料包](probes/ag20-company-materials-2026-10-10.md)来自实际 `.dev/data/app.db` 只读 SQLite backup。来源 revision **021**、size **360513536**、mtime_ns **1791489715347475627**、SHA256 **85c29a22e6ece11efff8f25d76f7baf886cbf8d10efeb6a4082460c112a32e3b**。隔离副本迁移到 **034**；采集前后原库 stat/hash 相同。副本清空 app_setting、变更历史并重置 settings import；不复制正式运行配置或保留其密钥。副本目录 `/tmp/ag20-company-trial-20261010-v2` 权限0700，trial.db权限0600。

材料截止 `2026-10-10T12:00:00+08:00`（UTC04:00），每公司最多18份按 provider/part/期间冻结的最新财务版本及最新行情；它是可审阅资料包，**不是完整实际 Agent 用料集合**。真实任务绑定相同 `research_at`，实际使用来源的 committed tool receipt/hash 另录；不能把 packet hash 填作候选正式材料hash。来源记录有证券主体、provider、locator、period、available_from、captured_at、version、raw_ref、单位定义和内容hash。未注册字段不猜单位；raw_ref只保留定位，未复制原件或声称核验。

|公司|实际现有材料|覆盖理由与仍缺项|
|---|---|---|
|招商银行 600036|中报2026-06-30；取得至2026-10-07 06:41:00.819756 UTC；行情2026-10-08；旧研究v1.3|金融行业方法排除/口径边界。现金流快照不是FCFE资格，银行不套本版FCFE；旧评分解释不等同Agent观点|
|海康威视 002415|中报2026-06-30；取得至2026-10-07 04:20:56.505360 UTC；行情2026-10-08；无旧研究快照|有结构化候选、缺旧研究与原件核验，适合检验有限范围/关键缺口表达；不预设优势或方向|
|国电南瑞 600406|中报2026-06-30；取得至2026-10-07 04:47:06.439418 UTC；行情2026-10-08；旧研究v1.3|供应商中报 cash_flow 行可定位；现金持续性及期间比较须进一步研究，旧解释必须保留版本，不用CFO-capex替代FCFE|

资料包冻结时：Agent立场未生成，反证待研究，下一步是核验关键论点与反证后形成有限候选；资料包模型版本null、实际请求0、token/延迟null、money cost null。随后授权的真实模型试次单独归档，不篡改资料包原始版本。上述状态和研究缺口均在资料包中明示。不是暂不判断的“正式公司观点”，不能拿资料包作为至少一条真实研究路径已启用的证据。FCFE资格仍 **0/4**，默认DCF继续关闭；七维、数值与逐主张语义发布门槛未放松。

## 可复现内部试用

仅准备资料（不调用网络、不改源库/正式设置）：

```sh
.venv/bin/python tools/agent_company_trial.py prepare \
  --source .dev/data/app.db --output /tmp/ag20-new-isolated-trial \
  --as-of 2026-10-10T12:00:00+08:00
```

工具拒绝已有输出目录，只升级新建且带 `ag20-isolated` 标记的副本。依赖固定仓库 `.venv` 和 Alembic；保留 migration.log。当前独立 `agent_base_url`、`agent_model`、`agent_api_key` 在env和真实设置中均缺失。AG02旧网关探针不等于当前独立配置可用。这是一开始的准备状态；随后用户已明确授权临时复用当前供应商和模型，按下述真实试次记录执行。正式独立配置仍未写入，绝非静默fallback。

明确授权和独立配置到位后：配置JSON只含非秘密 `agent_*` 键，至少指定 `agent_base_url` 与 `agent_model`。凭据仅由操作环境 `AG20_AGENT_API_KEY` 提供，不在聊天/命令行参数/JSON中给出；可由用户本地密钥加载器注入。不能从旧配置静默fallback。

```sh
.venv/bin/python tools/agent_company_trial.py run \
  --directory /tmp/ag20-new-isolated-trial --model-config /private/path/independent-agent.json
```

固定硬限额：每Task请求<=8、总日请求<=24，每Task token<=50000、总日token<=150000，每请求输出<=4096，每请求45s、Task360s（外层370s取消），工具<=24/Task，无SDK重试/模型解析重试、无后台/全池抓取。只为三名样本提交显式 `company_research`，逐公司执行指定 run_id；不运行复制过来的旧队列。有限预算可能得到 partial/failed，这不是绕过门槛的理由。已启动即写标记，禁止工具重复付费运行；中断需查看持久Task/预算和结果，不删除标记自动重跑。

输出 `actual-results.json`：实际Task、公开消息、实际binding cutoff、artifact_refs、实际材料receipt及hash、AgentModelRun 的framework/http请求、usage/unknown reason、响应状态、延迟、框架/配置版本及失败类型。未知money cost始终null；供应商价格未验证时不猜价，更不以0绕过后台预算。FunctionModel离线用例中http_attempts=null，明确不是真实HTTP。配置snapshot只保存端点hash，不打印secret或credential-bearing endpoint；上游错误只记录类型。

## 已授权真实试次（追加，未补跑）

用户明确选择“临时复用现有供应商和模型进行隔离试用”后，只读读取实际effective legacy tier3配置，仍为deepseek-flash、thinking=auto；端点仅记录hash、凭据仅由进程环境注入隔离配置。显式映射到副本的OpenAI兼容Provider+DeepSeek reasoning profile、prompted/json_object、required=false、json_schema=false，与AG02支持配置一致。正式store/原库不写；原限额和冻结cutoff未变。

[实际结果与失败说明](probes/ag20-real-trial-2026-10-10.md)、[实际审计JSON](probes/ag20-real-trial-2026-10-10.json)：真实HTTP **3次**均200，输入 **33144** / 输出 **2805** token，真实模型延迟分别2362/2201/8688ms；money cost仍null（价格未验证）。每公司仅一请求便停止，不使用剩余额度自动补跑。

招商银行、海康威视：模型把listing引用错作为get_evidence的ref，海康还将case引用错作subject，可信工具以InvestorError停止。国电南瑞：native结构化输出解析成功，但ResearchPolicy完成校验拒绝，ExecutionDeniedError/evidence_blocked，Task仍failed；当前公共审计不保留具体字段级拒绝原因，不能猜测。三项均无公开assistant答案、无research_progress/正式观点、无committed材料receipt，因此actual材料hash是空集合hash，packet不是本次已使用材料。

本次验证了实际端点、受控调用与真实usage，以及模型错引用/未过完成门槛时不公开或发布；没有交付合格公司研究。立场/依据/反证/下一步的可接受Agent输出仍未生成，人工评分不能针对虚构结果进行。后续可离线改善引用类型与错误诊断，再由父任务明确决定新的有限试次，不能删除失败/usage或擅自自动重试。本轮报告更新后不重跑无关full。

## 人工门槛与启用范围

目前真实模型有界试次已运行且三公司均未通过研究门槛；S03真实配对、人工研究量表、用户理解反馈继续 pending。量表需有评审者、模型/方法/材料集版本和具体证据定位：事实与结论关系须2；有方向的正式观点反证质量须2；其他适用项>=1；关键事实错和硬约束违规必须0。NA必须解释，不能掩盖方向结论。模型辅助不能签人工语义通过。可填写[未签署的人工评审模板](probes/ag20-human-review-template.json)，所有未知分数和违规计数初始null，不能默认零分或零违规。

用户反馈逐公司复述：当前立场、主要依据、最可能推翻观点的反证和下一步；采纳率不是验收指标。现无真实Agent结果，不提前向用户索取虚构质量评分；父任务将在具体结果就绪后请求审阅。

工程任务完成与产品阶段通过分别记录。计划第5行/任务可按默认关闭的工程交付推进，但计划“AG20通过后才开始A1b”的产品阶段门槛尚未达到；**本记录不修改该产品条件，不自动开放A1a，不替父任务决定AG21是否仅进行默认关闭的工程开发**。需父任务明确区分，不能将后续工程启动写成产品通过。

## 部署、关闭、兼容和恢复操作

- 新进程必须先在隔离副本验证迁移034，再按正式部署批准执行真正迁移；实际021库本轮没有升级。新API/worker代码依赖新表，不能在021库直接运行新Agent路径；默认off不替代schema迁移。
- 升级前备份SQLite及受控原件、配置/加密钥的可恢复来源，暂停旧worker/scheduler、关闭Agent/background admission；保留所有其他任务及业务数据。生产升级属于后续明确部署操作，本轮不执行。
- head034后先保持Agent/background关闭，验证旧health/股票/候选/研究/设置路径，再恢复旧worker正常任务。关闭Agent只停止新admission/后续Agent执行，不删除历史观点、已完成材料、决定或解释；在途调用仍需保留usage结算。
- 已产生新对象后回滚应恢复备份并配套代码/原件，不能把Alembic downgrade当作保留新Agent数据的无损回滚。临时库的034往返和备份恢复测试只证明其测试范围。
- API/worker SIGKILL测试清理仅自己的PID，不使用killall。单实例SQLite和两个连接的CAS均有证据；未宣称任意多实例生产压测、长期运行或真实供应商故障全部通过。SSE连接上限仍按进程，部署多进程需明确总额度。

## 验证记录

- focused：`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q tests/test_agent_research.py tests/test_agent_execution.py tests/test_agent_conversations.py tests/test_investor_publication.py tests/test_investor_controls.py tests/test_investor_monitoring.py tests/integration/test_agent_process_acceptance.py tests/integration/test_agent_company_trial.py tests/integration/test_api_agent.py tests/integration/test_api_agent_controls.py tests/integration/test_backup_restore.py tests/integration/test_migration_034.py`：**200 passed，62.37s**。新增配对与最终试用脚本专项：**2 passed，2.56s**。
- `ruff check .`通过；`mypy src`：**247 source files**通过；三份浏览器JS语法与 `git diff --check`通过。
- 真实Chromium AG17桌面1440/手机390：选择/草稿/丢ack/重试/断线/410/SSE/poll/XSS/布局通过；AG18桌面/手机：历史原始正文/来源/失败计算/通知CAS/探索/时间线通过；AG19桌面1440/手机390独立配置/控制/记忆/非成交决定通过。
- 单次完整 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'`：**1943 passed、3 skipped、6 deselected，194.37s**。三个skip均为未设置 `ZCLAUDIA_GATEWAY_DIR` 的实际外部Node gateway checkout用例；不计作通过，也不掩盖为网络排除。Agent API/worker/进程故障回归已实际执行。
- 最终原库复核：revision021、size360513536、mtime_ns1791489715347475627、SHA256与准备前一致；`alembic heads`为034（仅查询，不迁移）。

AG21接口交接：UserDecision独立于未来真实组合账本，Task/ToolAction和业务对象保留不可变来源/版本；未来成交只能来自明确事实事件。当前没有券商/成交写操作，不能将accept或defer映射为持仓变化。真实端点连通已验证，真实研究质量/理解、Provider输出遵约和阶段开放未过门槛须持续披露。

试次结束后已将隔离Agent/background开关关闭，并清除隔离设置及变更表中的临时agent_api_key记录；保留Task/动作/模型usage和失败审计。正式库hash/stat再次不变。

## 首轮暴露边界的修复与唯一验证轮

[修复验证记录](probes/ag20-real-repair-2026-10-10.md)、[实际审计](probes/ag20-real-repair-2026-10-10.json)。引用窄类型、有限原生schema修复、安全fieldpath诊断和私有拒绝候选保存已完成；新增native多轮及预算failclosed离线回归通过。不得把FunctionModel结果冒充真实公司语义通过。国电首轮具体字段无法恢复，原因和限制明示。

同一目录 `run --retry-existing` 只容许一份追加结果，parent_task_id关联原Task，原始审计不覆盖；累计每公司8requests/50000tokens、全试用24/150000 lifetime扣减，跨日不重置，未结usage拒绝。原剩余额度分别7/38461、7/38437、7/37153，全21/114051；实际本轮全部首HTTP前被保守schema输入预留拒绝，新增HTTP0/token0，没有继续扩容或额外付费。当前真实修复模型门槛仍pending；首轮3HTTP/35949tokens和money未知保留。

修复 focused104 passed28.30s；最终研究/工具/试用24 passed11.74s；跨日试用与实际进程故障3 passed17.58s；Ruff/mypy247通过。修复后单次必要 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'`：**1946 passed、3 skipped、6 deselected，192.33s**；三skip仍为未提供外部Node gateway checkout。无UI变更，无额外浏览器复跑。

## 预留最小修复与最终真实验证

[精确SDK预留分解](probes/ag20-reserve-diagnostic-2026-10-10.md)及JSON、[最后真实修复验证](probes/ag20-real-final-repair-2026-10-10.md)及JSON。父授权后的最小A+B已实现并离线证实：structured instruction_parts存在时不重复预留消息instructions，None时保留SDK fallback；原生tool.prepare只对齐prepare_company_view既有persisted七维计划前置，提交后同native run自动恢复。其他tools/output schema与所有发布/授权门槛保持。

最终真实repair-2三项可调用，3 HTTP200、新39720tokens；三项仍因progress.question文本中未引用证券代码/数量未完成研究。海康save工具错误字段包装遗失已单独透传修复、离线验证，旧尝试原样保留。Provider已发送max_tokens4096但reported输出4356/4156/4520超过请求边界，如实披露且结算实际usage；累计6HTTP/75669tokens，各公司24790/24613/26266均未突破50000，总未突破150000。余18requests/74331tokens，不再付费。无研究产物/正式观点，公开执行错误不是通过的研究答案；实际材料receipt为空。人工质量/理解、真实模型方法质量、A1a开放仍pending，不请人工硬打分。

最小修复affected108 passed29.51s；其后固定full1950 passed、3 skipped、6 deselected185.48s。最后海康safe字段透传/可执行文本提示专项 **8 passed，4.46s**；最终固定全量 **1951 passed、3 skipped、6 deselected，183.69s**。三个skip仍为外部Node gateway checkout缺失，不计通过。Ruff/mypy247/diff通过；无UI变更，前序浏览器证据保留。源021 stat/hash复核不变，临时隔离key清除，默认关闭与DCF off保持。

补充上下文审计限制：试用prompt及同Task可信assembly均不包含公司名/canonical ticker，只绑定内部listing ID，现工具也没有resolve_identity入口；get_snapshot需要canonical ticker。直接拒绝原因已定位progress.question，但缺身份上下文是独立协议限制，不能全部归模型质量。父确认这是工程bug：assemble仅对grant与binding共同授权listing按主键读取现有Company/Listing，注入当前canonical name/full ticker/exact listing_ref；明确身份元数据不是金融证据/历史核验名称，缺失unavailable不猜。没有新lookup工具/权限扩大，Text question/gaps只写定性内容或公司名，数字/证券代码走正文引用节点。默认ResearchWorker真实native SDK+MockTransport四项验证同名唯一身份、真实SQLite字段、错scope拒绝、历史binding冻结cutoff与观点省略、缺身份不猜；真实模型修复版本尚未再验证，旧失败不改写。

最终身份工程bug修复验证：`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q tests/integration/test_agent_company_acceptance.py tests/test_agent_research.py tests/test_investor_publication.py tests/test_investor_monitoring.py` **122 passed，31.69s**；`.venv/bin/ruff check src tests tools`、`.venv/bin/mypy src`（247 files）及`git diff --check`通过。最后固定`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'` **1955 passed、3 skipped、6 deselected，195.63s**，三个skip仍为外部Node gateway checkout未提供。身份修复后无真实模型调用；真实研究/人工/A1a门槛继续pending。隔离所有ciphertext凭据行及对应secret设置change已清除，Task/Action/ModelRun审计保留；源库021 stat/hash不变，Agent/background默认off。
