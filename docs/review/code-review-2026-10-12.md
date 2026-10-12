# 代码质量 Review 报告（2026-10-12）

- **触发方式**：定时任务（06:00 一次性），自主执行：提交工作区 → 制定 review 计划 → 7 个并行子代理分域只读 review → 汇总本报告。
- **Review 对象**：main 分支 `f147e65` 之上的全部未提交工作（investor-agent 波次：agent 运行时、portfolio 台账与估值、rules 草稿生成与区间检测、观察/持仓池、21 个迁移、API/Web、约 3.4 万行）。已按工作包提交为 `3de08bd..3ee9de3` 共 14 个提交（仅本地，未 push）。
- **方法**：每个子代理负责一个领域（agent 运行时 / portfolio 与估值 / rules 与观察池 / 存储与迁移 / API 与安全 / Web 与文档 / 测试质量），只读代码、引用实际行号作为证据；另以全量 pytest 做门禁。
- **严重级别**：P0=安全漏洞/数据损坏/资金计算错误/必崩；P1=生产路径高概率 bug 或明确逻辑错误；P2=边界条件/健壮性/性能；P3=可维护性/重复/文档漂移。

---

## 一、执行摘要

**总体评估：高质量的一波交付，可保留为主干基线；但有 8 个 P1 需要在下一波集中修复，其中"估值调度链"两处同根问题会让收盘后的估值重算在生产上实际不可用。**

- **未发现 P0**：金额计算全程 Decimal（唯一 float 路径是 rank-only 的分位数）、无注入直达面、密钥不落日志/响应、所有新 API 路由都挂了认证、XSS 基线（模板转义 + DOM API 收口）全站成立。
- **8 个 P1** 集中在三类：故障收敛链路（瞬态网络错误绕过重试、unknown 预算条目只封不放）、调度链幂等设计缺陷（估值钩子同日二触必崩、收盘后补估值实际失效）、以及三处单点功能必坏（空账户导入崩溃、CLI 手动草稿被 guard 拒、前端 422 报错显示成 `[object HTMLDetailsElement]`）。
- **19+1 个 P2**：以性能债（边界检查重复全量扫描、worker 每秒维护扫描、缺索引热路径）和时区/健壮性边角（UTC 日界、非 ASCII cookie 500、无界落盘）为主。
- **门禁**：2393 个用例，修正环境变量后仅 1 个真实失败（见第二节）；已知失败基线由 2 个收敛为 1 个（`test_migration_034` 已通过，`engine_lifecycle` 仍在且根因已定位）。

---

## 二、测试门禁结果

| 运行 | 环境 | 结果 |
| --- | --- | --- |
| 全量首跑 | `DATA_DIR` + `DATABASE_URL`（无 `API_KEY`） | 2387 passed, **6 failed**, 3 skipped |
| 6 个失败单独重跑 | 同上 | 同样 6 个，确定性失败（排除负载抖动） |
| 6 个失败补 `API_KEY` 重跑 | `DATA_DIR` + `DATABASE_URL` + `API_KEY` | **35/36 通过，仅 1 个真实失败** |

结论：

1. **5 个失败是门禁环境缺 `API_KEY` 的假失败**（`tests/test_agent_tasks.py`×3、`tests/test_method_evolution.py`×2）：这些用例直接实例化 `Settings()`，需要 shell 环境提供 `API_KEY`（conftest 只对走 fixture 的用例注入）。**本地跑全量门禁需要同时覆盖 `DATA_DIR`、`DATABASE_URL`、`API_KEY` 三个环境变量**（与既有的门禁约定一致，本次首跑漏了 `API_KEY`）。
2. **1 个真实失败（P2）**：`tests/unit/test_engine_lifecycle.py:132` `assert 2 == 1` —— 启动期 `initialize_directory`（`src/investment_agent/snapshots/directory.py:64`，属本波提交）在 lifespan 里开了一个后台 DB 会话，多占一个池化连接，破坏了"关机时引擎恰好回收 1 个连接"的既有契约。该失败在 2026-10-11 的已知基线中已存在（当时与 `test_migration_034` 并列两个已知失败），本次 review 定位到了根因；**034 那个已知失败在本次门禁中已通过，当前真实基线只剩 engine_lifecycle 这 1 个**。

