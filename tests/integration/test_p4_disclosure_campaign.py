from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.supplemental_rules.disclosure_campaign import (
    amend_disclosure_campaign,
    assert_campaign_recovery_authority,
    authorize_campaign_recovery,
    authorize_disclosure_campaign_poll,
    bind_disclosure_campaign_grant,
    cancel_campaign_hop,
    create_disclosure_campaign,
    mark_campaign_hop_sent,
    read_disclosure_campaign,
    reserve_campaign_action_hop,
    reserve_campaign_hop,
    settle_campaign_hop,
)
from investment_agent.supplemental_rules.disclosure_campaign_contracts import (
    BackgroundDisclosureAuthorizationV2,
    CampaignAction,
    DisclosureCampaignV2,
)
from investment_agent.supplemental_rules.disclosure_contracts import DisclosureAttemptIdentity


def _campaign(db_session, now: datetime, actions: tuple[CampaignAction, ...]) -> DisclosureCampaignV2:
    return DisclosureCampaignV2(
        campaign_id=uuid4(), revision=1, approval_ref="campaign-approval-1",
        sample_env_id="sample-env-1", database_path=f"sqlite-memory:{id(db_session.bind.sync_engine)}",
        valid_from=now - timedelta(minutes=1), expires_at=now + timedelta(days=3),
        http_cap=32, response_bytes_cap=192 * 1024 * 1024, storage_bytes_cap=512 * 1024 * 1024,
        s0_http_cap=8, s0_response_bytes_cap=40 * 1024 * 1024,
        s0_storage_bytes_cap=128 * 1024 * 1024, sample_http_cap=24,
        sample_response_bytes_cap=152 * 1024 * 1024, actions=actions,
    )


@pytest.mark.asyncio
async def test_s0_unknown_settlement_charges_full_reserved_bytes_and_cannot_replay(db_session):
    now = datetime(2026, 10, 17, 2, tzinfo=UTC)
    terms_url = "https://www.cninfo.com.cn/terms"
    action = CampaignAction(
        action_id="terms-1", kind="terms", status="approved", url_allowlist=(terms_url,),
        method="GET", http_cap=1, response_byte_cap=1024 * 1024,
        storage_byte_cap=2 * 1024 * 1024,
    )
    campaign = await create_disclosure_campaign(db_session, _campaign(db_session, now, (action,)), now=now)
    permit = await reserve_campaign_action_hop(
        db_session, campaign_id=campaign.campaign_id, action_id="terms-1", operation_key="terms-once",
        sample_env_id="sample-env-1", url=terms_url, request_bytes=0,
        response_bytes_limit=1024 * 1024, storage_bytes_limit=2 * 1024 * 1024,
        purpose="terms", now=now,
    )
    permit_id = UUID(permit["permit_id"])
    await mark_campaign_hop_sent(db_session, permit_id=permit_id, now=now)
    with pytest.raises(InvestorError):
        await mark_campaign_hop_sent(db_session, permit_id=permit_id, now=now)
    result = await settle_campaign_hop(
        db_session, permit_id=permit_id, response_bytes=None, storage_bytes=0,
        result_code="timeout_unknown", now=now,
    )
    assert result["state"] == "unknown"
    saved = await read_disclosure_campaign(db_session, campaign.campaign_id)
    assert saved.http_consumed == 1
    assert saved.http_reserved == 0
    assert saved.response_bytes_consumed == 1024 * 1024
    assert saved.storage_bytes_consumed == 2 * 1024 * 1024
    assert saved.s0_http_consumed == 1
    assert saved.s0_response_bytes_consumed == 1024 * 1024
    with pytest.raises(InvestorError):
        await reserve_campaign_action_hop(
            db_session, campaign_id=campaign.campaign_id, action_id="terms-1", operation_key="terms-twice",
            sample_env_id="sample-env-1", url=terms_url, request_bytes=0,
            response_bytes_limit=1, storage_bytes_limit=1, purpose="terms", now=now,
        )


