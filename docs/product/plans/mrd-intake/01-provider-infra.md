# mrd 引入 · 01 上游基础设施（A1 熔断器 · A2 调用台账与健康页 · A3 口径记录）

> 先读 [`00-conventions.md`](00-conventions.md)。波次 W1，可与 03 并行。无前置。

**目标：** 给所有外部数据源一个跨任务的熔断器（指数退避 + 负缓存），一张通用的上游调用台账，
健康页按 provider 展示 24h 调用 / 失败 / 熔断状态；`field_sources.yaml` 开始记录口径校准信息。

**决策：** D6（熔断状态放 worker 进程内存，不建状态表；状态变化写台账）。

## 代码事实

- 现有错误层级 `src/investment_agent/providers/errors.py`：`ProviderError(message, *, provider, error_code=None)` →
  `ProviderTransientError`、`ProviderDataError`（→ `LookaheadDataError`）、`QuotaExhaustedError`。
- 瞬态判定 `research/retry_policy.py:65 is_transient_error`。
- 现有单任务熔断：`snapshots/breadth.py:90-110 TransientBreaker`（连续 5 次）、`snapshots/eastmoney.py:185-201 FailureBreaker`（连续 3 次，触发 `report.stop_for_provider`，`collect.py:257`）。
- 客户端重试循环：`providers/akshare_eastmoney.py:763-790`（`AkshareEastmoneyClient._call`，`asyncio.Lock` 串行，`self.calls` 计数）、`providers/baostock_market.py` 与 `providers/baostock_reference.py` 的 3 次线性重试。
- iFinD 台账：`storage/models/verification.py:44-56 IfindCallLedger`，`verification/budget.py` 的 `record_call` / `ledger_hook`（best-effort `on_call` 钩子，`snapshots/handlers.py:113-121` 挂到 `IfindProxyClient`）。**不要改它**。
- 健康页：模型 `snapshots/health.py:329-342 DataHealth`，构建 `build_data_health`（1366-1505），告警 `derive_warnings`（1164）；路由 `api/routes/data_health.py`；前端 `web/static/health.js`（`HEALTH_REGIONS`，`bootHealth`）。
- 字段登记表 `config/field_sources.yaml`（版本 `2026-10-07.2`），加载与校验 `src/investment_agent/factors/field_sources.py`。

## 文件清单

```
新增  src/investment_agent/providers/circuit.py
新增  src/investment_agent/storage/models/provider_call.py
新增  src/investment_agent/storage/repositories/provider_call.py
新增  alembic/versions/022_provider_call.py           （编号以执行时最新 +1 为准）
新增  tests/unit/test_provider_circuit.py
新增  tests/unit/test_provider_call_ledger.py
新增  tests/integration/test_migration_022.py
修改  src/investment_agent/providers/errors.py        （ProviderUnavailableError）
修改  src/investment_agent/providers/akshare_eastmoney.py / baostock_market.py / baostock_reference.py（接熔断器 + on_call）
修改  src/investment_agent/snapshots/breadth.py / eastmoney.py（现有 breaker 触发时打开熔断器）
修改  src/investment_agent/snapshots/handlers.py      （构造熔断器与台账钩子，注入客户端）
修改  src/investment_agent/snapshots/health.py、api/routes/data_health.py、web/static/health.js
修改  config/field_sources.yaml、src/investment_agent/factors/field_sources.py、tests/unit/test_field_sources*.py
修改  docs/product/operations.md（熔断与台账说明一小节）
```

## Task 1：熔断器（A1）

- [ ] `providers/errors.py` 加 `ProviderUnavailableError(ProviderTransientError)`，属性 `open_until: datetime`。
- [ ] 新建 `providers/circuit.py`：
  - `class CircuitBreaker(clock, *, schedule=(5, 15, 45, 120, 300), on_change=None)`；键 `f"{provider}:{endpoint}"`；
    状态 `{fail_count, open_until, last_error}`；方法 `check(provider, endpoint)`（窗口内抛 `ProviderUnavailableError`）、
    `record_success`、`record_failure(exc)`（仅瞬态错误计数，终态错误不计）、`snapshot()` 供健康页读取。
  - 线程安全不需要（worker 单进程 asyncio），但 `akshare` 调用在 `to_thread` 中，状态更新放回事件循环侧。
