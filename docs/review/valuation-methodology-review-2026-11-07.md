# zcharlie 股票价值分析方法评审

| 项 | 内容 |
|---|---|
| 评审日期 | 2026-11-07 |
| 评审对象 | `/Users/zhvala/SourceCode/zclaudia-repositories/zcharlie`（个人 A 股研究台） |
| 评审范围 | `valuation/`、`research_kernel/scoring*.py`、`factors/`、`config/rule_templates/`、`investor/publication.py`、`docs/product/*valuation*` / `scoring-v1.3-changes.md` / `templates-v1-review.md` / `funnel-design.md`，以及 AG11 覆盖探针与相关测试 |
| 评审视角 | 卖方 / 买方股票研究： PM 七问、变异认知、估值口径、因子有效性、样本外校准 |
| 证据口径 | 每条结论标注 `[事实]`（代码/文档可查，附路径:行号）、`[判断]`（研究经验推断）、`[MISSING]`（本次未核验） |

> **一句话结论**：这是一台**机构级的「证据与预期差记录仪」**，但还不是**「价值判断引擎」**——PIT 纪律、fail-closed、不可变证据、版本治理整体优于多数个人研究台；然而真正决定投资结论的那一环（价格 vs 价值、评分权重、阈值校准）目前是空心的：**默认代码装配下反向 DCF 无法产出数值，5 个基线模板均为 `uncalibrated` 且应用内未见回测实现，评分因子存在同源重复计数**，因此当前输出只适合做研究优先级排序，不适合支撑方向性价格判断或仓位意见。

**综合评级（方法论，非投资评级）**

| 维度 | 评级 | 一句话理由 |
|---|---|---|
| 证据工程 / 前视偏差防护 | A- | `available_from` / `pubDate` / `captured_at` / 交易日日历 / 回补标记 / 重述标记层层设防；但最新重述值叠加原始 `pubDate` 与历史回补存在前视路径 |
| 估值口径审慎性 | B+ | FCFE 与 FCFF 严格区分、不合格不求解、不输出目标价；但默认路径无数值输出、终值结构缺乏衰减 |
| 评分 / 因子方法论 | C | 同源因子重复计数、概念与数据错配、缺失不惩罚、绝对阈值无行业/利率锚 |
| 决策与校准 | D+ | 5/5 基线模板 `uncalibrated`，应用内无 backtest / forward return / IC / 绩效归因，仅有净值与回撤模拟 |
| **综合** | **B-** | 作为「研究辅助台」合格且优秀；作为「股票价值分析系统」不合格 |

**行动分类**：`重新评估` + `等待证据`。在完成本文 P0 与 P1 之前，不建议用 `investment_score` 或模板命中结果支撑任何方向性价格观点与仓位动作；前瞻检验前必须先排除重述值的历史时点泄漏。

> **2026-11-07 代码复核修订**：本版区分默认装配、固定样本探针与实际部署，修正缺失值分桶、LLM 草稿审批、候选去重、杜邦残差与市值口径等表述，并将重述值 PIT 风险列为前置问题。

---

## 1. 方案还原：这套系统到底怎么给股票"定值"

### 1.1 四条并存、口径不同的链路（这是理解全部问题的前提）

`[事实]` 代码库里至少有 4 套互相独立、互不通信的「估值 / 打分」逻辑，同名但含义不同：

| # | 链路 | 入口 | 实际做什么 | 是否进投资意见 |
|---|---|---|---|---|
| A | **反向 DCF + 历史分位 + 杜邦** | `src/investment_agent/valuation/` | 求「当前市值隐含的 5 年 FCFE 增长率 h」；PE/PB 自身 5 年分位；ROE 恒等分解 | 仅作方向性价格观点的**准入锚** |
| B | **确定性评分 v1.3** | `research_kernel/scoring.py` | 0–100 分，5 套策略模型 + 4 套行业模型 | 只进离线研究报告与排序，**不进候选筛选** |
| C | **因子库 + 5 个规则模板** | `factors/library.py`、`config/rule_templates/*.yaml` | 沪深 300 周频阈值筛选 → 候选 → 区间事件 | 决定候选与 `buy_zone/add/trim` 事件 |
| D | **持仓 NAV 估值** | `portfolio/valuation.py` | 组合市值 / 净值 | 与股票估值无关 |

`[事实]` `docs/product/funnel-design.md:392` 明确「分数退为次要展示」；`candidates/ranking.py:192-240` 排序完全不引用 `investment_score`；`api/routes/investment_assessments.py` 全文无任何估值结果字段。
`[判断]` 因此「评分高的股票」与「被选中的候选」是两套逻辑，容易出现高分未入选 / 入选却低分的割裂，且用户很难察觉。

### 1.2 反向 DCF：`reverse_dcf_fcfe.v1` 的完整数学

