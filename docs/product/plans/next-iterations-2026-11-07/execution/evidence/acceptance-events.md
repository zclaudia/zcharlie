# EV-12 共享研究事件离线工程验收

日期：2026-11-07。独立审查/验收通过，可供 IT 的明确注册与业务投影接线依赖。审查者未编写 EV-01–09 业务实现；发现的 EV-R1 P1 已由原作者回修，独立复核关闭，详见 [审查记录](review-events.md)。没有未关闭 P1/P2。

## 实际验收证据

| 任务 | 可依赖的结果 | 证据 |
| --- | --- | --- |
| EV-01 | 闭合 source/ref/schema、严格 int/UUID、类型规范化、UTC/Decimal 稳定 hash、未来领域仅可解析不自动获发布权限 | `test_event_contracts.py`，实际 `contracts.py`/`canonical.py` |
| EV-02/03 | 三表、唯一键与索引、immutable event 的更新/删除/REPLACE 防护；062 模型/迁移等价；旧数据保留；空库降级及有历史拒绝降级 | `test_event_models.py`、`test_event_migrations.py`；未修改 061 |
| EV-04/05 | 原业务与 append 同 session、无自行提交；真实双 writer 同 hash 去重/异 hash 冲突；owner/来源/精确历史版本/时间回读；未安装 source 不可发布 | `test_event_append.py`、`test_event_resolvers.py` |
| EV-06/07 | 投影/receipt/cursor 同事务；真实同版本 CAS 双 session 仅一个提交；前缀50个真实效果提交、末片写后失败回滚、新 session 从50恢复、不重做前缀；manifest 不变；退避及隔离；扫描与成功分开 | `test_event_receipts.py`、`test_event_delivery_unit.py`、独立 `test_event_transactions.py`/`test_event_delivery.py` |
| EV-07 链序 | 已知可信链失败只阻同链、独立链继续；未知可信归属保守阻断同 owner/consumer（包括另一 listing），其他 consumer 独立 | 上述 delivery 两文件；未知链屏障为明确约定 |
| EV-08 | 全局50新/20重试/20唤醒，单片最多50效果；2s以内配置的真实异步 deadline；当前片取消/丢 lease 全无，继任者复用已提交前缀；SQLite 写锁不拖延到普通15s busy wait、池连接恢复原策略 | `test_event_maintenance.py`、独立 `test_event_worker.py`；EV-R1 原50ms预算/250ms验证阈值未动 |
| EV-09 | 专用 `research_events_maintenance`；ordinary/agent/collection 不领取；混 kind 拒绝；CLI/独立30s调度/单活入队/Compose service 正确装配；无 consumer 不入空任务 | `test_event_runtime_registration.py`、维护队列测试及运行文档 |
| EV-10/11 | 独立事务/恢复/链序/锁竞争反例，P1 由原作者回修、审查者独立关闭 | 三个独立新文件、`review-events.md` |

正式依赖接口为接收调用方 session 的 `append_event`、精确 source resolver/白名单 registry、同 session receipt/cursor/CAS、有界 `run_maintenance_tick`/专用 handler 与只读 `consumer_health`。原调用方拥有事务提交权。IT 继续负责具体目标选择、journal、generation 业务就绪判定、后处理受理及其授权；共享 API 的成功不替代这些业务校验。

## 精确运行记录

以下命令实际运行 **230 passed in 17.02s**。BASE-03 plugin 在 collection 前加载，临时 SQLite、惰性假凭证、网络/transport 拒绝策略覆盖全部文件；无外连或真实账户读取。未启动全应用、worker/scheduler 进程或真实 E2E。

