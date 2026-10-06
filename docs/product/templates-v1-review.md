# 模板 v1 阈值评审表（P1-C2 / P1-F2）

状态：**草稿，待用户逐条确认**。全部模板 `calibration_status: uncalibrated`，确认后合入；P3 回测再校准。

日期：2026-10-07

依据：[`funnel-design.md`](funnel-design.md) 第 5 节（三层规则模型）、7.2（周筛选）；
[`p1-plan.md`](p1-plan.md) P1-C2、P1-F2；`research_kernel` 五个策略原型（`classify_stock` 阈值与评分权重）。

模板文件：`config/rule_templates/*.yaml`（每条条件旁有 YAML 注释说明理由）。
`ia rules validate <file>` 离线校验，`ia rules sync` 入库，`ia screen run --as-of 2026-09-30 --now` 复现下文结果。

## 0. 怎么读这张表

- **筛选（screen）**：周频，在目标池上运行，全部条件成立即命中，进入候选。
- **候选区间**：`buy_zone`（可操作区，提示晋级）/ `drop`（不再候选）。P1 定义，日频检测在后续任务。
- **持仓区间**：`add` / `trim` / `exit_review`。P1 只定义不执行；可使用 P2 仓位因子
  （`position_weight`、`gain_vs_cost`、`holding_days`，校验器只允许它们出现在持仓区间）。没有 `sell` 区间。
- **参数**：条件里的 `$name` 引用模板参数；规则实例（LLM 起草）只能在"允许范围"内改数字。
  **默认值与允许范围都需要您确认。**
- 缺失与不适用：因子 `missing` → 该条件不成立并记录；`not_applicable`（如银行的毛利率、负债率）→ 跳过该条件；
  `approx`（如 baostock 的营收是主营业务收入）→ 允许使用，理由里带标记。
  缺失因子在 `drop` / `exit_review` 这类 `any` 组里永远不会触发，所以缺数较多的因子（`debt_ratio`）放在那里"优雅降级"。
- 全局预过滤：ST（`is_st`）与逾期未披露（`report_overdue`）标的不参加任何模板。
- 每模板最多 20 个命中，按"阈值内距离"强度排序（见 `rules/screen.py` 文档）。

## 1. 数据现状对模板的约束（2026-09-30，沪深 300 数据库副本）

| 事实 | 影响 |
|---|---|
| **没有股息率因子** | 红利蓝筹缺少核心指标，用行业组 + 规模 + ROE 稳定性 + 利润不下滑 + 低波动近似（已知缺口） |
| `free_cash_flow` 只来自 iFinD，目标池（baostock）全部 `missing` | 原型里的现金流质量用毛利率 / 净利率替代 |
| `debt_ratio`：117 只有值、132 只缺失、51 只金融不适用 | 筛选条件不用负债率；只放在 `drop` / `exit_review`（缺失不触发）。东财备用来源上线后可回到筛选 |
| `revenue_growth_3y` 295 只为 `approx`（主营业务收入） | 允许使用，理由带 `main_business_revenue` 标记；阈值比原型略放宽 |
| `pe_percentile_5y` 22 只缺失（亏损）、26 只 `approx`（历史不足 5 年） | 亏损股多的模板（高成长）改用 PB 分位 |
| 行业组分布：成长制造 103、周期 66、银行 24、券商 22、消费 22、公用 20、医药 18、风投 16、保险 5、地产 4 | 决定各模板候选面的大小 |

## 2. 命中汇总（as_of 2026-09-30，数据库副本，周筛选全流程）

| 模板 | 适用行业组 | 命中 | 未命中 | 跳过（行业不适用） | 剔除 |
|---|---|---:|---:|---:|---:|
| 红利蓝筹 `dividend_blue_chip` | utility, banking | **6** | 38 | 256 | 0 |
| 质量成长 `quality_growth` | growth_manufacturing, consumer, healthcare | **8** | 135 | 157 | 0 |
| 稳健质量成长 `steady_quality_growth` | growth_manufacturing, consumer, healthcare, utility | **8** | 155 | 137 | 0 |
| 周期反转 `cycle_reversal` | cyclical | **11** | 55 | 234 | 0 |
| 高成长风险投资 `high_growth_venture` | venture, growth_manufacturing, healthcare | **3** | 134 | 163 | 0 |
| 合计 | | **36**（35 只，德业股份命中两个模板） | | | |

