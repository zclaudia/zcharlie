# HK2-06 D0 前置清单与操作手册

日期：2026-10-17（Asia/Shanghai）。本文件是纯文档交付：HK2-06 离线工程收口的 D0 准备检查项、
判据、证据目录契约与操作序列说明。它不排期、不执行任何观测、不修改任何审批或开关。
事实依据为 [34 来源使用范围审查](34-hk2-06-source-review.md)、[35 三日观测计划](35-hk2-06-observation-plan.md)、
[36 准备阶段集成验收](36-hk2-06-preparation-acceptance.md)、[20 最新年度证据](20-latest-annual-evidence.md)、
`config/research_material/source_policy.yaml`、`config/research_material/observation_plan.yaml`
及 `tools/hk206_source_probe.py`、`tools/hk206_source_acceptance.py`、
`src/investment_agent/materials/source_observations.py`、`src/investment_agent/materials/source_probe.py`
的当前实现。本文不引入新的官方承诺；不确定处一律标"待人工确认"。

## 1. 目的与边界

- 本手册是 D0 准备检查与操作入口：逐项列出 SEC 与东财各自进入 D+0 前必须人工补齐的前置条件、
  通过/不通过判据，以及 D+0/D+1/D+2 执行时按序使用的命令与核验步骤。
- 不排期：D+0/D+1/D+2 日期由人工在条件满足后选定（35 号计划），本文不预填、不创建任何未来日期或自动任务。
- 不改变审批：两源在 `source_policy.yaml` 中保持 `licensing.status=unknown`、
  `technical_acceptance.status=not_accepted`。完成本手册清单不等于许可获批。
- 在线刷新与运维开关保持关闭：`ONLINE_REFRESH_ENABLED=False`，SEC/东财来源运维开关默认 false（36）。
  一次性隔离 probe 不要求打开这些开关（35）。
- 本文不为任何检查项替人工下结论：34 号文档对 SEC 再使用说明与 Choice 协议只给出适用性未决的结论，
  逐项确认责任在项目责任人与审批人。

## 2. SEC（`sec.companyfacts.v1`）D0 前置清单

以下每项在补齐证据前该来源保持准备状态，不发真实请求。勾选状态与证据落点建议记入
`<evidence-root>/reviews/` 下的人工文件（见 §5）。

- [ ] **2.1 责任主体确认**（需人工提供）：以哪个项目/组织身份访问 SEC、审批人是谁、批准方式与日期。
  记录为本地人工文件并留引用。
- [ ] **2.2 项目用途逐项确认**（需人工确认，对照 SEC FAQ 再使用说明）：
  34 号文档结论是 SEC FAQ 对政府创建内容及 EDGAR 公开申报内容有免费访问和再使用的一般表述，
  但未详述本地保存/缓存的期限与条件、应用内展示形态、持续镜像/批量转发条件、商业授权范围。
  须由责任人按 34 号"使用范围矩阵"逐项人工确认本项目实际用途（本地保存/缓存、私有应用内展示、
  是否商业用途）是否处于该说明覆盖范围；确认结论与依据引用留存，本文不代为判定。
- [ ] **2.3 真实 User-Agent 与可监控邮箱**（需人工提供）：格式模板
  `Product/Name (contact email)`，即 `产品或组织名/产品名 (联系邮箱)`，实际值形如
  `[待人工提供的产品或组织标识] ([待人工提供的可监控邮箱])`。
  - 邮箱必须真实可收信并由责任人验证监控到位；**禁止**把 example/test/invalid 等示例邮箱、
    占位字符串或默认 `zcharlie-research/0.1 (personal research)`（无联系邮箱，34 号已判差距）
    带入真实流程。
  - probe 的 UA 校验只检查格式：非空、不超过 256 字符、无控制字符、含邮箱、括号前有产品/组织标识、
    且拒绝常见占位邮箱模式。**格式校验通过不能证明邮箱可收信**（36），可监控性须人工证明。
  - 留存拟用 UA 字符串及邮箱可监控证据（接收测试记录等）。
