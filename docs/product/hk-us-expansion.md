# 港股与美股财务资料扩展（首期）

适用范围：`zcharlie` 在既有 A 股流程之外，为一份**人工核对的受控名单**提供
港股与美股年度财报资料（身份、来源绑定、不可变版本、确定性指标、只读财务上下文）；
A 股取数、评分、证据与任务流程的语义不变，港美走独立路径（`materials/`）。

- 设计与任务拆分：[`plans/hk-us-expansion/README.md`](plans/hk-us-expansion/README.md)
- 验收矩阵（逐条用例与实测命令）：[`plans/hk-us-expansion/12-acceptance.md`](plans/hk-us-expansion/12-acceptance.md)
- 完成记录与偏差登记：[`plans/hk-us-expansion/COMPLETION.md`](plans/hk-us-expansion/COMPLETION.md)
- 下一阶段实施计划：[`plans/hk-us-expansion/15-next-phase.md`](plans/hk-us-expansion/15-next-phase.md)（优先级、依赖、分阶段交付与验收标准）
- 当前能力与来源验收矩阵：[`plans/hk-us-expansion/17-capability-source-matrix.md`](plans/hk-us-expansion/17-capability-source-matrix.md)（身份绑定、数据源、许可未知项、产品接线与启用条件）
- HK2-01 与 HK2-02 实施及验收：[`执行拆分`](plans/hk-us-expansion/16-hk2-01-02-execution.md)、[`验收记录`](plans/hk-us-expansion/18-hk2-01-02-acceptance.md)
- 原始调研证据（只读，未纳入 git）：`research/market-data-validation/`

**首期所有验收都在禁网条件下成立。** 在线刷新默认关闭，见 §3。

## 1. 交付边界

| 已交付 | 未交付 |
|---|---|
| 八家受控证券（中电控股 `00002`、汇丰控股 `00005`、腾讯控股 `00700`、AAPL、COST、JPM、XOM、TSM）的身份、来源绑定与不可变资料 | 全市场筛选、港美统一评分、行业排行、历史估值分位、回测、实时行情、多币种组合账本 |
| 港美年度财报资料的**解析、契约、版本与采集任务**（港股原币端点 + SEC CompanyFacts/Submissions 的适配器与任务；离线导入路径已验收，在线抓取默认关闭，见 §3） | 严格历史时点查询、任意历史日期重建、全量不可依赖公告发现 |
| 确定性财务指标（收入同比、三年复合增长率、平均权益 ROE、资产负债率、经营现金流/利润、简化自由现金流）与适用性规则 | TTM、逆向 DCF、FCFE、市净率与股息率默认开启 |
| 财务上下文只读接口、显式刷新接口、研究档案与发布校验接入 | 把 A 股评分与阈值套到港美证券 |
| 条件启用能力的**决策与缺口表达**：六位能力及缺口可由财务上下文返回；年度 PE 与分红模块有纯计算函数，但尚未接入当前财务上下文、API 或股票页 | 行情、FX、PB、年度 PE 和分红事件的产品读写路径；供应商使用范围核对、最新年度与多日在线来源验收（见 §6 与验收矩阵） |

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
| `annual_pe` | 关 | 恒为 false；存在显式注入价格、年度 EPS 与可选 FX 的纯函数，但没有接入当前财务上下文/API/股票页 |
| `dividends` | 关 | 恒为 false；存在事件契约和纯函数，但没有接入当前财务上下文、生产存储/API/股票页 |
| `pb` | 关 | 恒为 false；港美财务产品路径没有 PB 计算或展示 |
| `online_refresh` | 关 | `ONLINE_REFRESH_ENABLED = False`；刷新可登记任务，handler 在上游调用前停止 |

`price` / `annual_pe` / `dividends` / `pb` 为假是当前产品决定；纯计算函数或样例
不表示产品能力已接通。`online_refresh` 为假时，采集任务在上游调用前停止并记录
`online_refresh_disabled`，刷新接口与页面不会把"已入队"显示成"已取到数据"。

