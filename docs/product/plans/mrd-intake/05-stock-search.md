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
