# P0/P1 主代理验收记录

后续全面复审、修复和最新验证见[复审记录](review-2026-10-16.md)；本页保留首次实施验收的历史证据。

最新的十四组针对性复审及全量集成补验见[最终复审与补验记录](followup-review-2026-10-16.md)。2026-10-16 追加补验：GPT‑6 Luna 执行完整集成，**689 项通过，零失败、零跳过、零排除**，含此前缺环境的四项网关契约；主代理独立逐项核对 JUnit 与 122 个模块的收集清单。共享工作区另有 `agent.js` 并行改动，主代理保留并补跑相关 Web 集成，**140 项通过**。本轮未发现需修复的业务代码问题，仅更新验收文档；日志收集脚本的非业务错误与最终核验依据在链接记录中单列。专项与全量有重复，不累加计数。未推进 P2、写生产库或启用生产开关。以下首次实施的数字和时间边界保持原样。

日期：2026-10-16（Asia/Shanghai）。用户授权制定计划并以 GPT‑6 Luna 子代理实现，主代理负责集成、复核和验收。执行顺序与文件责任见[执行计划](execution.md)，冻结契约和F01—F08见[P0记录](p0-freeze.md)。本记录覆盖工程实现；不构成生产启用或投资结论。

## 环境与结论

基线提交 `245f95e0bfe17da8407f57b927e5a00a74204315`，迁移由049增加050，仅新增两张表。测试使用项目`.venv`、临时SQLite和合成账户/行情/交易规则，模型凭据为惰性测试值；未迁移生产库、启用生产开关或接入真实采集。浏览器fixture运行真实FastAPI、HTTP admission和ResearchWorker，仅种子数据和交易规则为合成来源。

P0四项和P1十一项已实现；主代理独立重跑后端、浏览器并审阅截图。两个新开关默认false。`UnavailableTradingRules`仍是生产默认值，缺失来源保留unknown。P2类别/个股规则及政策事件评估未实施，`supplemental_binding=not_integrated`、研究及公司对齐为not_assessed；财务pass的整体bucket仍为incomplete。

## 十五项交付

路径均相对仓库根目录，表中省略公共前缀`src/investment_agent/`。

| 任务 | 交付与主要文件 | 验收 |
| --- | --- | --- |
| AF-P0-01 接缝与兼容基线 | p0-freeze基线节，旧算术、数值解析、候选生命周期、worker/SQLite边界 | 已冻结；受影响旧功能及全量离线回归通过 |
| AF-P0-02 契约 | p0-freeze契约节；状态、双时间、用途/估值绑定、费用、当前身份、容量与TTL | DTO及错误路径测试通过；P1不产生ready |
| AF-P0-03 独立样本 | p0-freeze F01—F08，`tests/fixtures/account_fit_v1/`、`tests/support/account_fit*.py` | 独立Decimal预期与真实HTTP/worker对应；F07只冻结P2语义 |
| AF-P0-04 数据就绪/G0 | p0-freeze就绪矩阵和资源测量 | 生产规则缺口明确，合成规则未进入生产默认路径 |
| AF-P1-01 DTO/引用/配置 | `portfolio/suitability_contracts.py`、`candidates/ranking_contracts.py`、`investor/common.py`、config/settings registry | 两新引用、严格绑定、Decimal界限、两个默认关闭开关及DB覆盖通过 |
| AF-P1-02 存储/迁移 | `storage/models/investment_assessment.py`、同名repository、`alembic/versions/050_investment_assessment.py` | fresh、049升级/降级往返、两表及foreign_keys=OFF直SQL保护通过 |
| AF-P1-03 输入冻结 | `portfolio/suitability_inputs.py` | 只读精确输入、owner/account、缺失/partial、价格与评估双时点、CSRC原行来源通过 |
| AF-P1-04 算术 | `portfolio/suitability_calculations.py` | 复用旧纯算术；hold、费用、缺失值、基线/情景区分和无算术分支通过 |
| AF-P1-05 服务/当前有效性 | `portfolio/suitability.py` | 不可变保存、同操作稳定UUID复用、动态current_limits、严格数值解析通过 |
| AF-P1-06 候选来源 | `candidates/ranking_sources.py` | hit+truncated、手动open、观察池/冷却/到期、原行快照、缺批次和显式cap通过 |
| AF-P1-07 排序/分页 | `candidates/ranking.py` | 全集去重、bucket后模板轮转、稳定cursor、财务过滤、未知动作占位通过；不合并strength评分 |
| AF-P1-08 admission/worker | `portfolio/suitability_jobs.py`、`candidates/ranking_jobs.py`、`research/worker.py` | 原子收据/冻结、幂等恢复、稳定制品、租约事务、运行时关停、显式终态通过 |
| AF-P1-09 HTTP/context | `api/routes/investment_assessments.py`、同名schema、app/deps | 真实认证/CSRF、只读context、静态路由、422/409/403、授权run及历史回放通过 |
| AF-P1-10 Web | `web/static/account_fit.js`、`candidate_ranking.js`、`pages/account_fit.css`、stock/candidates模板及app/settings | 单股与账户排序、用途/假设、旧结果失效、恢复和请求竞争通过；保留旧页面流程 |
| AF-P1-11 集成交接/G4 | 本记录、probes截图、真实API/worker及资源专项 | 全量离线、浏览器、静态和迁移门禁见下文 |

