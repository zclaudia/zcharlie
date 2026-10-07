# zcharlie — 个人 A 股研究台

单用户、研究辅助型的个人投资研究系统：持续收集自选 A 股的证据、计算可复现的
确定性评分、发现"上次以来的变化"，并把 Agent 解释严格限制在已审计字段上。

**研究辅助，非投资建议。** 没有下单、模拟盘、自动调仓或任何券商连接路径。

- 数据链路：`时点快照（baostock + 东财，iFinD 核对值优先）/ ifind-proxy → canonical(ifind-canonical.v1) → 确定性内核(评分 v1.3) → Agent 解释 → 持久化`
- 长任务：数据库队列 + 独立 worker，支持幂等、原子认领、心跳、孤儿恢复与有限重试
- 持久化：SQLite + Alembic（迁移 `020`）；制品目录 `data/research-runs/<run_id>/`
- 界面：同源 Web UI（`/app`，会话 Cookie 登录）+ CLI + HTTP API

## 快速开始（空环境到第一次研究）

```bash
# 1. 安装（Python 3.13）
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. 配置
cp .env.example .env
# 编辑 .env：API_KEY、IFIND_PROXY_URL/IFIND_PROXY_API_KEY（数据网关）、
# LLM_TIER3_*（解释模型，任何 OpenAI 兼容端点均可）

# 3. 建库（alembic 迁移，尊重 DATABASE_URL）
ia migrate up

# 4. 三个进程：API、worker、scheduler
ia api serve          # 默认 127.0.0.1:8000；远程/Docker 需显式 --host 0.0.0.0
ia worker start       # 研究执行器；调试可用 ia worker start --once
ia scheduler start    # A 股日终自选股刷新（Asia/Shanghai，版本化交易日历）

# 5. 打开 Web UI 登录
open http://127.0.0.1:8000/app
# 用 .env 里的 API_KEY 登录；密钥只用于服务端验证，不写入 Cookie。
```

Web 内即可完成日常闭环：添加自选股（代码 + 行业 + 报告年度）→ 刷新全部 →
在 `/app/stocks/<ticker>` 阅读确定性评分、因子、指标、数据质量、缺失字段、
freshness、Agent 解释（引用可点击定位证据）与"自上次以来的变化"。
运行中心 `/app/runs` 展示 queued/running/success/partial/failed、阶段、
重试、错误摘要与受控制品下载。

候选页 `/app/candidates`（P1-E1）：本周候选列表（行业组、命中模板 + "未校准"标记、
可读理由如 "ROE 12.0% ≥ 8%"、开启 / 到期日、草稿与 iFinD 核对状态；不展示 0～100 分）。
详情页 `/app/candidates/{id}` 先展示逐条理由（点击查看报告期、可得日期、提供方、字段核对状态），
再审阅规则草稿（区间条件按生效参数渲染，参数可在允许范围内修改并保存），
然后"批准并晋级 / 驳回草稿 / 忽略候选"（后两者必填理由），并显示 iFinD 核对与决策记录。
今日概览新增"本周候选"计数（待处理、草稿待审阅、起草中、iFinD 核对未完成）。

数据健康页 `/app/health`（P1-E2，导航"数据健康"）：快照积累进度"快照积累 X / 8 周"（阶段退出指标）、
日线 / 财报 / 分红新鲜度与覆盖率、各采集任务最近一次完整率（缺失按 failed / stopped / quota / no_data /
pending 分类，可展开缺失清单）与近 8 次趋势、成分与行业同步、历史回补进度、iFinD 预算与核对队列、
字段冲突、逾期财报，以及提醒列表。只读，不触发采集。

- **积累口径**：目标池（`snapshot.universes` 第一个）的实时 `market_snapshot` 运行（baostock，
  `success` / `partial` 且采到至少一只；回补不算）按交易日所在 ISO 周统计；同一交易日取最好的一次。
  一周至少有一个交易日的快照、且当周最低完整率 ≥ `SNAPSHOT_COMPLETENESS_THRESHOLD` 即达标；
  无交易日的周（按交易日历）与进行中的本周既不计数也不中断，其余周使连续周数归零。进度 = 连续达标周数。
- **提醒规则**：最新日线落后应有交易日（今天之前最近的交易日）≥ 1 个交易日为 warn、≥ 3 个为 error；
  最新日线覆盖成分低于阈值；任一采集任务最近一次失败（error）或完整率低于阈值（warn）；
  24 小时内因 iFinD 额度或数据源连续失败提前停止；成分同步超过 14 天、行业同步超过 40 天（info）；
  最近一个已结束的交易周未达标（积累归零）；iFinD 预算用尽；近 7 天的实时字段冲突（回补冲突只列出不提醒）；
  逾期财报；交易日历到期。warn / error 级提醒同时并入 `/dashboard/today` 的 `warnings`
  （`source = "data_health"`，`link` 指向页内分区），今日概览另显示"数据健康"计数卡片。

