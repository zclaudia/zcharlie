# AF-P4-07 工程执行记录

2026-10-17，Asia/Shanghai。状态：AF-P4-07实现、交叉审查与最终工程验收完成。用户授权仅 AF-P4-07，临时 SQLite/可控 HTTP；AF-P4-08 真实采集未执行。无生产操作、生产开关、提交或推送。

依据[阶段 B 计划](stage-b-plan.md)和[工程冻结](stage-b-freeze.md)，主代理负责设计、整合、交叉审查和最终验收，三名 GPT‑6 Luna 子代理分域实施，并互审其他子域。A 历史验收不计入 B 数字；最终结果、日志和源码快照独立归档。

## 任务与实际落点

路径相对仓库根目录；实现裁决以冻结记录和代码为准。

| 子任务 | 交付与验收重点 |
| --- | --- |
| 07.0 契约 | `supplemental_rules/disclosure_contracts.py`、`evidence/disclosure_pages.py`、V3 coverage DTO；首源仅 CNInfo/SZSE，生产协议默认未验；旧 DTO/hash 保持；[冻结](stage-b-freeze.md)明确各事务、身份与恢复边界 |
| 07.1 授权/额度 | `disclosure_authorization.py`，认证 API 创建/修订/撤销后台 grant；owner-source policy CAS、grant/day/poll/request/bytes/storage；逐 hop 不可变许可与结算，绑定不可变权威投影，unknown 不退款 |
| 07.2 page/HTTP | `disclosure_pages.py`、`evidence/disclosure_http.py`；单 hop 安全 HTTPS、独立 redirect 许可、目录与索引原响应归档、稳定类型/排序/两遍整窗复核；未知/缺失/漂移/容量不足明确 partial |
| 07.3 candidate | `disclosure_candidate_ingest.py`；原生文档映射、独立 provenance、同号正文新修订与 previous、旧 hash 回现隔离；blob/Original/Evidence/candidate/event generation/P4 journal/checkpoint/leaf 同事务。接收不立即创建 P2 评估 run，A 随后沿原授权受理，来源 grant 不授研究费用 |
| 07.4 durable poll | `disclosure_updates.py`、`disclosure_schedule.py`、`update_jobs.py`、`research/worker.py`；lease fencing、已知响应重放、未知保守结算、孤立 hop 恢复、显式 pending 替换、recent/backfill 轮转与容量拆窗；A+B 共用 128 个 queued/running 槽位 |
| 07.5 coverage/读面 | `contracts.py`、`coverage.py`、`publication_limits.py`、仓储 loader、`disclosure_views.py`、API/operations/JS；V3 无环精确证明、三水位/累计三水位、前代 final 精确绑定、历史 cutoff 与当前健康同步守卫；054 独立 partial index 和降级守卫，不新增表 |
| 07.6 整合 | 真实临时 SQLite/WAL、HTTP、ResearchWorker、故障注入、1440/390 浏览器及三完整测试分区；最终唯一结果见[工程验收](stage-b-acceptance.md) |
| 08 | 未执行。真实协议/使用条件、真实原件与修订关系及独立人工来源签核保持未验 |

## 关键整合与失败迭代

1. 真实 worker 发现 UUID wire/对象比较不一致；规范化持久输入和精确引用，测试经实际认领执行。
2. quota 自 hash 不足以证明消耗权威，改为不可变 typed projection receipt + 完整 payload/revision CAS；终态不能重复结算或退款。
3. 已发生 HTTP 的安全响应与结算先独立提交，再对 candidate 使用 savepoint；业务失败不丢用量，已知原响应可失租后零新 HTTP 重放。reserve 后未 sent 的新 lease 仍绑定原 reservation lease。
4. 封口从逐页相邻重查改为全窗口第一遍完成后再走完整第二遍，防跨页漂移；新 poll 重置 checkpoint，resume 保持原 checkpoint。
5. 手动替换失败 poll 必须绑定旧 poll/原因/不可变 checkpoint snapshot；待处理集合、旧消耗和 logical attempt 计数保留，窄窗口不能清旧缺口。正常健康轮次不当作失败重试。
6. 调度维护改为有租约的 durable quantum，5 秒合并唤醒；避免无进度时高速制造 maintenance run。长 Retry-After 不提前重试，错过的定时槽位合并，历史目标在 root 固定。
7. 覆盖续查必须引用同 basis 的旧不可变 final，以该 final 而非可变状态推导累计水位；loader 直接核一代，不递归全历史。fixture 默认不能给生产 prepare/read/publish 正向证明。
8. 最终互审发现孤立 hop 已取消/unknown 后 checkpoint 仍留旧 attempt、重定向每 hop 错与最终 PDF hash 比较；两处均作为阻断问题修复并增加实际恢复/候选回归，签收以前必须通过。

首次完整 unit 回归的 import graph 与材料迁移 fixture head 失败已修复。最后的V3空水位补洞分支修正后，完整integration主动中止并重启；中止运行不计通过。该修正由真实worker专项及重启后的完整integration复测，unit/根目录既有契约不走该分支。进度文案修正仅在JS，B浏览器另行重跑；最终记录只列各验证最后一次完整结果，不累加迭代。专项与完整分区重叠，不累加。原 P0—A 浏览器 probes 在运行后按开始时备份恢复，仅新 B 截图归档。

本轮保留移动端及并行银行行业改动；不将这些变更归因于 AF-P4-07。真实来源、暴露、研究质量/费用、交易规则、正式外发及生产启用仍分别按[独立门禁](gates.md)守门。

## 最终收口

unit2563通过、roots717通过、五浏览器通过，Ruff/mypy448/JS/054单head/保全检查通过。完整integration906通过，3处历史head断言失败；测试期望修为054后，迁移050—054全组37项通过。无业务/迁移代码的后续修订，唯一集成覆盖909项，完整原始失败记录与复测分开保留，不伪称一次全绿。具体日志、警告、源码43项和并行16项hash见[最终验收](stage-b-acceptance.md)及[快照](stage-b-source-snapshot.json)。
