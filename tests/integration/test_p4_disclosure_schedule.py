"""File-SQLite admission tests for the bounded CNInfo scheduler."""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.ext.asyncio.engine import AsyncEngine

from investment_agent.config import Settings
from investment_agent.evidence.disclosure_http import SourceResponse
from investment_agent.evidence.disclosure_pages import DisclosurePageService
from investment_agent.investor.common import ObjectRef
from investment_agent.jobs.base import ClaimedJob
from investment_agent.settings.loader import store_for
from investment_agent.storage.database import dispose_engine, init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.supplemental_updates import (
    SupplementalChangeReceiptRow,
    SupplementalSourceStateRow,
)
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.disclosure_authorization import apply_authorization_command
from investment_agent.supplemental_rules.disclosure_contracts import (
    BackgroundDisclosureAuthorizationBody,
    BackgroundDisclosureAuthorizationCommand,
    BackgroundDisclosureAuthorizationV1,
)
from investment_agent.supplemental_rules.disclosure_schedule import (
    SCHEDULE_FEED,
    schedule_due_disclosure_polls,
)
from investment_agent.supplemental_rules.disclosure_updates import (
    DISCLOSURE_POLL_KIND,
    DisclosurePollingHandler,
    DisclosurePollRequest,
    admit_disclosure_poll,
    source_scope_key,
)
from tests.support.p4_disclosures import fixture_protocol, fixture_source_response


class _InlineContext:
    worker_id = "p4-schedule-poll-test"

    async def run_with_heartbeat(self, _lease: Any, work: Any) -> Any:
        return await work()


class _ControlledHTTP:
    async def request_hop(
        self, url: str, purpose: str, *, body: bytes | None = None,
        max_bytes: int, timeout_seconds: float = 15,
    ) -> SourceResponse:
        del timeout_seconds
        status, headers, content = fixture_source_response(
            purpose, body or b"", published_at=datetime.now(UTC) - timedelta(hours=1), mode="empty",
        )
        if len(content) > max_bytes:
            raise AssertionError("fixture exceeded its declared response bound")
        return SourceResponse(status, headers, content, url)


class _AdvancingClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or (datetime.now(UTC) + timedelta(seconds=10))

    def __call__(self) -> datetime:
        self.current += timedelta(seconds=6)
        return self.current


@pytest_asyncio.fixture
async def schedule_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncEngine]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'p4-schedule.sqlite'}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await dispose_engine()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        api_key="p4-schedule-test",
        data_dir=tmp_path,
        supplemental_rules_enabled=True,
        supplemental_updates_enabled=True,
        supplemental_disclosure_polling_enabled=True,
        disclosure_originals_enabled=True,
    )


async def _seed_schedule_inputs(
    session: AsyncSession, settings: Settings, now: datetime, tickers: tuple[str, ...],
    *, history_days: int = 90,
) -> tuple[tuple[int, ...], int]:
    store = store_for(session, settings)
    await store.claim_import()
    await store.apply({
        "supplemental_rules_enabled": True,
        "supplemental_updates_enabled": True,
        "supplemental_disclosure_polling_enabled": True,
        "disclosure_originals_enabled": True,
    }, actor="test")
    session.add(Market(code="SZSE", name="Controlled fixture", timezone="UTC", currency="CNY"))
    company = Company(canonical_name="P4 schedule fixture")
    session.add(company)
    await session.flush()
    listings = []
    for ticker in tickers:
        listing = Listing(company_id=company.id, market_code="SZSE", ticker=ticker)
        session.add(listing)
        listings.append(listing)
    await session.flush()
    refs = tuple(ObjectRef(kind="listing", id=row.id, revision=1) for row in listings)
    body = BackgroundDisclosureAuthorizationBody(
        contract_hash="a" * 64,
        listing_refs=refs,
        doc_type_allowlist=("annual_report_full",),
        history_start=now - timedelta(days=history_days),
        history_end=now,
        min_poll_interval_seconds=21600,
        min_http_interval_seconds=5,
        expires_at=now + timedelta(days=30),
        max_staleness_seconds=86400,
        per_poll_query_http=4,
        per_poll_fetch=3,
        per_poll_http=12,
        per_poll_response_bytes=6 * 1024 * 1024,
        grant_daily_http=30,
        grant_daily_response_bytes=32 * 1024 * 1024,
        storage_bytes_cap=64 * 1024 * 1024,
    )
    command = BackgroundDisclosureAuthorizationCommand(
        action="create",
        operation_id=uuid4(),
        authorization_id=uuid4(),
        expected_revision=0,
        owner_daily_http=30,
        owner_daily_response_bytes=128 * 1024 * 1024,
        owner_min_request_interval_seconds=5,
        expected_owner_policy_revision=0,
        update_owner_policy=True,
        body=body,
    )
    await apply_authorization_command(session, command, actor="api", now=now)
    run, _ = await PipelineRunRepository(session).enqueue(
        kind="supplemental_disclosure_schedule",
        idempotency_key="p4:schedule:tick:initial",
        triggered_by="test",
        request_json={"record_type": "disclosure_schedule_tick_v1"},
        max_attempts=3,
    )
    return tuple(row.id for row in listings), run.id


