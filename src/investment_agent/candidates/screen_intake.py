"""Candidate intake from screen runs (P1-D1, write side) plus screen-result reads.

A screen run's ``hit`` rows are the funnel's automatic source of candidates::

    run hits -> per listing, strongest hit first
        an open candidate exists -> append one source per template
                                    (a retried run replaces its own source)
        on the active watchlist  -> skipped
        dismissed in cooldown    -> skipped
        otherwise                -> open candidate + system ``nominate`` decision
                                    + ``rule_draft`` job for the strongest hit

:func:`create_candidates_from_screen` runs in the caller's transaction (it only
``flush``es, like the rest of the funnel) and hands the journal entry and the
drafting job back to :mod:`investment_agent.candidates.lifecycle`;
:func:`latest_screen_results` reads the stored screen rows for a candidate's
sources (used by the decision context and the API detail).

The dependency runs one way - ``lifecycle`` imports this module (and re-exports
the names its importers use) - so the ``lifecycle`` import inside
:func:`create_candidates_from_screen` is deliberately function-level.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.rules import ScreenResultRecord
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.candidate import (
    CandidateRepository,
    DecisionRepository,
    as_utc,
)

if TYPE_CHECKING:
    # Annotations only (the policy lives in candidates.lifecycle, which imports this
    # module, so the name must not be imported at runtime here).
    from investment_agent.candidates.lifecycle import CandidatePolicy

log = structlog.get_logger(__name__)


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

    # The journal and rule-drafting pieces stay in candidates.lifecycle; the import is
    # function-level to keep the module dependency one-way (lifecycle -> screen_intake).
    from investment_agent.candidates.lifecycle import (
        SCREEN_TRIGGER,
        _now,
        candidate_context,
        enqueue_rule_draft,
    )

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
