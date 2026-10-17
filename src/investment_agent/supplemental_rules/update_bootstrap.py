"""Bounded, lease-fenced reconciliation for pre-journal local inputs."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, literal, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.pit_snapshot import IndustryClassification
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.research_material import ResearchDataset
from investment_agent.storage.models.supplemental_rules import (
    ResearchEventRevision,
    SupplementalRuleHead,
    SupplementalRuleRevision,
)
from investment_agent.storage.models.supplemental_updates import SupplementalChangeReceiptRow
from investment_agent.storage.repositories.supplemental_updates import (
    SupplementalUpdatesRepository,
    update_digest,
)
from investment_agent.supplemental_rules.contracts import Context

_OWNER: OwnerId = "local_owner"
_CONTEXTS: tuple[Context, ...] = (
    "company_research", "candidate", "account_fit", "holding_review",
)
_MAX_LIMIT = 100
_AUDIT_INTERVAL_SECONDS = 300
_CONFIG_HASHES = {
    name: update_digest({"feed": name, "version": 1})
    for name in (
        "bootstrap_saved_evidence", "bootstrap_research_dataset",
        "bootstrap_rule_revision", "bootstrap_research_event",
        "bootstrap_industry_classification",
    )
}


async def scan_saved_inputs(
    session: AsyncSession,
    repo: SupplementalUpdatesRepository,
    job: Any,
    limit: int = 50,
) -> dict[str, Any]:
    """Reconcile missing journals without charging for the initial history.

    Each immutable feed freezes a `(created_at, id, revision)` high-water key,
    walks it by keyset, then starts another bounded audit cycle. Repeated cycles
    catch late inserts whose UUID or creation timestamp sorts behind the cursor.
    The mutable classification feed cycles a bounded
    listing-id keyset and stores one current digest per listing. All journal,
    cursor and digest changes share the caller's leased transaction.
    """
    page_limit = min(max(limit, 1), _MAX_LIMIT)
    result: dict[str, Any] = {"processed": 0, "more": False, "feeds": {}, "blocked_reason": None}
    scanners: tuple[tuple[str, Any, Callable[..., Any]], ...] = (
        ("bootstrap_saved_evidence", SavedEvidence, _evidence_receipt),
        ("bootstrap_research_dataset", ResearchDataset, _dataset_receipt),
        ("bootstrap_research_event", ResearchEventRevision, _event_receipt),
    )
    for feed_id, model, make_receipt in scanners:
        processed, more, status, blocked = await _scan_immutable_feed(
            session, repo, job, feed_id, model, make_receipt, page_limit,
        )
        result["processed"] += processed
        result["more"] = result["more"] or more
        result["feeds"][feed_id] = status
        result["blocked_reason"] = result["blocked_reason"] or blocked
    processed, more, status, blocked = await _scan_rule_baseline(
        session, repo, job, page_limit,
    )
    result["processed"] += processed
    result["more"] = result["more"] or more
    result["feeds"]["bootstrap_rule_revision"] = status
    result["blocked_reason"] = result["blocked_reason"] or blocked
    processed, more, status = await _scan_classifications(session, repo, job, page_limit)
    result["processed"] += processed
    result["more"] = result["more"] or more
    result["feeds"]["bootstrap_industry_classification"] = status
    return result


async def _require_lease(session: AsyncSession, job: Any) -> None:
    # Import lazily because update_jobs calls this module from its own worker.
    from investment_agent.supplemental_rules.update_jobs import _require_lease as check

    await check(session, job)


async def _scan_immutable_feed(
    session: AsyncSession,
    repo: SupplementalUpdatesRepository,
    job: Any,
    feed_id: str,
    model: Any,
    make_receipt: Callable[..., Any],
    limit: int,
) -> tuple[int, bool, str, str | None]:
    await _require_lease(session, job)
    state = await repo.ensure_source(_OWNER, feed_id, "global", _CONFIG_HASHES[feed_id])
    payload = dict(state.payload)
    cursor = payload.get("cursor")
    high_water = payload.get("high_water")
    initial_complete = payload.get("initial_complete") is True
    if high_water is None and initial_complete and _audit_is_recent(payload):
        return 0, False, "caught_up", None
    created = (
        func.json_extract(model.payload, "$.created_at")
        if model is SavedEvidence else model.created_at
    )
    ident = model.id
    revision = model.revision
    owner_filter = model.owner_id == _OWNER if hasattr(model, "owner_id") else None
    if high_water is None:
        last_query = select(model)
        if owner_filter is not None:
            last_query = last_query.where(owner_filter)
        last = await session.scalar(last_query.order_by(
            created.desc(), ident.desc(), revision.desc()).limit(1))
        high_water = _immutable_key(last) if last is not None else None
    if high_water is None:
        payload["last_audit_completed_at"] = datetime.now(UTC).isoformat()
        updated = _cursor_state(state, payload, cursor=None, high_water=None,
                                initial_complete=True, blocked=None)
        await _require_lease(session, job)
        await repo.save_source(updated, expected_revision=state.revision)
        return 0, False, "caught_up", None
    try:
        query = select(model)
        if owner_filter is not None:
            query = query.where(owner_filter)
        if cursor is not None:
            query = query.where(tuple_(created, ident, revision) > _sql_key_expression(model, cursor))
        query = query.where(tuple_(created, ident, revision) <= _sql_key_expression(model, high_water))
        rows = list(await session.scalars(query.order_by(created, ident, revision).limit(limit + 1)))
        page = rows[:limit]
        initial_page = not initial_complete
        for row in page:
            receipt_fields = make_receipt(row, bootstrap_initial=initial_page)
            await _require_lease(session, job)
            await _append_if_missing(session, repo, **receipt_fields)
            cursor = _immutable_key(row)
        more = len(rows) > limit
        # The bounded query already proves that no further row exists through
        # the frozen high-water key. Do not compare driver-normalized datetime
        # values with serialized timestamps (SQLite JSON dates are text).
        finished = not more
        if finished:
            cursor = None
            high_water = None
            initial_complete = True
            payload["last_audit_completed_at"] = datetime.now(UTC).isoformat()
        updated = _cursor_state(state, payload, cursor=cursor, high_water=high_water,
                                initial_complete=initial_complete, blocked=None)
        await _require_lease(session, job)
        await repo.save_source(updated, expected_revision=state.revision)
        return len(page), more, "running" if high_water is not None else "caught_up", None
    except (ValueError, TypeError, KeyError):
        updated = _cursor_state(state, payload, cursor=cursor, high_water=high_water,
                                initial_complete=initial_complete,
                                blocked="bootstrap_invalid_saved_row")
        updated = updated.model_copy(update={
            "coverage_state": "partial", "failure_code": "bootstrap_invalid_saved_row",
            "last_attempt_at": datetime.now(UTC),
        })
        await _require_lease(session, job)
        await repo.save_source(updated, expected_revision=state.revision)
        return 0, True, "blocked", "bootstrap_invalid_saved_row"


def _immutable_key(row: Any) -> dict[str, Any]:
    # Preserve the exact timestamp string stored in the JSON column. SQLite
    # compares JSON text lexically, and normalizing `Z` to `+00:00` changes
    # cursor ordering at an otherwise identical instant.
    if isinstance(row, SavedEvidence):
        created = row.payload["created_at"]
        if not isinstance(created, str):
            raise ValueError("saved evidence created_at must be a timestamp string")
        return {"created_at": created, "id": row.id, "revision": int(row.revision)}
    created = (
        row.created_at
    )
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return {"created_at": created.astimezone(UTC).isoformat(),
            "id": row.id, "revision": int(row.revision)}


def _python_key(value: dict[str, Any]) -> tuple[datetime, int | str, int]:
    created = datetime.fromisoformat(value["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    ident = value["id"]
    return created.astimezone(UTC), ident if isinstance(ident, int) else str(ident), int(value["revision"])


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _sql_key_expression(model: Any, value: dict[str, Any]) -> Any:
    created_at: Any = value["created_at"]
    if model is not SavedEvidence:
        created_at = _python_key(value)[0]
    return tuple_(literal(created_at), literal(value["id"]), literal(int(value["revision"])))


def _cursor_state(
    state: Any, payload: dict[str, Any], *, cursor: dict[str, Any] | None,
    high_water: dict[str, Any] | None, initial_complete: bool,
    blocked: str | None,
) -> Any:
    next_payload = {**payload, "cursor": cursor, "high_water": high_water,
                    "initial_complete": initial_complete}
    if blocked is not None:
        next_payload["blocked_reason"] = blocked
    else:
        next_payload.pop("blocked_reason", None)
    now = datetime.now(UTC)
    return state.model_copy(update={
        "revision": state.revision + 1,
        "payload": next_payload,
        "last_attempt_at": now,
        "coverage_state": "partial" if blocked or high_water is not None else "complete",
        "failure_code": blocked,
        "health_limit_seconds": 0,
    })


def _evidence_receipt(row: SavedEvidence, *, bootstrap_initial: bool) -> dict[str, Any]:
    record = EvidenceRecord.model_validate(row.payload)
    return {
        "owner_id": _OWNER,
        "change_key": f"evidence:{row.id}:revision:{row.revision}",
        "kind": "evidence_revision",
        "acquired_at": record.acquired_at,
        "available_from": max(record.available_from, datetime.now(UTC)),
        "committed_at": datetime.now(UTC),
        "payload": {
            "schema_version": 1,
            "source_ref": {"kind": "evidence", "id": row.id, "revision": row.revision},
            "subject_ref": record.subject_ref.model_dump(mode="json"),
            "source_kind": record.source_kind,
            "content_hash": record.content_hash,
            "verification": record.verification,
            "bootstrap_initial": bootstrap_initial,
        },
    }


def _dataset_receipt(row: ResearchDataset, *, bootstrap_initial: bool) -> dict[str, Any]:
    available = _as_utc(row.source_fetched_at or row.captured_at)
    discovered_at = datetime.now(UTC)
    return {
        "owner_id": _OWNER,
        "change_key": f"material:{row.id}:revision:{row.revision}",
        "kind": "material_revision",
        "acquired_at": available,
        "available_from": max(available, discovered_at),
        "committed_at": discovered_at,
        "payload": {
            "schema_version": 1,
            "source_ref": {"kind": "research_dataset", "id": row.id, "revision": row.revision},
            "subject_kind": row.subject_kind, "subject_id": row.subject_id,
            "dataset_kind": row.kind, "source": row.source,
            "content_hash": row.content_hash, "raw_ref": row.raw_ref,
            "bootstrap_initial": bootstrap_initial,
        },
    }


async def _append_if_missing(
    session: AsyncSession, repo: SupplementalUpdatesRepository, **fields: Any,
) -> None:
    payload = fields["payload"]
    source_ref = payload.get("source_ref") if isinstance(payload, dict) else None
    query = select(SupplementalChangeReceiptRow).where(
        SupplementalChangeReceiptRow.owner_id == fields["owner_id"],
        SupplementalChangeReceiptRow.change_key == fields["change_key"],
    ).limit(1)
    existing_row = await session.scalar(query)
    # Hook and bootstrap may use the same immutable source identity with a
    # different local change key. Reconcile by exact source reference too.
    if isinstance(source_ref, dict):
        source_query = select(SupplementalChangeReceiptRow).where(
            SupplementalChangeReceiptRow.owner_id == fields["owner_id"],
            SupplementalChangeReceiptRow.kind == fields["kind"],
            func.json_extract(SupplementalChangeReceiptRow.payload, "$.source_ref.id")
            == str(source_ref.get("id")),
            func.json_extract(SupplementalChangeReceiptRow.payload, "$.source_ref.revision")
            == source_ref.get("revision"),
        ).order_by(SupplementalChangeReceiptRow.sequence).limit(1)
        existing_row = existing_row or await session.scalar(source_query)
    if existing_row is not None:
        existing = await repo.get_change(fields["owner_id"], existing_row.sequence)
        if (
            existing is None or existing.kind != fields["kind"]
            or existing.payload.get("source_ref") != payload.get("source_ref")
            or existing.payload.get("content_hash") != payload.get("content_hash")
        ):
            raise ValueError("existing source journal does not match saved object")
        return
    await repo.append_change(**fields)


async def _scan_classifications(
    session: AsyncSession, repo: SupplementalUpdatesRepository, job: Any, limit: int,
) -> tuple[int, bool, str]:
    feed_id = "bootstrap_industry_classification"
    await _require_lease(session, job)
    cursor_state = await repo.ensure_source(_OWNER, feed_id, "cursor", _CONFIG_HASHES[feed_id])
    cursor_payload = dict(cursor_state.payload)
    cursor = int(cursor_payload.get("cursor_listing_id", 0))
    high_water = cursor_payload.get("high_water_listing_id")
    initial_complete = cursor_payload.get("initial_complete") is True
    if high_water is None and initial_complete and _audit_is_recent(cursor_payload):
        return 0, False, "caught_up"
    if high_water is None:
        high_water = await session.scalar(select(func.max(Listing.id))) or 0
        cursor = 0
    ids = list(await session.scalars(select(Listing.id).where(
        Listing.id > cursor, Listing.id <= int(high_water),
    ).order_by(Listing.id).limit(limit + 1)))
    page = [int(value) for value in ids[:limit]]
    now = datetime.now(UTC)
    for listing_id in page:
        rows = list(await session.scalars(select(IndustryClassification).where(
            IndustryClassification.listing_id == listing_id,
        ).order_by(IndustryClassification.system, IndustryClassification.valid_from,
                   IndustryClassification.id)))
        digest = update_digest([{
            "id": row.id, "system": row.system, "code": row.code, "name": row.name,
            "valid_from": row.valid_from.isoformat(),
            "valid_to": row.valid_to.isoformat() if row.valid_to else None,
            "source": row.source, "captured_at": row.captured_at.isoformat(),
        } for row in rows])
        scope = f"listing:{listing_id}"
        await _require_lease(session, job)
        state = await repo.ensure_source(
            _OWNER, feed_id, scope, _CONFIG_HASHES[feed_id], listing_id=listing_id,
            payload={"classification_hash": None},
        )
        previous = state.payload.get("classification_hash")
        if previous != digest:
            initial = not initial_complete and state.revision == 0
            success_hash: str | None
            if rows or previous is not None:
                acquired_at = min((_as_utc(row.captured_at) for row in rows), default=now)
                await _require_lease(session, job)
                receipt = await repo.append_change(
                    _OWNER,
                    change_key=f"bootstrap:industry-classification:{listing_id}:revision:{state.revision + 1}:{digest}",
                    kind="bootstrap", listing_id=listing_id,
                    acquired_at=acquired_at,
                    available_from=max(acquired_at, now), committed_at=now,
                    payload={"schema_version": 1, "source_kind": "industry_classification",
                             "source_ref": {"kind": "listing", "id": listing_id, "revision": 1},
                             "source_digest": digest, "previous_digest": previous,
                             "contexts": list(_CONTEXTS), "bootstrap_initial": initial},
                )
                success_sequence, success_hash = receipt.sequence, receipt.payload_hash
            else:
                success_sequence, success_hash = state.success_sequence, state.success_receipt_hash
            changed = state.model_copy(update={
                "revision": state.revision + 1, "payload": {**state.payload,
                    "classification_hash": digest},
                "success_sequence": success_sequence, "success_receipt_hash": success_hash,
                "source_refs": [{"kind": "listing", "id": listing_id, "revision": 1}],
                "coverage_state": "complete", "last_attempt_at": now,
                "last_success_at": now, "health_limit_seconds": 0,
            })
            await _require_lease(session, job)
            await repo.save_source(changed, expected_revision=state.revision)
        cursor = listing_id
    has_more = len(ids) > limit
    finished = not has_more and (not page or cursor >= int(high_water))
    if finished:
        cursor_payload.update({"cursor_listing_id": 0, "high_water_listing_id": None,
                              "initial_complete": True,
                              "last_audit_completed_at": now.isoformat()})
    else:
        cursor_payload.update({"cursor_listing_id": cursor,
                               "high_water_listing_id": int(high_water),
                               "initial_complete": initial_complete})
    await _require_lease(session, job)
    await repo.save_source(cursor_state.model_copy(update={
        "revision": cursor_state.revision + 1,
        "payload": cursor_payload,
        "coverage_state": "complete" if finished else "partial",
        "last_attempt_at": now,
    }), expected_revision=cursor_state.revision)
    return len(page), has_more or not finished, "running" if not finished else "caught_up"


def _event_receipt(row: ResearchEventRevision, *, bootstrap_initial: bool) -> dict[str, Any]:
    return {
        "owner_id": _OWNER,
        "change_key": f"bootstrap:event:{row.id}:revision:{row.revision}",
        "kind": "research_event",
        "acquired_at": _as_utc(row.acquired_at),
        "available_from": max(_as_utc(row.available_from), datetime.now(UTC)),
        "committed_at": datetime.now(UTC),
        "payload": {
            "schema_version": 1,
            "source_ref": {"kind": "research_event", "id": row.id, "revision": row.revision},
            "change_type": "revision", "event_key": row.event_key,
            "previous_ref": row.previous_ref, "content_hash": row.content_hash,
            "bootstrap_initial": bootstrap_initial,
        },
    }


async def _scan_rule_baseline(
    session: AsyncSession, repo: SupplementalUpdatesRepository, job: Any, limit: int,
) -> tuple[int, bool, str, str | None]:
    feed_id = "bootstrap_rule_revision"
    await _require_lease(session, job)
    state = await repo.ensure_source(_OWNER, feed_id, "global", _CONFIG_HASHES[feed_id])
    payload = dict(state.payload)
    cursor = payload.get("cursor")
    high_water = payload.get("high_water")
    initial_complete = payload.get("initial_complete") is True
    if high_water is None and initial_complete and _audit_is_recent(payload):
        return 0, False, "caught_up", None
    if high_water is None:
        last = await session.scalar(select(SupplementalRuleRevision).where(
            SupplementalRuleRevision.owner_id == _OWNER,
        ).order_by(SupplementalRuleRevision.id.desc(),
                   SupplementalRuleRevision.revision.desc()).limit(1))
        high_water = {"id": last.id, "revision": last.revision} if last else None
    if high_water is None:
        payload["last_audit_completed_at"] = datetime.now(UTC).isoformat()
        updated = _cursor_state(state, payload, cursor=None, high_water=None,
                                initial_complete=True, blocked=None)
        await _require_lease(session, job)
        await repo.save_source(updated, expected_revision=state.revision)
        return 0, False, "caught_up", None
    query = select(SupplementalRuleRevision).where(
        SupplementalRuleRevision.owner_id == _OWNER,
        tuple_(SupplementalRuleRevision.id, SupplementalRuleRevision.revision)
        <= tuple_(literal(str(high_water["id"])), literal(int(high_water["revision"]))),
    )
    if isinstance(cursor, dict):
        query = query.where(tuple_(SupplementalRuleRevision.id, SupplementalRuleRevision.revision)
                            > tuple_(literal(str(cursor["id"])), literal(int(cursor["revision"]))))
    rows = list(await session.scalars(query.order_by(
        SupplementalRuleRevision.id, SupplementalRuleRevision.revision).limit(limit + 1)))
    page = rows[:limit]
    initial_page = not initial_complete
    now = datetime.now(UTC)
    for row in page:
        source_ref = ObjectRef(kind="supplemental_rule", id=UUID(row.id), revision=row.revision)
        reference = source_ref.model_dump(mode="json")
        if not await _has_source_receipt(session, repo, "rule_revision", reference):
            await _require_lease(session, job)
            await repo.append_change(
                _OWNER, change_key=f"bootstrap:rule:{row.id}:revision:{row.revision}",
                kind="rule_revision", acquired_at=now, available_from=now, committed_at=now,
                payload={"schema_version": 1, "source_ref": reference,
                         "change_type": "revision", "content_hash": row.content_hash,
                         "semantic_hash": row.semantic_hash, "bootstrap_initial": initial_page},
            )
        head = await session.get(SupplementalRuleHead, (_OWNER, row.id))
        if (
            head is not None and head.active_revision == row.revision
            and head.active_state is not None
            and not await _has_source_receipt(session, repo, "rule_lifecycle", reference)
        ):
            await _require_lease(session, job)
            await repo.append_change(
                _OWNER, change_key=f"bootstrap:rule-lifecycle:{row.id}:revision:{row.revision}",
                kind="rule_lifecycle", acquired_at=now, available_from=now, committed_at=now,
                payload={"schema_version": 1, "source_ref": reference,
                         "change_type": "lifecycle", "command": f"baseline_{head.active_state}",
                         "content_hash": row.content_hash,
                         "semantic_hash": row.semantic_hash,
                         "active_state": head.active_state,
                         "bootstrap_initial": initial_page},
            )
        cursor = {"id": row.id, "revision": row.revision}
    more = len(rows) > limit
    finished = not more and (not page or (
        cursor is not None
        and (str(cursor["id"]), int(cursor["revision"]))
        >= (str(high_water["id"]), int(high_water["revision"]))
    ))
    if finished:
        cursor = None
        high_water = None
        initial_complete = True
        payload["last_audit_completed_at"] = now.isoformat()
    updated = _cursor_state(state, payload, cursor=cursor, high_water=high_water,
                            initial_complete=initial_complete, blocked=None)
    await _require_lease(session, job)
    await repo.save_source(updated, expected_revision=state.revision)
    return len(page), more, "running" if high_water is not None else "caught_up", None


async def _has_source_receipt(
    session: AsyncSession, repo: SupplementalUpdatesRepository,
    kind: str, source_ref: dict[str, Any],
) -> bool:
    row = await session.scalar(select(SupplementalChangeReceiptRow).where(
        SupplementalChangeReceiptRow.owner_id == _OWNER,
        SupplementalChangeReceiptRow.kind == kind,
        func.json_extract(SupplementalChangeReceiptRow.payload, "$.source_ref.id")
        == str(source_ref["id"]),
        func.json_extract(SupplementalChangeReceiptRow.payload, "$.source_ref.revision")
        == int(source_ref["revision"]),
    ).order_by(SupplementalChangeReceiptRow.sequence).limit(1))
    if row is None:
        return False
    receipt = await repo.get_change(_OWNER, row.sequence)
    if receipt is None or receipt.payload.get("source_ref") != source_ref:
        raise ValueError("existing source journal identity is malformed")
    return True


def _audit_is_recent(payload: dict[str, Any]) -> bool:
    raw = payload.get("last_audit_completed_at")
    if not isinstance(raw, str):
        return False
    try:
        at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(UTC) - _as_utc(at)).total_seconds() < _AUDIT_INTERVAL_SECONDS
