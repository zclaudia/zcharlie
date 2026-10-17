"""Offline file-SQLite guards for the manual disclosure runner seam."""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio.engine import AsyncEngine

from investment_agent.config import Settings
from investment_agent.evidence.disclosure_http import (
    DIRECTORY,
    QUERY,
    OfficialHTTPS,
    SourceResponse,
)
from investment_agent.evidence.disclosure_pages import CNInfoPageProtocolV1
from investment_agent.evidence.disclosure_profiles import (
    ADAPTER_BUILD,
    DisclosureApprovalV1,
    DisclosureProfileRegistry,
    DisclosureProfileV1,
    expected_approval_hash,
    expected_contract_hash,
    expected_protocol_hash,
)
from investment_agent.investor.foundations import InvestorError
from investment_agent.research.disclosure_campaign_runner import (
    _assert_database_shape,
    admit_disclosure_campaign_recovery,
    resume_disclosure_campaign_poll,
    run_disclosure_campaign_once,
    run_disclosure_campaign_recovery_once,
)
from investment_agent.research.worker import ResearchWorker
from investment_agent.settings.loader import store_for
from investment_agent.storage.database import dispose_engine, init_engine, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.disclosure_authorization import apply_authorization_command
from investment_agent.supplemental_rules.disclosure_campaign import (
    amend_disclosure_campaign,
    bind_disclosure_campaign_grant,
    create_disclosure_campaign,
    read_disclosure_campaign,
)
from investment_agent.supplemental_rules.disclosure_campaign_contracts import (
    BackgroundDisclosureAuthorizationBodyV2,
    BackgroundDisclosureAuthorizationCommandV2,
    CampaignAction,
    DisclosureCampaignV2,
)
from investment_agent.supplemental_rules.disclosure_contracts import AUTH_FEED
from investment_agent.supplemental_rules.disclosure_schedule import schedule_due_disclosure_polls
from investment_agent.supplemental_rules.disclosure_updates import (
    DISCLOSURE_POLL_KIND,
    DisclosurePollingHandler,
    DisclosurePollRequest,
    admit_disclosure_poll,
    resume_disclosure_poll,
)
from tests.integration.test_p4_disclosure_authorization import _seed_listings
from tests.support.p4_disclosures import fixture_protocol, fixture_source_response


@pytest.mark.parametrize(
    "database_url",
    (
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite:///file::memory:?cache=shared&uri=true",
    ),
)
def test_manual_runner_rejects_memory_sqlite_before_dispatch(database_url: str) -> None:
    from types import SimpleNamespace

    session = SimpleNamespace(get_bind=lambda: SimpleNamespace(url=database_url))
    with pytest.raises(InvestorError, match="file-backed SQLite"):
        _assert_database_shape(session)


@pytest_asyncio.fixture
async def campaign_runner_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncEngine]:
    url = f"sqlite+aiosqlite:///{tmp_path / 'manual-campaign.sqlite'}"
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
        api_key="manual-campaign-test",
        data_dir=tmp_path,
        supplemental_rules_enabled=True,
        supplemental_updates_enabled=True,
        supplemental_disclosure_polling_enabled=True,
        disclosure_originals_enabled=True,
    )


def _approved_profile() -> tuple[DisclosureProfileV1, DisclosureApprovalV1]:
    protocol_data = fixture_protocol().model_dump(mode="json")
    protocol_data["protocol_hash"] = "0" * 64
    protocol = CNInfoPageProtocolV1.model_validate(protocol_data)
    protocol_data["protocol_hash"] = expected_protocol_hash(protocol)
    protocol = CNInfoPageProtocolV1.model_validate(protocol_data)
    profile_data = {
        "schema_version": 1,
        "source_id": "cninfo.v1",
        "feed_id": "cninfo_szse_v1",
        "protocol": protocol.model_dump(mode="json"),
        "adapter_build": ADAPTER_BUILD,
        "url_policy": "cninfo-https-single-hop-v1",
        "exact_terms_urls": (),
        "contract_hash": "0" * 64,
    }
    profile_data["contract_hash"] = expected_contract_hash(profile_data)
    profile = DisclosureProfileV1.model_validate(profile_data)
    approval_data = {
        "schema_version": 1,
        "profile_contract_hash": profile.contract_hash,
        "protocol_hash": protocol.protocol_hash,
        "evidence_hashes": ("d" * 64,),
        "approval_ref": "synthetic-p0-runner-profile",
        "executor_name": "offline executor",
        "reviewer_name": "offline independent reviewer",
        "purpose_scopes": ("directory", "query", "original"),
        "valid_from": datetime.now(UTC) - timedelta(minutes=1),
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "approval_hash": "0" * 64,
    }
    approval_data["approval_hash"] = expected_approval_hash(approval_data)
    return profile, DisclosureApprovalV1.model_validate(approval_data)


