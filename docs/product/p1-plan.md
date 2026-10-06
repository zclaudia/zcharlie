# P1 执行计划：目标池、模板筛选与候选

状态：Ready for execution（第 2 节决策已于 2026-10-06 确认；同日按 D6 数据源分工修订，见 2.1）

日期：2026-10-06

依据：[`funnel-design.md`](funnel-design.md) 第 12 节 P1，数据源结论见其第 15.1 节。

## 0. 目标与完成定义

P1 交付"目标池 → 周频模板筛选 → 候选（附理由与 LLM 起草的规则）→ 用户晋级/忽略"的完整闭环，
并从第一天起积累时点快照，为 P2 区间检测与 P3 回测打底。

P1 分两道门：

- **代码完成**：第 9 节全部任务通过门禁，离线端到端 smoke 跑通。
- **阶段退出**（设计文档原文）：快照连续积累 ≥ 8 周且完整率达标；每周稳定产出候选；
  每条理由可回溯到快照字段；每只候选都有通过校验的规则草稿；因子定义只有一份。

阶段退出依赖日历时间，因此**数据采集链路必须最先上线**（里程碑 M1～M2），其余工作与快照积累并行。

## 1. 现状要点（影响计划的事实）

| 事实 | 影响 |
|---|---|
| 现有单股链路每只股票发 10 次 proxy 请求，每次 1 只 | 沪深 300 全量刷新约 3000 次请求；即使按 3 只一批也约 1000 次 |
| **iFinD 经 Kimi 网关有 5 小时滚动额度**，本机实测约 350 次调用后返回 403（额度大小未知，可能与用户日常 Kimi 使用共用） | iFinD 不能承担目标池全量采集与回补；见 D6 |
| baostock 免费提供日线（不复权 / 后复权，含 PE TTM、PB MRQ、PS TTM、停牌、ST）与季度财务（含实际披露日 `pubDate`） | 广度数据改由 baostock 承担；披露日不再需要逐只查公告 |
| 因子计算分散在 `research/ifind_chain.py`（canonicalize）与 `research_kernel/scoring.py` | 需抽出统一因子库，否则会出现第二套定义 |
| 分类依赖行业**关键词**匹配用户手填的行业（如"电力设备"） | baostock 返回证监会分类（如"C38电气机械和器材制造业"），关键词匹配会失效，需要显式映射表 |
| 已有 `universe` / `universe_membership`（`valid_from` / `valid_to`）表，原为 legacy 保留 | 结构正好适合时点成分，可直接复用，不必新建 |
| 已有 `holding_item` 表（预留未用） | P1 不使用，P2 再评估是否沿用 |
| 队列、租约、心跳、幂等、调度、交易日历都已就绪 | 新增批处理任务全部复用现有 worker 与 `pipeline_run` |
| 数据库迁移当前到 `013`（M1 已合并） | 后续迁移从 `014` 起 |

## 2. 已确认的决策

| # | 决策 | 结论 |
|---|---|---|
| D1 | 规则草稿何时起草 | 候选产生时自动起草 |
| D2 | 财报"可得日期" | 取**实际披露日**（baostock `pubDate`，见 2.1）；缺失时退回法定截止日近似并标记 `approx`；逾期未披露视为不可得并标记 `report_overdue` |
| D3 | 观察端迁移到因子库后分数是否允许变化 | 不允许：golden corpus 逐字段一致，不升 scoring version |
| D4 | 成分表 | 复用 `universe` / `universe_membership` |
| D5 | 回补深度 | 价格 5 年、年报 6 年、最近 4 个季报（全部经 baostock） |
| D6 | 数据源分工 | **baostock 负责广度**（目标池全量日线、估值、季度财务、披露日、回补）；**iFinD 负责深度**（观察池 / 候选 / 持仓的单股研究、自由现金流、完整报表、公告）；两源重叠字段做交叉校验 |

### 2.1 数据源分工与实测（2026-10-06）

**iFinD 额度。** 本机首次真实采集沪深 300 日线时，约 350 次调用后 Kimi 网关返回
`403 You've reached your 5-hour usage limit`。额度按 5 小时滚动窗口计算，具体大小未知，且可能与用户日常 Kimi 使用共用。
ifind-proxy 把该错误转成 502，采集器误判为瞬时错误反复重试。

**baostock 实测**（贵州茅台等）：

