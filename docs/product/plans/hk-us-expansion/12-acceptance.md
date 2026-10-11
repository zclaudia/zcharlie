# 12 验收矩阵与文档收口

> 波次 W5 ‖ 可与 11 并行（本任务只写文档与跑验收，不碰 `web/**`）。
> 依赖：全部核心任务（01～11）已合并。**本任务必须诚实**：没跑过、没覆盖的用例写"未覆盖"，不得写成通过。
>
> **实际执行状态（重要）**：本任务执行时集成分支 HEAD = `d2ac41e merge: 09`，即**01～10 已合并、
> 11 尚未合并**（`hk-us/11`、`hk-us/13`、`hk-us/14` 三个分支都仍停在 `d2ac41e`，无提交）。
> 因此凡属 11/13/14 的测试文件本任务一律按"未覆盖，由控制 agent 在最终门禁确认"处理，见 §3。
>
> 本文件是**覆盖式重写**的产物：上表 10 行来自本子计划原稿，其后追加"实测结果"列与
> 「未覆盖清单」；§2 起是本次实测的命令与原始结果。

**目标：** 把设计 §实施阶段与验收 的 10 条强制验收用例逐条映射到可执行的测试；补齐产品文档与 README；跑一次完整门禁并记录数字。

## 文件清单

```
新增  docs/product/hk-us-expansion.md
新增  docs/product/plans/hk-us-expansion/12-acceptance.md   （本子任务的产物，覆盖式重写）
修改  README.md                                              （HTTP API 表 + 文档索引 + 功能一句话）
```

## 1. 验收映射（逐条给出"测试文件::测试函数"与运行结果）

