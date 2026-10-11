# 07 资料→证据适配分支

> 波次 W3 ‖ 可与 06、08 并行。依赖：05 已合并。
> 文件所有权：见 [`README.md`](README.md) §4 第 07 行。本任务是**唯一**允许改动 `evidence/contracts.py` 与 `evidence/store.py` 的任务。

**目标：** 让港美资料中**满足时间与来源要求**的事实转成可引用的 `EvidenceRecord`，不满足的留在资料层并给出明确原因；既有 A 股证据路径行为不变。

**决策：** 显式兼容式扩展证据契约（新增枚举值 + 可选字段），不伪造时间、不复用 A 股分支。

## 代码事实

- `EvidenceRecord` 定义与必填时间字段：`evidence/contracts.py:41-64`（`published_at`/`acquired_at`/`available_from` 必填；`source_kind: Literal["snapshot","document","user_report"]`；`verification` 三值；`document_provenance` 可选；`user_report` 与 text-only 的两个 validator）。
- `DocumentProvenance`：`contracts.py:24-38`（`parser_version`/`original_hash`/`fragment_hash`/`metric`/`raw_*`/`reporting_scope`/`header_*`/`correction_*`）。
- A 股适配分支（**不改行为，只学结构**）：`evidence/snapshots.py:58-167`，尤其 `day_start` 的上海午夜规则（54-56）与市场行情的 15:00 规则（115-124）。
- 保存与再物化校验：`evidence/store.py:130-310`（`save_evidence` 对 snapshot 分支会**重新物化并比对 12 个字段**后再落库；未知来源会被拒）。
- 读取可见性：`evidence/store.py:29-82`（`max(available_from, published_at, acquired_at) > as_of` 即不可见）。
- `ObjectRef` 的 kind 白名单：`investor/common.py:45-101`（int 型 ID 白名单）。

## 文件清单

```
新增  src/investment_agent/materials/evidence.py
新增  tests/unit/test_material_evidence.py
修改  src/investment_agent/evidence/contracts.py     （仅两处加法）
修改  src/investment_agent/investor/common.py        （仅 ObjectRef kind 集合）
修改  src/investment_agent/evidence/store.py         （仅新增 research_dataset 分支）
```

## 契约扩展（加法，向后兼容）

`evidence/contracts.py`：
1. `EvidenceRecord.source_kind` → `Literal["snapshot", "document", "user_report", "research_dataset"]`。
2. 新增 `date_precision: DatePrecision | None = None`（`DatePrecision` 从 `materials.contracts` 导入或在 `evidence/contracts.py` 内定义同名枚举 —— 选后者，避免 evidence 反向依赖 materials；`materials/contracts.py` 的 `DatePrecision` 值必须一致，加一条测试断言两处取值相同）。

`investor/common.py`：`ObjectRef` 的 kind 白名单加 `"research_dataset"`（int 型 ID，与既有 int 型 kind 同规则）。

兼容性要求：既有 payload 反序列化不受影响（新字段有默认值）；`Revision`/`schema_version` 语义不变；A 股测试全绿。

## 实现

```python
# materials/evidence.py
def material_evidence(
    dataset: ResearchDataset,
    *,
    subject_ref: ObjectRef,           # kind="listing"（研究档案以 listing_ref 建档）
    company_ref: ObjectRef | None = None,
    metrics: Sequence[str] | None = None,
    created_at: datetime | None = None,
) -> tuple[tuple[EvidenceRecord, ...], tuple[str, ...]]
    """返回 (证据列表, 未转换原因列表)。第二个返回值必须解释每一条被跳过的 fact。"""
```

转换规则（逐条可测）：

