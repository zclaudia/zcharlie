# 14 分红事件展示（可选尾部）

> 波次 W6（**仅当剩余时间 ≥45 分钟才启动**）‖ 可与 13 并行。依赖：09 已合并。
> 文件所有权：见 [`README.md`](README.md) §4 第 14 行。

**目标：** 把已核对的分红事件表达成受控资料，**最终金额确定时间**单独记录；在覆盖不完整时不输出任何"完整年度/TTM 股息率"。

**决策：** 事件本身（金额、日期）已可核对，但历史可得时间与完整覆盖未验收 → 首期只展示事件与缺口，股息率按命名规则逐项判定。

## 设计要求与权威事实

`hk-us-expansion-design.md` §分红：事件分别保存声明币种与金额、普通/特别类型、每股类别、公告/除息/登记/派付日期，以及最终支付币种金额、汇率和**最终金额确定时间**；不能用除息日填全部字段的 `available_from`。

权威事实（`remaining-validation.md` §5，测试直接引用）：

| 事件 | 公告 | 除息 | 登记 | 派付 | 金额 |
| --- | --- | --- | --- | --- | --- |
| 汇丰 2024 第四次中期 | 2025-02-19 | 2025-03-06 | 2025-03-07 | 2025-04-25 | 每普通股 USD 0.36；**最终 HKD 2.791007 于 2025-04-14 才确定**（USD 0.36 × 7.752798） |
| 中电 2024 第四次中期 | 2025-02-24 | 2025-03-07 | 2025-03-11 | 2025-03-20 | HKD 1.26（公告时已确定） |
| 苹果 2025 第一季度 | 2025-01-30 | 未用交易所来源独立核对 | 2025-02-10 | 2025-02-13 | USD 0.25 |

汇丰 2024 财年分红 USD 0.87 **含特别股息 USD 0.21**；剔除特别为 USD 0.66。
原始材料：`evidence/remaining-final-03/raw/dividends/{hsbc-official.html,hsbc-fx.pdf,clp-declaration.pdf,clp-annual-announcement.pdf,apple-declaration.html}`。

## 文件清单

```
新增  src/investment_agent/materials/dividends.py
新增  tests/unit/test_material_dividends.py
```

## 实现

```python
CAPABILITY = {"dividends": False, "dividend_yield": False}   # 在线事件源未启用；事件可由离线导入

class DividendType(StrEnum): ORDINARY = "ordinary"; SPECIAL = "special"

@dataclass(frozen=True)
class DividendEvent:
    declared_currency: str; declared_amount: Decimal; share_class: str      # "ordinary" / "ads"
    kind: DividendType
    announced_on: date
    ex_date: date | None; record_date: date | None; pay_date: date | None
    final_currency: str | None; final_amount: Decimal | None
    final_fx_rate: Decimal | None; final_fx_source: str | None
    final_amount_determined_on: date | None      # 最终金额确定时间（单独字段）
    available_from: date | None                  # 只能由上述时间推导
    date_precision: str                          # date | unknown（不造时刻）
    source_ref: str; locator: str | None

def event_view(raw: Mapping[str, Any]) -> DividendEvent
    """从导入的原始事件映射出事件；缺字段保持 None，不填默认值。"""

def known_amount_at(event: DividendEvent, at: date) -> tuple[Decimal, str]
    """返回该日期**可知**的金额与口径标签：
    at < announced_on → (0/None, "unknown")；
    announced_on <= at < final_amount_determined_on → (declared_amount, "declared:<currency>")；
    at >= final_amount_determined_on → (final_amount, "final:<currency>")。
    除息日不能把最终金额提前到确定日之前。"""

def dividend_yield(events: Sequence[DividendEvent], *, definition: str,
                   include_special: bool, price: FxInput | None) -> MetricResult
    """definition ∈ {"fiscal_year_declared", "trailing_twelve_months_ex_date",
    "trailing_twelve_months_paid"}；必须逐步判定覆盖完整性与币种一致性。"""
```

规则：
1. `available_from` **不由 `ex_date` 推导**：`final_amount_determined_on` 缺失时，`available_from` 取 `announced_on`（声明金额可知）且口径只能标 `declared`；`final` 金额只有在 `final_amount_determined_on` 存在时才进入可得计算。
2. `known_amount_at` 是唯一对外的时间判定入口；测试要断言"2025-03-10 拿到的是 USD 0.36 / declared"，"2025-04-14 之后才拿到 HKD 2.791007 / final"。
3. 普通与特别分列（`kind`）；汇丰财年总额在含特别与不含特别时给出两个数（0.87 / 0.66），不混称。
4. 每股类别：普通股与 ADS 分别处理；ADS 的税费与存托费用未验证 → 不输出净到账收益率（`gaps` 里写明）。
5. `dividend_yield`：覆盖不完整（缺事件、缺分母、缺汇率）→ `MISSING_INPUTS` 并列出缺口，**不输出估算值**；`definition` 必须回显在 `basis` 里，且 `basis` 说明含不含特别股息、原币还是估算换算、税前口径。取消 `price` 或汇率时不得退化成"仅事件表 + 一个百分比"。
6. 能力位默认 `dividends=False`（在线事件源未启用）、`dividend_yield=False`；页面只展示已核对事件与覆盖缺口。

## 验证（`tests/unit/test_material_dividends.py`）

| 用例 | 断言 |
| --- | --- |
| 汇丰时间线 | `known_amount_at(2025-03-06)` 与 `known_amount_at(2025-03-10)` 都返回 `declared USD 0.36`，**不是** HKD 2.791007；`known_amount_at(2025-04-14)` 返回 `final HKD 2.791007`；`known_amount_at(2025-04-13)` 仍是 declared |
| 除息日不能当可得日 | `available_from != ex_date`；构造一个只有 `ex_date` 的事件 → `available_from is None` 且口径为 `unknown` |
| 最终汇率记录 | `final_fx_rate == 7.752798`、`final_fx_source` 非空、`final_amount_determined_on == 2025-04-14` |
| 中电 | 公告时已确定：`final_amount_determined_on == announced_on`，`known_amount_at(announced_on)` 即为 HKD 1.26 |
| 特别股息分列 | 汇丰财年：`include_special=True` → 0.87；`False` → 0.66；两次数值不同且 `basis` 文本不同 |
| 苹果 | 金额/登记/派付日期与上表一致；除息日为 `None` 时不得编造 |
| 股息率覆盖不足 | 只有 1 个事件时有分母但缺其余事件 → `MISSING_INPUTS` + 缺口清单，返回值为 None |
| 命名三选一 | `definition` 非法值 → 抛 `ValueError`；三种合法值各自的 `basis` 文本互不相同且含定义名 |
| ADS 不输出净收益率 | 对 `share_class="ads"` 的事件请求净到账 → `NOT_APPLICABLE` + 原因含 tax/ADR fees |
| 能力关闭 | `CAPABILITY["dividend_yield"] is False` |

## 提交

```
feat(materials): verified dividend events with final-amount determination dates
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总）
