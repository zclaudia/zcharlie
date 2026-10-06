# MVP 收口执行计划与 Session 交接

状态：Ready for execution

快照日期：2026-10-05

目标：达到 [`engineering-baseline.md`](engineering-baseline.md) 中的 MVP Definition of Done，
使用户无需命令行即可添加 A 股、运行或刷新研究、阅读证据、查看历史变化，并能从空环境完成部署与恢复。

## 1. 当前进度快照

### 已完成能力

- `ifind-proxy → canonical → deterministic kernel → Agent explanation → persistence` 已真实贯通。
- 分类、评分、质量、freshness 和报告上下文已归仓，不再依赖用户目录下的 Skill 执行。
- 单股研究已使用持久化数据库队列，具备幂等、原子认领、heartbeat、孤儿恢复和有限重试。
- 评分成功而解释失败时保留确定性结果，并以 `partial` 暴露。
- 研究 run、结果、历史制品、结构化 diff 和 meaningful-change 通知均有 API。
- A 股自选股支持沪、深、北代码映射、自动建档、批量刷新、逐标的失败隔离和状态回写。
- 日终任务使用 Asia/Shanghai 时区和版本化交易日历；未知年份 fail-closed。
- 数据库已迁移到 `012`；当前全量门禁为 236 tests + ruff + strict mypy。

### 最近完成的提交

| Commit | 内容 |
|---|---|
| `e9b06bc` | 仅在重要研究变化时生成通知 |
| `8e91d77` | A 股日终调度、交易日历与瞬时错误重试 |
| `04cbec6` | 结构化研究变化跟踪 |
| `e364a30` | 自选股批量刷新与失败隔离 |
| `58c9c84` | A 股自选股持久化元数据 |
| `6d55e3e` | 研究运行、结果和制品 API |

### 尚未完成

1. `MVP-103` 只有国电南瑞一个真实 golden 样本，尚未达到 10～20 只。
2. iFinD 脱敏响应 fixture 和端到端契约回归覆盖不足。
3. 尚无 CI workflow。
4. 尚无 Web UI；浏览器所需概览和单股历史聚合读 API 也未完成。
5. 尚未做空环境安装、Docker、备份恢复、安全和发布验收。
6. 迁移前的历史快照不会反向生成 change set；从新运行开始自动比较最近快照。

### 已冻结的 MVP 产品选择

- 严格单用户；MVP 不增加用户、组织、权限组或订阅系统。
- 最终用户界面只做响应式 Web；CLI 继续承担诊断、恢复和运维，不做原生客户端。
- 非 A 股 Discovery/Attribution 保留为 legacy，不扩展、不删除，也不阻塞 A 股 MVP。（2026-10-06 后续：已移出主干，代码保留在 tag `legacy-nasdaq-final`。）
- 继续坚持“研究辅助、非投资建议”，不增加任何交易执行路径。
- MVP Web 使用 FastAPI 同源托管的 Jinja2 HTML、原生 ES modules 和仓库内 CSS；不引入 Node 构建链或外部 CDN。

## 2. 关键路径与工期

```text
MVP-103 golden/contract/CI
        ↓
MVP-401 UI shell + session + read models
        ↓
MVP-402 four pages
        ↓
MVP-403 evidence navigation
        ↓
MVP-404 clean-environment smoke
        ↓
MVP-405 security + backup + release gate
```

预计 8～12 个有效开发日；若真实 iFinD 样本采集或脱敏受阻，增加 2～3 天缓冲。

| 工作包 | 预计 | 退出结果 |
|---|---:|---|
| A. Golden、契约与 CI | 2～4 天 | 10～20 只离线 corpus，所有门禁进入 CI |
| B. Web 基础与读模型 | 1～2 天 | 浏览器安全进入应用，API client 和页面 shell 可用 |
| C. 四个页面 | 2～3 天 | 浏览器完成核心日常研究闭环 |
| D. 证据导航 | 1 天 | 解释可跳到确定性字段及来源 |
| E. 发布验收 | 2～3 天 | 空环境、备份、安全、文档全部通过 |

## 3. 工作包 A：完成 MVP-103、契约和 CI

