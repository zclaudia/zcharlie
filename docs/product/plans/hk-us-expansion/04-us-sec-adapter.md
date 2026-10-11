# 04 美股 SEC 适配器

> 波次 W2 ‖ 可与 03、05 并行。依赖：02 已合并。
> 文件所有权：见 [`README.md`](README.md) §4 第 04 行。**禁止修改 `materials/contracts.py`**、**禁止引入新依赖**（离线环境没有 edgartools；本任务自己写有界解析）。

**目标：** CompanyFacts + Submissions（含历史分片）接入为受控来源，产出两类 dataset：`annual_facts`（每公司每财年一份）与 `filing_index`（每公司一份，承载申报与版本状态）。版本选择与资格判断由应用拥有，不依赖任何第三方解析库。

**决策：**
- **不引入 EdgarTools**：`pyproject.toml` 不动。设计把"版本选择和资格判断"归属应用（设计 §美股财报适配），本任务用自有有界解析实现；将来引入须固定版本并重跑版本案例（记入遗留）。
- 财年边界取自发行人自己的申报期间（`form=10-K` 的 `reportDate`、`start`/`end`），**不用 frames 的自然年标签**，不硬编码 12-31。AAPL/COST 的 52/53 周保留真实天数。
- 严格离线：解析函数是纯函数；HTTP 只出现在 `requests()`/`fetch` 侧，单测用 fixture 直接调 `parse()`。

## 必须读的参考（只读，绝对路径）

- `/Users/zhvala/SourceCode/zcharlie/research/market-data-validation/scripts/directed_us_probe.py` —— 五家解析检查与字段口径。
- 同一目录 `validation.md:19-34, 76-83` —— **LNC 案例的权威事实**（下方回归用例直接引用）。
- `directed-validation.md:60-72` —— 20 个固定年度申报的"接受时间前一秒不可见"检查。
- `hk-us-expansion-design.md:118-128` —— 版本策略与 EdgarTools 边界。
- 原始响应：`evidence/remaining-final-03/raw/us/{AAPL,COST,JPM,XOM,TSM}/{companyfacts,submissions}.json`（JPM 另有 `shards/`）；LNC 用 `evidence/validated-run-01/raw/sec/{companyfacts,submissions}.json` + `derived/sec/cutoffs.json`。

## 文件清单

```
新增  src/investment_agent/materials/us_sec.py
新增  tests/fixtures/materials/us/{AAPL,COST,JPM,XOM,TSM}/{companyfacts,submissions}.json （裁剪）
新增  tests/fixtures/materials/us/LNC/{companyfacts,submissions}.json                    （裁剪）
新增  tests/unit/test_material_us_sec.py
```

裁剪规则：只保留本任务用到的 taxonomy/concept/unit 与必要申报（AAPL/COST 各 2 个财年、JPM/XOM/TSM 各 1 个财年、LNC 保留 2022 期间的 `us-gaap:NetIncomeLoss` 全部观察值与三个 accession）。原始 `companyfacts.json` 单文件 3.8–8 MB，裁剪后应 < 80 KB。**保留 `accn`、`filed`、`form`、`fy`、`fp`、`start`、`end`、`val`、`frame` 字段**（`frame` 只为证明"不依赖它"）。fixture 里不得出现完整 20 个申报的索引表，只留测试需要的最小集合，并在 docstring 说明裁剪与来源。

## 实现

```python
SOURCE_FACTS = "sec.companyfacts.v1"       # annual_facts
SOURCE_FILINGS = "sec.submissions.v1"      # filing_index
ADAPTER_VERSION = "1"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SHARD_URL = "https://data.sec.gov/submissions/{name}"      # filings.files[].name
USER_AGENT = "zcharlie-research/0.1 (personal research; contact in config)"
MIN_INTERVAL_SECONDS = 0.5          # 全应用共享限速由 06 注入；适配器内只声明下限

class SecFactsAdapter:
    def requests(self, identity, *, periods) -> tuple[UpstreamRequest, ...]
    def parse(self, identity, *, raw, periods) -> ParseResult
```

`requests()`：CompanyFacts 一个、Submissions 一个；Submissions 解析出 `filings.files[]` 后为每个**历史分片**再各发一个请求（分片必须一并处理，设计 §调研证据表最后一列）。CIK 由 `source_symbol(identity, SOURCE_FACTS)` 提供，写成 10 位零填充。

