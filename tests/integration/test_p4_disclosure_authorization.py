from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.supplemental_updates import SupplementalChangeReceiptRow
from investment_agent.supplemental_rules.disclosure_authorization import (
    apply_authorization_command,
    get_current_authorization,
    read_authorization_snapshot,
)
from investment_agent.supplemental_rules.disclosure_contracts import (
    BackgroundDisclosureAuthorizationBody,
    BackgroundDisclosureAuthorizationCommand,
)


def _body(now: datetime, refs=None) -> BackgroundDisclosureAuthorizationBody:
    return BackgroundDisclosureAuthorizationBody(
        contract_hash="a" * 64,
        listing_refs=tuple(refs or (ObjectRef(kind="listing", id=1, revision=1),)),
        doc_type_allowlist=("annual_report_full",),
        history_start=now - timedelta(days=4),
        history_end=now,
        min_poll_interval_seconds=21600,
        min_http_interval_seconds=5,
        expires_at=now + timedelta(days=4),
        max_staleness_seconds=86400,
        per_poll_query_http=2,
        per_poll_fetch=1,
        per_poll_http=4,
        per_poll_response_bytes=1024 * 1024,
        grant_daily_http=8,
        grant_daily_response_bytes=8 * 1024 * 1024,
        storage_bytes_cap=64 * 1024 * 1024,
    )



async def _seed_listings(session, count: int = 1):
    from investment_agent.storage.models.reference import Company, Listing, Market

    market = await session.get(Market, "SZSE")
    if market is None:
        market = Market(code="SZSE", name="Shenzhen fixture", timezone="Asia/Shanghai", currency="CNY")
        session.add(market)
        await session.flush()
    refs = []
    for index in range(count):
        company = Company(canonical_name=f"Authorization test issuer {uuid4()}")
        session.add(company)
        await session.flush()
        listing = Listing(company_id=company.id, market_code="SZSE", ticker=f"{300001 + index:06d}")
        session.add(listing)
        await session.flush()
        refs.append(ObjectRef(kind="listing", id=listing.id, revision=1))
    return tuple(refs)


def _command(now: datetime, *, authorization_id=None, policy_revision=0, action="create", expected_revision=0, body=None):
    return BackgroundDisclosureAuthorizationCommand(
        action=action,
        operation_id=uuid4(),
        authorization_id=authorization_id or uuid4(),
        expected_revision=expected_revision,
        owner_daily_http=24,
        owner_daily_response_bytes=128 * 1024 * 1024,
        owner_min_request_interval_seconds=5,
        expected_owner_policy_revision=policy_revision,
        body=body or _body(now),
    )


@pytest.mark.asyncio
async def test_authorization_command_receipt_is_authoritative_and_policy_is_shared(db_session):
    now = datetime(2026, 10, 17, 3, tzinfo=UTC)
    refs = await _seed_listings(db_session)
    command = _command(now, body=_body(now, refs))
    created = await apply_authorization_command(db_session, command, actor="api", now=now)
    await db_session.flush()
    snapshot = await read_authorization_snapshot(db_session, command.authorization_id)
    assert snapshot.authorization_hash == created.authorization.authorization_hash
    assert await get_current_authorization(db_session, command.authorization_id, now=now) == snapshot

    second = _command(now, policy_revision=1)
    second_result = await apply_authorization_command(db_session, second, actor="web", now=now)
    assert second_result.authorization.authorization_id == second.authorization_id
    assert second_result.authorization.authorization_hash != snapshot.authorization_hash

    # Same owner-source policy persists across grant identities and cannot be
    # silently widened or reset by creating a new grant.
    widen = second.model_copy(update={"authorization_id": uuid4(), "operation_id": uuid4(),
                                      "owner_daily_http": 25, "expected_owner_policy_revision": 1})
    with pytest.raises(InvestorError) as rejected:
        await apply_authorization_command(db_session, widen, actor="api", now=now)
    assert rejected.value.detail.code == "idempotency_conflict"

    receipts = await db_session.scalar(select(func.count()).select_from(SupplementalChangeReceiptRow))
    assert receipts == 2


