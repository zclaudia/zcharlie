"""Offline administration of the checked HK/US financial material."""

import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import typer
from sqlalchemy.exc import OperationalError

from investment_agent.cli.runtime import bootstrap_settings
from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
from investment_agent.materials.annual_validation import verify_annual_package
from investment_agent.materials.bootstrap import import_pilot_materials, initialize_pilot
from investment_agent.materials.handlers import default_adapters
from investment_agent.materials.publication_prepare import prepare_material_publication_payload
from investment_agent.materials.replay import ReplayError, replay_acquisition
from investment_agent.storage.database import run_with_engine, session_scope

app = typer.Typer(help="Import, replay, verify and prepare controlled HK/US material offline")


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
        identity
        for identity in identities
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
                    session,
                    identities=selected,
                    company_ids=setup.company_ids,
                    source_dir=source_dir,
                    data_dir=settings.data_dir,
                )
                output.update(
                    datasets_created=sum(item.created for item in report.saved),
                    datasets_reused=sum(not item.created for item in report.saved),
                    acquisition_ids=list(report.acquisition_ids),
                    problems=list(report.problems),
                    skipped=list(report.skipped),
                    informational=list(report.informational),
                )
            return output

    try:
        output = run_with_engine(settings.database_url, main)
    except OperationalError as exc:
        typer.echo(
            "Database is not ready; run `ia migrate up` with the same DATABASE_URL.", err=True
        )
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


@app.command("replay")
def replay_materials(
    acquisition_id: int = typer.Option(..., min=1, help="Saved acquisition row ID"),
) -> None:
    """Reparse one archived acquisition using its saved manifest and response bodies only."""

    try:
        identities = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    settings = bootstrap_settings()

    async def main() -> dict[str, Any]:
        async with session_scope() as session:
            report = await replay_acquisition(
                session,
                acquisition_id=acquisition_id,
                data_dir=settings.data_dir,
                identities=identities,
                adapters=default_adapters(),
            )
            return {
                "acquisition_id": report.acquisition_id,
                "datasets_created": sum(item.created for item in report.saved),
                "datasets_reused": sum(not item.created for item in report.saved),
                "problems": list(report.problems),
                "skipped": list(report.skipped),
                "informational": list(report.informational),
            }

    try:
        output = run_with_engine(settings.database_url, main)
    except OperationalError as exc:
        typer.echo(
            "Database is not ready; run `ia migrate up` with the same DATABASE_URL.", err=True
        )
        raise typer.Exit(1) from exc
    except (ReplayError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    if output.get("problems") or output.get("skipped"):
        raise typer.Exit(2)


@app.command("prepare-publication")
def prepare_publication(
    listing_id: int = typer.Option(..., min=1, help="Controlled listing ID"),
    as_of: str = typer.Option(..., help="Evidence visibility cutoff, ISO timestamp with timezone"),
    dataset_id: list[int] = typer.Option(  # noqa: B008
        [], min=1, help="Explicit current dataset selection; repeat for multiple versions"
    ),
) -> None:
    """Compute and save exact fact references for numeric publication, without network IO.

    Exit 2 means some metrics have gaps. Successful calculations are committed
    and returned individually; repeating the same operation reuses their refs.
    """
    try:
        cutoff = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--as-of") from exc
    settings = bootstrap_settings()

    async def main() -> dict[str, Any]:
        async with session_scope() as session:
            return await prepare_material_publication_payload(
                session, listing_id=listing_id, as_of=cutoff, dataset_ids=dataset_id or None
            )

    try:
        output = run_with_engine(settings.database_url, main)
    except OperationalError as exc:
        typer.echo(
            "Database is not ready; run `ia migrate up` with the same DATABASE_URL.", err=True
        )
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    if output["gaps"] or not output["metrics"]:
        raise typer.Exit(2)


@app.command("verify-annual")
def verify_annual(
    manifest: Path = typer.Option(..., exists=True, dir_okay=False),  # noqa: B008
    source_root: Path = typer.Option(..., exists=True, file_okay=False),  # noqa: B008
    as_of: str | None = typer.Option(None, help="Optional ISO date; must match manifest cutoff"),
    company_key: list[str] = typer.Option(  # noqa: B008
        [], help="Explicit subset; subset success never marks the eight-company package complete"
    ),
) -> None:
    """Verify original bytes and reviewed annual inputs offline; exit 2 for incomplete coverage."""
    try:
        cutoff = date.fromisoformat(as_of) if as_of else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--as-of") from exc
    output = verify_annual_package(
        manifest, source_root, as_of_date=cutoff, identities=company_key or None
    )
    typer.echo(json.dumps(output, ensure_ascii=False, indent=2))
    if not output["complete"]:
        raise typer.Exit(2)


@app.command("import-reviewed")
def import_reviewed_materials(
    manifest: Path = typer.Option(..., exists=True, dir_okay=False),  # noqa: B008
    source_root: Path = typer.Option(..., exists=True, file_okay=False),  # noqa: B008
    company_key: list[str] = typer.Option(  # noqa: B008
        [],
        help="Import selected controlled companies; the complete source package is still verified",
    ),
) -> None:
    """Verify saved originals and import reviewed annual facts without upstream requests."""
    from investment_agent.materials.reviewed_import import import_reviewed_annual

    identities = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
    unknown = sorted(set(company_key) - {item.company_key for item in identities})
    if unknown:
        raise typer.BadParameter(f"unknown company keys: {unknown}", param_hint="--company-key")
    selected = tuple(
        item for item in identities if not company_key or item.company_key in company_key
    )
    settings = bootstrap_settings()

    async def main() -> dict[str, Any]:
        async with session_scope() as session:
            setup = await initialize_pilot(session, identities)
            report = await import_reviewed_annual(
                session,
                manifest_path=manifest,
                source_root=source_root,
                data_dir=settings.data_dir,
                identities=selected,
                company_ids=setup.company_ids,
            )
            return {
                "listings": {
                    f"{market}/{ticker}": item.id
                    for (market, ticker), item in setup.listings.items()
                },
                "company_ids": setup.company_ids,
                "bindings_created": setup.bindings_created,
                "scope": "all_controlled" if len(selected) == len(identities) else "subset",
                "datasets_created": sum(item.created for item in report.saved),
                "datasets_reused": sum(not item.created for item in report.saved),
                **asdict(report),
            }

    try:
        output = run_with_engine(settings.database_url, main)
    except OperationalError as exc:
        typer.echo(
            "Database is not ready; run `ia migrate up` with the same DATABASE_URL.", err=True
        )
        raise typer.Exit(1) from exc
    except ValueError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    if output.get("problems") or any(
        company.get("status") != "complete" or not company.get("imported")
        for company in output.get("companies", [])
    ):
        raise typer.Exit(2)
