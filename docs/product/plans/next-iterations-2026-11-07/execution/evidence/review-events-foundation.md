# EV-01/EV-02 独立基础审查

日期：2026-11-07。范围仅为事件合同、canonical、包初始化、三表模型及其两个测试文件；不是 EV-11/EV-12 完整交付验收。审查者未编写本批实现，未修改受审业务文件。

结论：当前范围内未发现阻断问题，可进入后续来源 resolver、事务 API 与迁移集成。以下判断以冻结 BASE-02 合同和本地合成样本为准。

| 核查项 | 证据与结论 |
|---|---|
| 13 种事件 | `research_events/contracts.py` 的 EVENT_CONTRACTS 逐种绑定 source kind 与闭合 payload；冻结的全部 13 个有效样本完成解析、canonical 回写、hash 与 event_id 一致性核对。未知 type、额外字段、错误 payload/ref namespace 均被拒绝。 |
| 引用边界 | legacy source id 采用 strict 正整数，UUID 来源独立分支；revision 与 source_revision 相等，既有不可变来源固定 1。QualityArtifactRef 不进入事件 ExactRef，解析支持不等于来源发布授权。关键结果镜像字段及更正 replacement/source 对应关系有独立拒绝用例。 |
| 默认值与 hash | ObjectRef 的缺省 field_path 与 null 同义；仅约定的 backfill/manifest 默认统一。声明的集合数组排序去重，version_refs 保持原顺序。Decimal 与时间按类型归一，普通字符串不猜型，浮点及非有限数拒绝，超大指数在展开前拒绝。 |
| 不可变事件 | 模型同时提供 ORM 钩子及 SQLite update/delete/insert 冲突触发器；测试覆盖 bulk mutation 与 OR REPLACE 按 sequence/event_id/source 唯一键替换，原行保持不变。迁移尚须复用同组 DDL，不能只依赖 create_all。 |
| receipt 与 cursor | consumer/event 复合主键，状态、非负计数及 manifest/hash/cursor 成组存在约束明确；generation/rebuild 仅 research_inbox.v1 允许使用。真实临时 SQLite 验证回滚、唯一性、可变投递状态及 generation CAS。 |

后续任务仍须补齐：EV-04/05 校验真实 source owner/subject/精确回读，EV-06 校验 receipt manifest 的内容 hash、进度只前进、cursor CAS 与投影同事务；SQLite 未启用外键时的孤儿拒绝仍由服务负责。当前 ORM 约束不应被解释为这些服务责任已完成。EV-03 必须验证迁移实际创建三组事件保护触发器并保留已有 simulation 迁移。

验证命令：

```sh
NEXT_ITERATIONS_OFFLINE=1 PYTHONPATH=tests/support .venv/bin/python -m pytest -p next_iterations_offline tests/next_iterations/test_event_contracts.py tests/next_iterations/test_event_models.py -m 'not network' -q
```

结果：117 passed。仅纯合同与临时文件型 SQLite；未调用 provider、真实账户或生产数据库，未进行真实浏览器/全栈 E2E、迁移验收或持续运行验证。