## CLI

| 组 | 命令 | 用途 |
|---|---|---|
| `ia migrate` | `up/down/current/history` | alembic 迁移（尊重 `DATABASE_URL`） |
| `ia api` | `serve` | FastAPI 服务 |
| `ia worker` | `start [--once] [--kinds ...]` | 队列 worker（研究 + 快照采集，按 kind 分派） |
| `ia scheduler` | `start` | 日终刷新与快照采集调度 |
| `ia snapshot` | `sync-universe`、`sync-industry`、`collect-market`、`collect-financial`、`backfill`、`status` | 时点快照采集（见下节） |
| `ia rules` | `validate <file>`、`sync`、`list`、`show <id[@ver]\|instance-id>`、`draft --ticker --template [--as-of] [--now]` | 规则模板 / 实例校验与版本库、LLM 规则起草（见“规则与筛选”） |
| `ia screen` | `run [--universe] [--as-of] [--now] [--rerun]`、`show <run_id> [--reasons]` | 周频模板筛选 |
| `ia verify` | `status`、`request --ticker [--trigger] [--fields] [--period]`、`run [--now]` | iFinD 按需核对队列与预算（见“iFinD 按需核对”） |
| `ia research` | `snapshot <ticker> [--as-of] [--industry]`、`ifind <ticker> --industry --report-year` | 单股同步研究：免费源快照（日常，无 iFinD 调用）/ iFinD 深度研究（计入预算）；制品同 API 路径 |
| `ia admin` | `cost`、`db-migrate` | LLM 成本、旧迁移入口 |

## 快照采集

时点（PIT）快照由 worker 执行、scheduler 入队。数据源分工（D6）：**baostock 负责目标池广度**
（日线、估值、季度财务、披露日），**iFinD 只服务观察池**（`--scope watchlist`，额度有限）。
日线与财报任务的 payload 带 `provider`（`baostock` 默认 / `ifind` / 财报另有 `akshare`，见下文东财采集）与 `scope`
（`universe` 目标池成分 / `watchlist` 启用的观察池条目）；iFinD 只接受 `watchlist`。
幂等键为 `kind:provider:scope:date(:period)`（成分 / 行业为 `kind:universe:date`）。

| 任务 kind | CLI | 调度（Asia/Shanghai） | 内容 |
|---|---|---|---|
| `universe_sync` | `ia snapshot sync-universe --universe CSI300 [--as-of]` | 每周一 08:00 | baostock 成分 → `universe_membership`；有进出发 `info` 通知 |
| `industry_sync` | `ia snapshot sync-industry [--universe CSI300 \| --all-market] [--system sw2021\|csrc ...] [--as-of]` | 每月首个交易日 08:00 | 证监会行业（baostock，`system=csrc`）与申万 2021 行业（AkShare，`system=sw2021`）→ `industry_classification`，默认两者都同步（`--system` 可重复）；不带参数时默认配置的目标池（`snapshot.universes`），全市场需显式 `--all-market` |
| `market_snapshot` | `ia snapshot collect-market [--provider baostock\|ifind] [--scope universe\|watchlist] [--universe CSI300] [--date]` | 交易日 18:30 | 增量日线：不复权 `none`（估值）+ 后复权 `backward`（收益），不存前复权；baostock 行附 `pe_ttm`、`pb_mrq`、`ps_ttm`、`turnover`、`trade_status`、`is_st` |
| `financial_snapshot` | `ia snapshot collect-financial --period 2026-06-30 [--provider] [--scope] [--universe] [--as-of]` | 每周六 09:00 + 4/30、8/31、10/31 后首个交易日 19:00 | 最近已过法定截止期的报告期 + 披露季进行中的报告期，只采该数据源尚无该期的标的；截止日后顺带逾期检测 |

- **baostock（目标池）**：一次运行只登录一次，调用经进程级锁串行，无需令牌桶。每只股票每个序列从该数据源最后一个
  交易日 +1 增量取到 `--date`（无历史的从当日开始，深度历史由回补负责）。停牌日（`trade_status=0`，
  价格为前收、量为 0）原样入库并在运行元数据 `flags` 中列出，ST 同理。单只失败只影响该只；
  连续 5 次瞬时错误视为服务不可用，剩余标的不再调用、记为 `failed`，由 worker 稍后重试。
  财报：profit / balance / cash_flow / growth 四表合并为 canonical 字段（映射登记在
  `research/ifind_mapping.py` 的统一注册表，逐字段标明来源与单位；比率均为小数），
  `availability=actual`、`available_from`=baostock `pubDate`；未披露期为 `pending`。
  CSI300 日增量约 600 次查询、约 2.5 分钟；一个报告期全量约 1200 次查询、约 4 分钟（未披露的只查 1 表）。