| # | 设计要求 | 期望落点 | 实测结果 |
| --- | --- | --- | --- |
| 1 | 同名公司、代码前导零、CIK 与股票类别不混用；错误身份不能自动建档或合并 | `tests/unit/test_security_identity.py` | **通过**：34 passed / 0 failed（§2.1）。落点用例：`test_pilot_list_has_the_eight_checked_identities`、`test_pilot_list_binds_source_codes_per_source`、`test_normalize_ticker_stores_each_market_canonical_form`、`test_same_name_with_different_company_keys_never_merges`、`test_ambiguous_same_name_companies_are_refused`、`test_existing_listing_pointing_elsewhere_is_refused_and_kept`、`test_one_listing_cannot_be_claimed_by_two_company_keys` |
| 2 | 汇丰信用损失前后收入、普通股与其他权益持有人利润分开；港币报价、美元报表、人民币展示值不串用 | `tests/unit/test_material_hk_native.py`、`tests/unit/test_material_metrics.py` | **通过**：27 + 41 passed / 0 failed（§2.2）。落点用例：`test_hsbc_uses_the_net_credit_revenue_series`、`test_cash_conversion_ratio_uses_the_ordinary_shareholders_profit`、`test_all_parent_profit_does_not_replace_the_ordinary_figure`、`test_clp_and_tencent_amounts_stay_in_their_report_currency`、`test_currency_mismatch_blocks_only_its_table`、`test_display_currency_marker_rejects_the_whole_batch` |
| 3 | 中电与腾讯原币表、真实当期列和支出符号正确；不得误生成统一 FCF | 同上（含 `sign_convention` 三态断言） | **通过**：27 + 41 passed / 0 failed（§2.2）。`sign_convention` 三态断言：`test_capex_sign_conventions_stay_issuer_specific`（`assert len(set(texts.values())) == 3`：汇丰 `net purchases`／中电 `capital expenditure`／腾讯 `prepayments`），配套 `test_hsbc_capex_is_not_applicable_for_a_bank`、`test_tencent_capex_definition_is_rejected_by_name`、`test_clp_fcf_is_operating_cash_flow_minus_capex`；聚焦子集 1 passed / 0 failed（§2.6） |
| 4 | AAPL/COST 非自然年与 52/53 周；JPM 缺 capex 不填零；XOM 收入定义不替换 | `tests/unit/test_material_us_sec.py`、`tests/unit/test_material_metrics.py` | **通过**：57 + 41 passed / 0 failed（§2.3、§2.2）。落点用例：`test_aapl_52_53_week_years_keep_their_true_lengths`、`test_cost_fiscal_year_is_not_a_calendar_year`、`test_non_annual_forms_never_define_a_fiscal_year`、`test_jpm_missing_capex_is_missing_inputs_never_zero`、`test_jpm_missing_capex_is_null_with_a_reason_not_a_nearby_tag`、`test_xom_revenue_operating_binding_is_the_standard_revenues_concept`、`test_xom_sales_only_tag_does_not_become_revenue_operating`、`test_cagr_uses_the_real_apple_52_53_week_boundaries` |
| 5 | LNC 原始 / 不可依赖 / 修订三时点状态正确；日期过滤不绕过接受时间 | `tests/unit/test_material_us_sec.py` | **通过**：LNC 相关子集 14 passed / 0 failed（§2.4）。落点用例：`test_lnc_state_machine_sees_the_documented_time_points`、`test_lnc_state_machine_matches_the_saved_cutoffs_file`、`test_lnc_revision_replaces_the_original_without_using_is_restated`、`test_lnc_conflict_keeps_every_candidate_and_computes_no_growth`、`test_superseded_period_keeps_the_old_value_when_the_amendment_has_no_facts`、`test_unreliable_notices_are_exposed_from_the_submissions_document`、`test_filing_without_any_date_counts_as_known_at_every_cutoff`、`test_period_without_its_own_annual_report_is_not_filed` |
| 6 | TSM TWD、美元便利换算、ADS 与普通股分开；不同报告利润不拼接 | `tests/unit/test_security_identity.py`、`tests/unit/test_material_us_sec.py`、`tests/unit/test_material_valuation.py` | **通过**：34 + 57 passed（§2.1、§2.3）；落点用例 `test_validate_requires_ads_ratio_and_a_source_of_the_market`、`test_tsm_is_ifrs_twd_without_any_usd_conversion`、`test_tsm_profit_metrics_only_read_the_20f_series`、`test_tsm_ads_roe_is_not_applicable_but_other_metrics_are_allowed`、`test_currency_change_is_not_converted`。`tests/unit/test_material_valuation.py` 由控制 agent 在 13 合并后确认（24 passed）：`test_ads_profile_is_not_applicable_with_the_ratio_and_fx_gaps` 断言 TSM 的 ADS PE 即使注入价格与当日汇率也恒 `NOT_APPLICABLE`，并列出比例/比例有效期/当日汇率/股数基础四项缺口 |
| 7 | 苹果拆股后价格不直接匹配拆股前 EPS；汇丰最终 HKD 分红在确定日之前不能成为已知现金事实 | `tests/unit/test_material_valuation.py`、`tests/unit/test_material_dividends.py` | **通过（控制 agent 在最终门禁确认）**：两文件 24 + 41 passed / 0 failed，聚焦子集（拆股 + 分红时间线）9 passed。落点用例：`test_split_between_eps_period_and_price_is_not_applicable`、`test_split_consistent_inputs_can_compute`（苹果 2020-08-31 4:1）、`test_hsbc_final_amount_is_only_known_on_its_determination_date`（2025-03-06 / 03-07 / 03-10 / 04-13 一律 `declared:USD 0.36`，04-14 起才是 `final:HKD 2.791007`）、`test_ex_date_alone_cannot_be_the_available_date`（只有 ex_date 的事件 `available_from is None`） |
| 8 | 只有日期或未知公布时间的输入保留精度与缺口；导入旧材料不回填取得时间；新证据不改写旧研究 | `tests/unit/test_material_store.py`、`tests/unit/test_material_evidence.py`、`tests/integration/test_material_offline_import.py` | **通过**：12 + 24 + 9 passed / 0 failed（§2.5）。落点用例：`test_date_precision_is_never_earlier_than_the_acceptance_time`、`test_unknown_publication_time_stays_in_the_material_layer`、`test_missing_sidecar_keeps_the_fetch_time_unknown`、`test_sidecar_without_fetched_at_utc_does_not_back_fill`、`test_default_import_time_is_now`、`test_changed_payload_is_a_new_revision_and_the_old_row_is_untouched`、`test_timestamps_do_not_change_the_version` |
| 9 | HTTP 200 错误正文、分页截断、重复字段、行情异常、FX 日期越界、断网与 schema 变化都有明确状态 | `tests/unit/test_material_hk_native.py`、`tests/unit/test_material_us_sec.py`、`tests/integration/test_material_collection_job.py`；**行情/FX 首期未启用 → 在矩阵里写"能力未启用，缺口可见"并给出 `capabilities` 断言位置** | **通过（行情/FX 除外）**：27 + 57 + 14 passed / 0 failed（§2.2、§2.3、§2.7）。落点用例：`test_upstream_error_body_is_a_failure_and_does_not_raise`、`test_pagination_truncation_is_reported`、`test_duplicate_item_code_blocks_the_table`、`test_invalid_amount_is_reported_and_not_invented`、`test_unexpected_period_is_reported`、`test_html_body_is_a_structural_problem`、`test_missing_facts_block_is_a_structural_problem`、`test_a_transient_failure_is_retried_and_the_finished_source_is_kept`、`test_structural_problems_stay_partial_and_are_not_retried`。**行情异常与 FX 日期越界：能力未启用，缺口可见**——`price` 是 `CONDITIONAL_CAPABILITIES` 成员、FX 首期不启用，缺口由 `capabilities` 断言可见，断言位置见 §2.9 |
| 10 | 最新实际年报验收；缺价格仍可财务研究；许可未确认仍关闭在线源；A 股取数、评分、证据与任务回归 | `tests/integration/test_api_research_materials.py`、`tests/integration/test_material_collection_job.py`（`online_refresh_disabled`）、本任务的全量回归；**"最新实际年报"需联网 → 记为遗留，不得标为通过** | **部分通过**：18 + 14 + 22 passed / 0 failed（§2.8、§2.7），全量回归见 §4。「缺价格仍可财务研究」由 `test_material_uses_real_fiscal_years_and_keeps_price_missing` 覆盖；「许可未确认仍关闭在线源」由 `test_refresh_reports_the_disabled_online_refresh`（`capabilities["online_refresh"] is False`）与 `test_default_offline_branch_makes_no_upstream_call`（`online_refresh_disabled`）覆盖。「**最新实际年报验收：未覆盖：需联网补验**」——本地证据只覆盖 2021–2024，见 §3 |

