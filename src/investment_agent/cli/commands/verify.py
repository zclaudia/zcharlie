"""``ia verify``: iFinD on-demand verification queue and budget (P1-A13)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from datetime import UTC, date, datetime, timedelta

import typer
from rich import print as rprint
from rich.markup import escape
from rich.table import Table

from investment_agent.config import Settings

app = typer.Typer(help="iFinD on-demand verification queue and budget ledger")

_TRIGGERS = ("promote", "conflict", "missing", "exit_review", "calibration")


def _period(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint="--period") from exc
    if (parsed.month, parsed.day) not in {(3, 31), (6, 30), (9, 30), (12, 31)}:
        raise typer.BadParameter("must be a quarter end", param_hint="--period")
    return parsed


@app.command("status")
def status() -> None:
    """Budget used / remaining in the rolling window and the queue by status and trigger."""

    settings = Settings()  # type: ignore[call-arg]

    async def load() -> None:
        from sqlalchemy import func, select

        from investment_agent.storage.database import init_engine, session_scope
        from investment_agent.storage.models.verification import IfindCallLedger
        from investment_agent.verification.budget import as_utc, budget_status
        from investment_agent.verification.queue import count_due, queue_summary

        init_engine(settings.database_url)
        now = datetime.now(UTC)
        window = timedelta(hours=settings.ifind_budget_window_hours)
        async with session_scope() as session:
            budget = await budget_status(
                session, now=now, cap=settings.ifind_budget_per_5h, window=window
            )
            by_source = (
                await session.execute(
                    select(IfindCallLedger.source, func.count())
                    .where(IfindCallLedger.at > now - window)
                    .group_by(IfindCallLedger.source)
                )
            ).all()
            summary = await queue_summary(session)
            due = await count_due(session, now=now)
        rprint(
            f"iFinD budget: used {budget.used}/{budget.cap} in the last "
            f"{settings.ifind_budget_window_hours:g} h, remaining {budget.remaining}"
        )
        if by_source:
            rprint("  by source: " + ", ".join(f"{src}={count}" for src, count in by_source))
        if budget.remaining == 0:
            rprint(f"  next call fits at {as_utc(budget.available_at(1)).isoformat()}")
        table = Table("status", *_TRIGGERS, "total")
        for state in ("pending", "deferred", "running", "done", "failed", "superseded"):
            row = summary.get(state, {})
            table.add_row(state, *(str(row.get(t, 0)) for t in _TRIGGERS), str(sum(row.values())))
        rprint(table)
        rprint(f"Due now: {due}")

    asyncio.run(load())


@app.command("request")
def request(
    ticker: str = typer.Option(..., help="A-share ticker, e.g. 600519.SH"),
    trigger: str = typer.Option("promote", help=f"One of {', '.join(_TRIGGERS)}"),
    fields: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--fields",
        help="Factor-layer field (repeatable / comma separated); default all key fields",
    ),
    period: str | None = typer.Option(
        None, help="Report period YYYY-MM-DD (default: latest annual when the job runs)"
    ),
) -> None:
    """Queue a verification request (merged into an open one for the same stock/trigger/period)."""

    if trigger not in _TRIGGERS:
        raise typer.BadParameter(", ".join(_TRIGGERS), param_hint="--trigger")
    names = sorted({part.strip() for item in fields or [] for part in item.split(",") if part.strip()})
    report_period = _period(period)
    settings = Settings()  # type: ignore[call-arg]

    async def submit() -> None:
        from investment_agent.storage.database import init_engine, session_scope
        from investment_agent.storage.repositories.listing import ListingRepository
        from investment_agent.verification import enqueue_verification

        init_engine(settings.database_url)
        async with session_scope() as session:
            listing = await ListingRepository(session).get_a_share_listing(ticker.strip().upper())
            if listing is None:
                rprint(f"[red]No listing for {escape(ticker)}[/red]")
                raise typer.Exit(code=1)
            created = await enqueue_verification(
                listing_id=listing.id,
                ticker=ticker.strip().upper(),
                trigger=trigger,
                fields=names or None,
                report_period=report_period,
                detail={"requested_by": "cli"},
                session=session,
            )
        label = "[green]Queued[/green]" if created else "[yellow]Merged[/yellow]"
        rprint(
            f"{label} {trigger} verification of {escape(ticker)} "
            f"(fields={names or 'all key fields'}, period={report_period or 'latest annual'})"
        )

    asyncio.run(submit())


@app.command("run")
def run(
    now: bool = typer.Option(
        False, "--now", help="Execute in this process instead of leaving it to the worker"
    ),
    max_requests: int | None = typer.Option(None, min=1, max=500, help="Requests to pick"),
) -> None:
    """Enqueue one ifind_verify run (calls iFinD within the budget)."""

    settings = Settings()  # type: ignore[call-arg]

    async def submit() -> None:
        from investment_agent.research.worker import ResearchWorker
        from investment_agent.storage.database import init_engine, session_scope
        from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
        from investment_agent.verification.job import IfindVerifyHandler, enqueue_verify_run

        init_engine(settings.database_url)
        stamp = datetime.now(UTC).strftime("cli-%Y%m%dT%H%M%S%f")
        async with session_scope() as session:
            queued, _ = await enqueue_verify_run(
                session, slot=stamp, triggered_by="cli", max_requests=max_requests
            )
            run_id = queued.id
        rprint(f"[green]Queued[/green] ifind_verify run #{run_id}")
        if not now:
            return
        worker = ResearchWorker(
            settings=settings,
            worker_id=f"cli:{socket.gethostname()}:{os.getpid()}",
            handlers=[IfindVerifyHandler(settings)],
        )
        await worker.run_once(run_id=run_id)
        async with session_scope() as session:
            finished = await PipelineRunRepository(session).get(run_id)
            assert finished is not None
            rprint(f"Run #{run_id} finished: status={finished.status}")
            if finished.error_summary:
                rprint(f"Summary: {escape(finished.error_summary)}")
            typer.echo(json.dumps(finished.metadata_json, ensure_ascii=False, indent=1, default=str))

    asyncio.run(submit())