async def _add_expired_grants(session: AsyncSession, listing_id: int, now: datetime) -> None:
    ref = ObjectRef(kind="listing", id=listing_id, revision=1)
    for ordinal in range(10):
        created = now - timedelta(days=40 - ordinal * 2)
        body = BackgroundDisclosureAuthorizationBody(
            contract_hash=f"{ordinal + 1:064x}",
            listing_refs=(ref,),
            doc_type_allowlist=("annual_report_full",),
            history_start=created - timedelta(days=90),
            history_end=created,
            min_poll_interval_seconds=21600,
            min_http_interval_seconds=5,
            expires_at=created + timedelta(days=1),
            max_staleness_seconds=86400,
            per_poll_query_http=4,
            per_poll_fetch=3,
            per_poll_http=12,
            per_poll_response_bytes=6 * 1024 * 1024,
            grant_daily_http=30,
            grant_daily_response_bytes=32 * 1024 * 1024,
            storage_bytes_cap=64 * 1024 * 1024,
        )
        await apply_authorization_command(
            session,
            BackgroundDisclosureAuthorizationCommand(
                action="create", operation_id=uuid4(), authorization_id=uuid4(),
                expected_revision=0, owner_daily_http=30,
                owner_daily_response_bytes=128 * 1024 * 1024,
                owner_min_request_interval_seconds=5,
                expected_owner_policy_revision=1, update_owner_policy=False, body=body,
            ),
            actor="api", now=created,
        )


async def _scheduler_job(run_id: int, *, worker_id: str = "p4-scheduler-test") -> ClaimedJob:
    async with session_scope() as session:
        run = await PipelineRunRepository(session).claim_next(
            worker_id=worker_id, kind="supplemental_disclosure_schedule", run_id=run_id,
        )
        assert run is not None
        return ClaimedJob(
            run_id=run.id,
            kind=run.kind,
            lease=RunLease(run.id, worker_id, run.attempt_count),
            request_json=dict(run.request_json),
        )


async def _poll_job(run_id: int) -> ClaimedJob | None:
    async with session_scope() as session:
        run = await PipelineRunRepository(session).claim_next(
            worker_id="p4-schedule-poll-test", kind=DISCLOSURE_POLL_KIND, run_id=run_id,
        )
        if run is None:
            return None
        return ClaimedJob(
            run_id=run.id, kind=run.kind,
            lease=RunLease(run.id, "p4-schedule-poll-test", run.attempt_count),
            request_json=dict(run.request_json),
        )