全流程耗时：300 只成员因子计算约 11.8 秒，筛选 + 写库 0.3 秒。

"敏感度"一列：把该参数分别设为允许范围的下限 / 默认 / 上限时的命中数（其余参数不变），供调整参考。

---

## 3. 红利蓝筹 `dividend_blue_chip`

原型：红利行业（公用事业/银行/运营商/电力/高速）、市值 ≥ 3000 亿加分、股息率 ≥ 2.5%、FCF > 0；
评分权重 FCF 25、股息率 20、ROE 稳定性 20、负债 15、估值 15、增长 5。

适用行业组：`utility`、`banking`（问题 Q1：是否加入 `insurance`）。

### 筛选条件

| # | 条件 | 参数 | 默认 | 允许范围 | 理由 | 敏感度（下限/默认/上限） |
|---|---|---|---:|---|---|---|
| 1 | `market_cap >= $market_cap_min` | market_cap_min | 1000 亿 | 300 亿～5000 亿 | 只要蓝筹；原型 3000 亿是加分项，1000 亿保留中型公用与城商行 | 9 / 6 / 2 |
| 2 | `roe >= $roe_min` | roe_min | 8% | 5%～15% | 银行 ROE 中位 9%，公用 10% | 10 / 6 / 0 |
| 3 | `roe_stability >= $roe_stability_min` | roe_stability_min | 0.80 | 0.50～0.95 | 分红能力的代理：4 年 ROE 离散度低 | 6 / 6 / 2 |
| 4 | `net_profit_growth_3y >= $profit_growth_min` | profit_growth_min | -5% | -15%～5% | 利润不能在萎缩（分红可持续性代理） | 7 / 6 / 2 |
| 5 | `volatility_1y <= $volatility_max` | volatility_max | 30% | 20%～45% | 防御性 | 5 / 6 / 6 |
| 6 | `pb_percentile_5y <= $pb_pct_max` | pb_pct_max | 0.50 | 0.20～0.80 | 估值处于自身 5 年 PB 区间下半部；银行看 PB，PE 分位受利润下滑扭曲 | 1 / 6 / 12 |

### 候选区间

| 区间 | 条件 | 默认 | 允许范围 | 理由 |
|---|---|---:|---|---|
| buy_zone | `pb_percentile_5y <= $buy_pb_pct_max` | 0.30 | 0.05～0.50 | PB 进入 5 年区间底部三分之一 |
| drop（任一） | `pb_percentile_5y > $drop_pb_pct_min` | 0.80 | 0.60～0.95 | 估值已修复，不再是价值入口 |
| | `roe < $drop_roe_max` | 6% | 3%～10% | 盈利跌破底线 |
| | `net_profit_growth_3y < -10%` | 固定 | — | 利润明显萎缩 |

### 持仓区间（P1 只定义）

| 区间 | 条件 | 默认 | 允许范围 |
|---|---|---:|---|
| add（全部） | `pb_percentile_5y <= $add_pb_pct_max`；`position_weight < $add_weight_max`（P2） | 0.20；10% | 0.05～0.40；2%～20% |
| trim（任一） | `pb_percentile_5y >= $trim_pb_pct_min`；`position_weight > $trim_weight_min`（P2） | 0.90；15% | 0.70～0.99；8%～30% |
| exit_review（任一） | `roe < $exit_roe_max`；`net_profit_growth_3y < -15%`；`debt_ratio > 85%`（非金融；银行不适用） | 5% | 2%～8% |

### 命中（6）

