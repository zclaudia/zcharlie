# AG29 多资金用途的方案比较

2026-10-11。工程交付默认关闭；AG20 真实研究质量/A1b 产品开放仍 pending，真实记录仍 6 HTTP / 75,669 tokens / 0 有效研究；FCFE 0/4，生产可信交易规则 unavailable。本卡没有真实迁移、设置、模型、付费调用、采集或外部通知。

## 用户确认与真实现金

资金用途是同一精确账户现金上的规划分配，不是现金转账、真实账本事件、券商预留或第二个资金池。每项保留名称、金额、可用方案预算、短/长期、必留现金、需求日期、来源/来源版本和未知项；最多十二项。可用预算与必留现金不能超过该项金额，分配合计不能超过该账户 snapshot.known_cash；未分配现金明确对账。部分资产覆盖但现金已知时可以分配，未知现金、cash_subtotal 或部分 NAV 不能授权用途金额，不跨账户抵扣。

需求日期用于表明用户资金时间要求，不会自动释放现金；日期已过仍保护必留现金，只有用户修订能够改变。长期用途也有自己的现金要求和预算，不能因长期公司观点有利而侵占短期保留。未知项使用途需复核，方案数量检查不能将其忽略。金额使用字符串和 Decimal，128 位明确精度，无浏览器现金浮点计算。

认证 POST /portfolio/purposes 只接受用户确认；完整替换为新的不可变版本，expected_revision 与 exact current snapshot 双 CAS；SQLite 在短事务先取得 writer reservation。请求 UUID 与完整 body 留作收据，同一 body 重放先于 CAS/关闭检查，改 body 返回409。允许显式确认空版本撤销全部规划分配；旧版本与实际账本保留。新写入默认由已有 portfolio_ledger_enabled=false 关闭；没有额外权限框架，也没有模型确认/修改用途工具。模型可以提出文字草案，用户在用途表单明确确认，不能将未知资金显示为已有现金覆盖的生效用途。

GET /portfolio/purposes/current 验证 owner/exact account，返回当前用途和实际现金之间的投影。发生真实转出或现金下降时照常记录实际事实，旧用途不改；现金不足显示 overallocated、未知/快照变化显示 needs_review。快照变化的 needs_review 表示现金基础需要重新比较，不强制用户重复确认不变的用途约束；用户主动发起的新研究可用当前实际现金和原用途完成确定性检查，检查成功才形成新的整案。名称/来源文字改动不触发公司研究，也不让语义不变的已发布整案失效。

## 同一资金基础的整案与原生执行

沿用 AG22 同一现金、费用、价格、交易规则与 no_action 基线，以及 AG23 整案互斥选项。所有交易关联已有 purpose_id，计算绑定精确 purpose_ref；费用纳入用途支出，同一选项内累计，同一选项之外独立重置，卖出不扩大用途预算或创造新的资金池；卖出费用超过所得时，净现金流出也消耗关联用途预算。交易关联只是现金支出约束，不证明用途拥有证券。空用途版本撤销用途限制但仍绑定当前版本。未知费用/数量使用途支出与剩余预算为未知，不写成0。

方案检查同时验证账户现金、用途可用预算、全部用途必留现金和分配总额；实际已不足的现金基础不通过。机会成本显示为方案后现金、用途剩余预算和必留现金缺口，没有假设投资利润/最优回报。稳定 Quantity/result_paths 支持用途 reserve、spent、remaining_budget、shortfall 精确数字引用，带 exact purpose version/id 与场景诊断；计算 fact_refs、input_hash、原始 confirmed_at、source/version 可追溯，服务器重建时验证。

get_portfolio_context → calculate_portfolio_options → prepare_portfolio_plan → 用户来源的授权队列/ResearchPolicy/FunctionModel/worker → 整案发布已接通。新计算 API/原生工具必须绑定该账户当前用途；旧计算的不可变重放/历史重建继续可读。模型不能写 ledger、放宽用途/约定、批准用途或预留真实资金。接受方案继续只写 UserDecision，实际账本完全独立。

用途确认的知识时间与金融价格/现金快照估值时间分开审计，不能把新确认倒填成旧知识。原生工具检查用途 confirmed_at 不晚于已授权 Task.binding.as_of；用途在任务开始后变化或明确历史研究窗口早于确认时，任务受限并要求用户主动新消息。发布再次在 claim 事务检查 trusted task cutoff，并验证不晚于 publication as_of；不会通过移动 frozen valuation_at 或任意推进知识时点容纳新约束。

用途金额、预算、保留、日期、期限或未知项的相关变化立即使依赖整案 review_required；用途前的旧案也不能保持定量有效。公司观点不因此失效。实际现金变化沿用 AG23 当前快照限制与 AG15/23 持久 outbox/coalescing；没有自动付费重试或新 Task 重置预算。生产交易规则仍 unavailable，方向性数量只在隔离浏览器 fixture 注入明确 FixtureRules；不是交易发布验证。

## 页面与可靠性

AG24 原有组合卡增加“资金用途规划”，仅按按钮展开小型用途表单；摘要展示分配、未分配、必留现金、日期/来源和需复核状态。整案选项展示同一现金与用途预算比较。新增用途表单/摘要不以 UUID 或 raw JSON 代替业务文字；技术审计仍折叠。手机单列、桌面可读，无横向溢出。

