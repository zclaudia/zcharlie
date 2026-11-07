# 本轮执行计划审查记录

日期：2026-11-07。对象：[总执行计划](../execution-plan.md)及六个任务包。结论：计划主审完成，可作为后续派发依据；所有实现、测试、Review和验收任务仍为planned。本轮没有执行这些任务。

四方向由GPT-6.1 Sol子代理分别拆分，主代理逐份阅读任务卡及覆盖矩阵、复核共享边界；另安排Sol只读审查共享基础与跨功能集成。以下“关闭”指计划缺口已落实到任务，不表示代码问题已修复。

## 审查发现与处理

| 编号 | 发现 | 落地任务/约束 | 结论 |
|---|---|---|---|
| PR-01 | 分片恢复与generation切换没有明确持久head落点 | BASE-02、EV-02/06、IT-001/036：cursor列保存active_generation/rebuild_state，闭合DTO及同revision CAS；先结清分片再切换 | 计划关闭 |
| PR-02 | 实验创建丢响应后尚无experiment_id，无法查询原收据 | QT-001/039/042/045：quality-mutations只读查询；owner先查，关闭仍可读，404不证明无在途，不换key | 计划关闭 |
| PR-03 | 收件箱使用cycle_event_revision，但没有模型和迁移任务 | IT-002/003新增现有ReviewCycle标量列，旧行0；IT-008显式依赖迁移并CAS递增 | 计划关闭 |
| PR-04 | 静态专题/组合/inbox嵌套CSS可能源码存在、wheel缺失 | PK-01/02统一package-data/helper；QT-041、IT-049、BT-026、PT-027/054实际资产验收；XA-07全量包装 | 计划关闭 |
| PR-05 | QT消费source.invalidated，但未安排真实生产者 | SV-01至06：现有verified文档事实更正保存链实际新插入后同事务发事件；QT-046及XA-04依赖并验证真实接缝 | 计划关闭 |
| PR-06 | EvidenceRecord没有owner，不能从业务payload推断事件归属 | SV-01/02：grant→SavedEvidence/DisclosureOriginal精确owner；核listing/ref；纯parse/GET不发，候选/标题线索不发 | 计划关闭 |
| PR-07 | 新guard放子目录不能覆盖unit/integration和子进程 | BASE-03强制pytest plugin及Node bootstrap；跨目录/子进程诱饵；主计划命令显式隔离；XA-10复核 | 计划关闭 |
| PR-08 | source.invalidated schema若等XA才补，会晚于SV/QT验收 | EV-01预定义闭合payload；SV-02/03使用真实append校验；未装resolver的kind不能发布 | 计划关闭 |
| PR-09 | S2事件最终接线没有明确同session注入扩展点 | PT-042预留dispatcher/handler事件sink；PT-045注册注入；PT-048及XA-03验证结果/终态/事件事务 | 计划关闭 |
| PR-10 | 直接SQL不可变检查需要同时覆盖create_all和迁移 | BT-006明确模型DDL保护写范围；BT-007复用定义；XA-06核对模型/迁移一致性 | 计划关闭 |
| PR-11 | 原设计浏览器/真实试用验收与用户本轮要求冲突 | 总计划覆盖旧验收方式；各矩阵列离线替代和真实视觉/质量/生产未验证；不安排真实E2E | 计划关闭 |
| PR-12 | 工作区已有并行simulation与061迁移，不能按060分配 | BASE-01刷新实际基线；共享文件持锁、迁移逐次分配；保护的是本任务diff，不冻结其他合法并行开发 | 计划关闭 |

来源更正首期能力是可信文档事实`source_corrected`，没有通用原件合法撤回入口；没有可信撤回记录时只能source_unavailable并拒绝相应动作。计划没有用fixture伪装该能力，也没有顺势新增原件撤回平台。

## 静态核对范围

- 任务总数241，估算180.75人日；六包统计与总表一致。
- 每张任务卡7列：ID、类型、交付物、写集合、依赖、退出条件、人日。
- ID唯一；所有依赖存在；依赖图无环；每个任务均位于最终XA-12的前置链。
- G0验收QT-041、银行静态验收BT-026、S1验收PT-027不隐含依赖EV-12。
- 四功能完整交付以及QT来源提示均有终点，没有把条件检查、S2或事件接线留成可选尾项。
- 全文链接、JSON语法和空白检查；未来代码/测试/证据文件明确为计划产物，不要求本轮存在。
- 19次独立Review、19个回修节点和19个验收节点均显式列卡；发生新发现时可追加修复卡，不能被计划数量或预算封顶。

索引由Markdown任务卡生成。执行前必须再核实工作区、允许写范围、接口版本及测试清单；DAG无环不等于文件可同时写。代码和真实运行结果尚未验证，离线工程验收也尚未开始。