| 数据 | 接口 | 结果 |
|---|---|---|
| 日线 | `query_history_k_data_plus` | 5 年 1211 条一次取回：不复权 2.7 秒、后复权 8.6 秒；收盘价与 iFinD 一致（2026-09-30 为 1258.62）；附 `peTTM`、`pbMRQ`、`psTTM`、`turn`、`tradestatus`、`isST` |
| 季度财务 | `query_profit_data` / `balance` / `cash_flow` / `growth` / `operation` | ROE、毛利率、净利率、净利润、EPS TTM、主营收入、总股本、资产负债率、现金流比率等；每期带 `pubDate`（如 2025 年报 2026-04-17、2026 半年报 2026-08-15）与 `statDate`；未披露期返回空 |
| 速度 | — | 单只单日约 0.2 秒；沪深 300 日增量约 1～2 分钟 |

baostock 不提供：自由现金流绝对值、三大报表明细、公告。这些仍由 iFinD 在观察池范围提供。

**分工（D6）：**

| 范围 | 数据源 | 规模 |
|---|---|---|
| 目标池全量（沪深 300）：日线、估值、季度财务、披露日、历史回补 | baostock | 免费，不占 Kimi 额度 |
| 观察池 / 候选 / 持仓：单股研究、自由现金流、完整报表、公告[P4] | iFinD | 数十只，每日数十次调用 |
| 重叠字段（收盘价、ROE、毛利率等） | 两源交叉校验 | 不一致记为 `provider_conflict` |

iFinD 公告接口的实测结论（单次最多 50 条、多只合并查询无法归属）保留作为 P4 披露原文接入的依据，
不再用于披露日。

逾期规则不变：过法定截止日仍无该期 `pubDate` 的股票，该期视为不可得、沿用上一期，并产生 `report_overdue`；
筛选排除，P2 作为 `exit_review` 的候选触发条件。

### 2.2 AkShare 补充实测（2026-10-06，akshare 1.19.1）

目的：评估 AkShare 能否补上 baostock 与 iFinD 都覆盖不到、或成本过高的缺口。AkShare 本质是对东财、巨潮、中证、
申万等网站的爬虫封装，接口与返回列会随网站改版变化，因此只按具体能力评估，不作为通用 provider。

| 缺口 | 接口 | 实测结果 | 结论 |
|---|---|---|---|
| 申万行业（证监会分类过粗，见 funnel-design 15.1） | `stock_industry_clf_hist_sw` | 0.8 秒取回全市场 12925 行、5930 只；每行带 `start_date`，即**时点行业历史**（如贵州茅台 2021-07-30 起为 `340501` 白酒）。只有 6 位代码，没有名称；申万官网证书链不完整，默认会报 SSL 校验失败 | 可用。代码到名称的对照用 `sw_index_third_info`（335 个三级行业，含上级行业）加 `sw_index_third_cons`（每个约 0.7 秒，全部约 4 分钟）按当前成分反推；SSL 问题只对 `swsresearch.com` 补全中间证书，不全局关闭校验 |
| 指数成分权重 | `index_stock_cons_weight_csindex` | 3.1 秒取回沪深 300 共 300 行；权重为月度数据（最新 2026-08-31），只有最新一期，没有历史 | 可选。当前设计不依赖权重，先不接 |
| 自由现金流（目标池范围） | `stock_cash_flow_sheet_by_report_em` | 单只单表约 16～24 秒（每次拉回全部历史，约 100 期）。现金流量表 254 列，含 `NETCASH_OPERATE`（经营现金流净额）与 `CONSTRUCT_LONG_ASSET`（购建长期资产支付的现金），可以算 FCF；带 `NOTICE_DATE` 与 `UPDATE_DATE`。20 只连续调用 324 秒，没有被限流 | 可用但慢：沪深 300 全量约 1.4 小时，只适合在披露季窗口低频刷新。**不是时点数据**：贵州茅台 2025H1 的 `UPDATE_DATE` 为 2026-08-15，说明上年同期数在新报告披露时被覆盖，必须以采集时刻落快照 |
| 三大报表明细 | `stock_balance_sheet_by_report_em` / `stock_profit_sheet_by_report_em` | 资产负债表 319 列、利润表 203 列，单只各约 23～25 秒 | 同上；P1 不需要明细，先不接 |
| 全市场按期财务 | `stock_xjll_em` / `stock_zcfz_em` / `stock_lrb_em` | 每期一次调用、6～9 秒取回 5228 只，带公告日期；只有汇总项（经营 / 投资 / 筹资现金流净额、净利润、营收、资产负债率等），**没有资本支出** | 适合交叉校验 baostock 财务字段，不能用来算 FCF |
| 公告（P4）与披露日核对 | `stock_zh_a_disclosure_report_cninfo` | 单只 1.4 秒；`symbol` 留空即可查全市场：单日 6540 条用时 79 秒，半年报窗口（7/15～9/5）按类别过滤得 11220 条，用时 108 秒。返回代码、标题、精确到秒的公告时间、巨潮链接 | 可用，并且解决了 iFinD 公告接口"单次 50 条、合并查询无法归属"的问题。P4 接入公告原文时优先用它；P1 可用来抽查 baostock `pubDate` |

