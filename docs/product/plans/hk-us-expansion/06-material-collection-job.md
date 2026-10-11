# 06 单证券资料采集任务与 handler

> 波次 W3 ‖ 可与 07、08 并行。依赖：05 已合并（`MaterialStore`）；03/04 已合并（适配器）。
> 文件所有权：见 [`README.md`](README.md) §4 第 06 行。本任务独占 `research/worker.py` 的改动（只加注册行）。

**目标：** 新增一个受控的单证券资料采集任务：复用 worker 的租约/幂等/取消/恢复，按公司与资料种类提交检查点，结构问题不盲重试，**在线刷新默认关闭**。

## 代码事实

- 任务 kind 常量与 payload 模型：`snapshots/jobs.py:49-88`（`SNAPSHOT_KINDS`）、`356-378`（`PAYLOAD_MODELS`）；幂等键实现 `388-438`；入队 `445-466`。
- 租约/幂等/重试/恢复全部在 worker：`research/worker.py:332-397`（`run_once`）、`399-424`（`run_with_heartbeat`）、`458-512`（`_settle_failure`）、`311-330`（`recover_stale`）。
- handler 协议：`jobs/base.py:54-84`（`on_claimed`/`execute`/`on_retry_scheduled`/`on_recovered`/`finalize`），空实现基类 `86-110`。
- 注册点：`research/worker.py:201-222` `default_job_handlers()`。
- 瞬态错误分类：`research/retry_policy.py:65-107`（httpx transport / 408 / 429 / 5xx / SQLite lock 为瞬态；配额与文件系统错误为终态）。
- 限速器（直接复用）：`snapshots/throttle.py:10-54` `AsyncRateLimiter`。
- provider 错误类型：`providers/errors.py:8-79`（`ProviderTransientError`/`ProviderDataError`/`ProviderError`）。
- **本任务不新增运行时设置项**（不碰 `config.py` / `settings/registry.py`）：限速、UA 用模块常量；在线开关用模块常量，避免与用户正在改动的设置面冲突。

## 文件清单

```
新增  src/investment_agent/materials/jobs.py
新增  src/investment_agent/materials/handlers.py
新增  tests/unit/test_material_jobs.py
新增  tests/integration/test_material_collection_job.py
修改  src/investment_agent/research/worker.py      （只在 default_job_handlers 里加一行注册）
```

## 实现

```python
# materials/jobs.py
MATERIAL_COLLECTION = "material_collection"
MATERIAL_SOURCES: tuple[str, ...] = ("eastmoney.hk.native.v1", "sec.companyfacts.v1")

class MaterialCollectionPayload(Contract):        # frozen, extra=forbid
    listing_id: int
    sources: tuple[str, ...] = MATERIAL_SOURCES
    periods: tuple[str, ...] = ()                 # ISO 期末日期；空 = 适配器默认窗口
    refresh_batch: str                            # 刷新批次（来源 + 日期），参与幂等键
    adapter_versions: dict[str, str] = {}         # 许可范围绑定：来源 -> 适配器版本
    reason: str = "user_refresh"

def material_idempotency_key(payload: MaterialCollectionPayload) -> str
    # "material_collection:{listing_id}:{sources}:{periods}:{refresh_batch}"，超 128 字符时改用 sha1 摘要

async def enqueue_material_job(
    session: AsyncSession, payload: MaterialCollectionPayload, *, triggered_by: str,
    max_attempts: int = 3,
) -> tuple[PipelineRun, bool]
    # 直接调 PipelineRunRepository.enqueue(kind=MATERIAL_COLLECTION, idempotency_key=..., request_json=payload...)
    # 不建 universe 行（资料不按 universe 归属）
```

```python
# materials/handlers.py
ONLINE_REFRESH_ENABLED = False   # 许可未确认前保持关闭（设计 §尚未验证能力：东财/Yahoo 使用许可未确认）

class MaterialFetcher(Protocol):
    async def fetch(self, request: UpstreamRequest, *, identity) -> RawPayload: ...

class HttpxMaterialFetcher:
    """最小 HTTPS 取数：httpx.AsyncClient + 每来源独立 AsyncRateLimiter + UA 常量。
    错误映射：连接/超时/408/429/5xx → ProviderTransientError；其他 4xx 与非 JSON 正文 → ProviderDataError。"""
    MIN_INTERVAL = {"sec.companyfacts.v1": 0.5, "eastmoney.hk.native.v1": 1.0}
    USER_AGENT = "zcharlie-research/0.1 (personal research)"

class MaterialCollectionHandler(BaseJobHandler):
    kind = MATERIAL_COLLECTION
    def __init__(self, *, settings, fetcher: MaterialFetcher | None = None,
                 adapters: Mapping[str, MaterialAdapter] | None = None, clock=...) -> None: ...
    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome: ...
```