**并行任务交付的行（已由控制 agent 在最终门禁确认，2026-10-12 03:54）**：上表 7 与第 11/13/14 任务的测试
（`tests/integration/test_web_stock_financials.py`、`tests/unit/test_material_valuation.py`、
`tests/unit/test_material_dividends.py`）在本任务执行时尚未存在，现已由 11/13/14 合并交付。
控制 agent 在集成分支（HEAD `7c8ad28`）上复跑确认：

```
$ PYTHONPATH=$PWD/src python -m pytest -q tests/unit/test_material_valuation.py \
      tests/unit/test_material_dividends.py tests/integration/test_web_stock_financials.py
79 passed in 5.61s

$ PYTHONPATH=$PWD/src python -m pytest -q tests/unit/test_material_valuation.py -k "split" \
      tests/unit/test_material_dividends.py -k "hsbc or split or final"
9 passed, 56 deselected in 0.94s
```

因此上表 6、7 两行改判为**通过**；task 11 的页面回归由 `test_web_stock_financials.py` 14 passed 覆盖
（控制 agent 未做浏览器像素级复核，见 COMPLETION.md 未结事项 6）。

本任务执行时三个文件确实尚不存在的可复核证据（当时的集成分支 HEAD = `d2ac41e merge: 09`）：

```
$ ls tests/unit/test_material_valuation.py tests/unit/test_material_dividends.py \
      tests/integration/test_web_stock_financials.py
ls: tests/integration/test_web_stock_financials.py: No such file or directory
ls: tests/unit/test_material_dividends.py: No such file or directory
ls: tests/unit/test_material_valuation.py: No such file or directory
（exit=1，三个均不存在）

$ for b in hk-us/11 hk-us/13 hk-us/14; do git log --oneline -1 "$b"; \
    git ls-tree -r --name-only "$b" | grep -E "test_web_stock_financials|test_material_valuation|test_material_dividends"; done
hk-us/11: d2ac41e merge: 09        （无目标测试文件）
hk-us/13: d2ac41e merge: 09        （无目标测试文件）
hk-us/14: d2ac41e merge: 09        （无目标测试文件）
```

