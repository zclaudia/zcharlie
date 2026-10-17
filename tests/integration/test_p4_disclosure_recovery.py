"""Worker interruption boundaries for the durable disclosure poller.

These tests use a file-backed SQLite database and the fixture transport. They
exercise the real worker, HTTP attempt ledger and candidate writer together.
"""
from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from investment_agent.config import Settings
from investment_agent.evidence.disclosure_http import DIRECTORY, SourceResponse
from investment_agent.evidence.disclosure_pages import DisclosurePageService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import ClaimedJob
from investment_agent.settings.loader import store_for
from investment_agent.storage.database import dispose_engine, init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.disclosure import (
    DisclosureBlob,
    DisclosureItem,
    DisclosureOriginal,
)
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.supplemental_updates import SupplementalChangeReceiptRow
from investment_agent.storage.repositories.pipeline_run import (
    LeaseLostError,
    PipelineRunRepository,
    RunLease,
)
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.disclosure_authorization import (
    _quota_state,
    apply_authorization_command,
    inherit_poll_quota,
    mark_http_hop_sent,
    read_authorization_snapshot,
    recover_orphan_disclosure_hop,
    reserve_http_hop,
)
from investment_agent.supplemental_rules.disclosure_contracts import (
    BackgroundDisclosureAuthorizationBody,
    BackgroundDisclosureAuthorizationCommand,
    DisclosureAttemptIdentity,
    HttpHopPermit,
)
from investment_agent.supplemental_rules.disclosure_updates import (
    DISCLOSURE_POLL_KIND,
    DisclosurePollingHandler,
    DisclosurePollRequest,
    admit_disclosure_poll,
)
from tests.support.p4_disclosures import fixture_protocol, fixture_source_response


class _InlineContext:
    worker_id = "p4-recovery-test"

    async def run_with_heartbeat(self, _lease: RunLease, work: Any) -> Any:
        return await work()


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        self.value += timedelta(seconds=6)
        return self.value


class _HTTP:
    def __init__(
        self, *, mode: str = "normal", die_after_send: bool = False,
        after_response: Any | None = None,
    ) -> None:
        self.mode = mode
        self.die_after_send = die_after_send
        self.after_response = after_response
        self.calls = 0
        self.responses: dict[str, bytes] = {}

    async def request_hop(
        self, url: str, purpose: str, *, body: bytes | None = None,
        max_bytes: int, timeout_seconds: float = 15,
    ) -> SourceResponse:
        del timeout_seconds
        self.calls += 1
        if self.die_after_send:
            # BaseException models abrupt worker/process loss. The worker's
            # normal transport-error path must not convert this into a refund.
            raise KeyboardInterrupt("simulated process loss after sent receipt")
        status, headers, content = fixture_source_response(
            purpose, body or b"", published_at=datetime.now(UTC) - timedelta(hours=1),
            mode=self.mode,
        )
        assert len(content) <= max_bytes
        response = SourceResponse(status, headers, content, url)
        self.responses[purpose] = content
        if self.after_response is not None:
            await self.after_response(purpose)
        return response


@pytest_asyncio.fixture
async def recovery_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncEngine]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'p4-recovery.sqlite'}"
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
        api_key="p4-recovery-test",
        data_dir=tmp_path,
        supplemental_rules_enabled=True,
        supplemental_updates_enabled=True,
        supplemental_disclosure_polling_enabled=True,
        disclosure_originals_enabled=True,
    )


async def _claim(run_id: int | None = None, *, now: datetime | None = None) -> ClaimedJob | None:
    async with session_scope() as session:
        row = await PipelineRunRepository(session).claim_next(
            worker_id="p4-recovery-test", kind=DISCLOSURE_POLL_KIND, run_id=run_id, now=now,
        )
        if row is None:
            return None
        return ClaimedJob(
            run_id=row.id,
            kind=row.kind,
            lease=RunLease(row.id, "p4-recovery-test", row.attempt_count),
            request_json=dict(row.request_json),
        )


