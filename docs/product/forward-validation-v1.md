# 前瞻检验 v1：冻结评分与规则模板的离线可复现评估

日期：2026-11-08。状态：**离线工具已实现，合成数据测试通过；文件模板的真实库只读实跑已完成
（`--template-source files`，状态 `ok`）。仅数据库模板路径受本地库 schema 048 限制而未能运行；
样本量与统计推断仍不足，正式结论尚未得出**。

依据：[估值方法论评审](../../docs/review/valuation-methodology-review-2026-11-07.md) 第 6 节 P1 第 7 项，
以及[下一轮实施评审](plans/next-iterations-2026-11-07/review.md)第 3 节“评分回测另立阶段”的边界。

代码：`src/investment_agent/research/forward_validation/`（`contracts.py`、`stats.py`、`prices.py`、
`benchmark.py`、`evaluation.py`、`dataset.py`、`runner.py`），CLI 见
`src/investment_agent/cli/commands/forward_validation.py`。

**这个工具不训练、不调参、不写库、不改 `calibration_status`。** 它只回答一个问题：
在冻结的评分 `v1.4` 与现有模板版本上，当时可见的横截面在可成交、含成本的口径下是否表现出
可复现的排序能力，以及现有样本是否足以支持这个判断。

---

## 1. 结论口径

| 报告状态 | 含义 |
|---|---|
| `ok` | 每个 `as_of` 横截面都有 ≥ `min_scored` 个有分成员；每个 horizon 都有 ≥ `min_scored` 个可定价观测；至少 2 个 `as_of` 组成 walk-forward 序列；有模板时至少一个模板达到 `min_template_hits`；无评分失败、无基准缺口 |
| `partial` | 采样达标，但存在单点不足（例如某模板命中数不足、若干评分失败、个别基准缺口）；不因结果方向而升级或降级 |
| `insufficient` | 任一硬性条件不满足。**不得**据此升级 `calibration_status`；报告逐条列出原因 |

判定只看**样本量与覆盖率**，与观测到的符号、IC、单调性无关：一个足够大但方向为负的样本
仍是 `ok`，一个很小的正 IC 仍是 `insufficient`。这避免用结果反推结论。

报告永远写入 `guard.calibration_status_changed = false`；模板 `calibration_status` 只作为输入
快照复制到 `templates.calibration_status`，工具没有任何写回路径。

## 2. 数据时点（硬性口径）

1. **成员是 PIT 成分，不是当前成分。** 每个 `as_of` 用
   `UniverseRepository.members_as_of(universe, as_of)` 读取当时有效的 `universe_membership`
   区间，包含后来调出的股票。本地回补库含 2021-10-04 起 461 条 CSI300 成员区间，其中 161 条已调出；
   `as_of=2024-06-28` 读出 300 只当时成分。
2. **评分走生产路径。** `load_snapshot_view`（`TARGET_POOL` 策略）→ `build_snapshot_bundle` →
   `score_pool`，`as_of` 时间戳固定为当日 23:59:59（Asia/Shanghai）。
   财报可见性沿用 `available_from <= as_of`、`unknown` 排除、非 `backfill` 行需 `captured_at` 早于日终；
   重述行由工作区已实现的 `effective_available_from` 后移。
3. **前视自检。** 每只成员记录评分输入中出现的最晚日期（最新 bar 交易日、最新年报
   `available_from`、各载荷 `updated_at`），并要求 `<= as_of`。报告写
   `lookahead_check.violations` 与 `passed`；数据集加载时若发现更晚日期直接抛
   `LookaheadError`，不降级、不猜测。
4. **行情用项目已有复权序列。** 收益取 `market_snapshot.adjust='backward'`（后复权），
   未存后复权时回退原始价并在 `market_data.series_sources` 记 `raw_fallback` 数量。
   涨跌停判断用**原始价**（交易所按原始价计算），收益计算用后复权价。
5. **行情知识时间。** 前向行情用固定哨兵 `PRICE_KNOWLEDGE_SENTINEL = 2100-01-01` 读取：
   收盘价一旦收盘即定稿、不会重述，因此历史报告不能随运行日期漂移。该值写入报告。

## 3. 可成交与成本

**入场是 T+1，不是信号日收盘。**

* 信号日为 `as_of` 当日（或之前）最后一个市场交易日。
* 入场为信号日**之后**第一个可买交易日，价格为该日**开盘价**（后复权）。
* 停牌与涨停开盘不可买；逐个交易日递延，超过 `max_entry_deferral`（默认 5）记为
  `entry_untradable`，**不成交、不进入统计**。