`[事实]` `valuation/solver.py:25-34`：

```
F_t  = F0 · (1+h)^t                     t = 1 … 5（N=5 硬编码，增长率无衰减）
TV_5 = F_5 · (1+g) / (r − g)            Gordon 永续；第 6 年一步跳到 g
V(h) = Σ_{t=1..5} F_t/(1+r)^t + TV_5/(1+r)^5
求解 V(h) = M  （M = 当前普通股整体市值）
```

- 有界二分求根，`h ∈ [-0.95, 1.00]`，`WIDTH=1e-8`、`RESIDUAL=1e-6`、最多 200 次、Decimal 40 位精度（`solver.py:9-12, 45-68`）。
- 输出 `growth`（隐含 h）与 `terminal_pv_fraction`（终值现值占比），**不是内在价值、不是目标价、不是评级**（`docs/product/investor-agent-valuation.md:62`）。
- 基数 F0 = 三年 FCFE 中位数 / 最近一年；敏感性网格 = 2 种 F0 × (1 主 r + ≤6 敏感性 r)（`valuation/service.py:322-344`）。
- 关键假设：`r` 与 `g` **每次请求必填、无默认值、无 CAPM / 无风险利率 / beta / ERP 锚**（`valuation/contracts.py:18-20`、`service.py:112-141` 校验来源可见性、情景来源与敏感性结构）；`N=5`、无 stub period、无税率 / 税盾 / 折旧加回 / 维护 capex 拆分。
- 适用性机械筛：三年 FCFE 均为正且 `max/min ≤ 3`（`service.py:301-306`）。
- FCFE 桥接：`CFO − 购建长期资产 + 取得借款 + 发行债券 − 偿还债务 −(未计入 CFO 的)现金利息`（`valuation/fcfe.py:99-107`）。

**`[事实]` 最重大的一条：默认代码装配下，该模型不会产出数值。**
`valuation/contracts.py:72-78` 的默认 resolver `UnavailableQualification.resolve()` 恒返回 `None`，生产注册链路未注入其他 resolver（`agent/research.py:817`）→ `service.py:200-204` 在正常请求中追加 `missing_inputs: fcfe_source_mapping_unavailable`；若请求已处于 `missing_assumptions` / `not_applicable` / `stale_inputs`，则保留原状态。`docs/product/probes/ag11-fcfe-coverage-2026-10-10.json:13-19` 的固定代表样本（4 只，非 CSI300 全池）摘要为四层 0/4、`production_reverse_dcf_enabled: false`；其中 `raw_fields_available` 由数据计算，后三层由探针按未启用状态固定记 0（`tools/probe_fcfe_sources.py:89-99`），不能解释为全量生产实测覆盖。样本主因包括 `missing_borrowings` / `missing_bonds` / `financial_sector_excluded`。`tests/test_fcfe_sources.py:79-84` 是对该冻结探针报告的回归断言；service 层成功结果使用资格 fixture，纯求解器测试使用合成数字，发布测试另有手工构造的 `ok` 记录，均不构成真实数据 + 默认 resolver 的成功证据。

**`[判断]`** 这不是 bug，而是**诚实的 fail-closed**，值得表扬；但工程上意味着 `publication.py:67` 的估值锚集合 `{reverse_dcf_fcfe, pe_percentile_5y, pb_percentile_5y}` 在默认装配下只有后两者能解析为有效数值锚 —— **方向性价格观点的现实锚，退化为「自身 5 年 PE/PB 历史分位」这一纯相对价格指标**，全系统没有「价值 − 价格」的差。上述结论不排除部署环境注入自定义 resolver，实际部署与历史计算记录仍需另行核验。

### 1.3 评分 v1.3：线性可加 + 硬编码权重

`[事实]` `research_kernel/scoring_models.py:27-37, 76-91`：

```
因子分 = clamp((value − low)/(high − low) × 100, 0, 100)   # 单一线性段，逆向可选
总分   = Σ(因子分 × 权重) / Σ(有数据因子的权重)              # 缺失因子剔除后重新归一
```

估值因子是三档离散桶（`scoring_models.py:46-67`）：`PE≤15 且 PB≤2 → cheap(100)`；`PE≤30 且 PB≤5 → fair(75)`；否则 `expensive(35)`。**仅单边缺失/非正时**才给 PE/PB 填 `100.0 / 10.0` 占位并落到 `expensive`；两者同时缺失/非正时返回 `unknown`，估值因子为 `None` 并退出加权（`scoring_models.py:54-73`）。

`[事实]` 权重实例（`scoring.py:205-222`，质量成长模型）：