## PC01—PC12 证据

| 编号 | 实测行为 | 主要测试 |
| --- | --- | --- |
| PC01 开关 | Settings/RuntimeSettings/SPECS一致；任务接收后DB关停令worker拒绝写入；旧收据/结果仍可读 | `test_settings_registry.py`、`test_settings_store.py`、`test_account_fit_jobs.py::test_runtime_database_disable_after_acceptance_fails_worker` |
| PC02 不可变完整存储 | 关闭FK仍拒绝孤儿、错owner/account/kind/revision、越界ordinal和SQL UPDATE/DELETE/REPLACE；零条目合法，UUID重放与集合count/hash校验 | `test_migration_050.py`、`test_investment_assessment_store.py` |
| PC03 租约原子性 | 另一会话提交新worker/attempt后旧handler不能提交制品；结果flush后丢租约也回滚；success/partial显式写终态 | `test_account_fit_final_boundaries.py`、`test_account_fit_jobs.py`；排序HTTP终态断言 |
| PC04 admission顺序 | writer reservation先于账户读取；两独立会话并发同键仅一收据/run；预览后head推进得到409且不收新任务 | `test_api_account_fit.py` 两个pc04用例 |
| PC05 缺失/失效 | 无snapshot拒收；缺/partial估值保持unknown；同snapshot首次新增估值令旧missing结果stale且payload不变；未知持仓动作不伪造open | inputs/calculations、`test_account_fit_final_boundaries.py`、`test_candidate_ranking_jobs.py` |
| PC06 价费身份 | 不同价格/费用basis隔离；批量假设逐股冻结；空费用不是免费；hold无交易假设；只有专属resolver可转换比例为百分数 | contracts/calculations/inputs、`test_api_candidate_rankings.py`、浏览器同账户改费用再生成 |
| PC07 执行边界 | 空排序、全unknown正常success；受控单项错误partial；SQLite锁保留同run重试后success；IO故障走失败而非partial | 两组jobs、`test_account_fit_final_boundaries.py`、真实浏览器失败后显式重建 |
| PC08 浏览器恢复/竞争 | sessionStorage失败零POST；双击一次POST；服务端已接收但响应abort后原body/UUID返回同run；202后未执行即reload仍跟踪同run；账户迟到context和分页迟到响应不能覆盖新选择；轮询保留表单、hidden恢复、完成后新key重建 | `tests/browser/account_fit.mjs`，1440与390两视口均通过 |
| PC09 算术/发布边界 | 未知数量不构造ScenarioAction；完全无有效算术不保存OK；基线和情景违规分离；cash_upper_bound不显示为确切现金；已知blocked数字可展示但不授权正式发布 | `test_account_fit_calculations.py`、`test_account_fit_no_arithmetic.py`及旧publication/plans回归 |
| PC10 候选生命周期 | hit/truncated、逻辑到期、冷却中手动open优先、冷却后旧批次不复活/新批次重新入选、观察池优先排除、业务排除与cap区分；读取不提名、不建草稿 | `test_candidate_ranking_sources.py`、`test_api_candidate_rankings.py` |
| PC11 历史回放 | 只读原本功能run中的冻结manifest；错owner/hash/时点/缺run为422；后出现交易规则不补齐旧缺失；回放不进入live current | `test_api_account_fit.py::test_pc11_historical_worker_does_not_enter_live_current_even_if_rule_appears` |
| PC12 资源 | 101项fit、501持仓拒收；2001来源按真实cap形成2000已处理+1显式未处理；4MiB不能吞成完整；共享账户来源只在接收批量读取 | API/source容量用例、`tests/support/measure_account_fit_pc12.py`，测量见下文 |

## 最终执行结果

全量离线测试按三个互不遗漏的目录分区执行，各自使用独立临时库，共3724项通过、6项跳过、6项network排除。随后排序GET解析器修复新增一项HTTP回归，并对受影响路径独立复测76项全部通过；未将重复执行的测试累加为全量计数。共同环境变量为`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3`，每条命令另设置临时`DATABASE_URL`和`DATA_DIR`。

