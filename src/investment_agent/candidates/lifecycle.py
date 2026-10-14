"""Candidate lifecycle (P1-D1) and its decision journal entries (P1-D2).

State machine (funnel design 2)::

    (screen hit | manual nomination) -> open
    open -> promoted   (user, promote flow; see candidates.promote)
    open -> dismissed  (user, reason required)
    open -> expired    (system, expires_at passed)

``promoted`` / ``dismissed`` / ``expired`` are terminal; a later screen hit
opens a new candidate - except within the dismiss cooldown, or while the
listing is on the active watchlist. A hit for a listing that already has an
``open`` candidate appends a source instead (idempotent per screen run and
template).

Every transition appends a ``decision`` in the caller's transaction:
``nominate`` (user for manual nominations, system for screen-created
candidates), ``dismiss``, ``expire``. Callers own the transaction - the
functions here only ``flush`` - so a failure rolls back the state change
and its decision together.

Rule drafting (decision D1): creating a candidate enqueues a ``rule_draft``
job (the drafting module consumes it) and sets ``draft_state = pending``;
the job reports back through :func:`mark_draft_result`.

Candidate intake from screen runs lives in
:mod:`investment_agent.candidates.screen_intake`; the names this module's own
importers use are re-exported below.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Screen intake lives in candidates.screen_intake (B5 split); the block both serves
# nominate_candidate and re-exports the moved names so this module's existing importers
# (rules/, api/, cli/, watchlist/, scheduler/, tests) keep working unchanged.
from investment_agent.candidates.screen_intake import (
    ScreenCandidatesOutcome as ScreenCandidatesOutcome,
)
from investment_agent.candidates.screen_intake import (
    _merge_source,
    _new_candidate,
    _watchlisted,
)
from investment_agent.candidates.screen_intake import (
    create_candidates_from_screen as create_candidates_from_screen,
)
from investment_agent.candidates.screen_intake import (
    latest_screen_results as latest_screen_results,
)
from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.providers.shenwan_directory import default_shenwan_directory
from investment_agent.rules.generation import (
    claim_draft_generation,
    current_generation,
    is_current_token,
)
from investment_agent.rules.store import RuleStore
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import CandidateRecord, DecisionRecord
from investment_agent.storage.models.rules import RuleInstanceRecord, ScreenResultRecord
from investment_agent.storage.repositories.candidate import (
    CandidateRepository,
    DecisionRepository,
    as_utc,
)
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

log = structlog.get_logger(__name__)

RULE_DRAFT = "rule_draft"
"""Job kind of the LLM rule-drafting job (P1-C5; handler lives in rules.drafting)."""
CANDIDATE_SCOPE = "candidate"
PREFERENCE_NOTE_LIMIT = 10
SCREEN_TRIGGER = "screen_run"


class CandidateError(Exception):
    """Base of the candidate-flow errors; ``status_code`` is the HTTP mapping."""

    status_code = 400


class CandidateNotFoundError(CandidateError):
    status_code = 404


class CandidateConflictError(CandidateError):
    status_code = 409


class CandidateInputError(CandidateError):
    status_code = 422


@dataclass(frozen=True)
class CandidatePolicy:
    expiry_days: int = 14
    dismiss_cooldown_days: int = 30

    @classmethod
    def from_settings(cls, settings: Settings) -> CandidatePolicy:
        return cls(
            expiry_days=settings.candidate_expiry_days,
            dismiss_cooldown_days=settings.candidate_dismiss_cooldown_days,
        )


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Decision context
# --------------------------------------------------------------------------- #


def rule_ref_of(rule: RuleInstanceRecord) -> str:
    return (
        f"{rule.symbol}/{rule.scope}/v{rule.version} "
        f"({rule.template_id}@{rule.template_version})"
    )


def rule_params(rule: RuleInstanceRecord) -> dict[str, float]:
    """The instance's parameter overrides from its stored definition."""

    raw = (rule.definition or {}).get("params")
    if not isinstance(raw, dict):
        return {}
    return {str(key): float(value) for key, value in raw.items()}