| 因子名 | 权重 | 实际输入 | 阈值 |
|---|---:|---|---|
| `industry_space` | 25 | **`revenue_growth_3y`** | (0.0, 0.40) |
| `competitive_advantage` | 25 | **`roe`** | (0.05, 0.25) |
| `revenue_growth` | 20 | **`revenue_growth_3y`** | (0.0, 0.40) |
| `roe_level` | 15 | **`roe`** | (0.05, 0.25) |
| `cash_flow_quality` | 10 | FCF>0 → 85 否则 45（布尔） | — |
| `valuation_level` | 5 | PE/PB 桶 | — |

→ **85% 的权重实际只由 2 个连续变量决定**（营收增速 45% + ROE 40%），其余 15% 来自 FCF 布尔因子与 PE/PB 分桶；四个因子名并未对应四个独立证据维度。同类问题见 `high_growth_venture`：`team_quality = 50.0` 固定常数占权重 20（`scoring.py:262-275`）；银行/保险的 `*_review = 60.0` 固定占 10（`scoring_models.py:190, 214`）—— `docs/product/scoring-v1.3-changes.md:98-99` 自列为「已知未覆盖」。固定常数不制造同模型内的横截面区分度，但会稀释真实因子权重并虚增覆盖率。

`[事实]` 其他要点：`cycle_position = score_ratio(max_drawdown_1y, 0, 0.50)` 权重 30，即**过去一年跌得越深得分越高**（`scoring.py:233`），而同一变量在 `risk_level` 里又被记为风险（`scoring.py:151`）；`docs/product/scoring-v1.3-changes.md:68-73` 自承 v1.3 把缺失因子剔除后归一，导致移除低分股息因子后**该样本分数反而上升**（如 600519 74.19 → 76.88）；这是一例而非必然方向，缺失因子高于剩余均值时也会降分。`scoring.py:876-882` 的**研究评分池**按 `investment_score` 降序排序，排序键**不看** `factor_coverage` / `research_confidence` / `needs_review`；它不是候选漏斗的排序。

### 1.4 从证据到意见

`[事实]` 链路：`PIT 快照 → 因子 → 模板 screen（每模板最多 20 命中，按"阈值内距离"均值排序）→ 候选 → LLM 起草参数覆盖 → 区间事件 → LLM 公司观点 → 发布校验`。
发布门禁做得很严（`investor/publication.py:292-304`）：方向性观点需要 `counter_case` + `critical claims`；任何非 `undetermined` 的价格观点（含 `balanced`）都需要**估值计算锚**；`undetermined` 不支持 buy/sell。公司观点在结构上不承载仓位提案（`investor/contracts.py:232-233`），目标价禁令则位于 prompt 层（`rules/draft_policy.py:34-35,122`、`rules/recommend.py:112`）。
`[事实]` 但 **5/5 基线模板 `calibration_status: uncalibrated`**（如 `dividend_blue_chip.yaml:28`），`docs/product/templates-v1-review.md:3`「P3 回测再校准」、`funnel-design.md:360-368` 整节为待办；对 `src/` 的精确检索确认 **无 backtest / forward_return / hit_rate / IC / 样本外 实现**；`simulation/metrics.py` 只有 `cumulative_return` 与 `maximum_drawdown` 两个指标。`research/` 下独立第三方 alphalens 快照不属于应用生产链路。

---

## 2. 我的判断方法（推理链路）

1. **先分离「文档声称」与「代码实际」**。项目文档质量极高，但 `valuation/` 的设计文档描述了一个完整 DCF，代码却默认 fail-closed，固定样本探针记录为未启用。若只读文档会得出完全相反的结论；同时，探针的固定 0 值也不能外推为全量实测覆盖。
2. **定位「最终影响决策的数值」由谁产生**。不是最复杂的模块，而是 `scoring.py` 的权重表、`scoring_models.py` 的估值桶、`rules/screen.py` 的强度公式。排雷优先级按「影响排序/意见的链路」排，而非按代码复杂度。
3. **用 PM 七问逐项对照**（见第 5 节），重点问「什么被错误定价了」——这要求同时有「价格隐含预期」和「独立合理预期」两个量，缺一即无法定义"错"。
4. **以「3 年后回看是否站得住」检验**：输入能否复现、有无前视偏差、阈值有无样本外证据、结论是否可被证伪。
5. **区分「刻意取舍」与「能力缺口」**：不输出目标价、不让 LLM 打分、不用 0 填补缺失 FCFE，是优点；没有预期对照组、没有回测、没有利率锚，是缺口。两者不能混为一谈，否则改进会破坏掉最有价值的工程纪律。

---

## 3. 做得好、必须保留的部分