- **总股本**：baostock 日线不带股本；因子计算市值时使用该日已可得的最新 baostock 财报
  `total_shares`（`FinancialSnapshotRepository.total_shares_as_of`）。iFinD 观察池采集把 `/stock/info`
  股本直接写在当日不复权行上，`raw_ref` 不再拼接后缀。
- **iFinD（观察池）**：每次 proxy 调用最多 3 只标的；全部调用经共享令牌桶节流（`IFIND_COLLECT_RATE_PER_MINUTE`，
  默认 40/分钟），瞬时错误按批重试（`IFIND_COLLECT_BATCH_RETRIES`，默认 2）。
- **iFinD 额度耗尽**：ifind-proxy 返回 `429`（或旧版本返回带 `usage limit` / `[HTTP 403]` 的 `502`）时归类为
  `QuotaExhaustedError`，不重试：采集任务立即停止剩余批次、保留已入库数据，以 `partial` 收尾
  （元数据 `stop_reason=quota`，未采标的标记 `quota`），并发一次 `action_required` 通知；下次调度自然续采。
  单股研究直接以 `failed` 结束并注明额度耗尽，不占用重试次数。
- **披露逾期**：目标池财报任务在 `as_of` 晚于法定截止日（Q1 4/30、半年报 8/31、Q3 10/31、年报次年 4/30）时，
  把仍无该期财报的成分股写入 `report_overdue`（每只每期一次）并发一次 `action_required` 通知；
  迟到的财报入库时以实际 `pubDate` 写 `resolved_on`，记录保留。
- 运行元数据记录完整率：应采 / 实采 / 缺失清单（`failed` 错误、`no_data` 无数据、`pending` 财报未披露、
  `quota` 额度耗尽未采）。有 `failed` 或 `quota` 为 `partial`，全部失败为 `failed`；完整率低于
  `SNAPSHOT_COMPLETENESS_THRESHOLD`（默认 0.95）、失败、额度耗尽或新增逾期时发 `action_required` 通知（每次运行至多一次）。
- **申万 2021 行业（P1-A9，D7）**：`akshare`（锁定 `>=1.19.1,<1.20`）的 `stock_industry_clf_hist_sw`
  一次取回全市场每只股票的 `(计入日期, 6 位行业代码)` 变更历史（约 1 秒），按区间写入
  `industry_classification`（`system=sw2021`，与 `csrc` 并存；`valid_to` = 下一条计入日期），
  存最细的三级代码 + 名称。只更新**已存在**的 listing（目标池 / 回补标的），不为全市场建档；
  `--all-market` 时证监会会建档，申万只覆盖库内已有的 listing。股票代码按前缀推断交易所：
  `6`/`900` → `.SH`，`0`/`2`/`3` → `.SZ`，`920`/`4`/`8` → `.BJ`。计入日期晚于采集日（上海时区）报
  `LookaheadDataError`；返回列变化（网站改版）为终止错误；网络 / 超时 / HTTP 5xx / 429 为瞬时错误。
  一个系统终止失败不影响另一个（运行以 `partial` 结束，元数据 `failed_systems`）。
- **申万代码对照表**：`config/shenwan_2021.yaml`（511 个代码 → 名称、层级、上级），由
  `tools/refresh_shenwan_directory.py` 从申万官网的 `SwClassCode_2021.xls`（同域名、同证书处理，一次请求）
  生成并提交；建议每月或出现 `shenwan_codes_unresolved` 日志时运行。不带 `--write` 只打印与已提交文件的差异，
  `--write` 覆盖文件，需人工审阅后提交（不会自动提交）。`--cross-check` 另按原计划用乐咕乐股
  `sw_index_*_info` + 每个三级行业的 `sw_index_third_cons` 反推核对名称与交易所后缀（乐咕乐股限流严：
  连续请求会 429，随后数分钟 504；`--limit N` 抽样、`--cache DIR` 断点续跑）。同步时对照表里没有的 2021 年后
  代码以代码作名称写入并记日志；2021-07-30 之前已结束的区间、以及 2021 年前退市股票仍挂着的旧代码，是旧版
  （申万 2014 及更早）代码，按代码存名称，不参与行业组映射。
- **行业组（P1-B3）**：`config/industry_groups.yaml` 先按 `as_of` 当时的申万 2021 分类映射（一级默认、二 / 三级覆盖，
  `matched_by` = `sw_l1` / `sw_l2` / `sw_l3`），没有可用申万代码时退回证监会表并给因子值加 `industry_fallback`
  标记，两者都没有时为 `other`（`matched_by=fallback`）。因子 `inputs_ref.industry` 记录所用体系、代码与匹配方式。
- **申万证书**：`swsresearch.com` 下发的证书链缺中间证书，默认校验失败。只对该域名补全：仓库附带
  `src/investment_agent/providers/certs/swsresearch-intermediate.pem`（DigiCert「GeoTrust G2 TLS CN RSA4096
  SHA256 2022 CA1」，有效期至 2032-12-14），调用期间仅替换 AkShare 相关模块里的 `requests`，
  对 `swsresearch.com` 用 certifi + 该中间证书校验，其他域名与全局校验不变。证书更换时按文件头的步骤
  （`openssl s_client` 查 AIA 地址 → 下载 → 转 PEM）替换。
