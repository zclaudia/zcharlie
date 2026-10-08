# mrd 引入 · 05 股票名称 / 拼音搜索（D1）

> 先读 [`00-conventions.md`](00-conventions.md)。波次 W2，可与 02 并行。**前置：03 已合并**（同改 `app.js` / `candidates.js`）。

**目标：** 本地股票字典（全部 A 股，每周随 `sync-universe` 刷新），`GET /stocks/search` 三路匹配（代码前缀 / 名称子串 / 拼音首字母），
自选股添加与候选提名输入改为 combobox。

**决策：** D4（本地 `stock_directory` + baostock `query_all_stock` + `pypinyin`；不接新浪 / 东财 suggest）。

## 代码事实

- baostock 参考客户端 `providers/baostock_reference.py`（`query_hs/sz/zz`、`query_stock_industry`，3 次线性重试，PIT 校验 626 / 632）；本机已核实 `baostock.query_all_stock(day)` 存在（返回当日全部证券 `code, tradeStatus, code_name`，含指数与基金，需按 `sh.6 / sz.0 / sz.3 / bj.` 前缀过滤为股票）。
- `sync-universe`：`cli/commands/snapshot.py`，kind `UNIVERSE_SYNC`，采集器 `snapshots/reference.py`。
- 名称当前来自 `Company.canonical_name`（`storage/models/reference.py:34-62`）；自选股添加 `api/routes/watchlist.py:54`，body `WatchlistAdd`（`api/schemas/admin.py:50-73`，无 listing 且无快照时必须给 `name`）；前端 `app.js:248 wireWatchlistForm`；候选提名 `candidates.js:301 normalizeTicker`。
- 路由注册 `api/app.py`；守卫与 JS 清单见 00。

## 文件清单

```
新增  src/investment_agent/storage/models/stock_directory.py、storage/repositories/stock_directory.py
新增  alembic/versions/024_stock_directory.py + tests/integration/test_migration_024.py
新增  src/investment_agent/snapshots/directory.py        （同步逻辑 + 拼音）
新增  src/investment_agent/api/routes/stocks.py          （GET /stocks/search）
新增  src/investment_agent/web/static/combobox.js        （加进 node --check 清单）
新增  tests/unit/test_stock_directory.py、tests/integration/test_api_stock_search.py
修改  pyproject.toml（`pypinyin>=0.50`；安装用 `.venv/bin/pip install -e ".[dev]"`，不要运行裸 `uv run`，仓库里的 uv.lock 未跟踪且不是真源）
修改  src/investment_agent/providers/baostock_reference.py（`fetch_all_stocks(day)`）
修改  src/investment_agent/snapshots/reference.py（universe_sync 末尾刷新字典）
修改  src/investment_agent/snapshots/health.py（字典 `synced_on` 新鲜度，> 14 天 warning）
修改  src/investment_agent/api/app.py、web/static/app.js、candidates.js、app.css、web/templates/watchlist.html
```

## Task 1：字典表与同步 · 迁移 024

- [ ] 模型 `stock_directory`：`ticker`（主键，`600519.SH` 格式）、`name`、`pinyin_initials`、`status`（`listed | suspended | delisted`）、`synced_on`（date）。迁移 + 迁移测试。
- [ ] `providers/baostock_reference.py::fetch_all_stocks(day)`：调用 `query_all_stock`，过滤股票前缀，映射代码格式；PIT：`day` 不得晚于采集日。
- [ ] `snapshots/directory.py::sync_directory(session, client, *, today)`：全量 upsert（不删除，消失的标记 `delisted`）；`pypinyin.lazy_pinyin(name, style=Style.FIRST_LETTER)` 生成首字母（去掉 `*ST` 的符号、`ST` 保留）；返回 `{total, added, changed}` 写入 run metadata。
- [ ] `snapshots/reference.py` universe_sync 成功后调用一次；失败不影响 universe_sync 状态，只记 warning。
- [ ] `health.py`：`DirectoryHealth(synced_on, total)`，> 14 天发 warning。
- [ ] 测试：过滤与映射；拼音（含 `*ST`、多音字取首字母即可）；upsert / delisted；健康页。
- [ ] 提交：`feat(reference): stock_directory synced weekly from baostock with pinyin initials`

## Task 2：搜索接口

- [ ] `GET /stocks/search?q=&limit=10`（鉴权同其他 API）：`q` 去空格，匹配顺序：代码前缀（6 位数字或含 `.`）> 名称子串 > 拼音首字母前缀；每项 `{ticker, name, status, in_universe, on_watchlist}`；`in_universe` 用当前目标池成分，`on_watchlist` 用 watchlist 表；`q` 为空返回 `[]`。
- [ ] 测试：三路匹配与排序；大小写；limit；空查询。
- [ ] 提交：`feat(api): GET /stocks/search over the local stock directory`