### A1. 定义 corpus 矩阵

目标数量为 12 只，允许扩充到 20 只。先按特征选样，再以 iFinD 实际可用性确定代码：

- 交易所覆盖：上交所不少于 4、深交所不少于 4、北交所不少于 2。
- 业务覆盖：银行/金融、消费、高端制造、周期/资源、软件或轻资产、高成长。
- 数据边界：至少 2 只存在关键缺失字段，1 只低或负盈利，1 只 freshness 临界，1 只模型适配度较低。
- 规模覆盖：大盘、中盘、小盘均有样本。

产物：`tests/fixtures/research_kernel/manifest.yaml`，记录 ticker、公司、行业、报告年度、
样本特征、采集时间、canonical/scoring/schema 版本和脱敏状态。

验收：矩阵至少 12 只，且每个覆盖维度都能由 manifest 自动断言。

建议提交：`test: define A-share golden corpus matrix`

### A2. 采集与脱敏 fixture

- 通过现有 `ifind-proxy` 获取每只股票研究所需的原始响应。
- 编写可重复的脱敏器；移除 API key、请求头、用户路径、临时 URL 和无关供应商元数据。
- 保存 provider fixture 以及生成后的 `pool.json`、`market.json`、`financial.json`。
- fixture 必须可离线使用，默认测试不得访问网络。
- 原 `my-stocks` 仅作为只读 golden oracle；其输出提交到 fixture 后，生产和测试运行均不得依赖用户目录。

验收：仓库搜索不到凭证；删除外部 Skill 路径后仍可生成 canonical 数据和评分。

建议提交：`test: add sanitized A-share provider fixtures`

### A3. 参数化 golden 等价测试

- 将现有单样本测试改为读取 manifest 的参数化测试。
- 逐字段比较分类、因子分、总分、质量标记和 report context。
- 浮点字段使用明确容差；分类、标签、缺失项和版本必须严格相等。
- 差异失败输出 JSON pointer、旧值、新值和样本 ticker。
- 若算法确需变化，必须同时升级 scoring version 并提交差异说明；不得直接更新 fixture 消除失败。

验收：至少 12 个样本在全新 checkout 中离线通过；运行次序不影响结果。

建议提交：`test: expand deterministic kernel golden coverage`

### A4. 契约回归与 Agent eval

- iFinD response → canonical：覆盖正常、空行、未知格式、缺字段和异常状态。
- Agent eval：引用字段有效率 100%，`needs_agent_review` 规则保持一致，不出现交易指令或输入外事实。
- 对解释失败保留 partial 的集成测试继续作为强制门禁。

验收：每个 provider endpoint 至少有一个正常 fixture 和一个错误/缺失 fixture；Agent eval 全部通过。

建议提交：`test: enforce provider contracts and explanation evals`

### A5. CI 门禁

新增 `.github/workflows/ci.yml`：

1. Python 3.13。
2. `pip install -e ".[dev]"`。
3. `ruff check .`。
4. `mypy src`。
5. `pytest -q`，禁止真实网络和 secrets。
6. 缓存 pip，但不缓存运行数据库或研究制品。

验收：空 runner 一次通过；故意制造 lint、类型或 golden 差异时 CI 必须失败。

建议提交：`ci: enforce MVP quality gates`

## 4. 工作包 B：MVP-401 Web 基础与读模型

### B1. UI 目录与同源 shell

建议结构：

```text
src/investment_agent/web/
  routes.py
  templates/
    base.html
    login.html
    overview.html
    watchlist.html
    stock.html
    runs.html
  static/
    app.css
    api.js
    app.js
```

- `/app` 是 Web 入口，FastAPI 同源提供模板和静态资源。
- 移动优先响应式布局；只建立排版、状态颜色、表格和卡片基础，不做视觉精修。
- 不引入 CDN、前端遥测或第三方脚本。
- 所有页面固定展示“研究辅助，非投资建议”。

验收：桌面与窄屏均无横向溢出；无 JavaScript 时仍显示可理解的错误提示。

### B2. 浏览器会话