* **成交量为 0 一律不可交易**，无论 `trade_status` 写的是什么（数据自相矛盾时宁可等待，
  也不把 0 成交量的 bar 当作可成交证据）。
* 涨跌停幅度按板块：主板 10%、创业板/科创板 20%、北交所 30%；主板 ST 为 5%。
  限价按 0.01 元四舍五入，半个最小价位内视为“在限价”。
* **涨跌停基准是“该股当日之前最后一个实际收盘价”**，不是“前一市场交易日收盘价”。
  长期停牌复牌时前一市场日没有该股 bar，此时以停牌前最后一个收盘价（复牌参考价）计算；
  只有完全没有历史收盘价的新上市首日才记 `limit_unknown` 并放行。复牌涨停不可买、
  复牌跌停不可卖，均有独立测试。

**出场是固定交易日数。**

* 1/3/6 月 = **21/63/126 个市场交易日**（`HORIZON_SESSIONS`），从入场会话起算。
* 交易日来自**全部成分行情交易日的并集**，不是个股自身 bar 序列：个股停牌不会缩短窗口。
* 出场取该日**收盘价**；该日停牌或收在跌停（卖方难以成交）则向后递延，超过
  `max_exit_deferral` 记为 `exit_untradable`。
* 数据末尾尚未到期的窗口记 `pending_horizon`，**不是** 0 收益。

**成本是显式假设，不是实测。**

```
buy_factor  = 1 + (commission_bps + slippage_bps)/10000
sell_factor = 1 - (commission_bps + slippage_bps + stamp_duty_bps)/10000
net_return  = (1 + gross) * sell_factor / buy_factor - 1
```

默认 `commission_bps=3`、`slippage_bps=10`、`stamp_duty_bps=5`（卖出单边），
往返 31 bp，写入 `cost_model.round_trip_bps` 并注明“假设，非实测”。
所有分桶、IC、命中率只用**净收益**；信号日收盘到出场的毛收益只用于
`mean_signal_minus_net`（“把信号收益当可成交收益会高估多少”），不参与评估结论。

## 4. 指标与分组

每个 `as_of × horizon` 输出：

* **分桶**：按评分从高到低等分。样本足够时优先十分位，退化顺序为 5 组、3 组；
  任一组小于 `min_group_size` 则该组标 `below_min_group_size`，且单调性判定为
  `insufficient`，绝不静默丢组。桶 `b1` 是最高分。
* **超额收益**：`净收益 - 基准净收益`。默认基准为**等权基准**：该 `as_of` 的全部成分、
  同样的 T+1 入场、同样的交易日数与成本模型，按 `as_of` 生成一条窗口收益并施加给全体成员；
  因此超额收益对停牌个股的入场递延保持内部一致。基准窗口的 `constituents` /
  `priced_members` 写入 `market_data.benchmark_detail`，不同 `as_of` 的成分不会被并成一股。
  本地库没有 CSI300 指数行情，`--benchmark csi300` 会显式返回“基准不可用”，不会当作 0。
* **命中率**：超额收益 > 0 的比例。
* **Spearman IC**：同一 `as_of` 横截面内评分与净超额收益的秩相关（并列取平均秩）。
  跨 `as_of` 的均值在聚合层单独给出，不被当作时序 IC。
* **单调性**：按分数从低到高，相邻桶均值不下降为 `strict=true`；同时给出桶序与桶均值的
  Spearman。任一组不足即 `insufficient`。
* **分组**（彼此独立）：
  * `coverage_groups`：`high`（覆盖率 ≥ 0.9）/ `mid`（`min_coverage` ≤ 覆盖率 < 0.9）/
    `low`（< `min_coverage`）/ `suppressed`（未发布分数），互斥且覆盖全部成员；
    被抑制成员的前瞻收益仍然计算，否则无法回答“低覆盖/未发布分数的组是否被系统性高估”；
  * `review_groups`：`needs_review` vs `no_review`，与覆盖率完全独立的一根轴；
  * `industry_groups`：按 `as_of` 当日行业组（CSRC 回退标 `:fallback`）分组，
    `min_group_size_met` 明确标出样本不足。

**模板命中评估**：用 `rules.screen.run_screen` 在信号日对当时成分重放模板 `screen`，命中即
`hit`；比较命中组与未命中组的净超额收益（`spread_vs_non_hits`）与命中数，命中数不足
`min_template_hits` 标 `sufficient=false`。命中评估使用**入场 T+1 净收益**，
不使用信号日收益，也不把模板 `strength` 当作收益。

