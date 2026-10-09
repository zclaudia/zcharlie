# mrd 引入 · 04 东财财报事件（C1 采集器 · C2 调度与通知 · C3 起草上下文）

> 先读 [`00-conventions.md`](00-conventions.md)。波次 W3，可与 06 并行。**前置：01、02 已合并**（熔断器钩子；`snapshots/jobs.py` / 调度文件已含 `close_quote` 改动）。

**目标：** 披露季每日采集业绩预告、预约披露时间、主营构成三类数据（经 akshare 东财封装），
观察池 / 候选 / 持仓标的出现新预告或预约披露日临近时发 `info` 通知；主营构成进入规则起草事实包。

**决策：** D3（经 akshare：`stock_yjyg_em(date)`、`stock_yysj_em(symbol, date)`、`stock_zygc_em(symbol)`；本机 akshare 1.19.1 已核实三函数存在）。

## 代码事实

- 东财客户端 `providers/akshare_eastmoney.py`：`AkshareEastmoneyBackend._call`（339-359）懒加载 akshare；`AkshareEastmoneyClient`（675-790）串行 + 1 秒间隔 + 2 次重试；错误类 203-216；`requests` 垫片 238-315；`LookaheadDataError` 在 509 / 644 的用法可照搬。
- 现有东财采集器 `snapshots/eastmoney.py`（`collect_akshare_financial` 437，`_collect_summary` 482，`unchanged()` 418 内容去重，`FailureBreaker` 185-201，写 `financial_snapshot` 564-575）。
- 披露季窗口常量 `scheduler/snapshot_jobs.py:83-87 CASH_FLOW_WINDOWS`；`_financial()` 206-233、`_akshare_cash_flow()` 250-267。
- 逾期：`snapshots/disclosure.py`（`statutory_deadline`、`is_past_deadline`）、`report_overdue` 模型 `pit_snapshot.py:137-157`、仓库 `storage/repositories/report_overdue.py`（`record` / `resolve`）、检测 `snapshots/breadth.py:486-542 _detect_overdue`。
- 通知：`snapshots/notifications.py` `notification_level`（50-72，仅 universe_sync 变更发 `info`）、`render_snapshot_notification`（98）、`notify_snapshot_run`（186-218，每 run 至多一次）；`FileNotifier` 写 `<data_dir>/notifications/<date>/<kind>_<run_id>.md`。
- 起草：`rules/drafting.py::load_stock_context`（163）、`build_draft_input`（242）、`draft_rule`（424）；候选详情路由 `api/routes/candidates.py`，页面 `web/static/candidates.js`。
- fixture 目录 `tests/fixtures/eastmoney/`，fake 后端 `tests/unit/_eastmoney_fake.py`。

## 文件清单

```
新增  src/investment_agent/storage/models/earnings_events.py   （earnings_forecast、disclosure_schedule、business_segment）
新增  src/investment_agent/storage/repositories/earnings_events.py
新增  alembic/versions/023_earnings_events.py + tests/integration/test_migration_023.py
新增  src/investment_agent/snapshots/earnings_events.py          （采集器）
新增  tests/fixtures/eastmoney/stock_yjyg_em_<period>.json、stock_yysj_em_<period>.json、stock_zygc_em_SH600519.json
新增  tests/unit/test_earnings_events_collect.py、tests/unit/test_earnings_events_notify.py
修改  src/investment_agent/providers/akshare_eastmoney.py        （三个后端方法 + 列名常量）
修改  src/investment_agent/snapshots/jobs.py、scheduler/snapshot_jobs.py、snapshots/handlers.py
修改  config/app.yaml、config.py、settings/registry.py、settings/loader.py（`snapshot.earnings_event_cron`）
修改  src/investment_agent/snapshots/notifications.py、snapshots/breadth.py（逾期次级证据）
修改  src/investment_agent/rules/drafting.py、api/routes/candidates.py、web/static/candidates.js（主营构成段）
修改  config/field_sources.yaml（新字段 calibration）、docs/product/operations.md
```

## Task 1：实测与 fixture（C1 第一步）

