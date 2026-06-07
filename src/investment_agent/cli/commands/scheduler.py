"""ia scheduler start."""

import asyncio

import typer

from investment_agent.scheduler.runner import run_forever

app = typer.Typer(help="Scheduler commands")


@app.command("start")
def start(app_config_path: str = "config/app.yaml") -> None:
    """Run APScheduler in foreground (cron from config/app.yaml)."""
    asyncio.run(run_forever(app_config_path))