**定位：** AkShare 作为第三个数据源，只做补缺，不碰 D6 定下的主路径：

- 披露日与财务的时点语义仍以 baostock `pubDate` 为准；AkShare 的财务数据只作为补充字段或交叉校验，
  `provider` 记为 `akshare`。
- 两源冲突时以 iFinD 为准（仅限观察池范围）；目标池范围内的冲突只记 `provider_conflict`，不覆盖。
- 每项能力一个独立 adapter，锁定 akshare 版本，测试用 fixture 回放；接口失败只影响它负责的那一类快照。
- 数据仅供个人研究使用。

**待确认（Q1）：** P1-B3 的行业组映射是基于证监会分类（现方案），还是改为基于申万（P1-A9 第 1 项）。
申万更细，而且自带时点历史；代价是多一个外部依赖，同时要处理证书问题。确认前 B3 按现方案推进，映射表要能切换数据源。

## 3. 工作流 A：数据层

### P1-A1 baostock 参考数据 adapter（S）

- 新增依赖 `baostock`；独立模块 `providers/baostock_reference.py`，不与 iFinD 共享代码。
- 接口：`fetch_constituents(index_code, as_of)`、`fetch_industry(as_of, symbol=None)`。
- 代码映射 `sh.600000` ↔ `600000.SH`；`updateDate` 作为生效日期，拒收晚于采集日的结果。
- 登录/登出包装、超时、重试；结果附原始响应用于审计。
- 测试：录制的脱敏 fixture 离线回放；未来日期守卫；空结果（2005 年前）；代码映射往返。

### P1-A2 时点快照表（M）· 迁移 `013`

- `universe` / `universe_membership` 复用（D4），新增 `source`、`captured_at` 列。
- 新表 `industry_classification`：`symbol`、`system`（`csrc`）、`code`、`name`、`valid_from`、`valid_to`、`captured_at`。
- 新表 `market_snapshot`：`symbol`、`trade_date`、OHLC、成交量、总股本、`provider`、`captured_at`、`raw_ref`。
- 新表 `financial_snapshot`：`symbol`、`report_period`、`available_from`、`availability`（`actual` / `approx`）、
  `fields`（JSON，canonical 字段）、`provider`、`captured_at`、`raw_ref`、`is_backfill`。
- 不可变约束：同一主键重采产生新版本行（`version` 列），读取时按 `captured_at <= as_of` 取最新。
- 测试：迁移从空库与从 `012` 升级均可用；时点查询不读到 `as_of` 之后写入的数据。

### P1-A3 iFinD 批量采集器（M）· 已合并（`aa32e9b`～`d05930f`）

- worker 按任务类型分发；新任务类型 `universe_sync`、`industry_sync`、`market_snapshot`、`financial_snapshot`。
- 按 3 只一批调用 proxy、令牌桶节流、单批失败隔离、完整率统计；日线存不复权与后复权两套（不存前复权）。
- 按 D6，`market_snapshot` / `financial_snapshot` 的目标池范围改由 P1-A7 的 baostock 采集器承担；
  本任务的 iFinD 采集器保留，范围收窄为观察池 / 候选 / 持仓（P1-A7 一并调整）。

### P1-A7 广度采集切换到 baostock（M）· 迁移 `014`

- baostock 日线采集器：目标池全量，不复权 + 后复权，增量；`market_snapshot` 增加 `pe_ttm`、`pb_mrq`、`ps_ttm`、
  `turnover`、`trade_status`、`is_st` 列（迁移 `014`）。