async def _seed_poll(tmp_path: Path) -> tuple[Settings, int, int, datetime, str]:
    settings = _settings(tmp_path)
    now = datetime.now(UTC) - timedelta(minutes=2)
    start = now - timedelta(days=1)
    async with session_scope() as session:
        store = store_for(session, settings)
        await store.claim_import()
        await store.apply({
            "supplemental_rules_enabled": True,
            "supplemental_updates_enabled": True,
            "supplemental_disclosure_polling_enabled": True,
            "disclosure_originals_enabled": True,
        }, actor="test")
        session.add(Market(code="SZSE", name="fixture", timezone="UTC", currency="CNY"))
        company = Company(canonical_name="P4 recovery fixture")
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
            history_start=start,
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
        grant = await apply_authorization_command(
            session,
            BackgroundDisclosureAuthorizationCommand(
                action="create", operation_id=uuid4(), authorization_id=uuid4(),
                expected_revision=0, owner_daily_http=24,
                owner_daily_response_bytes=128 * 1024 * 1024,
                owner_min_request_interval_seconds=5,
                expected_owner_policy_revision=0, update_owner_policy=True, body=body,
            ),
            actor="api", now=now,
        )
        admitted = await admit_disclosure_poll(
            session,
            DisclosurePollRequest(
                operation_id=uuid4(),
                authorization_id=grant.authorization.authorization_id,
                authorization_revision=grant.authorization.revision,
                authorization_hash=grant.authorization.authorization_hash,
                listing_ref=listing_ref,
                window_start=start,
                window_end=now,
                expected_source_revision=0,
            ),
            settings=settings,
            now=now,
        )
        return settings, int(admitted["run_id"]), listing.id, now, str(grant.authorization.authorization_id)


async def _drive(handler: DisclosurePollingHandler, run_id: int | None) -> None:
    current: int | None = run_id
    for _ in range(32):
        job = await _claim(current)
        if job is None:
            return
        current = None
        result = await handler.execute(job, _InlineContext())
        assert result in {"completed", "partial"}
    raise AssertionError("poll did not reach a bounded terminal or partial state")


