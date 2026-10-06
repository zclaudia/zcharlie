# Scoring v1.3 变更与回归报告

- 评分版本：`v1.2` → `v1.3`（`research_kernel/scoring.py::SCORING_VERSION`）
- 评分输出 schema：`v2.0` → `v2.1`（仅新增字段，因子分与总分允许为 `null`）
- Canonical schema：保持 `ifind-canonical.v1`（字段形状未变，仅数值口径与质量标记变化）
- 回归基准：golden corpus 13 只样本，`as_of` 沿用各样本冻结的 v1.2 golden 时间戳
- 重新生成方式：`tools/regenerate_golden_fixtures.py`（完全离线，从 `provider_raw.json` 重放 canonical 并重新评分）

按 engineering-baseline「评分权重或阈值变化必须升级 scoring version」与「每次算法变化同时提交
scoring version 和新旧回归报告」执行。历史 run 不重算；新 run 以 v1.3 产出。

## 1. 算法变更

| # | 变更 | 位置 | 旧行为（v1.2） | 新行为（v1.3） |
|---|---|---|---|---|
| 1 | 非正分母估值 | `ifind_chain.canonicalize_ifind_results`、`scoring_models.valuation_level`、`peer_comparison._metric` | EPS<0 得到负 PE，负 PE ≤15 可被判为 `cheap`/`fair`；同理 PB、PS | EPS≤0 / 每股净资产≤0 / 每股营收≤0 时 PE/PB/PS 为 `null`，并加 `negative_earnings` / `negative_book_value` / `non_positive_revenue` 质量标记（severity=review，不计入 missing_fields）；`valuation_level` 把非正倍数视为不可用，永远不会判为 `cheap`；同行比较忽略非正 PE/PB |
| 2 | ROE 稳定性 | `ifind_chain._roe_stability` | 只要有任意年份即计算；均值为负时 `1 - std/负均值` 被钳制为 1.0（"完美稳定"） | 需 FY-3..FY 四年齐全且均值 > 0，否则为 `null`（missing） |
| 3 | 财务时效 | `scoring.assess_freshness` | 用抓取时间 `updated_at` 判断，永不过期 | 按报告期：A 股年报须在次年 4 月 30 日前披露，`as_of` 在 5 月 1 日及之后期望 FY(Y-1)，之前期望 FY(Y-2)；`report_year` 早于期望年度即 stale。`age_hours` 从财年末计。无 `report_year` 时回退到 `updated_at` 口径 |
| 4 | 行情时效 | `scoring.assess_freshness`、`research/kernel.py` | 日历日（kernel 7 天 / 默认 1 天），周末与长假运行会误判或掩盖 | 按交易日计：统计行情日期之后到 `as_of` 日期（上海时区）之间的交易日数，默认上限 1 个交易日。kernel 读取仓库内 `config/calendars/a_share.yaml`（含节假日）；日历未覆盖的年份逐日回退到工作日规则（`calendar_basis=weekday_fallback`），不会崩溃；kernel 仅接收 `is_trading_day` 回调，与 scheduler 解耦 |
| 5 | 提供方不可用字段 | `scoring.provider_unavailable_fields`、`scoring.row_requires_review` | iFinD 不提供 `dividend_yield`，导致每个样本 `needs_agent_review=True` | `field_sources[*].source == "unavailable"` 的字段视为已知提供方缺口：仍计入 missing_fields 与数据质量分，但不单独触发复核；其他缺失字段或 review/error 级质量标记仍然阻断。输出新增 `provider_unavailable_fields` |
| 6 | 缺失因子 | `scoring_models.score_ratio/weighted_score/factor_coverage` | `score_ratio(None)=50`，缺失因子按中性 50 混入加权；估值未知在通用模型中记 35、现金流缺失记 45 | 缺失因子为 `null`，从加权中剔除并按剩余权重归一；全部缺失时总分为 `null`。输出新增 `factor_coverage`（有数据的权重占比）与 `missing_factors`，加 `factor_missing` 信息标记；研究置信度乘以 `0.5 + 0.5 × coverage`；覆盖率 < 0.75 或总分为 `null` 时需要复核 |
| 7 | 次新股 | `ifind_chain._optional_first_row` | FY-3 利润表或历史盈利年度无数据时 `_first_row` 抛错，整次研究失败 | 基期/历史期改为可选，增长率与 ROE 稳定性变为 missing；最新期端点仍硬失败 |
| 8 | 解释引用校验 | `ifind_chain.validate_explanation_fields/policy` | 只校验路径存在；`needs_review` 用子串匹配 | 分类/正面/负面/风险论据不得引用值为 `null` 或列于 `missing_fields` 的字段（`data_quality_notes` 例外）；`needs_review` 需作为完整标记出现（`no_needs_review` 等不再通过） |
| 9 | "1y" 行情统计 | `ifind_chain._trim_to_one_year` | 使用请求窗口（as_of-400 天）内的全部数据 | 只用 `(as_of-365 天, as_of]` 内的交易日；窗口内少于 200 个交易日时对 `return_1y` / `volatility_1y` / `max_drawdown_1y` 加 `short_price_history` 复核标记 |