- 原始响应落盘到 `DATA_DIR/snapshot-runs/<run>/attempt-<n>/`，`raw_ref` 指向该文件。
- `--now` 在当前进程立即执行（调试用）；`ia snapshot status` 查看各类任务最近运行与完整率。
  调度配置见 `config/app.yaml` 的 `snapshot:` 段；日线 18:30 基于 baostock 晚间更新，需在正常交易日实测确认。

### 东财财务采集（P1-A11，AkShare）

`financial_snapshot` 任务 `provider=akshare` 加 `part`，行写入 `financial_snapshot`（`provider=akshare`）：

```bash
ia snapshot collect-financial --provider akshare --part summary --period 2025-12-31     # 按期汇总，全市场 3 次调用
ia snapshot collect-financial --provider akshare --part cash_flow --period 2026-06-30   # 单股现金流量表，约 20 秒/只
ia snapshot backfill --provider akshare [--with-cash-flow] [--dry-run|--status]          # D5 报告期回补
```

- **按期汇总**（`summary`）：`stock_lrb_em` / `stock_xjll_em` / `stock_zcfz_em` 各一次 →
  `net_profit_parent`（净利润列即归母）、`revenue_total`、`operating_cash_flow`、`debt_ratio`（东财为百分数，÷100）
  及总资产 / 总负债 / 权益。**单股现金流量表**（`cash_flow`）：`NETCASH_OPERATE`、`CONSTRUCT_LONG_ASSET`
  （`capital_expenditure`），`free_cash_flow` = 两者之差；一次调用取回全部历史，按任务的报告期（`report_period`
  + `extra_periods`）落库，已有全部期次的标的跳过。列名按 akshare 1.19.1 锁定，列变化为终止错误。
- **范围**：目标池成分 + 启用的观察池；回补用报告期以来的全部成分（幸存者偏差），行标记 `is_backfill`。
- **时点**：`available_from` 优先取同期 baostock `pubDate`（`actual`），否则取东财公告日（`approx`；汇总表的公告日是
  最近一次更新该行的公告，偏晚、保守）。东财数据是重述后的最新值：现金流量表 `UPDATE_DATE > NOTICE_DATE`、汇总表
  公告日晚于 baostock `pubDate`（无则晚于法定截止日）的行标 `restated`。内容有变化才写新版本，不覆盖旧行。
- **交叉校验**：`debt_ratio` 与 baostock 比（baostock 值不合理时只记 suspect）、`operating_cash_flow` 与 baostock
  `CFOToNP × netProfit` 比（实测相对差 < 1e-6）；营收口径不同不比。超容差写入行 `provider_conflicts` 与运行元数据
  `provider_conflict`；关键字段冲突 / 缺失投递 iFinD 核对请求（P1-A13）。
- **金融股**：银行 / 保险 / 券商四个汇总字段与现金流量表两列均可取到；取不到的字段记 `missing_fields`，不算错误。
- **节流**：调用串行、间隔 ≥ `AKSHARE_MIN_INTERVAL_SECONDS`（默认 1 秒），瞬时错误重试一次；
  连续 3 次失败停止本任务，剩余标的记 `stopped`，运行 `partial`（`stop_reason=provider_unavailable`）并发一次
  `action_required` 通知。
- **调度**：周六与截止日后的财报任务在 baostock 之后入队同期汇总；现金流量表只在披露季窗口
  （4/15–5/15、8/15–9/15、10/15–11/15）随周六任务入队（沪深 300 约 1.4 小时）。

### 字段来源登记表（P1-A12）

`config/field_sources.yaml` 为每个因子层字段登记 `primary`、`fallback`、口径、容差（默认 2%）、是否关键字段与按数据源的
`sanity` 规则；iFinD 不得为主 / 备来源（只做核对）。目标池视图按「iFinD 已核对值 > 主来源 > 备用来源」逐字段取值：
例如 baostock `debt_ratio` < 0.02（缩小 100 倍缺陷）被拒并改用东财，因子带 `provider_fallback`；无备用值时为缺失并带
`suspect_provider_value`。每个取值在 `FactorValue.inputs_ref` 中带 `provider` 与核对状态
（`unverified` / `verified` / `conflict`；免费源之间超容差为 `conflict`）。测试校验因子库读到的每个字段都已登记。

### iFinD 按需核对（P1-A13）

iFinD 只在固定触发时调用（D6 二次修订）：`promote`（晋级前）> `conflict`（免费源关键字段冲突）>
`missing`（免费源缺失）> `exit_review` > `calibration`（每周抽样）。请求写入 `verification_request`
（同一股票、触发、报告期只保留一个 pending / deferred 请求，重复投递合并字段）；其他流程调用
`investment_agent.verification.enqueue_verification(...)`（可传入调用方 session，与其事务一同提交），
东财交叉校验的冲突 / 缺失已自动投递。

