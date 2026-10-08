# P1 运维手册

状态：Current（2026-10-07，对应 P1 代码完成；迁移 `019`）

适用范围：目标池采集、因子、筛选、候选、观察池研究与 iFinD 核对的日常运行。
设计依据见 [`funnel-design.md`](funnel-design.md)，任务与决策见 [`p1-plan.md`](p1-plan.md)。
安全部署见 [`security-deployment.md`](security-deployment.md)，备份见 [`backup-and-recovery.md`](backup-and-recovery.md)。

## 1. 数据源分工（D6 二次修订、D7）

| 数据 | 来源 | 说明 |
|---|---|---|
| 指数成分、证监会行业 | baostock | 免费，无需凭证；按 `updateDate` 生效 |
| 申万 2021 行业 | AkShare（申万官网） | 需随仓库附带的中间证书，见第 6 节 |
| 日线（不复权 / 后复权）、PE TTM、PB MRQ、PS TTM、停牌、ST | baostock | 不存前复权 |
| 季度财务、实际披露日、分红 | baostock | `pubDate` 决定可得日期 |
| 归母净利润、营业总收入、经营现金流、负债率兜底、自由现金流 | AkShare（东财） | baostock 负债率 < 0.02 视为缺陷值，改取东财 |
| 按需核对 | iFinD（经 ifind-proxy） | 只在晋级、冲突、缺失、复核、每周抽样时调用；5 小时窗口预算默认 150 次 |

字段的主 / 备来源与容差登记在 `config/field_sources.yaml`；取值顺序为
已核对值 > 主来源 > 备用来源，iFinD 核对发现冲突时以 iFinD 为准。

## 2. 首次部署

1. 配置 `.env` 启动层（见 README「配置」）：
   - `API_KEY`（生产环境 ≥ 32 字符）；
   - `SETTINGS_ENCRYPTION_KEY`（加密数据库中的 API key；与数据库备份分开保管）。
2. 迁移：`ia migrate up`（必须在启动 api 之前，否则日志出现 `settings.import_failed`）。
   启动后在 `/app/settings` 填写并「测试连接」：
   - iFinD 网关地址与 key（与 ifind-proxy 的 `PROXY_API_KEY` 一致）；
   - LLM 地址 / 模型 / key（规则起草与解释，默认模型 deepseek-v4-flash）。
3. 历史回补（全部可中断续跑；**务必显式指定 `--as-of`**，否则过零点再跑会按新日期重新规划）：

   ```bash
   ia snapshot backfill --universe CSI300 --as-of 2026-10-07 --dry-run
   ia snapshot backfill --universe CSI300 --as-of 2026-10-07 --now
   ia snapshot backfill --universe CSI300 --as-of 2026-10-07 --provider akshare --with-cash-flow --now
   ia snapshot backfill --universe CSI300 --as-of 2026-10-07 --status
   ```

   本机实测：baostock 部分约 3 小时 15 分（439 只，含 139 只历史调出股）；分红约 15 分钟；
   东财按期汇总约 4 分钟；东财单股现金流量表约 2.5 小时。
4. 同步规则模板：`ia rules sync`（筛选任务每次运行前也会自动同步）。
5. 启动三个常驻进程：`ia api serve`、`ia worker start`、`ia scheduler start`。

## 3. 调度（Asia/Shanghai，标准 crontab 星期：0 = 周日）

| 任务 | 时间 | 门控 | 说明 |
|---|---|---|---|
| 成分同步 `universe_sync` | 周一 08:00 | — | 有进出时发 `info` 通知 |
| 行业同步 `industry_sync` | 每天 08:00 | 当月首个交易日 | 证监会 + 申万 |
| 日线快照 `market_snapshot` | 周一至周五 18:30 | 交易日 | baostock；发布时间待节后首个交易日确认 |
| 自选股研究刷新 | 周一至周五 19:30 | 交易日 | snapshot 模式，读取当天快照 |
| 财报快照（周） | 周六 09:00 | — | baostock + 东财按期汇总；披露季内加东财现金流量表 |
| 财报快照（截止日） | 每天 19:00 | 4/30、8/31、10/31 后首个交易日 | 同时检测逾期披露 |
| 分红快照 | 随周六财报任务 | — | 当年与上一年 |
| 周筛选 `screen_run` | 周日 20:00 | — | 生成候选并为最强命中模板起草规则 |
| 候选过期清理 | 每天 07:00 | — | 默认 14 天过期 |
| iFinD 核对派发 | 每 30 分钟 | 有到期请求时 | 受预算约束，不足时顺延 |
| iFinD 抽样校准 | 周日 21:00 | — | 观察池随机 10 只（按 ISO 周确定） |

调度器启动时会补跑当天已错过的任务；所有任务按日期幂等。

## 4. 日常检查

