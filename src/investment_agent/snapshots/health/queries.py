"""Session-level queries behind the data health read model (no repository layer).

Every ``select(...)`` of the health read path lives here: recent runs, per-member
freshness, universe / industry coverage, the iFinD verification queue and the
provider-call ledger. All of it is read-only and aggregate (one bounded scan of
recent runs, per-member index seeks for freshness) so the endpoint answers in
well under a second on a full-size database.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.domain.time import optional_utc as as_utc
from investment_agent.providers.circuit import CircuitStatus
from investment_agent.providers.ifind_budget import budget_status
from investment_agent.scheduler.trading_calendar import TradingCalendar
from investment_agent.snapshots.health.accumulation import (
    LiveSnapshot,
    previous_trading_day,
    trading_days_between,
)
from investment_agent.snapshots.health.models import (
    _FINANCIAL_PERIODS_SHOWN,
    _MAX_CHANGES,
    _MAX_CONFLICTS,
    _MAX_LISTED,
    _MAX_MISSING,
    _PROVIDER_WINDOW,
    _RUN_SCAN_LIMIT,
    _TREND_RUNS,
    _VERIFICATION_SCAN,
    COLLECTION_KINDS,
    COUNTED_STATUSES,
    MISSING_KINDS,
    TERMINAL_STATUSES,
    BudgetHealth,
    CollectionHealth,
    ConflictItem,
    DirectoryHealth,
    DividendFreshness,
    IndustrySystemSync,
    MarketFreshness,
    MembershipChange,
    MissingTicker,
    OverdueHealth,
    OverdueItem,
    PeriodCoverage,
    ProviderHealth,
    RunSummary,
    TrendPoint,
    UniverseSyncInfo,
    VerificationHealth,
)
from investment_agent.snapshots.jobs import (
    EARNINGS_EVENT,
    FINANCIAL_SNAPSHOT,
    INDUSTRY_SYNC,
    INDUSTRY_SYSTEMS,
    MARKET_SNAPSHOT,
    UNIVERSE_SYNC,
)
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.pit_snapshot import (
    DividendCoverage,
    FinancialSnapshot,
    IndustryClassification,
    MarketSnapshot,
    ReportOverdue,
)
from investment_agent.storage.models.reference import Listing, UniverseMembership
from investment_agent.storage.models.verification import VerificationRequest
from investment_agent.storage.repositories.provider_call import ProviderCallRepository
from investment_agent.storage.repositories.stock_directory import StockDirectoryRepository

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


@dataclass(frozen=True)
class LightRun:
    id: int
    kind: str
    status: str
    universe_code: str | None
    request: Mapping[str, Any]
    started_at: datetime | None
    finished_at: datetime | None
    error_summary: str | None

    @property
    def provider(self) -> str:
        if self.kind in (UNIVERSE_SYNC, INDUSTRY_SYNC):
            return "baostock+akshare" if self.kind == INDUSTRY_SYNC else "baostock"
        return str(self.request.get("provider") or "baostock")

    @property
    def scope(self) -> str:
        if self.kind == INDUSTRY_SYNC:
            codes = self.request.get("universe_codes")
            return "+".join(sorted(map(str, codes))) if isinstance(codes, list) and codes else "ALL"
        if self.universe_code:
            return self.universe_code
        return str(self.request.get("universe_code") or self.request.get("scope") or "-")

    @property
    def part(self) -> str | None:
        if self.kind == FINANCIAL_SNAPSHOT and self.provider == "akshare":
            return str(self.request.get("part") or "summary")
        if self.kind == EARNINGS_EVENT:
            return str(self.request.get("part") or "forecast")
        if self.kind == INDUSTRY_SYNC:
            systems = self.request.get("systems")
            if isinstance(systems, list) and systems:
                return "+".join(map(str, systems))
            return "+".join(INDUSTRY_SYSTEMS)
        return None

    @property
    def backfill(self) -> bool:
        return self.request.get("backfill") is True

    @property
    def subject(self) -> str | None:
        for key in ("trade_date", "report_period", "as_of"):
            value = self.request.get(key)
            if isinstance(value, str):
                return value
        return None

    @property
    def group_key(self) -> str:
        return ":".join(
            part for part in (self.kind, self.provider, self.part or "", self.scope) if part
        )


def _timestamp(run: LightRun) -> datetime | None:
    return as_utc(run.finished_at or run.started_at)


# --------------------------------------------------------------------------- #
# Accumulation input
# --------------------------------------------------------------------------- #


async def live_market_snapshots(
    session: AsyncSession, universe_code: str
) -> list[LiveSnapshot]:
    ratio = PipelineRun.metadata_json[("completeness", "ratio")].as_float()
    collected = PipelineRun.metadata_json[("completeness", "collected")].as_integer()
    stmt = select(PipelineRun.request_json, ratio, collected).where(
        PipelineRun.kind == MARKET_SNAPSHOT,
        PipelineRun.universe_code == universe_code,
        PipelineRun.status.in_(COUNTED_STATUSES),
    )
    snapshots: list[LiveSnapshot] = []
    for request, value, count in (await session.execute(stmt)).all():
        request = request or {}
        if str(request.get("provider") or "baostock") != "baostock":
            continue
        if request.get("scope", "universe") != "universe":
            continue
        day = _date(request.get("trade_date"))
        if day is None or value is None or not count:
            continue
        snapshots.append(LiveSnapshot(day, float(value)))
    return snapshots


# --------------------------------------------------------------------------- #
# Members, freshness, universes, industry
# --------------------------------------------------------------------------- #


async def current_member_ids(session: AsyncSession, universe_code: str, today: date) -> list[int]:
    stmt = select(UniverseMembership.listing_id).where(
        UniverseMembership.universe_code == universe_code,
        UniverseMembership.valid_from <= today,
        or_(UniverseMembership.valid_to.is_(None), UniverseMembership.valid_to > today),
    )
    return sorted(set((await session.execute(stmt)).scalars().all()))


async def _latest_member_bar(
    session: AsyncSession, universe_code: str, member_ids: Sequence[int], *, provider: str
) -> date | None:
    """Latest raw bar of ``provider`` stored for any of the members."""

    inner = (
        select(MarketSnapshot.trade_date)
        .where(
            MarketSnapshot.listing_id == UniverseMembership.listing_id,
            MarketSnapshot.provider == provider,
            MarketSnapshot.adjust == "none",
        )
        .order_by(MarketSnapshot.trade_date.desc())
        .limit(1)
        .correlate(UniverseMembership)
        .scalar_subquery()
    )
    stmt = select(func.max(inner)).where(
        UniverseMembership.universe_code == universe_code,
        UniverseMembership.listing_id.in_(member_ids),
    )
    return _date((await session.execute(stmt)).scalar_one_or_none())


async def market_freshness(
    session: AsyncSession,
    universe_code: str,
    member_ids: Sequence[int],
    *,
    today: date,
    calendar: TradingCalendar | None,
) -> MarketFreshness:
    latest: date | None = None
    latest_close_quote: date | None = None
    at_latest = 0
    if member_ids:
        latest = await _latest_member_bar(session, universe_code, member_ids, provider="baostock")
        latest_close_quote = await _latest_member_bar(
            session, universe_code, member_ids, provider="tencent"
        )
        if latest is not None:
            at_latest = int(
                (
                    await session.execute(
                        select(func.count(func.distinct(MarketSnapshot.listing_id))).where(
                            MarketSnapshot.listing_id.in_(member_ids),
                            MarketSnapshot.trade_date == latest,
                            MarketSnapshot.provider == "baostock",
                            MarketSnapshot.adjust == "none",
                        )
                    )
                ).scalar_one()
            )
    last_trading = previous_trading_day(calendar, today, inclusive=True)
    expected = previous_trading_day(calendar, today, inclusive=False)
    lag = None
    if latest is not None:
        lag = trading_days_between(calendar, latest, expected) if latest < expected else 0
    return MarketFreshness(
        universe_code=universe_code,
        members=len(member_ids),
        latest_trade_date=latest,
        members_at_latest=at_latest,
        latest_close_quote_date=latest_close_quote,
        last_trading_day=last_trading,
        expected_trade_date=expected,
        lag_trading_days=lag,
    )


async def financial_freshness(
    session: AsyncSession, member_ids: Sequence[int]
) -> list[PeriodCoverage]:
    if not member_ids:
        return []
    stmt = (
        select(FinancialSnapshot.report_period, func.count(func.distinct(FinancialSnapshot.listing_id)))
        .where(
            FinancialSnapshot.listing_id.in_(member_ids),
            FinancialSnapshot.provider == "baostock",
        )
        .group_by(FinancialSnapshot.report_period)
        .order_by(FinancialSnapshot.report_period.desc())
        .limit(_FINANCIAL_PERIODS_SHOWN)
    )
    members = len(member_ids)
    return [
        PeriodCoverage(
            report_period=period,
            listings=int(count),
            members=members,
            coverage=round(int(count) / members, 4),
        )
        for period, count in (await session.execute(stmt)).all()
    ]


async def dividend_freshness(
    session: AsyncSession, member_ids: Sequence[int], year: int
) -> DividendFreshness:
    covered = 0
    if member_ids:
        covered = int(
            (
                await session.execute(
                    select(func.count(func.distinct(DividendCoverage.listing_id))).where(
                        DividendCoverage.listing_id.in_(member_ids),
                        DividendCoverage.provider == "baostock",
                        DividendCoverage.year == year,
                    )
                )
            ).scalar_one()
        )
    members = len(member_ids)
    return DividendFreshness(
        year=year,
        covered=covered,
        members=members,
        coverage=round(covered / members, 4) if members else None,
    )


async def directory_freshness(session: AsyncSession, *, today: date) -> DirectoryHealth:
    synced_on, total = await StockDirectoryRepository(session).freshness()
    return DirectoryHealth(
        synced_on=synced_on,
        total=total,
        age_days=(today - synced_on).days if synced_on is not None else None,
    )


async def membership_changes(
    session: AsyncSession, universe_code: str, today: date
) -> list[MembershipChange]:
    added_stmt = (
        select(UniverseMembership.valid_from, func.count())
        .where(UniverseMembership.universe_code == universe_code, UniverseMembership.valid_from <= today)
        .group_by(UniverseMembership.valid_from)
        .order_by(UniverseMembership.valid_from.desc())
        .limit(_MAX_CHANGES)
    )
    removed_stmt = (
        select(UniverseMembership.valid_to, func.count())
        .where(
            UniverseMembership.universe_code == universe_code,
            UniverseMembership.valid_to.is_not(None),
            UniverseMembership.valid_to <= today,
        )
        .group_by(UniverseMembership.valid_to)
        .order_by(UniverseMembership.valid_to.desc())
        .limit(_MAX_CHANGES)
    )
    changes: dict[date, list[int]] = {}
    for day, count in (await session.execute(added_stmt)).all():
        changes.setdefault(day, [0, 0])[0] = int(count)
    for day, count in (await session.execute(removed_stmt)).all():
        if day is not None:
            changes.setdefault(day, [0, 0])[1] = int(count)
    return [
        MembershipChange(date=day, added=counts[0], removed=counts[1])
        for day, counts in sorted(changes.items(), reverse=True)[:_MAX_CHANGES]
    ]


def _universe_sync_info(run: LightRun, metadata: Mapping[str, Any]) -> UniverseSyncInfo:
    added = [str(item) for item in metadata.get("added") or [] if isinstance(item, str)]
    removed = [str(item) for item in metadata.get("removed") or [] if isinstance(item, str)]
    return UniverseSyncInfo(
        run_id=run.id,
        status=run.status,
        at=_timestamp(run),
        as_of=_str(run.request.get("as_of")),
        effective_date=_str(metadata.get("effective_date")),
        members=_int(metadata.get("members")),
        added=added[:_MAX_LISTED],
        removed=removed[:_MAX_LISTED],
        added_count=len(added),
        removed_count=len(removed),
    )


async def industry_coverage(
    session: AsyncSession, member_ids: Sequence[int], today: date
) -> tuple[int, int, int]:
    """(with sw2021, CSRC only, neither) among the current members."""

    if not member_ids:
        return 0, 0, 0
    stmt = (
        select(IndustryClassification.listing_id, IndustryClassification.system)
        .where(
            IndustryClassification.listing_id.in_(member_ids),
            IndustryClassification.valid_from <= today,
            or_(
                IndustryClassification.valid_to.is_(None),
                IndustryClassification.valid_to > today,
            ),
        )
        .distinct()
    )
    systems: dict[int, set[str]] = {}
    for listing_id, system in (await session.execute(stmt)).all():
        systems.setdefault(listing_id, set()).add(system)
    sw = sum(1 for listing_id in member_ids if "sw2021" in systems.get(listing_id, set()))
    csrc = sum(
        1
        for listing_id in member_ids
        if "sw2021" not in systems.get(listing_id, set())
        and "csrc" in systems.get(listing_id, set())
    )
    return sw, csrc, len(member_ids) - sw - csrc


def industry_system_syncs(
    runs: Sequence[LightRun], metadata: Mapping[int, Mapping[str, Any]]
) -> list[IndustrySystemSync]:
    """Latest terminal ``industry_sync`` outcome per system (runs newest first)."""

    result: dict[str, IndustrySystemSync] = {}
    for run in runs:
        if run.kind != INDUSTRY_SYNC or run.status not in TERMINAL_STATUSES:
            continue
        meta = metadata.get(run.id, {})
        systems = run.part.split("+") if run.part else list(INDUSTRY_SYSTEMS)
        failed_raw = meta.get("failed_systems")
        failed = {str(item) for item in failed_raw} if isinstance(failed_raw, list) else set()
        errors = meta.get("errors") if isinstance(meta.get("errors"), dict) else {}
        for system in systems:
            if system in result:
                continue
            ran = "applied_entries" in meta if system == "csrc" else "sw2021" in meta
            if run.status != "failed" and system not in failed and not ran:
                continue  # an older run that did not sync this system
            status = "failed" if run.status == "failed" or system in failed else "success"
            detail: str | None
            if status == "failed":
                detail = _str(errors.get(system)) if isinstance(errors, dict) else None
                detail = (detail or run.error_summary or "")[:200] or None
            elif system == "csrc":
                detail = (
                    f"applied {meta.get('applied_entries')}, changed {meta.get('changed')}"
                    if "applied_entries" in meta
                    else None
                )
            else:
                sw = meta.get("sw2021")
                detail = (
                    ", ".join(f"{key} {value}" for key, value in sw.items() if isinstance(value, int))[:200]
                    or None
                    if isinstance(sw, dict)
                    else None
                )
            result[system] = IndustrySystemSync(
                system=system, run_id=run.id, status=status, at=_timestamp(run), detail=detail
            )
    return [
        result.get(system)
        or IndustrySystemSync(system=system, run_id=None, status=None, at=None, detail=None)
        for system in INDUSTRY_SYSTEMS
    ]


# --------------------------------------------------------------------------- #
# Collections
# --------------------------------------------------------------------------- #


async def recent_runs(session: AsyncSession) -> list[LightRun]:
    """Newest first; light columns only (metadata is fetched per selected run)."""

    stmt = (
        select(
            PipelineRun.id,
            PipelineRun.kind,
            PipelineRun.status,
            PipelineRun.universe_code,
            PipelineRun.request_json,
            PipelineRun.started_at,
            PipelineRun.finished_at,
            PipelineRun.error_summary,
        )
        .where(PipelineRun.kind.in_(COLLECTION_KINDS))
        .order_by(PipelineRun.id.desc())
        .limit(_RUN_SCAN_LIMIT)
    )
    return [
        LightRun(
            id=row.id,
            kind=row.kind,
            status=row.status,
            universe_code=row.universe_code,
            request=row.request_json or {},
            started_at=row.started_at,
            finished_at=row.finished_at,
            error_summary=row.error_summary,
        )
        for row in (await session.execute(stmt)).all()
    ]


async def run_metadata(
    session: AsyncSession, run_ids: Iterable[int]
) -> dict[int, Mapping[str, Any]]:
    ids = sorted(set(run_ids))
    if not ids:
        return {}
    stmt = select(PipelineRun.id, PipelineRun.metadata_json).where(PipelineRun.id.in_(ids))
    return {
        run_id: (metadata if isinstance(metadata, dict) else {})
        for run_id, metadata in (await session.execute(stmt)).all()
    }


def _completeness(metadata: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = metadata.get("completeness")
    return value if isinstance(value, dict) else None


def run_summary(run: LightRun, metadata: Mapping[str, Any]) -> RunSummary:
    completeness = _completeness(metadata) or {}
    missing_raw = completeness.get("missing")
    missing: list[MissingTicker] = []
    counts: Counter[str] = Counter()
    if isinstance(missing_raw, dict):
        for ticker, info in missing_raw.items():
            if not isinstance(info, dict):
                continue
            kind = str(info.get("kind") or "failed")
            counts[kind] += 1
            missing.append(MissingTicker(ticker=str(ticker), kind=kind, reason=str(info.get("reason") or "")[:300]))
    order = {kind: index for index, kind in enumerate(MISSING_KINDS)}
    missing.sort(key=lambda item: (order.get(item.kind, len(order)), item.ticker))
    for kind in MISSING_KINDS:
        count = _int(completeness.get(f"{kind}_count"))
        if count is not None and count > counts.get(kind, 0):
            counts[kind] = count
    missing_total = _int(completeness.get("missing_count"))
    return RunSummary(
        run_id=run.id,
        status=run.status,
        subject=run.subject,
        started_at=as_utc(run.started_at),
        finished_at=as_utc(run.finished_at),
        ratio=_float(completeness.get("ratio")),
        expected=_int(completeness.get("expected")),
        collected=_int(completeness.get("collected")),
        missing_by_kind={kind: counts[kind] for kind in MISSING_KINDS if counts.get(kind)},
        missing_total=missing_total if missing_total is not None else len(missing),
        missing=missing[:_MAX_MISSING],
        stop_reason=_str(metadata.get("stop_reason")),
        stop_error=_str(metadata.get("stop_error")),
        error_summary=(run.error_summary or None) and str(run.error_summary)[:300],
    )


@dataclass
class _Group:
    key: str
    sample: LightRun
    terminal: list[LightRun]
    in_flight: int = 0


def group_collections(runs: Sequence[LightRun]) -> list[_Group]:
    """Live collection runs grouped by kind / provider / part / scope (backfill excluded)."""

    groups: dict[str, _Group] = {}
    for run in runs:  # newest first
        if run.backfill:
            continue
        group = groups.get(run.group_key)
        if group is None:
            group = groups[run.group_key] = _Group(run.group_key, run, [])
        if run.status in TERMINAL_STATUSES:
            if len(group.terminal) < _TREND_RUNS:
                group.terminal.append(run)
        elif not group.terminal:
            group.in_flight += 1
    order = {kind: index for index, kind in enumerate(COLLECTION_KINDS)}
    return sorted(groups.values(), key=lambda g: (order.get(g.sample.kind, 99), g.key))


def collection_health(
    group: _Group, metadata: Mapping[int, Mapping[str, Any]]
) -> CollectionHealth:
    last = group.terminal[0] if group.terminal else None
    trend = [
        TrendPoint(
            run_id=run.id,
            subject=run.subject,
            status=run.status,
            finished_at=_timestamp(run),
            ratio=_float((_completeness(metadata.get(run.id, {})) or {}).get("ratio")),
        )
        for run in reversed(group.terminal)
    ]
    return CollectionHealth(
        key=group.key,
        kind=group.sample.kind,
        provider=group.sample.provider,
        part=group.sample.part,
        scope=group.sample.scope,
        last_run=run_summary(last, metadata.get(last.id, {})) if last is not None else None,
        in_flight=group.in_flight,
        trend=trend,
    )


# --------------------------------------------------------------------------- #
# Verification, conflicts, overdue
# --------------------------------------------------------------------------- #


async def verification_health(
    session: AsyncSession,
    *,
    now: datetime,
    cap: int,
    window: timedelta,
    akshare_runs: Sequence[LightRun],
) -> VerificationHealth:
    status = await budget_status(session, now=now, cap=cap, window=window)
    by_status = {
        str(name): int(count)
        for name, count in (
            await session.execute(
                select(VerificationRequest.status, func.count()).group_by(VerificationRequest.status)
            )
        ).all()
    }
    open_by_trigger = {
        str(name): int(count)
        for name, count in (
            await session.execute(
                select(VerificationRequest.trigger, func.count())
                .where(VerificationRequest.status.in_(("pending", "deferred", "running")))
                .group_by(VerificationRequest.trigger)
            )
        ).all()
    }
    conflicts: list[ConflictItem] = []
    done = (
        await session.execute(
            select(
                VerificationRequest.id,
                VerificationRequest.ticker,
                VerificationRequest.result,
                VerificationRequest.finished_at,
                VerificationRequest.run_id,
                VerificationRequest.report_period,
            )
            .where(VerificationRequest.status == "done")
            .order_by(VerificationRequest.finished_at.desc(), VerificationRequest.id.desc())
            .limit(_VERIFICATION_SCAN)
        )
    ).all()
    for request_id, ticker, result, finished_at, run_id, report_period in done:
        names = result.get("conflicts") if isinstance(result, dict) else None
        if not isinstance(names, list) or not names:
            continue
        conflicts.append(
            ConflictItem(
                source="verification",
                ticker=ticker,
                fields=sorted(str(name) for name in names),
                report_period=_str(result.get("report_period"))
                or (report_period.isoformat() if report_period else None),
                at=as_utc(finished_at),
                run_id=run_id,
                request_id=request_id,
            )
        )
    if akshare_runs:
        stmt = select(
            PipelineRun.id, PipelineRun.metadata_json["provider_conflict"]
        ).where(PipelineRun.id.in_([run.id for run in akshare_runs]))
        by_id = {run.id: run for run in akshare_runs}
        for run_id, check in (await session.execute(stmt)).all():
            items = check.get("items") if isinstance(check, dict) else None
            if not isinstance(items, dict):
                continue
            run = by_id[run_id]
            for ticker, fields in items.items():
                conflicts.append(
                    ConflictItem(
                        source="akshare_crosscheck",
                        ticker=str(ticker),
                        fields=sorted(map(str, fields)) if isinstance(fields, dict) else [],
                        report_period=_str(run.request.get("report_period")),
                        at=_timestamp(run),
                        run_id=run_id,
                        request_id=None,
                        backfill=run.backfill,
                    )
                )
    epoch = datetime.min.replace(tzinfo=UTC)
    conflicts.sort(key=lambda item: (not item.backfill, item.at or epoch, item.ticker), reverse=True)
    next_free = status.available_at(1) if status.remaining == 0 else None
    return VerificationHealth(
        budget=BudgetHealth(
            cap=status.cap,
            window_hours=status.window.total_seconds() / 3600,
            used=status.used,
            remaining=status.remaining,
            next_free_at=next_free,
        ),
        by_status=dict(sorted(by_status.items())),
        open_by_trigger=dict(sorted(open_by_trigger.items())),
        conflicts=conflicts[:_MAX_CONFLICTS],
        conflicts_total=len(conflicts),
    )


async def overdue_health(session: AsyncSession) -> OverdueHealth:
    condition = ReportOverdue.resolved_on.is_(None)
    total = int(
        (
            await session.execute(select(func.count()).select_from(ReportOverdue).where(condition))
        ).scalar_one()
    )
    rows = (
        await session.execute(
            select(ReportOverdue, Listing.ticker, Listing.market_code)
            .join(Listing, Listing.id == ReportOverdue.listing_id)
            .where(condition)
            .order_by(ReportOverdue.report_period.desc(), Listing.ticker)
            .limit(50)
        )
    ).all()
    return OverdueHealth(
        open=total,
        items=[
            OverdueItem(
                ticker=format_listing_ticker(ticker=ticker, market_code=market_code),
                report_period=row.report_period,
                deadline=row.deadline,
                detected_on=row.detected_on,
            )
            for row, ticker, market_code in rows
        ],
    )


# --------------------------------------------------------------------------- #
# Provider ledger
# --------------------------------------------------------------------------- #


async def provider_health(
    session: AsyncSession,
    *,
    now: datetime,
    circuit_states: Sequence[CircuitStatus] = (),
) -> list[ProviderHealth]:
    """24-hour ledger counts per provider merged with the breaker state (A2)."""

    stats = {
        stat.provider: stat
        for stat in await ProviderCallRepository(session).summary(now - _PROVIDER_WINDOW)
    }
    by_provider: dict[str, list[CircuitStatus]] = {}
    for status in circuit_states:
        by_provider.setdefault(status.provider, []).append(status)
    items: list[ProviderHealth] = []
    for provider in sorted(set(stats) | set(by_provider)):
        stat = stats.get(provider)
        states = by_provider.get(provider, [])
        open_entries = [state for state in states if state.is_open(now)]
        open_until = max(state.open_until for state in open_entries) if open_entries else None  # type: ignore[type-var]
        last_open_at = (
            max((state.last_failure_at for state in open_entries if state.last_failure_at), default=None)
            if open_entries
            else None
        )
        failed_states = [state for state in states if state.last_failure_at is not None]
        newest_state = (
            max(failed_states, key=lambda state: state.last_failure_at or now)
            if failed_states
            else None
        )
        if open_until is None and stat is not None and stat.last_open_until is not None:
            # No breaker in this process: trust the ledger's last "opened" row.
            if stat.last_open_until > now:
                open_until = stat.last_open_until
            last_open_at = stat.last_open_at
        last_error_at: datetime | None
        last_error: str | None
        if newest_state is not None and (
            stat is None
            or stat.last_error_at is None
            or (newest_state.last_failure_at or now) >= stat.last_error_at
        ):
            last_error_at = newest_state.last_failure_at
            last_error = f"{newest_state.endpoint}: {newest_state.last_error}"
        else:
            last_error_at = stat.last_error_at if stat is not None else None
            last_error = stat.last_error if stat is not None else None
        items.append(
            ProviderHealth(
                provider=provider,
                calls_24h=stat.calls if stat is not None else 0,
                failed_24h=stat.failed if stat is not None else 0,
                skipped_24h=stat.skipped if stat is not None else 0,
                open_until=open_until,
                last_error_at=last_error_at,
                last_error=last_error,
                last_open_at=last_open_at,
            )
        )
    return items
