"""``ia forward-validation run``: offline, read-only forward validation.

The command never collects data and never writes to the database: it opens an
existing SQLite file with ``mode=ro`` and writes only its JSON report. It does
not change ``calibration_status``.

Examples::

    ia forward-validation run --database .dev/data/app.db \\
        --as-of 2024-06-28 --as-of 2024-12-31 --output /tmp/forward.json

    ia forward-validation run --database .dev/data/app.db \\
        --as-of 2024-06-28 --template-source files --horizon 3 --horizon 6
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

import typer
from rich import print as rprint
from rich.markup import escape
from rich.table import Table

from investment_agent.cli.parsing import date_option
from investment_agent.research.forward_validation import (
    CostModel,
    ValidationConfig,
    failed_report,
    run_forward_validation_sync,
    write_report,
)
from investment_agent.research.forward_validation.contracts import HORIZON_SESSIONS

app = typer.Typer(help="Offline forward validation of scoring v1.4 and rule templates")


def _horizons(values: list[int]) -> tuple[int, ...]:
    horizons: list[int] = []
    for value in values or [1, 3, 6]:
        if value not in HORIZON_SESSIONS:
            raise typer.BadParameter(
                f"unsupported horizon {value}; choose from {sorted(HORIZON_SESSIONS)}",
                param_hint="--horizon",
            )
        horizons.append(value)
    return tuple(dict.fromkeys(horizons))


def _template_source(value: str) -> Literal["db", "files"]:
    normalized = value.strip().lower()
    if normalized not in {"db", "files"}:
        raise typer.BadParameter("expected db or files", param_hint="--template-source")
    return "files" if normalized == "files" else "db"


def _as_of_dates(values: list[str]) -> tuple[date, ...]:
    if not values:
        raise typer.BadParameter("at least one --as-of date is required", param_hint="--as-of")
    parsed: list[date] = []
    for value in values:
        parsed.append(date_option(value, "--as-of"))
    return tuple(parsed)


@app.command("run")
def run(
    database: str = typer.Option(
        ..., "--database", help="Existing SQLite database (opened read-only)"
    ),
    as_of: list[str] = typer.Option(  # noqa: B008 - list default is Typer's own factory
        ..., "--as-of", help="Signal date (YYYY-MM-DD); repeat for walk-forward"
    ),
    universe: str = typer.Option("CSI300", help="Universe code; PIT members are read as of each date"),
    horizon: list[int] = typer.Option(  # noqa: B008 - list default is Typer's own factory
        [1, 3, 6], "--horizon", help="Horizon in months (1/3/6); repeat to select"
    ),
    benchmark: str = typer.Option("equal_weight", help="equal_weight or csi300"),
    template_source: str = typer.Option(
        "db", "--template-source", help="Where templates come from: db or files"
    ),
    template: list[str] = typer.Option(  # noqa: B008 - list default is Typer's own factory
        [], "--template", help="Restrict to these template refs/ids (repeatable)"
    ),
    min_scored: int = typer.Option(20, min=2, help="Minimum scored members per cross-section"),
    min_group_size: int = typer.Option(5, min=1, help="Minimum bucket / group size"),
    min_template_hits: int = typer.Option(5, min=1, help="Minimum evaluated hits per template"),
    commission_bps: float = typer.Option(3.0, min=0.0, help="Round-trip commission (bps, one way)"),
    stamp_duty_bps: float = typer.Option(5.0, min=0.0, help="Sell-side stamp duty (bps)"),
    slippage_bps: float = typer.Option(10.0, min=0.0, help="Slippage (bps, one way)"),
    max_entry_deferral: int = typer.Option(5, min=0, help="Sessions to wait for a tradable entry"),
    max_exit_deferral: int = typer.Option(5, min=0, help="Sessions to wait for a tradable exit"),
    no_backfill: bool = typer.Option(
        False, "--no-backfill", help="Exclude backfilled market bars (strict capture time)"
    ),
    output: str | None = typer.Option(None, "--output", help="Write the JSON report here"),
) -> None:
    """Run the validation and print a summary; never modifies the database."""

    config = ValidationConfig(
        as_of_dates=_as_of_dates(as_of),
        universe=universe.strip().upper(),
        horizons=_horizons(horizon),
        benchmark=benchmark,
        cost=CostModel(
            commission_bps=commission_bps,
            stamp_duty_bps=stamp_duty_bps,
            slippage_bps=slippage_bps,
        ),
        min_scored=min_scored,
        min_group_size=min_group_size,
        min_template_hits=min_template_hits,
        max_entry_deferral=max_entry_deferral,
        max_exit_deferral=max_exit_deferral,
        template_source=_template_source(template_source),
        template_refs=tuple(template) or None,
        include_backfill=not no_backfill,
    )
    database_path = Path(database).expanduser()
    try:
        report = run_forward_validation_sync(config, database=database_path)
    except (FileNotFoundError, NotImplementedError) as exc:
        report = failed_report(config, reason=str(exc), database=database_path)
        rprint(f"[red]Cannot run:[/red] {escape(str(exc))}")
    if output is not None:
        write_report(report, Path(output).expanduser())
        rprint(f"Report written to {escape(str(output))}")
    _print_summary(report)
    if report.get("status") == "insufficient":
        raise typer.Exit(code=2)


def _print_summary(report: dict[str, Any]) -> None:
    status = str(report.get("status"))
    sample = report.get("sample") or {}
    if not isinstance(sample, dict):
        sample = {}
    rprint(
        f"status=[bold]{escape(status)}[/bold] "
        f"observations={sample.get('total_observations')} "
        f"scored={sample.get('total_scored')} "
        f"failed={sample.get('total_failed')}"
    )
    for reason in _sequence(report.get("insufficient_reasons")):
        rprint(f"  - {escape(str(reason))}")
    sessions = sample.get("market_sessions") or {}
    if isinstance(sessions, dict) and sessions.get("first"):
        rprint(
            f"market sessions {sessions.get('first')}..{sessions.get('last')} "
            f"({sessions.get('count')})"
        )
    table = Table("as_of", "members", "scored", "horizon", "evaluated", "IC", "top-bottom", "hit rate")
    for section in _sequence(report.get("cross_sections")):
        if not isinstance(section, dict):
            continue
        for label, horizon in _mapping(section.get("horizons")).items():
            if not isinstance(horizon, dict):
                continue
            table.add_row(
                str(section.get("as_of")),
                str(section.get("members")),
                str(section.get("scored")),
                str(label),
                str(horizon.get("evaluated")),
                _fmt(horizon.get("ic_spearman")),
                _fmt(horizon.get("top_minus_bottom")),
                _fmt(horizon.get("hit_rate")),
            )
    if table.row_count:
        rprint(table)
    template_table = Table("as_of", "horizon", "template", "hits", "spread vs non-hits", "sufficient")
    for section in _sequence(report.get("cross_sections")):
        if not isinstance(section, dict):
            continue
        for label, by_template in _mapping(section.get("templates")).items():
            for ref, entry in _mapping(by_template).items():
                template_table.add_row(
                    str(section.get("as_of")),
                    str(label),
                    str(ref),
                    str(entry.get("evaluated_hits")),
                    _fmt(entry.get("spread_vs_non_hits")),
                    "yes" if entry.get("sufficient") else "no",
                )
    if template_table.row_count:
        rprint(template_table)


def _sequence(value: object) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, int | float):
        return f"{value:.4f}"
    return str(value)


@app.command("show")
def show(
    output: str = typer.Argument(..., help="Report JSON written by `run --output`"),
) -> None:
    """Print a stored report's summary (no database access)."""

    report = json.loads(Path(output).expanduser().read_text(encoding="utf-8"))
    _print_summary(report)
    meta = {
        "config_hash": report.get("config_hash"),
        "scoring": report.get("scoring"),
        "templates": report.get("templates"),
        "code_version": report.get("code_version"),
    }
    typer.echo(json.dumps(meta, ensure_ascii=False, sort_keys=True, default=str))
