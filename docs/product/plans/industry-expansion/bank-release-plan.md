# 银行年度研究发布收尾计划

日期：2026-10-17。目标：准备可执行子任务、隔离回归/迁移与镜像边界演练及发布手册；实际部署前须再次取得责任人对明确目标、镜像/提交、schema、备份恢复与中断窗口的最终确认。使用独立工作树 `/Users/zhvala/.codex/worktrees/bank-release-readiness/zcharlie`，初始基线 `acb0856`。主工作区不在本计划范围。不得提交、部署、推送、访问生产、调用真实业务来源或真实 LLM、读取 `.env`/密钥。隔离镜像/依赖构建按主代理授权的构建流程进行；真实业务网络请求与生产访问禁止。并行 P4 明确排除；最终全量门禁与判断由主代理负责。

| ID | 子任务 | 执行者 | 交付与验收 |
|---|---|---|---|
| B1 | 候选提交及来源/原件预检工具 | GPT-6 Luna、主代理 | 主代理冻结SHA/工作树；预检器只读核查指定证据根的100槽审计绑定、原件存在性、尺寸/哈希、年度及manifest引用；不读密钥/DB、不联网；给出机器报告和失败负例。 |
| B2 | 隔离回归、迁移和镜像边界演练 | GPT-6 Luna、主代理 | 仅本地临时tag/临时容器和独立SQLite/数据目录；验证048→052、两年度发布/历史不变/GET不入队、备份校验恢复及镜像边界；不运行共享Compose或生产卷。 |
| B2a | 修复FY2025来源切换后的离线发布入口 | GPT-6 Luna、主代理审查 | 显式`--bank-annual-source --cohort-key`；强制实际审计和原件、名单scope及成员核验，固定适配器，旧默认不变；真实公开CLI与两年度查询回归。 |
| B3 | 发布手册 | GPT-6 Luna | 仅编辑本文件及 [`bank-release-runbook.md`](../../bank-release-runbook.md)。核对实际部署脚本、compose、镜像内容、运维/备份手册、银行验收与 CLI；写明 API-only 确认后步骤、原件搬运、95/1/4 双年度批次、鉴权页面/API核对、失败恢复约束。不得改共享脚本/compose。 |
| B4 | 主代理全量门禁与验收 | 主代理 | 在同一候选树审查 B1–B3；运行经授权的隔离全量回归/类型与静态检查、迁移/镜像演练，审查 P4 隔离、文档命令和最终计数；记录通过与失败边界。任何门禁未通过不得写成候选可发布。 |

## 本轮发布边界

候选只承载已审查提交，不带入并行 P4 修改。离线银行研究读取与显式批次发布单独于在线来源采集、worker/scheduler 调度和外部 LLM/iFinD；该手册不代表来源用途许可或生产启用。通用 `tools/deploy.sh` 会启动 api、worker、scheduler，因而不适用于本次 API-only 范围；本轮不修改共享部署脚本或 Compose。主代理不得仅凭离线功能验收推断生产主机、卷、DB 当前 revision 或服务状态。

双年度预期矩阵：FY2024 为 47 verified、1 candidate、2 scope mismatch；FY2025 为 48、0、2。合计 95 verified、1 candidate、4 scope mismatch。candidate 与排除必须保留原状态及理由。导入顺序为：本地证券目录核验 → `ia peers import-roster --industry bank --bank-online --cohort-key bank_run02_sample` 建立名单/绑定读 scope → `ia peer-annual import-bank --bank-annual-source --cohort-key bank_run02_sample` 导入 FY2025 verified-manifest → FY2024 矩阵离线导入 → `eastmoney/bank-annual-v1` 两年度离线批次冻结。API 启动前先运行一次 `ia settings import`（不带 `--force`），避免首次 API lifespan 导入 env/app.yaml 时覆盖 DB 设置；再以 `ia settings get/set` 记录并设置持久 DB 值：`industry_peer_context_enabled=true`、`investor_agent_enabled=false`、`investor_agent_background_enabled=false`、`gateway_enabled=false`；不能仅更改 `.env`。API lifespan 启动 GatewayManager，并在 StockDirectory 总数为零时后台发起 Baostock 全目录同步，因此发布前要确认目标十家各有精确 `stock_directory.ticker` 记录并保持网关关闭。数据库与 `DATA_DIR` 研究原件/制品作为一个可恢复集合管理；先停止 worker、scheduler、API 的写入，再用旧镜像对静止状态备份。候选镜像由批准 SHA 的 `git archive` 在空目录构建，使用唯一候选 tag 并保留旧镜像 ID；通过临时 Compose image override 指向候选。回滚须按旧代码/schema/数据库/制品匹配恢复，不使用脚本的盲目 `rollback`。

## 状态

**B1–B4及B2a已完成，主代理发布准备验收通过；未提交、未部署。** 排除P4的基线全量4194 passed、6 skipped、6 network deselected；增量及兼容专项56 passed，新迁移/发布演练独立通过；正式脚本恢复后95字段、21份PDF和8项严格引用重验通过。候选443生产源文件mypy及修改范围Ruff通过。

隔离候选为`acb0856`加受审两CLI文件，增量标记`acb0856-bank-release-4a04d3bc6bb9`；Linux/amd64演练镜像烟测通过，唯一schema head为052。精确镜像ID、来源清单、用例与限制见[主代理验收记录](../../../review/bank-release-acceptance-2026-10-17.md)。这不是正式部署提交：先提交本轮代码，再从批准完整SHA重新构建并核对目标环境；仍须在实际部署前填写运行手册第5节并取得用户确认。不得表述为已部署、已获来源许可或已对生产库完成回归。
