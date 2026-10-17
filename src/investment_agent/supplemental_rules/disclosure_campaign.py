"""CAS-backed AF-P4-08 campaign authority and cumulative HTTP accounting.

The sidecar deliberately uses the existing supplemental source-state and
immutable change journal. Callers keep the surrounding transaction open so a
campaign phase and its ordinary P4 hop phase commit atomically.
"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.disclosure_campaign_contracts import (
    CAMPAIGN_CONTRACT_VERSION,
    CAMPAIGN_FEED,
    BackgroundDisclosureAuthorizationV2,
    DisclosureCampaignV2,
)
from investment_agent.supplemental_rules.disclosure_contracts import DisclosureAttemptIdentity
from investment_agent.supplemental_rules.update_contracts import (
    SupplementalChangeReceipt,
    SupplementalSourceState,
)

OWNER: OwnerId = "local_owner"
HOP_REQUEST_RESERVE = 1
HOP_STORAGE_METADATA = 1 * 1024 * 1024
MIN_DISK_FREE = 1 * 1024 * 1024 * 1024


def _utc(value: datetime | None) -> datetime:
    now = value or datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return now.astimezone(UTC)


def _scope(campaign_id: UUID) -> str:
    return f"campaign:{campaign_id}"


def _db_path(session: AsyncSession) -> str:
    bind = cast(AsyncEngine, session.bind)
    if bind is None:
        raise InvestorError("invalid_reference", "campaign requires a bound database")
    url = bind.url
    if url.get_backend_name() != "sqlite":
        raise InvestorError("invalid_reference", "campaign is limited to isolated SQLite")
    database = url.database
    if not database or database == ":memory:":
        # Stable for the lifetime of an in-memory test engine, never accepted
        # for a live sample environment by the runner's environment gate.
        return f"sqlite-memory:{id(bind.sync_engine)}"
    return str(Path(database).expanduser().resolve())


def _db_identity(session: AsyncSession) -> tuple[int, int]:
    bind = cast(AsyncEngine, session.bind)
    if bind is None:
        raise InvestorError("invalid_reference", "campaign requires a bound database")
    if bind.url.database in (None, ":memory:"):
        return (0, id(bind.sync_engine))
    try:
        info = os.stat(_db_path(session))
    except OSError as exc:
        raise InvestorError("invalid_reference", "campaign SQLite file identity unavailable") from exc
    return info.st_dev, info.st_ino


def _assert_disk_available(session: AsyncSession, *, response_reserve: int, storage_reserve: int) -> None:
    database = _db_path(session)
    folder = Path(database).parent if not database.startswith("sqlite-memory:") else Path.cwd()
    try:
        available = shutil.disk_usage(folder).free
    except OSError as exc:
        raise InvestorError("capacity_blocked", "campaign storage free space cannot be verified") from exc
    required = max(MIN_DISK_FREE, response_reserve + HOP_STORAGE_METADATA, storage_reserve)
    if available < required:
        raise InvestorError("capacity_blocked", "campaign requires at least 1 GiB free disk and its hop reservation")


async def _begin_write(session: AsyncSession) -> None:
    connection = await session.connection()
    if connection.dialect.name == "sqlite":
        active = await connection.run_sync(
            lambda conn: bool(getattr(conn.connection.driver_connection, "in_transaction", False))
        )
        if not active:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")


async def _load(
    session: AsyncSession, campaign_id: UUID, *, lock: bool = True,
) -> tuple[SupplementalSourceState, DisclosureCampaignV2]:
    repo = SupplementalUpdatesRepository(session)
    state = await repo.get_source(OWNER, CAMPAIGN_FEED, _scope(campaign_id), for_update=lock)
    if state is None:
        raise InvestorError("not_found", "campaign不存在")
    if state.contract_version != CAMPAIGN_CONTRACT_VERSION or not isinstance(state.payload, dict):
        raise InvestorError("invalid_reference", "campaign state version is invalid")
    try:
        model = DisclosureCampaignV2.model_validate(state.payload["campaign"])
    except (KeyError, ValueError, TypeError) as exc:
        raise InvestorError("invalid_reference", "campaign payload is invalid") from exc
    if model.campaign_id != campaign_id or model.revision != state.revision:
        raise InvestorError("invalid_reference", "campaign state revision mismatch")
    if (model.database_device is not None and model.database_inode is not None
            and (model.database_device, model.database_inode) != _db_identity(session)):
        raise InvestorError("invalid_reference", "campaign SQLite file identity changed; backup restore is fail closed")
    from investment_agent.supplemental_rules.disclosure_campaign_anchor import (
        verify_campaign_anchor,
    )

    await verify_campaign_anchor(session, campaign_id)
    authority = state.payload.get("authority_ref")
    if not isinstance(authority, dict):
        raise InvestorError("invalid_reference", "campaign authority receipt missing")
    sequence = authority.get("sequence")
    receipt = await repo.get_change(OWNER, sequence) if type(sequence) is int else None
    if (
        receipt is None or str(receipt.id) != authority.get("id")
        or receipt.payload_hash != authority.get("payload_hash")
        or receipt.payload.get("record_type") != "disclosure_campaign_v2"
        or receipt.payload.get("campaign_hash") != canonical_hash(model)
        or receipt.payload.get("revision") != state.revision
    ):
        raise InvestorError("invalid_reference", "campaign state does not match authority journal")
    previous = receipt.payload.get("previous_authority_ref")
    revision = state.revision - 1
    while revision > 0:
        if not isinstance(previous, dict):
            raise InvestorError("invalid_reference", "campaign authority chain is truncated")
        prior_sequence = previous.get("sequence")
        prior = await repo.get_change(OWNER, prior_sequence) if type(prior_sequence) is int else None
        if (prior is None or str(prior.id) != previous.get("id")
                or prior.payload_hash != previous.get("payload_hash")
                or prior.payload.get("record_type") != "disclosure_campaign_v2"
                or prior.payload.get("campaign_id") != str(campaign_id)
                or prior.payload.get("revision") != revision):
            raise InvestorError("invalid_reference", "campaign authority chain is broken")
        previous = prior.payload.get("previous_authority_ref")
        revision -= 1
    if previous is not None:
        raise InvestorError("invalid_reference", "campaign authority root has an unexpected predecessor")
    return state, model


async def _save(
    session: AsyncSession, state: SupplementalSourceState, model: DisclosureCampaignV2, *, now: datetime,
) -> DisclosureCampaignV2:
    repo = SupplementalUpdatesRepository(session)
    next_rev = state.revision + 1
    updated_model = model.model_copy(update={"revision": next_rev})
    prior_ref = state.payload.get("authority_ref")
    digest = canonical_hash(updated_model)
    receipt = await repo.append_change(
        OWNER,
        change_key=f"p4:campaign:{updated_model.campaign_id}:r{next_rev}",
        kind="source_attempt",
        payload={
            "record_type": "disclosure_campaign_v2",
            "campaign_id": str(updated_model.campaign_id),
            "previous_authority_ref": prior_ref,
            "revision": next_rev,
            "campaign_hash": digest,
            "campaign": updated_model.model_dump(mode="json"),
        },
        acquired_at=now,
        available_from=now,
        committed_at=now,
    )
    ref = {"sequence": receipt.sequence, "id": str(receipt.id), "payload_hash": receipt.payload_hash}
    projection = state.model_copy(update={
        "revision": next_rev,
        "payload": {"schema_version": 2, "campaign": updated_model.model_dump(mode="json"), "authority_ref": ref},
    })
    await repo.save_source(projection, expected_revision=state.revision)
    return updated_model


async def create_disclosure_campaign(
    session: AsyncSession, campaign: DisclosureCampaignV2, *, now: datetime | None = None,
) -> DisclosureCampaignV2:
    current = _utc(now)
    await _begin_write(session)
    path = _db_path(session)
    if campaign.database_path != path:
        raise InvestorError("disclosure_scope_forbidden", "campaign database path does not match current SQLite")
    if current < campaign.valid_from or current >= campaign.expires_at:
        raise InvestorError("disclosure_authorization_expired", "campaign is outside its absolute validity")
    repo = SupplementalUpdatesRepository(session)
    if any((campaign.bound_grants, campaign.active_poll, campaign.http_reserved, campaign.http_consumed,
            campaign.response_bytes_reserved, campaign.response_bytes_consumed, campaign.storage_bytes_reserved,
            campaign.storage_bytes_consumed, campaign.poll_usage, campaign.action_usage, campaign.sample_http_reserved,
            campaign.sample_http_consumed, campaign.sample_response_bytes_reserved,
            campaign.sample_response_bytes_consumed, campaign.s0_http_reserved,
            campaign.s0_response_bytes_reserved, campaign.s0_storage_bytes_reserved)):
        raise InvestorError("invalid_reference", "new campaign cannot contain prefilled usage or grant bindings")
    if any(action.poll_ordinal == 3 and action.status == "approved" for action in campaign.actions):
        raise InvestorError("disclosure_scope_forbidden", "third sample poll requires a later explicit amendment")
    device, inode = _db_identity(session)
    initial = campaign.model_copy(update={"revision": 1, "database_path": path,
                                          "database_device": device, "database_inode": inode})
    state = await repo.ensure_source(
        OWNER, CAMPAIGN_FEED, _scope(campaign.campaign_id), canonical_hash({"campaign": str(campaign.campaign_id)}),
        payload={"schema_version": 2, "campaign": initial.model_dump(mode="json"), "authority_ref": None},
        contract_version=CAMPAIGN_CONTRACT_VERSION,
    )
    if state.revision != 0 or state.payload.get("authority_ref") is not None:
        existing_state, existing = await _load(session, campaign.campaign_id)
        if canonical_hash(existing) != canonical_hash(initial):
            raise InvestorError("idempotency_conflict", "campaign id already exists with different authority")
        return existing
    # Seed authority receipt and first projection revision.
    created = await _save(session, state, initial, now=current)
    from investment_agent.supplemental_rules.disclosure_campaign_anchor import (
        initialize_campaign_anchor,
    )

    await initialize_campaign_anchor(session, campaign.campaign_id)
    return created


async def read_disclosure_campaign(session: AsyncSession, campaign_id: UUID) -> DisclosureCampaignV2:
    return (await _load(session, campaign_id, lock=False))[1]


async def amend_disclosure_campaign(
    session: AsyncSession, *, campaign_id: UUID, expected_revision: int,
    patch: dict[str, Any], approval_ref: str, now: datetime | None = None,
) -> DisclosureCampaignV2:
    current = _utc(now)
    await _begin_write(session)
    state, campaign = await _load(session, campaign_id)
    if state.revision != expected_revision:
        raise InvestorError("revision_conflict", "campaign CAS revision changed")
    if current >= campaign.expires_at or campaign.status != "active":
        raise InvestorError("disclosure_authorization_expired", "campaign is not active")
    if not approval_ref:
        raise InvestorError("disclosure_scope_forbidden", "campaign amendments require an approval reference")
    allowed = {"actions", "status", "http_cap", "response_bytes_cap", "storage_bytes_cap", "sample_http_cap", "sample_response_bytes_cap"}
    if set(patch) - allowed:
        raise InvestorError("disclosure_scope_forbidden", "campaign patch cannot change identity or window")
    candidate = DisclosureCampaignV2.model_validate({
        **campaign.model_dump(mode="python"), **patch, "approval_ref": approval_ref,
    })
    if candidate.http_cap > campaign.http_cap or candidate.response_bytes_cap > campaign.response_bytes_cap or candidate.storage_bytes_cap > campaign.storage_bytes_cap:
        raise InvestorError("disclosure_scope_forbidden", "campaign lifetime caps cannot increase")
    # Existing approvals are immutable. A reserved action can only be enabled
    # by a separately named amendment to that exact action.
    if "actions" in patch:
        if len(candidate.actions) != len(campaign.actions) or tuple(a.action_id for a in candidate.actions) != tuple(a.action_id for a in campaign.actions):
            raise InvestorError("disclosure_scope_forbidden", "campaign actions cannot be added, removed, or reordered")
        changed = [(old, new) for old, new in zip(campaign.actions, candidate.actions, strict=True)
                   if old.model_dump(mode="json") != new.model_dump(mode="json")]
        if len(changed) != 1:
            raise InvestorError("disclosure_scope_forbidden", "only one reserved action may be amended at a time")
        old, new = changed[0]
        if not (old.kind == "sample_poll" and old.poll_ordinal == 3
                and old.status in {"reserved", "disabled"} and new.status == "approved"
                and approval_ref != campaign.approval_ref and old.action_id == new.action_id
                and old.poll_ordinal == new.poll_ordinal):
            raise InvestorError("disclosure_scope_forbidden", "only the exact third reserved poll can be approved by amendment")
    return await _save(session, state, candidate, now=current)


async def bind_disclosure_campaign_grant(
    session: AsyncSession, *, campaign_id: UUID, grant_id: UUID, grant_revision: int,
    sample_env_id: str, now: datetime | None = None,
) -> DisclosureCampaignV2:
    current = _utc(now)
    await _begin_write(session)
    state, campaign = await _load(session, campaign_id)
    if campaign.status != "active" or current >= campaign.expires_at:
        raise InvestorError("disclosure_authorization_expired", "campaign is not active")
    if sample_env_id != campaign.sample_env_id or _db_path(session) != campaign.database_path:
        raise InvestorError("disclosure_scope_forbidden", "campaign environment binding mismatch")
    grants = list(campaign.bound_grants)
    binding = {"authorization_id": str(grant_id), "revision": grant_revision, "sample_env_id": sample_env_id}
    if binding not in grants:
        grants.append(binding)
    return await _save(session, state, campaign.model_copy(update={"bound_grants": tuple(grants)}), now=current)


async def assert_campaign_action(
    session: AsyncSession, *, campaign_id: UUID, action_id: str, grant_id: UUID,
    grant_revision: int, poll_id: str | None, sample_env_id: str, database_path: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = _utc(now)
    _state, campaign = await _load(session, campaign_id, lock=False)
    if campaign.status != "active" or current < campaign.valid_from or current >= campaign.expires_at:
        raise InvestorError("disclosure_authorization_expired", "campaign is not currently active")
    actual_path = _db_path(session)
    if sample_env_id != campaign.sample_env_id or actual_path != campaign.database_path:
        raise InvestorError("disclosure_scope_forbidden", "campaign environment or SQLite path changed")
    grants = campaign.bound_grants
    if {"authorization_id": str(grant_id), "revision": grant_revision, "sample_env_id": sample_env_id} not in grants:
        raise InvestorError("disclosure_scope_forbidden", "grant revision is not bound to campaign")
    action = next((item for item in campaign.actions if item.action_id == action_id), None)
    if action is None or action.status != "approved":
        raise InvestorError("disclosure_scope_forbidden", "campaign action is not approved")
    if poll_id is not None:
        admission = await SupplementalUpdatesRepository(session).get_change_by_key(OWNER, f"p4:campaign:{campaign_id}:poll:{poll_id}:admitted")
        if admission is None or admission.payload.get("action_id") != action_id or admission.payload.get("grant_id") != str(grant_id) or admission.payload.get("grant_revision") != grant_revision:
            raise InvestorError("disclosure_scope_forbidden", "poll is not admitted for this action/grant")
    return action.model_dump(mode="json")


async def authorize_disclosure_campaign_poll(
    session: AsyncSession, *, grant: BackgroundDisclosureAuthorizationV2, poll_id: str,
    action_id: str, listing_ref: ObjectRef, window_start: datetime, window_end: datetime,
    sample_env_id: str, database_path: str | None = None, profile_hash: str,
    contract_hash: str, now: datetime | None = None,
) -> dict[str, Any]:
    current = _utc(now)
    await _begin_write(session)
    if grant.schema_version != 2 or grant.manual_only is not True or grant.status != "active":
        raise InvestorError("disclosure_scope_forbidden", "manual V2 grant required")
    if current < grant.valid_from or current >= grant.expires_at:
        raise InvestorError("disclosure_authorization_expired", "grant outside absolute validity")
    state, campaign = await _load(session, grant.campaign_id)
    action_data = await assert_campaign_action(
        session, campaign_id=grant.campaign_id, action_id=action_id,
        grant_id=grant.authorization_id, grant_revision=grant.revision, poll_id=None,
        sample_env_id=sample_env_id, now=current,
    )
    action = next(a for a in campaign.actions if a.action_id == action_id)
    if action.kind != "sample_poll":
        raise InvestorError("disclosure_scope_forbidden", "campaign action is not an admitted sample poll")
    if (
        action.listing_ref != listing_ref
        or action.window_start != window_start or action.window_end != window_end
        or action.profile_hash != profile_hash or action.contract_hash != contract_hash
        or grant.contract_hash != contract_hash
    ):
        raise InvestorError("disclosure_scope_forbidden", "poll differs from exact campaign action")
    prior = await SupplementalUpdatesRepository(session).get_change_by_key(OWNER, f"p4:campaign:{grant.campaign_id}:poll:{poll_id}:admitted")
    if prior:
        if prior.payload.get("action_id") != action_id or prior.payload.get("grant_id") != str(grant.authorization_id):
            raise InvestorError("idempotency_conflict", "poll id reused for another campaign action")
        return action_data
    action_prior = await SupplementalUpdatesRepository(session).get_change_by_key(
        OWNER, f"p4:campaign:{grant.campaign_id}:action:{action_id}:admitted",
    )
    if action_prior is not None and action_prior.payload.get("poll_id") != poll_id:
        raise InvestorError("disclosure_scope_forbidden", "approved action already belongs to another poll id")
    if campaign.active_poll is not None and campaign.active_poll.get("poll_id") != poll_id:
        raise InvestorError("capacity_blocked", "another campaign poll root is active")
    # Distinct P1/P2 operations are only admitted by their exact preapproved action.
    repo = SupplementalUpdatesRepository(session)
    await repo.append_change(
        OWNER, change_key=f"p4:campaign:{grant.campaign_id}:poll:{poll_id}:admitted", kind="source_attempt",
        payload={"record_type": "campaign_poll_admission_v2", "campaign_id": str(grant.campaign_id), "poll_id": poll_id,
                 "action_id": action_id, "grant_id": str(grant.authorization_id), "grant_revision": grant.revision,
                 "grant_hash": grant.authorization_hash,
                 "listing_ref": listing_ref.model_dump(mode="json"), "window_start": window_start.isoformat(),
                 "window_end": window_end.isoformat(), "sample_env_id": sample_env_id,
                 "database_path": _db_path(session), "profile_hash": profile_hash, "contract_hash": contract_hash},
        acquired_at=current, available_from=current, committed_at=current,
    )
    await repo.append_change(
        OWNER, change_key=f"p4:campaign:{grant.campaign_id}:poll:{poll_id}:reserved", kind="source_attempt",
        payload={"record_type": "campaign_poll_reservation_v2", "action_id": action_id, "http_reserved": 8,
                 "response_bytes_reserved": 64 * 1024 * 1024, "storage_bytes_reserved": 64 * 1024 * 1024},
        acquired_at=current, available_from=current, committed_at=current,
    )
    if action_prior is None:
        await repo.append_change(
            OWNER, change_key=f"p4:campaign:{grant.campaign_id}:action:{action_id}:admitted", kind="source_attempt",
            payload={"record_type": "campaign_action_admission_v2", "action_id": action_id, "poll_id": poll_id},
            acquired_at=current, available_from=current, committed_at=current,
        )
    state, campaign = await _load(session, grant.campaign_id)
    usage = dict(campaign.poll_usage)
    usage[poll_id] = usage.get(poll_id, {"action_id": action_id, "http_reserved": 0, "http_consumed": 0,
                                         "bytes_reserved": 0, "bytes_consumed": 0, "storage_reserved": 0,
                                         "storage_consumed": 0, "complete": False})
    await _save(session, state, campaign.model_copy(update={
        "active_poll": {"poll_id": poll_id, "action_id": action_id}, "poll_usage": usage,
    }), now=current)
    return action_data


async def assert_campaign_attempt(
    session: AsyncSession, *, grant: BackgroundDisclosureAuthorizationV2,
    identity: DisclosureAttemptIdentity, now: datetime | None = None,
) -> dict[str, Any]:
    campaign_id = grant.campaign_id
    repo = SupplementalUpdatesRepository(session)
    admission = await repo.get_change_by_key(OWNER, f"p4:campaign:{campaign_id}:poll:{identity.poll_id}:admitted")
    if admission is None:
        raise InvestorError("disclosure_scope_forbidden", "attempt poll has no campaign admission")
    payload = admission.payload
    if (
        payload.get("grant_id") != str(grant.authorization_id)
        or payload.get("grant_revision") != grant.revision
        or payload.get("grant_hash") != grant.authorization_hash
        or payload.get("listing_ref") != identity.listing_ref.model_dump(mode="json")
        or payload.get("window_start") != identity.window_start.isoformat()
        or payload.get("window_end") != identity.window_end.isoformat()
        or payload.get("contract_hash") != identity.contract_hash
        or payload.get("database_path") != _db_path(session)
        or payload.get("sample_env_id") != grant.sample_env_id
    ):
        raise InvestorError("disclosure_scope_forbidden", "attempt differs from campaign binding")
    _state, campaign = await _load(session, campaign_id, lock=False)
    usage = campaign.poll_usage.get(identity.poll_id, {})
    if (campaign.active_poll is None or campaign.active_poll.get("poll_id") != identity.poll_id
            or usage.get("complete") is True):
        raise InvestorError("disclosure_scope_forbidden", "poll root is no longer open for HTTP")
    action = await assert_campaign_action(
        session, campaign_id=campaign_id, action_id=str(payload["action_id"]),
        grant_id=grant.authorization_id, grant_revision=grant.revision, poll_id=identity.poll_id,
        sample_env_id=grant.sample_env_id, now=now,
    )
    if action.get("profile_hash") != payload.get("profile_hash"):
        raise InvestorError("disclosure_scope_forbidden", "profile binding changed")
    if action.get("url_allowlist") and identity.url not in action["url_allowlist"]:
        raise InvestorError("disclosure_scope_forbidden", "attempt URL is outside exact campaign allowlist")
    return action


async def reserve_campaign_action_hop(
    session: AsyncSession, *, campaign_id: UUID, action_id: str, operation_key: str,
    sample_env_id: str, url: str, request_bytes: int, response_bytes_limit: int,
    storage_bytes_limit: int, purpose: Literal["terms", "raw_probe"],
    body_hash: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Reserve a single approved S0 terms/raw probe hop in the campaign ledger."""
    current = _utc(now)
    await _begin_write(session)
    state, campaign = await _load(session, campaign_id)
    action = next((item for item in campaign.actions if item.action_id == action_id), None)
    if action is None or action.status != "approved" or action.kind != purpose:
        raise InvestorError("disclosure_scope_forbidden", "S0 action is not explicitly approved")
    if current < campaign.valid_from or current >= campaign.expires_at or campaign.status != "active":
        raise InvestorError("disclosure_authorization_expired", "campaign is not currently active")
    if sample_env_id != campaign.sample_env_id or _db_path(session) != campaign.database_path:
        raise InvestorError("disclosure_scope_forbidden", "campaign environment or SQLite path changed")
    if url not in action.url_allowlist or request_bytes < 0 or response_bytes_limit < 1 or storage_bytes_limit < 0:
        raise InvestorError("disclosure_scope_forbidden", "S0 request exceeds exact action scope")
    if action.body_hash != body_hash:
        raise InvestorError("disclosure_scope_forbidden", "S0 request body differs from approved hash")
    if response_bytes_limit > action.response_byte_cap or storage_bytes_limit > action.storage_byte_cap:
        raise InvestorError("capacity_blocked", "S0 request exceeds approved action byte cap")
    _assert_disk_available(session, response_reserve=response_bytes_limit, storage_reserve=storage_bytes_limit)
    if request_bytes > action.request_byte_cap:
        raise InvestorError("capacity_blocked", "S0 request exceeds approved request-body cap")
    if action.method == "GET" and (request_bytes != 0 or body_hash is not None):
        raise InvestorError("disclosure_scope_forbidden", "GET S0 action cannot carry a request body")
    repo = SupplementalUpdatesRepository(session)
    active_key = f"s0:{action_id}"
    terminal_action = await repo.get_change_by_key(
        OWNER, f"p4:campaign:{campaign_id}:action:{action_id}:terminal",
    )
    if terminal_action is not None:
        raise InvestorError("disclosure_scope_forbidden", "S0 action is already terminal")
    if campaign.active_poll is not None and campaign.active_poll.get("poll_id") != active_key:
        raise InvestorError("capacity_blocked", "another campaign root is active")
    permit_id = uuid5(NAMESPACE_URL, f"p4-campaign:{campaign_id}:s0:{action_id}:{operation_key}")
    key = f"p4:campaign-hop:{permit_id}:reserved"
    spec = {"campaign_id": str(campaign_id), "poll_id": active_key, "action_id": action_id,
            "operation_key": operation_key, "request_bytes": response_bytes_limit,
            "request_body_bytes": request_bytes, "storage_bytes": storage_bytes_limit,
            "url": url, "purpose": purpose, "body_hash": body_hash,
            "sample_env_id": sample_env_id}
    prior = await repo.get_change_by_key(OWNER, key)
    if prior is not None:
        if prior.payload.get("spec") != spec:
            raise InvestorError("idempotency_conflict", "S0 operation key reused for another request")
        if (await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:sent") is not None
                or await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:result") is not None
                or await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:canceled") is not None):
            raise InvestorError("idempotency_conflict", "S0 operation is already sent or terminal")
        return {"permit_id": str(permit_id), "state": "reserved", **spec}
    if action.http_cap < 1:
        raise InvestorError("capacity_blocked", "S0 action HTTP cap exhausted")
    action_usage = dict(campaign.action_usage)
    usage = dict(action_usage.get(action_id, {"http_reserved": 0, "http_consumed": 0,
        "bytes_reserved": 0, "bytes_consumed": 0, "storage_reserved": 0, "storage_consumed": 0}))
    if usage["http_reserved"] + usage["http_consumed"] + 1 > action.http_cap:
        raise InvestorError("capacity_blocked", "S0 action HTTP cap exhausted")
    if usage["bytes_reserved"] + usage["bytes_consumed"] + response_bytes_limit > action.response_byte_cap:
        raise InvestorError("capacity_blocked", "S0 action response-byte cap exhausted")
    if usage["storage_reserved"] + usage["storage_consumed"] + storage_bytes_limit > action.storage_byte_cap:
        raise InvestorError("capacity_blocked", "S0 action storage cap exhausted")
    if campaign.http_consumed + campaign.http_reserved + 1 > campaign.http_cap:
        raise InvestorError("capacity_blocked", "campaign HTTP cap exhausted")
    if campaign.response_bytes_consumed + campaign.response_bytes_reserved + response_bytes_limit > campaign.response_bytes_cap:
        raise InvestorError("capacity_blocked", "campaign response-byte cap exhausted")
    if campaign.storage_bytes_consumed + campaign.storage_bytes_reserved + storage_bytes_limit > campaign.storage_bytes_cap:
        raise InvestorError("capacity_blocked", "campaign storage cap exhausted")
    if (campaign.s0_http_consumed + campaign.s0_http_reserved + 1 > campaign.s0_http_cap
            or campaign.s0_response_bytes_consumed + campaign.s0_response_bytes_reserved + response_bytes_limit > campaign.s0_response_bytes_cap
            or campaign.s0_storage_bytes_consumed + campaign.s0_storage_bytes_reserved + storage_bytes_limit > campaign.s0_storage_bytes_cap):
        raise InvestorError("capacity_blocked", "campaign S0 subcap exhausted")
    from investment_agent.supplemental_rules.disclosure_probe_quota import raw_reserve

    await raw_reserve(session, permit_id=permit_id, spec=spec, now=current)
    await repo.append_change(OWNER, change_key=key, kind="source_attempt",
        payload={"record_type": "campaign_hop_v2", "stage": "reserved", "spec": spec,
                 "reserved_at": current.isoformat(), "is_s0": True, "is_sample": False, "is_action": True},
        acquired_at=current, available_from=current, committed_at=current)
    updated = campaign.model_copy(update={
        "revision": campaign.revision + 1, "http_reserved": campaign.http_reserved + 1,
        "response_bytes_reserved": campaign.response_bytes_reserved + response_bytes_limit,
        "storage_bytes_reserved": campaign.storage_bytes_reserved + storage_bytes_limit,
        "s0_http_reserved": campaign.s0_http_reserved + 1,
        "s0_response_bytes_reserved": campaign.s0_response_bytes_reserved + response_bytes_limit,
        "s0_storage_bytes_reserved": campaign.s0_storage_bytes_reserved + storage_bytes_limit,
        "action_usage": {**action_usage, action_id: {
            **usage, "http_reserved": usage["http_reserved"] + 1,
            "bytes_reserved": usage["bytes_reserved"] + response_bytes_limit,
            "storage_reserved": usage["storage_reserved"] + storage_bytes_limit,
        }},
        "active_poll": {"poll_id": active_key, "action_id": action_id},
    })
    await _save(session, state, updated, now=current)
    return {"permit_id": str(permit_id), "state": "reserved", **spec}


async def reserve_campaign_hop(
    session: AsyncSession, *, grant: BackgroundDisclosureAuthorizationV2,
    identity: DisclosureAttemptIdentity, permit_id: UUID, response_bytes_limit: int,
    storage_bytes_limit: int, now: datetime | None = None,
) -> dict[str, Any]:
    current = _utc(now)
    await _begin_write(session)
    action = await assert_campaign_attempt(session, grant=grant, identity=identity, now=current)
    state, campaign = await _load(session, grant.campaign_id)
    key = f"p4:campaign-hop:{permit_id}:reserved"
    repo = SupplementalUpdatesRepository(session)
    prior = await repo.get_change_by_key(OWNER, key)
    spec = {"campaign_id": str(grant.campaign_id), "poll_id": identity.poll_id, "action_id": action["action_id"],
            "grant_id": str(grant.authorization_id), "grant_revision": grant.revision,
            "request_bytes": response_bytes_limit, "storage_bytes": storage_bytes_limit,
            "sample_env_id": grant.sample_env_id, "url": identity.url,
            "profile_hash": action.get("profile_hash"), "contract_hash": identity.contract_hash}
    if prior is not None:
        if prior.payload.get("spec") != spec:
            raise InvestorError("idempotency_conflict", "campaign permit reused with different reservation")
        if (await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:sent") is not None
                or await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:result") is not None
                or await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:canceled") is not None):
            raise InvestorError("idempotency_conflict", "campaign hop is already sent or terminal")
        return dict(prior.payload)
    if campaign.active_poll not in (None, {"poll_id": identity.poll_id, "action_id": action["action_id"]}):
        raise InvestorError("capacity_blocked", "another campaign poll is active")
    request_total = campaign.http_consumed + campaign.http_reserved + 1
    bytes_total = campaign.response_bytes_consumed + campaign.response_bytes_reserved + response_bytes_limit
    storage_total = campaign.storage_bytes_consumed + campaign.storage_bytes_reserved + storage_bytes_limit
    if request_total > campaign.http_cap or bytes_total > campaign.response_bytes_cap or storage_total > campaign.storage_bytes_cap:
        raise InvestorError("capacity_blocked", "campaign cumulative budget exhausted")
    _assert_disk_available(session, response_reserve=response_bytes_limit, storage_reserve=storage_bytes_limit)
    is_s0 = action["kind"] in ("terms", "raw_probe")
    if is_s0 and (
        campaign.s0_http_consumed + campaign.s0_http_reserved + 1 > campaign.s0_http_cap
        or campaign.s0_response_bytes_consumed + campaign.s0_response_bytes_reserved + response_bytes_limit > campaign.s0_response_bytes_cap
        or campaign.s0_storage_bytes_consumed + campaign.s0_storage_bytes_reserved + storage_bytes_limit > campaign.s0_storage_bytes_cap
    ):
        raise InvestorError("capacity_blocked", "campaign S0 subcap exhausted")
    is_sample = action["kind"] == "sample_poll"
    usage = dict(campaign.poll_usage)
    poll_usage = dict(usage.get(identity.poll_id, {"action_id": action["action_id"], "http_reserved": 0,
        "http_consumed": 0, "bytes_reserved": 0, "bytes_consumed": 0, "storage_reserved": 0,
        "storage_consumed": 0, "complete": False}))
    if is_sample:
        if campaign.sample_http_consumed + campaign.sample_http_reserved + 1 > campaign.sample_http_cap:
            raise InvestorError("capacity_blocked", "campaign sample HTTP subcap exhausted")
        if campaign.sample_response_bytes_consumed + campaign.sample_response_bytes_reserved + response_bytes_limit > campaign.sample_response_bytes_cap:
            raise InvestorError("capacity_blocked", "campaign sample response-byte subcap exhausted")
        if (poll_usage["http_consumed"] + poll_usage["http_reserved"] + 1 > min(8, action["http_cap"])
                or poll_usage["bytes_consumed"] + poll_usage["bytes_reserved"] + response_bytes_limit
                > min(64 * 1024 * 1024, action["response_byte_cap"])):
            raise InvestorError("capacity_blocked", "campaign per-poll cap exhausted")
    receipt = await repo.append_change(
        OWNER, change_key=key, kind="source_attempt",
        payload={"record_type": "campaign_hop_v2", "stage": "reserved", "spec": spec,
                 "reserved_at": current.isoformat(), "is_s0": is_s0, "is_sample": is_sample},
        acquired_at=current, available_from=current, committed_at=current,
    )
    updated = campaign.model_copy(update={
        "revision": campaign.revision + 1, "http_reserved": campaign.http_reserved + 1,
        "response_bytes_reserved": campaign.response_bytes_reserved + response_bytes_limit,
        "storage_bytes_reserved": campaign.storage_bytes_reserved + storage_bytes_limit,
        "active_poll": {"poll_id": identity.poll_id, "action_id": action["action_id"]},
        "s0_http_reserved": campaign.s0_http_reserved + (1 if is_s0 else 0),
        "s0_response_bytes_reserved": campaign.s0_response_bytes_reserved + (response_bytes_limit if is_s0 else 0),
        "s0_storage_bytes_reserved": campaign.s0_storage_bytes_reserved + (storage_bytes_limit if is_s0 else 0),
        "sample_http_reserved": campaign.sample_http_reserved + (1 if is_sample else 0),
        "sample_response_bytes_reserved": campaign.sample_response_bytes_reserved + (response_bytes_limit if is_sample else 0),
        "poll_usage": {**usage, identity.poll_id: {
            **poll_usage, "http_reserved": poll_usage["http_reserved"] + (1 if is_sample else 0),
            "bytes_reserved": poll_usage["bytes_reserved"] + (response_bytes_limit if is_sample else 0),
            "storage_reserved": poll_usage["storage_reserved"] + (storage_bytes_limit if is_sample else 0),
        }},
    })
    await _save(session, state, updated, now=current)
    return {"permit_id": str(permit_id), "receipt_sequence": receipt.sequence, **spec, "state": "reserved"}


async def _hop_phase(
    session: AsyncSession, permit_id: UUID, stage: str,
) -> tuple[SupplementalUpdatesRepository, SupplementalChangeReceipt, dict[str, Any]]:
    repo = SupplementalUpdatesRepository(session)
    reservation = await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:reserved")
    if reservation is None or reservation.payload.get("record_type") != "campaign_hop_v2":
        raise InvestorError("not_found", "campaign hop reservation missing")
    return repo, reservation, reservation.payload


async def assert_campaign_hop_current(
    session: AsyncSession, *, permit_id: UUID, sample_env_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Recheck action, authority, environment, and database immediately before use."""
    current = _utc(now)
    await _begin_write(session)
    _repo, _reservation, reserved_payload = await _hop_phase(session, permit_id, "current")
    spec = reserved_payload["spec"]
    campaign_id = UUID(spec["campaign_id"])
    _state, campaign = await _load(session, campaign_id, lock=False)
    env = sample_env_id or spec.get("sample_env_id")
    if (campaign.status != "active" or current < campaign.valid_from or current >= campaign.expires_at
            or env != campaign.sample_env_id or _db_path(session) != campaign.database_path):
        raise InvestorError("disclosure_authorization_expired", "campaign action/environment is no longer active")
    action = next((a for a in campaign.actions if a.action_id == spec.get("action_id")), None)
    if action is None or action.status != "approved" or spec.get("url") not in action.url_allowlist:
        raise InvestorError("disclosure_scope_forbidden", "campaign action or exact URL is no longer approved")
    if reserved_payload.get("is_action"):
        if (action.kind != spec.get("purpose") or action.body_hash != spec.get("body_hash")
                or action.method not in {"GET", "POST"}):
            raise InvestorError("disclosure_scope_forbidden", "S0 action method/body approval changed")
    else:
        if (action.kind != "sample_poll" or action.profile_hash != spec.get("profile_hash")
                or action.contract_hash != spec.get("contract_hash")):
            raise InvestorError("disclosure_scope_forbidden", "sample action/profile approval changed")
        admission = await SupplementalUpdatesRepository(session).get_change_by_key(
            OWNER, f"p4:campaign:{campaign_id}:poll:{spec['poll_id']}:admitted",
        )
        if (admission is None or admission.payload.get("action_id") != spec["action_id"]
                or admission.payload.get("grant_id") != spec.get("grant_id")
                or admission.payload.get("grant_revision") != spec.get("grant_revision")):
            raise InvestorError("disclosure_scope_forbidden", "sample poll admission is missing")
        from investment_agent.supplemental_rules.disclosure_authorization import (
            read_authorization_snapshot,
        )

        grant = await read_authorization_snapshot(session, UUID(spec["grant_id"]))
        if (not isinstance(grant, BackgroundDisclosureAuthorizationV2)
                or grant.status != "active" or grant.revision != spec["grant_revision"]
                or current < grant.valid_from or current >= grant.expires_at
                or grant.sample_env_id != env):
            raise InvestorError("disclosure_authorization_expired", "sample grant revision is no longer active")
    return action.model_dump(mode="json")


async def record_campaign_hop_quarantine(
    session: AsyncSession, *, permit_id: UUID, status: Literal["stored", "failed"],
    reason: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    current = _utc(now)
    await _begin_write(session)
    repo, reservation, payload = await _hop_phase(session, permit_id, "quarantine")
    if not payload.get("is_action"):
        raise InvestorError("disclosure_scope_forbidden", "quarantine state is only for raw S0 captures")
    result = await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:result")
    if result is None:
        raise InvestorError("invalid_reference", "raw response must be settled before quarantine state")
    if result.payload.get("quarantine_state") == status:
        return {"permit_id": str(permit_id), "state": status}
    other_status = "failed" if status == "stored" else "stored"
    if await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:quarantine:{other_status}"):
        raise InvestorError("idempotency_conflict", "raw quarantine result is already final")
    key = f"p4:campaign-hop:{permit_id}:quarantine:{status}"
    prior = await repo.get_change_by_key(OWNER, key)
    if prior is not None:
        return {"permit_id": str(permit_id), "state": status}
    await repo.append_change(
        OWNER, change_key=key, kind="source_attempt",
        payload={"record_type": "campaign_quarantine_v2", "permit_id": str(permit_id),
                 "reservation_sequence": reservation.sequence, "status": status,
                 "reason": (reason or "")[:256]},
        acquired_at=current, available_from=current, committed_at=current,
    )
    return {"permit_id": str(permit_id), "state": status}


async def read_campaign_hop_quarantine(session: AsyncSession, *, permit_id: UUID) -> dict[str, Any]:
    repo = SupplementalUpdatesRepository(session)
    failed = await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:quarantine:failed")
    stored = await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:quarantine:stored")
    result = await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:result")
    return {"permit_id": str(permit_id), "state": "failed" if failed else "stored" if stored else
            (result.payload.get("quarantine_state", "pending") if result else "not_settled")}


async def mark_campaign_hop_sent(session: AsyncSession, *, permit_id: UUID, now: datetime | None = None) -> dict[str, Any]:
    current = _utc(now)
    await _begin_write(session)
    repo, reservation, payload = await _hop_phase(session, permit_id, "sent")
    sent_key = f"p4:campaign-hop:{permit_id}:sent"
    prior = await repo.get_change_by_key(OWNER, sent_key)
    if prior is not None:
        raise InvestorError("idempotency_conflict", "campaign HTTP permit was already sent; do not transmit again")
    if (await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:canceled") is not None
            or await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:result") is not None):
        raise InvestorError("invalid_reference", "terminal campaign hop cannot be sent")
    spec = payload["spec"]
    _assert_disk_available(
        session, response_reserve=spec["request_bytes"], storage_reserve=spec["storage_bytes"],
    )
    await assert_campaign_hop_current(
        session, permit_id=permit_id, sample_env_id=spec.get("sample_env_id"), now=current,
    )
    if payload.get("is_action"):
        from investment_agent.supplemental_rules.disclosure_probe_quota import raw_mark_sent

        await raw_mark_sent(session, permit_id=permit_id, spec=spec, now=current)
    state, campaign = await _load(session, UUID(spec["campaign_id"]))
    if campaign.status != "active" or current >= campaign.expires_at:
        raise InvestorError("disclosure_authorization_expired", "campaign expired before HTTP send")
    sent_receipt = await repo.append_change(OWNER, change_key=sent_key, kind="source_attempt",
        payload={"record_type": "campaign_hop_v2", "stage": "sent", "reservation_sequence": reservation.sequence},
        acquired_at=current, available_from=current, committed_at=current)
    await _save(session, state, campaign, now=current)
    from investment_agent.supplemental_rules.disclosure_campaign_anchor import (
        advance_campaign_anchor,
    )

    await advance_campaign_anchor(session, UUID(spec["campaign_id"]), sent_receipt)
    return {"permit_id": str(permit_id), "state": "sent"}


async def settle_campaign_hop(
    session: AsyncSession, *, permit_id: UUID, response_bytes: int | None,
    storage_bytes: int, result_code: str, now: datetime | None = None,
) -> dict[str, Any]:
    current = _utc(now)
    await _begin_write(session)
    repo, reservation, reserved_payload = await _hop_phase(session, permit_id, "settle")
    spec = reserved_payload["spec"]
    result_key = f"p4:campaign-hop:{permit_id}:result"
    prior = await repo.get_change_by_key(OWNER, result_key)
    settlement = {"response_bytes": response_bytes, "storage_bytes": storage_bytes, "result_code": result_code}
    if prior is not None:
        if prior.payload.get("settlement") != settlement:
            raise InvestorError("idempotency_conflict", "campaign hop settlement replay mismatch")
        return dict(prior.payload)
    sent = await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:sent")
    if sent is None:
        raise InvestorError("invalid_reference", "unsent campaign hop cannot settle")
    response_cap = spec["request_bytes"]
    # Unknown sent outcomes consume their full reservation and are never refunded.
    actual_response = response_cap if response_bytes is None else response_bytes
    if actual_response < 0 or actual_response > response_cap or storage_bytes < 0 or storage_bytes > spec["storage_bytes"]:
        raise InvestorError("capacity_blocked", "settlement exceeds reserved campaign cap")
    state, campaign = await _load(session, UUID(spec["campaign_id"]))
    actual_storage = spec["storage_bytes"] if response_bytes is None else storage_bytes
    if reserved_payload.get("is_action"):
        from investment_agent.supplemental_rules.disclosure_probe_quota import raw_settle

        await raw_settle(session, permit_id=permit_id, spec=spec, response_bytes=response_bytes,
                         storage_bytes=actual_storage, now=current)
    await repo.append_change(OWNER, change_key=result_key, kind="source_attempt",
        payload={"record_type": "campaign_hop_v2", "stage": "unknown" if response_bytes is None else "result",
                 "reservation_sequence": reservation.sequence, "settlement": settlement,
                 "charged_http": 1, "charged_response_bytes": actual_response, "charged_storage_bytes": actual_storage,
                 **({"quarantine_state": "pending"} if reserved_payload.get("is_action") else {})},
        acquired_at=current, available_from=current, committed_at=current)
    is_s0 = bool(reserved_payload.get("is_s0"))
    is_sample = bool(reserved_payload.get("is_sample"))
    is_action = bool(reserved_payload.get("is_action"))
    usage = dict(campaign.poll_usage)
    poll_usage = dict(usage.get(spec["poll_id"], {}))
    action_usage = dict(campaign.action_usage)
    action_charge = dict(action_usage.get(spec["action_id"], {}))
    if (campaign.http_reserved < 1 or campaign.response_bytes_reserved < response_cap
            or campaign.storage_bytes_reserved < spec["storage_bytes"]):
        raise InvestorError("invalid_reference", "campaign reservation accounting is inconsistent")
    if is_s0 and (campaign.s0_http_reserved < 1 or campaign.s0_response_bytes_reserved < response_cap
                  or campaign.s0_storage_bytes_reserved < spec["storage_bytes"]):
        raise InvestorError("invalid_reference", "campaign S0 reservation accounting is inconsistent")
    if is_sample and (campaign.sample_http_reserved < 1 or campaign.sample_response_bytes_reserved < response_cap
                      or poll_usage.get("http_reserved", 0) < 1 or poll_usage.get("bytes_reserved", 0) < response_cap
                      or poll_usage.get("storage_reserved", 0) < spec["storage_bytes"]):
        raise InvestorError("invalid_reference", "campaign sample reservation accounting is inconsistent")
    if is_action and (action_charge.get("http_reserved", 0) < 1 or action_charge.get("bytes_reserved", 0) < response_cap
                      or action_charge.get("storage_reserved", 0) < spec["storage_bytes"]):
        raise InvestorError("invalid_reference", "campaign action reservation accounting is inconsistent")
    if is_sample:
        poll_usage.update({"http_reserved": poll_usage["http_reserved"] - 1,
                           "http_consumed": poll_usage.get("http_consumed", 0) + 1,
                           "bytes_reserved": poll_usage["bytes_reserved"] - response_cap,
                           "bytes_consumed": poll_usage.get("bytes_consumed", 0) + actual_response,
                           "storage_reserved": poll_usage["storage_reserved"] - spec["storage_bytes"],
                           "storage_consumed": poll_usage.get("storage_consumed", 0) + actual_storage})
    if is_action:
        action_charge.update({"http_reserved": action_charge["http_reserved"] - 1,
                              "http_consumed": action_charge.get("http_consumed", 0) + 1,
                              "bytes_reserved": action_charge["bytes_reserved"] - response_cap,
                              "bytes_consumed": action_charge.get("bytes_consumed", 0) + actual_response,
                              "storage_reserved": action_charge["storage_reserved"] - spec["storage_bytes"],
                              "storage_consumed": action_charge.get("storage_consumed", 0) + actual_storage})
    action_terminal = bool(is_action and action_charge.get("http_reserved", 0) == 0)
    if action_terminal:
        await repo.append_change(
            OWNER,
            change_key=f"p4:campaign:{spec['campaign_id']}:action:{spec['action_id']}:terminal",
            kind="source_attempt",
            payload={"record_type": "campaign_action_terminal_v2", "campaign_id": spec["campaign_id"],
                     "action_id": spec["action_id"], "permit_id": str(permit_id),
                     "terminal_stage": "settled"},
            acquired_at=current, available_from=current, committed_at=current,
        )
    updated = campaign.model_copy(update={
        "revision": campaign.revision + 1, "http_reserved": campaign.http_reserved - 1,
        "http_consumed": campaign.http_consumed + 1,
        "response_bytes_reserved": campaign.response_bytes_reserved - response_cap,
        "response_bytes_consumed": campaign.response_bytes_consumed + actual_response,
        "storage_bytes_reserved": campaign.storage_bytes_reserved - spec["storage_bytes"],
        "storage_bytes_consumed": campaign.storage_bytes_consumed + actual_storage,
        "s0_http_reserved": campaign.s0_http_reserved - (1 if is_s0 else 0),
        "s0_response_bytes_reserved": campaign.s0_response_bytes_reserved - (response_cap if is_s0 else 0),
        "s0_storage_bytes_reserved": campaign.s0_storage_bytes_reserved - (spec["storage_bytes"] if is_s0 else 0),
        "s0_http_consumed": campaign.s0_http_consumed + (1 if is_s0 else 0),
        "s0_response_bytes_consumed": campaign.s0_response_bytes_consumed + (actual_response if is_s0 else 0),
        "s0_storage_bytes_consumed": campaign.s0_storage_bytes_consumed + (actual_storage if is_s0 else 0),
        "sample_http_reserved": campaign.sample_http_reserved - (1 if is_sample else 0),
        "sample_response_bytes_reserved": campaign.sample_response_bytes_reserved - (response_cap if is_sample else 0),
        "sample_http_consumed": campaign.sample_http_consumed + (1 if is_sample else 0),
        "sample_response_bytes_consumed": campaign.sample_response_bytes_consumed + (actual_response if is_sample else 0),
        "poll_usage": {**usage, **({spec["poll_id"]: poll_usage} if is_sample else {})},
        "action_usage": {**action_usage, **({spec["action_id"]: action_charge} if is_action else {})},
        "active_poll": None if action_terminal and campaign.active_poll == {
            "poll_id": spec["poll_id"], "action_id": spec["action_id"],
        } else campaign.active_poll,
    })
    await _save(session, state, updated, now=current)
    return {"permit_id": str(permit_id), "state": "unknown" if response_bytes is None else "result",
            "http_consumed": 1, "response_bytes_consumed": actual_response, "storage_bytes_consumed": actual_storage}


async def cancel_campaign_hop(session: AsyncSession, *, permit_id: UUID, now: datetime | None = None) -> dict[str, Any]:
    current = _utc(now)
    await _begin_write(session)
    repo, reservation, reserved_payload = await _hop_phase(session, permit_id, "cancel")
    cancel_key = f"p4:campaign-hop:{permit_id}:canceled"
    prior = await repo.get_change_by_key(OWNER, cancel_key)
    if prior:
        return dict(prior.payload)
    if await repo.get_change_by_key(OWNER, f"p4:campaign-hop:{permit_id}:sent") is not None:
        raise InvestorError("invalid_reference", "sent campaign hop cannot be canceled")
    spec = reserved_payload["spec"]
    if reserved_payload.get("is_action"):
        from investment_agent.supplemental_rules.disclosure_probe_quota import raw_cancel

        await raw_cancel(session, permit_id=permit_id, spec=spec, now=current)
    state, campaign = await _load(session, UUID(spec["campaign_id"]))
    await repo.append_change(OWNER, change_key=cancel_key, kind="source_attempt",
        payload={"record_type": "campaign_hop_v2", "stage": "canceled", "reservation_sequence": reservation.sequence},
        acquired_at=current, available_from=current, committed_at=current)
    cap = spec["request_bytes"]
    is_s0 = bool(reserved_payload.get("is_s0"))
    is_sample = bool(reserved_payload.get("is_sample"))
    is_action = bool(reserved_payload.get("is_action"))
    usage = dict(campaign.poll_usage)
    poll_usage = dict(usage.get(spec["poll_id"], {}))
    action_usage = dict(campaign.action_usage)
    action_charge = dict(action_usage.get(spec["action_id"], {}))
    if campaign.http_reserved < 1 or campaign.response_bytes_reserved < cap or campaign.storage_bytes_reserved < spec["storage_bytes"]:
        raise InvestorError("invalid_reference", "campaign reservation accounting is inconsistent")
    if is_s0 and (campaign.s0_http_reserved < 1 or campaign.s0_response_bytes_reserved < cap
                  or campaign.s0_storage_bytes_reserved < spec["storage_bytes"]):
        raise InvestorError("invalid_reference", "campaign S0 reservation accounting is inconsistent")
    if is_sample and (campaign.sample_http_reserved < 1 or campaign.sample_response_bytes_reserved < cap
                      or poll_usage.get("http_reserved", 0) < 1 or poll_usage.get("bytes_reserved", 0) < cap
                      or poll_usage.get("storage_reserved", 0) < spec["storage_bytes"]):
        raise InvestorError("invalid_reference", "campaign sample reservation accounting is inconsistent")
    if is_sample:
        poll_usage.update({"http_reserved": poll_usage["http_reserved"] - 1,
                           "bytes_reserved": poll_usage["bytes_reserved"] - cap,
                           "storage_reserved": poll_usage["storage_reserved"] - spec["storage_bytes"]})
    if is_action and (action_charge.get("http_reserved", 0) < 1 or action_charge.get("bytes_reserved", 0) < cap
                      or action_charge.get("storage_reserved", 0) < spec["storage_bytes"]):
        raise InvestorError("invalid_reference", "campaign action reservation accounting is inconsistent")
    if is_action:
        action_charge.update({"http_reserved": action_charge["http_reserved"] - 1,
                              "bytes_reserved": action_charge["bytes_reserved"] - cap,
                              "storage_reserved": action_charge["storage_reserved"] - spec["storage_bytes"]})
    updated = campaign.model_copy(update={
        "revision": campaign.revision + 1, "http_reserved": campaign.http_reserved - 1,
        "response_bytes_reserved": campaign.response_bytes_reserved - cap,
        "storage_bytes_reserved": campaign.storage_bytes_reserved - spec["storage_bytes"],
        "s0_http_reserved": campaign.s0_http_reserved - (1 if is_s0 else 0),
        "s0_response_bytes_reserved": campaign.s0_response_bytes_reserved - (cap if is_s0 else 0),
        "s0_storage_bytes_reserved": campaign.s0_storage_bytes_reserved - (spec["storage_bytes"] if is_s0 else 0),
        "sample_http_reserved": campaign.sample_http_reserved - (1 if is_sample else 0),
        "sample_response_bytes_reserved": campaign.sample_response_bytes_reserved - (cap if is_sample else 0),
        "poll_usage": {**usage, **({spec["poll_id"]: poll_usage} if is_sample else {})},
        "action_usage": {**action_usage, **({spec["action_id"]: action_charge} if is_action else {})},
        "active_poll": None if is_action and action_charge.get("http_reserved", 0) == 0
            and campaign.active_poll == {
            "poll_id": spec["poll_id"], "action_id": spec["action_id"],
        } else campaign.active_poll,
    })
    await _save(session, state, updated, now=current)
    return {"permit_id": str(permit_id), "state": "canceled"}


async def admit_manual_recovery(
    session: AsyncSession, *, campaign_id: UUID, poll_id: str, action_id: str,
    grant_id: UUID, grant_revision: int, sample_env_id: str, now: datetime | None = None,
) -> dict[str, Any]:
    _state, campaign = await _load(session, campaign_id, lock=False)
    if sample_env_id != campaign.sample_env_id or _db_path(session) != campaign.database_path:
        raise InvestorError("disclosure_scope_forbidden", "recovery environment binding mismatch")
    binding = {"authorization_id": str(grant_id), "revision": grant_revision, "sample_env_id": sample_env_id}
    if binding not in campaign.bound_grants:
        raise InvestorError("disclosure_scope_forbidden", "recovery grant revision was never bound")
    admission = await SupplementalUpdatesRepository(session).get_change_by_key(
        OWNER, f"p4:campaign:{campaign_id}:poll:{poll_id}:admitted",
    )
    if admission is None or admission.payload.get("action_id") != action_id:
        raise InvestorError("disclosure_scope_forbidden", "recovery poll/action mapping is missing")
    action = next((a.model_dump(mode="json") for a in campaign.actions if a.action_id == action_id), None)
    if action is None:
        raise InvestorError("invalid_reference", "recovery action is missing")
    return {"campaign_id": str(campaign_id), "poll_id": poll_id, "action_id": action_id,
            "recovery_only": True, "allow_network": False, "action": action}


async def authorize_campaign_recovery(
    session: AsyncSession, *, campaign_id: UUID, poll_id: str, action_id: str,
    grant_id: UUID, grant_revision: int, sample_env_id: str,
    recovery_approval_ref: str, operation_id: UUID, now: datetime | None = None,
) -> dict[str, Any]:
    """Record one narrowly scoped recovery authorization; it grants no HTTP."""
    current = _utc(now)
    await _begin_write(session)
    _state, campaign = await _load(session, campaign_id)
    if not recovery_approval_ref:
        raise InvestorError("disclosure_scope_forbidden", "recovery requires an explicit approval reference")
    if sample_env_id != campaign.sample_env_id or _db_path(session) != campaign.database_path:
        raise InvestorError("disclosure_scope_forbidden", "recovery environment or SQLite path changed")
    admission = await SupplementalUpdatesRepository(session).get_change_by_key(
        OWNER, f"p4:campaign:{campaign_id}:poll:{poll_id}:admitted",
    )
    if admission is None:
        raise InvestorError("disclosure_scope_forbidden", "original poll admission is missing")
    original = admission.payload
    if (original.get("action_id") != action_id or original.get("grant_id") != str(grant_id)
            or original.get("grant_revision") != grant_revision
            or original.get("sample_env_id") != sample_env_id
            or original.get("database_path") != campaign.database_path):
        raise InvestorError("disclosure_scope_forbidden", "recovery differs from original poll authority")
    payload = {
        "record_type": "campaign_recovery_authorization_v2", "campaign_id": str(campaign_id),
        "poll_id": poll_id, "action_id": action_id, "grant_id": str(grant_id),
        "grant_revision": grant_revision, "sample_env_id": sample_env_id,
        "database_path": campaign.database_path,
        "listing_ref": original.get("listing_ref"), "window_start": original.get("window_start"),
        "window_end": original.get("window_end"), "profile_hash": original.get("profile_hash"),
        "contract_hash": original.get("contract_hash"), "approval_ref": recovery_approval_ref,
        "operation_id": str(operation_id), "network_allowed": False,
    }
    repo = SupplementalUpdatesRepository(session)
    key = f"p4:campaign:{campaign_id}:poll:{poll_id}:recovery:{operation_id}"
    prior = await repo.get_change_by_key(OWNER, key)
    if prior is not None:
        if prior.payload != payload:
            raise InvestorError("idempotency_conflict", "recovery operation id was reused")
        return {"campaign_id": str(campaign_id), "poll_id": poll_id, "action_id": action_id,
                "recovery_only": True, "allow_network": False, "receipt_sequence": prior.sequence}
    receipt = await repo.append_change(
        OWNER, change_key=key, kind="source_attempt", payload=payload,
        operation_id=operation_id, request_hash=canonical_hash(payload),
        acquired_at=current, available_from=current, committed_at=current,
    )
    return {"campaign_id": str(campaign_id), "poll_id": poll_id, "action_id": action_id,
            "recovery_only": True, "allow_network": False, "receipt_sequence": receipt.sequence}


async def assert_campaign_recovery_authority(
    session: AsyncSession, *, permit_id: UUID, recovery_operation_id: UUID,
    sample_env_id: str,
) -> dict[str, Any]:
    _repo, reservation, payload = await _hop_phase(session, permit_id, "recovery")
    spec = payload["spec"]
    repo = SupplementalUpdatesRepository(session)
    ticket = await repo.get_change_by_key(
        OWNER, f"p4:campaign:{spec['campaign_id']}:poll:{spec['poll_id']}:recovery:{recovery_operation_id}",
    )
    if (ticket is None or ticket.payload.get("action_id") != spec.get("action_id")
            or ticket.payload.get("grant_id") != spec.get("grant_id")
            or ticket.payload.get("sample_env_id") != sample_env_id
            or ticket.payload.get("database_path") != _db_path(session)
            or ticket.payload.get("network_allowed") is not False):
        raise InvestorError("disclosure_scope_forbidden", "no matching recovery-only authority")
    return {"permit_id": str(permit_id), "poll_id": spec["poll_id"], "action_id": spec["action_id"],
            "recovery_only": True, "allow_network": False, "receipt_sequence": ticket.sequence}


async def _campaign_resume_scope(
    session: AsyncSession, *, campaign_id: UUID, poll_id: str, action_id: str,
    grant_id: UUID, grant_revision: int, grant_hash: str, sample_env_id: str,
    now: datetime, require_paused: bool,
) -> tuple[DisclosureCampaignV2, Any, dict[str, Any], BackgroundDisclosureAuthorizationV2]:
    from investment_agent.supplemental_rules import disclosure_authorization as auth
    from investment_agent.supplemental_rules.disclosure_authorization import (
        read_authorization_snapshot,
    )
    from investment_agent.supplemental_rules.disclosure_contracts import OWNER_STORAGE_BYTES_CAP

    campaign = await read_disclosure_campaign(session, campaign_id)
    if require_paused and campaign.status != "paused":
        raise InvestorError("disclosure_scope_forbidden", "only a paused campaign can be resumed")
    if not require_paused and campaign.status not in {"paused", "active"}:
        raise InvestorError("disclosure_scope_forbidden", "campaign is not resumable")
    if not campaign.valid_from <= now < campaign.expires_at:
        raise InvestorError("disclosure_authorization_expired", "campaign absolute deadline has passed")
    if sample_env_id != campaign.sample_env_id or _db_path(session) != campaign.database_path:
        raise InvestorError("disclosure_scope_forbidden", "resume environment or SQLite path changed")
    binding = {"authorization_id": str(grant_id), "revision": grant_revision,
               "sample_env_id": sample_env_id}
    if binding not in campaign.bound_grants:
        raise InvestorError("disclosure_scope_forbidden", "resume grant revision was never bound")
    admission = await SupplementalUpdatesRepository(session).get_change_by_key(
        OWNER, f"p4:campaign:{campaign_id}:poll:{poll_id}:admitted",
    )
    if admission is None:
        raise InvestorError("disclosure_scope_forbidden", "original poll admission is missing")
    original = admission.payload
    if (original.get("action_id") != action_id or original.get("grant_id") != str(grant_id)
            or original.get("grant_revision") != grant_revision or original.get("grant_hash") != grant_hash
            or original.get("sample_env_id") != sample_env_id
            or original.get("database_path") != campaign.database_path):
        raise InvestorError("disclosure_scope_forbidden", "resume differs from original immutable poll scope")
    active = campaign.active_poll
    if active != {"poll_id": poll_id, "action_id": action_id}:
        raise InvestorError("disclosure_scope_forbidden", "original poll root is not open")
    action = next((item for item in campaign.actions if item.action_id == action_id), None)
    if (action is None or action.kind != "sample_poll" or action.status != "approved"
            or action.listing_ref is None or action.window_start is None or action.window_end is None
            or action.listing_ref.model_dump(mode="json") != original.get("listing_ref")
            or action.window_start.isoformat() != original.get("window_start")
            or action.window_end.isoformat() != original.get("window_end")
            or action.profile_hash != original.get("profile_hash")
            or action.contract_hash != original.get("contract_hash")):
        raise InvestorError("disclosure_scope_forbidden", "resume action or original scope changed")
    usage = campaign.poll_usage.get(poll_id, {})
    if (usage.get("complete") is True or usage.get("http_reserved", 0) != 0
            or usage.get("bytes_reserved", 0) != 0 or usage.get("storage_reserved", 0) != 0):
        raise InvestorError("capacity_blocked", "resume poll is complete or still has unresolved permits")

    grant = await read_authorization_snapshot(session, grant_id)
    if (not isinstance(grant, BackgroundDisclosureAuthorizationV2) or grant.status != "active"
            or grant.revision != grant_revision or grant.authorization_hash != grant_hash
            or grant.campaign_id != campaign_id or grant.sample_env_id != sample_env_id
            or grant.contract_hash != action.contract_hash or grant.listing_refs != (action.listing_ref,)
            or not grant.valid_from <= now < grant.expires_at):
        raise InvestorError("disclosure_authorization_expired", "original V2 grant is no longer current")
    try:
        from investment_agent.evidence.disclosure_http import DisclosureError
        from investment_agent.evidence.disclosure_profiles import require_approved_contract

        approved = require_approved_contract(
            grant.contract_hash, now=now, protocol_hash=action.profile_hash,
        )
    except DisclosureError as exc:
        raise InvestorError("disclosure_scope_forbidden", "original campaign profile is no longer approved") from exc

    # Resume is an authority check, not a fresh reservation. Require at least
    # one unit left in every applicable cumulative budget; the next hop still
    # validates its exact request and byte reservation through the normal path.
    if (campaign.http_consumed + campaign.http_reserved >= campaign.http_cap
            or campaign.response_bytes_consumed + campaign.response_bytes_reserved >= campaign.response_bytes_cap
            or campaign.storage_bytes_consumed + campaign.storage_bytes_reserved >= campaign.storage_bytes_cap
            or campaign.sample_http_consumed + campaign.sample_http_reserved >= campaign.sample_http_cap
            or campaign.sample_response_bytes_consumed + campaign.sample_response_bytes_reserved >= campaign.sample_response_bytes_cap
            or usage.get("http_consumed", 0) + usage.get("http_reserved", 0) >= min(8, action.http_cap)
            or usage.get("bytes_consumed", 0) + usage.get("bytes_reserved", 0) >= min(64 * 1024 * 1024, action.response_byte_cap)):
        raise InvestorError("capacity_blocked", "resume has no remaining campaign or poll HTTP capacity")
    policy = (await auth._load_policy(session))[1]
    if (grant.grant_daily_http > policy["owner_daily_http"]
            or grant.grant_daily_response_bytes > policy["owner_daily_response_bytes"]):
        raise InvestorError("capacity_blocked", "current owner policy no longer permits this grant")
    day = auth._day(now)
    _owner_state, owner = await auth._quota_state(session, f"owner-day:{day}", day=day)
    _grant_state, grant_day = await auth._quota_state(
        session, f"grant-day:{grant_id}:{day}", day=day,
    )
    _poll_state, poll = await auth._quota_state(session, f"poll:{poll_id}")
    _storage_state, storage = await auth._quota_state(session, auth.STORAGE_SCOPE)
    _grant_storage_state, grant_storage = await auth._quota_state(
        session, f"grant-storage:{grant_id}",
    )
    _cooldown_state, cooldown = await auth._quota_state(session, auth.COOLDOWN_SCOPE)
    if (owner["request_reserved"] + owner["request_consumed"] >= min(24, policy["owner_daily_http"])
            or owner["bytes_reserved"] + owner["bytes_consumed"] >= min(128 * 1024 * 1024, policy["owner_daily_response_bytes"])
            or grant_day["request_reserved"] + grant_day["request_consumed"] >= grant.grant_daily_http
            or grant_day["bytes_reserved"] + grant_day["bytes_consumed"] >= grant.grant_daily_response_bytes
            or poll["request_reserved"] + poll["request_consumed"] >= int(original.get("poll_http_cap", 8))
            or poll["bytes_reserved"] + poll["bytes_consumed"] >= int(original.get("poll_response_bytes_cap", 64 * 1024 * 1024))
            or storage["storage_reserved"] + storage["storage_consumed"] >= OWNER_STORAGE_BYTES_CAP
            or grant_storage["storage_reserved"] + grant_storage["storage_consumed"] >= grant.storage_bytes_cap
            or cooldown.get("active_hop") is not None):
        raise InvestorError("capacity_blocked", "resume has no remaining shared quota or source token")
    # Keep this lookup in the scope check so a future profile extension cannot
    # accidentally make resume broader than the currently approved profile.
    if not approved.approval.purpose_scopes:
        raise InvestorError("disclosure_scope_forbidden", "current profile has no approved purpose")
    return campaign, action, original, grant


async def authorize_campaign_resume(
    session: AsyncSession, *, campaign_id: UUID, poll_id: str, action_id: str,
    grant_id: UUID, grant_revision: int, grant_hash: str, sample_env_id: str,
    resume_approval_ref: str, operation_id: UUID, now: datetime | None = None,
) -> dict[str, Any]:
    """Persist a manual-only resume ticket for the exact unfinished poll."""
    current = _utc(now)
    await _begin_write(session)
    if not resume_approval_ref.strip() or len(resume_approval_ref) > 256:
        raise InvestorError("disclosure_scope_forbidden", "resume requires a separate approval reference")
    key = f"p4:campaign:{campaign_id}:poll:{poll_id}:resume-auth:{operation_id}"
    repo = SupplementalUpdatesRepository(session)
    prior = await repo.get_change_by_key(OWNER, key)
    if prior is not None:
        # Ticket creation is idempotent even after resumption has consumed
        # capacity. Its use is still checked independently by the assertion
        # helper against current grant, campaign and quota state.
        expected = {
            "record_type": "campaign_resume_authorization_v2", "campaign_id": str(campaign_id),
            "poll_id": poll_id, "action_id": action_id, "grant_id": str(grant_id),
            "grant_revision": grant_revision, "grant_hash": grant_hash,
            "sample_env_id": sample_env_id, "database_path": _db_path(session),
            "approval_ref": resume_approval_ref, "operation_id": str(operation_id),
            "allow_network_resume": True,
        }
        if any(prior.payload.get(name) != value for name, value in expected.items()):
            raise InvestorError("idempotency_conflict", "resume ticket operation reused with a different scope")
        return {**prior.payload, "receipt_sequence": prior.sequence}
    _campaign, action, original, grant = await _campaign_resume_scope(
        session, campaign_id=campaign_id, poll_id=poll_id, action_id=action_id,
        grant_id=grant_id, grant_revision=grant_revision, grant_hash=grant_hash,
        sample_env_id=sample_env_id, now=current, require_paused=False,
    )
    payload = {
        "record_type": "campaign_resume_authorization_v2", "campaign_id": str(campaign_id),
        "poll_id": poll_id, "action_id": action_id, "grant_id": str(grant_id),
        "grant_revision": grant_revision, "grant_hash": grant_hash,
        "sample_env_id": sample_env_id, "database_path": _db_path(session),
        "listing_ref": original["listing_ref"], "window_start": original["window_start"],
        "window_end": original["window_end"], "profile_hash": action.profile_hash,
        "contract_hash": grant.contract_hash, "approval_ref": resume_approval_ref,
        "operation_id": str(operation_id), "allow_network_resume": True,
    }
    if _campaign.status != "paused":
        raise InvestorError("disclosure_scope_forbidden", "new resume tickets require a paused campaign")
    ticket = await repo.append_change(
        OWNER, change_key=key, kind="source_attempt",
        request_hash=canonical_hash(payload), payload=payload,
        acquired_at=current, available_from=current, committed_at=current,
    )
    return {**payload, "receipt_sequence": ticket.sequence}


async def assert_campaign_resume_authority(
    session: AsyncSession, *, campaign_id: UUID, poll_id: str, action_id: str,
    grant_id: UUID, grant_revision: int, grant_hash: str, sample_env_id: str,
    operation_id: UUID, resume_approval_ref: str, now: datetime | None = None,
) -> dict[str, Any]:
    current = _utc(now)
    campaign, action, original, grant = await _campaign_resume_scope(
        session, campaign_id=campaign_id, poll_id=poll_id, action_id=action_id,
        grant_id=grant_id, grant_revision=grant_revision, grant_hash=grant_hash,
        sample_env_id=sample_env_id, now=current, require_paused=False,
    )
    if campaign.status != "active":
        raise InvestorError(
            "disclosure_scope_forbidden",
            "native resume requires the campaign to be active after its explicit transition",
        )
    ticket = await SupplementalUpdatesRepository(session).get_change_by_key(
        OWNER, f"p4:campaign:{campaign_id}:poll:{poll_id}:resume-auth:{operation_id}",
    )
    if (ticket is None or ticket.payload.get("action_id") != action_id
            or ticket.payload.get("grant_id") != str(grant_id)
            or ticket.payload.get("grant_revision") != grant_revision
            or ticket.payload.get("grant_hash") != grant_hash
            or ticket.payload.get("sample_env_id") != sample_env_id
            or ticket.payload.get("database_path") != _db_path(session)
            or ticket.payload.get("listing_ref") != original.get("listing_ref")
            or ticket.payload.get("window_start") != original.get("window_start")
            or ticket.payload.get("window_end") != original.get("window_end")
            or ticket.payload.get("profile_hash") != action.profile_hash
            or ticket.payload.get("contract_hash") != grant.contract_hash
            or ticket.payload.get("approval_ref") != resume_approval_ref
            or ticket.payload.get("operation_id") != str(operation_id)
            or ticket.payload.get("allow_network_resume") is not True):
        raise InvestorError("disclosure_scope_forbidden", "no matching current manual resume authority")
    return {"campaign_id": str(campaign_id), "poll_id": poll_id, "action_id": action_id,
            "resume_only": True, "allow_network_resume": True, "receipt_sequence": ticket.sequence}


async def resume_disclosure_campaign(
    session: AsyncSession, *, campaign_id: UUID, poll_id: str, action_id: str,
    grant_id: UUID, grant_revision: int, grant_hash: str, sample_env_id: str,
    operation_id: UUID, expected_revision: int, resume_approval_ref: str,
    now: datetime | None = None,
) -> DisclosureCampaignV2:
    """Explicitly reactivate only the paused campaign for its original poll."""
    current = _utc(now)
    await _begin_write(session)
    if not resume_approval_ref.strip() or len(resume_approval_ref) > 256:
        raise InvestorError("disclosure_scope_forbidden", "resume requires a separate approval reference")
    key = f"p4:campaign:{campaign_id}:poll:{poll_id}:resume:{operation_id}"
    request = {"campaign_id": str(campaign_id), "poll_id": poll_id, "action_id": action_id,
               "grant_id": str(grant_id), "grant_revision": grant_revision, "grant_hash": grant_hash,
               "sample_env_id": sample_env_id, "approval_ref": resume_approval_ref}
    repo = SupplementalUpdatesRepository(session)
    prior = await repo.get_change_by_key(OWNER, key)
    if prior is not None:
        if prior.request_hash != canonical_hash(request):
            raise InvestorError("idempotency_conflict", "resume transition operation reused with another scope")
        campaign = await read_disclosure_campaign(session, campaign_id)
        if campaign.status != "active":
            raise InvestorError("disclosure_scope_forbidden", "an old resume receipt cannot reactivate a paused campaign")
        return campaign
    campaign, _action, _original, _grant = await _campaign_resume_scope(
        session, campaign_id=campaign_id, poll_id=poll_id, action_id=action_id,
        grant_id=grant_id, grant_revision=grant_revision, grant_hash=grant_hash,
        sample_env_id=sample_env_id, now=current, require_paused=False,
    )
    ticket_key = f"p4:campaign:{campaign_id}:poll:{poll_id}:resume-auth:{operation_id}"
    ticket = await SupplementalUpdatesRepository(session).get_change_by_key(OWNER, ticket_key)
    if (ticket is None or ticket.payload.get("action_id") != action_id
            or ticket.payload.get("grant_id") != str(grant_id)
            or ticket.payload.get("grant_revision") != grant_revision
            or ticket.payload.get("grant_hash") != grant_hash
            or ticket.payload.get("sample_env_id") != sample_env_id
            or ticket.payload.get("approval_ref") != resume_approval_ref
            or ticket.payload.get("allow_network_resume") is not True):
        raise InvestorError("disclosure_scope_forbidden", "resume transition lacks its exact authorization ticket")
    if campaign.revision != expected_revision:
        raise InvestorError("revision_conflict", "campaign revision changed before resume")
    if campaign.status != "paused":
        raise InvestorError("disclosure_scope_forbidden", "only a paused campaign can be resumed")
    if campaign.active_poll != {"poll_id": poll_id, "action_id": action_id}:
        raise InvestorError("disclosure_scope_forbidden", "resume cannot change or recreate poll root")
    transition = await repo.append_change(
        OWNER, change_key=key, kind="source_attempt",
        request_hash=canonical_hash(request),
        payload={"record_type": "campaign_resume_transition_v2", **request,
                 "from_revision": campaign.revision, "to_revision": campaign.revision + 1,
                 "valid_from": campaign.valid_from.isoformat(), "expires_at": campaign.expires_at.isoformat()},
        acquired_at=current, available_from=current, committed_at=current,
    )
    state, _current_campaign = await _load(session, campaign_id)
    updated = campaign.model_copy(update={
        "revision": campaign.revision + 1, "status": "active", "approval_ref": resume_approval_ref,
    })
    await _save(session, state, updated, now=current)
    # Keep the transition receipt linked to the authority update in the same transaction.
    if transition.payload.get("to_revision") != updated.revision:
        raise InvestorError("invalid_reference", "resume transition revision changed unexpectedly")
    return updated


async def complete_campaign_poll(
    session: AsyncSession, *, campaign_id: UUID, poll_id: str,
    action_id: str, now: datetime | None = None,
) -> DisclosureCampaignV2:
    """Release the single-root fence only after the exact poll has no reserved hops."""
    current = _utc(now)
    await _begin_write(session)
    state, campaign = await _load(session, campaign_id)
    active = campaign.active_poll
    if active is None or active.get("poll_id") != poll_id or active.get("action_id") != action_id:
        raise InvestorError("disclosure_scope_forbidden", "campaign root identity mismatch")
    usage = dict(campaign.poll_usage.get(poll_id, {}))
    if usage.get("http_reserved", 0) != 0 or usage.get("bytes_reserved", 0) != 0 or usage.get("storage_reserved", 0) != 0:
        raise InvestorError("capacity_blocked", "poll still has unfinalized campaign hop reservations")
    usage["complete"] = True
    next_poll = dict(campaign.poll_usage)
    next_poll[poll_id] = usage
    updated = campaign.model_copy(update={"active_poll": None, "poll_usage": next_poll})
    return await _save(session, state, updated, now=current)


async def settle_campaign_poll(
    session: AsyncSession, *, campaign_id: UUID, poll_id: str, operation_key: str,
    state: Literal["sent", "result", "unknown", "canceled"], http_requests: int = 0,
    response_bytes: int = 0, storage_bytes: int = 0, now: datetime | None = None,
) -> dict[str, Any]:
    """Compatibility helper for runner summaries; HTTP must use per-hop APIs."""
    if state != "canceled" or any((http_requests, response_bytes, storage_bytes)):
        raise InvestorError("invalid_reference", "campaign accounting must settle individual HTTP permits")
    return {"campaign_id": str(campaign_id), "poll_id": poll_id, "operation_key": operation_key, "state": state}

