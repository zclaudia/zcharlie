"""Scheduled dual-source probe of the next unreviewed calendar year.

The exchanges publish next year's holiday schedule around December, and the
committed calendar is fail-closed until the year is added. This module closes
the "who remembers to check" gap: the scheduler enqueues one probe per week
while the calendar is inside its expiry window
(:func:`investment_agent.scheduler.runner.run_calendar_probe_job`), and the
worker then

* fetches baostock's day-by-day answers and Sina's list (via AkShare);
* when both answer the whole year and agree on every trading day, writes a
  canonical candidate calendar to ``data/calendar_candidates/a_share-<year>.yaml``
  and notifies once per distinct candidate (``info``);
* when the sources disagree, notifies (``action_required``) and finishes
  ``partial`` with the offending days in the run metadata;
* when a source has not published the year yet, finishes ``success`` silently -
  the calendar coverage warning is already on the dashboard.

Applying stays manual and reviewed: ``tools/refresh_trading_calendar.py
--write`` (which re-runs the same cross-check) plus a commit. The candidate is
canonical output, so copying it over ``config/calendars/a_share.yaml`` is
equivalent; nothing here ever touches the runtime calendar.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Protocol

import structlog

from investment_agent.domain.protocols import RenderedContent
from investment_agent.notifiers.file_notifier import FileNotifier
from investment_agent.scheduler.calendar_refresh import (
    VERIFIED_BY,
    days_of_year,
    parse_calendar_text,
    plan_refresh,
    render_calendar,
)
from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    calendar_coverage,
    load_trading_calendar,
)
from investment_agent.snapshots.jobs import CalendarProbePayload

log = structlog.get_logger(__name__)

CANDIDATE_DIR = "calendar_candidates"
TOOL = "tools/refresh_trading_calendar.py"


class TradeDateClient(Protocol):
    async def fetch_trade_dates(self, start: date, end: date) -> dict[date, bool]: ...


FetchTradingDays = Callable[..., Collection[date]]
"""``providers.akshare_calendar.fetch_trading_days`` (``backend=`` keyword)."""


@dataclass(frozen=True)
class ProbeOutcome:
    status: Literal["success", "partial"]
    metadata: dict[str, object]
    summary: str = ""


def candidate_path(data_dir: Path, year: int) -> Path:
    return Path(data_dir) / CANDIDATE_DIR / f"a_share-{year}.yaml"


@dataclass(frozen=True)
class CalendarCandidate:
    """One verified candidate file, parsed (the UI status and the apply action)."""

    path: Path
    year: int
    calendar: TradingCalendar

    @property
    def closed_dates(self) -> tuple[date, ...]:
        item = next(entry for entry in self.calendar.years if entry.year == self.year)
        return tuple(sorted(item.closed_dates))

    @property
    def open_dates(self) -> tuple[date, ...]:
        item = next(entry for entry in self.calendar.years if entry.year == self.year)
        return tuple(sorted(item.open_dates))

    @property
    def trading_days(self) -> int:
        item = next(entry for entry in self.calendar.years if entry.year == self.year)
        return sum(
            1
            for day in days_of_year(self.year)
            if (day.weekday() < 5 and day not in item.closed_dates) or day in item.open_dates
        )

    @property
    def verified_by(self) -> tuple[str, ...]:
        return tuple(
            next(entry for entry in self.calendar.years if entry.year == self.year).verified_by
        )

    @property
    def verified_on(self) -> date | None:
        return next(entry for entry in self.calendar.years if entry.year == self.year).verified_on


def candidates(data_dir: Path) -> list[CalendarCandidate]:
    """Every parseable candidate under ``data/calendar_candidates``, by year."""

    directory = Path(data_dir) / CANDIDATE_DIR
    parsed: list[CalendarCandidate] = []
    for path in sorted(directory.glob("a_share-*.yaml")) if directory.is_dir() else []:
        try:
            calendar = parse_calendar_text(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("calendar_probe.candidate_unreadable", path=str(path))
            continue
        if not calendar.years:
            continue
        year = max(item.year for item in calendar.years)
        parsed.append(CalendarCandidate(path=path, year=year, calendar=calendar))
    return sorted(parsed, key=lambda item: (item.year, str(item.path)))


class CandidateConflictError(RuntimeError):
    """The candidate cannot be applied to the committed calendar as it is."""


def apply_candidate(calendar_path: Path, candidate: CalendarCandidate) -> tuple[TradingCalendar, tuple[int, ...]]:
    """Write a verified candidate over the runtime calendar (atomic).

    A click must never rewrite history, so the candidate may only *add* years:
    it must contain every reviewed year the committed file has, with identical
    data and provenance, and at least one new year. The candidate file is
    removed once the calendar is written.
    """

    current = load_trading_calendar(calendar_path)
    before = {item.year: item for item in current.years}
    after = {item.year: item for item in candidate.calendar.years}
    missing = sorted(before.keys() - after.keys())
    if missing:
        raise CandidateConflictError(
            f"candidate {candidate.path.name} lacks reviewed years {missing}"
        )
    for year in sorted(before.keys() & after.keys()):
        if before[year] != after[year]:
            raise CandidateConflictError(
                f"candidate would change the reviewed year {year}"
            )
    added = tuple(sorted(after.keys() - before.keys()))
    if not added:
        raise CandidateConflictError("candidate adds no year")

    text = render_calendar(candidate.calendar)
    if parse_calendar_text(text) != candidate.calendar:
        raise CandidateConflictError("candidate does not round-trip")
    _write_atomic(Path(calendar_path), text)
    candidate.path.unlink()
    log.info("calendar_probe.applied", year=added[-1], path=str(calendar_path))
    return candidate.calendar, added


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _candidate_body(year: int, path: Path, calendar: TradingCalendar) -> str:
    item = next(entry for entry in calendar.years if entry.year == year)
    closed = "、".join(day.isoformat() for day in sorted(item.closed_dates)) or "（无）"
    trading = sum(
        1
        for day in days_of_year(year)
        if (day.weekday() < 5 and day not in item.closed_dates) or day in item.open_dates
    )
    return (
        f"# 交易日历候选 {year}\n\n"
        f"baostock 与 AkShare/Sina 双源逐日一致：{trading} 个交易日，"
        f"工作日休市 {len(item.closed_dates)} 天，周末交易日 {len(item.open_dates)} 天。\n\n"
        f"休市日：{closed}\n\n"
        f"候选文件：{path}\n\n"
        "在『数据健康』页的交易日历卡片点「应用候选」即可写入 "
        "`config/calendars/a_share.yaml`（再提交一次文件变更以随部署保留）。\n"
        f"命令行等价操作：`python {TOOL} --write` 或直接用候选文件覆盖日历文件。\n"
    )


def _conflict_body(year: int, days: tuple[date, ...]) -> str:
    listed = "、".join(day.isoformat() for day in days)
    return (
        f"# 交易日历核对分歧 {year}\n\n"
        f"baostock 与 AkShare/Sina 对以下日期是否交易不一致：{listed}\n\n"
        "未生成候选、未写入任何文件。请稍后重跑（下周自动重试）或手动运行 "
        f"`python {TOOL}` 查看完整差异。\n"
    )


async def _notify(
    notifier: FileNotifier,
    *,
    run_id: int,
    today: date,
    level: str,
    title: str,
    body: str,
) -> str | None:
    content = RenderedContent(format="markdown", body=body, metadata={"level": level})
    metadata: dict[str, object] = {
        "kind": f"calendar_probe_{title}",
        "run_id": run_id,
        "date": today.isoformat(),
        "level": level,
    }
    target = notifier.target_path(content.format, metadata)
    if target.is_file():
        return str(target)
    result = await notifier.send(content, channel_metadata=metadata)
    if not result.success:
        raise RuntimeError(result.error or "calendar probe notification failed")
    return result.target


async def collect_calendar_probe(
    payload: CalendarProbePayload,
    *,
    calendar: TradingCalendar | None,
    reference_client: TradeDateClient,
    sina_backend: object | None,
    fetch_trading_days: FetchTradingDays,
    data_dir: Path,
    notifier: FileNotifier,
    run_id: int,
    today: date,
) -> ProbeOutcome:
    """One probe run: cross-check ``payload.year`` and record a candidate.

    ``payload.force`` (a manual check from the health page) skips the
    expiry-window gate only; publication and agreement still decide.
    """

    if calendar is None:
        return ProbeOutcome(
            "partial",
            {"status": "calendar_unavailable", "year": payload.year},
            "交易日历不可读，跳过核对",
        )
    coverage = calendar_coverage(calendar, today)
    if not coverage.expiring_soon and not payload.force:
        return ProbeOutcome(
            "success",
            {
                "status": "not_due",
                "year": payload.year,
                "coverage_end": (
                    coverage.coverage_end.isoformat() if coverage.coverage_end else None
                ),
            },
            "日历未临近到期，跳过核对",
        )
    if any(item.year == payload.year for item in calendar.years):
        return ProbeOutcome(
            "success",
            {"status": "up_to_date", "year": payload.year},
            f"{payload.year} 年已在日历中",
        )

    answers = await reference_client.fetch_trade_dates(
        date(payload.year, 1, 1), date(payload.year, 12, 31)
    )
    sina_days = fetch_trading_days(backend=sina_backend)
    plan = plan_refresh(
        calendar,
        years=[payload.year],
        baostock_answers=answers,
        sina_days=sina_days,
        verified_by=VERIFIED_BY,
        verified_on=today,
    )
    if plan.conflicts:
        days = plan.conflicts[0][1]
        await _notify(
            notifier,
            run_id=run_id,
            today=today,
            level="action_required",
            title="conflict",
            body=_conflict_body(payload.year, days),
        )
        return ProbeOutcome(
            "partial",
            {
                "status": "conflict",
                "year": payload.year,
                "disagreements": [day.isoformat() for day in days],
            },
            f"{payload.year} 双源分歧：{', '.join(day.isoformat() for day in days)}",
        )
    reasons = dict(plan.skipped)
    if payload.year in reasons:
        return ProbeOutcome(
            "success",
            {"status": "not_published", "year": payload.year, "reason": reasons[payload.year]},
            f"{payload.year} 尚未公布",
        )

    text = render_calendar(plan.proposed)
    if parse_calendar_text(text) != plan.proposed:
        raise RuntimeError("rendered candidate calendar does not round-trip")
    path = candidate_path(data_dir, payload.year)
    previous = path.read_text(encoding="utf-8") if path.is_file() else None
    _write_atomic(path, text)
    if previous != text:
        await _notify(
            notifier,
            run_id=run_id,
            today=today,
            level="info",
            title="candidate",
            body=_candidate_body(payload.year, path, plan.proposed),
        )
    log.info(
        "calendar_probe.candidate",
        year=payload.year,
        path=str(path),
        changed=previous != text,
        changes=list(plan.changes),
    )
    return ProbeOutcome(
        "success",
        {
            "status": "candidate",
            "year": payload.year,
            "candidate": str(path),
            "changes": list(plan.changes),
            "changed": previous != text,
        },
        f"{payload.year} 双源一致，候选已生成",
    )