- [ ] **2.4 聚合限流证明**（需证据）。SEC Fair Access 当前规定每用户每秒 10 次请求、无论来自多少台
  机器（34）。可接受的证明方式二选一：
  - (a) **单实例独占证明**：全部 SEC 消费者的进程/主机清单；probe/消费者单实例运行的约束证据
    （独占锁、部署上只允许一个实例的说明）；以及运行日志核对（请求串行、同源相邻请求间隔
    不小于 SEC 最短间隔 0.5 秒）。probe 报告自身固定标注
    `single_process_serial_exclusive_required=true` 与 `multi_process_global_rate_limit_verified=false`，
    后者不会自动翻转，聚合合规由本项人工证据承担。
  - (b) **跨进程共享限流器**：覆盖全部消费者的共享限流机制配置与其生效证据（配置文件/服务端设置
    加实测日志），证明总速率不超过 SEC 上限。
  - **进程内 limiter（SEC 最短间隔 0.5 秒，`MIN_INTERVAL`）不构成跨进程证明**；34 号已明确单实例
    低于上限不能证明部署总流量合规。
  - 须列出**全部 SEC 消费者清单**（凡会请求 data.sec.gov 的主机/进程）：

    | 主机 | 进程/程序 | 用途 | 请求速率 |
    | --- | --- | --- | --- |
    | [待人工提供] | [待人工提供] | [待人工提供] | [待人工提供] |

- [ ] **2.5 archive 空间与留存策略确认**（需人工确认）：单日原件含六发行人的 CompanyFacts、
  Submissions 及当次 Submissions 列出的全部历史分片，分片数随当次列表变化、不可预固定
  （量级参考：20 号文档 2026-10-15 证据包曾逐项校验 79 个分片，覆盖 8 家发行人；该数字仅为
  历史参考，不是上限或承诺）。确认 evidence-root 所在卷空间余量、留存期限与到期处置流程。
  34 号指出 SEC 通用再使用声明未详述抓取后保存期限，留存策略须经 2.2 的人工确认覆盖。
  probe 工作目录必须全新且事后不得改写（见 §8）。
- [ ] **2.6 公司范围人工批准**（需人工审批）：以下六发行人逐一批准并记录批准人、日期与依据。
  CIK 是发行人标识，不推导股票类别或交易所身份（34）。

  | company_key | CIK |
  | --- | --- |
  | hsbc-holdings | 1089113 |
  | apple | 320193 |
  | costco | 909832 |
  | jpmorgan-chase | 19617 |
  | exxon-mobil | 34088 |
  | tsmc | 1046179 |

  **每日范围定义**：每一 CIK 各一份 CompanyFacts JSON、一份 Submissions JSON，再加**当次**
  Submissions `filings.files` 列出的全部历史分片。不预固定分片数；当日核对清单必须逐项列出
  每个初始文档、每个分片、响应结果与重试（35）。任何公司只取两份基础文档而跳过已列分片即为
  范围不完整。批准落点是受控 source policy 审批流程（由审批人按 34 号第 3 条执行），不是本手册。

## 3. 东财（`eastmoney.hk.native.v1`）D0 前置清单

34 号结论：该来源当前 **blocked**——公开连通性、旧缓存或内部 1 秒间隔都不能替代授权。
以下 3.1 未完成前其余各项均无法生效。

- [ ] **3.1 端点特定书面授权**（需人工取得，当前缺口）：须通过东方财富正式渠道取得覆盖
  `https://datacenter.eastmoney.com/securities/api/data/v1/get` 的书面答复/授权，至少逐项覆盖：
  1. 该端点的自动化访问（含逐公司年度三表采集）许可；
  2. 速率与并发上限；
  3. 要求的 User-Agent 与联系方式；
  4. 429/`Retry-After` 的遵守要求；
  5. 403/拒绝访问的处置规则；
  6. 原始响应留存/缓存许可；
  7. 私有应用内展示许可；
  8. 对外再分发许可；
  9. 商业使用许可。
  书面证据本地留存并人工核对（35）。仅端点连通或公开可访问不构成任何一项的满足。
