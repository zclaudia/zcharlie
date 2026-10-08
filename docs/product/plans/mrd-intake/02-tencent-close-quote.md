# mrd 引入 · 02 腾讯收盘快照（B1 客户端 · B2 任务与读取优先级）

> 先读 [`00-conventions.md`](00-conventions.md)。波次 W2，可与 05 并行。**前置：01 已合并**（需要 `CircuitBreaker` 与 `provider_ledger_hook`）。

**目标：** 交易日 15:10 用腾讯批量报价把目标池 + 观察池 + 候选 + 持仓的当日收盘价写入 `market_snapshot`
（`provider = tencent`），把 P2 日频区间判定的可用时点从次日提前到当日 15:10；baostock 18:30 版本到达后因子层改用 baostock。

**决策：** D1（只做收盘快照，不做盘中）、D2（baostock 优先，tencent 兜底，差超 0.1% 记 `conflict`）。

## 代码事实

- `market_snapshot`：`storage/models/pit_snapshot.py:71-111`，唯一 `(listing_id, trade_date, adjust, version)`；`provider` / `captured_at` 不在键内；仓库 `storage/repositories/market_snapshot.py`：`insert(...)`（23-85）、`series_as_of(listing_id, start, end, as_of_captured, *, adjust, provider=None, include_backfill)`（87-148）、`latest_trade_dates`。
- 目标池日线采集样板 `snapshots/breadth.py:194-314 collect_baostock_market`（`store_daily_bars` 159-191）；`captured_at` 来自客户端时钟。
- kind 与 payload：`snapshots/jobs.py:42-56`（`SNAPSHOT_KINDS`、`SnapshotProvider = Literal["baostock","ifind","akshare"]`、`MarketSnapshotPayload` 在 135-142 拒绝 akshare）；`enqueue_snapshot_job` 333-354。
- 调度：`scheduler/snapshot_jobs.py:60-75`（`SNAPSHOT_SCHEDULE_JOBS`、`CALENDAR_FREE_JOBS`）、`snapshot_payloads` 160-203；`scheduler/runner.py:462-524 add_jobs`、`_snapshot_triggers` 362-368；cron 在 `config/app.yaml:25` 与 `config.py:149-185 SnapshotScheduleConfig`、`settings/registry.py:122-133 / 288-291` 镜像。
- 范围：`snapshots/scope.py:25 scope_members(...)`。
- httpx 客户端样板：`research/ifind_chain.py::IfindProxyClient`（可注入 `transport`、`on_call`）。
- 代码解析：`parse_a_share_ticker`（watchlist 路由处引用）。
- 健康页市场新鲜度：`snapshots/health.py:145 MarketFreshness`。
- 腾讯接口（mrd 实测，`server/lib/tencent-urls.cjs` / `sources/tencent.cjs`）：`GET https://qt.gtimg.cn/q=sh600519,sz000001`，GBK，每行 `v_sh600519="…"`，`~` 分隔；字段位：1 名称、3 现价、4 昨收、5 开、6 成交量（手）、30 时间 `YYYYMMDDHHMMSS`、31 涨跌、32 涨跌幅、33 高、34 低、37 成交额（**万元**）、38 换手率、39 PE、43 振幅；60 码一批；需 UA 头。

## 文件清单

```
新增  src/investment_agent/providers/tencent_quote.py
新增  src/investment_agent/snapshots/close_quote.py
新增  tests/fixtures/tencent/quotes_sample.txt            （真实响应，含停牌、ST、北交所各一）
新增  tests/unit/test_tencent_quote.py、tests/unit/test_close_quote_job.py
修改  src/investment_agent/snapshots/jobs.py、scheduler/snapshot_jobs.py、scheduler/runner.py
修改  config/app.yaml、src/investment_agent/config.py、settings/registry.py、settings/loader.py
修改  src/investment_agent/snapshots/handlers.py、snapshots/health.py
修改  config/field_sources.yaml（market 表字段的 primary / fallback）、factors/field_sources.py（market 表解析）
修改  src/investment_agent/storage/repositories/market_snapshot.py（provider 优先级读取）
修改  docs/product/operations.md、docs/product/settings-plan.md（新 cron 键）
```

## Task 1：腾讯报价客户端（B1）