| 项 | 证据 | 为什么在机构研究里也难得 |
|---|---|---|
| PIT / 防前视偏差 | `factors/snapshot_model.py:266-300`、`view_db.py:173-191`、`providers/errors.py:56` `LookaheadDataError`、`service.py:255-278, 371-383` | 财报按可见日期生效、行情按 `available_from` 可见、回补单独标记；多数个人系统直接读"最新值"。但 AkShare/Eastmoney 重述值的历史回补路径未被这些纪律完全覆盖，见 A7 |
| fail-closed 与口径纪律 | `contracts.py:72-78`、`service.py:200-204`、`valuation.md:11-29` | 明确拒绝把 `CFO − capex` 冒充 FCFE、拒绝用利润表利息费用代理现金利息、缺科目不填 0、不适用就不发布 |
| 事实 / 假设分离 | `evidence/contracts.py:90-97` assumption provenance 三分类；`service.py:345` 明确标注未来现金流与折现率是情景 | 避免把主观假设伪装成公司事实，是研报最常见的失真来源 |
| 版本治理与可复现 | `scoring.py:44` v1.3、`docs/product/scoring-v1.3-changes.md` 13 样本逐只回归、`tests/unit/test_golden_corpus_replay.py` 禁止为过测改 golden | 算法变更必须升版本 + 书面 diff；历史 run 不静默重算 |
| 反 DCF 的取向本身 | `valuation.md:5, 62`「不直接输出目标价」「不得直接以 h 低于历史收入增速推导价格有吸引力」 | 问"价格隐含什么"比问"值多少钱"更稳健（expectations investing），且主动堵死了最常见的误读 |
| 多空平衡与证伪 | `publication.py:292-304`、模板 `falsification` / `exit_review` 字段、`config/review_lenses.yaml` bear lens | 方向性观点强制带反例与关键论据 |
| 逐格敏感性不补齐 | `service.py:340-344`、终值现值占比随结果输出 | 失败格不用邻格填，避免"看起来完整"的假敏感性 |

---

## 4. 核心问题（按对结论的杀伤力排序）

### A 类：影响结论正确性的硬伤（P0）

| # | 问题 | 证据 | 后果 `[判断]` |
|---|---|---|---|
| A1 | **同源因子重复计数** | `scoring.py:215-218`：营收 CAGR 与 ROE 各被计两次，合计 85% 权重 | 85% 权重只是 2 个连续变量的重复表达，其余 15% 为粗粒度布尔/分桶；LLM 解释层可能据此写出"行业空间大/护城河深"的论据，而证据只是历史增速。是否降低预测力仍需实证 |
| A2 | **因子名与数据错配（概念替换）** | `scoring.py:215-216, 233-235, 270-272`：TAM/技术壁垒/团队质量/资源储备/行业空间均用现成财务比率代理，团队质量甚至是常数 50 | 报告结论的语义与底层证据脱节，属于研报里最危险的一类风险：数字可复算、论断却超出证据。固定常数还会稀释真实因子权重并虚增覆盖率 |
| A3 | **缺失数据不惩罚，且可能提分** | `scoring_models.py:82-91` 剔除 `None` 后归一；`scoring-v1.3-changes.md:68-73` 记录 600519 提分样本；`scoring.py:501-520, 679-689, 876-882` | 分数方向取决于缺失因子的应有得分，可能升也可能降；但 coverage 只影响置信度与复核标签，不改变总分和研究评分排序，不同信息覆盖的分数仍被直接比较 |
| A4 | **数据质量问题处理不一致且不进总分** | `scoring.py:512-520, 627-689`：stale 压 confidence 并触发复核；provider_conflict 只进复核/风险摘要；单独 unverified 不压 confidence 也不触发复核；`field_resolution.py:83-104,125-126` 把 `restated` 元数据从因子字段丢弃 | 数据质量不会在总分中折价；重述标记的丢失还使评分层无法识别历史时点风险，见 A7 |
| A5 | **估值桶用绝对阈值 + 单边未知即判贵** | `scoring_models.py:46-73`：PE≤15 & PB≤2 一刀切；单边缺失/非正时 PE 填 100 或 PB 填 10 → `expensive`(35)；双边缺失/非正则 `unknown` 并退出加权 | 跨行业/跨利率/跨增长阶段不可比；负 PE 且 PB 有效时被贴"贵"标签（601111 案例见 `scoring-v1.3-changes.md:76-79`）；与模板侧「周期股用 PB 不用 PE」结论相反 |
| A6 | **回撤越大分越高** | `scoring.py:233` 权重 30；`scoring.py:151` 同一变量计风险 | 无周期位置/产能/价格分位验证，"跌得多"被当作反转依据，且同一变量在输出里既加分又加风险；其经济有效性未验证 |
| A7 | **重述值叠加历史回补存在前视路径** | `providers/akshare_eastmoney.py:72-78` 值始终为最新重述值；`snapshots/eastmoney.py:344-364` 可用原始 `pubDate` 且重述不参与可见性；`factors/view_db.py:173-184` 回补绕过 `captured_at`；`field_resolution.py:83-104` 丢弃重述标记 | 在历史回补场景，重述后的值可能被重述前的 `as_of` 看到，污染因子、评分与回测。代码路径成立；实际数据库中哪些记录受影响尚未核验 |

