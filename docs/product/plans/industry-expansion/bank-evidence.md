# 银行同行扩展：FY2025 证据盘点

范围：仅检查 `research/industry-data-validation/evidence/run-02` 已保存数据与当前年度事实导入/原件证据代码。银行样本是有目的抽样的 10 家，不代表全行业。以下“供应商覆盖”只说明本地保存的东方财富/Eastmoney 原始接口响应中有字段和值；不等于官方原件核验，也不等于已获数据使用许可。

## 结论

- 供应商原始 `main` 表在 10 家 × FY2025（2025-12-31）中，净息差 `NET_INTEREST_MARGIN`、不良贷款率 `NONPERLOAN`、拨备覆盖率 `BLDKBBL`、加权 ROE `ROEJQ` 均为数值：各 10/10。六个报告期聚合表也各是 60/60，但该数值覆盖统计不是 PDF 核验。
- 归母净资产不在 run-02 银行覆盖字段集合中：`round2_analyze.py` 对银行只定义上述四项比率；没有对应 bank `balance` 原始文件、归母权益派生值、页码审计记录。`*_main.json` 中虽有 `NZBJE`（供应商名为“净资产金额”）和 `TOTAL_EQUITY_PK`，但它们并非本研究已确认的归母净资产字段；两者在工行 2025 数值分别 5,302,796,000,000 和 4,272,024,000,000，而工行年报补充资料第 216 页明确“归属于母公司普通股股东的年末净资产”为 3,859,602 百万元。不可把 `NZBJE` 或 `TOTAL_EQUITY_PK` 直接映射为归母净资产。
- 仅 3/10 有原件 PDF、CNINFO 公告候选元数据、PDF 哈希和全篇抽取页文本：工行 601398、招行 600036、江苏银行 600919。结构化 `semantic-checks.json` 实际只确认工行四项和江苏银行三项供应商值与抽取 PDF 文本相符；招行虽有原件/元数据/页文本，本轮没有对应 bank 数值审计检查。其余 7 家缺少原件与页码证据。
- 当前 `peer_annual.py` 导入器与默认路径固定为白酒 2025 年样例及 114 条白酒原件审计；bank 专属字段、银行样例和审计输入尚不存在。`evidence.py` 可基于完整字段级证明降级/提升 evidence，但 run-02 银行材料不满足这条链。当前不应把银行字段标成 PDF verified。

## FY2025 原始供应商字段

来源文件均为 `research/industry-data-validation/evidence/run-02/raw/financial/<代码>_main.json`。数值单位为百分比（供应商原响应），不是百分点转小数。列顺序为净息差 / 不良贷款率 / 拨备覆盖率 / 加权 ROE：

| 代码 | FY2025 原始供应商值（%） |
|---|---|
| 601398.SH 工商银行 | 1.28 / 1.31 / 213.60 / 9.45 |
| 601939.SH 建设银行 | 1.34 / 1.31 / 233.15 / 10.04 |
| 601288.SH 农业银行 | 1.28 / 1.27 / 292.55 / 10.16 |
| 600036.SH 招商银行 | 1.87 / 0.94 / 391.79 / 13.44 |
| 601166.SH 兴业银行 | 1.71 / 1.08 / 228.41 / 9.15 |
| 000001.SZ 平安银行 | 1.78 / 1.05 / 220.88 / 9.15 |
| 600919.SH 江苏银行 | 1.73 / 0.84 / 322.98 / 13.14 |
| 601009.SH 南京银行 | 1.82 / 0.83 / 313.62 / 12.05 |
| 002142.SZ 宁波银行 | 1.74 / 0.76 / 373.16 / 13.11 |
| 601128.SH 常熟银行 | 2.53 / 0.76 / 451.25 / 14.05 |

逐行字段值已用 Python 从原始响应中筛出 `REPORT_DATE == 2025-12-31`，并确认十家各有一行、四字段均非空。对应的覆盖统计在 `derived/coverage-summary.json`，逐公司逐报告期记录在 `derived/coverage-cells.json`；采样范围在 `sample.json`。这些 JSON 的成功状态、coverage 的 present 状态不构成原件核验。