即当时的三个分支都仍停在集成分支起点、尚无提交；本任务对其**不判定通过，也不判定失败**（现由控制 agent 补判）。

## 2. 分节点实测（命令与原始结果）

环境（`00-conventions.md` 门禁约定）：解释器 `/Users/zhvala/SourceCode/zcharlie/.venv/bin/python`，
`PYTHONPATH=/tmp/zc-hkus-12/src`，`DATA_DIR=/tmp/zc-hkus-gate-12-target/data`，
`DATABASE_URL=sqlite+aiosqlite:////tmp/zc-hkus-gate-12-target/t.db`，
`API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3`。
测试环境未安装 `pytest-randomly`（`pytest --version` = 9.1.1，`pip list` 无 random/xdist），
用例顺序确定，可复现。

### 2.1 身份（用例 1、6）

```
$ python -m pytest -q tests/unit/test_security_identity.py
..................................                                       [100%]
34 passed in 1.98s
```

### 2.2 港股口径（用例 2、3）与指标（用例 2、3、4）

```
$ python -m pytest -q tests/unit/test_material_hk_native.py
...........................                                              [100%]
27 passed in 1.43s

$ python -m pytest -q tests/unit/test_material_metrics.py
.........................................                                [100%]
41 passed in 1.14s
```

### 2.3 美股口径（用例 4、6）

```
$ python -m pytest -q tests/unit/test_material_us_sec.py
.........................................................                [100%]
57 passed in 1.47s
```

### 2.4 LNC（用例 5）

```
$ python -m pytest -q tests/unit/test_material_us_sec.py \
    -k "lnc or filing_without_any_date or without_its_own_annual_report or superseded"
14 passed, 43 deselected in 0.97s
```

（该子集是 `test_material_us_sec.py` 的 57 passed 的真子集，不是额外用例。）

### 2.5 精度与时间（用例 8）

```
$ python -m pytest -q tests/unit/test_material_store.py
............                                                             [100%]
12 passed in 1.94s

$ python -m pytest -q tests/unit/test_material_evidence.py
........................                                                 [100%]
24 passed in 2.77s

$ python -m pytest -q tests/integration/test_material_offline_import.py
.........                                                                [100%]
9 passed in 2.49s
```

### 2.6 `sign_convention` 三态（用例 3，聚焦子集）

```
$ python -m pytest -q tests/unit/test_material_hk_native.py -k "sign_convention or capex_or_fcf"
1 passed, 26 deselected in 1.02s
```

### 2.7 错误结构与状态（用例 9）与任务回归（用例 10）

```
$ python -m pytest -q tests/integration/test_material_collection_job.py
..............                                                           [100%]
14 passed in 3.09s
```

### 2.8 能力门槛与回归（用例 10）

```
$ python -m pytest -q tests/integration/test_api_research_materials.py
..................                                                       [100%]
18 passed in 6.05s

$ python -m pytest -q tests/integration/test_hk_us_publication_flow.py
......................                                                   [100%]
22 passed in 2.83s
```

### 2.9 `capabilities` 断言位置（用例 9 的"行情/FX 能力未启用，缺口可见"）

