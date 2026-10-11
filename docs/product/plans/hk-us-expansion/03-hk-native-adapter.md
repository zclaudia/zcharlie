# 03 港股原币适配器

> 波次 W2 ‖ 可与 04、05 并行。依赖：02 已合并（`materials/contracts.py`）。
> 文件所有权：见 [`README.md`](README.md) §4 第 03 行。**禁止修改 `materials/contracts.py`**（冻结）；来源特有字段进 `payload`。

**目标：** 把已实测的三个东财原币端点接成受控来源，产出 `DatasetDraft(kind=ANNUAL_FACTS, subject_kind=COMPANY)`；离线可测，错误结构明确暴露，缺失不猜。

**决策：** 只映射已逐字段核对过的项目（下表 18 项）。`RPT_HKF10_FN_*_PC`（人民币展示值）与 `MAININDICATOR` **不作为金额来源**；未映射明细行不产出事实，只在 `problems` 里计数。

## 必须读的参考（只读，绝对路径）

- `/Users/zhvala/SourceCode/zcharlie/research/market-data-validation/scripts/native_hk_probe.py` —— **口径权威**：`REPORTS`、`query()` 参数、结构校验、`SPECS` 的 18 项映射与符号规则（第 11-80 行、82-205 行）。
- `/Users/zhvala/SourceCode/zcharlie/research/market-data-validation/remaining-validation.md` §1、§2 —— 单位（接口为元、PDF 为百万）、汇丰口径差异、7.1884 展示乘数只用于复现差异。
- 原始响应：`evidence/remaining-final-03/raw/native/{00005,00002,00700}/*.json`（`income-four-years.json`、`balance-four-years.json`、`cashflow-original-four-years.json`）。
- 逐字段核对结果：`evidence/remaining-final-03/derived/native-hk/result.json`（`companies.<code>.comparisons` 含页码、官方金额、符号规则）。

## 文件清单

```
新增  src/investment_agent/materials/hk_native.py
新增  tests/fixtures/materials/hk/00005/{income,balance,cashflow}.json      （裁剪样本）
新增  tests/fixtures/materials/hk/00005/{error_body,html_body,truncated}.json
新增  tests/unit/test_material_hk_native.py
```

fixture 必须裁剪：只留必要行、去掉四年度冗余（单年或两年），但**保留** 字段结构 `success/result.count/pages/data`、`STD_ITEM_CODE/ITEM_NAME/AMOUNT/CURRENCY_CODE/STD_REPORT_DATE/START_DATE/REPORT_DATE/FISCAL_YEAR/ACCOUNT_STANDARD/REPORT_TYPE`、cashflow 的 `VALUE_LIST` 嵌套。单文件应 < 60 KB。docstring 里写明裁剪来源与时间。

## 实现

```python
SOURCE = "eastmoney.hk.native.v1"
ADAPTER_VERSION = "1"
BASE = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
REPORTS = {
    "income": "RPT_HKF10_FN_INCOME",
    "balance": "RPT_HKF10_FN_BALANCE",
    "cashflow": "RPT_CUSTOM_HKSK_APPFN_CASHFLOW",
}
# 映射：(report, STD_ITEM_CODE) -> (metric, sign, source_label 说明)
ITEM_MAP = {
    ("income",   "004001999"): "revenue_operating",            # 中电 收入 / 腾讯 Revenues
    ("income",   "001003999"): "revenue_net_credit",           # 汇丰 经营收入总额（信用损失后）
    ("income",   "004025002"): "profit_ordinary_shareholders",  # 股东应占溢利 / Equity holders
    ("income",   "001025002"): "profit_ordinary_shareholders",  # 汇丰 股东应占溢利
    ("balance",  "004009999"): "total_assets",
    ("balance",  "001001999"): "total_assets",
    ("balance",  "004030999"): "equity_parent",
    ("balance",  "001009999"): "equity_parent",
    ("cashflow", "003999"):    "operating_cash_flow",
    ("cashflow", "005005"):    "capex_ppe",                     # 三家均为正数支出规模
}

@dataclass(frozen=True)
class HkNativeAdapter:
    source: str = SOURCE
    adapter_version: str = ADAPTER_VERSION
    def requests(self, identity, *, periods) -> tuple[UpstreamRequest, ...]
    def parse(self, identity, *, raw, periods) -> ParseResult
```

`requests()`：对三张表各一个请求，参数与 `native_hk_probe.query` 一致（`columns=ALL`、`filter=(SECUCODE="00005.HK")(REPORT_DATE>='2021-01-01')(REPORT_DATE<='2024-12-31')(REPORT_TYPE="年报")`、`pageNumber=1`、`pageSize=500`）；`SECUCODE` 由 `source_symbol(identity, SOURCE)` 提供（**前导零必须来自 01 的名单，不在适配器里补**）。`periods` 为空时用默认四年窗口。