指标：六个确定性指标见 §1。**毛利/净利率不计算**——收入与成本字段本轮未逐家补验，
以 `gaps` 里的固定条目明示，不猜补。腾讯的资本开支口径、汇丰的净买卖口径
按名称拒绝，不生成"统一自由现金流"。

## 3. 来源与许可状态

八家身份的实际 provider symbol、逐来源实现状态与验收门槛见
[`17-capability-source-matrix.md`](plans/hk-us-expansion/17-capability-source-matrix.md)。
下表概括财务材料使用的来源；绑定不等于来源许可或在线验收通过。

| 来源标识 | 角色 | 许可状态 |
|---|---|---|
| `eastmoney.hk.native.v1` | 港股原币三表（收入/资产负债/现金流）**候选来源** | **未确认**——展示、缓存与商业授权均未核实 |
| `sec.companyfacts.v1` | 仅名单中绑定的六个 CIK 有 CompanyFacts 财务解析和离线导入 | 公开申报可读；具体来源使用范围、完整原件归档及在线验收仍须核对 |
| `sec.submissions.v1` | SEC 适配器按 CIK 解析申报索引及历史分片；不是 pilot list 中独立绑定的 source | 实际取得的响应均归档；未取得分片仍列缺口，未知抓取时间保持未知 |
| `yahoo.chart.v1` | 名单仅绑定 CLP、HSBC、Tencent、AAPL 行情代码；HK/US materials 无 Yahoo 行情适配器 | **许可未确认**；AAPL 仅一个已记录行情来源，绑定不构成交叉验证 |
| HKMA 日汇率 | 旧调研文档记录曾保存汇率证据；身份名单未绑定 FX 来源 | 币对、日期覆盖、许可及产品存储/解析/展示均未验收 |

分红模块无 pilot-list 来源绑定或在线 provider；其中纯函数和数据契约不等于
已经导入并展示事件。PB 在当前港美路径未实现。详见验收矩阵。

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
先使用同一套 `DATABASE_URL`、`DATA_DIR` 和 `API_KEY` 配置迁移数据库，再登记身份并导入：

```bash
ia migrate up
ia materials init
ia materials import \
  --source-dir research/market-data-validation/evidence/remaining-final-03/raw \
  --company-key hsbc-holdings --company-key jpmorgan-chase
```

`init` 幂等登记八家证券、三个市场及来源绑定，不抓取数据；`import` 自动初始化，
通过真实适配器解析选中公司的本地材料。省略 `--company-key` 则导入整份受控名单。
可用 `--pilot-list` 指定名单文件；改变已登记的绑定会被拒绝，不自动改写旧映射。
页面仍只覆盖应用默认的人工核对名单，传入自定义名单不会自动扩大页面覆盖范围。

文件定位：按公司 ticker / 来源 symbol 子目录查找。港股请求 `income`、`balance`、
`cashflow` 也识别调研文件名，优先 `*-four-years.json`；现金流优先
`cashflow-original-four-years.json`。同一别名的多个候选无法唯一定位时拒绝。
SEC 先解析 `companyfacts.json` / `submissions.json`，再补读索引列出的历史分片；
例如 `JPM/shards/CIK0000019617-submissions-007.json`。缺文件会明确报告。
现有 `hsbc-companyfacts.json` / `hsbc-submissions.json` 须复制到
`1089113/companyfacts.json` / `1089113/submissions.json`（连同边车）；保留原件。
现有调研目录并未保留 SEC 索引列出的全部分片，因此直接重放可能退出 2。

导入结果为 JSON：新增/复用版本数、身份 id、`acquisition_ids`、`problems`、`skipped` 与
`informational`。退出码 **0** 表示完整导入；**2** 表示可用材料已提交、仍有缺口；
**1** 表示初始化、数据库或参数错误。未映射条目计数只进 `informational`。
汇丰也绑定 SEC 作为交叉核对；只有港股文件的目录会导入原币三表，同时报告 SEC 缺口。

数据规则：

- `captured_at` 是本次导入时刻；原始抓取时间另存 `source_fetched_at`。
  边车缺失、时间缺失或无时区时保留未知并报告，不回填历史时间。