`capabilities` 六位：`financials`、`price`、`annual_pe`、`dividends`、`pb`、`online_refresh`
（`src/investment_agent/materials/context.py:104`，`CONDITIONAL_CAPABILITIES` 在 `:114`）。

| 断言 | 位置 |
| --- | --- |
| 未覆盖证券六位全 `False` | `tests/integration/test_api_research_materials.py:316` |
| 有名单未采集：`financials is False` | `tests/integration/test_api_research_materials.py:340` |
| 有资料：`financials is True` 且 `price is False` | `tests/integration/test_api_research_materials.py:355`、`:357` |
| `pb is False`（注释 "still not delivered"） | `tests/integration/test_api_research_materials.py:441` |
| `online_refresh` 随许可位翻转 | `tests/integration/test_api_research_materials.py:430`、`:647` |
| 读模型对未交付能力给出状态码 | `src/investment_agent/materials/context.py:709`（`material_capabilities`）、`:738`（`online_refresh_disabled`） |

`price` / `annual_pe` / `dividends` / `pb` 一律为假是**决定**（设计"尚未验证能力"），
不是检查失败；行情异常与 FX 日期越界因此**没有首期数据路径可测**，缺口以能力位与状态码可见。
**这是"能力未启用"的诚实表述，不是"已验收"。**

## 3. 未覆盖清单（含原因与恢复条件）

| 项 | 用例 | 原因 | 恢复条件 |
| --- | --- | --- | --- |
| 苹果拆股后价格与拆股前 EPS 不直接匹配 | 7 | `tests/unit/test_material_valuation.py` 不存在——属**并行/尾部任务 13**，不在本 worktree | 13 合并后由控制 agent 在最终门禁跑该文件 |
| 汇丰最终 HKD 分红在确定日之前不成为已知现金事实 | 7 | `tests/unit/test_material_dividends.py` 不存在——属**尾部任务 14** | 14 合并后由控制 agent 在最终门禁跑该文件 |
| 财务页面展示回归 | 11 任务 | `tests/integration/test_web_stock_financials.py` 不存在——属**并行任务 11** | 11 合并后由控制 agent 在最终门禁跑该文件 |
| 最新实际年报验收 | 10 | **需联网**抓取最新财年；本地证据只覆盖 2021–2024，首期所有验收在禁网下成立 | 联网环境逐家核对最新年报、原件与实际期间（`plans/hk-us-expansion/README.md` §7） |
| 行情异常、FX 日期越界的数据路径 | 9 | **能力未启用**（`price` 是条件能力、FX 首期不使用）；缺口由 `capabilities` 与 `statuses` 可见（§2.9） | 行情/FX 来源许可与汇率时间点验收后另开任务 |
| 在线刷新（真实上游抓取） | 10 | `ONLINE_REFRESH_ENABLED = False`，许可未确认；只在"关闭且如实报告"这一面被验收 | 确认展示/缓存/商业授权后置 `true` 并补联网验收 |
| 跨币种发布换算 | — | 发布只支持原币（偏差 4）；`test_declared_cross_currency_publication_is_refused` 验收的是"仍然拒绝"这一面 | 汇率能力验收后另开任务 |
| 全量 pytest 中的 1 个失败 | — | `tests/unit/test_engine_lifecycle.py::test_api_lifespan_disposes_the_engine_on_shutdown`，既存**顺序相关**失败（偏差 8），非本次引入，不修、不放宽 | 由用户处置该既有缺陷；门禁判定为"不新增失败" |

## 4. 全量门禁与基线对比

```bash
PY=/Users/zhvala/SourceCode/zcharlie/.venv/bin/python
mkdir -p /tmp/zc-hkus-gate-12
cd /tmp/zc-hkus-12
PYTHONPATH=$PWD/src DATA_DIR=/tmp/zc-hkus-gate-12/data \
  DATABASE_URL=sqlite+aiosqlite:////tmp/zc-hkus-gate-12/gate.db \
  API_KEY=ci-inert-api-key LLM_TIER3_API_KEY=ci-inert-tier3 \
  $PY -m pytest -q -m "not network"
/Users/zhvala/SourceCode/zcharlie/.venv/bin/ruff check .
```