- **数据健康页** `/app/health`（API：`GET /health/data`）：
  - 「快照积累 X / 8 周」是 P1 阶段退出指标：连续 8 个完整周（每周每个交易日日线完整率 ≥
    `SNAPSHOT_COMPLETENESS_THRESHOLD`，默认 0.95）；无交易日的周不计也不中断。
  - 采集完整率、缺失清单、新鲜度、回补进度、iFinD 预算与核对队列、来源冲突、逾期披露。
  - 「上游调用」：每个免费数据源（baostock / AkShare）最近 24 小时的调用 / 失败 / 熔断拒绝次数与熔断状态，
    来自 `provider_call` 台账（见 4.1）。
  - 警告级别 warn / error 同时显示在首页。

### 4.1 上游熔断与调用台账

- **熔断器**（worker 进程内存，重启清零）：采集任务自身的连续失败断路器（baostock 连续 5 次瞬态失败、
  东财连续 3 次失败）触发时，把该数据源整体置为"熔断中"，窗口 5 秒起按 5s / 15s / 45s / 2 分 / 5 分递增；
  窗口内所有该源的任务**不打上游**，直接以 `provider_unavailable` 结束为 `partial`，窗口过后第一次调用试探，
  成功即复位、再失败则进入下一档。瞬态失败才计数；列名变化等终态错误不开熔断。
- **调用台账** `provider_call`：每次逻辑调用（含客户端内部重试）一行，`outcome` 为
  `ok` / `transient` / `terminal` / `skipped_open`（被熔断拒绝）；熔断器每次打开也记一行
  （`endpoint = circuit`，`duration_ms` 为窗口长度），API 进程据此显示"熔断中，至 …"。
  iFinD 预算台账 `ifind_call_ledger` 不变。
- **保留 90 天**：`ia admin prune-provider-calls [--days 90]`，建议每月跑一次。
- **候选页** `/app/candidates`：周日筛选后审阅新候选与规则草稿。
- **通知**：目前以 Markdown 写入 `DATA_DIR/notifications/`；`action_required` 需要人工处理。

## 5. 常见情况处理

| 现象 | 原因 | 处理 |
|---|---|---|
| 日线落后 ≥ 1 个交易日 | baostock 未更新或任务失败 | 看健康页对应任务的缺失清单；手动补采 `ia snapshot collect-market --universe CSI300 --date <D> --now` |
| iFinD 核对显示「已延后」 | 5 小时窗口预算用尽 | 正常现象，窗口滚动后自动继续；`ia verify status` 查看下次可用时间 |
| 通知「iFinD 额度已用尽」 | Kimi 网关 5 小时额度（可能与日常 Kimi 使用共用） | 无需操作；当前批次停止，下个窗口自动续采。频繁出现时在设置页调低「iFinD 5 小时预算」 |
| 东财采集 `partial`（`provider_unavailable`） | 东财接口连续失败，或该源仍在熔断窗口内（健康页「上游调用」显示熔断至何时） | 下次调度自动重试；持续失败时检查 AkShare 版本与接口列名（列名变化会直接报错） |
| 逾期披露通知 | 过法定截止日仍未见该期报告 | 人工关注；该股票在筛选中被排除，补披露后自动解除 |
| 规则草稿一直「起草中」 | 设置页未配置 LLM key 或 worker 未运行 | 在设置页填写并测试连接（worker 下个任务生效）；候选页可点「重新起草」 |
| 回补中断 | 进程退出或网络错误 | 用**相同的 `--as-of`** 重跑同一命令，已完成部分自动跳过 |
| 运行卡在 running | 进程被杀 | worker 启动与运行中会定期回收超过心跳超时的任务 |

## 6. 年度 / 不定期维护

- **交易日历**：交易所公布次年休市安排后（通常 12 月），补入 `config/calendars/a_share.yaml`；
  到期前 60 天健康页与调度日志会提醒，未知年份的交易日门控任务会停止（fail-closed）。
- **申万代码表**：`python tools/refresh_shenwan_directory.py` 查看差异，确认后加 `--write` 并提交。
- **申万官网证书**：`src/investment_agent/providers/certs/swsresearch-intermediate.pem` 文件头写有刷新步骤；
  证书校验失败时申万同步会终止并提示更新证书。
- **规则模板**：修改 `config/rule_templates/*.yaml` 后运行 `ia rules validate <file>`，下次筛选自动同步为新版本；
  已生效的个股规则不会被自动改写。模板阈值变更须先回测（P3）。
- **测试 fixture 重录**：`tools/capture_baostock_fixtures.py`、`tools/capture_golden_fixtures.py`。

## 7. 已知限制

- 目标池目前只有沪深 300；baostock 成分只覆盖沪深 300、上证 50、中证 500。
- 模板全部为 `uncalibrated`，回测校准在 P3。
- 推送渠道尚未接入（P2），通知只落盘。
- 手填行业恰为申万行业名时尚不映射为对应行业组（会改变 golden 分类，需要评分版本升级后再启用）。
