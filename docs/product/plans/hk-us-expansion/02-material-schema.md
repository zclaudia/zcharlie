# 02 研究资料契约、模型与迁移

> 波次 W1 ‖ 可与 01 并行。依赖：无。**本任务是 03～09 的接口基石，契约一旦提交即为冻结。**
> 文件所有权：见 [`README.md`](README.md) §4 第 02 行。本任务独占 `tests/conftest.py`、`alembic/env.py`、`storage/models/__init__.py`。

**目标：** 新增两个持久化对象 `research_source_binding`、`research_dataset`，并把设计的"字段必需信息"固化成可校验的契约；`materials/contracts.py` 是后续所有适配器/存储/证据/指标的共用语言。

**决策：**
- dataset 粒度：**一个 dataset = 一个 subject 的一个来源的一个报告期**（`filing_index` 例外，`period` 为空表示公司级申报状态）。价格/汇率/公司行为将来复用同一张表。
- 来源特有字段进 `payload`（版本化 JSON），**不进契约**；契约只承载跨来源通用的语义与时间。
- 不引入 EdgarTools 依赖（离线环境无此包，且设计把版本选择与资格判断归属应用）。

## 代码事实

- 最新迁移 `045_zone_events.py`（`revision="045"`，`down_revision="044"`）：`alembic/versions/`；新迁移 = `046`，`down_revision="045"`。
- 迁移文件头风格（不用 mako 头，直接模块 docstring）：`alembic/versions/045_zone_events.py:1-20`。
- 模型注册：`alembic/env.py:13-38` 逐个 import 模型模块；`tests/conftest.py:7-32` 有同一份清单；`storage/models/__init__.py` **当前为空**。
- 不可变行的既有实现（照抄这个模式）：`storage/models/evidence.py:27-33`（`before_update`/`before_delete` 抛错）。
- 仓储基类：`storage/repositories/base.py:8-15`（`Repository[T]`，`__init__(session: AsyncSession)`）。
- `TimestampMixin`：`storage/models/base.py:13-22`（`created_at`/`updated_at`，timezone-aware，`updated_at` 带 `onupdate`）。
- `Market`/`Company`/`Listing` 定义：`storage/models/reference.py:18-62`。

## 文件清单

```
新增  src/investment_agent/materials/__init__.py        （只放模块 docstring，保持空壳）
新增  src/investment_agent/materials/contracts.py
新增  src/investment_agent/storage/models/research_material.py
新增  src/investment_agent/storage/repositories/research_material.py
新增  alembic/versions/046_research_material.py
新增  tests/unit/test_research_material_contracts.py
新增  tests/unit/test_research_material_repo.py
新增  tests/integration/test_migration_046.py
修改  alembic/env.py, tests/conftest.py, storage/models/__init__.py
```

## 契约（冻结，逐条实现）

