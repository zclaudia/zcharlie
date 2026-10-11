# 数据源与交互引入计划（源自 marketingdashboard 调研）

状态：Done（2026-10-09 全部 12 个任务代码完成并合入 main，`0dee293`～`0a8b5b2`，1509 tests；本机真实链路验收见各子计划完成记录）

日期：2026-10-09

依据：[`funnel-design.md`](funnel-design.md) 第 12 节 P2 / P4 "数据源准备"与 15.3；
调研对象 [theBigGavin/marketingdashboard](https://github.com/theBigGavin/marketingdashboard)（下称 mrd）。

## 0. 目标与完成定义

把调研表中的 1～7 项引入本项目，分四个里程碑交付：

| 调研项 | 内容 | 任务 |
|---|---|---|
| 3 | provider 级退避 + 负缓存 + 多源回退 | A1 |
| 7（表中"上游计数"） | `/health/data` 每个 provider 的上游调用 / 失败计数 | A2 |
| 4 | 各源单位口径与校准日期的记录 | A3 |
| 1 | 腾讯批量报价 → 当日收盘快照 | B1、B2 |
| 5 | 手写 SVG 日线迷你图 | B3 |
| 2 | 东财业绩预告 / 披露日历 / 主营构成 | C1、C2、C3 |
| 6 | 股票名称 / 拼音搜索 | D1 |
| 7（表中"只读 MCP"） | 只读 MCP Server | D2 |

完成定义：第 7 节全部任务通过门禁（`ruff check .`、`mypy src`、`pytest -q -m "not network"`，
静态文件守卫测试与 `node --check` 清单更新）；本机真实链路各跑通一次并记入第 7 节表格。

所有新接口都是非官方公开接口，随时可能变更或断连。原则：**只做增量能力，不作为任何字段的唯一来源；
每条写入都带 `provider` 与 `captured_at`，遵守现有 PIT 校验（`LookaheadDataError`）**。

## 1. 现状要点（影响计划的事实）

| 事实 | 影响 |
|---|---|
| `market_snapshot` 以 (listing_id, trade_date, adjust, version) 唯一，`provider` / `captured_at` 不在键内；重采以 `version = max+1` 追加（`storage/models/pit_snapshot.py:71-111`） | 腾讯收盘价可作为同一 trade_date 的另一 provider 版本写入，不改表结构；但读取侧需定义 provider 优先级 |
| 目标池日线由 `snapshots/breadth.py::collect_baostock_market` 在 18:30 采集（`config/app.yaml` `market_snapshot_cron: "30 18 * * 1-5"`） | 当日 15:00 后到 18:30 前没有收盘价；P2 的日频区间判定只能次日前做。腾讯报价把这个窗口提前到 15:10 |
| 重试与熔断都是"单次任务内"的：`breadth.TransientBreaker`（连续 5 次）、`eastmoney.FailureBreaker`（连续 3 次）；无跨任务状态、无负缓存 | 上游整族断连时每次任务都要重新撞满阈值；A1 补跨任务状态 |
| 上游计数只有 iFinD 的 `ifind_call_ledger`（预算逻辑依赖它）；akshare / baostock 只在 run metadata 里记 `akshare_calls` / `baostock_queries` | A2 新建通用台账，不动 iFinD 台账 |
| 东财接入统一经 `AkshareEastmoneyClient`（`requests` 垫片映射瞬态错误、全局锁串行、1 秒间隔、2 次重试） | 新的东财报表继续走这个客户端，不另起 httpx 通道；主营构成也在 akshare 有封装 |
| `financial_snapshot` 的 `available_from` 取 baostock `pubDate`；`report_overdue` 仅由 baostock "未披露"推断 | 东财披露日历只作次级信号，不改变现有逾期判定主口径 |
| 通知只有 `FileNotifier`；`notification_level` 仅对 universe_sync 变更发 `info` | 业绩预告通知先落文件，推送渠道仍是 P2 的事 |
| Web：Jinja 壳 + ES module，CSP 禁内联样式 / 脚本；守卫测试禁 `http://` 字面量、禁 innerHTML；新 JS 文件要加进 `test_web_client_contract.py` 的 `node --check` 清单 | 迷你图与搜索框必须走 `ui.js` 的 `svg()` / `el()` 路径 |
| `mcp` 2.x 已随 `pydantic-ai` 间接锁定，未显式声明 | D2 要显式声明依赖 |
| 迁移最新为 `021`；每个迁移配一份 `tests/integration/test_migration_0NN.py` | 本计划迁移从 `022` 起 |

## 2. 已确认的决策（2026-10-09）

| # | 决策 | 结论 | 备注 |
|---|---|---|---|
| D1 | 腾讯报价的用途 | 只做**当日收盘快照**（15:10 后采集），不做盘中 | P2 区间判定仍是日频；盘中报价留给以后 |
| D2 | 同一 trade_date 同时有 baostock 与 tencent 版本时谁优先 | **baostock 优先**，tencent 仅在 baostock 缺失时使用；两者收盘价差超 0.1% 记 `conflict` | 在 `field_sources.yaml` 增加 market 表 `close` 等字段的 primary / fallback 声明 |
| D3 | 东财三类报表的接入方式 | 经 akshare：`stock_yjyg_em`（业绩预告）、`stock_yysj_em`（预约披露）、`stock_zygc_em`（主营构成）；函数名以 akshare 1.19 实测为准 | 与 P1-A11 同一客户端、同一垫片、同一节流 |
| D4 | 股票搜索的字典来源 | 本地 `stock_directory` 表，每周随 `sync-universe` 从 baostock `query_all_stock` 同步全部 A 股，拼音首字母用 `pypinyin` 生成 | 不接新浪 / 东财 suggest，离线可用，不受上游条款约束 |
| D5 | MCP 传输与权限 | stdio，只读；对 SQLite 以只读方式打开；不暴露写操作与 LLM 调用 | 供 Claude Desktop 本机使用；HTTP 挂载到 FastAPI 以后再议 |
| D6 | 跨任务熔断状态存哪 | worker 进程内存（worker 常驻），同时把每次状态变化写入 A2 台账便于观测；不新建状态表 | 重启后状态清零，可接受 |
| D7 | 迷你图数据窗口 | 最近 120 个交易日，不复权，baostock 版本；预留 `bands` 参数给 P2 区间带 | 只读已有快照，不新增采集 |

## 3. 工作流 A：上游基础设施

### A1 provider 熔断器与负缓存（M）

- 新建 `providers/circuit.py`：按 `provider:endpoint` 键维护 `{fail_count, open_until, last_error}`；
  退避序列 5s → 15s → 45s → 2min → 5min（上限），成功即复位。
- 窗口内调用直接抛新异常 `ProviderUnavailableError(ProviderTransientError)`（负缓存，不打上游）；
  采集器收到它按既有 `report.stop_for_provider` 路径置 `partial`，任务级重试照常，由下一次调度自然重试。
- 接入点：`AkshareEastmoneyClient._call`、baostock 两个客户端的重试循环、B1 腾讯客户端；
  现有 `TransientBreaker` / `FailureBreaker` 保留，改为在触发时同步把熔断器打开。
- 多源回退：market 表字段纳入 `field_sources.yaml`（D2），因子层按既有 primary / fallback 规则解析；
  不在采集层做跨源替换。
- 测试：退避序列、窗口内不调用上游、成功复位、`partial` 落地；用 `freezegun` 推时间。

### A2 通用上游调用台账与健康页计数（S）· 迁移 `022`

- 新表 `provider_call`：`at`、`provider`、`endpoint`、`outcome`（ok / transient / terminal / skipped_open）、
  `duration_ms`、`run_id`、`ticker_count`；索引 `(provider, at)`。
- 客户端 `on_call` 钩子写入（沿用 `verification/budget.py::ledger_hook` 的 best-effort 风格）；
  iFinD 台账保持不变。
- `/health/data` 新增 `providers: list[ProviderHealth]`（24h 调用数、失败数、熔断中 / 熔断至、最近错误）；
  `web/static/health.js` 新增一个区块；`derive_warnings` 对"熔断中"发 warning。
- 保留策略：90 天，随现有清理任务删除（若无清理任务，加到 `ia admin` 子命令）。
- 测试：台账写入、健康页聚合、迁移测试。

### A3 口径与校准记录（S）

- `field_sources.yaml` 每个 provider 条目增加 `calibration: {checked_on, method, note}`；
  本计划新增字段（腾讯收盘价、东财预告与主营构成）首次入库时填实测值。
- `providers/akshare_eastmoney.py` 与 B1 客户端文件头加"源链 + 单位口径 + 实测日期"说明块
  （mrd `moneyflow.cjs` 的写法），注明腾讯 `amount` 为万元、baostock 为元等差异。
- `factors/field_sources.py` 的加载校验：`definition` 必须含单位；缺 `calibration` 的新 provider 条目报错。
- 测试：校验器用例。

## 4. 工作流 B：行情与迷你图

### B1 腾讯报价客户端（M）

- 新建 `providers/tencent_quote.py`：httpx，`https://qt.gtimg.cn/q=<codes>`，60 码一批，
  GBK 解码，解析 `v_<code>="…"` 的 `~` 分隔字段（现价 3、昨收 4、开 5、量 6、时间 30、
  涨跌 31/32、高 33、低 34、成交额 37 万元、换手 38、PE 39、振幅 43）；带 UA；
  注入 `transport` 与 `on_call` 钩子，仿 `IfindProxyClient`。
- PIT 守卫：字段 30 的日期必须等于请求的 trade_date 且时间 ≥ 15:00，否则该标的记 `pending`（未收盘），
  晚于采集日则抛 `LookaheadDataError`。
- 代码映射：`600519.SH → sh600519`、`000001.SZ → sz000001`、`BJ → bj`，复用 `parse_a_share_ticker`。
- 测试：fixture 一份真实响应（含停牌、ST、北交所）；分批；未收盘；字段缺失。

### B2 收盘快照任务（M）

- 新快照 kind `close_quote`（`snapshots/jobs.py` `SNAPSHOT_KINDS` 与 payload），
  cron `10 15 * * 1-5`（`config/app.yaml` 与 `settings/registry.py` 镜像），交易日门控同 `market_snapshot`。
- 写 `market_snapshot`：`provider = tencent`、`adjust = none`、OHLC / volume / amount（换算为元）/ turnover / pe_ttm；
  pb、ps 留空；`captured_at` 取客户端时钟。
- 范围：目标池 + 观察池 + 候选 + 持仓（复用 `scope_members`）。
- 读取侧：`field_sources.yaml` market 字段按 D2 声明；`series_as_of` 在同一 trade_date 多 provider 时按声明取值；
  健康页 `market_stale` 同时报告两源的最新日期。
- 测试：payload 幂等键、调度门控、写入与版本、与 baostock 同日冲突标记、`partial` 路径。

### B3 SVG 日线迷你图（S）

- `ui.js` 新增 `sparkline(points, { width, height, bands = [] })`：`viewBox 0 0 100 30`、
  `preserveAspectRatio="none"`、`polyline` + `vector-effect: non-scaling-stroke`，颜色走 CSS class；
  `bands` 为 `{from, to, kind}` 的水平区间矩形（P2 区间带占位）。
- 新接口 `GET /market/{ticker}/bars?days=120`（as_of 当前时刻、不复权、provider 优先级同 D2），
  返回 `[{trade_date, close}]` 与 `provider`。
- 挂载：观察池行、候选卡、股票页顶部（股票页用 240 日、高 80）。
- 守卫：无 `http://` 字面量、无内联样式；若新建 JS 文件须加进 `node --check` 清单。
- 测试：接口契约；`sparkline` 的 DOM 结构用现有 web 契约测试方式覆盖。

## 5. 工作流 C：财报事件

### C1 东财预告 / 预约披露 / 主营构成采集器（M）· 迁移 `023`

- 先用 akshare 1.19 实测三函数并落 fixture，确认列名；函数名或列名变化时测试失败（同 P1-A11）。
- 新表：
  - `earnings_forecast`：`listing_id`、`report_period`、`notice_date`、`forecast_type`（预增 / 预减 / 扭亏 / 首亏 /
    略增 / 略减 / 减亏 / 增亏 / 续盈 / 续亏 / 不确定）、`profit_low` / `profit_high`、`yoy_low` / `yoy_high`、
    `content`、`provider`、`captured_at`、`raw_ref`；唯一 (listing_id, report_period, notice_date)。
  - `disclosure_schedule`：`listing_id`、`report_period`、`scheduled_date`（预约 / 变更后）、`actual_date`、
    `captured_at`；唯一 (listing_id, report_period, captured_at)，按时点追加。
  - `business_segment`：`listing_id`、`report_period`、`kind`（product / industry）、`name`、`revenue`、
    `profit`、`margin`、`share`、`captured_at`、`raw_ref`。
- PIT：`notice_date` / `scheduled_date` 晚于采集日抛 `LookaheadDataError`；全市场按期拉取后按目标池 + 观察池 +
  候选 + 持仓过滤。
- 测试：三份 fixture、类型归一、金融股缺失、重复公告去重。

### C2 采集调度与 `info` 通知（S）

- 新 kind `earnings_event`，cron 每个交易日 19:00，仅在披露季窗口（1 月、4 月、7 月、10 月整月，
  窗口常量与 `CASH_FLOW_WINDOWS` 并列）运行；主营构成随现金流量表窗口每季一次。
- `notification_level`：观察池 / 候选 / 持仓中的标的出现**新的**业绩预告，或预约披露日在 3 个交易日内，发 `info`；
  正文列类型、净利区间、公告日与来源。
- `report_overdue`：`actual_date` 可作为解除逾期的次级证据（baostock `pubDate` 仍为主），差异记 metadata。
- 测试：窗口门控；新预告才通知，重复不通知；通知正文可回溯到 `raw_ref`。

### C3 主营构成进入起草上下文（S）

- 规则起草 Agent 的事实包加入最新一期主营构成（Top 5 段、占比），仅作为事实，不参与评分。
- 候选详情页展示该段落。
- 测试：起草 prompt 快照含该段；无数据时省略。

## 6. 工作流 D：交互

### D1 股票搜索（M）· 迁移 `024`

- 新表 `stock_directory`：`ticker`、`name`、`pinyin_initials`、`status`、`synced_on`；
  `sync-universe` 末尾用 baostock `query_all_stock(day)` 全量刷新（约 5500 行，一次调用），
  `pypinyin` 生成首字母（新增依赖，纯 Python）。
- 接口 `GET /stocks/search?q=&limit=10`：代码前缀、名称子串、拼音首字母前缀三路匹配，按精确度排序，
  返回 `{ticker, name, in_universe, on_watchlist}`。
- 前端：观察池添加表单与候选"提名"输入改为 combobox（`el()` 构建，键盘上下 / 回车；无第三方库），
  选中后自动填 `name`，`parse_a_share_ticker` 规则不变。
- 测试：三路匹配、拼音生成、迁移、表单契约。

### D2 只读 MCP Server（M）

- 显式声明 `mcp>=2,<3`；新建 `mcp_server/`，命令 `ia mcp serve`（stdio）。
- 工具（全部只读，返回值带 `captured_at` / `provider`）：`list_watchlist`、`get_stock_snapshot(ticker, as_of?)`
  （因子层字段 + 最新快照）、`get_bars(ticker, days)`、`list_candidates(status?)`、`get_rule(rule_id)`、
  `list_earnings_events(ticker?)`、`get_data_health`、`search_stocks(q)`；资源 `zcharlie://health`。
- 数据库以只读方式打开（SQLite URI `mode=ro`），不加载 LLM 配置，不注册任何写工具；
  这是第 11 节 LLM 边界的延伸：外部助手可以问，不能改。
- 文档：`operations.md` 加 Claude Desktop 配置片段。
- 测试：`tools/list` 清单快照；每个工具对种子库一例；写操作不存在的断言。

## 7. 依赖与里程碑

```text
M1 地基     A1 ─┬─ A2
                └─ A3
M2 行情     A1 ─ B1 ─ B2        B3（可先行，只依赖已有快照）
M3 财报事件 A1 ─ C1 ─ C2 ─ C3
M4 交互     D1 ─ D2（D2 的 search_stocks 依赖 D1；其余工具可先做）
```

| 里程碑 | 任务 | 完成标志 |
|---|---|---|
| M1 地基 | A1、A2、A3 | 人为断网下东财任务 1 次失败后进入熔断、后续任务秒级 `partial`、健康页可见；`field_sources.yaml` 校验器通过。**已完成（2026-10-09，`3247042`～`49043d4`）**：熔断改为任务级 breaker 触发整 provider 熔断（单次抖动不中断全量采集）；断网验收由离线测试覆盖 |
| M2 行情 | B1、B2、B3 | 交易日 15:10 目标池 300 只收盘价入库；18:30 baostock 版本到达后因子层改用 baostock；观察池与候选页出现迷你图。**已完成（2026-10-09，`0ae821f`～`f3c7cc2`）**：真实腾讯接口在临时库 5/5 入库、单位正确；迷你图浏览器实测无 CSP 报错；全量 300 只的 15:10 实跑待下一个交易日 |
| M3 财报事件 | C1、C2、C3 | 一个披露季内：预告 / 预约 / 主营构成三表有数据；观察池标的新预告产生 `info` 通知文件。**代码完成（2026-10-09，`a560d5a`～`74e66cd`，迁移 024）**：akshare 三函数真实调用各一次落 fixture；预告区间字段 akshare 丢失，改从其分页经过的 datacenter 行读取；披露季实跑待 10 月中 |
| M4 交互 | D1、D2 | 观察池添加支持拼音搜索；Claude Desktop 通过 stdio 列出工具并查询一只观察池。**已完成（2026-10-09，`39fa116`～`d548a3e`、`e531fc5`～`0a8b5b2`，迁移 023）**：字典 5224 只、浏览器实测 `gzmt` → 贵州茅台；MCP 9 个只读工具，JSON-RPC 管道冒烟通过，Claude Desktop 真实配置待用户完成 |

规模：S 约 0.5～1 天，M 约 1～3 天。合计 12 个任务，约 2～3 周代码量。
M2 是 P2 区间检测的前置，建议最先排；M3 可与 P2 并行；M4 独立，随时可做。

## 8. 风险

| 风险 | 处理 |
|---|---|
| 腾讯 / 东财接口格式变更或封锁 | fixture 固定列名与字段位，变更即测试失败；熔断器限制冲击；所有字段都有 baostock 主源 |
| 腾讯 15:10 数据偶有未收盘（集合竞价延迟） | 字段 30 时间校验，未收盘记 `pending`，16:00 补采一次（B2 可加第二个 cron） |
| 同日两 provider 收盘价不一致 | D2 优先级 + `conflict` 标记 + 健康页展示，不自动改值 |
| akshare 函数名 / 列名与预期不符 | C1 第一步实测落 fixture，不符则改用 datacenter 直连（URL 见 funnel-design 15.3 的 mrd 实现） |
| `stock_directory` 全量同步失败 | 保留上次内容，健康页报 stale；搜索退化为仅 DB 已知 listing |
| MCP 暴露敏感数据 | 只读、本机 stdio、不含密钥与 LLM 调用记录；工具清单由测试快照锁定 |

## 9. 执行拆分（2026-10-09）

按文件不重叠原则拆成 6 个可由 sub-agent 独立执行的子计划，执行约定与波次见
[`00-conventions.md`](plans/mrd-intake/00-conventions.md)：

| 子计划 | 任务 | 波次 | 前置 |
|---|---|---|---|
| [01 上游基础设施](plans/mrd-intake/01-provider-infra.md) | A1、A2、A3 | W1 | 无 |
| [03 迷你图](plans/mrd-intake/03-sparkline.md) | B3 | W1 | 无 |
| [02 腾讯收盘快照](plans/mrd-intake/02-tencent-close-quote.md) | B1、B2 | W2 | 01 |
| [05 股票搜索](plans/mrd-intake/05-stock-search.md) | D1 | W2 | 03 |
| [04 东财财报事件](plans/mrd-intake/04-eastmoney-events.md) | C1、C2、C3 | W3 | 01、02 |
| [06 只读 MCP](plans/mrd-intake/06-mcp-server.md) | D2 | W3 | 05（04 可选） |

实际按 01 → 03 → 02 → 05 → 04 → 06 顺序在 main 上串行执行（2026-10-09）。实际迁移：01→022、05→023、04→024。