- [ ] **3.2 Choice EULA 与该端点适用关系确认**（需人工确认）：34 号查到 Choice 产品最终用户许可
  协议限制个人非商业使用、未经书面同意不得爬虫抓取/复制转载/衍生服务，但**没有官方材料证明该
  协议适用于本项目调用的证券数据路径**。须在 3.1 的书面答复中一并确认 Choice 协议与该端点的
  适用关系；确认前不能假定已获授权，也不能外推为最终法律解释。
- [ ] **3.3 公司范围**（需人工审批）：`clp-holdings` / `00002.HK`、`hsbc-holdings` / `00005.HK`、
  `tencent-holdings` / `00700.HK`。每个合格日期每公司三张表各一请求、共 3×3=9 个成功归档响应，
  表码固定为 `RPT_HKF10_FN_INCOME`（收入）、`RPT_HKF10_FN_BALANCE`（资产负债）、
  `RPT_CUSTOM_HKSK_APPFN_CASHFLOW`（现金流）。**禁用** `_PC`（展示币种）、`MAININDICATOR`
  （主要指标）及任何展示币种端点作为金额来源（35；35 号对收入表另有"PC/展示币种端点不得用于
  金额"的要求）。
- [ ] **3.4 财年窗口确认**（需人工对照年报）：probe 必须显式给出**四个连续完整年度**（每年
  1 月 1 日至 12 月 31 日）的 `--period`。当前已核实的参考窗口为 **2022–2025**（HK2-04 2026-10-15
  年度验证材料，见 20 号与 35 号）。未来窗口必须由人工重新对照各公司最新已披露年报确认，
  **不得按日历年份推定最新年度**，也不得沿用旧抓取值（35）。
- [ ] **3.5 请求身份与速率来源**（需人工确认）：UA 与联系方法以 3.1 书面授权核准值为准；
  内部 1 秒最短间隔只是本实现的下限，**不是官方允许速率**（34）。
- [ ] **3.6 停止/恢复/重放演练环境准备**（需人工准备）：按 35 号东财 D0 行要求准备停止、恢复、
  重放的隔离演练环境（对应 §5 演练契约与 §7 步骤 4 的证据补齐）；演练环境本身不发真实来源请求。

## 4. D0 检查流程

D0 是人工准备检查日：只核对、不发请求。两源分别独立检查，互不以对方进度替代。

1. **入口命令**（仓库根目录；默认零网络 dry-run，工作目录必须是不存在的新目录）：

   ```sh
   PYTHONPATH=src python tools/hk206_source_probe.py \
     --source sec.companyfacts.v1 --work-dir /absolute/new-evidence/sec-d0-preflight
   PYTHONPATH=src python tools/hk206_source_probe.py \
     --source eastmoney.hk.native.v1 --work-dir /absolute/new-evidence/emo-d0-preflight
   ```

   dry-run 报告只列 `planned_company_keys`、计划请求与缺口；`company_keys` 为空、请求数为 0。
   东财预检可省略 `--period`，报告会给出 `hk_annual_spans_required` 缺口（36）。退出码：dry-run/passed
   为 0，blocked/failed 为 1，命令被拒绝为 2。
2. **SEC 通过判据**：§2.1–2.6 全部勾选并各有证据；dry-run 预检列出且仅列出六家计划公司；
   审批人书面核准。通过后由人工另选 D+0 日期（本文与工具均不排期）。
3. **东财通过判据**：§3.1 书面授权取得并核实，3.2–3.5 全部满足；dry-run 预检列出且仅列出三家公司；
   审批人书面核准。
4. **不通过时**：该来源保持 blocked/准备状态，在 `<evidence-root>/reviews/`（或任意本地人工文件）
   中列出未满足项清单；不执行 `--allow-network`，不改审批状态。

## 5. 日证据目录规范（固定契约）

evidence-root 是人工指定的一个目录，结构如下。目录契约与并行实现的演练工具
`tools/hk206_drills.py` 对齐；probe 与验收工具的行为以
`src/investment_agent/materials/source_probe.py`、`source_observations.py` 为准。