### B 类：估值模型的结构性局限（P1）

| # | 问题 | 证据 | 后果 `[判断]` |
|---|---|---|---|
| B1 | **r 完全主观、无市场锚** | `contracts.py:18-20`、`service.py:112-141`；`valuation.md:97` 示例 r=8/9/10% → h=11.9/16.0/19.7% | **r 每变动 1pp，隐含 h 变动约 4pp**；"价格隐含预期"的不确定区间远大于单点展示给人的印象，且没有 10Y 国债 / ERP / beta 作为可审计依据 |
| B2 | **终值结构无衰减** | `solver.py:29-33` 5 年恒定 h 后一步跳 g；`valuation.md:97` 的**单一虚构算例**终值现值占比 **76.7%** | 该算例约 3/4 由永续假设决定，但不能外推为全样本比例；结构上 h 很大程度受终值参数影响，对高增长/周期公司可能失真。代码只"暴露"占比，没有阈值或降权 |
| B3 | **无 stub period，历史年现金流当作从 as_of 起等距年度** | `service.py:43` warning、`valuation.md:47` 自承 | 报告期与估值日错位最多近 1 年，相当于凭空移动现金流时点而不折现；跨披露季时 h 不可比 |
| B4 | **适用范围收窄** | `service.py:301-306` 三年为正且 max/min≤3；`valuation.md:31` 排除金融 | 设计上只剩"稳态现金流公司"，最需要 DCF 的周期/成长/扭亏/银行被排除；这类公司是否用 PE/PB 分位已足够，是方法判断而非代码事实。默认 resolver 下这些适用性检查尚不会实际执行 |
| B5 | **无内在价值 / 无安全边际 / 无正向估值** | `src/` 无 `intrinsic_value` / `margin_of_safety` / `fair_value` 的计算字段与数值输出；评审提示词中的"安全边际"只是文字视角；`investor-agent-design.md:46` 明确不生成目标价 | 只有"相对自身历史的价格"，没有"价值 − 价格"。**变异认知缺第二个量** |
| B6 | **年报口径基本面因子信息滞后** | `factors/library.py:136-143,162-207,249-312` 多项基本面因子只取最新年报；`config/field_sources.yaml:69`、`ifind_mapping.py:259`「Q1/H1/Q3 not annualized」是字段属性，代码因此未直接用未年化季报；`pe_ttm` 直接取 baostock 现成列、无自建 TTM 拼接（`field_sources.yaml:311-322`） | 11 月使用上年年报时滞后约 10 个月，年报披露前可达 12–16 个月，并非"最多 10 个月"；已披露季报未进入这些因子。`eps_ttm` 与股本等并非全部采用年报口径，需逐项区分 |
| B7 | **市值的普通股归属与股本时点未闭环** | `factors/library.py:374-387,710-724` 明确为未复权收盘价 × 最新报告总股本，即总市值；`investor/coverage.py:123-124` 标注 `total_market_cap`、普通股归属未核验；`valuation.md:37` 的总/流通待核表述已过时 | 当前总市值使用未复权现价本身正确，风险在报告期后送转/拆股时股本未同步，及 A/H、多类股、限售、可转债稀释、少数股东权益未处理。反向 DCF 的市值来自独立证据引用，并非 `service.py:371-373` 计算 |
| B8 | **杜邦口径不自洽，且残差门禁未实现** | `factors/library.py:275-319` 分子 `net_profit_parent`（归母），分母 `total_equity`（含少数股东）；`valuation/service.py:499-525` 只输出残差，并无残差超容差即降级/阻断的分支 | 乘积不是归母 ROE；文档声称的"阻断引用"与代码不符。生产 AkShare 行通常缺 `roe`，残差常为 `None`，因此该恒等检验也未实际发挥作用 |
| B9 | **行业适用性只是布尔声明** | `contracts.py:36-63` 12 个 bool 全由外部 resolver 传入，`service.py:216-227` 只读布尔 | 银行/保险/券商/公用事业无专用估值（无 PB-ROE、无内含价值、无管制资产回报）；一旦接入 resolver，正确性完全依赖外部映射，代码本身不做会计识别 |

### C 类：决策链与校准（P2/P3）

