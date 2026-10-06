"""``ia rules``: validate rule files, sync templates into the rule store, inspect."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich import print as rprint
from rich.markup import escape
from rich.table import Table

from investment_agent.config import Settings
from investment_agent.rules.schema import RuleTemplate, to_yaml
from investment_agent.rules.templates import (
    DEFAULT_TEMPLATES_DIR,
    load_template_file,
    load_templates,
    read_rule_file,
)
from investment_agent.rules.validation import RuleValidationError, parse_instance

app = typer.Typer(help="Rule templates and instances (validate, sync, list, show)")

_DIR_HELP = "Template directory (default: config/rule_templates)"


def _init() -> None:
    from investment_agent.storage.database import init_engine

    settings = Settings()  # type: ignore[call-arg]
    init_engine(settings.database_url)


def _report(exc: RuleValidationError) -> None:
    typer.echo(f"Invalid {exc.source or ''}: {len(exc.issues)} issue(s)")
    for issue in exc.issues:
        typer.echo(f"  {issue}")


@app.command("validate")
def validate(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Template or instance file"),  # noqa: B008
) -> None:
    """Validate a template file (offline) or an instance file (against the stored template)."""

    try:
        raw = read_rule_file(file)
        if "template_ref" not in raw:
            template = load_template_file(file)
            rprint(f"[green]Valid template[/green] {template.id} ({template.name})")
            return
        _init()
        instance_template = asyncio.run(_stored_template(str(raw["template_ref"])))
        if instance_template is None:
            rprint(f"[red]Unknown template[/red] {raw['template_ref']}; run `ia rules sync`")
            raise typer.Exit(code=1)
        instance = parse_instance(raw, instance_template, source=str(file))
        rprint(f"[green]Valid instance[/green] {instance.symbol} {instance.scope} "
               f"on {instance.template_ref}")
    except RuleValidationError as exc:
        _report(exc)
        raise typer.Exit(code=1) from exc


async def _stored_template(ref: str) -> RuleTemplate | None:
    from investment_agent.rules.store import RuleStore
    from investment_agent.storage.database import session_scope

    async with session_scope() as session:
        return await RuleStore(session).template_by_ref(ref)


@app.command("sync")
def sync(
    directory: Path = typer.Option(DEFAULT_TEMPLATES_DIR, "--dir", help=_DIR_HELP),  # noqa: B008
) -> None:
    """Load every template file and store a new version for changed content."""

    try:
        templates = load_templates(directory)
    except RuleValidationError as exc:
        _report(exc)
        raise typer.Exit(code=1) from exc
    _init()

    async def run() -> None:
        from investment_agent.rules.store import RuleStore
        from investment_agent.storage.database import session_scope

        async with session_scope() as session:
            outcomes = await RuleStore(session).sync_templates(
                [(path, template) for path, template in templates]
            )
        table = Table("template", "version", "action", "hash")
        for outcome in outcomes:
            table.add_row(outcome.template_id, str(outcome.version), outcome.action,
                          outcome.hash[:12])
        rprint(table)

    asyncio.run(run())


@app.command("list")
def list_rules(
    symbol: str | None = typer.Option(None, help="Only instances of this ticker"),
) -> None:
    """Stored template versions and rule instances."""

    _init()

    async def run() -> None:
        from investment_agent.rules.store import RuleStore
        from investment_agent.storage.database import session_scope

        async with session_scope() as session:
            store = RuleStore(session)
            records = await store.template_records()
            instances = await store.instances(symbol=symbol)
        templates = Table("template", "version", "name", "calibration", "hash", "created")
        for record in records:
            templates.add_row(
                record.id,
                str(record.version),
                record.name,
                record.calibration_status,
                record.hash[:12],
                record.created_at.isoformat(timespec="seconds") if record.created_at else "",
            )
        rprint(templates)
        table = Table("id", "symbol", "scope", "version", "status", "template", "approved")
        for item in instances:
            table.add_row(
                str(item.id),
                item.symbol,
                item.scope,
                str(item.version),
                item.status,
                f"{item.template_id}@{item.template_version}",
                item.approved_at.isoformat(timespec="seconds") if item.approved_at else "",
            )
        rprint(table)

    asyncio.run(run())


@app.command("show")
def show(
    ref: str = typer.Argument(..., help="Template id[@version], or a numeric rule instance id"),
) -> None:
    """Print a stored template version or rule instance as canonical YAML."""

    _init()

    async def run() -> str | None:
        from investment_agent.rules.store import RuleStore, instance_from_record
        from investment_agent.storage.database import session_scope

        async with session_scope() as session:
            store = RuleStore(session)
            if ref.isdigit():
                record = await store.get_instance(int(ref))
                if record is None:
                    return None
                header = (
                    f"# rule instance #{record.id} {record.symbol} {record.scope} "
                    f"v{record.version} {record.status} hash={record.hash[:12]}\n"
                )
                return header + to_yaml(instance_from_record(record))
            template = await store.template_by_ref(ref)
            if template is None:
                return None
            return f"# template {template.ref}\n" + to_yaml(template)

    text = asyncio.run(run())
    if text is None:
        rprint(f"[red]Not found[/red]: {escape(ref)}")
        raise typer.Exit(code=1)
    typer.echo(text)
