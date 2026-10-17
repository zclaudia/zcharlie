"""Durable worker retry/cooldown behavior for AF-P4-07 polling."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from investment_agent.config import Settings
from investment_agent.evidence.disclosure_http import SourceResponse
from investment_agent.evidence.disclosure_pages import DisclosurePageService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import ClaimedJob, JobOutcome
from investment_agent.settings.loader import store_for
from investment_agent.storage.database import dispose_engine, init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.disclosure import DisclosureOriginal
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.disclosure_authorization import apply_authorization_command
from investment_agent.supplemental_rules.disclosure_contracts import (
    BackgroundDisclosureAuthorizationBody,
    BackgroundDisclosureAuthorizationCommand,
)
from investment_agent.supplemental_rules.disclosure_updates import (
    DISCLOSURE_POLL_KIND,
    DisclosurePollingHandler,
    DisclosurePollRequest,
    admit_disclosure_poll,
    maintain_disclosure_polls,
    resume_disclosure_poll,
)
from tests.support.p4_disclosures import fixture_protocol, fixture_source_response


class _InlineContext:
    worker_id = "p4-poll-retry-test"

    async def run_with_heartbeat(
        self, _lease: RunLease, work: Callable[[], Awaitable[JobOutcome]]
    ) -> JobOutcome:
        return await work()


class _RateLimitOnceHTTP:
    def __init__(self, published_at: datetime) -> None:
        self.published_at = published_at
        self.first = True
        self.calls = 0

    async def request_hop(
        self,
        url: str,
        purpose: str,
        *,
        body: bytes | None = None,
        max_bytes: int,
        timeout_seconds: float = 15,
    ) -> SourceResponse:
        del timeout_seconds
        self.calls += 1
        mode = "rate_limited" if self.first else "empty"
        self.first = False
        status, headers, content = fixture_source_response(
            purpose,
            body or b"",
            published_at=self.published_at,
            mode=mode,
        )
        if len(content) > max_bytes:
            raise AssertionError("fixture exceeded its response bound")
        return SourceResponse(status, headers, content, url)


class _FixtureModeHTTP:
    def __init__(self, *, mode: str, published_at: datetime) -> None:
        self.mode = mode
        self.published_at = published_at
        self.query_count = 0

    async def request_hop(
        self,
        url: str,
        purpose: str,
        *,
        body: bytes | None = None,
        max_bytes: int,
        timeout_seconds: float = 15,
    ) -> SourceResponse:
        del timeout_seconds
        if purpose == "query":
            self.query_count += 1
        status, headers, content = fixture_source_response(
            purpose,
            body or b"",
            published_at=self.published_at,
            mode=self.mode,
        )
        assert len(content) <= max_bytes
        return SourceResponse(status, headers, content, url)


class _UnknownTypeHTTP(_FixtureModeHTTP):
    async def request_hop(
        self,
        url: str,
        purpose: str,
        *,
        body: bytes | None = None,
        max_bytes: int,
        timeout_seconds: float = 15,
    ) -> SourceResponse:
        response = await super().request_hop(
            url,
            purpose,
            body=body,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )
        if purpose == "query" and response.status == 200:
            import json

            material = json.loads(response.body)
            if material["announcements"]:
                material["announcements"][0]["fixtureDocumentType"] = "new_unmapped_type"
            return SourceResponse(
                response.status,
                response.headers,
                json.dumps(material).encode(),
                response.url,
            )
        return response


class _RedirectOriginalHTTP(_FixtureModeHTTP):
    def __init__(self, *, published_at: datetime) -> None:
        super().__init__(mode="normal", published_at=published_at)
        self.original_urls: list[str] = []
        self.initial_original_url = (
            "https://static.cninfo.com.cn/finalpage/2026-10-16/10000001.PDF"
        )
        self.redirect_url = (
            "https://static.cninfo.com.cn/finalpage/2026-10-16/10000001-redirected.PDF"
        )

    async def request_hop(
        self,
        url: str,
        purpose: str,
        *,
        body: bytes | None = None,
        max_bytes: int,
        timeout_seconds: float = 15,
    ) -> SourceResponse:
        if purpose == "original":
            self.original_urls.append(url)
            if url == self.initial_original_url:
                return SourceResponse(
                    307,
                    {"content-type": "text/plain", "location": self.redirect_url},
                    b"controlled 307 redirect response",
                    url,
                )
        return await super().request_hop(
            url,
            purpose,
            body=body,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
        )


class _Clock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        self.current += timedelta(seconds=6)
        return self.current


@pytest_asyncio.fixture
async def p4_polling_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncEngine]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'p4-polling.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await dispose_engine()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="p4-polling-test",
        data_dir=tmp_path,
        supplemental_rules_enabled=True,
        supplemental_updates_enabled=True,
        supplemental_disclosure_polling_enabled=True,
        disclosure_originals_enabled=True,
    )


async def _claim(run_id: int | None = None) -> ClaimedJob | None:
    async with session_scope() as session:
        row = await PipelineRunRepository(session).claim_next(
            worker_id="p4-poll-retry-test",
            kind=DISCLOSURE_POLL_KIND,
            run_id=run_id,
        )
        if row is None:
            return None
        return ClaimedJob(
            run_id=row.id,
            kind=row.kind,
            lease=RunLease(row.id, "p4-poll-retry-test", row.attempt_count),
            request_json=dict(row.request_json),
        )


async def _drive(handler: DisclosurePollingHandler, run_id: int) -> None:
    next_run_id: int | None = run_id
    for _ in range(10):
        job = await _claim(next_run_id)
        if job is None:
            return
        next_run_id = None
        result = await handler.execute(job, cast(Any, _InlineContext()))
        assert result in {"completed", "partial"}
    raise AssertionError("disclosure poll exceeded its bounded empty-window steps")


@pytest.mark.asyncio
async def test_http_429_retry_after_cannot_be_resumed_early_and_poll_recovers(
    p4_polling_db: AsyncEngine,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC) - timedelta(minutes=1)
    history_start = now - timedelta(days=1)
    async with session_scope() as session:
        store = store_for(session, settings)
        await store.claim_import()
        await store.apply(
            {
                "supplemental_rules_enabled": True,
                "supplemental_updates_enabled": True,
                "supplemental_disclosure_polling_enabled": True,
                "disclosure_originals_enabled": True,
            },
            actor="test",
        )
        session.add(Market(code="SZSE", name="Controlled fixture", timezone="UTC", currency="CNY"))
        company = Company(canonical_name="P4 retry fixture")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SZSE", ticker="300901")
        session.add(listing)
        await session.flush()
        listing_ref = ObjectRef(kind="listing", id=listing.id, revision=1)
        grant_body = BackgroundDisclosureAuthorizationBody(
            contract_hash="a" * 64,
            listing_refs=(listing_ref,),
            doc_type_allowlist=("annual_report_full",),
            history_start=history_start,
            history_end=now + timedelta(days=1),
            min_poll_interval_seconds=21600,
            min_http_interval_seconds=5,
            expires_at=now + timedelta(days=5),
            max_staleness_seconds=86400,
            per_poll_query_http=4,
            per_poll_fetch=3,
            per_poll_http=12,
            per_poll_response_bytes=32 * 1024 * 1024,
            grant_daily_http=24,
            grant_daily_response_bytes=32 * 1024 * 1024,
            storage_bytes_cap=64 * 1024 * 1024,
        )
        command = BackgroundDisclosureAuthorizationCommand(
            action="create",
            operation_id=uuid4(),
            authorization_id=uuid4(),
            expected_revision=0,
            owner_daily_http=24,
            owner_daily_response_bytes=128 * 1024 * 1024,
            owner_min_request_interval_seconds=5,
            expected_owner_policy_revision=0,
            update_owner_policy=True,
            body=grant_body,
        )
        grant = (
            await apply_authorization_command(
                session,
                command,
                actor="api",
                now=now,
            )
        ).authorization
        admitted = await admit_disclosure_poll(
            session,
            DisclosurePollRequest(
                operation_id=uuid4(),
                authorization_id=grant.authorization_id,
                authorization_revision=grant.revision,
                authorization_hash=grant.authorization_hash,
                listing_ref=listing_ref,
                window_start=history_start,
                window_end=now,
                expected_source_revision=0,
            ),
            settings=settings,
            now=now,
        )
        listing_id = listing.id

    clock = _Clock(datetime.now(UTC))
    http = _RateLimitOnceHTTP(now - timedelta(hours=1))
    service = DisclosurePageService.for_tests(protocol=fixture_protocol(), http=http)
    handler = DisclosurePollingHandler.for_tests(
        settings=settings,
        page_service=service,
        clock=clock,
    )
    await _drive(handler, admitted["run_id"])
    async with session_scope() as session:
        source = await SupplementalUpdatesRepository(session).get_source(
            "local_owner",
            "cninfo_szse_v1",
            f"listing:{listing_id}:company_research",
        )
        assert source is not None and source.payload["poll_state"] == "retry_wait"
        assert source.retry_at is not None
        with pytest.raises(InvestorError) as early:
            await resume_disclosure_poll(
                session,
                UUID(str(admitted["poll_id"])),
                uuid4(),
                source.revision,
                "do not bypass server Retry-After",
                settings=settings,
                now=source.retry_at - timedelta(seconds=1),
            )
        assert early.value.detail.code == "source_cooldown"
        resumed = await resume_disclosure_poll(
            session,
            UUID(str(admitted["poll_id"])),
            uuid4(),
            source.revision,
            "resume after Retry-After",
            settings=settings,
            now=source.retry_at + timedelta(seconds=1),
        )
    clock.current = source.retry_at + timedelta(seconds=1)
    await _drive(handler, resumed["run_id"])
    assert http.calls > 2
    async with session_scope() as session:
        final_source = await SupplementalUpdatesRepository(session).get_source(
            "local_owner",
            "cninfo_szse_v1",
            f"listing:{listing_id}:company_research",
        )
        assert final_source is not None
        assert final_source.payload["poll_state"] == "completed"
        assert final_source.retry_at is None


@pytest.mark.asyncio
async def test_query_cap_marks_window_split_required_without_fetching_or_proving_complete(
    p4_polling_db: AsyncEngine,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC) - timedelta(minutes=1)
    history_start = now - timedelta(days=1)
    async with session_scope() as session:
        store = store_for(session, settings)
        await store.claim_import()
        await store.apply(
            {
                "supplemental_rules_enabled": True,
                "supplemental_updates_enabled": True,
                "supplemental_disclosure_polling_enabled": True,
                "disclosure_originals_enabled": True,
            },
            actor="test",
        )
        session.add(Market(code="SZSE", name="Controlled fixture", timezone="UTC", currency="CNY"))
        company = Company(canonical_name="P4 query cap fixture")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SZSE", ticker="300901")
        session.add(listing)
        await session.flush()
        listing_ref = ObjectRef(kind="listing", id=listing.id, revision=1)
        body = BackgroundDisclosureAuthorizationBody(
            contract_hash="a" * 64,
            listing_refs=(listing_ref,),
            doc_type_allowlist=("annual_report_full",),
            history_start=history_start,
            history_end=now + timedelta(days=1),
            min_poll_interval_seconds=21600,
            min_http_interval_seconds=5,
            expires_at=now + timedelta(days=5),
            max_staleness_seconds=86400,
            per_poll_query_http=1,
            per_poll_fetch=3,
            per_poll_http=12,
            per_poll_response_bytes=32 * 1024 * 1024,
            grant_daily_http=24,
            grant_daily_response_bytes=32 * 1024 * 1024,
            storage_bytes_cap=64 * 1024 * 1024,
        )
        grant = (
            await apply_authorization_command(
                session,
                BackgroundDisclosureAuthorizationCommand(
                    action="create",
                    operation_id=uuid4(),
                    authorization_id=uuid4(),
                    expected_revision=0,
                    owner_daily_http=24,
                    owner_daily_response_bytes=128 * 1024 * 1024,
                    owner_min_request_interval_seconds=5,
                    expected_owner_policy_revision=0,
                    update_owner_policy=True,
                    body=body,
                ),
                actor="api",
                now=now,
            )
        ).authorization
        admitted = await admit_disclosure_poll(
            session,
            DisclosurePollRequest(
                operation_id=uuid4(),
                authorization_id=grant.authorization_id,
                authorization_revision=grant.revision,
                authorization_hash=grant.authorization_hash,
                listing_ref=listing_ref,
                window_start=history_start,
                window_end=now,
                expected_source_revision=0,
            ),
            settings=settings,
            now=now,
        )
        listing_id = listing.id

    http = _FixtureModeHTTP(mode="two_pages", published_at=now - timedelta(hours=1))
    service = DisclosurePageService.for_tests(protocol=fixture_protocol(), http=http)
    handler = DisclosurePollingHandler.for_tests(
        settings=settings,
        page_service=service,
        clock=_Clock(datetime.now(UTC)),
    )
    await _drive(handler, admitted["run_id"])
    assert http.query_count == 1
    async with session_scope() as session:
        source = await SupplementalUpdatesRepository(session).get_source(
            "local_owner",
            "cninfo_szse_v1",
            f"listing:{listing_id}:company_research",
        )
        assert source is not None
        assert source.payload["poll_state"] == "window_split_required"
        assert source.payload["failure_code"] == "query_budget_exhausted"


@pytest.mark.asyncio
async def test_unknown_document_type_blocks_worker_completion_and_original_fetch(
    p4_polling_db: AsyncEngine,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC) - timedelta(minutes=1)
    history_start = now - timedelta(days=1)
    async with session_scope() as session:
        store = store_for(session, settings)
        await store.claim_import()
        await store.apply(
            {
                "supplemental_rules_enabled": True,
                "supplemental_updates_enabled": True,
                "supplemental_disclosure_polling_enabled": True,
                "disclosure_originals_enabled": True,
            },
            actor="test",
        )
        session.add(Market(code="SZSE", name="Controlled fixture", timezone="UTC", currency="CNY"))
        company = Company(canonical_name="P4 unknown type fixture")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SZSE", ticker="300901")
        session.add(listing)
        await session.flush()
        listing_ref = ObjectRef(kind="listing", id=listing.id, revision=1)
        body = BackgroundDisclosureAuthorizationBody(
            contract_hash="a" * 64,
            listing_refs=(listing_ref,),
            doc_type_allowlist=("annual_report_full",),
            history_start=history_start,
            history_end=now + timedelta(days=1),
            min_poll_interval_seconds=21600,
            min_http_interval_seconds=5,
            expires_at=now + timedelta(days=5),
            max_staleness_seconds=86400,
            per_poll_query_http=4,
            per_poll_fetch=3,
            per_poll_http=12,
            per_poll_response_bytes=32 * 1024 * 1024,
            grant_daily_http=24,
            grant_daily_response_bytes=32 * 1024 * 1024,
            storage_bytes_cap=64 * 1024 * 1024,
        )
        grant = (
            await apply_authorization_command(
                session,
                BackgroundDisclosureAuthorizationCommand(
                    action="create",
                    operation_id=uuid4(),
                    authorization_id=uuid4(),
                    expected_revision=0,
                    owner_daily_http=24,
                    owner_daily_response_bytes=128 * 1024 * 1024,
                    owner_min_request_interval_seconds=5,
                    expected_owner_policy_revision=0,
                    update_owner_policy=True,
                    body=body,
                ),
                actor="api",
                now=now,
            )
        ).authorization
        admitted = await admit_disclosure_poll(
            session,
            DisclosurePollRequest(
                operation_id=uuid4(),
                authorization_id=grant.authorization_id,
                authorization_revision=grant.revision,
                authorization_hash=grant.authorization_hash,
                listing_ref=listing_ref,
                window_start=history_start,
                window_end=now,
                expected_source_revision=0,
            ),
            settings=settings,
            now=now,
        )
        listing_id = listing.id

    http = _UnknownTypeHTTP(mode="normal", published_at=now - timedelta(hours=1))
    service = DisclosurePageService.for_tests(protocol=fixture_protocol(), http=http)
    handler = DisclosurePollingHandler.for_tests(
        settings=settings,
        page_service=service,
        clock=_Clock(datetime.now(UTC)),
    )
    await _drive(handler, admitted["run_id"])
    assert http.query_count == 1
    async with session_scope() as session:
        source = await SupplementalUpdatesRepository(session).get_source(
            "local_owner",
            "cninfo_szse_v1",
            f"listing:{listing_id}:company_research",
        )
        assert source is not None
        assert source.payload["poll_state"] == "unknown_source_document_type"
        assert source.payload["failure_code"] == "unknown_source_document_type"
        assert await session.scalar(select(func.count()).select_from(DisclosureOriginal)) == 0


@pytest.mark.asyncio
async def test_maintenance_wakeups_coalesce_within_bucket_after_completion(
    p4_polling_db: AsyncEngine,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    now = datetime(2026, 10, 17, 12, 0, 1, tzinfo=UTC)
    async with session_scope() as session:
        store = store_for(session, settings)
        await store.claim_import()
        await store.apply(
            {
                "supplemental_rules_enabled": True,
                "supplemental_updates_enabled": True,
                "supplemental_disclosure_polling_enabled": True,
                "disclosure_originals_enabled": True,
            },
            actor="test",
        )
        assert await maintain_disclosure_polls(session, settings, now=now) == 1
        maintenance = (
            await session.execute(
                select(PipelineRun).where(
                    PipelineRun.kind == DISCLOSURE_POLL_KIND,
                    PipelineRun.request_json["record_type"].as_string()
                    == "poll_maintenance_v1",
                )
            )
        ).scalar_one()
        claimed = await PipelineRunRepository(session).claim_next(
            worker_id="p4-maintenance-test",
            kind=DISCLOSURE_POLL_KIND,
            run_id=maintenance.id,
        )
        assert claimed is not None
        await PipelineRunRepository(session).mark_success(
            maintenance.id,
            {"poll_state": "maintenance_complete", "poll_complete": False},
            lease=RunLease(maintenance.id, "p4-maintenance-test", claimed.attempt_count),
        )

    async def wake() -> int:
        async with session_scope() as session:
            return await maintain_disclosure_polls(session, settings, now=now)

    results = await asyncio.gather(*(wake() for _ in range(100)))
    async with session_scope() as session:
        count = await session.scalar(
            select(func.count(PipelineRun.id)).where(
                PipelineRun.kind == DISCLOSURE_POLL_KIND,
                PipelineRun.request_json["record_type"].as_string()
                == "poll_maintenance_v1",
            )
        )
    assert sum(results) == 0
    assert count == 1


@pytest.mark.asyncio
async def test_worker_accepts_ledgered_original_redirect_chain(
    p4_polling_db: AsyncEngine,
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC) - timedelta(minutes=1)
    history_start = now - timedelta(days=1)
    async with session_scope() as session:
        store = store_for(session, settings)
        await store.claim_import()
        await store.apply(
            {
                "supplemental_rules_enabled": True,
                "supplemental_updates_enabled": True,
                "supplemental_disclosure_polling_enabled": True,
                "disclosure_originals_enabled": True,
            },
            actor="test",
        )
        session.add(Market(code="SZSE", name="Controlled fixture", timezone="UTC", currency="CNY"))
        company = Company(canonical_name="P4 redirect worker fixture")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SZSE", ticker="300901")
        session.add(listing)
        await session.flush()
        listing_ref = ObjectRef(kind="listing", id=listing.id, revision=1)
        body = BackgroundDisclosureAuthorizationBody(
            contract_hash="a" * 64,
            listing_refs=(listing_ref,),
            doc_type_allowlist=("annual_report_full",),
            history_start=history_start,
            history_end=now + timedelta(days=1),
            min_poll_interval_seconds=21600,
            min_http_interval_seconds=5,
            expires_at=now + timedelta(days=5),
            max_staleness_seconds=86400,
            per_poll_query_http=4,
            per_poll_fetch=3,
            per_poll_http=12,
            per_poll_response_bytes=32 * 1024 * 1024,
            grant_daily_http=24,
            grant_daily_response_bytes=32 * 1024 * 1024,
            storage_bytes_cap=64 * 1024 * 1024,
        )
        grant = (
            await apply_authorization_command(
                session,
                BackgroundDisclosureAuthorizationCommand(
                    action="create",
                    operation_id=uuid4(),
                    authorization_id=uuid4(),
                    expected_revision=0,
                    owner_daily_http=24,
                    owner_daily_response_bytes=128 * 1024 * 1024,
                    owner_min_request_interval_seconds=5,
                    expected_owner_policy_revision=0,
                    update_owner_policy=True,
                    body=body,
                ),
                actor="test",
                now=now,
            )
        ).authorization
        admitted = await admit_disclosure_poll(
            session,
            DisclosurePollRequest(
                operation_id=uuid4(),
                authorization_id=grant.authorization_id,
                authorization_revision=grant.revision,
                authorization_hash=grant.authorization_hash,
                listing_ref=listing_ref,
                window_start=history_start,
                window_end=now,
                expected_source_revision=0,
            ),
            settings=settings,
            now=now,
        )
        listing_id = listing.id

    http = _RedirectOriginalHTTP(published_at=now - timedelta(hours=1))
    service = DisclosurePageService.for_tests(protocol=fixture_protocol(), http=http)
    handler = DisclosurePollingHandler.for_tests(
        settings=settings,
        page_service=service,
        clock=_Clock(datetime.now(UTC)),
    )
    await _drive(handler, admitted["run_id"])

    async with session_scope() as session:
        source = await SupplementalUpdatesRepository(session).get_source(
            "local_owner",
            "cninfo_szse_v1",
            f"listing:{listing_id}:company_research",
        )
        originals = (await session.scalars(select(DisclosureOriginal))).all()
        assert source is not None and source.payload["poll_state"] == "completed"
        assert len(originals) == 1
        assert http.original_urls == [http.initial_original_url, http.redirect_url]
        assert originals[0].payload["resolved_url"] == http.redirect_url
