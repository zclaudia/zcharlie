"""Promote flow (P1-D3) and rule reject / edit with their journal entries.

Promotion = approving the stock's rule (funnel design 8). In ONE
transaction (:func:`promote_candidate`):

1. resolve the rule: ``rule_instance_id`` if given, else the candidate's
   draft (lazily derived), else an already active candidate-scope rule;
   none -> :class:`CandidateConflictError` (drafting not ready);
2. optional ``params_override``: validated against the template's
   ``param_ranges``; a new instance version via ``RuleStore.edit`` + an
   ``edit_rule`` decision;
3. approve the draft -> ``active`` (the previous active version is retired)
   + an ``approve_rule`` decision;
4. add / re-enable the ``watchlist_item`` (industry = Shenwan level-1 name
   valid today, CSRC name as fallback; report_year = latest fiscal year
   whose annual report is available today);
5. candidate -> ``promoted`` + a ``promote`` decision.

After the commit (:func:`run_promotion`), best effort - a failure is logged
and returned as a warning, never rolling the promotion back:

6. iFinD ``promote`` verification via :func:`_request_promote_verification`;
7. one single-stock research run for the new watchlist entry.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.candidates.lifecycle import (
    CANDIDATE_SCOPE,
    CandidateConflictError,
    CandidateInputError,
    CandidateNotFoundError,
    candidate_context,
    effective_draft,
    open_candidate,
    rule_params,
    rule_ref_of,
)
from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.factors.industry_groups import usable_sw_code
from investment_agent.providers.shenwan_directory import (
    SW2021_SYSTEM,
    default_shenwan_directory,
)
from investment_agent.research.watchlist_refresh import enqueue_single_stock_research
from investment_agent.rules.store import RuleStore, RuleStoreError, instance_from_record
from investment_agent.rules.validation import RuleValidationError
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.candidates import CandidateRecord, DecisionRecord
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.models.rules import RuleInstanceRecord
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.candidate import CandidateRepository, DecisionRepository
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.watchlist import WatchlistRepository

log = structlog.get_logger(__name__)

USER = "user"
ANNUAL_REPORT_DEADLINE = (4, 30)
"""Statutory annual-report deadline (month, day): the fallback report year."""


class RuleNotEditableError(CandidateConflictError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def _template_ref(rule: RuleInstanceRecord) -> str:
    return f"{rule.template_id}@{rule.template_version}"


# --------------------------------------------------------------------------- #
# Watchlist metadata
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WatchMetadata:
    industry: str | None
    industry_source: str | None
    """``sw2021_l1`` / ``csrc`` / ``None``."""
    report_year: int
    report_year_source: str
    """``annual_snapshot`` or ``deadline_fallback``."""


async def watch_metadata(session: AsyncSession, listing_id: int, today: date) -> WatchMetadata:
    """Industry (Shenwan level-1 name, CSRC fallback) and report year as of ``today``."""

    repo = IndustryRepository(session)
    industry: str | None = None
    industry_source: str | None = None
    sw = await repo.industry_as_of(listing_id, today, system=SW2021_SYSTEM)
    if sw is not None:
        directory = default_shenwan_directory()
        code = usable_sw_code(sw.code, sw.valid_from, sw.valid_to, directory, sw.name)
        chain = directory.chain(code) if code is not None else ()
        if chain and chain[0].level == 1:
            industry, industry_source = chain[0].name, "sw2021_l1"
    if industry is None:
        csrc = await repo.industry_as_of(listing_id, today, system="csrc")
        if csrc is not None and csrc.name:
            industry, industry_source = csrc.name, "csrc"
    stmt = select(FinancialSnapshot.report_period).where(
        FinancialSnapshot.listing_id == listing_id,
        FinancialSnapshot.available_from.is_not(None),
        FinancialSnapshot.available_from <= today,
    )
    annual_years = [
        period.year
        for period in (await session.execute(stmt)).scalars()
        if (period.month, period.day) == (12, 31)
    ]
    if annual_years:
        return WatchMetadata(industry, industry_source, max(annual_years), "annual_snapshot")
    month, day = ANNUAL_REPORT_DEADLINE
    fallback = today.year - 1 if (today.month, today.day) > (month, day) else today.year - 2
    return WatchMetadata(industry, industry_source, fallback, "deadline_fallback")


# --------------------------------------------------------------------------- #
# Rule edit / reject
# --------------------------------------------------------------------------- #


async def _rule(session: AsyncSession, rule_instance_id: int) -> RuleInstanceRecord:
    rule = await session.get(RuleInstanceRecord, rule_instance_id)
    if rule is None:
        raise CandidateNotFoundError(f"no rule instance {rule_instance_id}")
    return rule


async def _open_candidate_for(
    session: AsyncSession, listing_id: int
) -> CandidateRecord | None:
    return await CandidateRepository(session).open_for_listing(listing_id)


async def _rule_context(
    session: AsyncSession, rule: RuleInstanceRecord, **extra: Any
) -> dict[str, Any]:
    candidate = await _open_candidate_for(session, rule.listing_id)
    base: dict[str, Any] = (
        await candidate_context(session, candidate) if candidate is not None else {}
    )
    base.update(
        {
            "template_ref": _template_ref(rule),
            "rule": {
                "rule_instance_id": rule.id,
                "version": rule.version,
                "status": rule.status,
                "params": rule_params(rule),
            },
        }
    )
    base.update(extra)
    return base


async def edit_rule_params(
    session: AsyncSession,
    rule_instance_id: int,
    params: dict[str, float],
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> tuple[RuleInstanceRecord, DecisionRecord]:
    """New draft version with ``params`` merged over the rule's overrides.

    Validated against the template's ``param_ranges``
    (:class:`RuleValidationError` -> 422). An open candidate pointing at the
    edited rule follows the new version. Writes an ``edit_rule`` decision.
    """

    at = now or _now()
    base = await _rule(session, rule_instance_id)
    if base.status == "retired":
        raise RuleNotEditableError(f"rule instance {rule_instance_id} is retired")
    instance = instance_from_record(base)
    before = dict(instance.params)
    edited = instance.model_copy(update={"params": {**before, **params}, "drafted_by": USER})
    try:
        new = await RuleStore(session).edit(base.id, edited, now=at)
    except RuleStoreError as exc:
        raise CandidateConflictError(str(exc)) from exc
    candidate = await _open_candidate_for(session, base.listing_id)
    if candidate is not None and candidate.draft_rule_instance_id in {None, base.id}:
        candidate.draft_rule_instance_id = new.id
        candidate.draft_state = "ready"
    await session.flush()
    decision = await DecisionRepository(session).append(
        listing_id=new.listing_id,
        ticker=new.symbol,
        kind="edit_rule",
        actor=USER,
        reason_text=reason,
        rule_instance_id=new.id,
        rule_ref=rule_ref_of(new),
        candidate_id=candidate.id if candidate is not None else None,
        context=await _rule_context(
            session,
            new,
            edited_from={"rule_instance_id": base.id, "version": base.version},
            params_before=before,
            params_after=dict(edited.params),
        ),
        at=at,
    )
    return new, decision


async def reject_rule(
    session: AsyncSession,
    rule_instance_id: int,
    *,
    reason: str,
    now: datetime | None = None,
) -> tuple[RuleInstanceRecord, DecisionRecord]:
    """Retire a draft with a required reason (``reject_rule`` decision).

    The reason feeds later drafts as a preference note; an open candidate
    that pointed at the draft returns to ``draft_state = none``.
    """

    if not reason or not reason.strip():
        raise CandidateInputError("rejecting a rule requires a non-empty reason")
    at = now or _now()
    rule = await _rule(session, rule_instance_id)
    if rule.status != "draft":
        raise RuleNotEditableError(
            f"only draft rules can be rejected (#{rule_instance_id} is {rule.status})"
        )
    context = await _rule_context(session, rule)
    await RuleStore(session).retire(rule.id, now=at)
    candidate = await _open_candidate_for(session, rule.listing_id)
    if candidate is not None and candidate.draft_rule_instance_id in {None, rule.id}:
        candidate.draft_rule_instance_id = None
        candidate.draft_state = "none"
    await session.flush()
    decision = await DecisionRepository(session).append(
        listing_id=rule.listing_id,
        ticker=rule.symbol,
        kind="reject_rule",
        actor=USER,
        reason_text=reason,
        rule_instance_id=rule.id,
        rule_ref=rule_ref_of(rule),
        candidate_id=candidate.id if candidate is not None else None,
        context=context,
        at=at,
    )
    return rule, decision


# --------------------------------------------------------------------------- #
# Promote
# --------------------------------------------------------------------------- #


@dataclass
class PromotionOutcome:
    candidate: CandidateRecord
    rule: RuleInstanceRecord
    previous_active_id: int | None
    watchlist_item: WatchlistItem
    metadata: WatchMetadata
    decisions: list[DecisionRecord]
    research_run_id: int | None = None
    verification_requested: bool = False
    warnings: list[str] = field(default_factory=list)


async def _resolve_rule(
    session: AsyncSession, candidate: CandidateRecord, rule_instance_id: int | None
) -> RuleInstanceRecord:
    rule: RuleInstanceRecord | None
    if rule_instance_id is not None:
        rule = await _rule(session, rule_instance_id)
        if rule.listing_id != candidate.listing_id:
            raise CandidateConflictError(
                f"rule instance {rule_instance_id} belongs to {rule.symbol}, not {candidate.ticker}"
            )
    else:
        state, draft = await effective_draft(session, candidate)
        rule = draft if draft is not None and draft.status != "retired" else None
        if rule is None:
            rule = await RuleStore(session).active_instance(candidate.listing_id, CANDIDATE_SCOPE)
        if rule is None:
            raise CandidateConflictError(
                f"no rule to approve for candidate {candidate.id}: rule drafting not ready "
                f"(draft_state={state}); wait for the draft or pass rule_instance_id"
            )
    if rule.scope != CANDIDATE_SCOPE:
        raise CandidateConflictError(f"rule instance {rule.id} has scope {rule.scope!r}")
    if rule.status == "retired":
        raise CandidateConflictError(f"rule instance {rule.id} is retired")
    return rule


async def promote_candidate(
    session: AsyncSession,
    candidate_id: int,
    *,
    rule_instance_id: int | None = None,
    params_override: dict[str, float] | None = None,
    reason: str | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> PromotionOutcome:
    """Steps 1-5 of the promote flow in the caller's transaction."""

    at = now or _now()
    day = today or shanghai_today()
    candidate = await open_candidate(session, candidate_id)
    rule = await _resolve_rule(session, candidate, rule_instance_id)
    decisions: list[DecisionRecord] = []
    if params_override:
        rule, edit = await edit_rule_params(
            session, rule.id, dict(params_override), reason=reason, now=at
        )
        decisions.append(edit)
    store = RuleStore(session)
    previous_active_id: int | None = None
    if rule.status == "draft":
        previous = await store.active_instance(rule.listing_id, rule.scope)
        previous_active_id = previous.id if previous is not None else None
        rule = await store.approve(rule.id, approved_by=USER, now=at)
        decisions.append(
            await DecisionRepository(session).append(
                listing_id=rule.listing_id,
                ticker=rule.symbol,
                kind="approve_rule",
                actor=USER,
                reason_text=reason,
                rule_instance_id=rule.id,
                rule_ref=rule_ref_of(rule),
                candidate_id=candidate.id,
                context=await candidate_context(
                    session,
                    candidate,
                    template_ref=_template_ref(rule),
                    retired_rule_instance_id=previous_active_id,
                ),
                at=at,
            )
        )
    metadata = await watch_metadata(session, candidate.listing_id, day)
    watchlist = WatchlistRepository(session)
    existing = await watchlist.get_by_listing(candidate.listing_id)
    item = await watchlist.add(
        candidate.listing_id,
        industry=(
            metadata.industry if existing is None or not existing.industry else None
        ),
        provider="ifind" if existing is None else None,
        report_year=(
            metadata.report_year if existing is None or existing.report_year is None else None
        ),
        enabled=True,
    )
    context = await candidate_context(
        session,
        candidate,
        template_ref=_template_ref(rule),
        watchlist={
            "industry": item.industry,
            "industry_source": metadata.industry_source,
            "report_year": item.report_year,
            "report_year_source": metadata.report_year_source,
        },
    )
    candidate.status = "promoted"
    candidate.closed_at = at
    candidate.draft_rule_instance_id = rule.id
    candidate.draft_state = "ready"
    await session.flush()
    decisions.append(
        await DecisionRepository(session).append(
            listing_id=candidate.listing_id,
            ticker=candidate.ticker,
            kind="promote",
            actor=USER,
            reason_text=reason,
            rule_instance_id=rule.id,
            rule_ref=rule_ref_of(rule),
            candidate_id=candidate.id,
            context=context,
            at=at,
        )
    )
    return PromotionOutcome(
        candidate=candidate,
        rule=rule,
        previous_active_id=previous_active_id,
        watchlist_item=item,
        metadata=metadata,
        decisions=decisions,
    )