async def _drain_empty_poll(
    listing_id: int, settings: Settings, *, clock_start: datetime | None = None,
) -> str:
    handler = DisclosurePollingHandler.for_tests(
        settings=settings,
        page_service=DisclosurePageService.for_tests(
            protocol=fixture_protocol(), http=_ControlledHTTP(),
        ),
        clock=_AdvancingClock(clock_start),
    )
    result = "partial"
    for _ in range(12):
        async with session_scope() as session:
            source = await SupplementalUpdatesRepository(session).get_source(
                "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
            )
            if source is None:
                return "missing"
            if source.payload.get("poll_state") == "completed":
                return "completed"
            if source.payload.get("poll_state") == "window_split_required":
                return "window_split_required"
            run_id = source.payload.get("active_step_run_id")
        if not isinstance(run_id, int):
            break
        job = await _poll_job(run_id)
        if job is None:
            break
        result = await handler.execute(job, _InlineContext())
    async with session_scope() as session:
        source = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        if source is not None and source.payload.get("poll_state") == "completed":
            return "completed"
        if source is not None and source.payload.get("poll_state") == "window_split_required":
            return "window_split_required"
    return result


async def _new_tick(tick: int) -> int:
    async with session_scope() as session:
        run, _ = await PipelineRunRepository(session).enqueue(
            kind="supplemental_disclosure_schedule",
            idempotency_key=f"p4:schedule:tick:{tick}",
            triggered_by="test",
            request_json={"record_type": "disclosure_schedule_tick_v1", "tick": tick},
            max_attempts=3,
        )
        return run.id


@pytest.mark.asyncio
async def test_cold_start_fair_rotation_fixed_backfill_windows_and_terminal_failure_stays_blocked(
    schedule_db: Any, tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    async with session_scope() as session:
        listing_ids, first_tick = await _seed_schedule_inputs(
            session, settings, now, ("300901", "300902"),
        )
    listing_one, listing_two = sorted(listing_ids)

    first_job = await _scheduler_job(first_tick)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=first_job, now=now,
        ) == 1
    async with session_scope() as session:
        roots = await SupplementalUpdatesRepository(session).list_changes(
            "local_owner", kinds=("source_attempt",), limit=100,
        )
        polls = [row for row in roots if row.payload.get("record_type") == "poll_root_v1"]
        assert len(polls) == 1
        first = polls[0]
        assert first.listing_id == listing_one
        assert datetime.fromisoformat(first.payload["window_start"]) == now - timedelta(days=90)
        assert datetime.fromisoformat(first.payload["window_end"]) == now - timedelta(days=59)
        source = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_one}:company_research",
        )
        assert source is not None and source.payload["poll_state"] == "queued"
        assert await session.scalar(select(func.count()).select_from(PipelineRun).where(
            PipelineRun.kind == DISCLOSURE_POLL_KIND,
        )) == 1
        first_run_id = int(first.payload["run_id"])

    # A second listing gets the next slot while the first poll is still queued.
    second_tick = await _new_tick(2)
    second_job = await _scheduler_job(second_tick)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=second_job, now=now + timedelta(seconds=1),
        ) == 1
    async with session_scope() as session:
        polls = [row for row in await SupplementalUpdatesRepository(session).list_changes(
            "local_owner", kinds=("source_attempt",), limit=100,
        ) if row.payload.get("record_type") == "poll_root_v1"]
        assert len(polls) == 2
        assert {row.listing_id for row in polls} == {listing_one, listing_two}
        schedules = await session.scalars(select(SupplementalChangeReceiptRow).where(
            SupplementalChangeReceiptRow.payload["record_type"].as_string() == "poll_schedule_v1",
        ))
        assert list(schedules.all())

    # A successful backfill moves to the bounded recent lane first. After that
    # poll completes, the next fixed backfill chunk resumes at the exact prior
    # frontier even when the scheduler missed many days.
    assert first_run_id > 0
    assert await _drain_empty_poll(
        listing_one, settings, clock_start=now + timedelta(seconds=10),
    ) == "completed"
    third_tick = await _new_tick(3)
    third_job = await _scheduler_job(third_tick)
    late = now + timedelta(days=20)
    async with session_scope() as session:
        # list two is still queued, so fair rotation should select listing one.
        scheduled = await schedule_due_disclosure_polls(
            session, settings=settings, job=third_job, now=late,
        )
        assert scheduled == 1, (await SupplementalUpdatesRepository(session).get_source(
            "local_owner", SCHEDULE_FEED, f"listing:{listing_one}",
        ), await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_one}:company_research",
        ))
    async with session_scope() as session:
        polls = [row for row in await SupplementalUpdatesRepository(session).list_changes(
            "local_owner", kinds=("source_attempt",), limit=100,
        ) if row.payload.get("record_type") == "poll_root_v1"]
        latest = max((row for row in polls if row.listing_id == listing_one), key=lambda row: row.sequence)
        assert latest.payload["window_start"] == (now - timedelta(days=3)).isoformat()
        assert latest.payload["window_end"] == now.isoformat()

    assert await _drain_empty_poll(
        listing_one, settings, clock_start=late + timedelta(seconds=10),
    ) == "completed"
    fifth_tick = await _new_tick(5)
    fifth_job = await _scheduler_job(fifth_tick)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=fifth_job, now=late + timedelta(days=7),
        ) == 1
    async with session_scope() as session:
        # Per-hop quota authority receipts can place this third root beyond
        # the first 100 journal entries. Select the typed root, not a truncated
        # prefix of unrelated audit phases.
        latest = await session.scalar(select(SupplementalChangeReceiptRow).where(
            SupplementalChangeReceiptRow.owner_id == "local_owner",
            SupplementalChangeReceiptRow.listing_id == listing_one,
            SupplementalChangeReceiptRow.payload["record_type"].as_string() == "poll_root_v1",
        ).order_by(SupplementalChangeReceiptRow.sequence.desc()).limit(1))
        assert latest is not None
        assert latest.payload["window_start"] == (now - timedelta(days=59)).isoformat()
        assert latest.payload["window_end"] == (now - timedelta(days=28)).isoformat()

    # Terminal failure remains attached to its poll and is never converted to
    # a fresh paid cycle by later maintenance.
    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        source = await repo.get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_one}:company_research",
            for_update=True,
        )
        assert source is not None
        await repo.save_source(source.model_copy(update={
            "revision": source.revision + 1,
            "payload": {**source.payload, "poll_state": "partial"},
        }), expected_revision=source.revision)
        poll_count = await session.scalar(select(func.count()).select_from(
            SupplementalChangeReceiptRow
        ).where(SupplementalChangeReceiptRow.payload["record_type"].as_string() == "poll_root_v1"))
    fourth_tick = await _new_tick(4)
    fourth_job = await _scheduler_job(fourth_tick)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=fourth_job, now=late + timedelta(days=8),
        ) == 0
        new_count = await session.scalar(select(func.count()).select_from(
            SupplementalChangeReceiptRow
        ).where(SupplementalChangeReceiptRow.payload["record_type"].as_string() == "poll_root_v1"))
        assert new_count == poll_count


