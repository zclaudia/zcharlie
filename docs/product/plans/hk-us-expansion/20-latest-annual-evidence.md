# HK2-04 最新年度原件与字段证据

> 本文保留当时的 partial 结论；后续三项 P0 已收口，当前八家通过结果及边界见 [25](25-hk2-04-closeout-acceptance.md)。

核对截至日：2026-10-15。事实均以发行人年度报告或 SEC 年报为准；下表采用报告币种、百万单位。期间按各发行人的实际完整财年，不把临时报告当成年报。版本化事实清单为 [annual_validation_2026-10-15.json](../../../../config/research_material/annual_validation_2026-10-15.json)，离线生成器为 [build_hk_us_annual_validation_manifest.py](../../../../tools/build_hk_us_annual_validation_manifest.py)。

| 公司 | 最新年度与公布/申报日 | 收入 | 利润 | 期末资产 | 期末权益 | 经营现金流 | 资本开支 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| [中电控股](https://www.clpgroup.com/content/dam/clp-group/channels/investor/document/3-3-financial-reports/2025/e_2025%20Annual%20Report.pdf.coredownload.pdf) | FY2025；2026-03-12 | 88,018 HKD | 股东应占 10,468 HKD | 238,644 HKD | 股东资金 107,610 HKD | 24,378 HKD | 15,539 HKD |
| [汇丰控股](https://www.sec.gov/Archives/edgar/data/1089113/000108911326000010/hsbc-20251231.htm) | FY2025；2026-02-26（20-F） | ECL 前净经营收入 68,274 USD | 普通股股东应占 21,102 USD | 3,233,034 USD | 普通股股东权益 177,509 USD | 29,766 USD | 不适用：披露 1,447 USD 为 PPE 净买卖，不是毛资本开支 |
| [腾讯](https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0409/2026040901231.pdf) | FY2025；2026-04-09 | 751,766 CNY | 母公司股东应占 224,842 CNY | 2,038,986 CNY | 母公司股东权益 1,154,152 CNY | 303,052 CNY | 不适用：87,482 CNY 合并 PPE、建设中物业及投资物业付款，不能标作纯 PPE 资本开支 |
| [Apple](https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm) | FY2025（2024-09-29 至 2025-09-27）；2025-10-31（10-K） | 净销售 416,161 USD | 净利润 112,010 USD | 359,241 USD | 股东权益 73,733 USD | 111,482 USD | PPE 购置 12,715 USD |
| [Costco](https://www.sec.gov/Archives/edgar/data/909832/000090983226000093/cost-20260830.htm) | FY2026（2025-09-01 至 2026-08-30）；2026-10-07（10-K） | 总收入 303,154 USD | 净利润 9,226 USD | 89,045 USD | 股东权益 35,803 USD | 15,817 USD | PPE 购置 6,435 USD |
| [JPMorgan Chase](https://www.sec.gov/Archives/edgar/data/19617/000162828026008131/jpm-20251231.htm) | FY2025；2026-02-13（10-K） | GAAP 收入 182,447 USD | 净利润 57,048 USD | 4,424,900 USD | 股东权益 362,438 USD | (147,782) USD | 不适用：银行控股公司，不把常规投资支出重命名为 PPE 资本开支 |
| [Exxon Mobil](https://www.sec.gov/Archives/edgar/data/34088/000003408826000045/xom-20251231.htm) | FY2025；2026-02-18（10-K） | 收入及其他收入 332,238 USD | ExxonMobil 应占净利润 28,844 USD | 448,980 USD | 股东权益 259,386 USD | 51,970 USD | PPE 购置 28,358 USD |
| [台积电](https://www.sec.gov/Archives/edgar/data/1046179/000162828026025362/tsm-20251231.htm) | FY2025；2026-04-16（20-F） | 3,809,054.3 TWD | 母公司股东应占净利润 1,697,604.0 TWD | 7,932,842.5 TWD | 母公司股东权益 5,355,038.7 TWD | 2,274,975.6 TWD | PPE 购置 1,272,410.5 TWD |

“百万”是表中数值单位；原件金额、币种、倍率、现金流符号及引用均保存在清单。括号表示原件披露的负数，例如 JPM 经营现金流。CLP 原件将资本开支列作括号数；清单保留项目约定 `capital expenditure` 表示正的支出金额。其他资本开支事实采用正支出金额约定，并在来源证据中保留括号及 XBRL 原符号。台积电财务数按 20-F 的 TWD 报表列，不能把 ADS 的 USD 市场报价当作报表币种。

证据包包含 8 份最新年报、官方年度索引/披露记录、SEC CompanyFacts 与 Submissions 原始响应，以及 Submissions 正文引用的全部 79 个 SEC 分片（79/79 原件已抓取并逐项校验）。每个原件正文均在忽略的 `data/hk2-latest-evidence/2026-10-15/raw/`，清单记录实际抓取时间、状态码、URL、字节数与 SHA-256；人工复核片段另存于 `tests/fixtures/materials/annual_validation/`。这些原始响应没有纳入 Git。清单重建不联网，另用内嵌 XBRL 身份、上下文、单位及数值定位原始 SEC 表格行。

八家公司都已提供最新年度六类字段和连续四个完整年度的核心收入、利润、权益及期初权益证据；历史六类字段覆盖另按公司逐年列在离线报告，不把最新年度六类覆盖扩大解释为每年六类全覆盖。当前可核验覆盖不等于六项指标均可计算或可发布：例如银行、腾讯的资本开支是有出处的不适用项；Apple 的原件利润为 `net_profit`，现有 ROE 计算只接受与权益归属匹配的 `profit_parent` 或 `profit_ordinary_shareholders`，不得通过改名制造可用输入。这里是应用输入映射的缺口：FASB 将 `us-gaap:NetIncomeLoss` 定义为归属于母公司的净利润（[官方分类表](https://xbrl.fasb.org/impdocs/OCI_TIG/othercompincome.htm)），不能把该标签误述为包含少数股东的总利润。Costco FY2022–FY2023 的 XBRL 权益标签明确包括非控股权益，后续年份采用不同权益标签，保留该范围变化；FY2024 的期初权益取自 FY2023 期末的 total-equity 列，与 FY2024 期末 parent-equity 列范围不一致。汇丰 FY2021 原始 IFRS 4 与后续 IFRS 17 重述存在不可比边界，四年序列采用 FY2025 年报中的一致重述列并明确保留该限制。台积电本报告选用 SEC 20-F 的 TWD 报表输入；本轮未核对当地语言年报，故不声称已复核两者差异。台积电 ADS 的 USD 市场报价与 TWD 报表金额分别处理，不产生未验证 ADS 口径 ROE。

在已安装本工作树依赖的 Python 环境中，原件核验与计算覆盖可由 `PYTHONPATH=src python -m investment_agent.cli.main materials verify-annual --manifest config/research_material/annual_validation_2026-10-15.json --source-root data/hk2-latest-evidence/2026-10-15/raw` 离线重放。最终验证报告为部分完成：8/8 最新年度、48/48 最新字段证据、79/79 SEC 分片通过核对，8/8 四年期间连续、原始核心字段证据齐备；5/8 公司证据包通过整体计算资格门槛（中电、腾讯、JPMorgan、Exxon Mobil、台积电）。汇丰因 IFRS 4/17 重述冲突保持部分状态；Apple 的 ROE 输入不符合现有公式允许的利润标签；Costco 因权益范围变化，FY2024 的期初（FY2023 期末）总权益不能与 FY2024 期末归母权益直接匹配，且现有公式不接受其 `net_profit` 输入标签。验证状态只描述该契约的证据、口径和计算资格，不代表通用财务事实分类器覆盖所有财务语义。此项一次性研究不改变应用在线刷新设置；`ONLINE_REFRESH_ENABLED` 仍为 `False`。