| 命令 | 最终结果 |
| --- | --- |
| `.venv/bin/pytest -q tests/unit` | **2334 passed，2 skipped，6 deselected**，278.71秒；skip为两份未随仓库分发的原始资料，deselect为network |
| `.venv/bin/pytest -q tests/integration` | **673 passed，4 skipped**，644.19秒；skip均要求独立构建的ZCLAUDIA_GATEWAY_DIR |
| `.venv/bin/pytest -q tests/test_*.py` | **717 passed**，213.55秒 |
| 最后修复后受影响专项 | **76 passed**，20.43秒；ranking jobs/sources、两组账户及排序HTTP、Web模块契约 |
| `tests/browser/run.sh account_fit.mjs` | 主代理独立复跑通过，真实1440/390两视口；105成员排序、实际第二页与过滤竞争等PC08行为 |
| `.venv/bin/ruff check .` | 通过 |
| `.venv/bin/mypy src` | 通过，401个源码文件 |
| `git diff --check`、Alembic ScriptDirectory单head | 通过；head050，down_revision049 |

项目默认排除network标记，未把网络测试或跳过项计为通过。新增JS模块由既有Web client contract真实解析导入，另执行`node --check`；Web专项49项通过。截图和可重复浏览器说明见[probes](../../probes/account-fit-p1/README.md)。主代理看过四张截图；移动端原有固定操作栏占据部分底部视口，内容可继续滚动。

首次全量发现过设置目录漏注册、旧迁移测试标签049及包依赖门禁问题，均修复；缺惰性API凭据的早期试跑不计最终结果。首次单元分区为2331通过、2失败，修复子集31通过后完整重跑为2334通过；未通过改期望掩盖业务失败。最终截图复核另发现排序GET丢失交易规则解析器注入、令刚生成结果误标stale；补齐与POST一致的依赖并增加真实HTTP即时有效性断言；UI将不可变头的状态明确标为“生成时证据”，与实时current_limits分开。该修复后的76项受影响测试、两视口浏览器与静态检查均重新通过。抽取共同请求契约消除portfolio到candidates反向依赖；其余7条导入边经主代理审阅后逐条更新基线，理由见执行计划。旧设置启动测试隔离目录provider，避免离线验收启动真实后台采集。

## 数值与资源测量

F01：`q=100`、估值价及情景执行价1500、固定费用10。小账户情景仓位为`150000/199990 ≈ 75.00375%`，超过10%约束，financial/overall均blocked；大账户为`150000/1999990 ≈ 7.5000375%`，financial pass而overall incomplete。费用从现金/NAV扣除，不能把150010当证券市值。这些精确数值由独立算术用例验证。浏览器小账户NAV为20万，大账户为200万现金另加10万已有持仓（NAV210万），验证相同的blocked/pass方向并兼测hold；不把其截图当作NAV200万的精确数值证据。不同估值价与执行价、未知费用和现金保留另有用例。

100项真实admission+ResearchWorker批次测量：

| 指标 | 本机合成实测 |
| --- | --- |
| 接收SQL总数/账户来源SELECT | 20 / 9 |
| worker SQL总数/账户来源SELECT | 1508 / 0 |
| worker耗时/终态 | 1.5695秒 / success |
| 2001条真实screen源冻结 | 2000处理、1未处理、coverage=partial |
| 上述manifest canonical bytes/冻结耗时 | 2,543,082 bytes / 0.2968秒 |

100项测量可用`PYTHONPATH=. .venv/bin/python tests/support/measure_account_fit_pc12.py`重现。2001项使用`test_api_account_fit.py::test_pc12_2001_source_members_are_partial_at_the_real_cap`同一实际resolver链路，计时覆盖冻结读取和序列化。记录只说明当前本机样本；写入次数随单项计算/制品增加，并非恒定SQL承诺，生产大集合延迟尚未实测。

## 交接与限制

- P1新请求使用`operation_id`作为唯一幂等UUID；历史回放需要原run_ref、manifest_hash与input_as_of。单股GET返回payload，排序GET返回snapshot/entries，动态current_limits独立于不可变结果；设计文档已同步。
- 当前旧模型只有owner级单一MandateHead；一次仅能把`portfolio_constraints.account_id`绑定到一个账户。其他账户返回约束unknown，不复活旧约定。浏览器F01通过既有真实约定修订接口逐一切换账户绑定。
- 数据依赖仍包括可信交易规则、有效本地日历、价格/CSRC、账户快照/估值及明确费用假设。缺失值和不完整覆盖不会转成零或通过；warning暂为保留状态。
- 首次运行需按既有部署流程升级至050，再由既有设置入口按需启用；本轮没有执行生产迁移/配置。关闭新入口是优先技术回退方式，已保存历史仍可读；不自动降级删除审计制品。
- 此阶段没有交易、真实资金预留、自动研究、通知或个股政策事实核验。阳光电源禁令之类事项不能据本交付宣称已确认风险或已影响评分。
- 验收期间出现的`mobile/src/transport.ts`、`transport.test.ts`、`ui/PairForm.tsx`改动不属于本任务，未触碰或验收；原有`.playwright-mcp/`、`.zcode/`保留。未自动提交Git。