### pytest

```
1 failed, 2761 passed, 3 skipped, 6 deselected in 363.58s (0:06:03)

FAILED tests/unit/test_engine_lifecycle.py::test_api_lifespan_disposes_the_engine_on_shutdown
```

（`--collect-only` 独立复核：`2765/2771 tests collected (6 deselected)`，
与 `2761 passed + 1 failed + 3 skipped = 2765` 自洽。）

| 对比项 | 起点（01–10 合并，父任务给定） | 本次（12，纯文档） | 差 |
| --- | --- | --- | --- |
| passed | 2760 | **2761** | **+1** |
| failed | 1 | **1** | **0（无新增）** |
| skipped | 3 | **3** | 0 |
| deselected | （未给定） | 6 | — |

**关于 passed 的 +1（诚实记录，未解释成"通过"）**：本次实测比父任务给定的起点多 1 个通过用例。
可复核的排除证据表明这**不是任务 12 造成的**：

1. 任务 12 的改动集只有 3 个文档（`git status --short` 输出恰好为
   `M README.md`、`M docs/product/plans/hk-us-expansion/12-acceptance.md`、
   `?? docs/product/hk-us-expansion.md`），不含任何代码或测试；
2. 用 `--collect-only` 做了对照实验（把新增文档临时移出 worktree 再收集一次）：
   带该文档与不带该文档**都是** `2765/2771 tests collected (6 deselected)`，
   即任务 12 的文档不改变用例收集数量；
3. `testpaths = ["tests"]`（`pyproject.toml:87`），文档目录不参与收集。

因此该 +1 在集成分支起点 `d2ac41e merge: 09` 上**已经存在**，应与控制 agent 记录起点时的
测量点/环境差异有关，**留给控制 agent 核对**，本任务不把它当成自己带来的通过。

唯一失败为既存顺序相关失败 `tests/unit/test_engine_lifecycle.py::test_api_lifespan_disposes_the_engine_on_shutdown`
（COMPLETION.md 偏差 8 / 基线失败清单第 1 条，报 `no such table: app_setting_meta`），
本次全量复现同一失败，**不修、不放宽**；`failed` 数与起点相同，**无新增失败**。

### ruff

```
$ ruff check . --output-format=concise
src/investment_agent/storage/models/portfolio.py:5:30: F401 [*] `sqlalchemy.Date` imported but unused
tests/integration/test_portfolio_valuation_jobs.py:9:1: I001 [*] Import block is un-sorted or un-formatted
tests/test_portfolio_import.py:10:1: I001 [*] Import block is un-sorted or un-formatted
tests/test_portfolio_valuation_binding.py:9:1: I001 [*] Import block is un-sorted or un-formatted
tests/test_portfolio_valuation_binding.py:10:21: N817 CamelCase `Decimal` imported as acronym `D`
Found 5 errors.
[*] 4 fixable with the `--fix` option.
```

（同一命令的默认格式输出同样是 `Found 5 errors.`，只是带代码片段；这里用 concise 格式便于逐行比对。）

5 个错误与 COMPLETION.md「基线失败清单」第 2 条**逐行一致**（位置、规则码均相同），
全部落在用户 WIP 的 portfolio 文件，非本次代码，**无新增**。

## 5. README 两行 API 路径与代码逐字校对

提取代码里的实际前缀与路径：

```
$ grep -n 'prefix="/research"' src/investment_agent/api/routes/research_materials.py
54:    prefix="/research",
$ grep -n '"/listings/{listing_id}/financial-context"' src/investment_agent/api/routes/research_materials.py
76:    "/listings/{listing_id}/financial-context",
$ grep -n '"/listings/{listing_id}/refresh"' src/investment_agent/api/routes/research_materials.py
93:    "/listings/{listing_id}/refresh",
$ grep -rn "research_materials" src/investment_agent/api/app.py
145:        research_materials,
156:    app.include_router(research_materials.router)
```