`execute()` 流程：
1. `async with ctx.run_with_heartbeat():` 包住全部工作（与既有 handler 一致）。
2. reader：`listing_id` → `Listing` → `Company`；没有 company 或不在名单 → `mark_partial`，状态 `listing_not_covered`，不发起任何请求。
3. 用 01 的 `find_identity`/`source_symbol` 得到各来源的代码；名单缺该来源 → 记 `source_not_bound`（该来源跳过，不算失败）。
4. `ONLINE_REFRESH_ENABLED` 为假 → **不发请求**，`record_metadata({"problems": ["online_refresh_disabled"], ...})` 并 `mark_partial`（这是首期的默认行为，必须有一条测试断言"零网络调用"）。
5. 否则逐来源：`adapter.requests(identity, periods=...)` → `fetcher.fetch()` → `adapter.parse()` → 每个 draft `store.save()`；每处理完一个来源 `session.commit()` + `record_metadata` 写检查点（`{"checked": [...], "saved": [...], "problems": [...]}`），使恢复时不必重做已提交来源。
6. `ParseResult.problems` 非空 → 该来源标记 `structural_failure`，**不抛异常、不重试**（结构变化不盲重试）。
7. 网络瞬态错误 → 抛 `ProviderTransientError` 交给 worker 的重试预算；重试耗尽后 `_settle_failure` 会标失败（可用 `mark_partial` 先行记录已完成来源）。
8. 返回值：全部来源成功 → `"completed"`；有 `problems`/未覆盖来源 → `"partial"`（handler 自己 `mark_partial`）；价格/FX 这类将来才有的可选能力失败只影响对应能力（本任务里体现为"来源级 partial"）。

注册：在 `research/worker.py::default_job_handlers` 的返回列表里加
`MaterialCollectionHandler(settings=settings)`，位置与既有项并列，不改动其他行。

## 验证

- `tests/unit/test_material_jobs.py`：幂等键稳定性与字段敏感性（改 `periods`/`sources`/`refresh_batch` 各产生新键）；payload 拒绝未知来源与非法 `listing_id`；`enqueue_material_job` 二次调用返回 `created=False` 且不新增 `pipeline_run` 行；`request_json` 往返一致。
- `tests/integration/test_material_collection_job.py`（用 `_hk_us_support` 播种港股公司 + listing + 名单身份；用假 fetcher 与假适配器，**禁网**）：
  - 默认关闭分支：`ONLINE_REFRESH_ENABLED=False` 时 `execute` 后 `pipeline_run.status=="partial"`，假 fetcher 的调用计数为 **0**，元数据里 `problems` 含 `online_refresh_disabled`。
  - 打开分支（`monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)`）：假 fetcher 返回 fixture 原始响应 → 库中出现 `research_dataset` 行，`raw_ref` 文件存在；状态 `completed`。
  - 结构问题分支：适配器返回 `problems` → 状态 `partial`，元数据含该来源 `structural_failure`，**重试计数不增加**（`pipeline_run.attempt_count` 不变）。
  - 瞬态分支：fetcher 抛 `ProviderTransientError` → run 被调度重试（`status=="queued"`、`available_at > now`、`attempt_count==1`）。
  - 未覆盖证券：listing 不在名单 → `partial` + `listing_not_covered`，无请求。
  - 恢复/幂等：同一 payload 二次入队不新增 run；已提交来源在重跑时不重复写 dataset（第二次 `created=False`）。
  - 取消：在假 fetcher 里 `raise LeaseLostError` 后 `execute` 不吞异常（由 worker 层处理），断言异常向上传播且已有提交保留。

## 提交

```
feat(materials): per-security material collection job
```

## 完成记录

（由控制 agent 在 COMPLETION.md 汇总）