归母净资产字段应补充口径确认后的合并资产负债表“归属于母公司股东权益/所有者权益”值，并记录币种、单位、合并范围、期末日期及原始字段。建议先把它列为待补充字段，而非从供应商同名附近指标推断。

## 原件、元数据与页码证据

| 公司 | 本地原件与申报元数据 | 已实际交叉核对的项目 | 可用页码证据及缺口 |
|---|---|---|---|
| 工商银行 601398.SH | `raw/official/601398.SH_2025.pdf`；`derived/filings/601398.SH.json` 含完整报告标题、公告时间、CNINFO URL、PDF SHA-256、408 页；`derived/pages/601398.SH_2025.json` 有 408 页抽取文本 | `derived/semantic-checks.json` 确认四项：1.28、1.31、213.60、9.45 与页面文本一致 | 关键页为 PDF 页 16（年报摘要列出风险指标及加权 ROE）、21（净息差）；`derived/pages` 第 216 页还有归母普通股股东年末净资产 3,859,602 百万元。当前语义检查只记录值与 PDF 文件引用，没有逐指标页码/标题/摘录/哈希 audit row，也没有达到 `originals.py` 和 `evidence.py` 所需的字段证明契约。 |
| 招商银行 600036.SH | `raw/official/600036.SH_2025.pdf`；`derived/filings/600036.SH.json` 含公告元数据、URL、哈希、350 页；`derived/pages/600036.SH_2025.json` 有 350 页抽取文本 | 没有列入银行 `semantic-checks.json` 的指标核对 | 有 PDF 和可搜索抽取页，但没有已保存的逐字段页码/摘录证明。 |
| 江苏银行 600919.SH | `raw/official/600919.SH_2025.pdf`；`derived/filings/600919.SH.json` 含公告元数据、URL、哈希、227 页；`derived/pages/600919.SH_2025.json` 有 227 页抽取文本 | `derived/semantic-checks.json` 确认净息差 1.73、不良率 0.84、拨备覆盖率 322.98 | 抽取页文本中净息差出现于 PDF 页 6、23；不良率/拨备覆盖率出现于页 5、19、28。现有检查以 `f'{value:.2f}' in 全文` 证明数字存在，并未将指标标签和值绑定到特定页的结构化 audit。未核对该公司 ROE 或归母净资产。 |
| 其余 7 家：601939.SH、601288.SH、601166.SH、000001.SZ、601009.SH、002142.SZ、601128.SH | 无对应 `raw/official/<代码>_2025.pdf`、`derived/filings/<代码>.json` 或 `derived/pages/<代码>_2025.json` | 无 | 无原件页码证明。`601128.SH` 的原始公告搜索尝试在 `raw/announcements/601128.SH.json` 中保存但 attempts 为空；不能据此说申报不存在，只能说本地未取得证据。 |

`raw/announcements/<代码>.json` 仅对上述已有三家含公告候选搜索记录；候选元数据和 URL 可定位文件，但只有 PDF 字节、哈希、发行人/报告期识别与字段 locator 一起核验，才构成可复核的原件链。`round2_checks.py` 中 `assert 数值在全文` 的检查适用于这次调研抽查，不是逐字段页码审计。

## 实现边界与下一步建议

`src/investment_agent/cli/commands/peer_annual.py` 的 `FIELD_SPECS` 固定为收入、归母利润/净资产、合同负债、现金流、ROE 等白酒字段，`DEFAULT_SAMPLE_PATH`/`DEFAULT_AUDIT_PATH` 固定白酒样例及审计。银行四指标既不在该映射，也没有对应页面 audit；银行 `main` 表的年度比率不能冒充已验证的年报原文字段。`src/investment_agent/industry_peers/originals.py` 的现有 alias 与页码核查针对部分通用年报会计指标，不含净息差、不良率或拨备覆盖率；`evidence.py` 要求 `original_ref`、`pdf_hash`、`report_identity`、`accounting_scope`、`checked_at`、`check_rule_version`、页码、标题、原始/解析值、摘录及哈希、audit 绑定等完整 proof 才可能 materialize 为 verified。