@pytest.mark.asyncio
async def test_grant_projection_tamper_is_detected_by_receipt_binding(db_session):
    now = datetime(2026, 10, 17, 3, tzinfo=UTC)
    refs = await _seed_listings(db_session)
    command = _command(now, body=_body(now, refs))
    await apply_authorization_command(db_session, command, actor="api", now=now)
    from investment_agent.storage.models.supplemental_updates import SupplementalSourceStateRow

    row = (await db_session.execute(select(SupplementalSourceStateRow).where(
        SupplementalSourceStateRow.scope_key == f"grant:{command.authorization_id}"
    ))).scalar_one()
    payload = dict(row.payload)
    payload["authorization"] = {**payload["authorization"], "actor": "tampered"}
    row.payload = payload
    with pytest.raises(InvestorError) as rejected:
        await read_authorization_snapshot(db_session, command.authorization_id)
    assert rejected.value.detail.code == "invalid_reference"


@pytest.mark.asyncio
async def test_active_grants_share_three_listing_owner_source_allowlist(db_session):
    now = datetime(2026, 10, 17, 3, tzinfo=UTC)
    refs = await _seed_listings(db_session, 4)
    first_body = _body(now, refs[:3])
    first = _command(now, body=first_body)
    await apply_authorization_command(db_session, first, actor="api", now=now)
    second = _command(now, policy_revision=1, body=_body(now, refs[3:]))
    with pytest.raises(InvestorError) as rejected:
        await apply_authorization_command(db_session, second, actor="api", now=now)
    assert rejected.value.detail.code == "capacity_blocked"


@pytest.mark.asyncio
async def test_active_grant_count_rejects_ninth_same_listing_grant(db_session):
    now = datetime(2026, 10, 17, 3, tzinfo=UTC)
    refs = await _seed_listings(db_session, 1)
    for index in range(8):
        command = _command(now, body=_body(now, refs))
        command = command.model_copy(update={
            "operation_id": uuid4(), "authorization_id": uuid4(),
            "expected_revision": 0,
            "update_owner_policy": index == 0,
            "expected_owner_policy_revision": 0 if index == 0 else 1,
        })
        await apply_authorization_command(db_session, command, actor="api", now=now)
    ninth = _command(now, policy_revision=1, body=_body(now, refs))
    with pytest.raises(InvestorError) as rejected:
        await apply_authorization_command(db_session, ninth, actor="api", now=now)
    assert rejected.value.detail.code == "capacity_blocked"


@pytest.mark.asyncio
async def test_quota_projection_requires_exact_authority_receipt_and_ref(db_session):
    from investment_agent.storage.models.supplemental_updates import SupplementalSourceStateRow
    from investment_agent.supplemental_rules.disclosure_authorization import (
        _quota_state,
        freeze_poll_quota,
    )

    now = datetime(2026, 10, 17, 3, tzinfo=UTC)
    refs = await _seed_listings(db_session)
    grant = (await apply_authorization_command(
        db_session, _command(now, body=_body(now, refs)), actor="api", now=now
    )).authorization
    await freeze_poll_quota(
        db_session, poll_id="authority-check", authorization=grant,
        listing_ref=grant.listing_refs[0], window_start=grant.history_start,
        window_end=grant.history_end, poll_http_cap=4, poll_query_http_cap=2,
        poll_fetch_cap=1, poll_response_bytes_cap=1024 * 1024, now=now,
    )
    scope = "poll:authority-check"
    row = (await db_session.execute(select(SupplementalSourceStateRow).where(
        SupplementalSourceStateRow.feed_id == "cninfo_szse_quota_v1",
        SupplementalSourceStateRow.scope_key == scope,
    ))).scalar_one()
    original = dict(row.payload)
    tampered = dict(original)
    tampered["request_consumed"] = 1
    row.payload = tampered
    with pytest.raises(InvestorError) as mismatch:
        await _quota_state(db_session, scope, day=None)
    assert mismatch.value.detail.code == "invalid_reference"

    row.payload = {**original, "quota_authority_ref": None}
    with pytest.raises(InvestorError) as missing:
        await _quota_state(db_session, scope, day=None)
    assert missing.value.detail.code == "invalid_reference"


