# AG24 / A1b 组合交互工程验收

2026-10-10。本记录区分默认关闭的工程交付与产品开放；不是人工质量签署或交易权限。

## 当前状态

组合 UI 工程闭环已实现；A1b 产品开放仍 pending。AG20 的真实研究累计 6 HTTP / 75669 tokens，零合格公司研究产物，真实研究质量、人工复述及评审门槛尚未通过。FCFE 仍 0/4；生产可信交易规则解析器仍 unavailable。不能把隔离 FunctionModel 演示或确定性算术测试当成真实研究/可信交易规则验证。`portfolio_ledger_enabled` 默认 false，必须在设置中明确启用；本卡仅临时测试配置开启，不改真实设置或数据库。

## 可操作的闭环

Agent 页面保留公司提问、草稿、对象查找和操作面板；组合以独立全宽卡片展示。账户、现金、总资产、持仓、覆盖和限制为摘要；开户、实际事实、对账、冲销和数值约束按明确按钮一次展开一个表单。桌面整案选项并排，手机单列；无需滚过账本表单才能提问。

1. 明确账户名称；起点明确资产范围和完整/部分覆盖，现金空白为未知，持仓必须查找并选择 canonical name + full ticker，然后填写确认数量。事实时间包含日期、时间与时区，来源和“已经发生”确认不可省略。
2. 明确绑定当前不可变组合版本到下一条消息，主动选择 `portfolio_action` 并发送。读取、切换账户或刷新不会调用模型。新绑定清理旧组合/整案引用；持仓之外的新证券仍需查找结果中明确选择。已保存的 pending 消息不静默改绑。
3. 同一现金/价格基础下呈现整案互斥选项与不行动。现金/总资产/方案后持仓/单股与行业占比、交易数量分开；服务端 Decimal 序列化结果按字符串展示，浏览器不计算现金/NAV。占比仅移动十进制字符位置，支持科学计数法/负数；超过展示位数以省略号提示，精确值在版本审计中。费用、执行价格、假设性质与来源有普通文本展示，完整 calculation/source/version 位于折叠审计。
4. 接受、暂缓和独立决定只写 `UserDecision`，绑定 exact plan/option，接受不预留现金、不提交订单。已发布的不行动/等待可接受，即使原方向性数量 `executable=false`。原 flag 不变；当前 `PlanView.status` 及 limitation reasons 限制接受；服务器最终再做版本检查。刷新组合记录会撤下旧显示并要求重新查看当前整案。
5. 实际买卖、资金转入转出和分红为单独的确认事实。成交数量是本次交易数量；没有从方案预填数量、价格或费用。费用空白是未知，显式 0 是确认免费。对账保留差异解释；冲销引用原 event ID，原事实与快照保留。历史采用每页100条的显式加载更多，不截断后续冲销入口。
6. 实际事件改变组合版本后旧案保留并受限；用户可明确重新研究。未知费用/覆盖导致受限检查；修正后可显式替代整案并沿 previous_ref 回看旧案。页面不编造收益，不写实际订单。

实际事实时间采用datetime-local和可见北京时间UTC+08:00/UTC+00:00选择，提交前展示将保存的日期时间与时区；不要求用户理解ISO格式，也不从方案推断发生时间。

数值约束通过 authenticated `mandate_revision` 与 expected_revision/CAS 修订，明确绑定一个账户；其他字段/自由文字/授权范围和预算保留，服务端添加授权 instruction 来源。切换为另一账户约束会显式说明改绑。未有 mandate 时使用现有操作面板先建立明确研究约定。

## 可靠性与边界

账本/决定/约束请求在发送前把 UUID、exact body、path、method 保存到 sessionStorage；存储失败不发送。待确认期间输入和写按钮锁定；提交有 busy guard，异步查找/账户/方案/失败读取有 epoch guard。网络或5xx后刷新重取原请求；401/403保留同一 pending（权限错误不能证明原请求未提交）。明确422/409拒绝后解除 pending；409读取当前组合并展示原请求供重新审阅，不自动重写/重发。成功新账户使用服务器返回 ID 选择对应账户。

`GET /portfolio/artifacts/{draft|publication_check|review}/{id}?account_id=...` 是本卡的最小读取接缝：验证认证、owner、exact account、kind、revision；只返回专属 public artifact payload，不暴露准备阶段或系统私有 prompt。UI 展示具体发布字段错误，并按 account/ref 去重，账户/epoch改变不会串入旧检查。

## 实际验证证据

