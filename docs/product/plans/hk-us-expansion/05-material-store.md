# 05 不可变资料存储、原始制品与离线导入

> 波次 W2 ‖ 可与 03、04 并行。依赖：02 已合并。
> 文件所有权：见 [`README.md`](README.md) §4 第 05 行。**禁止修改 `materials/contracts.py`**。

**目标：** 提供唯一的写入口 `MaterialStore.save()`：内容 hash 去重、原文落盘、版本递增、旧版本不改写；并提供把既有调研证据离线导入为受控资料的入口（首期禁网条件下的主路径）。

## 代码事实

- 原始响应落盘的既有风格（原子写 + 排序 JSON + 相对 `data_dir` 的 `raw_ref`）：`snapshots/collect.py:106-129` `ArtifactWriter`。
- `data_dir` 来自 `Settings.data_dir`（env `DATA_DIR`）：`config.py`；运行制品目录约定 `snapshot-runs/`、`research-runs/`、`calendars/`。
- 迁移里的 `.meta.json` 格式（离线导入要解析它取原抓取时间）：`fetched_at_utc`、`url`、`status`、`sha256`、`bytes`（见 `evidence/remaining-final-03/raw/native/00005/income-four-years.json.meta.json`）。
- `ObjectRef` 的 kind 白名单在 `investor/common.py:45-101`；**本任务不新增 kind**，`save()` 返回下面的 `SavedDataset`，避免改动 wire 契约。
- 不可变触发器：`storage/models/evidence.py:27-33`（02 已在新表上照抄）。

## 文件清单

```
新增  src/investment_agent/materials/store.py
新增  src/investment_agent/materials/offline_import.py
新增  tests/unit/test_material_store.py
新增  tests/integration/test_material_offline_import.py
新增  tests/fixtures/materials/offline/{00005/income-four-years.json,00005/income-four-years.json.meta.json}
```

## 实现

```python
@dataclass(frozen=True)
class SavedDataset:
    id: int; revision: int; content_hash: str; raw_ref: str | None; created: bool

class MaterialStore:
    def __init__(self, session: AsyncSession, *, data_dir: Path) -> None: ...
    async def save(self, draft: DatasetDraft) -> SavedDataset: ...
    async def latest(self, *, kind, subject_kind, subject_id, period_end=None) -> ResearchDataset | None
    async def history(self, *, kind, subject_kind, subject_id) -> tuple[ResearchDataset, ...]
    def raw_path(self, *, subject_kind, subject_id, kind, content_hash, suffix=".json") -> Path
```

`save()` 顺序固定：
1. `validate_draft(draft)` 有违规 → `raise ValueError("; ".join(problems))`（契约违规是代码缺陷，不当数据问题吞掉）。
2. `h = content_hash(draft)`；`ResearchDatasetRepository.by_content_hash(h)` 命中 → 直接返回 `SavedDataset(..., created=False)`，**不新增行、不重写文件**（幂等）。
3. `draft.raw_bytes` 非空 → 写 `raw_path`（`<data_dir>/research-material/<subject_kind>/<subject_id>/<kind>/<content_hash 前 2 位>/<content_hash><suffix>`）；先写 `.tmp` 再 `os.replace`，父目录 `mkdir(parents=True, exist_ok=True)`。`raw_ref` 存**相对 `data_dir`** 的路径。
4. `insert_version()` 落库：`revision`/`previous_id` 由 02 的仓储计算；`raw_ref` 与 `payload` 一起写入。返回 `created=True`。

不变量（测试必须覆盖）：
- 同 content_hash 第二次 `save()` → `created=False`、`research_dataset` 行数不变、文件 mtime 不变。
- payload 里任一 `raw_value` 变化 → content_hash 变化 → 新行（`revision+1`、`previous_id` 指向旧行），旧行内容与 `raw_ref` 完全不变。
- `captured_at` **不参与** content_hash（同一份内容在不同时间导入不应产生新版本），但 `source_fetched_at` 也不参与；两者都只作为行字段。
- 无 `raw_bytes`（如未来某些合成资料）→ `raw_ref is None` 且不建目录。

### 离线导入

```python
@dataclass(frozen=True)
class ImportReport:
    saved: tuple[SavedDataset, ...]
    problems: tuple[str, ...]
    skipped: tuple[str, ...]

async def import_offline_evidence(
    session: AsyncSession, *, data_dir: Path, source_dir: Path,
    identities: Sequence[ControlledIdentity], company_ids: Mapping[str, int],
    sources: Sequence[str], *, imported_at: datetime | None = None,
) -> ImportReport
```

- 参数 `source_dir` 是调研证据目录（例如 `.../evidence/remaining-final-03/raw`），按 03/04 的适配器能识别的文件名定位原始响应与同名 `.meta.json`。
- `captured_at = imported_at or now(UTC)` —— **真实导入时间**；`source_fetched_at` 取 `.meta.json` 的 `fetched_at_utc`；`draft.raw_source_url` 取 `meta.url`。**不得**把 `fetched_at` 写进 `captured_at`，也不得回填其他过去时间。
- 单个文件解析失败只记入 `problems`，不中断其余导入（返回 `skipped` 列出）。
- `company_ids` 由调用方（CLI/测试）用 01 的名单解析 `company_key -> Company.id` 后传入；本模块不自己匹配公司。

## 验证

- `tests/unit/test_material_store.py`：
  - 幂等（同上）；版本递增与 `previous_id` 链；旧行不变（改 payload 后旧行的 `payload`/`raw_ref`/`content_hash` 逐字段相等）；
  - `raw_ref` 是相对路径，文件真实存在且内容 == `draft.raw_bytes`；路径含 hash 前 2 位目录；
  - `content_hash` 不受 `captured_at`/`source_fetched_at` 影响（同一 draft 改这两个字段 → `created=False`）；
  - 契约违规 draft（如 `ANNUAL` 只 300 天）→ `ValueError`，不落盘、不建行；
  - 相同内容但不同 `subject_id` → 各自独立行（hash 相同也不跨 subject 合并）。
- `tests/integration/test_material_offline_import.py`：
  - 用 `tests/fixtures/materials/offline/` 造一个临时 `source_dir` + 临时 `DATA_DIR`，导入汇丰 income 一年期：`saved` 非空、`captured_at == imported_at`、`source_fetched_at == meta.fetched_at_utc`、`raw_ref` 指向的字节与 fixture 一致；
  - **第二次导入同一目录** → 全部 `created=False`（离线重放幂等），行数不变；
  - 缺 `.meta.json` 的文件 → `problems` 有人可读的原因，其余仍导入；
  - `.meta.json` 里 `fetched_at_utc` 缺失 → `source_fetched_at is None` 且**不回填**导入时间（断言两者不相等）；
  - 真实证据目录存在时（`/Users/zhvala/SourceCode/zcharlie/research/market-data-validation/evidence/remaining-final-03/raw`，用 `pytest.mark.skipif(not Path(...).exists())` 守卫）导入汇丰三表四年：`problems == ()`、产生 ≥ 3 个数据集、`captured_at` 是本次运行时间而 `source_fetched_at` 是 2026-10-11。此用例在 CI（无该目录）自动跳过，不影响门禁。

## 提交

```
feat(materials): immutable dataset store with raw artifacts and offline import
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总）
