# HK2-06 分来源三日真实观测计划

日期：2026-10-17（Asia/Shanghai）。本文件是**条件式计划**，不是排期、自动任务或观测记录。观测尚未开始。在线刷新和来源运行开关仍默认关闭；许可和技术审批仍由 `source_policy.yaml` 独立管理。来源政策事实、代码差距和人工确认事项见 [34 来源使用范围审查](34-hk2-06-source-review.md)。

## 计划边界与开始条件

- `config/research_material/observation_plan.yaml` 是 `plan_only` 文档数据，`default_enabled: false`，没有 observations 或 reviews。运行时代码不得把它当作许可、来源策略或启用配置。日常 global/source/ops 刷新继续关闭，技术接受继续 `not_accepted`。本文和离线汇总工具都不改 `source_policy.yaml`、Settings、数据库或在线刷新设置。
- D0 仅指来源责任人完成先决条件、项目审批人核准并人工选定一个开始日期之后的准备检查；**D+0** 才是首个候选真实观测日。随后分别在另两个不同的 Asia/Shanghai 日期执行 D+1、D+2。日期由人工在条件满足后选定，本文件不预填未来日期、不创建 automation、不发起请求。
- 每个来源独立累计自己的三日 streak。一个来源失败只阻塞该来源。simulation 与 dry-run 永不构成真实观测日期；dry-run 仅记录 `planned_company_keys`，实际 `company_keys` 应为空。
- 当日停止、请求或解析失败、受控范围缺失、HTTP 非成功、响应结构漂移、未解释版本变化、原件/引用 hash 不匹配或演练不通过，均令该来源 streak 清零。问题修复、重新满足前置条件且审批仍有效后，由人工另定新 D+0 窗口。旧 archive 不得改写时间戳或重新贴成新日期；同日重试不增加日期数。
- 真实观测只允许在受控许可与公司范围已人工批准、请求身份和来源要求已核准后，由人明确执行未来一次性的 `--allow-network` 隔离 probe；这不要求先开启日常 global/source/ops，也不要求先通过三日技术验收。probe 的显式 STOP 与许可撤回停止能力须另有真实证据。一次性 probe 没有 checkpoint/restart 功能，不声称支持断点续跑。
- HK2-05 隔离 fake 的关停/inflight/restart/replay 证据只能证明日常 worker 行为，不能代替特批 probe 的真实 STOP/许可撤回停止证据。独立 `restart_resume` 证据仍待执行，须由另一个隔离演练证明；它不是 probe 自带能力。
- 自动汇总最多输出 `technical_review_candidate`，不写 `approved`/`accepted`，不启用全局或来源开关，不宣称 HK2-06 完成。来源授权与真实性仍须责任人人工核实。

## SEC CompanyFacts/Submissions：`sec.companyfacts.v1`

本来源包含 HSBC 发行人级 SEC 数据交叉核对以及 Apple、Costco、JPMorgan、Exxon Mobil、TSMC 五家发行人，共六个受控 `company_key`：`hsbc-holdings` (CIK 1089113)、`apple` (320193)、`costco` (909832)、`jpmorgan-chase` (19617)、`exxon-mobil` (34088)、`tsmc` (1046179)。CIK 是发行人标识，不推导股票类别或交易所身份。来源许可仍为 unknown，技术接受仍为 not_accepted；SEC 公共 API 文档并非本项目许可批准。

**D0 / D+0 进入条件。** 由项目责任人书面确认项目主体、目标应用对资料的使用（本地保存/缓存、私有展示及是否涉及商业用途）是否处于 SEC 官方再使用说明范围，并按受控流程完成人工许可及公司范围批准；设置真实、可监控的项目/组织 User-Agent 和有效联系邮箱；统计所有主机及进程对 SEC 的总请求，证明聚合限流符合 SEC Fair Access 上限及请求身份要求。单进程 0.5 秒间隔不证明跨机器合规。日常总开关、SEC 来源开关、ops 刷新仍关闭，技术接受仍为 `not_accepted`；满足上述特批前置后，人员可显式启动未来一次性隔离 probe，无需先通过三日验收。条件未齐时只保持准备状态，不请求。

**每日范围与矩阵。** 每日完整覆盖上述六个受控公司。每一 CIK 请求一份 CompanyFacts JSON 和一份 Submissions JSON；再依当次 Submissions `filings.files` 列出的历史 JSON 文件逐项取回所有历史 shard。不要预先固定历史 shard 数；当日核对清单必须逐公司列明初始文档、每个 shard、响应结果和重试。任何公司只做两份基础文档但跳过已列 shard，均为范围不完整。