def primary_template_ref(candidate: CandidateRecord) -> str | None:
    """Template of the newest source naming one (``id@version``)."""

    for source in reversed(candidate.sources or []):
        template_id = source.get("template_id")
        if template_id:
            version = source.get("template_version")
            return f"{template_id}@{version}" if version is not None else str(template_id)
    return None


def screen_result_snapshot(row: ScreenResultRecord) -> dict[str, Any]:
    """A screen result as stored in decision context / API detail."""

    return {
        "screen_result_id": row.id,
        "screen_run_id": row.screen_run_id,
        "as_of": row.as_of.isoformat(),
        "template_id": row.template_id,
        "template_version": row.template_version,
        "template_ref": f"{row.template_id}@{row.template_version}",
        "outcome": row.outcome,
        "rank": row.rank,
        "reasons": list(row.reasons or []),
        "context": context_with_industry_name(row.context),
    }


def context_with_industry_name(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """The stored screen context plus the Shenwan industry name (display only)."""

    result = dict(context or {})
    industry = result.get("industry")
    if (
        isinstance(industry, dict)
        and industry.get("system") == "sw2021"
        and not industry.get("name")
    ):
        name = default_shenwan_directory().name_of(industry.get("code"))
        if name:
            result["industry"] = {**industry, "name": name}
    return result


async def candidate_context(
    session: AsyncSession, candidate: CandidateRecord, **extra: Any
) -> dict[str, Any]:
    """Decision context: sources plus the screen reasons and factor values
    (with their snapshot refs) the user saw at decision time."""

    results = await latest_screen_results(session, candidate)
    context: dict[str, Any] = {
        "candidate_id": candidate.id,
        "candidate_status": candidate.status,
        "template_ref": primary_template_ref(candidate),
        "sources": list(candidate.sources or []),
        "screen_results": [screen_result_snapshot(row) for row in results],
    }
    context.update(extra)
    return context


# --------------------------------------------------------------------------- #
# Rule drafting
# --------------------------------------------------------------------------- #


def rule_draft_idempotency_key(candidate_id: int, template_id: str, template_version: int) -> str:
    return f"{RULE_DRAFT}:{candidate_id}:{template_id}@{template_version}"


def preference_note(decision: DecisionRecord) -> str:
    template = (decision.context or {}).get("template_ref") or "no-template"
    return f"[{template}] {decision.kind} {decision.ticker}: {decision.reason_text or ''}".rstrip()


async def preference_notes(
    session: AsyncSession, *, limit: int = PREFERENCE_NOTE_LIMIT
) -> list[str]:
    """The user's latest ``reject_rule`` / ``dismiss`` reasons, newest first."""

    decisions = await DecisionRepository(session).recent(limit=limit)
    return [preference_note(item) for item in decisions]


def rule_draft_payload(
    *,
    candidate_id: int,
    ticker: str,
    template_id: str,
    template_version: int,
    as_of: date,
    screen_result_id: int | None,
    preference_notes: Sequence[str],
) -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "ticker": ticker,
        "template_id": template_id,
        "template_version": template_version,
        "as_of": as_of.isoformat(),
        "screen_result_id": screen_result_id,
        "preference_notes": list(preference_notes),
    }


_LIVE_RUN_STATUSES = frozenset({"queued", "running"})


def _draft_identity(payload: Mapping[str, object]) -> dict[str, object]:
    """Payload comparison ignores the frozen generation token.

    The token is deterministic per run identity (it *is* the idempotency key),
    so a retried enqueue still recognises its own run; publishing the field
    must not turn the retry into a new draft intent (plan 7.2 WP9).
    """

    return {key: value for key, value in payload.items() if key != "draft_token"}


async def _draft_run_key(
    repository: PipelineRunRepository,
    base_key: str,
    payload: Mapping[str, object],
    *,
    fresh: bool = False,
) -> str:
    """``base_key`` for the first draft; ``base_key:r<n>`` for each re-draft.

    An enqueue with an identical payload reuses its run (a retried screen run
    stays idempotent); a different payload (new preference notes, manual
    trigger) gets its own run instead of tripping the payload-mismatch guard.
    ``fresh`` (user re-draft) only reuses a run that is still queued/running.
    """

    key = base_key
    attempt = 1
    while True:
        existing = await repository.by_idempotency_key(kind=RULE_DRAFT, idempotency_key=key)
        if existing is None:
            return key
        if (
            _draft_identity(existing.request_json or {}) == _draft_identity(payload)
            and (not fresh or existing.status in _LIVE_RUN_STATUSES)
        ):
            return key
        key = f"{base_key}:r{attempt}"
        attempt += 1


