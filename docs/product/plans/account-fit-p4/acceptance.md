# P4 阶段 A 工程验收（AF-P4-00—06）

日期：2026-10-17，Asia/Shanghai。状态：**AF-P4-00—06 阶段 A 工程验收通过（主代理整合、交叉审查与最终验收）。** 本记录只记完整成功的实测，专项与全量不累加。

## 验收基线与范围

基线提交 `7df697aeeb951f9bf1b76de7b2194ca6a62c6e7a`，P3 工程已验收并提交；不重复实施 P3。主代理负责总体设计、整合、交叉审查与最终验收，三个 GPT‑6 Luna 子代理分别完成基础存储/补偿、调度/研究授权、来源覆盖/发布接缝。裁决见 [review.md](review.md)，逐任务落点见 [execution.md](execution.md)。

最终源码冻结指纹见 [source-snapshot.json](source-snapshot.json)：1229 个 src/tests/alembic 文件，树 SHA256 `b8949477fee7b1f208fa17e34a96a579bdc9f360249d8e27e9444b7332d4a23a`，其中 60 个本轮改动源码/测试/迁移文件逐项记录 hash。冻结后只整理文档、恢复旧探针并验证内容保全。

测试全部使用临时 SQLite/DATA_DIR、惰性 API_KEY、本地源 fixture/可控 provider，不访问 `.dev` 或生产数据库。集成带 `ZCLAUDIA_GATEWAY_DIR=/Users/zhvala/SourceCode/zclaudia-gateway` 实跑本机网关契约；默认排除 network，用本地 fixture 不替代真实来源质量验收。

## 最终验证

| 分区/门禁 | 实测结果 |
| --- | --- |
| `API_KEY=test-key .venv/bin/pytest -q tests/unit` | **2491 passed / 2 skipped / 6 network 默认排除**；365.91s；两项 skip 为 Costco/HSBC 缺原始 captured 材料，未伪造替代 |
| `API_KEY=test-key ZCLAUDIA_GATEWAY_DIR=… .venv/bin/pytest -q tests/integration` | **853 passed；无失败/错误/跳过**；726.07s；357 warnings，明细保存在日志，未以 skip 代替断言 |
| `API_KEY=test-key .venv/bin/pytest -q tests/test_*.py` | **717 passed**；193.82s |
| 四个浏览器脚本串行 | **全部通过**：research_closure / supplemental_rules / account_fit / supplemental_updates，1440/390；P4 新截图 7 张，包含来源失败、未覆盖、关闭只读和已处理队列。P1/P2/P3 探针恢复原档；旧 P3 测试仅修复独立卡片加载时序等待，断言保留 |
| 最终接缝专项 | **24 passed**：P4 recovery + HTTP API + engine + migration053；涉及 source/batch cursor、mid-close 两/三代恢复与五类篡改负例、102 listing 自身 run 绑定、downgrade 孤立旧表 P4 payload；与全量重叠不累加 |
| Luna P4 真实研究桥接专项 | **42 passed**：source publication / source coverage / review dispatch / engine / P3 publication_closure / coverage_prepare；另 engine/recovery/dispatch/target/bootstrap/notification 35 项，均有重叠，不累加 |
| Ruff `src tests alembic` | **All checks passed** |
| mypy 全量 `src` | **439 文件，0 错误** |
| JavaScript 语法 | supplemental_items.js / settings.js / research_closure.mjs / supplemental_updates.mjs 通过 |
| 源码迁移图 | **053 单 head**；050/051/052 原文件逐字节对照 HEAD 未改变；仅临时库迁移验证，不代表生产已迁移 |
| 文档/工作区 | 相对引用与 `git diff --check` 通过；HEAD 保持基线；移动端 Cargo.lock 与开始时 hash 一致；既有其他并行内容未整理/回退/提交 |