- 边车明确声明非 2xx HTTP 状态（包括非法状态值），或声明的 SHA-256 / 字节数与正文不符时，
  正文仍归档，但标记为不可解析，不进入财务事实；重放也不能重新接受该正文。
- 港股同一来源/年度的三表合为一个版本，旧的逐表版本仍可读；重导入同内容复用版本。
- 迁移 **047** 添加 SQLite UPDATE / DELETE / REPLACE 保护；ORM 与直接 SQL 都不能改删已保存资料。
  绑定区间为 `[valid_from, valid_to)`，结束日不属于旧绑定。
- 新导入和采集通过独立、不可变的采集记录关联多响应原件包，原件位于
  `DATA_DIR/research-material/archive/`。清单保存身份、来源、适配器版本、预期请求、
  逐响应原抓取时间、HTTP 状态、脱敏定位、哈希、字节数和解析资格；数据库不保存密钥 URL。
- 迁移 **049** 新增采集记录及 SQL/ORM 不可变保护。相同证据重复导入复用采集记录；
  新抓取时间可产生新采集记录，不改变相同财务内容的资料版本。
- 旧单文件 `raw_ref` 继续可读；没有采集记录的旧资料显示“原件归档不完整”。
  未归档的旧材料仍须保留原调研目录和边车，升级不会为旧行补造正文。

使用导入结果中的采集记录 ID，可以只依赖数据库与原件包重放：

```bash
ia materials replay --acquisition-id 1
```

`replay` 不读取原 `--source-dir`，不访问网络；先校验数据库记录中的可信清单哈希、
正文完整性、身份绑定与适配器版本，再通过同一适配器重建资料。结果 **0** 为完整通过，
**2** 为原记录仍有资料或响应缺口，**1** 为校验失败、版本不匹配或数据库未就绪。
未知原抓取时间继续保留未知。复制数据库和 `research-material/` 到新 `DATA_DIR` 后可重放；
备份与恢复见[操作说明](backup-and-recovery.md)。这不提供严格历史时点查询能力。

单个文件或来源失败不阻断其它来源；契约不合法的草稿属于代码错误，抛出并回滚本次数据库导入。
这些命令不访问上游；来源授权、付费权限与最新财报补验仍按 §3 / §6 单独验收。

### 显式准备指标发布

```bash
ia materials prepare-publication --listing-id 1 --as-of 2026-10-15T23:59:59+08:00
# 可重复指定 --dataset-id，显式选择该公司当前资料版本，消除多来源歧义。
```

该操作复用财务上下文的六项指标规则，将每项成功结果保存为计算记录，并返回精确事实证据引用、
报告期、原币、计算状态与缺口。证据不可见、缺失、不适用、来源歧义或冲突的结果没有计算引用。
相同输入重复执行复用引用；修订产生新引用，历史计算仍绑定原资料版本。调用前需按同一
`DATABASE_URL` 完成迁移并导入资料；该操作不会获取上游资料，也不会发布文章。
退出码 **0** 表示无报告缺口，**2** 表示逐项返回的缺口（可同时包含已保存计算），**1** 表示操作失败。
`--as-of` 必须包含时区；财务 GET 保持只读，不会隐式创建证据或计算。

### 离线验收年度证据

```bash
ia materials verify-annual \
  --manifest config/research_material/annual_validation_2026-10-15.json \
  --source-root data/hk2-latest-evidence/2026-10-15/raw
```

验收器读取本地原件，重新核对字节数、哈希、日期、官方索引、字段证据、实际财年及 SEC 分片。
它不连接数据库，也不访问网络；PDF 原件核对需要安装 `.[original-parsing]`。
退出码 **0** 仅代表八家完整验收通过，**2** 表示有缺口。
`--company-key` 可以选择子集，但子集成功不会把八家公司证据包标为完成。
复核清单与候选接口响应分别保存；本操作不把人工复核记录伪装成供应商响应，也不自动导入生产资料。

## 5. 只读接口与刷新接口

