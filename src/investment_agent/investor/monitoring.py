"""Bounded, trusted committed-snapshot/calendar adapter and durable review queue.

No model supplied events or executable conditions. All methods join the caller's
short transaction; no model/network calls occur while holding the writer lock.
"""
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.budget import BudgetService
from investment_agent.agent.contracts import ContextBinding, Task
from investment_agent.agent.execution import (
    ExecutionDeniedError,
    ExecutionGrant,
    admit,
    cost_limits,
    live_policy,
)
from investment_agent.agent.research import registrations
from investment_agent.agent.tasks import TaskService
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef, utc_timestamp
from investment_agent.investor.contracts import Case, MonitorSpec, ReviewEvent
from investment_agent.investor.controls import UserControlService
from investment_agent.investor.foundations import InvestorError, InvestorService
from investment_agent.investor.publication import PublicationService
from investment_agent.storage.models.agent import AgentTask
from investment_agent.storage.models.investor import InvestorCase
from investment_agent.storage.models.monitoring import (
    InvestorNotification,
    MonitorReceipt,
    ReviewCycle,
)
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot
from investment_agent.storage.models.publication import PublicationOutbox

ACTIVE = ("pending", "running", "blocked")
TERMINAL = {"succeeded", "partial", "failed", "cancelled"}