```
<evidence-root>/
  observations/<source>/<probe 工作目录>/report.json   # tools/hk206_source_probe.py 产出；
                                                       #   归档 manifest/body 与报告同目录树下，
                                                       #   报告内 archive 引用相对该报告目录解析
  drills/<source>/<drill>-<时间戳>/evidence.json        # tools/hk206_drills.py 产出；
                                                       #   <drill> 六类固定名，<时间戳> 保证目录唯一
  reviews/                                             # 人工复核文件（年度对照记录、范围确认、缺口清单等）
  ledger-<source>.json                                 # 账本（§6）
```

- `<source>` 为来源 id 原文：`sec.companyfacts.v1` 或 `eastmoney.hk.native.v1`。
- `<drill>` 六类固定名：`shutdown`、`inflight_stop`、`restart_resume`、`old_versions_readable`、
  `offline_replay`、`probe_stop_withdrawal`。各演练证据文件为 `evidence.json`，由
  `tools/hk206_drills.py` 产出（子命令与参数以该工具 `--help` 为准；唯一固定契约是上述落点路径
  与六类名称）。
- `<probe 工作目录>` 即 probe `--work-dir` 指定的新目录，建议按观测日人工命名；重复观测须另起新目录。
- **review/账本中所有 citation 引用相对 evidence-root 解析**，且必须给出文件 SHA-256 并与实际文件
  匹配；路径必须是安全相对路径（非绝对、不含 `..`、不含反斜杠、沿途与目标均不得为符号链接、
  必须解析为 evidence-root 内的普通文件——`source_observations.py` 的 `_safe_relative_file` 规则）。
- `tools/hk206_source_acceptance.py` 的 `--output` 若落盘，必须是 evidence-root 之外的新文件；
  已有文件或 evidence-root 内路径会被拒绝。

## 6. 账本与人工 review 模板

模板文件在 `docs/product/hk-us-expansion/d0-templates/`：`ledger.template.json`（空账本）、
`review.template.json`（review 声明骨架）、`drill-citations.template.json`（六类演练 citation 骨架）。
下划线开头的 `_comment` 字段仅为模板注释，**正式 review 文件不带任何 `_comment` 字段**
（`source_observations.py` 对 review 是宽松 dict 校验，模板的作用是引导正确形态而非放宽它）。

**账本**（schema 1，初始样例照抄 35 号；东财账本把 `source` 换成 `eastmoney.hk.native.v1`）：

```json
{
  "schema_version": 1,
  "source": "sec.companyfacts.v1",
  "adapter_version": "1",
  "observation_reports": [],
  "reviews": []
}
```

- `adapter_version` 必须等于当前适配器版本（两源当前均为 `"1"`，见
  `us_sec.py`/`hk_native.py` 的 `ADAPTER_VERSION` 与 `observation_plan.yaml`；版本变化后旧账本作废）。
- `observation_reports` 是 report.json 相对 evidence-root 的路径列表；`reviews` 是 review 声明数组，
  每份声明以 `report_sha256`（该报告文件 SHA-256）绑定到对应报告。

**review 声明字段**（与 `source_observations.py` 校验一致，不发明新字段）：

| 字段 | 语义 |
| --- | --- |
| `report_sha256` | 所审 report.json 文件的 SHA-256；重复绑定同一报告、或绑定不到账本内报告均为问题 |
| `reviewer` | 非空字符串，人工复核人身份 |
| `reviewed_at` | 带时区 ISO 时间；必须晚于报告 `observed_at` 且不晚于当前时刻（不得未来） |
| `annual_comparison.status` | `matched` 或 `revision_reviewed`。`unreviewed` 是合法枚举值但**永远不满足 ready**，模板不引导使用 |
| `annual_comparison.expected_periods` | 必须与报告 `periods` **完全一致**（规范化 JSON 相等）。HK 为四个连续完整年度对象（从报告 `periods` 逐字复制，勿手写省略字段）；SEC 报告 `periods` 恒为 `[]`（见下），`expected_periods` 亦为 `[]` |
| `annual_comparison.citation` | 年度对照证据（相对 evidence-root 路径 + SHA-256）。SEC 的空 period 列表仍须由此引用说明适用窗口；HK 窗口须引用人工年报对照记录，不得由日历推定 |
| `source_scope_review.status` | 只能 `approved` |
| `source_scope_review.citation` | 来源使用范围人工复核证据；SEC 应覆盖真实联系 UA 与聚合速率证据，东财应覆盖端点特定书面授权（35） |
| `drills.<name>.status` | 六类各自 `passed` |
| `drills.<name>.citation` | 六类各自证据引用（相对 evidence-root + SHA-256）；`restart_resume` 是独立演练证据，一次性 probe 没有断点续跑能力，不得以 probe 冒充；`probe_stop_withdrawal` 是特批 probe 的真实 STOP/许可撤回证据 |