日志位于本机 `/tmp/zcharlie-p4a-{unit,integration,roots,browser}-final.log`、`/tmp/zcharlie-p4a-seams-final.log`、`/tmp/zcharlie-p4a-{ruff,mypy}-final.log`。中途预检查、因最后修复而主动终止重跑的分区不算最终通过数字。SQLite 表达式索引反射警告如有，仅是 Alembic/SQLAlchemy 无法对比该类索引；独立 DDL/EXPLAIN 验证另有断言，不以跳过反射代替索引验证。

## 场景证据

| 验收边界 | 主要证据 |
| --- | --- |
| F01 持久化与兼容 | test_migration_053、test_supplemental_updates_storage、旧 048 drill；原 V1 coverage/grant literal/hash 回归 |
| F02 来源与可信性 | test_api_supplemental_updates、test_p4_update_targets：精确主体/refs/hash、拒绝伪造 verified/坏 manifest、同 manifest attempt 不续健康、不自动核验事实 |
| F03—F05 调度/故障/恢复 | test_p4_update_engine、test_p4_update_bootstrap、test_p4_update_recovery：真实 worker、105 due/handoff 页、128 cap、旧 lease、迟到审计、saved/pending 两次中断与三代恢复、原 cutoff/成功 pair 不变 |
| F06—F07 授权/费用未知 | test_p4_update_review_dispatch + 既有 budget/native recovery：真实导入→P2 success→fenced settle→exact batch/grant；无授权/价格/预算零 task；健康重核及 candidate/account_fit 不收费；dirty 成功接续、取消/终态不复活、unknown 沿旧对账 |
| F09—F10 通知/读面 | test_p4_notifications、test_api_supplemental_updates、既有 external_notifications：固定 ID/body、容量恢复/授权重核、unknown 不盲重发、GET 零 DML、关闭可读/重放、游标集合变化409 |
| F11—F12 来源同步/发布原子性 | test_p4_source_publication、test_p4_source_coverage、P3 publication/coverage/binding：cutoff 后证明不入旧 manifest，健康同事实续查与变更区分，timer 不运行仍 stale，source 拒发不闭合旧问题；评分/账户适配/硬约束及 reduce/exit 继续回归 |
| F13—F14 浏览器/保全 | 四脚本串行、1440/390、7 张新 P4 探针；真实 API+worker 状态、pending=0、来源失败/未声明/关闭与无残留；源码/旧迁移/旧探针/移动端 hash 核对 |
| F08 线上周期来源 | **未实施、未验收，留阶段 B**；不把下载原件或本地 fixture 当连续生产来源验收 |

## 独立门禁与限制

工程行为之外，真实来源可信性、公司暴露、研究质量、生产交易规则、外部通知目的地及生产运行授权均**未因此通过**；详见 [gates.md](gates.md)。P4 两开关及 supplemental/investor/background/originals/account-fit/ranking/external bootstrap 均只读核对为 false，测试切换仅作用临时库。部署 DB/runtime overlay 未核验，不进行生产启用。

本地 explicit_saved_refs 仅代表声明输入已检查；没有采集到不代表没有风险。user_report/candidate 不晋升 verified；不改变财务基础评分、Quality/DSL、硬约束或任务 cutoff 白名单。单 scope 超出 P2 2000 pair/4MiB 显式容量拒绝，不确认完成；恢复最多 16 代，超限接收前待人工；原终态收费任务与未知通知不复活。三表及旧表 P4 载荷存在时禁有损降级。

最终裁决：主代理接受三个 GPT‑6 Luna 子代理的交付与复审，并亲自核对共享授权、恢复、来源窗口、闭环、队列/通知和读面接缝；上述 AF-P4-00—06 逐项通过，**阶段 A 工程完成**。最终完整分区和源码指纹均已复核。阶段 B/C、真实来源/暴露/质量/交易规则与生产授权继续按独立门禁处理，不能将本结论解释为生产启用批准。本轮无生产操作、启用、提交或推送。