`parse()` 的结构校验（**每条失败都进 `problems`，不产出对应表的 drafts**）：
1. 正文必须是 JSON 对象；`js` 解析失败或正文以 `<` 开头 → `problems=("not_json: <name>",)`。
2. `success is not True` → `problems=("upstream_success_false: <name>",)`（HTTP 200 但返回错误对象就是失败）。
3. `result.data` 缺失/类型不对 → `problems=("missing_result_data",)`。
4. 分页完整性：`len(data) != result.count` 或 `result.pages != 1` → `problems=("pagination_truncated",)`。
5. 每张表内币种集合必须唯一（income/balance 用 `CURRENCY_CODE`，cashflow 用内层 `CURRENCY_ABBR`），且等于 `identity.report_currency`；否则 `problems=("currency_mismatch: ...",)` 且该表不产事实。
6. `STD_REPORT_DATE[:10]` 集合必须与请求的 `periods` 一致；出现未请求期间 → `problems=("unexpected_period: ...",)`。
7. `STD_ITEM_CODE` 必须唯一；重复 → `problems=("duplicate_item_code: ...",)`。
8. cashflow 必须是外层行 + `VALUE_LIST` 内层结构；出现 `RPT_HKF10_FN_INCOME_PC` / `MAININDICATOR` 字样或 `_PC` 来源 → `problems=("display_currency_source_rejected",)` **并拒绝整批**（这是硬红线）。
9. 明细行项目名/编码在 `ITEM_MAP` 之外 → 不产事实，`problems` 里累加 `unmapped_items: <table>/<n>`（一行一个计数，不逐个铺开）。

单位与符号：
- `raw_value` = `AMOUNT` 的十进制字符串（接口单位是元），`raw_unit="元"`、`scale=1`、`unit=identity.report_currency`、`currency=identity.report_currency`。
- `capex_ppe` 三家均为"正数支出规模"，报表为括号负号 → `sign_convention` 必须写明：汇丰 `"net purchases and sales of fixed assets, positive spend magnitude"`、中电 `"capital expenditure, positive spend magnitude"`、腾讯 `"purchases and prepayments incl. property/construction in progress/investment property, positive spend magnitude"`。**三家定义不同，不得合并成同一口径**（`sign_convention` 是唯一承载处）。
- `verification = CANDIDATE`（首期港股来源是候选来源，设计 §信息来源表）；`applicability` 按公司画像：银行（汇丰）的 `capex_ppe` 标 `not_applicable` 并给原因。

`payload`（每个 draft）：
```json
{
  "report": "income",
  "report_name": "RPT_HKF10_FN_INCOME",
  "account_standard": "国际会计准则",
  "fiscal_year_label": "12-31",
  "facts": [ { "metric": "...", "source_item_code": "001003999", "source_label": "经营收入总额",
               "amount": "62440000000", "sign_rule": "...", "official_ref": {"url": "...", "page": 12, "label": "Net operating income"},
               "verification": "candidate", "missing_reason": null } ],
  "unmapped_item_count": 3,
  "provider": {"secucode": "00005.HK", "report_type": "年报"}
}
```
`drafts` 粒度 = 每公司 × 每表 × 每报告期一个 draft：`kind=ANNUAL_FACTS`、`subject_kind=COMPANY`、`subject_id=company.id`、`period=FactPeriod(basis=ANNUAL)`、`source=SOURCE`、`captured_at=now`。

**明确缺失的项必须写进 payload 事实列表（`raw_value=None` + `missing_reason`），不要省略**：汇丰的 `revenue_gross`（信用损失前 65,854）与 `credit_loss_expense`（3,414）不在端点明细中——原始响应里只有 `001003999` 经营收入总额 62,440；`equity_ordinary`（普通股净资产）对汇丰不可取（母公司权益含其他权益工具）→ 三条都要以 `missing_reason` 落库，**不得由其他项目反推或按 7.1884 换算**。

## 验证（`tests/unit/test_material_hk_native.py`）

| 用例 | 断言 |
| --- | --- |
| 汇丰 00005 正常路径 | 6 条事实：`revenue_net_credit=62440000000`、`profit_ordinary_shareholders=22917000000`、`total_assets=3017048000000`、`equity_parent=184973000000`、`operating_cash_flow=65305000000`、`capex_ppe=1344000000`；单位/币种 = USD；`raw_value` 为十进制字符串 |
| 中电/腾讯 | `revenue_operating`、`profit_ordinary_shareholders` 值与本任务 fixture 一致；HKD / CNY 各自正确，不出现 CNY 与 HKD 混用 |
| 汇丰缺失项 | `revenue_gross`/`credit_loss_expense`/`equity_ordinary` 三条 `raw_value is None` 且 `missing_reason` 非空；不产生任何近似值 |
| 展示值端点拒绝 | 输入含 `RPT_HKF10_FN_INCOME_PC` 的响应 → `problems` 含 `display_currency_source_rejected` 且 `drafts == ()` |
| HTTP 200 错误正文 | `success=false` + `message` → `problems` 含 `upstream_success_false`，`drafts == ()`，不抛异常 |
| HTML 正文 | `<html>...` → `problems` 含 `not_json` |
| 分页截断 | `pages=2` 或 `len(data) != count` → `problems` 含 `pagination_truncated` |
| 重复编码 | 构造重复 `STD_ITEM_CODE` → `problems` 含 `duplicate_item_code` |
| 错误年度 | `STD_REPORT_DATE=2023-12-31` 而请求 2024 → `problems` 含 `unexpected_period` |
| 币种不符 | 汇丰响应标 `CURRENCY_CODE=HKD` → 该表不产事实 + `currency_mismatch` |
| cashflow 嵌套 | 内层 `VALUE_LIST` 解析出 OCF/capex；外层 `REPORT_DATE` 与请求期间一致 |
| 前导零 | `requests()` 的 SECUCODE 为 `00005.HK`（不是 `5.HK`），来自 identity 而非本地补零 |
| 符号规则 | 三条 capex 的 `sign_convention` 互不相同且含关键词（net purchases / capital expenditure / prepayments） |

每条 `validate_draft` 都要对产出的 draft 调用一次并通过（用 02 的校验器，不重复实现）。

## 提交

```
feat(materials): native-currency HK statement adapter
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总）
