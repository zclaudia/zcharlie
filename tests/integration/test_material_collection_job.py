"""Material collection end to end: handler, worker and real evidence (plan 06).

Two layers live here.

The first drives :class:`~investment_agent.materials.handlers.MaterialCollectionHandler`
against a seeded HK/US pilot listing with fake fetchers and fake adapters (no
socket, no sleep): the default-offline branch makes zero upstream calls, the
enabled branch stores datasets with their raw response, structural parse
problems do not consume the retry budget while transient failures do, an
uncovered security and an unbound source stay partial without a request, a
retried attempt resumes instead of redoing a finished source, re-running a
batch writes no second version, and a lost lease is not swallowed.

The second layer runs the real adapters over the saved research evidence: the
offline importer replays the four-year HSBC responses exactly as the checked
evidence holds them, and the handler's own fetch/parse path replays the same
bytes through the production assembly (which is what injects ``Company.id``
into ``parse``). Both are guarded by ``skipif``, so a checkout without
``research/`` still gates clean.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select

from investment_agent.config import Settings
from investment_agent.domain.security import (
    DEFAULT_PILOT_LIST_PATH,
    ControlledIdentity,
    load_pilot_identities,
)
from investment_agent.jobs.base import ClaimedJob
from investment_agent.materials import handlers
from investment_agent.materials.contracts import (
    SCHEMA_VERSION,
    AccountingStandard,
    DatasetDraft,
    DatasetKind,
    DatePrecision,
    EquityScope,
    FactPeriod,
    FactProvenance,
    FactRecord,
    FactSemantics,
    FactValue,
    ParseResult,
    PeriodBasis,
    PerShareBasis,
    RawPayload,
    ReportingScope,
    SubjectKind,
    UpstreamRequest,
    Verification,
    fact_to_payload,
)
from investment_agent.materials.handlers import MaterialCollectionHandler
from investment_agent.materials.hk_native import HkNativeAdapter
from investment_agent.materials.jobs import (
    MATERIAL_SOURCES,
    MaterialCollectionPayload,
    enqueue_material_job,
)
from investment_agent.materials.offline_import import import_offline_evidence
from investment_agent.providers.errors import ProviderTransientError
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.research_material import ResearchDataset
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import (
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
)
from tests.unit._hk_us_support import pilot_identities, seed_pilot_listings

HK_SOURCE = "eastmoney.hk.native.v1"
SEC_SOURCE = "sec.companyfacts.v1"
HSBC = "hsbc-holdings"
CLP = "clp-holdings"
BATCH = "2026-10-12"
REPLAY_FILES: Mapping[str, str] = {
    "income": "income-four-years.json",
    "balance": "balance-four-years.json",
    "cashflow": "cashflow-original-four-years.json",
}
"""The four-year responses, under the document names the adapter requests."""

HSBC_FETCHED_AT = datetime(2026, 10, 11, 15, 47, 50, 746113, tzinfo=UTC)
"""The original research fetch time the evidence sidecars carry."""

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "materials" / "offline"
SAVED_EVIDENCE = Path(
    "/Users/zhvala/SourceCode/zcharlie/research/market-data-validation/evidence/"
    "remaining-final-03/raw"
)
HSBC_NATIVE = SAVED_EVIDENCE / "native" / "00005"
needs_evidence = pytest.mark.skipif(
    not (HSBC_NATIVE / "income-four-years.json").is_file(),
    reason="the saved research evidence directory is not present in this checkout",
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key="test",
        llm_tier3_api_key="test",
        data_dir=tmp_path,
    )


def _replay_response(name: str) -> RawPayload:
    """The checked evidence body of one HK table request, as a fresh response."""

    path = FIXTURE_DIR / "00005" / "income-four-years.json"
    return RawPayload(
        name=name,
        content=path.read_bytes(),
        media_type="application/json",
        fetched_at=datetime.now(UTC),
        source_url="https://example.invalid/material/income",
        http_status=200,
    )


def _utc(moment: datetime) -> datetime:
    """SQLite drops ``tzinfo`` on read-back; compare instants in UTC."""

    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def _identity(company_key: str) -> ControlledIdentity:
    return next(
        identity
        for identity in load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
        if identity.company_key == company_key
    )


async def _seed(session) -> dict[str, int]:
    """Seed the markets and the eight pilot listings; return company_key -> id."""

    listings = await seed_pilot_listings(session)
    await session.commit()
    return {
        identity.company_key: listings[(identity.market_code, identity.ticker)].company_id
        for identity in pilot_identities()
    }


async def _listing_id(session, market_code: str, ticker: str) -> int:
    listing = await ListingRepository(session).get_by_ticker(market_code, ticker)
    assert listing is not None, f"{market_code}/{ticker} was not seeded"
    return listing.id


@dataclass
class _Ctx:
    """The worker's side of ``JobContext``, without a worker around it."""

    worker_id: str = "test-worker"

    async def run_with_heartbeat(self, lease: RunLease, work) -> object:
        return await work()