| 排名 | 标的 | 行业组 | 强度 | 关键值 |
|---:|---|---|---:|---|
| 1 | 600036.SH 招商银行 | banking | 0.605 | 市值 1.02 万亿，ROE 12.0%，稳定性 0.91，PB 分位 0.22 |
| 2 | 600941.SH 中国移动 | utility | 0.549 | 市值 2.11 万亿，ROE 10.0%，稳定性 0.98，PB 分位 0.33（approx：上市不足 5 年） |
| 3 | 000001.SZ 平安银行 | banking | 0.492 | ROE 8.2%，利润 3 年 -2.2%/年，PB 分位 0.11 |
| 4 | 601985.SH 中国核电 | utility | 0.449 | ROE 8.1%，稳定性 0.85，PB 分位 0.34 |
| 5 | 600926.SH 杭州银行 | banking | 0.426 | ROE 12.7%，利润 3 年 +17.7%/年，PB 分位 0.42 |
| 6 | 601009.SH 南京银行 | banking | 0.413 | ROE 11.0%，PB 分位 0.42 |

未命中主因：PB 分位 > 0.5（17）、ROE < 8%（15）、市值 < 1000 亿（12）。

---

## 4. 质量成长 `quality_growth`

原型：成长行业（电力设备/新能源/自动化/汽车零部件/医药/高端制造）、3 年营收增速 ≥ 18%、ROE ≥ 15%、市值 ≥ 1000 亿加分；
评分权重 行业空间 25、竞争优势 25、营收增长 20、ROE 15、现金流 10、估值 5。

适用行业组：`growth_manufacturing`、`consumer`、`healthcare`。

### 筛选条件

| # | 条件 | 参数 | 默认 | 允许范围 | 理由 | 敏感度 |
|---|---|---|---:|---|---|---|
| 1 | `roe >= $roe_min` | roe_min | 15% | 10%～25% | 竞争优势：高 ROE（原型 15%） | 8 / 8 / 5 |
| 2 | `roe_stability >= $roe_stability_min` | roe_stability_min | 0.70 | 0.40～0.95 | ROE 不是一年的尖峰 | 11 / 8 / 1 |
| 3 | `revenue_growth_3y >= $revenue_growth_min` | revenue_growth_min | 15% | 8%～30% | 原型 18%；baostock 为主营收入（approx），略放宽 | 11 / 8 / 3 |
| 4 | `net_profit_growth_3y >= $profit_growth_min` | profit_growth_min | 12% | 5%～30% | 增长要转化为利润 | 9 / 8 / 5 |
| 5 | `gross_margin >= $gross_margin_min` | gross_margin_min | 25% | 15%～50% | 定价权，替代 FCF 质量 | 10 / 8 / 1 |
| 6 | `pe_percentile_5y <= $pe_pct_max` | pe_pct_max | 0.60 | 0.30～0.90 | 估值不在自身历史高位（原型估值权重仅 5） | 5 / 8 / 11 |

### 候选区间

| 区间 | 条件 | 默认 | 允许范围 | 理由 |
|---|---|---:|---|---|
| buy_zone | `pe_percentile_5y <= $buy_pe_pct_max` | 0.30 | 0.10～0.50 | PE 进入 5 年区间下三分之一 |
| drop（任一） | `pe_percentile_5y > $drop_pe_pct_min` | 0.85 | 0.70～0.98 | 估值过热 |
| | `revenue_growth_3y < $drop_revenue_growth_max` | 5% | 0～10% | 增长失速 |
| | `roe < $drop_roe_max` | 10% | 5%～15% | 质量下滑 |

### 持仓区间（P1 只定义）

| 区间 | 条件 | 默认 | 允许范围 |
|---|---|---:|---|
| add（全部） | `pe_percentile_5y <= $add_pe_pct_max`；`position_weight < $add_weight_max` | 0.25；8% | 0.05～0.50；2%～20% |
| trim（任一） | `pe_percentile_5y >= $trim_pe_pct_min`；`gain_vs_cost >= $trim_gain_min`；`position_weight > $trim_weight_min` | 0.90；50%；15% | 0.70～0.99；20%～150%；8%～30% |
| exit_review（任一） | `gross_margin < $exit_gross_margin_max`；`roe < $exit_roe_max`；`net_profit_growth_3y < 0` | 18%；8% | 10%～30%；3%～12% |

### 命中（8）