- `tests/browser/agent_portfolio.mjs` + `portfolio_fixture_server.py`：临时目录、独立端口19853、真实HTTP、file SQLite、原生 FunctionModel → 工具 → ResearchWorker → 组合发布链路。创建元数据前导入全部 portfolio 模型。两个视口1440/390分别完成起点、同资金的两项方向性减仓与不行动、accept/defer/independent、未知费实际成交、丢回执、暂时真实403、恢复权限/reload/同UUID重取、旧案受限、显式重算失败字段、冲销、第二会话修正后新整案/previous_ref历史、partial/未知现金、真实并发CAS409、关闭入口、XSS与零横向溢出。首个发布5次离线 FunctionModel 调用；含失败重算/修正后复核的最终演示总15次，每次本地 usage=20 input/10 output，无网络/付费调用。
- 真实生产默认 TradingRuleResolver 不可用。浏览器方向性演示只在隔离OS fixture进程注入明确 FixtureRules resolver，真实native工具与completion均使用同一服务器重建接缝：两项减仓分别10/20股、费用1/2元，方案后现金1099/1198、总资产1999/1998、最终持仓90/80；接受后真实账本仍100股/1000现金。fixture规则来源/版本留在calculation，不伪造生产供应。互斥现金、两SQLite会话CAS及精确目标数量也由 `tests/test_portfolio_plans.py` / calculations / ledger 既有回归覆盖。
- 新增 `test_native_portfolio_answer_without_published_plan_remains_partial` 验证只有progress prose而无已发布整案不能succeeded；失败发布的真实native browser任务也assert partial/evidence_blocked。中间progress改为“模型输出已保存，正在核验研究与发布条件”，不把completion前状态留为“未认证草稿”终态错觉。
- 新增 `test_public_portfolio_failure_artifacts_exact_owner_account_kind`：失败draft/check的真实HTTP认证、owner/account/kind边界、默认关闭可读和公开字段；未开放 preparation。
- 首批受影响专项65 passed / 11.68s；增加reader专项后 portfolio plans 30 passed / 7.48s。最终research/execution/portfolio专项85 passed / 26.37s，legacy guard最小范围适配专项23 passed / 5.25s。`portfolio_format.mjs`覆盖1E-18、-0.1、负零、重复小数和有界超大指数。AG17/18/19 原有桌面/手机真实浏览器脚本顺序通过；最终full结束后又串行补验AG18 agent_views（sources/history/failed calc/notification CAS/discovery/timeline）与AG19 controls，1440/390均exit0，普通公司Agent与控制流程保持。
- 固定版本全量离线 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'`：2021 passed、3 skipped、6 deselected，298.14s。3skip为未配置外部ZCLAUDIA_GATEWAY_DIR，不能计通过。修前中断全量真实384 passed、1 failed、3 skipped、6 deselected、129.38s/exit2；唯一legacy禁买卖用语guard限定豁免新的实际事实模块，保留其他legacy页规则，专项23 passed。Ruff、mypy（258 files）、JS语法、diff与Alembic head036检查通过。

首轮浏览器修复记录：fixture未把写开关放入运行时env而被默认关闭；旧成功notice让等待条件提前满足，现打开表单清空notice且写入立即标记等待；mandate revision会由服务端增加instruction授权来源，验证仅允许此审计增长；409刷新覆盖错误notice，现刷新后明确再次展示冲突。扩展第三次研究曾因同一长会话预算边界停止而无法发布；方向性场景的完整工具返回也在100k预留上限停止；最终fixture从启动时采用300k/task、1.5m/owner/day，保留累计生命周期预算且不在任务中重置，生产预算不变；保留原预算停止行为，最终演示用用户主动新建第二会话和最新绑定，不提高生产预算。成功API没有替身，断网仅用于丢回执注入，403来自真实开关关闭。

截图在 `probes/ag24-ui/`，包含两视口的账户摘要、方向性选项、实际成交表单、实际事实后受限状态、关闭入口（390×844 viewport截图可直接读字）；已使用 view_image 检查桌面三列和手机单列，无溢出。

## 启用、关闭与恢复

工程演示只在独立临时库启用真实账本和Agent；生产启用必须先满足AG20质量/A1b阶段条件并由用户明确设置。关闭账本不会删除历史；已录事实仍可读取。恢复时先重取未确认 UUID/body 的收据，再刷新账户/组合并查看受限原因；不得把未知输入改0、把旧方案目标当成交事实或自动继续付费研究。跨会话共享现金仍由一个 current plan head 和整案CAS控制。

真实 `.dev/data/app.db` 保持 Alembic021、size360513536、mtime1791489715；没有真实迁移、设置/模型/网络调用或 commit/reset。现有dirty/gateway改动保留。工程交付完成后停在父任务review，不启动AG25。