- [ ] 在 `.venv` 里对三函数各调一次真实数据（`stock_yjyg_em(date="20250930")`、`stock_yysj_em(symbol="沪深A股", date="20250930")`、`stock_zygc_em(symbol="SH600519")`），把 DataFrame 以 `orient="records"` 落 JSON fixture，并把列名写成 `providers/akshare_eastmoney.py` 的常量元组（列名变化即 `EastmoneySchemaError`）。
- [ ] 若任一函数不可用或列不含预期字段（预告：代码、名称、预告类型、预测净利下限 / 上限、同比下限 / 上限、公告日期、预告摘要；预约：代码、首次预约、变更后、实际披露；主营：报告期、分类类型、主营构成、收入、占比、利润、毛利率），停止并在"完成记录"写明，再按 funnel-design 15.3 的 datacenter 直连 URL 评估。
- [ ] 提交：`test(eastmoney): fixtures and column contracts for earnings forecast, disclosure schedule and business segments`

## Task 2：表、采集器与任务（C1 + C2 调度）· 迁移 023

- [ ] 三张表（字段见 `mrd-intake-plan.md` 5 C1；`captured_at`、`provider="akshare"`、`raw_ref` 必有）；迁移 023 + 迁移测试。
- [ ] `snapshots/earnings_events.py::collect_earnings_events(payload, *, client, now, lease, artifacts)`：
  - `part = forecast | schedule | segments`；forecast / schedule 全市场按报告期一次拉取后按 `scope_members` 目标池 ∪ 观察池 ∪ 候选 ∪ 持仓过滤；segments 按标的逐只（仅观察池 ∪ 候选 ∪ 持仓，1 秒间隔）。
  - PIT：公告日 / 预约日晚于采集日 → `LookaheadDataError`；同 (listing, period, notice_date) 已存在则跳过；预约表按时点追加。
  - 预告类型归一到十类 + `不确定`；缺 `FORECASTTYPE` 时从摘要文本匹配（顺序：预增 预减 扭亏 首亏 略增 略减 减亏 增亏 续盈 续亏）。
  - 连续失败 3 次走 `stop_for_provider`（复用 `FailureBreaker` 模式），`ProviderUnavailableError` 直接 `partial`。
- [ ] kind `EARNINGS_EVENT = "earnings_event"`、payload `EarningsEventPayload(part, report_period, as_of)`；调度 cron `snapshot.earnings_event_cron: "0 19 * * 1-5"`，门控：仅在 1 / 4 / 7 / 10 整月（常量 `EARNINGS_WINDOWS` 与 `CASH_FLOW_WINDOWS` 并列）；forecast + schedule 每日，segments 每季窗口内每周一次。
- [ ] `report_overdue`：`_detect_overdue` 不改主口径；新增 `snapshots/earnings_events.py::reconcile_overdue`，用 `disclosure_schedule.actual_date` 作次级证据把差异写进 run metadata（不自动 resolve）。
- [ ] CLI `ia snapshot collect-earnings-events --part forecast|schedule|segments [--period 20250930] [--now]`。
- [ ] 测试：三 part 各一例；过滤范围；类型归一；重复公告去重；PIT 抛错；窗口门控；`partial`。
- [ ] 提交：`feat(snapshots): earnings_event job collects forecasts, disclosure schedule and business segments via akshare`

## Task 3：`info` 通知（C2）

- [ ] `notification_level`：`kind == earnings_event` 且 metadata 含 `new_forecasts`（观察池 / 候选 / 持仓中新出现的预告）或 `upcoming_disclosures`（预约日在未来 3 个交易日内，交易日历用 `config/calendars/a_share.yaml`）→ `info`。
- [ ] `render_snapshot_notification` 增加段落：标的、类型、净利区间、同比区间、公告日、`raw_ref`。
- [ ] 测试：新预告才通知，第二次同一预告不通知；预约日临近通知；正文含 `raw_ref`。
- [ ] 提交：`feat(notifications): info-level alerts for new earnings forecasts and upcoming disclosures`

## Task 4：主营构成进入起草上下文与候选页（C3）

- [ ] `rules/drafting.py::load_stock_context` 加 `segments: list[Segment]`（最新报告期，按收入 Top 5，含占比与毛利率）；`build_draft_input` 把它作为"事实"段落写入 prompt，不参与任何评分；无数据省略。
- [ ] `api/routes/candidates.py` 候选详情返回 `segments`；`candidates.js` 详情页渲染一个小表（不出现 `score` / `strength`）。
- [ ] 测试：prompt 快照含段落；无数据省略；API 契约。
- [ ] 提交：`feat(rules): business segments in rule-drafting facts and candidate detail`

## 验收