- **预算账本**：每次 ifind-proxy 调用记一行 `ifind_call_ledger`（核对任务、观察池 A3 采集器、单股研究链路都计数），
  按 5 小时滚动窗口计数，上限 `IFIND_BUDGET_PER_5H`（默认 150）。核对任务每批调用前检查余量，不足则剩余请求
  `deferred`，`not_before` = 窗口内足够多的旧调用过期的时刻；遇额度错误（A8）立即停止、全部延后一个窗口并发
  一条 `action_required` 通知。其他路径只计数、不受限。
- **任务 `ifind_verify`**：按优先级出队，同一报告期最多 3 只合并为一次调用，复用 A3 的请求形状
  （`/financial/...` 各端点、`/stock/info` 总股本、`/stock/history` 不复权收盘价）。结果写入 iFinD 快照
  （`provider = ifind`），与观察端按登记表实际取用的免费源值比较（登记表容差）：新版本财务行带
  `fields.verification = {字段: "verified" | "conflict"}`，因子视图对 `verified` 字段取 iFinD 值，
  `conflict` 字段仍取免费源并标记冲突；关键字段冲突发 `action_required` 通知。
- **调度**（`config/app.yaml` 的 `verification:` 段，标准 crontab）：每 30 分钟有到期请求时入队一次
  `ifind_verify`（按时间槽幂等）；每周日 21:00 按 ISO 周固定种子从观察池抽 10 只投递 `calibration`。

```bash
ia verify status                                              # 窗口内已用 / 剩余预算，队列按状态 × 触发统计
ia verify request --ticker 600519.SH --trigger promote        # 全部关键字段，最近年报
ia verify request --ticker 601398.SH --trigger conflict --fields debt_ratio --period 2025-12-31
ia verify run --now                                           # 当场执行一次（消耗 iFinD 额度）
```

### 回补

一次性历史回补（D5，全部经 baostock，不占 iFinD 额度）：

```bash
ia snapshot backfill --universe CSI300 --prices 5y --annual 6 --quarters 4 --dry-run  # 计划与预估，不取数据
ia snapshot backfill --universe CSI300 --prices 5y --annual 6 --quarters 4            # 入队（worker 执行）
ia snapshot backfill --universe CSI300 --status                                        # 各部分进度
```

- 任务 kind `snapshot_backfill`，按部分拆分：`membership`（单任务）→ 成功后自动入队 `industry`（单任务）与
  `prices` / `financials`（每 20 只一个任务）。`--as-of` 默认今天，窗口为 `[as_of - 5 年, as_of]`。
- **成分历史**：按月扫描成分快照，仅在前后两月成员不同时按周细化，找到每次变化的生效日（`updateDate`）；
  窗口内以快照为准重建 `universe_membership`，与窗口之后的实时同步区间合并（无重复、无空档；
  只移动边界的行保留原 `source` / `captured_at`）。重复执行结果不变。
- **幸存者偏差**：回补标的 = 窗口内任何时点的成分股（当前 + 已调出），2026-10-06 实测沪深 300 为
  439 只（当前 300 + 调出 139）。
- **行业历史**：窗口起点、窗口内每年 1 月 1 日与 `as_of` 各查一次全市场证监会行业（每次约 1 分钟），
  只写回补标的，不为非成分股建档；变化按年度采样定日。同一任务再调用一次 AkShare，把回补标的的
  申万 2021 完整历史（精确到计入日期）写为 `sw2021`；申万终止错误只记入元数据，不影响证监会部分。
- **日线**：不复权 + 后复权，从窗口起点取到该股已存 baostock 最早一根的前一天（无数据则到 `as_of`），
  每只每序列一次查询；行标记 `is_backfill=true`。时点读取（`series_as_of`）默认按交易日可见回补行，
  `include_backfill=False` 时严格按 `captured_at`。
- **财报**：最近 6 个年报 + 最近 4 个已过法定截止日的报告期，跳过已存的 baostock 期；`is_backfill=true`、
  `available_from`=实际 `pubDate`；上市日（baostock `ipoDate`）之前披露的报告保留并标记 `pre_listing`。
- `report_overdue` 不回补（只做实时检测）。
- **预计耗时**（沪深 300、5 年，`--dry-run` 实测预估上限）：成分约 1 分钟、行业约 7 分钟、日线约 1.5 小时、
  财报约 2.7 小时，合计约 4 小时；已存数据跳过。
- **可续跑**：每个任务按股票幂等（跳过已存数据），单只失败只影响该只，连续 5 次瞬时错误停止本任务。
  中断或失败后重新执行同一命令即续跑：已完成部分不动，`partial` / `failed` 的任务只以剩余标的重新入队
  （幂等键加 `:r<n>`）。`--now` 在当前进程依次执行所有排队的回补任务（无 worker 时使用）。

