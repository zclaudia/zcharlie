"""/portfolio/rule endpoints: the holding pool's rule review surface (WP8).

Holding rules (``add`` / ``trim`` / ``exit_review``) are approved on the
holding pool page exactly like observation rules: draft, review the zones and
parameters, then approve, edit or reject with a reason. Approving activates the
draft and retires the previous active version - the shared rule slot semantics
of :class:`~investment_agent.rules.store.RuleStore` - and never rewrites a
historical version. Nothing here writes to the ledger: a rule is a research
commitment, not a trading instruction.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.api.schemas.holding_rules import (
    HoldingRuleApproveRequest,
    HoldingRuleDraftRequest,
    HoldingRuleEditRequest,
    HoldingRuleOut,
    HoldingRuleRejectRequest,
)
from investment_agent.clock import shanghai_today
from investment_agent.rules.holding import (
    HoldingRuleError,
    approve_holding_rule,
    create_holding_draft,
    edit_holding_rule_params,
    holding_rule_snapshot,
    reject_holding_rule,
)
from investment_agent.rules.validation import RuleValidationError
from investment_agent.rules.zone_detection import event_block, zone_events
from investment_agent.storage.database import session_scope

router = APIRouter(
    prefix="/portfolio/rule",
    tags=["portfolio"],
    dependencies=[Depends(verify_api_key_or_session)],
)


@router.get("", response_model=HoldingRuleOut)
async def get_holding_rule(listing_id: int = Query(ge=1)) -> HoldingRuleOut:
    """The listing's holding rule slot: state, active version, open draft."""

    async with session_scope() as session:
        try:
            snapshot = await holding_rule_snapshot(session, listing_id)
        except HoldingRuleError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return HoldingRuleOut.model_validate(snapshot)


@router.post("/draft", response_model=HoldingRuleOut)
async def draft_holding_rule(body: HoldingRuleDraftRequest) -> HoldingRuleOut:
    """(Re-)draft the holding rule; the template falls back to the newest intent."""

    async with session_scope() as session:
        try:
            await create_holding_draft(
                session,
                body.listing_id,
                template_id=body.template_id,
                as_of=shanghai_today(),
                triggered_by="holding_api",
            )
        except HoldingRuleError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        snapshot = await holding_rule_snapshot(session, body.listing_id)
    return HoldingRuleOut.model_validate(snapshot)


@router.post("/approve", response_model=HoldingRuleOut)
async def approve_holding(body: HoldingRuleApproveRequest) -> HoldingRuleOut:
    """Approve the open draft; the previous active version retires."""

    async with session_scope() as session:
        try:
            await approve_holding_rule(
                session,
                body.listing_id,
                rule_instance_id=body.rule_instance_id,
                expected_active_rule_instance_id=body.expected_active_rule_instance_id,
                reason=body.reason,
            )
        except HoldingRuleError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        snapshot = await holding_rule_snapshot(session, body.listing_id)
    return HoldingRuleOut.model_validate(snapshot)


@router.post("/edit", response_model=HoldingRuleOut)
async def edit_holding(body: HoldingRuleEditRequest) -> HoldingRuleOut:
    """Save a new draft version with ``params`` merged over the current draft."""

    async with session_scope() as session:
        try:
            await edit_holding_rule_params(
                session,
                body.listing_id,
                rule_instance_id=body.rule_instance_id,
                params=body.params,
                reason=body.reason,
            )
        except HoldingRuleError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except RuleValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        snapshot = await holding_rule_snapshot(session, body.listing_id)
    return HoldingRuleOut.model_validate(snapshot)


@router.post("/reject", response_model=HoldingRuleOut)
async def reject_holding(body: HoldingRuleRejectRequest) -> HoldingRuleOut:
    """Reject the open draft with a required reason."""

    async with session_scope() as session:
        try:
            await reject_holding_rule(
                session,
                body.listing_id,
                rule_instance_id=body.rule_instance_id,
                reason=body.reason,
            )
        except HoldingRuleError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        snapshot = await holding_rule_snapshot(session, body.listing_id)
    return HoldingRuleOut.model_validate(snapshot)


@router.get("/zone-events")
async def list_zone_events(
    listing_id: int | None = Query(default=None, ge=1),
    scope: str | None = Query(default=None, pattern="^(candidate|holding)$"),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, list[dict[str, object]]]:
    """Recent zone events (newest first) - the shared review/history read."""

    async with session_scope() as session:
        events = await zone_events(session, listing_id=listing_id, scope=scope, limit=limit)
        return {"events": [event_block(event) for event in events]}
