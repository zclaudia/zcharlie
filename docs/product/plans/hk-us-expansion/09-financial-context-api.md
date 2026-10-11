# 09 财务上下文读模型、只读 API 与刷新 API

> 波次 W4 ‖ 可与 10 并行。依赖：06、07、08 已合并。
> 文件所有权：见 [`README.md`](README.md) §4 第 09 行。本任务独占 `api/app.py`。

**目标：** 实现设计 §页面任务与接口方案 的两个端点：
- `GET /research/listings/{listing_id}/financial-context`：只读，返回真实报告期、字段、指标、来源引用、状态与能力集合；**不访问上游**。
- `POST /research/listings/{listing_id}/refresh`：绑定许可范围、数据提供方、适配版本与刷新批次，创建一个去重的单证券任务。

## 代码事实

- 路由注册顺序与 DI：`api/app.py:130-175`（`_register_protected_routes`，逐个 `include_router`）；`api/routes/research.py` 已用 `prefix="/research"`（两个 router 同前缀不冲突，路径不重叠）。
- 认证：`api/auth.py` 的 `verify_api_key_or_session`（cookie + CSRF origin 校验），写接口沿用；参考 `api/routes/stocks.py:47-51` 与 `api/routes/investor.py:37-52` 两种 DI 写法。
- `session_scope()` 用法：`api/routes/stocks.py:66`；`GET` 只读库，不触发采集。
- 业务错误→HTTP 的映射范例：`api/routes/investor.py:37-52`（`InvestorError.detail.code` → 404/422/409）。
- 任务入队（06 提供）：`materials/jobs.py::enqueue_material_job`。
- 读模型可用的资料仓储：`ResearchDatasetRepository`（02）+ `MaterialStore.latest`（05）。
- 现有 web 页面与 API 都按 A 股 ticker 校验；**本任务不改 web**（属 11）。

## 文件清单

```
新增  src/investment_agent/materials/context.py
新增  src/investment_agent/materials/refresh.py
新增  src/investment_agent/api/routes/research_materials.py
新增  src/investment_agent/api/schemas/research_materials.py
新增  tests/integration/test_api_research_materials.py
修改  src/investment_agent/api/app.py      （只加一行 include_router）
```

## 读模型

```python
# materials/context.py
@dataclass(frozen=True)
class FiscalYearView:  end: date; label: str | None; days: int | None; basis: str
@dataclass(frozen=True)
class FactView:
    metric: str; label: str; period_end: date; basis: str
    amount: str | None; unit: str; currency: str | None; missing_reason: str | None
    definition: str                 # 口径：source_label/equity_scope/reporting_scope/accounting_standard 的可读组合
    verification: str               # candidate | verified
    conflict: str
    source: str; source_date: date | None; locator: str | None   # 来源定位（页码/片段），不暴露 CIK/端点名
@dataclass(frozen=True)
class MetricView:
    key: str; label: str; status: str; value: str | None; unit: str; currency: str | None
    basis: str; periods: tuple[str, ...]; notes: tuple[str, ...]
@dataclass(frozen=True)
class StatusView:
    code: str        # 业务状态码
    affected: tuple[str, ...]   # 受影响功能
    next_step: str              # 下一步
@dataclass(frozen=True)
class FinancialContextView:
    listing_id: int; market: str; display_code: str; company_name: str
    security_type: str; quote_currency: str; report_currency: str
    covered: bool
    fiscal_years: tuple[FiscalYearView, ...]
    facts: tuple[FactView, ...]
    metrics: tuple[MetricView, ...]
    capabilities: dict[str, bool]     # financials/price/annual_pe/dividends/pb/online_refresh
    coverage: dict[str, int]          # periods_covered / facts_total / facts_candidate / facts_verified
    statuses: tuple[StatusView, ...]

async def financial_context(session: AsyncSession, listing_id: int) -> FinancialContextView
```

规则：
- **零 IO 上游**：本模块只 import 仓储、`materials` 的纯函数（`metrics`/`evidence.dataset_facts`）与 `materials.handlers.ONLINE_REFRESH_ENABLED` 这一个常量；**不 import `materials/hk_native.py`、`materials/us_sec.py`、`materials/offline_import.py`**（用测试断言这几个模块不在 `context` 的 import 图里），也不在 GET 路径上调用任何 fetcher。
- 资料事实一律经 07 的 `dataset_facts(dataset)` 读取；`facts` 视图的 `conflict`/`verification`/`amount`/`unit`/`currency`/`missing_reason` 全部来自 `FactRecord`，不要在 context 里重新解析 payload。
- 财年来自资料层的 `FactPeriod`（真实期间）；**不出现 `date(year, 12, 31)` 之类硬编码**，不把自然年当财年。
- 资料覆盖与核对覆盖**分别统计**（`facts_total` vs `facts_verified`），不合并成一个布尔。
- 状态码固定集合，逐个映射文案与下一步：
  `listing_not_covered`（证券不在受控名单 → 功能：全部财务研究；下一步：先完成身份核对）、
  `material_missing`（无任何 dataset → 财务展示/指标；下一步：创建资料采集任务）、
  `partial_verification`（有 candidate 未核对 → 关键结论；下一步：核对原件）、
  `metric_not_applicable`（某指标不适用 → 对应指标；下一步：查看口径说明）、
  `source_conflict`（存在 `conflict=unresolved` → 相关指标；下一步：人工判定）、
  `material_stale`（最新 dataset 的 `captured_at` 晚于…或期间落后于 `fiscal_years` 期望 → 展示；下一步：刷新）、
  `online_refresh_disabled`（在线刷新关闭 → 刷新入口；下一步：确认使用许可）。
