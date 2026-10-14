"""``ia rules``: validate rule files, sync templates into the rule store, inspect."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import typer
from rich import print as rprint
from rich.markup import escape
from rich.table import Table

from investment_agent.cli.runtime import bootstrap_settings, run_inline
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


def _run[T](main: Callable[[], Awaitable[T]]) -> T:
    """Run ``main`` with the database engine opened and disposed on its loop."""

    from investment_agent.storage.database import run_with_engine

    return run_with_engine(bootstrap_settings().database_url, main)


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
            template = load_template_file(file, require_docs=True)
            rprint(f"[green]Valid template[/green] {template.id} ({template.name})")
            return
        instance_template = _run(lambda: _stored_template(str(raw["template_ref"])))
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

    _run(run)


@app.command("list")
def list_rules(
    symbol: str | None = typer.Option(None, help="Only instances of this ticker"),
) -> None:
    """Stored template versions and rule instances."""

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

    _run(run)


@app.command("show")
def show(
    ref: str = typer.Argument(..., help="Template id[@version], or a numeric rule instance id"),
) -> None:
    """Print a stored template version or rule instance as canonical YAML."""

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

    text = _run(run)
    if text is None:
        rprint(f"[red]Not found[/red]: {escape(ref)}")
        raise typer.Exit(code=1)
    typer.echo(text)


@app.command("draft")
def draft(
    ticker: str = typer.Option(..., help="A-share ticker, e.g. 600519.SH"),
    template: str = typer.Option(..., help="Template id (latest stored version) or id@version"),
    as_of: str | None = typer.Option(None, help="Factor values as of (YYYY-MM-DD, default today)"),
    now: bool = typer.Option(
        False, "--now", help="Also execute the job in this process instead of the worker"
    ),
    candidate_id: int = typer.Option(
        0, min=0, help="Candidate to notify when done (0 = manual draft, no candidate)"
    ),
    screen_result_id: int | None = typer.Option(None, help="screen_result row to cite"),
    note: list[str] | None = typer.Option(  # noqa: B008
        None, "--note", help="Preference note (repeatable), e.g. a past rejection reason"
    ),
    rerun: bool = typer.Option(
        False, "--rerun", help="Queue a new run even if an identical draft run exists"
    ),
) -> None:
    """Enqueue (or with --now run) an LLM rule draft for one stock and template.

    Calls the configured tier-3 LLM; the draft is stored only if it passes
    validation (one retry with the issues fed back).
    """

    from datetime import UTC, date, datetime

    from investment_agent.clock import shanghai_today
    from investment_agent.rules.drafting import RuleDraftPayload

    try:
        day = date.fromisoformat(as_of) if as_of else shanghai_today()
    except ValueError as exc:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint="--as-of") from exc
    bootstrap = bootstrap_settings()

    async def submit() -> int:
        from investment_agent.rules.drafting import (
            RuleDraftHandler,
            enqueue_rule_draft,
            rule_draft_idempotency_key,
        )
        from investment_agent.rules.generation import claim_draft_generation
        from investment_agent.rules.store import RuleStore, instance_from_record
        from investment_agent.storage.database import session_scope
        from investment_agent.storage.repositories.pipeline_run import (
            IdempotencyPayloadMismatchError,
        )

        async with session_scope() as session:
            stored = await RuleStore(session).template_by_ref(template)
        if stored is None:
            rprint(f"[red]Unknown template[/red] {escape(template)}; run `ia rules sync`")
            return 1
        symbol = ticker.strip().upper()
        payload = RuleDraftPayload(
            candidate_id=candidate_id,
            ticker=symbol,
            template_id=stored.id,
            template_version=stored.version,
            as_of=day,
            screen_result_id=screen_result_id,
            preference_notes=list(note or []),
        )
        key = rule_draft_idempotency_key(payload)
        if candidate_id == 0:
            key = f"{key}:{symbol}:{day.isoformat()}"
        if rerun:
            key = f"{key}:{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
        # A manual draft claims the shared (listing, scope) generation exactly
        # like the scheduled origins (plan 7.2 WP9), so both paths share the
        # slot discipline: without the claim the store guard would refuse any
        # stock that has ever been through the funnel ("superseded").
        payload = payload.model_copy(update={"draft_token": key})
        try:
            async with session_scope() as session:
                queued, created = await enqueue_rule_draft(
                    session, payload, triggered_by="cli", key=key
                )
                if created and queued.listing_id is not None:
                    await claim_draft_generation(
                        session,
                        listing_id=queued.listing_id,
                        symbol=symbol,
                        scope=payload.scope,
                        token=key,
                        origin_kind=payload.origin_kind,
                        origin_id=candidate_id or None,
                        template_ref=payload.template_ref,
                    )
                run_id, status = queued.id, queued.status
        except IdempotencyPayloadMismatchError:
            rprint(f"[red]Run {escape(key)} exists with different input[/red]; use --rerun")
            return 1
        label = "[green]Queued[/green]" if created else "[yellow]Existing[/yellow]"
        rprint(f"{label} rule draft run #{run_id} (key={escape(key)}) status={status}")
        if not now:
            return 0
        if status != "queued":
            rprint(f"Run #{run_id} is {status}; nothing to run inline")
            return 0
        from investment_agent.settings.loader import load_settings

        settings = await load_settings(base=bootstrap)
        finished = await run_inline(
            settings, run_id=run_id, handlers=[RuleDraftHandler(settings=settings)]
        )
        if finished.error_summary:
            rprint(f"Error: {escape(finished.error_summary)}")
        async with session_scope() as session:
            rule_id = (finished.metadata_json or {}).get("rule_instance_id")
            if not isinstance(rule_id, int):
                return 0 if finished.status == "success" else 2
            record = await RuleStore(session).get_instance(rule_id)
            assert record is not None
            typer.echo(
                f"# rule instance #{record.id} {record.symbol} {record.scope} "
                f"v{record.version} {record.status} drafted_by={record.drafted_by}"
            )
            typer.echo(to_yaml(instance_from_record(record)))
        return 0

    from investment_agent.storage.database import run_with_engine

    code = run_with_engine(bootstrap.database_url, submit)
    if code:
        raise typer.Exit(code=code)