## 5. walk-forward 与调参禁令

* 支持一次传入多个 `--as-of`，**逐横截面**独立评估，不把多个日期拼成一个样本池再调阈值。
* 所有阈值来自 `ValidationConfig` 固定默认值，写入 `config_hash`；没有“看结果再改阈值”的入口。
* 单日结果在 `partial` 之上不足以称 `ok`：判定要求至少 2 个 `as_of`，报告里明确
  “a single as_of cross-section is a snapshot, not a walk-forward series”。
* 评分版本校验：报告记录模块内 `SCORING_VERSION`、期望值（`v1.4`）与 `matches`；
  版本不符时状态直接为 `insufficient`（除非显式 `allow_scoring_version_mismatch`），
  避免把结果误归因到 v1.4。

## 6. 输出内容

一次运行写一份 JSON（`--output`），包含：

| 字段 | 内容 |
|---|---|
| `status` / `insufficient_reasons` | 结论与逐条原因 |
| `sample` | 样本期、市场交易日首末与数量、各 `as_of` 成员数/有分数、总观测/有分/失败数、各 horizon 交易日数 |
| `config` / `config_hash` | 全部冻结参数（哈希可复算） |
| `cost_model` / `benchmark` | 成本假设与基准构造、局限 |
| `scoring` / `templates` / `code_version` | 评分版本与匹配、模板版本与哈希/路径、内核版本、本包源码哈希 |
| `database` | 路径、`read_only=true`、`alembic_version` |
| `market_data` | 复权来源、知识哨兵、会话覆盖（是否覆盖到各 horizon 终点、大缺口清单）、基准窗口明细 |
| `lookahead_check` | 违规清单与 `passed` |
| `failures` | 每只评分失败成员的阶段与原因（不删样本） |
| `diagnostics` | 每个 `as_of` 的成员数、评分输入日期、模板命中计数 |
| `cross_sections` | 每个日期的 `horizons`、`coverage_groups`、`review_groups`、`industry_groups`、`templates` |
| `guard` | `calibration_status_changed=false` 与说明 |

失败与未到期观测保留在分母里：`scored` / `evaluated` / `pending_horizon` / `untradable` /
`missing_excess` / `unresolved` 分别计数，不用“只看可用样本”掩盖缺口。每个 horizon 都满足
`evaluated + pending_horizon + untradable + missing_excess + unresolved = scored`；
`unresolved_statuses` 列出任何未归入上述类别的 outcome 状态，避免未来新增状态静默漏计。

## 7. CLI

```bash
# 只读运行（数据库以 SQLite mode=ro + PRAGMA query_only 打开，不迁移、不写入）
.venv/bin/ia forward-validation run \
  --database .dev/data/app.db \
  --as-of 2024-06-28 --as-of 2024-12-31 --as-of 2025-06-30 \
  --horizon 1 --horizon 3 --horizon 6 \
  --template-source files \
  --output /tmp/forward-validation.json

# 复看已保存报告（不访问数据库）
.venv/bin/ia forward-validation show /tmp/forward-validation.json
```

常用选项：`--universe CSI300`、`--benchmark equal_weight|csi300`、`--template-source db|files`、
`--template <ref>`（可重复）、`--min-scored`、`--min-group-size`、`--min-template-hits`、
`--commission-bps`、`--stamp-duty-bps`、`--slippage-bps`、
`--max-entry-deferral`、`--max-exit-deferral`、`--no-backfill`。

退出码：`0` 表示 `ok`/`partial`；`2` 表示 `insufficient`（报告仍写出）。CLI 不读取
`DATABASE_URL`、不读 `.env`，只操作 `--database` 指定的文件。

## 8. 测试

* `tests/unit/test_forward_validation_stats.py`（28 项）：平均秩与 Spearman（含并列、零方差）、
  分桶数量与排序、单调性 `strict/insufficient`、板块涨跌停幅度、停牌与涨停递延、
  固定交易日窗口、`pending_horizon`、跌停出场递延、出场递延耗尽（`suspended` / `missing_bar` /
  `limit_down`）统一为 `exit_untradable` 且保留 reason、成本因子与往返 31 bp；
  另含三条可成交边界：长期停牌复牌日以停牌前最后一个实际收盘价为涨跌停基准
  （复牌涨停不可买、复牌跌停不可卖），以及 `trade_status=1` 但成交量为 0 仍不可交易。
