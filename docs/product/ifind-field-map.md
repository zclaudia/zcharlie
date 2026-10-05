# iFinD canonical 字段映射

状态：MVP contract v1

可执行契约注册表：`investment_agent.research.ifind_mapping.IFIND_FIELD_MAPPINGS`

## 股票池

| Canonical 字段 | iFinD/输入 | 方式 | 单位或公式 |
|---|---|---|---|
| `name` | `/stock/info.ths_stock_short_name_stock` | direct | 文本 |
| `industry` | 命令/API 输入 | manual | canonical 行业标签 |
| `market_cap` | `close`、`ths_total_shares_stock` | derived | 最新收盘价 × 总股本，人民币 |

`symbol` 和 `market` 来自已经校验的 ticker，不依赖供应商自由文本。

## 市场数据

| Canonical 字段 | iFinD 字段 | 方式 | 单位或公式 |
|---|---|---|---|
| `latest_price` | `/stock/history.close` | direct | 元/股 |
| `pe` | `close`、`ths_eps_basic_stock` | derived | 股价 ÷ 最近完整财年基本 EPS |
| `pb` | `close`、`ths_nav_ps_stock` | derived | 股价 ÷ 最近完整财年每股净资产 |
| `ps` | `close`、`ths_revenue_ps_stock` | derived | 股价 ÷ 最近完整财年每股收入 |
| `dividend_yield` | 当前网关无稳定字段 | missing | `null`，强制 `needs_review` |
| `return_1y` | 历史 `close` | derived | 区间末值 ÷ 首值 - 1 |
| `volatility_1y` | 历史 `close` | derived | 日收益总体标准差 × √252 |
| `max_drawdown_1y` | 历史 `close` | derived | 正数形式的最大峰谷回撤 |
| `market_cap` | `close`、`ths_total_shares_stock` | derived | 最新收盘价 × 总股本，人民币 |

验证过的 `/stock/realtime` 类型中，`realtime_price` 和 `realtime_tech` 未提供现金分红；`close_summary` 当前返回供应商 502。因此 MVP 不从未经审计的字段猜测股息率。后续应优先在 `ifind-proxy` 增加正式分红接口，再采用“最近完整财年每股现金分红 ÷ as-of 股价”的方法。

## 财务数据

| Canonical 字段 | iFinD 字段 | 方式 | 单位或公式 |
|---|---|---|---|
| `revenue` | `ths_operating_total_revenue_stock` | direct | 人民币 |
| `net_profit` | `ths_np_atoopc_stock` | direct | 归母净利润，人民币 |
| `gross_margin` | `ths_gross_selling_rate_stock` | direct | 百分数 ÷ 100 |
| `net_margin` | `ths_net_sales_rate_stock` | direct | 百分数 ÷ 100 |
| `roe` | `ths_roe_stock` | direct | 百分数 ÷ 100 |
| `roe_stability` | 四个财年 `ths_roe_stock` | derived | clamp(1 - 总体标准差 ÷ 均值, 0, 1) |
| `debt_ratio` | `ths_asset_liab_ratio_stock` | direct | 百分数 ÷ 100 |
| `operating_cash_flow` | `ths_ncf_from_oa_stock` | direct | 人民币 |
| `free_cash_flow` | 经营现金流、长期资产现金支出 | derived | 前者减后者，人民币 |
| `revenue_growth_3y` | 两个完整财年收入 | derived | 三年 CAGR |
| `net_profit_growth_3y` | 两个完整财年归母净利润 | derived | 三年 CAGR |

## 时间语义

- 行情时间使用最后一个交易日 15:00 Asia/Shanghai。
- 财务数据必须明确 `report_year`，MVP 只接受用户指定的最近完整财年。
- 研究 `as_of` 显式传入评分内核，不再隐式使用内核执行时钟。
- 原始供应商响应、canonical 值、公式和来源同时保留，历史结果不静默重算。
