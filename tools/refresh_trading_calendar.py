"""Refresh ``config/calendars/a_share.yaml`` from two independent providers.

Sources (both must answer the whole year and agree on every trading day):

1. baostock ``query_trade_dates`` - the official calendar per calendar day;
2. Sina's calendar via AkShare (``tool_trade_date_hist_sina``).

An agreeing year is proposed with per-year provenance (``verified_by`` /
``verified_on``). Without ``--write`` the tool only prints the diff against the
committed file and exits 1 when there is drift, so it doubles as a watchdog;
with ``--write`` it rewrites the file - review and commit it yourself (nothing
is committed automatically). A year the sources still disagree about aborts
the whole run (exit 2) and nothing is written.

The exchanges publish next year's holiday schedule around December; until then
both sources answer nothing for that year and the tool reports it as skipped,
not as an error. See docs/product/operations.md, "年度 / 不定期维护".

Usage::

    python tools/refresh_trading_calendar.py                 # show the diff
    python tools/refresh_trading_calendar.py --write         # rewrite + verify
    python tools/refresh_trading_calendar.py --year 2027     # one year only
    python tools/refresh_trading_calendar.py --notice-url URL --notice-title T \
        --notice-published-on 2026-12-18                     # new announcement
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from investment_agent.clock import shanghai_today  # noqa: E402
from investment_agent.providers.akshare_calendar import fetch_trading_days  # noqa: E402
from investment_agent.providers.baostock_reference import (  # noqa: E402
    BaostockReferenceClient,
)
from investment_agent.providers.errors import ProviderError  # noqa: E402
from investment_agent.scheduler.calendar_refresh import (  # noqa: E402
    VERIFIED_BY,
    parse_calendar_text,
    plan_refresh,
    refresh_years,
    render_calendar,
)
from investment_agent.scheduler.trading_calendar import (  # noqa: E402
    TradingCalendar,
    load_trading_calendar,
)
from investment_agent.settings.loader import calendar_path  # noqa: E402
from investment_agent.snapshots.calendar_probe import candidate_path  # noqa: E402


def _apply_notice(
    calendar: TradingCalendar, args: argparse.Namespace
) -> tuple[TradingCalendar, str | None]:
    updates: dict[str, object] = {}
    if args.notice_url is not None:
        updates["url"] = args.notice_url
    if args.notice_title is not None:
        updates["title"] = args.notice_title
    if args.notice_published_on is not None:
        updates["published_on"] = args.notice_published_on
    if not updates:
        return calendar, None
    source = calendar.source.model_copy(update=updates)
    return calendar.model_copy(update={"source": source}), source.title


def _fetch_baostock_answers(start: date, end: date) -> dict[date, bool]:
    client = BaostockReferenceClient()
    return asyncio.run(client.fetch_trade_dates(start, end))


def _write_calendar(path: Path, proposed: TradingCalendar) -> str | None:
    """Render and write after a round-trip check; returns an error message, else None."""

    text = render_calendar(proposed)
    if parse_calendar_text(text) != proposed:
        return "refusing to write: the rendered file does not round-trip"
    path.write_text(text, encoding="utf-8")
    return None


def _clear_applied_candidates(calendar: TradingCalendar, data_dir: Path) -> list[Path]:
    """Drop probe candidates whose year the written calendar now covers."""

    applied: list[Path] = []
    for year in sorted(item.year for item in calendar.years):
        candidate = candidate_path(data_dir, year)
        if candidate.is_file():
            candidate.unlink()
            applied.append(candidate)
    return applied


def main(args: argparse.Namespace) -> int:
    path = Path(args.calendar)
    current = load_trading_calendar(path)
    years = sorted(set(args.year)) if args.year else refresh_years(current)
    print(f"calendar: {path} ({len(current.years)} reviewed years); checking {years}")

    start, end = date(min(years), 1, 1), date(max(years), 12, 31)
    try:
        answers = _fetch_baostock_answers(start, end)
    except ProviderError as exc:
        print(f"baostock: {exc}", file=sys.stderr)
        return 2
    try:
        sina_days = fetch_trading_days()
    except ProviderError as exc:
        print(f"akshare/sina: {exc}", file=sys.stderr)
        return 2
    print(
        f"baostock: {sum(answers.values())} trading days in {start}..{end}; "
        f"akshare/sina: {len(sina_days)} days to {max(sina_days)}"
    )

    plan = plan_refresh(
        current,
        years=years,
        baostock_answers=answers,
        sina_days=sina_days,
        verified_by=VERIFIED_BY,
        verified_on=shanghai_today(),
    )
    for year, reason in plan.skipped:
        print(f"year {year}: skipped - {reason}")
    if plan.conflicts:
        for year, days in plan.conflicts:
            listed = ", ".join(day.isoformat() for day in days)
            print(f"year {year}: SOURCES DISAGREE on {listed}", file=sys.stderr)
        print("nothing written; investigate the sources first", file=sys.stderr)
        return 2

    proposed, notice_title = _apply_notice(plan.proposed, args)
    changes = list(plan.changes)
    if notice_title is not None:
        changes.append(f"source: recorded the announcement {notice_title!r}")
    if not changes:
        print("calendar is up to date - nothing to write")
        return 0
    for line in changes:
        print(f"  {line}")
    if not args.write:
        print("differences above; review them, then re-run with --write")
        return 1

    failure = _write_calendar(path, proposed)
    if failure is not None:
        print(failure, file=sys.stderr)
        return 2
    print(f"wrote {path} - review the diff and commit it")
    for candidate in _clear_applied_candidates(proposed, Path(args.data_dir)):
        print(f"removed the applied candidate {candidate}")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument(
        "--write", action="store_true", help="rewrite the calendar file (then commit it)"
    )
    parser.add_argument(
        "--year",
        type=int,
        action="append",
        default=None,
        help="year to check (repeatable; default: reviewed years + the next one)",
    )
    parser.add_argument(
        "--calendar",
        default=calendar_path(REPO_ROOT / "config" / "app.yaml"),
        help="calendar file to refresh",
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("DATA_DIR", "data"),
        help="runtime data dir holding calendar_candidates/ (default: $DATA_DIR or ./data)",
    )
    parser.add_argument("--notice-url", default=None, help="next announcement URL for source.url")
    parser.add_argument("--notice-title", default=None, help="next announcement title")
    parser.add_argument(
        "--notice-published-on",
        type=date.fromisoformat,
        default=None,
        help="next announcement date (YYYY-MM-DD)",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(main(_parse_args()))