```sh
NEXT_ITERATIONS_OFFLINE=1 PYTHONPATH=tests/support .venv/bin/python -m pytest -p next_iterations_offline \
  tests/next_iterations/test_event_contracts.py \
  tests/next_iterations/test_event_models.py \
  tests/next_iterations/test_event_migrations.py \
  tests/next_iterations/test_event_resolvers.py \
  tests/next_iterations/test_event_append.py \
  tests/next_iterations/test_event_receipts.py \
  tests/next_iterations/test_event_delivery_unit.py \
  tests/next_iterations/test_event_maintenance.py \
  tests/next_iterations/test_event_runtime_registration.py \
  tests/next_iterations/test_event_transactions.py \
  tests/next_iterations/test_event_delivery.py \
  tests/next_iterations/test_event_worker.py -m 'not network' -q
```

Ruff 与 mypy 验证范围为 `src/investment_agent/research_events` 及三个独立新测试文件，均通过（mypy：12 source files）。

## 冻结版本

以下 SHA-256 标记本次源码验收点；后续新增 source/consumer 注册应以新任务审查，不能沿用本记录声称其业务已验收。

| 文件（`research_events/` 下，另注明除外） | SHA-256 |
| --- | --- |
| `contracts.py` | `0d9cb5eb777cb44f219ae6ed4eba8e4af94d4540d3d82b4999b18beef050e793` |
| `canonical.py` | `0e71c9d6e620ef37da44fa5d97253d5420fe785be7821ad058d8587848ee90f3` |
| `resolvers.py` | `fed6f0c0a0d29e9bde5c2e920dd2ae5fa6188affa9279cd10ef4ccff31ec8907` |
| `registry.py` | `f44bb869bbfb92667652e0afda360b33d2522eb21c7ee3cba75793334dba7ce7` |
| `repository.py` | `ae0bcc2e82693e67801d63a2ac179c0b5e49f1f9e36bedd06e5a7149c6ff8e67` |
| `delivery.py` | `aad5742f5fa7434fc3454a0353294bb4b2853ad4b3fbeb503959f375a48bb8a2` |
| `maintenance.py`（FX-01后） | `baad7a60faebb2b7e73f264bceb0922aa19153abde188cf934c9d9f735db744f` |
| `health.py` | `8cf8cc583655907d64919797c0e5a81c16f15cf03ba2f1ecc13b410321bd25bc` |
| `storage/models/research_events.py` | `3e506eaa8533bccfc8aaec22823ea8a88f19266babe2fadced5fa181ffb9f625` |
| `alembic/versions/062_research_events.py` | `733d270b9a6145f66b4ef5deea4a0b55c98d7adedf0cedf03f7fed0353b16dc4` |

运行接线审查点：`research/worker.py`=`321f65b0743a46d57cd7cc1cb31ca3cb64ccdc61cf4933b6a3194d760872ef31`；`scheduler/runner.py`=`449b6dda78c3f7e99c76dd53c255e0af754a30ae887c42947bd49fa4f5d7ca38`；`cli/commands/worker.py`=`e32e3d82f613ab33516507f2018c9565ec4f53c6357439d8881ca62807d78c31`；`docker-compose.yml`=`0629a50ca3c1f2526c198c0b1c7531d0e959ab33c4f5a7c79f5ac4487bddc65c`；运行文档=`5dc15e5d4e519c1ce0c33a726f5bf98afcec63050419d8a9fbab5b46fb85e6e8`。

运行文档已独立复读：2s 是 `run_maintenance_tick` 的协作执行预算，包含规划、回调和事务栅栏；线程清理/事件循环调度有小量开销，不承诺生产端到端延迟。普通队列 claim、heartbeat、失败结账不计入此 2s；最终成功结账使用短锁等待且错误交 worker 恢复。此边界与本次测试范围一致。

验收只覆盖共享机械基础。当前未安装的业务消费者健康状态应是 `not_installed`，不是“已交付”；新 bank/topic、portfolio/decision、review-cycle 或来源更正接缝需要其独立 producer/resolver/consumer 任务。生产连续运行延迟、真实部署恢复时间、真实 provider/账户与全栈旅程没有在本轮验证。
