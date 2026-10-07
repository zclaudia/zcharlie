# 观察端：因子库指标与免费源快照模式（P1-B4 / P1-B5）

状态：已实现（2026-10-07）

依据：[`p1-plan.md`](p1-plan.md) 第 2 节 D3 / D6 二次修订 / D7、2.3、B1、B3、B4、B5、A12、A13。

## 1. 结论

- **B4 指标**：iFinD 单股研究的 canonical 派生指标全部由因子库计算：`canonicalize_ifind_results`
  把解码后的 iFinD 行构造成 `OBSERVATION` 视图（`ifind_research_view`），读取 `FactorValue`。
  删除的并行代码：`_trim_to_one_year`、`_return_stats`、`_row_date`、`_optional_first_row`
  以及对 `annualized_growth` / `roe_stability` / `positive_multiple` 的直接调用。
  golden corpus：13 只样本 iFinD fixture 重放到 canonical 仍逐字节一致（`test_ifind_contract`），
  内核重放逐字段一致（`test_golden_corpus_replay`），**不升 scoring version**（D3）。
- **B4 行业**：内核的分类与公司画像不再自行匹配关键词，改读 `IndustryBasis`（`research_kernel/industry.py`）。
  用户填写的行业文字等于行业组 id 时取该组，否则沿用旧关键词表；未填写时用数据的行业组（申万优先、证监会兜底）。
  "用户文字等于申万一/二/三级名称即映射到该组"会改变 4 只 golden 的分类，按 D3 **未启用**，差异见第 3 节。
- **B5**：新增数据源模式 `snapshot`（自选股、晋级后研究、`POST /research/single-stock` 的默认值），
  从 `load_snapshot_view(policy=TARGET_POOL)` 构造 canonical bundle，不调用 iFinD；`ifind` 模式保留给手动深度研究。

## 2. 行业组 → 内核特征

旧内核用四组关键词（股息 / 成长 / 周期 / 风投）给策略加分，用另四组关键词（含公司名）决定公司画像。
行业组按"该类业务在旧关键词表里会命中什么"映射为固定特征：

| 行业组 | 策略特征 | 公司画像 |
|---|---|---|
| `banking` | dividend | banking |
| `insurance` | – | insurance |
| `broker` | – | general |
| `utility` | dividend | public_utility |
| `cyclical` | cycle | general |
| `growth_manufacturing` | growth | manufacturing_growth |
| `venture` | venture | general |
| `healthcare` | growth（旧表"医药"） | general |
| `consumer` / `real_estate` / `other` | – | general |

解析顺序（`resolve_industry_basis`）：

1. 用户文字（`field_sources.industry.provider == "manual"`，或没有来源记录——所有 iFinD bundle 都是这种）：
   等于行业组 id（如 `utility`）→ 该组；否则 → 旧关键词表（原样保留，含"电力设备"同时命中"电力"的历史行为）。
2. 无用户文字 → `industry_group`（快照模式 bundle 提供，`as_of` 当日申万 2021，缺失时证监会兜底）。
3. 都没有 → 无特征（general）。

## 3. 被搁置的映射：用户文字 = 申万名称（D3 阻断）

按任务要求先试了"用户文字恰好等于申万一/二/三级名称时映射到该名称的行业组"，golden 重放结果：

| 样本 | 用户文字 | 申万匹配 → 组 | 差异 |
|---|---|---|---|
| 600406.SH | 电力设备 | 630000 → growth_manufacturing | `classification_fit_scores.dividend_blue_chip` 55 → 20（失去旧表"电力"子串命中）；策略、模型、分数不变 |
| 601111.SH | 航空运输 | 421001 → cyclical | 策略 / 模型 quality_growth → cycle_reversal；`business_score` 55.25 → 33.51；cycle_reversal 适配分 35 → 100；共 27 处字段差异 |
| 688019.SH | 半导体材料 | 270103 → growth_manufacturing | 策略 high_growth_venture → quality_growth，模型 → manufacturing_growth；`business_score` 77.22 → 86.77；`suggestion` needs_review → risk_review；公司画像 general → manufacturing_growth；共 37 处 |
| 002415.SZ | 安防设备 | 710102 → growth_manufacturing | 模型 steady_quality_growth → manufacturing_growth；`business_score` 62.32 → 45.07；`suggestion` low_priority → needs_review；画像 general → manufacturing_growth；共 28 处 |

