"""Bounded, lease-fenced due selection for explicitly authorized disclosure scopes.

This module selects durable work. It performs no HTTP, research, or notification.
The poll worker owns the publication proof and its continuous coverage frontier.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import AwareDatetime, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.investor.common import Contract, ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.jobs.base import ClaimedJob
from investment_agent.storage.models.supplemental_updates import SupplementalSourceStateRow
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.canonical import canonical_hash
from investment_agent.supplemental_rules.disclosure_authorization import (
    assert_run_lease,
    get_current_authorization,
)
from investment_agent.supplemental_rules.disclosure_campaign_contracts import (
    BackgroundDisclosureAuthorizationV2,
)
from investment_agent.supplemental_rules.disclosure_contracts import (
    AUTH_FEED,
    BackgroundDisclosureAuthorizationV1,
)

SCHEDULE_FEED = "cninfo_szse_schedule_v1"
SCHEDULE_CONFIG = canonical_hash({"scheduler": "bounded_cninfo_v1", "schema_version": 1})
OWNER: Literal["local_owner"] = "local_owner"


class DisclosureScheduleV1(Contract):
    schema_version: Literal[1] = 1
    basis_hash: str
    listing_ref: ObjectRef
    backfill_after: AwareDatetime
    recent_end: AwareDatetime | None = None
    next_lane: Literal["recent", "backfill"] = "backfill"
    next_poll_at: AwareDatetime | None = None
    deep_next_at: AwareDatetime | None = None
    active_poll_id: UUID | None = None
    active_lane: Literal["recent", "backfill", "deep_sweep"] | None = None
    active_start: AwareDatetime | None = None
    active_end: AwareDatetime | None = None
    split_parent_poll_id: UUID | None = None
    window_days: int = Field(default=31, strict=True, ge=1, le=31)
    generation: int = Field(default=0, strict=True, ge=0)
    blocked_reason: str | None = None


def select_disclosure_window(
    state: DisclosureScheduleV1, grant: BackgroundDisclosureAuthorizationV1, now: datetime,
) -> tuple[Literal["recent", "backfill", "deep_sweep"], datetime, datetime] | None:
    """One fixed window, with alternating recent/backfill and a low-rate sweep."""
    end = min(now, grant.history_end)
    if end <= grant.history_start:
        return None
    lane: Literal["recent", "backfill", "deep_sweep"] = state.next_lane
    backfill_due = state.backfill_after < end
    if lane == "backfill" and not backfill_due:
        lane = "recent"
    if (lane == "recent" and grant.allow_deep_sweep and state.deep_next_at is not None
            and state.deep_next_at <= now):
        lane = "deep_sweep"
    if lane == "backfill":
        start = max(grant.history_start, state.backfill_after)
        return lane, start, min(end, start + timedelta(days=state.window_days))
    if lane == "deep_sweep":
        return lane, max(grant.history_start, end - timedelta(days=min(31, state.window_days))), end
    if not grant.allow_overlap:
        start = max(grant.history_start, state.recent_end or end - timedelta(days=3))
        if start >= end:
            # A no-overlap grant cannot be used for an automatic health re-fetch.
            return None
    else:
        start = max(grant.history_start, (state.recent_end or end) - timedelta(hours=72))
    return lane, start, min(end, start + timedelta(days=state.window_days))


async def _save_schedule(session: AsyncSession, row: Any, state: DisclosureScheduleV1,
                         *, now: datetime) -> None:
    repo = SupplementalUpdatesRepository(session)
    payload = state.model_dump(mode="json")
    receipt = await repo.append_change(
        OWNER, change_key="p4:disclosure-schedule:"
        + canonical_hash({"feed_id": SCHEDULE_FEED, "scope_key": row.scope_key})
        + f":{row.revision + 1}",
        kind="source_attempt", payload={"record_type": "poll_schedule_v1", "scope_key": row.scope_key,
            "state_revision": row.revision + 1, "previous_ref": row.payload.get("authority_ref"),
            "state": payload}, acquired_at=now, available_from=now, committed_at=now,
    )
    await repo.save_source(row.model_copy(update={
        "revision": row.revision + 1, "payload": {"state": payload, "authority_ref": {
            "sequence": receipt.sequence, "id": str(receipt.id), "payload_hash": receipt.payload_hash}},
    }), expected_revision=row.revision)


async def _read_schedule(session: AsyncSession, row: Any) -> DisclosureScheduleV1:
    if row.revision == 0 and set(row.payload) == {"state"}:
        return DisclosureScheduleV1.model_validate(row.payload["state"])
    ref = row.payload.get("authority_ref", {})
    receipt = await SupplementalUpdatesRepository(session).get_change(OWNER, ref.get("sequence", -1))
    if (receipt is None or str(receipt.id) != ref.get("id")
            or receipt.payload_hash != ref.get("payload_hash")
            or receipt.payload.get("record_type") != "poll_schedule_v1"
            or receipt.payload.get("scope_key") != row.scope_key
            or receipt.payload.get("state_revision") != row.revision
            or receipt.payload.get("state") != row.payload.get("state")):
        raise InvestorError("invalid_reference", "来源调度续点缺少匹配的不可变收据")
    return DisclosureScheduleV1.model_validate(row.payload["state"])


async def _completed_source_poll(repo: SupplementalUpdatesRepository, source: Any,
                                 listing_id: int) -> bool:
    if source is None or source.payload.get("poll_state") != "completed":
        return False
    final = await repo.get_change(OWNER, source.success_sequence)
    return bool(final is not None and final.payload_hash == source.success_receipt_hash
        and final.kind == "source_page" and final.payload.get("record_type") == "poll_final_v1"
        and final.payload.get("poll_id") == source.payload.get("active_poll_id")
        and final.listing_id == listing_id and final.context == "company_research")


async def schedule_due_disclosure_polls(
    session: AsyncSession, *, settings: Settings, job: ClaimedJob, now: datetime,
) -> int:
    """Inspect at most eight grants/three scopes and admit at most one poll.

    Missed slots collapse into the chosen absolute window. The immutable poll
    root is reused on recovery; a new scheduler tick never resets a failed poll.
    """
    from investment_agent.settings.loader import load_settings
    from investment_agent.supplemental_rules.disclosure_updates import (
        DisclosurePollRequest,
        admit_disclosure_poll,
        source_scope_key,
    )

    await assert_run_lease(session, job.lease)
    current = await load_settings(session, base=settings)
    if not (current.supplemental_rules_enabled and current.supplemental_updates_enabled
            and current.supplemental_disclosure_polling_enabled and current.disclosure_originals_enabled):
        return 0
    now = now.astimezone(UTC)
    repo = SupplementalUpdatesRepository(session)
    rows = list(await session.scalars(select(SupplementalSourceStateRow).where(
        SupplementalSourceStateRow.owner_id == OWNER,
        SupplementalSourceStateRow.feed_id == AUTH_FEED,
        SupplementalSourceStateRow.scope_key.like("grant:%"),
        SupplementalSourceStateRow.payload["authorization"]["status"].as_string() == "active",
        # V1 has no manual_only member and remains eligible for the legacy
        # scheduler. A strict V2 manual grant is excluded in SQL, before it is
        # loaded or considered for any due window.
        or_(
            SupplementalSourceStateRow.payload["authorization"]["manual_only"].as_boolean().is_(None),
            SupplementalSourceStateRow.payload["authorization"]["manual_only"].as_boolean().is_(False),
        ),
        func.julianday(SupplementalSourceStateRow.payload["authorization"]["expires_at"].as_string())
        > func.julianday(now.isoformat()),
    ).order_by(SupplementalSourceStateRow.id.desc()).limit(9)))
    grants: list[BackgroundDisclosureAuthorizationV1] = []
    # Expired grants may remain in the journal; they grant no scheduling rights.
    for row in rows:
        try:
            grant = await get_current_authorization(session, UUID(row.scope_key[6:]), now=now)
        except InvestorError as exc:
            if exc.detail.code == "disclosure_authorization_expired":
                continue
            raise
        if isinstance(grant, BackgroundDisclosureAuthorizationV2):
            continue
        grants.append(grant)
    if len(grants) > 8:
        raise InvestorError("capacity_blocked", "来源活动授权超过调度硬上限")
    scopes: dict[int, tuple[ObjectRef, BackgroundDisclosureAuthorizationV1]] = {}
    # Newest explicit server-authored revision wins for overlapping active
    # grants; UUID is only the deterministic tie break, never an authority.
    for grant in sorted(grants, key=lambda item: (
        item.created_at, item.revision, str(item.authorization_id)), reverse=True):
        for ref in grant.listing_refs:
            scopes.setdefault(int(ref.id), (ref, grant))
    if len(scopes) > 3:
        raise InvestorError("capacity_blocked", "来源活动证券超过调度硬上限")
    rotation = await repo.ensure_source(OWNER, SCHEDULE_FEED, "rotation", SCHEDULE_CONFIG,
                                        payload={"after_listing_id": 0})
    after = rotation.payload.get("after_listing_id", 0)
    if type(after) is not int or after < 0:
        raise InvestorError("invalid_reference", "来源调度轮转游标损坏")
    ordered = sorted(scopes, key=lambda value: (value <= after, value))
    for listing_id in ordered:
        await assert_run_lease(session, job.lease)
        ref, grant = scopes[listing_id]
        source = await repo.get_source(OWNER, "cninfo_szse_v1", source_scope_key(ref), for_update=True)
        basis = canonical_hash({"listing_ref": ref, "contract_hash": grant.contract_hash,
                                "history_start": grant.history_start})
        state_row = await repo.ensure_source(OWNER, SCHEDULE_FEED, f"listing:{listing_id}",
            SCHEDULE_CONFIG, payload={"state": DisclosureScheduleV1(
                basis_hash=basis, listing_ref=ref, backfill_after=grant.history_start,
                deep_next_at=now + timedelta(days=7)).model_dump(mode="json")})
        state = await _read_schedule(session, state_row)
        if state.basis_hash != basis:
            # A new incompatible coverage contract needs an explicit poll/epoch
            # transition; a timer must not silently reinterpret old history.
            if (await _completed_source_poll(repo, source, listing_id)
                    and source is not None and source.config_hash == grant.contract_hash
                    and not source.payload.get("pending_predecessor")
                    and source.payload.get("history_start") == grant.history_start.isoformat()):
                state = DisclosureScheduleV1(basis_hash=basis, listing_ref=ref,
                    backfill_after=grant.history_start, deep_next_at=now + timedelta(days=7),
                    next_poll_at=now + timedelta(seconds=grant.min_poll_interval_seconds))
                await _save_schedule(session, state_row, state, now=now)
                continue
            if state.blocked_reason != "history_contract_changed":
                await _save_schedule(session, state_row, state.model_copy(update={
                    "blocked_reason": "history_contract_changed"}), now=now)
            continue
        if state.active_poll_id is not None:
            if source is None or source.payload.get("active_poll_id") != str(state.active_poll_id):
                # A manual operation can replace a terminal scheduled poll.
                # Only a matching immutable successful final may release that
                # pointer; it cannot advance the old lane or erase its gap.
                if (await _completed_source_poll(repo, source, listing_id)
                        and source is not None and not source.payload.get("pending_predecessor")):
                    state = state.model_copy(update={"active_poll_id": None, "active_lane": None,
                        "active_start": None, "active_end": None, "split_parent_poll_id": None,
                        "blocked_reason": None})
                    await _save_schedule(session, state_row, state, now=now)
                    continue
                if state.blocked_reason != "manual_poll_pending":
                    await _save_schedule(session, state_row, state.model_copy(update={
                        "blocked_reason": "manual_poll_pending"}), now=now)
                continue
            outcome = source.payload.get("poll_state")
            if outcome == "completed":
                final = await repo.get_change(OWNER, source.success_sequence)
                if (final is None or final.payload_hash != source.success_receipt_hash
                        or final.kind != "source_page"
                        or final.payload.get("record_type") != "poll_final_v1"
                        or final.payload.get("poll_id") != str(state.active_poll_id)
                        or final.listing_id != listing_id or final.context != "company_research"
                        or state.active_start is None or state.active_end is None
                        or datetime.fromisoformat(final.payload["publication_window_start"])
                        != state.active_start
                        or datetime.fromisoformat(final.payload["publication_window_end"])
                        != state.active_end):
                    await _save_schedule(session, state_row, state.model_copy(update={
                        "blocked_reason": "completed_window_receipt_invalid"}), now=now)
                    continue
                changes: dict[str, Any] = {"active_poll_id": None, "active_lane": None,
                    "active_start": None, "active_end": None, "blocked_reason": None,
                    "split_parent_poll_id": None,
                    "next_lane": "recent" if state.active_lane == "backfill" else "backfill",
                    "window_days": 31}
                if state.active_end is not None:
                    if state.active_lane == "backfill":
                        changes["backfill_after"] = max(state.backfill_after, state.active_end)
                    elif state.active_lane == "recent":
                        changes["recent_end"] = max(state.recent_end or state.active_end, state.active_end)
                    else:
                        changes["deep_next_at"] = now + timedelta(days=7)
                state = state.model_copy(update=changes)
                await _save_schedule(session, state_row, state, now=now)
                refreshed_schedule = await repo.get_source(
                    OWNER, SCHEDULE_FEED, state_row.scope_key, for_update=True)
                assert refreshed_schedule is not None
                state_row = refreshed_schedule
            elif outcome == "window_split_required":
                actual_days = max(1, int((state.active_end - state.active_start).total_seconds()
                    + 86_399) // 86_400) if state.active_end and state.active_start else state.window_days
                if actual_days <= 1:
                    if state.blocked_reason != "one_day_window_capacity":
                        await _save_schedule(session, state_row, state.model_copy(update={
                            "blocked_reason": "one_day_window_capacity"}), now=now)
                    continue
                state = state.model_copy(update={"active_poll_id": None, "active_lane": None,
                    "active_start": None, "active_end": None,
                    "split_parent_poll_id": state.active_poll_id,
                    "window_days": max(1, min(state.window_days, actual_days) // 2),
                    "blocked_reason": "window_split_required", "generation": state.generation + 1})
                await _save_schedule(session, state_row, state, now=now)
                continue
            else:
                # Includes unknown, canceled, quarantine, and exhausted retry.
                # An automatic new poll cannot erase their pending collection.
                if outcome not in {"queued", "running", "retry_wait", "capacity_blocked", "paused"}:
                    reason = "poll_requires_review:" + str(outcome)[:64]
                    if state.blocked_reason != reason:
                        await _save_schedule(session, state_row, state.model_copy(update={
                            "blocked_reason": reason}), now=now)
                continue
        if source is not None and source.payload.get("poll_state") in {
            "queued", "running", "retry_wait", "capacity_blocked",
        }:
            continue
        split_resume = bool(source is not None
            and source.payload.get("poll_state") == "window_split_required"
            and state.split_parent_poll_id is not None
            and source.payload.get("active_poll_id") == str(state.split_parent_poll_id))
        if (source is not None and source.payload.get("poll_state") not in {"completed", "unvalidated"}
                and not split_resume):
            # A manual poll's failure also remains pending; its absence from our
            # scheduler projection does not authorize automatic replacement.
            continue
        if state.next_poll_at is not None and state.next_poll_at > now:
            continue
        chosen = select_disclosure_window(state, grant, now)
        if chosen is None:
            continue
        lane, start, end = chosen
        coverage_target_end = min(now, grant.history_end)
        op = uuid5(NAMESPACE_URL, "p4-disclosure-schedule:" + canonical_hash({
            "grant_hash": grant.authorization_hash, "listing": listing_id,
            "lane": lane, "generation": state.generation, "start": start, "end": end,
            "coverage_target_end": coverage_target_end,
            "slot": int(now.timestamp()) // grant.min_poll_interval_seconds,
        }))
        try:
            async with session.begin_nested():
                accepted = await admit_disclosure_poll(session, DisclosurePollRequest(
                    operation_id=op, authorization_id=grant.authorization_id,
                    authorization_revision=grant.revision, authorization_hash=grant.authorization_hash,
                    listing_ref=ref, window_start=start, window_end=end,
                    expected_source_revision=source.revision if source is not None else 0,
                    replace_poll_id=state.split_parent_poll_id if split_resume else None,
                    replace_reason="按原授权及配额承接容量受限的父窗口并缩窗复核" if split_resume else None,
                ), settings=settings, now=now, coverage_target_end=coverage_target_end)
        except InvestorError as exc:
            if exc.detail.code in {"source_cooldown", "source_capacity_exceeded", "capacity_blocked"}:
                continue
            raise
        state = state.model_copy(update={"active_poll_id": UUID(str(accepted["poll_id"])),
            "active_lane": lane, "active_start": start, "active_end": end,
            "generation": state.generation + 1, "blocked_reason": None,
            "next_poll_at": now + timedelta(seconds=grant.min_poll_interval_seconds)})
        await _save_schedule(session, state_row, state, now=now)
        await repo.save_source(rotation.model_copy(update={"revision": rotation.revision + 1,
            "payload": {"after_listing_id": listing_id}}), expected_revision=rotation.revision)
        return 1
    return 0