class MonitoringService:
    def __init__(self, session: AsyncSession, base: Settings) -> None:
        self.session, self.base = session, base
        self.publication = PublicationService(session)

    async def _receipt(self, case: Case, key: str, payload: dict[str, Any]) -> bool:
        if await self.session.get(MonitorReceipt, key):
            return False
        self.session.add(MonitorReceipt(key=key, case_id=str(case.id), payload=payload))
        await self.session.flush()
        return True

    async def scan(self, *, now: datetime | None = None, limit: int = 50) -> int:
        """Stable least-recently-checked pagination; max one batch per case.

        Monotonic source row ids detect old-period corrections and new provider
        conflicts as well as new periods. All relevant dependencies are checked,
        independent of which source row moved the cursor. Backfills coalesce.
        """
        now = utc_timestamp(now or datetime.now(UTC))
        # Reserve before selecting the page. Separate worker sessions therefore
        # cannot both advance a cursor or schedule the same cycle.
        connection = await self.session.connection()
        if connection.dialect.name == "sqlite":
            active = await connection.run_sync(lambda c: bool(getattr(c.connection.driver_connection, "in_transaction", False)))
            if not active:
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
        rows = (await self.session.scalars(select(InvestorCase).outerjoin(MonitorReceipt,
            MonitorReceipt.key == "scan:" + InvestorCase.id).where(InvestorCase.owner_id == "local_owner")
            .order_by(MonitorReceipt.payload["checked_at"].as_string().asc().nulls_first(), InvestorCase.id)
            .limit(min(max(limit, 1), 100)))).all()
        for row in rows:
            await self.publication._reserve(UUID(row.id))
            case = Case.model_validate(row.payload)
            cursor = await self.session.get(MonitorReceipt, f"scan:{case.id}")
            old = cursor.payload if cursor else {}
            positions: dict[str, int] = {}
            source_refs: list[dict[str, Any]] = []
            for kind, model in (("financial_snapshot", FinancialSnapshot), ("market_snapshot", MarketSnapshot)):
                position = await self.session.scalar(select(func.max(model.id)).where(model.listing_id == row.listing_id, model.captured_at <= now))
                positions[kind] = position or 0
                if position and position > old.get(kind, 0):
                    snapshot = await self.session.get(model, position)
                    assert isinstance(snapshot, (FinancialSnapshot, MarketSnapshot))
                    source_refs.append({"kind": kind, "id": position, "revision": snapshot.version,
                        "captured_at": (snapshot.captured_at.replace(tzinfo=UTC) if snapshot.captured_at.tzinfo is None else utc_timestamp(snapshot.captured_at)).isoformat(),
                        "hash": sha256(repr((snapshot.id, snapshot.version, snapshot.captured_at,
                            snapshot.fields if isinstance(snapshot, FinancialSnapshot) else snapshot.close)).encode()).hexdigest()})
            if case.current_recommendation_ref and case.status != "archived":
                view = await self.publication.get_recommendation(case.current_recommendation_ref, at=now)
                terminal = view.status in {"withdrawn", "superseded"}
                reasons = [] if terminal else [r for r in view.limitation_reasons if r not in {"review_required", "expired"} and not r.startswith("monitor_change:")]
                due = not terminal and view.effective_next_review_at <= now
                monitor_dates = []
                for ref in (() if terminal else view.recommendation.monitor_refs):
                    monitor = MonitorSpec.model_validate((await self.publication.artifact(ref)).payload)
                    if monitor.active and monitor.next_due_at <= now:
                        monitor_dates.append(monitor.next_due_at.isoformat())
                # Extended next-review supersedes the original monitor calendar.
                if view.effective_next_review_at > view.recommendation.next_review_at:
                    monitor_dates = []
                sources = ["snapshot_committed:" + sha256(repr(source_refs).encode()).hexdigest()] if source_refs else []
                if due or monitor_dates:
                    sources.append("calendar_due:" + view.effective_next_review_at.isoformat())
                if reasons:
                    sources.append("limitations:" + sha256(repr(reasons).encode()).hexdigest())
                new_events = []
                for source in sources:
                    event_key = f"change:{case.id}:" + sha256(repr((str(case.current_recommendation_ref.id), source)).encode()).hexdigest()
                    if await self._receipt(case, event_key, {"checked_at": now.isoformat(), "source": source,
                        "source_refs": source_refs, "reasons": reasons, "changed": bool(reasons)}):
                        new_events.append(event_key)
                if new_events and (reasons or due or monitor_dates):
                    for event_key in new_events:
                        await self._merge(case, event_key, reasons, source_refs, now)
            payload = {**positions, "checked_at": now.isoformat()}
            if cursor:
                cursor.payload = payload
            else:
                self.session.add(MonitorReceipt(key=f"scan:{case.id}", case_id=str(case.id), payload=payload))
        await self.session.flush()
        return len(rows)

    async def _merge(self, case: Case, event_key: str, reasons: list[str], sources: list[dict[str, Any]], now: datetime) -> ReviewCycle:
        cycle = await self.session.scalar(select(ReviewCycle).where(ReviewCycle.case_id == str(case.id), ReviewCycle.state.in_(ACTIVE)).order_by(ReviewCycle.id))
        if cycle is None:
            cycle = ReviewCycle(id=str(uuid5(case.id, event_key)), case_id=str(case.id), task_id=None, state="pending", payload={
                "recommendation_ref": case.current_recommendation_ref.model_dump(mode="json") if case.current_recommendation_ref else None,
                "created_at": now.isoformat(), "events": [], "dirty": [], "reasons": [], "source_refs": [], "attempted": False})
            self.session.add(cycle)
        data = dict(cycle.payload)
        target = "dirty" if cycle.state == "running" else "events"
        data[target] = list(dict.fromkeys([*data[target], event_key]))
        data["reasons"] = list(dict.fromkeys([*data["reasons"], *reasons]))
        data["source_refs"] = [*data["source_refs"], *sources]
        cycle.payload = data
        if reasons:
            # Calendar checks preserve still valid opinions. Actual dependency
            # changes are restricted immediately; read projection also guards.
            existing_view = await self.publication.get_recommendation(case.current_recommendation_ref) if case.current_recommendation_ref else None
            if case.current_recommendation_ref and existing_view and not any(r.startswith("monitor_change:") for r in existing_view.limitation_reasons):
                await self.publication.restrict(case.current_recommendation_ref, reason="monitor_change:" + event_key, at=now)
            await self._notification(cycle, category="major_change", stage="review_required", now=now)
        await self.session.flush()
        return cycle

    async def dispatch(self, cycle: ReviewCycle, *, interactive: bool = False) -> Task | None:
        await self.publication._reserve(UUID(cycle.case_id))
        await self.session.refresh(cycle)
        if cycle.task_id:
            return await TaskService(self.session).get_task(UUID(cycle.task_id))
        case = await InvestorService(self.session).get_case("local_owner", UUID(cycle.case_id))
        if case.status == "archived":
            cycle.state = "stopped"
            cycle.payload = {**cycle.payload, "pending_reason": "scope_archived"}
            return None
        mandate = await InvestorService(self.session).get_mandate("local_owner")
        refs = (case.listing_ref, ObjectRef(kind="case", id=case.id, revision=case.revision))
        grant = ExecutionGrant(mode="interactive" if interactive else "background",
            source="authenticated_message" if interactive else "authorized_background",
            prompt="复核观点。旧观点、触发原因与确切来源收据：" + repr(cycle.payload),
            tool_names=tuple(r.name for r in registrations()) + ("publish_recommendation", "extend_recommendation"),
            scope_refs=refs, mandate_id=mandate.id, current_research=True, research_kind="review", purpose="review")
        try:
            settings = await live_policy(self.session, grant, self.base)
            await cost_limits(self.session, grant)
            if not interactive and BudgetService.prices(settings) == (None, None):
                raise ExecutionDeniedError("background_price_unavailable")
            now = datetime.now(UTC)
            task = await admit(self.session, base=self.base, input_key=f"review:{cycle.id}",
                binding=ContextBinding(context_refs=refs, base_refs=(*refs, ObjectRef(kind="mandate", id=mandate.id, revision=mandate.revision)), as_of=now),
                grant=grant, kind="agent_review")
        except ExecutionDeniedError as exc:
            cycle.state = "blocked"
            cycle.payload = {**cycle.payload, "pending_reason": str(exc), "dispatch_checked_at": datetime.now(UTC).isoformat()}
            return None
        cycle.task_id, cycle.state = str(task.id), "running"
        cycle.payload = {**cycle.payload, "attempted": True, "started_at": now.isoformat(), "consumed_events": list(cycle.payload["events"]), "pending_reason": None}
        await self.session.flush()
        return task

    async def manual(self, case_id: UUID, key: UUID, *, expected_revision: int, reason: str = "手动复核") -> dict[str, Any]:
        await self.publication._reserve(case_id)
        case = await InvestorService(self.session).get_case("local_owner", case_id)
        event = f"manual:{case.id}:{key}"
        receipt = await self.session.get(MonitorReceipt, event)
        if receipt:
            if receipt.payload["expected_revision"] != expected_revision or receipt.payload.get("reason", "手动复核") != reason:
                raise InvestorError("idempotency_conflict", "Review key reused with different revision")
            cycle = await self.session.get(ReviewCycle, receipt.payload["cycle_id"])
            assert cycle is not None
            return self.cycle_view(cycle)
        if case.revision != expected_revision:
            raise InvestorError("revision_conflict", "Case changed; reload before requesting review")
        if case.status == "archived" or case.current_recommendation_ref is None:
            raise InvestorError("invalid_reference", "An active saved recommendation is required")
        # Explicit requests may retry failed research, but never duplicate an
        # already running cycle. They do not require background permission.
        if case.background_paused:
            active = await self.session.scalar(select(ReviewCycle).where(ReviewCycle.case_id == str(case.id), ReviewCycle.state == "running"))
            if active and active.task_id:
                from investment_agent.storage.models.agent import ExecutionGrantRow
                authorization = await self.session.get(ExecutionGrantRow, active.task_id)
                if authorization and authorization.payload["mode"] == "background":
                    await TaskService(self.session).cancel(UUID(active.task_id))
                    active.state = "stopped"
                    active.payload = {**active.payload, "pending_reason": "manual_review_replaces_paused_background"}
        cycle = await self._merge(case, event, [], [], datetime.now(UTC))
        cycle.payload = {**cycle.payload, "questions": {**cycle.payload.get("questions", {}), event: reason}, "interactive_requested": True}
        await self.dispatch(cycle, interactive=True)
        await self._receipt(case, event, {"cycle_id": cycle.id, "expected_revision": expected_revision, "reason": reason})
        return self.cycle_view(cycle)

    @staticmethod
    def cycle_view(cycle: ReviewCycle) -> dict[str, Any]:
        return {"id": cycle.id, "case_id": cycle.case_id, "task_id": cycle.task_id, "state": cycle.state, **cycle.payload}

    async def settle(self, *, now: datetime | None = None, limit: int = 50) -> int:
        now = utc_timestamp(now or datetime.now(UTC))
        cycles = (await self.session.scalars(select(ReviewCycle).join(AgentTask, AgentTask.id == ReviewCycle.task_id).where(ReviewCycle.state == "running", AgentTask.status.in_(TERMINAL)).order_by(ReviewCycle.id).limit(limit))).all()
        for cycle in cycles:
            assert cycle.task_id
            task = await self.session.get(AgentTask, cycle.task_id)
            if task is None or task.status not in TERMINAL:
                continue
            await self.publication._reserve(UUID(cycle.case_id))
            await self.session.refresh(cycle)
            if cycle.state != "running":
                continue
            model = Task.model_validate(task.payload)
            reviews = []
            for ref in model.artifact_refs:
                if ref.kind == "review":
                    reviews.append(ReviewEvent.model_validate((await self.publication.artifact(ref)).payload))
            recommendations = [r for r in model.artifact_refs if r.kind == "recommendation"]
            usable = bool(reviews or recommendations) and task.status == "succeeded"
            cycle.state = "completed" if usable else "stopped"
            cycle.payload = {**cycle.payload, "completed_at": now.isoformat(), "task_status": task.status,
                "result_refs": [r.model_dump(mode="json") for r in model.artifact_refs],
                "pending_reason": None if usable else str(model.stop_reason or "review_not_completed")}
            if any(r.result == "inconclusive" for r in reviews) or task.status == "partial":
                await self._notification(cycle, category="needs_input", stage="needs_input", now=now)
            elif recommendations or any(r.result != "maintain" or r.reasoning_revision for r in reviews):
                await self._notification(cycle, category="major_change", stage="reviewed", now=now)
            dirty = cycle.payload["dirty"]
            if usable and dirty:
                case = await InvestorService(self.session).get_case("local_owner", UUID(cycle.case_id))
                # Latest unresolved changes only; consumed original batch stays
                # in history. Failure/cancellation never auto-starts more costs.
                if case.current_recommendation_ref:
                    current = await self.publication.get_recommendation(case.current_recommendation_ref, at=now)
                    reasons = [r for r in current.limitation_reasons if r not in {"review_required", "expired"} and not r.startswith("monitor_change:")]
                    if reasons:
                        await self._merge(case, dirty[-1], reasons, cycle.payload["source_refs"], now)
                pending_questions = {event: question for event, question in cycle.payload.get("questions", {}).items() if event in dirty}
                if pending_questions and case.status != "archived":
                    followup = await self._merge(case, next(iter(pending_questions)), [], [], now)
                    followup.payload = {**followup.payload, "questions": pending_questions, "interactive_requested": True}

        await self.session.flush()
        return len(cycles)

    async def _notification(self, cycle: ReviewCycle, *, category: str, stage: str, now: datetime) -> None:
        await self._upsert("review:" + cycle.id, cycle.case_id, {"category": category, "stage": stage,
            "cycle_id": cycle.id, "original_recommendation_ref": cycle.payload["recommendation_ref"],
            "reasons": cycle.payload["reasons"], "source_refs": cycle.payload["source_refs"],
            "result_refs": [ref for ref in cycle.payload.get("result_refs", []) if ref["kind"] in {"thesis", "recommendation", "review"}], "updated_at": now.isoformat()})

    async def _upsert(self, key: str, case_id: str, payload: dict[str, Any]) -> None:
        row = await self.session.scalar(select(InvestorNotification).where(InvestorNotification.key == key))
        # Delivery-time control, including later updates. Suppressed receipts are
        # consumed permanently; unmuting cannot replay old noise.
        if not await UserControlService(self.session).notifications_allowed(UUID(case_id)):
            return
        if row:
            prior = {k: v for k, v in row.payload.items() if k not in {"read", "updated_at"}}
            incoming = {k: v for k, v in {**row.payload, **payload}.items() if k not in {"read", "updated_at"}}
            if prior == incoming:
                return
            row.revision += 1
            row.payload = {**row.payload, **payload, "read": False}
        else:
            self.session.add(InvestorNotification(key=key, owner_id="local_owner", case_id=case_id,
                revision=1, payload={**payload, "read": False}))
        await self.session.flush()
        from investment_agent.investor.notifications import freeze
        current = await self.session.scalar(select(InvestorNotification).where(InvestorNotification.key == key))
        if current is not None:
            await freeze(self.session, self.base, current)

    async def consume_outbox(self, *, now: datetime | None = None, limit: int = 100) -> int:
        now = utc_timestamp(now or datetime.now(UTC))
        rows = (await self.session.scalars(select(PublicationOutbox).where(PublicationOutbox.state == "pending").order_by(PublicationOutbox.id).limit(limit))).all()
        for row in rows:
            ref = ObjectRef.model_validate(row.payload["recommendation_ref"])
            artifact = await self.publication.artifact(ref)
            await self.publication._reserve(UUID(artifact.case_id))
            await self.session.refresh(row)
            if row.state != "pending":
                continue
            # Attribute through trusted Task artifact refs, never by client key.
            cycles = (await self.session.scalars(select(ReviewCycle).where(ReviewCycle.case_id == artifact.case_id, ReviewCycle.task_id.is_not(None)))).all()
            linked = None
            for cycle in cycles:
                task = await self.session.get(AgentTask, cycle.task_id)
                if task and ref in Task.model_validate(task.payload).artifact_refs:
                    linked = cycle
                    break
            if linked:
                linked_task = await TaskService(self.session).get_task(UUID(linked.task_id))
                linked.payload = {**linked.payload, "result_refs": [r.model_dump(mode="json") for r in linked_task.artifact_refs]}
                await self._notification(linked, category="major_change", stage="reviewed", now=now)
            else:
                await self._upsert(row.notification_key, artifact.case_id, {"category": "major_change", "stage": "published",
                    "recommendation_ref": ref.model_dump(mode="json"), "updated_at": now.isoformat()})
            row.state = "consumed"
        await self.session.flush()
        return len(rows)

    async def maintain(self, *, limit: int = 50) -> None:
        from investment_agent.portfolio.plans import maintain_portfolio_plans
        await maintain_portfolio_plans(self.session, self.base, limit=limit)
        await self.scan(limit=limit)
        await self.settle(limit=limit)
        cycles = (await self.session.scalars(select(ReviewCycle).where(ReviewCycle.state.in_(("pending", "blocked"))).order_by(ReviewCycle.payload["dispatch_checked_at"].as_string().asc().nulls_first(), ReviewCycle.id).limit(limit))).all()
        for cycle in cycles:
            # Authorization blocks are cheap to recheck. Once a Task exists no
            # admission retry occurs, including day boundaries and budget stops.
            if not cycle.payload.get("attempted"):
                await self.dispatch(cycle, interactive=bool(cycle.payload.get("interactive_requested")))
        await self.consume_outbox(limit=limit)

    async def notifications(self, *, after: int = 0, limit: int = 50, unread: bool = False) -> list[dict[str, Any]]:
        stmt = select(InvestorNotification).where(InvestorNotification.owner_id == "local_owner", InvestorNotification.sequence > after)
        if unread:
            stmt = stmt.where(InvestorNotification.payload["read"].as_boolean().is_(False))
        rows = (await self.session.scalars(stmt.order_by(InvestorNotification.sequence).limit(min(max(limit, 1), 100)))).all()
        return [{"sequence": r.sequence, "revision": r.revision, "case_id": r.case_id, **r.payload} for r in rows]

    async def read(self, sequence: int, *, expected_revision: int, read: bool, key: UUID | None = None) -> dict[str, Any]:
        row = await self.session.get(InvestorNotification, sequence)
        if row is None or row.owner_id != "local_owner":
            raise InvestorError("not_found", "Notification unavailable")
        await self.publication._reserve(UUID(row.case_id))
        await self.session.refresh(row)
        request = {"sequence": sequence, "expected_revision": expected_revision, "read": read}
        receipt_key = f"notification_read:{key}"
        receipt = await self.session.get(MonitorReceipt, receipt_key) if key else None
        if receipt:
            if receipt.payload["request"] != request:
                raise InvestorError("idempotency_conflict", "Notification key reused with different request")
            return dict(receipt.payload["result"])
        if row.payload["read"] == read:
            result = {"sequence": row.sequence, "revision": row.revision, **row.payload}
            if key:
                self.session.add(MonitorReceipt(key=receipt_key, case_id=row.case_id, payload={"request": request, "result": result}))
                await self.session.flush()
            return result
        if row.revision != expected_revision:
            raise InvestorError("revision_conflict", "Notification changed")
        row.revision += 1
        row.payload = {**row.payload, "read": read}
        await self.session.flush()
        result = {"sequence": row.sequence, "revision": row.revision, **row.payload}
        if key:
            self.session.add(MonitorReceipt(key=receipt_key, case_id=row.case_id, payload={"request": request, "result": result}))
            await self.session.flush()
        return result