---

## 三、提交清单（本地，未 push）

| 提交 | 内容 |
| --- | --- |
| `3de08bd` chore(repo) | 忽略根目录 research/ 数据产物（1.6GB，不入库） |
| `25e551a` chore(deps) | investor-agent 模型与搜索依赖 |
| `02cd986` feat(storage) | 迁移 025–045、10 个新领域模型、investor 仓储 |
| `67c6962` feat(config) | agent 模型/通知/估值调度设置 |
| `38d5a4e` fix(repo) | research 忽略规则锚定到根（修正误伤 src 内同名目录） |
| `bb9ec78` feat(agent) | agent 运行时、evidence、investor 服务、research worker |
| `65093b7` feat(portfolio) | 台账、计划、估值任务、资金用途 |
| `11153ce` feat(rules) | 草稿生成、持仓规则、区间检测、观察/持仓池、模板 v3 |
| `3ea9146` feat(api) | agent/investor/portfolio/methods/disclosures/holding-rules 路由 |
| `024c7c4` feat(web) | agent 工作台、portfolio、methods 页面与观察池 UI |
| `5f070b4` test(investor) | 单测、迁移 roundtrip 025–045、进程级崩溃恢复验收 |
| `cd5e826` docs(product) | 计划、契约、研究政策、AG20–AG30 验收与 probes 证据 |
| `3cbc7b6` feat(tools) | agent trial、估值覆盖审计、模型 spike 脚本 |
| `3ee9de3` feat(scheduler) | 每日估值与区间检测调度任务 |

---

## 四、问题清单

### P1（8 个，建议下一波优先修复）

| # | 位置 | 问题 | 修复建议 |
| --- | --- | --- | --- |
| 1 | `src/investment_agent/agent/runtime.py:276` | 一切异常被压成字符串码（`type(exc).__name__`）后 `raise AgentRunError`，executor 只认 `ModelHTTPError` 才转可重试；`retry_policy.py:65-120` 列的瞬态网络错误（TransportError/超时）永不匹配 → **一次连接抖动 = 任务永久 FAILED，需重发并重新计费** | AgentRunError 保留原始异常类型/安全码枚举，executor 将连接类失败转 `ResearchRetryableError` 交 worker 重试 |
| 2 | `src/investment_agent/agent/budget.py:54-55` | 一条 `state == "unknown"` 的预算条目即封锁当日全部任务，而全仓没有任何 unknown→settled/released 的对账出口；取消/超时恰落在模型请求在途窗口（长请求的大部分时间）即触发 → **用户点一次取消，当天所有 agent 任务被拒** | 提供受信对账入口（按 provider 后账单补记后 settle），或对"本地未收到响应"的取消改记 released；至少给运营 CLI 一个显式释放通道 |
| 3 | `src/investment_agent/portfolio/service.py:338` | 空账户整批导入：`_project` 在 records 为空时 `max()` 空序列抛 `ValueError`（`service.py:949/999`）。新建账户第一笔导入期初余额是设计内路径，现有测试都先 append 期初余额未覆盖 → **新手路径必踩 500** | records 为空跳过 before 投影，`fact_as_of`/`revision` 取哨兵值 |
| 4 | `src/investment_agent/scheduler/runner.py:382` | 估值钩子把墙钟 `knowledge_cutoff` 烘进 payload，但幂等 key 只有 `(account, revision, date)`；00:05 cron 与 15:10/16:00/18:30 ingestion 钩子同日各触发一次时 key 相同而 payload 必不同 → `IdempotencyPayloadMismatchError` 逐层逃逸，`except InvestorError` 接不住，**整个 fan-out 中止且 zone 入队被跳过；正常交易日必现** | payload 用确定值 cutoff（不含墙钟），或把 cutoff 纳入幂等 key，或按账户捕获后继续 |
| 5 | `src/investment_agent/scheduler/runner.py:250` | "收盘价入库后补估值"在 `enqueue_snapshot_payloads` 之后立即调用——调度器只入队、worker 才执行；且 key 已被 00:05 cron 占用，`enqueue` 对已存在 key 一律复用，全仓无 worker 完成回调 → **当日收盘价落库后永不重算，估值（及作为输入的区间检测）滞后一个交易日，链路实际失效** | 改为 worker 在 close_quote/market_snapshot run 成功后触发估值入队，或把价格知识水位纳入幂等 key |
| 6 | `src/investment_agent/rules/generation.py:184` | guard 对 `token is None` 一刀切拒绝，而任何进过漏斗的股票都有 generation 行（claim 永不过期）；CLI 手动草稿（`cli/commands/rules.py:235`）不带 token → **`ia rules draft` 对这类股票必然失败且报错误导（"superseded"）** | CLI 入口补 claim，或给 guard 加显式覆盖通道（`--force`） |
| 7 | `src/investment_agent/api/routes/portfolio.py:86` | 交易日历判定直接读仓库基线 `load_app_yaml(DEFAULT_APP_YAML)`，绕过 `resolved_calendar_path` 的"DATA_DIR 应用副本优先"解析（scheduler/dashboard 均走后者）→ **健康页应用新日历后，估值时效判定仍用旧基线，结论可能错误** | 改用与 scheduler 同源的 calendar 解析 |
| 8 | `src/investment_agent/web/static/portfolio.js:928` | 422 校验拒绝时把 `errorText()` 返回的 DOM 节点传给只接受字符串的 renderRegion error 分支 → **用户看到 `[object HTMLDetailsElement]` 而非"卖出被拒绝"等具体原因** | 传字符串（`error.message`），或让 renderRegion 支持 Node |

