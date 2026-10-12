"""Typer entry point: `ia` command."""

import typer

from investment_agent.cli.commands import admin as admin_cmd
from investment_agent.cli.commands import api as api_cmd
from investment_agent.cli.commands import materials as materials_cmd
from investment_agent.cli.commands import mcp as mcp_cmd
from investment_agent.cli.commands import migrate as migrate_cmd
from investment_agent.cli.commands import research as research_cmd
from investment_agent.cli.commands import rules as rules_cmd
from investment_agent.cli.commands import scheduler as scheduler_cmd
from investment_agent.cli.commands import screen as screen_cmd
from investment_agent.cli.commands import settings as settings_cmd
from investment_agent.cli.commands import snapshot as snapshot_cmd
from investment_agent.cli.commands import verify as verify_cmd
from investment_agent.cli.commands import worker as worker_cmd

app = typer.Typer(help="Investment Agent CLI")
app.add_typer(admin_cmd.app, name="admin", help="Admin and ops commands")
app.add_typer(materials_cmd.app, name="materials", help="Controlled HK/US offline material")
app.add_typer(research_cmd.app, name="research", help="Evidence-first research commands")
app.add_typer(api_cmd.app, name="api", help="API server commands")
app.add_typer(scheduler_cmd.app, name="scheduler", help="Scheduler commands")
app.add_typer(migrate_cmd.app, name="migrate", help="Database migration commands")
app.add_typer(worker_cmd.app, name="worker", help="Persistent worker commands")
app.add_typer(snapshot_cmd.app, name="snapshot", help="PIT snapshot collection commands")
app.add_typer(rules_cmd.app, name="rules", help="Rule templates and instances")
app.add_typer(screen_cmd.app, name="screen", help="Weekly template screen")
app.add_typer(verify_cmd.app, name="verify", help="iFinD on-demand verification (P1-A13)")
app.add_typer(settings_cmd.app, name="settings", help="Runtime settings stored in the database")
app.add_typer(mcp_cmd.app, name="mcp", help="Read-only MCP server (stdio)")


@app.callback()
def _bootstrap() -> None:
    try:
        from investment_agent.config import Settings
        from investment_agent.logging import configure_logging

        settings = Settings()  # type: ignore[call-arg]
        configure_logging(settings.log_level)
    except Exception:
        # Allow --help invocations without full env configuration
        pass


if __name__ == "__main__":
    app()