| 端点 | 用途 |
|---|---|
| `GET /research/listings/{listing_id}/financial-context` | 该证券已保存的财务资料、口径、状态与能力位。**严格只读**：从本地行与受控名单作答，不触碰任何上游（其读模型甚至无法 import 取数器）；未知 listing 404，名单外证券返回显式的"未覆盖"视图 |
| `POST /research/listings/{listing_id}/refresh` | 校验许可范围后入队**一次**（幂等去重）资料采集运行。未知 listing 404，被拒请求 422；响应如实给出 `online_refresh_enabled`，入队的运行不会被说成已取到的数据 |

两个端点都需要 API key 或同源会话；目录级路径见 [`README.md`](../../README.md) 的 `## HTTP API`。

## 6. 遗留清单（不在首期范围，留给联网验收）

| 项 | 原因 | 恢复条件 |
|---|---|---|
| 八家最新年度口径与可计算性收口 | 已有 [2026-10-15 补验包](plans/hk-us-expansion/20-latest-annual-evidence.md)，包含八家最新财年、六类字段与连续年度复核；整体结论按逐家公司报告判定，复核记录不自动导入生产资料 | 三项 P0 已收口，八家证据包断网核验通过，见 [25](plans/hk-us-expansion/25-hk2-04-closeout-acceptance.md)；保持精确来源与历史版本，完成规范资料导入后再验收页面 |
| 在线刷新开关打开 | 东财/Yahoo/新浪/HKMA 的使用许可未确认 | 确认展示/缓存/商业授权后置 `online_refresh=true` |
| 供应商付费权限（FMP/EODHD/HKEX） | 无真实账户端点证据 | 账户与端点逐项验收后作为可替换来源 |
| 港股公布/修订历史、跨日稳定性 | 无完整已验证时间链与多日保障 | 多日 fresh fetch 与官方披露关联验收 |
| 不引入 EdgarTools | 离线环境无该依赖，且设计把版本选择与资格判断归属应用 | 若引入须固定版本、重跑解析与版本案例 |
| 年度 PE、分红事件产品接线 | `materials/valuation.py` 与 `materials/dividends.py` 有纯计算逻辑；当前 HK/US 财务上下文、API、生产资料读写及股票页没有接线，能力位保持 false | 按矩阵补齐输入来源与数据存储，接通计算/读模型/API/页面；逐项通过覆盖、日期、币种、来源与回归验收后按证券开启 |
| 跨币种发布换算 | 汇率时间点与使用许可未验收 | 汇率能力验收后另开任务（发布只支持原币，跨币种拒绝） |

## 7. 验收状态

首期强制验收用例的逐条映射、实测命令与结果、以及**未覆盖清单**见
[`plans/hk-us-expansion/12-acceptance.md`](plans/hk-us-expansion/12-acceptance.md)。
首期历史记录保留当时的时间与结果；HK2-01、HK2-02 的新增行为和验证结果以
[`18-hk2-01-02-acceptance.md`](plans/hk-us-expansion/18-hk2-01-02-acceptance.md) 为准。
HK2-03 指标发布准备及 HK2-04 年度补验的本轮结果见
[`21-hk2-03-04-acceptance.md`](plans/hk-us-expansion/21-hk2-03-04-acceptance.md)。
HK2-04 三项 P0 收口及 HK2-07 开工评估见
[`25-hk2-04-closeout-acceptance.md`](plans/hk-us-expansion/25-hk2-04-closeout-acceptance.md)。
HK2-07 离线实际导入、状态时间契约及八家桌面/移动浏览器验收见
[`27-hk2-07-offline-acceptance.md`](plans/hk-us-expansion/27-hk2-07-offline-acceptance.md)。在线刷新继续关闭，生产环境尚未迁移或导入。
最新 HK2-08 M1 本地发布、原件完整性检查及配套备份恢复验收见
[`29-hk2-08-m1-acceptance.md`](plans/hk-us-expansion/29-hk2-08-m1-acceptance.md)。生产环境未操作，在线刷新仍关闭。
来源启用和后续产品能力仍须分别验收。