* `tests/unit/test_forward_validation_runner.py`（11 项）：用合成 PIT 库跑真实 `load_dataset`
  路径，验证
  * 已知单调信号 → 正 IC、严格单调、顶底差为正；
  * 无信号横截面 → 不判为严格单调、IC 接近 0；
  * `as_of` 之后才公开的年报不进入评分（`max_input_date <= as_of`、`lookahead_check.passed`）；
  * 未来输入被 `lookahead_check` 标为违规；
  * 停牌入场在 `max_entry_deferral=1` 时 `entry_untradable`、放宽后在第 4 个交易日成交；
  * 成本为正时净收益下降，且因基准同成本而对超额收益中性；
  * 样本不足 → `insufficient` 且 `calibration_status_changed=false`；
  * `valid_to` 早于 `as_of` 的成员被排除、`valid_to` 晚于 `as_of` 的成员仍纳入；
  * 无行情成员记为 `failed` 并进入 `total_failed`，整体 `partial`；
  * `config_hash` 稳定且对参数敏感。
* `tests/unit/test_forward_validation_report.py`（12 项，纯函数）：充分性只由样本量/覆盖决定
  （负 IC 不降级、单日期最多 `partial`、`min_scored` 不足为 `insufficient`、未到期 horizon
  为 `insufficient` 而非 0）、评分版本不符可见、覆盖率四组互斥且覆盖全部有分成员、
  平坦信号不判为负单调、聚合层池化桶均值、成本进入配置但超额收益中性、
  评分版本不符默认判 `insufficient`（只有显式 `allow_scoring_version_mismatch` 才放行）、
  `exit_untradable`/`no_price` 等状态全部进入 `untradable`/`unresolved` 且分母恒等式成立。
* `tests/unit/test_forward_validation_cli.py`（6 项）：`mode=ro` URL、缺失文件不创建、
  缺失库与未迁移库都输出 `insufficient`（退出码 2）、`show` 不访问数据库、非法 horizon 被拒绝。

合计 **57 项，全部通过**（离线；无网络、无真实库写入）：

```bash
.venv/bin/python -m pytest -q tests/unit/test_forward_validation_stats.py \
  tests/unit/test_forward_validation_report.py tests/unit/test_forward_validation_runner.py \
  tests/unit/test_forward_validation_cli.py
```

## 9. 实际运行结果（本地只读，2026-11-08）

**数据库**：`.dev/data/app.db`（382 MB，`alembic_version=048`，本代码 head 为 `066`），
以 `mode=ro` + `query_only` 打开；未迁移、未写入。

**可运行范围**：文件模板路径已实跑（见第 2–4 条）；数据库模板路径因缺少
`rule_template.created_by` 列而只返回显式 `insufficient`（第 1 条），不影响评分与行情口径。

1. `--template-source db`：**未完成**。工具检测到 schema 落后，返回
   `status=insufficient`，原因 `no such column: rule_template.created_by`，并提示在副本上执行
   `ia migrate up` 或改用 `--template-source files`。这是预期的显式失败，不是静默跳过。
2. `--template-source files --as-of 2024-06-28 --horizon 1 --horizon 3`：**已运行**。
   * 成员 300（PIT 成分），有分 280，失败 0，耗时约 30 秒；
   * 市场交易日 2022-09-27..2025-08-22，共 704 个（窗口需 640 天历史 + 420 天前瞻）；
   * 1 月：evaluated 280，IC ≈ **-0.1845**，顶底差 ≈ **-0.0483**，命中率 0.507，
     单调性 `strict=false`；3 月：IC ≈ **-0.1113**，顶底差 ≈ **-0.0878**，命中率 0.425，
     `strict=false`；
   * 信号日收益与可成交净收益之差（`mean_signal_minus_net`）1 月约 +0.16 pp、3 月约 +0.18 pp：
     把信号日收盘当入场会系统性高估。
   * `insufficient_reasons` 仅剩“单一 `as_of` 不构成 walk-forward”，故状态 `partial`；
   * 模板命中（1 月，spread vs 非命中）：`dividend_blue_chip@1` +2.6%（5 只）、
     `high_growth_venture@1` +2.6%（21 只）、`quality_growth@1` -2.5%（27 只）、
     `cycle_reversal@1` -2.9%（11 只）、`steady_quality_growth@1` -4.0%（11 只）；
     3 月 `high_growth_venture@1` +6.1%、`quality_growth@1` +1.9%，其余为负。
