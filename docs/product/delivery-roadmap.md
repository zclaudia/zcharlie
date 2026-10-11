# 个人投资 Agent 开工与交付路线

> 下一阶段规划（2026-10-09）已按用户确认的自主投资研究方向展开，见
> [产品设计](investor-agent-design.md)和[技术设计与交付计划](investor-agent-architecture.md)。
> 完整行为与契约见[研究与运营设计](investor-agent-research-policy.md)、[领域契约与交互流程](investor-agent-contracts.md)和[估值计算设计](investor-agent-valuation.md)。
> 新方案按前置验证、A0 研究契约与执行基础、A1a 公司观点闭环、A1b 组合与仓位建议、A2 持续自主研究、A3 组合与方法改进推进。
> 可执行拆分见[实现任务清单](investor-agent-implementation-plan.md)：AG01 至 AG20 交付公司观点闭环，AG21 至 AG30 分阶段扩展组合、取证与方法改进。
> 2026-10-10 已确认 Python + Pydantic AI 为 Agent 核心，复用现有 FastAPI、SQLite、worker 与 Web，
> 模型独立配置并保留供应商切换；前置验证只覆盖框架内输出模式、错误边界与估值数据覆盖，故障恢复与等待/取消归 A0 任务基础验收。
> 用户决定记录在 A1a 交付，真实账本在 A1b；前置结果只约束对应集成，不阻塞独立业务契约和样本设计。
> 具体集成方案仍为 Draft，尚未实施；下文保留既有交付记录与此前路线，不代表新功能已经完成。

本路线以一名主要开发者配合编码 Agent 为估算单位。时间是容量规划，不是发布日期承诺；每个阶段以退出条件而不是日历宣告完成。

> 当前进度快照与逐任务收口计划见
> [`mvp-completion-plan.md`](mvp-completion-plan.md)。截至 2026-10-06，
> Sprint 1～4 全部完成；MVP 关键路径（MVP-103、MVP-401～405）已收口，
> 逐项签字式验收见 [`mvp-release-checklist.md`](mvp-release-checklist.md)。

## 总体里程碑

| 阶段 | 参考周期 | 用户结果 | 退出门槛 |
|---|---:|---|---|
| M0 工程基线 | 1～2 天 | 技术与产品边界明确 | 基线、契约、首个 API、CI 门禁 |
| MVP 个人研究闭环 | 3～4 周 | 能持续研究自选 A 股并看变化 | 满足 MVP Definition of Done |
| ~~V1 长期可用~~ | — | 已被漏斗设计 P1～P4 替代 | 见 [`funnel-design.md`](funnel-design.md) |
| ~~V2 组合视角~~ | — | 已被漏斗设计 P1～P4 替代 | 见 [`funnel-design.md`](funnel-design.md) |
| V3 最终形态 | 持续演进 | 个人投资研究操作系统 | 多 Agent 有评测、有权限、有审计 |

## M0：工程基线

### 已完成

- 验证 `ifind-proxy → canonical → my-stocks → Agent → persistence` 的真实链路。
- 建立严格 canonical Pydantic 契约。
- 将分类、评分、数据质量和报告上下文迁入仓库内 `DeterministicResearchKernel`。
- 用真实国电南瑞输入验证新旧内核 score 与 report context 逐字段等价。
- 从 CLI、API 和配置中移除外部 Skill 路径及 subprocess 依赖。
- 提供 `POST /research/single-stock` 的持久化任务入口与独立 worker。
- 保留 CLI 入口和完整审计制品。

### 本阶段剩余

- 固定首批 10～20 只股票的 golden corpus。
- 为 iFinD 响应建立脱敏 fixture 和契约回归测试。
- 将本文档中的门禁接入 CI。
- 记录三项产品选择：是否严格单用户、首版是否只做 Web、是否把非 A 股功能冻结为 legacy。

## MVP：个人研究闭环

### Sprint 1：内核归仓与数据契约（第 1 周）

目标：`zcharlie` 能独立完成确定性评分。

任务：

- `[done] MVP-101` 迁移策略分类、评分模型、质量/freshness 逻辑。
- `[done] MVP-102` 迁移报告上下文生成，保持 score schema v2.0。
- `[done] MVP-103` 13 只不同特征 A 股的 golden corpus、脱敏 provider fixture 与参数化等价测试（含北交所阻塞记录）。
- `[done] MVP-104` 完成 iFinD endpoint → canonical 字段注册表、映射文档和真实 fixture。
- `[done] MVP-105` 调查股息率；现有网关无可靠字段，按契约保持 `null + needs_review`，等待网关增加正式分红接口。
- `[done] MVP-106` 从运行配置中移除外部 Skill 路径。

验收：离线 fixture 可以在全新 checkout 中完成评分，无用户目录依赖；同版本输入输出与旧内核等价。

### Sprint 2：持久化运行系统（第 2 周）

目标：研究任务可查询、可恢复、可重试。

任务：

- `[done] MVP-201` 已复用 `pipeline_run` 并建立 `research_snapshot`、artifact manifest、队列生命周期字段和按 run 查询 API。
- `[done] MVP-202` 已将数据、评分、解释、持久化拆成明确阶段，并记录 attempt 与 heartbeat。
- `[done] MVP-203` 已实现数据库队列、原子认领、独立 worker、孤儿任务恢复和有限重试。
- `[done] MVP-204` 已在 API 与 CLI 接入幂等键；API 返回已有任务状态并拒绝同键异参，数据库唯一约束阻止重复运行。
- `[done] MVP-205` 评分成功、解释失败时持久化确定性快照与制品，并以 `partial` 状态暴露复核原因。
- `[done] MVP-206` 已提供研究 run 列表、统一详情、结果与受控 artifact 下载 API。

