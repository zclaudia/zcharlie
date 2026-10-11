# 08 确定性财务指标与适用性规则

> 波次 W3 ‖ 可与 06、07 并行。依赖：02 已合并（契约）；只需 01 的 `ControlledIdentity` 类型。
> 文件所有权：见 [`README.md`](README.md) §4 第 08 行。**禁止修改 `materials/contracts.py`**。

**目标：** 实现设计 §财务分析规则 的六项指标与全部"不满足时"的处理；纯函数，无 IO、无数据库、无网络。银行与 ADS 的适用性单独判定。

## 代码事实

- 状态枚举直接复用：`investor/enums.py:88-95` `CalculationStatus`（`OK`/`NOT_APPLICABLE`/`MISSING_INPUTS`/`MISSING_ASSUMPTIONS`/`STALE_INPUTS`/`OUTSIDE_SEARCH_RANGE`/`CALCULATION_ERROR`），中文标签在 `enums.py:152-160`。
- 数值类型用 `investor/common.py:116-126` 的 `Quantity`（`value`/`unit`/`currency`/`unknown_reason`，未知必须有原因）。
- `CalculationRecord` 的字段要求（`input_hash` 覆盖版本/单位/币种/公式）见 `evidence/contracts.py:77-100`；**本任务不写库**，是否需要落 `CalculationRecord` 由 09/10 决定，本模块只产出可映射的 `MetricSet`。
- A 股既有指标实现（**只学口径，不复用代码**）：`src/investment_agent/research_kernel/`；A 股的 `debt_ratio`/`roe` 定义与港美要求不同，不要照搬阈值。

## 文件清单

```
新增  src/investment_agent/materials/metrics.py
新增  tests/fixtures/materials/metrics/pilots.json     （三家港股 + 两家美股的最小 fact 集合）
新增  tests/unit/test_material_metrics.py
```

## 实现

```python
@dataclass(frozen=True)
class CompanyProfile:
    company_key: str; is_bank: bool; is_ads: bool; ads_ratio: str | None
    report_currency: str; quote_currency: str; fiscal_year_end_note: str
    # 从 01 的 ControlledIdentity 直接映射；由调用方构造（本模块不读名单文件）

@dataclass(frozen=True)
class MetricResult:
    key: str                      # revenue_yoy / revenue_cagr_3y / roe_avg_equity / debt_ratio / cfo_to_profit / fcf_simple
    label: str                    # 中文展示名
    status: CalculationStatus
    value: Quantity | None
    basis: str                    # 口径说明，永不为空
    periods: tuple[FactPeriod, ...]
    notes: tuple[str, ...]        # 可比性/不适用/转盈亏说明
    inputs: tuple[str, ...]       # "metric@period_end"，用于 input_hash 与追溯

@dataclass(frozen=True)
class MetricSet:
    metrics: tuple[MetricResult, ...]
    gaps: tuple[str, ...]         # 数据与能力缺口，人可读（含 gross_margin/net_margin 未交付说明）

def compute_metrics(*, facts: Sequence[FactRecord], profile: CompanyProfile,
                    fiscal_years: Sequence[FactPeriod]) -> MetricSet
```

`fiscal_years` 是**真实财年**（升序、来自资料层，不由自然年反推）。`facts` 按 `metric` 与 `period.end` 索引。

指标规则（逐条实现，逐条测试）：

| key | 公式 | 必须满足 | 不满足时的状态与说明 |
| --- | --- | --- | --- |
| `revenue_yoy` | `(cur - prev)/prev`，同币种同范围同定义 | 相邻两个完整财年；`semantics` 四要素（accounting_standard/reporting_scope/equity_scope/per_share_basis）一致；`currency` 相同 | 基期为 0 → `NOT_APPLICABLE` + note `"prior period is zero; 金额变化 <x> 已给出"`；利润类指标转亏（prev>0, cur<0）→ `NOT_APPLICABLE` + note `"turned to loss: <金额变化>"`，**不输出增长率** |
| `revenue_cagr_3y` | `(end/start)**(1/3) - 1` | 四个连续完整财年（无缺期、无 gap）；首末均为正；口径兼容 | 缺期/不连续 → `MISSING_INPUTS` + 列出缺哪一期；首末非正 → `NOT_APPLICABLE`；首末期间长度差 > 7 天 → `NOT_APPLICABLE` + `"fiscal year length changed"`；差 ≤ 7 天（52/53 周）→ `OK` + note `"52/53-week fiscal year; lengths <n>/<m> days"` |
| `roe_avg_equity` | `profit_parent / ((equity_parent_begin + equity_parent_end)/2)` | 非金融；同 `equity_scope`；第 N 个财年需要第 N 与第 N-1 个期末的 `equity_parent`（即 N 年 ROE 要 N+1 个期末观察） | 缺期初权益 → `MISSING_INPUTS` + `"beginning equity unavailable; end-only equity is not substituted"`；银行/ADS → `NOT_APPLICABLE` + 原因；`basis` 必须写明"平均权益口径，区别于加权 ROE" |
| `debt_ratio` | `total_liabilities / total_assets`（同一日期/同一 instant） | 两者 `period.end` 相同、`reporting_scope` 一致 | 缺 `total_liabilities` → `MISSING_INPUTS`（**不得**由其他项目推导，不得用有息负债比率代替）；银行 → `OK` 但 note `"bank: 不使用普通企业阈值判断"` |
| `cfo_to_profit` | `operating_cash_flow / profit_ordinary_shareholders`（同期间） | 同期间、同币种 | 利润 ≤ 0 或分母为 0 → `NOT_APPLICABLE`；银行 → `NOT_APPLICABLE` + `"bank: 不套用普通企业现金质量结论"` |
| `fcf_simple` | `operating_cash_flow - capex_ppe`（同期间） | 非金融；`capex_ppe.applicability == "applicable"` 且 `sign_convention` 命中白名单 `FCF_ELIGIBLE_CAPEX`（首期只含中电式 `"capital expenditure"` 口径）；同币种同期间 | 定义不等价 → `NOT_APPLICABLE` + 原因**点名具体定义差异**（汇丰 `net purchases and sales`、腾讯 `prepayments for property/construction in progress/investment property`）；银行 → `NOT_APPLICABLE`；**永不命名为 FCFE** |