3. 两个 `as_of`（2024-06-28、2024-12-31）跑通，状态 `ok`，耗时约 60 秒：
   有分 556/600（92.7%），两次都无评分失败。
   * 2024-06-28：1 月 IC -0.185 / 顶底差 -0.048 / 命中率 0.507；3 月 IC -0.111 / -0.088 / 0.425；
   * 2024-12-31：1 月 IC -0.064 / 顶底差 **+0.015** / 命中率 0.393；3 月 IC **+0.106** / **+0.032** / 0.487；
   * 覆盖率分组（3 月净超额）：2024-06-28 `high` 230 只 -0.90%、`mid` 50 只 +4.68%、
     `suppressed` 20 只 -1.33%；2024-12-31 `high` 221 只 +0.03%、`mid` 54 只 +0.00%、
     `suppressed` 24 只 -0.27%。第一个日期 `high` 明显弱于 `mid`，第二个日期三者差异都在
     0.3 pp 以内、方向与前一次不一致；两次样本都不支持稳定的“覆盖率越高越好”或
     “低覆盖被系统性高估”，但样本量不足以定论；
   * 所有日期、所有 horizon 的严格单调性均为 `false`：IC 与顶底差的符号在不同日期之间翻转，
     不支持“评分排序稳定有效”的结论，也不支持相反方向的结论。
   * 可成交边界修复（复牌涨跌停基准、零成交量）后**重跑同一配置，报告字段逐项对比 0 差异**：
     该样本的入场/出场窗口没有落在复牌涨跌停日；实际发生的递延为 2 次入场、5 次出场，
     另有 1 次出场停牌、1 次出场缺 bar 被排除。
4. 三个 `as_of`（增加 2025-06-30）也跑通（状态 `ok`，约 72 秒），6 月 horizon 可定价：
   IC 1 月 -0.185 / -0.064 / -0.281，3 月 -0.111 / +0.106 / -0.134，6 月 -0.092 / +0.050 / -0.077。

**这些是样本内、单库、单评分版本的观测事实，不是“评分无效”的定论。** 需注意的局限见下。

## 10. 局限（必须与结果一起引用）

1. **只有数据库模板路径受本地库 schema 048 限制**：缺 `rule_template.created_by` 等列，
   因此模板只能从文件读取；文件模板的实跑（第 9 节第 2–4 条）已完成。数据库模板版本与
   `as_of` 时点的绑定关系无法在本库验证。
2. **本地库无 CSI300 指数行情**：只能使用等权基准；真实指数基准需要先回补指数序列。
3. **覆盖期**：指数成分自 2021-10-04，行情至 2026-08-24；`as_of` 较早时 6 月窗口可用，
   `as_of` 过近时自动 `pending_horizon`。当前样本只覆盖约 4 年、3 个横截面，
   远不足以宣称统计显著。
4. **成本为假设**：未按个股流动性、冲击成本或实际券商费率标定；小市值/低流动性标的的真实
   成本很可能高于 31 bp。
5. **复权与公司行动**：收益使用库内后复权序列；退市、长期停牌、并购换股等公司行动如果
   在库内表现为价格跳变而非缺失，会进入收益而不被单独标记。
6. **快照数据质量**：本地快照以 baostock 为主、iFinD 少量核验，绝大多数成员
   `needs_agent_review=true`（未核验），因此 `review_groups` 几乎只有 `needs_review` 一支，
   `no_review` 为空。`coverage_groups` 的 `low` 组为空、`suppressed` 组仅 20/24 只；
   分组结论需要更完整的核验数据才能解释。
7. **模板重放使用当前模板版本**，不是历史版本；`calibration_status` 全部为 `uncalibrated`。
8. **份额与稀释**：收益是每股价格口径，未处理增发、可转债转股等对每股价值的稀释。
9. **不评估研究质量**：本工具只检验评分排序与模板命中，不评价 Agent 研究内容或观点正确性。
10. **没有统计学推断**：没有置信区间、显著性检验或多重比较校正；`n` 小、日期少时
    应把结果当作描述性诊断。

**在以上限制被逐条解决、且样本达到预先声明的门槛之前，不得依据本工具输出升级任何
`calibration_status`，也不得把结果用于方向性仓位建议。**

## 11. 后续（不在本次范围）

* 在数据库副本上迁移到当前 head，使 `--template-source db` 可用并冻结模板版本绑定。
* 回补 CSI300 指数日线与 2021 年前的成分历史，扩大 walk-forward 段数。
* 成本按成交额分层标定；增加流动性门槛（20 日均成交额 / 自由流通市值）。
* 行业中性化与多期滚动，加入 bootstrap 置信区间（需先固定算法版本，避免用结果调参）。
* 与 P0 的重述 PIT 修复联动审计受影响样本范围（当前工具只消费已修复的可见性规则）。