| 排名 | 标的 | 行业组 | 强度 | 关键值 |
|---:|---|---|---:|---|
| 1 | 605499.SH 东鹏饮料 | consumer | 0.822 | ROE 51.6%，营收 +35%（approx），利润 +45%，PE 分位 0.00 |
| 2 | 605117.SH 德业股份 | growth_manufacturing | 0.688 | ROE 32.1%，营收 +27%，PE 分位 0.28 |
| 3 | 300866.SZ 安克创新 | growth_manufacturing | 0.657 | ROE 26.1%，毛利 45%，PE 分位 0.45 |
| 4 | 300274.SZ 阳光电源 | growth_manufacturing | 0.656 | ROE 32.2%，利润 +54%，PE 分位 0.27 |
| 5 | 600660.SH 福耀玻璃 | growth_manufacturing | 0.585 | ROE 25.4%，PE 分位 0.06 |
| 6 | 000999.SZ 华润三九 | healthcare | 0.558 | ROE 16.3%，稳定性 0.95，PE 分位 0.00 |
| 7 | 688072.SH 拓荆科技 | growth_manufacturing | 0.523 | ROE 15.6%，营收 +57%，PE 分位 0.39（approx） |
| 8 | 002028.SZ 思源电气 | growth_manufacturing | 0.486 | ROE 22.6%，利润 +36%，PE 分位 0.47 |

未命中主因：营收增速（92）、ROE（85）、利润增速（68 + 17 缺失）。

---

## 5. 稳健质量成长 `steady_quality_growth`

原型：成长行业、ROE ≥ 12%、3 年营收增速 6%～18%、FCF > 0、股息率 ≥ 1.5%、负债率 ≤ 55%；
评分权重 盈利 20、增长持续性 20、ROE 稳定性 15、现金流 15、资产负债 10、股东回报 10、估值 10。

适用行业组：`growth_manufacturing`、`consumer`、`healthcare`、`utility`。

### 筛选条件

| # | 条件 | 参数 | 默认 | 允许范围 | 理由 | 敏感度 |
|---|---|---|---:|---|---|---|
| 1 | `roe >= $roe_min` | roe_min | 12% | 8%～20% | 原型 12% | 9 / 8 / 4 |
| 2 | `roe_stability >= $roe_stability_min` | roe_stability_min | 0.80 | 0.50～0.95 | "稳健"的定义特征 | 8 / 8 / 2 |
| 3 | `revenue_growth_3y between [$revenue_growth_min, $revenue_growth_max]` | revenue_growth_min / max | 6% / 18% | 0～10% / 12%～30% | 原型的温和增长区间 | 下限 12 / 8 / 6；上限 4 / 8 / 12 |
| 4 | `net_profit_growth_3y >= $profit_growth_min` | profit_growth_min | 5% | 0～15% | 利润持续增长 | 10 / 8 / 2 |
| 5 | `net_margin >= $net_margin_min` | net_margin_min | 8% | 3%～20% | 盈利质量，替代 FCF > 0 | 9 / 8 / 5 |
| 6 | `volatility_1y <= $volatility_max` | volatility_max | 45% | 25%～60% | 价格稳健 | 1 / 8 / 9 |
| 7 | `pe_percentile_5y <= $pe_pct_max` | pe_pct_max | 0.60 | 0.30～0.90 | 估值不过热 | 8 / 8 / 9 |

原型的"负债率 ≤ 55%"与"股息率 ≥ 1.5%"：前者移到 `exit_review`（负债率 > 70% 复核，缺失不触发），后者无因子（已知缺口）。

### 候选区间

| 区间 | 条件 | 默认 | 允许范围 | 理由 |
|---|---|---:|---|---|
| buy_zone | `pe_percentile_5y <= $buy_pe_pct_max` | 0.30 | 0.10～0.50 | |
| drop（任一） | `pe_percentile_5y > $drop_pe_pct_min` | 0.85 | 0.70～0.98 | 估值过热 |
| | `roe_stability < $drop_roe_stability_max` | 0.60 | 0.30～0.75 | 不再"稳健" |
| | `net_profit_growth_3y < 0` | 固定 | — | 利润转负增长 |

### 持仓区间（P1 只定义）

