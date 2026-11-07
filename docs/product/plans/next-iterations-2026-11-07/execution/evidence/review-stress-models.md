# PT-003压力模型独立审查

2026-11-07，Sol非作者审查。基线HEAD=1f62fa4；只读审查portfolio_stress.py/test_stress_models.py，未修改作者代码。

结论：发现1项一般阻断问题，作者修复后已独立复核关闭；其余本卡字段/唯一键/配置与控制版本分离符合设计。

## PTM-01：隐藏rowid可绕过不可变历史保护（已关闭）

位置：原`_history_statements`的BEFORE INSERT guard与两张历史表的默认SQLite rowid存储。原guard只查(stress_id,revision)、(owner_id,result_id)或(owner_id,operation_id)，未涵盖隐藏rowid。

触发：SQLite recursive_triggers默认off时，用直接SQL `INSERT OR REPLACE`指定旧rowid=1，同时更换所有声明唯一身份。已在guard内临时文件数据库真实复现：portfolio_stress_revision的s1/r1被s2/r2替换；portfolio_study_operation的o1被o2替换。旧行被隐式删除，UPDATE/DELETE触发器均未拦住，违反历史/原收据不可变合同。原16项模型测试通过，未覆盖此入口。

修复：作者为PortfolioStressRevision/PortfolioStudyOperation设置`sqlite_with_rowid=False`；两张表复合主键适合WITHOUT ROWID，不再存在隐藏物理身份可替换路径。新增`test_hidden_rowid_cannot_replace_an_unrelated_history_identity`分别验证两表直接SQL反例。

关闭条件已满足：独立复读作者修复代码；guard临时SQLite中两个隐藏rowid反例均拒绝，原payload/ID保留；所有18项模型测试通过。修复发生在对应迁移分配前，无已发布表结构更改。

## 其他审查范围

- Head配置revision、control_revision、permission_epoch分别落列，均>=1；控制CAS只推进control/epoch，历史配置revision及预分配result_id不变，陈旧CAS rowcount=0。
- revision/operation有ORM与SQLite UPDATE/DELETE及业务唯一身份OR REPLACE保护，owner+result_id唯一且原收据按owner+operation_id唯一。
- owner/account实体列与索引完整；SQLite ForeignKey未强制，模型不声称已验证跨owner/account归属。该责任仍由PT服务在写入/读取/入队/提交时核验，模型测试刻意不冒充领域授权测试。
- 结果与运行态分离；本卡没有修改SavedCalculation或引入交易/网络行为。

## 独立执行证据

初审命令：`NEXT_ITERATIONS_OFFLINE=1 PYTHONPATH=tests/support .venv/bin/python -m pytest -p next_iterations_offline tests/portfolio_stress/test_stress_models.py -m 'not network' -q`，exit0，16 passed。

修复复核命令：同guard入口执行`tests/next_iterations/test_event_resolvers.py tests/portfolio_stress/test_stress_models.py`，exit0，52 passed，其中PT模型18项、EV来源回归34项；2.61s。未读取真实账户/数据库，未真实E2E/部署。

| 修复后审查文件 | SHA256 |
|---|---|
| `src/investment_agent/storage/models/portfolio_stress.py` | `0a6e207b3a55a9252a7368e21773b827f1763fa377ed504fb82e750ae98a3689` |
| `tests/portfolio_stress/test_stress_models.py` | `11ea681de2bc109b37cbeb455b41a40e7584d1b6010f3bc7aa3f71a95f65c360` |