@pytest.mark.asyncio
async def test_scheduler_queue_full_and_lost_lease_do_not_create_root_or_spend_authorization(
    schedule_db: Any, tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    async with session_scope() as session:
        listing_ids, tick_id = await _seed_schedule_inputs(session, settings, now, ("300903",))
        grant_state = await session.scalar(select(SupplementalSourceStateRow).where(
            SupplementalSourceStateRow.feed_id == "cninfo_szse_auth_v1",
            SupplementalSourceStateRow.scope_key.like("grant:%"),
        ))
        assert grant_state is not None
        before = dict(grant_state.payload["authorization"])

    # A stale attempt is fenced before any schedule state or poll root changes.
    lost = await _scheduler_job(tick_id)
    invalid_job = ClaimedJob(
        run_id=lost.run_id,
        kind=lost.kind,
        lease=RunLease(lost.run_id, lost.lease.worker_id, lost.lease.attempt + 1),
        request_json=dict(lost.request_json),
    )
    from investment_agent.storage.repositories.pipeline_run import LeaseLostError
    with pytest.raises(LeaseLostError):
        async with session_scope() as session:
            await schedule_due_disclosure_polls(session, settings=settings, job=invalid_job, now=now)
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(
            SupplementalChangeReceiptRow
        ).where(SupplementalChangeReceiptRow.payload["record_type"].as_string() == "poll_root_v1")) == 0

    # A full durable queue leaves the schedule cursor and grant unchanged.
    async with session_scope() as session:
        for ordinal in range(128):
            await PipelineRunRepository(session).enqueue(
                kind=DISCLOSURE_POLL_KIND,
                idempotency_key=f"p4:schedule:queue-full:{ordinal}",
                triggered_by="test_capacity_fixture",
                request_json={"fixture": True},
                max_attempts=3,
            )
        scheduler, _ = await PipelineRunRepository(session).enqueue(
            kind="supplemental_disclosure_schedule",
            idempotency_key="p4:schedule:queue-full-tick",
            triggered_by="test",
            request_json={"record_type": "disclosure_schedule_tick_v1"},
            max_attempts=3,
        )
    queue_job = await _scheduler_job(scheduler.id)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=queue_job, now=now,
        ) == 0
    async with session_scope() as session:
        assert await session.scalar(select(func.count()).select_from(
            SupplementalChangeReceiptRow
        ).where(SupplementalChangeReceiptRow.payload["record_type"].as_string() == "poll_root_v1")) == 0
        state = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", SCHEDULE_FEED, f"listing:{listing_ids[0]}",
        )
        assert state is not None
        assert state.revision == 0
        assert state.payload["state"]["next_lane"] == "backfill"
        assert state.payload["state"]["backfill_after"] == (now - timedelta(days=90)).isoformat().replace("+00:00", "Z")
        assert state.payload["state"]["active_poll_id"] is None
        auth_state = await session.get(SupplementalSourceStateRow, grant_state.id)
        assert auth_state is not None
        assert auth_state.payload["authorization"] == before
        assert await session.scalar(select(func.count()).select_from(SupplementalSourceStateRow).where(
            SupplementalSourceStateRow.feed_id == "cninfo_szse_quota_v1",
        )) == 0