- [ ] 接入三个客户端：调用前 `check`，成功 / 失败后记录；`AkshareEastmoneyClient` 与两个 baostock 客户端构造函数新增可选参数 `circuit: CircuitBreaker | None`。
- [ ] `breadth.TransientBreaker` / `eastmoney.FailureBreaker` 触发时调用 `circuit.record_failure` 并把熔断器直接打开（`open_now`），保证下一次任务秒级 `partial`。
- [ ] 采集器收到 `ProviderUnavailableError` 时走既有 `stop_for_provider` 路径（reason `provider_unavailable`），不新增重试。
- [ ] `snapshots/handlers.py` 构造一个进程级 `CircuitBreaker` 实例（随 `SnapshotRuntime` 存活），注入所有客户端。
- [ ] 测试 `tests/unit/test_provider_circuit.py`：退避序列；窗口内不调上游（计数不变）；成功复位；终态错误不计数；采集器集成一例（`partial` + metadata reason）。用 `freezegun` 推时间。
- [ ] 提交：`feat(providers): cross-run circuit breaker with exponential backoff and negative cache`

## Task 2：通用调用台账 + 健康页（A2）· 迁移 022

- [ ] 模型 `provider_call`：`id`、`at`（tz）、`provider`（32）、`endpoint`（64）、`outcome`（`ok | transient | terminal | skipped_open`）、`duration_ms`（int, nullable）、`ticker_count`（int）、`run_id`（nullable）；索引 `(provider, at)`。迁移 `022` + `tests/integration/test_migration_022.py`（仿 `test_migration_021.py`）。
- [ ] 仓库 `ProviderCallRepository.record(...)` 与 `summary(since) -> list[ProviderStat]`（按 provider 聚合 calls / failed / last_error_at）。
- [ ] 钩子：仿 `verification/budget.py::ledger_hook` 写 `providers/ledger.py::provider_ledger_hook(source, run_id, clock)`，best-effort（异常只记日志）；三个客户端的 `on_call` 接入；熔断器 `on_change` 也写一行 `outcome=skipped_open`（endpoint 为 `circuit`）。
- [ ] 保留 90 天：`ia admin prune-provider-calls [--days 90]`（在 `cli/commands/admin.py`）。
- [ ] 健康页：`snapshots/health.py` 新增 `ProviderHealth(provider, calls_24h, failed_24h, open_until, last_error)` 与 `DataHealth.providers: list[ProviderHealth]`；`build_data_health` 聚合台账 + 熔断器快照（熔断器快照经 `HealthSettings` 传入，API 进程没有 worker 的内存状态时只展示台账里最近一条 `skipped_open`）；`derive_warnings` 对熔断中 provider 发 warning；`health.js` 新增"上游调用"区块（表格：provider / 24h 调用 / 失败 / 状态）。
- [ ] 测试：台账写入与聚合；健康页模型含 `providers`；`health.js` 通过守卫与 `node --check`。
- [ ] 提交：`feat(health): provider call ledger and per-provider upstream counters on /health/data`

## Task 3：口径与校准记录（A3）

- [ ] `config/field_sources.yaml`：schema 增加可选 `calibration: {checked_on: date, method: str, note: str}`（provider 级，与 `sanity` / `approx` 同层）；为现有 baostock / akshare 关键字段补一条（`checked_on` 用 p1-plan 2.1 / 2.2 的实测日期 2026-10-06）；`version` 升到 `2026-10-09.1`。
- [ ] `factors/field_sources.py` 加载校验：`definition` 必须含单位词（`fraction | CNY | 元 | 万元 | % | shares | days | ratio` 之一，列表放常量）；新 provider（非 baostock / akshare / derived）条目必须有 `calibration`，否则 `ValueError`。
- [ ] `providers/akshare_eastmoney.py` 文件头加"源链与口径"说明块：各接口单位（元）、`NOTICE_DATE` / `UPDATE_DATE` 语义、1 秒节流、与 baostock 的重叠字段。
- [ ] 测试：校验器通过 / 失败各一例；现有 yaml 通过。
- [ ] 提交：`docs(field-sources): calibration records and unit check for provider entries`

## 验收

- 本机：把 `AkshareEastmoneyClient` 指向不可达地址跑一次 `ia snapshot collect-financial --provider akshare --now`：第一次 `partial`（reason `provider_unavailable`），再跑一次应在 1 秒内 `partial`；`/health/data` 的 `providers` 里 akshare 显示熔断中。
- 门禁三条通过；在本文件末尾写"完成记录"（哈希、测试数、实测输出摘要）。