| README 行 | 代码来源 | 逐字核对 |
| --- | --- | --- |
| `GET /research/listings/{listing_id}/financial-context` | 前缀 `:54` + 路径 `:76`（`@router.get`） | 一致：前缀 `/research`、路径 `/listings/{listing_id}/financial-context`，无尾斜杠，参数名 `listing_id` |
| `POST /research/listings/{listing_id}/refresh` | 前缀 `:54` + 路径 `:93`（`@router.post`，`status_code=202`） | 一致：前缀 `/research`、路径 `/listings/{listing_id}/refresh`，无尾斜杠，参数名 `listing_id` |

**说明**：路径参数按代码实际写作 `{listing_id}`（不是 `{id}`）——README 与代码逐字一致。
前缀由 `APIRouter(prefix="/research")` 提供，路由经 `app.include_router`（`api/app.py:156`）挂载，
故对外完整路径如上表。

## 6. 遗留（不在本次实施范围，留给联网验收）

照 `plans/hk-us-expansion/README.md` §7 搬运，另加 W6 尾部任务状态：

| 项 | 原因 | 恢复条件 |
| --- | --- | --- |
| 八家最新实际年报补验 | 需联网抓取最新财年，本地证据只覆盖 2021–2024 | 联网环境逐家核对最新年报、原件与实际期间 |
| 在线刷新开关打开 | 东财/Yahoo/新浪/HKMA 的使用许可未确认 | 确认展示/缓存/商业授权后置 `online_refresh=true` |
| 供应商付费权限（FMP/EODHD/HKEX） | 无真实账户端点证据 | 账户与端点逐项验收后作为可替换来源 |
| 港股公布/修订历史、跨日稳定性 | 无完整已验证时间链与多日保障 | 多日 fresh fetch 与官方披露关联验收 |
| 不引入 EdgarTools | 离线环境无该依赖，且设计把版本选择与资格判断归属应用 | 若引入须固定版本、重跑解析与版本案例 |
| 年度 PE、分红事件展示 | W6 尾部任务（13/14），本次实施未并入 | 启动 13/14 并按各自子计划验收 |

## 7. 与子计划原稿的偏差

1. **原计划基线口径已过期**：本文件原稿 T12.2 写"与代码事实基线（`75d3656` 上 1606 passed /
   3 skipped / 0 failed）对比"；控制 agent 已将基线改为施工基线 `d5cf937` 的 2392 passed 快照，
   并给出**起点（01–10 合并）全量 = 2760 passed / 1 failed / 3 skipped**。本文件按后者对比（§4）。
   本次实测 passed = 2761（比起点多 1），failed 与 skipped 相同；+1 的排除证据见 §4，
   **留待控制 agent 核对起点测量点**，本任务不声称自己多通过了一个用例。
2. **`tests/unit/test_material_store.py` 实际 12 passed**（非 11 个 `def test_` 计数）——
   `test_draft_without_raw_bytes_has_no_artifact` 被 `@pytest.mark.parametrize("raw_bytes", [None, b""])`
   展开成 2 例，故 11 个 `def test_` → 12 个用例；矩阵按**实测用例数**记录。
3. **路径参数写作 `{listing_id}`**（非任务描述里的 `{id}`），以与代码逐字一致（§5）。
4. **"功能一句话"落在 `## 文档` 索引条目**，未在 README 正文新增独立功能段落——
   父任务给定的三项改动为 API 表／文档索引／已知限制，保持最小改动面。
5. **`mypy src` 本次未跑**：父任务给定的门禁命令只有全量 pytest 与 `ruff check .` 两条
   （00-conventions.md 列出三条，其中 mypy 针对 `src`，而本任务不改 `src`）。
   本任务改动为纯文档，对 mypy 无影响；如需完整三门禁，由控制 agent 在最终门禁补跑。

## 8. 提交

```
docs(hk-us): acceptance matrix and product documentation
```