统一规则：
- 所有指标 `currency` 必须等于 `profile.report_currency`；跨年报表币种变化 → `NOT_APPLICABLE` + `"reporting currency changed"`，**不做换算后趋势**。
- `basis` 必须完整写出"分子/分母 + 期间 + 口径 + 是否原币"，页面直接展示，不允许空字符串。
- 数值用 `Decimal`；`Quantity.unit` 用 `"ratio"`/`"USD"`/`"USD/year"` 等既有风格（`evidence/snapshots.py:20-41` 的 `UNITS` 命名习惯）；比率类不带 currency。
- `gaps` 必须包含 `"gross_margin: 收入与成本字段本轮未逐家补验，不交付"` 与 `"net_margin: 同上"`，以及本次未参与计算的 metric 缺失原因。
- 不输出任何"买入/卖出/评分"措辞（守卫测试会扫源码与页面）。

## 验证（`tests/unit/test_material_metrics.py`）

fixture `pilots.json` 用**已验证的 2024 数额**（`research/market-data-validation/remaining-validation.md` §1 表 + `derived/native-hk/result.json`）：汇丰（USD，银行）`revenue_net_credit=62440e6`、`profit_ordinary_shareholders=22917e6`、`total_assets=3017048e6`、`equity_parent=184973e6`、`operating_cash_flow=65305e6`、`capex_ppe=1344e6`；中电（HKD）90,964 / 11,742 / 233,713 / 104,055 / 23,140 / 15,076（百万）；腾讯（CNY）660,257 / 194,073 / 1,780,995 / 973,548 / 258,521 / 62,927（百万）。多年度序列由本任务按同一结构构造并**在 docstring 注明哪些年份是构造值**（构造值只能用于测试规则分支，不得当成已验证事实）。

| 用例 | 断言 |
| --- | --- |
| 汇丰（银行） | `roe_avg_equity`/`cfo_to_profit` → `NOT_APPLICABLE` 且原因含 bank；`debt_ratio` → `OK` + bank note；`fcf_simple` → `NOT_APPLICABLE` + 原因含 `net purchases and sales` |
| 腾讯 capex 口径 | `fcf_simple` → `NOT_APPLICABLE`，原因含 prepayments/construction in progress |
| 中电 FCF | 给定四年序列时 `fcf_simple == OCF - capex`（`Decimal` 精确相等） |
| JPM 缺 capex | `capex_ppe.raw_value is None` → `fcf_simple` 为 `MISSING_INPUTS`（不是 0、不是 `NOT_APPLICABLE`） |
| 基期为 0 | `revenue_yoy` 基期 0 → `NOT_APPLICABLE` + 金额变化落在 notes 里，**没有**增长率数字 |
| 转亏损 | 上年利润正、本年负 → `NOT_APPLICABLE` + note 含 `loss` 与两期金额 |
| CAGR 四期 | 四期连续正数 → 三个间隔的 CAGR，`internal` 校验：`(end/start)**(1/3)-1` 与结果一致（1e-12） |
| CAGR 缺期 | 去掉中间一年 → `MISSING_INPUTS` 且 notes 点名缺失财年 |
| 52/53 周 | 首末 364 vs 371 天 → `OK` + note；改成 364 vs 400 → `NOT_APPLICABLE` |
| ROE 需 N+1 期末 | 只有 4 个期末权益算 4 年 ROE → 第 1 年 `MISSING_INPUTS`，其余 3 年 `OK`；断言**没有**用期末权益冒充期初 |
| 币种变化 | 某年 `currency="CNY"` 混入汇丰序列 → 相关指标 `NOT_APPLICABLE` + `reporting currency changed`，无换算值 |
| gaps | 含 gross_margin/net_margin 两条固定缺口说明 |
| 纯函数 | 模块内不 import httpx/sqlalchemy/网络与数据库模块（`grep` 断言或直接检查模块 `__dict__`） |

## 提交

```
feat(materials): deterministic financial metrics with applicability rules
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总）