其余 9 只（银行、白酒、石油化工、软件办公、动力电池、人工智能软件、多元工业、电子特气与光刻材料）
不匹配或结果相同。启用该映射需要单独评估并升级 scoring version。

## 4. 快照模式 canonical 字段映射

视图：`TARGET_POOL`（baostock 锚定报告期与 `pubDate`；字段经 `config/field_sources.yaml` 解析：iFinD 已核对值 >
主来源 > 备用来源）。基本面取 `as_of` 时已披露的最近年报（请求的报告年度不同时加 `report_year_substituted` 提示）。

| canonical 字段 | 快照模式来源 | iFinD 模式来源 | 口径差异 |
|---|---|---|---|
| `pool.name` | `listing → company.canonical_name` | `/stock/info` | – |
| `pool.industry` | 用户文字；无则申万三级名称（证监会兜底） | 用户文字 | 见第 2 节 |
| `pool.industry_group`（新增，可选） | 行业组（`as_of`） | –（不输出） | – |
| `market_cap`（pool / market） | 因子 `market_cap`：不复权收盘价 × 最近报告总股本 | 收盘价 × `/stock/info` 总股本 | 股本口径时点不同 |
| `latest_price` | baostock 不复权收盘价（≤ `as_of`） | `/stock/history` 最新收盘价 | – |
| `pe` / `pb` / `ps` | 因子 `pe_ttm` / `pb_mrq` / `ps_ttm`（baostock 日频估值；≤ 0 视为不适用并标 `negative_earnings` 等） | 因子 `pe_fy` / `pb_fy` / `ps_fy`（收盘价 ÷ 年报每股值） | **定义不同**：TTM / MRQ vs 财年 |
| `dividend_yield` | 因子 `dividend_yield_ttm`（近 365 天税前现金分红 ÷ 收盘价）；分红未采集时为缺失并标 `unavailable`（不阻断） | 无（`unavailable`） | **新增数据**：可让 dividend_blue_chip（≥2.5% +25）、steady_quality_growth（≥1.5% +10）加分 |
| `return_1y` / `volatility_1y` / `max_drawdown_1y` | 同名因子，baostock 后复权序列 | 同名因子，iFinD 前复权序列 | 复权方式不同，数值相近 |
| `revenue` | 年报 `revenue_total`（营业总收入，东财主） | `ths_operating_total_revenue_stock` | 同口径 |
| `net_profit` | 年报 `net_profit_parent`（归母净利润，东财主） | `ths_np_atoopc_stock`（归母） | 同口径 |
| `operating_cash_flow` | 年报 `operating_cash_flow`（东财主；baostock `CFOToNP × netProfit` 备用，标 `proxy`） | 现金流量表 | 同口径（备用为近似） |
| `gross_margin` / `net_margin` / `roe` / `debt_ratio` | 同名因子（baostock 主；负债率 < 0.02 判缺陷改取东财） | 同名因子（iFinD 财务指标） | ROE：baostock `roeAvg`（平均净资产） |
| `roe_stability` | 因子（近 4 个年报 ROE） | 同 | – |
| `free_cash_flow` | 因子：经营现金流 − 资本支出（东财单股现金流量表） | 同（iFinD 现金流量表） | 东财单股表未采集时缺失 |
| `revenue_growth_3y` | 因子：baostock `revenue_main`（主营收入，`approx` → `proxy`；金融股缺失时 `unavailable`） | 因子：`revenue_total` | **口径不同**（主营 vs 营业总收入） |
| `net_profit_growth_3y` | 因子：baostock `net_profit_total`（含少数股东） | 因子：`net_profit_parent` | **口径不同**（B1 同名异义拆分） |

