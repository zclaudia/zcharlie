"""Persistent queue worker commands."""

import asyncio
import os
import socket

import typer

from investment_agent.config import Settings
from investment_agent.logging import configure_logging
from investment_agent.research.worker import run_worker
from investment_agent.storage.database import init_engine

app = typer.Typer(help="Persistent queue worker commands")


def parse_kinds(raw: str | None) -> list[str] | None:
    """``"a, b"`` -> ``["a", "b"]``; empty / missing means every registered kind."""

    if raw is None:
        return None
    kinds = [item.strip() for item in raw.split(",") if item.strip()]
    return kinds or None


@app.command("start")
def start(
    once: bool = typer.Option(False, help="Process at most one available job and exit"),
    poll_interval: float = typer.Option(1.0, min=0.05, help="Idle poll interval in seconds"),
    stale_after: float = typer.Option(
        300.0,
        min=1.0,
        help="Requeue running jobs after this heartbeat age in seconds",
    ),
    worker_id: str | None = typer.Option(None, help="Stable worker identity for audits"),
    kinds: str | None = typer.Option(
        None,
        help=(
            "Comma-separated job kinds to serve (e.g. ifind_research,market_snapshot); "
            "default: all registered kinds"
        ),
        show_default=False,
    ),
) -> None:
    """Claim queued jobs (research and snapshot collection) and execute them."""

    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    init_engine(settings.database_url)
    identity = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    if len(identity) > 128:
        raise typer.BadParameter("must be at most 128 characters", param_hint="--worker-id")
    try:
        asyncio.run(
            run_worker(
                settings=settings,
                worker_id=identity,
                poll_interval_seconds=poll_interval,
                stale_after_seconds=stale_after,
                once=once,
                kinds=parse_kinds(kinds),
            )
        )
    except ValueError as exc:
        if "no handler registered" in str(exc):
            raise typer.BadParameter(str(exc), param_hint="--kinds") from exc
        raise
