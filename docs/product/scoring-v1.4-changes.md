# Scoring v1.4 变更与回归报告

- 评分版本：`v1.3` → `v1.4`（`research_kernel/scoring.py::SCORING_VERSION`）
- 评分输出 schema：`v2.2` → `v2.3`（仅新增输出字段，不改变既有字段含义）
- Canonical schema：保持 `ifind-canonical.v1`
- 回归基准：golden corpus 13 只样本，`as_of` 沿用各样本冻结时间戳
- 重新生成方式：`tools/regenerate_golden_fixtures.py`（完全离线；本次只重写
  `golden_scores.json` / `golden_report_context.json`，canonical `pool/market/financial`
  输入未变）

本版只落实评审 A1–A6 中**不依赖新外部数据、也不需要伪造校准结论**的评分安全修复。
v1.3 文档原样保留为当时行为记录，不回溯修改。

## 1. 算法变更

| # | 变更 | 位置 | 旧行为（v1.3） | 新行为（v1.4） |
|---|---|---|---|---|
| 1 | 质量成长同源重复计权 | `scoring.factor_scores_for("quality_growth")` | `industry_space`（营收）25 + `revenue_growth`（营收）20 + `competitive_advantage`（ROE）25 + `roe_level`（ROE）15，同函数同阈值重复计权 | 合并为 `growth_proxy`（营收 CAGR）45 与 `profitability_proxy`（ROE）40；每个输入只出现一次，权重总和仍为 100，另有现金流 10、估值 5 |
| 2 | 概念代理命名 | `scoring.factor_scores_for` | `tam_potential` / `technical_barrier` / `commercialization_path` / `cash_reserve` / `cost_advantage` / `resource_reserve` 用财务比率代理，名称却像独立证据 | 直接改为数据名代理：`revenue_growth_proxy` / `roe_proxy` / `profit_growth_proxy` / `debt_ratio_proxy`，并在 `proxy_factors` 输出；LLM 不再看到 TAM、技术壁垒、团队、资源储备等概念名 |
| 3 | 固定常数因子 | `scoring_models._insurance_scores` / `_banking_scores`、`scoring.factor_scores_for("high_growth_venture")` | `team_quality=50`（权重 20）、`financial_leverage_review=60`（权重 10）、`capital_structure_review=60`（权重 10）作为有数据因子参与加权并计入 coverage | 三者从 `weights` 移除，在 `factor_scores` 中保留 `None`，由 `unevaluated_factors` 给出原因（`no_evidence_source`）；不再稀释真实因子权重或虚增覆盖率 |
| 4 | 低覆盖不发布分数 | `scoring.score_stock`、`research.ifind_chain` 解释提示词 | `factor_coverage < 0.75` 只触发复核，仍发布按剩余权重归一的总分；解释 Agent 可把 `investment_score` 当论据 | `factor_coverage < 0.75` 时 `investment_score` / `business_score` 为 `None`，`score_suppression_reason="factor_coverage_below_minimum"`；不引入未校准的 coverage 乘数。提示词明确：分数为 `null` 时不得作为分类/正面/负面/风险论据，只能在 `data_quality_notes` / `open_questions` 引用 `score_suppression_reason`、`factor_coverage`、`missing_factors`；validator 不放宽 |
| 5 | 研究评分排序 | `scoring.research_rank_key`、`scoring.score_pool` | 只按 `investment_score` 降序，同分时依赖输入顺序 | `investment_score` 仍是主键（降序），有分者在前、`None` 置后；`needs_agent_review=False` 优先、`research_confidence_score` 降序**只用于同分平局**。只影响研究评分池，不改候选 rules/ranking 链路 |
| 6 | 估值分桶 | `scoring_models.valuation_level` | PE/PB 双边未知才 `unknown`；单边未知时用 PE=100 / PB=10 占位并判 `expensive` | 任一边未知/非正即 `unknown`，估值因子为 `None` 并退出加权；不再用占位值判贵 |
| 7 | 回撤作为周期位置正向加权 | `scoring.factor_scores_for("cycle_reversal")` | `cycle_position`（权重 30）= 1 年回撤越大得分越高 | 从加权模型移除，保留 `cycle_position=None` 并标注 `no_calibrated_cycle_position_input`；未回测前不做简单反向 |
| 8 | 数据来源质量信号 | `scoring.collect_quality_signals`、`research_confidence_score` | `provider_conflicts` 只进复核；单独 `unverified` 不压置信度；`restated` 在评分层不可见 | 若输入已携带 `restated` / `unverified` / `provider_conflict`（行级、`field_sources`、`quality_flags`），统一进入 `quality_signals`、`needs_agent_review` 与排序；按信号种类对研究置信度乘一次（冲突/重述 0.9，未核验 0.95），**不折价投资分** |
| 9 | 输出字段 | `scoring.score_stock` | — | 新增 `unevaluated_factors`、`proxy_factors`、`quality_signals`、`score_suppression_reason`；schema 升 `v2.3` |