@pytest.mark.asyncio
async def test_candidate_savepoint_failure_keeps_http_response_but_no_candidate_rows(
    recovery_db: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, run_id, listing_id, _, _ = await _seed_poll(tmp_path)
    controlled = _HTTP(mode="normal")
    service = DisclosurePageService.for_tests(protocol=fixture_protocol(), http=controlled)
    handler = DisclosurePollingHandler.for_tests(settings=settings, page_service=service, clock=_Clock(datetime.now(UTC)))

    import investment_agent.supplemental_rules.disclosure_candidate_ingest as candidate_module

    original = candidate_module.ingest_disclosure_original

    async def write_then_fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await original(*args, **kwargs)
        raise InvestorError("invalid_reference", "injected final candidate leaf failure")

    monkeypatch.setattr(candidate_module, "ingest_disclosure_original", write_then_fail)
    await _drive(handler, run_id)

    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        source = await repo.get_source("local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research")
        assert source is not None
        assert source.payload["poll_state"] == "injected final candidate leaf failure"
        assert controlled.calls >= 3  # directory, query, and original fetch
        blob_count = await session.scalar(select(func.count()).select_from(DisclosureBlob))
        assert blob_count is not None and blob_count >= 1
        assert await session.scalar(select(func.count()).select_from(DisclosureItem)) == 0
        assert await session.scalar(select(func.count()).select_from(DisclosureOriginal)) == 0
        # The candidate savepoint rolls back all economic rows and leaf receipt,
        # while the already settled HTTP response/blob remains durable.
        assert await session.scalar(select(func.count()).select_from(DisclosureItem).where(
            DisclosureItem.listing_id == listing_id,
        )) == 0


@pytest.mark.asyncio
async def test_sent_without_result_is_unknown_and_spent_after_stale_worker_reclaim(
    recovery_db: AsyncEngine,
    tmp_path: Path,
) -> None:
    settings, run_id, listing_id, _, _ = await _seed_poll(tmp_path)
    http = _HTTP(die_after_send=True)
    service = DisclosurePageService.for_tests(protocol=fixture_protocol(), http=http)
    handler = DisclosurePollingHandler.for_tests(settings=settings, page_service=service)
    first_job = await _claim(run_id)
    assert first_job is not None
    with pytest.raises(KeyboardInterrupt, match="simulated process loss"):
        await handler.execute(first_job, _InlineContext())

    stale_at = datetime.now(UTC) + timedelta(hours=1)
    async with session_scope() as session:
        requeued, failed = await PipelineRunRepository(session).recover_stale(
            kind=DISCLOSURE_POLL_KIND,
            stale_before=stale_at,
            now=stale_at,
        )
        assert requeued == [run_id]
        assert failed == []
        state = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        assert state is not None
        checkpoint = state.payload["poll_checkpoint"]
        attempt_id = checkpoint["attempt_id"]
        reserved = await SupplementalUpdatesRepository(session).get_change_by_key(
            "local_owner", f"p4:attempt:{attempt_id}:0:reserved",
        )
        sent = await SupplementalUpdatesRepository(session).get_change_by_key(
            "local_owner", f"p4:attempt:{attempt_id}:0:sent",
        )
        result = await SupplementalUpdatesRepository(session).get_change_by_key(
            "local_owner", f"p4:attempt:{attempt_id}:0:result",
        )
        unknown = await SupplementalUpdatesRepository(session).get_change_by_key(
            "local_owner", f"p4:attempt:{attempt_id}:0:unknown",
        )
        assert reserved is not None and sent is not None
        assert result is None and unknown is None

    reclaimed = await _claim(run_id, now=stale_at + timedelta(seconds=1))
    assert reclaimed is not None and reclaimed.lease.attempt > first_job.lease.attempt
    http.die_after_send = False
    await handler.execute(reclaimed, _InlineContext())

    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        unknown = await repo.get_change_by_key(
            "local_owner", f"p4:attempt:{attempt_id}:0:unknown",
        )
        result = await repo.get_change_by_key(
            "local_owner", f"p4:attempt:{attempt_id}:0:result",
        )
        source = await repo.get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        assert unknown is not None and unknown.payload["stage"] == "unknown"
        assert result is None
        assert unknown.payload["storage_bytes"] > 0
        assert source is not None and source.payload["poll_state"] == "retry_wait"
        assert await session.scalar(select(func.count()).select_from(DisclosureItem)) == 0


@pytest.mark.asyncio
async def test_settled_raw_response_replays_under_new_lease_without_extra_http(
    recovery_db: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, run_id, listing_id, _, _ = await _seed_poll(tmp_path)
    http = _HTTP(mode="normal")
    service = DisclosurePageService.for_tests(protocol=fixture_protocol(), http=http)
    handler = DisclosurePollingHandler.for_tests(
        settings=settings, page_service=service, clock=_Clock(datetime.now(UTC)),
    )
    import investment_agent.supplemental_rules.disclosure_candidate_ingest as candidate_module

    original = candidate_module.ingest_disclosure_original
    crash_once = True

    async def crash_before_candidate(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal crash_once
        if crash_once:
            crash_once = False
            raise KeyboardInterrupt("simulated process loss before candidate transaction")
        return await original(*args, **kwargs)

    monkeypatch.setattr(candidate_module, "ingest_disclosure_original", crash_before_candidate)
    first_job = await _claim(run_id)
    assert first_job is not None
    crashed_run_id: int | None = None
    # Directory and query complete first. The crash occurs on the fetch stage,
    # after the response and its result receipt have been committed separately.
    for _ in range(8):
        try:
            await handler.execute(first_job, _InlineContext())
        except KeyboardInterrupt as exc:
            assert "before candidate" in str(exc)
            crashed_run_id = first_job.run_id
            break
        async with session_scope() as session:
            claimed = await PipelineRunRepository(session).claim_next(
                worker_id="p4-recovery-test", kind=DISCLOSURE_POLL_KIND,
            )
            if claimed is None:
                raise AssertionError("worker did not enqueue its next bounded step")
            first_job = ClaimedJob(
                run_id=claimed.id, kind=claimed.kind,
                lease=RunLease(claimed.id, "p4-recovery-test", claimed.attempt_count),
                request_json=dict(claimed.request_json),
            )
    else:
        raise AssertionError("worker never reached candidate ingestion")

    calls_at_crash = http.calls
    assert crashed_run_id is not None
    assert calls_at_crash >= 3
    stale_at = datetime.now(UTC) + timedelta(hours=1)
    async with session_scope() as session:
        requeued, failed = await PipelineRunRepository(session).recover_stale(
            kind=DISCLOSURE_POLL_KIND, stale_before=stale_at, now=stale_at,
        )
        assert crashed_run_id in requeued and failed == []
    reclaimed = await _claim(crashed_run_id, now=stale_at + timedelta(seconds=1))
    assert reclaimed is not None and reclaimed.lease.attempt > 1
    from investment_agent.supplemental_rules.disclosure_candidate_ingest import assert_run_lease

    async with session_scope() as session:
        with pytest.raises(LeaseLostError):
            await assert_run_lease(session, first_job.lease)
    # Restore real candidate ingestion. The durable response should replay from
    # the immutable response receipt/blob without invoking the transport again.
    monkeypatch.setattr(candidate_module, "ingest_disclosure_original", original)
    await handler.execute(reclaimed, _InlineContext())
    assert http.calls == calls_at_crash
    async with session_scope() as session:
        item_count = await session.scalar(select(func.count()).select_from(DisclosureItem))
        original_count = await session.scalar(select(func.count()).select_from(DisclosureOriginal))
        blob_count = await session.scalar(select(func.count()).select_from(DisclosureBlob))
        assert blob_count is not None and blob_count >= 1
        source = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        assert source is not None
        assert item_count == original_count == 1, (
            item_count, original_count, source.payload.get("poll_state"),
            source.payload.get("failure_code"), source.payload.get("poll_checkpoint"),
        )


@pytest.mark.asyncio
async def test_reclaimed_worker_can_send_reserved_unsent_hop(
    recovery_db: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, run_id, listing_id, _, _ = await _seed_poll(tmp_path)
    http = _HTTP(mode="normal")
    service = DisclosurePageService.for_tests(protocol=fixture_protocol(), http=http)
    handler = DisclosurePollingHandler.for_tests(
        settings=settings, page_service=service, clock=_Clock(datetime.now(UTC)),
    )
    from investment_agent.supplemental_rules import disclosure_authorization as authorization_module

    original_mark_sent = authorization_module.mark_http_hop_sent
    crash_once = True

    async def crash_before_send(*args: Any, **kwargs: Any) -> Any:
        nonlocal crash_once
        if crash_once:
            crash_once = False
            raise KeyboardInterrupt("simulated process loss after reservation")
        return await original_mark_sent(*args, **kwargs)

    monkeypatch.setattr(authorization_module, "mark_http_hop_sent", crash_before_send)
    first_job = await _claim(run_id)
    assert first_job is not None
    with pytest.raises(KeyboardInterrupt, match="after reservation"):
        await handler.execute(first_job, _InlineContext())
    assert http.calls == 0

    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        source = await repo.get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        assert source is not None
        attempt_id = source.payload["poll_checkpoint"]["attempt_id"]
        reserved = await repo.get_change_by_key(
            "local_owner", f"p4:attempt:{attempt_id}:0:reserved",
        )
        assert reserved is not None
        assert await repo.get_change_by_key(
            "local_owner", f"p4:attempt:{attempt_id}:0:sent",
        ) is None

    stale_at = datetime.now(UTC) + timedelta(hours=1)
    async with session_scope() as session:
        requeued, failed = await PipelineRunRepository(session).recover_stale(
            kind=DISCLOSURE_POLL_KIND, stale_before=stale_at, now=stale_at,
        )
        assert run_id in requeued and failed == []
    reclaimed = await _claim(run_id, now=stale_at + timedelta(seconds=1))
    assert reclaimed is not None and reclaimed.lease.attempt > first_job.lease.attempt
    from investment_agent.supplemental_rules.disclosure_authorization import assert_run_lease

    async with session_scope() as session:
        with pytest.raises(LeaseLostError):
            await assert_run_lease(session, first_job.lease)
        orphan_receipt = await SupplementalUpdatesRepository(session).get_change_by_key(
            "local_owner", f"p4:attempt:{attempt_id}:0:reserved",
        )
        assert orphan_receipt is not None
        with pytest.raises(LeaseLostError):
            await authorization_module.mark_http_hop_sent(
                session,
                permit=HttpHopPermit.model_validate(orphan_receipt.payload["permit"]),
                lease=first_job.lease,
                base_settings=settings,
                now=stale_at + timedelta(seconds=2),
            )

    monkeypatch.setattr(authorization_module, "mark_http_hop_sent", original_mark_sent)
    await handler.execute(reclaimed, _InlineContext())
    await _drive(handler, None)
    assert http.calls >= 3
    async with session_scope() as session:
        sent = await SupplementalUpdatesRepository(session).get_change_by_key(
            "local_owner", f"p4:attempt:{attempt_id}:0:sent",
        )
        assert sent is not None
        assert sent.payload["reservation_lease"] == reserved.payload["lease"]
        assert sent.payload["lease"]["attempt"] == reclaimed.lease.attempt
        assert sent.payload["lease"] != sent.payload["reservation_lease"]
        source = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        assert source is not None
        assert source.payload.get("poll_state") == "completed"
        assert await session.scalar(select(func.count()).select_from(DisclosureItem)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("intervention", ["close", "revoke"])
async def test_close_or_revoke_during_inflight_directory_never_exposes_candidate(
    recovery_db: AsyncEngine,
    tmp_path: Path,
    intervention: str,
) -> None:
    settings, run_id, listing_id, now, authorization_id = await _seed_poll(tmp_path)
    changed = False

    async def intervene(purpose: str) -> None:
        nonlocal changed
        if changed or purpose != "directory":
            return
        changed = True
        async with session_scope() as session:
            if intervention == "close":
                store = store_for(session, settings)
                await store.apply(
                    {"supplemental_disclosure_polling_enabled": False}, actor="test-close",
                )
            else:
                await apply_authorization_command(
                    session,
                    BackgroundDisclosureAuthorizationCommand(
                        action="revoke", operation_id=uuid4(),
                        authorization_id=UUID(authorization_id),
                        expected_revision=1, owner_daily_http=24,
                        owner_daily_response_bytes=128 * 1024 * 1024,
                        owner_min_request_interval_seconds=5,
                        expected_owner_policy_revision=1,
                        reason="inflight recovery test",
                    ),
                    actor="api", now=now + timedelta(seconds=6),
                )

    http = _HTTP(mode="normal", after_response=intervene)
    service = DisclosurePageService.for_tests(protocol=fixture_protocol(), http=http)
    handler = DisclosurePollingHandler.for_tests(
        settings=settings, page_service=service, clock=_Clock(datetime.now(UTC)),
    )
    await _drive(handler, run_id)
    assert changed and http.calls == 1
    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        source = await repo.get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        assert source is not None and source.payload["poll_state"] != "completed"
        assert await session.scalar(select(func.count()).select_from(DisclosureItem)) == 0
        assert await session.scalar(select(func.count()).select_from(DisclosureOriginal)) == 0
        body = http.responses["directory"]
        body_hash = hashlib.sha256(body).hexdigest()
        assert await session.get(DisclosureBlob, body_hash) is None
        receipts = await repo.list_changes(
            "local_owner", kinds=("source_attempt",), limit=500,
        )
        result = next(
            receipt for receipt in receipts
            if receipt.payload.get("stage") == "result"
            and receipt.payload.get("response_hash") == body_hash
        )
        assert result.payload["response_bytes"] == len(body)
        assert result.payload["storage_bytes"] > 0
        discarded = next(
            receipt for receipt in receipts
            if receipt.payload.get("record_type") == "poll_http_discard_v1"
            and receipt.payload.get("response_hash") == body_hash
        )
        assert discarded.payload["response_bytes"] == len(body)
        assert discarded.payload["settlement_ref"]["id"] == str(result.id)
        assert "raw_blob_hash" not in discarded.payload


@pytest.mark.asyncio
async def test_replacement_poll_inherits_parent_reserved_and_consumed_quota(
    recovery_db: AsyncEngine,
    tmp_path: Path,
) -> None:
    settings, run_id, listing_id, now, authorization_id = await _seed_poll(tmp_path)
    http = _HTTP(mode="rate_limited")
    handler = DisclosurePollingHandler.for_tests(
        settings=settings,
        page_service=DisclosurePageService.for_tests(protocol=fixture_protocol(), http=http),
        clock=_Clock(now),
    )
    job = await _claim(run_id)
    assert job is not None
    assert await handler.execute(job, _InlineContext()) == "partial"
    assert http.calls == 1

    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        source = await repo.get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        assert source is not None and source.payload["poll_state"] == "retry_wait"
        old_poll_id = UUID(source.payload["active_poll_id"])
        old_root = await repo.get_change("local_owner", old_poll_id)
        assert old_root is not None
        _old_quota_state, old_quota = await _quota_state(
            session, f"poll:{old_poll_id}", day=None,
        )
        grant = await read_authorization_snapshot(session, UUID(authorization_id))
        listing_ref = grant.listing_refs[0]
        replacement_at = now + timedelta(hours=7)
        # Record the daily ledger before replacement; carrying poll-local usage
        # must not charge the owner/grant day ledger a second time.
        from investment_agent.supplemental_rules.disclosure_authorization import (
            _day,
            _quota_scope,
        )

        owner_day_scope = _quota_scope("owner-day", _day(replacement_at))
        _owner_state, owner_before = await _quota_state(
            session, owner_day_scope, day=_day(replacement_at),
        )
        admitted = await admit_disclosure_poll(
            session,
            DisclosurePollRequest(
                operation_id=uuid4(),
                authorization_id=grant.authorization_id,
                authorization_revision=grant.revision,
                authorization_hash=grant.authorization_hash,
                listing_ref=listing_ref,
                window_start=grant.history_start,
                window_end=replacement_at,
                expected_source_revision=source.revision,
                replace_poll_id=old_poll_id,
                replace_reason="carry unfinished poll quota",
            ),
            settings=settings,
            now=replacement_at,
        )
        child_state, child_quota = await _quota_state(
            session, f"poll:{admitted['poll_id']}", day=None,
        )
        _owner_after_state, owner_after = await _quota_state(
            session, owner_day_scope, day=_day(replacement_at),
        )
        assert child_quota["inherited_from_poll_id"] == str(old_poll_id)
        assert child_quota["request_consumed"] == (
            old_quota["request_consumed"] + old_quota["request_reserved"]
        ) == 1
        assert child_quota["bytes_consumed"] == (
            old_quota["bytes_consumed"] + old_quota["bytes_reserved"]
        ) > 0
        assert child_quota["request_reserved"] == child_quota["bytes_reserved"] == 0
        for key in ("request_reserved", "request_consumed", "bytes_reserved", "bytes_consumed"):
            assert owner_after[key] == owner_before[key]
        new_root = await repo.get_change("local_owner", UUID(admitted["poll_id"]))
        assert new_root is not None
        replacement_ref = new_root.payload["predecessor_snapshot_ref"]
        replayed_usage = await inherit_poll_quota(
            session,
            new_poll_id=UUID(admitted["poll_id"]),
            old_poll_id=old_poll_id,
            replacement_ref=replacement_ref,
            now=replacement_at,
        )
        assert replayed_usage["request_consumed"] == child_quota["request_consumed"]
        reread_state, reread_child = await _quota_state(
            session, f"poll:{admitted['poll_id']}", day=None,
        )
        assert reread_state.revision == child_state.revision
        assert reread_child["request_consumed"] == child_quota["request_consumed"]
        with pytest.raises(InvestorError) as wrong_predecessor:
            await inherit_poll_quota(
                session,
                new_poll_id=UUID(admitted["poll_id"]),
                old_poll_id=uuid4(),
                replacement_ref=replacement_ref,
                now=replacement_at,
            )
        assert wrong_predecessor.value.detail.code == "invalid_reference"


@pytest.mark.parametrize(("was_sent", "expected"), [(False, "canceled"), (True, "unknown")])
@pytest.mark.asyncio
async def test_maintenance_recovers_only_fenced_orphan_http_hops(
    recovery_db: AsyncEngine,
    tmp_path: Path,
    was_sent: bool,
    expected: str,
) -> None:
    settings, run_id, _listing_id, _seeded_at, authorization_id = await _seed_poll(tmp_path)
    op_now = datetime.now(UTC)
    old_job = await _claim(run_id, now=op_now)
    assert old_job is not None
    async with session_scope() as session:
        poll_root = await SupplementalUpdatesRepository(session).get_change(
            "local_owner", old_job.request_json["poll_sequence"],
        )
        assert poll_root is not None
        auth = await read_authorization_snapshot(
            session, UUID(authorization_id), owner_id="local_owner",
        )
        identity = DisclosureAttemptIdentity(
            poll_id=str(poll_root.id),
            logical_request_key="orphan-test-directory",
            attempt_id=uuid4(), attempt_ordinal=1, hop_ordinal=0,
            hop_id="directory-0", request_kind="directory",
            request_hash="c" * 64,
            authorization_id=auth.authorization_id,
            authorization_revision=auth.revision,
            authorization_hash=auth.authorization_hash,
            contract_hash=auth.contract_hash,
            scope_hash=poll_root.payload["attempt_scope_hash"],
            listing_ref=ObjectRef.model_validate(poll_root.payload["listing_ref"]),
            window_start=datetime.fromisoformat(poll_root.payload["window_start"]),
            window_end=datetime.fromisoformat(poll_root.payload["window_end"]),
            poll_http_cap=poll_root.payload["poll_http_cap"],
            poll_query_http_cap=poll_root.payload["poll_query_http_cap"],
            poll_fetch_cap=poll_root.payload["poll_fetch_cap"],
            poll_response_bytes_cap=poll_root.payload["poll_response_bytes_cap"],
            url=DIRECTORY,
            response_bytes_limit=4096,
        )
        permit = await reserve_http_hop(
            session, identity=identity, response_byte_limit=4096,
            lease=old_job.lease, base_settings=settings,
            now=op_now + timedelta(seconds=1),
        )
        if was_sent:
            await mark_http_hop_sent(
                session, permit=permit, lease=old_job.lease,
                base_settings=settings, now=op_now + timedelta(seconds=2),
            )
        await PipelineRunRepository(session).mark_partial(
            run_id, "simulated_orphaned_worker", lease=old_job.lease,
        )
        maintenance, _created = await PipelineRunRepository(session).enqueue(
            kind="supplemental_update_schedule",
            idempotency_key=f"orphan-recovery-{was_sent}-{uuid4()}",
            triggered_by="test", request_json={
                "schema_version": 1, "action": "maintenance",
            }, available_at=op_now + timedelta(seconds=3),
        )
        maintenance_row = await PipelineRunRepository(session).claim_next(
            worker_id="p4-recovery-maintenance", kind="supplemental_update_schedule",
            run_id=maintenance.id,
            now=op_now + timedelta(seconds=4),
        )
        assert maintenance_row is not None
        maintenance_lease = RunLease(
            maintenance_row.id, "p4-recovery-maintenance", maintenance_row.attempt_count,
        )
        result = await recover_orphan_disclosure_hop(
            session, lease=maintenance_lease, now=op_now + timedelta(seconds=5),
        )
        assert result == {
            "state": expected,
            "poll_id": identity.poll_id,
            "attempt_id": str(identity.attempt_id),
            "hop_ordinal": 0,
        }
        _poll_state, poll = await _quota_state(
            session, f"poll:{poll_root.id}", day=None,
        )
        if was_sent:
            assert poll["request_reserved"] == 0
            assert poll["request_consumed"] == 1
            assert poll["bytes_consumed"] == 4096
            terminal = await SupplementalUpdatesRepository(session).get_change_by_key(
                "local_owner",
                f"p4:attempt:{identity.attempt_id}:0:unknown",
            )
            assert terminal is not None
            assert terminal.payload["observed_response_bytes"] is None
        else:
            assert poll["request_reserved"] == 0
            assert poll["request_consumed"] == 0
            assert poll["bytes_consumed"] == 0
            terminal = await SupplementalUpdatesRepository(session).get_change_by_key(
                "local_owner",
                f"p4:attempt:{identity.attempt_id}:0:canceled",
            )
            assert terminal is not None


@pytest.mark.parametrize("orphan_phase", ["unsent", "sent_unknown"])
@pytest.mark.asyncio
async def test_maintenance_resets_only_recovered_orphan_checkpoint_then_poll_completes(
    recovery_db: AsyncEngine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    orphan_phase: str,
) -> None:
    settings, run_id, listing_id, _, _ = await _seed_poll(tmp_path)
    http = _HTTP(
        mode="normal",
        die_after_send=orphan_phase == "sent_unknown",
    )
    recovery_at = datetime.now(UTC)
    service = DisclosurePageService.for_tests(protocol=fixture_protocol(), http=http)
    handler = DisclosurePollingHandler.for_tests(
        settings=settings, page_service=service, clock=_Clock(recovery_at),
    )
    authorization_module = __import__(
        "investment_agent.supplemental_rules.disclosure_authorization",
        fromlist=["mark_http_hop_sent"],
    )
    original_mark_sent = authorization_module.mark_http_hop_sent
    if orphan_phase == "unsent":
        async def crash_before_send(*_args: Any, **_kwargs: Any) -> Any:
            raise KeyboardInterrupt("process loss after reservation before sent")

        monkeypatch.setattr(authorization_module, "mark_http_hop_sent", crash_before_send)

    first_job = await _claim(run_id)
    assert first_job is not None
    with pytest.raises(KeyboardInterrupt):
        await handler.execute(first_job, _InlineContext())
    assert http.calls == (1 if orphan_phase == "sent_unknown" else 0)

    # Exhaust the run's bounded lease retries without running it again. The
    # immutable original hop then belongs to a terminal, fenced run so only a
    # maintenance lease may cancel/unknown-settle it.
    stale_at = recovery_at + timedelta(hours=1)
    for attempt_number in (1, 2, 3):
        async with session_scope() as session:
            requeued, failed = await PipelineRunRepository(session).recover_stale(
                kind=DISCLOSURE_POLL_KIND, stale_before=stale_at, now=stale_at,
            )
            if attempt_number < 3:
                assert run_id in requeued and failed == []
            else:
                assert run_id in failed and requeued == []
        stale_at += timedelta(hours=1)
        if attempt_number < 3:
            claim_at = stale_at + timedelta(seconds=1)
            queued_attempt = await _claim(run_id, now=claim_at)
            assert queued_attempt is not None
            assert queued_attempt.lease.attempt == attempt_number + 1
            stale_at = claim_at + timedelta(hours=1)

    async with session_scope() as session:
        old_state = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        assert old_state is not None
        checkpoint = old_state.payload["poll_checkpoint"]
        orphan_attempt_id = checkpoint["attempt_id"]
        before_ordinals = dict(checkpoint["logical_attempt_ordinals"])
        assert before_ordinals["directory"] == 1

        maintenance, _ = await PipelineRunRepository(session).enqueue(
            kind=DISCLOSURE_POLL_KIND,
            idempotency_key=f"p4-test-orphan-maintenance:{orphan_phase}:{uuid4()}",
            triggered_by="test",
            request_json={"record_type": "poll_maintenance_v1", "schema_version": 1,
                          "owner_id": "local_owner", "operation_id": str(uuid4())},
            available_at=stale_at,
        )
        claimed_maintenance = await PipelineRunRepository(session).claim_next(
            worker_id="p4-recovery-test", kind=DISCLOSURE_POLL_KIND,
            run_id=maintenance.id, now=stale_at,
        )
        assert claimed_maintenance is not None
        maintenance_job = ClaimedJob(
            run_id=claimed_maintenance.id,
            kind=claimed_maintenance.kind,
            lease=RunLease(
                claimed_maintenance.id, "p4-recovery-test", claimed_maintenance.attempt_count,
            ),
            request_json=dict(claimed_maintenance.request_json),
        )

    monkeypatch.setattr(authorization_module, "mark_http_hop_sent", original_mark_sent)
    handler._clock = _Clock(stale_at + timedelta(minutes=1))
    assert await handler.execute(maintenance_job, _InlineContext()) == "completed"

    async with session_scope() as session:
        resumed_state = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        assert resumed_state is not None
        resumed_checkpoint = resumed_state.payload["poll_checkpoint"]
        assert resumed_checkpoint["attempt_id"] is None
        assert resumed_checkpoint["redirect_count"] == 0
        assert resumed_checkpoint["current_url"] is None
        assert resumed_checkpoint["logical_attempt_ordinals"] == before_ordinals
        phase = "canceled" if orphan_phase == "unsent" else "unknown"
        orphan_receipt = await SupplementalUpdatesRepository(session).get_change_by_key(
            "local_owner", f"p4:attempt:{orphan_attempt_id}:0:{phase}",
        )
        assert orphan_receipt is not None

    if orphan_phase == "sent_unknown":
        http.die_after_send = False
    await _drive(handler, None)

    async with session_scope() as session:
        repo = SupplementalUpdatesRepository(session)
        final_source = await repo.get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_id}:company_research",
        )
        assert final_source is not None and final_source.payload["poll_state"] == "completed"
        assert await session.scalar(select(func.count()).select_from(DisclosureItem)) == 1
        attempts = list((await session.scalars(select(SupplementalChangeReceiptRow).where(
            SupplementalChangeReceiptRow.owner_id == "local_owner",
            SupplementalChangeReceiptRow.change_key.like("p4:attempt:%:reserved"),
        ))).all())
        directory_ordinals = sorted(
            row.payload["permit"]["identity"]["attempt_ordinal"]
            for row in attempts
            if row.payload.get("permit", {}).get("identity", {}).get("poll_id")
            == final_source.payload.get("active_poll_id")
            and row.payload["permit"]["identity"].get("logical_request_key") == "directory"
        )
        assert directory_ordinals == [1, 2]
        resumed_attempt_id = next(
            row.payload["permit"]["identity"]["attempt_id"]
            for row in attempts
            if row.payload.get("permit", {}).get("identity", {}).get("poll_id")
            == final_source.payload.get("active_poll_id")
            and row.payload["permit"]["identity"].get("logical_request_key") == "directory"
            and row.payload["permit"]["identity"].get("attempt_ordinal") == 2
        )
        resumed_result = await repo.get_change_by_key(
            "local_owner",
            f"p4:attempt:{resumed_attempt_id}:0:result",
        )
        assert resumed_result is not None
