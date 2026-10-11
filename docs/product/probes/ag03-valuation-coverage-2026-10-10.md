# AG03 / V3a 现有估值数据覆盖审计

审计日/as_of：2026-10-10（Asia/Shanghai）。只读现有快照，没有网络请求、补采、迁移或数据修复。机器可查明细：[JSON](ag03-valuation-coverage-2026-10-10.json)。

## 数据库定位与固定范围

- 环境 `Settings.database_url` 解析到 `/Users/zhvala/SourceCode/zcharlie/data/app.db`，4096 字节，SQLite `quick_check=ok`，但没有任何表。AG02 runtime settings 的 `OperationalError` 可由该库缺少 `app_setting`/schema 解释；本项不创建表、不更换配置。
- `.claude/launch.json` 的 `zcharlie-api` 使用默认环境，`zcharlie-api-dbcopy` 配置的 `/private/tmp/claude-501/-Users-zhvala-SourceCode-zcharlie/0341b884-bc80-4d38-ac8d-822e2e9f23fc/scratchpad/app.db` 是另一份库（1,050,415 行行情、11,061 行财务）。文件只读保留。本次没有仅凭该配置认定它正在运行。
- 实际运行 `ia api serve --port 8002 --reload` 的父进程 PID 5947 / API 子进程 PID 93406，通过 `lsof -p 93406 -Fn` 确认打开 `.dev/data/app.db` 及对应 WAL/SHM。本报告选该实际启用库：`/Users/zhvala/SourceCode/zcharlie/.dev/data/app.db`，Alembic revision `021`。
- `data/recovery-20261008/app.db` 是恢复副本（1,048,915 行行情、11,061 行财务），未当成启用库，也未修改。启用库现存 1,049,515 行行情、11,061 行财务；这些是库存行数，不能充当覆盖分母。
- 目标池为 `UniverseRepository.members_as_of("CSI300", 2026-10-10)` 的300只证券，保留全部身份/区间/来源；目标池版本 SHA256：`265af9bd154b472feb4e11cf2776033ed886bd540caead86ca498915293228b8`。它是已有成员记录的版本，不是声称实时核对了指数官网。
- 所选输入逻辑版本：`59b86cd1b95c3d7cb0e65110ca268f1f2c69b43b2ef5ab98bc00be75d242656e`。JSON 保留快照 ID/version、来源、金额、原始引用、逐证券原因及行情输入哈希。整个审计在显式 SQLite `BEGIN` 的一致读快照内执行，`mode=ro` 与 `query_only=ON` 双重只读，不锁住写入或修改业务表。

## 覆盖结果

所有证券级覆盖率的分母均为300，同一证券可同时出现多项原因；每一字段有唯一主原因及全部原因，近似可计算与完整历史分别统计。

| 能力/字段 | 现有输入可得 | 完整/限制 |
|---|---:|---|
| 总市值输入 | 300/300 (100%) | 来源未经普通股归属核验；并非 DCF 市值口径启用率 |
| PE 五年分位 | 278/300 (92.67%) | 252 `ok`，26 `approx/short_history`；21 当前PE非正，1 正值历史少于240 |
| PB 五年分位 | 299/300 (99.67%) | 279 `ok`，20 `approx/short_history`；1 正值历史少于240 |
| CFO 原始金额（任一可知期间） | 300/300 (100%) | AkShare300只；另3只有iFinD金额来源，来源覆盖可重叠 |
| capex 原始金额（任一可知期间） | 300/300 (100%) | AkShare300只；未发现其他该金额来源 |
| CFO 三个完整年度金额 | 300/300 (100%) | 固定2023、2024、2025年度，年度中零/负值也只算金额可得 |
| capex 三个完整年度金额 | 300/300 (100%) | 固定同三个年度；不是完整年度FCFE |
| 两金额三个年度同时齐全 | 300/300 (100%) | 不验证融资、利息、股权归属或方法适用 |
| FCFE口径可用 / 方法适用 / 求解成功 | 未核验 | V3b负责；不能将未核验写作0或100%覆盖 |