- `/app/login` 接收 API key，只用于服务端验证。
- 验证成功后签发短期、签名、`HttpOnly`、`SameSite=Strict` cookie；cookie 不包含 API key。
- API 继续支持 `X-API-Key`；仅同源 UI 可使用会话 cookie。
- 登出立即清理 cookie；错误页和日志不得显示 key。
- 对 UI 的状态变更请求校验同源 Origin，并保持现有 API key 路径兼容。

验收：未认证访问重定向登录；cookie 篡改、过期或跨源请求被拒绝；HTML/日志无秘钥。

### B3. 浏览器 API client

- 统一处理 JSON、204、文件下载、401、409、422、503 和网络超时。
- 支持 loading、empty、error、retry 四种状态。
- 对提交按钮做进行中锁定；研究和刷新请求使用幂等键。
- 页面不自行解释 score 字段，展示逻辑只消费版本化 API。

### B4. UI 所需聚合读 API

在不复制真相源的前提下补充：

- `GET /dashboard/today`：待复核、失败、partial、meaningful change 和运行中计数及条目。
- `GET /watchlist`：增加最新 run 的分数、置信度、freshness、变化时间和状态摘要。
- `GET /research/stocks/{ticker}/history`：按 ticker 返回有界分页的历史快照摘要。
- 延续现有 `/research/runs/{id}` 作为详情真相源，不新建第二套结果 schema。

验收：四个页面不需要 N+1 请求；所有聚合字段可追溯到现有 snapshot/run/change 表。

建议提交边界：

1. `feat: add secure web session and application shell`
2. `feat: expose MVP dashboard read models`
3. `feat: add browser API client states`

## 5. 工作包 C：MVP-402 四个页面

### C1. 今日概览 `/app`

- 展示运行中、失败、partial、待复核和重要变化。
- 条目可跳转 run 或单股详情。
- 无事项时显示明确的“今日无待处理项”，而非空白页面。

### C2. 自选股 `/app/watchlist`

- 列表展示 ticker、名称、行业、最新分数、置信度、freshness、变化时间和刷新状态。
- 支持添加、启用/停用、删除和“刷新全部”。
- 批量刷新逐项显示 queued/failed，不能用一个总成功提示掩盖失败。

### C3. 单股详情 `/app/stocks/{ticker}`

- 展示分数、策略分类、因子、关键指标、质量状态、缺失项和 freshness。
- 展示 Agent 解释，但视觉上与确定性事实分区。
- 展示历史快照与“自上次以来变化”。
- partial 状态仍展示确定性结果，并明确解释不可用。

### C4. 运行中心 `/app/runs`

- 展示 queued/running/success/partial/failed、阶段、attempt、worker、时间和错误。
- 支持状态筛选、分页、查看详情和下载受控制品。
- 错误只显示可理解摘要，不显示 traceback、请求头或秘钥。

每页必须有 loading、empty、error、partial 和正常状态测试。优先使用 FastAPI/TestClient 断言 HTML
结构和 API 交互；仅对关键浏览器流程增加少量端到端测试。

建议提交：按页面独立提交，最后提交 `feat: complete MVP web workflows`。

## 6. 工作包 D：MVP-403 证据导航

- 为 Agent claim 的 `supporting_fields` 建立确定性字段索引。
- 点击引用后定位到 score、factor、metric、quality 或 provenance 区块。
- 展示字段值、来源 endpoint、provider、更新时间、quality 和 note。
- 引用字段不存在时显示“引用失效”并触发测试失败，不能静默隐藏。
- artifact 下载继续使用现有受控端点，不直接暴露文件系统路径。

验收：fixture 中所有解释引用都能定位到唯一字段；无效引用返回明确错误；页面不把 Agent 文本作为事实源。

建议提交：`feat: link agent claims to audited evidence`

## 7. 工作包 E：MVP-404～405 发布验收

### E1. 空环境 smoke test

新增可自动运行的 smoke 流程：

1. 创建临时数据目录和全新 SQLite。
2. 安装应用并执行 `ia migrate up`。
3. 启动 API、worker 和 scheduler，检查 `/healthz`。
4. 添加自选股，入队研究，使用受控 stub provider 完成一次运行。
5. 验证结果、diff、制品和 Web 页面。
6. 停止后重启，确认状态仍可查询。

