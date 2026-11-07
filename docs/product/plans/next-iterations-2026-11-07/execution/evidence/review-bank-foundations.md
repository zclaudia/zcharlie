# 银行专题基础批审查

范围：BT001–BT013。主要作者 implement_bank_fixture；root 审查合同、不可变存储、精确引用与原件/计算链，并复跑显式allowlist。历史模型/063迁移此前亦经 implement_shared_contracts 交叉审查；共享文件由root集成，不以root自审替代该交叉检查。

已核对：TopicFactRowRef内部绑定与外部topic_revision/facts.ID分离，owner/case/listing关系；当前head控制与历史revision独立；mutation当前授权先于幂等回放、CAS和receipt同事务；WITHOUT ROWID及SQL触发器防替换；候选与冲突不升为verified；日期粒度的公开时间取保守上界；实际PDF hash/原页/印刷页/行列/单位/脚注；集团/CAS/人民币/完整年度；净息差百分比只转换一次、利息支出贡献相减；缺数不补零、前期零与负值增长率明确未知；旧peer/evidence仍走原验证规则。

| 编号 | 严重度 | 触发与影响 | 关闭证据 |
|---|---|---|---|
| B1-01 | P1 | 解析条件历史时未充分核对对应ConditionHead，可能允许孤儿/错误case引用 | 原作者补owner/case/head revision完整性；6项针对反例闭合，暂停/取消状态的合法历史仍可读 |
| B1-02 | P1 | UUID历史表保留隐藏rowid时，INSERT OR REPLACE可绕过只检查UUID的保护 | 历史表使用WITHOUT ROWID，模型/063一致；独立迁移/模型替换检查通过 |
| B1-03 | P2 | Decimal局部上下文仅设prec会继承调用者ROUND_DOWN及Inexact trap，影响计算或抛异常 | 原作者改显式Context(prec=40, rounding=ROUND_HALF_EVEN)，显示格式同样隔离；prec2+ROUND_DOWN+Inexact=True反例通过 |

本批无剩余阻断。原件profile刻意限定已审核的招行FY2025；旧peer桥接只覆盖本次审核过的同原件NIM，其他报告或重述保持pending_review。未审核原件不能借verified元数据扩大支持范围。FactObservation是内部结果，不是请求或授权凭据；后续正式服务必须经BankTopicReferenceResolver。

主代理新proof/reference四文件103 passed（12.43s）；随后整个基础allowlist13文件327 passed（17.85s）。命令：`.venv/bin/python scripts/check_next_iterations.py --scope BT-foundation --offline`。两个pytest插件重复导入提示不影响断言。模型/迁移、旧peer原规则、source owner和真实本地PDF读取均包含在上述有界检查中。

未覆盖：静态创建/导入/API/UI、未来年度选择、条件调度、自动检查、事件接入与最终方向验收，不能把本批结果表述为整个银行功能已完成。