## 单股研究：数据源模式（P1-B4 / B5）

单股研究有两种数据源模式，评分内核与评分版本相同（v1.3），canonical 指标都由因子库计算（一份定义）：

- **`snapshot`（默认）**：从时点快照库读 `load_snapshot_view(policy=TARGET_POOL)`，字段按来源登记表取值
  （iFinD 已核对值 > 主来源 > 备用来源），不调用 iFinD。自选股 `provider` 为 `auto` / `snapshot` 时走此模式，
  `POST /research/single-stock` 默认也是它；行业与报告年度可省略（用当日申万行业组、`as_of` 时已披露的最近年报）。
  每个 canonical 字段的 `field_sources` 记录提供方与核对状态（`unverified` / `verified` / `conflict`，冲突进入
  `provider_conflicts` 并要求复核）。快照库没有该股数据（例如不在目标池）时，运行以 `partial` 结束并投递
  iFinD `missing` 核对请求；关键字段缺失（如资本支出 / 自由现金流）也投递同类请求，由核对任务在预算内执行。
- **`ifind`**：手动深度研究（`ia research ifind`、API `provider=ifind`、自选股显式 `provider=ifind`），
  每次 proxy 调用记入 iFinD 预算账本；需要行业与报告年度。golden corpus 仍以 iFinD fixture 逐字段重放（D3）。

运行 metadata 与研究快照记录 `source_mode`。某只股票从 iFinD 切到快照后的第一次运行标记 `source_changed`：
变化集保留全部差异但不计入重要变化，并单独发送一条通知说明"分数跳变可能源于数据源切换"。
行业分类：用户填写的行业文字优先（等于行业组 id 时直接取该组，否则沿用旧关键词表）；未填写时用数据的
申万（证监会兜底）行业组。字段映射、两种模式的偏差与已知差异见
[`docs/product/observation-snapshot-mode.md`](docs/product/observation-snapshot-mode.md)。

## 规则与筛选

- 模板文件 `config/rule_templates/*.yaml`（P1-C2，五个策略原型，`uncalibrated`）；`ia rules validate <file>`
  离线校验（未知因子、op/值类型、自由文本、参数越界、区间名、行业组，错误一次性汇总），
  `ia rules sync` 按内容哈希入库（`rule_template`，内容变化即升版本，旧版本不改）。
- 规则实例（`rule_instance`）：`draft → active → retired`，每次修改生成新版本，批准时退役上一 active 版本。
- 周筛选任务 `screen_run`（每周日 20:00，`config/app.yaml` 的 `screen:` 段）：先同步模板，再对目标池按时点计算因子、
  运行全部模板的 `screen` 条件，结果写入 `screen_result`（命中 / 截断 / 未命中 / 跳过 / 剔除，附结构化理由）。
  ST 与逾期未披露标的全局剔除；每模板默认最多 20 个命中，按"阈值内距离"强度排序。
  `ia screen run --as-of 2026-09-30 --now` 当场执行，`ia screen show <run_id> --reasons` 查看理由。
- 规则起草（P1-C5，任务 `rule_draft`，由候选生命周期按 `rule_draft:{candidate_id}:{template}@{ver}` 投递）：
  LLM（`LLM_TIER3_*`）只为模板**选择参数取值**（条件与区间来自模板），输入为该股时点因子值（值 / 质量 / 标记 / 单位，
  含 5 年估值分位）、行业组与申万行业、命中的筛选理由与用户偏好备注。输出须通过校验才入库：参数存在且在
  `param_ranges` 内、逐参数理由只引用输入中非空的因子、无交易指令用语（买入 / 卖出 / 加仓 / 止损 / 目标价……）、
  P1-C1 实例校验；不通过则把问题反馈给 LLM 重试一次，仍失败则不入库、任务 `partial`（"草稿不可用"，可人工补写）。
  通过的草稿存为 `scope=candidate` 的 `draft`（`drafted_by=llm:<model>`，`draft_rationale` 为 JSON），同内容重复起草返回已有草稿；
  每次调用记入 `llm_call`（`purpose=rule_draft`）。手动试用：`ia rules draft --ticker 600519.SH --template quality_growth --as-of 2026-09-30 --now`
  （会调用已配置的 LLM）。

## 候选与决策日志（P1-D1～D3，迁移 `018`）

- 候选 `candidate`：`open → promoted / dismissed / expired`，同一标的最多一条 `open`。
  每次 `screen_run` 成功后，命中（`hit`，不含截断）自动建候选：已有 `open` 候选则合并来源；
  已在自选股（启用）中、或 30 天内被忽略过的标的跳过（记入运行 metadata 的 `candidates` 段）。
  候选默认 14 天过期：每日 07:00 扫描（`config/app.yaml` 的 `candidates:` 段），每次筛选开始时也扫描一次。
  时长见 `CANDIDATE_EXPIRY_DAYS`、`CANDIDATE_DISMISS_COOLDOWN_DAYS`。
