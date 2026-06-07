"""ia migrate — thin alembic wrapper."""

import subprocess

import typer
from rich import print as rprint

app = typer.Typer(help="Database migration commands (alembic wrapper)")


def _run(args: list[str]) -> None:
    rprint(f"[bold]Running:[/bold] alembic {' '.join(args)}")
    result = subprocess.run(["alembic", *args], check=False)  # noqa: S603, S607
    if result.returncode != 0:
        raise typer.Exit(result.returncode)


@app.command("up")
def upgrade(revision: str = "head") -> None:
    """alembic upgrade <revision> (default head)."""
    _run(["upgrade", revision])


@app.command("down")
def downgrade(revision: str = "-1") -> None:
    """alembic downgrade <revision> (default -1)."""
    _run(["downgrade", revision])


@app.command("current")
def current() -> None:
    """alembic current."""
    _run(["current"])


@app.command("history")
def history() -> None:
    """alembic history."""
    _run(["history"])