- baostock 季度财务采集器：profit / balance / cash_flow / growth 四张表映射为 canonical 字段，
  `available_from = pubDate`、`availability = actual`；字段映射登记到统一注册表，标明来源。
- 总股本取 baostock 季度 `totalShare` 并按 `pubDate` 生效，替代把取数日期拼在 `raw_ref` 的做法（清理技术债）。
- 任务类型区分数据源（如 `market_snapshot` 的 payload 增加 `provider`），iFinD 采集器只对观察池运行。
- 同一快照表、同一时点语义；两源并存时按 `provider` 区分，因子层按 D6 选择来源。
- 测试：fixture 回放；`pubDate` 驱动的可得性；停牌（`tradestatus=0`）与 ST 标记；未披露期返回空。

### P1-A8 iFinD 额度耗尽处理（S，跨 ifind-proxy 与 zcharlie）

- ifind-proxy：识别上游额度错误（`403` + usage limit），返回 `429` 与可读详情，而非 `502`。
- zcharlie：`429` 额度错误归类为 `QuotaExhausted`：立即停止本次任务剩余批次、状态记为 `partial`（原因 `quota`），
  发 `action_required` 通知，不做批内重试；下次调度自然续采。
- 测试：额度错误不触发重试；已采数据保留；通知只发一次。

### P1-A4 历史回补（M）

- 一次性 CLI：`ia snapshot backfill --universe CSI300 --prices 5y --annual 6 --quarters 4`（按 D5），全部经 baostock。
- 成分历史：按周从最早日期向后依次同步（baostock 成分每周发布）。现有规则拒绝"早于最近一次变化"的快照，
  回补需支持**向前延伸**成员区间（较早快照与当前状态一致时把 `valid_from` 前移），或在任何实时同步之前完成回补。
- 可中断续跑（按股票幂等）；回补行标记 `is_backfill=true`；`market_snapshot` 增加 `is_backfill` 列（迁移 `015`），
  历史查询按交易日纳入回补价格（价格不追溯修改），严格模式可排除。
- 回补范围包含窗口内曾经是成分、后被调出的股票，避免幸存者偏差。
- 预估：日线 300 只 × 两种复权约 1 小时；财务 300 只 × 10 期 × 4 表约 2 小时；不占 iFinD 额度。

### P1-A6 披露日与逾期检测（S）

- 披露日直接取 baostock `pubDate`（P1-A7 写入 `financial_snapshot.available_from`），不再新建公告表。
- 逾期检测：每个法定截止日次日扫描，目标池中尚无该期 `pubDate` 的股票写入 `report_overdue`，发 `action_required` 通知。
- 测试：截止日边界；逾期后补披露的处理（`available_from` 取实际 `pubDate`，逾期标记保留历史）。

### P1-A5 采集调度（S）

在现有调度器中新增（全部 Asia/Shanghai、仅交易日）：

| 任务 | 频率 | 内容 |
|---|---|---|
| 成分同步 | 每周一 08:00 | baostock 成分 → `universe_membership`；有进出则发 `info` 通知 |
| 行业同步 | 每月首个交易日 08:00 | baostock 行业 → `industry_classification` |
| 日线快照 | 交易日 16:30 | 全成分股 `market_daily` |
| 财报快照 | 每周六 + 4/30、8/31、10/31 后首个交易日 | 最近报告期 `financial_period`，只采尚无该期数据的股票 |

沿用 misfire 宽限、启动补跑与日历到期告警。已随 P1-A3 合并；P1-A7 完成后目标池日线与财报改调 baostock 采集器，
执行时间需按 baostock 实际数据更新时间调整（待实测，预计晚于 17:30）。

遗留修正：`ia snapshot sync-industry` 未指定 `--universe` 时默认同步全市场并为约 5000 只股票建档，
应改为默认使用配置中的目标池，与调度器一致。

### P1-A9 AkShare 补缺采集（候选，M）· 未排入里程碑

依据 2.2 的实测结论。按价值排序，每一项可以单独落地：

1. **申万行业**（S，取决于 Q1）：`stock_industry_clf_hist_sw` 按 `start_date` 写入 `industry_classification`
   （`scheme = sw2021`，与证监会分类并存）；代码到名称的对照表每月由三级行业成分反推一次；
   SSL 只为该域名补中间证书。行业同步任务每月顺带执行。