### P2（20 个）

**Agent 运行时与 worker**

1. `src/investment_agent/agent/tools.py:142-148` — 每次工具调用执行 3-5 次全量 `check_boundary`，每次重跑 fence/grant/memory 全表扫描/live_policy 等（约数十条 SQL），成本随任务时长线性放大。建议单次调用合并为一次边界检查并在同事务复用，mandate 按 attempt 版本化缓存。
2. `src/investment_agent/research/worker.py:340-350` — 默认 1s 轮询每次都在 claim 事务前全量跑维护（expire_due 拉全部 waiting 行、resume_collectors 逐依赖查询、Monitoring/Discovery maintain），空转时每秒一组扫描事务，SQLite 写锁竞争源。建议加最小执行间隔或持久化游标。
3. `tests/unit/test_engine_lifecycle.py:132` — 启动期 `initialize_directory` 多占一个池化连接，违反引擎生命周期契约（2026-10-11 起即失败，本次定位根因，详见第二节；当前唯一真实基线失败）。

**Portfolio 与估值**

4. `src/investment_agent/portfolio/valuation.py:477` — `latest_at` 只遍历最近 200 条历史再内存过滤，活跃账户一天即可产生几十条估值，超窗后静默返回 None。建议 SQL 里直接按 `knowledge_cutoff <= cutoff` 取一。
5. `src/investment_agent/portfolio/service.py:445` — 幂等重放的审计行集合只用本批事件重放，与首次提交的全史投影可能对同一行给出不同 `audit_prior_history` 判定。

**Rules 与观察池**

6. `src/investment_agent/candidates/lifecycle.py:413-418` — 旧 run 的失败回调可把候选从 `pending`（新请求在途）改写为 `failed`；watchlist 侧（`watchlist/drafts.py:648-656`）只在 current 时才改。两处语义不一致，候选页可能持续显示失败。
7. `src/investment_agent/rules/recommend.py:64` — 建议日限额按 UTC 日界滚动，与北京时间差 8 小时，跨 08:00 的窗口可超 `daily_limit` 意图。改用 `shanghai_today()`。
8. `src/investment_agent/rules/zone_detection.py:991-1001` — `zone_summaries` 无 LIMIT 取全部 entered 事件只为取每 listing 最新一条，观察池列表页每 item 调一次，随历史线性增长。建议按 listing 分组取最新。