```python
SCHEMA_VERSION = 1

class DatasetKind(StrEnum):
    ANNUAL_FACTS = "annual_facts"          # 年度财报事实（company 级）
    FILING_INDEX = "filing_index"          # 申报与版本状态（company 级，period 为空）
    PRICE_DAILY = "price_daily"            # 每日价格（listing 级，条件启用）
    FX_DAILY = "fx_daily"                  # 汇率（company/listing 级，条件启用）
    CORPORATE_ACTION = "corporate_action"  # 分红等公司行为（listing 级，条件启用）

class SubjectKind(StrEnum):   COMPANY = "company"; LISTING = "listing"
class Verification(StrEnum):  CANDIDATE = "candidate"; VERIFIED = "verified"
class PeriodBasis(StrEnum):   ANNUAL="annual"; INTERIM="interim"; QUARTER="quarter"; YTD="ytd"; INSTANT="instant"; DAILY="daily"
class AccountingStandard(StrEnum): US_GAAP="us_gaap"; IFRS="ifrs"; HKFRS="hkfrs"; UNKNOWN="unknown"
class ReportingScope(StrEnum): CONSOLIDATED="consolidated"; PARENT_ONLY="parent_only"; UNKNOWN="unknown"
class EquityScope(StrEnum): ORDINARY="ordinary_shareholders"; ALL_PARENT="all_parent_equity_holders"; TOTAL="total"; UNKNOWN="unknown"
class PerShareBasis(StrEnum): BASIC="basic"; DILUTED="diluted"; NA="not_applicable"
class DatePrecision(StrEnum): TIMESTAMP="timestamp"; DATE="date"; UNKNOWN="unknown"

FACT_METRICS: frozenset[str] = frozenset({
    "revenue_operating", "revenue_gross", "revenue_net_credit", "credit_loss_expense", "cost_of_revenue",
    "net_profit", "profit_parent", "profit_ordinary_shareholders", "profit_other_equity_holders",
    "total_assets", "total_liabilities", "total_equity", "equity_parent", "equity_ordinary",
    "operating_cash_flow", "capex_ppe",
    "eps_basic", "eps_diluted", "shares_weighted_basic", "shares_outstanding_end",
})

@dataclass(frozen=True)
class FactPeriod:
    start: date | None; end: date; basis: PeriodBasis
    label: str | None = None      # 财年标签，如 "FY2024"；不由自然季反推
    days: int | None = None       # 实际天数（52/53 周制保留真实长度）

@dataclass(frozen=True)
class FactSemantics:
    metric: str; source_label: str | None; source_field: str | None
    accounting_standard: AccountingStandard; reporting_scope: ReportingScope
    equity_scope: EquityScope; per_share_basis: PerShareBasis

@dataclass(frozen=True)
class FactValue:
    raw_value: str | None; raw_unit: str; scale: int; unit: str
    currency: str | None; sign_convention: str
    missing_reason: str | None = None    # raw_value 为 None 时必填

@dataclass(frozen=True)
class FactProvenance:
    source: str; accession: str | None; original_hash: str | None
    source_published_at: datetime | None; source_published_date: date | None
    source_published_precision: DatePrecision
    accepted_at: datetime | None; source_available_at: datetime | None

@dataclass(frozen=True)
class FactRecord:
    metric: str; period: FactPeriod; semantics: FactSemantics
    value: FactValue; provenance: FactProvenance
    verification: Verification; conflict: str          # "none" | "unresolved" | "resolved"
    applicability: str                                 # "applicable" | "not_applicable" | "unknown"
    conflict_note: str | None = None
    original_locator: str | None = None                # 页码/片段/表格定位
    source_version: str | None = None                  # 上游版本；可未知

@dataclass(frozen=True)
class DatasetDraft:
    kind: DatasetKind; subject_kind: SubjectKind; subject_id: int
    source: str; schema_version: int; adapter_version: str
    captured_at: datetime                              # 真实系统取得时间
    payload: dict[str, Any]
    period: FactPeriod | None = None
    raw_bytes: bytes | None = None
    raw_media_type: str | None = None
    raw_source_url: str | None = None
    source_fetched_at: datetime | None = None           # 原研究抓取时间（导入时另存）

@dataclass(frozen=True)
class UpstreamRequest:
    name: str; url: str; params: tuple[tuple[str, str], ...] = ()
    media_type: str = "application/json"

@dataclass(frozen=True)
class RawPayload:
    name: str; content: bytes; media_type: str; fetched_at: datetime
    source_url: str; http_status: int

@dataclass(frozen=True)
class ParseResult:
    drafts: tuple[DatasetDraft, ...]
    problems: tuple[str, ...]      # 结构/覆盖问题：不盲重试，必须可读地暴露

class MaterialAdapter(Protocol):
    source: str; adapter_version: str
    def requests(self, identity: ControlledIdentity, *, periods: Sequence[FactPeriod]) -> tuple[UpstreamRequest, ...]: ...
    def parse(self, identity: ControlledIdentity, *, raw: Sequence[RawPayload], periods: Sequence[FactPeriod]) -> ParseResult: ...

def validate_draft(draft: DatasetDraft) -> tuple[str, ...]      # 返回违规原因，空元组表示合法
def content_hash(draft: DatasetDraft) -> str                    # payload 规范化 JSON + raw_bytes 的 sha256
```

`validate_draft` 必须实现（每条都要有测试）：
1. `captured_at` 时区感知；缺失即违规。
2. `metric` 必须在 `FACT_METRICS`；不在集合的 fact 由适配器放进 `problems`，**不得**塞进 payload。
3. 单位与币种：`unit in {"USD","CNY","HKD","TWD"}` ⇔ `currency` 非空；`"*/share"` 必须带 currency；`"ratio"/"shares"/"percent"` 必须 `currency is None`。
4. `raw_value is None` ⇔ `missing_reason` 非空（缺失必须有原因，已知值不得带原因）。
5. `basis == INSTANT` ⇒ `start is None`；`basis == ANNUAL` ⇒ `start` 存在且 `330 <= (end-start).days <= 400`（52/53 周合法，360 天以上异常必须报出）。
6. `source_published_precision == UNKNOWN` ⇒ 两个公布时间字段都为 None；否则至少一个非空。
7. `conflict == "unresolved"` ⇒ `conflict_note` 非空。
8. `raw_value` 为十进制字符串（`decimal.Decimal` 可解析），拒绝 float 字面量与空串。

## 模型与迁移