| 条件日期 | 操作和进入下一日的条件 |
| --- | --- |
| D0 | 核对身份、UA 联系邮箱、SEC 使用范围与公司范围人工许可、所有流量聚合限速及 archive 空间；日常运行开关保持关闭，不自动启动，也不代表真实观测。 |
| D+0 | 首个候选观测日；完整请求六家基础文档及各自当日全部历史 shard。仅当所有请求/响应、当前适配器重解析和本地原件检查全部通过，才可由人工决定在新日期安排 D+1。 |
| D+1 | 重新完整观测六家，不复用前一日原件。复核当日 Submissions shard 列表、请求身份、聚合限速与每份响应 fetched_at；前两日任一不合格则本次不能续接旧窗口。 |
| D+2 | 第三个不同本地日期再次完整观测六家。没有三份最新连续合格真实日和演练证据时，仍 pending；不通过则来源 streak 清零并重选新窗口。 |

**每日验证与留存。** 保存实际 User-Agent、逐请求开始/结束、HTTP 状态、重试与 limiter 信息、公司和 CIK 绑定、Submissions 列示的 shard 名单、请求总数，以及 report SHA。逐份 `archive_capture` 原件经 `verify_capture` 核验 manifest/body SHA、长度、archive/source/company identity、adapter version、响应集合和 HTTP/parse 标记；每个响应 `fetched_at` 必须与真实观测当天的 Asia/Shanghai 日期一致。再用当前 `SecFactsAdapter` 对六家公司原件完整重解析，需有事实/filing drafts 且不能报告结构或解析问题。将所选年报字段、期间、accession/版本变化和 SEC 原件作逐项人工对照；差异写入本地评审文件，引用时记录相对路径及 SHA。SEC 官方说明政府创建内容和 EDGAR 公开申报内容免费访问和再使用，但特定产品缓存、展示或商业使用的范围仍按 34 所列事项人工确认。

每日还必须留存并人工审阅以下独立演练证据；每份引用均为 evidence-root 内可解析的本地文件及匹配 SHA：

- `shutdown`：使用 HK2-05 隔离 fake 验证日常 worker 关闭总开关/SEC 来源开关后不再开始请求；这不证明特批 probe 受日常开关控制；
- `inflight_stop`：使用 HK2-05 隔离 fake 验证日常 worker 等待限速或多阶段请求时关闭后停止后续发起。特批 probe 的 STOP 文件和许可撤回停止行为须另留真实证据；已收到响应保留为不完整证据；
- `restart_resume`：另行执行隔离演练并留证，验证其检查点/恢复语义。一次性 probe 没有断点续跑功能，不得以 probe 声称通过此项；该证据完成前 grader 保持 pending；
- `old_versions_readable`：旧 adapter/source version 原件仍可验证读取且版本差异可见；
- `offline_replay`：断网重放归档输入，结果可重复并且不发请求。

SEC 403/Access Denied 立即停止该来源后续请求，保存拒绝响应并按 SEC 官方指引联系；不得换 IP、轮换/伪装身份、绕过拒绝或盲目继续重试。其它传输失败按有界重试策略；任何失败或后续请求未完成都令当日不合格。三日合格条件是最新连续三次真实完整范围观测各自落在三个不同本地日期，响应均当日获取，adapter 版本与当前一致，当前解析无结构错误，年报原件对照已完成，且所需独立演练/STOP 证据齐全。之后仍仅为技术复核候选，等待来源真实性人工复核。

## 东方财富港股原币：`eastmoney.hk.native.v1`

完整范围为中电控股 `clp-holdings` / `00002.HK`、汇丰控股 `hsbc-holdings` / `00005.HK`、腾讯控股 `tencent-holdings` / `00700.HK`。官方审查尚未证明证券数据 API 路径 `datacenter.eastmoney.com/securities/api/data/v1/get` 获端点特定许可，Choice 协议对爬虫、复制和用途的限制范围也未由官方材料明确到该路径。因此真实观测当前**blocked**；公开连通性、旧缓存或内部 1 秒间隔不能替代授权。

**D0 / D+0 进入条件。** 首先由东方财富正式渠道书面确认该端点及三家公司年度三表的自动化请求范围、供应产品/协议关系、速率/并发、User-Agent/联系方法、429/Retry-After 和 403 处理、原件留存/缓存、应用内展示、对外再分发及商业使用。书面证据须本地留存并经人工核对。随后按受控 source policy 独立记录许可与公司范围。只有特定端点授权、公司范围/许可批准和请求身份/来源要求核准后，才可由人显式执行一次性隔离 probe；日常总开关、来源开关、ops 仍关闭，技术接受仍为 `not_accepted`，无需先通过三日验收。任何端点授权缺失时保持 blocked，不发真实请求。

**每日范围与矩阵。** 每个公司每日三张表各一请求，使用受控原始代码和年度窗口：`RPT_HKF10_FN_INCOME`（收入）、`RPT_HKF10_FN_BALANCE`（资产负债）及 `RPT_CUSTOM_HKSK_APPFN_CASHFLOW`（现金流）。每个合格日期总计九个公司/表响应。HK2-04/2026-10-15 年度验证材料所核实的参考窗口为 2022–2025；未来窗口须先由人工年报对照确认各公司最新完整已披露年度，不能由日历年份推定或沿用旧抓取值。不得使用 `_PC`/`MAININDICATOR`/展示币种结果作为金额来源。SEC 相同日期规则适用：D0 人工准备，D+0、D+1、D+2 由责任人在前序合格后选三个不同 Asia/Shanghai 日期；不预先排期。