## 2. 逐样本回归（v1.3 → v1.4）

| Ticker | 名称 | 投资分 | 研究置信度 | 估值 | 工作流标签 | 需要复核 | 因子覆盖 | 缺失因子 |
|---|---|---|---|---|---|---|---|---|
| 600406.SH | 国电南瑞 | 55.22 → 55.22 | 92.0 → 92.0 | fair | low_priority → low_priority | False → False | 1.0 | - |
| 601398.SH | 工商银行 | 58.0 → None | 80.5 → 79.22 | cheap | low_priority → needs_review | False → True | 0.7222 | shareholder_return |
| 600519.SH | 贵州茅台 | 76.88 → 76.88 | 87.4 → 87.4 | expensive | defer → defer | False → False | 0.9 | shareholder_return |
| 600028.SH | 中国石化 | 39.43 → 20.67 | 92.0 → 92.0 | fair | low_priority → low_priority | False → False | 1.0 | - |
| 601111.SH | 中国国航 | 55.25 → 56.32 | 76.0 → 74.1 | expensive → unknown | needs_review → needs_review | True → True | 0.95 | valuation_level |
| 688111.SH | 金山办公 | 74.51 → 74.51 | 87.4 → 87.4 | expensive | risk_review → risk_review | False → False | 0.9 | shareholder_return |
| 688019.SH | 安集科技 | 77.22 → 84.02 | 92.0 → 92.0 | expensive | needs_review → needs_review | True → True | 1.0 | - |
| 300750.SZ | 宁德时代 | 69.5 → 69.5 | 87.4 → 87.4 | fair | monitor → monitor | False → False | 0.9 | shareholder_return |
| 000858.SZ | 五粮液 | 58.03 → 58.03 | 82.8 → 82.8 | expensive | needs_review → needs_review | True → True | 0.8 | dividend_yield |
| 002415.SZ | 海康威视 | 62.32 → 62.32 | 87.4 → 87.4 | fair | low_priority → low_priority | False → False | 0.9 | shareholder_return |
| 002230.SZ | 科大讯飞 | 47.24 → 47.24 | 87.4 → 87.4 | expensive | needs_review → needs_review | True → True | 0.9 | shareholder_return |
| 000039.SZ | 中集集团 | 38.77 → 38.77 | 82.8 → 82.8 | expensive | needs_review → needs_review | True → True | 0.8 | dividend_yield |
| 300346.SZ | 南大光电 | 54.12 → 55.15 | 92.0 → 92.0 | expensive | needs_review → needs_review | True → True | 1.0 | - |

行情字段（`return_1y` / `max_drawdown_1y`）全部未变，本版未改行情窗口。

## 3. 变化原因（逐条）