## Task 3：combobox

- [ ] `web/static/combobox.js` 导出 `attachCombobox(input, { fetch, onSelect, minChars = 1 })`：输入防抖 200ms → `get('/stocks/search?q=…')`；下拉用 `el('ul', {class:'combo-list', role:'listbox'})`；上下键 / 回车 / Esc；选中回填 `ticker` 并触发 `onSelect(item)`。
- [ ] 自选股表单：选中后自动填 `name`，`ticker` 校验规则不变；候选提名输入同样接入。
- [ ] `app.css` 下拉样式；`combobox.js` 加入 `test_web_client_contract.py:106` 清单；守卫通过。
- [ ] 提交：`feat(web): combobox stock search on watchlist add and candidate nomination`

## 验收

- 本机 `ia snapshot sync-universe --now` 后 `stock_directory` 约 5000 行；输入 `gzmt` 下拉出现贵州茅台；选中后 `name` 自动填充并能提交。
- 门禁三条通过；写"完成记录"。

## 完成记录（2026-10-09）

| Task | 提交 | 说明 |
|---|---|---|
| 1 字典表与同步 | `39fa116` feat(reference): stock_directory synced weekly from baostock with pinyin initials | 迁移 **023**（计划预期 024；按"最新 +1"），`storage/models/stock_directory.py` + `repositories/stock_directory.py`、`baostock_reference.py::fetch_all_stocks` / `is_stock_code` / `DirectorySnapshot`、`snapshots/directory.py`（`sync_directory`、`pinyin_initials`、回退找交易日）、`reference.py::sync_universe` 末尾 `_refresh_directory`、`health.py::DirectoryHealth`（挂在 `freshness.directory`）、`health.js` 新鲜度区块「股票字典」行、fixture `tests/fixtures/baostock/all_stock.json`、`pyproject.toml` 加 `pypinyin>=0.50` |
| 2 搜索接口 | `ed63af2` feat(api): GET /stocks/search over the local stock directory | `api/routes/stocks.py`、`api/schemas/stocks.py`、`tests/integration/test_api_stock_search.py` 6 例 |
| 3 combobox | `5026ed6` feat(web): combobox stock search on watchlist add and candidate nomination | `web/static/combobox.js`（`attachCombobox(input, {fetch, onSelect, minChars})`）、`app.js` / `candidates.js` 接入、`app.css` `.combo*`、两个模板的占位文案，契约测试加 `combobox.js` 清单 + 标记检查 + node 假 DOM 行为用例 |

门禁：`ruff check .`、`mypy src`、`pytest -q -m "not network"` 全部通过；最终 **1438 passed, 6 deselected**（起点 1394，Task 1 后 1429，Task 2 后 1435）。`node --check` combobox.js / app.js / candidates.js / health.js 通过。pypinyin 0.55 自带 `.pyi`，mypy strict 无需 override。

### 实测事实（2026-10-09 07:22 CST 实网一次，用于 fixture）

- `baostock.query_all_stock(day="2026-10-08")` 返回 **7423 行**，字段 `code, tradeStatus, code_name`，**没有 `updateDate`**；4 页 × 2000。
  前缀分布：`sh.0` 指数 221、`sh.5` 基金 943、`sh.6` 2320、`sz.0` 1495、`sz.1` 基金 749、`sz.3` 1695（其中 **`sz.399` 指数 286**）；**没有任何 `bj.` 行**（baostock 不覆盖北交所）；`tradeStatus=0` 15 行。
- 同一调用对 **非交易日（10-04 / 10-01）、当天开盘前（10-09 07:23）、未来日期、`day=""`** 都返回 0 行（`error_code=0`）。
- fixture 只保留手选 37 行（指数 / ETF / 主板 / 科创 / 创业板 / ST / 停牌各若干）+ 一条 10-09 的空答复，文件内 `note` 记录了全量统计。

### 与计划的偏差