**API 与安全**

9. `src/investment_agent/web/session.py:88` — `hmac.compare_digest` 收到非 ASCII cookie（原始 HTTP 客户端可伪造）抛 TypeError → 未处理 500，且每请求复现。比较前双方 `.encode()`。
10. `src/investment_agent/settings/loader.py:249-255` — `runtime_data_dir()` 只读 `os.environ`，纯 `.env` 部署时应用后的日历解析到与真实 DATA_DIR 分叉的目录。
11. `src/investment_agent/api/routes/stocks.py:141-150` — report-year 每次未命中都写新的 `uuid4` 工件文件，无清理，磁盘无界增长；且 GET 带最多 12s 的上游串行调用副作用。改确定性命名覆盖写。

**存储**

12. `src/investment_agent/storage/models/investor.py:50,58`（+ `alembic/versions/025_investor_foundations.py:46-60`）— episode/plan revision 表的 `case_id` 外键无索引，而 case 详情页热路径按它全表查，append-only 表随历史线性变慢。补 `ix_*_case_id`。

**Web**

13. `src/investment_agent/web/static/portfolio.js:602` — 近 24h 变动快速路径只重渲持仓表、绕过 `renderValuation`，行内规则列永远停在"加载中…"。
14. `src/investment_agent/web/static/watchlist.js:155-168,218-229` — 启用/停用与移除用裸 `fetch` 无 `.catch`，断网时 unhandled rejection、无任何提示（同文件其余操作走 api.js 有超时与错误包装）。
15. `src/investment_agent/web/static/agent_views.js:139-142`（另 `agent_controls.js:140`、`agent_portfolio.js:126,239`、`methods.js:104`、`agent.js:412`）— Pydantic UTC ISO 串原样渲染，违反"时间一律北京时间"约定，同一事实跨页时区不一致。

**测试**

16. `tests/test_valuation_calculations.py:259-262` — percentile 断言与生产实现同源计算（tautological），分位算法级回归抓不到；buy/drop 区判定依赖此数。补手算硬编码 oracle。
17. `tests/test_agent_research.py:476-479` — 50ms 余量的真实时钟过期测试，重负载 CI 假阳。改注入时钟（仓库已有惯例 `test_investor_controls.py:292`）。
18. `tests/test_investor_controls.py:195-199` — 同上，1.05s 真实时钟。
19. `tests/integration/test_migration_044.py` 缺失 — 025–045 中唯一没有专属测试的迁移（仅被 045 顺带覆盖），044 downgrade 数据保留未独立验证。
20. `tests/integration/test_agent_process_acceptance.py:98-99` — 裸 SQL `update app_setting ...` 若行不存在则静默 no-op，后续 409 断言失败时原因被掩盖。断言 `rowcount == 1`。

### P3（47 个，紧凑列出）

**Agent 运行时（8）**

- `src/investment_agent/agent/executor.py:349` — 用错误字符串子串推断 stop_reason，异常类型一变即误分类；改结构化映射表。
- `src/investment_agent/agent/research.py:686-687` — 直接写 `row.checkpoint` 绕过 CAS 通道 `_save`，fencing 纪律缺口；另 677/680 行重复赋值。
- `src/investment_agent/agent/progress.py:58-64` — 事件查询无 ORDER BY，依赖 SQLite 插入顺序。
- `src/investment_agent/agent/executor.py:230` — `agent_retries == 0` 时 output_check 被整体静默跳过而非"只校验不重试"。
- `src/investment_agent/agent/runtime.py:167` — 注入模型在 request()/request_stream() 的历史投影策略不一致。
- `src/investment_agent/research/worker.py:474,488` — 原始异常文本进持久化 error_summary 并流入 watchlist 展示，与 agent 侧脱敏纪律不一致。
- `src/investment_agent/agent/job.py:39` — handler 实例字典跨调用传 claim，隐式状态。
- `src/investment_agent/agent/research.py:187-201` — 中文关键词子串决定 research_kind（决定流程深度与预算用途），用户无感知也不能覆写。