- **601398 工商银行：58.0 → None**。银行模型移除固定 `capital_structure_review=60`（权重 10）后，
  有数据的权重总数为 90；仍缺 `shareholder_return`（权重 25），覆盖率从 0.75 变为
  65/90 = 0.7222，低于 0.75 门槛，因此不发布投资分并转为 `needs_review`。置信度
  80.5 → 79.22 来自 coverage 乘数由 0.875 变为 0.8611。
- **600028 中国石化：39.43 → 20.67**。该样本走 `cycle_reversal`。旧模型 `cycle_position` 权重 30，
  由 1 年回撤 0.416 得分约 83.2；移除后按剩余 70 权重归一，等价于 `0.7 × 20.67 ≈ 14.47`，
  旧分中约 25 分来自回撤反向加权。v1.4 不再把“跌得多”当作正向分。
- **601111 中国国航：55.25 → 56.32，估值 expensive → unknown**。PE 因亏损为 `None`；v1.3 用
  PB 有效 + PE 占位 100 判 `expensive`（35 分）；v1.4 单边未知即 `unknown`，估值因子退出
  加权。质量成长模型剩余 95 权重均值为 56.32，故分数上升；覆盖率 0.95 使置信度
  76.0 → 74.1。
- **688019 安集科技：77.22 → 84.02；300346 南大光电：54.12 → 55.15**。两者走
  `high_growth_venture`。旧模型 `team_quality=50` 权重 20；移除后按其余 80 权重归一。
  旧值可精确还原为 `0.2 × 50 + 0.8 × 新分`：688019 为 `10 + 0.8 × 84.02 = 77.216`，
  300346 为 `10 + 0.8 × 55.15 = 54.12`。变化完全来自固定先验退出，而非真实因子变化。
- **其余 8 只分数与标签未变**。其中 `manufacturing_growth`（600406）、`dividend_blue_chip`
  （600519、000858、000039 等）与 `steady_quality_growth` 未涉及本轮重复计权或固定常量；
  估值均为双边有效，故分桶不变。

## 4. 测试与回归

- 更新 `tests/unit/test_scoring_v13_regressions.py` 中随 v1.4 语义变化的断言：
  单边估值由 `expensive` 改为 `unknown`；银行缺失股息因子覆盖率 0.75 → 0.7222 且不再发布分数；
  次新股低覆盖不再发布分数；版本断言改为 `v1.4`。
- 新增 `tests/unit/test_scoring_v14_regressions.py`，覆盖：重复计权合并、固定常数退出权重与
  coverage、估值双边、覆盖门槛、确定性排序、数据来源质量信号、`cycle_position` 移除；
  `test_agent_eval.py` 另覆盖无分样本不得把 `investment_score` 作为证据。
- 主验收已运行：评分/golden 聚焦回归 **96 passed**；无分解释与评分联合回归 **106 passed**；
  研究 API 集成 **13 passed**；PIT、模拟、估值与评分联合回归 **232 passed**；
  本次修改文件的 Ruff 检查通过。golden replay / manifest 测试用于确认 13 只样本逐字段一致。
  完整单元测试结果不在这段聚焦回归中预先声明。

## 5. 明确 deferred（本版未做）

- **行业估值分位**：PE/PB 绝对阈值（15/2、30/5）仍未行业中性化；没有行业分位数据，不伪实现。
- **回测 / IC / 样本外校准**：未实现，`calibration_status` 不变。
- **DDM / PB-ROE / EV/EBITDA 第二条估值腿**：未实现。
- **重述值 PIT 可见性**：由 PIT/因子代理负责；本版只在评分输入已携带 `restated` 时消费该信号，
  不改变重述值的 `available_from` 规则。
- **coverage 折价乘数**：未引入；低于 0.75 直接不发布分数。

## 6. 已知未覆盖

- `cycle_position` 目前完全未评估；在获得行业价格/产能/库存分位并通过样本外验证前，不恢复加权，
  也不做简单反向。
- `unverified` 信号在 iFinD canonical 输入中通常不可得；只有上游实际提供该字段时才会影响
  置信度与复核。该折减是操作性的置信度惩罚，尚未校准。
