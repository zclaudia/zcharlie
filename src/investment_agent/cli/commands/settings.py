"""``ia settings``: runtime settings stored in the DB (settings-plan §4.7).

For hosts without the Web UI and for operations inside Docker. Secret values
are never printed; ``set KEY -`` reads a secret from a hidden prompt so it
stays out of the shell history.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich import print as rprint
from rich.markup import escape
from rich.table import Table

from investment_agent.cli.runtime import bootstrap_settings
from investment_agent.settings.crypto import EncryptionKeyError
from investment_agent.settings.registry import SPECS_BY_KEY, defaults
from investment_agent.settings.store import SettingsValidationError
from investment_agent.storage.database import run_with_engine

app = typer.Typer(help="Runtime settings stored in the database")

_DEFAULT_APP_YAML = Path("config/app.yaml")
_NULL_WORDS = frozenset({"", "null", "none", "off"})


def _spec_or_exit(key: str) -> None:
    if key not in SPECS_BY_KEY:
        rprint(f"[red]Unknown setting {escape(key)}[/red]; see `ia settings list`")
        raise typer.Exit(code=2)


def parse_value(key: str, raw: str) -> Any:
    """CLI text -> setting value; pydantic coerces numbers on validation."""

    default = defaults()[key]
    if isinstance(default, list):
        if raw.strip().startswith("["):
            return json.loads(raw)
        return [item.strip() for item in raw.split(",") if item.strip()]
    if key.endswith("cron") and raw.strip().lower() in _NULL_WORDS:
        return None
    return raw


def _display(item: dict[str, Any]) -> str:
    if item["secret"]:
        if item.get("decrypt_error"):
            return "[red]unreadable[/red]"
        if not item["configured"]:
            return "未设置"
        return f"已设置 ···{item['hint']}" if item["hint"] else "已设置"
    value = item["value"]
    return "—" if value is None else escape(json.dumps(value, ensure_ascii=False))


@app.command("list")
def list_settings() -> None:
    """Every runtime setting with its current value (secrets masked)."""

    settings = bootstrap_settings()

    async def run() -> list[dict[str, Any]]:
        from investment_agent.settings.loader import store_for
        from investment_agent.storage.database import session_scope

        async with session_scope() as session:
            return await store_for(session, settings).describe()

    items = run_with_engine(settings.database_url, run)
    table = Table()
    table.add_column("group")
    table.add_column("key", no_wrap=True)
    table.add_column("value")
    table.add_column("default")
    table.add_column("apply")
    for item in items:
        table.add_row(
            item["group"],
            item["key"],
            _display(item),
            "yes" if item["is_default"] else "",
            item["apply"],
        )
    rprint(table)


@app.command("get")
def get_setting(key: str = typer.Argument(..., help="Setting key, e.g. candidate_expiry_days")) -> None:
    """Print one setting (a secret prints whether it is set and its hint only)."""

    _spec_or_exit(key)
    settings = bootstrap_settings()

    async def run() -> dict[str, Any]:
        from investment_agent.settings.loader import store_for
        from investment_agent.storage.database import session_scope

        async with session_scope() as session:
            items = await store_for(session, settings).describe()
        return next(item for item in items if item["key"] == key)

    item = run_with_engine(settings.database_url, run)
    if item["secret"]:
        rprint(_display(item))
        if item.get("decrypt_error"):
            rprint(f"[red]{escape(item['decrypt_error'])}[/red]")
    else:
        typer.echo(json.dumps(item["value"], ensure_ascii=False))


@app.command("set")
def set_setting(
    key: str = typer.Argument(..., help="Setting key"),
    value: str = typer.Argument(
        ...,
        help="New value. Lists: comma-separated or JSON; crons: 'off' disables; "
        "secrets: '-' reads it from a hidden prompt, '' clears it",
    ),
    note: str | None = typer.Option(None, help="Free-text note kept in the history"),
) -> None:
    """Validate and store one setting."""

    _spec_or_exit(key)
    spec = SPECS_BY_KEY[key]
    if spec.secret:
        parsed: Any = typer.prompt(f"{key}", hide_input=True, default="") if value == "-" else value
    else:
        try:
            parsed = parse_value(key, value)
        except json.JSONDecodeError as exc:
            rprint(f"[red]Invalid JSON for {escape(key)}: {escape(str(exc))}[/red]")
            raise typer.Exit(code=2) from exc
    settings = bootstrap_settings()

    async def run() -> list[str]:
        from investment_agent.settings.loader import store_for
        from investment_agent.storage.database import session_scope

        async with session_scope() as session:
            return await store_for(session, settings).apply({key: parsed}, actor="cli", note=note)

    try:
        changed = run_with_engine(settings.database_url, run)
    except SettingsValidationError as exc:
        for issue_key, messages in exc.issues.items():
            rprint(f"[red]{escape(issue_key)}: {escape('; '.join(messages))}[/red]")
        raise typer.Exit(code=1) from exc
    except EncryptionKeyError as exc:
        rprint(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc
    if changed:
        rprint(f"Updated {escape(key)} (applies: {spec.apply})")
    else:
        rprint(f"{escape(key)} unchanged")


@app.command("import")
def import_settings(
    force: bool = typer.Option(
        False, "--force", help="Re-import even if already imported (overwrites stored values)"
    ),
    app_yaml: Path = typer.Option(_DEFAULT_APP_YAML, help="app.yaml with the schedules"),  # noqa: B008
) -> None:
    """Import runtime values explicitly set in env / .env and the app.yaml schedules."""

    settings = bootstrap_settings()

    async def run() -> list[str] | None:
        from investment_agent.settings.loader import ensure_imported
        from investment_agent.storage.database import session_scope

        async with session_scope() as session:
            return await ensure_imported(session, settings, app_yaml, force=force)

    imported = run_with_engine(settings.database_url, run)
    if imported is None:
        rprint("Already imported; use --force to import env / app.yaml values again")
    elif imported:
        rprint(f"Imported {len(imported)} setting(s): {escape(', '.join(imported))}")
    else:
        rprint("Imported: every value already matched")


@app.command("history")
def history(limit: int = typer.Option(20, min=1, max=500, help="Most recent N changes")) -> None:
    """Recent setting changes (secrets shown masked)."""

    settings = bootstrap_settings()

    async def run() -> list[tuple[str, str, str, str, str, str]]:
        from investment_agent.settings.loader import store_for
        from investment_agent.storage.database import session_scope

        async with session_scope() as session:
            rows = await store_for(session, settings).history(limit)
            return [
                (
                    row.changed_at.isoformat(timespec="seconds"),
                    row.actor,
                    row.key,
                    json.dumps(row.old_value_json, ensure_ascii=False),
                    json.dumps(row.new_value_json, ensure_ascii=False),
                    row.note or "",
                )
                for row in rows
            ]

    rows = run_with_engine(settings.database_url, run)
    table = Table("changed at", "actor", "key", "old", "new", "note")
    for row in rows:
        table.add_row(*(escape(cell) for cell in row))
    rprint(table)