@pytest.mark.asyncio
async def test_http_attempt_uses_global_permit_and_sent_permit_can_settle(db_session):
    from investment_agent.config import Settings
    from investment_agent.evidence.disclosure_http import DIRECTORY
    from investment_agent.storage.models.analysis import PipelineRun
    from investment_agent.storage.models.supplemental_updates import SupplementalSourceStateRow
    from investment_agent.storage.repositories.pipeline_run import RunLease
    from investment_agent.supplemental_rules.disclosure_authorization import (
        freeze_poll_quota,
        mark_http_hop_sent,
        reserve_http_hop,
        settle_http_hop_result,
    )
    from investment_agent.supplemental_rules.disclosure_contracts import (
        DisclosureAttemptIdentity,
        authorization_scope_hash,
    )

    now = datetime(2026, 10, 17, 3, tzinfo=UTC)
    refs = await _seed_listings(db_session)
    command = _command(now, body=_body(now, refs))
    grant = (await apply_authorization_command(db_session, command, actor="api", now=now)).authorization
    listing = grant.listing_refs[0]
    poll_id = "poll-fixed"
    scope = authorization_scope_hash(grant.authorization_id, grant.revision,
                                     grant.authorization_hash, grant.contract_hash,
                                     listing, grant.history_start, grant.history_end)
    await freeze_poll_quota(db_session, poll_id=poll_id, authorization=grant,
                            listing_ref=listing, window_start=grant.history_start,
                            window_end=grant.history_end, poll_http_cap=4,
                            poll_query_http_cap=2, poll_fetch_cap=1,
                            poll_response_bytes_cap=1024 * 1024, now=now)
    run = PipelineRun(kind="supplemental_update", triggered_by="test", started_at=now,
                      status="running", stage="disclosure", attempt_count=1,
                      worker_id="worker-test", request_json={})
    db_session.add(run)
    await db_session.flush()
    lease = RunLease(run_id=run.id, worker_id="worker-test", attempt=1)
    settings = Settings(api_key="test", supplemental_rules_enabled=True,
                       supplemental_updates_enabled=True,
                       supplemental_disclosure_polling_enabled=True,
                       disclosure_originals_enabled=True)

    def identity(attempt_id):
        return DisclosureAttemptIdentity(
            poll_id=poll_id, logical_request_key="directory:1", attempt_id=attempt_id,
            attempt_ordinal=1, hop_ordinal=0, hop_id=f"hop:{attempt_id}",
            request_kind="directory", request_hash="e" * 64,
            authorization_id=grant.authorization_id, authorization_revision=grant.revision,
            authorization_hash=grant.authorization_hash, contract_hash=grant.contract_hash,
            scope_hash=scope, listing_ref=listing, window_start=grant.history_start,
            window_end=grant.history_end, poll_http_cap=4, poll_query_http_cap=2,
            poll_fetch_cap=1, poll_response_bytes_cap=1024 * 1024,
            url=DIRECTORY, response_bytes_limit=1024,
        )

    first = identity(uuid4())
    permit = await reserve_http_hop(db_session, identity=first, response_byte_limit=1024,
                                    lease=lease, base_settings=settings, now=now)
    replay = await reserve_http_hop(db_session, identity=first, response_byte_limit=1024,
                                    lease=lease, base_settings=settings, now=now)
    assert replay.permit_id == permit.permit_id
    with pytest.raises(InvestorError) as busy:
        await reserve_http_hop(db_session, identity=identity(uuid4()), response_byte_limit=1024,
                               lease=lease, base_settings=settings, now=now + timedelta(seconds=6))
    assert busy.value.detail.code == "source_cooldown"
    sent_permit = await mark_http_hop_sent(db_session, permit=permit, lease=lease,
                                           base_settings=settings, now=now)
    with pytest.raises(InvestorError) as oversized:
        await settle_http_hop_result(db_session, permit=sent_permit, lease=lease,
                                     response_bytes=1025, result_code="oversized", now=now)
    assert oversized.value.detail.code == "capacity_blocked"
    with pytest.raises(InvestorError) as no_replay:
        await reserve_http_hop(db_session, identity=first, response_byte_limit=1024,
                               lease=lease, base_settings=settings, now=now)
    assert no_replay.value.detail.code == "idempotency_conflict"
    settled = await settle_http_hop_result(db_session, permit=sent_permit, lease=lease,
                                           response_bytes=128, result_code="ok",
                                           storage_bytes=0, now=now)
    assert settled.response_bytes_consumed == 128
    with pytest.raises(InvestorError) as cross_terminal_replay:
        await settle_http_hop_result(
            db_session, permit=sent_permit, lease=lease, response_bytes=None,
            result_code="late_unknown", now=now + timedelta(seconds=1),
        )
    assert cross_terminal_replay.value.detail.code == "idempotency_conflict", cross_terminal_replay.value.detail.message
    poll_state = (await db_session.execute(select(SupplementalSourceStateRow).where(
        SupplementalSourceStateRow.feed_id == "cninfo_szse_quota_v1",
        SupplementalSourceStateRow.scope_key == "poll:poll-fixed",
    ))).scalar_one()
    assert poll_state.payload["query_reserved"] == 0
    assert poll_state.payload["query_consumed"] == 0
    assert poll_state.payload["request_consumed"] == 1
    storage_state = (await db_session.execute(select(SupplementalSourceStateRow).where(
        SupplementalSourceStateRow.feed_id == "cninfo_szse_quota_v1",
        SupplementalSourceStateRow.scope_key == "owner-storage:cninfo_szse_v1",
    ))).scalar_one()
    storage_payload = dict(storage_state.payload)
    storage_payload["storage_consumed"] = 512 * 1024 * 1024
    storage_state.payload = storage_payload
    with pytest.raises(InvestorError) as full:
        await reserve_http_hop(db_session, identity=identity(uuid4()), response_byte_limit=1024,
                               lease=lease, base_settings=settings, now=now + timedelta(seconds=6))
    assert full.value.detail.code == "invalid_reference"