| 规则 | 处理 |
| --- | --- |
| `verification` | 资料里 `CANDIDATE` → 证据 `"candidate"`；`VERIFIED` → `"verified"`。**不允许**在证据层把 candidate 升级为 verified |
| 时间精度 TIMESTAMP | `published_at = source_published_at`；`available_from = accepted_at or source_published_at`；`date_precision=TIMESTAMP` |
| 时间精度 DATE | `published_at` = 该日期 00:00 UTC；`available_from` = **保守边界：该日期的次日 00:00 UTC 与 `accepted_at`（若有）取较晚者**（设计：日期精度用下一日开始的保守边界，不宣称具体时刻已知）；`date_precision=DATE` |
| 时间精度 UNKNOWN | **不产出证据**，原因 `"unknown_publication_time"`（留在资料层，页面可展示事实与限制） |
| `source_available_at` 未知 | 不填补；`available_from` 只用上面能证明的量 |
| `acquired_at` | 资料行的 `captured_at`（真实系统取得时间） |
| `period` | 由 `FactPeriod` 映射：`basis` 照抄（`annual`/`interim`/`quarter`/`ytd`/`instant` → `Period(basis=...)`，`instant` 时 `start == end == period.end`） |
| `value` | `Quantity(value=Decimal(raw_value) if raw_value else None, unit=unit, currency=currency, unknown_reason=missing_reason)` —— **`unknown_reason` 与 `value` 互斥**由 `Quantity` 自己的 validator 保证；缺失事实照样产出证据（带原因），不得丢弃 |
| `source_ref` | `ObjectRef(kind="research_dataset", id=dataset.id, revision=dataset.revision)` |
| `locator` | `f"{dataset.source}:{dataset.kind}:{period.label or period.end}"`；带 `original_locator`（页码/片段）时拼在后面 |
| `content_hash` | 资料行的 `content_hash`（+ fact metric，保证同一 dataset 内不同 fact 的 hash 不同） |
| `conflict` | 资料 fact 的 `conflict` 原样传递（`none`/`unresolved`/`resolved`）；`unresolved` 时证据必须保持 `unresolved` |
| `document_provenance` | 有原件定位时填（`metric`/`raw_unit`/`raw_currency`/`raw_value`/`reporting_scope`/`original_hash`），无则 `None` |
| `text` | 与 `document_provenance` 无关时留空；仅当 value 缺失且无 provenance 时用一句说明（不得写成结论） |

`evidence/store.py::save_evidence` 新增分支：`source_kind == "research_dataset"` 时，按 `source_ref.id/revision` 读出 `research_dataset` 行，**用本模块的 `material_evidence` 重新物化**，逐字段比对（至少：value/unit/currency、period.start/end/basis、published_at、available_from、acquired_at、verification、conflict、content_hash、locator、date_precision），不一致即拒绝（沿用现有 A 股分支的拒绝语义与错误类型）。比对通过后按既有流程落 `investor_evidence`（新 UUID、revision=1、receipt 幂等）。**不得**跳过再物化校验直接写入。

## 验证（`tests/unit/test_material_evidence.py` + store 分支）

| 用例 | 断言 |
| --- | --- |
| 日期精度保守边界 | DATE 精度、无 `accepted_at`、日期 `2024-02-16` → `available_from == 2024-02-17T00:00:00Z`、`published_at == 2024-02-16T00:00:00Z`、`date_precision=="date"` |
| 有接受时刻 | TIMESTAMP 精度、`accepted_at=2023-02-16T21:19:28Z` → `available_from == accepted_at` |
| 未知时间 | precision=UNKNOWN → `evidence == ()` 且原因列表含 `unknown_publication_time` |
| 缺失值不丢 | `raw_value=None` + `missing_reason` → 产出证据，`value.value is None`、`value.unknown_reason` 非空 |
| 币种/单位 | USD/TWD 事实的证据 `value.currency` 正确；`ratio` 类事实 `currency is None` |
| 精度不升级 | 资料 `CANDIDATE` → 证据 `"candidate"`；尝试传入 `verified` 的伪造资料 → 仍为 candidate（或以拒绝方式失败，二者取一并说明） |
| conflict 传递 | `conflict="unresolved"` 的资料 → 证据 `conflict=="unresolved"` 且读取路径（`store.get_evidence`）映射为 conflict 状态 |
| 再物化拒绝 | 手工篡改落库后的证据 payload 一个金额 → `save_evidence` 抛既有错误类型，且不写入 |
| A 股回归 | 既有证据测试（`tests/test_evidence_services.py`、snapshot 相关用例）全绿；`SnapshotQueryService` 路径行为不变 |
| 精度枚举一致 | `evidence.contracts.DatePrecision` 与 `materials.contracts.DatePrecision` 取值集合相同 |

## 提交

```
feat(materials): material-backed evidence records with explicit date precision
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总）
