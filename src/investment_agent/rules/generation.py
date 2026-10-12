"""Shared rule-draft generations (watch-holding-plan 7.2, WP9).

One ``(listing, scope)`` rule slot has at most one *current* draft request,
whichever origin enqueued it - the candidate lifecycle or a watchlist entry.
The enqueue claims the next generation and freezes the request identity
(``token``: the candidate run's idempotency key, or the watch request UUID)
plus the shared rule head it saw (:meth:`RuleStore.head_token`). The store
(:func:`guard_store`) and the completion callbacks re-check the frozen claim
inside their own transaction, so a request a newer one (or a rule change) has
superseded can only leave audit rows behind.

A slot without a row has never had an explicit request: legacy in-flight
candidate runs keep their historical behaviour there, while any request whose
token does not match the recorded claim is refused.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.rules.store import RuleStore
from investment_agent.storage.models.rules import RuleDraftGenerationRecord
from investment_agent.storage.repositories.listing import ListingRepository

CANDIDATE_SCOPE = "candidate"

SUPERSEDED_MESSAGE = "superseded by a newer draft request"
RULE_HEAD_MESSAGE = "shared rule version changed; request a new draft"


@dataclass(frozen=True)
class DraftGeneration:
    listing_id: int
    scope: str
    generation: int
    token: str | None
    origin_kind: str | None
    origin_id: int | None
    template_ref: str | None
    rule_head: str | None


def _state(record: RuleDraftGenerationRecord) -> DraftGeneration:
    return DraftGeneration(
        listing_id=record.listing_id,
        scope=record.scope,
        generation=record.generation,
        token=record.current_token,
        origin_kind=record.current_origin_kind,
        origin_id=record.current_origin_id,
        template_ref=record.template_ref,
        rule_head=record.rule_head,
    )


async def _locked(
    session: AsyncSession, listing_id: int, scope: str
) -> RuleDraftGenerationRecord | None:
    # SQLite ignores SELECT FOR UPDATE; reserve its writer first. The no-op
    # self-assignment keeps ``updated_at`` in place - the lock is not a change.
    await session.execute(
        update(RuleDraftGenerationRecord)
        .where(
            RuleDraftGenerationRecord.listing_id == listing_id,
            RuleDraftGenerationRecord.scope == scope,
        )
        .values(
            generation=RuleDraftGenerationRecord.generation,
            updated_at=RuleDraftGenerationRecord.updated_at,
        )
    )
    stmt = (
        select(RuleDraftGenerationRecord)
        .where(
            RuleDraftGenerationRecord.listing_id == listing_id,
            RuleDraftGenerationRecord.scope == scope,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _insert_row(
    session: AsyncSession, *, listing_id: int, scope: str
) -> RuleDraftGenerationRecord:
    """First claim of a slot: insert the row, or adopt the winner's row.

    Two concurrent first claims both read "no row" (the reservation is a no-op
    on an empty slot) and both insert; the loser hits the primary key inside
    its own savepoint, re-reads and adopts the row the winner committed, so
    the first-ever concurrent claim heals instead of erroring.
    """

    nested = await session.begin_nested()
    try:
        record = RuleDraftGenerationRecord(listing_id=listing_id, scope=scope, generation=0)
        session.add(record)
        await session.flush()
    except IntegrityError:
        await nested.rollback()
        winner = await _locked(session, listing_id, scope)
        if winner is None:  # pragma: no cover - the conflict proves the row exists
            raise
        return winner
    else:
        await nested.commit()
        return record


async def claim_draft_generation(
    session: AsyncSession,
    *,
    listing_id: int,
    symbol: str,
    scope: str,
    token: str,
    origin_kind: str,
    origin_id: int | None,
    template_ref: str | None = None,
) -> DraftGeneration:
    """Make ``token`` the current request of the slot (caller's transaction).

    Called exactly once per *new* draft request; retried enqueues that reuse an
    existing run must not claim again, or a network retry would supersede a
    later user request.
    """

    record = await _locked(session, listing_id, scope)
    if record is None:
        record = await _insert_row(session, listing_id=listing_id, scope=scope)
    record.generation += 1
    record.current_token = token
    record.current_origin_kind = origin_kind
    record.current_origin_id = origin_id
    record.template_ref = template_ref
    record.rule_head = await RuleStore(session).head_token(symbol, scope)
    await session.flush()
    return _state(record)


async def current_generation(
    session: AsyncSession, *, listing_id: int, scope: str
) -> DraftGeneration | None:
    record = await session.get(RuleDraftGenerationRecord, (listing_id, scope))
    return _state(record) if record is not None else None


async def is_current_token(
    session: AsyncSession, *, listing_id: int, scope: str, token: str | None
) -> bool:
    """``True`` when ``token`` is the slot's current claim.

    A slot without a row has no claim: that is *not* "current" (a read-only
    pre-flight must not block legacy runs, so callers distinguish the two via
    :func:`current_generation` when they need to).
    """

    if token is None:
        return False
    generation = await current_generation(session, listing_id=listing_id, scope=scope)
    return generation is not None and generation.token == token


async def preflight_reason(
    session: AsyncSession, *, symbol: str, scope: str, token: str | None
) -> str | None:
    """Read-only early check: ``None`` when the run may proceed.

    Reports a claim that a newer request already took, or a shared rule head
    that changed since the claim (the worker can then skip the model call); a
    slot without a row or claim cannot be judged, and :func:`guard_store`
    stays authoritative at store time.
    """

    if token is None:
        return None
    listing = await ListingRepository(session).get_a_share_listing(symbol)
    if listing is None:
        return None
    generation = await current_generation(session, listing_id=listing.id, scope=scope)
    if generation is None or generation.token is None:
        return None
    if generation.token != token:
        return SUPERSEDED_MESSAGE
    if generation.rule_head is not None:
        head = await RuleStore(session).head_token(symbol, scope)
        if head != generation.rule_head:
            return RULE_HEAD_MESSAGE
    return None


async def guard_store(
    session: AsyncSession, *, symbol: str, scope: str, token: str | None
) -> str | None:
    """``None`` when the request may store; otherwise the stale reason.

    Runs on the locked generation row inside the caller's store transaction
    (check and draft write commit together): a request whose token is no
    longer current - or whose frozen rule head changed - is refused. A slot
    without a row accepts the request (legacy run, no explicit claim exists).
    """

    listing = await ListingRepository(session).get_a_share_listing(symbol)
    if listing is None:
        return None  # the store path itself reports the unknown listing
    record = await _locked(session, listing.id, scope)
    if record is None:
        return None
    if token is None or record.current_token != token:
        return SUPERSEDED_MESSAGE
    if record.rule_head is not None:
        head = await RuleStore(session).head_token(symbol, scope)
        if head != record.rule_head:
            return RULE_HEAD_MESSAGE
    return None


__all__ = [
    "CANDIDATE_SCOPE",
    "RULE_HEAD_MESSAGE",
    "SUPERSEDED_MESSAGE",
    "DraftGeneration",
    "claim_draft_generation",
    "current_generation",
    "guard_store",
    "is_current_token",
    "preflight_reason",
]
