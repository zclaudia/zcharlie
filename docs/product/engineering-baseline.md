# 个人投资 Agent 工程基线

状态：Accepted for MVP

基线日期：2026-10-05

适用范围：`zcharlie` 与 `ifind-proxy` 组成的 A 股个人投研系统

## 1. 产品契约

第一目标用户是单个投资者本人。产品帮助用户持续收集证据、计算可复现指标、发现变化并形成待核查问题；它不是交易系统，也不代替用户作出投资决策。

MVP 必须完成的用户闭环：

1. 用户输入一只 A 股或从自选股选择标的。
2. 系统从 `ifind-proxy` 获取行情与财务数据。
3. 系统把供应商响应转换为有版本的 canonical 数据。
4. 确定性内核完成分类、评分、风险与数据质量判断。
5. Agent 只根据确定性结果生成带字段引用的解释。
6. 原始数据、标准数据、分数、解释和运行状态被持久化。
7. 用户能够在下一次刷新时看到“什么发生了变化”。

明确不属于 MVP：

- 下单、模拟下单、自动调仓或券商账户控制。
- 根据用户资产给出买卖、仓位或收益承诺。
- 多用户、订阅收费、公开荐股和社交功能。
- 全市场高频扫描和盘中交易信号。
- 用 LLM 生成或修正财务数字、因子分和排名。

## 2. 系统边界

```text
ifind / agent_gw
        │
        ▼
ifind-proxy                 数据网关
  鉴权、限流、供应商调用、响应解包、缓存
        │ provider response
        ▼
zcharlie                    产品与研究主系统
  provider adapter
        │ canonical.vN
        ▼
  deterministic kernel     分类、因子、评分、风险、质量
        │ score.vN
        ├────────► storage / history / diff
        ▼
  explanation agent        仅解释、引用、列出待核问题
        │
        ▼
  API + CLI + Web UI
```

所有权约束：

| 能力 | 唯一所有者 | 约束 |
|---|---|---|
| iFinD 凭证与供应商调用 | `ifind-proxy` | `zcharlie` 不直接依赖 iFinD SDK |
| canonical schema | `zcharlie` | 独立于供应商字段，显式版本化 |
| 策略分类与评分 | `zcharlie` research kernel | 纯确定性、可离线重放 |
| Agent 解释 | `zcharlie` | 不能更改分数，论据必须引用已存在字段 |
| 运行、历史和变更 | `zcharlie` | 数据库为状态真相，文件为可审计制品 |
| Skill | 薄客户端 | 最终只调用 App API，不拥有另一套评分实现 |

MVP 采用模块化单体。除现有 `ifind-proxy` 外，不拆微服务。单用户阶段使用 SQLite；只有出现多用户或并发写入需求时才评估 PostgreSQL。

## 3. 不可破坏的研究规则

- 未知数值一律为 `null`，不得用 `0`、空字符串或模型猜测代替。
- 每个进入评分的研究字段必须带来源、更新时间和质量类型。
- `business_score` 与 `research_confidence_score` 始终分离。
- 缺字段、过期、供应商冲突或模型不适配必须触发复核。
- `needs_agent_review=true` 时工作流标签必须是 `needs_review`。
- 工作流标签只描述研究优先级，不能使用交易指令语言。
- 每次运行固定 canonical schema、scoring version、as-of 时间和原始响应。
- LLM 失败不得改变已经计算出的确定性结果。MVP 退出前应支持“评分成功、解释失败”的 partial 状态。
- 历史回放只能使用当时已经可获得的数据，不能引入未来数据。

当前 canonical 输入契约由 `investment_agent.research.contracts` 定义。任何供应商适配器在调用评分内核前都必须通过该契约。

## 4. 运行与接口基线

MVP 同时保留两个入口：

- CLI：用于开发、诊断、批处理和恢复。
- HTTP API：用于 Web UI 和未来 Skill 调用。

首个 API 为同步单股研究接口：

```http
POST /research/single-stock
X-API-Key: ...

{
  "ticker": "600406.SH",
  "industry": "电力设备",
  "report_year": 2025,
  "as_of": "2026-10-05"
}
```

同步执行只是 MVP 起步实现。MVP 退出前，耗时研究必须进入可恢复的持久化任务队列，API 返回 `run_id`，客户端查询状态或订阅事件；不得使用进程内 `BackgroundTasks` 作为最终任务系统。

## 5. 数据与存储基线

每次研究运行至少保存：