@pytest.mark.asyncio
async def test_raw_action_terminal_releases_root_for_next_action_and_sample_poll(db_session):
    now = datetime(2026, 10, 17, 2, tzinfo=UTC)
    listing = ObjectRef(kind="listing", id=99, revision=1)
    start, end = now - timedelta(days=4), now - timedelta(days=3)
    profile_hash, contract_hash = "b" * 64, "a" * 64
    raw_urls = ("https://www.cninfo.com.cn/probe-1", "https://www.cninfo.com.cn/probe-2")
    actions = (
        CampaignAction(action_id="raw-1", kind="raw_probe", status="approved",
                       url_allowlist=(raw_urls[0],), http_cap=1, response_byte_cap=1024,
                       storage_byte_cap=2 * 1024 * 1024),
        CampaignAction(action_id="raw-2", kind="raw_probe", status="approved",
                       url_allowlist=(raw_urls[1],), http_cap=1, response_byte_cap=1024,
                       storage_byte_cap=2 * 1024 * 1024),
        CampaignAction(action_id="sample-p1", kind="sample_poll", status="approved", poll_ordinal=1,
                       listing_ref=listing, window_start=start, window_end=end,
                       profile_hash=profile_hash, contract_hash=contract_hash,
                       url_allowlist=("https://www.cninfo.com.cn/query",)),
        CampaignAction(action_id="sample-p2", kind="sample_poll", status="approved", poll_ordinal=2,
                       listing_ref=listing, window_start=start, window_end=end,
                       profile_hash=profile_hash, contract_hash=contract_hash,
                       url_allowlist=("https://www.cninfo.com.cn/query",)),
        CampaignAction(action_id="sample-p3", kind="sample_poll", status="reserved", poll_ordinal=3),
    )
    campaign = await create_disclosure_campaign(db_session, _campaign(db_session, now, actions), now=now)

    async def finish_raw(action_id: str, url: str, at: datetime) -> None:
        permit = await reserve_campaign_action_hop(
            db_session, campaign_id=campaign.campaign_id, action_id=action_id,
            operation_key=f"operation-{action_id}", sample_env_id="sample-env-1", url=url,
            request_bytes=0, response_bytes_limit=1024, storage_bytes_limit=2 * 1024 * 1024,
            purpose="raw_probe", now=at,
        )
        permit_id = UUID(permit["permit_id"])
        await mark_campaign_hop_sent(db_session, permit_id=permit_id, now=at)
        await settle_campaign_hop(db_session, permit_id=permit_id, response_bytes=128,
                                  storage_bytes=1024 * 1024 + 128, result_code="ok", now=at)

    await finish_raw("raw-1", raw_urls[0], now)
    saved = await read_disclosure_campaign(db_session, campaign.campaign_id)
    assert saved.active_poll is None
    with pytest.raises(InvestorError):
        await reserve_campaign_action_hop(
            db_session, campaign_id=campaign.campaign_id, action_id="raw-1",
            operation_key="new-operation-for-terminal-action", sample_env_id="sample-env-1",
            url=raw_urls[0], request_bytes=0, response_bytes_limit=1024,
            storage_bytes_limit=2 * 1024 * 1024, purpose="raw_probe", now=now + timedelta(seconds=11),
        )

    await finish_raw("raw-2", raw_urls[1], now + timedelta(seconds=11))
    grant_id = uuid4()
    grant = BackgroundDisclosureAuthorizationV2(
        schema_version=2, campaign_id=campaign.campaign_id, sample_env_id="sample-env-1",
        manual_only=True, valid_from=now - timedelta(minutes=1), contract_hash=contract_hash,
        listing_refs=(listing,), doc_type_allowlist=("annual_report_full",), history_start=start,
        history_end=end, min_poll_interval_seconds=21600, min_http_interval_seconds=10,
        expires_at=now + timedelta(hours=48), max_staleness_seconds=86400,
        per_poll_query_http=4, per_poll_fetch=1, per_poll_http=8,
        per_poll_response_bytes=64 * 1024 * 1024, grant_daily_http=24,
        grant_daily_response_bytes=128 * 1024 * 1024, storage_bytes_cap=128 * 1024 * 1024,
        authorization_id=grant_id, revision=1, owner_id="local_owner", created_at=now,
        actor="test", authorization_hash="c" * 64,
    )
    await bind_disclosure_campaign_grant(
        db_session, campaign_id=campaign.campaign_id, grant_id=grant_id, grant_revision=1,
        sample_env_id="sample-env-1", now=now + timedelta(seconds=22),
    )
    await authorize_disclosure_campaign_poll(
        db_session, grant=grant, poll_id="sample-after-s0", action_id="sample-p1",
        listing_ref=listing, window_start=start, window_end=end, sample_env_id="sample-env-1",
        profile_hash=profile_hash, contract_hash=contract_hash, now=now + timedelta(seconds=22),
    )
    saved = await read_disclosure_campaign(db_session, campaign.campaign_id)
    assert saved.active_poll == {"poll_id": "sample-after-s0", "action_id": "sample-p1"}


