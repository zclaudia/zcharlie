# mrd 引入 · 03 SVG 日线迷你图（B3）

> 先读 [`00-conventions.md`](00-conventions.md)。波次 W1，可与 01 并行。无前置（只读已有 baostock 日线）。

**目标：** 自选股行、候选卡、股票页顶部显示最近 120 个交易日（股票页 240 日）的收盘价迷你图，
手写 SVG，不引入图表库，满足 CSP 与静态守卫；预留 P2 区间带参数。

**决策：** D7（120 个交易日、不复权、baostock 版本；`bands` 参数占位）。

## 代码事实

- `web/static/ui.js`：`svg(tag, attributes, text)` 用拆分后的命名空间字符串（line 7）；现有 `scale` / `bar` / `ring` 都用百分比坐标与 CSS class；`dom.js` 导出 `el`。
- 自选股行 `web/static/app.js:167 watchlistRow`（`stockCell` 146，加载 `loadWatchlist` 117）；候选卡 `web/static/candidates.js:186 candidateItem`；股票页 `web/templates/stock.html`（`#stock-detail`）与 `app.js:877 loadStockDetail`。
- 日线读取：`storage/repositories/market_snapshot.py::series_as_of(listing_id, start, end, as_of_captured, *, adjust="none", provider=None, include_backfill=True)`；listing 查找 `storage/repositories/listing.py`。
- 路由样板：`api/routes/watchlist.py`（鉴权依赖、schema 在 `api/schemas/`）；路由注册在 `api/app.py`。
- 守卫：不得出现 `http://`、`style=`、innerHTML；`candidates.js` 不得出现 `score` / `strength`；新 JS 文件须加进 `test_web_client_contract.py:106` 清单。样式在 `web/static/app.css`。

## 文件清单

```
新增  src/investment_agent/api/routes/market.py          （GET /market/{ticker}/bars）
新增  src/investment_agent/api/schemas/market.py
新增  tests/integration/test_api_market_bars.py
修改  tests/integration/test_web_client_contract.py（契约：ui.js 导出 sparkline、app.js / candidates.js 调用 /market/）
修改  src/investment_agent/api/app.py（注册路由）
修改  src/investment_agent/web/static/ui.js（sparkline）、app.js（自选股行 + 股票页）、candidates.js（候选卡）、app.css
修改  src/investment_agent/web/templates/stock.html（迷你图容器）
```

## Task 1：日线接口

- [ ] `GET /market/{ticker}/bars?days=120`（`ticker` 校验同股票页正则 `\d{6}\.(SH|SZ|BJ)`；`days` 20～400）：
  as_of 取当前时刻，`adjust="none"`，`provider="baostock"`（01/02 合并后可改为 `None` 走优先级，本计划不做）；
  返回 `{ticker, provider, as_of, bars: [{trade_date, close}]}`，无 listing → 404，无数据 → 空数组。
- [ ] 测试：种子两只股票 150 根日线，断言条数 / 顺序 / 未来 `captured_at` 的行被 as_of 过滤；404。
- [ ] 提交：`feat(api): GET /market/{ticker}/bars for sparkline rendering`

## Task 2：`sparkline` 组件与挂载

- [ ] `ui.js` 导出 `sparkline(points, { width = 120, height = 32, bands = [] })`：
  `<svg class="spark" viewBox="0 0 100 30" preserveAspectRatio="none" aria-hidden="true">`；
  `bands` 每项 `{from, to, kind}` 画 `rect.spark-band.spark-band-<kind>`；折线 `polyline.spark-line`（`vector-effect="non-scaling-stroke"`）；
  末点 `circle.spark-last`；涨跌 class `spark-up` / `spark-down`（首尾比较）；点数 < 2 返回空占位 `span.spark-empty`。
  坐标用 0～100 / 0～30 的数值字符串，保留两位小数。
- [ ] `app.css`：`.spark` 尺寸、线宽、颜色 token（沿用现有红涨绿跌变量）、`.spark-band` 半透明。
- [ ] 挂载：
  - `app.js::watchlistRow` 增加一列容器，`loadWatchlist` 后按行异步 `get('/market/<ticker>/bars?days=120')` 并替换（并发上限 6，顺序无关）；
  - `candidates.js::candidateItem` 卡片右上角同样处理；
  - `stock.html` 增加 `#stock-spark` 容器，`app.js::loadStockDetail` 加载 240 日，`height = 80`。
- [ ] 契约测试：`ui.js` 含 `export function sparkline`；`app.js` / `candidates.js` 引用 `/market/`；守卫测试与 `node --check` 通过。
- [ ] 提交：`feat(web): SVG sparkline on watchlist rows, candidate cards and stock page`

## 验收

- 本机 `ia api serve` 后打开 `/app/watchlist`：每行出现迷你图；`/app/stocks/600519.SH` 顶部出现 240 日曲线；控制台无 CSP 报错。
- 门禁三条通过；写"完成记录"。
