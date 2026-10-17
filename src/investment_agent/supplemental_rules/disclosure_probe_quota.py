"""S0 uses the same owner/day, storage and concurrency ledger as sample HTTP.

These transaction-local hooks never send HTTP or grant sample/research rights.
The caller has already checked the immutable, exact campaign raw action.
"""

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.disclosure_contracts import (
    AUTH_FEED,
    OWNER_DAILY_HTTP_CAP,
    OWNER_DAILY_RESPONSE_BYTES_CAP,
    OWNER_MIN_REQUEST_INTERVAL_SECONDS,
    OWNER_STORAGE_BYTES_CAP,
)

OWNER: OwnerId = "local_owner"
DAILY_HTTP = 24
DAILY_BYTES = 128 * 1024 * 1024
MIN_INTERVAL = 10


async def _policy(session: AsyncSession, spec: dict[str, Any], now: datetime) -> dict[str, Any]:
    from investment_agent.supplemental_rules import disclosure_authorization as auth

    repo = SupplementalUpdatesRepository(session)
    existing = await repo.get_source(OWNER, AUTH_FEED, auth.OWNER_POLICY_SCOPE)
    if existing is None:
        # The fixed S0 owner policy is part of the explicit campaign raw-action
        # approval, not a runtime/default permission. Its receipt is understood
        # by the existing grant policy reader and survives campaign changes.
        state, payload = await auth._ensure_state(
            session, feed=AUTH_FEED, scope=auth.OWNER_POLICY_SCOPE,
            config_hash=auth.AUTH_CONFIG_HASH, initial_payload={"schema_version": 1},
        )
        policy = {"revision": 1, "owner_daily_http": DAILY_HTTP,
                  "owner_daily_response_bytes": DAILY_BYTES,
                  "owner_min_request_interval_seconds": MIN_INTERVAL}
        receipt = await repo.append_change(
            OWNER, change_key="p4:s0:initial-owner-policy", kind="source_authorization",
            payload={"record_type": "s0_owner_policy_v2", "campaign_id": spec["campaign_id"],
                     "action_id": spec["action_id"], "owner_policy": policy},
            acquired_at=now, available_from=now, committed_at=now,
        )
        payload.update({"policy_revision": 1, **{k: v for k, v in policy.items() if k != "revision"},
                        "hard_http": OWNER_DAILY_HTTP_CAP,
                        "hard_response_bytes": OWNER_DAILY_RESPONSE_BYTES_CAP,
                        "min_request_interval_seconds": OWNER_MIN_REQUEST_INTERVAL_SECONDS,
                        "policy_receipt_id": str(receipt.id),
                        "policy_receipt_sequence": receipt.sequence,
                        "policy_receipt_hash": receipt.payload_hash})
        await auth._save_state(session, state, payload)
    return (await auth._load_policy(session))[1]


def _keys(permit_id: UUID, phase: str) -> str:
    return f"p4:s0-quota:{permit_id}:{phase}"


async def _reservation(session: AsyncSession, permit_id: UUID, spec: dict[str, Any]) -> dict[str, Any]:
    row = await SupplementalUpdatesRepository(session).get_change_by_key(OWNER, _keys(permit_id, "reserved"))
    if row is None or row.payload.get("spec_hash") != canonical_hash(spec):
        raise InvestorError("invalid_reference", "S0 owner quota reservation is missing or mismatched")
    return dict(row.payload)


async def raw_reserve(session: AsyncSession, *, permit_id: UUID, spec: dict[str, Any],
                      now: datetime) -> None:
    from investment_agent.supplemental_rules import disclosure_authorization as auth

    policy = await _policy(session, spec, now)
    repo = SupplementalUpdatesRepository(session)
    prior = await repo.get_change_by_key(OWNER, _keys(permit_id, "reserved"))
    if prior is not None:
        await _reservation(session, permit_id, spec)
        return
    day = auth._day(now)
    owner_state, owner = await auth._quota_state(session, f"owner-day:{day}", day=day)
    storage_state, storage = await auth._quota_state(session, auth.STORAGE_SCOPE)
    cooldown_state, cooldown = await auth._quota_state(session, auth.COOLDOWN_SCOPE)
    response_cap, storage_cap = spec["request_bytes"], spec["storage_bytes"]
    if (owner["request_reserved"] + owner["request_consumed"] + 1 > min(DAILY_HTTP, policy["owner_daily_http"])
        or owner["bytes_reserved"] + owner["bytes_consumed"] + response_cap
        > min(DAILY_BYTES, policy["owner_daily_response_bytes"])
        or storage["storage_reserved"] + storage["storage_consumed"] + storage_cap > OWNER_STORAGE_BYTES_CAP):
        raise InvestorError("capacity_blocked", "shared source day/storage quota exhausted")
    if cooldown.get("active_hop") is not None:
        raise InvestorError("source_cooldown", "source has an unsettled HTTP hop")
    last = cooldown.get("last_sent_at")
    if last is not None and now < datetime.fromisoformat(last) + timedelta(
        seconds=max(MIN_INTERVAL, policy["owner_min_request_interval_seconds"])
    ):
        raise InvestorError("source_cooldown", "shared source minimum interval has not elapsed")
    owner.update(request_reserved=owner["request_reserved"] + 1,
                 bytes_reserved=owner["bytes_reserved"] + response_cap, revision=owner["revision"] + 1)
    storage.update(storage_reserved=storage["storage_reserved"] + storage_cap,
                   revision=storage["revision"] + 1)
    cooldown.update(active_hop={"probe_permit_id": str(permit_id)}, revision=cooldown["revision"] + 1)
    await auth._save_state(session, owner_state, owner)
    await auth._save_state(session, storage_state, storage)
    await auth._save_state(session, cooldown_state, cooldown)
    await repo.append_change(
        OWNER, change_key=_keys(permit_id, "reserved"), kind="source_attempt",
        payload={"record_type": "s0_owner_quota_v2", "stage": "reserved", "day": day,
                 "spec_hash": canonical_hash(spec), "response_cap": response_cap, "storage_cap": storage_cap},
        acquired_at=now, available_from=now, committed_at=now,
    )