@pytest.mark.asyncio
async def test_many_expired_active_projection_rows_do_not_hide_current_grant(
    schedule_db: Any, tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    async with session_scope() as session:
        listing_ids, tick_id = await _seed_schedule_inputs(session, settings, now, ("300904",))
        await _add_expired_grants(session, listing_ids[0], now)
    job = await _scheduler_job(tick_id)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=job, now=now,
        ) == 1
        roots = [row for row in await SupplementalUpdatesRepository(session).list_changes(
            "local_owner", kinds=("source_attempt",), limit=200,
        ) if row.payload.get("record_type") == "poll_root_v1"]
        assert len(roots) == 1
        assert roots[0].listing_id == listing_ids[0]


@pytest.mark.asyncio
async def test_one_day_window_capacity_is_terminal_after_canonical_split_failure(
    schedule_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_agent.supplemental_rules import disclosure_updates

    settings = _settings(tmp_path)
    now = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    async with session_scope() as session:
        listing_ids, tick_id = await _seed_schedule_inputs(
            session, settings, now, ("300901",), history_days=1,
        )
    job = await _scheduler_job(tick_id)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=job, now=now,
        ) == 1

    # A protocol handler enforces the real poll root/lease/HTTP ledger and
    # reaches the bounded page-cap stop without relying on a fake final proof.
    monkeypatch.setattr(disclosure_updates, "MAX_QUERY_PAGES", 0)
    assert await _drain_empty_poll(listing_ids[0], settings) == "window_split_required"
    first_split_tick = await _new_tick(20)
    first_split_job = await _scheduler_job(first_split_tick)
    split_at = now + timedelta(hours=7)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=first_split_job, now=split_at,
        ) == 0
        state = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", SCHEDULE_FEED, f"listing:{listing_ids[0]}",
        )
        assert state is not None
        assert state.payload["state"]["blocked_reason"] == "one_day_window_capacity"
        assert state.payload["state"]["window_days"] == 31

    # Subsequent ticks cannot turn the same failed one-day operation into
    # another paid collection root.
    count_before = None
    async with session_scope() as session:
        count_before = await session.scalar(select(func.count()).select_from(
            SupplementalChangeReceiptRow
        ).where(SupplementalChangeReceiptRow.payload["record_type"].as_string() == "poll_root_v1"))
    second_split_tick = await _new_tick(21)
    second_split_job = await _scheduler_job(second_split_tick)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=second_split_job, now=split_at + timedelta(hours=7),
        ) == 0
        count_after = await session.scalar(select(func.count()).select_from(
            SupplementalChangeReceiptRow
        ).where(SupplementalChangeReceiptRow.payload["record_type"].as_string() == "poll_root_v1"))
        assert count_after == count_before