**Portfolio 与估值（3）**

- `src/investment_agent/portfolio/service.py:954` — 快照选价用 UTC `recorded.date()`，北京时间 0-8 点落后一天（当前被其他条件掩盖）。
- `src/investment_agent/valuation/service.py:383-385` — percentile 路径金额转 float，与"金额全 Decimal"约定不一致（rank-only，影响有限）。
- `src/investment_agent/portfolio/valuation_jobs.py:264` — 失败日志把 account_id 记成 run_id 字段名。

**Rules 与观察池（10）**

- `src/investment_agent/rules/zone_detection.py:848-852` — 清仓后状态行不再更新，`inside` 永久 True、不产生 left 事件，页面停留在旧区间。
- `src/investment_agent/rules/zone_detection.py:871-875` — 事件 `as_of` 用估值日但因子用运行日，点时纪律不一致。
- `src/investment_agent/rules/zone_detection.py:688-744` — 通知写入吞异常但 flush 失败会让外层 run 整体失败；用 `begin_nested`。
- `src/investment_agent/rules/zone_jobs.py:110-115,130-133` — 日历未覆盖年份时 gate 放行，节假日产生噪音事件。
- `src/investment_agent/rules/generation.py:101-105` — 槽位首条并发 claim 撞主键（自愈但报错）；捕获 IntegrityError 重读。
- `src/investment_agent/rules/drafting.py:658-659` — rule_head 不匹配时不写 stale_notes，审计粒度退化为通用文案。
- `src/investment_agent/watchlist/drafts.py:716` — `status = "approved"` 不在枚举 `WATCH_DRAFT_REQUEST_STATUSES` 内，契约漂移。
- `src/investment_agent/watchlist/drafts.py:337-348` — 全量取 RULE_DRAFT run 再 Python 过滤，应下推 SQL（`holding.py:122-135` 已是正确写法）。
- `src/investment_agent/rules/zone_detection.py:138-149` — 实例自带 zones 不查区间名白名单，遗留/手改行可生成未知区间事件。
- `src/investment_agent/rules/drafting.py:645`（另 `watchlist/drafts.py:116,559`、`zone_detection.py:517-525`、`generation.py:63-70`）— no-op UPDATE 预留锁因 `onupdate` 刷新 `updated_at`，污染审计时间戳。

**存储与迁移（5）**

- `alembic/versions/030_agent_conversations.py:26` — 回填时间戳格式与 ORM 存储格式不一致（读得回，但字符串比较语义差一天）。
- `alembic/versions/030_agent_conversations.py:15-29`、`034_bounded_discovery.py:12-17` — pysqlite DDL 自动提交后回填失败会卡死重跑；041 已有正确先例（先校验后 DDL）。
- `alembic/env.py:47-48` — `DATABASE_URL` 未转义 `%`，密码含 `%` 时迁移直接不可用；`.replace("%", "%%")`。
- 全部 025–045 外键无 `ondelete` 且 SQLite 未开 `PRAGMA foreign_keys` — 约束实际不生效（当前删除路径不触及父表，无实害），未来加保留期清理时会静默孤儿化；开启 PRAGMA 或在 docstring 明示。
- `alembic/versions/031_investor_publication.py:36` 等 — outbox.state / agent_task.status 无索引，调度 tick 全表扫（单用户规模可接受，数据量上涨前补）。

**API 与配置（8）**

