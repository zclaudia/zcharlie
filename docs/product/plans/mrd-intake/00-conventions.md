# mrd 引入计划 · 00 执行约定（所有子计划共用）

> 子计划位于 `docs/product/plans/mrd-intake/`（已跟踪，worktree 内可见）。每个子计划（01～06）都是自包含的执行单元，可交给一个 sub-agent 独立完成。执行前先读本文件，再读对应子计划。
> 总计划与决策：[`docs/product/mrd-intake-plan.md`](../../mrd-intake-plan.md)（状态 Ready，决策 D1～D7 已确认）。

## 执行波次与并行约束

| 波次 | 可并行的子计划 | 依赖 |
|---|---|---|
| W1 | 01 上游基础设施 ‖ 03 迷你图 | 无 |
| W2 | 02 腾讯收盘快照 ‖ 05 股票搜索 | 02 依赖 01；05 依赖 03（同改 `app.js` / `candidates.js`） |
| W3 | 04 东财财报事件 ‖ 06 MCP | 04 依赖 01、02（同改 `snapshots/jobs.py` 等）；06 依赖 05 |

同一波次内两个子计划**不触碰同一文件**；跨波次按顺序执行。并行执行时各用独立 worktree，合并到 `main` 后再启动下一波。

## 仓库事实

- Python ≥ 3.13，`.venv` 已就绪；依赖在 `pyproject.toml`，开发依赖组 `dev`。无 Makefile。
- 门禁（三条都必须通过，与 `.github/workflows/ci.yml` 一致）：

```bash
.venv/bin/ruff check . && .venv/bin/mypy src && API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 .venv/bin/pytest -q -m "not network"
```

- ruff：`line-length=100`，规则 `E F I N UP B SIM ASYNC`；mypy strict。
- 测试：`tests/unit/`、`tests/integration/`，fixture 在 `tests/fixtures/<provider>/`；`asyncio_mode=auto`；
  `tests/conftest.py` 的 autouse fixture `_no_real_akshare` 禁止真实 akshare 调用（`@pytest.mark.network` 例外）。
  HTTP 级 mock 用 `respx`；时间用 `freezegun`。
- 迁移：`alembic/versions/NNN_snake.py`，`revision: str = "NNN"`，每个迁移配 `tests/integration/test_migration_0NN.py`。
  最新为 `021`。**执行时以当时最新编号 +1 为准**，子计划里的编号只是预期。
- 运行时设置：新 env 设置键须同时出现在 `config.py::Settings` 与 `settings/registry.py::RuntimeSettings` + `SPECS`
  （`tests/unit/test_settings_registry.py` 强制镜像）；cron 键在 `config/app.yaml` + `config.py` 的 `*ScheduleConfig` +
  `registry.py` 的 `schedule.*` spec + `settings/loader.py::app_config_from`。
- 队列任务：kind 常量在 `snapshots/jobs.py::SNAPSHOT_KINDS`，payload 类带 `idempotency_key()`；
  调度门控在 `scheduler/snapshot_jobs.py::snapshot_payloads`；处理器由 `snapshots/handlers.py` 的
  `snapshot_job_handlers(settings)` 组装，`research/worker.py::default_job_handlers` 注册；
  完成度 / `partial` 语义在 `snapshots/collect.py`（`Completeness`、`CollectionReport.stop_for_provider`）。
- Web：Jinja 壳 `web/templates/*.html` + ES module `web/static/*.js`；CSP 禁内联样式 / 脚本（`api/app.py:38-41`）。
  守卫测试（`tests/integration/test_web_candidates.py:120-157`）：禁 `style=` / `<style`、禁 innerHTML 族、
  import 必须 `./x.js`、源码不得出现 `http://` 或 `https://` 字面量；`candidates.js` / `format.js` / `health.js`
  不得出现 `score` / `strength`。新 JS 文件必须加进 `tests/integration/test_web_client_contract.py:106` 的清单。
  SVG 用 `ui.js` 的 `svg(tag, attrs, text)`（命名空间字符串已拆分，line 7），DOM 用 `dom.js` 的 `el`。
- PIT 纪律：所有写入带 `provider` 与 `captured_at`；晚于采集日的数据抛 `providers/errors.py::LookaheadDataError`；
  快照行不可变，重采追加 `version`。
- 日志 `structlog`；禁止在产品文案与 JS 中出现交易建议 / 打分措辞（见守卫测试）。

## 提交约定

- 每个 Task 一次提交，信息用子计划给出的文本；结尾加 `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`。
- 直接在 `main`（或该子计划的 worktree 分支）工作，不开 PR；完成后在子计划末尾的"完成记录"写上提交哈希与测试数。
- 不改本计划之外的产品行为；发现前置假设不成立时停下来写进"完成记录"，不要绕路修改他人文件。