- 新候选自动投递 `rule_draft` 任务（LLM 起草规则，幂等键 `rule_draft:{candidate_id}:{template}@{version}`），
  payload 附最近 10 条驳回 / 忽略理由作为偏好输入；`draft_state`：`none / pending / ready / failed`。
- 决策日志 `decision` 只追加、不可改删：`nominate`、`promote`、`dismiss`、`approve_rule`、`reject_rule`、
  `edit_rule`、`expire`，与状态变化同一事务写入，并记录当时的筛选理由与因子值（含快照引用）。
  `dismiss`、`reject_rule` 必填理由。
- 晋级 `POST /candidates/{id}/promote`：（可选改参数 → 新版本）→ 批准规则为 `active`（退役旧 active）→
  写入 / 启用自选股（行业取当日申万一级名称，缺失时用证监会名称；报告年度取已披露的最近年报）→ 候选 `promoted`，
  以上一个事务；随后尽力投递 iFinD `promote` 核对与一次单股研究，失败只出现在响应 `warnings` 中。
- 接口不返回 0～100 分数；模板 `calibration_status`（目前全部 `uncalibrated`）随候选与规则返回，界面需标注"未校准"。

## HTTP API（`X-API-Key` 或 Web 会话）

| 端点 | 用途 |
|---|---|
| `GET /healthz` | 存活探针（无鉴权） |
| `POST /research/single-stock` | 入队单股研究（HTTP 202 + `run_id`，幂等键） |
| `GET /research/runs`、`/research/runs/{id}` | 运行列表 / 生命周期详情 + 版本化结果 |
| `GET /research/runs/{id}/changes`、`/change-notification` | 结构化 diff / 重要变化通知下载 |
| `GET /research/runs/{id}/artifacts[/{name}]` | 受控制品下载（限本 run 目录内） |
| `GET /research/stocks/{ticker}/history` | 分页快照历史 |
| `GET /dashboard/today` | 今日概览聚合（运行中/失败/partial/待复核/重要变化 + `candidates` 候选计数 + `data_health` 计数；`warnings` 含日历与数据健康提醒） |
| `GET /health/data` | 数据健康聚合（只读）：`accumulation`、`freshness`、`universes`、`industry`、`collections`、`backfill`、`verification`、`overdue`、`warnings` |
| `GET/POST/PATCH/DELETE /watchlist`、`POST /watchlist/refresh` | 自选股管理与隔离型批量刷新 |
| `GET /admin/llm-calls`、`/admin/llm-calls/cost` | LLM 调用审计 / 月度成本 |
| `GET /candidates?status=&limit=&offset=`、`GET /candidates/{id}` | 候选分页列表（来源、最新理由、草稿状态）/ 详情（理由与快照引用、草稿规则、决策历史） |
| `POST /candidates` | 手动提名 `{ticker, template_id?, note?}` |
| `POST /candidates/{id}/dismiss`、`/promote` | 忽略 `{reason}` / 晋级 `{rule_instance_id?, params_override?, reason?}`（无可批准规则时 409） |
| `GET /rules/{id}`、`PATCH /rules/{id}`、`POST /rules/{id}/reject` | 规则详情（含参数范围）/ 改参数（新版本）/ 驳回草稿 `{reason}` |
| `GET /screen-runs/{id}` | 筛选运行摘要 + 各模板命中 |
| `GET /verification/requests?listing_id=&ticker=&status=&limit=` | iFinD 核对请求（只读）：状态、延后时间、核对一致 / 冲突字段 |

批量刷新：`Idempotency-Key` 重放同一批运行；单只失败不影响其他标的；
只有 meaningful change（或数据源切换后的第一次运行）才生成一次 Markdown 通知，baseline 与噪声保持静默。
快照模式的自选股刷新不需要 `IFIND_PROXY_API_KEY`；未配置时只有显式 `ifind` 的条目失败。

## 配置

`.env`：`API_KEY`、`DATABASE_URL`（默认 `sqlite+aiosqlite:///./data/app.db`）、
`DATA_DIR`、`IFIND_PROXY_URL` / `IFIND_PROXY_API_KEY`、
`IFIND_COLLECT_RATE_PER_MINUTE`、`IFIND_COLLECT_BATCH_RETRIES`、`SNAPSHOT_COMPLETENESS_THRESHOLD`、
`AKSHARE_MIN_INTERVAL_SECONDS`（默认 1）、
`IFIND_BUDGET_PER_5H`（默认 150）、`IFIND_BUDGET_WINDOW_HOURS`（默认 5）、`IFIND_VERIFY_MAX_REQUESTS`（默认 30）、
`LLM_TIER3_*`、`LLM_REQUEST_TIMEOUT_SECONDS`（默认 120）、
`WEB_SESSION_TTL_MINUTES`（默认 720）、`WEB_SESSION_SECRET`、`WEB_COOKIE_SECURE`、
`WEB_PUBLIC_ORIGIN`（远程部署见 [`docs/product/security-deployment.md`](docs/product/security-deployment.md)）、
`LOG_LEVEL`。调度见 `config/app.yaml`；交易日历见
`config/calendars/a_share.yaml`（未知年份 fail-closed）。

