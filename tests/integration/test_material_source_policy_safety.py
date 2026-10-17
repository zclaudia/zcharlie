from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from investment_agent.config import Settings
from investment_agent.domain.security import ControlledIdentity
from investment_agent.jobs.base import ClaimedJob
from investment_agent.materials import handlers, source_policy
from investment_agent.materials.contracts import (
    FactPeriod,
    ParseResult,
    RawPayload,
    UpstreamRequest,
)
from investment_agent.materials.handlers import HttpxMaterialFetcher, MaterialCollectionHandler
from investment_agent.materials.jobs import MaterialCollectionPayload, enqueue_material_job
from investment_agent.research.worker import ResearchWorker
from investment_agent.settings.store import SettingsStore
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.research_material import ResearchDataset
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from tests.integration.test_material_collection_job import (
    BATCH,
    HK_SOURCE,
    HSBC,
    SEC_SOURCE,
    _Ctx,
    _draft,
    _identity,
    _seed,
)
from tests.integration.test_material_collection_job import (
    _settings as _base_settings,
)
from tests.support.material_source_policy import synthetic_approved_source_policy


@pytest_asyncio.fixture
async def temporary_material_db(tmp_path: Path):
    """Use a file-backed temporary DB so settings can change during a request."""

    db_path = tmp_path / "material-policy-safety.db"
    async with engine_lifespan(f"sqlite+aiosqlite:///{db_path}") as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield


async def _save_ops(*, hk: bool, sec: bool) -> None:
    async with session_scope() as session:
        store = SettingsStore(session)
        await store.claim_import()
        await store.apply(
            {
                "materials_hk_refresh_enabled": hk,
                "materials_sec_refresh_enabled": sec,
            },
            actor="safety-test",
        )


async def _company_and_listing() -> tuple[int, int]:
    async with session_scope() as session:
        company_ids = await _seed(session)
        listing = await ListingRepository(session).get_by_ticker("HKEX", "00005")
        assert listing is not None
        return company_ids[HSBC], listing.id


async def _claimed_job(
    listing_id: int,
    *,
    sources: tuple[str, ...],
    adapter_versions: dict[str, str],
    max_attempts: int = 3,
) -> tuple[ClaimedJob, int]:
    async with session_scope() as session:
        payload = MaterialCollectionPayload(
            listing_id=listing_id,
            sources=sources,
            refresh_batch=BATCH,
            adapter_versions=adapter_versions,
        )
        run, created = await enqueue_material_job(
            session, payload, triggered_by="policy-safety-test", max_attempts=max_attempts
        )
        assert created
        claimed = await PipelineRunRepository(session).claim_next(worker_id="safety-worker")
        assert claimed is not None
        return (
            ClaimedJob(
                run_id=claimed.id,
                kind=claimed.kind,
                lease=RunLease(claimed.id, "safety-worker", claimed.attempt_count),
                listing_id=claimed.listing_id,
                universe_code=claimed.universe_code,
                request_json=dict(claimed.request_json or {}),
            ),
            run.id,
        )


async def _run(run_id: int) -> PipelineRun:
    async with session_scope() as session:
        row = await PipelineRunRepository(session).get(run_id)
        assert row is not None
        return row


class _Adapter:
    source = HK_SOURCE
    adapter_version = "policy-safety-1"

    def __init__(self) -> None:
        self.stage_two = False

    def requests(
        self, identity: ControlledIdentity, *, periods: Sequence[FactPeriod]
    ) -> tuple[UpstreamRequest, ...]:
        if not self.stage_two:
            return (
                UpstreamRequest(
                    name="stage-one.json",
                    url="https://datacenter.eastmoney.com/stage-one",
                ),
            )
        return (
            UpstreamRequest(
                name="stage-two.json",
                url="https://datacenter.eastmoney.com/stage-two",
            ),
        )

    def parse(
        self,
        identity: ControlledIdentity,
        *,
        raw: Sequence[RawPayload],
        periods: Sequence[FactPeriod],
        subject_id: int | None = None,
    ) -> ParseResult:
        if any(item.name == "stage-one.json" for item in raw):
            self.stage_two = True
        if any(item.name == "stage-two.json" for item in raw):
            return ParseResult(
                drafts=(
                    replace(
                        _draft(source=self.source, subject_id=subject_id),
                        adapter_version=self.adapter_version,
                    ),
                ),
                problems=(),
            )
        return ParseResult(drafts=(), problems=())