`parse()` 的硬规则：

1. **事实选择键**：`CIK + taxonomy + concept + unit + start/end + accession + form`。`duration`（有 `start`）与 `instant`（无 `start`）分开处理，绝不混用。
2. **期间与财年**：年报期间由 `form in {"10-K", "20-F"}` 的申报 `reportDate`（或事实的 `start`/`end`）确定；`fy`/`fp` 只作为标签参考。`FactPeriod.days` 用 `(end-start).days` 真值（AAPL 财年 364/371 天都必须能出现，且不得被改写成 365）。
3. **不使用 `frame`** 选择事实；`frame` 只允许出现在 payload 里作为对照信息（测试要断言"删掉 frame 后结果不变"）。
4. **缺失不猜**：某 metric 的标准标签在本公司不存在（例如 JPM 的固定资产现金支出）→ 产出 `raw_value=None` + `missing_reason`（写"standard tag absent in CompanyFacts for this issuer"），**不得**用相近标签（如 `PaymentsToAcquirePropertyPlantAndEquipment` 之外的自定义标签）替代。
5. **定义不替换**：XOM 的 `revenue_operating` 必须绑定标准 `Revenues`（`us-gaap:Revenues`）的 `source_field`；不得改绑纯销售额标签。JPM 的 `net_profit` 为标准 GAAP 口径；不得替换成管理口径。
6. **IFRS / ADS**：TSM 走 `ifrs-full` taxonomy + `TWD` unit，`report_currency="TWD"`，`adapter` 不把 TWD 换算成 USD；`annual_facts` 里的 `profit_*` 只取 20-F 系列口径（不拼接台湾本地报告的不同利润值）。
7. **taxonomy 白名单**：`us-gaap:*` 与 `ifrs-full:*`；其他 taxonomy（`dei`、`srt`、自定义扩展）不产事实，计数进 `unmapped`。
8. **concept 映射表**（`CONCEPT_MAP`，逐条写进代码注释其口径）至少包含：
   `Revenues`/`RevenueFromContractWithCustomerExcludingAssessedTax` → `revenue_operating`；
   `NetIncomeLoss` → `net_profit`；`ProfitLoss` → `profit_parent`；`StockholdersEquity` → `equity_parent`；
   `Assets` → `total_assets`；`Liabilities` → `total_liabilities`；
   `NetCashProvidedByUsedInOperatingActivities` → `operating_cash_flow`；
   `PaymentsToAcquirePropertyPlantAndEquipment` → `capex_ppe`；
   `EarningsPerShareBasic`/`EarningsPerShareDiluted` → `eps_basic`/`eps_diluted`（`per_share_basis`）；
   `WeightedAverageNumberOfSharesOutstandingBasic` → `shares_weighted_basic`；
   `CommonStockSharesOutstanding`/`EntityCommonStockSharesOutstanding` → `shares_outstanding_end`（instant）。
   每个映射都要有对应 fixture 断言，**映射不到的 metric 不要求强行产出**。
9. `equity_scope`/`reporting_scope`/`accounting_standard` 必须显式填（不落 `unknown` 除非确实无从判断）；`equity_parent` 与 `equity_ordinary` 不得互填。
10. **时间**：`filed`（日期）→ `source_published_date`，`acceptanceDateTime`（时刻，UTC）→ `accepted_at` + `source_published_at` + `source_published_precision=TIMESTAMP`；只有日期时 `precision=DATE`；都缺 → `UNKNOWN`。`source_available_at` 一律留空（未知就是未知）。
11. **`filing_index` payload**：
```json
{
  "cik": "0000320193",
  "filings": [{"accession":"...","form":"10-K","filing_date":"2024-11-01","accepted_at":"2024-11-01T18:04:00+00:00",
               "report_period":"2024-09-28","primary_document":"aapl-20240928.htm","is_amendment":false}],
  "shards": ["CIK0000320193-submissions-001.json"],
  "periods": {"2024-09-28": {"selected_accession":"...","status":"current",
                             "candidates":[{"accession":"...","state":"current","usable":true}]}},
  "unreliable_notices": [{"accession":"...","form":"8-K","accepted_at":"...","items":["4.02"],"known_at":"..."}]
}
```
12. **版本状态机**（纯函数，`select_period_state(filings, notices)`），状态取值固定为 `current` / `superseded` / `nonreliance_notice_known` / `conflict`：
   - `10-K/A` 存在且接受时间晚于原始 `10-K` → 原始为 `superseded`、修订为 `current`；**不能只看 `/A` 后缀或 `is_restated`**（LNC 案例里修订事实的 `is_restated` 仍为 false）。
   - 出现 Item 4.02 的 8-K 且其接受时间晚于原申报 → 该期间标记 `nonreliance_notice_known`，`usable=false`，**旧值保留可追溯**。
   - 存在多个候选且无法判定谁替代谁（例如后续报告再次引用旧期间的比较值）→ `conflict` 并保留全部候选，**不自动拼接增长率**。
   - 若安装库版本变化 → 重跑本任务测试（写进模块 docstring）。

