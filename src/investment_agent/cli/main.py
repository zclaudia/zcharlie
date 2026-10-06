"""Typer entry point: `ia` command."""

import typer

from investment_agent.cli.commands import admin as admin_cmd
from investment_agent.cli.commands import api as api_cmd
from investment_agent.cli.commands import migrate as migrate_cmd
from investment_agent.cli.commands import research as research_cmd
from investment_agent.cli.commands import scheduler as scheduler_cmd
from investment_agent.cli.commands import snapshot as snapshot_cmd
from investment_agent.cli.commands import worker as worker_cmd

app = typer.Typer(help="Investment Agent CLI")
app.add_typer(admin_cmd.app, name="admin", help="Admin and ops commands")
app.add_typer(research_cmd.app, name="research", help="Evidence-first research commands")
app.add_typer(api_cmd.app, name="api", help="API server commands")
app.add_typer(scheduler_cmd.app, name="scheduler", help="Scheduler commands")
app.add_typer(migrate_cmd.app, name="migrate", help="Database migration commands")
app.add_typer(worker_cmd.app, name="worker", help="Persistent worker commands")
app.add_typer(snapshot_cmd.app, name="snapshot", help="PIT snapshot collection commands")


@app.callback()
def _bootstrap() -> None:
    try:
        from investment_agent.config import Settings
        from investment_agent.logging import configure_logging
        from investment_agent.storage.database import init_engine

        settings = Settings()  # type: ignore[call-arg]
        configure_logging(settings.log_level)
        init_engine(settings.database_url)
    except Exception:
        # Allow --help invocations without full env configuration
        pass


if __name__ == "__main__":
    app()