| 区间 | 条件 | 默认 | 允许范围 |
|---|---|---:|---|
| add（全部） | `pe_percentile_5y <= $add_pe_pct_max`；`position_weight < $add_weight_max` | 0.25；10% | 0.05～0.50；2%～20% |
| trim（任一） | `pe_percentile_5y >= $trim_pe_pct_min`；`gain_vs_cost >= $trim_gain_min`；`position_weight > $trim_weight_min` | 0.90；40%；15% | 0.70～0.99；20%～100%；8%～30% |
| exit_review（任一） | `roe < $exit_roe_max`；`roe_stability < 0.40`；`debt_ratio > $exit_debt_ratio_min` | 8%；70% | 3%～12%；55%～90% |

### 命中（8）

| 排名 | 标的 | 行业组 | 强度 | 关键值 |
|---:|---|---|---:|---|
| 1 | 600519.SH 贵州茅台 | consumer | 0.775 | ROE 34.5%，稳定性 0.94，营收 +11.6%，PE 分位 0.02 |
| 2 | 600809.SH 山西汾酒 | consumer | 0.722 | ROE 32.9%，营收 +13.9%，PE 分位 0.04 |
| 3 | 300750.SZ 宁德时代 | growth_manufacturing | 0.690 | ROE 24.7%，稳定性 0.97，利润 +31.9%，PE 分位 0.03 |
| 4 | 600406.SH 国电南瑞 | growth_manufacturing | 0.616 | ROE 16.3%，稳定性 0.99，PE 分位 0.01 |
| 5 | 300015.SZ 爱尔眼科 | healthcare | 0.607 | ROE 15.2%，营收 +11.5%，PE 分位 0.12 |
| 6 | 600276.SH 恒瑞医药 | healthcare | 0.583 | ROE 14.4%，利润 +26.5%，PE 分位 0.05 |
| 7 | 300832.SZ 新产业 | healthcare | 0.537 | ROE 18.3%，净利率 35%，PE 分位 0.07 |
| 8 | 300628.SZ 亿联网络 | growth_manufacturing | 0.515 | ROE 28.5%，净利率 43%，PE 分位 0.20 |

未命中主因：营收增速区间（110）、ROE（86）、稳定性（81）、波动率（71）。

---

## 6. 周期反转 `cycle_reversal`

原型：周期行业（有色/煤炭/石油/化工/钢铁/航运/能源/矿）、1 年最大回撤 ≥ 30%、营收增速 ≥ 10% 加分；
评分权重 周期位置（回撤）30、成本优势（ROE）25、资源储备（利润增速）20、资产负债 15、估值 10。

适用行业组：`cyclical`。

### 筛选条件

| # | 条件 | 参数 | 默认 | 允许范围 | 理由 | 敏感度 |
|---|---|---|---:|---|---|---|
| 1 | `max_drawdown_1y >= $drawdown_min` | drawdown_min | 30% | 20%～60% | 周期位置：过去一年深度下跌（原型 30%） | 18 / 11 / 1 |
| 2 | `pb_percentile_5y <= $pb_pct_max` | pb_pct_max | 0.30 | 0.10～0.50 | 相对自身周期历史便宜；用 PB 不用 PE（低谷利润扭曲 PE） | 7 / 11 / 16 |
| 3 | `roe >= $roe_min` | roe_min | 5% | 0～12% | 成本优势：低谷仍盈利 | 16 / 11 / 6 |
| 4 | `market_cap >= $market_cap_min` | market_cap_min | 500 亿 | 200 亿～2000 亿 | 只看能熬过低谷的龙头 | 14 / 11 / 1 |

### 候选区间

| 区间 | 条件 | 默认 | 允许范围 | 理由 |
|---|---|---:|---|---|
| buy_zone | `pb_percentile_5y <= $buy_pb_pct_max` | 0.15 | 0.05～0.30 | PB 处于 5 年底部 |
| drop（任一） | `pb_percentile_5y > $drop_pb_pct_min` | 0.60 | 0.40～0.90 | 估值已修复 / 离开底部 |
| | `roe < 0` | 固定 | — | 亏损，不再是成本优势者 |
| | `debt_ratio > $drop_debt_ratio_min` | 80% | 65%～95% | 资产负债压力（缺失不触发） |

### 持仓区间（P1 只定义）