2. **目标池 FCF 近似**（S）：`stock_cash_flow_sheet_by_report_em` 只在披露季窗口刷新；
   `fcf = NETCASH_OPERATE - CONSTRUCT_LONG_ASSET`，`available_from` 取 baostock `pubDate`（缺失时取 `NOTICE_DATE`
   并标记 `approx`），`provider = akshare`；`UPDATE_DATE` 晚于 `NOTICE_DATE` 的行标记 `restated`。
   观察池仍以 iFinD FCF 为准；两者重叠时做交叉校验。
3. **财务交叉校验**（S）：每期披露窗口结束后用 `stock_lrb_em` / `stock_xjll_em` / `stock_zcfz_em` 各调用一次，
   与 baostock 的净利润、营收、经营现金流、资产负债率对比，不一致写入 `provider_conflict`，数据健康页展示。
4. **巨潮公告**（P4 范围，P1 只留接口）：`stock_zh_a_disclosure_report_cninfo` 按日全市场拉取，
   P1 只用来抽查 `pubDate`。

- 依赖：`akshare` 锁定小版本，作为可选依赖（`[project.optional-dependencies] akshare`），未安装时对应任务跳过并记日志。
- 节流：东财接口单线程，默认每次调用间隔至少 1 秒；连续失败 3 次，本次任务置为 `partial` 并发通知。
- 测试：每个接口一份 fixture，固定列名，列名变化时测试失败；`restated` 标记；`approx` 回退。

## 4. 工作流 B：因子库

### P1-B1 因子库骨架（M）

- **同名异义字段须先拆开**：baostock `net_profit` 含少数股东损益、`revenue` 为主营收入，与 iFinD 的归母净利润、
  营业总收入口径不同。因子库必须使用不同的 canonical 名称（如 `net_profit_total` / `net_profit_parent`、
  `revenue_main` / `revenue_total`），不得让两种口径共用一个字段名，也不得跨源直接比较。

- 新包 `factors/`：注册表 + 纯函数。每个因子声明 `name`、`version`、`unit`、`inputs`、
  `applicability`（行业组）、`compute(snapshot_view) -> FactorValue`。
- `FactorValue`：`value`、`quality`（`ok` / `missing` / `not_applicable` / `approx`）、`inputs_ref`（快照引用）。
- `snapshot_view(symbol, as_of)`：只读时点视图，是因子唯一的数据入口。
- 首批因子从现有代码搬迁并保持数值一致：ROE、ROE 稳定性、毛利率、净利率、负债率、营收与利润 3 年增速、
  自由现金流、PE、PB、PS、市值、1 年收益、波动率、最大回撤。
- 测试：对 golden corpus 的 13 只样本，因子值与现有 canonical 输出逐字段一致。

### P1-B2 估值分位因子（M）

- `pe_percentile_5y`、`pb_percentile_5y`：基于 baostock 每日 `peTTM` / `pbMRQ` 序列计算分位（按 D6）；
  观察池内用 iFinD 年报 EPS 自算的 PE 作交叉校验，偏差超阈值记为 `provider_conflict`。
- 历史不足 5 年按实际长度计算并标记 `approx`；不足 1 年为 `missing`。
- 逾期未披露的股票，因子附带 `report_overdue` 标记。
- PE ≤ 0（亏损）的交易日不计入分布（与 v1.3 负 PE 规则一致）。
- 测试：合成序列验证分位；时点边界（披露截止日前后）；亏损年份排除。

### P1-B3 行业适用性映射（S）

- `config/industry_groups.yaml`：证监会大类 → 行业组（`banking`、`insurance`、`utility`、`cyclical`、
  `growth_manufacturing`、`venture`、`consumer`、`other`），版本化。
- 因子 `applicability` 与模板适用行业都引用行业组，不再使用关键词。
- 覆盖校验：沪深 300 当前成分所属的每个证监会大类都必须有映射，否则测试失败。

### P1-B4 观察端改读因子库（M）

- 单股研究链路的 canonical 指标改由因子库计算；`research_kernel` 评分逻辑不变。
- 行业关键词分类改为基于行业组；用户手填行业仍可覆盖。
- 按 D3：golden corpus 结果必须逐字段一致，不升 scoring version。若不一致，单独评估后再决定。
- 删除被替代的重复计算代码。

## 5. 工作流 C：规则