`market_cap` 沿用 `factors/library.py::_market_cap`：最新未复权收盘价 × `SnapshotView.total_shares()`；target-pool 股数来自已公开财报 `total_shares`，并非 `float_shares`。Baostock 的 `profit.totalShare`、iFinD 总股数适配及引用可追溯，单位CNY。总市值不自动证明普通股整体市值与FCFE归属一致，跨类股、币种和归属由AG11核验；旧 legacy `fundamental_snapshot.market_cap` 未进入本统计。

分位直接调用 `VALUATION_FACTORS.compute(view)`，没有新增公式。窗口为 `(2021-10-10, 2026-10-10]`；正值有效日门槛240，起点落后窗口起点超过31天为 `short_history`。PE正值日数范围83–1211，PB为203–1211。`001280.SZ` 两项均不足240；其他缺失PE的21只在JSON逐证券列出。所有当前行情日期为2026-10-08，距as_of两天，均未超过既有14个日历日新鲜度门槛；截至as_of没有开放的report_overdue记录。这只代表库中健康记录，未重新检查供应商或公告。

现金流直接检查 `FinancialSnapshot.fields.operating_cash_flow` / `capital_expenditure` 的有限数值，不计算 `cfo_to_np × net_profit`，也不将 `free_cash_flow` 快照数算作FCFE。各provider/part/report_period仅取最新可知version，不把重复捕获或季度累计现金流相加。原始金额与 `source_values.NETCASH_OPERATE`、`CONSTRUCT_LONG_ASSET`、CNY、raw_ref均保留。目标池选中金额证据包含AkShare 5,398条（含summary与cash_flow两part，不视作独立来源）及iFinD3条；只是证据库存。

## 可知性与真实限制

沿用 `load_snapshot_view` / 仓储PIT约定：financial `available_from <= as_of`、availability非unknown、已捕获；`is_backfill=True`允许按available_from重建历史可知性。行情同样仅取不晚于as_of的交易日，并沿用历史backfill。成员仓储按有效区间选择，不声称捕获时间意义的历史成员认知重放。截止时间是下一上海自然日零点对应UTC的排他边界。

当前原始财务首次捕获在2026-10-06至10-07；`available_from`可能早于首次捕获，不能声称系统在那些历史日期实际已持有材料。本次as_of在首次捕获之后，没有通过移动截止日提高覆盖。金额证据中5,330条标actual/baostock_pub_date，68条标approx/eastmoney_notice_date，iFinD3条为approx；`available_from`/NOTICE_DATE并不解决事后重述、首次下载材料版本与历史原件缺失的问题。原始引用是否仍能打开以及财报附件内容没有在本项复核。

借款流入、发债、偿债、现金利息、普通股归属、合并范围/币种/单位、资本结构与非经营重大项目均需要AG11核验。东财现金流适配器现有保留列仅NETCASH_OPERATE及CONSTRUCT_LONG_ASSET和元数据，缺列不是零；iFinD来源也不能仅凭金额视为补齐这些缺口。金融企业等方法不适用范围本项不做FCFE筛选；fixture算例不会扩大真实覆盖。

## 重复执行与验证

```sh
.venv/bin/python tools/audit_valuation_coverage.py --database .dev/data/app.db --as-of 2026-10-10 --universe CSI300 --expected-pool-version 265af9bd154b472feb4e11cf2776033ed886bd540caead86ca498915293228b8 --output docs/product/probes/ag03-valuation-coverage-2026-10-10.json
.venv/bin/pytest -q tests/test_valuation_coverage.py tests/unit/test_factor_valuation.py
.venv/bin/ruff check .
.venv/bin/mypy src
.venv/bin/pytest -q -m 'not network'
```

入口要求显式database/as_of/output，不静默切换库或补采。目标池版本不匹配时拒绝覆盖输出；库缺失不会新建，schema缺失如实报错。报告随现有数据库增加新版本可能变化，`data_version`与逐字段引用用于识别变化；固定JSON是本次冻结证据。离线fixture验证只读边界、PIT未来/unknown/backfill、原始金额/ratio区分、零负数/null/nonfinite、版本去重、240/31/五年边界、负值与时效原因；不冒充实际字段覆盖。

验证结果：专项29 passed（新审计16、既有分位13）；ruff全仓通过；mypy src通过202个文件；完整离线回归1593 passed、6 deselected（87.58秒）。真实审计命令重复执行至 `/tmp/ag03-repeat.json`，完整JSON与data_version均相同；没有重复采集。