验收：在任意阶段终止进程后，重启能识别未完成任务并安全重试；同一幂等请求不产生重复结果。

### Sprint 3：观察池与变化跟踪（第 3 周）

目标：从一次性报告变为持续研究。

任务：

- `[done] MVP-301` 已支持沪深北证券代码映射、A 股 listing 自动建档，以及可持久化的行业、数据源、报告年度和刷新状态元数据。
- `[done] MVP-302` 已支持启用观察池一键批量入队、批次幂等、逐标的校验与失败隔离，并由 worker 独立回写 queued/running/success/partial/failed 状态。
- `[done] MVP-303` 已对稳定的分数、策略分类、关键财务指标和质量状态生成版本化结构化 diff，过滤时间戳等噪声，并在运行列表与详情 API 暴露重要变化。
- `[done] MVP-304` 已添加 Asia/Shanghai 日终刷新任务、基于上交所公告的版本化交易日历、未知年份 fail-closed 保护，以及仅针对瞬时网络/上游错误的指数退避有限重试。
- `[done] MVP-305` 已生成幂等的 Markdown 研究变化通知，仅包含 meaningful diff；baseline、无变化与小幅噪声保持静默，并提供受控下载 API。

验收：刷新列表时单只失败不影响其他股票；用户能看到“上次以来的变化”和原始依据。

### Sprint 4：最小 Web UI 与发布验收（第 4 周）

目标：不依赖命令行完成日常研究。

页面：

- 今日概览：待复核、失败、有变化的标的。
- 观察池：最新评分、置信度、freshness 和变化时间。
- 单股详情：因子、指标、证据、缺失项、历史快照。
- 运行中心：任务阶段、错误、重试和制品。

任务：

- `[done] MVP-401` 同源 Web shell、签名会话与聚合读模型（dashboard/watchlist/history）。
- `[done] MVP-402` 四个最小页面及 loading/error/empty/partial 状态，浏览器截图验收通过。
- `[done] MVP-403` Agent 解释引用点击定位确定性字段、来源与 provenance；失效引用显式可见。
- `[done] MVP-404` 空环境 smoke（迁移 → API → 自选 → 刷新 → worker → 重启）离线自动化。
- `[done] MVP-405` 安全检查、备份恢复 runbook 与演练、pip-audit 扫描、发布清单。

验收：从浏览器添加标的、运行研究、阅读证据和查看变化；所有错误均可理解且不泄漏秘钥。

## V1 / V2（已替代）

> 2026-10-06：V1、V2 已被 [`funnel-design.md`](funnel-design.md) 的 P1～P4 替代，以下内容仅作历史记录。
> 对应关系：持仓与暴露 → P2；回测与校准 → P3；正式披露与证据账本 → P4。

### V1：长期可用的个人研究台（历史）

重点：

- 年报、公告等正式披露的发现、人工确认和证据账本。
- 研究假设、待核问题、结论变化的生命周期。
- 数据源容灾、字段冲突比较和质量趋势。
- 数据库/制品备份、恢复演练和保留策略。
- 成本、延迟、成功率和 token 使用仪表盘。
- 更完整的历史快照、bucket backtest 与 walk-forward 验证。

V1 退出条件：连续四周日常运行无不可恢复数据丢失；正式披露与推断清晰分层；模型或评分升级有回归报告。

### V2：组合视角（历史）

重点：

- 用户手工录入或文件导入持仓，不连接券商交易权限。
- 行业、风格、单一标的和相关性暴露。
- 收益归因、论文/事实更新对组合的影响映射。
- 观察名单与持仓的研究优先级合并。
- 情景分析明确展示假设，不生成收益承诺。

V2 退出条件：所有组合指标由持仓快照和市场数据确定性计算；可以回放任一历史日；Agent 只解释结果。

## V3：最终形态——个人投资研究操作系统

最终形态不是“自动炒股机器人”，而是一个可审计的长期认知系统：

- Research Planner 维护问题与证据需求。
- Data Agent 调用受控数据工具并生成 provenance。
- Deterministic Kernel 计算全部数值与信号。
- Critic Agent 检查证据缺口、冲突和过度推断。
- Portfolio Agent 解释组合暴露和变化。
- Memory 保存假设、决策日志、反证和结果复盘。

每个 Agent 都必须有最小权限、结构化输入输出、离线评测和完整审计。任何自动交易能力若未来被考虑，必须作为独立项目重新做权限、风险与合规评审，不能从当前研究路径自然扩张出来。

## 当前开工顺序

MVP 关键路径已于 2026-10-06 收口（见
[`mvp-release-checklist.md`](mvp-release-checklist.md)）。遗留事项：

1. 待 ifind-proxy/agent_gw 恢复北交所行情后，为 golden corpus 补回不少于 2 只北交所样本。
2. 推送至 GitHub 后确认 CI 首次真实 runner 运行。
3. 下一阶段按 [`funnel-design.md`](funnel-design.md) 的 P1 推进：目标池（baostock 成分）、因子库、模板筛选、规则起草与候选队列。
4. legacy NASDAQ Discovery/Attribution 已移出主干，代码保留在 tag `legacy-nasdaq-final`。
