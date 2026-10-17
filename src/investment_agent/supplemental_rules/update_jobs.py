"""Durable, lease-fenced work for locally saved supplemental changes."""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid5

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.jobs.base import BaseJobHandler, ClaimedJob, JobContext, JobOutcome
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.models.investor import InvestorCase
from investment_agent.storage.models.monitoring import ReviewCycle
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.research_material import ResearchSourceBinding
from investment_agent.storage.models.supplemental_rules import (
    ResearchEventRevision,
    SupplementalRuleHead,
)
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.supplemental_updates import (
    SupplementalUpdatesRepository,
    update_digest,
)
from investment_agent.supplemental_rules.admission import SupplementalRuleAdmission
from investment_agent.supplemental_rules.contracts import Context, EvaluationRequest
from investment_agent.supplemental_rules.update_targets import (
    freeze_target_snapshot,
    target_scopes,
)

LOCAL_OWNER: OwnerId = "local_owner"


def _owner(value: str) -> OwnerId:
    if value != LOCAL_OWNER:
        raise ValueError("supplemental updates owner is unavailable")
    return LOCAL_OWNER

SUPPLEMENTAL_UPDATE_KIND = "supplemental_update_process"
UPDATE_INPUT_KINDS = (
    "rule_revision", "rule_lifecycle", "research_event", "evidence_revision",
    "material_revision", "due_boundary", "bootstrap",
)
MAX_FANOUT_LISTINGS = 100
UPDATE_SWEEP_LIMIT = 50
MAX_DUE_SCAN_ROWS = 100
MAX_ACTIVE_UPDATE_RUNS = 128
P4_PENDING_KINDS = (SUPPLEMENTAL_UPDATE_KIND, "supplemental_disclosure_poll")


async def _begin_write(session: AsyncSession) -> None:
    """Serialize SQLite readers before lease validation and related writes."""
    connection = await session.connection()
    if connection.dialect.name == "sqlite":
        active = await connection.run_sync(
            lambda c: bool(getattr(c.connection.driver_connection, "in_transaction", False))
        )
        if not active:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")


async def _require_lease(session: AsyncSession, job: ClaimedJob) -> PipelineRun:
    """Validate the attempt token in the same write transaction as its writes."""
    await _begin_write(session)
    run = (await session.execute(select(PipelineRun).where(
        PipelineRun.id == job.run_id,
    ).with_for_update())).scalar_one_or_none()
    if (
        run is None or run.status != "running" or run.worker_id != job.lease.worker_id
        or run.attempt_count != job.lease.attempt
    ):
        from investment_agent.storage.repositories.pipeline_run import LeaseLostError

        raise LeaseLostError(job.run_id, "supplemental update lease no longer owns run")
    return run


async def _p4_gates_open(session: AsyncSession, base: Settings) -> bool:
    from investment_agent.settings.loader import load_settings

    current = await load_settings(session, base=base)
    return bool(current.supplemental_rules_enabled and current.supplemental_updates_enabled)


async def request_update_maintenance(
    session: AsyncSession, *, settings: Settings, owner_id: OwnerId = LOCAL_OWNER,
) -> bool:
    """Persist a wakeup; schedulers never run maintenance inline."""
    from investment_agent.settings.loader import load_settings

    effective = await load_settings(session, base=settings)
    if not effective.supplemental_rules_enabled or not effective.supplemental_updates_enabled:
        return False
    await _begin_write(session)
    active = await session.scalar(select(PipelineRun.id).where(
        PipelineRun.kind == SUPPLEMENTAL_UPDATE_KIND,
        PipelineRun.status.in_(("queued", "running")),
    ).order_by(PipelineRun.id.desc()).limit(1))
    if active is not None:
        return False
    pending_count = int(await session.scalar(select(func.count(PipelineRun.id)).where(
        PipelineRun.kind.in_(P4_PENDING_KINDS),
        PipelineRun.status.in_(("queued", "running")),
    )) or 0)
    if pending_count >= MAX_ACTIVE_UPDATE_RUNS:
        return False
    bucket = datetime.now(UTC).strftime("%Y%m%d%H%M")
    _, created = await PipelineRunRepository(session).enqueue(
        kind=SUPPLEMENTAL_UPDATE_KIND,
        idempotency_key=f"p4:maintenance:{bucket}",
        triggered_by="supplemental_updates_scheduler",
        request_json={"schema_version": 1, "owner_id": owner_id, "action": "maintenance"},
        max_attempts=3,
    )
    return created


async def enqueue_pending_updates(
    session: AsyncSession, *, settings: Settings, owner_id: OwnerId = LOCAL_OWNER,
    limit: int = UPDATE_SWEEP_LIMIT, job: ClaimedJob | None = None,
) -> int:
    """Wake durable journal rows. Journal receipts remain the source of truth."""
    if job is None:
        return int(await request_update_maintenance(session, settings=settings, owner_id=owner_id))
    await _require_lease(session, job)
    from investment_agent.settings.loader import load_settings

    effective = await load_settings(session, base=settings)
    if not effective.supplemental_updates_enabled or not effective.supplemental_rules_enabled:
        return 0
    repo = SupplementalUpdatesRepository(session)
    await _scan_due_boundaries(session, repo, owner_id=owner_id, limit=limit, job=job)
    from investment_agent.supplemental_rules.update_bootstrap import scan_saved_inputs

    bootstrap_result = await scan_saved_inputs(
        session, repo, job, limit=min(max(limit, 1), 100),
    )
    cursor = await repo.ensure_source(
        owner_id, "local_journal_dispatch", "all",
        sha256(b"supplemental-update-journal-v1").hexdigest(),
        payload={"after_sequence": 0},
    )
    after_sequence = int(cursor.payload.get("after_sequence", 0))
    rows = await repo.list_changes(
        owner_id, after_sequence=after_sequence,
        limit=min(max(limit, 1), 100), kinds=UPDATE_INPUT_KINDS,
    )
    count = 0
    runs = PipelineRunRepository(session)
    active_count = int(await session.scalar(select(func.count(PipelineRun.id)).where(
        PipelineRun.kind.in_(P4_PENDING_KINDS),
        PipelineRun.status.in_(("queued", "running")),
    )) or 0)
    capacity = max(0, MAX_ACTIVE_UPDATE_RUNS - active_count)
    processed_through = after_sequence
    for receipt in rows[:capacity]:
        await _require_lease(session, job)
        # A deterministic no-op key makes concurrent wakeups converge. A
        # terminal coordinator is intentionally not revived by a timer.
        key = f"p4:u:{receipt.sequence}"
        snapshot = await freeze_target_snapshot(session, receipt)
        _, created = await runs.enqueue(
            kind=SUPPLEMENTAL_UPDATE_KIND,
            idempotency_key=key,
            triggered_by="supplemental_updates",
            request_json={"schema_version": 1, "owner_id": owner_id,
                          "receipt_sequence": receipt.sequence,
                          "receipt_id": str(receipt.id),
                          "receipt_payload_hash": receipt.payload_hash,
                          "target_snapshot": snapshot},
            max_attempts=3,
        )
        count += int(created)
        processed_through = receipt.sequence
    if rows or cursor.payload.get("capacity_blocked"):
        await _require_lease(session, job)
        blocked = len(rows) > capacity
        await repo.save_source(
            cursor.model_copy(update={
                "revision": cursor.revision + 1,
                "payload": {**cursor.payload, "after_sequence": processed_through,
                            "capacity_blocked": blocked,
                            "blocked_from_sequence": (rows[capacity].sequence
                                if blocked and capacity < len(rows) else None)},
            }),
            expected_revision=cursor.revision,
        )
    return count + int(bootstrap_result.get("processed", 0))