async def enqueue_rule_draft(
    session: AsyncSession,
    candidate: CandidateRecord,
    *,
    template_id: str,
    template_version: int,
    as_of: date,
    screen_result_id: int | None,
    triggered_by: str,
    fresh: bool = False,
) -> PipelineRun:
    """Queue the drafting job for ``candidate`` and mark its draft pending.

    A *new* run claims the shared ``(listing, scope)`` drafting generation
    (plan 7.2 WP9): the token is frozen into the payload and the run may store
    only while it stays current. A reused run (idempotent retry) must not
    re-claim - a network retry is not a new drafting intent. Without a
    registered ``rule_draft`` handler the run simply stays queued.
    """

    payload = rule_draft_payload(
        candidate_id=candidate.id,
        ticker=candidate.ticker,
        template_id=template_id,
        template_version=template_version,
        as_of=as_of,
        screen_result_id=screen_result_id,
        preference_notes=await preference_notes(session),
    )
    repository = PipelineRunRepository(session)
    key = await _draft_run_key(
        repository,
        rule_draft_idempotency_key(candidate.id, template_id, template_version),
        payload,
        fresh=fresh,
    )
    payload["draft_token"] = key
    run, created = await repository.enqueue(
        kind=RULE_DRAFT,
        idempotency_key=key,
        triggered_by=triggered_by,
        listing_id=candidate.listing_id,
        request_json=payload,
    )
    if created:
        await claim_draft_generation(
            session,
            listing_id=candidate.listing_id,
            symbol=candidate.ticker,
            scope=CANDIDATE_SCOPE,
            token=key,
            origin_kind="candidate",
            origin_id=candidate.id,
            template_ref=f"{template_id}@{template_version}",
        )
    candidate.draft_state = "pending"
    await session.flush()
    return run


async def _newer_request_in_flight(session: AsyncSession, candidate: CandidateRecord) -> bool:
    """``True`` when the slot's current claim is a still-live draft request of
    this candidate (a newer one superseded the reporting run)."""

    generation = await current_generation(
        session, listing_id=candidate.listing_id, scope=CANDIDATE_SCOPE
    )
    if generation is None or generation.token is None:
        return False
    if generation.origin_kind != "candidate" or generation.origin_id != candidate.id:
        return False
    run = await PipelineRunRepository(session).by_idempotency_key(
        kind=RULE_DRAFT, idempotency_key=generation.token
    )
    return run is not None and run.status in _LIVE_RUN_STATUSES