每个字段的 `field_sources` 记录：`provider`（实际来源，多个输入时以 `+` 连接，如 `akshare`、`baostock`、`ifind`）、
`source`（`snapshot_view:factor:<因子>` 或 `snapshot_view:financial_snapshot.<字段>`）、`quality`
（`direct` / `derived` / `proxy` / `missing`）、`verification`（`unverified` / `verified` / `conflict`，新增可选字段）
与 `note`（因子版本、质量、标志）。`conflict` 字段进入 `provider_conflicts` 并加 `provider_conflict` 复核标志。
新增的可选字段（`verification`、`industry_group`、`metadata.source_mode`）在未赋值时不序列化，
因此 iFinD bundle 与 golden 逐字节一致；`canonical_schema_version` 仍为 `ifind-canonical.v1`。
原始审计制品为 `raw_snapshot.json`（视图引用、各报告期字段来源、因子值与 `inputs_ref`）。

## 5. 路由、运行记录与 `source_changed`

| 入口 | 模式 |
|---|---|
| 自选股 `provider = auto`（默认）/ `snapshot` | snapshot（不需要 `IFIND_PROXY_API_KEY`；行业与报告年度可空） |
| 自选股 `provider = ifind` / `ifind-proxy` | ifind（需要 key、行业、报告年度） |
| `POST /research/single-stock` | `provider` 默认 `snapshot`；`provider=ifind` 时需要行业与报告年度，未配置 key 返回 503 |
| `ia research snapshot` / `ia research ifind` | 对应模式 |
| 候选晋级后的研究 | 跟随自选股条目（新条目为 `auto`） |
| 队列中旧 payload（无 `provider`） | ifind（与入队时语义一致） |

- 运行 metadata：`source_mode`、`previous_source_mode`、`source_changed`、`verification_requested_fields`；
  研究快照的 `score_json.source_mode` 记录本次模式（旧快照视为 `ifind`）。
- **`source_changed`**：同一股票本次模式与上一份研究快照不同。变化集新增 `methodology` 类变化
  `kind = source_changed`，`diff_json.source_changed = true`；其余所有差异（分数、策略、指标、质量）照常列出，
  但标为非重要（`suppressed_reason = source_changed`），`has_meaningful_changes = false`。
  与评分版本变更的处理方式一致；不同的是数据源切换仍会发送一条通知，说明"本次评分、策略与指标的跳变可能源于
  数据源切换（字段口径不同）"，并列出被抑制的差异。下一次同模式运行恢复正常语义。

## 6. 免费源缺失

- **整只缺失**（快照库没有该股的行情或 `as_of` 时已披露的年报，例如不在目标池）：不调用 iFinD；
  投递 A13 核对请求（`trigger = missing`，全部关键字段，报告期为请求年度的年报），运行以 `partial` 结束，
  原因形如 `snapshot_data_missing: no free-source price snapshot for … ; iFinD verification request (trigger=missing) queued`，
  不写研究快照。证券代码不在 `listing` 表时无法投递，原因中注明。
- **关键字段缺失**（营业总收入、归母净利润、经营现金流、资本支出 / 自由现金流、负债率、ROE 中被内核计为缺失且对
  公司画像适用的字段，例如银行不请求自由现金流）：照常评分，同时投递 `missing` 请求（字段为对应登记字段）。
  核对任务在预算内取数；核对写回的 iFinD 值经视图优先级生效，此后不再重复请求。

## 7. 快照模式与 iFinD 模式偏差

### 7.1 Fixture 报告（`tests/unit/test_snapshot_deviation_report.py`，不设断言门槛）

golden 样本中同时有 baostock 因子 fixture 的 600519、601398，按 golden 的 `as_of`（2026-10-05）与相同用户行业文字
比较。fixture 只有 baostock 字段（无东财行），因此营业总收入、归母净利润、自由现金流缺失，置信度偏低；
行情与 baostock 财务比率与 iFinD 基本一致（ROE、毛利率、净利率、ROE 稳定性逐位一致）。运行 `pytest -s` 打印完整表格。

### 7.2 本机库副本（2026-09-30，免费源全量，`as_of` 早 3 个交易日于 golden）

快照模式（无用户行业文字，行业组来自申万）：