async def raw_mark_sent(session: AsyncSession, *, permit_id: UUID, spec: dict[str, Any],
                        now: datetime) -> None:
    from investment_agent.supplemental_rules import disclosure_authorization as auth

    repo = SupplementalUpdatesRepository(session)
    reservation = await _reservation(session, permit_id, spec)
    if reservation["day"] != auth._day(now):
        raise InvestorError("revision_conflict", "S0 unsent reservation crossed a Shanghai day")
    for phase in ("sent", "result", "canceled"):
        if await repo.get_change_by_key(OWNER, _keys(permit_id, phase)) is not None:
            raise InvestorError("idempotency_conflict", "S0 owner permit already sent or terminal")
    policy = (await auth._load_policy(session))[1]
    state, cooldown = await auth._quota_state(session, auth.COOLDOWN_SCOPE)
    if cooldown.get("active_hop") != {"probe_permit_id": str(permit_id)}:
        raise InvestorError("source_cooldown", "S0 no longer owns the source HTTP token")
    last = cooldown.get("last_sent_at")
    if last is not None and now < datetime.fromisoformat(last) + timedelta(
        seconds=max(MIN_INTERVAL, policy["owner_min_request_interval_seconds"])
    ):
        raise InvestorError("source_cooldown", "source minimum interval has not elapsed")
    # A policy may have tightened since reserve. The total includes this hop.
    _, owner = await auth._quota_state(session, f"owner-day:{reservation['day']}", day=reservation["day"])
    if (owner["request_reserved"] + owner["request_consumed"] > min(DAILY_HTTP, policy["owner_daily_http"])
        or owner["bytes_reserved"] + owner["bytes_consumed"] > min(DAILY_BYTES, policy["owner_daily_response_bytes"])):
        raise InvestorError("capacity_blocked", "source policy tightened before S0 send")
    cooldown.update(last_sent_at=now.isoformat(), revision=cooldown["revision"] + 1)
    await auth._save_state(session, state, cooldown)
    await repo.append_change(
        OWNER, change_key=_keys(permit_id, "sent"), kind="source_attempt",
        payload={"record_type": "s0_owner_quota_v2", "stage": "sent", "sent_at": now.isoformat()},
        acquired_at=now, available_from=now, committed_at=now,
    )


async def _finish(session: AsyncSession, *, permit_id: UUID, spec: dict[str, Any],
                  response_bytes: int | None, storage_bytes: int, sent: bool, now: datetime) -> None:
    from investment_agent.supplemental_rules import disclosure_authorization as auth

    repo = SupplementalUpdatesRepository(session)
    reservation = await _reservation(session, permit_id, spec)
    sent_row = await repo.get_change_by_key(OWNER, _keys(permit_id, "sent"))
    if bool(sent_row) != sent:
        raise InvestorError("invalid_reference", "S0 settlement does not match the durable sent marker")
    phase = "result" if sent else "canceled"
    if await repo.get_change_by_key(OWNER, _keys(permit_id, phase)) is not None:
        raise InvestorError("idempotency_conflict", "S0 owner quota already settled")
    cap, storage_cap = reservation["response_cap"], reservation["storage_cap"]
    charged = cap if response_bytes is None else response_bytes
    stored = storage_cap if response_bytes is None else storage_bytes
    if not (0 <= charged <= cap and 0 <= stored <= storage_cap):
        raise InvestorError("capacity_blocked", "S0 result exceeds owner quota reservation")
    owner_state, owner = await auth._quota_state(session, f"owner-day:{reservation['day']}", day=reservation["day"])
    storage_state, storage = await auth._quota_state(session, auth.STORAGE_SCOPE)
    cooldown_state, cooldown = await auth._quota_state(session, auth.COOLDOWN_SCOPE)
    if (owner["request_reserved"] < 1 or owner["bytes_reserved"] < cap
        or storage["storage_reserved"] < storage_cap
        or cooldown.get("active_hop") != {"probe_permit_id": str(permit_id)}):
        raise InvestorError("invalid_reference", "S0 shared quota reservation/token is inconsistent")
    owner.update(request_reserved=owner["request_reserved"] - 1,
                 bytes_reserved=owner["bytes_reserved"] - cap,
                 request_consumed=owner["request_consumed"] + int(sent),
                 bytes_consumed=owner["bytes_consumed"] + (charged if sent else 0),
                 revision=owner["revision"] + 1)
    storage.update(storage_reserved=storage["storage_reserved"] - storage_cap,
                   storage_consumed=storage["storage_consumed"] + (stored if sent else 0),
                   revision=storage["revision"] + 1)
    cooldown.update(active_hop=None, revision=cooldown["revision"] + 1)
    for state, payload in ((owner_state, owner), (storage_state, storage), (cooldown_state, cooldown)):
        await auth._save_state(session, state, payload)
    await repo.append_change(
        OWNER, change_key=_keys(permit_id, phase), kind="source_attempt",
        payload={"record_type": "s0_owner_quota_v2", "stage": phase, "unknown": response_bytes is None,
                 "charged_http": int(sent), "charged_bytes": charged if sent else 0,
                 "charged_storage": stored if sent else 0, "day": reservation["day"]},
        acquired_at=now, available_from=now, committed_at=now,
    )