| # | 问题 | 证据 |
|---|---|---|
| C1 | **阈值未校准**：5/5 基线模板 `uncalibrated`；默认值来自旧评分原型与人工调整，**部分参数**另参考 **2026-09-30 单日横截面中位数**（红利股息率/ROE，`dividend_blue_chip.yaml:17-19`、`templates-v1-review.md:67,394,412`），不能概括为全部模板参数均来自该日中位数；因**单只异常股**加 ROE 上限（海航控股 ROE 52.5%，`templates-v1-review.md:320, 340`）；"敏感度"= 单日命中数扰动（`:51`） | `templates-v1-review.md:3`、`funnel-design.md:360-368` |
| C2 | **LLM 单股调参 = 机制化过拟合风险**：输入是单只股票单一 `as_of` 的因子值，prompt 要求"结合该股命中情况决定参数松紧"（`rules/draft_policy.py:120`、`drafting.py:395-424`），论据只能引用该股当期非 null 因子。生成的是 `draft` 单股实例覆盖，模板默认值不被修改，须人工 `approve()` 才进入 active（`rules/store.py:281-310,328-346`） | 无样本外 / 滚动窗口 / 多重检验校正；人工审批是重要的生产门禁，但不能替代统计验证。系统自承「参数调整同样是假设」（`draft_policy.py:113`） |
| C3 | **强度排序无经济含义**：`rules/screen.py:259-274, 345-354` 对**已满足且可计算 margin 的比较条件**等权（等值/`in` 不贡献强度），按阈值相对距离归一后平均、`clamp[0,1]` 丢失尾部；不同量纲靠 `/max(|t|,0.01)` 强行可比；`between` 取较近端距离导致排序互换（`templates-v1-review.md:446-447` 自承） | 非风险调整、非预期收益 |
| C4 | **变异认知文档写到位、生产未强制**：`investor-agent-research-policy.md:71` 要求说清"价格隐含预期 vs 自身取证预期的差"，但 v2 `expectations_gap` 包**非生产默认**（`investor-agent-method-packages.md:377`），基线 `company-research.v1.yaml:19-21` 三个 discipline 开关全 `false`；且全库无一致预期 / 分析师预测数据作对照组 | 无对照组即无法定义"市场错在哪" |
| C5 | **「为什么是现在」缺失**：全库无 catalyst / 催化剂字段；只有业绩预告、披露日历等事件数据，无「催化剂 → 预期差 → 兑现窗口」时间轴 | 观点有效期 7 天复核 / 14 天失效（`publication.py:171-177`）与模板 `horizon` 2-5 年 / 3 年以上严重错配 |
| C6 | **候选层未接组合预算 / 无流动性约束**：同一股票命中多模板时，候选层已按 listing 合并 sources（`candidates/screen_intake.py:176-260`），ranking 也通过 `emitted` 去重（`ranking.py:243-270`）；真正缺失的是跨模板强度可比性、候选排序与组合预算的联动。组合模块另有 mandate 级单票/行业/现金约束（`investor/contracts.py:132-145`），不能笼统称为"无组合风险预算"；5 个模板 screen 条件无一使用成交额 / 自由流通市值，流动性明确未评估 | `ranking.py:198-201` 明确不跨模板比较强度；`portfolio/stress_results.py:37-38,209-211` 标记 `liquidity_not_assessed` |
| C7 | **无会计质量识别 / 一次性损益调整**：只在 `review_lenses.yaml` bear lens 提示词里要求排查应收、存货、现金流与利润背离，无确定性因子；`quality_growth.yaml:70` `data_note` 自承「自由现金流目标池缺失，用毛利率代替」 | 现金流质量被毛利率代理，是概念替换的又一处 |
| C8 | **观点 → 决策 → 真实成交 → 绩效归因闭环断裂**：无券商连接、`funnel-design.md:340` 复盘页待实现、`funnel-design.md:363` 跟随率面板待实现 | 无真实绩效反馈，`calibration_status` 没有升级数据源 |
| C9 | 测试边界：真实数据的 DCF 数值、行业样本（银行/公用事业）、`g>r`、h 边界、`g→r⁻` 近奇异、除权复权、一次性损益、稀释、商誉均无覆盖 | `tests/test_valuation_calculations.py`、`tests/test_fcfe_sources.py:82-83` |

---

## 5. PM 七问对照（用这套系统回答一次）

| PM 七问 | 系统能否回答 | 依据 |
|---|---|---|
| 1. 什么被错误定价了？ | **不能** | 历史分位可算；隐含 h 仅在注入可用 resolver 后才有数值。更关键的是没有独立的「合理预期」基准（一致预期 / 行业增长 / 产能 / 历史转化），无差值即无"错" |
| 2. 当前价格反映了什么？ | 部分 | h 与分位的设计可用；但 r 主观、终值结构无衰减、默认 DCF 无数值输出 → 默认装配下实际只剩自身 5 年分位 |
| 3. 什么能证明论点？ | 能 | `claims` + evidence refs + 关键论据门禁（`publication.py:299-302`） |
| 4. 什么能推翻论点？ | **能，做得好** | `counter_case`、模板 `falsification`、`exit_review`、`Hypothesis.falsification_condition` |
| 5. 为什么是现在？ | **不能** | 无催化剂日历；触发只来自价格分位与因子区间；有效期与持有期错配 |
| 6. 什么会改变仓位/评级？ | 部分 | 有确定性区间事件，但 `add_weight_max` 等阈值未校准，且无组合预算 |
| 7. 还缺什么证据？ | **能，做得好** | `missing_fields` / `factor_coverage` / `gaps` / `[MISSING]` 纪律、AG11 四层覆盖报告 |