评分成功但解释失败时运行以 `partial` 收尾：确定性分数与制品保留，
`explanation` 为 `null` 并注明原因，不重试非瞬时错误。

## 备份与恢复

见 [`docs/product/backup-and-recovery.md`](docs/product/backup-and-recovery.md)：
`tools/backup_research.py` 生成一致备份与 SHA-256 manifest，
`tools/verify_backup.py` 校验，恢复演练已自动化为集成测试。

## 测试与门禁

```bash
pytest -q -m "not network"  # 1219 个测试（离线，fixture 全部脱敏）
ruff check .
mypy src/investment_agent  # strict
```

离线端到端 smoke（P1-F1，`tests/integration/test_funnel_e2e.py`，默认套件内，约 11 秒）：
空库 `ia migrate up` 后经 worker 跑真实任务处理器——成分 / 行业（证监会 + 申万）同步、日线 / 财报 /
分红采集、两年回补、东财汇总 → 模板筛选（每条命中理由回溯到快照行）→ 候选与 LLM 规则草稿 →
HTTP 晋级（决策日志、自选股、iFinD 晋级核对请求、研究入队）→ 快照模式研究 → iFinD 核对写回 →
`/health/data`；最后校验同一 `as_of` 重跑筛选逐字节一致、重跑采集不产生新版本、决策日志完整且不可改。
只在数据源边界替换：合成 baostock / 申万 / 东财（`tests/integration/_funnel_support.py`，10 只股票覆盖
银行、公用事业、周期、成长、亏损股、次新股）、respx 桩 LLM 与假 ifind-proxy；时间冻结，结果确定。

CI（`.github/workflows/ci.yml`）在 push/PR 上运行同样三门禁；默认测试不访问
网络、不使用真实 secrets。golden corpus（13 只不同特征 A 股）离线重放，
任何算法变化必须升级 scoring version 并附差异说明，不允许静默改 golden。

## 已知限制

- 严格单用户；无组织、权限组或订阅体系。
- SQLite 单机存储；并发写入成为瓶颈时才评估 PostgreSQL。
- 目标池与观察池日常数据走免费源（baostock + 东财，D6 二次修订）；iFinD 经 Kimi 网关有 5 小时滚动额度，
  只用于按需核对与手动深度研究。东财单股现金流量表尚未批量采集时，快照模式缺资本支出 / 自由现金流（需复核）。
- 交易日历仅审核到 2026 年；未审核年份 fail-closed。到期前 60 天起调度日志与
  `/dashboard/today` 会给出警告，交易所公布次年安排后需补入 `config/calendars/a_share.yaml`。
- 北交所行情（history/realtime）暂被上游网关阻塞，corpus 中北交所样本
  以沪深小盘替代并记录在
  [`tests/fixtures/research_kernel/manifest.yaml`](tests/fixtures/research_kernel/manifest.yaml)。
- 无任何交易执行路径；未来若考虑自动交易必须作为独立项目重新评审。

## Legacy

早期 NASDAQ-100 Discovery / Attribution 产品已从主干移除，代码保留在 tag
`legacy-nasdaq-final`（设计文档 `docs/superpowers/specs/2026-06-07-investment-agent-design.md`）。

## 文档

- 产品与工程基线：[`docs/product/engineering-baseline.md`](docs/product/engineering-baseline.md)
- 交付路线：[`docs/product/delivery-roadmap.md`](docs/product/delivery-roadmap.md)
- MVP 收口计划：[`docs/product/mvp-completion-plan.md`](docs/product/mvp-completion-plan.md)
- 发布清单：[`docs/product/mvp-release-checklist.md`](docs/product/mvp-release-checklist.md)
- 备份恢复：[`docs/product/backup-and-recovery.md`](docs/product/backup-and-recovery.md)
- 评分 v1.3 变更与回归差异：[`docs/product/scoring-v1.3-changes.md`](docs/product/scoring-v1.3-changes.md)
- 安全部署：[`docs/product/security-deployment.md`](docs/product/security-deployment.md)
- 选股—观察—持仓漏斗设计：[`docs/product/funnel-design.md`](docs/product/funnel-design.md)
- P1 执行计划与决策：[`docs/product/p1-plan.md`](docs/product/p1-plan.md)
- P1 运维手册（部署、回补、调度、日常检查、故障处理）：[`docs/product/operations.md`](docs/product/operations.md)
- 规则模板审阅与决定：[`docs/product/templates-v1-review.md`](docs/product/templates-v1-review.md)
- 观察端 snapshot 模式：[`docs/product/observation-snapshot-mode.md`](docs/product/observation-snapshot-mode.md)