@pytest.mark.asyncio
async def test_split_window_retries_only_as_strictly_narrower_child_of_failed_poll(
    schedule_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_agent.supplemental_rules import disclosure_updates

    settings = _settings(tmp_path)
    now = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    async with session_scope() as session:
        listing_ids, tick_id = await _seed_schedule_inputs(
            session, settings, now, ("300901",), history_days=31,
        )
    job = await _scheduler_job(tick_id)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=job, now=now,
        ) == 1
    monkeypatch.setattr(disclosure_updates, "MAX_QUERY_PAGES", 0)
    assert await _drain_empty_poll(listing_ids[0], settings) == "window_split_required"

    split_tick = await _new_tick(30)
    split_job = await _scheduler_job(split_tick)
    split_at = now + timedelta(hours=7)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=split_job, now=split_at,
        ) == 0
    async with session_scope() as session:
        first_roots = [row for row in await SupplementalUpdatesRepository(session).list_changes(
            "local_owner", kinds=("source_attempt",), limit=200,
        ) if row.payload.get("record_type") == "poll_root_v1"]
        parent = first_roots[0]
        state = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", SCHEDULE_FEED, f"listing:{listing_ids[0]}",
        )
        assert state is not None
        assert state.payload["state"]["window_days"] == 15
        assert state.payload["state"]["split_parent_poll_id"] == str(parent.id)
        assert state.payload["state"]["active_poll_id"] is None

    # The split transition is a two-tick operation: first record the failed
    # parent and the narrower cursor, then create its exact child window.
    child_tick = await _new_tick(31)
    child_job = await _scheduler_job(child_tick)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=child_job, now=split_at + timedelta(seconds=1),
        ) == 1
    async with session_scope() as session:
        roots = [row for row in await SupplementalUpdatesRepository(session).list_changes(
            "local_owner", kinds=("source_attempt",), limit=200,
        ) if row.payload.get("record_type") == "poll_root_v1"]
        assert len(roots) == 2
        child = max(roots, key=lambda row: row.sequence)
        assert child.id != parent.id
        assert child.payload["window_start"] == parent.payload["window_start"]
        assert datetime.fromisoformat(child.payload["window_end"]) - datetime.fromisoformat(
            child.payload["window_start"]
        ) == timedelta(days=15)


@pytest.mark.asyncio
async def test_tampered_schedule_projection_fails_closed_before_admission(
    schedule_db: Any, tmp_path: Path,
) -> None:
    from investment_agent.investor.foundations import InvestorError

    settings = _settings(tmp_path)
    now = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    async with session_scope() as session:
        listing_ids, tick_id = await _seed_schedule_inputs(session, settings, now, ("300907",))
    job = await _scheduler_job(tick_id)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=job, now=now,
        ) == 1
    async with session_scope() as session:
        row = await session.scalar(select(SupplementalSourceStateRow).where(
            SupplementalSourceStateRow.feed_id == SCHEDULE_FEED,
            SupplementalSourceStateRow.scope_key == f"listing:{listing_ids[0]}",
        ))
        assert row is not None
        payload = dict(row.payload)
        payload["state"] = {**payload["state"], "next_lane": "recent"}
        row.payload = payload
    tick = await _new_tick(40)
    stale_job = await _scheduler_job(tick)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match="authority|权威|续点"):
            await schedule_due_disclosure_polls(
                session, settings=settings, job=stale_job, now=now + timedelta(days=7),
            )
        count = await session.scalar(select(func.count()).select_from(
            SupplementalChangeReceiptRow
        ).where(SupplementalChangeReceiptRow.payload["record_type"].as_string() == "poll_root_v1"))
        assert count == 1