async def _request_promote_verification(
    *, listing_id: int, ticker: str, detail: dict[str, Any]
) -> str | None:
    """Ask for an iFinD ``promote`` verification (P1-A13); returns a warning
    instead of raising. The verification package is optional at import time:
    without it the request is skipped and logged."""

    try:
        module = importlib.import_module("investment_agent.verification")
        enqueue_verification = module.enqueue_verification
    except (ImportError, AttributeError):
        log.warning("candidates.promote.verification_unavailable", ticker=ticker)
        return "iFinD verification is not available; promote verification skipped"
    try:
        result = enqueue_verification(
            listing_id=listing_id,
            ticker=ticker,
            trigger="promote",
            fields=None,
            report_period=None,
            detail=detail,
            run_id=None,
        )
        if inspect.isawaitable(result):
            await result
    except Exception as exc:  # noqa: BLE001 - must not undo the promotion
        log.exception("candidates.promote.verification_failed", ticker=ticker)
        return f"promote verification request failed: {type(exc).__name__}: {exc}"
    return None


async def _enqueue_promote_research(
    outcome: PromotionOutcome, *, settings: Settings | None, as_of: date
) -> tuple[int | None, str | None]:
    item = outcome.watchlist_item
    ticker = outcome.candidate.ticker
    if settings is not None and not settings.ifind_proxy_api_key:
        return None, "research not queued: IFIND_PROXY_API_KEY is not configured"
    if not item.industry:
        return None, "research not queued: no industry known for the watchlist entry"
    if item.report_year is None:
        return None, "research not queued: no report year known for the watchlist entry"
    try:
        async with session_scope() as session:
            run, _created = await enqueue_single_stock_research(
                session,
                listing_id=item.listing_id,
                ticker=ticker,
                industry=item.industry,
                report_year=item.report_year,
                as_of=as_of,
                triggered_by="promote",
                idempotency_key=f"promote:{outcome.candidate.id}",
            )
            return run.id, None
    except Exception as exc:  # noqa: BLE001 - must not undo the promotion
        log.exception("candidates.promote.research_enqueue_failed", ticker=ticker)
        return None, f"research enqueue failed: {type(exc).__name__}: {exc}"


