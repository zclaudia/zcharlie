# 12 验收矩阵与文档收口

> 波次 W5 ‖ 可与 11 并行（本任务只写文档与跑验收，不碰 `web/**`）。
> 依赖：全部核心任务（01～11）已合并。**本任务必须诚实**：没跑过、没覆盖的用例写"未覆盖"，不得写成通过。

**目标：** 把设计 §实施阶段与验收 的 10 条强制验收用例逐条映射到可执行的测试；补齐产品文档与 README；跑一次完整门禁并记录数字。

## 文件清单

```
新增  docs/product/hk-us-expansion.md
新增  docs/product/plans/hk-us-expansion/12-acceptance.md   （本子任务的产物，覆盖式重写）
修改  README.md                                              （HTTP API 表 + 文档索引 + 功能一句话）
```

## 验收映射（必须逐条给出"测试文件::测试函数"与运行结果）

| # | 设计要求 | 期望落点 |
| --- | --- | --- |
| 1 | 同名公司、代码前导零、CIK 与股票类别不混用；错误身份不能自动建档或合并 | `tests/unit/test_security_identity.py` |
| 2 | 汇丰信用损失前后收入、普通股与其他权益持有人利润分开；港币报价、美元报表、人民币展示值不串用 | `tests/unit/test_material_hk_native.py`、`tests/unit/test_material_metrics.py` |
| 3 | 中电与腾讯原币表、真实当期列和支出符号正确；不得误生成统一 FCF | 同上（含 `sign_convention` 三态断言） |
| 4 | AAPL/COST 非自然年与 52/53 周；JPM 缺 capex 不填零；XOM 收入定义不替换 | `tests/unit/test_material_us_sec.py`、`tests/unit/test_material_metrics.py` |
| 5 | LNC 原始 / 不可依赖 / 修订三时点状态正确；日期过滤不绕过接受时间 | `tests/unit/test_material_us_sec.py` |
| 6 | TSM TWD、美元便利换算、ADS 与普通股分开；不同报告利润不拼接 | `tests/unit/test_security_identity.py`、`tests/unit/test_material_us_sec.py`、（若启动）`tests/unit/test_material_valuation.py` |
| 7 | 苹果拆股后价格不直接匹配拆股前 EPS；汇丰最终 HKD 分红在确定日之前不能成为已知现金事实 | （尾部）`tests/unit/test_material_valuation.py`、`tests/unit/test_material_dividends.py` |
| 8 | 只有日期或未知公布时间的输入保留精度与缺口；导入旧材料不回填取得时间；新证据不改写旧研究 | `tests/unit/test_material_store.py`、`tests/unit/test_material_evidence.py`、`tests/integration/test_material_offline_import.py` |
| 9 | HTTP 200 错误正文、分页截断、重复字段、行情异常、FX 日期越界、断网与 schema 变化都有明确状态 | `tests/unit/test_material_hk_native.py`、`tests/unit/test_material_us_sec.py`、`tests/integration/test_material_collection_job.py`；**行情/FX 首期未启用 → 在矩阵里写"能力未启用，缺口可见"并给出 `capabilities` 断言位置** |
| 10 | 最新实际年报验收；缺价格仍可财务研究；许可未确认仍关闭在线源；A 股取数、评分、证据与任务回归 | `tests/integration/test_api_research_materials.py`、`tests/integration/test_material_collection_job.py`（`online_refresh_disabled`）、本任务的全量回归；**"最新实际年报"需联网 → 记为遗留，不得标为通过** |

## 步骤

- [ ] T12.1 逐条跑上表的测试节点，记录 `passed/failed` 与命令；凡未落地的（尾部任务未启动、需联网）写"未覆盖 + 原因 + 恢复条件"。
- [ ] T12.2 跑完整门禁（`00-conventions.md` 三条），记录 pytest 数字并与代码事实基线（1590 passed / 0 failed）对比；出现新增失败必须修掉或在矩阵里写明为已知偏差。
- [ ] T12.3 写 `docs/product/hk-us-expansion.md`：交付边界（照 `README.md` §1 的表，但用产品语言）、能力位与默认关闭项、来源与许可状态、离线导入用法、遗留清单。**不得**把未验收能力写成已交付。
- [ ] T12.4 更新 `README.md`：`## HTTP API` 表加两行（`GET /research/listings/{id}/financial-context`、`POST /research/listings/{id}/refresh`），`## 文档` 索引加本计划与新功能文档，`## 已知限制` 补港美条目的当前边界（在线刷新关闭、最新年报待补验、PB/TTM/股息率未启用）。
- [ ] T12.5 重写本文件为验收矩阵（保留上表 + 实测结果 + 未覆盖清单）。

## 验证

- 文档中的每个"通过"都能用给出的命令复现；每条命令的输出（测试数量/失败名）写进矩阵。
- `README.md` 的两行 API 路径与 `api/routes/research_materials.py` 的实际路径逐字一致（用 `grep` 校对，写进矩阵）。
- 全量门禁数字与基线对比明确写出（`pytest -q -m "not network"` 的 passed 数 ≥ 基线且 failed 不增加）。

## 提交

```
docs(hk-us): acceptance matrix and product documentation
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总）
