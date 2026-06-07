"""Typer entry point: `ia` command."""

import typer

from investment_agent.cli.commands import admin as admin_cmd
from investment_agent.cli.commands import auxiliary as auxiliary_cmd
from investment_agent.cli.commands import data as data_cmd
from investment_agent.cli.commands import strategy as strategy_cmd

app = typer.Typer(help="Investment Agent CLI")
app.add_typer(data_cmd.app, name="data", help="Market data ingestion commands")
app.add_typer(admin_cmd.app, name="admin", help="Admin and ops commands")
app.add_typer(strategy_cmd.app, name="strategy", help="Strategy management commands")
app.add_typer(
    auxiliary_cmd.app, name="auxiliary", help="Auxiliary listings management"
)


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
