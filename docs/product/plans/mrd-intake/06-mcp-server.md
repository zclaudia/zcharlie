# mrd 引入 · 06 只读 MCP Server（D2）

> 先读 [`00-conventions.md`](00-conventions.md)。波次 W3，可与 04 并行。**前置：05 已合并**（`search_stocks` 复用其仓库）。

**目标：** `ia mcp serve`（stdio）向 Claude Desktop 等 MCP 客户端暴露只读工具与资源，
让外部助手能查询自选股、快照、候选、规则、财报事件与数据健康，但不能写、不触发 LLM。

**决策：** D5（stdio、只读、SQLite `mode=ro`、不挂 HTTP）。

## 代码事实

- `mcp` 2.3.0 已随 `pydantic-ai` 间接安装（`.venv` 可 `import mcp`），`pyproject.toml` 未声明 → 显式加 `mcp>=2,<3`。
- CLI：`cli/main.py` 用 `add_typer` 注册子命令组，各组在 `cli/commands/<name>.py`；引擎与设置加载样板见 `cli/commands/snapshot.py::_run / run_with_engine`，`load_settings(base=bootstrap)`。
- 数据读取可复用：自选股 `api/routes/watchlist.py` 的查询逻辑（抽成服务函数后共用）、因子视图 `factors/`（`field_sources` 解析）、日线 `MarketSnapshotRepository.series_as_of`、候选 `api/routes/candidates.py`、规则库 `rules/`、健康 `api/routes/data_health.py::compute_data_health`、搜索（05）`stock_directory` 仓库、财报事件（04）仓库（若 04 未合并，`list_earnings_events` 先不实现并在完成记录注明）。
- 数据库 URL 在 `config.py::Settings`（SQLite，aiosqlite）；只读打开用 URI `sqlite+aiosqlite:///file:<path>?mode=ro&uri=true`。
- 运维文档 `docs/product/operations.md`。

## 文件清单

```
新增  src/investment_agent/mcp_server/__init__.py、server.py（工具与资源）、readonly.py（只读引擎）
新增  src/investment_agent/cli/commands/mcp.py
新增  tests/unit/test_mcp_tools.py
修改  pyproject.toml（`mcp>=2,<3`；`ia mcp` 入口无需改 script）、src/investment_agent/cli/main.py
修改  docs/product/operations.md（Claude Desktop 配置片段）、README.md（一段）
```

## Task 1：只读引擎与服务骨架

- [ ] `mcp_server/readonly.py::readonly_engine(settings)`：把 `database_url` 改写为 `mode=ro` URI；非 SQLite 时抛 `NotImplementedError`（说明只支持本机 SQLite）。
- [ ] `mcp_server/server.py`：用 `mcp.server` 的低阶 `Server`（或 `FastMCP`，取 2.x 文档推荐；执行者以 `.venv` 内 `mcp` 实际 API 为准，先用 `python -c "import mcp, inspect"` 核对），`serverInfo.name = "zcharlie"`；不加载 LLM 设置（`load_settings` 后不读取 `LLM_*`）。
- [ ] `ia mcp serve`：stdio 运行；`ia mcp tools` 打印工具清单（便于测试与文档）。
- [ ] 提交：`feat(mcp): read-only stdio MCP server skeleton`

## Task 2：工具与资源

- [ ] 工具（全部只读；返回 JSON，字段带 `captured_at` / `provider` / `as_of`）：
  - `list_watchlist()`；
  - `get_stock_snapshot(ticker, as_of?: date)`：因子层字段 + 最新 market / financial 快照摘要 + 行业；
  - `get_bars(ticker, days = 120)`；
  - `list_candidates(status?)`、`get_candidate(candidate_id)`（含理由与规则草稿摘要）；
  - `get_rule(rule_id)`；
  - `list_earnings_events(ticker?, limit = 50)`（依赖 04）；
  - `get_data_health()`（复用 `compute_data_health`）；
  - `search_stocks(q, limit = 10)`（复用 05）。
- [ ] 资源：`zcharlie://health`（同 `get_data_health`）。
- [ ] 输入校验：`ticker` 正则 `\d{6}\.(SH|SZ|BJ)`，`days` 20～400，`limit` ≤ 200；错误以 MCP error 返回，不泄漏路径与密钥。
- [ ] 测试：`tools/list` 名称快照（锁定清单）；每个工具对 `db_session` 种子库一例；断言没有任何工具名含 `set|add|update|delete|run|draft`；只读引擎对写语句抛错。
- [ ] 提交：`feat(mcp): read-only tools for watchlist, snapshots, candidates, rules, earnings events and health`