| 区间 | 条件 | 默认 | 允许范围 |
|---|---|---:|---|
| add（全部） | `pb_percentile_5y <= $add_pb_pct_max`；`position_weight < $add_weight_max` | 0.10；6% | 0.02～0.25；2%～15% |
| trim（任一） | `pb_percentile_5y >= $trim_pb_pct_min`；`gain_vs_cost >= $trim_gain_min`；`position_weight > $trim_weight_min` | 0.80；60%；10% | 0.60～0.98；20%～200%；5%～25% |
| exit_review（任一） | `roe < $exit_roe_max`；`debt_ratio > 85%`；`holding_days > $exit_holding_days_min`（P2：反转逻辑设时限） | -2%；730 天 | -10%～5%；180～1460 天 |

### 命中（11）

| 排名 | 标的 | 强度 | 关键值 |
|---:|---|---:|---|
| 1 | 600547.SH 山东黄金 | 0.823 | 回撤 62%，PB 分位 0.21，ROE 11.5% |
| 2 | 600346.SH 恒力石化 | 0.778 | 回撤 43%，PB 分位 0.10，ROE 10.9% |
| 3 | 603799.SH 华友钴业 | 0.751 | 回撤 58%，PB 分位 0.06，ROE 14.3% |
| 4 | 000792.SZ 盐湖股份 | 0.717 | 回撤 43%，PB 分位 0.17，ROE 20.8% |
| 5 | 002714.SZ 牧原股份 | 0.656 | 回撤 38%，PB 分位 0.19，ROE 20.7% |
| 6 | 002311.SZ 海大集团 | 0.605 | 回撤 33%，PB 分位 0.01，ROE 17.8% |
| 7 | 601390.SH 中国中铁 | 0.592 | 回撤 34%，PB 分位 0.01，ROE 6.3% |
| 8 | 601669.SH 中国电建 | 0.485 | 回撤 36%，PB 分位 0.00，ROE 6.0% |
| 9 | 600221.SH 海航控股 | 0.485 | 回撤 36%，PB 分位 0.09（approx），**ROE 52.5%（异常，见 Q6）** |
| 10 | 002001.SZ 新和成 | 0.480 | 回撤 34%，PB 分位 0.23，ROE 21.8% |
| 11 | 600009.SH 上海机场 | 0.318 | 回撤 35%，PB 分位 0.00，ROE 5.0%（**机场归入周期组，见 Q7**） |

未命中主因：PB 分位（35）、回撤不足（15）、ROE（14）。

---

## 7. 高成长风险投资 `high_growth_venture`

原型：风投行业（半导体/AI/机器人/创新药/计算机/电子）、3 年营收增速 ≥ 30%、市值 < 1000 亿；总是需要人工复核；
评分权重 TAM（营收增速）30、技术壁垒（ROE）25、团队 20、商业化（利润增速）15、现金储备（负债率）10。

适用行业组：`venture`、`growth_manufacturing`、`healthcare`。

### 筛选条件

| # | 条件 | 参数 | 默认 | 允许范围 | 理由 | 敏感度 |
|---|---|---|---:|---|---|---|
| 1 | `revenue_growth_3y >= $revenue_growth_min` | revenue_growth_min | 25% | 15%～50% | 原型 30%；主营收入口径（approx）略放宽 | 9 / 3 / 1 |
| 2 | `gross_margin >= $gross_margin_min` | gross_margin_min | 30% | 15%～60% | 技术壁垒代理（亏损企业 ROE 无意义） | 7 / 3 / 2 |
| 3 | `market_cap <= $market_cap_max` | market_cap_max | 1000 亿 | 300 亿～3000 亿 | 原型 < 1000 亿 | 0 / 3 / 5 |
| 4 | `pb_percentile_5y <= $pb_pct_max` | pb_pct_max | 0.70 | 0.30～0.90 | 不追自身 PB 高位；亏损股 PE 缺失，故用 PB | 0 / 3 / 7 |

不要求利润增速（商业化可能滞后）。

### 候选区间

| 区间 | 条件 | 默认 | 允许范围 | 理由 |
|---|---|---:|---|---|
| buy_zone | `pb_percentile_5y <= $buy_pb_pct_max` | 0.40 | 0.10～0.60 | |
| drop（任一） | `revenue_growth_3y < $drop_revenue_growth_max` | 10% | 0～20% | 增长失速 |
| | `pb_percentile_5y > $drop_pb_pct_min` | 0.90 | 0.70～0.99 | 估值过热 |