class _OneRequestAdapter:
    def __init__(self, source: str, adapter_version: str, host: str, name: str) -> None:
        self.source = source
        self.adapter_version = adapter_version
        self.host = host
        self.name = name

    def requests(
        self, identity: ControlledIdentity, *, periods: Sequence[FactPeriod]
    ) -> tuple[UpstreamRequest, ...]:
        return (
            UpstreamRequest(
                name=self.name,
                url=f"https://{self.host}/{self.name}",
            ),
        )

    def parse(
        self,
        identity: ControlledIdentity,
        *,
        raw: Sequence[RawPayload],
        periods: Sequence[FactPeriod],
        subject_id: int | None = None,
    ) -> ParseResult:
        if not raw:
            return ParseResult(drafts=(), problems=())
        return ParseResult(
            drafts=(
                replace(
                    _draft(source=self.source, subject_id=subject_id),
                    adapter_version=self.adapter_version,
                ),
            ),
            problems=(),
        )


class _WaitingLimiter:
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self.entered = entered
        self.release = release

    async def acquire(self) -> None:
        self.entered.set()
        await self.release.wait()


def _settings(tmp_path: Path) -> Settings:
    return _base_settings(tmp_path).model_copy(
        update={
            "materials_hk_refresh_enabled": True,
            "materials_sec_refresh_enabled": True,
        }
    )


async def test_ops_switch_closed_during_httpx_limiter_wait_prevents_send_and_restores_guard(
    temporary_material_db,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)
    _, listing_id = await _company_and_listing()
    await _save_ops(hk=True, sec=True)
    identity = _identity(HSBC)
    adapter = _Adapter()
    entered, release = asyncio.Event(), asyncio.Event()
    http_calls: list[httpx.Request] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        http_calls.append(request)
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    fetcher = HttpxMaterialFetcher(client=client, min_interval={HK_SOURCE: 0.0001})
    request = adapter.requests(identity, periods=())[0]
    fetcher._limiters[urlparse(request.url).netloc] = _WaitingLimiter(entered, release)
    original_guard_calls: list[str] = []

    async def original_guard(_request: UpstreamRequest) -> None:
        original_guard_calls.append(_request.name)

    fetcher.before_send = original_guard
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path),
        fetcher=fetcher,
        adapters={HK_SOURCE: adapter},
        approvals=synthetic_approved_source_policy(HK_SOURCE, identity, adapter.adapter_version),
    )
    job, run_id = await _claimed_job(
        listing_id,
        sources=(HK_SOURCE,),
        adapter_versions={HK_SOURCE: adapter.adapter_version},
    )

    task = asyncio.create_task(handler.execute(job, _Ctx()))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        await _save_ops(hk=False, sec=True)
    finally:
        release.set()
    assert await task == "partial"
    await client.aclose()

    run = await _run(run_id)
    assert http_calls == []
    assert original_guard_calls == ["stage-one.json"]
    assert fetcher.before_send is original_guard
    assert run.metadata_json["source_status"][HK_SOURCE] == "source_policy_disabled"
    assert run.metadata_json["responses"].get(HK_SOURCE, []) == []


async def test_ops_switch_between_adapter_shards_archives_only_first_response(
    temporary_material_db,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)
    _, listing_id = await _company_and_listing()
    await _save_ops(hk=True, sec=True)
    identity = _identity(HSBC)
    adapter = _Adapter()
    http_calls: list[str] = []
    original_guard_calls: list[str] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        name = Path(urlparse(str(request.url)).path).name + ".json"
        http_calls.append(name)
        if name == "stage-one.json":
            await _save_ops(hk=False, sec=True)
        return httpx.Response(200, json={"name": name})

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    fetcher = HttpxMaterialFetcher(client=client, min_interval={HK_SOURCE: 0.0001})

    async def original_guard(request: UpstreamRequest) -> None:
        original_guard_calls.append(request.name)

    fetcher.before_send = original_guard
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path),
        fetcher=fetcher,
        adapters={HK_SOURCE: adapter},
        approvals=synthetic_approved_source_policy(HK_SOURCE, identity, adapter.adapter_version),
    )
    job, run_id = await _claimed_job(
        listing_id,
        sources=(HK_SOURCE,),
        adapter_versions={HK_SOURCE: adapter.adapter_version},
    )

    assert await handler.execute(job, _Ctx()) == "partial"
    await client.aclose()

    run = await _run(run_id)
    assert http_calls == ["stage-one.json"]
    assert original_guard_calls == ["stage-one.json"]
    assert fetcher.before_send is original_guard
    assert run.metadata_json["source_status"][HK_SOURCE] == "source_policy_disabled"
    (response,) = run.metadata_json["responses"][HK_SOURCE]
    assert response["name"] == "stage-one.json"
    assert len(run.metadata_json["acquisitions"]) == 1
    manifest_ref = run.metadata_json["acquisitions"][0]["manifest_ref"]
    manifest = json.loads((tmp_path / manifest_ref).read_text(encoding="utf-8"))
    assert [row["name"] for row in manifest["responses"]] == ["stage-one.json"]
    assert manifest["expected_requests"] == ["stage-one.json", "stage-two.json"]
    assert await _dataset_rows() == []


