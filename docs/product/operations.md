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
| 业绩预告、预约披露时间、主营构成 | AkShare（东财） | `earnings_forecast` / `disclosure_schedule` / `business_segment`，只作事实与通知，不进因子 |
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

### 2.1 Docker 一键部署 / 回滚

`tools/deploy.sh` 把 api、worker、scheduler 作为一组部署（本机差异——端口、外部网络、数据库卷——只写在未提交的 `docker-compose.override.yml`）：

```bash
tools/deploy.sh             # 预检 → 停 worker/scheduler → 备份 → 构建 → 迁移 → 启动 → 验证
tools/deploy.sh status      # 三个服务的状态与健康、运行版本、数据库迁移版本、上次部署
tools/deploy.sh rollback    # 回到上一次部署的镜像（不跑迁移）
```

- **预检**：docker 可用；`.env` 有 `API_KEY`、`SETTINGS_ENCRYPTION_KEY`；compose 引用的外部网络（如 `ifind-proxy_default`）已存在；所有服务的 `DATABASE_URL` 一致（防止 `migrate` 迁到另一个库）。
- **备份**：先停 worker / scheduler，用当前镜像在线备份 SQLite 并校验、压缩，存到 `~/backups/deploy-<时间>-<提交号>`（保留最近 5 份；`BACKUP_ROOT` 可改）。备份失败会恢复原服务并中止，镜像和数据库都不动。`--no-backup` 可跳过。
- **构建**：只从 `git archive HEAD` 构建，工作区里未提交 / 未跟踪的文件不会进入镜像；当前镜像先另存为 `investment-agent:prev`。提交号写进镜像，`/healthz` 返回 `{"status":"ok","version":"<提交号>"}`。
- **启动顺序**（`docker-compose.yml` 自带，直接 `docker compose up -d` 也一样）：一次性服务 `migrate`（`ia migrate up`）成功后启动 api，api 健康后再启动 worker 与 scheduler。
- **健康检查**：api 查 `/healthz`；worker / scheduler 在事件循环里每 15 秒刷新 `$LIVENESS_FILE`（`/tmp/*.alive`），超过 120 秒未刷新判为不健康。
- **验证**：等三个服务都 healthy（默认最多 300 秒，`DEPLOY_WAIT_SECONDS`），并确认 api 报告的版本就是刚构建的提交号。部署记录写在 `.deploy/last.env`。
- **回退**：把 `:prev` 标回 `:dev`，只重建 api / worker / scheduler（跳过 `migrate`：旧代码不认识新迁移）。如果上次部署改了数据库迁移版本，脚本拒绝回退，提示先用部署前备份按 [备份与恢复](backup-and-recovery.md) 恢复数据库；确认兼容可加 `--force`。被替换的镜像保存为 `investment-agent:rolled-back`。
- **回退到加存活检查之前的版本（一次性注意事项）**：worker / scheduler 的存活文件健康检查从 `27b354c`（2026-10-15）开始才有。第一次用 `tools/deploy.sh` 部署时，`:prev` 是这之前的旧镜像，它的 worker / scheduler 不会写存活文件，回退后会一直被判为不健康，脚本等待超时报错——但进程本身是正常在跑的。遇到这种情况用 `docker compose logs worker scheduler` 确认它们在工作即可，或者别回退到那个版本。之后再正常部署一次，`:prev` 就是带存活检查的版本，这个问题不再出现。

## 3. 调度（Asia/Shanghai，标准 crontab 星期：0 = 周日）

| 任务 | 时间 | 门控 | 说明 |
|---|---|---|---|
| 成分同步 `universe_sync` | 周一 08:00 | — | 有进出时发 `info` 通知 |
| 行业同步 `industry_sync` | 每天 08:00 | 当月首个交易日 | 证监会 + 申万 |
| 日线快照 `market_snapshot` | 周一至周五 18:30 | 交易日 | baostock；发布时间待节后首个交易日确认 |
| 收盘快照 `close_quote` | 周一至周五 15:10 | 交易日 | 腾讯批量报价写入当日收盘（目标池 + 观察池 + 候选 + 持仓，`provider=tencent`）；cron 键 `snapshot.close_quote_cron` |
| 收盘快照补采 | 周一至周五 16:00 | 交易日 | 同日第二次运行，只补 15:10 仍未收盘 / 未报价的标的；`snapshot.close_quote_retry_cron`。18:30 baostock 到达后读取以 baostock 为准，同日收盘价差超 0.1% 记 `conflict` |
| 观察池研究刷新 | 周一至周五 19:30 | 交易日 | snapshot 模式，读取当天快照 |
| 财报快照（周） | 周六 09:00 | — | baostock + 东财按期汇总；披露季内加东财现金流量表 |
| 财报快照（截止日） | 每天 19:00 | 4/30、8/31、10/31 后首个交易日 | 同时检测逾期披露 |
| 财报事件 `earnings_event` | 周一至周五 19:00 | 1 / 4 / 7 / 10 月的交易日 | 东财业绩预告 + 预约披露时间（目标池 + 观察池 + 候选 + 持仓）每日；主营构成（观察池 + 候选 + 持仓，逐只）每周首个交易日；cron 键 `snapshot.earnings_event_cron`。观察池 / 候选 / 持仓出现新预告或预约日在 3 个交易日内时发 `info` 通知 |
| 分红快照 | 随周六财报任务 | — | 当年与上一年 |
| 周筛选 `screen_run` | 周日 20:00 | — | 生成候选并为最强命中模板起草规则 |
| 候选过期清理 | 每天 07:00 | — | 默认 14 天过期 |
| iFinD 核对派发 | 每 30 分钟 | 有到期请求时 | 受预算约束，不足时顺延 |
| iFinD 抽样校准 | 周日 21:00 | — | 观察池随机 10 只（按 ISO 周确定） |
| 交易日历核对 `calendar_probe` | 周一 08:00 | 日历到期前 60 天内 | 双源核对下一年，一致时写候选文件并发 `info` 通知；分歧发 `action_required`；未公布则静默跳过。cron 键 `snapshot.calendar_probe_cron` |