建议实现分两批：

1. 先支持银行 FY2025 供应商事实保存为 candidate，并保留供应商字段名、原始单位/值、报告期、通知/修订时间、采集时间与来源版本；明确 bank metrics 不复用白酒导入器的假设，也不标为 verified。
2. 补齐十家 FY2025 公告 PDF 元数据、原件、哈希、发行人/年度身份检查和每项页码/字段头/当前列/摘录审计；净资产单独从合并资产负债表取归母口径。每个银行比率建立适用的原件 alias 与检查规则后，再让 proof 驱动 evidence 状态。若首期只实现供应商 candidate，则 UI/计算应清楚展示证据级别和 3/10 原件覆盖边界。

半年度 ROE 有额外口径差异：`derived/semantic-checks.json` 记录招行 2024H1 原摘要为年化 15.44%，同期供应商 `ROEJQ` 为 7.72%。本次 FY2025 年报字段不受该特定半年度问题直接影响，但不应把年度与半年度 ROE 未经规则转换而混排。

## 检查方法与可复现位置

- 只读 Python 检查十个原始 `*_main.json` 的 2025-12-31 行、四项字段非空，并检查 PDF/公告元数据/抽取页文件存在性及页数。
- 阅读了 `source/round2_capture.py`、`source/round2_analyze.py`、`source/round2_checks.py` 和 `source/round2_extra.py`，以及上述三个应用模块。
- 页文本命中位置由本地保存的 `derived/pages/*.json` 检索。关键指标页码与交叉检查范围已按上表区分。
- 本调查未联网、未写数据库、未调用 LLM，也未执行测试套件；除本文件外没有改文件。

## I2 离线导入实现状态

已实现名单入口 `ia peers import-roster --industry bank`（默认仍为 `baijiu`）和 `ia peer-annual import-bank --evidence-root <ROOT> --report-period 2025-12-31`。银行名单保存 `metric_pack=bank_annual_v1`、原始分类记录及其报告期采集语义；明确是有目的抽样、没有官方完整行业名单声明。白酒 payload 保持原 schema 形状与默认 hash 逻辑。

年度导入每家公司要求供应商响应中恰有一条目标年末记录，且 `REPORT_TYPE=年报`、发行代码匹配。四个指标保持原始 percent，在规范值中除 100 一次并标为 fraction；导入保留原字段、精确字符串原值、原始 percent 单位、原文件 SHA-256 和 `NOTICE_DATE`/`UPDATE_DATE`。导入不接受供应商的 verification 标志；四个指标固定 candidate。`equity_parent` 始终以明确原因 missing，未从 `NZBJE` 或 `TOTAL_EQUITY_PK` 推导。若来源日期缺失则保留空日期；未来通知/修订日期或无时区 `acquired_at` 会拒绝。

本地 run-02 导入可登记两份通过既有原件身份/报告期检查的文件：工行 601398 与江苏银行 600919；它们只进入披露原件库作为 candidate original，四个比率字段仍为 candidate，因为未建立银行字段级页面定位/语义检查。招行 600036 的 PDF 封面抽取不含可确认的年度报告期，拒绝登记；导入结果和字段证明记录原件未登记原因。其余七家公司本地没有原件。原件 title 只额外接受“YYYY年度报告”明确变体，PDF 实际首页仍必须确认目标年度与年度报告；江苏银行英文名称通过显式 `江苏银行`→`BANK OF JIANGSU` 封面别名对应。

银行字段级 proof 校验器/显式 proof 输入格式本轮尚未实现；即使保留原件，字段不会因此升级为 verified。可执行端到端命令和覆盖现状见上文实现参数及 `tests/fixtures/bank_run02/SOURCE.md`。