- 能力位默认：`financials=True`（有资料即真）、`price=False`、`annual_pe=False`、`dividends=False`、`pb=False`、`online_refresh=False`（读 `materials.handlers.ONLINE_REFRESH_ENABLED`）。**未通过验收的能力一律 False，并在 `statuses` 里给出缺口**（设计：不默认提供）。
- 文案纪律：主产品文案不出现供应商端点名、`CIK`、`accession`、运行内部 ID；来源详情用"申报/原件 + 日期 + 页码"表达。

## 刷新入口

```python
# materials/refresh.py
@dataclass(frozen=True)
class RefreshRequest:
    listing_id: int; sources: tuple[str, ...]; periods: tuple[str, ...]; reason: str
@dataclass(frozen=True)
class RefreshOutcome:
    run_id: int; created: bool; idempotency_key: str; adapter_versions: dict[str, str]

async def request_refresh(session: AsyncSession, request: RefreshRequest) -> RefreshOutcome
```

- 结构问题必须先拒绝：listing 不存在 → 404；listing 不在名单 → 422 `listing_not_covered`；来源不在 `MATERIAL_SOURCES` → 422；`periods` 非 ISO 日期 → 422。
- `adapter_versions` 从 `materials.hk_native.ADAPTER_VERSION` / `materials.us_sec.ADAPTER_VERSION` 取（**绑定适配版本**，写进 payload 参与幂等键与追溯）。
- 通过 `enqueue_material_job(triggered_by="api:refresh")` 入队；已存在同幂等键的 run → 返回 `created=False` 与既有 `run_id`（去重）。
- 许可范围绑定：`refresh_batch = f"{sources}|{date.today().isoformat()}"`（同日同来源重复点击复用同一批次；跨日产生新批次）。

## 端点

```
GET  /research/listings/{listing_id}/financial-context   → FinancialContextResponse（200；404 无此 listing）
POST /research/listings/{listing_id}/refresh             → RefreshResponse（202；422 校验失败）
```

- 两者都用 `verify_api_key_or_session`；POST 额外要求 JSON body（`sources`/`periods`/`reason` 可选）。
- GET 严格只读：测试用 `monkeypatch` 让 `httpx.AsyncClient.request` 抛异常，断言 GET 仍 200（证明无上游调用）。
- 响应 schema 用 `api/schemas/research_materials.py` 显式声明（`extra="forbid"` 风格与 `api/schemas/stocks.py` 一致），不直接塞 dataclass。

## 验证（`tests/integration/test_api_research_materials.py`）

用 `api_client` fixture（`tests/conftest.py:105-126`）+ `_hk_us_support` 播种；资料用 05 的 `MaterialStore.save` 直接写入假 draft（fixture 值取 `tests/fixtures/materials/metrics/pilots.json`）。

| 用例 | 断言 |
| --- | --- |
| 未覆盖证券 | 播种一个不在名单的 listing → GET 200、`covered=False`、`statuses[0].code=="listing_not_covered"`、`capabilities` 全 False、`facts==[]` |
| 已覆盖但无资料 | 名单内 listing 无 dataset → GET 200、`material_missing`、`coverage.facts_total==0` |
| 有资料 | GET 200：`fiscal_years` 的 `end` 来自资料期间（**不是** 12-31 假设构造的年份）、`facts` 中 `candidate`/`verified` 分开计数、`metrics` 含 `roe_avg_equity` 的 `NOT_APPLICABLE`（银行） |
| 缺价格不阻断 | `capabilities["price"] is False`、`capabilities["financials"] is True`；`facts`/`metrics` 非空（设计：价格缺失不能造成整个公司研究失败） |
| GET 不访问上游 | 打桩 httpx 抛错 → GET 仍 200 |
| 无上游标识 | 响应 JSON 文本不含 `CIK`、`eastmoney`、`sec.gov`、`accession` 字样 |
| POST 去重 | 同 body 连发两次 → 第一次 `created=True`，第二次 `created=False` 且 `run_id` 相同；`pipeline_run` 只有一行 |
| POST 校验 | 不存在的 listing→404；名单外→422；未知来源→422；坏日期→422；每种都断言未新增 `pipeline_run` |
| POST 许可关闭 | POST 成功创建 run（`created=True`），但 `pipeline_run.metadata_json` 或响应不含"已采集"承诺；能力位仍 `online_refresh=False` |
| 认证 | 无 API key / 无会话 → 401/403（与既有 `test_api_stock_search.py:54-56` 的断言风格一致） |
| 只读约束 | GET 不产生 `pipeline_run` 行（前后计数相等） |

## 提交

```
feat(api): read-only financial context and material refresh endpoints
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总）