@pytest.mark.asyncio
async def test_manual_poll_failure_without_scheduler_ownership_is_not_resurrected(
    schedule_db: Any, tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    async with session_scope() as session:
        listing_ids, tick_id = await _seed_schedule_inputs(session, settings, now, ("300906",))
        auth_state = await session.scalar(select(SupplementalSourceStateRow).where(
            SupplementalSourceStateRow.feed_id == "cninfo_szse_auth_v1",
            SupplementalSourceStateRow.scope_key.like("grant:%"),
        ))
        assert auth_state is not None
        grant = BackgroundDisclosureAuthorizationV1.model_validate(auth_state.payload["authorization"])
        listing_ref = ObjectRef(kind="listing", id=listing_ids[0], revision=1)
        manual = await admit_disclosure_poll(
            session,
            DisclosurePollRequest(
                operation_id=uuid4(), authorization_id=grant.authorization_id,
                authorization_revision=grant.revision,
                authorization_hash=grant.authorization_hash,
                listing_ref=listing_ref,
                window_start=now - timedelta(days=1), window_end=now,
                expected_source_revision=0,
            ),
            settings=settings,
            now=now,
        )
        manual_run_id = int(manual["run_id"])
    assert await _drain_empty_poll(listing_ids[0], settings) == "partial"
    async with session_scope() as session:
        source = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", source_scope_key(ObjectRef(
                kind="listing", id=listing_ids[0], revision=1,
            )),
        )
        assert source is not None
        assert source.payload["poll_state"] == "ambiguous_or_missing_official_issuer"
    assert manual_run_id > 0
    schedule_job = await _scheduler_job(tick_id)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=schedule_job, now=now + timedelta(hours=7),
        ) == 0
        roots = [row for row in await SupplementalUpdatesRepository(session).list_changes(
            "local_owner", kinds=("source_attempt",), limit=100,
        ) if row.payload.get("record_type") == "poll_root_v1"]
        assert len(roots) == 1


@pytest.mark.asyncio
async def test_manual_success_releases_stale_scheduler_pointer_but_pending_or_failed_does_not(
    schedule_db: Any, tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    now = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    async with session_scope() as session:
        listing_ids, tick_id = await _seed_schedule_inputs(
            session, settings, now, ("300901",),
        )
    listing_id = listing_ids[0]
    initial_job = await _scheduler_job(tick_id)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=initial_job, now=now,
        ) == 1
        schedule = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", SCHEDULE_FEED, f"listing:{listing_id}",
        )
        assert schedule is not None
        old_scheduler_poll = schedule.payload["state"]["active_poll_id"]
    assert await _drain_empty_poll(listing_id, settings, clock_start=now + timedelta(seconds=10)) == "completed"

    # A later explicit manual poll supersedes the terminal source pointer.
    manual_at = now + timedelta(hours=7)
    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        current_source = await repo.get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        grant_state = await session.scalar(select(SupplementalSourceStateRow).where(
            SupplementalSourceStateRow.feed_id == "cninfo_szse_auth_v1",
            SupplementalSourceStateRow.scope_key.like("grant:%"),
        ))
        assert current_source is not None and grant_state is not None
        grant = BackgroundDisclosureAuthorizationV1.model_validate(
            grant_state.payload["authorization"],
        )
        manual = await admit_disclosure_poll(
            session,
            DisclosurePollRequest(
                operation_id=uuid4(), authorization_id=grant.authorization_id,
                authorization_revision=grant.revision,
                authorization_hash=grant.authorization_hash,
                listing_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
                window_start=now - timedelta(days=90),
                window_end=now - timedelta(days=59),
                expected_source_revision=current_source.revision,
            ),
            settings=settings,
            now=manual_at,
        )
        manual_run_id = int(manual["run_id"])
        assert str(manual["poll_id"]) != old_scheduler_poll

    # A queued manual replacement cannot release the old scheduled window.
    pending_tick = await _new_tick(101)
    pending_job = await _scheduler_job(pending_tick)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=pending_job,
            now=manual_at + timedelta(seconds=1),
        ) == 0
        schedule = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", SCHEDULE_FEED, f"listing:{listing_id}",
        )
        assert schedule is not None
        assert schedule.payload["state"]["active_poll_id"] == old_scheduler_poll
        assert schedule.payload["state"]["blocked_reason"] == "manual_poll_pending"

    assert await _drain_empty_poll(
        listing_id, settings, clock_start=manual_at + timedelta(seconds=10),
    ) == "completed"
    success_tick = await _new_tick(102)
    success_job = await _scheduler_job(success_tick)
    async with session_scope() as session:
        # Once the replacement is backed by its exact completed final, the
        # stale pointer is released without advancing the old scheduler lane.
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=success_job,
            now=manual_at + timedelta(hours=7),
        ) == 0
        schedule = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", SCHEDULE_FEED, f"listing:{listing_id}",
        )
        assert schedule is not None
        assert schedule.payload["state"]["active_poll_id"] is None
        assert schedule.payload["state"]["blocked_reason"] is None
    next_tick = await _new_tick(106)
    next_job = await _scheduler_job(next_tick)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=next_job,
            now=manual_at + timedelta(hours=14),
        ) == 1
        schedule = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", SCHEDULE_FEED, f"listing:{listing_id}",
        )
        source = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        assert schedule is not None and source is not None
        assert schedule.payload["state"]["active_poll_id"] == source.payload["active_poll_id"]
        assert schedule.payload["state"]["active_poll_id"] != old_scheduler_poll
    assert manual_run_id > 0