### P1-C1 规则 schema 与校验器（M）

- Pydantic 模型：`RuleTemplate`、`RuleInstance`、条件（`factor` + `op` + `value`，`all` / `any` 组合）、区间枚举。
- 校验：因子存在于注册表；参数在模板 `param_ranges` 内；区间名属于该 `scope` 的枚举；无自由文本条件。
- 序列化为 YAML/JSON，可 diff；测试覆盖每一类校验失败。

### P1-C2 模板 v1（M）

- 把五个策略原型改写为 `config/rule_templates/*.yaml`：适用行业组、`screen` 条件、
  候选区间（`buy_zone` / `drop`）与持仓区间（`add` / `trim` / `exit_review`）默认参数、`param_ranges`。
- 持仓区间在 P1 只定义不执行（P2 使用），但必须通过校验。
- 全部标注 `calibration_status: uncalibrated`。
- 需要用户参与：每个模板的默认阈值由开发者起草、用户逐条确认（见第 8 节）。

### P1-C3 规则引擎 · 筛选（M）

- 输入：目标池时点成分 + 因子值 + 模板 `screen` 规则；输出：命中标的 + 结构化 `reason`（规则、因子、阈值、实际值、快照引用）。
- 纯函数、确定性；同一 `as_of` 与规则版本重复运行结果逐字节一致。
- 行业不适用的因子跳过而非判失败；缺失因子使该条件不成立并记录。
- 每周日 20:00 运行（新任务类型 `screen_run`），结果写入 `screen_result`。

### P1-C4 规则库与版本（S）· 迁移 `016`

- 表 `rule_template`、`rule_instance`（`symbol`、`scope`、`template_ref`、`version`、`status`、`definition`、
  `drafted_by`、`draft_rationale`、`approved_at`）。
- 生命周期 `draft → active → retired`；修改即新版本；模板文件变更时同步入库并升版本。

### P1-C5 规则起草 Agent（M）

- 输入：该股因子值与分位、行业组、可选模板、用户历史驳回理由（最近 N 条）。
- 输出：`RuleInstance` 草稿 + 逐条起草理由；复用现有 OpenAI 兼容客户端、PromptedOutput、超时与重试分类。
- 草稿必须通过 P1-C1 校验，失败重试一次，仍失败则候选标记"草稿不可用"并可人工补写。
- LLM 调用写入 `llm_call` 成本表。
- 离线 eval：固定输入下草稿 100% 通过校验；不出现交易指令语言；参数全部在允许范围内。

## 6. 工作流 D：候选与决策日志

### P1-D1 候选生命周期（M）· 迁移 `017`

- 表 `candidate`：`symbol`、`opened_at`、`expires_at`（默认 14 天）、`sources`（screen / manual）、
  `screen_result_ref`、`draft_rule_ref`、`status`（`open` / `promoted` / `dismissed` / `expired`）。
- 同一标的已有 `open` 候选时合并来源而非重复创建；被忽略的标的 30 天内不再自动提名（可配置）。
- 筛选完成后自动创建候选并按 D1 触发起草。

### P1-D2 决策日志（S）

- 表 `decision`（不可变）：`symbol`、`kind`（`promote` / `dismiss` / `nominate` / `approve_rule` /
  `reject_rule` / `edit_rule`）、`reason_text`、`rule_ref`、`context_ref`（当时的筛选结果与因子快照）、`at`。
- `dismiss`、`reject_rule` 必填理由；理由作为 P1-C5 的偏好输入。

### P1-D3 晋级流程与 API（M）

- `POST /candidates/{id}/promote`：要求批准（可编辑参数后批准）草稿规则 → 规则 `active` → 写入现有 `watchlist_item`
  （行业取证监会分类、报告年度取最近完整财年）→ 触发一次单股研究。
- `POST /candidates/{id}/dismiss`、`POST /candidates`（手动提名）、`GET /candidates`、`GET /screen-runs/{id}`、
  `GET /rules/{id}`、`PATCH /rules/{id}`。
- 全部在一个事务内写决策日志；复用现有鉴权、CSRF 与限流。

## 7. 工作流 E：Web UI

### P1-E1 候选页（M）

- 本周候选列表：标的、行业、命中模板、理由（可点击展开到快照字段与原始响应）、草稿状态。
- 规则审阅：渲染草稿条件与起草理由；参数可在允许范围内编辑；批准晋级 / 驳回 / 忽略（必填理由）。
- 首屏展示理由，不展示 0～100 综合分；全页显示"未校准模板"标记。

