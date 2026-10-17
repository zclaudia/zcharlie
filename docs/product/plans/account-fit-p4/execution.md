# P4 阶段 A 执行记录

日期：2026-10-17，Asia/Shanghai。用户授权 AF-P4-00—06；主代理设计、整合、交叉审查和最终验收，三个 GPT‑6 Luna 子代理分别负责存储/补偿、调度/授权、来源覆盖/发布接缝。状态：AF-P4-00—06 工程完成并通过主代理最终验收，结果见 [acceptance.md](acceptance.md)。

基线：`7df697aeeb951f9bf1b76de7b2194ca6a62c6e7a`（P3 已提交）。本轮没有实施 B/C，没有联网轮询、真实付费研究、生产数据库操作、生产开关启用、提交或推送。既有移动端 Cargo.lock、`.playwright-mcp/`、`.zcode/` 并行内容保留。

| 任务 | 实际交付与接缝 | 逐项验证 |
| --- | --- | --- |
| AF-P4-00 | [冻结](p4-freeze.md)、独立 policy fixture；三表/来源合同/身份/hash/容量/终态/兼容矩阵 | 主代理与 Luna 交叉审查；不把 P3 工程通过当真实门禁通过 |
| AF-P4-01 | 053；不可变 change receipt、source/refresh CAS 仓储；rule/event/material/evidence 同事务 hook | SQLite 不可变、唯一性、两连接并发/CAS、精确回读、损坏拒绝、索引 EXPLAIN、fresh/052/空降级/有数据降级拒绝；旧表孤立 P4 run/恢复命令/task/grant/V2 payload 同样拒绝降级；048 离线导入 drill |
| AF-P4-02 | update_targets、update_jobs、updates；2000 ID 冻结段/100 页，原 P2 认领与缺口补排；新本地受理按 listing 拆分 | 102/105/2102 主体分页、精确目标/hash、原生 P2 受理；各 scope 的 active_run 只指向自己的 P2 请求；真实 P2→outcome/hash→研究交接 |
| AF-P4-03 | 既有 worker/settings watcher 唤醒持久 maintenance；due 根收据；300 秒有界历史审计 | rule/app/check_by/event/material 绝对到期键，重复安全；105 due、105 handoff 公平翻页；迟到旧时间/UUID、分类摘要变化、旧 lease 写入拒绝、128 协调器容量游标保留；读面同步 due 不等 timer |
| AF-P4-04 | ReviewCycle + 既有 admission/grant/budget；按成员证明绑定 batch；专用服务端 pending 子集恢复 | 无授权/无价格/零预算不建收费 task/grant；同批运行/终态重放不复活；dirty 成功接续、cancel 保留；真实受控导入/P2/settle/dispatch 链区分内容变化、健康重核、candidate/account_fit；两次运行中关开关形成三代恢复且累计每 pair 仅一次，原知识窗不变；坏 source/member/hash/saved ordinal 拒绝 |
| AF-P4-05 | 正式 NotificationDelivery 容量恢复；认证 imports/preview/sources/scope/receipt/batch/resume、operations；单股与设置 UI | 原 ID/body、256 容量和权限重核，failed/unknown 终态；真实 HTTP 关闭重放/只读、GET 零 DML、401/403/404/409/422、主体隔离、游标集合绑定；1440/390 来源失败/未覆盖/关闭/无残留 |
| AF-P4-06 | V2 来源 binding 进入 prepared/semantic/published hash；V1 原字段域/hash 保留；发布/续期/current-limits 共享同步门槛 | 原 V1 档案逐项回读；真实 prepare→P2 worker→semantic→publish；来源变化拒发不闭合；cutoff 后证明/资料不进入旧任务；同事实重核只续健康，新 refs/内容/合同、future/partial/failure/stale 限制；旧评分/账户/Quality/DSL/交易硬约束与 reduce/exit 回归 |

运行机制只复用 PipelineRun、ReviewCycle、BudgetEntry、原件/资料仓储和正式 outbox，不建通用消息或新闻平台。P4 settings 默认 false，A 的 polling 标志只是关闭的预留值，没有线上 handler/后台来源授权实施。

所有工程验证使用 OS 临时 SQLite/DATA_DIR、惰性凭据、本地归档 fixture/可控 provider。网关契约指向本机相邻仓库；没有外部模型或生产渠道调用。完整分区与专项重叠，不累加。最终源码指纹和 60 个改动源码/测试/迁移文件的 SHA256 见 [source-snapshot.json](source-snapshot.json)。

主要失败迭代及修复见 [review.md](review.md)。预检查中旧 grant hash、scheduler inventory、import graph、052 head 断言、WAL 测试副本、048 工具、浏览器卡片加载时序等均按根因修复，没有更新历史 golden 或放宽包依赖基线；最终通过数字只记完整成功运行。最终冻结后仅整理文档、核对源码快照与并行文件。

保留限制：本地范围过大仍遵守 P2 2000 pair/4MiB 和其显式容量拒绝，不把拒绝认作完成；恢复最多 16 代，超限 upfront 进入人工处理。来源覆盖只代表声明的精确已保存 refs，不代表官方、历史或全网覆盖；user_report/candidate 不自动升级 verified。无授权、失败、费用/ACK 未知保留待处理/对账，不靠新 task 或新通知 ID 自动重试。
