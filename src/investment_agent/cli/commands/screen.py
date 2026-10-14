"""``ia screen``: enqueue (or run inline) the weekly template screen; show results."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import typer
from rich import print as rprint
from rich.markup import escape
from rich.table import Table

from investment_agent.cli.parsing import UNIVERSE_HELP, date_option, universe_code
from investment_agent.cli.runtime import bootstrap_settings, run_inline
from investment_agent.settings.loader import load_settings
from investment_agent.storage.database import run_with_engine

app = typer.Typer(help="Weekly template screen over a target pool (screen_run)")


@app.command("run")
def run(
    universe: str = typer.Option("CSI300", help=UNIVERSE_HELP),
    as_of: str | None = typer.Option(None, help="Screen as of (YYYY-MM-DD, default today)"),
    now: bool = typer.Option(
        False, "--now", help="Also execute the job in this process instead of the worker"
    ),
    rerun: bool = typer.Option(
        False, "--rerun", help="Queue a new run even if this universe/date already has one"
    ),
    hit_cap: int = typer.Option(20, min=1, max=500, help="Hits kept per template"),
    sync: bool = typer.Option(
        True, "--sync/--no-sync", help="Sync config/rule_templates into the rule store first"
    ),
) -> None:
    """Enqueue a screen_run for one universe and date."""

    from investment_agent.rules.screen_job import (
        ScreenRunPayload,
        enqueue_screen_run,
        screen_idempotency_key,
    )

    payload = ScreenRunPayload(
        universe_code=universe_code(universe),
        as_of=date_option(as_of),
        hit_cap=hit_cap,
        sync_templates=sync,
    )
    key = screen_idempotency_key(payload)
    if rerun:
        key = f"{key}:{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
    bootstrap = bootstrap_settings()

    async def submit() -> None:
        from investment_agent.candidates.lifecycle import CandidatePolicy
        from investment_agent.rules.screen_job import ScreenRunHandler
        from investment_agent.storage.database import session_scope

        async with session_scope() as session:
            queued, created = await enqueue_screen_run(
                session, payload, triggered_by="cli", key=key
            )
            run_id, status = queued.id, queued.status
        label = "[green]Queued[/green]" if created else "[yellow]Existing[/yellow]"
        rprint(f"{label} screen run #{run_id} (key={escape(key)}) status={status}")
        if not now:
            return
        if status != "queued":
            rprint(f"Run #{run_id} is {status}; nothing to run inline")
            return
        settings = await load_settings(base=bootstrap)
        finished = await run_inline(
            settings,
            run_id=run_id,
            handlers=[ScreenRunHandler(candidate_policy=CandidatePolicy.from_settings(settings))],
        )
        if finished.error_summary:
            rprint(f"Error: {escape(finished.error_summary)}")
        _print_summary(finished.metadata_json)

    run_with_engine(bootstrap.database_url, submit)


def _print_summary(metadata: dict[str, object]) -> None:
    templates = metadata.get("templates")
    if not isinstance(templates, dict):
        return
    table = Table("template", "hit", "truncated", "miss", "skipped", "excluded")
    for ref, item in templates.items():
        counts = item.get("counts", {}) if isinstance(item, dict) else {}
        table.add_row(
            ref,
            *(str(counts.get(key, 0)) for key in ("hit", "truncated", "miss", "skipped", "excluded")),
        )
    rprint(table)
    candidates = metadata.get("candidates")
    if isinstance(candidates, dict):
        rprint(
            "Candidates: "
            + ", ".join(
                f"{key}={len(value) if isinstance(value, list) else value}"
                for key, value in candidates.items()
            )
        )
    timing = metadata.get("timing_seconds")
    if timing:
        rprint(f"Timing (s): {timing}")


@app.command("show")
def show(
    run_id: int = typer.Argument(..., help="screen_run pipeline run id"),
    template: str | None = typer.Option(None, help="Only this template id"),
    reasons: bool = typer.Option(False, "--reasons", help="Print each hit's reasons as JSON"),
) -> None:
    """Hits of one screen run (rank, strength, reasons)."""

    settings = bootstrap_settings()

    async def load() -> None:
        from investment_agent.rules.screen_job import SCREEN_RUN, screen_results
        from investment_agent.storage.database import session_scope
        from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

        async with session_scope() as session:
            pipeline_run = await PipelineRunRepository(session).get(run_id)
            if pipeline_run is None or pipeline_run.kind != SCREEN_RUN:
                rprint(f"[red]No screen run #{run_id}[/red]")
                raise typer.Exit(code=1)
            rows = await screen_results(session, run_id, template_id=template)
            rprint(
                f"Screen run #{run_id} {pipeline_run.status} "
                f"{json.dumps(pipeline_run.request_json, sort_keys=True)}"
            )
            _print_summary(pipeline_run.metadata_json)
            table = Table("template", "rank", "ticker", "outcome", "strength", "conditions met")
            for row in rows:
                if not row.hit:
                    continue
                met = sum(1 for reason in row.reasons if reason.get("outcome") == "met")
                table.add_row(
                    f"{row.template_id}@{row.template_version}",
                    str(row.rank),
                    row.ticker,
                    row.outcome,
                    f"{row.strength:.3f}" if row.strength is not None else "",
                    str(met),
                )
            rprint(table)
            if reasons:
                for row in rows:
                    if row.hit:
                        typer.echo(
                            json.dumps(
                                {"template": row.template_id, "ticker": row.ticker,
                                 "rank": row.rank, "reasons": row.reasons},
                                ensure_ascii=False,
                                sort_keys=True,
                            )
                        )

    run_with_engine(settings.database_url, load)
