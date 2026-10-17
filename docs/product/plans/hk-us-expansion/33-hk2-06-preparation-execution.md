# HK2-06 来源核对、验收工具与三日计划实施

日期：2026-10-17（Asia/Shanghai）。基线为 HK2-05 提交 `02daa6d`，继续使用独立工作树
`/Users/zhvala/.codex/worktrees/hkus-source-policy/zcharlie`、分支 `codex/hkus-source-policy`。
本轮是 HK2-06 的准备阶段，不执行真实财报接口采集、不累计三日真实记录、不操作生产库、
不覆盖主 checkout 的 P4 未提交改动，不合并 main 或部署。官方政策网页阅读不计为财报观测。

## 子任务、依赖与验收

| 子任务 | 执行者 / 优先级 | 交付与验收标准 |
| --- | --- | --- |
| A 来源使用范围与请求要求 | GPT-6 Luna / P0 | [34](34-hk2-06-source-review.md)：一手官方材料逐源列明内部研究、缓存、展示、再分发与商业使用依据，区分事实、推断和待确认。SEC 身份/邮箱、跨机器总速率、拒绝处理；东财实际端点与 Choice 协议适用边界未知，不能从公开可访问推定授权 |
| B 隔离单次 probe | GPT-6 Luna / P0，依赖 A 请求约束 | 新 `source_probe.py` 和 CLI。默认仅 dry-run，零请求。未来显式 `--allow-network` 仍须固定受控策略对该源全部证券许可通过；真实联系 UA、全源串行共享 limiter、每请求及等待后复核许可/独立 STOP，401/403/429 停止整源，有限重试。所有响应标准归档，模拟注入明确标 simulation；不改普通在线开关或技术审批 |
| C 离线观测汇总与三日计划 | GPT-6 Luna / P0，依赖 A、B 报告契约 | 新 `source_observations.py`、CLI、plan-only YAML 与 [35](35-hk2-06-observation-plan.md)。逐报告核验公司范围、请求与原件哈希、响应日期、当前适配器重解析和本地人工复核引用；不同日期、最新连续三次、失败清零；simulation/dry_run 不计日。最多输出技术复核候选，绝不自动审批 |
| D 集成验收与提交 | 主代理 / P0，依赖 A/B/C | 独立审查官方依据、接口和安全边界，真实 CLI 零网络预检/空账本演练及真实适配器 MockTransport 联测；补期间、路径、关闭和三日计数负例；适当离线回归、Ruff/mypy，通过后 commit |

## 冻结边界与验收范围

- `ONLINE_REFRESH_ENABLED=False`、两来源 ops 默认 false、受控许可 unknown 和技术 not_accepted 全部保持不变；`observation_plan.yaml` 只用于计划，不被运行时当作激活配置。
- 常规刷新与未来一次性取证分离：隔离 probe 可在责任人核实来源范围、许可及请求要求后，经过显式命令执行；它不需要先伪造三日技术验收，也不能因此打开日常在线刷新。本轮不执行 `--allow-network` 的真实路径。
- 默认预检和离线汇总不初始化数据库。未来获准或测试注入的单次 probe 仅可显式创建命令内 `sqlite+aiosqlite:///:memory:` 公司/证券表，以得到标准 archive 的本地 subject ID；不加载 .env、Settings.database_url 或全局应用 engine，不接入任何既有库和任务队列。报告标明 `isolated_probe_memory` 命名空间，不能把这些 ID 当成生产 ID。
- probe 工作目录必须全新，原件与报告不覆盖现有文件；汇总输出不能覆盖报告、账本、引用或原件。报告内归档引用相对各自报告所在目录，所有输入仍受统一 evidence-root 约束。
- 港股必须明确给出连续完整年度起止跨度，不能把保留的 2021–2024 样本窗口当最新窗口；SEC 随当前 adapter 选取实际申报财年和全部列示历史分片。
- 一次性 probe 不宣称已有自动断点续跑/应用资料版本能力。真实重启恢复、旧版本读取、关闭与无网重放须另有隔离演练证据；当前 HK2-05 模拟记录不冒充 HK2-06 实测。
- 本地哈希只能证明文件一致，不能独立证明响应确实来自某日源站。汇总结果只能是 `pending` 或 `technical_review_candidate`；还须负责人核实来源真实性、许可、年度对照和真实演练，再单独审批技术状态。
- 三日以条件满足后人工选定的 D+0/D+1/D+2 表达，既不预填已完成日期，也不安排自动任务。SEC 可先准备，东财必须先厘清端点授权；两源分别累计、分别失败，不互相扩大发布范围。

临时演练和日志使用本工作树的忽略目录 `data/hk206-preparation/` 及 `/tmp/hk206-*`。
最终交付与门禁见 [36](36-hk2-06-preparation-acceptance.md)。