async def raw_settle(session: AsyncSession, *, permit_id: UUID, spec: dict[str, Any],
                     response_bytes: int | None, storage_bytes: int, now: datetime) -> None:
    await _finish(session, permit_id=permit_id, spec=spec, response_bytes=response_bytes,
                  storage_bytes=storage_bytes, sent=True, now=now)


async def raw_cancel(session: AsyncSession, *, permit_id: UUID, spec: dict[str, Any],
                     now: datetime) -> None:
    await _finish(session, permit_id=permit_id, spec=spec, response_bytes=0,
                  storage_bytes=0, sent=False, now=now)


async def recover_raw_probe_hop(
    session: AsyncSession, *, permit_id: UUID, campaign_id: UUID, sample_env_id: str,
    operation_id: UUID, approval_ref: str, now: datetime,
) -> dict[str, Any]:
    """Explicit, exact recovery of old S0 accounting; never grants another send.

    Cancellation/result markers fence the old executor's subsequent sent/result
    CAS. A paused/revoked capture approval cannot prevent conservative charging.
    Restored files with a different database identity remain blocked.
    """
    from investment_agent.supplemental_rules.disclosure_authorization import _begin_write
    from investment_agent.supplemental_rules.disclosure_campaign import (
        cancel_campaign_hop,
        read_disclosure_campaign,
        settle_campaign_hop,
    )

    if not approval_ref.strip() or len(approval_ref) > 256:
        raise InvestorError("disclosure_scope_forbidden", "exact raw recovery approval is required")
    await _begin_write(session)
    repo = SupplementalUpdatesRepository(session)
    campaign = await read_disclosure_campaign(session, campaign_id)
    if campaign.sample_env_id != sample_env_id:
        raise InvestorError("disclosure_scope_forbidden", "raw recovery environment mismatch")
    reservation = await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:reserved")
    if (reservation is None or reservation.payload.get("is_action") is not True
        or reservation.payload.get("spec", {}).get("campaign_id") != str(campaign_id)):
        raise InvestorError("disclosure_scope_forbidden", "raw recovery is not bound to that exact permit")
    spec = reservation.payload["spec"]
    decision = {"record_type": "s0_exact_recovery_v2", "permit_id": str(permit_id),
                "campaign_id": str(campaign_id), "sample_env_id": sample_env_id,
                "approval_ref": approval_ref, "allow_network": False}
    key = f"p4:s0-recovery:{operation_id}"
    prior = await repo.get_change_by_key(OWNER, key)
    if prior is not None:
        if prior.request_hash != canonical_hash(decision):
            raise InvestorError("idempotency_conflict", "raw recovery operation reused with a different scope")
        return dict(prior.payload)
    result = await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:result")
    canceled = await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:canceled")
    sent = await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:sent")
    if result is not None:
        outcome = str(result.payload.get("stage"))
    elif canceled is not None:
        outcome = "canceled"
    elif sent is not None:
        await settle_campaign_hop(session, permit_id=permit_id, response_bytes=None,
                                  storage_bytes=spec["storage_bytes"],
                                  result_code="unknown_after_explicit_raw_recovery", now=now)
        outcome = "unknown"
    else:
        await cancel_campaign_hop(session, permit_id=permit_id, now=now)
        outcome = "canceled"
    receipt = await repo.append_change(
        OWNER, change_key=key, kind="source_attempt", operation_id=operation_id,
        request_hash=canonical_hash(decision), payload={**decision, "outcome": outcome},
        acquired_at=now, available_from=now, committed_at=now,
    )
    return dict(receipt.payload)