| 条件日期 | 操作和进入下一日的条件 |
| --- | --- |
| D0 | 取得并核实端点特定书面许可、用户用途和限速/拒绝规则；逐公司策略审批通过并准备停止/恢复/重放环境；不发请求。当前此项尚未满足。 |
| D+0 | 只有 D0 全部人工前置条件满足后，才由责任人启动三公司九张表的第一轮真实观测。任何缺表、授权/速率不明或拒绝响应均阻止后续来源请求。 |
| D+1 | 前日完整合格且许可仍有效后，在不同日期重新获取完整九响应；重核供应方规则、速率和版本。 |
| D+2 | 前两日均合格后，于第三个不同日期重复完整九响应。否则 streak 清零，修复授权/技术问题后重新人工确定 D+0。 |

**每日验证与留存。** 验证九个请求逐一映射到公司/表，HTTP 状态成功、原件及 body 哈希、完整 `success`/`result.data`/分页、报告期匹配、表码不重复、币种等于发行人报表币种、adapter version 与当前一致，并以当前 `HkNativeAdapter` 重解析。`unmapped_items` 属已知显式计数，原样记录；结构/分页/币种/期间问题不能以该计数掩盖。逐公司将年度金额、原币、期间和字段定义与发行人年度报告逐项对照；HSBC 的信用损失后经营收入、固定资产净购售及权益口径差异须保留并人工解释，不能把定义差异合并。response fetched_at 必须是报告观测日，不得旧原件新贴日期。

每日报告和人工证据另需覆盖独立演练：使用 HK2-05 隔离 fake 证明日常 worker 的开关关闭、inflight 停止、旧版本可读及无网重放；独立 `restart_resume` 仍待另行演练和证据，probe 本身没有 checkpoint/restart 能力。特批 probe 的 STOP 文件与许可撤回停止行为须留单独真实证据，不能由普通 ops 开关演练代替。证据均为 evidence-root 内本地文件与 SHA。书面规则要求等待的 429 应遵守；403/拒绝访问按授权方书面规则停止。任何失败、遗漏或未解释版本变化都清零本来源 streak。只有三份最新连续真实观测、完整九响应和全部人工证据均合格，离线汇总才可给出 `technical_review_candidate`；来源真实性复核仍是后续必需步骤。

## 离线汇总工具和输入契约

`tools/hk206_source_acceptance.py` 只接受明确指定的 JSON `--ledger` 和 `--evidence-root`。账本为 schema 1，包含一个来源、当前 adapter version、报告相对路径列表及按每份报告 SHA 绑定的人工 review 声明；报告内的 archive refs 相对各自报告 work-dir，review 引用相对共同 evidence-root。声明必须包含 reviewer、晚于报告且非未来的带时区 `reviewed_at`。review 的年度对照状态只能为 `matched`、`revision_reviewed` 或 `unreviewed`，并显式给出 `expected_periods`；它必须与 capture/report 的 periods 完全一致。SEC 的空 period 列表仍需由所引年报对照证据说明适用窗口，HK 窗口必须人工参照已核实年报而不能由日历推定。另须有 `source_scope_review: {status: approved, citation: {path, sha256}}` 记录来源使用范围人工复核；SEC 应覆盖真实联系 UA 和聚合速率，东财应覆盖端点特定书面授权。来源范围、年度对照及六类独立演练引用均需 evidence-root 内本地文件路径及 SHA，其中 `restart_resume` 是独立演练而非一次性 probe 的能力，`probe_stop_withdrawal` 是特批 probe 的真实 STOP/撤回证据。报告必须列出完整真实公司范围和逐公司已批准/绑定的许可决定（仅作报告证据，不代替 source policy 审批），manifest 通过 `verify_capture`，原件响应、日期、HTTP、请求/响应映射及当前 adapter 解析需完整；常规在线刷新仍须关闭。simulation/dry_run 不计日期，最新失败中断 streak。同一日多次只贡献一个日期，重复报告/manifest/hash 或重贴旧原件均不能补足三日。

初始 ledger 仅列出来源和当前版本，观测与 review 为空；真实材料产生后，人工再将 evidence-root 内报告相对路径和 SHA 绑定的 review 加入：

```json
{
  "schema_version": 1,
  "source": "sec.companyfacts.v1",
  "adapter_version": "1",
  "observation_reports": [],
  "reviews": []
}
```

工具只读取明示输入和静态受控身份清单；不读取 `.env`、Settings、数据库或 `source_policy.yaml`，不访问网络，不修改报告、ledger、原件、审批文件或来源策略。输出 `pending` 表示证据尚不完整，`technical_review_candidate` 只标示可以进入人工来源许可和真实性复核。输出路径可选且须为新文件，已有文件或 evidence-root 内路径会被拒绝。工具不具有批准、启用来源或写数据库的能力。