- `src/investment_agent/api/routes/agent.py:216-223` — CancelInput 必填 idempotency_key 但 handler 完全忽略 body，契约矛盾。
- `src/investment_agent/api/routes/disclosures.py:65` — 路由直接调私有 `service._replay`。
- `src/investment_agent/api/routes/methods.py:100-105` — offline_shadow 回调 `next(...)`/`json.loads` 无防护，存档形状不符即 500。
- `src/investment_agent/api/schemas/holding_rules.py:60` — params 缺有限性校验（admin.py/candidates.py 均有），靠下游兜底，防御纵深缺口。
- `src/investment_agent/api/routes/watchlist.py:481-482` — DELETE 带 JSON body，部分代理丢弃 body 导致 422；改 POST /remove。
- `src/investment_agent/api/routes/agent.py:268-272` — SSE 已 200 后抛 HTTPException 只能粗暴断流；改为 yield error 事件。
- `src/investment_agent/config.py:206-210` — `web_cookie_secure` 默认 False 且 prod 不强制（API key 有强校验，cookie 没有）。
- `src/investment_agent/api/routes/investor.py:97-101` 等 — 请求体文本无长度上限且无全局 body 限制，反代暴露时内存放大点。

**Web 与文档（6）**

- `README.md:18` — 声称迁移 `020`，实际 head 已是 `045`。
- `src/investment_agent/web/templates/candidates.html:15-16` — placeholder 承诺"代码/名称/拼音"，但 pattern 与 normalizeTicker 只接受 6 位数字，直接输名称会被浏览器通用文案拦截。
- `src/investment_agent/web/static/methods.js:96,104` — async onclick 无 try/catch，`api()` 假定响应必为 JSON 且无超时。
- `src/investment_agent/web/static/methods.js:117` — `evaluations.at(-1)` 未判空，空 evaluations 点按钮抛 TypeError。
- `src/investment_agent/web/static/agent_portfolio.js:109` — append 后立即被 `replaceChildren()` 清空，死代码。
- `docs/product/ag30-method-evolution.md:11` — 文档称 2 MiB，代码是 2,000,000（约 1.91 MiB），口径不一致。

**测试（7）**

- `tests/test_agent_research.py:204` — 三元表达式与 assert 纠缠，极易误读；拆成显式 if/else。
- `tests/test_agent_tasks.py:175`、`tests/test_agent_research.py:699-700` — 直调 `service._row/_save` 私有方法构造前置状态，重构即碎。
- `tests/test_disclosure_native.py:21-31` 等 4 处 — 跨测试模块 import fixture，单点重命名连锁破坏；沉淀到 `tests/support/`。
- `tests/browser/*.mjs`（11 个脚本）— 浏览器回归在 pytest 体系外，复用命令硬编码本机绝对路径，换机器即失传；提供 run.sh。
- `tests/integration/test_agent_process_acceptance.py:51` — SIGKILL 后 `sleep(0.15)` 等锁释放；改轮询。
- `tests/integration/test_migration_041.py:75-76` — 结尾 upgrade 到中间版本无断言，意图不明。
- 覆盖缺口 — `agent/builtin_tools.py` 参数白名单拒绝分支无直接参数化测试；`rules/holding.py`（467 行）仅经 HTTP 间接覆盖，失败定位粗。

---

## 五、项目优点（按领域）