async def _claim(
    payload: MaterialCollectionPayload, *, worker_id: str = "test-worker"
) -> tuple[ClaimedJob, int]:
    """Enqueue and claim one run; return the job the worker would hand over."""

    async with session_scope() as session:
        run, created = await enqueue_material_job(session, payload, triggered_by="test")
        assert created is True
        claimed = await PipelineRunRepository(session).claim_next(worker_id=worker_id)
        assert claimed is not None and claimed.id == run.id
        return (
            ClaimedJob(
                run_id=claimed.id,
                kind=claimed.kind,
                lease=RunLease(claimed.id, worker_id, claimed.attempt_count),
                listing_id=claimed.listing_id,
                universe_code=claimed.universe_code,
                request_json=dict(claimed.request_json or {}),
            ),
            claimed.id,
        )


async def _run_row(run_id: int):
    async with session_scope() as session:
        run = await PipelineRunRepository(session).get(run_id)
        assert run is not None
        return run


async def _datasets() -> list[ResearchDataset]:
    async with session_scope() as session:
        return list((await session.execute(select(ResearchDataset))).scalars())


async def _dataset_count() -> int:
    async with session_scope() as session:
        return (
            await session.execute(select(func.count()).select_from(ResearchDataset))
        ).scalar_one()


@dataclass
class FakeFetcher:
    """A ``MaterialFetcher`` that answers from a script and never dials out."""

    responses: Mapping[str, RawPayload] = field(default_factory=dict)
    error: Exception | None = None
    error_on_call: int | None = None
    calls: list[UpstreamRequest] = field(default_factory=list)

    async def fetch(
        self, request: UpstreamRequest, *, identity: ControlledIdentity
    ) -> RawPayload:
        self.calls.append(request)
        if self.error is not None and self.error_on_call in (None, len(self.calls)):
            raise self.error
        raw = self.responses.get(request.name)
        assert raw is not None, f"unexpected request {request.name!r}"
        return raw


@dataclass
class FakeAdapter:
    """Minimal source adapter: one request, one draft, scripted problems.

    ``parse`` records the ``subject_id`` it was given; because the draft is
    built from that value, a missing injection produces an invalid draft and
    the store refuses it instead of storing a document without a subject.
    ``emit_draft=False`` models the table-level case where a report produced no
    dataset at all (task 03 skips a table it could not read).
    """

    source: str
    adapter_version: str = "fake-1"
    request_name: str = "income.json"
    problems: tuple[str, ...] = ()
    emit_draft: bool = True
    subject_ids: list[int | None] = field(default_factory=list)
    raw_names: list[tuple[str, ...]] = field(default_factory=list)

    def requests(
        self, identity: ControlledIdentity, *, periods: Sequence[FactPeriod]
    ) -> tuple[UpstreamRequest, ...]:
        return (
            UpstreamRequest(name=self.request_name, url="https://example.invalid/material"),
        )

    def parse(
        self,
        identity: ControlledIdentity,
        *,
        raw: Sequence[RawPayload],
        periods: Sequence[FactPeriod],
        subject_id: int | None = None,
    ) -> ParseResult:
        self.subject_ids.append(subject_id)
        self.raw_names.append(tuple(item.name for item in raw))
        drafts = (_draft(source=self.source, subject_id=subject_id),) if self.emit_draft else ()
        return ParseResult(drafts=drafts, problems=self.problems)


