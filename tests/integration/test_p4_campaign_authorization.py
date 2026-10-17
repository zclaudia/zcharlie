"""08-P0: the native hop ledger and campaign authority commit together."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from investment_agent.config import Settings
from investment_agent.evidence.disclosure_http import DIRECTORY, QUERY
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import RunLease
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.disclosure_authorization import (
    apply_authorization_command,
    cancel_unsent_http_hop,
    freeze_poll_quota,
    mark_http_hop_sent,
    read_authorization_snapshot,
    reserve_http_hop,
    settle_http_hop_result,
)
from investment_agent.supplemental_rules.disclosure_campaign import (
    amend_disclosure_campaign,
    assert_campaign_resume_authority,
    authorize_campaign_resume,
    authorize_disclosure_campaign_poll,
    complete_campaign_poll,
    create_disclosure_campaign,
    read_disclosure_campaign,
    resume_disclosure_campaign,
)
from investment_agent.supplemental_rules.disclosure_campaign_contracts import (
    BackgroundDisclosureAuthorizationBodyV2,
    BackgroundDisclosureAuthorizationCommandV2,
    CampaignAction,
    DisclosureCampaignV2,
)
from investment_agent.supplemental_rules.disclosure_contracts import (
    BackgroundDisclosureAuthorizationCommand,
    BackgroundDisclosureAuthorizationV1,
    DisclosureAttemptIdentity,
    authorization_scope_hash,
)
from tests.integration.test_p4_disclosure_authorization import _seed_listings


@pytest.fixture(autouse=True)
def synthetic_approval(monkeypatch):
    # This suite isolates durable accounting, not source approval acceptance.
    # The source/runner suites exercise the real synthetic registry boundary.
    from types import SimpleNamespace

    import investment_agent.evidence.disclosure_profiles as profiles

    monkeypatch.setattr(profiles, "require_approved_contract", lambda *a, **k: SimpleNamespace(
        approval=SimpleNamespace(purpose_scopes=("directory", "query", "original"))))


@pytest_asyncio.fixture
async def campaign_sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'campaign.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False), tmp_path / "campaign.db"
    await engine.dispose()


async def _setup(session, path, now, *, raw_first=False):
    refs = await _seed_listings(session)
    campaign_id = uuid4()
    contract = "a" * 64
    start, end = now - timedelta(days=2), now - timedelta(days=1)
    campaign = DisclosureCampaignV2(
        campaign_id=campaign_id, revision=1, approval_ref="synthetic-review-only",
        sample_env_id="isolated-p0-test", database_path=str(path.resolve()),
        valid_from=now - timedelta(seconds=1), expires_at=now + timedelta(days=3),
        http_cap=32, response_bytes_cap=192 * 1024**2, storage_bytes_cap=512 * 1024**2,
        s0_http_cap=8, s0_response_bytes_cap=40 * 1024**2,
        s0_storage_bytes_cap=128 * 1024**2, sample_http_cap=24,
        sample_response_bytes_cap=152 * 1024**2,
        actions=(CampaignAction(
            action_id="p1", kind="sample_poll", status="approved", listing_ref=refs[0],
            window_start=start, window_end=end, poll_ordinal=1,
            profile_hash="b" * 64, contract_hash=contract,
            url_allowlist=(DIRECTORY, QUERY),
        ), CampaignAction(
            action_id="p2", kind="sample_poll", status="approved", listing_ref=refs[0],
            window_start=start, window_end=end, poll_ordinal=2,
            profile_hash="b" * 64, contract_hash=contract, url_allowlist=(DIRECTORY, QUERY),
        ), CampaignAction(action_id="p3", kind="sample_poll", status="reserved", poll_ordinal=3),
        CampaignAction(action_id="s0-terms", kind="terms", status="approved",
                       url_allowlist=("https://www.cninfo.com.cn/",), http_cap=1,
                       response_byte_cap=4096, storage_byte_cap=2 * 1024**2)),
    )
    await create_disclosure_campaign(session, campaign, now=now)
    body = BackgroundDisclosureAuthorizationBodyV2(
        campaign_id=campaign_id, sample_env_id="isolated-p0-test", valid_from=now,
        contract_hash=contract, listing_refs=refs, doc_type_allowlist=("annual_report_full",),
        history_start=start, history_end=end, min_poll_interval_seconds=43200,
        min_http_interval_seconds=10, expires_at=now + timedelta(days=2),
        max_staleness_seconds=86400, per_poll_query_http=4, per_poll_fetch=1,
        per_poll_http=8, per_poll_response_bytes=64 * 1024**2,
        grant_daily_http=24, grant_daily_response_bytes=128 * 1024**2,
        storage_bytes_cap=512 * 1024**2,
    )
    command = BackgroundDisclosureAuthorizationCommandV2(
        action="create", operation_id=uuid4(), authorization_id=uuid4(), expected_revision=0,
        expected_owner_policy_revision=0, owner_daily_http=24,
        owner_daily_response_bytes=128 * 1024**2, owner_min_request_interval_seconds=10,
        body=body,
    )
    result = await apply_authorization_command(session, command, actor="api", now=now)
    grant = result.authorization
    if raw_first:
        from investment_agent.supplemental_rules.disclosure_campaign import (
            mark_campaign_hop_sent,
            reserve_campaign_action_hop,
            settle_campaign_hop,
        )

        raw = await reserve_campaign_action_hop(
            session, campaign_id=campaign_id, action_id="s0-terms", operation_key="initial-s0",
            sample_env_id=grant.sample_env_id, url="https://www.cninfo.com.cn/", request_bytes=0,
            response_bytes_limit=4096, storage_bytes_limit=2 * 1024**2, purpose="terms", now=now,
        )
        from uuid import UUID
        raw_id = UUID(raw["permit_id"])
        await mark_campaign_hop_sent(session, permit_id=raw_id, now=now)
        await settle_campaign_hop(session, permit_id=raw_id, response_bytes=128,
                                   storage_bytes=1024**2 + 128, result_code="synthetic-known", now=now)
    poll = str(uuid4())
    await authorize_disclosure_campaign_poll(
        session, grant=grant, poll_id=poll, action_id="p1", listing_ref=refs[0],
        window_start=start, window_end=end, sample_env_id=grant.sample_env_id,
        database_path=str(path), profile_hash="b" * 64, contract_hash=contract, now=now,
    )
    scope = await freeze_poll_quota(
        session, poll_id=poll, authorization=grant, listing_ref=refs[0],
        window_start=start, window_end=end, poll_http_cap=8, poll_query_http_cap=4,
        poll_fetch_cap=1, poll_response_bytes_cap=64 * 1024**2, now=now,
    )
    run = PipelineRun(kind="supplemental_update", triggered_by="test", started_at=now,
                      status="running", stage="disclosure", attempt_count=1,
                      worker_id="campaign-test", request_json={})
    session.add(run)
    await session.flush()
    identity = DisclosureAttemptIdentity(
        poll_id=poll, logical_request_key="directory:1", attempt_id=uuid4(), attempt_ordinal=1,
        hop_ordinal=0, hop_id="p0-hop", request_kind="directory", request_hash="c" * 64,
        authorization_id=grant.authorization_id, authorization_revision=grant.revision,
        authorization_hash=grant.authorization_hash, contract_hash=contract, scope_hash=scope,
        listing_ref=refs[0], window_start=start, window_end=end, poll_http_cap=8,
        poll_query_http_cap=4, poll_fetch_cap=1, poll_response_bytes_cap=64 * 1024**2,
        url=DIRECTORY, response_bytes_limit=4096,
    )
    settings = Settings(_env_file=None, api_key="isolated-p0-test",
                        supplemental_rules_enabled=True, supplemental_updates_enabled=True,
                        supplemental_disclosure_polling_enabled=True, disclosure_originals_enabled=True)
    return command, grant, identity, RunLease(run.id, "campaign-test", 1), settings


async def test_v2_wire_rejects_old_reader_and_preserves_exact_replay(campaign_sessions):
    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        command, grant, *_ = await _setup(session, path, now)
        with pytest.raises(ValidationError):
            BackgroundDisclosureAuthorizationV1.model_validate(grant.model_dump(mode="json"))
        replay = await apply_authorization_command(session, command, actor="api", now=now)
        assert replay.replayed and replay.authorization == grant
    async with factory() as session:
        assert await read_authorization_snapshot(session, grant.authorization_id) == grant


@pytest.mark.parametrize("sent", [False, True])
async def test_revoke_cancels_unsent_or_charges_unknown_in_both_ledgers(campaign_sessions, sent):
    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        _, grant, identity, lease, settings = await _setup(session, path, now)
        permit = await reserve_http_hop(session, identity=identity, response_byte_limit=4096,
                                        lease=lease, base_settings=settings, now=now)
        if sent:
            permit = await mark_http_hop_sent(session, permit=permit, lease=lease,
                                              base_settings=settings, now=now)
    # Reopening a disk-backed database preserves every reservation and permit.
    async with factory.begin() as session:
        revoke = BackgroundDisclosureAuthorizationCommand(
            action="revoke", operation_id=uuid4(), authorization_id=grant.authorization_id,
            expected_revision=1, expected_owner_policy_revision=1, owner_daily_http=24,
            owner_daily_response_bytes=128 * 1024**2, owner_min_request_interval_seconds=10,
            reason="synthetic revoked in flight",
        )
        await apply_authorization_command(session, revoke, actor="api", now=now)
        if sent:
            result = await settle_http_hop_result(session, permit=permit, lease=lease,
                                                  response_bytes=None, result_code="unknown", now=now)
            assert result.response_bytes_consumed == 4096
        else:
            with pytest.raises(InvestorError):
                await mark_http_hop_sent(session, permit=permit, lease=lease,
                                         base_settings=settings, now=now)
            assert await cancel_unsent_http_hop(session, permit=permit, lease=lease, now=now)
        campaign = await read_disclosure_campaign(session, grant.campaign_id)
        assert campaign.http_reserved == campaign.response_bytes_reserved == 0
        assert campaign.http_consumed == int(sent)
        assert campaign.response_bytes_consumed == (4096 if sent else 0)
        from investment_agent.supplemental_rules.disclosure_authorization import STORAGE_SCOPE
        from investment_agent.supplemental_rules.disclosure_contracts import QUOTA_FEED

        owner_storage = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", QUOTA_FEED, STORAGE_SCOPE)
        grant_storage = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", QUOTA_FEED, f"grant-storage:{grant.authorization_id}")
        assert owner_storage.payload["storage_consumed"] == (permit.storage_reserved_bytes if sent else 0)
        assert grant_storage.payload["storage_consumed"] == (permit.storage_reserved_bytes if sent else 0)
        assert identity.scope_hash == authorization_scope_hash(
            grant.authorization_id, grant.revision, grant.authorization_hash,
            grant.contract_hash, identity.listing_ref, identity.window_start, identity.window_end,
        )


async def test_concurrent_reservations_have_one_native_and_campaign_winner(campaign_sessions):
    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        _, grant, identity, lease, settings = await _setup(session, path, now)

    async def reserve(attempt):
        try:
            async with factory.begin() as session:
                return await reserve_http_hop(
                    session, identity=identity.model_copy(update={"attempt_id": attempt}),
                    response_byte_limit=4096, lease=lease, base_settings=settings, now=now,
                )
        except InvestorError as exc:
            return exc.detail.code

    results = await asyncio.gather(reserve(uuid4()), reserve(uuid4()))
    assert sum(not isinstance(result, str) for result in results) == 1
    assert "source_cooldown" in results
    async with factory() as session:
        campaign = await read_disclosure_campaign(session, grant.campaign_id)
        assert campaign.http_reserved == 1 and campaign.http_consumed == 0
        assert campaign.response_bytes_reserved == 4096


async def test_native_receipt_failure_rolls_back_campaign_reservation(campaign_sessions, monkeypatch):
    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        _, grant, identity, lease, settings = await _setup(session, path, now)
    append = SupplementalUpdatesRepository.append_change

    async def fail_native(self, *args, **kwargs):
        if kwargs.get("change_key", "").startswith("p4:attempt:"):
            raise RuntimeError("injected failure after campaign reserve")
        return await append(self, *args, **kwargs)

    monkeypatch.setattr(SupplementalUpdatesRepository, "append_change", fail_native)
    with pytest.raises(RuntimeError, match="injected failure"):
        async with factory.begin() as session:
            await reserve_http_hop(session, identity=identity, response_byte_limit=4096,
                                   lease=lease, base_settings=settings, now=now)
    async with factory() as session:
        campaign = await read_disclosure_campaign(session, grant.campaign_id)
        assert campaign.http_reserved == campaign.response_bytes_reserved == 0
        assert campaign.http_consumed == campaign.response_bytes_consumed == 0


async def test_profile_revoked_between_reserve_and_sent_is_fenced(campaign_sessions, monkeypatch):
    from investment_agent.evidence import disclosure_profiles as profiles
    from investment_agent.evidence.disclosure_http import DisclosureError

    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        _, grant, identity, lease, settings = await _setup(session, path, now)
        permit = await reserve_http_hop(session, identity=identity, response_byte_limit=4096,
                                        lease=lease, base_settings=settings, now=now)

    def revoked(*args, **kwargs):
        raise DisclosureError("source_profile_revoked")

    monkeypatch.setattr(profiles, "require_approved_contract", revoked)
    async with factory.begin() as session:
        with pytest.raises(InvestorError):
            await mark_http_hop_sent(session, permit=permit, lease=lease,
                                     base_settings=settings, now=now)
        await cancel_unsent_http_hop(session, permit=permit, lease=lease, now=now)
        campaign = await read_disclosure_campaign(session, grant.campaign_id)
        assert campaign.http_consumed == campaign.http_reserved == 0


async def test_campaign_and_current_profile_read_models_never_write(campaign_sessions):
    from investment_agent.supplemental_rules.disclosure_views import (
        authorization_view,
        source_authorization_view,
    )

    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        _, grant, *_ = await _setup(session, path, now)
    statements = []
    async with factory() as session:
        engine = session.bind.sync_engine

        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement.lstrip().split()[0].upper())

        event.listen(engine, "before_cursor_execute", capture)
        try:
            view = await authorization_view(session, grant.authorization_id, at=now)
            assert view["manual_only"] and view["campaign_status"] == "active"
            assert view["campaign_remaining_http"] == 32
            source = await source_authorization_view(session, {
                "authorization_id": str(grant.authorization_id), "contract_hash": grant.contract_hash,
                "protocol_profile": "live", "protocol_status": "live_validated",
            }, at=now)
            # Persisted "live_validated" is insufficient when the server has
            # no current source approval. No timer or worker is involved.
            assert source["profile_approval_status"] == "unapproved"
            assert source["protocol_status"] == "unvalidated"
        finally:
            event.remove(engine, "before_cursor_execute", capture)
    assert statements and not set(statements) & {"INSERT", "UPDATE", "DELETE", "REPLACE"}


async def test_api_source_view_uses_source_config_hash_without_rewriting_payload(campaign_sessions, monkeypatch):
    from investment_agent.api.routes.supplemental_updates import source_views
    from investment_agent.evidence import disclosure_profiles as profiles

    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        _, grant, identity, *_ = await _setup(session, path, now)
        payload = {"authorization_id": str(grant.authorization_id),
                   "protocol_profile": "live", "protocol_status": "live_validated"}
        source = await SupplementalUpdatesRepository(session).ensure_source(
            "local_owner", "cninfo_szse_v1", f"listing:{identity.listing_ref.id}:company_research",
            grant.contract_hash, listing_id=int(identity.listing_ref.id), context="company_research",
            payload=payload,
        )
    checked = []

    def approved(contract, *, now, **kwargs):
        checked.append(contract)
        return contract == grant.contract_hash

    monkeypatch.setattr(profiles, "approved_contract", approved)
    async with factory() as session:
        views = await source_views(session, [source], now)
        assert views[0]["profile_approval_status"] == "approved"
        assert views[0]["protocol_status"] == "live_validated"
        assert checked == [grant.contract_hash]
        from investment_agent.supplemental_rules.disclosure_views import disclosure_metrics

        metrics = await disclosure_metrics(session)
        assert metrics["scopes"][0]["profile_approval_status"] == "approved"
        assert metrics["scopes"][0]["protocol_status"] == "live_validated"
        assert checked == [grant.contract_hash, grant.contract_hash]
        saved = await SupplementalUpdatesRepository(session).get_source(
            "local_owner", source.feed_id, source.scope_key)
        assert "contract_hash" not in saved.payload


async def test_old_sqlite_backup_at_same_inode_cannot_erase_sent_high_water(campaign_sessions, tmp_path):
    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        _, grant, identity, lease, settings = await _setup(session, path, now)
    backup = tmp_path / "before-sent.sqlite"
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    inode = path.stat().st_ino
    async with factory.begin() as session:
        permit = await reserve_http_hop(session, identity=identity, response_byte_limit=4096,
                                        lease=lease, base_settings=settings, now=now)
        await mark_http_hop_sent(session, permit=permit, lease=lease,
                                 base_settings=settings, now=now)
    await factory.kw["bind"].dispose()
    # SQLite's backup API can replace contents while preserving the file inode.
    # The independent sent anchor is deliberately not part of this restore.
    with sqlite3.connect(backup) as source, sqlite3.connect(path) as destination:
        source.backup(destination)
    assert path.stat().st_ino == inode
    async with factory() as session:
        with pytest.raises(InvestorError, match="sent anchor"):
            await read_disclosure_campaign(session, grant.campaign_id)


async def test_sent_transaction_rollback_blocks_conservatively_without_http(campaign_sessions, monkeypatch):
    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        _, grant, identity, lease, settings = await _setup(session, path, now)
        permit = await reserve_http_hop(session, identity=identity, response_byte_limit=4096,
                                        lease=lease, base_settings=settings, now=now)
    append = SupplementalUpdatesRepository.append_change

    async def fail_native_sent(self, *args, **kwargs):
        if kwargs.get("change_key", "").startswith("p4:attempt:") and kwargs["change_key"].endswith(":sent"):
            raise RuntimeError("injected native sent commit failure")
        return await append(self, *args, **kwargs)

    monkeypatch.setattr(SupplementalUpdatesRepository, "append_change", fail_native_sent)
    with pytest.raises(RuntimeError, match="sent commit failure"):
        async with factory.begin() as session:
            await mark_http_hop_sent(session, permit=permit, lease=lease,
                                     base_settings=settings, now=now)
    async with factory() as session:
        with pytest.raises(InvestorError, match="sent anchor"):
            await read_disclosure_campaign(session, grant.campaign_id)


@pytest.mark.parametrize("was_sent", [False, True])
async def test_exact_orphan_ticket_settles_after_revoke_and_pause(campaign_sessions, was_sent):
    from investment_agent.supplemental_rules.disclosure_authorization import (
        recover_orphan_disclosure_hop,
    )
    from investment_agent.supplemental_rules.disclosure_campaign import (
        amend_disclosure_campaign,
        authorize_campaign_recovery,
    )

    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        _, grant, identity, old_lease, settings = await _setup(session, path, now)
        permit = await reserve_http_hop(session, identity=identity, response_byte_limit=4096,
                                        lease=old_lease, base_settings=settings, now=now)
        if was_sent:
            await mark_http_hop_sent(session, permit=permit, lease=old_lease,
                                     base_settings=settings, now=now)
    async with factory.begin() as session:
        original_run = await session.get(PipelineRun, old_lease.run_id)
        original_run.status = "partial"
        revoke = BackgroundDisclosureAuthorizationCommand(
            action="revoke", operation_id=uuid4(), authorization_id=grant.authorization_id,
            expected_revision=1, expected_owner_policy_revision=1, owner_daily_http=24,
            owner_daily_response_bytes=128 * 1024**2, owner_min_request_interval_seconds=10,
            reason="test revocation before recovery",
        )
        await apply_authorization_command(session, revoke, actor="api", now=now)
        campaign = await read_disclosure_campaign(session, grant.campaign_id)
        await amend_disclosure_campaign(session, campaign_id=grant.campaign_id,
                                         expected_revision=campaign.revision, patch={"status": "paused"},
                                         approval_ref="explicit-test-pause", now=now)
        recovery = PipelineRun(kind="supplemental_disclosure_poll", triggered_by="test", started_at=now,
                               status="running", stage="recovery", attempt_count=1,
                               worker_id="exact-recovery", request_json={})
        session.add(recovery)
        await session.flush()
        lease = RunLease(recovery.id, "exact-recovery", 1)
        assert await recover_orphan_disclosure_hop(session, lease=lease, now=now) is None
        with pytest.raises(InvestorError):
            await recover_orphan_disclosure_hop(session, lease=lease, poll_id=identity.poll_id, now=now)
        operation = uuid4()
        await authorize_campaign_recovery(
            session, campaign_id=grant.campaign_id, poll_id=identity.poll_id, action_id="p1",
            grant_id=grant.authorization_id, grant_revision=grant.revision,
            sample_env_id=grant.sample_env_id, operation_id=operation,
            recovery_approval_ref="explicit-test-accounting-only", now=now,
        )
        result = await recover_orphan_disclosure_hop(
            session, lease=lease, poll_id=identity.poll_id, recovery_operation_id=operation,
            sample_env_id=grant.sample_env_id, now=now,
        )
        assert result["state"] == ("unknown" if was_sent else "canceled")
        campaign = await read_disclosure_campaign(session, grant.campaign_id)
        assert campaign.status == "paused"
        assert campaign.http_reserved == 0 and campaign.http_consumed == int(was_sent)
        assert campaign.response_bytes_consumed == (4096 if was_sent else 0)
        assert await recover_orphan_disclosure_hop(
            session, lease=lease, poll_id=identity.poll_id, recovery_operation_id=operation,
            sample_env_id=grant.sample_env_id, now=now,
        ) is None


async def test_s0_and_sample_share_actual_day_storage_and_cooldown(campaign_sessions):
    from investment_agent.supplemental_rules.disclosure_authorization import STORAGE_SCOPE, _day
    from investment_agent.supplemental_rules.disclosure_contracts import QUOTA_FEED

    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        _, grant, identity, lease, settings = await _setup(session, path, now, raw_first=True)
    with pytest.raises(InvestorError, match="间隔"):
        async with factory.begin() as session:
            await reserve_http_hop(session, identity=identity, response_byte_limit=4096,
                                   lease=lease, base_settings=settings, now=now + timedelta(seconds=1))
    later = now + timedelta(seconds=11)
    async with factory.begin() as session:
        permit = await reserve_http_hop(session, identity=identity, response_byte_limit=4096,
                                        lease=lease, base_settings=settings, now=later)
        await mark_http_hop_sent(session, permit=permit, lease=lease, base_settings=settings, now=later)
        await settle_http_hop_result(session, permit=permit, lease=lease, response_bytes=256,
                                     storage_bytes=256, result_code="synthetic-known", now=later)
        repo = SupplementalUpdatesRepository(session)
        day = await repo.get_source("local_owner", QUOTA_FEED, f"owner-day:{_day(now)}")
        storage = await repo.get_source("local_owner", QUOTA_FEED, STORAGE_SCOPE)
        assert day.payload["request_consumed"] == 2
        assert day.payload["bytes_consumed"] == 128 + 256
        assert storage.payload["storage_consumed"] == 2 * 1024**2 + 128 + 256
        campaign = await read_disclosure_campaign(session, grant.campaign_id)
        assert campaign.http_consumed == 2 and campaign.response_bytes_consumed == 384
        assert campaign.s0_http_consumed == campaign.sample_http_consumed == 1


async def test_paused_campaign_requires_exact_current_manual_resume_ticket(campaign_sessions):
    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        _, grant, identity, _, _ = await _setup(session, path, now)
        campaign = await read_disclosure_campaign(session, grant.campaign_id)
        paused = await amend_disclosure_campaign(
            session, campaign_id=grant.campaign_id, expected_revision=campaign.revision,
            patch={"status": "paused"}, approval_ref="synthetic-pause", now=now,
        )
        scope = {
            "campaign_id": grant.campaign_id, "poll_id": identity.poll_id,
            "action_id": "p1", "grant_id": grant.authorization_id,
            "grant_revision": grant.revision, "grant_hash": grant.authorization_hash,
            "sample_env_id": grant.sample_env_id,
        }
        with pytest.raises(InvestorError):
            await resume_disclosure_campaign(
                session, **scope, operation_id=uuid4(), expected_revision=paused.revision,
                resume_approval_ref="review-resume", now=now,
            )
        operation_id = uuid4()
        ticket = await authorize_campaign_resume(
            session, **scope, resume_approval_ref="review-resume", operation_id=operation_id, now=now,
        )
        assert ticket["allow_network_resume"] is True
        assert await authorize_campaign_resume(
            session, **scope, resume_approval_ref="review-resume", operation_id=operation_id, now=now,
        ) == ticket
        with pytest.raises(InvestorError):
            await authorize_campaign_resume(
                session, **scope, resume_approval_ref="different-review", operation_id=operation_id, now=now,
            )
        with pytest.raises(InvestorError, match="campaign to be active"):
            await assert_campaign_resume_authority(
                session, **scope, operation_id=operation_id,
                resume_approval_ref="review-resume", now=now,
            )
        resumed = await resume_disclosure_campaign(
            session, **scope, operation_id=operation_id, expected_revision=paused.revision,
            resume_approval_ref="review-resume", now=now,
        )
        assert resumed.status == "active"
        authority = await assert_campaign_resume_authority(
            session, **scope, operation_id=operation_id,
            resume_approval_ref="review-resume", now=now,
        )
        assert authority["allow_network_resume"] is True
        with pytest.raises(InvestorError, match="no matching current manual resume authority"):
            await assert_campaign_resume_authority(
                session, **scope, operation_id=operation_id,
                resume_approval_ref="different-review", now=now,
            )
        assert resumed.valid_from == paused.valid_from and resumed.expires_at == paused.expires_at
        assert resumed.http_cap == paused.http_cap and resumed.sample_http_cap == paused.sample_http_cap
        assert resumed.poll_usage == paused.poll_usage and resumed.active_poll == paused.active_poll
        replay = await resume_disclosure_campaign(
            session, **scope, operation_id=operation_id, expected_revision=paused.revision,
            resume_approval_ref="review-resume", now=now,
        )
        assert replay == resumed
        # Replaying a completed transition remains idempotent even after the
        # exact poll root has been released.
        await complete_campaign_poll(session, campaign_id=grant.campaign_id,
                                     poll_id=identity.poll_id, action_id="p1", now=now)
        completed_replay = await resume_disclosure_campaign(
            session, **scope, operation_id=operation_id, expected_revision=paused.revision,
            resume_approval_ref="review-resume", now=now,
        )
        assert completed_replay.status == "active" and completed_replay.active_poll is None
        expired = grant.expires_at + timedelta(seconds=1)
        with pytest.raises(InvestorError):
            await authorize_campaign_resume(
                session, **scope, resume_approval_ref="expired-review", operation_id=uuid4(), now=expired,
            )


async def test_resume_rejects_unresolved_original_poll_accounting(campaign_sessions):
    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        _, grant, identity, lease, settings = await _setup(session, path, now)
        permit = await reserve_http_hop(session, identity=identity, response_byte_limit=4096,
                                        lease=lease, base_settings=settings, now=now)
        campaign = await read_disclosure_campaign(session, grant.campaign_id)
        paused = await amend_disclosure_campaign(
            session, campaign_id=grant.campaign_id, expected_revision=campaign.revision,
            patch={"status": "paused"}, approval_ref="synthetic-pause", now=now,
        )
        with pytest.raises(InvestorError, match="unresolved permits"):
            await authorize_campaign_resume(
                session, campaign_id=grant.campaign_id, poll_id=identity.poll_id,
                action_id="p1", grant_id=grant.authorization_id,
                grant_revision=grant.revision, grant_hash=grant.authorization_hash,
                sample_env_id=grant.sample_env_id, resume_approval_ref="review-resume",
                operation_id=uuid4(), now=now,
            )
        assert permit.state == "reserved" and paused.status == "paused"


async def test_resume_rejects_exhausted_campaign_budget(campaign_sessions):
    factory, path = campaign_sessions
    now = datetime.now(UTC)
    async with factory.begin() as session:
        _, grant, identity, *_ = await _setup(session, path, now)
        campaign = await read_disclosure_campaign(session, grant.campaign_id)
        campaign = await amend_disclosure_campaign(
            session, campaign_id=grant.campaign_id, expected_revision=campaign.revision,
            patch={"sample_http_cap": 0}, approval_ref="lower-cumulative-budget", now=now,
        )
        paused = await amend_disclosure_campaign(
            session, campaign_id=grant.campaign_id, expected_revision=campaign.revision,
            patch={"status": "paused"}, approval_ref="synthetic-pause", now=now,
        )
        with pytest.raises(InvestorError, match="no remaining campaign"):
            await authorize_campaign_resume(
                session, campaign_id=grant.campaign_id, poll_id=identity.poll_id,
                action_id="p1", grant_id=grant.authorization_id,
                grant_revision=grant.revision, grant_hash=grant.authorization_hash,
                sample_env_id=grant.sample_env_id, resume_approval_ref="review-resume",
                operation_id=uuid4(), now=now,
            )
        assert paused.status == "paused"
