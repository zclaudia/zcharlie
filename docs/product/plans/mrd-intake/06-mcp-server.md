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