def _draft(*, source: str, subject_id: int | None, raw_value: str = "1000") -> DatasetDraft:
    """One contract-valid annual-facts draft for the fake adapters."""

    period = FactPeriod(start=date(2024, 1, 1), end=date(2024, 12, 31), basis=PeriodBasis.ANNUAL)
    fact = FactRecord(
        metric="revenue_operating",
        period=period,
        semantics=FactSemantics(
            metric="revenue_operating",
            source_label="经营收入总额",
            source_field="001003999",
            accounting_standard=AccountingStandard.IFRS,
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=EquityScope.ORDINARY,
            per_share_basis=PerShareBasis.NA,
        ),
        value=FactValue(
            raw_value=raw_value,
            raw_unit="元",
            scale=1,
            unit="USD",
            currency="USD",
            sign_convention="as reported",
        ),
        provenance=FactProvenance(
            source=source,
            accession=None,
            original_hash=None,
            source_published_at=None,
            source_published_date=None,
            source_published_precision=DatePrecision.UNKNOWN,
            accepted_at=None,
            source_available_at=None,
        ),
        verification=Verification.CANDIDATE,
        conflict="none",
        applicability="applicable",
    )
    return DatasetDraft(
        kind=DatasetKind.ANNUAL_FACTS,
        subject_kind=SubjectKind.COMPANY,
        subject_id=subject_id,  # type: ignore[arg-type]  (None only if the handler forgot to inject)
        source=source,
        schema_version=SCHEMA_VERSION,
        adapter_version="fake-1",
        captured_at=datetime.now(UTC),
        payload={"facts": [fact_to_payload(fact)], "report": "income"},
        period=period,
    )


def _handler(
    tmp_path: Path, fetcher: FakeFetcher, *, sources: Sequence[str] = (HK_SOURCE,)
) -> MaterialCollectionHandler:
    return MaterialCollectionHandler(
        settings=_settings(tmp_path),
        fetcher=fetcher,
        adapters={source: FakeAdapter(source=source) for source in sources},
    )


def _open_the_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handlers, "ONLINE_REFRESH_ENABLED", True)


# --- the default-offline branch --------------------------------------------


async def test_default_offline_branch_makes_no_upstream_call(
    db_session, tmp_path: Path
) -> None:
    companies = await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = FakeFetcher()
    handler = _handler(tmp_path, fetcher, sources=MATERIAL_SOURCES)
    job, run_id = await _claim(
        MaterialCollectionPayload(listing_id=listing_id, refresh_batch=BATCH)
    )

    assert handlers.ONLINE_REFRESH_ENABLED is False
    assert await handler.execute(job, _Ctx()) == "partial"

    run = await _run_row(run_id)
    assert run.status == "partial"
    assert fetcher.calls == []
    metadata = run.metadata_json
    assert "online_refresh_disabled" in metadata["problems"]
    assert metadata["source_status"] == {
        HK_SOURCE: "online_refresh_disabled",
        SEC_SOURCE: "online_refresh_disabled",
    }
    assert metadata["checked"] == []
    assert metadata["listing_status"] == "covered"
    assert metadata["subject"] == {
        "listing_id": listing_id,
        "company_id": companies[HSBC],
        "market_code": "HKEX",
        "ticker": "00005",
        "company_key": HSBC,
    }
    assert await _dataset_count() == 0


# --- the enabled branch -----------------------------------------------------


