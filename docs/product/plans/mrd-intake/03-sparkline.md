# mrd 引入 · 03 SVG 日线迷你图（B3）

> 先读 [`00-conventions.md`](00-conventions.md)。波次 W1，可与 01 并行。无前置（只读已有 baostock 日线）。

**目标：** 观察池行、候选卡、股票页顶部显示最近 120 个交易日（股票页 240 日）的收盘价迷你图，
手写 SVG，不引入图表库，满足 CSP 与静态守卫；预留 P2 区间带参数。

**决策：** D7（120 个交易日、不复权、baostock 版本；`bands` 参数占位）。

## 代码事实

- `web/static/ui.js`：`svg(tag, attributes, text)` 用拆分后的命名空间字符串（line 7）；现有 `scale` / `bar` / `ring` 都用百分比坐标与 CSS class；`dom.js` 导出 `el`。
- 观察池行 `web/static/app.js:167 watchlistRow`（`stockCell` 146，加载 `loadWatchlist` 117）；候选卡 `web/static/candidates.js:186 candidateItem`；股票页 `web/templates/stock.html`（`#stock-detail`）与 `app.js:877 loadStockDetail`。
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
修改  src/investment_agent/web/static/ui.js（sparkline）、app.js（观察池行 + 股票页）、candidates.js（候选卡）、app.css
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

## 完成记录（2026-10-09）

- 提交：`0ae821f` feat(api): GET /market/{ticker}/bars for sparkline rendering（Task 1）；
  `02e74d3` feat(web): SVG sparkline on watchlist rows, candidate cards and stock page（Task 2）。
- 门禁：ruff / mypy 通过；`pytest -m "not network"` 1364 passed（Task 1 后 1362，基线 1349）。
- 本机验收：临时 SQLite（独立 DATA_DIR / DATABASE_URL，未碰 `data/app.db`）种 3 只股票各 260 根 baostock 日线，
  `ia api serve --port 8765` 后在内置浏览器查看：`/app/watchlist` 每行出现 120 日迷你图（红涨绿跌），
  `/app/stocks/600519.SH` 与无研究快照的 `/app/stocks/300750.SZ` 顶部均出现 240 日曲线（高 80），
  手动提名的候选卡右上角出现迷你图；窄屏堆叠布局正常；控制台无 CSP 或其他报错。

### 偏离与说明

- 接口 `ticker` 不合法返回 422（而非 404），与 `days` 越界一致；大小写不敏感（`600519.sh` 可用）。
- 取数窗口：`end = shanghai_today()`，`start = end − (days×2 + 30) 天`，再取 `series_as_of` 结果的尾部 `days` 根；
  provider 固定 `baostock`、`adjust="none"`，按计划不走优先级。
- 页面不发请求的 `ui.js` 新增了 `fillSparklines(targets, load, {width, height, bands, limit = 6})`：
  读 `[data-spark="<ticker>"]` 占位、并发上限 6、失败 / 无数据留 `span.spark-empty`；`load(ticker)` 由 `app.js` /
  `candidates.js` 注入（它们各自保留 `/market/<ticker>/bars?days=` 字面量，满足契约），因此没有新建 JS 文件。
- 末点 `circle.spark-last` 用半径 0.01 + `vector-effect="non-scaling-stroke"` 的 5px 描边画成圆点，
  避免 `preserveAspectRatio="none"` 拉伸成椭圆；首尾相等时 class 为 `spark-flat`。
- `app.css` 原无红涨绿跌变量（文件头明确"不用红绿表达好坏"），新增 `--c-spark-up / --c-spark-down / --c-spark-flat /
  --c-spark-band`（含暗色值），只表达价格方向。`.spark` 尺寸由 svg 的 `width` / `height` 属性给出，股票页传 `width: "100%"`。
- 股票页迷你图与研究快照无关：`loadStockDetail` 先挂载迷你图再处理快照；`boot` 里改为只要存在 `#stock-detail`
  就调用 `loadStockDetail(region, latest)`（latest 为 null 时原有分支仅隐藏详情区），无快照的股票也能看到曲线。
  `stock.html` 的 `#stock-spark` 带 `data-ticker`，放在研究表单下方独立卡片内。
- 契约测试除源码标记外，增加了用 node 假 `document` 渲染 `sparkline()` 并断言 SVG 结构（class、viewBox、
  points 坐标、band / polyline / circle 顺序、空占位）的用例。

### 计划清单之外触碰的文件

- 无。未改 `api/routes/watchlist.py`、`health.*`、`test_web_candidates.py` 或其他子计划文件。