async def verify_unchanged_review(session: AsyncSession, task_id: UUID, review: ReviewEvent, publication: PublicationService) -> None:
    """A monitor cycle cannot call missing or unchanged old data a fresh check.

    Manual/non-monitor historical research retains the existing AG12 guards.
    Monitor maintain must have a successful registered query receipt containing
    fresh current snapshot sources, and explicitly check those saved versions.
    """
    method_view = await publication.get_recommendation(review.recommendation_ref)
    await publication._check_method_renewal(task_id, method_view.recommendation)
    cycle = await session.scalar(select(ReviewCycle).where(ReviewCycle.task_id == str(task_id)))
    if cycle is None:
        return
    from investment_agent.agent.observation import effective_cutoff
    from investment_agent.evidence.contracts import EvidenceRecord
    from investment_agent.storage.models.agent import ToolAction
    view = await publication.get_recommendation(review.recommendation_ref)
    if view.status != "published":
        raise InvestorError("invalid_reference", "Restricted or expired opinions require a new recommendation")
    actions = (await session.scalars(select(ToolAction).where(ToolAction.task_id == str(task_id), ToolAction.state == "committed"))).all()
    queried: set[tuple[str, int, int]] = set()
    def collect(value: Any) -> None:
        if isinstance(value, dict):
            source = value.get("source_ref")
            if isinstance(source, dict) and source.get("kind") in {"financial_snapshot", "market_snapshot"}:
                queried.add((source["kind"], int(source["id"]), int(source["revision"])))
            if value.get("kind") == "evidence" and "id" in value and "revision" in value:
                evidence_query_refs.add(ObjectRef.model_validate(value))
            for nested in value.values():
                collect(nested)
        elif isinstance(value, list):
            for nested in value:
                collect(nested)
    completed_checks = 0
    evidence_query_refs: set[ObjectRef] = set()
    for action in actions:
        if action.result and action.result.get("status") == "ok" and action.parameters.get("tool") in {"get_evidence", "get_snapshot", "capture_snapshot"}:
            completed_checks += 1
            collect(action.result)
    from investment_agent.evidence.store import EvidenceService
    for ref in evidence_query_refs:
        captured = await EvidenceService(session).get_evidence("local_owner", ref, review.completed_at)
        if captured.status == "ok" and captured.data and captured.data.source_ref.kind in {"financial_snapshot", "market_snapshot"}:
            source = captured.data.source_ref
            queried.add((source.kind, int(source.id), source.revision))
    checked: list[EvidenceRecord] = []
    for ref in review.evidence_diff_refs:
        if ref.kind == "evidence":
            from investment_agent.evidence.store import EvidenceService
            result = await EvidenceService(session).get_evidence("local_owner", ref, review.completed_at)
            if result.status != "ok" or result.data is None:
                raise InvestorError("invalid_reference", "Checked evidence is unavailable")
            checked.append(result.data)
    cutoff = min(await effective_cutoff(session, task_id), review.completed_at)
    from investment_agent.investor.contracts import RecommendationEvent
    from investment_agent.storage.models.publication import PublicationEvent
    previous_check = view.recommendation.as_of
    events = (await session.scalars(select(PublicationEvent).where(PublicationEvent.recommendation_id == str(review.recommendation_ref.id)))).all()
    for saved_event in events:
        event = RecommendationEvent.model_validate(saved_event.payload)
        if event.type == "extended" and event.checked_at <= cutoff:
            previous_check = max(previous_check, event.checked_at)
    snapshot_dependencies = 0
    for dependency in view.recommendation.dependency_refs:
        if dependency.materiality != "critical":
            continue
        if dependency.ref.kind == "calculation":
            from investment_agent.industry_peers.resolver import (
                PeerContextResolver,
                peer_method_payload,
            )
            if await peer_method_payload(session, "local_owner", dependency.ref) is not None:
                # WP12: the dependency recheck goes through the method entry;
                # renewal blocking for critical peer dependencies is WP13.
                binding = await publication.saved_peer_binding(view.recommendation)
                if binding is None:
                    raise InvestorError("invalid_reference", "Peer dependency recheck requires its saved origin-task binding")
                try:
                    validation = await PeerContextResolver(session, binding=binding).verify(dependency.ref)
                except (InvestorError, LookupError, ValueError) as exc:
                    raise InvestorError("invalid_reference", f"Peer dependency no longer verifies: {exc}") from exc
                if validation.inconsistent_sources:
                    raise InvestorError("invalid_reference", "Peer inputs await re-check; this cannot be an unchanged review")
                continue
        from investment_agent.evidence.store import EvidenceService
        from investment_agent.investor.publication_numbers import NumericResolver
        case = await publication.foundation.get_case("local_owner", view.recommendation.case_ids[0])
        resolver = NumericResolver(EvidenceService(session), case.listing_ref, cutoff)
        refs = (dependency.ref,) if dependency.ref.kind == "evidence" else (await resolver.calculation(dependency.ref, require_fresh=False)).fact_refs if dependency.ref.kind == "calculation" else ()
        for ref in refs:
            original = await resolver.fact(ref, require_fresh=False)
            if original.source_ref.kind not in {"financial_snapshot", "market_snapshot"}:
                if ref.model_copy(update={"field_path": None}) not in evidence_query_refs:
                    raise InvestorError("invalid_reference", "Every critical premise needs a completed exact evidence query")
                continue
            snapshot_dependencies += 1
            latest = await publication._snapshot_candidates(original, cutoff)
            if latest.status != "ok" or not latest.data:
                raise InvestorError("invalid_reference", "Fresh current monitor data is unavailable")
            for evidence in latest.data:
                if original.verification == "verified" and evidence.verification != "verified":
                    raise InvestorError("invalid_reference", "Current source quality cannot downgrade a verified premise")
                source = evidence.source_ref
                if evidence.acquired_at <= max(original.acquired_at, previous_check) or (source.kind, int(source.id), source.revision) not in queried:
                    raise InvestorError("invalid_reference", "Old snapshot or incomplete query is not an unchanged review")
                if not any(item.source_ref == source and item.content_hash == evidence.content_hash for item in checked):
                    raise InvestorError("invalid_reference", "Every fresh source version must be explicitly checked")
    if not snapshot_dependencies and not completed_checks:
        raise InvestorError("invalid_reference", "An unchanged review requires completed evidence checks")
