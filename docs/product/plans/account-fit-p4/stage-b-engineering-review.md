# AF-P4-07 工程交叉复审

2026-10-17。状态：交叉复核与主代理工程验收通过。依据[工程冻结](stage-b-freeze.md)，主代理与三名GPT‑6 Luna对子域交叉检查；[阶段B原计划审查](stage-b-review.md)保留为历史，不以本记录改写其计划授权范围。

## 重点接缝

| 审查项 | 最终整合裁决 |
| --- | --- |
| 持久run与收据UUID | request_json仅JSON原语，UUID比较规范化；实际worker读取而非fixture直接改状态 |
| HTTP许可与业务原子块 | reserve/sent分别commit；安全result+可重放body先独立commit；candidate失败不回滚已经发生的HTTP用量；失租/撤权/关闭再次核对 |
| quota可信性 | CAS投影绑定不可变quota_projection收据；校验精确ref/revision/full payload，缺失或篡改拒绝。unknown不退款，新grant不清总额度 |
| 候选事实与修订 | 公开user_report不扩权；正文新hash必须新Original、精确previous及Event revision；A→B→A旧正文回现隔离。URL-only健康变化不新增经济语义代次 |
| 来源代次与知识窗口 | per-source semantic_generation与R3原子更新；epoch只随不兼容basis变化，anchor稳定；collector白名单不扩展、旧task cutoff不推进 |
| 页面封口 | 必须首次完整页链后再复核完整页链；跨页排序/重复/total/末页稳定，unknown类型不能算filtered。单页邻接重查不足以证明整个窗口稳定 |
| 证明DAG | index→document→bridge→final无环精确引用；读取核原始blob/body hash、Original/Evidence/Event所有权、revision及cutoff，缺件不能保持complete |
| 历史与当前健康 | 绑定历史final可精确读取；同事实健康续查不破坏旧manifest，新正文/新事件generation立即限制。单窗水位与从history_start起的连续水位分开 |
| 调度与容量 | scheduler只入队maintenance；有lease的worker恢复active后有界挑选due。A+B共享128容量；recent/backfill轮转、deep sweep明确授权，漏跑合并固定窗口，失败不被定时器复活 |
| 升降级 | 独立054 partial index和guard；真实B run/grant/V3旧表残留亦拒降，不改050—053，不用模糊关键字拦普通旧资料 |
| 读面与真实来源门禁 | fixture明确标记且生产prepare/read/publish正向默认拒绝；API零DML。1440/390来源卡显示索引/原件/桥接/配额/调度/未知局限 |

## 最终互审的阻断问题

以下阻断问题由主代理与三名 GPT‑6 Luna 在真实代码交叉审查中发现，均已修复并通过专项正反例；最终完整回归结果另行签收。

| 问题 | 修复与已测证据 |
| --- | --- |
| 孤立 hop 终态与 checkpoint 脱节 | maintenance cancel/unknown 后，精确匹配旧 poll/attempt/hop 并清理当前 attempt/redirect URL，保留逻辑重试计数；实际 worker 后续能继续或准确停在上限，不能以新身份绕限 |
| 重定向 candidate 比错 hash | 各中间响应核自身 raw blob/result/ref/hash/bytes/redirect-next URL，末 hop 才核最终 PDF；合法 redirect 正例与篡改拒绝负例 |
| 新事实代次与前代 final 混同 | 新事实立即增加 source semantic_generation 以限制旧观点，但合法下一轮仍能封口；用不可变受理基线/原子 leaf 增量证明代次，不能将旧 final 代次强制等于新事实后的代次，也不能从可变 projection 任意放宽 |
| 历史缺口的全 null 水位 | partial 审计 proof 可精确读取，expected-state/binding拒绝；随后从history_start完整回填能建立新连续水位并绑定，跳过缺口或pending时仍全null。真实worker两轮回归通过 |

最终唯一结果、日志和源码快照见[工程验收](stage-b-acceptance.md)；测试未通过以前不计通过。

## 主代理整合裁决

三代理分别完成授权/恢复、来源/candidate、覆盖/兼容子域，随后互审其他域；主代理直接核对事务、checkpoint、proof与generation共享边界。最终未发现剩余阻断性代码问题。实际worker在candidate前核PDF MIME/签名，helper是封闭的服务器入口；公共user_report和模型不能注入候选接收命令。

来源默认协议仍未实证；更正/补充仅候选线索，不能将其关系或影响说成已核实。工程复核通过不批准S0、真实样本、公司暴露、付费研究、交易规则、通知终点或生产启用。

完整integration额外暴露3处历史迁移测试head仍写死053。只更新050/051/052测试的当前head为054，并修正050测试名；迁移文件未修改。050—054全组37项通过，完整运行其余906项通过；最终记录分别归档，不将两次命令合并成一次全绿或重复累加用例。此遗漏已补入工程检查清单：新增head必须联查所有历史current-head断言。