`[判断]` 七问中 1、5 两问（恰是"变异认知"与"时机"）失守，3、4、7 三问优秀。**这是一套擅长记录与证伪、但不擅长发现错误定价的系统**——与它「研究辅助」的自我定位一致，问题只在于若被当作「价值分析/决策系统」使用就会越界。

---

## 6. 改进建议（按投入产出排序，均给出代码落点）

### P0 · 先修明确影响结论的代码问题

**前置：先排除 A7 的重述值历史时点泄漏，再做任何历史回测。**至少应让重述值的 `available_from` 不早于实际重述可见日，回补数据不得借原始 `pubDate` 绕过采集时点约束，并把 `restated` 保留到因子/评分层。否则后续 IC 与超额收益检验可能建立在未来信息上。

1. **消除重复计数**：`scoring.py:205-222` 把 `industry_space` / `revenue_growth` 合并为一个 `growth` 因子，`competitive_advantage` / `roe_level` 合并为 `profitability`；或补真正的代理证据（行业空间→行业规模/份额，竞争优势→毛利率或 ROIC 的**行业内分位**）。短期无数据时宁可显式标注 `proxy` 并降权，也不要用两个名字计两次。
2. **修缺失不惩罚**：`scoring_models.py:76-91` 改为「`factor_coverage < 0.75` 时总分置 `None` 或乘以 coverage 折价」；排序键（`scoring.py:876-882`）改为 `score × confidence` 或至少把 `needs_review` / `stale` 降档。
3. **让数据质量进分数**：`scoring.py:627-689` 对 `stale` / `provider_conflict` / `unverified` 明确一致的分档折价或复核策略；`field_resolution.py:83-104,125-126` 保留 `restated` 标记，并按上述前置要求修复 PIT 可见性（解决重述泄漏）。
4. **估值桶重构**：`scoring_models.py:46-73` 改为「行业内分位 + 相对无风险利率的股债性价比」；保持双边未知为 `None`，同时移除单边未知的 100/10 占位；周期/亏损股走 PB 或 EV/EBITDA 路径，不用 PE。
5. **概念对齐**：`scoring.py:262-275` 等因子重命名或补数据；`team_quality=50`、`*_review=60` 从加权中移除或单列为"未评估"，不再虚增 coverage。
6. **重构回撤因子**：`scoring.py:233` 的 `cycle_position` 不能单独用 1 年回撤，需叠加行业价格/产能分位或改为风险折价项；在样本外验证前，不预设它应当正向还是反向。

> 以上任何权重/阈值变更，按项目现有纪律必须升 `SCORING_VERSION` 并附 13 样本回归 diff（`docs/product/engineering-baseline.md:135`）。

### P1 · 校准与验证（完成 P0 后，优先于新增估值模型）

7. **最小可行前瞻检验**：前置条件是排除 A7 的重述泄漏。已有 PIT 快照 + 成分历史（回补含调出股共 439 只，`README.md:262`，已缓解幸存者偏差）。对每个模板命中计算 T+1/3/6 月相对 CSI300 超额收益分布、命中率、IC；按行业/年份分组；用 walk-forward 而非单日横截面调参，并纳入交易成本、停牌/涨跌停等可交易性约束。达标后再把 `calibration_status` 升 `backtested`（`funnel-design.md:365-368`）。
8. **限制单股调参**：保留 `rules/store.py:281-346` 的 draft / 人工 approve 门禁；`rules/drafting.py:395-424` 改为「按行业组历史分布选择离散档位」，输入加横截面分位与历史多期，记录样本期；禁止用 `preference_notes` 直接松紧阈值。在完成样本外验证前，不应仅凭单股单时点命中结果批准参数覆盖。
9. **强度公式**：`rules/screen.py:259-274, 345-354` 改为行业内 z-score 加权（权重来自 IC），去掉 `clamp` 饱和与 `between` 取较近端距离。
10. **组合层**：复用现有跨模板 listing 合并与 ranking 去重，把组合 mandate 的单票/行业/现金约束接入候选排序，并增加流动性门槛（20 日均成交额 / 自由流通市值）；不要重复建设候选去重。
11. **测试补齐**：`g>r`、`h=±边界`、`g→r⁻`、equity_value 缺失、银行/公用事业真实样本、除权复权、一次性损益与稀释。

### P2 · 把「价值 − 价格」这一环补上（以完成基本校准为前提）

