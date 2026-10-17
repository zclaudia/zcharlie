# P4 阶段 A 整合与交叉审查

日期：2026-10-17。主代理负责设计裁决、交叉审查和最终验收；三个 GPT‑6 Luna 子代理分别检查/实施存储与补偿、调度与授权、来源覆盖与发布接缝。最终完整回归通过，主代理接受交叉审查结论，逐项证据以 [acceptance.md](acceptance.md) 为准。

| 发现 | 修复及验收依据 |
| --- | --- |
| P2 lifecycle/event 已经入队，新增 journal 不应重复评估 | hook 保存原 run/hash；以授权原 manifest/checkpoint 和精确来源认领，遗漏范围独立受理；原生 P2 不增加 updates 开关依赖。engine 专项覆盖 native origin |
| 页大小被误作完整范围上限 | 冻结 2000 listing ID 段，100/page，跨段保持原 high-water；公司资料/事件和 due root 使用同一解析器。102、105、2102 主体反例；不宣称一次覆盖全市场 |
| 多主体/上下文合成请求可能超过 P2 2000 pair | 按 listing 拆本地 P2 受理并核对完整冻结清单；P2 的既有容量拒绝保持，不扩大原上限、不确认未受理成员 |
| terminal、scope 与 pair 不是同一恢复单位 | 真实 HTTP/SQLite 故障注入在首 pair 提交后关 updates，验证 saved/pending；恢复使用专用服务端 `supplemental_rule.update_recovery.v1`，绑定原 run/manifest/member、pending ordinals 和已保存 refs，重建精确子集。旧 EvaluationRequest.exact_rule_refs 只验证身份，不修改其原语义 |
| 重编号会改变 pair hash，旧时间不能冒充新受理 | 子集逐 pair 重 ordinal/hash，重算 scope/input/manifest hash，保留原 as_of/evaluated_at/generation/输入；专用 authorizer 独立重建核对。普通 live 的 accepted_at 等式保留 |
| 新协调器只有最终 run 状态受 fencing 不足 | 每个工作事务先验证 run/worker/attempt/running，writer reservation + CAS；旧 worker 在接管后不得写 journal/state；两连接 SQLite 重复收据/CAS 测试 |
| 历史随机 UUID/旧时间与可变分类无法靠最大游标补偿 | 有界 keyset/high-water 完整审计，完成后每 5 分钟再扫；迟到旧时间/UUID、分类摘要变化均可发现，hook 即时捕获。历史初始输入只做本地复核，不自动触发收费 |
| 首批手工导入易混淆来源证明与 verified fact | 只收已保存精确 refs；服务端回读 owner/subject/revision/bytes/hash/可见时间，不接受 URL 或 verified 字段。相同 manifest 新操作仅记 attempt，不 fanout/续健康；合同与 refs hash 绑定成功证明 |
| prepare 使用 now 会把任务窗口后取得的证明纳入依据 | 以 effective_cutoff 筛选历史 receipt 和精确资料可见时间；后到来源使旧任务 unknown，不进正向 summary/V2。后续同事实健康复核只更新当前健康，不推进 cutoff。真实 prepare 测试先来源持久化、再 admit |
| 后续健康成功不应无条件使旧结论失效，也不能让变更伪装续期 | 原 proof 不变；新当前 proof 仅同合同/scope/refs/content、窗口不收窄时延续健康。新 refs/内容/合同、partial/failure、未来时间与 stale 都同步限制；截止使用 `>=` |
| 发布拒绝前已写 closure 会解除旧问题 | 先收齐当前限制/评审失败，再进入闭环 savepoint；来源变化拒发时不得写正式闭合。真实 prepare→worker→semantic→publish 与旧问题 refs 验证 |
| scope_member 的 origin hash 不能代替其实际 P2 结果 hash | 成功 outcome 绑定真正 P2 run/manifest；批次按每成员 outcome 冻结证明，付费边界重新授权 P2 与 checkpoint。成员本身仍不可变 |
| 重复研究交接会把同一成员再次记 dirty，terminal 可能碰撞重建 | immutable batch 精确重放返回原 cycle/task；运行中只收新成员；成功后逐成员后继交接。无授权的未尝试 cycle 可在授权恢复后重核；已尝试失败/取消/unknown 不自动花钱 |
| 新 grant 默认 None 字段改变旧序列化/hash，exclude 又可能掩盖新旧授权变化 | serializer 仅旧 grant 无 P4 绑定时省略新增字段；完整 dump 比较仍拒绝给旧 input 附加不同授权。归档 literal/golden 不更新 |
| 内部扫描游标不能显示为声明来源，没 timer 也要显示复核边界 | sources/coverage 只统计 listing+context 的声明；operations 单列 maintenance feeds。GET 从来源健康、最新应用和当前结论推导 due，零 DML；失败/未声明不显示“无风险” |
| 旧 schema 离线工具在升级前调用资料 hook | 显式检测 journal 表后才附加 P4 收据，不吞数据库异常；原 048 导入/迁移/备份恢复 drill 真实通过 |
| 新迁移页布局暴露测试包复制未包含 SQLite WAL | 测试包生成改用 SQLite backup API，完整保留源库已提交事务；未改生产备份策略、未弱化负例/审计断言 |
| 通知容量恢复换新身份会重复或改发送内容 | 原 delivery ID/body 有界恢复，writer 锁下重核 256 容量、站内/case revision、config/授权；failed/unknown/suppressed/superseded 不复活 |
| 新定时 cron 改变默认关闭时旧 scheduler 清单，直接 import 增添包耦合 | 复用 settings watch 和 worker maintenance 唤醒，正常 durable handler 执行；经 research worker facade，import graph baseline 不放宽 |

复审保留：真实来源、公司暴露、研究质量、生产交易规则、通知目的地和生产启用均未通过本轮工程测试追认。当前迁移图 053 不代表部署库已迁移。无 B/C 适配器或线上轮询实施。


最终复审结论：共享接缝发现均按上述裁决修复并验证，未发现剩余阶段 A 工程阻塞；AF-P4-00—06 通过。后续 16 代恢复上限、P2 容量和无授权/未知费用/来源不足状态明确保留，不当完成或自动解除。完整结果为 unit 2491 passed / 2 缺原件 skipped / 6 network 排除，integration 853 passed，根目录 717 passed，四浏览器脚本 1440/390 全通过；Ruff/mypy 通过。