复用已验证 pending full body/key/sessionStorage 请求流程：发送前保存，失败留同一 UUID/body，reload 后重取原收据，pending 期间锁定账户；409 不自动重写或重发，读取当前现金/用途供重新审阅。真实浏览器覆盖丢回执后关闭入口重放、reload、账户锁定、用途与现金并发409；未知金额不能填成0。接受提案不会创建实际现金事件。

## 验证与 AG30 交接

测试证据、最终全量结果见本记录后续追加。迁移039只增加 purpose head/version 两个对象，临时 fresh 和038→039→038→039保留既有原件，固定历史038不改；仅 current head assertions 更新。新模型位于既有 portfolio_plan metadata import 内，OS/browser fixture 无遗漏。真实 .dev/data/app.db 仍021 / 360513536 bytes / mtime1791489715。

AG30 继续方法版本与显式用户批准，不把用途期限、用户采纳或通知 acknowledgement 作为方法有效性。保留 AG28 managed scheduler 的边界、原 Task lifetime/day/purpose budgets、现有 grant 与默认关闭；不要把本卡隔离 FunctionModel/浏览器演示称为真实研究质量通过。本 agent 完成 AG29 后停在父任务 review，不启动 AG30。

### 冻结前验证记录

- `tests/test_portfolio_purposes.py` 覆盖共享现金/费用、互斥用途支出、短期保留、金额精度边界、未知现金/partial 已知现金、账户隔离、精确版本与独立 SQLite writers、重放先于关闭/CAS、空版本释放、实际转出仍记录、未知费用保持未知、卖出费用超过所得的净支出、新用途对旧案即时限制/outbox合并、模型不能确认用途、实际用户消息→授权→队列→ResearchPolicy→FunctionModel→计算/整案发布，以及确认晚于任务/明确历史任务不能读新用途。旧 portfolio 三组回归63 passed / 12.85s；扩展集中78 passed /36.56s；头断言集中65 passed /22.47s；最终用途/计算/整案集中63 passed /13.00s。迁移 fresh/038 roundtrip 专项通过。
- `tests/browser/agent_purposes.mjs` 独立端口19859、临时库、真正 HTTP 与 native worker，1440/390 都完成两个用途分配、必留现金比较、丢回执后默认关闭重放同一 UUID/body、reload/account lock、接受不行动不写实际事件、名称改动保持整案有效、保留/预算变化立即需复核、真实并发409重新审阅、真实转出导致 overallocated、XSS/无横向溢出。方向性 FixtureRules 仅在隔离 OS fixture 注入，无生产规则供应声明；浏览器没有 API 成功替身，没有网络/付费模型调用。最终截图 `probes/ag29-ui/` 包含表单、1440三列/390单列的可读 viewport 比较及现金下降摘要，已用 view_image 检查。
- 初轮 API 专项失败分别为测试 create_app 参数错误（1 failed/5 passed）和缺 env bootstrap auth key（1 failed/6 passed），修正 fixture 后7 passed。初轮浏览器的 exact label selector、消息提交与 worker claim 时序各一次 timeout；改为实际可见 label，并等服务器接收消息后 claim，最终两视口通过。初轮 outbox fixture 误忽略既有 published 事件（1 failed/10 passed），只修正 expected count 保留一次 review/coalescing；后续62、最终63 focused全通过。早期 mypy 的模型循环变量类型、state Literal、导入及 Optional Decimal narrowing 已修正，不将修前失败计作成功。
- 最终 Ruff、mypy271、两处 JS syntax、git diff --check、Alembic single head039通过。既有 AG24 完整浏览器流程另在独立端口19860重新通过1440/390，事实/决定/丢回执/旧案受限与显式替代保留；固定全量离线结果待本轮结束追加；三项外部 gateway skip 不会计作通过。真实库 size/mtime仍360513536/1791489715；没有 commit/reset，所有既有 dirty/gateway 改动保留。

首次全量离线实际结果 **2180 passed、1 failed、3 skipped、6 deselected，264.30s，exit1**。唯一失败是 migration034 未纳入扫描的单引号 current-head038 断言；更新为039，保持固定历史038不变，报告保留在 `probes/ag29-full-first.txt`。生产复核同时发现新增可选字段与无用途时新增 hash key 会破坏旧 AG22 请求/计算的精确重放和重建：现只有新 purpose_ref/purpose_id 为 absent 时不序列化，保留原 no-purpose hash 字段形状；有用途时完整纳入 exact purpose inputs。既有请求字段、旧行、数字与输入等价检查未放宽。新隔离 fixture 插入真正缺少这些字段、使用原始 hash 形状的历史计算，验证 replay、native business 序列化/指纹输入、rebuild input_hash/results/details 和 plan verification。初次 fixture 误尝试 ORM 更新 immutable 行（1 failed/64 passed），改为插入独立历史标识保留不可变守卫，最终集中 **65 passed /13.63s**。Ruff/mypy271和两视口实际浏览器重新通过；最终固定 full 已重新开始，结果稍后追加。

最终固定 `API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m 'not network'`：**2182 passed、3 skipped、6 deselected，263.77s，exit0**。三 skip 仍是未配置外部 `ZCLAUDIA_GATEWAY_DIR`，不计作通过。最终日志 `probes/ag29-full-fixed.txt`。所有测试/浏览器 fixture 子进程已结束，Ruff/mypy271、JS syntax、diff、single head039与1440/390真实交互通过；真实数据库 stat 保持360513536 bytes / mtime1791489715，没有真实迁移或设置/模型/采集/通知调用。工程完成，产品质量与可信交易规则门槛仍未开放；交付父任务后停止。
