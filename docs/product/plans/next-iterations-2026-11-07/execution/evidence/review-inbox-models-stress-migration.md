# IT-002模型与064迁移独立审查

2026-11-07，Sol非作者只读审查。基线HEAD=1f62fa4及下列文件内容；未修改作者代码、迁移或测试。未发现本次范围内阻断/一般修复项。

- inbox三业务模型：listing/account受限归属、case/episode配对、revision/content_revision/read_content_revision分离及有界关系正确；journal含generation/event/ordinal、owner/request唯一键及sequence正值，完整entry_view/回执成对；三条immutable trigger同时覆盖ORM、bulk SQL、声明身份和rowid替换。nullable scope/bootstrap journal可追加，不误复制用户request身份；entry/source为可变投影，不冒充历史。
- ReviewCycle新增真实非空INTEGER cycle_event_revision，Python/server默认0且>=0；不从可变payload推版本。实际owner/引用归属仍由服务验证；SQLite FK未强制，不宣称模型已提供领域授权。模型尚未在本轮接入inbox迁移，IT-003待EV依赖完成。
- 064为冻结SQLite SQL，真实前驱063，未导入运行时模型。压力历史/operation两表WITHOUT ROWID和六条immutable trigger与模型等价；旧bank行保留；空库downgrade/reupgrade通过；有operation历史时拒绝降级。

独立执行的两个pytest进程均显式启用BASE-03早期guard，避免将尚未迁移的inbox模型混入EV/其他迁移的metadata检查：

| 命令 | 退出码/结果 |
|---|---|
| `NEXT_ITERATIONS_OFFLINE=1 PYTHONPATH=tests/support .venv/bin/python -m pytest -p next_iterations_offline tests/inbox/test_models.py -m 'not network' -q` | 0；41 passed，2.02s |
| `NEXT_ITERATIONS_OFFLINE=1 PYTHONPATH=tests/support .venv/bin/python -m pytest -p next_iterations_offline tests/portfolio_stress/test_stress_migrations.py -m 'not network' -q` | 0；4 passed，2.95s |

inbox测试出现1条默认datetime adapter弃用warning，测试未失败。未真实E2E、应用/生产服务启动、生产迁移或账户数据操作。已知simulation多个文件持续由外部并行工作修改；本任务未写这些文件，不能将“整个simulation工作区哈希不变”作为已满足事实。061保留情况由主代理对应迁移基线证据核对。

| 审查输入 | SHA256 |
|---|---|
| `src/investment_agent/storage/models/research_inbox.py` | `2ed5ec456609269346c1ea84393731ffb2f7370fb3764533a0a3ad2329822b06` |
| `src/investment_agent/storage/models/monitoring.py` | `7d518feb8e864837a2867b2f02a85ba7541cf60c23a2787ab2b9f0a931af3ee8` |
| `tests/inbox/test_models.py` | `eeaa48f5a4852d59ddf1f4f2871e65640893953b9882de0bd5d61d331b04fe2a` |
| `alembic/versions/064_portfolio_stress.py` | `499ef4a3f6336a073c93c57535933e3847c54d65cf6914da2010e0be9ee88722` |
| `tests/portfolio_stress/test_stress_migrations.py` | `081f6f1aff0aa648ba91fd521e76d05b68424d149bbd8156a104ecf04852d6bd` |