## 验证（`tests/unit/test_material_us_sec.py`）

| 用例 | 断言 |
| --- | --- |
| AAPL 52/53 周 | 至少两个财年的 `FactPeriod.days` 出现 364 与 371（或两者之一），且 `start/end` 与申报 `reportDate` 一致；不得是 12-31 |
| COST 非自然年 | 财年结束日不是 12-31；期间长度在 364±1 / 371±1 内 |
| AAPL 字段 | `revenue_operating`/`net_profit`/`total_assets`/`equity_parent`/`operating_cash_flow`/`eps_basic`/`eps_diluted`/`shares_weighted_basic` 各一条，单位 `USD`/`USD/share`/`shares` 正确，金额为十进制字符串且与 fixture 值一致 |
| JPM 缺失不猜 | 固定资产现金支出：`raw_value is None` + `missing_reason` 非空；且 payload 里不存在用相似标签补出的近似值 |
| XOM 定义 | `revenue_operating.source_field` 为标准 Revenues 概念；换成纯销售额概念会失败的断言（负例） |
| TSM IFRS/TWD | `accounting_standard=IFRS`、所有金额 `currency=TWD`、无任何 USD 换算值与"便利阅读"汇率；`source_field` 以 `ifrs-full:` 开头 |
| frame 无关 | 同一 fixture 删除全部 `frame` 字段后 `parse()` 结果（drafts 的 facts 值与期间）不变 |
| instant vs duration | `total_assets`/`shares_outstanding_end` 的 `period.start is None` 且 `basis=INSTANT`；收入类 `basis=ANNUAL` 且 `start` 存在 |
| 时间精度 | 有 `acceptanceDateTime` 的事实 `precision=TIMESTAMP` 且 `accepted_at` 时区为 UTC；只有 `filed` 的事实 `precision=DATE` 且 `source_published_at is None`；`source_available_at` 全为 None |
| LNC 三时点 | 见下 |
| 历史分片 | `requests()` 对 JPM fixture 返回 CompanyFacts + Submissions + 每个 `files[].name` 各一个请求；`parse()` 合并分片后 `filings` 覆盖早期申报 |
| 错误结构 | CompanyFacts 正文为 HTML / `facts` 缺失 / CIK 不匹配（响应里 CIK ≠ 请求 CIK）→ 对应 `problems`，`drafts==()`，不抛异常 |

**LNC 回归用例**（必做，逐条断言，事实见 `validation.md:21-34`）：
- CIK `0000059558`，`us-gaap:NetIncomeLoss`，USD，期间 `2022-01-01..2022-12-31`。
- 原始 10-K accession `0000059558-23-000009`，接受时间 `2023-02-16 21:19:28 UTC`，值 `-2227000000`。
- 修订 10-K/A `0000059558-23-000019`，接受时间 `2023-03-30 21:13:26 UTC`，值 `-2241000000`。
- 不可依赖公告 8-K `0001193125-23-080586`，Item 4.02，接受时间 `2023-03-27 21:05:57 UTC`。
- 同一期间在后续观察中还存在 `+1358000000`：必须保留为候选，**不自动覆盖、不拼接**。
- `select_period_state` 在四个时点上的输出与 `evidence/validated-run-02/derived/sec/cutoffs.json` 一致（读取该文件做对照断言；若字段名不同，按实际结构调整并记录）。
- 修订事实的 `is_restated` 为 false → 断言状态机**不依赖** `is_restated`。

## 提交

```
feat(materials): SEC CompanyFacts and Submissions adapter with filing state machine
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总）
