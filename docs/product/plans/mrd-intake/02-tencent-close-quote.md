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

## 完成记录（2026-10-09）

| Task | 提交 | 说明 |
|---|---|---|
| 1 腾讯报价客户端 | `ba5554b` feat(providers): Tencent batch close-quote client with PIT guards | `providers/tencent_quote.py`（源链与口径块、字段位、`to_tencent_code` / `from_tencent_code`、`closed_quote`、`TencentQuoteClient`），fixture `tests/fixtures/tencent/quotes_sample.txt`（真实响应）+ `quotes_edge_cases.txt`（构造），`tests/unit/test_tencent_quote.py` 16 例 |
| 2 收盘快照任务 | `b2ee203` feat(snapshots): close_quote job writes Tencent same-day close with baostock-first read precedence | kind `close_quote` + `CloseQuotePayload(slot)`、`snapshots/close_quote.py`、handler / 调度 / 四处 cron 镜像、读取优先级、因子层 conflict、健康页两源日期、`ia snapshot collect-close-quote`、`tests/unit/test_close_quote_job.py` 14 例 |

门禁：`ruff check .`、`mypy src`、`pytest -q -m "not network"` 全部通过；最终 **1394 passed, 6 deselected**（起点 1364，Task 1 后 1380）。`node --check health.js` 通过。

### fixture

- `quotes_sample.txt`：2026-10-09 06:46 CST 用计划给出的 curl 命令实取（`sh600519,sz000001,bj920748,sz000002`，
  GBK → UTF-8 落盘），内容为 2026-10-08 收盘（600519 收 1255.79 / 开 1252.90 / 高 1258.00 / 低 1242.00 /
  量 25168 手 / 额 314541 万元 / 换手 0.20% / PE 19.28，字段 30 = `20261008161457`）。字段 4 昨收 1258.62
  等于 `field_sources.yaml` 已记录的 baostock 2026-09-30 收盘，作为价格口径的交叉校验。
- 当天无停牌 / ST 样本，`quotes_edge_cases.txt` 由真实行**构造**：停牌（现价 0、量 0）、`*ST`、未收盘（14:35）
  各一；测试不触网（respx）。

### 与计划的偏差

1. **分批与熔断的分工**。`fetch_close` 仍按 60 码切请求（61 只 → 2 次，respx 断言），但采集器自己按
   `client.chunk_size` 分块逐次调用，使失败按请求隔离；任务级 `QuoteBreaker`（连续 3 次瞬态失败）按 01 的语义
   `open_now("tencent")` 打开整个 provider，客户端 `record_failure` 只升级已打开的熔断器。全部请求失败且零入库
   时 run 为 `failed`（与 baostock 采集器一致，worker 重试时撞上打开的熔断器即 `partial`）。
2. **补采时段用独立 run**：同一幂等键只会返回 15:10 已完成的 run，因此 `CloseQuotePayload.slot="retry"` 使键带
   `:retry` 后缀；调度任务 `close_quote` / `close_quote_retry` 各自入队，已有 baostock 或 tencent 当日行的标的跳过
   （`baostock_present` / `already_present` 计入 collected）。
3. **范围**：`close_quote_scope` = `scope_members`（目标池）∪ 启用的观察池 ∪ `status=open` 候选 ∪ `holding_item`，
   去重；`scope="watchlist"` 只取用户池；用户池可为空，目标池为空仍抛 `SnapshotPreconditionError`。
   `snapshots/scope.py` 仅放宽了 `scope_members` 的类型注解。
4. **读取优先级**：`series_as_of(provider=None)` 改为按 `MARKET_PROVIDER_PRECEDENCE=("baostock","tencent")`
   逐交易日取第一个存在的来源（未列出的 provider 排最后），`provider=` 也接受序列（限定来源 + 顺序）；新增
   `series_by_provider` 返回每日每来源的最新版本。原先 `provider=None` = 任意来源取最大 version 的语义已被替换。
5. **conflict 的落点**：因子视图（`TARGET_POOL`）改用 `market_precedence(registry)`（`close` 条目的
   primary + fallback）读不复权序列，`resolve_market_bars` 把同日其它来源 `close` 差超 `tolerance`（0.001）记到
   `Bar.conflicts`，`_bar_ref` 把 `provider` 与 `verification="conflict"` / `conflicts` 写进因子 `inputs_ref["market"]`
   —— 复用的是 `FieldResolution` 的 conflict 结构，不是 run 元数据里的 `provider_conflict`，因此健康页「来源冲突」
   区块暂不显示日线冲突（见后续）。`OBSERVATION` 仍只读 iFinD。