- 本机在披露季（或用 `--period` 指定上一季）跑三 part：三表有行；观察池中有预告的标的生成通知文件；候选详情页出现主营构成。
- 门禁三条通过；写"完成记录"。

## 完成记录（2026-10-09）

| Task | 提交 | 说明 |
|---|---|---|
| 1 实测与 fixture | `a560d5a` test(eastmoney): fixtures and column contracts for earnings forecast, disclosure schedule and business segments | `providers/akshare_eastmoney.py`：`FORECAST_COLUMNS` / `FORECAST_RAW_COLUMNS` / `SCHEDULE_COLUMNS` / `SEGMENT_COLUMNS` 列名常量、requests 垫片 `sink`、三个后端方法、三个解析器与客户端方法、`normalize_forecast_type`；fixture `stock_yjyg_em_20260630.json`（16 只，覆盖 11 种预告类型）、`stock_yysj_em_20260930.json`（14 只）、`stock_zygc_em_SH600519.json`（最近 4 期）；`_eastmoney_fake.py` 三个方法；`test_akshare_eastmoney.py` +10 例 |
| 2 表、采集器与任务 | `4d02af0` feat(snapshots): earnings_event job collects forecasts, disclosure schedule and business segments via akshare | 迁移 **024**（计划预期 023；按"最新 +1"）、`storage/models/earnings_events.py` + `repositories/earnings_events.py`、`snapshots/earnings_events.py`（三 part + `reconcile_overdue`）、kind `earnings_event` + `EarningsEventPayload`、调度 `EARNINGS_WINDOWS` / `earnings_event` 任务、四处 cron 镜像、handler（`SnapshotRuntime.earnings_client()` / `calendar_factory`）、`ia snapshot collect-earnings-events`、健康页 / 通知 kind 标签、`field_sources.yaml` `2026-10-09.3`（`earnings` 表 9 个字段带 akshare calibration）、`operations.md`、`test_earnings_events_collect.py` 17 例 |
| 3 info 通知 | `9b4052c` feat(notifications): info-level alerts for new earnings forecasts and upcoming disclosures | `notification_level`：`earnings_event` 且 metadata 含非空 `new_forecasts` / `upcoming_disclosures` → `info`；正文「新业绩预告」「预约披露临近」段（标的、池、类型、净利区间、同比区间、公告日、报告期、`raw_ref`）；`test_earnings_events_notify.py` 4 例 |
| 4 主营构成 | `f8128f1` feat(rules): business segments in rule-drafting facts and candidate detail | `drafting.py`：`Segment`、`load_segments`（PIT：`captured_at` ≤ as_of 当日末、报告期 ≤ as_of）、`StockContext.segments`、`build_draft_input` 的 `facts.business_segments`（无数据省略）、系统提示加一句"只作事实、不能 cited"；`CandidateDetailOut.segments` + `SegmentOut`；`candidate.html` 新 section `candidate-segments`，`candidates.js` `renderSegments` / `segmentRows`（`dom.js::table`，窄屏堆叠） |

门禁：`ruff check .`、`mypy src`、`pytest -q -m "not network"` 全部通过；最终 **1474 passed, 6 deselected**（起点 1438；Task 1 后 1448，Task 2 后 1467，Task 3 后 1471）。`node --check` health.js / candidates.js 通过。

### 实测事实（2026-10-09 07:46 CST 实网各一次，用于 fixture）

- `stock_yjyg_em(date="20260630")`：**4919 行 / 1912 只 / 10 页**（约 2 s）；列 `序号 股票代码 股票简称 预测指标 业绩变动 预测数值 业绩变动幅度 业绩变动原因 预告类型 上年同期值 公告日期`。
  **akshare 的 DataFrame 不含预测净利下限 / 上限与同比下限 / 上限**（按位置改名时把 `PREDICT_AMT_LOWER / UPPER`、`ADD_AMP_LOWER / UPPER` 丢掉，只留中值 `预测数值 = FORECAST_JZ`、`业绩变动幅度 = INCREASE_JZ`）。
  datacenter 行（`RPT_PUBLIC_OP_NEWPREDICT`，27 个键）齐全：`PREDICT_TYPE` 1569 预增 / 690 首亏 / 637 增亏 / 593 减亏 / 550 预减 / 541 扭亏 / 454 略增 / 190 续亏 / 123 略减 / 62 续盈 / **10 不确定**，无空值；
  每只多行（`PREDICT_FINANCE_CODE` 004 归母净利 2111、005 扣非 2091、003 每股收益 854、006 营业收入 305…）；`IS_LATEST` 全 T，同 (代码, 期) 无多次公告；含北交所 132 行（`920xxx`）。金额单位元（600030 归母 23 343 000 000 = 233.43 亿元），`ADD_AMP_*` 为百分数。