### P1-E2 数据健康页（S）

- 成分最近同步时间与变化；各采集任务最近一次完整率与缺失清单；回补进度；快照累计周数（阶段退出指标）。
- 关键指标并入今日概览的 warnings。

## 8. 工作流 F：验收与文档

### P1-F1 离线端到端 smoke（M）

- fixture：baostock 成分与行业 + 10 只股票的 iFinD 快照（覆盖银行、周期、成长、亏损、次新）。
- 流程：成分同步 → 快照采集 → 因子 → 筛选 → 候选 → 起草（stub LLM）→ 批准晋级 → 观察池单股研究。
- 断言：每条理由可回溯到快照；重复运行结果一致；决策日志完整。

### P1-F2 模板阈值评审（S，需用户参与）

- 开发者给出五个模板的默认阈值与依据；用户逐条确认或修改后合入 P1-C2。

### P1-F3 文档（S）

- README、`engineering-baseline.md` 技术决策表（新增 baostock）、运维说明（回补、调度、数据健康）。

## 9. 依赖与里程碑

```text
M1 数据地基          A1 ─┬─ A2 ─┬─ A3 ─┬─ A5     ← 代码已合并
M1' 分工调整              │      ├─ A8   └─ A7 ─┬─ A6   ← 广度切到 baostock 后开始积累快照
                         │      │             └─ A4
M2 因子库                └─ B3  └─ B1 ─── B2
M3 规则与筛选                          C1 ─ C2 ─ C3 ─ C4
M4 候选闭环                                 D1 ─ D2 ─ D3 ─ C5 ─ E1
M5 收口                                     B4 · E2 · F1 · F2 · F3
```

| 里程碑 | 任务 | 完成标志 |
|---|---|---|
| M1 数据地基 | A1、A2、A3、A5 | 代码已合并（`d245847`～`d05930f`）；本机真实链路验证：成分、行业、不复权日线与总股本入库正常，后复权因 iFinD 额度耗尽中断 |
| M1' 分工调整 | A7、A8、A6 | 目标池日线、估值、财务经 baostock 每日入库；iFinD 额度错误不再重试（**8 周计时起点**）。代码已合并（`c7c2d71`～`8fd183f`）；本机实测 2026-09-30 日线与 2026H1 财务均 300/300，299 只与 iFinD 收盘价逐只一致 |
| M2 因子库 | A4、B1、B2、B3 | 回补完成；13 只 golden 样本因子一致；分位因子可算 |
| M3 规则与筛选 | C1、C2、C3、C4、F2 | 周筛选稳定产出带理由的命中列表 |
| M4 候选闭环 | D1、D2、D3、C5、E1 | 浏览器内完成"候选 → 审阅草稿 → 晋级"全流程 |
| M5 收口 | B4、E2、F1、F3 | 代码完成门禁通过；观察端只剩一套因子定义 |

规模：S 约 0.5～1 天，M 约 1～3 天（一名开发者配合编码 Agent）。合计约 20 个任务、3～4 周代码量；
阶段退出另需快照积累满 8 周，从 M1 完成之日起算。

## 10. 风险

| 风险 | 缓解 |
|---|---|
| Kimi 网关 5 小时额度（大小未知，可能与用户日常使用共用） | 广度与回补改走 baostock（D6）；iFinD 只覆盖观察池；额度耗尽立即停止并通知（A8） |
| baostock 服务不稳定（D6 后成为广度数据的单一来源） | 快照已落库，故障只影响增量；失败发 `action_required` 通知；观察池仍有 iFinD 交叉校验；必要时评估 Tushare Pro 作为备用 |
| AkShare 接口随网站改版失效（爬虫性质） | 只用于补缺与交叉校验（2.2），不进入主路径；锁定版本 + fixture 列名校验；失败只影响对应快照类型 |
| 因子库迁移导致观察端分数变化 | D3 要求逐字段一致；B4 放在最后，失败可延后到 P2 |
| 模板阈值拍脑袋 | 全部标注 `uncalibrated`；F2 由用户确认；P3 校准 |
| 候选过多导致审阅负担 | 每模板设命中上限（如前 20），超出按理由强度截断并在页面提示 |