## 2. 逐样本回归（v1.2 → v1.3）

| Ticker | 名称 | 投资分 | 研究置信度 | 估值 | 工作流标签 | 需要复核 | 因子覆盖 | 缺失因子 |
|---|---|---|---|---|---|---|---|---|
| 600406.SH | 国电南瑞 | 55.22 → 55.22 | 92.0 → 92.0 | fair | needs_review → low_priority | True → False | 1.0 | - |
| 601398.SH | 工商银行 | 56.0 → 58.0 | 92.0 → 80.5 | cheap | needs_review → low_priority | True → False | 0.75 | shareholder_return |
| 600519.SH | 贵州茅台 | 74.19 → 76.88 | 92.0 → 87.4 | expensive | needs_review → defer | True → False | 0.9 | shareholder_return |
| 600028.SH | 中国石化 | 39.43 → 39.43 | 92.0 → 92.0 | fair | needs_review → low_priority | True → False | 1.0 | - |
| 601111.SH | 中国国航 | 57.25 → 55.25 | 84.0 → 76.0 | fair → expensive | needs_review → needs_review | True → True | 1.0 | - |
| 688111.SH | 金山办公 | 72.06 → 74.51 | 92.0 → 87.4 | expensive | needs_review → risk_review | True → False | 0.9 | shareholder_return |
| 688019.SH | 安集科技 | 77.22 → 77.22 | 92.0 → 92.0 | expensive | needs_review → needs_review | True → True | 1.0 | - |
| 300750.SZ | 宁德时代 | 67.55 → 69.5 | 92.0 → 87.4 | fair | needs_review → monitor | True → False | 0.9 | shareholder_return |
| 000858.SZ | 五粮液 | 56.42 → 58.03 | 92.0 → 82.8 | expensive | needs_review → needs_review | True → True | 0.8 | dividend_yield |
| 002415.SZ | 海康威视 | 61.09 → 62.32 | 92.0 → 87.4 | fair | needs_review → low_priority | True → False | 0.9 | shareholder_return |
| 002230.SZ | 科大讯飞 | 47.52 → 47.24 | 92.0 → 87.4 | expensive | needs_review → needs_review | True → True | 0.9 | shareholder_return |
| 000039.SZ | 中集集团 | 41.02 → 38.77 | 92.0 → 82.8 | expensive | needs_review → needs_review | True → True | 0.8 | dividend_yield |
| 300346.SZ | 南大光电 | 54.12 → 54.12 | 92.0 → 92.0 | expensive | needs_review → needs_review | True → True | 1.0 | - |

行情"1y"统计（变更 9，canonical 输入随之重放）：

| Ticker | return_1y | max_drawdown_1y |
|---|---|---|
| 600406.SH | 0.078028（未变，无 provider_raw） | 0.297914（未变） |
| 601398.SH | 0.167842 → 0.179487 | 0.152882 |
| 600519.SH | -0.112291 → -0.088003 | 0.230684 |
| 600028.SH | -0.018587 → 0.027237 | 0.416 |
| 601111.SH | -0.209893 → -0.257538 | 0.388474 |
| 688111.SH | -0.337532 → -0.309975 | 0.473596 |
| 688019.SH | 0.446394 → 0.197367 | 0.38645 |
| 300750.SZ | -0.023841 → -0.276026 | 0.37425 |
| 000858.SZ | -0.424275 → -0.387641 | 0.437936 → 0.408887 |
| 002415.SZ | 0.095881 → -0.001231 | 0.155667 |
| 002230.SZ | -0.298001 → -0.374454 | 0.414634 |
| 000039.SZ | 0.087231 → 0.047503 | 0.456036 |
| 300346.SZ | 0.361509 → 0.152681 | 0.496703 |

