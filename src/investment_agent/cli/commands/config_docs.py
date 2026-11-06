"""``ia config-docs``: DB-managed config documents (config-docs-plan §4.7).

The headless equivalent of the ``/app/data-sources`` and
``/app/industry-groups`` pages: per kind revision / import / drift state, the
effective document as canonical YAML, the one-time baseline import plus its
explicit ``--force`` re-import, and the full-document change history. Editing
itself stays in the UI / API — there is no ``set`` here, and no secrets are
involved, so nothing is masked or prompted for.
"""

from __future__ import annotations

from typing import Any

import typer
import yaml
from rich import print as rprint
from rich.markup import escape
from rich.table import Table

from investment_agent.cli.runtime import bootstrap_settings
from investment_agent.config_docs import registry
from investment_agent.config_docs.registry import ConfigDocSpec
from investment_agent.config_docs.store import ConfigDocStore
from investment_agent.storage.database import run_with_engine

app = typer.Typer(
    help="DB-managed config documents (field sources, source policy, industry groups)"
)

_DEFAULT_FORCE_NOTE = "cli baseline import"


def _spec_or_exit(kind: str) -> ConfigDocSpec:
    try:
        return registry.spec_for(kind)
    except KeyError:
        rprint(f"[red]Unknown config document {escape(kind)}[/red]; see `ia config-docs list`")
        raise typer.Exit(code=2) from None


def _yes(value: bool) -> str:
    return "yes" if value else ""


def _when(value: Any) -> str:
    return "—" if value is None else str(value)


@app.command("list")
def list_config_docs() -> None:
    """Every kind with its revision, import state, baseline drift and baseline ref."""

    settings = bootstrap_settings()

    async def run() -> list[tuple[str, str, int, str, str, str | None]]:
        from investment_agent.storage.database import session_scope

        async with session_scope() as session:
            store = ConfigDocStore(session)
            status = await store.baseline_status()
            rows = []
            for spec in registry.KINDS:
                entry = status[spec.kind]
                rows.append(
                    (
                        spec.kind,
                        spec.title,
                        await store.revision(spec.kind),
                        _yes(bool(entry["imported"])),
                        _yes(bool(entry["drifted"])),
                        entry["baseline"],
                    )
                )
            return rows

    rows = run_with_engine(settings.database_url, run)
    table = Table()
    table.add_column("kind", no_wrap=True)
    table.add_column("title")
    table.add_column("revision", justify="right")
    table.add_column("imported")
    table.add_column("drifted")
    table.add_column("baseline", no_wrap=True)
    for row in rows:
        table.add_row(row[0], row[1], str(row[2]), row[3], row[4], _when(row[5]))
    rprint(table)


@app.command("show")
def show_config_doc(
    kind: str = typer.Argument(..., help="Config document kind, e.g. field_sources"),
    baseline: bool = typer.Option(
        False, "--baseline", help="Print the baseline file's document instead of the stored one"
    ),
) -> None:
    """Print one effective document as canonical YAML under a ``#`` meta header line."""

    spec = _spec_or_exit(kind)
    settings = bootstrap_settings()

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        from investment_agent.storage.database import session_scope

        async with session_scope() as session:
            store = ConfigDocStore(session)
            value, row = await store.get(spec.kind)
            drifted = bool((await store.baseline_status())[spec.kind]["drifted"])
            meta = {
                "revision": await store.revision(spec.kind),
                "updated_at": row.updated_at.isoformat(timespec="seconds") if row else None,
                "updated_by": row.updated_by if row else None,
                "imported_at": (
                    row.imported_at.isoformat(timespec="seconds")
                    if row and row.imported_at
                    else None
                ),
                "drifted": drifted,
            }
            return value, meta

    value, meta = run_with_engine(settings.database_url, run)
    if baseline:
        try:
            value = registry.baseline_document(spec)
        except (OSError, ValueError) as exc:
            rprint(
                f"[red]The baseline file of {escape(spec.kind)} is unreadable: {escape(str(exc))}[/red]"
            )
            raise typer.Exit(code=1) from exc
    header = (
        f"# {spec.kind} revision={meta['revision']} updated_at={_when(meta['updated_at'])} "
        f"updated_by={_when(meta['updated_by'])} imported_at={_when(meta['imported_at'])} "
        f"drifted={'yes' if meta['drifted'] else 'no'}"
    )
    typer.echo(header)
    typer.echo(yaml.safe_dump(value, sort_keys=True, allow_unicode=True, default_flow_style=False))


@app.command("import")
def import_config_docs(
    force: bool = typer.Option(
        False, "--force", help="Re-import every kind's baseline, overwriting the stored documents"
    ),
    note: str = typer.Option(
        _DEFAULT_FORCE_NOTE, "--note", help="Change note recorded by --force re-imports"
    ),
) -> None:
    """Import every kind's baseline file once; --force re-imports them explicitly."""

    settings = bootstrap_settings()

    async def run() -> tuple[list[str], list[tuple[str, str]]]:
        from investment_agent.storage.database import session_scope

        async with session_scope() as session:
            store = ConfigDocStore(session)
            if not force:
                return await store.ensure_imported(), []
            written: list[str] = []
            skipped: list[tuple[str, str]] = []
            for spec in registry.KINDS:
                try:
                    if await store.import_baseline(spec.kind, actor="cli", note=note, force=True):
                        written.append(spec.kind)
                except (ValueError, OSError) as exc:
                    # Validation errors, an unreadable or a non-mapping baseline
                    # skip the kind (§4.3: only a file that parses is imported).
                    skipped.append((spec.kind, str(exc)))
            return written, skipped

    imported, skipped = run_with_engine(settings.database_url, run)
    for kind, error in skipped:
        rprint(f"[yellow]Skipped {escape(kind)}[/yellow]: {escape(error)}")
    if force:
        # The settings import ergonomics: name what moved, stay quiet on no-ops.
        if imported:
            rprint(f"Re-imported {len(imported)} document(s): {escape(', '.join(imported))}")
        elif not skipped:
            rprint("Re-imported: every stored document already matched its baseline")
    elif imported:
        rprint(f"Imported {len(imported)} document(s): {escape(', '.join(imported))}")
    else:
        rprint("Already imported; use --force to re-import every kind's baseline")


@app.command("history")
def history(
    kind: str = typer.Argument(..., help="Config document kind"),
    limit: int = typer.Option(20, min=1, max=500, help="Most recent N changes"),
) -> None:
    """One kind's full-document changes, newest first (actor and mandatory note)."""

    spec = _spec_or_exit(kind)
    settings = bootstrap_settings()

    async def run() -> list[tuple[str, str, str]]:
        from investment_agent.storage.database import session_scope

        async with session_scope() as session:
            rows = await ConfigDocStore(session).history(spec.kind, limit)
            return [
                (
                    row.changed_at.isoformat(timespec="seconds"),
                    row.actor,
                    row.note or "",
                )
                for row in rows
            ]

    rows = run_with_engine(settings.database_url, run)
    table = Table("changed at", "actor", "note")
    for row in rows:
        table.add_row(*(escape(cell) for cell in row))
    rprint(table)