12. **给 r 一个可审计锚**：`valuation/contracts.py:18`、`service.py:112-141` 增加 `risk_free_rate` / `erp` / `beta` 或行业分档 + 必填出处；把敏感性网格的结果**汇总成 h 的区间与中位数**输出，而非让模型只引用某一个格子。
13. **终值与路径**：`solver.py:29-33` 增加三阶段衰减（h 逐年向 g 收敛）、stub period（按 `as_of` 与财报日实际间隔折现）；`service.py:340-344` 对 `terminal_pv_fraction > 0.70` 打「低信息量」标记并限制引用。
14. **建立预期差对照组**（高价值、无需先接卖方数据）：把 `h` 与「自身 3/5 年营收及 FCFE CAGR」「申万同行业中位数」「历史现金转化率」并列输出为结构化 `expectations_gap`。这直接补上 PM 第 1 问。
15. **补第二条估值腿**：对红利/公用事业/银行用 **DDM 或 PB-ROE 隐含**（`g = ROE × 留存率` 一致性检验），对制造业用 **EV/EBITDA 同业可比**；目标不是出目标价，而是至少有一个可计算的「合理区间」与安全边际。
16. **接季报 / 自建 TTM**：`factors/library.py:5-9`、`ifind_chain.py:448-489` 目前只取两个年报；接入最新季报、自建 TTM 拼接并登记口径，把年报口径因子的信息滞后压到一个季度内。
17. **修口径**：`factors/library.py:275-319` 杜邦分母改用归母权益或分子改用净利润；保留总市值定义，补公司行为生效日、股本时点与 A/H/多类股处理，并核验普通股归属一致性（`investor/coverage.py:123-124`）。同时补齐杜邦残差超容差的实际降级/引用门禁，或删除文档中尚未实现的阻断承诺。

### P3 · 研究框架

18. 催化剂日历 + `CompanyRecommendation` 增「为什么是现在」与兑现窗口；观点有效期与模板 `horizon` 对齐（当前 14 天 vs 2-5 年）。
19. 确定性会计质量因子（应计率、CFO/净利润背离、扣非归母、商誉/减值、一次性损益识别），替换 `quality_growth.yaml:70` 的毛利率代理。
20. 行业中性化 + 周期股正常化盈利（跨周期平均 ROE / 席勒 PE）；明确标注 `[MISSING]` 的一致预期数据，在补齐前保持价格观点非方向性（这条纪律文档已有，继承即可）。

---

## 7. 一个可以立刻做的验证（判断我的结论是否成立）

`[判断]` 若想用最小成本验证「评分是否真的有效」，建议在现有 13 只 golden 之外做一次离线检验（不改动生产）。**但须先完成 A7 的重述值 PIT 修复或隔离受影响数据，否则检验结果可能含前视偏差。**

1. 取回补窗口内任一时点 `as_of`，用 `score_pool` 对当时的 CSI300 成分（含后来调出的）算 `investment_score`；
2. 按十分位分组，计算各组 T+3 / T+6 月相对等权基准的超额收益与 IC；
3. 同时记录 `factor_coverage`、`needs_review` 标记，检验「低覆盖组是否被系统性高估」；
4. 若十分位单调性不成立或 IC 接近 0，则 P0 各项必须优先于任何新估值模型开发。

这一步不需要新数据源，正好用上项目最引以为傲的 PIT 快照积累，且能把「`uncalibrated`」从免责声明变成可证伪的结论。

---

## 8. 边界与未核验项

- `[MISSING]` 本次未运行系统、未连数据库，未核验 `data/*.db` 中真实快照的字段覆盖率、受重述回补影响的记录范围与实时运行状态；对「默认 DCF 无数值输出」的判断基于代码默认 resolver、生产注册链路与 2026-10-10 的 4 只固定样本 AG11 探针，**不排除部署环境注入了自定义 resolver，也不据此断言全池覆盖为 0**。
- `[MISSING]` 未核验 `publication.py` 之外是否另有把估值数字写入仓位/评级路径；调研结论是候选排序与组合仓位均不引用 `valuation/`。
- `[MISSING]` 未逐行审计 `rules/screen.py` 全部条件求值、`review_panel` 与 `simulation` 的全部实现，相关结论引用自代码定位与项目文档。
- `[判断]` 本文对「价值分析」的评判采用卖方/买方研究标准（内在价值、预期差、样本外校准、组合风险）。若产品定位严格限定为「证据管理与研究优先级排序」，则 B5、C5、C6 属于**范围外**而非缺陷；A1–A7、C1–C2 在其自我定位内依然是缺陷。
- 本文所有数字（权重、阈值、占比、命中数）均引自仓库内代码或文档，未引入外部行情或预测数据。

---

**本报告仅供研究参考，不构成个人投资建议。**