- 请求参数和 `as_of`。
- 原始供应商响应。
- canonical `pool`、`market`、`financial` 数据。
- 确定性 `scores.json` 与报告上下文。
- Agent 结构化解释和可读报告。
- 字段来源、缺失项、质量标记、冲突和 freshness。
- 模型、token、延迟、错误和运行状态。

数据库记录负责检索和状态；制品目录负责完整审计。数据库与制品必须通过 `run_id` 关联。不得把 API Key、完整请求头或其他凭证写入两者。

Schema 兼容策略：

- 破坏性字段变更升级 major，例如 `canonical.v2`。
- 新增可选字段升级 minor，并保持旧读取路径。
- 评分权重或阈值变化必须升级 scoring version。
- 历史结果永不静默重算；重算产生新的 run。

## 6. 安全与隐私基线

- 秘钥只来自环境变量或未来的 secret manager。
- HTTP API 默认绑定本地或受控网络，并要求 `X-API-Key`。
- 日志默认脱敏，不记录认证头和完整 LLM prompt。
- 原始供应商数据和个人自选股不进入公开遥测。
- 不执行来自网页、报告或模型输出的代码或工具指令。
- 所有外部请求有超时、有限重试和明确错误分类。

## 7. 可观测性与可靠性

每次运行至少可查询：状态、阶段、开始/结束时间、供应商延迟、LLM 延迟、错误类型、schema/scoring/model 版本和制品位置。

MVP 服务等级目标：

- 相同 canonical 输入与评分版本得到字节语义等价的评分结果。
- 单只股票正常运行成功率不低于 95%（排除明确的供应商停机）。
- 失败运行不留下 `running` 状态；重启后能够识别和恢复或终止孤儿任务。
- API 和日志不泄漏秘钥。
- 典型单股研究在 60 秒内完成；确定性部分在 5 秒内完成。

## 8. 测试与验收基线

提交门禁：

```bash
pytest -q
ruff check .
mypy src/investment_agent
```

测试层级：

- 单元测试：字段映射、派生指标、契约、评分、freshness、解释引用校验。
- 契约测试：固定 `ifind-proxy` 响应 fixture 到 canonical 输出。
- Golden 测试：10～20 只 A 股固定输入的分类、分数和质量标签。
- 集成测试：数据库、API、任务恢复与制品写入。
- 少量 live smoke test：验证真实网关，不进入默认测试套件。
- Agent eval：引用字段有效率 100%，不出现交易指令，不虚构输入外事实。

## 9. 技术决策

| 项目 | MVP 决策 | 重新评估条件 |
|---|---|---|
| 后端 | Python 3.13、FastAPI、Pydantic、SQLAlchemy | 无 |
| 架构 | 模块化单体 | 独立伸缩或发布需求出现 |
| 主存储 | SQLite + Alembic | 多用户或并发写入成为瓶颈 |
| 长任务 | 持久化数据库队列 + worker | 多机器后再评估 Redis/Celery |
| UI | 响应式 Web UI | 明确需要系统级移动能力 |
| 数据源 | iFinD 优先、provider 接口可替换 | 新数据源能改善覆盖或容灾 |
| 评分 | 仓库内确定性 kernel | 每次变化必须版本化与回测 |
| LLM | OpenAI-compatible adapter | 保持供应商可替换 |
| 部署 | 本地 Docker Compose | 需要远程访问时再设计云部署 |

## 10. 内核迁移状态

分类、评分、数据质量、freshness、同行比较和报告上下文已经迁入 `zcharlie` 的 `investment_agent.research_kernel`。生产 CLI/API 已移除 `LEGACY_RESEARCH_KERNEL_ROOT` 和 subprocess 依赖。

原 `my-stocks` 0.3.0 输出被保留为只读 golden oracle。MVP 退出前仍必须：

1. 将 golden corpus 从当前真实样本扩充到 10～20 只不同类型 A 股。
2. 每次算法变化同时提交 scoring version 和新旧回归报告。
3. 把原 Skill 改成 API 薄客户端或归档，避免形成第二套运行时。

## 11. MVP Definition of Done

只有同时满足以下条件才称为 MVP 完成：

- 单股研究与自选股刷新均可通过 API 使用。
- `zcharlie` 不依赖用户目录下的 Skill 才能评分。
- 具备持久化任务、运行查询、失败恢复和幂等键。
- 具备研究历史和相邻两次结果 diff。
- 有最小 Web 页面展示分数、证据、缺失项和变化。
- golden corpus、集成测试与 Agent eval 全部通过。
- 能从空数据库按文档完成配置、迁移、启动和一次研究。
- 明确展示“研究辅助、非投资建议”，不存在交易执行路径。