async def test_enabled_branch_stores_the_dataset_and_its_raw_response(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    companies = await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = FakeFetcher(responses={"income.json": _replay_response("income.json")})
    adapter = FakeAdapter(source=HK_SOURCE)
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path), fetcher=fetcher, adapters={HK_SOURCE: adapter}
    )
    job, run_id = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id, sources=(HK_SOURCE,), refresh_batch=BATCH
        )
    )

    assert await handler.execute(job, _Ctx()) == "completed"

    run = await _run_row(run_id)
    assert run.status == "success"
    assert [request.name for request in fetcher.calls] == ["income.json"]
    # the assembly injects Company.id into parse (a checked identity has no id)
    assert adapter.subject_ids == [companies[HSBC]]
    assert adapter.raw_names == [("income.json",)]

    rows = await _datasets()
    assert len(rows) == 1
    row = rows[0]
    assert (row.kind, row.subject_kind, row.source) == ("annual_facts", "company", HK_SOURCE)
    assert row.subject_id == companies[HSBC]
    assert row.raw_ref is not None
    artifact = tmp_path / row.raw_ref
    assert artifact.read_bytes() == _replay_response("income.json").content
    assert row.content_hash in row.raw_ref

    metadata = run.metadata_json
    assert metadata["listing_status"] == "covered"
    assert metadata["checked"] == [HK_SOURCE]
    assert metadata["source_status"] == {HK_SOURCE: "saved"}
    assert metadata["problems"] == []
    assert metadata["informational"] == []
    (saved,) = metadata["saved"]
    assert saved == {
        "source": HK_SOURCE,
        "dataset_id": row.id,
        "created": True,
        "content_hash": row.content_hash,
    }
    (response,) = metadata["responses"][HK_SOURCE]
    assert (response["name"], response["http_status"]) == ("income.json", 200)
    assert response["bytes"] == len(_replay_response("income.json").content)


