# 10 研究档案与发布校验接入

> 波次 W4 ‖ 可与 09 并行。依赖：07 已合并（`material_evidence` 与 `save_evidence` 分支）。
> 文件所有权：见 [`README.md`](README.md) §4 第 10 行。本任务是**唯一**允许改动 `investor/publication_numbers.py` 的任务。

**目标：** 港美资料证据能进入既有研究档案流程（Case → 研究计划 → 证据引用 → 发布校验），关键数值必须经 `evidence_ref`/`calculation_ref` 输出；**A 股流程行为不变**。

**决策：** 首期发布只支持**原币**展示：`format_quantity` 允许 `HKD`/`USD`/`TWD` 原币值，但**跨币种换算仍然拒绝**（沿用既有错误语义），因为汇率使用许可与时间点未验收。这样港美研究可以按原币发布，不会静默引入未验收的换算。

## 代码事实

- 建档以 `listing_ref` 为准，`listing_ref.kind == "listing"` 且 listing 必须存在：`investor/foundations.py:219-321`（`create_case`，`CreateCase` 契约在 `67-72`）。
- 产物/证据挂载：`investor/foundations.py:469-565`（`attach_artifacts`，会按 case 的 listing 用 `EvidenceService` 校验每个 ref）。
- 计划与修订：`save_plan`（`foundations.py:361-467`），`Mutation(expected_revision, idempotency_key)`（`investor/common.py:142-144`）。
- 发布校验：`investor/publication.py:167` `check(candidate, semantic) -> (PublicationCheck, dict[str, ResolvedNode])`；`publish` 在 `333`。
- 数值与币种格式化的硬约束：`investor/publication_numbers.py:23`（`MONEY` 只含 CNY 系列）、`38-60`（`format_quantity` 对非 CNY/None 币种直接拒绝 "Cross currency publication requires a verified conversion"；单位白名单在 `55`）。
- 既有测试脚手架（**必须照它的方式搭**）：`tests/test_investor_publication.py`、`tests/test_investor_foundations.py`。

## 文件清单

```
新增  src/investment_agent/materials/investor_bridge.py
新增  tests/integration/test_hk_us_publication_flow.py
修改  src/investment_agent/investor/publication_numbers.py   （只放行原币，不改跨币种拒绝）
```

## 实现

```python
# materials/investor_bridge.py
@dataclass(frozen=True)
class MaterialEvidenceBatch:
    refs: tuple[ObjectRef, ...]        # 保存后的证据引用
    skipped: tuple[str, ...]           # 未转换原因（来自 material_evidence）
    dataset_ids: tuple[int, ...]

async def save_material_evidence(
    session: AsyncSession, *, listing_id: int, dataset_ids: Sequence[int],
    metrics: Sequence[str] | None = None, owner_id: OwnerId = "local_owner",
) -> MaterialEvidenceBatch
    """读出 dataset → material_evidence(...) → EvidenceService.save_evidence(每条) →
    返回 refs。任一条被拒（例如再物化不一致）必须抛出，不静默跳过。"""

async def attach_material_evidence(
    session: AsyncSession, *, case_id: UUID, refs: Sequence[ObjectRef],
    expected_revision: int | None = None, owner_id: OwnerId = "local_owner",
) -> Case
    """薄封装 InvestorService.attach_artifacts（不改其语义）。"""
```

发布数值路径（测试必须证明）：
- 发布候选中引用某事实的节点用 `ObjectRef(kind="evidence", id=<uuid>, revision=1)`；`PublicationService.check` 解析出的值与资料值**完全相等**（`Decimal` 精确比较）。
- 需要计算值（如 `fcf_simple`）时，走 `EvidenceService.save_calculation` 落 `CalculationRecord`，`fact_refs` 指向证据 refs，之后再引用 `calculation_ref`。**不允许**在发布正文里写未挂 ref 的数字。

`publication_numbers.py` 的改动（最小、加法）：
- `MONEY` 增加 `"HKD": 1`、`"USD": 1`、`"TWD": 1`；单位白名单（`55` 行附近）增加 `"HKD"`/`"USD"`/`"TWD"` 与对应 `"/share"` 形式。
- `format_quantity` 对 `currency in {"HKD","USD","TWD"}` 放行并原样格式化（**不乘任何汇率**）；`currency not in {None,"CNY","HKD","USD","TWD"}` 仍走既有拒绝分支。
- **保留**"存在跨币种换算需求时拒绝"的行为：如果调用方声明了换算（现有签名/参数或 `Quantity.currency` 与目标展示币种不一致），仍抛既有错误。既有 CNY 用例的返回元组形状（`tuple[str,str,int,str]`）不得改变。

## 验证（`tests/integration/test_hk_us_publication_flow.py`）

用 `db_session`（`tests/conftest.py:82-89`）+ `_hk_us_support`；资料用 05 的 store 写入 `pilots.json` 的汇丰/中电 fixture 值。

| 用例 | 断言 |
| --- | --- |
| 建档 | 为 `HKEX/00002` 的 listing 建 Case 成功；`case.listing_ref.kind=="listing"`；listing 不存在时抛既有 `InvestorError`（不新建） |
| 证据保存 | `save_material_evidence` 返回 refs；库中 `investor_evidence` 行数等于 facts 中"有时间可转"的条数；每条 payload 的 `source_kind=="research_dataset"`、`date_precision` 正确 |
| 挂载 | `attach_material_evidence` 后 `case` revision 递增，`CaseDetail` 能列出这些 evidence refs（`InvestorService.detail`） |
| 发布校验（原币） | 构造引用该证据的发布候选 → `check()` 通过，解析值与资料值精确相等；币种为 HKD 时**没有**任何 CNY 换算值 |
| 跨币种仍拒绝 | 声明把 HKD 值当 CNY 发布 → 抛既有错误（`Cross currency publication requires a verified conversion`），且不落任何发布记录 |
| 未挂 ref 的数字 | 发布候选里的金额节点若不带 evidence/calculation ref → `check()` 返回失败项（沿用既有校验行为，不新增放行口） |
| 未知时间不入档 | 只有日期精度/未知公布时间的资料 fact：前者进档且 `date_precision=="date"`，后者留在资料层（`skipped` 含原因），**不出现在证据里** |
| 计算 ref | `fcf_simple` 的 `CalculationRecord`：`fact_refs` 指向证据 refs、`method_version` 固定、`input_hash` 覆盖单位/币种/期间（改一个输入值 → hash 变化） |
| A 股回归 | `tests/test_investor_publication.py`、`tests/test_investor_foundations.py`、`tests/test_evidence_services.py` 全绿，无断言被放宽 |

## 提交

```
feat(materials): material evidence into research cases with original-currency publication
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总）