### 持仓区间（P1 只定义；仓位上限比其他模板更紧）

| 区间 | 条件 | 默认 | 允许范围 |
|---|---|---:|---|
| add（全部） | `pb_percentile_5y <= $add_pb_pct_max`；`position_weight < $add_weight_max` | 0.30；4% | 0.05～0.50；1%～10% |
| trim（任一） | `gain_vs_cost >= $trim_gain_min`；`position_weight > $trim_weight_min`；`pb_percentile_5y >= $trim_pb_pct_min` | 100%；8%；0.95 | 30%～300%；4%～15%；0.80～0.99 |
| exit_review（任一） | `revenue_growth_3y < $exit_revenue_growth_max`；`gross_margin < $exit_gross_margin_max`；`debt_ratio > 70%` | 5%；20% | -10%～15%；10%～40% |

### 命中（3）

| 排名 | 标的 | 行业组 | 强度 | 关键值 |
|---:|---|---|---:|---|
| 1 | 688506.SH 百利天恒 | healthcare | 0.611 | 营收 +53%，毛利 91%，市值 974 亿，PB 分位 0.41 |
| 2 | 002558.SZ 巨人网络 | venture | 0.508 | 营收 +35%，毛利 91%，市值 420 亿，PB 分位 0.67 |
| 3 | 605117.SH 德业股份 | growth_manufacturing | 0.250 | 营收 +27%，毛利 38%，市值 708 亿（同时命中质量成长） |

未命中主因：营收增速（105）、毛利率（74）、市值 > 1000 亿（56）。沪深 300 本身偏大盘，此模板在中证 500 上才有意义（Q5）。

---

## 8. 待您确认的问题

| # | 问题 | 草案做法 |
|---|---|---|
| Q1 | 红利蓝筹是否纳入 `insurance`（5 只）与 `broker`？ | 未纳入：保险利润波动大、券商强周期 |
| Q2 | 股息率因子缺失。是否把"新增股息率因子（baostock 分红数据或东财）"列为 M3 前置？ | 先用代理条件上线，标注已知缺口 |
| Q3 | `debt_ratio` 缺失约一半。东财备用来源（P1-A11）上线后，是否把稳健质量成长的"负债率 ≤ 55%"放回筛选？ | 目前只在 drop / exit_review 使用 |
| Q4 | 每周命中合计 36 只（单周快照）。目标是"每周数十只新候选"——是否偏多 / 偏少？每模板上限 20 是否合适？ | 维持默认，上线后看 8 周分布 |
| Q5 | 高成长模板在沪深 300 仅 3 只。是否放宽营收增速（下限 15% 时 9 只）或等中证 500 加入目标池？ | 维持 25% |
| Q6 | 海航控股 ROE 52.5%（净资产极低导致的异常值）命中周期反转。是否给 ROE 加上限（如 `roe between [5%, 40%]`）或依赖人工审阅？ | 依赖人工审阅 |
| Q7 | 上海机场等交通基础设施落在 `cyclical`（申万交通运输默认周期，只有铁路公路覆盖为公用）。是否把机场（申万三级"机场"）改为 `utility`？ | 不在本任务改动（行业映射属 P1-B3） |
| Q8 | 估值统一用"自身 5 年分位"，没有绝对估值上限（如 PE ≤ 30）。是否需要加绝对上限？ | 不加，避免行业间不可比 |
| Q9 | `drop` / `exit_review` 中几处固定阈值（-10%、0、-15%、85%）未参数化，实例不能改。是否需要参数化？ | 作为模板级常量，升模板版本才能改 |
| Q10 | 持仓区间的仓位类阈值（加仓 < 6%～10%、减仓 > 8%～15%，风投更紧）是否符合您的仓位习惯？ | 草案值，P2 执行前再确认 |
| Q11 | 强度排序只用"阈值内距离"的平均；是否需要按某个因子（如 ROE 或 PE 分位）优先排序？ | 维持平均距离 |
