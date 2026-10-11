# 港股与美股财务资料扩展（首期）

适用范围：`zcharlie` 在既有 A 股流程之外，为一份**人工核对的受控名单**提供
港股与美股年度财报资料（身份、来源绑定、不可变版本、确定性指标、只读财务上下文）；
A 股取数、评分、证据与任务流程的语义不变，港美走独立路径（`materials/`）。

- 设计与任务拆分：[`plans/hk-us-expansion/README.md`](plans/hk-us-expansion/README.md)
- 验收矩阵（逐条用例与实测命令）：[`plans/hk-us-expansion/12-acceptance.md`](plans/hk-us-expansion/12-acceptance.md)
- 完成记录与偏差登记：[`plans/hk-us-expansion/COMPLETION.md`](plans/hk-us-expansion/COMPLETION.md)
- 原始调研证据（只读，未纳入 git）：`research/market-data-validation/`

**首期所有验收都在禁网条件下成立。** 在线刷新默认关闭，见 §3。

## 1. 交付边界

| 已交付 | 未交付 |
|---|---|
| 八家受控证券（中电控股 `00002`、汇丰控股 `00005`、腾讯控股 `00700`、AAPL、COST、JPM、XOM、TSM）的身份、来源绑定与不可变资料 | 全市场筛选、港美统一评分、行业排行、历史估值分位、回测、实时行情、多币种组合账本 |
| 港美年度财报资料的**解析、契约、版本与采集任务**（港股原币端点 + SEC CompanyFacts/Submissions 的适配器与任务；离线导入路径已验收，在线抓取默认关闭，见 §3） | 严格历史时点查询、任意历史日期重建、全量不可依赖公告发现 |
| 确定性财务指标（收入同比、三年复合增长率、平均权益 ROE、资产负债率、经营现金流/利润、简化自由现金流）与适用性规则 | TTM、逆向 DCF、FCFE、市净率与股息率默认开启 |
| 财务上下文只读接口、显式刷新接口、研究档案与发布校验接入 | 把 A 股评分与阈值套到港美证券 |
| 条件启用能力的**决策与缺口表达**：年度 PE、分红事件的能力位为假，缺口同时出现在 `statuses` 中，页面不会把"未交付"显示成"无异常" | 年度 PE 与分红事件的**数据与展示**（尾部任务 13/14，未交付）；供应商付费权限、最新年报联网补验（见 §6） |

受控名单是"做"与"不做"的边界：名单外的证券即使能取到数据也不采信，
读模型对它们返回显式的"未覆盖"状态而不是空数据。

## 2. 能力位与默认关闭项

`GET /research/listings/{listing_id}/financial-context` 的响应带 `capabilities` 六位，
前端据此决定展示什么；**未交付的能力位为假并同时出现在 `statuses` 里**，
因此"没做"与"无异常"在页面上可区分。

| 能力位 | 默认 | 语义 |
|---|---|---|
| `financials` | **有资料即开** | 该证券在受控名单内**且**至少存在一份资料数据集时为真；否则为假并提示先采集 |
| `price` | 关 | 价格走势：行情来源未验收（`CONDITIONAL_CAPABILITIES`） |
| `annual_pe` | 关 | 年度市盈率：需价格与一致 EPS，首期不交付 |
| `dividends` | 关 | 分红记录：来源未验收，首期不交付 |
| `pb` | 关 | 市净率：首期不交付 |
| `online_refresh` | 关 | 是否允许采集任务访问上游；对应 `ONLINE_REFRESH_ENABLED = False`（`materials/handlers.py`） |

`price` / `annual_pe` / `dividends` / `pb` 为假是**决定**（设计"尚未验证能力"），
不是检查失败；`online_refresh` 为假时，采集任务在上游调用前停止并记录
`online_refresh_disabled`，刷新接口的响应也如实说明，不会把"已入队"显示成"已取到数据"。

指标：六个确定性指标见 §1。**毛利/净利率不计算**——收入与成本字段本轮未逐家补验，
以 `gaps` 里的固定条目明示，不猜补。腾讯的资本开支口径、汇丰的净买卖口径
按名称拒绝，不生成"统一自由现金流"。

## 3. 来源与许可状态

| 来源标识 | 角色 | 许可状态 |
|---|---|---|
| `eastmoney.hk.native.v1` | 港股原币三表（收入/资产负债/现金流）**候选来源** | **未确认**——展示、缓存与商业授权均未核实 |
| `sec.companyfacts.v1` | 美股**主来源**（CompanyFacts 年度事实） | 公开申报数据，来源与抓取时间逐条留痕 |
| `sec.submissions.v1` | 申报索引数据集（`FILING_INDEX`）：年报申报清单与不可依赖公告 | 公开申报数据，同样逐条留痕 |
| `yahoo.chart.v1` | 行情第二来源（仅身份绑定，未启用） | **未确认**，首期不采集 |
| HKMA 日汇率 | 汇率（仅证据留存，未启用） | 首期不使用 |