@pytest.mark.asyncio
async def test_cross_shanghai_day_rollover_and_unknown_keeps_reservation_accounted(db_session):
    from investment_agent.config import Settings
    from investment_agent.evidence.disclosure_http import DIRECTORY
    from investment_agent.storage.models.analysis import PipelineRun
    from investment_agent.storage.models.supplemental_updates import SupplementalSourceStateRow
    from investment_agent.storage.repositories.pipeline_run import RunLease
    from investment_agent.supplemental_rules.disclosure_authorization import (
        freeze_poll_quota,
        mark_http_hop_sent,
        reserve_http_hop,
        rollover_reserved_http_hop,
        settle_http_hop_result,
    )
    from investment_agent.supplemental_rules.disclosure_contracts import (
        DisclosureAttemptIdentity,
        authorization_scope_hash,
    )

    # UTC day is still Oct 17, while the second call crosses Shanghai midnight.
    before = datetime(2026, 10, 17, 15, 59, 50, tzinfo=UTC)
    after = datetime(2026, 10, 17, 16, 0, 5, tzinfo=UTC)
    refs = await _seed_listings(db_session)
    grant = (await apply_authorization_command(db_session, _command(before, body=_body(before, refs)), actor="api", now=before)).authorization
    listing = grant.listing_refs[0]
    poll_id = "rollover-poll"
    scope = authorization_scope_hash(grant.authorization_id, grant.revision,
                                     grant.authorization_hash, grant.contract_hash,
                                     listing, grant.history_start, grant.history_end)
    await freeze_poll_quota(db_session, poll_id=poll_id, authorization=grant,
                            listing_ref=listing, window_start=grant.history_start,
                            window_end=grant.history_end, poll_http_cap=4,
                            poll_query_http_cap=2, poll_fetch_cap=1,
                            poll_response_bytes_cap=1024 * 1024, now=before)
    run = PipelineRun(kind="supplemental_update", triggered_by="test", started_at=before,
                      status="running", stage="disclosure", attempt_count=1,
                      worker_id="worker-rollover", request_json={})
    db_session.add(run)
    await db_session.flush()
    lease = RunLease(run_id=run.id, worker_id="worker-rollover", attempt=1)
    settings = Settings(api_key="test", supplemental_rules_enabled=True,
                       supplemental_updates_enabled=True,
                       supplemental_disclosure_polling_enabled=True,
                       disclosure_originals_enabled=True)

    def identity(attempt_id, ordinal):
        return DisclosureAttemptIdentity(
            poll_id=poll_id, logical_request_key="directory:rollover", attempt_id=attempt_id,
            attempt_ordinal=ordinal, hop_ordinal=0, hop_id=f"hop:{ordinal}",
            request_kind="directory", request_hash="f" * 64,
            authorization_id=grant.authorization_id, authorization_revision=grant.revision,
            authorization_hash=grant.authorization_hash, contract_hash=grant.contract_hash,
            scope_hash=scope, listing_ref=listing, window_start=grant.history_start,
            window_end=grant.history_end, poll_http_cap=4, poll_query_http_cap=2,
            poll_fetch_cap=1, poll_response_bytes_cap=1024 * 1024,
            url=DIRECTORY, response_bytes_limit=1024,
        )

    first_id, next_id = uuid4(), uuid4()
    first = await reserve_http_hop(db_session, identity=identity(first_id, 1),
                                   response_byte_limit=1024, lease=lease,
                                   base_settings=settings, now=before)
    # Pipeline recovery fences the old unsent lease. It may cancel/refund the
    # reservation and migrate it across Shanghai midnight, never send it.
    run.worker_id = "worker-rollover-recovery"
    run.attempt_count = 2
    await db_session.flush()
    recovery_lease = RunLease(run_id=run.id, worker_id="worker-rollover-recovery", attempt=2)
    next_permit = await rollover_reserved_http_hop(
        db_session, old_permit=first, next_identity=identity(next_id, 2), lease=recovery_lease,
        base_settings=settings, now=after,
    )
    assert next_permit.shanghai_day == "2026-10-18"
    sent = await mark_http_hop_sent(db_session, permit=next_permit, lease=recovery_lease,
                                    base_settings=settings, now=after)
    # A second worker takeover resolves a sent-but-unobserved outcome as
    # unknown and consumes the full reserved response/storage budget.
    run.worker_id = "worker-sent-recovery"
    run.attempt_count = 3
    await db_session.flush()
    sent_recovery = RunLease(run_id=run.id, worker_id="worker-sent-recovery", attempt=3)
    unknown = await settle_http_hop_result(db_session, permit=sent, lease=sent_recovery,
                                           response_bytes=None, result_code="transport_unknown",
                                           now=after + timedelta(seconds=1))
    assert unknown.state == "unknown" and unknown.response_bytes_consumed == 1024
    assert unknown.storage_bytes_consumed == 1024 * 1024
    with pytest.raises(InvestorError) as late_result:
        await settle_http_hop_result(
            db_session, permit=sent, lease=sent_recovery, response_bytes=10,
            result_code="late_response", now=after + timedelta(seconds=2),
        )
    assert late_result.value.detail.code == "idempotency_conflict"
    rows = (await db_session.execute(select(SupplementalSourceStateRow).where(
        SupplementalSourceStateRow.feed_id == "cninfo_szse_quota_v1",
        SupplementalSourceStateRow.scope_key.in_(
            ("owner-day:2026-10-17", "owner-day:2026-10-18")
        ),
    ))).scalars().all()
    by_day = {row.payload["day"]: row.payload for row in rows}
    assert by_day["2026-10-17"]["request_consumed"] == 0
    assert by_day["2026-10-18"]["request_consumed"] == 1
    assert by_day["2026-10-18"]["bytes_consumed"] == 1024