**SEC 报告 periods 为空的语义**：SEC 端年度窗口由适配器当期选择（报告
`period_selection=adapter_default_latest_fiscal_years`，`periods` 恒为 `[]`；probe 对 SEC 拒绝
`--period`）。因此 SEC review 的 `expected_periods` 也是 `[]`，年度窗口的人工核实通过
`annual_comparison.citation` 引用的年报对照证据表达，而不是写入 periods。

## 7. 每日操作序列（D+0/D+1/D+2 执行时使用；本文只写步骤，不排期）

每一步失败即当日不合格，转 §8。以下命令均在工作树仓库根目录执行；`--work-dir` 必须是
evidence-root 下不存在的新目录。

1. **前提重申**：许可与公司范围批准仍有效、审批未撤回；日常 global/source/ops 刷新保持关闭。
2. **一次性隔离 probe**（人显式执行；单实例串行）：

   SEC（UA 必带真实联系邮箱，即 §2.3 核准值）：

   ```sh
   PYTHONPATH=src python tools/hk206_source_probe.py \
     --source sec.companyfacts.v1 \
     --work-dir /absolute/<evidence-root>/observations/sec.companyfacts.v1/<新工作目录> \
     --user-agent "Product/Name (contact email)" \
     --allow-network
   ```

   东财（四个显式 `--period`，年度值来源于 §3.4 的人工年报核实结果；下例为当前已核实参考窗口
   2022–2025，执行日若有更新年度披露，先人工对照最新年报再替换）：

   ```sh
   PYTHONPATH=src python tools/hk206_source_probe.py \
     --source eastmoney.hk.native.v1 \
     --work-dir /absolute/<evidence-root>/observations/eastmoney.hk.native.v1/<新工作目录> \
     --user-agent "<书面授权核准的 UA 与联系方式>" \
     --period 2022-01-01/2022-12-31 \
     --period 2023-01-01/2023-12-31 \
     --period 2024-01-01/2024-12-31 \
     --period 2025-01-01/2025-12-31 \
     --allow-network
   ```

   运行中需要中止时在 probe 工作目录创建 `STOP` 文件（每次请求前与限速等待后都会复核）；
   真实 STOP/许可撤回停止证据属 `probe_stop_withdrawal` 演练（§5）。
3. **当日报告自检**：`report.json` 的 `status=passed`、`requests_failed=0`、`issues` 仅含
   `unmapped`/`unmapped_items` 类信息项；`company_keys` 精确覆盖该源全部受控公司；SEC 逐公司核对
   当日 Submissions 列出的全部分片均有对应成功请求记录；东财核对 9 个公司/表响应与 `periods`。
4. **演练证据补齐**：确认当日所依赖的六类演练证据在 `drills/<source>/...` 下齐全（命令以
   `tools/hk206_drills.py --help` 为准）；缺失项当日补齐或在 review 中如实记录为缺口（导致
   pending）。
5. **人工复核**：完成年度对照与来源范围复核，文件写入 `<evidence-root>/reviews/`，并按 §6 模板
   填写 review 声明（引用全部相对 evidence-root 且带 SHA-256）。
