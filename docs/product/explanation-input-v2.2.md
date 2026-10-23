# 解释输入字段补全（2026-10-23）

线上任务 #426（阳光电源，2026-10-22 19:31，Asia/Shanghai）在解释阶段失败：AI 引用了 `key_metrics.operating_cash_flow` 和 `key_metrics.volatility_1y`，但评分输出没有这两个路径。该任务的 `financial.json`、`market.json` 已有对应值，评分输出中的 `field_sources` 也保留了来源；问题发生在标准数据到解释输入的字段投影，并非上游缺采。

核对全部标准行情、财务指标后，原 `key_metrics` 仅暴露 9 项，另有 11 项只有来源、没有数值。除截图两项外，`free_cash_flow`、`roe_stability` 也参与策略或因子评分，解释 Agent 却无法引用原值。

此次将评分输出 schema 从 `v2.1` 升至 `v2.2`，在 `key_metrics` 补齐以下 11 项：

- 行情：`latest_price`、`ps`、`return_1y`、`volatility_1y`。
- 财务：`revenue`、`net_profit`、`gross_margin`、`net_margin`、`roe_stability`、`operating_cash_flow`、`free_cash_flow`。

现有 9 项的取值逻辑保持；所有新增值使用同一数值标准化函数，缺值为 `null`，零值和负值保留。现有 `field_sources` 与其原始时点和来源一起传递，报告上下文和持久化评分使用同一份指标。AI 引用白名单按实际输入生成，`null` 仍只能用于数据质量说明。同步补齐中文名和单位；最新价与每股金额以元展示。

评分公式、因子权重、风险等级和工作流标签算法未变，`scoring_version` 仍为 `v1.3`。历史快照不回填；跨输出 schema 比较时，仅在一侧不存在的指标新增/移除记录保留但不计为有意义变化，原因标记为 `score_schema_changed`。已有指标的真实变化，以及显式 `null` 与数值间的变化仍正常报告。

13 个冻结样本逐一离线重放。更新 golden 前，逐字段断言输出只能包含 schema 版本变更与上述 11 项新增，原字段、分数、因子、分类、风险、置信度、来源、质量及报告结构均无额外变化；保留原 `generated_at`。仅更新 golden 输出与 manifest 的输出 schema 版本，没有重采 canonical 输入或修改 `legacy_*` 历史 oracle。

回归检查覆盖标准指标到评分及报告上下文的完整性、实际引用有效性、缺值/零值/负值与 schema 升级差异。后续标准契约若增加指标而解释输入未同步，完整性测试将失败。本记录为本地代码交付说明，不表示已部署生产或重跑线上任务。