@pytest.mark.asyncio
async def test_authorization_requires_existing_szse_listing_and_exact_ticker(db_session):
    from investment_agent.storage.models.reference import Company, Listing, Market

    now = datetime(2026, 10, 17, 3, tzinfo=UTC)
    refs = await _seed_listings(db_session)
    missing = _command(now, body=_body(now, (ObjectRef(kind="listing", id=999999, revision=1),)))
    with pytest.raises(InvestorError) as rejected:
        await apply_authorization_command(db_session, missing, actor="api", now=now)
    assert rejected.value.detail.code == "invalid_reference"

    db_session.add(Market(code="SSE", name="Shanghai fixture", timezone="Asia/Shanghai", currency="CNY"))
    company = Company(canonical_name="Non-SZSE authorization fixture")
    db_session.add(company)
    await db_session.flush()
    listing = Listing(company_id=company.id, market_code="SSE", ticker="600001")
    db_session.add(listing)
    await db_session.flush()
    wrong_market = _command(now, body=_body(now, (ObjectRef(kind="listing", id=listing.id, revision=1),)))
    with pytest.raises(InvestorError) as rejected_market:
        await apply_authorization_command(db_session, wrong_market, actor="api", now=now)
    assert rejected_market.value.detail.code == "invalid_reference"
    assert len(refs) == 1