| 股票 | 行业（申万三级）→ 组 | 策略 / 模型 | 业务分 | 置信度 | 标签 | 估值 / 风险 | 缺失 |
|---|---|---|---|---|---|---|---|
| 600519.SH | 白酒Ⅲ → consumer | steady_quality_growth / steady_quality_growth | 80.04 | 85.1 | needs_review | expensive / medium_low | free_cash_flow |
| 601398.SH | 国有大型银行Ⅲ → banking | dividend_blue_chip / banking | 59.47 | 100.0 | low_priority | cheap / medium | – |
| 300750.SZ | 锂电池 → growth_manufacturing | steady_quality_growth / manufacturing_growth | 67.15 | 92.0 | needs_review | fair / medium | free_cash_flow |
| 600900.SH | 水力发电 → utility | dividend_blue_chip / public_utility | 69.66 | 92.0 | needs_review | fair / medium_low | free_cash_flow |
| 688223.SH | 光伏电池组件 → growth_manufacturing | quality_growth / manufacturing_growth | 18.75 | 75.6 | needs_review | expensive / high | free_cash_flow, net_profit_growth_3y |

复核原因几乎都是自由现金流缺失：库中东财行只有按期汇总（`part = summary`，3231 行），没有单股现金流量表，
因此没有资本支出；四只股票已各自投递 `missing` 请求（`capital_expenditure`、`free_cash_flow`）。
688223 另有 PE 不适用（亏损）与净利润增速缺失。iFinD 预算账本 0 行（无 iFinD 调用）。

与 golden iFinD（2026-10-05）对比，快照侧换成与 golden 相同的用户行业文字以对齐分类输入：

| 股票 | 字段 | iFinD | 快照 | 说明 |
|---|---|---|---|---|
| 600519.SH | pe / pb / ps | 19.17 / 6.443 / 9.161 | 18.97 / 6.147 / 8.916 | TTM / MRQ vs 财年；价格日期不同（−1.8%） |
| 600519.SH | dividend_yield | – | 4.21% | 新增数据 |
| 600519.SH | revenue_growth_3y / net_profit_growth_3y | 10.49% / 9.49% | 11.60% / 9.28% | 主营 vs 营业总收入；含少数股东 vs 归母 |
| 600519.SH | 营收、归母净利、毛利率、净利率、ROE、ROE 稳定性、负债率、经营现金流 | – | – | 逐位一致 |
| 600519.SH | 业务分 / 标签 | 76.88 / defer | 80.04 / needs_review | 股息率加分；自由现金流缺失触发复核 |
| 601398.SH | pe / pb / ps | 8.28 / 0.7645 / 3.52 | 7.773 / 0.734 / 3.316 | 同上 |
| 601398.SH | dividend_yield | – | 3.80% | 新增数据 |
| 601398.SH | net_profit_growth_3y | 0.74% | 0.89% | 含少数股东 vs 归母 |
| 601398.SH | 业务分 / 置信度 / 标签 | 58.0 / 80.5 / low_priority | 59.47 / 100.0 / low_priority | 银行不适用自由现金流；股息率不再是缺失字段 |
| 300750.SZ | pe / pb / ps | 18.04 / 3.941 / 3.136 | 15.61 / 3.498 / 2.544 | TTM 含 2026H1 增长（−13%～−19%） |
| 300750.SZ | dividend_yield | – | 2.92% | 新增数据 |
| 300750.SZ | net_profit_growth_3y | 32.94% | 31.91% | 口径 |
| 300750.SZ | 业务分 / 标签 | 69.5 / monitor | 67.17 / needs_review | 自由现金流缺失 |

300750 在无用户文字时按申万组 growth_manufacturing 选用 manufacturing_growth 模型（67.15），
用户文字"动力电池"不命中旧关键词时为 steady_quality_growth 模型（67.17）。

## 8. 已知差异与后续

- 东财单股现金流量表（P1-A11 `cash_flow` 部分）需要批量采集，否则快照模式的自由现金流长期依赖 iFinD `missing` 核对。
- 估值倍数定义不同（TTM / MRQ vs 财年）、增速口径不同（B1 拆分）属于预期差异；在线分数会有一次跳变（2.3"影响"），
  由 `source_changed` 记录与说明。
- "用户文字 = 申万名称"映射见第 3 节，需评估后随新 scoring version 启用。