来源特有的字段全部进版本化 `payload`，不进跨来源契约；每个来源按自己的
证券代码单独保存（港股五位含前导零、Yahoo 四位、CIK 十进制），**不互相换算**。
刷新接口把适配器版本绑进请求，**当前许可范围只含两个适配器**
（`eastmoney.hk.native.v1`、`sec.companyfacts.v1`，见 `materials/refresh.py` 的 `ADAPTER_VERSIONS`）：
适配器版本移动后，旧版本的运行会被拒绝而不是按新版本重采。

**在线刷新默认关闭**（`handlers.py` 的 `ONLINE_REFRESH_ENABLED = False`）：
东财与 Yahoo 的使用许可未确认，首期一律不访问上游。这是一个评审决定，不是偏好——
因此**没有**加运行时设置项，设置页无法把它翻成开。打开需先确认展示/缓存/商业授权，
并补联网验收（见 §6）。

## 4. 离线导入用法

首期的资料来自已保存的调研证据（每个响应体配一个 `<name>.meta.json` 边车）。
导入走与在线路径**同一套适配器**，适配器由调用方注入，模块本身不 import 具体来源：

```python
from pathlib import Path

from investment_agent.materials.offline_import import import_offline_evidence
from investment_agent.materials.handlers import default_adapters

report = await import_offline_evidence(
    session,                       # AsyncSession
    data_dir=Path("data"),         # 制品目录
    source_dir=Path("research/market-data-validation/evidence/remaining-final-03"),
    identities=identities,         # 受控身份（config/research_material/pilot_list.yaml）
    company_ids=company_ids,       # company_key -> Company.id
    adapters=default_adapters(),   # 或测试内的假适配器
    imported_at=None,              # 省略则用真实当前时间
)
# report.saved / report.problems / report.skipped
```

时间规则（`materials/offline_import.py`）：

- 入库的 `captured_at` 是**本次导入真实发生的时刻**（`imported_at` 或当前时间），
  绝不是边车里的历史 `fetched_at_utc`。
- 原始抓取时间另存 `source_fetched_at`；边车缺失、无 `fetched_at_utc`
  或该值不是带 UTC 偏移的 ISO-8601 时，该字段保持 `None`——**不回填过去**。
- 同一份材料导入两次是一组版本（内容 hash 相同则返回既有版本），重导入是 no-op。

失败隔离：单个文件缺失、边车缺失或某个适配器的 `problems` 都不会中断其它来源，
各自记进 `report.problems`，受影响的 `<company_key>/<source>` 记进 `report.skipped`，
调用方无需解析原因文本即可区分"干净重放"与"部分导入"。
（不满足契约校验的草稿属代码缺陷，直接抛出，不吞掉。）

## 5. 只读接口与刷新接口

| 端点 | 用途 |
|---|---|
| `GET /research/listings/{listing_id}/financial-context` | 该证券已保存的财务资料、口径、状态与能力位。**严格只读**：从本地行与受控名单作答，不触碰任何上游（其读模型甚至无法 import 取数器）；未知 listing 404，名单外证券返回显式的"未覆盖"视图 |
| `POST /research/listings/{listing_id}/refresh` | 校验许可范围后入队**一次**（幂等去重）资料采集运行。未知 listing 404，被拒请求 422；响应如实给出 `online_refresh_enabled`，入队的运行不会被说成已取到的数据 |

两个端点都需要 API key 或同源会话；目录级路径见 [`README.md`](../../README.md) 的 `## HTTP API`。

## 6. 遗留清单（不在首期范围，留给联网验收）

| 项 | 原因 | 恢复条件 |
|---|---|---|
| 八家最新实际年报补验 | 需联网抓取最新财年；本地证据只覆盖 2021–2024 | 联网环境逐家核对最新年报、原件与实际期间 |
| 在线刷新开关打开 | 东财/Yahoo/新浪/HKMA 的使用许可未确认 | 确认展示/缓存/商业授权后置 `online_refresh=true` |
| 供应商付费权限（FMP/EODHD/HKEX） | 无真实账户端点证据 | 账户与端点逐项验收后作为可替换来源 |
| 港股公布/修订历史、跨日稳定性 | 无完整已验证时间链与多日保障 | 多日 fresh fetch 与官方披露关联验收 |
| 不引入 EdgarTools | 离线环境无该依赖，且设计把版本选择与资格判断归属应用 | 若引入须固定版本、重跑解析与版本案例 |
| 年度 PE、分红事件展示 | W6 尾部任务（13/14）；本次实施执行时 13/14 分支尚无提交，未并入 | 启动 13/14 并按各自子计划验收 |
| 跨币种发布换算 | 汇率时间点与使用许可未验收 | 汇率能力验收后另开任务（发布只支持原币，跨币种拒绝） |

## 7. 验收状态

首期强制验收用例的逐条映射、实测命令与结果、以及**未覆盖清单**见
[`plans/hk-us-expansion/12-acceptance.md`](plans/hk-us-expansion/12-acceptance.md)。
该矩阵是本文档"已交付"声明的唯一依据：矩阵里标为未覆盖的能力，本文档也不得写成交付。
