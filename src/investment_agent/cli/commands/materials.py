"""Offline administration of the checked HK/US financial material."""

import json
from pathlib import Path
from typing import Any

import typer
from sqlalchemy.exc import OperationalError

from investment_agent.cli.runtime import bootstrap_settings
from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
from investment_agent.materials.bootstrap import import_pilot_materials, initialize_pilot
from investment_agent.storage.database import run_with_engine, session_scope

app = typer.Typer(help="Initialize and import controlled HK/US material offline")


def _run(pilot_list: Path, source_dir: Path | None, company_keys: list[str]) -> None:
    try:
        identities = load_pilot_identities(pilot_list)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    unknown = sorted(set(company_keys) - {identity.company_key for identity in identities})
    if unknown:
        raise typer.BadParameter(f"unknown company keys: {unknown}", param_hint="--company-key")
    selected = tuple(
        identity for identity in identities
        if not company_keys or identity.company_key in company_keys
    )
    settings = bootstrap_settings()

    async def main() -> dict[str, Any]:
        async with session_scope() as session:
            setup = await initialize_pilot(session, identities)
            output: dict[str, Any] = {
                "listings": {
                    f"{market}/{ticker}": listing.id
                    for (market, ticker), listing in setup.listings.items()
                },
                "company_ids": setup.company_ids,
                "bindings_created": setup.bindings_created,
            }
            if source_dir is not None:
                report = await import_pilot_materials(
                    session, identities=selected, company_ids=setup.company_ids,
                    source_dir=source_dir, data_dir=settings.data_dir,
                )
                output.update(
                    datasets_created=sum(item.created for item in report.saved),
                    datasets_reused=sum(not item.created for item in report.saved),
                    problems=list(report.problems),
                    skipped=list(report.skipped),
                    informational=list(report.informational),
                )
            return output

    try:
        output = run_with_engine(settings.database_url, main)
    except OperationalError as exc:
        typer.echo("Database is not ready; run `ia migrate up` with the same DATABASE_URL.", err=True)
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    if output.get("problems") or output.get("skipped"):
        # Valid sources have been committed; incomplete sources remain explicit.
        raise typer.Exit(2)


@app.command("init")
def initialize(
    pilot_list: Path = typer.Option(  # noqa: B008
        DEFAULT_PILOT_LIST_PATH, exists=True, dir_okay=False, help="Checked identity YAML"
    ),
) -> None:
    """Register all checked identities and source bindings (safe to repeat)."""
    _run(pilot_list, None, [])


@app.command("import")
def import_materials(
    source_dir: Path = typer.Option(  # noqa: B008
        ..., exists=True, file_okay=False, help="Local evidence root, usually evidence/<run>/raw"
    ),
    pilot_list: Path = typer.Option(  # noqa: B008
        DEFAULT_PILOT_LIST_PATH, exists=True, dir_okay=False, help="Checked identity YAML"
    ),
    company_key: list[str] = typer.Option(  # noqa: B008
        [], help="Only import this company key; repeat to select several (default: all)"
    ),
) -> None:
    """Initialize the pilot and import saved responses with their .meta.json sidecars.

    Exit 0: clean import. Exit 2: usable sources committed, with reported gaps.
    Missing sidecars retain unknown original fetch time. No network calls.
    """
    _run(pilot_list, source_dir, company_key)