@pytest.mark.asyncio
async def test_unsent_raw_cancel_releases_root_without_reactivating_old_permit(db_session):
    now = datetime(2026, 10, 17, 2, tzinfo=UTC)
    url = "https://www.cninfo.com.cn/cancel-test"
    action = CampaignAction(
        action_id="raw-cancel", kind="raw_probe", status="approved", url_allowlist=(url,),
        http_cap=2, response_byte_cap=2048, storage_byte_cap=4 * 1024 * 1024,
    )
    campaign = await create_disclosure_campaign(db_session, _campaign(db_session, now, (action,)), now=now)
    first = await reserve_campaign_action_hop(
        db_session, campaign_id=campaign.campaign_id, action_id="raw-cancel", operation_key="approval-1",
        sample_env_id="sample-env-1", url=url, request_bytes=0, response_bytes_limit=1024,
        storage_bytes_limit=2 * 1024 * 1024, purpose="raw_probe", now=now,
    )
    first_id = UUID(first["permit_id"])
    await cancel_campaign_hop(db_session, permit_id=first_id, now=now)
    with pytest.raises(InvestorError):
        await mark_campaign_hop_sent(db_session, permit_id=first_id, now=now)
    saved = await read_disclosure_campaign(db_session, campaign.campaign_id)
    assert saved.active_poll is None
    assert saved.s0_http_consumed == 0 and saved.s0_http_reserved == 0

    second = await reserve_campaign_action_hop(
        db_session, campaign_id=campaign.campaign_id, action_id="raw-cancel", operation_key="approval-2",
        sample_env_id="sample-env-1", url=url, request_bytes=0, response_bytes_limit=1024,
        storage_bytes_limit=2 * 1024 * 1024, purpose="raw_probe", now=now,
    )
    assert second["permit_id"] != first["permit_id"]
    await cancel_campaign_hop(db_session, permit_id=UUID(second["permit_id"]), now=now)


