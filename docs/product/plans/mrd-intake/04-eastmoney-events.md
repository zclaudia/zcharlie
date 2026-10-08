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