@pytest.mark.asyncio
async def test_revocation_before_sent_blocks_but_sent_attempt_settles_consumed(db_session):
    from investment_agent.config import Settings
    from investment_agent.evidence.disclosure_http import DIRECTORY
    from investment_agent.storage.models.analysis import PipelineRun
    from investment_agent.storage.models.supplemental_updates import SupplementalSourceStateRow
    from investment_agent.storage.repositories.pipeline_run import RunLease
    from investment_agent.supplemental_rules.disclosure_authorization import (
        cancel_unsent_http_hop,
        freeze_poll_quota,
        mark_http_hop_sent,
        reserve_http_hop,
        settle_http_hop_result,
    )
    from investment_agent.supplemental_rules.disclosure_contracts import (
        BackgroundDisclosureAuthorizationCommand,
        DisclosureAttemptIdentity,
        authorization_scope_hash,
    )

    now = datetime(2026, 10, 17, 3, tzinfo=UTC)
    refs = await _seed_listings(db_session)
    first_cmd = _command(now, body=_body(now, refs))
    first_grant = (await apply_authorization_command(db_session, first_cmd, actor="api", now=now)).authorization
    run = PipelineRun(kind="supplemental_update", triggered_by="test", started_at=now,
                      status="running", stage="disclosure", attempt_count=1,
                      worker_id="worker-revoke", request_json={})
    db_session.add(run)
    await db_session.flush()
    lease = RunLease(run_id=run.id, worker_id="worker-revoke", attempt=1)
    settings = Settings(api_key="test", supplemental_rules_enabled=True,
                       supplemental_updates_enabled=True,
                       supplemental_disclosure_polling_enabled=True,
                       disclosure_originals_enabled=True)

    async def admitted(grant, poll_id):
        listing = grant.listing_refs[0]
        scope = authorization_scope_hash(grant.authorization_id, grant.revision,
                                         grant.authorization_hash, grant.contract_hash,
                                         listing, grant.history_start, grant.history_end)
        await freeze_poll_quota(db_session, poll_id=poll_id, authorization=grant,
                                listing_ref=listing, window_start=grant.history_start,
                                window_end=grant.history_end, poll_http_cap=4,
                                poll_query_http_cap=2, poll_fetch_cap=1,
                                poll_response_bytes_cap=1024 * 1024, now=now)
        return DisclosureAttemptIdentity(
            poll_id=poll_id, logical_request_key=f"directory:{poll_id}", attempt_id=uuid4(),
            attempt_ordinal=1, hop_ordinal=0, hop_id=f"hop:{poll_id}",
            request_kind="directory", request_hash="1" * 64,
            authorization_id=grant.authorization_id, authorization_revision=grant.revision,
            authorization_hash=grant.authorization_hash, contract_hash=grant.contract_hash,
            scope_hash=scope, listing_ref=listing, window_start=grant.history_start,
            window_end=grant.history_end, poll_http_cap=4, poll_query_http_cap=2,
            poll_fetch_cap=1, poll_response_bytes_cap=1024 * 1024,
            url=DIRECTORY, response_bytes_limit=1024,
        )

    first_identity = await admitted(first_grant, "before-revoke")
    first_permit = await reserve_http_hop(db_session, identity=first_identity,
                                          response_byte_limit=1024, lease=lease,
                                          base_settings=settings, now=now)
    revoke_first = BackgroundDisclosureAuthorizationCommand(
        action="revoke", operation_id=uuid4(), authorization_id=first_grant.authorization_id,
        expected_revision=1, owner_daily_http=24,
        owner_daily_response_bytes=128 * 1024 * 1024,
        owner_min_request_interval_seconds=5, expected_owner_policy_revision=1,
        reason="test revoke before send",
    )
    await apply_authorization_command(db_session, revoke_first, actor="api", now=now)
    with pytest.raises(InvestorError) as blocked:
        await mark_http_hop_sent(db_session, permit=first_permit, lease=lease,
                                 base_settings=settings, now=now)
    assert blocked.value.detail.code == "disclosure_authorization_required"
    assert await cancel_unsent_http_hop(db_session, permit=first_permit, lease=lease, now=now)

    second_cmd = _command(now, policy_revision=1, body=_body(now, refs))
    second_grant = (await apply_authorization_command(db_session, second_cmd, actor="api", now=now)).authorization
    second_identity = await admitted(second_grant, "after-revoke-sent")
    second_permit = await reserve_http_hop(db_session, identity=second_identity,
                                           response_byte_limit=1024, lease=lease,
                                           base_settings=settings, now=now)
    sent = await mark_http_hop_sent(db_session, permit=second_permit, lease=lease,
                                    base_settings=settings, now=now)
    revoke_second = BackgroundDisclosureAuthorizationCommand(
        action="revoke", operation_id=uuid4(), authorization_id=second_grant.authorization_id,
        expected_revision=1, owner_daily_http=24,
        owner_daily_response_bytes=128 * 1024 * 1024,
        owner_min_request_interval_seconds=5, expected_owner_policy_revision=1,
        reason="test revoke after send",
    )
    await apply_authorization_command(db_session, revoke_second, actor="api", now=now)
    result = await settle_http_hop_result(db_session, permit=sent, lease=lease,
                                          response_bytes=256, result_code="ok",
                                          now=now + timedelta(seconds=1))
    assert result.request_consumed == 1
    owner_day = (await db_session.execute(select(SupplementalSourceStateRow).where(
        SupplementalSourceStateRow.scope_key == "owner-day:2026-10-17",
        SupplementalSourceStateRow.feed_id == "cninfo_szse_quota_v1",
    ))).scalar_one()
    assert owner_day.payload["request_consumed"] == 1
    assert owner_day.payload["bytes_consumed"] == 256

    # A reservation made under a looser owner policy cannot be sent after a
    # separate explicit policy-CAS tightens the shared cap.
    third_cmd = _command(now, policy_revision=1, body=_body(now, refs).model_copy(
        update={"grant_daily_http": 2}
    ))
    third_grant = (await apply_authorization_command(
        db_session, third_cmd, actor="api", now=now
    )).authorization
    third_identity = await admitted(third_grant, "owner-cap-race")
    third_permit = await reserve_http_hop(
        db_session, identity=third_identity, response_byte_limit=1024,
        lease=lease, base_settings=settings, now=now + timedelta(seconds=6),
    )
    tight_body = _body(now + timedelta(seconds=6), refs).model_copy(
        update={"grant_daily_http": 1}
    )
    tighten = BackgroundDisclosureAuthorizationCommand(
        action="create", operation_id=uuid4(), authorization_id=uuid4(),
        expected_revision=0, owner_daily_http=1,
        owner_daily_response_bytes=128 * 1024 * 1024,
        owner_min_request_interval_seconds=5, expected_owner_policy_revision=1,
        update_owner_policy=True, body=tight_body,
    )
    await apply_authorization_command(db_session, tighten, actor="api", now=now + timedelta(seconds=6))
    with pytest.raises(InvestorError) as tightened:
        await mark_http_hop_sent(db_session, permit=third_permit, lease=lease,
                                 base_settings=settings, now=now + timedelta(seconds=6))
    assert tightened.value.detail.code == "disclosure_scope_forbidden"
    assert await cancel_unsent_http_hop(
        db_session, permit=third_permit, lease=lease, now=now + timedelta(seconds=6)
    )