@pytest.mark.asyncio
async def test_sample_action_is_single_poll_mapping_and_reserved_third_slot_has_zero_admission(db_session):
    now = datetime(2026, 10, 17, 2, tzinfo=UTC)
    listing = ObjectRef(kind="listing", id=99, revision=1)
    start, end = now - timedelta(days=4), now - timedelta(days=3)
    profile_hash, contract_hash = "b" * 64, "a" * 64
    urls = ("https://www.cninfo.com.cn/query", "https://static.cninfo.com.cn/finalpage/sample.pdf")
    actions = tuple(
        CampaignAction(
            action_id=f"sample-p{i}", kind="sample_poll",
            status="reserved" if i == 3 else "approved", poll_ordinal=i,
            listing_ref=listing, window_start=start, window_end=end,
            profile_hash=profile_hash, contract_hash=contract_hash, url_allowlist=urls,
        ) for i in (1, 2, 3)
    )
    campaign = await create_disclosure_campaign(db_session, _campaign(db_session, now, actions), now=now)
    grant_id = uuid4()
    grant = BackgroundDisclosureAuthorizationV2(
        schema_version=2, campaign_id=campaign.campaign_id, sample_env_id="sample-env-1",
        manual_only=True, valid_from=now - timedelta(minutes=1),
        contract_hash=contract_hash, listing_refs=(listing,), doc_type_allowlist=("annual_report_full",),
        history_start=start, history_end=end, allow_overlap=True, allow_deep_sweep=False,
        min_poll_interval_seconds=21600, min_http_interval_seconds=5, expires_at=now + timedelta(hours=48),
        max_staleness_seconds=86400, per_poll_query_http=4, per_poll_fetch=1,
        per_poll_http=8, per_poll_response_bytes=64 * 1024 * 1024,
        grant_daily_http=24, grant_daily_response_bytes=128 * 1024 * 1024,
        storage_bytes_cap=128 * 1024 * 1024,
        authorization_id=grant_id, revision=1, owner_id="local_owner", created_at=now,
        actor="test", authorization_hash="c" * 64,
    )
    await bind_disclosure_campaign_grant(
        db_session, campaign_id=campaign.campaign_id, grant_id=grant_id, grant_revision=1,
        sample_env_id="sample-env-1", now=now,
    )
    await authorize_disclosure_campaign_poll(
        db_session, grant=grant, poll_id="poll-p1", action_id="sample-p1", listing_ref=listing,
        window_start=start, window_end=end, sample_env_id="sample-env-1", profile_hash=profile_hash,
        contract_hash=contract_hash, now=now,
    )
    with pytest.raises(InvestorError):
        await authorize_disclosure_campaign_poll(
            db_session, grant=grant, poll_id="poll-p1-replacement", action_id="sample-p1", listing_ref=listing,
            window_start=start, window_end=end, sample_env_id="sample-env-1", profile_hash=profile_hash,
            contract_hash=contract_hash, now=now,
        )
    with pytest.raises(InvestorError):
        await authorize_disclosure_campaign_poll(
            db_session, grant=grant, poll_id="poll-p3", action_id="sample-p3", listing_ref=listing,
            window_start=start, window_end=end, sample_env_id="sample-env-1", profile_hash=profile_hash,
            contract_hash=contract_hash, now=now,
        )
    saved = await read_disclosure_campaign(db_session, campaign.campaign_id)
    assert saved.active_poll == {"poll_id": "poll-p1", "action_id": "sample-p1"}


@pytest.mark.asyncio
async def test_low_disk_free_rejects_s0_before_reserving_or_sending(db_session, monkeypatch):
    from types import SimpleNamespace

    from investment_agent.supplemental_rules import disclosure_campaign as campaign_module

    now = datetime(2026, 10, 17, 2, tzinfo=UTC)
    terms_url = "https://www.cninfo.com.cn/terms"
    action = CampaignAction(
        action_id="terms-1", kind="terms", status="approved", url_allowlist=(terms_url,),
        method="GET", http_cap=1, response_byte_cap=1024 * 1024,
        storage_byte_cap=2 * 1024 * 1024,
    )
    campaign = await create_disclosure_campaign(db_session, _campaign(db_session, now, (action,)), now=now)
    monkeypatch.setattr(campaign_module.shutil, "disk_usage", lambda _path: SimpleNamespace(free=1024 * 1024))
    with pytest.raises(InvestorError):
        await reserve_campaign_action_hop(
            db_session, campaign_id=campaign.campaign_id, action_id="terms-1", operation_key="terms-no-space",
            sample_env_id="sample-env-1", url=terms_url, request_bytes=0,
            response_bytes_limit=1024 * 1024, storage_bytes_limit=2 * 1024 * 1024,
            purpose="terms", now=now,
        )
    saved = await read_disclosure_campaign(db_session, campaign.campaign_id)
    assert saved.http_reserved == 0
    assert saved.http_consumed == 0