## Task 3：文档

- [ ] `operations.md` 新节"MCP 只读接入"：Claude Desktop `claude_desktop_config.json` 片段（command 为 `.venv/bin/ia`，args `["mcp","serve"]`，env `DATABASE_URL` / `API_KEY` 等最小集合）、边界说明（只读、无 LLM、无推送）。
- [ ] README 一段。
- [ ] 提交：`docs(mcp): Claude Desktop setup for the read-only MCP server`

## 验收

- 本机：Claude Desktop 配置后能列出工具并查询一只自选股的快照与 120 日日线；`ia mcp tools` 输出与测试快照一致。
- 门禁三条通过；写"完成记录"。

## 完成记录（2026-10-09）

| Task | 提交 | 说明 |
|---|---|---|
| 1 只读引擎与骨架 | `e531fc5` feat(mcp): read-only stdio MCP server skeleton | `pyproject.toml` 加 `mcp>=2,<3`（已装 2.3.0）；`mcp_server/readonly.py`：`readonly_url`（`sqlite+aiosqlite:///file:<绝对路径>?mode=ro&uri=true`，非 SQLite / 内存库抛 `NotImplementedError`）、`readonly_engine(settings)`（私有引擎，连接时 `busy_timeout` + `PRAGMA query_only`）、`readonly_sessions`（只关闭不提交）；`mcp_server/server.py::build_server`（`MCPServer(name="zcharlie")`）；`cli/commands/mcp.py`：`ia mcp serve` / `ia mcp tools`；`cli/main.py` 注册 |
| 2 工具与资源 | `38c89aa` feat(mcp): read-only tools for watchlist, snapshots, candidates, rules, earnings events and health | 9 个工具 + 资源 `zcharlie://health`；复用 API 的服务函数（见下）；`test_mcp_tools.py` 35 例 |
| 3 文档 | `2610e42` docs(mcp): Claude Desktop setup for the read-only MCP server | `operations.md` 新节「7. MCP 只读接入（D5）」（原「已知限制」改为第 8 节），README CLI 表加 `ia mcp` 行 + 新节「MCP 只读接入」 |

门禁：`ruff check .`、`mypy src`、`pytest -q -m "not network"` 全部通过；最终 **1509 passed, 6 deselected**（起点 1474；Task 1 后 1482，Task 2 后 1509，Task 3 不改代码）。

### SDK 事实

`mcp` 2.3.0 没有 `mcp.server.fastmcp`（导入时提示已更名）；用 `mcp.server.mcpserver.MCPServer`：`@server.tool()` 以函数签名 + pydantic `Annotated[..., Field(...)]` 生成 `inputSchema`，`dict[str, Any]` 返回值同时作为 `structuredContent` 与 JSON 文本；参数校验失败与 `ToolError` 在 stdio 上都变成 `isError: true` 的结果，其它异常被 SDK 替换为固定文案「Error executing tool <name>」（不泄漏路径 / 密钥）；`run_stdio_async()` 在服务期间把 fd 1 转到 stderr，杂散 stdout 日志不会污染协议。测试直接调 `server.list_tools()` / `call_tool()` / `read_resource()`，不起进程。

### 复用的服务函数（API 行为与测试不变）

- `api/routes/watchlist.py`：`_to_out` 改名为公开的 `watchlist_item_out`（`list_watchlist` 工具 = `GET /watchlist` 同一函数）。
- `api/routes/market.py`：路由体抽出 `load_bars(session, listing_id, days, *, as_of)`（含窗口计算与 `rows[-days:]`）；`get_bars` 返回每根 K 线的 `provider` / `captured_at` / `is_backfill`（API 的 `MarketBar` 仍只有 `trade_date` / `close`）。
- `api/routes/candidates.py`：`_Templates` / `_candidate_summary` / `_candidate_detail` / `_rule_detail` 改为公开的 `TemplateCache` / `candidate_summary_out` / `candidate_detail_out` / `rule_detail_out`（纯改名）。
- `api/routes/stocks.py`：路由体抽出 `search_directory(session, settings, q, *, limit)`。
- `rules/drafting.py`：`load_stock_context` 的行业段抽出 `industry_context(view)`；`get_stock_snapshot` = `load_snapshot_view` + `DEFAULT_REGISTRY.compute_all` + `factor_input` + `industry_context` + `load_segments` / `segments_input`（与起草 prompt 同一口径），另加最新日线（`latest_bar`，含 `provider` / `ref` / `conflicts` / `stale_price` 旗标）与最新报告期（`FinancialPeriod.describe` + `fields`）。
- `storage/repositories/earnings_events.py`：新增 `recent_forecasts` / `recent_schedules(*, listing_id=None, limit)`（04 只有按 listing 的 `forecasts_for` 与按期的 `latest_schedules`；跨标的「最新事件」需要新查询）。
- 健康：`api/routes/data_health.py::compute_data_health` 原样复用；`settings` 用 `load_settings(session, base=Settings())` 在只读会话上取运行时阈值（只读表；工具不读任何 `llm_*` 字段）。

