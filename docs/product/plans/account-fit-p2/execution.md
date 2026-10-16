# P2 执行分工与实际文件边界

日期：2026-10-16（Asia/Shanghai）。本轮依据修订后的15项计划实施；最终状态、测试证据与能力限制见[验收记录](acceptance.md)。使用三个 GPT‑6 Luna 子代理，主代理负责接缝整合、独立审查与验收。没有自动提交或推送。

| 顺序/任务 | 实际交付文件与边界 | 执行责任及依赖 |
| --- | --- | --- |
| 01 G0冻结 | p2-freeze.md、fixtures/supplemental_rules_v1、support/supplemental_rules.py | Luna foundations；先固定契约/能力/容量 |
| 02 契约配置 | supplemental_rules/contracts.py、canonical.py、commands.py；ObjectRef/config/settings窄接缝 | foundations；01后；commands为整合中抽出的纯DTO，避免服务依赖HTTP |
| 03 存储迁移 | models/repositories/supplemental_rules.py、assessment扩展、051 | foundations；02后；五表，050不改写 |
| 04 来源 | inputs.py、sources.py、event_sources.py | Luna evaluation；02/03后；只读严格cutoff，不改旧loader默认 |
| 05 匹配 | matching.py | evaluation；04后；无法证明的产品/地区关系保持unknown |
| 06 指标 | metric_adapter.py、metric_evaluation.py | evaluation；04后；原Quality和筛选不变 |
| 07 未决评估 | evaluation.py、issues.py | evaluation与主代理；05/06后；无P3评审不解除 |
| 08 应用服务 | service.py、current_limits.py、views.py | 主代理；03—07后；typed展示分组保留全部精确依据 |
| 09 接收与worker | admission.py、jobs.py、run_views.py、research/worker.py | Luna workflows；08后；收据/清单/检查点/租约原子边界 |
| 10 生命周期 | lifecycle.py、validation.py | workflows；09后；草案槽位和生效槽位分开 |
| 11 事件 | events.py | workflows；04/09后；公网仅user_report，不能自报verified |
| 12 HTTP | api/routes/supplemental_rules.py、schemas镜像、app/deps、research旁路守卫 | workflows；09—11后；原P1 run白名单不扩充 |
| 13 管理页 | supplemental_rules.js/css/html、Web路由与导航 | foundations；12后；结构化编辑、稳定命令、受控批准、run跟踪 |
| 14 单股页 | supplemental_items.js、stock.html、app.js | foundations与主代理；13/08后；只读A股面板、分页、去重及逐规则审计 |
| 15 验收交接 | unit/integration/browser、交付索引/验收/复审/合成截图 | 主代理验收，Luna交叉复核；全部实现后 |

测试按实际责任整合到contract/store/inputs/matching/evaluation/service及migration/acceptance/concurrency/source-boundaries/Web模块，而非为每个概念另建空测试文件；计划中的hashes/metrics/issues/lifecycle/events/API单独文件名是建议边界。完整业务链从HTTP、真实临时SQLite、收据/队列、ResearchWorker到精确应用/当前投影；恢复反例另调用同一生产handler以控制崩溃/失租发生点。

G0容量采用pair及4 MiB两项同时满足的上限。G1覆盖迁移往返及直接SQL。G2由真实来源冻结和应用服务验证。G3覆盖命令事务、并发及恢复。G4覆盖真实浏览器、全量离线回归及静态检查。各门禁结论以最终验收记录为准，不累加子代理和主代理重复运行的测试数。

生产迁移、生产开关、真实监管采集、P3正式发布/解除/排序接入和P4外部监听均未执行；并行mobile工作不属于本轮。

后续全面复审补充：主代理新增P2局部read_snapshot.py以保持多查询GET一致性，不改全局SQLite策略；共享assessment增加一条P2专用operation_id部分表达式索引，不增表/业务列。实际变更及新的七个review回归模块见[后续复审](followup-review-2026-10-16.md)。