async def mark_draft_result(
    candidate_id: int,
    *,
    rule_instance_id: int | None,
    failed: bool = False,
    error: str | None = None,
    draft_token: str | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Record the drafting job's outcome on the candidate.

    ``failed=True`` -> ``draft_state = failed`` (the UI offers a manual
    rule); otherwise ``ready`` with ``draft_rule_instance_id``. Opens its own
    transaction unless ``session`` is given.

    ``draft_token`` makes the callback re-check the shared generation inside
    that transaction (plan 7.2 WP9): a run a newer request has superseded
    leaves audit only - it never links its rule as the candidate's draft and
    never flips a shared open draft to a stale one. A failure is still
    recorded (the attempt produced nothing), matching the watchlist side.
    """

    if session is None:
        async with session_scope() as own:
            await mark_draft_result(
                candidate_id,
                rule_instance_id=rule_instance_id,
                failed=failed,
                error=error,
                draft_token=draft_token,
                session=own,
            )
        return
    candidate = await CandidateRepository(session).get(candidate_id)
    if candidate is None:
        raise CandidateNotFoundError(f"no candidate {candidate_id}")
    if draft_token is not None and not await is_current_token(
        session, listing_id=candidate.listing_id, scope=CANDIDATE_SCOPE, token=draft_token
    ):
        log.warning(
            "candidate.draft.superseded_callback",
            candidate_id=candidate_id,
            draft_token=draft_token,
            rule_instance_id=rule_instance_id,
            failed=failed,
        )
        # Never link this run's rule (it may be stale); show whatever the
        # shared open draft currently is - the effective_draft derivation.
        open_draft = await _newest_open_draft(session, candidate)
        if open_draft is not None:
            candidate.draft_state = "ready"
            candidate.draft_rule_instance_id = open_draft.id
        else:
            # No shared open draft to adopt: a stale success must not leave
            # its own (possibly stale) rule linked, and the state only moves
            # to "failed" when nothing newer is drafting for this candidate -
            # while a newer request is queued/running, its "pending" stands
            # (the slot owner reports; the stale callback is audit only,
            # matching the watchlist side).
            candidate.draft_rule_instance_id = None
            if (
                (failed or rule_instance_id is None)
                and not await _newer_request_in_flight(session, candidate)
            ):
                candidate.draft_state = "failed"
                if error:
                    log.warning("candidate.draft_failed", candidate_id=candidate_id, error=error)
        await session.flush()
        return
    if failed or rule_instance_id is None:
        candidate.draft_state = "failed"
        if error:
            log.warning("candidate.draft_failed", candidate_id=candidate_id, error=error)
    else:
        rule = await session.get(RuleInstanceRecord, rule_instance_id)
        if rule is None or rule.listing_id != candidate.listing_id:
            raise CandidateInputError(
                f"rule instance {rule_instance_id} is not a rule of candidate {candidate_id}"
            )
        candidate.draft_state = "ready"
        candidate.draft_rule_instance_id = rule_instance_id
    await session.flush()


async def _newest_open_draft(
    session: AsyncSession, candidate: CandidateRecord
) -> RuleInstanceRecord | None:
    """The newest open candidate-scope draft of the listing, opened after the
    candidate itself (a draft from before its nomination is not its draft)."""

    stmt = (
        select(RuleInstanceRecord)
        .where(
            RuleInstanceRecord.listing_id == candidate.listing_id,
            RuleInstanceRecord.scope == CANDIDATE_SCOPE,
            RuleInstanceRecord.status == "draft",
        )
        .order_by(RuleInstanceRecord.version.desc())
        .limit(1)
    )
    rule = (await session.execute(stmt)).scalar_one_or_none()
    # ``created_at`` is a server default (whole seconds on SQLite).
    opened = as_utc(candidate.opened_at).replace(microsecond=0)
    if rule is not None and as_utc(rule.created_at) >= opened:
        return rule
    return None


async def effective_draft(
    session: AsyncSession, candidate: CandidateRecord
) -> tuple[str, RuleInstanceRecord | None]:
    """``(draft_state, draft rule)`` - derived lazily when the drafting job
    has stored a draft but not (yet) reported back."""

    if candidate.draft_rule_instance_id is not None:
        rule = await session.get(RuleInstanceRecord, candidate.draft_rule_instance_id)
        return candidate.draft_state, rule
    if candidate.draft_state in {"none", "pending"}:
        rule = await _newest_open_draft(session, candidate)
        if rule is not None:
            return "ready", rule
    return candidate.draft_state, None


# --------------------------------------------------------------------------- #
# Creation from screen runs
# --------------------------------------------------------------------------- #
# Moved to investment_agent.candidates.screen_intake (B5 split); the names this
# module's importers use are re-exported at the top of this module.


# --------------------------------------------------------------------------- #
# Expiry, manual nomination, dismiss
# --------------------------------------------------------------------------- #


async def expire_candidates(session: AsyncSession, *, now: datetime | None = None) -> list[int]:
    """Move open candidates past ``expires_at`` to ``expired`` (system decision)."""

    at = now or _now()
    expired: list[int] = []
    decisions = DecisionRepository(session)
    for candidate in await CandidateRepository(session).due_for_expiry(at):
        candidate.status = "expired"
        candidate.closed_at = at
        await session.flush()
        await decisions.append(
            listing_id=candidate.listing_id,
            ticker=candidate.ticker,
            kind="expire",
            actor="system",
            reason_text=f"open since {as_utc(candidate.opened_at).date().isoformat()}, "
            f"expired at {as_utc(candidate.expires_at).isoformat()}",
            candidate_id=candidate.id,
            rule_instance_id=candidate.draft_rule_instance_id,
            context=await candidate_context(session, candidate),
            at=at,
        )
        expired.append(candidate.id)
    if expired:
        log.info("candidates.expired", count=len(expired), candidate_ids=expired)
    return expired


@dataclass(frozen=True)
class NominationOutcome:
    candidate: CandidateRecord
    created: bool
    draft_run_id: int | None


async def nominate_candidate(
    session: AsyncSession,
    *,
    ticker: str,
    policy: CandidatePolicy,
    as_of: date,
    template_id: str | None = None,
    note: str | None = None,
    now: datetime | None = None,
) -> NominationOutcome:
    """Manual nomination: a ``manual`` source plus a user ``nominate`` decision.

    An open candidate for the listing gets the source appended (no second
    row); a listing on the active watchlist is refused. With ``template_id``
    the latest template version is drafted (when no draft is pending/ready).
    The dismiss cooldown only gates automatic screen nominations.
    """

    at = now or _now()
    template_version: int | None = None
    if template_id is not None:
        record = await RuleStore(session).latest_template_record(template_id)
        if record is None:
            raise CandidateInputError(f"unknown template {template_id!r}")
        template_version = record.version
    listing = await ListingRepository(session).ensure_a_share_listing(ticker)
    if listing.id in await _watchlisted(session, [listing.id]):
        raise CandidateConflictError(f"{ticker} is already on the active watchlist")
    source: dict[str, Any] = {
        "kind": "manual",
        "screen_run_id": None,
        "template_id": template_id,
        "template_version": template_version,
        "screen_result_id": None,
        "as_of": as_of.isoformat(),
    }
    repo = CandidateRepository(session)
    candidate = await repo.open_for_listing(listing.id)
    created = candidate is None
    if candidate is None:
        candidate = _new_candidate(
            listing_id=listing.id, ticker=ticker, source=source, now=at, policy=policy, note=note
        )
        session.add(candidate)
    else:
        _merge_source(candidate, source)
        if note is not None:
            candidate.note = note
    await session.flush()
    await DecisionRepository(session).append(
        listing_id=listing.id,
        ticker=ticker,
        kind="nominate",
        actor="user",
        reason_text=note,
        candidate_id=candidate.id,
        context=await candidate_context(session, candidate, trigger="manual"),
        at=at,
    )
    draft_run_id: int | None = None
    if (
        template_id is not None
        and template_version is not None
        and candidate.draft_state in {"none", "failed"}
    ):
        run = await enqueue_rule_draft(
            session,
            candidate,
            template_id=template_id,
            template_version=template_version,
            as_of=as_of,
            screen_result_id=None,
            triggered_by="manual",
        )
        draft_run_id = run.id
    return NominationOutcome(candidate, created, draft_run_id)


async def redraft_candidate(
    session: AsyncSession,
    candidate_id: int,
    *,
    template_id: str | None = None,
    today: date | None = None,
) -> PipelineRun:
    """Queue a fresh rule draft for an open candidate (user-requested).

    The template defaults to the candidate's newest templated source; the
    latest stored template version is drafted, with the user's latest
    rejection/dismissal reasons as preference notes. Each re-draft gets its own
    run (see :func:`_draft_run_key`).
    """

    candidate = await open_candidate(session, candidate_id)
    if template_id is None:
        ref = primary_template_ref(candidate)
        if ref is None:
            raise CandidateInputError(
                f"candidate {candidate_id} has no template; pass template_id"
            )
        template_id = ref.split("@", 1)[0]
    record = await RuleStore(session).latest_template_record(template_id)
    if record is None:
        raise CandidateInputError(f"unknown template {template_id!r}")
    screen_result_id: int | None = None
    for source in reversed(candidate.sources or []):
        if source.get("template_id") == template_id and source.get("screen_result_id"):
            screen_result_id = int(source["screen_result_id"])
            break
    return await enqueue_rule_draft(
        session,
        candidate,
        template_id=template_id,
        template_version=record.version,
        as_of=today or shanghai_today(),
        screen_result_id=screen_result_id,
        triggered_by="manual",
        fresh=True,
    )


async def open_candidate(session: AsyncSession, candidate_id: int) -> CandidateRecord:
    candidate = await CandidateRepository(session).get(candidate_id)
    if candidate is None:
        raise CandidateNotFoundError(f"no candidate {candidate_id}")
    if candidate.status != "open":
        raise CandidateConflictError(f"candidate {candidate_id} is {candidate.status}, not open")
    return candidate


async def dismiss_candidate(
    session: AsyncSession,
    candidate_id: int,
    *,
    reason: str,
    now: datetime | None = None,
) -> CandidateRecord:
    """``open -> dismissed`` with a required reason; a pending draft rule is retired."""

    if not reason or not reason.strip():
        raise CandidateInputError("a dismiss requires a non-empty reason")
    at = now or _now()
    candidate = await open_candidate(session, candidate_id)
    _state, draft = await effective_draft(session, candidate)
    context = await candidate_context(session, candidate)
    candidate.status = "dismissed"
    candidate.closed_at = at
    if draft is not None and draft.status == "draft":
        await RuleStore(session).retire(draft.id, now=at)
    await session.flush()
    await DecisionRepository(session).append(
        listing_id=candidate.listing_id,
        ticker=candidate.ticker,
        kind="dismiss",
        actor="user",
        reason_text=reason,
        candidate_id=candidate.id,
        rule_instance_id=draft.id if draft is not None else None,
        rule_ref=rule_ref_of(draft) if draft is not None else None,
        context=context,
        at=at,
    )
    return candidate


def zone_event_summary(event: Any) -> dict[str, Any]:
    """The event reference a ``drop`` decision records (rule version included)."""

    conditions = [
        {
            "factor": reason.get("factor"),
            "op": reason.get("op"),
            "threshold": reason.get("threshold"),
            "actual": reason.get("actual"),
            "quality": reason.get("quality"),
        }
        for reason in (event.conditions_met or [])
        if isinstance(reason, Mapping)
    ]
    text = "、".join(
        f"{item['factor']} {item['op']} {item['threshold']}（实际 {item['actual']}）"
        for item in conditions
    )
    return {
        "id": event.id,
        "as_of": event.as_of.isoformat(),
        "zone": event.zone,
        "level": event.level,
        "rule_instance_id": event.rule_instance_id,
        "rule_version": event.rule_version,
        "template_ref": f"{event.template_id}@{event.template_version}",
        "calibration_status": event.calibration_status,
        "conditions_met": conditions,
        "summary": text,
    }


async def archive_candidate_on_drop(
    session: AsyncSession,
    candidate: CandidateRecord,
    *,
    event: Any,
    now: datetime | None = None,
) -> DecisionRecord | None:
    """``open -> dismissed`` by a ``drop`` zone event (funnel 7.3).

    The daily zone detector archives a candidate whose ``drop`` conditions
    started holding, and records a system ``drop`` decision carrying the event
    and the rule version that fired. An already-terminal candidate is left
    alone (``None``). The event is a prompt and a record - it archives the
    candidate row only; the shared rule slot, the observation entry and the
    user's portfolio ledger are untouched.
    """

    if candidate.status != "open":
        return None
    at = now or _now()
    summary = zone_event_summary(event)
    candidate.status = "dismissed"
    candidate.closed_at = at
    await session.flush()
    decision = await DecisionRepository(session).append(
        listing_id=candidate.listing_id,
        ticker=candidate.ticker,
        kind="drop",
        actor="system",
        reason_text=(
            f"区间事件：移出候选条件成立（规则 v{event.rule_version}，"
            f"{event.template_id}@{event.template_version}）。{summary['summary']}"
        ),
        candidate_id=candidate.id,
        rule_instance_id=event.rule_instance_id,
        rule_ref=f"{candidate.ticker}/{event.template_id}@{event.template_version}"
        f"/v{event.rule_version}",
        context=await candidate_context(session, candidate, zone_event=summary),
        at=at,
    )
    log.info(
        "candidates.dropped",
        candidate_id=candidate.id,
        listing_id=candidate.listing_id,
        zone_event_id=event.id,
        rule_instance_id=event.rule_instance_id,
    )
    return decision
