"""Holding-scope rule drafting and review (watch-holding-plan 6.2 WP8, funnel 5.4/8).

Holding rules (``add`` / ``trim`` / ``exit_review``) are the same kind of object
as candidate rules - template reference plus parameter overrides, versioned per
``(listing, scope)`` - but a different slot. This module owns their user-facing
lifecycle on the holding pool page:

* :func:`holding_rule_snapshot` - what the page shows: the state of the slot
  (``needs_template`` / ``drafting`` / ``ready`` / ``failed`` / ``none``), the
  active version, the open draft with its zones and ranges, and the listing's
  holding zone events;
* :func:`create_holding_draft` - one request identity (UUID) plus its
  ``rule_draft`` run, claiming the shared ``(listing, holding)`` drafting
  generation (module :mod:`investment_agent.rules.generation`) so a late or
  superseded run can only leave audit rows;
* :func:`approve_holding_rule` / :func:`edit_holding_rule_params` /
  :func:`reject_holding_rule` - review actions with their decision journal
  entries. Approving activates the draft and retires the previous active
  version; no historical version is ever rewritten (funnel 5.5).

Drafting reuses the candidate drafting agent unchanged (parameters only, zones
come from the template), with ``scope="holding"`` so the instance lands in the
holding slot and the prompt names the holding zones.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.candidates.lifecycle import (
    preference_notes,
    rule_params,
    rule_ref_of,
)
from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
from investment_agent.rules.drafting import (
    RULE_DRAFT,
    RuleDraftPayload,
    enqueue_rule_draft,
    rule_draft_idempotency_key,
)
from investment_agent.rules.generation import claim_draft_generation, current_generation
from investment_agent.rules.rule_view import rule_block
from investment_agent.rules.store import RuleStore, RuleStoreError, instance_from_record
from investment_agent.rules.zone_detection import HOLDING_SCOPE, zone_summaries
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import DecisionRecord
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.rules import RuleInstanceRecord
from investment_agent.storage.repositories.candidate import DecisionRepository

log = structlog.get_logger(__name__)

SCOPE = HOLDING_SCOPE
USER = "user"
LIVE_RUN_STATUSES = frozenset({"queued", "running"})
FAILED_RUN_STATUSES = frozenset({"failed", "partial"})

RULE_DECISION_KINDS = ("approve_rule", "reject_rule", "edit_rule")


class HoldingRuleError(Exception):
    """Base of the holding-rule errors; ``status_code`` is the HTTP mapping."""

    status_code = 400


class HoldingRuleNotFoundError(HoldingRuleError):
    status_code = 404


class HoldingRuleConflictError(HoldingRuleError):
    status_code = 409


class HoldingRuleInputError(HoldingRuleError):
    status_code = 422


def _now() -> datetime:
    return datetime.now(UTC)


async def _listing(session: AsyncSession, listing_id: int) -> Listing:
    listing = await session.get(Listing, listing_id)
    if listing is None:
        raise HoldingRuleNotFoundError(f"no listing {listing_id}")
    return listing


async def _a_share_ticker(session: AsyncSession, listing_id: int) -> str:
    listing = await _listing(session, listing_id)
    ticker = format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
    if parse_a_share_ticker(ticker) is None:
        raise HoldingRuleInputError(
            f"rule drafting only supports A-share listings ({ticker!r})"
        )
    return ticker


async def _open_draft(session: AsyncSession, listing_id: int) -> RuleInstanceRecord | None:
    stmt = (
        select(RuleInstanceRecord)
        .where(
            RuleInstanceRecord.listing_id == listing_id,
            RuleInstanceRecord.scope == SCOPE,
            RuleInstanceRecord.status == "draft",
        )
        .order_by(RuleInstanceRecord.version.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _latest_run(session: AsyncSession, listing_id: int) -> PipelineRun | None:
    """The newest holding-origin ``rule_draft`` run of one listing."""

    stmt = (
        select(PipelineRun)
        .where(
            PipelineRun.kind == RULE_DRAFT,
            PipelineRun.request_json["origin_kind"].as_string() == "holding",
            PipelineRun.request_json["origin_id"].as_integer() == listing_id,
        )
        .order_by(PipelineRun.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _latest_rule_decision(
    session: AsyncSession, listing_id: int
) -> DecisionRecord | None:
    stmt = (
        select(DecisionRecord)
        .where(
            DecisionRecord.listing_id == listing_id,
            DecisionRecord.kind.in_(RULE_DECISION_KINDS),
            DecisionRecord.context["scope"].as_string() == SCOPE,
        )
        .order_by(DecisionRecord.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def holding_rule_snapshot(session: AsyncSession, listing_id: int) -> dict[str, Any]:
    """The holding rule slot of one listing, as the page renders it."""

    store = RuleStore(session)
    active = await store.active_instance(listing_id, SCOPE)
    draft = await _open_draft(session, listing_id)
    run = await _latest_run(session, listing_id)
    generation = await current_generation(session, listing_id=listing_id, scope=SCOPE)
    current_run = (
        run
        if run is not None
        and generation is not None
        and (run.request_json or {}).get("draft_request_id") == generation.token
        else None
    )
    decision = await _latest_rule_decision(session, listing_id)
    state = "needs_template"
    if current_run is not None and current_run.status in LIVE_RUN_STATUSES:
        state = "drafting"
    elif draft is not None:
        state = "ready"
    elif current_run is not None and current_run.status in FAILED_RUN_STATUSES:
        state = "failed"
    elif active is not None:
        state = "none"
    elif decision is not None and decision.kind == "reject_rule":
        state = "rejected"
    summaries = await zone_summaries(session, listing_ids=[listing_id], scope=SCOPE)
    return {
        "listing_id": listing_id,
        "scope": SCOPE,
        "state": state,
        "active": (
            await rule_block(session, active, include_definition=False, store=store)
            if active is not None
            else None
        ),
        "draft": (
            await rule_block(session, draft, include_definition=True, store=store)
            if draft is not None
            else None
        ),
        "run": (
            {
                "id": current_run.id,
                "status": current_run.status,
                "template_ref": (
                    f"{current_run.request_json.get('template_id')}"
                    f"@{current_run.request_json.get('template_version')}"
                ),
                "error": current_run.error_summary,
            }
            if current_run is not None
            else None
        ),
        "requested_template_ref": generation.template_ref if generation is not None else None,
        "last_decision": (
            {
                "kind": decision.kind,
                "at": decision.at,
                "reason": decision.reason_text,
                "rule_instance_id": decision.rule_instance_id,
            }
            if decision is not None
            else None
        ),
        "zone_events": summaries.get(listing_id),
    }


async def create_holding_draft(
    session: AsyncSession,
    listing_id: int,
    *,
    template_id: str | None,
    as_of: date,
    triggered_by: str = "holding_api",
) -> tuple[str, int]:
    """One new holding draft request + its ``rule_draft`` run (caller's transaction).

    The template falls back to the slot's newest intent: the active holding
    rule, else the stock's active candidate rule - a holding rule for a stock
    the user already researched continues that strategy instead of guessing.
    """

    ticker = await _a_share_ticker(session, listing_id)
    resolved = template_id or await _default_template(session, listing_id)
    if not resolved:
        raise HoldingRuleInputError("no drafting template chosen yet")
    record = await RuleStore(session).latest_template_record(resolved)
    if record is None:
        raise HoldingRuleInputError(f"unknown template {resolved!r}")
    request_id = str(uuid4())
    generation = await claim_draft_generation(
        session,
        listing_id=listing_id,
        symbol=ticker,
        scope=SCOPE,
        token=request_id,
        origin_kind="holding",
        origin_id=listing_id,
        template_ref=f"{resolved}@{record.version}",
    )
    payload = RuleDraftPayload(
        candidate_id=0,
        ticker=ticker,
        template_id=resolved,
        template_version=record.version,
        as_of=as_of,
        scope=SCOPE,
        origin_kind="holding",
        origin_id=listing_id,
        draft_request_id=request_id,
        draft_token=request_id,
        preference_notes=await preference_notes(session),
    )
    run, _created = await enqueue_rule_draft(
        session,
        payload,
        triggered_by=triggered_by,
        key=rule_draft_idempotency_key(payload),
    )
    log.info(
        "holding_rule.draft.enqueued",
        listing_id=listing_id,
        request_id=request_id,
        template_ref=f"{resolved}@{record.version}",
        rule_head=generation.rule_head,
        run_id=run.id,
    )
    return request_id, run.id


async def _default_template(session: AsyncSession, listing_id: int) -> str | None:
    store = RuleStore(session)
    for scope in (SCOPE, "candidate"):
        active = await store.active_instance(listing_id, scope)
        if active is not None:
            return active.template_id
    return None


async def _reviewable_draft(
    session: AsyncSession, listing_id: int, rule_instance_id: int
) -> RuleInstanceRecord:
    rule = await session.get(RuleInstanceRecord, rule_instance_id)
    if rule is None:
        raise HoldingRuleNotFoundError(f"no rule instance {rule_instance_id}")
    if rule.listing_id != listing_id or rule.scope != SCOPE:
        raise HoldingRuleConflictError(
            f"rule instance {rule_instance_id} is not a holding rule of this listing"
        )
    current = await _open_draft(session, listing_id)
    if rule.status != "draft" or current is None or current.id != rule.id:
        raise HoldingRuleConflictError(
            "the holding rule draft is out of date; refresh and review the current version"
        )
    return rule


async def _context(
    session: AsyncSession, listing_id: int, rule: RuleInstanceRecord, **extra: Any
) -> dict[str, Any]:
    active = await RuleStore(session).active_instance(listing_id, SCOPE)
    context: dict[str, Any] = {
        "source": "holding",
        "scope": SCOPE,
        "listing_id": listing_id,
        "rule": {
            "rule_instance_id": rule.id,
            "version": rule.version,
            "status": rule.status,
            "template_ref": f"{rule.template_id}@{rule.template_version}",
            "params": rule_params(rule),
        },
        "active_rule": (
            {
                "rule_instance_id": active.id,
                "version": active.version,
                "template_ref": f"{active.template_id}@{active.template_version}",
            }
            if active is not None
            else None
        ),
    }
    context.update(extra)
    return context


async def approve_holding_rule(
    session: AsyncSession,
    listing_id: int,
    *,
    rule_instance_id: int,
    expected_active_rule_instance_id: int | None = None,
    reason: str | None = None,
    now: datetime | None = None,
) -> tuple[RuleInstanceRecord, DecisionRecord]:
    """Activate the open holding draft; the previous active version retires."""

    at = now or _now()
    rule = await _reviewable_draft(session, listing_id, rule_instance_id)
    store = RuleStore(session)
    previous = await store.active_instance(listing_id, SCOPE)
    if (previous.id if previous is not None else None) != expected_active_rule_instance_id:
        raise HoldingRuleConflictError("active rule changed; refresh and review both versions")
    approved = await store.approve(rule.id, approved_by=USER, now=at)
    decision = await DecisionRepository(session).append(
        listing_id=listing_id,
        ticker=approved.symbol,
        kind="approve_rule",
        actor=USER,
        reason_text=reason,
        rule_instance_id=approved.id,
        rule_ref=rule_ref_of(approved),
        context=await _context(
            session,
            listing_id,
            approved,
            retired_rule_instance_id=previous.id if previous is not None else None,
        ),
        at=at,
    )
    log.info(
        "holding_rule.approved",
        listing_id=listing_id,
        rule_instance_id=approved.id,
        version=approved.version,
    )
    return approved, decision


async def edit_holding_rule_params(
    session: AsyncSession,
    listing_id: int,
    *,
    rule_instance_id: int,
    params: Mapping[str, float],
    reason: str | None = None,
    now: datetime | None = None,
) -> tuple[RuleInstanceRecord, DecisionRecord]:
    """A new draft version with ``params`` merged over the draft's overrides."""

    at = now or _now()
    rule = await _reviewable_draft(session, listing_id, rule_instance_id)
    instance = instance_from_record(rule)
    before = dict(instance.params)
    edited = instance.model_copy(update={"params": {**before, **params}, "drafted_by": USER})
    try:
        new = await RuleStore(session).edit(rule.id, edited, now=at)
    except RuleStoreError as exc:
        raise HoldingRuleConflictError(str(exc)) from exc
    decision = await DecisionRepository(session).append(
        listing_id=listing_id,
        ticker=new.symbol,
        kind="edit_rule",
        actor=USER,
        reason_text=reason,
        rule_instance_id=new.id,
        rule_ref=rule_ref_of(new),
        context=await _context(
            session,
            listing_id,
            new,
            edited_from={"rule_instance_id": rule.id, "version": rule.version},
            params_before=before,
            params_after=dict(edited.params),
        ),
        at=at,
    )
    return new, decision


async def reject_holding_rule(
    session: AsyncSession,
    listing_id: int,
    *,
    rule_instance_id: int,
    reason: str,
    now: datetime | None = None,
) -> tuple[RuleInstanceRecord, DecisionRecord]:
    """Reject the open holding draft with a required reason."""

    if not reason or not reason.strip():
        raise HoldingRuleInputError("rejecting a draft requires a non-empty reason")
    at = now or _now()
    rule = await _reviewable_draft(session, listing_id, rule_instance_id)
    await RuleStore(session).retire(rule.id, now=at)
    decision = await DecisionRepository(session).append(
        listing_id=listing_id,
        ticker=rule.symbol,
        kind="reject_rule",
        actor=USER,
        reason_text=reason,
        rule_instance_id=rule.id,
        rule_ref=rule_ref_of(rule),
        context=await _context(session, listing_id, rule),
        at=at,
    )
    return rule, decision


__all__ = [
    "SCOPE",
    "HoldingRuleConflictError",
    "HoldingRuleError",
    "HoldingRuleInputError",
    "HoldingRuleNotFoundError",
    "approve_holding_rule",
    "create_holding_draft",
    "edit_holding_rule_params",
    "holding_rule_snapshot",
    "reject_holding_rule",
]