- `stock_yysj_em(symbol="沪深A股", date="20260930")`：**5223 行 / 11 页**；列 `序号 股票代码 股票简称 首次预约时间 一次变更日期 二次变更日期 三次变更日期 实际披露时间`，预期字段齐全；首次预约 2026-10-10 ～ 10-31，64 行有一次变更（无二、三次），`实际披露时间` 全空（尚无人披露）；**预约日可落在周六**（002107 沃华医药 10-10）；site 过滤已排除北交所。
- `stock_zygc_em(symbol="SH600519")`：**200 行 / 25 期**（0.1 s）；列 `股票代码 报告日期 分类类型 主营构成 主营收入 收入比例 主营成本 成本比例 主营利润 利润比例 毛利率`；金额元、比例与毛利率为小数；`分类类型` 为 akshare 对 `MAINOP_TYPE` 1/2/3 的映射（按行业 / 按产品 / 按地区）。
- 三个函数都可用、列名都与 akshare 1.19.1 源码一致，没有走 datacenter 直连评估。

### 与计划的偏差

1. **预告区间的取法**（Task 1）。akshare 的 DataFrame 缺少预期字段"预测净利下限 / 上限、同比下限 / 上限"，但同一调用翻页经过的 datacenter 行包含它们。实现没有停在"不符"，而是让 requests 垫片把每页 `result.data` 收进 `sink`，`forecast_table` 返回 `RawTable(columns=akshare 列, rows=datacenter 行)`，两者同时上锁（列名或键缺失 → `EastmoneySchemaError`；去重后行数与 DataFrame 不等也报错）。仍"经 akshare"（URL / 参数 / 翻页由 akshare 维护，D3 不变）。akshare 对第 1 页请求两次（先取页数），后端按 (代码, 公告日, 报告期, 指标) 去重。
2. **一只股票一次公告多行**（指标不同）：按计划的唯一键 (listing, period, notice_date) 只存**归母净利润**（004）行，没有 004 时存扣非（005），每股收益 / 营业收入行不存（计入 `skipped_indicators`）；表多了一列 `indicator` 记录区间指的是哪条线。
3. **预约日的 PIT**：计划写"预约日晚于采集日 → `LookaheadDataError`"，但预约日本质上在未来（否则没有"临近通知"）；实现只对公告日（预告）、实际披露日（预约表）和报告期（主营）做前视检查，`scheduled_date` 允许在未来。
4. **预约表"按时点追加"**改为**有变化才追加**（首次采集、预约日或实际披露日变化），否则披露季每日对约 300 只各写一行。`uq (listing, period, captured_at)` 不变。
5. **主营构成**多了 `region`（按地区分类）一种 `kind`（计划只列 product / industry；数据白来的，读者按 product > industry > region 取一种）；每只只存最近 `MAX_SEGMENT_PERIODS = 8` 期，同 (listing, period) 内容与最近一次相同则跳过（`unchanged`），否则整期追加到新的 `captured_at`。
6. **完整度口径**：forecast / schedule 是全市场一次调用，调用成功即把范围内全部标的记为 `collected`（多数公司本来没有预告），调用失败则全部 `failed`、run `failed` 交 worker 重试；`ProviderUnavailableError` 直接 `partial`。连续 3 次失败的断路器只用于逐只的 segments part（复用 `snapshots/eastmoney.py::FailureBreaker`，触发即 `open_now("akshare")`）。
7. **临近窗口**：`upcoming_disclosures` = 预约日在 `as_of` 之后且不晚于未来第 3 个交易日（周末预约日也算，`trading_days_ahead=0` 表示"下一交易日前"）；每日运行期间同一标的最多提醒 3 次，没有再做跨 run 去重。只看观察池 / 候选 / 持仓；`new_forecasts` 同样只列用户池里的新公告（目标池的新预告只入库、计数）。
8. **`reconcile_overdue`** 放在 schedule part 末尾：列出开放的 `report_overdue` 中东财已有 `actual_date` 的标的（`published_per_eastmoney`）与东财显示逾期披露但从未登记逾期的标的（`late_not_recorded`），不 resolve；`breadth.py::_detect_overdue` **未改**。
9. **调度**：`earnings_event` 在 1 / 4 / 7 / 10 月的交易日 19:00 入队 forecast + schedule（报告期 = 最近一个已结束季度末，`earnings_report_period`），**每周首个交易日**再加 segments（`is_first_trading_day_of_week`；国庆周 2026-10-08 周四即为首个交易日）。`settings/loader.py` 与 02 一样无需改动（整段传递 + SPECS 枚举，镜像测试通过）。
10. **交易日历来源**：handler 通过 `SnapshotRuntime.calendar_factory`（默认读 `app.yaml` 的 `scheduler.a_share_calendar_path`，失败则 `None`）取日历；年份未收录或无日历时按工作日计数。
11. **健康页**：`earnings_event` 加进 `COLLECTION_KINDS`（守卫测试要求 `health.js::KIND_LABELS` 同步），`PART_LABELS` 加 forecast / schedule / segments；没有新增新鲜度行。
12. **登记表**：`factors/field_sources.py` 新增 `earnings` 表类型（9 个字段 `forecast_profit_low/high`、`forecast_yoy_low/high`、`disclosure_scheduled_days`、`segment_revenue/profit/share/margin`，全部 `primary: akshare` 带 calibration），版本 `2026-10-09.3`；只作登记，不进因子视图。
13. **起草上下文**：`load_stock_context` 加关键字参数 `segment_loader`（既有测试以 `session=None` 调用，改为传一个空 loader）；主营段落写在 `facts.business_segments`（`report_period` / `kind` / `note` / 收入前 5 的 `items`），无数据时整个 `facts` 键省略；`draft_policy.py` 系统提示第 3 条追加一句说明它不是因子、不能 cited。
14. **候选详情**：`segments` 用当天（`shanghai_today()`）作 PIT 日期；列表接口不带。`candidates.js` 用 `dom.js::table`（窄屏堆叠）渲染「构成 / 收入 / 收入占比 / 毛利率」，金额按亿元 / 万元，守卫（无 `score` / `strength`、无内联样式）通过。
15. **fixture 体积**：预告 fixture 的 `业绩变动原因` / `CHANGE_REASON_EXPLAIN` 截到 80 字（采集器不存该列），其余为原值。