class _ControlledHTTP:
    def __init__(self, *, first_query_429: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.first_query_429 = first_query_429
        self.query_count = 0

    async def request_hop(
        self, url: str, purpose: str, *, body: bytes | None = None,
        max_bytes: int, timeout_seconds: float = 15,
    ) -> SourceResponse:
        del timeout_seconds
        self.calls.append((purpose, url))
        if purpose == "query":
            self.query_count += 1
            if self.first_query_429 and self.query_count == 1:
                return SourceResponse(429, {"Retry-After": "0"}, b"", url)
        mode = "normal" if purpose == "directory" else "empty"
        status, headers, content = fixture_source_response(
            purpose, body or b"", published_at=datetime.now(UTC) - timedelta(hours=1), mode=mode,
        )
        if len(content) > max_bytes:
            raise AssertionError("controlled response exceeded the reserved bound")
        return SourceResponse(status, headers, content, url)


async def test_ordinary_scheduler_does_not_enumerate_manual_v2_grants(
    campaign_runner_db: AsyncEngine, tmp_path: Path,
) -> None:
    now = datetime(2026, 10, 17, 2, tzinfo=UTC)
    settings = _settings(tmp_path)
    authorization_id = uuid4()
    async with session_scope() as session:
        store = store_for(session, settings)
        await store.claim_import()
        await store.apply({
            "supplemental_rules_enabled": True,
            "supplemental_updates_enabled": True,
            "supplemental_disclosure_polling_enabled": True,
            "disclosure_originals_enabled": True,
        }, actor="test")
        repo = SupplementalUpdatesRepository(session)
        await repo.ensure_source(
            "local_owner",
            AUTH_FEED,
            f"grant:{authorization_id}",
            "a" * 64,
            payload={"authorization": {
                "schema_version": 2,
                "authorization_id": str(authorization_id),
                "status": "active",
                "manual_only": True,
                "expires_at": (now + timedelta(hours=1)).isoformat(),
            }},
        )
        run, _ = await PipelineRunRepository(session).enqueue(
            kind=DISCLOSURE_POLL_KIND,
            idempotency_key="test:maintenance-manual-filter",
            triggered_by="test",
            request_json={"record_type": "poll_maintenance_v1"},
            max_attempts=1,
        )
        claimed = await PipelineRunRepository(session).claim_next(
            worker_id="manual-filter-test",
            kind=DISCLOSURE_POLL_KIND,
            run_id=run.id,
        )
        assert claimed is not None

    from investment_agent.jobs.base import ClaimedJob
    from investment_agent.storage.repositories.pipeline_run import RunLease

    job = ClaimedJob(
        run_id=claimed.id,
        kind=claimed.kind,
        lease=RunLease(claimed.id, "manual-filter-test", claimed.attempt_count),
        request_json=dict(claimed.request_json or {}),
    )
    async with session_scope() as session:
        assert await schedule_due_disclosure_polls(
            session, settings=settings, job=job, now=now,
        ) == 0
        rows = await SupplementalUpdatesRepository(session).list_changes(
            "local_owner", kinds=("source_attempt",), limit=100,
        )
        assert not any(row.payload.get("record_type") == "poll_root_v1" for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("first_query_429", (False, True))
async def test_non_test_manual_campaign_uses_exact_run_once_and_approved_controlled_http(
    campaign_runner_db: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest, first_query_429: bool,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    profile, approval = _approved_profile()
    import investment_agent.evidence.disclosure_profiles as profile_module

    profile_registry = DisclosureProfileRegistry.for_tests()
    monkeypatch.setattr(profile_module, "SERVER_PROFILE_REGISTRY", profile_registry)
    profile_registry.install_approved(profile, approval)
    cleanup_at = datetime.now(UTC)
    request.addfinalizer(lambda: profile_registry.revoke(
        approval.approval_hash, at=cleanup_at, reference="offline-test-cleanup",
    ))
    listing_start, listing_end = now - timedelta(days=2), now - timedelta(days=1)
    settings = _settings(tmp_path)
    grant_id, campaign_id = uuid4(), uuid4()
    db_path = str((tmp_path / "manual-campaign.sqlite").resolve())
    async with session_scope() as session:
        refs = await _seed_listings(session)
        listing_ref = refs[0]
        from investment_agent.storage.models.reference import Listing

        listing = await session.get(Listing, int(listing_ref.id))
        assert listing is not None
        listing.ticker = "300901"
        actions = tuple(
            CampaignAction(
                action_id=f"sample-poll-{ordinal}", kind="sample_poll",
                status="reserved" if ordinal == 3 else "approved", poll_ordinal=ordinal,
                listing_ref=listing_ref, window_start=listing_start, window_end=listing_end,
                profile_hash=profile.protocol.protocol_hash, contract_hash=profile.contract_hash,
                url_allowlist=(DIRECTORY, QUERY),
            )
            for ordinal in (1, 2, 3)
        )
        campaign = DisclosureCampaignV2(
            campaign_id=campaign_id, revision=1, approval_ref="offline-test-campaign",
            sample_env_id="sample-env-offline", database_path=db_path,
            valid_from=now - timedelta(minutes=1), expires_at=now + timedelta(days=2),
            http_cap=24, response_bytes_cap=128 * 1024 * 1024, storage_bytes_cap=256 * 1024 * 1024,
            s0_http_cap=0, s0_response_bytes_cap=0, s0_storage_bytes_cap=0,
            sample_http_cap=24, sample_response_bytes_cap=128 * 1024 * 1024, actions=actions,
        )
        campaign = await create_disclosure_campaign(session, campaign, now=now)
        body = BackgroundDisclosureAuthorizationBodyV2(
            schema_version=2, campaign_id=campaign_id, sample_env_id="sample-env-offline",
            manual_only=True, valid_from=now - timedelta(minutes=1),
            contract_hash=profile.contract_hash, listing_refs=(listing_ref,),
            doc_type_allowlist=("annual_report_full",), history_start=listing_start,
            history_end=listing_end, allow_overlap=True, allow_deep_sweep=False,
            min_poll_interval_seconds=21_600, min_http_interval_seconds=5,
            expires_at=now + timedelta(days=1), max_staleness_seconds=86_400,
            per_poll_query_http=4, per_poll_fetch=0, per_poll_http=8,
            per_poll_response_bytes=32 * 1024 * 1024, grant_daily_http=24,
            grant_daily_response_bytes=128 * 1024 * 1024, storage_bytes_cap=128 * 1024 * 1024,
        )
        grant_result = await apply_authorization_command(
            session,
            BackgroundDisclosureAuthorizationCommandV2(
                action="create", operation_id=uuid4(), authorization_id=grant_id,
                expected_revision=0, owner_daily_http=24,
                owner_daily_response_bytes=128 * 1024 * 1024,
                owner_min_request_interval_seconds=5, expected_owner_policy_revision=0,
                body=body,
            ), actor="api", now=now,
        )
        grant = grant_result.authorization
        await bind_disclosure_campaign_grant(
            session, campaign_id=campaign_id, grant_id=grant_id, grant_revision=1,
            sample_env_id="sample-env-offline", now=now,
        )
        await store_for(session, settings).claim_import()
        await store_for(session, settings).apply({
            "supplemental_rules_enabled": True,
            "supplemental_updates_enabled": True,
            "supplemental_disclosure_polling_enabled": True,
            "disclosure_originals_enabled": True,
        }, actor="offline-test")
        result = await admit_disclosure_poll(
            session,
            DisclosurePollRequest(
                operation_id=uuid4(), authorization_id=grant_id,
                authorization_revision=grant.revision, authorization_hash=grant.authorization_hash,
                listing_ref=listing_ref, window_start=listing_start, window_end=listing_end,
                expected_source_revision=0, campaign_action_id="sample-poll-1",
                campaign_profile_hash=profile.protocol.protocol_hash,
            ),
            settings=settings, now=now,
        )

    controlled = _ControlledHTTP(first_query_429=first_query_429)

    async def controlled_hop(
        _self: OfficialHTTPS, url: str, purpose: str, *, body: bytes | None = None,
        max_bytes: int = 16 * 1024 * 1024, timeout_seconds: float = 15,
        exact_urls: tuple[str, ...] = (),
    ) -> SourceResponse:
        del exact_urls
        return await controlled.request_hop(
            url, purpose, body=body, max_bytes=max_bytes, timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(OfficialHTTPS, "request_one_hop", controlled_hop)
    clock_value = [now]
    original_handler_init = DisclosurePollingHandler.__init__

    def advancing_handler_init(self: DisclosurePollingHandler, *, settings: Settings) -> None:
        original_handler_init(self, settings=settings)

        def advance() -> datetime:
            clock_value[0] += timedelta(seconds=6)
            return clock_value[0]

        self._clock = advance

    monkeypatch.setattr(DisclosurePollingHandler, "__init__", advancing_handler_init)
    run_id = result["run_id"]
    assert isinstance(run_id, int)
    hops = 0
    while True:
        await run_disclosure_campaign_once(
            run_id=run_id, campaign_id=campaign_id, action_id="sample-poll-1",
            grant_id=grant_id, grant_revision=grant.revision, poll_id=result["poll_id"],
            sample_env_id="sample-env-offline", settings=settings,
        )
        hops += 1
        async with session_scope() as session:
            repo = SupplementalUpdatesRepository(session)
            source = await repo.get_source("local_owner", "cninfo_szse_v1", f"listing:{listing_ref.id}:company_research")
            assert source is not None
            poll_state = source.payload.get("poll_state")
            next_run_id = source.payload.get("active_step_run_id")
            if poll_state == "completed":
                break
            if first_query_429 and poll_state == "retry_wait":
                stopped_run = await session.get(PipelineRun, next_run_id)
                assert stopped_run is not None and stopped_run.status == "partial"
                break
            assert hops <= 32, f"nonterminal campaign state: {poll_state}, run={next_run_id}, http={controlled}"
            assert isinstance(next_run_id, int)
            run_id = next_run_id
    if first_query_429:
        assert [purpose for purpose, _url in controlled.calls] == ["directory", "query"]
        async with session_scope() as session:
            source = await SupplementalUpdatesRepository(session).get_source(
                "local_owner", "cninfo_szse_v1", f"listing:{listing_ref.id}:company_research",
            )
            assert source is not None and source.payload["poll_state"] == "retry_wait"
            assert source.retry_at is not None
            original_checkpoint = dict(source.payload["poll_checkpoint"])
            assert original_checkpoint["stage"] == "query"
            assert original_checkpoint["attempt_ordinal"] == 1
            with pytest.raises(InvestorError) as generic_resume:
                await resume_disclosure_poll(
                    session, UUID(str(result["poll_id"])), uuid4(), source.revision,
                    "ordinary resume cannot continue a manual campaign",
                    settings=settings, now=source.retry_at + timedelta(seconds=1),
                )
            assert generic_resume.value.detail.code == "disclosure_campaign_resume_required"
            await session.rollback()
            rows = (await session.scalars(
                select(PipelineRun).where(PipelineRun.kind == DISCLOSURE_POLL_KIND)
            )).all()
            assert all(run.status not in {"queued", "running"} for run in rows)
            campaign_state = await read_disclosure_campaign(session, campaign_id)
            paused = await amend_disclosure_campaign(
                session, campaign_id=campaign_id,
                expected_revision=campaign_state.revision,
                patch={"status": "paused"}, approval_ref="offline-operator-pause",
                now=source.retry_at + timedelta(seconds=1),
            )
            assert paused.status == "paused"
            resume_now = source.retry_at + timedelta(seconds=1)
            retry_at = source.retry_at
        with pytest.raises(InvestorError) as early_resume:
            await resume_disclosure_campaign_poll(
                campaign_id=campaign_id, action_id="sample-poll-1", grant_id=grant_id,
                grant_revision=grant.revision, poll_id=UUID(str(result["poll_id"])),
                sample_env_id="sample-env-offline",
                resume_approval_ref="offline-operator-early-resume-ticket",
                operation_id=uuid4(), reason="must respect Retry-After",
                settings=settings, now=retry_at - timedelta(seconds=1),
            )
        assert early_resume.value.detail.code == "source_cooldown"
        resume_operation_id = uuid4()
        resumed = await resume_disclosure_campaign_poll(
            campaign_id=campaign_id, action_id="sample-poll-1", grant_id=grant_id,
            grant_revision=grant.revision, poll_id=UUID(str(result["poll_id"])),
            sample_env_id="sample-env-offline",
            resume_approval_ref="offline-operator-resume-ticket",
            operation_id=resume_operation_id, reason="resume after 429 Retry-After",
            settings=settings, now=resume_now,
        )
        assert resumed["state"] == "queued" and not resumed["replayed"]
        resumed_replay = await resume_disclosure_campaign_poll(
            campaign_id=campaign_id, action_id="sample-poll-1", grant_id=grant_id,
            grant_revision=grant.revision, poll_id=UUID(str(result["poll_id"])),
            sample_env_id="sample-env-offline",
            resume_approval_ref="offline-operator-resume-ticket",
            operation_id=resume_operation_id, reason="resume after 429 Retry-After",
            settings=settings, now=resume_now,
        )
        assert resumed_replay["replayed"] is True
        assert resumed_replay["run_id"] == resumed["run_id"]
        with pytest.raises(InvestorError, match="another scope"):
            await resume_disclosure_campaign_poll(
                campaign_id=campaign_id, action_id="sample-poll-1", grant_id=grant_id,
                grant_revision=grant.revision, poll_id=UUID(str(result["poll_id"])),
                sample_env_id="sample-env-offline",
                resume_approval_ref="offline-operator-resume-ticket",
                operation_id=resume_operation_id, reason="different resume reason",
                settings=settings, now=resume_now,
            )
        async with session_scope() as session:
            resumed_state = await SupplementalUpdatesRepository(session).get_source(
                "local_owner", "cninfo_szse_v1", f"listing:{listing_ref.id}:company_research",
            )
            assert resumed_state is not None
            assert resumed_state.payload["poll_checkpoint"] == original_checkpoint
            assert resumed_state.payload["retry_at"] is None
            run = await PipelineRunRepository(session).by_idempotency_key(
                kind=DISCLOSURE_POLL_KIND,
                idempotency_key=f"p4:disclosure-resume:{resume_operation_id}",
            )
            assert run is not None and run.max_attempts == 1
            assert run.request_json["manual_campaign_resume"] is True
        run_id = int(resumed["run_id"])
        hops = 0
        while True:
            await run_disclosure_campaign_once(
                run_id=run_id, campaign_id=campaign_id, action_id="sample-poll-1",
                grant_id=grant_id, grant_revision=grant.revision,
                poll_id=UUID(str(result["poll_id"])),
                sample_env_id="sample-env-offline", settings=settings,
            )
            hops += 1
            async with session_scope() as session:
                source = await SupplementalUpdatesRepository(session).get_source(
                    "local_owner", "cninfo_szse_v1", f"listing:{listing_ref.id}:company_research",
                )
                assert source is not None
                if source.payload.get("poll_state") == "completed":
                    break
                run_id = source.payload.get("active_step_run_id")
                assert hops <= 32 and isinstance(run_id, int)
        async with session_scope() as session:
            resumed_campaign = await read_disclosure_campaign(session, campaign_id)
            assert resumed_campaign.status == "active"
            assert resumed_campaign.poll_usage[str(result["poll_id"])]["http_consumed"] >= 4
    else:
        assert [purpose for purpose, _url in controlled.calls] == [
            "directory", "query", "query",
        ]

    # A full history scope is required before this completed, non-test proof
    # can become a live V3 source binding. Keep the registry isolated so the
    # synthetic approval and its revocation cannot affect other tests.
    from investment_agent.evidence.disclosure_profiles import approved_contract
    from investment_agent.supplemental_rules.contracts import (
        PollWindowSourceProof,
        SourceCoverageBindingV3,
    )
    from investment_agent.supplemental_rules.coverage import build_update_source_binding
    from investment_agent.supplemental_rules.disclosure_views import source_authorization_view
    from investment_agent.supplemental_rules.publication_limits import _poll_source_coverage_limits

    proof_at = datetime.now(UTC) + timedelta(minutes=1)
    async with session_scope() as session:
        debug_state = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_ref.id}:company_research",
        )
        assert debug_state is not None
        final = await SupplementalUpdatesRepository(session).get_change(
            "local_owner", int(debug_state.payload.get("latest_final_sequence", 0)),
        )
        assert final is not None
        # The deterministic handler clock advances beyond wall time; proof
        # knowledge must be cut off after the committed final receipt.
        proof_at = max(proof_at, final.committed_at + timedelta(seconds=1))
        binding = await build_update_source_binding(
            session, owner="local_owner", listing_ref=listing_ref,
            observed_at=proof_at, knowledge_cutoff=proof_at,
        )
        assert isinstance(binding, SourceCoverageBindingV3)
        poll_proof = next(
            item for item in binding.sources if isinstance(item, PollWindowSourceProof)
        )
        assert poll_proof.protocol_profile == "live"
        assert poll_proof.protocol_status == "live_validated"
        assert poll_proof.history_start == listing_start
        assert poll_proof.publication_window_end == listing_end
        assert approved_contract(profile.contract_hash, now=proof_at)
        source_state = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_ref.id}:company_research",
        )
        assert source_state is not None
        source_payload = {**source_state.payload, "contract_hash": poll_proof.contract_hash}
        source_view = await source_authorization_view(session, source_payload, at=proof_at)
        assert source_view["profile_approval_status"] == "approved"
        assert source_view["protocol_status"] == "live_validated"
        assert await _poll_source_coverage_limits(
            session, owner="local_owner", listing_id=int(listing_ref.id),
            context="company_research", source=poll_proof, at=proof_at,
            lock_source_state=False,
        ) == ()

    profile_registry.revoke(
        approval.approval_hash, at=cleanup_at, reference="offline-test-cleanup",
    )
    revoked_at = datetime.now(UTC)
    async with session_scope() as session:
        revoked_binding = await build_update_source_binding(
            session, owner="local_owner", listing_ref=listing_ref,
            observed_at=revoked_at, knowledge_cutoff=revoked_at,
        )
        assert revoked_binding is None
        source_state = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", "cninfo_szse_v1", f"listing:{listing_ref.id}:company_research",
        )
        assert source_state is not None
        revoked_view = await source_authorization_view(
            session, {**source_state.payload, "contract_hash": poll_proof.contract_hash}, at=revoked_at,
        )
        assert revoked_view["profile_approval_status"] == "unapproved"
        assert revoked_view["protocol_status"] == "unvalidated"
        assert await _poll_source_coverage_limits(
            session, owner="local_owner", listing_id=int(listing_ref.id),
            context="company_research", source=poll_proof, at=revoked_at,
            lock_source_state=False,
        ) == ("source_coverage_unknown:cninfo_szse_v1",)

    ordinary_recovery_run_id = await admit_disclosure_campaign_recovery(
        campaign_id=campaign_id,
        action_id="sample-poll-1",
        grant_id=grant_id,
        grant_revision=grant.revision,
        poll_id=result["poll_id"],
        sample_env_id="sample-env-offline",
        recovery_approval_ref="offline-operator-recovery-ticket",
        operation_id=uuid4(),
    )
    ordinary_worker = ResearchWorker(
        settings=settings,
        worker_id="ordinary-worker-no-manual-capability",
        handlers=[DisclosurePollingHandler(settings=settings)],
        kinds=[DISCLOSURE_POLL_KIND],
    )
    await ordinary_worker.run_once(run_id=ordinary_recovery_run_id)
    expected_calls = (
        ["directory", "query", "query", "query"]
        if first_query_429 else ["directory", "query", "query"]
    )
    assert [purpose for purpose, _url in controlled.calls] == expected_calls

    recovery_operation_id = uuid4()
    recovery_run_id = await admit_disclosure_campaign_recovery(
        campaign_id=campaign_id,
        action_id="sample-poll-1",
        grant_id=grant_id,
        grant_revision=grant.revision,
        poll_id=result["poll_id"],
        sample_env_id="sample-env-offline",
        recovery_approval_ref="offline-operator-recovery-ticket",
        operation_id=recovery_operation_id,
    )
    await run_disclosure_campaign_recovery_once(
        run_id=recovery_run_id,
        campaign_id=campaign_id,
        action_id="sample-poll-1",
        grant_id=grant_id,
        grant_revision=grant.revision,
        poll_id=result["poll_id"],
        sample_env_id="sample-env-offline",
        recovery_approval_ref="offline-operator-recovery-ticket",
        settings=settings,
    )
    assert [purpose for purpose, _url in controlled.calls] == expected_calls
    async with session_scope() as session:
        poll_runs = (await session.scalars(
            select(PipelineRun).where(PipelineRun.kind == DISCLOSURE_POLL_KIND)
        )).all()
        assert all(run.status not in {"queued", "running"} for run in poll_runs)