调度器启动时会补跑当天已错过的任务；所有任务按日期幂等。

## 4. 日常检查

- **数据健康页** `/app/health`（API：`GET /health/data`）：
  - 「快照积累 X / 8 周」是 P1 阶段退出指标：连续 8 个完整周（每周每个交易日日线完整率 ≥
    `SNAPSHOT_COMPLETENESS_THRESHOLD`，默认 0.95）；无交易日的周不计也不中断。
  - 采集完整率、缺失清单、新鲜度、回补进度、iFinD 预算与核对队列、来源冲突、逾期披露。
    新鲜度区块同时显示 baostock 日线与腾讯收盘快照的最新日期；`market_stale` 仍只看 baostock。
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
| 交易日历候选已生成 | 周一核对发现下一年双源一致 | 在 `/app/health` 的「交易日历」卡片点「应用候选」（写入 `DATA_DIR/calendars/`，无需提交），健康页与今日概览同时给出提醒 |
| 交易日历核对分歧 | baostock 与 Sina 对某天是否交易不一致 | 不写入任何文件；下周自动重试，也可在健康页点「检查更新」立刻重试，或用 `python tools/refresh_trading_calendar.py` 看完整差异 |
| 交易日历「应用候选」不可点 / 报数据目录不可写 | `DATA_DIR` 不可写 | 检查数据目录挂载与权限（容器里 `./data` 必须可写）；仓库 `config/` 只读不影响应用 |
| 交易日历读取报错（`无法读取交易日历`） | `DATA_DIR/calendars/` 里的副本损坏或格式非法 | 修复或删除该副本即回落仓库基线；fail-closed 不静默回落是刻意设计 |
| 通知「iFinD 额度已用尽」 | Kimi 网关 5 小时额度（可能与日常 Kimi 使用共用） | 无需操作；当前批次停止，下个窗口自动续采。频繁出现时在设置页调低「iFinD 5 小时预算」 |
| 东财采集 `partial`（`provider_unavailable`） | 东财接口连续失败，或该源仍在熔断窗口内（健康页「上游调用」显示熔断至何时） | 下次调度自动重试；持续失败时检查 AkShare 版本与接口列名（列名变化会直接报错） |
| 财报事件缺采 / 补采上一季 | 披露季外不调度，或 worker 未运行 | `ia snapshot collect-earnings-events --part forecast\|schedule\|segments [--period 20250930] --now`；`--period` 缺省为最近一个已结束季度 |
| 逾期披露通知 | 过法定截止日仍未见该期报告 | 人工关注；该股票在筛选中被排除，补披露后自动解除 |
| 规则草稿一直「起草中」 | 设置页未配置 LLM key 或 worker 未运行 | 在设置页填写并测试连接（worker 下个任务生效）；候选页可点「重新起草」 |
| 回补中断 | 进程退出或网络错误 | 用**相同的 `--as-of`** 重跑同一命令，已完成部分自动跳过 |
| 运行卡在 running | 进程被杀 | worker 启动与运行中会定期回收超过心跳超时的任务 |

## 6. 年度 / 不定期维护