async def test_a_single_response_source_writes_its_raw_artifact(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One response per source is unambiguous, so the store keeps the bytes."""

    _open_the_gate(monkeypatch)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    body = b'{"success": true, "result": {"count": 0, "pages": 1, "data": []}}'
    fetcher = FakeFetcher(
        responses={
            "income.json": RawPayload(
                name="income.json",
                content=body,
                media_type="application/json",
                fetched_at=datetime.now(UTC),
                source_url="https://example.invalid/material/income",
                http_status=200,
            )
        }
    )
    handler = _handler(tmp_path, fetcher)
    job, run_id = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id, sources=(HK_SOURCE,), refresh_batch=BATCH
        )
    )

    assert await handler.execute(job, _Ctx()) == "completed"

    (row,) = await _datasets()
    assert row.raw_ref is not None
    assert (tmp_path / row.raw_ref).read_bytes() == body
    assert (await _run_row(run_id)).status == "success"


# --- structural vs informational problems ----------------------------------


async def test_structural_problems_stay_partial_and_are_not_retried(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = FakeFetcher(responses={"income.json": _replay_response("income.json")})
    adapter = FakeAdapter(
        source=HK_SOURCE, problems=("pagination_truncated: income",), emit_draft=False
    )
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path), fetcher=fetcher, adapters={HK_SOURCE: adapter}
    )
    job, run_id = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id, sources=(HK_SOURCE,), refresh_batch=BATCH
        )
    )

    assert await handler.execute(job, _Ctx()) == "partial"

    run = await _run_row(run_id)
    assert run.status == "partial"
    assert run.attempt_count == 1  # nothing re-queued the run
    assert run.metadata_json["source_status"] == {HK_SOURCE: "structural_failure"}
    assert run.metadata_json["problems"] == [f"{HK_SOURCE}: pagination_truncated: income"]
    assert run.metadata_json["informational"] == []
    assert await _dataset_count() == 0


async def test_informational_counters_leave_the_outcome_alone(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = FakeFetcher(responses={"income.json": _replay_response("income.json")})
    adapter = FakeAdapter(source=HK_SOURCE, problems=("unmapped_items: income/124",))
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path), fetcher=fetcher, adapters={HK_SOURCE: adapter}
    )
    job, run_id = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id, sources=(HK_SOURCE,), refresh_batch=BATCH
        )
    )

    assert await handler.execute(job, _Ctx()) == "completed"

    run = await _run_row(run_id)
    assert run.status == "success"
    assert run.metadata_json["source_status"] == {HK_SOURCE: "saved"}
    assert run.metadata_json["problems"] == []
    assert run.metadata_json["informational"] == [
        f"{HK_SOURCE}: unmapped_items: income/124"
    ]
    assert await _dataset_count() == 1


async def test_an_adapter_version_outside_the_licensed_scope_is_not_fetched(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = FakeFetcher()
    adapter = FakeAdapter(source=HK_SOURCE, adapter_version="2")
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path), fetcher=fetcher, adapters={HK_SOURCE: adapter}
    )
    job, run_id = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id,
            sources=(HK_SOURCE,),
            refresh_batch=BATCH,
            adapter_versions={HK_SOURCE: "1"},
        )
    )

    assert await handler.execute(job, _Ctx()) == "partial"

    run = await _run_row(run_id)
    assert run.status == "partial"
    assert fetcher.calls == []
    assert run.metadata_json["source_status"] == {HK_SOURCE: "adapter_version_mismatch"}
    assert "adapter_version_mismatch" in run.metadata_json["problems"][0]


# --- transient failures, retry and resume ----------------------------------


@dataclass
class TwoStageAdapter:
    """An adapter that names a document only after a parse (04's shard shape).

    ``SecFactsAdapter`` returns CompanyFacts + Submissions first and appends the
    historical shards once ``parse`` has seen Submissions, so a single fetch
    round would silently skip every shard.
    """

    source: str = SEC_SOURCE
    adapter_version: str = "two-stage"
    parsed: list[tuple[str, ...]] = field(default_factory=list)
    second_stage: bool = False

    def requests(
        self, identity: ControlledIdentity, *, periods: Sequence[FactPeriod]
    ) -> tuple[UpstreamRequest, ...]:
        documents = (
            UpstreamRequest(name="companyfacts.json", url="https://example.invalid/facts"),
            UpstreamRequest(name="submissions.json", url="https://example.invalid/submissions"),
        )
        if not self.second_stage:
            return documents
        return (*documents, UpstreamRequest(name="shards/0001.json", url="https://example.invalid/shards"))

    def parse(
        self,
        identity: ControlledIdentity,
        *,
        raw: Sequence[RawPayload],
        periods: Sequence[FactPeriod],
        subject_id: int | None = None,
    ) -> ParseResult:
        self.parsed.append(tuple(item.name for item in raw))
        self.second_stage = True
        if len(raw) < 3:
            return ParseResult(drafts=(), problems=())
        return ParseResult(drafts=(_draft(source=self.source, subject_id=subject_id),), problems=())


async def test_an_adapter_may_ask_for_further_documents_after_a_parse(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = FakeFetcher(
        responses={
            name: _replay_response(name)
            for name in ("companyfacts.json", "submissions.json", "shards/0001.json")
        }
    )
    adapter = TwoStageAdapter()
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path), fetcher=fetcher, adapters={SEC_SOURCE: adapter}
    )
    job, run_id = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id, sources=(SEC_SOURCE,), refresh_batch=BATCH
        )
    )

    assert await handler.execute(job, _Ctx()) == "completed"

    assert [request.name for request in fetcher.calls] == [
        "companyfacts.json",
        "submissions.json",
        "shards/0001.json",
    ]
    # The second round re-parses with everything fetched so far, shards included.
    assert adapter.parsed == [
        ("companyfacts.json", "submissions.json"),
        ("companyfacts.json", "submissions.json", "shards/0001.json"),
    ]
    run = await _run_row(run_id)
    assert run.status == "success"
    assert len(run.metadata_json["responses"][SEC_SOURCE]) == 3
    assert await _dataset_count() == 1


async def test_a_transient_failure_is_retried_and_the_finished_source_is_kept(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = FakeFetcher(
        responses={
            "income.json": _replay_response("income.json"),
            "companyfacts.json": _replay_response("companyfacts.json"),
        },
        error=ProviderTransientError("upstream is down", provider=SEC_SOURCE),
        error_on_call=2,
    )
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path),
        fetcher=fetcher,
        adapters={
            HK_SOURCE: FakeAdapter(source=HK_SOURCE),
            SEC_SOURCE: FakeAdapter(source=SEC_SOURCE, request_name="companyfacts.json"),
        },
    )
    worker = ResearchWorker(
        settings=_settings(tmp_path),
        worker_id="test-worker",
        handlers=[handler],
        retry_base_delay_seconds=0.0,
    )
    async with session_scope() as session:
        run, _ = await enqueue_material_job(
            session,
            MaterialCollectionPayload(listing_id=listing_id, refresh_batch=BATCH),
            triggered_by="test",
        )
        run_id = run.id

    started = datetime.now(UTC)
    assert await worker.run_once() == run_id

    run = await _run_row(run_id)
    assert run.status == "queued"
    assert run.attempt_count == 1
    assert run.available_at is not None
    assert _utc(run.available_at) > started  # scheduled for a later moment
    assert "retry scheduled" in (run.error_summary or "")
    assert [request.name for request in fetcher.calls] == ["income.json", "companyfacts.json"]
    metadata = run.metadata_json
    assert metadata["checked"] == [HK_SOURCE]
    assert metadata["source_status"] == {HK_SOURCE: "saved"}
    assert SEC_SOURCE not in metadata["source_status"]
    assert await _dataset_count() == 1

    # The retry resumes: the finished source is not fetched or stored again.
    assert await worker.run_once() == run_id
    run = await _run_row(run_id)
    assert run.status == "success"
    assert [request.name for request in fetcher.calls] == [
        "income.json",
        "companyfacts.json",
        "companyfacts.json",
    ]
    assert run.metadata_json["checked"] == [HK_SOURCE, SEC_SOURCE]
    assert run.metadata_json["skipped"] == [
        {"source": HK_SOURCE, "reason": "already_checked"}
    ]
    assert await _dataset_count() == 2


async def test_a_new_batch_rerun_writes_no_second_version(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = FakeFetcher(responses={"income.json": _replay_response("income.json")})
    handler = _handler(tmp_path, fetcher)

    first_job, first_run = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id, sources=(HK_SOURCE,), refresh_batch=BATCH
        )
    )
    assert await handler.execute(first_job, _Ctx()) == "completed"
    (first_row,) = await _datasets()
    assert first_row.raw_ref is not None
    mtime = (tmp_path / first_row.raw_ref).stat().st_mtime_ns

    second_job, second_run = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id, sources=(HK_SOURCE,), refresh_batch="2026-10-13"
        )
    )
    assert await handler.execute(second_job, _Ctx()) == "completed"

    assert await _dataset_count() == 1
    run = await _run_row(second_run)
    assert run.metadata_json["saved"] == [
        {
            "source": HK_SOURCE,
            "dataset_id": first_row.id,
            "created": False,
            "content_hash": first_row.content_hash,
        }
    ]
    assert (tmp_path / first_row.raw_ref).stat().st_mtime_ns == mtime
    assert (await _run_row(first_run)).status == "success"


async def test_a_lost_lease_is_not_swallowed_and_the_checkpoint_survives(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = FakeFetcher(responses={"income.json": _replay_response("income.json")})
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path),
        fetcher=fetcher,
        adapters={
            HK_SOURCE: FakeAdapter(source=HK_SOURCE),
            SEC_SOURCE: FakeAdapter(source=SEC_SOURCE, request_name="companyfacts.json"),
        },
    )
    job, run_id = await _claim(
        MaterialCollectionPayload(listing_id=listing_id, refresh_batch=BATCH)
    )
    # The second source discovers that the run was recovered by another worker.
    fetcher.error = LeaseLostError(run_id, "recovered by another worker")
    fetcher.error_on_call = 2

    with pytest.raises(LeaseLostError):
        await handler.execute(job, _Ctx())

    # The first source's dataset and checkpoint were committed before the
    # error, and nothing closed the run (the worker owns the run's state now).
    assert await _dataset_count() == 1
    run = await _run_row(run_id)
    assert run.status == "running"
    assert run.metadata_json["checked"] == [HK_SOURCE]
    assert run.metadata_json["source_status"] == {HK_SOURCE: "saved"}


# --- uncovered securities and unbound sources ------------------------------


async def test_an_uncovered_listing_is_partial_without_a_request(
    db_session, tmp_path: Path
) -> None:
    await _seed(db_session)
    async with session_scope() as session:
        listing = await ListingRepository(session).upsert_listing(
            market_code="HKEX", ticker="00999", company_name="Unchecked Industries Limited"
        )
        await session.commit()
        unchecked_id = listing.id
    fetcher = FakeFetcher()
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path), fetcher=fetcher, adapters={}
    )

    job, run_id = await _claim(
        MaterialCollectionPayload(listing_id=unchecked_id, refresh_batch=BATCH)
    )
    assert await handler.execute(job, _Ctx()) == "partial"
    run = await _run_row(run_id)
    assert run.status == "partial"
    assert run.metadata_json["listing_status"] == "listing_not_covered"
    assert any(
        problem.startswith("listing_not_covered") for problem in run.metadata_json["problems"]
    )
    assert run.metadata_json["source_status"] == {}
    assert fetcher.calls == []

    # A listing id that exists in no table behaves the same way.
    missing_job, missing_run = await _claim(
        MaterialCollectionPayload(listing_id=987_654, refresh_batch=BATCH)
    )
    assert await handler.execute(missing_job, _Ctx()) == "partial"
    assert (await _run_row(missing_run)).metadata_json["listing_status"] == (
        "listing_not_covered"
    )
    assert fetcher.calls == []
    assert await _dataset_count() == 0


async def test_a_source_the_pilot_list_does_not_bind_is_skipped(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00002")  # CLP binds no SEC source
    fetcher = FakeFetcher(responses={"income.json": _replay_response("income.json")})
    handler = _handler(tmp_path, fetcher)
    job, run_id = await _claim(
        MaterialCollectionPayload(listing_id=listing_id, refresh_batch=BATCH)
    )

    assert await handler.execute(job, _Ctx()) == "partial"

    run = await _run_row(run_id)
    assert run.status == "partial"
    assert [request.name for request in fetcher.calls] == ["income.json"]
    assert run.metadata_json["source_status"] == {
        HK_SOURCE: "saved",
        SEC_SOURCE: "source_not_bound",
    }
    assert {"source": SEC_SOURCE, "reason": "source_not_bound"} in run.metadata_json["skipped"]
    assert await _dataset_count() == 1


# --- real adapters over the saved research evidence ------------------------


def _hsbc_evidence_dir(tmp_path: Path) -> Path:
    """The real four-year responses, named as the adapter's requests are.

    The saved probe files carry a ``-four-years`` suffix; the adapter asks for
    its documents by name (``income``/``balance``/``cashflow``) — exactly how
    the online path names each response after its request. Only the file names
    are normalised: the bytes and the sidecars (with their original fetch
    times) are copied unchanged.
    """

    target = tmp_path / "evidence" / "00005"
    target.mkdir(parents=True)
    for name, filename in REPLAY_FILES.items():
        shutil.copy(HSBC_NATIVE / filename, target / name)
        shutil.copy(HSBC_NATIVE / (filename + ".meta.json"), target / (name + ".meta.json"))
    return tmp_path / "evidence"


def _adapter_problem(problem: str, *, company_key: str, source: str) -> str:
    """Drop the ``<company_key>/<source>: `` label the offline importer adds."""

    label = f"{company_key}/{source}: "
    return problem[len(label) :] if problem.startswith(label) else problem


@needs_evidence
async def test_the_real_adapters_import_the_saved_hsbc_evidence(
    db_session, tmp_path: Path
) -> None:
    companies = await _seed(db_session)
    identity = _identity(HSBC)
    evidence = _hsbc_evidence_dir(tmp_path)
    data_dir = tmp_path / "data"
    adapter = HkNativeAdapter()

    before = datetime.now(UTC)
    report = await import_offline_evidence(
        db_session,
        data_dir=data_dir,
        source_dir=evidence,
        identities=(identity,),
        company_ids={HSBC: companies[HSBC]},
        adapters={HK_SOURCE: adapter},
    )
    after = datetime.now(UTC)

    # Every reported problem is an unmapped-line counter (task 03's inventory),
    # so nothing structural was found in the real evidence.
    structural = [
        problem
        for problem in report.problems
        if not handlers.is_informational_problem(
            _adapter_problem(problem, company_key=HSBC, source=HK_SOURCE)
        )
    ]
    assert structural == []
    assert len(report.informational) == 3  # one unmapped counter per table
    assert report.skipped == ()

    rows = await _datasets()
    assert len(report.saved) == len(rows) == 4  # all three tables in each fiscal year
    assert [outcome.created for outcome in report.saved] == [True] * 4
    assert {row.kind for row in rows} == {"annual_facts"}
    assert {row.period_end for row in rows} == {
        date(2021, 12, 31),
        date(2022, 12, 31),
        date(2023, 12, 31),
        date(2024, 12, 31),
    }
    for row in rows:
        assert (row.subject_kind, row.subject_id, row.source) == (
            "company",
            companies[HSBC],
            HK_SOURCE,
        )
        assert (row.adapter_version, row.schema_version) == ("1", SCHEMA_VERSION)
        # captured_at is this run; the original research fetch time is kept
        # apart and never back-filled into it.
        assert before <= _utc(row.captured_at) <= after
        assert row.source_fetched_at is not None
        assert _utc(row.source_fetched_at) == HSBC_FETCHED_AT < _utc(row.captured_at)
        # Three responses per source cannot be attributed to one dataset, so no
        # raw artifact is written (which response a payload came from is never
        # guessed — 05's rule, mirrored by the handler).
        assert row.raw_ref is None

    again = await import_offline_evidence(
        db_session,
        data_dir=data_dir,
        source_dir=evidence,
        identities=(identity,),
        company_ids={HSBC: companies[HSBC]},
        adapters={HK_SOURCE: HkNativeAdapter()},
        imported_at=datetime.now(UTC),
    )
    assert [outcome.created for outcome in again.saved] == [False] * 4
    assert [
        problem
        for problem in again.problems
        if not handlers.is_informational_problem(
            _adapter_problem(problem, company_key=HSBC, source=HK_SOURCE)
        )
    ] == []
    assert await _dataset_count() == 4


@dataclass
class EvidenceReplayFetcher:
    """Serves the saved evidence as if the responses had just arrived.

    The request names come from the adapter itself, so the same evidence backs
    the handler's production path (``requests -> fetch -> parse``) without a
    socket.
    """

    root: Path
    calls: list[UpstreamRequest] = field(default_factory=list)

    async def fetch(
        self, request: UpstreamRequest, *, identity: ControlledIdentity
    ) -> RawPayload:
        self.calls.append(request)
        path = self.root / "00005" / request.name
        sidecar = json.loads(
            path.with_name(path.name + ".meta.json").read_text(encoding="utf-8")
        )
        return RawPayload(
            name=request.name,
            content=path.read_bytes(),
            media_type=request.media_type,
            fetched_at=datetime.fromisoformat(sidecar["fetched_at_utc"]),
            source_url=sidecar["url"],
            http_status=sidecar["status"],
        )


@needs_evidence
async def test_the_handler_collects_the_saved_evidence_with_the_real_adapter(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _open_the_gate(monkeypatch)
    companies = await _seed(db_session)
    listing_id = await _listing_id(db_session, "HKEX", "00005")
    fetcher = EvidenceReplayFetcher(root=_hsbc_evidence_dir(tmp_path))
    handler = MaterialCollectionHandler(
        settings=_settings(tmp_path), fetcher=fetcher, adapters={HK_SOURCE: HkNativeAdapter()}
    )
    job, run_id = await _claim(
        MaterialCollectionPayload(
            listing_id=listing_id, sources=(HK_SOURCE,), refresh_batch=BATCH
        )
    )

    assert await handler.execute(job, _Ctx()) == "completed"

    run = await _run_row(run_id)
    assert run.status == "success"
    assert [request.name for request in fetcher.calls] == ["income", "balance", "cashflow"]
    metadata = run.metadata_json
    assert metadata["problems"] == []
    assert len(metadata["informational"]) == 3
    assert metadata["informational"][0].startswith(f"{HK_SOURCE}: unmapped_items: ")
    assert len(metadata["responses"][HK_SOURCE]) == 3

    rows = await _datasets()
    assert len(rows) == 4
    # The assembly path under test: listing -> company -> Company.id -> parse.
    assert {row.subject_id for row in rows} == {companies[HSBC]}
    assert {row.period_end for row in rows} == {
        date(2021, 12, 31),
        date(2022, 12, 31),
        date(2023, 12, 31),
        date(2024, 12, 31),
    }
    assert all(row.raw_ref is None for row in rows)  # three responses, no guess