Docker Compose 另做一次人工或 CI smoke；默认自动测试不得依赖真实 iFinD 或真实 LLM。

### E2. 安全检查

- API、UI、日志和制品无 secret。
- artifact 与 notification 路径穿越测试全部通过。
- 默认只绑定本地/受控地址；远程暴露必须显式配置。
- cookie、Origin、文件下载和错误响应满足 B2 约束。
- 依赖扫描只阻断已知高风险且可利用问题；结果记录在发布清单。

### E3. 备份与恢复

- 文档化停止写入、SQLite 一致性备份、`data/research-runs` 和 `data/notifications` 的同步步骤。
- 提供校验 manifest，包含文件大小和 SHA-256。
- 在临时目录完成一次恢复演练：迁移版本、watchlist、run、snapshot、diff 和制品均可读取。
- 明确保留策略；MVP 不自动删除原始证据。

### E4. 文档与发布清单

- README 以 A 股个人研究 MVP 为主线，legacy 功能移至单独章节。
- 更新测试数量、配置、三进程启动方式和 Web 登录步骤。
- 列出已知限制：单用户、SQLite、iFinD 单数据源、2026 交易日历、无交易能力。
- 建立 `docs/product/mvp-release-checklist.md`，逐项签字式验收。

建议提交边界：

1. `test: add clean-environment MVP smoke`
2. `docs: add backup and recovery runbook`
3. `chore: complete MVP release checklist`

## 8. MVP 最终验收清单

- [ ] 至少 12 只不同特征 A 股的 golden 与 provider fixture 离线通过。
- [ ] ruff、strict mypy、全量 pytest 和 Agent eval 在 CI 通过。
- [ ] 浏览器可登录、添加/管理自选股并批量刷新。
- [ ] 浏览器可查看分数、置信度、freshness、证据、缺失项、历史和 diff。
- [ ] queued/running/success/partial/failed 均有可理解 UI。
- [ ] 单只失败不阻断同批其他股票；重启可恢复或终止孤儿任务。
- [ ] meaningful change 产生一次通知；baseline、无变化和噪声静默。
- [ ] 空数据库可以按文档迁移、启动并完成一次 stub 研究。
- [ ] 备份恢复演练通过，数据库与制品仍由 run_id 对齐。
- [ ] API、日志、页面和归档不泄漏任何秘钥。
- [ ] 所有页面明确展示“研究辅助、非投资建议”。
- [ ] 不存在下单、券商连接或自动交易路径。

## 9. 执行纪律与停止条件

- 每个工作包先写失败测试，再实现，再跑定向测试，最后跑全量三门禁。
- 每个可独立回滚的工作包单独提交；不得把 corpus、UI 和发布脚本混在一个提交中。
- 不静默改 golden；差异必须解释为 bug fix 或版本升级。
- 不为 UI 复制评分或变化逻辑；数据库和现有 API schema 仍是真相源。
- 不在 MVP 内增加新 LLM 角色、新供应商、组合管理或交易能力。
- 若真实样本包含敏感字段，先停下完善脱敏器，不把原始数据直接提交。
- 若 UI 需要的字段无法从现有审计记录推导，先补版本化后端契约，再做展示层。

## 10. 新 Session 启动指令

建议在新 session 直接发送：

> 请在 `/Users/zhvala/SourceCode/zcharlie` 继续个人投资 Agent MVP。
> 完整阅读 `docs/product/engineering-baseline.md`、`docs/product/delivery-roadmap.md`
> 和 `docs/product/mvp-completion-plan.md`，核对 HEAD 与工作区状态，然后从工作包 A1
> 开始。严格按计划的依赖、验收条件和提交边界执行；每完成一个可回滚工作包就提交，
> 不扩展到 V1/V2，也不引入交易能力。

启动前核对：

```bash
git status --short
git log -6 --oneline
.venv/bin/ia migrate current
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest -q
```

预期基线：工作区干净、HEAD 至少包含 `e9b06bc`、数据库为 `012 (head)`、全量测试不少于 236 个。