async def _dataset_rows() -> list[ResearchDataset]:
    async with session_scope() as session:
        return list((await session.execute(select(ResearchDataset))).scalars())


async def test_retry_budget_exhaustion_keeps_saved_source_once_and_marks_run_failed(
    temporary_material_db,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)
    company_id, listing_id = await _company_and_listing()
    await _save_ops(hk=True, sec=True)
    identity = _identity(HSBC)
    hk_adapter = _OneRequestAdapter(
        HK_SOURCE, "policy-safety-hk-1", "datacenter.eastmoney.com", "hk.json"
    )
    sec_adapter = _OneRequestAdapter(
        SEC_SOURCE, "policy-safety-sec-1", "data.sec.gov", "sec.json"
    )
    http_calls: list[str] = []

    async def transport(request: httpx.Request) -> httpx.Response:
        host = urlparse(str(request.url)).netloc
        http_calls.append(host)
        if host == "datacenter.eastmoney.com":
            return httpx.Response(503, json={"error": "temporary"})
        return httpx.Response(200, json={"ok": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    fetcher = HttpxMaterialFetcher(
        client=client,
        min_interval={HK_SOURCE: 0.0001, SEC_SOURCE: 0.0001},
    )
    approvals = {
        **synthetic_approved_source_policy(HK_SOURCE, identity, hk_adapter.adapter_version),
        **synthetic_approved_source_policy(SEC_SOURCE, identity, sec_adapter.adapter_version),
    }
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path),
        fetcher=fetcher,
        adapters={HK_SOURCE: hk_adapter, SEC_SOURCE: sec_adapter},
        approvals=approvals,
    )
    async with session_scope() as session:
        payload = MaterialCollectionPayload(
            listing_id=listing_id,
            sources=(HK_SOURCE, SEC_SOURCE),
            refresh_batch=BATCH,
            adapter_versions={
                HK_SOURCE: hk_adapter.adapter_version,
                SEC_SOURCE: sec_adapter.adapter_version,
            },
        )
        queued, created = await enqueue_material_job(
            session, payload, triggered_by="policy-safety-test", max_attempts=2
        )
        assert created
        run_id = queued.id
    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="safety-worker",
        handlers=[handler],
        retry_base_delay_seconds=0,
        heartbeat_interval_seconds=60,
    )

    assert await worker.run_once() == run_id
    first_attempt = await _run(run_id)
    assert first_attempt.status == "queued"
    assert first_attempt.attempt_count == 1
    assert first_attempt.metadata_json["source_status"][SEC_SOURCE] == "saved"
    assert await worker.run_once() == run_id
    await client.aclose()

    final = await _run(run_id)
    assert final.status == "failed"  # plan 06 terminal retry-budget contract
    assert final.attempt_count == 2
    assert final.metadata_json["source_status"] == {
        HK_SOURCE: "transient_error",
        SEC_SOURCE: "saved",
    }
    assert len(final.metadata_json["saved"]) == 1
    rows = await _dataset_rows()
    assert len(rows) == 1
    assert (rows[0].source, rows[0].revision, rows[0].subject_id) == (
        SEC_SOURCE,
        1,
        company_id,
    )
    assert http_calls == [
        "datacenter.eastmoney.com",
        "data.sec.gov",
        "datacenter.eastmoney.com",
    ]


async def test_policy_content_hash_changes_when_controlled_record_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = _identity(HSBC)
    settings = Settings(api_key="test")
    original = source_policy.load_source_policy()
    first = source_policy.evaluate_source_policy(
        HK_SOURCE,
        identity,
        settings=settings,
        adapter_version="unused",
        global_enabled=False,
    )
    changed = json.loads(json.dumps(original))
    changed["sources"][HK_SOURCE]["licensing"]["citation"] = "synthetic:changed"
    monkeypatch.setattr(source_policy, "load_source_policy", lambda: changed)
    second = source_policy.evaluate_source_policy(
        HK_SOURCE,
        identity,
        settings=settings,
        adapter_version="unused",
        global_enabled=False,
    )
    assert first.policy_revision != second.policy_revision