### 与计划的偏差

1. **`readonly_engine` 不走 `storage/database.py` 的进程级引擎**：`configure_sqlite_engine` 连接时执行 `PRAGMA journal_mode = WAL`，对非 WAL 文件的只读连接直接报 `attempt to write a readonly database`（实测）。只读引擎自建（`create_async_engine` + 自己的 connect 监听），`session_scope()` 在 MCP 进程内保持未初始化，任何误经进程级引擎的写路径都会失败；`build_server(settings, sessions)` 接收会话工厂，测试用 `db_session` 直接喂入。
2. **`ia mcp serve --project-dir`**（计划没有）：MCP 客户端不设工作目录，而 `.env`、`config/app.yaml`、交易日历与默认 `DATABASE_URL` 都是相对路径；smoke 从 scratchpad 目录启动时健康工具报 `calendar_unavailable`。加该选项（`exists=True`）并写进 Claude Desktop 片段；`serve` 还把 stdout 日志处理器改到 stderr（`configure_logging` 默认写 stdout）。
3. **`list_candidates` 多一个 `limit`（默认 20，≤ 200）**；`status` 用正则限定四种。`get_stock_snapshot` 的 `as_of` 缺省为上海当日；`list_earnings_events` 返回 `forecasts` + `disclosures` 两组（预约表为每次捕获一行，`captured_at` 降序）。
4. **`ticker` 参数严格大写**（`^\d{6}\.(SH|SZ|BJ)$`，HTTP 接口会 `upper()`）；`search_stocks` 的 `limit` ≤ 200（HTTP 为 ≤ 50），`q` ≤ 32 字。
5. **Decimal 列显式转 float**（`to_jsonable_python` 默认把 Decimal 序列化为字符串）。
6. 未改 `storage/database.py`、未开 HTTP、未注册 prompt。

### 计划清单之外触碰的文件

`src/investment_agent/api/routes/watchlist.py`、`market.py`、`candidates.py`、`stocks.py`（抽服务函数 / 改名）、`src/investment_agent/rules/drafting.py`（`industry_context`）、`src/investment_agent/storage/repositories/earnings_events.py`（两个查询）。

### 本机验收

- **未**对真实 `data/app.db` 执行任何命令。scratchpad 临时 SQLite（`create_all` + 1 只自选股 + 25 根日线）：把 `initialize` / `notifications/initialized` / `tools/list` / `tools/call`（`list_watchlist`、`get_bars` 正常与非法 ticker、`get_stock_snapshot`）/ `resources/read zcharlie://health` 八行 JSON-RPC 管道送入 `env -i … ia mcp serve --project-dir <checkout>`：stdout 只有 7 行 JSON-RPC 应答（`tools/list` 9 个工具、非法 ticker 得 `isError` 结果），日志全部在 stderr，数据库文件 SHA-256 前后一致；只读连接会在旁边建 `-shm` / 0 字节 `-wal`（WAL 库的 SQLite 常规行为）。
- `ia mcp tools` 输出与测试快照一致（CLI 测试覆盖）。
- 未做：真实 Claude Desktop 配置与对话验证（需用户在本机写 `claude_desktop_config.json` 并重启）。

### 后续

- Claude Desktop 实配后若需要「按日期范围取日线」或「候选决策历史分页」，在现有服务函数上加参数即可；`list_earnings_events` 可补主营构成（已有 `latest_segments`，目前只在 `get_stock_snapshot` 里）。
- Docker 部署下数据库在容器卷内，MCP 进程读不到；可考虑 `ia mcp serve` 的 HTTP/SSE 变体（D5 明确不做）或定时导出只读副本。
