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
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.rules.store import RuleStore
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import CandidateRecord, DecisionRecord
from investment_agent.storage.models.rules import RuleInstanceRecord, ScreenResultRecord
from investment_agent.storage.models.user_domain import WatchlistItem
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
        "context": dict(row.context or {}),
    }


async def latest_screen_results(
    session: AsyncSession, candidate: CandidateRecord
) -> list[ScreenResultRecord]:
    """The newest screen result per template among the candidate's sources.

    Looked up by (run, template version, listing) - stable across a retried
    screen run that rewrote its rows - with the stored id as fallback.
    """

    newest: dict[str, ScreenResultRecord] = {}
    for source in candidate.sources or []:
        if source.get("kind") != "screen":
            continue
        row: ScreenResultRecord | None = None
        run_id, template_id = source.get("screen_run_id"), source.get("template_id")
        version = source.get("template_version")
        if run_id is not None and template_id and version is not None:
            stmt = select(ScreenResultRecord).where(
                ScreenResultRecord.screen_run_id == run_id,
                ScreenResultRecord.template_id == template_id,
                ScreenResultRecord.template_version == version,
                ScreenResultRecord.listing_id == candidate.listing_id,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None and source.get("screen_result_id") is not None:
            row = await session.get(ScreenResultRecord, source["screen_result_id"])
        if row is None:
            continue
        current = newest.get(row.template_id)
        if current is None or (row.as_of, row.id) >= (current.as_of, current.id):
            newest[row.template_id] = row
    return sorted(newest.values(), key=lambda item: (item.as_of, item.id), reverse=True)


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


async def enqueue_rule_draft(
    session: AsyncSession,
    candidate: CandidateRecord,
    *,
    template_id: str,
    template_version: int,
    as_of: date,
    screen_result_id: int | None,
    triggered_by: str,
) -> PipelineRun:
    """Queue the drafting job for ``candidate`` and mark its draft pending.

    Without a registered ``rule_draft`` handler the run simply stays queued.
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
    run, _created = await PipelineRunRepository(session).enqueue(
        kind=RULE_DRAFT,
        idempotency_key=rule_draft_idempotency_key(candidate.id, template_id, template_version),
        triggered_by=triggered_by,
        listing_id=candidate.listing_id,
        request_json=payload,
    )
    candidate.draft_state = "pending"
    await session.flush()
    return run


async def mark_draft_result(
    candidate_id: int,
    *,
    rule_instance_id: int | None,
    failed: bool = False,
    error: str | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Record the drafting job's outcome on the candidate.

    ``failed=True`` -> ``draft_state = failed`` (the UI offers a manual
    rule); otherwise ``ready`` with ``draft_rule_instance_id``. Opens its own
    transaction unless ``session`` is given.
    """

    if session is None:
        async with session_scope() as own:
            await mark_draft_result(
                candidate_id,
                rule_instance_id=rule_instance_id,
                failed=failed,
                error=error,
                session=own,
            )
        return
    candidate = await CandidateRepository(session).get(candidate_id)
    if candidate is None:
        raise CandidateNotFoundError(f"no candidate {candidate_id}")
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


async def effective_draft(
    session: AsyncSession, candidate: CandidateRecord
) -> tuple[str, RuleInstanceRecord | None]:
    """``(draft_state, draft rule)`` - derived lazily when the drafting job
    has stored a draft but not (yet) reported back."""

    if candidate.draft_rule_instance_id is not None:
        rule = await session.get(RuleInstanceRecord, candidate.draft_rule_instance_id)
        return candidate.draft_state, rule
    if candidate.draft_state in {"none", "pending"}:
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
            return "ready", rule
    return candidate.draft_state, None


# --------------------------------------------------------------------------- #
# Creation from screen runs
# --------------------------------------------------------------------------- #


def _screen_source(row: ScreenResultRecord) -> dict[str, Any]:
    return {
        "kind": "screen",
        "screen_run_id": row.screen_run_id,
        "template_id": row.template_id,
        "template_version": row.template_version,
        "screen_result_id": row.id,
        "as_of": row.as_of.isoformat(),
    }


def _merge_source(candidate: CandidateRecord, source: dict[str, Any]) -> bool:
    """Append ``source``; a source of the same run and template is replaced
    (retried run). Returns True when a new source was added."""

    sources = list(candidate.sources or [])
    for index, existing in enumerate(sources):
        if (
            existing.get("kind") == "screen"
            and source.get("kind") == "screen"
            and existing.get("screen_run_id") == source.get("screen_run_id")
            and existing.get("template_id") == source.get("template_id")
        ):
            sources[index] = source
            candidate.sources = sources
            return False
    sources.append(source)
    candidate.sources = sources
    return True


@dataclass
class ScreenCandidatesOutcome:
    created: list[dict[str, Any]] = field(default_factory=list)
    merged: list[dict[str, Any]] = field(default_factory=list)
    skipped_watchlist: list[str] = field(default_factory=list)
    skipped_cooldown: list[dict[str, Any]] = field(default_factory=list)
    expired: list[int] = field(default_factory=list)
    drafts_enqueued: int = 0

    def to_metadata(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "merged": self.merged,
            "skipped_watchlist": self.skipped_watchlist,
            "skipped_cooldown": self.skipped_cooldown,
            "expired": self.expired,
            "drafts_enqueued": self.drafts_enqueued,
        }


async def _watchlisted(session: AsyncSession, listing_ids: Sequence[int]) -> set[int]:
    if not listing_ids:
        return set()
    stmt = select(WatchlistItem.listing_id).where(
        WatchlistItem.listing_id.in_(list(listing_ids)), WatchlistItem.enabled.is_(True)
    )
    return set((await session.execute(stmt)).scalars())


def _new_candidate(
    *,
    listing_id: int,
    ticker: str,
    source: dict[str, Any],
    now: datetime,
    policy: CandidatePolicy,
    last_screen_result_id: int | None = None,
    note: str | None = None,
) -> CandidateRecord:
    return CandidateRecord(
        listing_id=listing_id,
        ticker=ticker,
        status="open",
        opened_at=now,
        expires_at=now + timedelta(days=policy.expiry_days),
        sources=[source],
        last_screen_result_id=last_screen_result_id,
        draft_state="none",
        note=note,
    )


async def create_candidates_from_screen(
    session: AsyncSession,
    *,
    run_id: int,
    policy: CandidatePolicy,
    now: datetime | None = None,
) -> ScreenCandidatesOutcome:
    """Open / merge candidates from the ``hit`` rows of one screen run.

    Per listing (several templates may hit it): an open candidate gets the
    sources appended; a listing on the active watchlist or dismissed within
    the cooldown is skipped; otherwise a candidate is opened with a system
    ``nominate`` decision and a ``rule_draft`` job for its strongest hit.
    """

    at = now or _now()
    stmt = (
        select(ScreenResultRecord)
        .where(ScreenResultRecord.screen_run_id == run_id, ScreenResultRecord.outcome == "hit")
        .order_by(ScreenResultRecord.listing_id, ScreenResultRecord.template_id)
    )
    by_listing: dict[int, list[ScreenResultRecord]] = {}
    for row in (await session.execute(stmt)).scalars():
        by_listing.setdefault(row.listing_id, []).append(row)
    outcome = ScreenCandidatesOutcome()
    if not by_listing:
        return outcome
    listing_ids = sorted(by_listing)
    repo = CandidateRepository(session)
    decisions = DecisionRepository(session)
    watchlisted = await _watchlisted(session, listing_ids)
    open_map = await repo.open_by_listing(listing_ids)
    cooldown_since = at - timedelta(days=policy.dismiss_cooldown_days)
    dismissed = (
        await repo.dismissed_since(listing_ids, cooldown_since)
        if policy.dismiss_cooldown_days > 0
        else {}
    )
    for listing_id in listing_ids:
        rows = sorted(
            by_listing[listing_id],
            key=lambda item: (-(item.strength or 0.0), item.template_id),
        )
        primary = rows[0]
        ticker = primary.ticker
        if listing_id in watchlisted:
            outcome.skipped_watchlist.append(ticker)
            continue
        existing = open_map.get(listing_id)
        if existing is not None:
            added = [row.template_id for row in rows if _merge_source(existing, _screen_source(row))]
            existing.last_screen_result_id = primary.id
            outcome.merged.append(
                {"candidate_id": existing.id, "ticker": ticker, "added_templates": added}
            )
            continue
        if listing_id in dismissed:
            previous = dismissed[listing_id]
            outcome.skipped_cooldown.append(
                {
                    "ticker": ticker,
                    "candidate_id": previous.id,
                    "dismissed_at": as_utc(previous.closed_at or at).isoformat(),
                }
            )
            continue
        candidate = _new_candidate(
            listing_id=listing_id,
            ticker=ticker,
            source=_screen_source(primary),
            now=at,
            policy=policy,
            last_screen_result_id=primary.id,
        )
        for row in rows[1:]:
            _merge_source(candidate, _screen_source(row))
        session.add(candidate)
        await session.flush()
        await decisions.append(
            listing_id=listing_id,
            ticker=ticker,
            kind="nominate",
            actor="system",
            reason_text="screen hit: " + ", ".join(
                f"{row.template_id}@{row.template_version}" for row in rows
            ),
            candidate_id=candidate.id,
            context=await candidate_context(session, candidate, trigger=SCREEN_TRIGGER),
            at=at,
        )
        await enqueue_rule_draft(
            session,
            candidate,
            template_id=primary.template_id,
            template_version=primary.template_version,
            as_of=primary.as_of,
            screen_result_id=primary.id,
            triggered_by=SCREEN_TRIGGER,
        )
        outcome.drafts_enqueued += 1
        outcome.created.append(
            {
                "candidate_id": candidate.id,
                "ticker": ticker,
                "templates": [f"{row.template_id}@{row.template_version}" for row in rows],
            }
        )
    await session.flush()
    log.info(
        "candidates.screen.applied",
        run_id=run_id,
        created=len(outcome.created),
        merged=len(outcome.merged),
        skipped_watchlist=len(outcome.skipped_watchlist),
        skipped_cooldown=len(outcome.skipped_cooldown),
    )
    return outcome


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