- **交易日历**：自 2026-10 起自动探测 + 一键应用。
  `calendar_probe` 在日历到期前 60 天内每周一 08:00 双源核对下一年，一致时把候选写到
  `DATA_DIR/calendar_candidates/a_share-<年>.yaml` 并发 `info` 通知（健康页与今日概览同时提醒），
  分歧时发 `action_required` 且不写任何文件。
  **生效**：在 `/app/health` 的「交易日历」卡片点「应用候选」——把候选写入
  `DATA_DIR/calendars/a_share.yaml`（数据目录副本）并删除候选；或点「检查更新」立即核对一次
  （跳过 60 天窗口，但仍要求两源全年覆盖且逐日一致）。应用只会*新增*年份：候选若缺少或改写
  已审年份会被拒绝（409）。
  **解析顺序**：所有读取方（调度任务每次触发、worker、健康页/今日概览、持仓估值与分区检测、
  研究内核的行情时效）都优先用 `DATA_DIR/calendars/<名>`，不存在时回落到仓库基线
  `config/calendars/a_share.yaml`——**无需提交、无需重启**；副本存在但损坏不会静默回落
  （读取直接失败，fail-closed）。清理副本即可回到基线。
  仓库基线仍随之更新年份（`config/calendars/` 保持只读：容器里 `config` 是只读挂载）：
  需要把某年烘进仓库时用 `python tools/refresh_trading_calendar.py --write
  --calendar config/calendars/a_share.yaml` 后提交。
  无浏览器场景用 `python tools/refresh_trading_calendar.py --write`（默认写数据目录副本，与按钮等价）。
  每年写入都记录核对来源与日期（`verified_by` / `verified_on`）。次年公告发布时用
  `--notice-url` / `--notice-title` / `--notice-published-on` 更新 `source`。
  该任务可用设置页 `schedule.snapshot.calendar_probe_cron` 停用；不加 `--write` 手动运行工具
  只打印差异（有漂移返回 1，可作巡检）。到期前 60 天健康页与调度日志会提醒，
  未知年份的交易日门控任务会停止（fail-closed）。
  备份范围含 `DATA_DIR/calendars/`（见 `backup-and-recovery.md`）。
- **申万代码表**：`python tools/refresh_shenwan_directory.py` 查看差异，确认后加 `--write` 并提交。
- **申万官网证书**：`src/investment_agent/providers/certs/swsresearch-intermediate.pem` 文件头写有刷新步骤；
  证书校验失败时申万同步会终止并提示更新证书。
- **规则模板**：修改 `config/rule_templates/*.yaml` 后运行 `ia rules validate <file>`，下次筛选自动同步为新版本；
  已生效的个股规则不会被自动改写。模板阈值变更须先回测（P3）。
- **测试 fixture 重录**：`tools/capture_baostock_fixtures.py`、`tools/capture_golden_fixtures.py`。

## 7. MCP 只读接入（D5）

`ia mcp serve` 把本机数据库以 **只读** 方式暴露给 Claude Desktop 等 MCP 客户端（stdio，不开 HTTP 端口）。
工具清单 `ia mcp tools`：`list_watchlist`、`get_stock_snapshot`、`get_bars`、`list_candidates`、
`get_candidate`、`get_rule`、`list_earnings_events`、`get_data_health`、`search_stocks`；资源 `zcharlie://health`。
返回与同名 HTTP 接口字段一致，带 `captured_at` / `provider` / `as_of`。

`claude_desktop_config.json`（macOS：`~/Library/Application Support/Claude/`）片段，路径换成本机 checkout：

```json
{
  "mcpServers": {
    "zcharlie": {
      "command": "/path/to/zcharlie/.venv/bin/ia",
      "args": ["mcp", "serve", "--project-dir", "/path/to/zcharlie"],
      "env": {
        "API_KEY": "<同 .env>",
        "DATABASE_URL": "sqlite+aiosqlite:////path/to/zcharlie/data/app.db"
      }
    }
  }
}
```

- `--project-dir` 让进程切到 checkout（MCP 客户端不设置工作目录）：`.env`、`config/app.yaml`、交易日历与相对路径的
  `DATABASE_URL` 都从这里解析；`.env` 已有 `API_KEY` / `DATABASE_URL` 时 `env` 可留空。
  Docker 部署时数据库在容器卷内，MCP 进程需在宿主机能读到同一文件（或用 `docker compose cp` 取只读副本）。
- 边界：SQLite 以 `mode=ro` URI 打开并设 `PRAGMA query_only`，没有任何写工具；不加载 LLM 客户端、不入队任务、
  不推送通知；非 SQLite 的 `DATABASE_URL` 直接拒绝启动。只读连接仍会与 api / worker 共享 WAL 的 `-shm` / `-wal` 文件，
  这是 SQLite 的正常行为，数据库文件本身不会被改动。
- 日志走 stderr（Claude Desktop 的 `mcp-server-zcharlie.log`），stdout 只有 JSON-RPC。
- 验证：重启 Claude Desktop 后，工具面板出现 9 个 `zcharlie` 工具；让助手「列出观察池」「取 600519.SH 最近 120 日收盘」。
  本机离线自检：把 `initialize` / `tools/list` 的 JSON-RPC 行通过管道送入 `ia mcp serve`。

## 8. 已知限制

- 目标池目前只有沪深 300；baostock 成分只覆盖沪深 300、上证 50、中证 500。
- 模板全部为 `uncalibrated`，回测校准在 P3。
- 推送渠道尚未接入（P2），通知只落盘。
- 手填行业恰为申万行业名时尚不映射为对应行业组（会改变 golden 分类，需要评分版本升级后再启用）。