- [ ] `providers/tencent_quote.py`：`PROVIDER = "tencent"`；`TencentQuoteClient(clock, *, transport=None, on_call=None, circuit=None, timeout_seconds=8, chunk_size=60)`；`async fetch_close(tickers: Sequence[str], trade_date: date) -> QuoteBatch`。
  - 代码映射 `600519.SH → sh600519`、`SZ → sz`、`BJ → bj`；响应按 `v_<code>=` 解析；缺行 → `missing`。
  - 返回每只 `ClosedQuote(ticker, trade_date, open, high, low, close, volume_shares（手×100）, amount_cny（万元×1e4）, turnover, pe, time)`。
  - PIT 守卫：字段 30 的日期 > 采集日 → `LookaheadDataError`；日期 < trade_date 或时间 < 15:00:00 → 该标的 `pending`（未收盘 / 停牌当日无成交时按 `trade_status` 处理：现价 0 且量 0 视为停牌，写 `trade_status=0`）。
  - 文件头写"源链与口径"块（单位、字段位、实测日期）。
- [ ] 错误映射：网络 / 5xx / 超时 → `ProviderTransientError`；4xx / 解析失败 → `ProviderDataError`；经 `circuit.check/record_*` 与 `on_call`。
- [ ] fixture：用 `curl -s --compressed -H 'User-Agent: Mozilla/5.0' 'https://qt.gtimg.cn/q=sh600519,sz000001,bj920748,sz000002' | iconv -f gbk -t utf-8` 取一份真实响应落盘（执行者自取；若网络不可用，标注并用 mrd 字段位构造）。
- [ ] 测试：解析字段位；分批（61 只 → 2 次请求，respx 断言）；未收盘 `pending`；晚于采集日抛错；停牌。
- [ ] 提交：`feat(providers): Tencent batch close-quote client with PIT guards`

## Task 2：收盘快照任务与调度（B2）

- [ ] `snapshots/jobs.py`：新 kind `CLOSE_QUOTE = "close_quote"` 加入 `SNAPSHOT_KINDS`；`SnapshotProvider` 加 `"tencent"`；`CloseQuotePayload(provider="tencent", scope, universe_code, trade_date)` + `idempotency_key`。
- [ ] `snapshots/close_quote.py::collect_close_quotes(payload, *, client, now, lease, artifacts)`：范围 = `scope_members` 的目标池 ∪ 观察池 ∪ 候选 ∪ 持仓（去重）；先用 `latest_trade_dates(provider="baostock")` 跳过 baostock 已有当日行的标的；写 `market_snapshot`（`provider="tencent"`、`adjust="none"`、`amount` 换算为元、`pe_ttm` 取字段 39、pb / ps 留空、`captured_at` 取客户端时钟、`raw_ref` 指向 artifact）；`pending` 记入 `Completeness.mark_missing(kind="pending")`。
- [ ] `snapshots/handlers.py`：注册 handler，`TencentQuoteClient` 挂熔断器与台账钩子。
- [ ] 调度：`SNAPSHOT_SCHEDULE_JOBS` 加 `close_quote`；`snapshot_payloads` 交易日门控同 `market_snapshot`；cron 键 `snapshot.close_quote_cron: "10 15 * * 1-5"`（app.yaml / config.py / registry.py / loader.py 四处镜像）；第二次补采 `snapshot.close_quote_retry_cron: "0 16 * * 1-5"`（同 payload，幂等键含日期，已完成的标的跳过）。
- [ ] 读取优先级：`config/field_sources.yaml` 新增 `table: market` 条目 `close`、`open`、`high`、`low`、`volume`、`amount`、`turnover`（primary baostock，fallback tencent，tolerance 0.001，`calibration` 填实测日期）；`MarketSnapshotRepository.series_as_of` 在 `provider=None` 时按同一 `trade_date` 取 baostock 版本，缺失才取 tencent；两者同日 `close` 差超 0.1% 时在 `factors` 层记 `conflict`（复用现有 conflict 机制）。
- [ ] 健康页：`MarketFreshness` 增加 `latest_close_quote_date`；`market_stale` 判定不变（仍按 baostock）。
- [ ] CLI：`ia snapshot collect-close-quote [--date] [--now]`（仿 `collect-market`，`cli/commands/snapshot.py:271-293`）。
- [ ] 测试：payload 幂等键；交易日 / 非交易日门控；写入与版本追加；baostock 已有当日行时跳过；同日冲突标记；`ProviderUnavailableError` → `partial`；settings 镜像测试通过。
- [ ] 提交：`feat(snapshots): close_quote job writes Tencent same-day close with baostock-first read precedence`

## 验收

- 本机某交易日 15:10 后：`ia snapshot collect-close-quote --now` → 目标池约 300 只入库；`ia snapshot status` 可见 tencent 行；18:30 baostock 到达后 `series_as_of` 返回 baostock 版本；健康页显示两源日期。
- 门禁三条通过；写"完成记录"。