@pytest.mark.asyncio
async def test_failed_manual_replacement_keeps_stale_scheduler_pointer_for_review(
    schedule_db: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failed manual replacement remains attached for review; it cannot be
    # treated as a successful predecessor of the scheduler lane.
    from investment_agent.supplemental_rules import disclosure_updates

    settings = _settings(tmp_path)
    now = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=1)
    async with session_scope() as session:
        listing_ids, initial_tick = await _seed_schedule_inputs(
            session, settings, now, ("300901",),
        )
    listing_id = listing_ids[0]
    initial_job = await _scheduler_job(initial_tick)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=initial_job, now=now,
        ) == 1
        schedule = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", SCHEDULE_FEED, f"listing:{listing_id}",
        )
        assert schedule is not None
        stale_scheduler_poll = schedule.payload["state"]["active_poll_id"]
    assert await _drain_empty_poll(listing_id, settings, clock_start=now + timedelta(seconds=10)) == "completed"

    monkeypatch.setattr(disclosure_updates, "MAX_QUERY_PAGES", 0)
    manual_at = now + timedelta(hours=7)
    async with session_scope() as session:
        source = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        grant_state = await session.scalar(select(SupplementalSourceStateRow).where(
            SupplementalSourceStateRow.feed_id == "cninfo_szse_auth_v1",
            SupplementalSourceStateRow.scope_key.like("grant:%"),
        ))
        assert source is not None and grant_state is not None
        grant = BackgroundDisclosureAuthorizationV1.model_validate(
            grant_state.payload["authorization"],
        )
        failed_manual = await admit_disclosure_poll(
            session,
            DisclosurePollRequest(
                operation_id=uuid4(), authorization_id=grant.authorization_id,
                authorization_revision=grant.revision,
                authorization_hash=grant.authorization_hash,
                listing_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
                window_start=now - timedelta(days=90),
                window_end=now - timedelta(days=59),
                expected_source_revision=source.revision,
            ),
            settings=settings,
            now=manual_at,
        )
        failed_poll_id = failed_manual["poll_id"]
    assert await _drain_empty_poll(
        listing_id, settings, clock_start=manual_at + timedelta(seconds=10),
    ) == "window_split_required"
    failed_reconcile_tick = await _new_tick(105)
    failed_reconcile_job = await _scheduler_job(failed_reconcile_tick)
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=failed_reconcile_job,
            now=manual_at + timedelta(seconds=1),
        ) == 0
        schedule = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", SCHEDULE_FEED, f"listing:{listing_id}",
        )
        source = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        assert schedule is not None and source is not None
        assert source.payload["active_poll_id"] == failed_poll_id
        assert schedule.payload["state"]["active_poll_id"] == stale_scheduler_poll
        assert schedule.payload["state"]["blocked_reason"] == "manual_poll_pending"