@pytest.mark.asyncio
async def test_recovery_ticket_survives_pause_and_is_bound_to_original_hop(db_session):
    now = datetime(2026, 10, 17, 2, tzinfo=UTC)
    listing = ObjectRef(kind="listing", id=99, revision=1)
    start, end = now - timedelta(days=4), now - timedelta(days=3)
    profile_hash, contract_hash = "b" * 64, "a" * 64
    url = "https://www.cninfo.com.cn/query"
    actions = tuple(
        CampaignAction(
            action_id=f"sample-p{i}", kind="sample_poll",
            status="reserved" if i == 3 else "approved", poll_ordinal=i,
            listing_ref=listing, window_start=start, window_end=end,
            profile_hash=profile_hash, contract_hash=contract_hash, url_allowlist=(url,),
        ) for i in (1, 2, 3)
    )
    campaign = await create_disclosure_campaign(db_session, _campaign(db_session, now, actions), now=now)
    grant_id = uuid4()
    grant = BackgroundDisclosureAuthorizationV2(
        schema_version=2, campaign_id=campaign.campaign_id, sample_env_id="sample-env-1",
        manual_only=True, valid_from=now - timedelta(minutes=1), contract_hash=contract_hash,
        listing_refs=(listing,), doc_type_allowlist=("annual_report_full",), history_start=start, history_end=end,
        min_poll_interval_seconds=21600, min_http_interval_seconds=5, expires_at=now + timedelta(hours=48),
        max_staleness_seconds=86400, per_poll_query_http=4, per_poll_fetch=1, per_poll_http=8,
        per_poll_response_bytes=64 * 1024 * 1024, grant_daily_http=24,
        grant_daily_response_bytes=128 * 1024 * 1024, storage_bytes_cap=128 * 1024 * 1024,
        authorization_id=grant_id, revision=1, owner_id="local_owner", created_at=now,
        actor="test", authorization_hash="c" * 64,
    )
    await bind_disclosure_campaign_grant(db_session, campaign_id=campaign.campaign_id,
        grant_id=grant_id, grant_revision=1, sample_env_id="sample-env-1", now=now)
    await authorize_disclosure_campaign_poll(
        db_session, grant=grant, poll_id="poll-recover", action_id="sample-p1", listing_ref=listing,
        window_start=start, window_end=end, sample_env_id="sample-env-1", profile_hash=profile_hash,
        contract_hash=contract_hash, now=now,
    )
    identity = DisclosureAttemptIdentity(
        poll_id="poll-recover", logical_request_key="directory", attempt_id=uuid4(), attempt_ordinal=1,
        hop_ordinal=0, hop_id="directory-1", request_kind="query", request_hash="d" * 64,
        authorization_id=grant_id, authorization_revision=1, authorization_hash="c" * 64,
        contract_hash=contract_hash, scope_hash="e" * 64, listing_ref=listing,
        window_start=start, window_end=end, poll_http_cap=8, poll_query_http_cap=4,
        poll_fetch_cap=1, poll_response_bytes_cap=64 * 1024 * 1024, url=url,
        response_bytes_limit=1024 * 1024,
    )
    permit = await reserve_campaign_hop(
        db_session, grant=grant, identity=identity, permit_id=uuid4(),
        response_bytes_limit=1024 * 1024, storage_bytes_limit=2 * 1024 * 1024, now=now,
    )
    current = await read_disclosure_campaign(db_session, campaign.campaign_id)
    await amend_disclosure_campaign(
        db_session, campaign_id=campaign.campaign_id, expected_revision=current.revision,
        patch={"status": "paused"}, approval_ref="pause-review", now=now,
    )
    operation_id, approval = uuid4(), "recovery-approval-1"
    ticket = await authorize_campaign_recovery(
        db_session, campaign_id=campaign.campaign_id, poll_id="poll-recover", action_id="sample-p1",
        grant_id=grant_id, grant_revision=1, sample_env_id="sample-env-1",
        recovery_approval_ref=approval, operation_id=operation_id, now=now,
    )
    assert ticket["allow_network"] is False
    assert (await authorize_campaign_recovery(
        db_session, campaign_id=campaign.campaign_id, poll_id="poll-recover", action_id="sample-p1",
        grant_id=grant_id, grant_revision=1, sample_env_id="sample-env-1",
        recovery_approval_ref=approval, operation_id=operation_id, now=now,
    )) == ticket
    with pytest.raises(InvestorError):
        await authorize_campaign_recovery(
            db_session, campaign_id=campaign.campaign_id, poll_id="poll-recover", action_id="sample-p1",
            grant_id=grant_id, grant_revision=1, sample_env_id="sample-env-1",
            recovery_approval_ref="different-approval", operation_id=operation_id, now=now,
        )
    decision = await assert_campaign_recovery_authority(
        db_session, permit_id=UUID(permit["permit_id"]), recovery_operation_id=operation_id,
        sample_env_id="sample-env-1",
    )
    assert decision["allow_network"] is False
