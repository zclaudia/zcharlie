# 13 年度 PE 条件启用（可选尾部）

> 波次 W6（**仅当剩余时间 ≥45 分钟才启动**）‖ 可与 14 并行。依赖：09 已合并。
> 文件所有权：见 [`README.md`](README.md) §4 第 13 行。

**目标：** 实现年度 PE 的算术与门控，**默认关闭**并输出具体缺口；把"股数基础/汇率/权限未验收"变成可展示的缺口而不是猜测值。

**决策：** 首期没有验收通过的每日价格源，也没有验收过的汇率时间点能力 → 能力位 `annual_pe=False`。本任务交付的是：可测试的算术、明确的关闭理由、以及一个显式注入输入时才能算的内部函数。

## 设计要求（`hk-us-expansion-design.md` §估值和股数）

- `年度 PE = 同股数基础的价格 / 同范围年度 EPS`，先把 EPS 转为报价币种。
- 基本与稀释 EPS **分别命名**，**不能标成 TTM**；EPS 非正 → 不适用。
- TSM 若启用 ADS PE：需核对普通股 EPS × 五股比例，再用**经过验收的当日 TWD→USD 汇率**转换；20-F 的便利阅读换算不是估值日汇率 → 比例与汇率未同时验收前保持关闭。
- 价格与 EPS、股数必须具有**相同股数基础**（拆股调整后的 Close 不能直接配拆股前的 EPS）。

## 文件清单

```
新增  src/investment_agent/materials/valuation.py
新增  tests/unit/test_material_valuation.py
```

## 实现

```python
CAPABILITY = {"annual_pe": False, "annual_pe_basic": False, "annual_pe_diluted": False}
DISABLED_REASONS = (
    "no verified daily price source in the first release",
    "quote-currency conversion requires a verified same-date FX record",
)

@dataclass(frozen=True)
class PriceInput:
    close: Decimal; trade_date: date; currency: str
    share_basis: str          # 如 "split_adjusted" / "as_traded"
    is_split_adjusted: bool

@dataclass(frozen=True)
class FxInput:
    pair: str                 # "TWD/USD"（按报价币种分子）
    rate: Decimal; as_of: date
    source: str; source_date_precision: str

def annual_pe(*, price: PriceInput | None, eps: FactRecord, profile: CompanyProfile,
              fx: FxInput | None = None) -> MetricResult
    """EPS 非正 / 股数基础不一致 / 无价格 / 无汇率 → 明确状态与原因；
    只有输入齐备且同一股数基础时才返回 OK 的 annual_pe_basic 或 annual_pe_diluted。"""

def ads_pe_gap(profile: CompanyProfile, *, fx: FxInput | None) -> tuple[str, ...]
    """ADS 证券（TSM）的缺口清单：比例来源、比例有效期、当日汇率、股数基础。"""
```

规则：
1. `eps.semantics.per_share_basis` 决定 key：`BASIC` → `annual_pe_basic`，`DILUTED` → `annual_pe_diluted`，`NA` → `NOT_APPLICABLE`。
2. `eps.value.raw_value is None`（缺失）→ `MISSING_INPUTS` + 原因；`<= 0` → `NOT_APPLICABLE` + `"EPS non-positive"`。**永不**输出 TTM 命名（`grep` 断言模块内无 `ttm`/`TTM`）。
3. 币种：`eps.currency == price.currency` 时直接算；不等时**必须**有 `fx`，且 `fx.source_date_precision` 为 `DATE`/`TIMESTAMP`（未知精度不接受）；`fx.as_of` 与 `price.trade_date` 相差 > 3 天 → 拒绝并说明"不静默沿用旧率"。
4. 股数基础：`price.is_split_adjusted` 与 EPS 的配股假设不一致（首期只有 AAPL 拆股案例可判定：`eps.period.end < 拆股日 <= price.trade_date`）→ `NOT_APPLICABLE` + `"share basis mismatch: split between EPS period and price date"`。拆股日与比例作为常量 `SPLIT_EVENTS = {"AAPL": (("2020-08-31", 4),)}` 写在模块里并注明来源，**不得**由价格序列反推。
5. `profile.is_ads` → 必须走 `ads_pe_gap` 分支并要求 `profile.ads_ratio` 有来源（01 的名单）；本口径下默认 `NOT_APPLICABLE` + 缺口清单（首期 TSM 一律不输出 ADS PE）。
6. `average PE`、`历史分位`、`TTM PE`、`PB`：**不实现**，在 `gaps` 里显式列出未支持。

## 验证（`tests/unit/test_material_valuation.py`）

| 用例 | 断言 |
| --- | --- |
| 基本/稀释分开 | 同一输入下 `annual_pe_basic` 与 `annual_pe_diluted` 各自使用对应 EPS，`basis` 文本不同且都不含 TTM |
| EPS 非正 | `eps <= 0` → `NOT_APPLICABLE`，无数值 |
| 股数基础不一致 | AAPL 2020-08-31 前期间的 EPS + 之后日期的拆股调整价格 → `NOT_APPLICABLE` + 原因含 split |
| 拆股前后一致 | 拆股后期间 EPS + 拆股调整价格 → 可以计算（若汇率/币种齐备） |
| 缺价格 | `price=None` → `MISSING_INPUTS` + `DISABLED_REASONS` 中至少一条出现在 notes |
| 跨币种缺汇率 | EPS TWD + 价格 USD 且 `fx=None` → `MISSING_INPUTS`，**不出现**任何便利换算值（断言 `"convenience"`/`1.0` 之外的换算不存在） |
| 汇率越界 | `fx.as_of` 距交易日 10 天 → 拒绝并说明，不静默沿用 |
| TSM | `profile.is_ads` → `NOT_APPLICABLE` + 缺口含比例与当日汇率；无任何数值 |
| 能力关闭 | 模块 `CAPABILITY["annual_pe"] is False`，且 09 的 `financial_context` 中 `capabilities["annual_pe"] is False`（跨任务断言，若 09 未按此实现则以 09 为准并记录） |
| 无 TTM 字样 | 模块源码不含 `ttm`（大小写不敏感） |

## 提交

```
feat(materials): gated annual PE with explicit capability gaps
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总）