## 3. 变化原因

- **工作流标签与复核（13 只中 7 只解除复核）**：v1.2 下所有样本都因 iFinD 不提供 `dividend_yield`
  被强制 `needs_review`，标签失去区分度。v1.3 把这一提供方级缺口视为非阻断后，仍需复核的 6 只均有
  独立原因：601111 有真实缺失字段与 `negative_earnings` 标记；688019、300346 被分类为高成长创业型
  （策略本身要求复核）；000858、002230、000039 分类置信度 0.5 < 0.6。
- **投资分**：只有依赖 `dividend_yield` 的因子发生变化。v1.2 把缺失的股息因子按 50 计入加权，v1.3 将其
  剔除并归一化剩余权重：`steady_quality_growth` 模型的 `shareholder_return`（权重 10/100：600519、
  688111、300750、002415、002230）、`dividend_blue_chip` 的 `dividend_yield`（20/100：000858、
  000039）、银行模型的 `shareholder_return`（25/100：601398）。其余因子高于 50 时分数上升，低于 50
  时下降（002230、000039）。不含股息因子的模型（manufacturing_growth、cycle_reversal、
  high_growth_venture）分数不变（600406、600028、688019、300346）。
- **研究置信度**：因子覆盖率 < 1 时乘以 `0.5 + 0.5 × coverage`（0.9 → ×0.95，0.8 → ×0.9，0.75 → ×0.875）。
  601111 另因 `roe_stability` 变为缺失，数据质量分 0.84 → 0.76。
- **601111 中国国航（亏损样本）**：FY2025 EPS = -0.11，v1.2 得到 PE = -53.73，因负数满足 `PE ≤ 30` 被判为
  `fair`；v1.3 PE 为 `null` + `negative_earnings`，估值按 PB 2.42 与 PE 不可用的保守占位判为 `expensive`，
  quality_growth 模型的估值因子 75 → 35（权重 5），投资分 -2.0。四年 ROE 全为负（-0.6% ~ -90.9%），
  v1.2 的稳定性被钳制为 1.0，v1.3 为缺失。
- **"1y" 收益/波动/回撤**：请求窗口约 400 天，v1.2 把最早约 35 天也算进"1 年"。裁剪到 365 天后多数样本
  `return_1y` 变化明显（如 300750 -2.4% → -27.6%），`max_drawdown_1y` 仅 000858 变化（高点移出窗口）。
  这些字段只进入风险等级与周期分类阈值，本次未引起风险等级或策略分类变化。
- **时效**：全部样本在 2026 国庆休市期间采集，`market_data` 日历日龄约 5 天但交易日龄为 0，按交易日判定
  仍新鲜；财务数据 `report_year=2025` 等于期望年度 2025，均不过期，与 v1.2 结论一致。

## 4. Fixture 处理说明

- 12 只带 `provider_raw.json` 的样本：canonical 输入（market/financial）由当前 `canonicalize_ifind_results`
  从原始响应离线重放（`as_of` = manifest `collected_at`），golden 输出随之重新冻结。`provider_raw.json`
  与 `pool.json` 未变。
- 600406.SH 未保留原始响应，canonical 输入保持采集时形态（含 v1.2 的 400 天行情窗口）；新增
  `golden_scores.json` / `golden_report_context.json` 作为 v1.3 期望，`legacy_*` 文件作为 my-stocks
  0.3.0 / v1.2 历史 oracle 原样保留。回放测试优先比较 golden 文件。
- 新增离线合成用例覆盖亏损公司与次新股（`tests/unit/test_scoring_v13_regressions.py`），无需联网。

## 5. 已知未覆盖

- 通用高成长模型的 `team_quality`（50）与银行/保险模型的 `*_review`（60）仍是固定先验，不依赖数据，
  因此不计入缺失因子；后续如需去除应另行升级版本。
- Web 端时效展示仍读取 `max_age_days`（行情块中该值现为交易日上限）；新增的 `age_trading_days` /
  `calendar_basis` / `basis` 字段尚未在 UI 中展示。
