"""Observation-entry rule drafts (观察池规则收口, watch-holding-plan 1.5).

An entry in the watchlist either has an approved rule (``active``), an open
draft awaiting review, a drafting job in flight, a failure, a rejection - or
no drafting template chosen yet. This module owns that lifecycle:

* :func:`sync_add_state` - what ``POST /watchlist`` does after the entry row
  exists: keep the current rule/draft, enqueue a request when a template was
  chosen, or surface the ``needs_template`` gap. A repeated add never replaces
  a rule or a live request (plan 1.5 重复提交 / 规则归属).
* :func:`create_draft_request` - one request identity (UUID) + the
  ``rule_draft`` queue job in the caller's transaction. An explicit re-draft
  supersedes the previous open request; the old job may still finish, but the
  store-and-link guard (:func:`store_watch_draft`) only accepts the request the
  entry currently points at, so a late worker can leave audit rows and nothing
  else (plan 1.5 过期任务隔离).
* :func:`mark_claimed` / :func:`mark_draft_result` - worker-side transitions
  (``queued`` -> ``drafting`` -> ``ready`` | ``failed``), all guarded by the
  same "is this still the current request" check.
* :func:`approve_draft` / :func:`edit_draft_params` / :func:`reject_draft` -
  the user review actions from the watchlist page, with their decision journal
  entries. Rules stay shared per ``listing_id + scope`` with the candidate
  flow: approving here never creates a candidate row and never promotes.
* :func:`remove_entry` - removal requires a reason (decision ``remove_watch``);
  open requests are cancelled so a late worker cannot revive the entry.

The drafting job itself (:func:`investment_agent.rules.drafting.draft_rule`)
stays the single implementation; the watchlist origin only adds the request
identity, the guarded store and the completion callback.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import structlog
from sqlalchemy import select, update
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
from investment_agent.rules.generation import (
    claim_draft_generation,
    guard_store,
    is_current_token,
)
from investment_agent.rules.rule_view import rule_block
from investment_agent.rules.store import RuleStore, RuleStoreError, instance_from_record
from investment_agent.rules.zone_detection import zone_summaries
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import DecisionRecord
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.rules import RuleInstanceRecord
from investment_agent.storage.models.user_domain import WatchDraftRequest, WatchlistItem
from investment_agent.storage.repositories.candidate import DecisionRepository

log = structlog.get_logger(__name__)

CANDIDATE_SCOPE = "candidate"
USER = "user"
LIVE_REQUEST_STATUSES = frozenset({"queued", "drafting"})
OPEN_REQUEST_STATUSES = frozenset({"queued", "drafting", "ready"})


class WatchDraftError(Exception):
    """Base of the watchlist-rule errors; ``status_code`` is the HTTP mapping."""

    status_code = 400


class WatchDraftNotFoundError(WatchDraftError):
    status_code = 404


class WatchDraftConflictError(WatchDraftError):
    status_code = 409


class WatchDraftInputError(WatchDraftError):
    status_code = 422


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- #
# Queries
# --------------------------------------------------------------------------- #


async def newest_request(
    session: AsyncSession, item: WatchlistItem
) -> WatchDraftRequest | None:
    if item.draft_request_id is None:
        return None
    return await session.get(WatchDraftRequest, item.draft_request_id)


async def _locked_item(session: AsyncSession, watchlist_id: int) -> WatchlistItem | None:
    # SELECT FOR UPDATE is ignored by SQLite; reserve its writer first. The
    # no-op self-assignment keeps ``updated_at`` in place - the lock is not a
    # change and must not refresh the audit timestamp.
    await session.execute(
        update(WatchlistItem)
        .where(WatchlistItem.id == watchlist_id)
        .values(
            draft_state=WatchlistItem.draft_state,
            updated_at=WatchlistItem.updated_at,
        )
    )
    stmt = (
        select(WatchlistItem)
        .where(WatchlistItem.id == watchlist_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def _ticker_text(session: AsyncSession, listing_id: int) -> str:
    listing = await session.get(Listing, listing_id)
    if listing is None:
        raise WatchDraftInputError(f"no listing {listing_id}")
    return format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)


async def _ticker_of(session: AsyncSession, listing_id: int) -> str:
    """The formatted ticker, A-share only (rule drafting scope)."""

    ticker = await _ticker_text(session, listing_id)
    if parse_a_share_ticker(ticker) is None:
        raise WatchDraftInputError(
            f"rule drafting only supports A-share listings ({ticker!r})"
        )
    return ticker


async def watch_rule_context(
    session: AsyncSession,
    item: WatchlistItem,
    *,
    rule: RuleInstanceRecord | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Decision context: the entry's state, current request and both rules."""

    active = await RuleStore(session).active_instance(item.listing_id, CANDIDATE_SCOPE)
    request = await newest_request(session, item)
    context: dict[str, Any] = {
        "source": "watchlist",
        "watchlist": {
            "watchlist_id": item.id,
            "listing_id": item.listing_id,
            "draft_state": item.draft_state,
        },
        "request": (
            {
                "id": request.id,
                "status": request.status,
                "template_ref": f"{request.template_id}@{request.template_version}",
            }
            if request is not None
            else None
        ),
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
    if rule is not None:
        context["template_ref"] = f"{rule.template_id}@{rule.template_version}"
        context["rule"] = {
            "rule_instance_id": rule.id,
            "version": rule.version,
            "status": rule.status,
            "params": rule_params(rule),
        }
    context.update(extra)
    return context


async def rule_snapshot(session: AsyncSession, item: WatchlistItem) -> dict[str, Any]:
    """The ``rule`` block of a watchlist item (API + MCP shared shape)."""

    store = RuleStore(session)
    active = await store.active_instance(item.listing_id, CANDIDATE_SCOPE)
    request = await newest_request(session, item)
    draft: RuleInstanceRecord | None = None
    if item.draft_rule_instance_id is not None:
        draft = await session.get(RuleInstanceRecord, item.draft_rule_instance_id)
    if draft is not None and (draft.status != "draft" or item.draft_state != "ready"):
        draft = None
    # The rule now fires: the daily zone detection (WP8) records buy_zone / drop
    # events for this listing, and the interval block is what the page shows.
    summaries = await zone_summaries(
        session, listing_ids=[item.listing_id], scope=CANDIDATE_SCOPE
    )
    return {
        "state": item.draft_state,
        "request": (
            {
                "id": request.id,
                "status": request.status,
                "template_ref": f"{request.template_id}@{request.template_version}",
                "error": request.error,
                "reason": request.reason,
            }
            if request is not None
            else None
        ),
        "draft": (
            await rule_block(session, draft, include_definition=True, store=store)
            if draft is not None
            else None
        ),
        "active": (
            await rule_block(session, active, include_definition=False, store=store)
            if active is not None
            else None
        ),
        "zone_events": summaries.get(item.listing_id),
    }


def migration_request_id(item_id: int, *, template_ref: str, as_of: date) -> str:
    """Deterministic request identity of a legacy-entry migration (plan 6.2 WP9).

    A batch that marks stored ``needs_template`` entries for drafting can be
    re-run: the identity is derived from the entry, template and date, so a
    second run reuses the same request (and its queue job) instead of creating
    another drafting intent.
    """

    return str(uuid5(NAMESPACE_URL, f"watchlist-migration:{item_id}:{template_ref}:{as_of.isoformat()}"))


async def create_draft_request(
    session: AsyncSession,
    item: WatchlistItem,
    *,
    template_id: str,
    as_of: date,
    triggered_by: str,
    request_id: str | None = None,
) -> tuple[WatchDraftRequest, int]:
    """One new request identity + its ``rule_draft`` run (caller's transaction).

    The entry, the request and the queue job commit together (plan 1.5
    添加与入队一致性); the previous open request is superseded - its worker
    stays audit-only. A deterministic ``request_id`` (migration) that already
    exists is returned as is, making the migration re-runnable. Returns
    ``(request, run_id)``.
    """

    record = await RuleStore(session).latest_template_record(template_id)
    if record is None:
        raise WatchDraftInputError(f"unknown template {template_id!r}")
    ticker = await _ticker_of(session, item.listing_id)
    locked = await _locked_item(session, item.id)
    if locked is not None:
        item = locked
    if request_id is not None:
        existing = await session.get(WatchDraftRequest, request_id)
        if existing is not None:
            return existing, await _request_run(session, existing.id)
    previous = await newest_request(session, item)
    request = WatchDraftRequest(
        id=request_id or str(uuid4()),
        watchlist_id=item.id,
        listing_id=item.listing_id,
        template_id=template_id,
        template_version=record.version,
        as_of=as_of,
        status="queued",
    )
    session.add(request)
    if previous is not None:
        previous.superseded_by = request.id
    item.draft_request_id = request.id
    item.draft_state = "queued"
    item.draft_rule_instance_id = None
    await session.flush()
    # The request claims the shared (listing, scope) drafting generation in the
    # same transaction as the entry, the request row and the queue job (plan
    # 7.2 WP9): from here on, only this request may store.
    generation = await claim_draft_generation(
        session,
        listing_id=item.listing_id,
        symbol=ticker,
        scope=CANDIDATE_SCOPE,
        token=request.id,
        origin_kind="watchlist",
        origin_id=item.id,
        template_ref=f"{template_id}@{record.version}",
    )
    payload = RuleDraftPayload(
        candidate_id=0,
        ticker=ticker,
        template_id=template_id,
        template_version=record.version,
        as_of=as_of,
        screen_result_id=None,
        preference_notes=await preference_notes(session),
        origin_kind="watchlist",
        origin_id=item.id,
        draft_request_id=request.id,
        expected_rule_head=generation.rule_head,
    )
    run, _created = await enqueue_rule_draft(
        session,
        payload,
        triggered_by=triggered_by,
        key=rule_draft_idempotency_key(payload),
    )
    log.info(
        "watchlist.draft.enqueued",
        watchlist_id=item.id,
        listing_id=item.listing_id,
        request_id=request.id,
        template_ref=f"{template_id}@{record.version}",
        run_id=run.id,
    )
    return request, run.id


async def _request_run(session: AsyncSession, request_id: str) -> int:
    """The ``rule_draft`` run of an already-created watchlist request."""

    stmt = (
        select(PipelineRun)
        .where(
            PipelineRun.kind == RULE_DRAFT,
            PipelineRun.request_json["draft_request_id"].as_string() == request_id,
        )
        .order_by(PipelineRun.id.desc())
        .limit(1)
    )
    run = (await session.execute(stmt)).scalar_one_or_none()
    if run is None:
        raise WatchDraftInputError(f"no rule_draft run for request {request_id}")
    return run.id


async def migrate_pending_entries(
    session: AsyncSession,
    *,
    template_for: Callable[[WatchlistItem], str | None],
    as_of: date,
    triggered_by: str = "migration",
) -> list[int]:
    """Enqueue drafting for stored entries without a rule or a live request.

    One deterministic request per entry (re-runnable, never duplicated) - the
    plan 1.5 存量处理 boundary: entries with an active rule or an open request
    are left alone instead of being marked "待批准".
    """

    created: list[int] = []
    entries = list(
        (
            await session.execute(
                select(WatchlistItem).order_by(WatchlistItem.id)
            )
        ).scalars()
    )
    for item in entries:
        if item.draft_state in {*OPEN_REQUEST_STATUSES, "rejected"}:
            continue  # a live request or an explicit rejection is never auto re-drafted
        if item.draft_state == "none" and (
            await RuleStore(session).active_instance(item.listing_id, CANDIDATE_SCOPE)
        ) is not None:
            continue
        template_id = template_for(item)
        if template_id is None:
            continue
        record = await RuleStore(session).latest_template_record(template_id)
        if record is None:
            continue
        request, run_id = await create_draft_request(
            session,
            item,
            template_id=template_id,
            as_of=as_of,
            triggered_by=triggered_by,
            request_id=migration_request_id(
                item.id, template_ref=f"{template_id}@{record.version}", as_of=as_of
            ),
        )
        if request.status == "queued":
            created.append(run_id)
    return created


async def redraft(
    session: AsyncSession,
    item: WatchlistItem,
    *,
    as_of: date,
    template_id: str | None = None,
    triggered_by: str = "watchlist_api",
) -> tuple[WatchDraftRequest, int]:
    """User-requested (re-)draft; the template falls back to the newest intent."""

    if template_id is None:
        request = await newest_request(session, item)
        if request is not None:
            template_id = request.template_id
        else:
            active = await RuleStore(session).active_instance(item.listing_id, CANDIDATE_SCOPE)
            template_id = active.template_id if active is not None else None
    if not template_id:
        raise WatchDraftInputError("no drafting template chosen yet")
    return await create_draft_request(
        session, item, template_id=template_id, as_of=as_of, triggered_by=triggered_by
    )


async def sync_add_state(
    session: AsyncSession,
    item: WatchlistItem,
    *,
    template_id: str | None,
    as_of: date,
    triggered_by: str,
) -> WatchDraftRequest | None:
    """Post-add reconciliation (plan 1.5 规则归属 / 重复提交).

    An active rule is never replaced by an add; a live or ready request of the
    same template is kept (a retried add reuses its identity). Without a
    template the entry surfaces ``needs_template`` unless something is already
    in place. Non-A-share entries (legacy product leftovers) never draft -
    losing an explicit template choice for one is an input error.
    """

    try:
        await _ticker_of(session, item.listing_id)
        supported = True
    except WatchDraftInputError:
        supported = False
    if not supported:
        if template_id is not None:
            raise WatchDraftInputError("rule drafting only supports A-share listings")
        item.draft_state = "none"
        await session.flush()
        return None
    active = await RuleStore(session).active_instance(item.listing_id, CANDIDATE_SCOPE)
    request = await newest_request(session, item)
    if template_id is not None:
        if active is not None:
            item.draft_state = "none"
            await session.flush()
            return request
        if request is not None and request.template_id == template_id:
            if request.status in LIVE_REQUEST_STATUSES:
                item.draft_state = request.status
                await session.flush()
                return request
            if request.status == "ready" and request.rule_instance_id is not None:
                rule = await session.get(RuleInstanceRecord, request.rule_instance_id)
                if rule is not None and rule.status == "draft":
                    item.draft_state = "ready"
                    item.draft_rule_instance_id = request.rule_instance_id
                    await session.flush()
                    return request
        created, _run_id = await create_draft_request(
            session, item, template_id=template_id, as_of=as_of, triggered_by=triggered_by
        )
        return created
    if request is not None:
        if request.status in LIVE_REQUEST_STATUSES:
            item.draft_state = request.status
            await session.flush()
            return request
        if request.status == "ready" and request.rule_instance_id is not None:
            rule = await session.get(RuleInstanceRecord, request.rule_instance_id)
            if rule is not None and rule.status == "draft":
                item.draft_state = "ready"
                item.draft_rule_instance_id = request.rule_instance_id
                await session.flush()
                return request
        if request.status in {"failed", "rejected"}:
            item.draft_state = request.status
            await session.flush()
            return request
    item.draft_state = "none" if active is not None else "needs_template"
    item.draft_rule_instance_id = None
    await session.flush()
    return request


# --------------------------------------------------------------------------- #
# Worker-side transitions (all guarded by "still the current request")
# --------------------------------------------------------------------------- #


def _is_current(item: WatchlistItem | None, request: WatchDraftRequest) -> bool:
    return (
        item is not None
        and item.draft_request_id == request.id
        and request.superseded_by is None
    )


async def mark_claimed(session: AsyncSession, request_id: str) -> bool:
    """``queued`` -> ``drafting`` when the worker claims the run.

    The entry only shows ``drafting`` while the request still owns the shared
    generation; a request a newer one superseded keeps its queued state (the
    run may still execute, but it will end stale and only leave audit).
    """

    request = await session.get(WatchDraftRequest, request_id)
    if request is None or request.status != "queued":
        return False
    item = await session.get(WatchlistItem, request.watchlist_id)
    if not _is_current(item, request):
        return False
    if not await is_current_token(
        session, listing_id=request.listing_id, scope=CANDIDATE_SCOPE, token=request.id
    ):
        log.info("watchlist.draft.claim.superseded", request_id=request.id)
        return False
    assert item is not None
    request.status = "drafting"
    item.draft_state = "drafting"
    await session.flush()
    return True


async def store_watch_draft(
    request_id: str,
    instance: Any,
    template: Any,
    *,
    now: datetime | None = None,
    expected_rule_head: str | None = None,
    stale_notes: list[str] | None = None,
) -> tuple[int, bool] | None:
    """Guarded store-and-link for a watchlist-origin draft, one transaction.

    Returns ``(rule_instance_id, reused)``, or ``None`` when the request lost
    its currency before the store (superseded, entry removed, or a newer
    request of either origin claimed the shared generation): in that case
    nothing is written - the run metadata stays the only audit (plan 1.5
    过期任务隔离, 检查与草稿入库同一事务). ``stale_notes`` receives the
    refusal reason for the run result.
    """

    from investment_agent.rules.drafting import reuse_equivalent_draft

    async with session_scope() as session:
        # Reserve the writer before reading (SQLite ignores FOR UPDATE); the
        # self-assignment keeps ``updated_at`` in place.
        await session.execute(
            update(WatchlistItem)
            .where(WatchlistItem.draft_request_id == request_id)
            .values(
                draft_state=WatchlistItem.draft_state,
                updated_at=WatchlistItem.updated_at,
            )
        )
        request = await session.get(WatchDraftRequest, request_id, with_for_update=True)
        if request is None:
            return None
        item = await _locked_item(session, request.watchlist_id)
        if not _is_current(item, request):
            request.error = "superseded before store"
            await session.flush()
            log.info(
                "watchlist.draft.store.stale",
                request_id=request.id,
                watchlist_id=request.watchlist_id,
            )
            return None
        assert item is not None
        if request.status == "ready" and request.rule_instance_id is not None:
            linked = await session.get(RuleInstanceRecord, request.rule_instance_id)
            if linked is not None and linked.status == "draft":
                return request.rule_instance_id, True
            # The produced rule was since approved or retired: a late attempt
            # cannot reopen the request (and "approved" itself is not a
            # WATCH_DRAFT_REQUEST_STATUS, so the status stays "ready").
            return None
        if request.status not in LIVE_REQUEST_STATUSES:
            return None  # A late attempt cannot reopen an approved/rejected request.
        refused = await guard_store(
            session, symbol=instance.symbol, scope=instance.scope, token=request.id
        )
        if refused is not None:
            if stale_notes is not None:
                stale_notes.append(refused)
            request.error = refused
            log.info(
                "watchlist.draft.store.superseded",
                request_id=request.id,
                watchlist_id=item.id,
                reason=refused,
            )
            return None
        if expected_rule_head is not None and await RuleStore(session).head_token(instance.symbol, instance.scope) != expected_rule_head:
            request.error = "shared rule version changed; request a new draft"
            return None
        reused = await reuse_equivalent_draft(session, instance, template)
        if reused is not None:
            rule_id, was_reused = reused.id, True
        else:
            record = await RuleStore(session).create_draft(instance, now=now)
            rule_id, was_reused = record.id, False
        request.status = "ready"
        request.rule_instance_id = rule_id
        request.error = None
        item.draft_state = "ready"
        item.draft_rule_instance_id = rule_id
        await session.flush()
        log.info(
            "watchlist.draft.store.linked",
            request_id=request.id,
            watchlist_id=item.id,
            rule_instance_id=rule_id,
            reused=was_reused,
        )
        return rule_id, was_reused


async def mark_draft_result(
    request_id: str,
    *,
    rule_instance_id: int | None = None,
    failed: bool = False,
    error: str | None = None,
    session: AsyncSession | None = None,
) -> None:
    """Terminal outcome of a watchlist draft run (the handler's ``finalize``).

    The success path is normally a no-op: :func:`store_watch_draft` already
    linked the draft in its guarded transaction. A failure marks the entry
    ``failed`` while it stays the current request; an outcome for a superseded
    or removed request only records audit text.
    """

    if session is None:
        async with session_scope() as own:
            await mark_draft_result(
                request_id,
                rule_instance_id=rule_instance_id,
                failed=failed,
                error=error,
                session=own,
            )
        return
    request = await session.get(WatchDraftRequest, request_id)
    if request is None:
        return
    item = await session.get(WatchlistItem, request.watchlist_id)
    current = _is_current(item, request)
    if failed and request.status in LIVE_REQUEST_STATUSES:
        request.error = error
        if request.status != "cancelled":
            request.status = "failed"
        if current:
            request.status = "failed"
            assert item is not None
            item.draft_state = "failed"
        log.warning(
            "watchlist.draft.failed",
            request_id=request.id,
            watchlist_id=request.watchlist_id,
            current=current,
            error=error,
        )
    elif not current and request.rule_instance_id is None:
        # Success of a superseded run that never stored (guard rejected it).
        request.error = request.error or "superseded before store"
    await session.flush()


# --------------------------------------------------------------------------- #
# User review actions
# --------------------------------------------------------------------------- #


async def _current_draft(
    session: AsyncSession, item: WatchlistItem, rule_instance_id: int
) -> RuleInstanceRecord:
    locked = await _locked_item(session, item.id)
    if locked is None:
        raise WatchDraftConflictError("observation entry was removed; refresh")
    rule = await session.get(RuleInstanceRecord, rule_instance_id)
    if rule is None:
        raise WatchDraftNotFoundError(f"no rule instance {rule_instance_id}")
    if rule.listing_id != item.listing_id or rule.scope != CANDIDATE_SCOPE:
        raise WatchDraftConflictError(
            f"rule instance {rule_instance_id} is not a rule of this observation entry"
        )
    if item.draft_state != "ready" or item.draft_rule_instance_id != rule.id or rule.status != "draft":
        raise WatchDraftConflictError(
            "rule review is out of date; refresh the watchlist and retry"
        )
    return rule


async def approve_draft(
    session: AsyncSession,
    item: WatchlistItem,
    *,
    rule_instance_id: int,
    reason: str | None = None,
    expected_active_rule_instance_id: int | None = None,
    now: datetime | None = None,
) -> tuple[RuleInstanceRecord, DecisionRecord]:
    """Approve the entry's current draft (the previous active version retires)."""

    at = now or _now()
    rule = await _current_draft(session, item, rule_instance_id)
    store = RuleStore(session)
    previous = await store.active_instance(item.listing_id, CANDIDATE_SCOPE)
    if (previous.id if previous is not None else None) != expected_active_rule_instance_id:
        raise WatchDraftConflictError("active rule changed; refresh and review both versions")
    approved = await store.approve(rule.id, approved_by=USER, now=at)
    item.draft_state = "none"
    item.draft_rule_instance_id = None
    # The request keeps its in-enum "ready" status (its row keeps pointing at
    # the approved rule via ``rule_instance_id``): the earlier status="approved"
    # write fell outside WATCH_DRAFT_REQUEST_STATUSES. The approval is carried
    # by the ``approve_rule`` decision entry and the rule's own "active"
    # status; a late worker cannot reopen the request because the rule is no
    # longer an open draft (:func:`store_watch_draft`).
    request = await newest_request(session, item)
    if request is not None and request.rule_instance_id == approved.id:
        log.info(
            "watchlist.draft.request_rule_approved",
            request_id=request.id,
            watchlist_id=item.id,
            rule_instance_id=approved.id,
        )
    await session.flush()
    decision = await DecisionRepository(session).append(
        listing_id=item.listing_id,
        ticker=approved.symbol,
        kind="approve_rule",
        actor=USER,
        reason_text=reason,
        rule_instance_id=approved.id,
        rule_ref=rule_ref_of(approved),
        context=await watch_rule_context(
            session,
            item,
            rule=approved,
            retired_rule_instance_id=previous.id if previous is not None else None,
        ),
        at=at,
    )
    log.info(
        "watchlist.draft.approved",
        watchlist_id=item.id,
        rule_instance_id=approved.id,
        version=approved.version,
    )
    return approved, decision


async def edit_draft_params(
    session: AsyncSession,
    item: WatchlistItem,
    *,
    rule_instance_id: int,
    params: Mapping[str, float],
    reason: str | None = None,
    now: datetime | None = None,
) -> tuple[RuleInstanceRecord, DecisionRecord]:
    """A new draft version with ``params`` merged over the draft's overrides."""

    at = now or _now()
    rule = await _current_draft(session, item, rule_instance_id)
    instance = instance_from_record(rule)
    before = dict(instance.params)
    edited = instance.model_copy(
        update={"params": {**before, **params}, "drafted_by": USER}
    )
    try:
        new = await RuleStore(session).edit(rule.id, edited, now=at)
    except RuleStoreError as exc:
        raise WatchDraftConflictError(str(exc)) from exc
    item.draft_rule_instance_id = new.id
    request = await newest_request(session, item)
    if request is not None and request.status == "ready":
        request.rule_instance_id = new.id
    await session.flush()
    decision = await DecisionRepository(session).append(
        listing_id=item.listing_id,
        ticker=new.symbol,
        kind="edit_rule",
        actor=USER,
        reason_text=reason,
        rule_instance_id=new.id,
        rule_ref=rule_ref_of(new),
        context=await watch_rule_context(
            session,
            item,
            rule=new,
            edited_from={"rule_instance_id": rule.id, "version": rule.version},
            params_before=before,
            params_after=dict(edited.params),
        ),
        at=at,
    )
    return new, decision


async def reject_draft(
    session: AsyncSession,
    item: WatchlistItem,
    *,
    rule_instance_id: int,
    reason: str,
    now: datetime | None = None,
) -> tuple[RuleInstanceRecord, DecisionRecord]:
    """Reject the current draft with a required reason (never auto re-drafts)."""

    if not reason or not reason.strip():
        raise WatchDraftInputError("rejecting a draft requires a non-empty reason")
    at = now or _now()
    rule = await _current_draft(session, item, rule_instance_id)
    await RuleStore(session).retire(rule.id, now=at)
    request = await newest_request(session, item)
    if request is not None and request.rule_instance_id == rule.id:
        request.status = "rejected"
        request.reason = reason.strip()
    item.draft_state = "rejected"
    item.draft_rule_instance_id = None
    await session.flush()
    decision = await DecisionRepository(session).append(
        listing_id=item.listing_id,
        ticker=rule.symbol,
        kind="reject_rule",
        actor=USER,
        reason_text=reason,
        rule_instance_id=rule.id,
        rule_ref=rule_ref_of(rule),
        context=await watch_rule_context(session, item, rule=rule),
        at=at,
    )
    return rule, decision


async def remove_entry(
    session: AsyncSession,
    item: WatchlistItem,
    *,
    reason: str,
    now: datetime | None = None,
) -> DecisionRecord:
    """Journal the removal, cancel open requests and delete the entry.

    The decision goes in before the row disappears; in-flight drafting runs
    turn audit-only because their request no longer is the item's current one
    (and the item itself is gone).
    """

    if not reason or not reason.strip():
        raise WatchDraftInputError("removing an observation entry requires a reason")
    at = now or _now()
    active = await RuleStore(session).active_instance(item.listing_id, CANDIDATE_SCOPE)
    context = await watch_rule_context(
        session,
        item,
        active_rule=(
            {
                "rule_instance_id": active.id,
                "version": active.version,
                "template_ref": f"{active.template_id}@{active.template_version}",
            }
            if active is not None
            else None
        ),
    )
    stmt = select(WatchDraftRequest).where(
        WatchDraftRequest.watchlist_id == item.id,
        WatchDraftRequest.status.in_(sorted(LIVE_REQUEST_STATUSES)),
    )
    for request in (await session.execute(stmt)).scalars():
        request.status = "cancelled"
    decision = await DecisionRepository(session).append(
        listing_id=item.listing_id,
        ticker=await _ticker_text(session, item.listing_id),
        kind="remove_watch",
        actor=USER,
        reason_text=reason,
        rule_instance_id=active.id if active is not None else None,
        rule_ref=rule_ref_of(active) if active is not None else None,
        context=context,
        at=at,
    )
    await session.delete(item)
    await session.flush()
    log.info("watchlist.removed", watchlist_id=item.id, listing_id=item.listing_id)
    return decision


__all__ = [
    "LIVE_REQUEST_STATUSES",
    "OPEN_REQUEST_STATUSES",
    "WatchDraftConflictError",
    "WatchDraftError",
    "WatchDraftInputError",
    "WatchDraftNotFoundError",
    "approve_draft",
    "create_draft_request",
    "edit_draft_params",
    "mark_claimed",
    "migrate_pending_entries",
    "migration_request_id",
    "mark_draft_result",
    "newest_request",
    "redraft",
    "reject_draft",
    "remove_entry",
    "rule_snapshot",
    "store_watch_draft",
    "sync_add_state",
    "watch_rule_context",
]
