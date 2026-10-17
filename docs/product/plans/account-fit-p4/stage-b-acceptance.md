# AF-P4-07 工程验收

2026-10-17，Asia/Shanghai。**状态：AF-P4-07 工程验收通过（主代理整合、交叉审查与最终签收）。** 只验AF-P4-07；AF-P4-08真实来源采集未执行。工程、来源真实性、公司暴露、研究质量、交易规则、正式外发和生产授权分别守门。

## 实际交付边界

- 首源工程适配限CNInfo/SZSE指定发行人的年度报告全文及类型可识别的更正/补充线索。目录、页响应和原件bytes保存hash/取得/可用时间；未知分类、issuer不明、原件/桥接/封口缺失均不能完整。candidate不升级verified，不判断产品/地区/订单暴露。真实更正关系未核实，`verified_correction_refs`保持空；不按相似标题自动关联或解除旧问题。
- 独立后台grant、owner-source与grant日配额、poll cap及逻辑存储预算；逐hop reserve→sent→single HTTP→result，各阶段不可变。显式owner策略CAS、跨日、并发、unknown不退款、旧grant/revision不清用量。
- persistent poll/lease fencing、全窗两遍索引复核、分文档原子接收及P4 journal、失败重试/Retry-After/重启补偿。新poll与resume分开；手动替换须保存旧pending/计数/用量及精确snapshot；定时任务不能清未决失败。
- recent/backfill有界轮转、受授权deep sweep、固定历史子窗/容量拆分、合并唤醒及A+B共享队列。单窗与累计连续三水位分开，历史缺口保持可见。
- 显式V3 poll proof、空窗口证明、历史cutoff与当前健康同步限制；V1/V2原payload/hash保留。精确原件/证据/事件与raw blob缺失或hash不符时fail closed；健康续查不增经济语义代次，新正文/新事件立即限制旧观点。
- 复用053三窄表、DisclosureBlob、原worker/ReviewCycle/预算/outbox；054只新增poll-final读取partial index和降级守卫，无新增业务表。050—053迁移原文件未改；生产库未连接/迁移。

具体实现裁决与故障纠偏见[冻结](stage-b-freeze.md)、[工程复审](stage-b-engineering-review.md)及[执行记录](stage-b-execution.md)。旧A验收数字不计为B；专项数字不与完整回归累加。

## 验证记录

最终实测如下；专项与全量重叠不累加，失败迭代保留原记录。

| 验证 | 当前记录 |
| --- | --- |
| unit全量 | 最终2563 passed，2材料缺失skip，6 network deselected，5既有Pydantic warnings，328.27s |
| 根目录全量 | 最终 717 passed，170.59s |
| integration全量 | 完整运行906 passed / 3 failed，345 warnings，867.79s；3项均为旧head=053断言。仅修测试为054后，050—054全组37 passed、60 warnings、23.96s，包含全部3项。唯一集成用例覆盖909项，不伪称一次909 passed，也不累加906+37 |
| SQLite/worker/故障/HTTP专项 | 授权、候选、恢复、覆盖、调度/API、054、共享容量专项通过；已覆盖替换继承、孤立hop、合法redirect、新事实后继final、局部窗→history_start补洞 |
| 浏览器 | 四个既有脚本串行通过；B真实loopback HTTP/worker的1440/390场景通过。最终5 HTTP、1 candidate、0 AgentTask；进度文案修正后B重跑通过 |
| Ruff/mypy/JS/迁移图/引用/差异 | Ruff全src/tests/alembic通过；mypy 448文件0错误；共享JS/A/B脚本语法、054单head、Markdown本地引用及git diff --check通过 |

B专项及浏览器使用临时SQLite/WAL、临时DATA_DIR、合成协议/字节及可控HTTP；没有真实provider、收费Mandate或外发终点。完整工作区回归也读取既有本地归档材料（包含并行银行域原件回读），不新增真实来源采集、不将这些旧材料计为P4来源样本。fixture proof只由测试专用session opt-in正向使用，认证API/模型/环境开关不能获得这个权限。默认生产协议仍unvalidated，工程fixture不是S0或真实样本证据。

## 独立门禁与运行限制

| 门禁 | 结论 |
| --- | --- |
| B-E工程 | 已通过；仅AF-P4-07工程能力，不授live/生产/费用权限 |
| S0协议/使用条件/来源限速 | 未执行，真实字段分类、tie-break、末页语义及许可须后续核验冻结；当前不配置live协议 |
| S1—5真实原件/重复/修订/更正/反例/空窗 | 全部未执行；合成样本不能替代真实或独立人工签核 |
| X/Q/T/N/P | 公司暴露、真实研究质量/费用、生产交易规则、新外发授权与生产启用均未获本轮授权 |

下一步如开展08，仍须明确源/精确listing及报告、绝对窗口、有效期、频率、owner/grant请求与字节/存储配额、独立来源评审人及S0使用条件；建议额度不等于许可。来源grant不授研究费用，已有独立A授权仍可能使新事实沿A产生合法费用；本轮隔离工程环境无这类权限。暂停/撤权/失联保留旧声明、审计及未决限制；有B/V3载荷时不得降到不认识它们的版本。

本轮未启用生产开关、未执行生产操作、未提交或推送。移动端及并行银行行业工作保留，最终源码快照单列本任务变更与观察到的并行变化。

## 验收裁决与归档

主代理接受三名GPT‑6 Luna的实现与交叉复审，并核对授权/额度、逐hop HTTP、候选原子性、租约/恢复、历史与新事实代次、读/发布同步限制及浏览器共享接缝。所有发现的阻断均已消除。完整integration暴露这三项失败后仅调整三个历史测试的current-head期望（053→054）及一个测试名，未改迁移或业务代码；因此以该完整运行的906项通过与迁移全组37项复测共同验收，不为测试文字修正再重复本地原件回读，也不隐藏原完整命令退出码1。最终集成覆盖909个唯一用例。此裁决不豁免任何失败的业务行为。

- [验证清单与精确命令](stage-b-validation/manifest.json)：[unit](stage-b-validation/unit.log)、[完整integration原始记录](stage-b-validation/integration.log)、[迁移全组复测](stage-b-validation/migrations-050-054.log)、[roots](stage-b-validation/roots.log)、[五个浏览器](stage-b-validation/browser.log)、[Ruff](stage-b-validation/ruff.log)、[mypy](stage-b-validation/mypy.log)、[保全与检查](stage-b-validation/checks.log)。
- [最终源码快照](stage-b-source-snapshot.json)：1261个src/tests/alembic文件，本任务43项源码/测试/迁移变更逐项hash；16项并行银行域源码变化单列。050—053原迁移、mobile Cargo.lock、依赖锁、collector白名单及18张历史探针与开始时一致。B独立归档5张截图。
- 两项unit skip分别为Costco/HSBC缺归档原始材料；6个network用例默认排除。Pydantic与SQLite表达式索引反射警告原样保留，索引另有DDL/EXPLAIN断言，不以反射skip替代索引验证。

AF-P4-07工程已完成；AF-P4-08/S0/S1—5及X/Q/T/N/P未因此通过。后续真实来源样本须另行明确授权，工程不自动启用生产功能。