6. **账本追加**：`ledger-<source>.json` 的 `observation_reports` 追加该报告相对路径，
   `reviews` 追加对应 review 声明。同日多次重试只以最新报告替换当日条目，不新增日期。
7. **离线汇总**：

   ```sh
   PYTHONPATH=src python tools/hk206_source_acceptance.py \
     --ledger /absolute/<evidence-root>/ledger-<source>.json \
     --evidence-root /absolute/<evidence-root> \
     --output /absolute/<evidence-root>-外/新文件.json
   ```

   汇总内部已按报告执行 `verify_capture`（manifest/body SHA、身份、适配器版本、响应集合）、
   逐响应 `fetched_at` 当日核对与当前适配器重解析（35；`source_observations.py`）。
8. **结果解读**：只可能是 `pending`（读 `pending_reasons` 补缺口）或 `technical_review_candidate`
   （进入人工来源许可与真实性复核；不写 `approved`/`accepted`，不改 `source_policy.yaml`）。
   三日合格条件与逐日进入条件见 35 号两源矩阵。

## 8. 失败恢复步骤

- **429 / Too Many Requests**：两条路径行为不同，不要混用。
  - 一次性 probe（D+n 当日使用）：收到 429 即终止状态、整源停止（`terminal_http_429_stop_source`），
    不等待、不重试；该来源当日不合格。
  - 日常 worker（本阶段保持关闭）：遵守响应 `Retry-After`（秒数或 HTTP 日期）但最多等待 5 秒
    （`MAX_RETRY_AFTER_SECONDS=5.0`），随后按有界重试策略处理。
  若 `Retry-After` 指示超过本地 5 秒上限，意味着截短后重试可能违背上游指示（34）：该来源当日
  暂停，人工评估（是否继续、是否联系来源方、是否重选 D+0）前不得再请求。
- **403 / Access Denied**：立即停止整源（probe 自动终止该来源后续请求并归档拒绝响应）。
  - SEC：按官方 FAQ 指引联系 `webmaster@sec.gov`，附错误信息与本机 IP（34）；**不换 IP、不轮换/
    伪装 UA、不盲目重试、不绕过拒绝**。
  - 东财：按 3.1 书面授权中的 403 处置条款执行；无条款即停止并经正式渠道人工联系。
- **结构漂移 / 哈希不符**：验收会报"当前适配器解析/结构漂移"或引用哈希不匹配类 issue。保留原工作
  目录与原件不动，不得修改或重贴；排查适配器与上游结构差异，当日判不合格。
- **streak 清零与重选 D+0**：当日停止、请求或解析失败、范围缺失、HTTP 非成功、结构漂移、未解释
  版本变化、引用哈希不符或演练不通过，均令该来源 streak 清零（35）。恢复流程：
  1) 记录失败与根因（人工文件，入 `reviews/`）；2) 修复问题；3) 重新满足对应来源 §2/§3 全部前置；
  4) 确认审批仍有效；5) 人工另定新的 D+0 日期与窗口。
- **旧 archive 纪律**：旧 archive 不得改时间戳、不得重新标注成新观测日；重复报告内容、重复
  manifest 哈希不能补足三日（验收显式拒绝）；同日重跑不增加日期数。

## 9. 红线条目

- 模拟报告（`simulation`/`dry_run`）永不构成真实观测日期，不计入三日。
- 自动汇总只输出 `pending` / `technical_review_candidate`，绝不写 `approved`/`accepted`，不启用
  任何全局或来源开关，不宣称 HK2-06 完成。
- 一次性 probe 没有断点续跑（无 checkpoint/restart）；`restart_resume` 必须由独立隔离演练另行
  留证，不得以 probe 冒充。
- 单进程进程内 limiter（SEC 0.5 秒最短间隔）不是跨进程/跨机器聚合限流证明。
- 若取得的许可仅限个人研究，产品用途必须约束在书面确认范围内；不得借再使用的一般表述扩大用途。
- 公开端点可访问、无需登录、旧数据曾被保存，都不等于采集、缓存或再使用已获许可（34）。
- UA 通过格式校验不等于联系邮箱可收信、不等于请求身份合规（36）。