- **Agent 运行时**：durable 执行纪律罕见地完整——写侧统一 CAS revision、意图指纹幂等、lease 三层 fencing、fail-closed 边界检查、prompt 注入面（历史投影标注、FORBIDDEN 参数、approval 拒收、cutoff 不可前移）处理周到；凭据 `SecretStr(exclude=True)`、审计层刻意剥离 provider 文本。
- **Portfolio 台账**：bitemporal 重放、remaining weighted cost、未知量显式化（missing_price/unknown_fee）而非猜测、负库存/负现金整批拒绝；估值 `(snapshot, date, input_digest)` 不可变 + revision CAS 移指针的竞态语义写得非常清楚；FCFE 求解器 40 位精度二分，无一处 float。
- **Rules 与区间检测**：区间事件七元组唯一约束 + 输入摘要的幂等身份、过期任务 store 前锁内复检、因子缺失以 `quality="missing"` 上报而非补 0；三值逻辑求值与筛选引擎复用同一 `_evaluate`。
- **存储与迁移**：21 个迁移经 `alembic history` 验证线性单 head，downgrade 删序正确；模型与迁移逐列一致；不可变性下沉到数据库层（before_update 拒改删、唯一约束承载幂等）；030 为 SQLite 补 `sqlite_autoincrement` 防 event_id 复用这类细节到位。
- **API 与安全**：新路由认证零遗漏；CSRF（Origin/Referer 优先 + SameSite=Strict + HttpOnly）方案健全；`disclosure_http.py` 的 SSRF 防御（purpose 白名单、每跳重校验、DNS 钉住、字节/时限双上限）非常扎实；密钥处理（Fernet + last-4 hint、永不回显明文）干净。
- **Web 前端**：全站 XSS 基线成立（无 innerHTML/|safe）；SSE→快照→轮询降级、丢回执恢复、generation/epoch 竞态防护、IME 处理等工程化程度远超同类手写 JS；"浏览器不算钱"的产品红线贯彻到位。
- **测试**：2402 用例全部离线，断言普遍是精确值 + 负向断言双保险；真子进程 SIGKILL 崩溃恢复测试、迁移 roundtrip 真验证 schema（compare_metadata + PRAGMA notnull）、幂等/竞态/失败路径成对覆盖——绝大多数项目不写这类测试。

---

## 六、修复优先级建议

**第一批（上线/日常使用前，预计 1-2 天）**

1. P1#4 + P1#5（同根）：估值调度的幂等 key 与触发时机一起改——payload 用确定 cutoff 或把 cutoff/价格水位纳入 key，并把"收盘后重算"改为 worker 完成回调触发。这是当前生产路径上必然发生且静默影响信号时效的问题。
2. P1#1 + P1#2（同根）：agent 故障收敛——瞬态错误保留原始类型走重试；给 unknown 预算条目开受信对账/释放通道。
3. P1#3（空账户导入崩溃）与 P1#6（CLI 手动草稿被拒）：两个用户会直接踩到的必坏路径，修复面都很小。
4. P2#3（engine_lifecycle，当前唯一真实基线失败）：`initialize_directory` 的连接使用方式对齐引擎生命周期契约（复用 lifespan 已有引擎/会话或收敛为单连接）。

**第二批（一周内）**

5. P1#7（日历解析同源）、P1#8（前端 422 报错通道）、P2#9（cookie TypeError 500）、P2#10（.env 日历分叉）、P2#12（case_id 索引）。
6. 性能债：P2#1（边界检查合并）、P2#2（worker 维护节流）、P2#8（zone_summaries 分组取最新）——三者都是随使用时长线性恶化的形态，宜早不宜迟。

**第三批（随版本演进）**

7. 时区纪律收尾（P2#7、P2#15、P3 portfolio UTC 日期）、观察池/superseded 状态语义统一（P2#6）、测试时钟注入三处（P2#17/18）、补 044 迁移专属测试、README 迁移号等文档漂移。
8. 结构性改进（可排期）：异常→stop_reason 的结构化映射、`tests/support/` 沉淀共享 fixture、浏览器脚本 runner 化、外键 PRAGMA 策略决策（开启或明示应用层保证）。

---

## 附：方法与可复现性

- 门禁命令：`DATA_DIR=<tmp> DATABASE_URL=sqlite+aiosqlite:///<tmp>/gate.db API_KEY=<.env 中的值> .venv/bin/pytest -q`（2393 collected / 2387+ passed / 1 failed / 3 skipped / 6 deselected）。
- 7 个子代理的原始发现已按上方清单完整收录，无删减合并（仅去重与按域归组）；各发现的行号以提交 `3ee9de3` 的代码为准。
- 本报告按计划**不随代码提交**，留待审阅后自行处置。