6. **03 留下的 `BARS_PROVIDER`** 已切到优先级读取（`/market/{ticker}/bars` 的 `provider` 字段为
   `"baostock,tencent"`），测试同步改为"同日 baostock 优先、仅有腾讯时补当日"。
7. **`settings/loader.py` 未改**：`app_config_from` 整段传递 `schedule.snapshot`，`yaml_import_values` 按 SPECS 枚举，
   新键自动镜像（`test_settings_registry` / `test_settings_store` 通过）。
8. **字段取舍**：`amount` 按计划取字段 37（万元 ×1e4；字段 35 另有精确到元的成交额，未采用）；`pe_ttm` 写字段 39，
   但登记表 `pe_ttm` 条目仍只有 baostock（未与 baostock peTTM 对照，不敢写 calibration）；字段 46 实测为 PB
   （600519 6.25、000001 0.49）但按计划留空。停牌判定先看日期（早于 trade_date → `pending`），再看现价 0 且量 0 →
   `trade_status=0`、OHLC = 昨收；`is_st` 取名称 `ST` / `*ST` 前缀；报价日期晚于 trade_date 但不晚于采集日 →
   `no_data`（接口只提供最近一个交易日）。
9. **登记表** `version` 升到 `2026-10-09.2`；新增 `open` / `high` / `low` / `volume` / `amount`，`close` / `turnover`
   加 `fallback: [tencent]`、`tolerance: 0.001`，tencent 条目均带 `calibration`（2026-10-09 实取）。

### 计划清单之外触碰的文件

`snapshots/scope.py`（注解）、`factors/library.py`（`_bar_ref`）、`snapshots/notifications.py`（kind 标签 +
完整度告警纳入 `close_quote`）、`web/static/health.js`（`KIND_LABELS` 加 `close_quote`，新鲜度区块加「收盘快照」行；
守卫测试 `test_health_labels_cover_api_enums` 要求与 `COLLECTION_KINDS` 一致）、`tests/unit/_snapshot_support.py`
（`runtime_for(close_quote=)`）、既有测试 `test_api_market_bars` / `test_cli_snapshot` / `test_field_sources` /
`test_job_dispatch` / `test_settings_store` / `test_snapshot_schedule`、`tests/fixtures/tencent/quotes_edge_cases.txt`。

### 本机验收

- **未**对真实 `data/app.db` 执行任何命令。2026-10-09 07:14 CST（休市、开盘前，接口提供的是已结束的 2026-10-08
  交易日）在 scratchpad 的临时 SQLite（`create_all` + 手工种 4 只 CSI300 成分 + 1 只观察池北交所股）上实网跑
  `ia snapshot collect-close-quote --date 2026-10-08 --now`：run #1 success 5/5，5 行 `provider=tencent`
  （600519 收 1255.79 / 量 2516800 / 额 3145410000 / 换手 0.002 / PE 19.28，920748 额 138670100），`provider_call`
  一行 `tencent q ok ticker_count=5 run_id=1`，artifact `close-quote-2026-10-08-000.json`；`--retry --now` → run #2
  success，无上游请求（全部 `already_present`）；`ia snapshot status` 显示 tencent 行。
- 未做：15:10 对约 300 只目标池的实跑（需先 `universe_sync`）、浏览器查看健康页（只 `node --check` + 标签守卫测试）、
  18:30 baostock 到达后的实时切换（离线由 `test_series_as_of_prefers_baostock_per_trade_date` /
  `test_factor_view_marks_same_day_close_conflicts` 覆盖）。

### 后续

- 健康页「来源冲突」区块显示日线 `close` 冲突（目前只在因子 `inputs_ref` 中）。
- 某交易日 18:30 后用 baostock 当日行校准腾讯字段 39（PE）与 46（PB），再给登记表 `pe_ttm` / `pb_mrq` 加 tencent 兜底。
- `amount` 可改取字段 35 的精确元值（小盘股按万元取整时相对误差可接近 0.1%）。
- `test_research_worker_robustness::test_worker_heartbeats_while_runner_executes` 在一次全量跑中超时失败（计时类，
  单独与再次全量均通过），与本计划无关。