def _parse_due(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


async def _scan_due_boundaries(
    session: AsyncSession, repo: SupplementalUpdatesRepository, *,
    owner_id: OwnerId, limit: int, job: ClaimedJob,
) -> int:
    """Persist due identities for declared P2 rule/application/event boundaries.

    The source cursor is paged and durable. A due key binds immutable source
    revision + boundary + scope, so repeated wakeups do not slide time or
    generate duplicate work. Coverage/evidence without an explicit expiry is
    deliberately not assigned one here.
    """
    from investment_agent.storage.repositories.supplemental_rules import (
        SupplementalRulesRepository,
    )

    now = datetime.now(UTC)
    page_size = min(MAX_DUE_SCAN_ROWS, max(1, limit))
    cursor_state = await repo.ensure_source(
        owner_id, "local_due_scan", "rules",
        sha256(b"supplemental-due-scan-v1").hexdigest(),
        payload={"after_rule_id": None, "high_water_rule_id": None},
    )
    after = cursor_state.payload.get("after_rule_id")
    high_water = cursor_state.payload.get("high_water_rule_id")
    if not isinstance(high_water, str):
        high_water = await session.scalar(select(func.max(SupplementalRuleHead.rule_id)).where(
            SupplementalRuleHead.owner_id == owner_id,
            SupplementalRuleHead.active_state == "active",
        ))
    rules, rule_truncated = await SupplementalRulesRepository(session).list_active_rules(
        owner_id, limit=page_size, after_rule_id=after if isinstance(after, str) else None,
    )
    if isinstance(high_water, str):
        rules = tuple(rule for rule in rules if str(rule.id) <= high_water)
    rule_has_more = rule_truncated and bool(rules) and str(rules[-1].id) < str(high_water or "")
    candidates: list[tuple[Any, int | None, Context | None, str, datetime]] = []
    future_boundaries: list[datetime] = []
    for rule in rules:
        ref = ObjectRef(kind="supplemental_rule", id=UUID(str(rule.id)), revision=rule.revision)
        boundaries = (("rule_review_due", rule.review_due_at),
                      ("rule_valid_until", rule.valid_until))
        for boundary_kind, due_at in boundaries:
            if due_at is None:
                continue
            if due_at > now:
                future_boundaries.append(due_at)
                continue
            candidates.append((ref, None, None, boundary_kind, due_at))

    # The latest live application carries declared validity inherited from its
    # frozen rule. Only the latest application per (listing, series) is due.
    app_state = await repo.ensure_source(
        owner_id, "local_due_scan", "applications",
        sha256(b"supplemental-due-applications-v1").hexdigest(),
        payload={"after_id": None, "high_water_id": None},
    )
    app_after = app_state.payload.get("after_id")
    app_high = app_state.payload.get("high_water_id")
    if not isinstance(app_high, str):
        app_high = await session.scalar(select(func.max(InvestmentAssessment.id)).where(
            InvestmentAssessment.owner_id == owner_id,
            InvestmentAssessment.kind == "rule_application",
            InvestmentAssessment.application_mode == "live",
        ))
    app_query = select(InvestmentAssessment).where(
            InvestmentAssessment.owner_id == owner_id,
            InvestmentAssessment.kind == "rule_application",
            InvestmentAssessment.application_mode == "live",
    )
    if isinstance(app_after, str):
        app_query = app_query.where(InvestmentAssessment.id > app_after)
    if isinstance(app_high, str):
        app_query = app_query.where(InvestmentAssessment.id <= app_high)
    app_page = list((await session.scalars(
        app_query.order_by(InvestmentAssessment.id).limit(page_size + 1)
    )).all())
    app_truncated = len(app_page) > page_size
    app_rows = app_page[:page_size]
    seen_series: set[tuple[int, str]] = set()
    for row in app_rows:
        if row.listing_id is None or row.series_key is None:
            continue
        identity = (row.listing_id, row.series_key)
        if identity in seen_series:
            continue
        seen_series.add(identity)
        latest_sequence = await session.scalar(select(func.max(
            InvestmentAssessment.application_sequence
        )).where(
            InvestmentAssessment.owner_id == owner_id,
            InvestmentAssessment.kind == "rule_application",
            InvestmentAssessment.application_mode == "live",
            InvestmentAssessment.listing_id == row.listing_id,
            InvestmentAssessment.series_key == row.series_key,
        ))
        if row.application_sequence != latest_sequence:
            continue
        payload = row.payload
        app_ref = ObjectRef(kind="rule_application", id=UUID(row.id), revision=row.revision)
        contexts = payload.get("context")
        if not isinstance(contexts, str):
            frozen_pair = payload.get("frozen_pair")
            contexts = frozen_pair.get("context") if isinstance(frozen_pair, dict) else None
        if not isinstance(contexts, str):
            continue
        for key, kind in (("review_due_at", "application_review_due"),
                          ("valid_until", "application_valid_until")):
            due_at = _parse_due(payload.get(key))
            if due_at is not None and due_at > now:
                future_boundaries.append(due_at)
            elif due_at is not None:
                candidates.append((app_ref, row.listing_id, cast(Context, contexts), kind, due_at))
        frozen_pair = payload.get("frozen_pair")
        frozen_rule = frozen_pair.get("rule") if isinstance(frozen_pair, dict) else None
        definition = frozen_rule.get("definition") if isinstance(frozen_rule, dict) else None
        check_by = _parse_due(definition.get("check_by")) if isinstance(definition, dict) else None
        if check_by is not None and check_by > now:
            future_boundaries.append(check_by)
        elif check_by is not None:
            candidates.append((app_ref, row.listing_id, cast(Context, contexts),
                               "research_requirement_check_by", check_by))

    # Research-event revisions use a stable keyset and frozen high-water id.
    event_state = await repo.ensure_source(
        owner_id, "local_due_scan", "events",
        sha256(b"supplemental-due-events-v1").hexdigest(),
        payload={"after_id": None, "after_revision": 0, "high_water_id": None},
    )
    event_after_id = event_state.payload.get("after_id")
    event_after_revision = event_state.payload.get("after_revision", 0)
    event_high_water = event_state.payload.get("high_water_id")
    if not isinstance(event_high_water, str):
        event_high_water = await session.scalar(select(func.max(ResearchEventRevision.id)).where(
            ResearchEventRevision.owner_id == owner_id,
            ResearchEventRevision.available_from <= now,
            ResearchEventRevision.created_at <= now,
        ))
    event_query = select(ResearchEventRevision).where(
        ResearchEventRevision.owner_id == owner_id,
        ResearchEventRevision.available_from <= now,
        ResearchEventRevision.created_at <= now,
    )
    if isinstance(event_high_water, str):
        event_query = event_query.where(ResearchEventRevision.id <= event_high_water)
    if isinstance(event_after_id, str) and isinstance(event_after_revision, int):
        event_query = event_query.where(or_(
            ResearchEventRevision.id > event_after_id,
            and_(ResearchEventRevision.id == event_after_id,
                 ResearchEventRevision.revision > event_after_revision),
        ))
    event_page = list((await session.scalars(event_query.order_by(
        ResearchEventRevision.id, ResearchEventRevision.revision,
    ).limit(page_size + 1))).all())
    event_truncated = len(event_page) > page_size
    event_rows = event_page[:page_size]
    events = []
    rules_repo = SupplementalRulesRepository(session)
    for event_row in event_rows:
        parsed = await rules_repo.get_event_revision(
            owner_id, ObjectRef(kind="research_event", id=UUID(event_row.id), revision=event_row.revision),
        )
        if parsed is not None:
            events.append(parsed)
    for event in events:
        ref = ObjectRef(kind="research_event", id=event.id, revision=event.revision)
        for kind, due_at in (("event_effective_from", event.effective_from),
                             ("event_effective_to", event.effective_to)):
            if due_at is not None and due_at > now:
                future_boundaries.append(due_at)
            elif due_at is not None:
                candidates.append((ref, None, None, kind, due_at))

    # The source-symbol contract has an explicit exclusive valid_to date.
    # Convert it to a stable UTC midnight boundary; do not invent expiry for
    # datasets whose contracts do not state one.
    binding_state = await repo.ensure_source(
        owner_id, "local_due_scan", "material_bindings",
        sha256(b"supplemental-due-material-bindings-v1").hexdigest(),
        payload={"after_id": None, "high_water_id": None},
    )
    binding_after = binding_state.payload.get("after_id")
    binding_high = binding_state.payload.get("high_water_id")
    if not isinstance(binding_high, int):
        binding_high = await session.scalar(select(func.max(ResearchSourceBinding.id)))
    binding_query = select(ResearchSourceBinding).where(
        ResearchSourceBinding.valid_to.is_not(None),
    )
    if isinstance(binding_after, int):
        binding_query = binding_query.where(ResearchSourceBinding.id > binding_after)
    if isinstance(binding_high, int):
        binding_query = binding_query.where(ResearchSourceBinding.id <= binding_high)
    binding_page = list((await session.scalars(
        binding_query.order_by(ResearchSourceBinding.id).limit(page_size + 1)
    )).all())
    binding_truncated = len(binding_page) > page_size
    binding_rows = binding_page[:page_size]
    for binding in binding_rows:
        if binding.valid_to is None:
            continue
        boundary_at = datetime.combine(binding.valid_to, datetime.min.time(), tzinfo=UTC)
        if boundary_at > now:
            future_boundaries.append(boundary_at)
            continue
        binding_ref = {"kind": "research_source_binding", "id": binding.id, "revision": 1}
        candidates.append((binding_ref, None, None,
                           "material_binding_valid_to", boundary_at))

    for source_ref, listing_id, context, boundary_kind, due_at in candidates:
        await _require_lease(session, job)
        ref_payload = (source_ref.model_dump(mode="json")
                       if isinstance(source_ref, ObjectRef) else source_ref)
        key = (f"p4:due:{ref_payload['kind']}:{ref_payload['id']}:{ref_payload['revision']}:"
               f"{boundary_kind}:{due_at.isoformat()}")
        await repo.append_change(
            owner_id, change_key=key, kind="due_boundary",
            listing_id=listing_id, context=context,
            payload={"schema_version": 1, "source_ref": ref_payload,
                     "boundary": boundary_kind, "due_at": due_at.isoformat()},
            available_from=due_at,
        )
    rule_payload = {
        **cursor_state.payload,
        "after_rule_id": str(rules[-1].id) if rule_has_more and rules else None,
        "high_water_rule_id": high_water if rule_has_more else None,
        "rules_scan_truncated": rule_has_more,
        "last_scan_at": now.isoformat(),
    }
    app_payload = {
        **app_state.payload,
        "after_id": str(app_rows[-1].id) if app_truncated and app_rows else None,
        "high_water_id": app_high if app_truncated else None,
        "scan_truncated": app_truncated,
        "last_scan_at": now.isoformat(),
    }
    event_payload = {
        **event_state.payload,
        "after_id": event_rows[-1].id if event_truncated and event_rows else None,
        "after_revision": event_rows[-1].revision if event_truncated and event_rows else 0,
        "high_water_id": event_high_water if event_truncated else None,
        "scan_truncated": event_truncated,
        "last_scan_at": now.isoformat(),
    }
    binding_payload = {
        **binding_state.payload,
        "after_id": binding_rows[-1].id if binding_truncated and binding_rows else None,
        "high_water_id": binding_high if binding_truncated else None,
        "scan_truncated": binding_truncated,
        "last_scan_at": now.isoformat(),
    }
    next_due_at = min(future_boundaries).isoformat() if future_boundaries else None
    all_scans_truncated = bool(rule_has_more or app_truncated or event_truncated or binding_truncated)
    rule_payload["next_due_at"] = next_due_at
    rule_payload["source_scan_truncated"] = all_scans_truncated
    for state, payload in ((cursor_state, rule_payload), (app_state, app_payload),
                           (event_state, event_payload), (binding_state, binding_payload)):
        if state.payload != payload:
            await _require_lease(session, job)
            await repo.save_source(state.model_copy(update={
                "revision": state.revision + 1, "payload": payload,
            }), expected_revision=state.revision)
    return len(candidates)


async def _rule_scope_rows(
    session: AsyncSession, rule: Any,
) -> tuple[list[tuple[int, Context]], bool]:
    selector = rule.selector
    query = select(Listing.id).order_by(Listing.id)
    if selector.kind == "listing":
        query = query.where(Listing.id == int(selector.listing_ref.id))
    elif selector.kind == "company":
        query = query.where(Listing.company_id == selector.company_id)
    ids = list((await session.scalars(query.limit(MAX_FANOUT_LISTINGS + 1))).all())
    complete = len(ids) <= MAX_FANOUT_LISTINGS and selector.kind != "category"
    return [(int(listing_id), context) for listing_id in ids[:MAX_FANOUT_LISTINGS]
            for context in sorted(rule.contexts)], complete


async def _target_scopes(
    session: AsyncSession, receipt: Any, *, offset: int = 0,
    target_snapshot: dict[str, Any] | None = None,
) -> tuple[list[tuple[int, Context]], bool]:
    """Read the persisted snapshot; never re-enumerate a resumed fanout."""
    return await target_scopes(
        session, receipt, offset=offset, target_snapshot=target_snapshot,
    )


async def _enqueue_scope_evaluation(
    session: AsyncSession, *, settings: Settings, receipt: Any,
    scopes: list[tuple[int, Context]], members: list[Any],
    operation_id_override: UUID | None = None,
    exact_rule_refs: tuple[ObjectRef, ...] = (),
    prior_application_refs: tuple[ObjectRef, ...] = (),
) -> int | None:
    if not scopes:
        return None
    operation_id = operation_id_override or uuid5(receipt.id, "p4-evaluation:" + ":".join(
        f"{listing}:{context}" for listing, context in scopes
    ))
    request = EvaluationRequest(
        idempotency_key=operation_id,
        listing_refs=tuple(ObjectRef(kind="listing", id=listing, revision=1)
                           for listing in sorted({listing for listing, _ in scopes})),
        contexts=frozenset(context for _, context in scopes), mode="live",
        exact_rule_refs=exact_rule_refs,
        prior_application_refs=prior_application_refs,
    )
    admission = await SupplementalRuleAdmission(session, settings=settings,
                                                 owner_id=_owner(receipt.owner_id)).accept_evaluation(request)
    p2_run_id = int(admission.run_ref.id)
    p2_run = await session.get(PipelineRun, p2_run_id, populate_existing=True)
    if p2_run is None:
        raise RuntimeError("P2 admission returned a missing PipelineRun")
    await SupplementalUpdatesRepository(session).append_change(
        _owner(receipt.owner_id),
        change_key=f"p4:admission:{receipt.sequence}:{admission.operation_id}",
        kind="evaluation_admission",
        payload={"receipt_sequence": receipt.sequence,
                 "scope_member_sequences": [member.sequence for member in members],
                 "run_id": p2_run_id, "source_manifest_hash": admission.manifest_hash},
        parent_sequence=receipt.sequence,
        origin_run_id=p2_run_id,
        source_manifest_hash=admission.manifest_hash,
    )
    return p2_run_id


async def _origin_run_state(
    session: AsyncSession, receipt: Any, scopes: list[tuple[int, Context]],
) -> tuple[str, int | None, set[tuple[int, str]]]:
    """Return a verified P2 handoff state for a hooked domain mutation."""
    if receipt.origin_run_id is None:
        return "new_work", None, set()
    run = await session.get(PipelineRun, receipt.origin_run_id, populate_existing=True)
    if run is None or run.kind != "supplemental_rule_evaluate":
        return "needs_manual", None, set()
    from investment_agent.supplemental_rules.jobs import (
        _read_checkpoint,
        _verify_checkpoint,
        authorize_supplemental_run,
    )

    try:
        _, authorized = await authorize_supplemental_run(
            session, run.id, owner_id=receipt.owner_id
        )
        if authorized.manifest.manifest_hash != receipt.source_manifest_hash:
            return "needs_manual", run.id, set()
        covered_pairs: set[tuple[int, str]] = set()
        source_ref_raw = receipt.payload.get("source_ref")
        source_ref = ObjectRef.model_validate(source_ref_raw) if isinstance(source_ref_raw, dict) else None
        if not authorized.manifest.scan_diagnostics:
            for pair in authorized.manifest.pairs:
                scope = (int(pair.listing_ref.id), pair.context)
                if receipt.kind == "rule_lifecycle":
                    # A lifecycle receipt adopts only the exact changed rule
                    # pair, never every rule in the same listing/context.
                    if source_ref is not None and pair.rule_ref == source_ref:
                        covered_pairs.add((scope[0], str(scope[1])))
                elif receipt.kind == "research_event":
                    selected = pair.selected_event
                    if (source_ref is not None and selected is not None
                            and selected.id == source_ref.id
                            and selected.revision == source_ref.revision):
                        covered_pairs.add((scope[0], str(scope[1])))
                else:
                    covered_pairs.add((scope[0], str(scope[1])))
        covered: set[tuple[int, str]] = {
            (listing, str(context)) for listing, context in scopes
            if (listing, str(context)) in covered_pairs
        }
        if not covered:
            return "new_work", run.id, set()
        if run.status in {"queued", "running"}:
            return "waiting_origin", run.id, covered
        if run.status == "success":
            checkpoint = _read_checkpoint(run.metadata_json, authorized.manifest)
            await _verify_checkpoint(session, checkpoint, authorized)
            return "completed_origin", run.id, covered
        # Terminal partial/failed P2 runs are not revived and do not get
        # silently duplicated by a new automatic admission.
        return "needs_manual", run.id, set()
    except Exception:
        return "needs_manual", run.id, set()


class SupplementalUpdateHandler(BaseJobHandler):
    kind = SUPPLEMENTAL_UPDATE_KIND

    def __init__(self, *, settings: Settings) -> None:
        self.settings = settings

    async def execute(self, job: ClaimedJob, ctx: JobContext) -> JobOutcome:
        async def work() -> JobOutcome:
            async with session_scope() as session:
                from investment_agent.settings.loader import load_settings

                await _require_lease(session, job)
                payload = job.request_json
                if payload.get("schema_version") != 1:
                    await PipelineRunRepository(session).mark_partial(
                        job.run_id, "invalid_supplemental_update_request", lease=job.lease
                    )
                    return "partial"
                if payload.get("action") == "maintenance":
                    effective = await load_settings(session, base=self.settings)
                    queued = await enqueue_pending_updates(
                        session, settings=effective, owner_id=LOCAL_OWNER,
                        limit=UPDATE_SWEEP_LIMIT, job=job,
                    )
                    settled = await settle_update_handoffs(
                        session, settings=effective, limit=UPDATE_SWEEP_LIMIT, job=job,
                    )
                    await PipelineRunRepository(session).mark_success(
                        job.run_id,
                        {"supplemental_update_state": "maintenance_complete",
                         "queued_count": queued, "settled_count": settled},
                        lease=job.lease,
                    )
                    return "completed"
                repo = SupplementalUpdatesRepository(session)
                raw_owner = payload.get("owner_id")
                raw_sequence = payload.get("receipt_sequence")
                if not isinstance(raw_owner, str) or not isinstance(raw_sequence, int):
                    await PipelineRunRepository(session).mark_partial(
                        job.run_id, "invalid_supplemental_update_request", lease=job.lease
                    )
                    return "partial"
                receipt = await repo.get_change(_owner(raw_owner), raw_sequence)
                if receipt is None or str(receipt.id) != payload.get("receipt_id") or receipt.payload_hash != payload.get("receipt_payload_hash"):
                    await PipelineRunRepository(session).mark_partial(
                        job.run_id, "supplemental_update_receipt_mismatch", lease=job.lease
                    )
                    return "partial"
                effective = await load_settings(session, base=self.settings)
                if not effective.supplemental_rules_enabled or not effective.supplemental_updates_enabled:
                    await PipelineRunRepository(session).mark_partial(
                        job.run_id, "supplemental_updates_paused",
                        {"supplemental_update_state": "paused", "source_sequence": receipt.sequence},
                        lease=job.lease,
                    )
                    return "partial"
                resume_scope_rows = payload.get("resume_scopes")
                resume_attempt_sequence = payload.get("resume_parent_sequence")
                resume_operation_raw = payload.get("resume_operation_id")
                resume_operation_id = None
                target_snapshot = payload.get("target_snapshot")
                if target_snapshot is not None and not isinstance(target_snapshot, dict):
                    await PipelineRunRepository(session).mark_partial(
                        job.run_id, "invalid_supplemental_update_target_snapshot", lease=job.lease
                    )
                    return "partial"
                if resume_scope_rows is not None:
                    if (
                        not isinstance(resume_scope_rows, list)
                        or not isinstance(resume_attempt_sequence, int)
                        or not isinstance(resume_operation_raw, str)
                    ):
                        await PipelineRunRepository(session).mark_partial(
                            job.run_id, "invalid_supplemental_update_resume", lease=job.lease
                        )
                        return "partial"
                    resume_operation_id = uuid5(
                        UUID(resume_operation_raw),
                        "supplemental-update-evaluation:" + str(receipt.sequence),
                    )
                    scopes = []
                    for row in resume_scope_rows:
                        if not isinstance(row, dict) or not isinstance(row.get("listing_id"), int) or not isinstance(row.get("context"), str):
                            await PipelineRunRepository(session).mark_partial(
                                job.run_id, "invalid_supplemental_update_resume", lease=job.lease
                            )
                            return "partial"
                        scopes.append((row["listing_id"], row["context"]))
                    complete = True
                else:
                    if target_snapshot is None:
                        target_snapshot = await freeze_target_snapshot(session, receipt)
                        coordinator = await _require_lease(session, job)
                        coordinator.request_json = {
                            **coordinator.request_json,
                            "target_snapshot": target_snapshot,
                        }
                        await session.flush()
                        payload = {**payload, "target_snapshot": target_snapshot}
                    scopes, complete = await _target_scopes(
                        session, receipt, target_snapshot=target_snapshot,
                    )
                raw_offset = payload.get("scope_offset", 0)
                if not isinstance(raw_offset, int) or raw_offset < 0:
                    await PipelineRunRepository(session).mark_partial(
                        job.run_id, "invalid_supplemental_update_offset", lease=job.lease
                    )
                    return "partial"
                scope_offset = raw_offset
                if scope_offset:
                    scopes, complete = await _target_scopes(
                        session, receipt, offset=scope_offset,
                        target_snapshot=target_snapshot,
                    )
                if not scopes:
                    no_target_reason = (
                        target_snapshot.get("no_target_reason")
                        if isinstance(target_snapshot, dict) else None
                    )
                    if complete and isinstance(no_target_reason, str):
                        await PipelineRunRepository(session).mark_success(
                            job.run_id,
                            {"supplemental_update_state": "settled_noop",
                             "source_sequence": receipt.sequence,
                             "scope_count": 0, "no_target_reason": no_target_reason},
                            lease=job.lease,
                        )
                        return "completed"
                    await PipelineRunRepository(session).mark_partial(
                        job.run_id, "supplemental_update_scope_unknown",
                        {"supplemental_update_state": "blocked", "blocked_reason": "scope_unknown",
                         "source_sequence": receipt.sequence},
                        lease=job.lease,
                    )
                    return "partial"
                members = []
                scope_run_ids: dict[tuple[int, str], int | None] = {}
                parent_sequence = (
                    resume_attempt_sequence if isinstance(resume_attempt_sequence, int)
                    else receipt.sequence
                )
                for listing_id, context in scopes:
                    await _require_lease(session, job)
                    if not await _p4_gates_open(session, effective):
                        await PipelineRunRepository(session).mark_partial(
                            job.run_id, "supplemental_updates_paused",
                            {"supplemental_update_state": "paused",
                             "source_sequence": receipt.sequence,
                             "scope_offset": scope_offset}, lease=job.lease,
                        )
                        return "partial"
                    member = await repo.append_change(
                        _owner(receipt.owner_id),
                        change_key=f"p4:member:{parent_sequence}:{listing_id}:{context}",
                        kind="scope_member",
                        parent_sequence=parent_sequence,
                        listing_id=listing_id, context=context,
                        origin_run_id=receipt.origin_run_id,
                        source_manifest_hash=receipt.source_manifest_hash,
                        payload={"schema_version": 1, "source_sequence": receipt.sequence,
                                 "source_id": str(receipt.id), "source_kind": receipt.kind,
                                 "source_hash": receipt.payload_hash,
                                 "resume_of_member_sequence": next((
                                     int(row["member_sequence"]) for row in resume_scope_rows
                                     if isinstance(row, dict) and row.get("listing_id") == listing_id
                                     and row.get("context") == context
                                 ), None) if isinstance(resume_scope_rows, list) else None,
                                 "resume_has_manual_pair": next((
                                     row.get("has_manual_pair") for row in resume_scope_rows
                                     if isinstance(row, dict) and row.get("listing_id") == listing_id
                                     and row.get("context") == context
                                 ), False) if isinstance(resume_scope_rows, list) else False,
                                 "origin_run_id": str(receipt.origin_run_id) if receipt.origin_run_id else None,
                                 "source_manifest_hash": receipt.source_manifest_hash},
                    )
                    members.append(member)
                # Every call to local P2 admission and the resulting P4 audit
                # receipt shares this transaction. Bootstrap is evaluated by
                # P2 too; settlement suppresses paid investor review for its
                # historical baseline receipts.
                if resume_operation_id is not None:
                    assert isinstance(resume_scope_rows, list)
                    from investment_agent.supplemental_rules.contracts import UpdateRecoveryCommand

                    p2_run_ids: list[int] = []
                    for member, row in zip(members, resume_scope_rows, strict=True):
                        if not isinstance(row, dict):
                            raise ValueError("invalid resume scope")
                        raw_ordinals = row.get("pending_ordinals")
                        raw_saved = row.get("saved_application_refs", [])
                        original_run_id = row.get("original_run_id")
                        original_manifest_hash = row.get("original_manifest_hash")
                        member_sequence = row.get("member_sequence")
                        if (
                            not isinstance(raw_ordinals, list) or not raw_ordinals
                            or not all(isinstance(value, int) for value in raw_ordinals)
                            or not isinstance(raw_saved, list)
                            or not isinstance(original_run_id, int)
                            or not isinstance(original_manifest_hash, str)
                            or not isinstance(member_sequence, int)
                        ):
                            raise ValueError("resume requires exact pending pair references")
                        if member.listing_id is None or member.context is None:
                            raise ValueError("resume scope member is incomplete")
                        if not await _p4_gates_open(session, effective):
                            await PipelineRunRepository(session).mark_partial(
                                job.run_id, "supplemental_updates_paused",
                                {"supplemental_update_state": "paused",
                                 "source_sequence": receipt.sequence,
                                 "scope_offset": scope_offset}, lease=job.lease,
                            )
                            return "partial"
                        recovery = UpdateRecoveryCommand(
                            idempotency_key=uuid5(
                                resume_operation_id,
                                f"{member.listing_id}:{member.context}:{member.sequence}",
                            ),
                            original_run_id=original_run_id,
                            original_manifest_hash=original_manifest_hash,
                            source_sequence=receipt.sequence,
                            member_sequence=member_sequence,
                            recovery_generation=int(row.get("recovery_generation", 0)),
                            pending_ordinals=tuple(raw_ordinals),
                            saved_application_refs=tuple(
                                ObjectRef.model_validate(ref) for ref in raw_saved
                            ),
                            has_manual_pair=bool(row.get("has_manual_pair", False)),
                        )
                        admission = await SupplementalRuleAdmission(
                            session, settings=effective,
                            owner_id=_owner(receipt.owner_id),
                        ).accept_update_recovery(recovery)
                        p2_id = int(admission.run_ref.id)
                        p2_run_ids.append(p2_id)
                        scope_run_ids[(member.listing_id, member.context)] = p2_id
                        await repo.append_change(
                            _owner(receipt.owner_id),
                            change_key=f"p4:handoff:{receipt.sequence}:{job.run_id}:{member.sequence}",
                            kind="scope_handoff",
                            payload={"coordinator_run_id": job.run_id, "p2_run_id": p2_id,
                                     "state": "dispatched", "member_sequences": [member.sequence],
                                     "complete": True, "resume_of_member_sequence": member.payload.get("resume_of_member_sequence")},
                            parent_sequence=parent_sequence, origin_run_id=p2_id,
                            source_manifest_hash=receipt.source_manifest_hash,
                        )
                else:
                    origin_state, origin_run_id, covered_scopes = await _origin_run_state(
                        session, receipt, scopes,
                    )
                    by_scope = {(member.listing_id, member.context): member for member in members}
                    covered_members = [by_scope[scope] for scope in sorted(covered_scopes)
                                       if scope in by_scope]
                    uncovered_members = [member for member in members
                                         if (member.listing_id, member.context) not in covered_scopes]
                    if covered_members:
                        for covered_member in covered_members:
                            assert covered_member.listing_id is not None and covered_member.context is not None
                            scope_run_ids[(covered_member.listing_id, covered_member.context)] = origin_run_id
                        await repo.append_change(
                            _owner(receipt.owner_id),
                            change_key=f"p4:handoff:{receipt.sequence}:{job.run_id}:origin",
                            kind="scope_handoff",
                            payload={"coordinator_run_id": job.run_id,
                                     "p2_run_id": origin_run_id,
                                     "state": origin_state,
                                     "member_sequences": [member.sequence for member in covered_members],
                                     "complete": complete},
                            parent_sequence=parent_sequence, origin_run_id=origin_run_id,
                            source_manifest_hash=receipt.source_manifest_hash,
                        )
                    p2_run_id = origin_run_id if covered_members else None
                    if uncovered_members and origin_state != "needs_manual":
                        by_listing: dict[int, list[Any]] = {}
                        for member in uncovered_members:
                            assert member.listing_id is not None
                            by_listing.setdefault(member.listing_id, []).append(member)
                        for listing_id, listing_members in sorted(by_listing.items()):
                            if not await _p4_gates_open(session, effective):
                                await PipelineRunRepository(session).mark_partial(
                                    job.run_id, "supplemental_updates_paused",
                                    {"supplemental_update_state": "paused",
                                     "source_sequence": receipt.sequence,
                                     "scope_offset": scope_offset}, lease=job.lease,
                                )
                                return "partial"
                            listing_scopes = [
                                (listing_id, cast(Context, member.context))
                                for member in listing_members if member.context is not None
                            ]
                            lifecycle_refs: tuple[ObjectRef, ...] = ()
                            if receipt.kind == "rule_lifecycle":
                                raw_source_ref = receipt.payload.get("source_ref")
                                if isinstance(raw_source_ref, dict):
                                    lifecycle_refs = (ObjectRef.model_validate(raw_source_ref),)
                            p2_run_id = await _enqueue_scope_evaluation(
                                session, settings=effective, receipt=receipt,
                                scopes=listing_scopes, members=listing_members,
                                exact_rule_refs=lifecycle_refs,
                            )
                            for listing_scope in listing_scopes:
                                scope_run_ids[listing_scope] = p2_run_id
                            await repo.append_change(
                                _owner(receipt.owner_id),
                                change_key=f"p4:handoff:{receipt.sequence}:{job.run_id}:new:{listing_id}",
                                kind="scope_handoff",
                                payload={"coordinator_run_id": job.run_id, "p2_run_id": p2_run_id,
                                         "state": "dispatched",
                                         "member_sequences": [member.sequence for member in listing_members],
                                         "complete": complete},
                                parent_sequence=parent_sequence, origin_run_id=p2_run_id,
                                source_manifest_hash=receipt.source_manifest_hash,
                            )
                    elif uncovered_members:
                        await repo.append_change(
                            _owner(receipt.owner_id),
                            change_key=f"p4:handoff:{receipt.sequence}:{job.run_id}:manual",
                            kind="scope_handoff",
                            payload={"coordinator_run_id": job.run_id, "p2_run_id": origin_run_id,
                                     "state": "needs_manual",
                                     "member_sequences": [member.sequence for member in uncovered_members],
                                     "complete": complete},
                            parent_sequence=parent_sequence, origin_run_id=origin_run_id,
                            source_manifest_hash=receipt.source_manifest_hash,
                        )
                for listing_id, context in scopes:
                    state = await repo.ensure_refresh(_owner(receipt.owner_id), listing_id, context)
                    next_sequence = max(state.requested_sequence, max(m.sequence for m in members
                        if m.listing_id == listing_id and m.context == context))
                    new_state = state.model_copy(update={
                        "revision": state.revision + 1,
                        "requested_sequence": next_sequence,
                        "confirmed_sequence": state.confirmed_sequence,
                        "active_run_id": scope_run_ids.get((listing_id, context)),
                        "blocked_reason": None if complete else "coverage_pending",
                    })
                    await repo.save_refresh(new_state, expected_revision=state.revision)
                snapshot_ids = target_snapshot.get("listing_ids", []) if isinstance(target_snapshot, dict) else []
                next_offset = scope_offset + 100
                has_next_page = isinstance(snapshot_ids, list) and next_offset < len(snapshot_ids)
                next_after = target_snapshot.get("next_after_listing_id") if isinstance(target_snapshot, dict) else None
                has_next_segment = isinstance(next_after, int) and next_after > 0
                continuation_needed = resume_operation_id is None and (has_next_page or has_next_segment)
                continuation_capacity_blocked = False
                if continuation_needed:
                    next_payload = {**payload, "scope_offset": next_offset}
                    continuation_key = f"p4:u:{receipt.sequence}:o:{next_offset}"
                    if not has_next_page and has_next_segment:
                        snapshot_high_water = (
                            target_snapshot.get("high_water_id")
                            if isinstance(target_snapshot, dict) else None
                        )
                        if not isinstance(snapshot_high_water, int):
                            raise ValueError("frozen target snapshot has no high-water fence")
                        next_snapshot = await freeze_target_snapshot(
                            session, receipt,
                            high_water_id=snapshot_high_water,
                            after_listing_id=next_after,
                        )
                        next_payload = {
                            **payload, "scope_offset": 0,
                            "target_snapshot": next_snapshot,
                        }
                        continuation_key = f"p4:u:{receipt.sequence}:s:{next_after}"
                    active_count = int(await session.scalar(select(func.count(PipelineRun.id)).where(
                        PipelineRun.kind.in_(P4_PENDING_KINDS),
                        PipelineRun.status.in_(("queued", "running")),
                        PipelineRun.id != job.run_id,
                    )) or 0)
                    if active_count >= MAX_ACTIVE_UPDATE_RUNS:
                        continuation_capacity_blocked = True
                    else:
                        await PipelineRunRepository(session).enqueue(
                            kind=SUPPLEMENTAL_UPDATE_KIND,
                            idempotency_key=continuation_key,
                            triggered_by="supplemental_updates",
                            request_json=next_payload,
                            max_attempts=3,
                        )
                if continuation_capacity_blocked:
                    await PipelineRunRepository(session).mark_partial(
                        job.run_id, "supplemental_update_capacity_blocked",
                        {"supplemental_update_state": "capacity_blocked",
                         "source_sequence": receipt.sequence,
                         "scope_offset": scope_offset,
                         "continuation_enqueued": False,
                         "continuation_blocked_reason": "queue_capacity",
                         "target_snapshot": target_snapshot},
                        lease=job.lease,
                    )
                    return "partial"
                await PipelineRunRepository(session).mark_success(
                    job.run_id,
                    {"supplemental_update_state": "dispatched" if complete else "partial",
                     "source_sequence": receipt.sequence, "scope_count": len(scopes),
                     "p2_run_id": (p2_run_ids[0] if resume_operation_id is not None and p2_run_ids
                                   else p2_run_id if resume_operation_id is None else None),
                     "origin_state": ("resumed_exact_pending_pairs" if resume_operation_id is not None else origin_state),
                     "scope_offset": scope_offset, "continuation_enqueued": continuation_needed,
                     "target_snapshot_complete": complete},
                    lease=job.lease,
                )
                return "completed"

        return await ctx.run_with_heartbeat(job.lease, work)


async def settle_update_handoffs(
    session: AsyncSession, *, settings: Settings, limit: int = 50,
    job: ClaimedJob | None = None,
) -> int:
    """Settle completed P2 handoffs and advance only proven per-scope cursors."""
    if job is None:
        await request_update_maintenance(session, settings=settings)
        return 0
    await _require_lease(session, job)
    from investment_agent.settings.loader import load_settings

    effective = await load_settings(session, base=settings)
    if not effective.supplemental_rules_enabled or not effective.supplemental_updates_enabled:
        return 0
    from investment_agent.supplemental_rules.jobs import (
        _read_checkpoint,
        _verify_checkpoint,
        authorize_supplemental_run,
    )

    repo = SupplementalUpdatesRepository(session)
    cursor = await repo.ensure_source(
        LOCAL_OWNER, "local_handoff_settlement", "all",
        sha256(b"supplemental-handoff-settlement-v1").hexdigest(),
        payload={"after_sequence": 0},
    )
    after_sequence = int(cursor.payload.get("after_sequence", 0))
    handoffs = await repo.list_changes(
        LOCAL_OWNER, after_sequence=after_sequence,
        limit=min(max(limit, 1), 100), kinds=("scope_handoff",),
    )
    if not handoffs and after_sequence:
        # Wrap after one bounded pass; pending/slow P2 runs are revisited while
        # newly appended handoffs cannot be starved behind old rows.
        await repo.save_source(cursor.model_copy(update={
            "revision": cursor.revision + 1,
            "payload": {**cursor.payload, "after_sequence": 0},
        }), expected_revision=cursor.revision)
        cursor = cursor.model_copy(update={
            "revision": cursor.revision + 1,
            "payload": {**cursor.payload, "after_sequence": 0},
        })
        handoffs = await repo.list_changes(
            LOCAL_OWNER, after_sequence=0,
            limit=min(max(limit, 1), 100), kinds=("scope_handoff",),
        )
    outcome_by_member: dict[int, Any] = {}
    settled = 0
    for handoff in handoffs:
        if not await _p4_gates_open(session, effective):
            return settled
        run_id = handoff.payload.get("p2_run_id")
        member_sequences = handoff.payload.get("member_sequences")
        if not isinstance(member_sequences, list):
            continue
        if not isinstance(run_id, int):
            if handoff.payload.get("state") != "needs_manual":
                continue
            for raw_sequence in member_sequences:
                if not isinstance(raw_sequence, int):
                    continue
                member = await repo.get_change(LOCAL_OWNER, raw_sequence)
                if member is None or member.kind != "scope_member":
                    continue
                prior = await repo.get_latest_member_outcome(LOCAL_OWNER, member.sequence)
                if prior is None:
                    await _require_lease(session, job)
                    await repo.append_change(
                        LOCAL_OWNER,
                        change_key=f"p4:outcome:{member.sequence}:needs_manual:no_origin",
                        kind="scope_outcome", parent_sequence=member.parent_sequence,
                        listing_id=member.listing_id, context=member.context,
                        source_manifest_hash=member.source_manifest_hash,
                        payload={"member_sequence": member.sequence,
                                 "state": "needs_manual",
                                 "reason": "origin_run_unavailable"},
                    )
                    if member.listing_id is not None and member.context is not None:
                        state_row = await repo.ensure_refresh(
                            LOCAL_OWNER, member.listing_id, member.context,
                        )
                        await _require_lease(session, job)
                        await repo.save_refresh(state_row.model_copy(update={
                            "revision": state_row.revision + 1,
                            "blocked_reason": "needs_manual",
                        }), expected_revision=state_row.revision)
                    settled += 1
            continue
        run = await session.get(PipelineRun, run_id, populate_existing=True)
        if run is None:
            for raw_sequence in member_sequences:
                if not isinstance(raw_sequence, int):
                    continue
                member = await repo.get_change(LOCAL_OWNER, raw_sequence)
                if member is None or member.kind != "scope_member":
                    continue
                prior = await repo.get_latest_member_outcome(LOCAL_OWNER, member.sequence)
                if prior is not None:
                    continue
                await _require_lease(session, job)
                await repo.append_change(
                    LOCAL_OWNER,
                    change_key=f"p4:outcome:{member.sequence}:needs_manual:missing_run",
                    kind="scope_outcome", parent_sequence=member.parent_sequence,
                    listing_id=member.listing_id, context=member.context,
                    source_manifest_hash=member.source_manifest_hash,
                    payload={"member_sequence": member.sequence,
                             "state": "needs_manual", "reason": "origin_run_missing"},
                )
                settled += 1
            continue
        if run.status not in {"success", "partial", "failed"}:
            continue
        current_outcomes = {}
        for sequence in member_sequences:
            outcome = await repo.get_latest_member_outcome(LOCAL_OWNER, int(sequence))
            if outcome is not None:
                current_outcomes[int(sequence)] = outcome
        if len(current_outcomes) == len(member_sequences) and all(
            item.payload.get("state") in {"completed", "research_required_no_case", "needs_manual"}
            for item in current_outcomes.values()
        ):
            continue
        state = "needs_manual"
        reason = "origin_run_terminal_unverified"
        verified_manifest_hash: str | None = None
        if run.status == "success":
            try:
                _, authorized = await authorize_supplemental_run(
                    session, run.id, owner_id="local_owner"
                )
                native_origin_proof = False
                if authorized.supplemental_update_sequence is None:
                    native_origin_proof = True
                    for sequence in member_sequences:
                        member = await repo.get_change(LOCAL_OWNER, int(sequence))
                        if member is None:
                            native_origin_proof = False
                            break
                        source = await repo.get_change(
                            LOCAL_OWNER, int(member.payload.get("source_sequence", 0))
                        )
                        if (
                            source is None or source.origin_run_id != run.id
                            or source.source_manifest_hash != authorized.manifest.manifest_hash
                        ):
                            native_origin_proof = False
                            break
                if authorized.supplemental_update_sequence is not None or native_origin_proof:
                    checkpoint = _read_checkpoint(run.metadata_json, authorized.manifest)
                    await _verify_checkpoint(session, checkpoint, authorized)
                    state, reason = "completed", "p2_run_verified_success"
                    verified_manifest_hash = authorized.manifest.manifest_hash
            except Exception:
                state, reason = "needs_manual", "p2_run_result_unverifiable"
        review_scopes: dict[tuple[int, str], list[int]] = {}
        for member_sequence in member_sequences:
            if not await _p4_gates_open(session, effective):
                return settled
            sequence = int(member_sequence)
            prior_outcome = await repo.get_latest_member_outcome(LOCAL_OWNER, sequence)
            if prior_outcome is not None and prior_outcome.payload.get("state") in {
                "completed", "research_required_no_case", "needs_manual",
            }:
                continue
            member_row = await repo.get_change(LOCAL_OWNER, sequence)
            if member_row is None or member_row.kind != "scope_member":
                continue
            actual_state, actual_reason = state, reason
            if state == "completed":
                case_id = await session.scalar(select(InvestorCase.id).where(
                    InvestorCase.owner_id == LOCAL_OWNER,
                    InvestorCase.listing_id == member_row.listing_id,
                    InvestorCase.payload["status"].as_string() != "archived",
                ).limit(1))
                if case_id is None:
                    actual_state = "research_required_no_case"
                    actual_reason = "no_active_investor_case"
                elif (
                    not isinstance(member_row.payload.get("resume_of_member_sequence"), int)
                    and member_row.listing_id is not None and member_row.context is not None
                    and member_row.context in {"company_research", "holding_review"}
                ):
                    source = await repo.get_change(
                        LOCAL_OWNER, int(member_row.payload.get("source_sequence", 0))
                    )
                    previous = await repo.get_previous_completed_scope_member(
                        LOCAL_OWNER, member_row.listing_id, member_row.context,
                        member_row.sequence,
                    )
                    previous_source = None
                    if previous is not None:
                        previous_source = await repo.get_change(
                            LOCAL_OWNER, int(previous.payload.get("source_sequence", 0))
                        )
                    if _review_needed(source, member_row, previous, previous_source):
                        review_scopes.setdefault(
                            (member_row.listing_id, member_row.context), []
                        ).append(sequence)
                    else:
                        actual_reason = "no_material_change_or_irrelevant_context"
            await _require_lease(session, job)
            member_outcome = await repo.append_change(
                LOCAL_OWNER,
                change_key=f"p4:outcome:{sequence}:{actual_state}",
                kind="scope_outcome",
                parent_sequence=member_row.parent_sequence,
                listing_id=member_row.listing_id, context=member_row.context,
                origin_run_id=run.id,
                source_manifest_hash=verified_manifest_hash or member_row.source_manifest_hash,
                payload={"member_sequence": sequence, "run_id": run.id,
                         "state": actual_state, "reason": actual_reason},
            )
            outcome_by_member[sequence] = member_outcome
            settled += 1
            resume_of = member_row.payload.get("resume_of_member_sequence")
            if isinstance(resume_of, int):
                original_member = await repo.get_change(LOCAL_OWNER, resume_of)
                if original_member is not None and original_member.kind == "scope_member":
                    resumed_state = (
                        "needs_manual" if member_row.payload.get("resume_has_manual_pair")
                        else actual_state
                    )
                    await _require_lease(session, job)
                    original_outcome = await repo.append_change(
                        LOCAL_OWNER,
                        change_key=f"p4:resume-settled:{member_row.sequence}:{resume_of}",
                        kind="scope_outcome", parent_sequence=original_member.parent_sequence,
                        listing_id=original_member.listing_id, context=original_member.context,
                        origin_run_id=run.id,
                        source_manifest_hash=verified_manifest_hash or original_member.source_manifest_hash,
                        payload={"member_sequence": resume_of, "run_id": run.id,
                                 "state": resumed_state,
                                 "reason": "resumed_pending_pairs_complete" if resumed_state == "completed"
                                 else "resumed_scope_still_requires_manual_review"},
                    )
                    outcome_by_member[resume_of] = original_outcome
                    if (
                        resumed_state in {"completed", "research_required_no_case"}
                        and actual_state == "completed"
                        and original_member.listing_id is not None
                        and original_member.context is not None
                    ):
                        review_scopes.setdefault(
                            (original_member.listing_id, original_member.context), []
                        ).append(resume_of)
        if verified_manifest_hash is not None and review_scopes:
            from investment_agent.investor.monitoring import MonitoringService

            monitor = MonitoringService(session, settings)
            for (listing_id, context), sequences in review_scopes.items():
                await _require_lease(session, job)
                await monitor.dispatch_update_scope(
                    listing_id=listing_id, context=context,
                    member_sequences=sequences,
                    source_manifest_hash=verified_manifest_hash,
                )
    # Advance a scope's high watermark only over contiguous member records that
    # each have a durable successful outcome. A manual/unknown item stays dirty.
    scope_members = [member for handoff in handoffs
                     for sequence in handoff.payload.get("member_sequences", [])
                     if isinstance(sequence, int)
                     for member in [await repo.get_change(LOCAL_OWNER, sequence)]
                     if member is not None and member.listing_id is not None and member.context is not None]
    for listing_id, context in sorted({
        (member.listing_id, member.context) for member in scope_members
        if member.listing_id is not None and member.context is not None
    }):
        current = await repo.ensure_refresh(LOCAL_OWNER, listing_id, context)
        pending = await repo.list_changes(
            LOCAL_OWNER, after_sequence=current.confirmed_sequence,
            limit=100, kinds=("scope_member",), listing_id=listing_id, context=context,
        )
        confirmed = current.confirmed_sequence
        for member in pending:
            pending_outcome = await repo.get_latest_member_outcome(LOCAL_OWNER, member.sequence)
            if pending_outcome is None or pending_outcome.payload.get("state") not in {
                "completed", "research_required_no_case",
            }:
                break
            confirmed = member.sequence
        if confirmed > current.confirmed_sequence:
            await _require_lease(session, job)
            await repo.save_refresh(current.model_copy(update={
                "revision": current.revision + 1,
                "confirmed_sequence": confirmed,
                "blocked_reason": None,
                "active_run_id": None,
                "last_success": datetime.now(UTC),
            }), expected_revision=current.revision)
    if handoffs:
        await _require_lease(session, job)
        await repo.save_source(cursor.model_copy(update={
            "revision": cursor.revision + 1,
            "payload": {**cursor.payload, "after_sequence": handoffs[-1].sequence},
        }), expected_revision=cursor.revision)
    return settled


def _review_needed(
    source: Any, member: Any, previous_member: Any = None, previous_source: Any = None,
) -> bool:
    """Conservatively require review only for meaningful research-scope changes."""
    if source is None:
        return False
    kind = source.kind
    payload = source.payload if isinstance(source.payload, dict) else {}
    if payload.get("bootstrap_initial") is True:
        return False
    if kind == "due_boundary":
        boundary = payload.get("boundary")
        return boundary in {
            "rule_valid_until", "rule_review_due", "research_requirement_check_by",
            "application_review_due", "application_valid_until", "event_effective_from",
            "event_effective_to", "material_binding_valid_to",
        }
    if kind not in {"rule_lifecycle", "research_event", "evidence_revision", "material_revision"}:
        return False
    if previous_member is None or previous_source is None:
        return False
    if previous_member.sequence >= member.sequence:
        return False
    previous_kind = previous_member.payload.get("source_kind")
    if previous_kind != kind or previous_source.kind != kind:
        return True

    def semantic_projection(row: Any) -> Any:
        payload = row.payload if isinstance(row.payload, dict) else {}
        if kind == "rule_lifecycle":
            # Suspension/retirement is materially different even if the rule
            # definition has not changed.
            return {
                "state": payload.get("state"),
                "command": payload.get("command"),
                "semantic_hash": payload.get("semantic_hash"),
                "content_hash": payload.get("content_hash"),
            }
        if kind == "research_event":
            return {"content_hash": payload.get("content_hash"),
                    "event_key": payload.get("event_key")}
        sources = payload.get("sources")
        if isinstance(sources, list):
            rows = [item for item in sources if isinstance(item, dict)]
            return {"sources": sorted((
                update_digest(item.get("ref")), item.get("content_hash"), item.get("status")
            ) for item in rows)}
        return {"content_hash": payload.get("content_hash"),
                "source_hash": payload.get("source_hash")}

    current_projection = semantic_projection(source)
    prior_projection = semantic_projection(previous_source)
    if current_projection == prior_projection:
        return False
    # Controlled revalidation may mint a new receipt while preserving exact
    # manifest semantics. Ignore timestamps and operation IDs.
    current_manifest = source.payload.get("manifest_hash")
    prior_manifest = previous_source.payload.get("manifest_hash")
    return not (
        isinstance(current_manifest, str) and current_manifest == prior_manifest
    )


async def _confirmed_scope_sequence(
    confirmed_sequence: int, members: list[Any], repo: SupplementalUpdatesRepository,
    owner_id: OwnerId,
) -> int:
    """Advance a bootstrap scope watermark only through contiguous local proofs."""
    confirmed = confirmed_sequence
    for member in members:
        outcome = await repo.get_latest_member_outcome(owner_id, member.sequence)
        if outcome is None or outcome.payload.get("state") not in {
            "completed", "research_required_no_case",
        }:
            break
        confirmed = member.sequence
    return confirmed


async def validate_review_batch(
    session: AsyncSession, *, owner_id: OwnerId, batch_id: UUID,
    batch_hash: str | None, task_id: UUID,
) -> None:
    """Validate a server-bound update batch before every paid execution step."""
    from investment_agent.investor.foundations import InvestorError

    repo = SupplementalUpdatesRepository(session)
    batch = await repo.get_change(owner_id, batch_id)
    if (
        batch is None or batch.kind != "scope_handoff"
        or batch.payload_hash != batch_hash
        or batch.payload.get("purpose") != "investor_review"
    ):
        raise InvestorError("invalid_reference", "supplemental_update_batch_unavailable")
    cycle_id = batch.payload.get("cycle_id")
    cycle = await session.get(ReviewCycle, cycle_id) if isinstance(cycle_id, str) else None
    if (
        cycle is None or cycle.task_id != str(task_id) or cycle.state != "running"
        or cycle.payload.get("supplemental_update_batch_id") != str(batch.id)
        or cycle.payload.get("supplemental_update_batch_hash") != batch.payload_hash
    ):
        raise InvestorError("invalid_reference", "supplemental_update_batch_task_mismatch")
    sequences = batch.payload.get("member_sequences")
    if not isinstance(sequences, list) or not sequences:
        raise InvestorError("invalid_reference", "supplemental_update_batch_unavailable")
    for sequence in sequences:
        member = await repo.get_change(owner_id, sequence)
        expected_manifest = batch.payload.get("member_source_manifest_hashes", {}).get(
            str(sequence)
        ) if isinstance(batch.payload.get("member_source_manifest_hashes"), dict) else None
        if (
            member is None or member.kind != "scope_member"
            or member.listing_id != batch.payload.get("listing_id")
            or member.context != batch.payload.get("context")
        ):
            raise InvestorError("invalid_reference", "supplemental_update_batch_unavailable")
        outcome = await repo.get_latest_member_outcome(owner_id, member.sequence)
        if outcome is None or outcome.payload.get("state") not in {
            "completed", "research_required_no_case",
        } or outcome.source_manifest_hash != expected_manifest:
            raise InvestorError("invalid_reference", "supplemental_update_batch_member_incomplete")
        run_id = outcome.payload.get("run_id")
        if not isinstance(run_id, int) or outcome.origin_run_id != run_id:
            raise InvestorError("invalid_reference", "supplemental_update_batch_member_incomplete")
        try:
            from investment_agent.supplemental_rules.jobs import (
                _read_checkpoint,
                _verify_checkpoint,
                authorize_supplemental_run,
            )

            run, authorized = await authorize_supplemental_run(
                session, run_id, owner_id=owner_id,
            )
            manifest_scope = {
                (int(listing_ref.id), context)
                for listing_ref in authorized.manifest.listing_refs
                for context in authorized.manifest.contexts
            }
            if (
                run.status != "success"
                or authorized.manifest.manifest_hash != expected_manifest
                or (member.listing_id, member.context) not in manifest_scope
            ):
                raise ValueError("P2 member pair is not in the authorized manifest")
            if authorized.supplemental_update_sequence is None:
                source = await repo.get_change(
                    owner_id, int(member.payload.get("source_sequence", 0)),
                )
                if (
                    source is None or source.origin_run_id != run_id
                    or source.source_manifest_hash != expected_manifest
                ):
                    raise ValueError("native P2 source does not bind this member")
            checkpoint = _read_checkpoint(run.metadata_json, authorized.manifest)
            await _verify_checkpoint(session, checkpoint, authorized)
        except Exception as exc:
            raise InvestorError(
                "invalid_reference", "supplemental_update_batch_member_incomplete"
            ) from exc