@pytest.mark.asyncio
async def test_file_sqlite_serializes_competing_source_reservations(tmp_path):
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from investment_agent.config import Settings
    from investment_agent.evidence.disclosure_http import DIRECTORY
    from investment_agent.storage.models import Base
    from investment_agent.storage.models.analysis import PipelineRun
    from investment_agent.storage.repositories.pipeline_run import RunLease
    from investment_agent.supplemental_rules.disclosure_authorization import (
        apply_authorization_command,
        freeze_poll_quota,
        reserve_http_hop,
    )
    from investment_agent.supplemental_rules.disclosure_contracts import (
        BackgroundDisclosureAuthorizationV1,
        DisclosureAttemptIdentity,
        authorization_scope_hash,
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'p4-disclosure-race.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 10, 17, 3, tzinfo=UTC)
    settings = Settings(api_key="test", supplemental_rules_enabled=True,
                       supplemental_updates_enabled=True,
                       supplemental_disclosure_polling_enabled=True,
                       disclosure_originals_enabled=True)
    async with sessions() as session, session.begin():
        refs = await _seed_listings(session)
        grant = (await apply_authorization_command(
            session, _command(now, body=_body(now, refs)), actor="api", now=now
        )).authorization
        listing = grant.listing_refs[0]
        scope = authorization_scope_hash(grant.authorization_id, grant.revision,
                                         grant.authorization_hash, grant.contract_hash,
                                         listing, grant.history_start, grant.history_end)
        await freeze_poll_quota(session, poll_id="file-race", authorization=grant,
                                listing_ref=listing, window_start=grant.history_start,
                                window_end=grant.history_end, poll_http_cap=4,
                                poll_query_http_cap=2, poll_fetch_cap=1,
                                poll_response_bytes_cap=1024 * 1024, now=now)
        run = PipelineRun(kind="supplemental_update", triggered_by="test", started_at=now,
                          status="running", stage="disclosure", attempt_count=1,
                          worker_id="worker-race", request_json={})
        session.add(run)
        await session.flush()
        lease = RunLease(run_id=run.id, worker_id="worker-race", attempt=1)
        grant_data = grant.model_dump(mode="python")
        listing_data = listing.model_dump(mode="python")

    def make_identity(attempt_id):
        from investment_agent.investor.common import ObjectRef
        local_grant = BackgroundDisclosureAuthorizationV1.model_validate(grant_data)
        local_listing = ObjectRef.model_validate(listing_data)
        return DisclosureAttemptIdentity(
            poll_id="file-race", logical_request_key="directory:file-race", attempt_id=attempt_id,
            attempt_ordinal=1, hop_ordinal=0, hop_id=f"hop:{attempt_id}",
            request_kind="directory", request_hash="2" * 64,
            authorization_id=local_grant.authorization_id, authorization_revision=local_grant.revision,
            authorization_hash=local_grant.authorization_hash, contract_hash=local_grant.contract_hash,
            scope_hash=scope, listing_ref=local_listing, window_start=local_grant.history_start,
            window_end=local_grant.history_end, poll_http_cap=4, poll_query_http_cap=2,
            poll_fetch_cap=1, poll_response_bytes_cap=1024 * 1024,
            url=DIRECTORY, response_bytes_limit=1024,
        )

    async def attempt(delay: float):
        async with sessions() as session:
            try:
                async with session.begin():
                    result = await reserve_http_hop(
                        session, identity=make_identity(uuid4()), response_byte_limit=1024,
                        lease=lease, base_settings=settings, now=now,
                    )
                    await asyncio.sleep(delay)
                return result
            except InvestorError as exc:
                return exc.detail.code

    results = await asyncio.gather(attempt(0.15), attempt(0))
    assert sum(not isinstance(result, str) for result in results) == 1
    assert "source_cooldown" in results
    await engine.dispose()