```python
class ResearchSourceBinding(Base, TimestampMixin):
    __tablename__ = "research_source_binding"
    id: int PK; subject_kind: str(16); subject_id: int
    source: str(64); source_symbol: str(64)
    valid_from: Date (NOT NULL); valid_to: Date | None
    verified_at: DateTime; verified_by: str(64)      # 例 "pilot_list@2026-10-12"
    payload: JSON (default dict)
    UniqueConstraint("subject_kind","subject_id","source","source_symbol","valid_from")

class ResearchDataset(Base, TimestampMixin):
    __tablename__ = "research_dataset"
    id: int PK; kind: str(32); subject_kind: str(16); subject_id: int
    source: str(64); schema_version: int; adapter_version: str(32)
    period_end: Date | None; period_basis: str(32) | None
    revision: int; previous_id: int | None
    content_hash: str(64)                            # 唯一版本标识
    captured_at: DateTime; source_fetched_at: DateTime | None
    raw_ref: str(512) | None                          # 相对 data_dir 的路径
    payload: JSON
    UniqueConstraint("kind","subject_kind","subject_id","source","period_end","content_hash",
                     name="uq_research_dataset_version")
    Index("ix_research_dataset_lookup", "kind","subject_kind","subject_id","period_end")
```

- `subject_id` 指 `company.id` 或 `listing.id`，**不建多态外键**（与设计"公司事实属于 company，证券行情和行为属于 listing"一致），在 repository 与契约层校验 `subject_kind` 与调用语义一致。
- 两张表都加 `before_update`/`before_delete` 不可变触发器（照抄 `storage/models/evidence.py:27-33`）。
- `materials/__init__.py` 只放 docstring；`storage/models/__init__.py` 里加 `research_material` 的 import（保持现有空文件风格：只 import 不加逻辑）。
- 迁移 046 建两表 + 两个索引 + 唯一约束；`downgrade()` drop 两表。

## 仓储

```python
class ResearchSourceBindingRepository(Repository[ResearchSourceBinding]):
    async def list_for(self, subject_kind: str, subject_id: int, *, at: date | None = None) -> list[ResearchSourceBinding]
    async def symbol_for(self, subject_kind, subject_id, source, *, at: date | None = None) -> str | None

class ResearchDatasetRepository(Repository[ResearchDataset]):
    async def latest(self, *, kind, subject_kind, subject_id, period_end: date | None = None) -> ResearchDataset | None
    async def history(self, *, kind, subject_kind, subject_id) -> list[ResearchDataset]
    async def by_content_hash(self, *, content_hash: str) -> ResearchDataset | None
    async def insert_version(self, row: ResearchDataset) -> ResearchDataset
        """同 (kind,subject,source,period_end) 已有版本时：revision = max+1、previous_id = 上一版本 id；
        内容 hash 相同由调用方（05 的 store）先去重，本方法不做幂等判断。"""
```

## Task

- [ ] T2.1 `materials/contracts.py`：枚举 + dataclass + `FACT_METRICS` + `validate_draft` + `content_hash` + `MaterialAdapter` Protocol。
- [ ] T2.2 `storage/models/research_material.py`（含不可变触发器）+ 三处注册（`alembic/env.py`、`tests/conftest.py`、`storage/models/__init__.py`）。
- [ ] T2.3 `alembic/versions/046_research_material.py`。
- [ ] T2.4 `storage/repositories/research_material.py`。
- [ ] T2.5 三个测试文件（见下）。

## 验证

- `tests/unit/test_research_material_contracts.py`：
  - 合法 `DatasetDraft` 通过；上表 8 条校验逐条各一个反例（含"ANNUAL 但只有 300 天"、"float 字面量金额"、"UNKNOWN 精度却带了日期"）。
  - `content_hash` 稳定性：同 draft 两次调用相同；改一个 `raw_value` 或 `raw_bytes` 即变化；payload 键顺序不影响结果。
  - `PERIOD_52W`：`days=371` 的 AAPL 财年通过校验；`days=364` 通过；`days=300` 被拒。
- `tests/unit/test_research_material_repo.py`：`insert_version` 递增 revision 与 previous_id；`latest` 取最大 revision；`history` 有序；更新/删除 dataset 与 binding 抛错（不可变）；`list_for` 的 `at` 过滤（`valid_from <= at` 且 `valid_to is None or valid_to >= at`）。
- `tests/integration/test_migration_046.py`：照 `tests/integration/test_migration_045.py` 的写法，upgrade 后 `sqlalchemy.inspect` 断言两张表、唯一约束名、索引名；`downgrade` 后两张表消失；迁移链 head 为 `046`。
- 全量门禁见 `00-conventions.md`（**新增迁移后必须重跑全量**，确认无新增失败）。

## 提交

```
feat(materials): immutable research dataset and source binding schema
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总）
