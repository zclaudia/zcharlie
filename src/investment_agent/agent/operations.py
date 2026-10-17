"""Bounded scheduling of the existing durable queue; no new task or budget ledger.

The cursor is durable and protected by a short SQLite writer reservation. Slots
are 3 interactive : 1 review : 1 exploration : 1 legacy, with empty-slot lending.
Within a class, least recently served account/case wins, then immutable enqueue
order. Retries never reset the cursor, task identity or lifetime budget.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.models.agent import AgentTask, BudgetEntry, ExecutionGrantRow
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.app_settings import AppSetting
from investment_agent.storage.models.monitoring import OperationsCursor

if TYPE_CHECKING:
    from investment_agent.config import Settings

AGENT_KINDS = ("agent_turn", "agent_research", "agent_review", "agent_discovery")


async def reconcile_budget(
    session: AsyncSession, *, base: "Settings", entry_id: UUID,
    action: Literal["settle", "release"], operator: str, reason: str,
    usage: dict[str, int] | None = None,
) -> BudgetEntry:
    """Trusted operator seam over the budget ledger's fail-closed unknown entries.

    Nothing here runs automatically or from model output: an operator settles
    real provider-reported usage or releases a request that provably never
    reached the provider, always with an operator+reason audit event on the
    owning task. Only ``state == "unknown"`` rows are accepted.
    """
    from investment_agent.agent.budget import BudgetService

    return await BudgetService(session, base).reconcile(
        entry_id, action=action, operator=operator, reason=reason, usage=usage
    )
SLOTS = ("interactive", "interactive", "interactive", "review", "exploration", "legacy")
# Four total native executions, one slot reserved from all background work.
CONCURRENCY = {"interactive": 4, "review": 2, "exploration": 1, "legacy": 4}
CAPACITY = {"interactive": 128, "review": 96, "exploration": 32}
MAX_CONCURRENCY = 4
MAX_BACKGROUND = 3
AGING_SECONDS = 300


def purpose(run: PipelineRun, grant: ExecutionGrantRow | None) -> str:
    if grant is not None:
        if grant.payload.get("mode") == "interactive":
            return "interactive"
        return "exploration" if grant.payload.get("purpose") == "exploration" else "review"
    if run.kind in AGENT_KINDS and run.request_json.get("queue_class") in CAPACITY:
        return str(run.request_json["queue_class"])
    return {"agent_discovery": "exploration", "agent_review": "review"}.get(
        run.kind, "interactive" if run.kind in AGENT_KINDS else "legacy"
    )


def group(task: AgentTask | None, grant: ExecutionGrantRow | None) -> str:
    data = grant.payload if grant else {}
    refs = data.get("scope_refs", []) or (
        task.payload.get("binding", {}).get("context_refs", []) if task else []
    )
    account = data.get("portfolio_account_id")
    if account:
        return "account:" + str(account)
    ref = next((r for r in refs if r.get("kind") in {"portfolio_account", "account", "case"}), None)
    return str(task.owner_id if task else "legacy") + ":" + (str(ref["id"]) if ref else "general")


async def reserve(session: AsyncSession) -> OperationsCursor:
    await session.execute(
        insert(OperationsCursor)
        .values(id=1, payload={"slot": 0, "served": {}, "count": 0})
        .on_conflict_do_nothing()
    )
    await session.execute(update(OperationsCursor).where(OperationsCursor.id == 1).values(id=1))
    row = await session.get(OperationsCursor, 1, populate_existing=True)
    assert row is not None
    return row


async def classify(
    session: AsyncSession, runs: list[PipelineRun]
) -> list[tuple[PipelineRun, str, str]]:
    ids = [str(r.request_json.get("task_id")) for r in runs if r.kind in AGENT_KINDS]
    tasks = {t.id: t for t in await session.scalars(select(AgentTask).where(AgentTask.id.in_(ids)))}
    grants = {
        g.task_id: g
        for g in await session.scalars(
            select(ExecutionGrantRow).where(ExecutionGrantRow.task_id.in_(ids))
        )
    }
    return [
        (
            r,
            purpose(r, grants.get(str(r.request_json.get("task_id")))),
            group(
                tasks.get(str(r.request_json.get("task_id"))),
                grants.get(str(r.request_json.get("task_id"))),
            ),
        )
        for r in runs
    ]


async def limits(session: AsyncSession) -> tuple[int, dict[str, int]]:
    """Only bounded persisted runtime overrides; malformed rows fail conservative."""
    keys = {
        "agent_queue_max_running": MAX_CONCURRENCY,
        **{"agent_queue_" + p + "_capacity": cap for p, cap in CAPACITY.items()},
    }
    values = dict(keys)
    for row in await session.scalars(select(AppSetting).where(AppSetting.key.in_(list(keys)))):
        maximum = keys[row.key]
        if type(row.value_json) is int and 1 <= row.value_json <= maximum:
            values[row.key] = row.value_json
    return values["agent_queue_max_running"], {
        p: values["agent_queue_" + p + "_capacity"] for p in CAPACITY
    }


async def admission(session: AsyncSession, class_name: str) -> bool:
    await reserve(session)
    rows = list(
        await session.scalars(
            select(PipelineRun).where(
                PipelineRun.kind.in_(AGENT_KINDS), PipelineRun.status.in_(("queued", "running"))
            )
        )
    )
    classes = await classify(session, rows)
    _, capacity = await limits(session)
    return sum(p == class_name for _, p, _ in classes) < capacity[class_name]


async def choose(
    session: AsyncSession, *, kinds: list[str], now: datetime, run_id: int | None = None
) -> int | None:
    cursor = await reserve(session)
    eligible = select(PipelineRun).where(
        PipelineRun.status == "queued",
        PipelineRun.attempt_count < PipelineRun.max_attempts,
        or_(PipelineRun.available_at.is_(None), PipelineRun.available_at <= now),
    )
    if run_id is not None:
        eligible = eligible.where(PipelineRun.id == run_id)
    native = list(
        await session.scalars(
            eligible.where(PipelineRun.kind.in_([k for k in kinds if k in AGENT_KINDS]))
            .order_by(PipelineRun.id)
            .limit(sum(CAPACITY.values()))
        )
    )
    legacy = list(
        await session.scalars(
            eligible.where(PipelineRun.kind.in_([k for k in kinds if k not in AGENT_KINDS]))
            .order_by(PipelineRun.id)
            .limit(128)
        )
    )
    rows = native + legacy
    classes = await classify(session, rows)
    # Limits are global across all kinds/workers, not just this worker's registry.
    global_running = list(
        await session.scalars(
            select(PipelineRun)
            .where(PipelineRun.status == "running", PipelineRun.worker_id.is_not(None))
            .limit(MAX_CONCURRENCY)
        )
    )
    all_running = await classify(session, global_running)
    maximum, _ = await limits(session)
    background_maximum = min(MAX_BACKGROUND, max(1, maximum - 1))
    if len(all_running) >= maximum:
        return None
    running = all_running
    ready = []
    for r, p, g in classes:
        available = r.available_at
        if available and available.tzinfo is None:
            available = available.replace(tzinfo=UTC)
        if (
            r.status != "queued"
            or r.attempt_count >= r.max_attempts
            or available
            and available > now
            or run_id is not None
            and r.id != run_id
        ):
            continue
        if sum(q == p for _, q, _ in running) >= CONCURRENCY[p]:
            continue
        if (
            p != "interactive"
            and sum(q != "interactive" for _, q, _ in running) >= background_maximum
        ):
            continue
        ready.append((r, p, g))
    state = cursor.payload
    slot = int(state.get("slot", 0))
    served = dict(state.get("served", {}))
    for offset in range(len(SLOTS)):
        index = (slot + offset) % len(SLOTS)
        candidates = [(r, g) for r, p, g in ready if p == SLOTS[index]]
        if not candidates:
            continue

        def rank(
            item: tuple[PipelineRun, str],
            class_index: int = index,
            previous: dict[str, Any] = served,
        ) -> tuple[int, int, int]:
            queued_at = item[0].started_at
            if queued_at.tzinfo is None:
                queued_at = queued_at.replace(tzinfo=UTC)
            aged = (now - queued_at).total_seconds() >= AGING_SECONDS
            return (
                0 if aged else 1,
                item[0].id if aged else int(previous.get(SLOTS[class_index] + ":" + item[1], 0)),
                item[0].id,
            )

        selected, scope = min(candidates, key=rank)
        count = int(state.get("count", 0)) + 1
        served[SLOTS[index] + ":" + scope] = count
        # Bound persisted bookkeeping; old inactive scopes become least served.
        if len(served) > 512:
            served = dict(sorted(served.items(), key=lambda item: item[1], reverse=True)[:512])
        cursor.payload = {"slot": (index + 1) % len(SLOTS), "served": served, "count": count}
        await session.flush()
        return selected.id
    return None


async def metrics(session: AsyncSession, *, now: datetime | None = None) -> dict[str, Any]:
    at = now or datetime.now(UTC)
    rows = list(
        await session.scalars(
            select(PipelineRun).where(
                PipelineRun.kind.in_(AGENT_KINDS), PipelineRun.status.in_(("queued", "running"))
            )
        )
    )
    classes = await classify(session, rows)
    maximum, capacity = await limits(session)
    result: dict[str, Any] = {
        "max_concurrency": maximum,
        "background_max": min(MAX_BACKGROUND, max(1, maximum - 1)),
        "slots": SLOTS,
        "purposes": {},
    }
    for p, cap in capacity.items():
        queue = [r for r, q, _ in classes if p == q and r.status == "queued"]
        times = [
            (r.started_at.replace(tzinfo=UTC) if r.started_at.tzinfo is None else r.started_at)
            for r in queue
        ]
        result["purposes"][p] = {
            "queued": len(queue),
            "ready": sum(
                r.available_at is None
                or (
                    r.available_at.replace(tzinfo=UTC)
                    if r.available_at.tzinfo is None
                    else r.available_at
                )
                <= at
                for r in queue
            ),
            "running": sum(q == p and r.status == "running" for r, q, _ in classes),
            "capacity": cap,
            "max_concurrency": min(CONCURRENCY[p], maximum),
            "oldest_wait_seconds": max(0, (at - min(times)).total_seconds()) if times else 0,
        }
    return result


async def supplemental_update_metrics(session: AsyncSession) -> dict[str, Any]:
    """Local P4 operating state; source health is separate from task success."""
    from sqlalchemy import func

    from investment_agent.storage.models.supplemental_updates import (
        SupplementalChangeReceiptRow,
        SupplementalRefreshStateRow,
        SupplementalSourceStateRow,
    )
    from investment_agent.supplemental_rules.disclosure_views import disclosure_metrics
    now = datetime.now(UTC)
    refresh = SupplementalRefreshStateRow
    source = SupplementalSourceStateRow
    root = SupplementalChangeReceiptRow
    queue = dict((await session.execute(select(PipelineRun.status, func.count()).where(
        PipelineRun.kind == "supplemental_update_process").group_by(PipelineRun.status))).tuples().all())
    coverage = dict((await session.execute(select(source.coverage_state, func.count()).where(
        source.owner_id == "local_owner", source.listing_id.is_not(None),
        source.context.is_not(None)).group_by(source.coverage_state))).tuples().all())
    oldest = await session.scalar(select(func.min(PipelineRun.started_at)).where(
        PipelineRun.kind == "supplemental_update_process", PipelineRun.status.in_(("queued", "running"))))
    maintenance = list(await session.scalars(select(source).where(
        source.owner_id == "local_owner", source.listing_id.is_(None),
    ).order_by(source.feed_id, source.scope_key).limit(101)))
    return {"queue_states": queue, "source_coverages": coverage,
            "source_stale_count": await session.scalar(select(func.count()).select_from(source).where(
                source.owner_id == "local_owner", source.listing_id.is_not(None), source.context.is_not(None),
                or_(source.last_success_at.is_(None), source.health_limit_seconds <= 0,
                    (func.julianday(now) - func.julianday(source.last_success_at)) * 86400 >= source.health_limit_seconds))),
            "declared_source_count": sum(coverage.values()),
            "overdue_scopes": await session.scalar(select(func.count()).select_from(refresh).where(
                refresh.owner_id == "local_owner", refresh.next_due_at <= now)),
            "blocked_scopes": await session.scalar(select(func.count()).select_from(refresh).where(
                refresh.owner_id == "local_owner", refresh.blocked_reason.is_not(None))),
            "receipt_high_watermark": await session.scalar(select(func.max(root.sequence)).where(root.owner_id == "local_owner")),
            "maintenance_feeds": [{"feed_id": row.feed_id, "scope_key": row.scope_key,
                "state": row.coverage_state, "last_attempt_at": row.last_attempt_at,
                "failure_code": row.failure_code,
                "next_due_at": row.payload.get("next_due_at"),
                "scan_truncated": row.payload.get("source_scan_truncated", row.payload.get("scan_truncated")),
                "blocked_reason": row.payload.get("blocked_reason"),
                "capacity_blocked": row.payload.get("capacity_blocked", False),
                "initial_complete": row.payload.get("initial_complete")}
                for row in maintenance[:100]],
            "maintenance_feeds_truncated": len(maintenance) > 100,
            "oldest_pending_at": oldest, "observed_at": now,
            "source_coverage_notice": "仅统计声明范围；未采集到不表示没有风险。",
            "online_polling": "bounded_cninfo_protocol_unvalidated",
            "disclosure_polling": await disclosure_metrics(session)}