async def run_promotion(
    candidate_id: int,
    *,
    rule_instance_id: int | None = None,
    params_override: dict[str, float] | None = None,
    reason: str | None = None,
    settings: Settings | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> PromotionOutcome:
    """The full promote flow: one transaction, then verification + research."""

    day = today or shanghai_today()
    async with session_scope() as session:
        outcome = await promote_candidate(
            session,
            candidate_id,
            rule_instance_id=rule_instance_id,
            params_override=params_override,
            reason=reason,
            today=day,
            now=now,
        )
    warning = await _request_promote_verification(
        listing_id=outcome.candidate.listing_id,
        ticker=outcome.candidate.ticker,
        detail={
            "candidate_id": outcome.candidate.id,
            "rule_instance_id": outcome.rule.id,
            "rule_ref": rule_ref_of(outcome.rule),
            "as_of": day.isoformat(),
        },
    )
    outcome.verification_requested = warning is None
    if warning is not None:
        outcome.warnings.append(warning)
    research_run_id, warning = await _enqueue_promote_research(outcome, settings=settings, as_of=day)
    outcome.research_run_id = research_run_id
    if warning is not None:
        outcome.warnings.append(warning)
    if outcome.metadata.report_year_source != "annual_snapshot":
        outcome.warnings.append(
            f"report_year {outcome.metadata.report_year} estimated from the annual-report "
            "deadline (no annual financial snapshot available)"
        )
    if outcome.metadata.industry is None:
        outcome.warnings.append("no Shenwan / CSRC industry classification found")
    log.info(
        "candidates.promoted",
        candidate_id=outcome.candidate.id,
        ticker=outcome.candidate.ticker,
        rule_instance_id=outcome.rule.id,
        research_run_id=research_run_id,
        warnings=outcome.warnings,
    )
    return outcome


__all__ = [
    "PromotionOutcome",
    "RuleNotEditableError",
    "RuleValidationError",
    "WatchMetadata",
    "edit_rule_params",
    "promote_candidate",
    "reject_rule",
    "run_promotion",
    "watch_metadata",
]
