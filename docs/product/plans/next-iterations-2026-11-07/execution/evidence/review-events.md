# EV-11 共享研究事件独立审查

日期：2026-11-07。审查者未编写 EV-01–09 业务实现。本轮只写 EV-10 三个独立测试文件及本审查/验收记录；业务、迁移、运行配置保持只读。结论为 **发现的一个 P1 已由原作者回修并经独立复核关闭，无未关闭阻断项；EV-12 离线工程验收通过**。

## 范围与方法

逐层核对 `research_events/{contracts,canonical,resolvers,registry,repository,delivery,maintenance,health}.py`、`storage/models/research_events.py`、`alembic/versions/062_research_events.py`，以及 `research/worker.py`、`scheduler/runner.py`、`cli/commands/worker.py`、`docker-compose.yml` 和 `docs/operations/research-events.md` 的专用 kind 接线。遵循 BASE-03，只运行显式文件清单的 guarded offline pytest。数据库为临时文件 SQLite；竞争使用两个真实 AsyncSession；无真实服务进程、provider、模型、通知、行情或账户调用。

## 分级发现

### EV-R1 · P1 · SQLite 写锁使机械 tick 超过硬预算（已关闭）

- 位置：回修前 `maintenance.py:167–183` 的租约 UPDATE/事务退出，以及 `maintenance.py:222–242` 的 `asyncio.timeout` 包装。
- 触发：另一个数据库 session 保持写事务；tick 的首个租约 UPDATE 等待该 writer。Python 协程到达 deadline 后被取消，但 aiosqlite 工作线程及事务清理仍等待 SQLite 写锁释放，`asyncio.timeout` 不能单独限制这一等待。
- 复现：`test_event_worker.py::test_sqlite_writer_contention_does_not_outlive_tick_deadline` 设置 50ms tick 预算，另一 writer 在 500ms 后释放。原实现实际返回 **607ms**；测试给予 250ms（预算五倍）调度余量仍失败。没有回调效果或回执提交。
- 影响：机械轮次的硬预算在真实本地写锁竞争时失效；默认 2s 预算同样不能约束底层较长的 busy wait。独立进程解决研究任务等待，不解决共享 SQLite writer 竞争。
- 修复验收：保持此独立反例阈值；竞争时及时退出且当前事务无效果、回执或 cursor 提交。不得留下继续执行/提交的后台 SQL。若临时调整连接 busy_timeout，必须在同一连接恢复原值，不能把机械轮次设置泄漏给共享连接池中的普通研究 session。已有 timeout、lease、恢复、全局额度及 kind 隔离测试必须继续通过。
- 协作：已向 root 提交可复现 P1；root 已分配 EV 原作者回修，独立测试归审查者维护。
- 回修与独立关闭：每个机械 unit 及最终队列结账在其连接上暂设 `busy_timeout=5`；pool reset 在调用方 commit/rollback 后恢复原值，失效连接丢弃。只有真实 SQLite BUSY/LOCKED（含扩展错误码）映射为本轮 `budget_exhausted`，其他数据库错误继续传播。没有自行 commit/new session 或后台重试。独立原 50ms/250ms 反例通过，确认确实进入租约 UPDATE，且无效果/receipt/cursor 提交；同时借出两条池连接验证各自恢复 15000ms。原作者另补 final-success 锁竞争及非锁 SQL 错误反例，均经独立整套复跑通过。

未发现其他需要业务回修的阻断项。无法解析可信链时，阻断同 owner/consumer 的其他 listing 是约定的保守屏障，未作为缺陷；未安装 consumer 的 `not_installed` 同样为预期。

## 独立补测与既有证据

- `test_event_transactions.py`：两个真实 session 使用同一个已提交 expected cursor revision；第一 writer 提交一个效果和一张 receipt，另一 writer 得到显式 CAS 冲突；按新 cursor 重放不再调用 callback。
- `test_event_delivery.py`：51 个真实 target 效果，前 50 个先提交；新 session 的末片在写入第 51 个效果后失败，当前片效果回滚且持久 cursor 仍为 50；再开 session 只恢复末片，已提交前缀没有重复，terminal 重放没有 callback。
- 同文件另验证 unknown chain 的隔离范围：不同 listing 仍被同 owner/consumer 屏障阻断，另一 consumer 可以按序独立交付；对照既有已知链测试，独立可信链继续。
- 既有 `test_event_append.py` 已实际覆盖双 writer 同 hash 去重/异 hash 冲突、业务与事件同事务全无、savepoint 失败和外层事务边界；不重复实现。
- 既有 `test_event_receipts.py`、`test_event_delivery_unit.py` 覆盖投影/receipt/cursor 全无、manifest 固定、进度上限、退避/隔离、已知链屏障、错误日志脱敏、取消及零副作用健康读取。
- 既有 `test_event_maintenance.py` 覆盖 50/20/20 **全局**额度、callback/wakeup 真实取消、每片短事务、提交前 lease fence、丢 lease 后停止及继任者恢复已提交前缀。`test_event_runtime_registration.py` 核对专用 kind、普通 worker 排除、CLI/Compose/调度装配，没有启动实际进程。
- 模型/迁移测试覆盖唯一键、不可变 UPDATE/DELETE/OR REPLACE（含 sequence 隐式 rowid 别名）、旧配置保留、模型与迁移 schema 等价、空库降级与有历史拒绝降级。

初次完整清单运行：既有 EV 测试 **224 项通过**；新事务/交付三项行为通过。完整运行曾因独立新测试自身对 `asyncio.gather` 返回 list/类型标注 tuple 的比较差异失败，已改用 `tuple(results)`，未修改业务。SQLite 预算反例独立运行 **1 failed**，正是 EV-R1。

最终 BASE-03 guarded offline 的十二文件显式清单 **230 passed in 17.02s**，包含全部既有 EV 测试、两项原作者回修反例及四项独立补测。原 callback/wakeup 取消测试的预算由作者调整为 0.6s（仍不超过 2s），让测试确实进入 callback 后验证取消；独立 writer 的 50ms/250ms 阈值始终未改。此前 150ms wakeup 项出现过一次 callback 尚未进入就耗尽总预算的调度波动，单独复跑通过，最终整套也通过。

冻结细节及可复现命令见 [EV-12 验收](acceptance-events.md)。本记录不证明收件箱/银行/决定消费者已安装，也不证明持续运行延迟、真实部署恢复或全栈 E2E。