1. **股票前缀过滤**改为 `sh.6 / sz.00 / sz.30 / bj.`（计划写 `sz.0 / sz.3`）：`sz.399xxx` 是深证指数，按 `sz.3` 会混入 286 个指数。`bj.` 保留但目前无数据。
2. **PIT 校验只能守请求日**：答复没有日期，`fetch_all_stocks(day)` 在 `day > 采集日` 时先抛 `LookaheadDataError`（不发请求）；答复为空视为空快照而非错误。
3. **交易日回退**：因非交易日 / 开盘前返回空，`sync_directory(session, client, *, today)` 在 `client.session()` 内从 `today` 起逐日回退（最多 `MAX_LOOKBACK_DAYS=10`，覆盖国庆周），全空则 `ProviderDataError`；`synced_on` = 实际答复的交易日。周一 08:00 的 `universe_sync` 因而会落到上周五的名单。
4. **`today` 的来源**：`sync_universe(..., today=None)` 新增关键字参数，handler 传 `runtime.clock()` 的上海日期（测试可确定），默认 `shanghai_today()`；没有用 `payload.as_of`（回填历史成分时不应把字典同步到旧日期）。`ReferenceClient` Protocol 加了 `session()` 与 `fetch_all_stocks()`。
5. **失败隔离**：`_refresh_directory` 捕获 `Exception`（含 `ProviderUnavailableError` / 假后端缺方法的 `AttributeError`），只记 warning，metadata 写 `directory: {"error": ...}`；取消异常不吞。
6. **表多了 `provider` / `captured_at` 两列**（00 约定"所有写入带 provider 与 captured_at"），并给 `pinyin_initials` 加索引；`upsert_all` 对未变化的行也刷新 `synced_on` / `captured_at`，`changed` 只数名称 / 首字母 / 状态变化，另返回 `delisted` 计数。
7. **健康页落点**：`DirectoryHealth(synced_on, total, age_days)` 放在 `freshness.directory`（不改 `/health/data` 顶层键清单与页面区块清单）；未同步发 `info` `directory_missing`，`age_days > 14` 发 `warn` `directory_stale`，section 都是 `freshness`。
8. **搜索细节**：`q` 最长 32，`limit` 1～50（默认 10）；代码分支的判定是 `^\d{1,6}(\.[A-Za-z]{0,2})?$`，拼音分支只对纯字母查询；三路各取 `limit` 后按顺序去重；组内 `listed > suspended > delisted` 再按代码；LIKE 通配符用 `autoescape` 转义。`in_universe` 取运行时设置里目标池第一项的当日成分（`members_as_of`），`on_watchlist` 不区分启用 / 停用。
9. **combobox**：`fetch` 默认即 `get('/stocks/search?q=…&limit=10')`；回车在下拉打开且有结果时选中高亮项（无高亮取第一项）并阻止提交，无结果时放行提交；列表 `mousedown` 阻止默认以免 blur 先关闭；`form reset` 时关闭。自选股表单选中后**覆盖**填入 `name`；候选提名只回填 ticker，`normalizeTicker` 不变。`candidates.html` 的 `pattern` 保留（输入拼音未选中就点提交会被浏览器拦下）。
10. **`api/schemas/stocks.py`** 新增（计划清单里没有，沿用 `schemas/market.py` 的放法）；`stocks.py` 复用了 `data_health._snapshot_universes`（与 `data_health` 引 `dashboard._APP_CONFIG_PATH` 同一做法）。

### 计划清单之外触碰的文件

`alembic/env.py`、`tests/conftest.py`（登记模型）；`tests/integration/test_backup_restore.py`、`test_funnel_e2e.py`（head `022` → `023`，e2e 健康检查加字典断言）；`tests/integration/_funnel_support.py`（`SyntheticBaostock.query_all_stock`）；`tests/unit/test_baostock_reference.py`（`FakeBaostock.query_all_stock` + 4 个取数 / 过滤用例）；`tests/unit/test_snapshot_reference.py`（假后端加 `query_all_stock`，成分同步用例断言字典 metadata，新增"字典失败不影响成分同步"用例）；`tests/unit/test_data_health.py`（空库期望加 `directory_missing`）；`web/templates/candidates.html`（占位文案 + `autocomplete="off"`）；`api/schemas/stocks.py`。

### 本机验收

- **未**对真实 `data/app.db` 执行任何命令。scratchpad 临时 SQLite：`ia migrate up` 到 023，用实网全量答复（7423 行）回放 `sync_directory` → **5224 行**（`sh.6` 2320 + `sz.00` 1495 + `sz.30` 1409），`as_of=2026-10-08`。
- 内置浏览器（`ia api serve --port 8765`，临时 launch 配置已还原）：`/app/watchlist` 输入 `gzmt` 下拉出现「600519.SH 贵州茅台」，↓ + 回车后代码框 `600519.SH`、名称框自动填「贵州茅台」，填行业后提交成功，列表出现该行；`/app/candidates` 输入 `cjdl` 出现「600900.SH 长江电力」，输入「长江」出现 5 条、鼠标点击回填 `000783.SZ` 且焦点留在输入框；`/app/health` 新鲜度区块出现「股票字典 同步于 2026-10-08（1 天前） 5224 只」。控制台除 favicon 404 外无报错，无 CSP 告警。
- 未做：真实 `ia snapshot sync-universe --now` 跑完整 universe_sync（需 worker；字典部分已用同一份实网答复回放）。

### 后续

- 06 MCP 子计划可直接复用 `StockDirectoryRepository.search` / `GET /stocks/search`。
- 北交所标的目前搜不到（baostock 无 `bj.` 行）；若需要可从另一源补字典。
- `on_watchlist` 可考虑区分停用项；下拉可展示 `status`/目标池标签以外的更多上下文（当前只有标签）。