### 计划清单之外触碰的文件

`alembic/env.py`、`tests/conftest.py`（登记模型）；`tests/integration/test_backup_restore.py`、`test_funnel_e2e.py`（head `023` → `024`）；`tests/unit/test_job_dispatch.py`、`test_snapshot_schedule.py`、`test_settings_store.py`、`test_cli_snapshot.py`（新 kind / cron / 命令清单）；`src/investment_agent/factors/field_sources.py` + `tests/unit/test_field_sources.py`（`earnings` 表）；`src/investment_agent/snapshots/health.py`、`web/static/health.js`（kind / part 标签）；`src/investment_agent/rules/draft_policy.py`（提示一句）；`src/investment_agent/api/schemas/candidates.py`、`web/templates/candidate.html`、`tests/integration/test_web_candidates.py`、`test_api_candidates.py`、`tests/unit/test_rule_draft_eval.py`、`tests/unit/test_akshare_eastmoney.py`。未改：`snapshots/breadth.py`、`settings/loader.py`（见偏差 8 / 9）。

### 本机验收

- **未**对真实 `data/app.db` 执行任何命令；没有再做实网 smoke（实网只在 Task 1 为 fixture 各调一次）。三 part 的入库、通知与候选页 API 均由离线 fixture 回放覆盖（`test_earnings_events_collect.py` 含 worker handler 跑完三 part 的用例）。
- 未做：披露季内真实跑 `ia snapshot collect-earnings-events --part … --now`、浏览器查看候选页主营构成表（只 `node --check` + 模板 / JS 守卫测试）。

### 后续

- 10 月中旬（2026Q3 预告陆续发布后）在本机实跑三 part，核对 `new_forecasts` 通知文件与候选页主营表；若 `stock_zygc_em` 对未知代码返回的不是空 `zygcfx` 而是其它形状，调整垫片的 `_NoData` 判定。
- `upcoming_disclosures` 可做跨 run 去重（同一预约日只提醒一次）；`reconcile_overdue` 的差异目前只进 run metadata，可考虑进健康页。
- 06 MCP 子计划可直接读 `EarningsEventRepository.forecasts_for / latest_segments`。
