"""Read-only assembly of one forward-validation report from the PIT store.

Design rules:

* **Members are the PIT constituents** of ``universe`` on ``as_of``
  (:meth:`UniverseRepository.members_as_of`), including names removed later, so
  survivorship bias is not re-introduced by reading today's index.
* **Scores** come from :func:`load_snapshot_view` + :func:`build_snapshot_bundle`
  + :func:`score_pool` with the reference timestamp set to ``as_of`` 23:59:59
  Asia/Shanghai. That is exactly the production scoring path; this module only
  feeds it and records what it saw.
* **Forward prices** read the stored backward-adjusted series through a fixed
  far-future knowledge sentinel (:data:`PRICE_KNOWLEDGE_SENTINEL`): the rows
  were final once their session closed, so the run date must not change a
  historical report.
* **Nothing is written.** The caller supplies a session; the CLI opens SQLite
  with ``mode=ro`` and ``PRAGMA query_only``.
* A lookahead guard compares the latest input date against ``as_of`` and
  aborts the run rather than silently using later data.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.snapshot_model import SnapshotView
from investment_agent.factors.view_db import load_snapshot_view
from investment_agent.research.forward_validation import prices
from investment_agent.research.forward_validation.benchmark import (
    coverage_window,
    empty_benchmark,
    equal_weight_benchmark,
    market_sessions,
)
from investment_agent.research.forward_validation.contracts import (
    FORMULA_VERSION,
    PRICE_KNOWLEDGE_SENTINEL,
    LookaheadError,
    PriceSeries,
    ScoredObservation,
    ValidationConfig,
)
from investment_agent.research.forward_validation.evaluation import (
    build_report,
    scoring_metadata,
)
from investment_agent.research.snapshot_source import (
    SnapshotDataMissingError,
    build_snapshot_bundle,
)
from investment_agent.research_kernel import __version__ as kernel_version
from investment_agent.research_kernel.scoring import SCORING_VERSION, score_pool
from investment_agent.rules.schema import RuleTemplate
from investment_agent.rules.screen import ScreenConfig, ScreenMember, required_factors, run_screen
from investment_agent.rules.store import RuleStore
from investment_agent.rules.templates import load_templates
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.reference import Company
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.universe import UniverseRepository

#: Calendar days of history loaded for the adjusted series (covers a 6-month
#: horizon plus entry deferral and the long holidays around it).
FORWARD_HISTORY_DAYS = 420
#: Calendar days of history used to build the session list.
SESSION_HISTORY_DAYS = 640
_PRICE_CAPTURED_AT = datetime.fromisoformat(PRICE_KNOWLEDGE_SENTINEL)


@dataclass(frozen=True)
class LoadedDataset:
    report: dict[str, Any]
    observations: tuple[ScoredObservation, ...]
    sessions: tuple[date, ...]


def _as_of_reference(as_of: date) -> datetime:
    """The scoring ``as_of`` timestamp: end of the business day, Shanghai."""

    return datetime.combine(as_of, time(23, 59, 59), SHANGHAI_TZ)


def _value_date(iso_value: str | None) -> date | None:
    if not iso_value:
        return None
    try:
        parsed = datetime.fromisoformat(str(iso_value).replace("Z", "+00:00"))
    except ValueError:
        try:
            return date.fromisoformat(str(iso_value)[:10])
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(SHANGHAI_TZ).date()


def _industry_group_label(view: SnapshotView) -> str:
    """Stable industry bucket for the group table (industry or fallback flag)."""

    group = view.industry_group
    return f"{group}:fallback" if view.industry_fallback else group


def _max_input_date(view: SnapshotView, item: Mapping[str, Any]) -> date | None:
    """Latest date visible in the scoring inputs (must be ``<= as_of``)."""

    dates: list[date] = [view.as_of]
    bar = view.latest_raw_bar()
    if bar is not None:
        dates.append(bar.trade_date)
    latest = view.latest_annual()
    if latest is not None and latest.available_from is not None:
        dates.append(latest.available_from)
    for key in ("stock", "market_data", "financial_data"):
        block = item.get("updated_at")
        if isinstance(block, Mapping):
            found = _value_date(block.get(key))
            if found is not None:
                dates.append(found)
    return max(dates) if dates else None


def _quality_signals(item: Mapping[str, Any]) -> tuple[str, ...]:
    signals = item.get("quality_signals") or []
    flags = sorted(
        {
            str(signal.get("flag"))
            for signal in signals
            if isinstance(signal, Mapping) and signal.get("flag")
        }
    )
    return tuple(flags)


def _price_series(
    *,
    ticker: str,
    raw: Sequence[MarketSnapshot],
    adjusted: Sequence[MarketSnapshot],
) -> PriceSeries:
    raw_open = {row.trade_date: float(row.open) for row in raw if row.open is not None}
    raw_close = {row.trade_date: float(row.close) for row in raw if row.close is not None}
    adjusted_open = {row.trade_date: float(row.open) for row in adjusted if row.open is not None}
    adjusted_close = {row.trade_date: float(row.close) for row in adjusted if row.close is not None}
    volume = {row.trade_date: float(row.volume) for row in raw if row.volume is not None}
    trade_status = {row.trade_date: row.trade_status for row in raw}
    is_st = {row.trade_date: row.is_st for row in raw}
    source = "backward" if adjusted else "raw_fallback"
    if not adjusted:
        adjusted_open, adjusted_close = dict(raw_open), dict(raw_close)
    return PriceSeries(
        ticker=ticker,
        raw_open=raw_open,
        raw_close=raw_close,
        adjusted_open=adjusted_open,
        adjusted_close=adjusted_close,
        volume=volume,
        trade_status=trade_status,
        is_st=is_st,
        adjusted_source=source,
    )


async def _load_series(
    session: AsyncSession,
    *,
    listing_id: int,
    ticker: str,
    start: date,
    end: date,
    include_backfill: bool,
) -> PriceSeries:
    repository = MarketSnapshotRepository(session)
    raw = await repository.series_as_of(
        listing_id,
        start,
        end,
        _PRICE_CAPTURED_AT,
        adjust="none",
        provider="baostock",
        include_backfill=include_backfill,
    )
    adjusted = await repository.series_as_of(
        listing_id,
        start,
        end,
        _PRICE_CAPTURED_AT,
        adjust="backward",
        provider="baostock",
        include_backfill=include_backfill,
    )
    return _price_series(ticker=ticker, raw=raw, adjusted=adjusted)


async def _load_sessions(session: AsyncSession, start: date, end: date) -> list[date]:
    stmt = (
        select(MarketSnapshot.trade_date)
        .where(
            MarketSnapshot.adjust == "none",
            MarketSnapshot.provider == "baostock",
            MarketSnapshot.trade_date >= start,
            MarketSnapshot.trade_date <= end,
        )
        .distinct()
        .order_by(MarketSnapshot.trade_date)
    )
    return [row[0] for row in (await session.execute(stmt)).all()]


async def _load_templates(
    session: AsyncSession, config: ValidationConfig
) -> tuple[list[RuleTemplate], dict[str, Any]]:
    if config.template_source == "files":
        loaded = load_templates()
        templates: list[RuleTemplate] = []
        refs: dict[str, str] = {}
        for path, template in loaded:
            templates.append(template)
            refs[template.ref] = str(path)
        selected = _select_templates(templates, config)
        return selected, {
            "source": "files",
            "template_refs": [template.ref for template in selected],
            "paths": {template.ref: refs.get(template.ref) for template in selected},
            "calibration_status": {
                template.ref: template.calibration_status for template in selected
            },
        }
    store = RuleStore(session)
    templates = await store.latest_templates()
    selected = _select_templates(templates, config)
    hashes: dict[str, str] = {}
    for template in selected:
        record = await store.latest_template_record(template.id)
        if record is not None and record.version == template.version:
            hashes[template.ref] = record.hash
    return selected, {
        "source": "db",
        "template_refs": [template.ref for template in selected],
        "hashes": hashes,
        "calibration_status": {
            template.ref: template.calibration_status for template in selected
        },
    }


def _select_templates(
    templates: Iterable[RuleTemplate], config: ValidationConfig
) -> list[RuleTemplate]:
    items = sorted(templates, key=lambda template: (template.id, template.version))
    if not config.template_refs:
        return items
    wanted = set(config.template_refs)
    return [template for template in items if template.ref in wanted or template.id in wanted]


async def load_dataset(session: AsyncSession, config: ValidationConfig) -> LoadedDataset:
    """Build the report and observations from a read-only session."""

    templates, template_meta = await _load_templates(session, config)
    screen_config = ScreenConfig()
    needed = sorted(
        set(required_factors(templates, screen_config)) | set(DEFAULT_REGISTRY.names())
    )
    first = min(config.as_of_dates) - timedelta(days=SESSION_HISTORY_DAYS)
    last = max(config.as_of_dates) + timedelta(days=FORWARD_HISTORY_DAYS)
    sessions = market_sessions(await _load_sessions(session, first, last))
    session_list = list(sessions)

    observations: list[ScoredObservation] = []
    failures: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {}
    series_cache: dict[str, PriceSeries] = {}
    missing_members: list[str] = []
    members_by_as_of: dict[date, list[str]] = {}
    for as_of in config.as_of_dates:
        members = await UniverseRepository(session).members_as_of(config.universe, as_of)
        members_by_as_of[as_of] = [member.ticker for member in members]
        diagnostics.setdefault(as_of.isoformat(), {})
        for member in members:
            if member.ticker not in series_cache:
                series_cache[member.ticker] = await _load_series(
                    session,
                    listing_id=member.listing_id,
                    ticker=member.ticker,
                    start=first,
                    end=last,
                    include_backfill=config.include_backfill,
                )

    for as_of in config.as_of_dates:
        members = await UniverseRepository(session).members_as_of(config.universe, as_of)
        diag = diagnostics[as_of.isoformat()]
        diag.update({"members": len(members), "missing_listing": 0, "failed": 0})
        scored_items: list[ScoredObservation] = []
        screen_members: list[ScreenMember] = []
        for member in members:
            listing = await ListingRepository(session).get_a_share_listing(member.ticker)
            if listing is None:
                missing_members.append(member.ticker)
                diag["missing_listing"] += 1
                scored_items.append(
                    _unscored(
                        member.ticker,
                        member.listing_id,
                        as_of,
                        config,
                        series_cache.get(member.ticker),
                        status="missing_listing",
                        failure="listing_not_found",
                    )
                )
                continue
            try:
                view = await load_snapshot_view(session, member.ticker, as_of)
                company = await session.get(Company, listing.company_id)
                bundle = build_snapshot_bundle(
                    view,
                    company_name=company.canonical_name if company is not None else member.ticker,
                    industry=None,
                    retrieved_at=_as_of_reference(as_of).isoformat(),
                )
                result = score_pool(
                    bundle.pool,
                    bundle.market,
                    bundle.financial,
                    pool_name=config.universe,
                    as_of=_as_of_reference(as_of),
                )
                (item,) = result["items"]
                if result["metadata"]["scoring_version"] != SCORING_VERSION:
                    raise LookaheadError(
                        "score_pool returned a scoring version different from the module"
                    )
                latest_input = _max_input_date(view, item)
                if latest_input is not None and latest_input > as_of:
                    raise LookaheadError(
                        f"{member.ticker}: input dated {latest_input.isoformat()} "
                        f"is later than as_of {as_of.isoformat()}"
                    )
                diagnostics.setdefault(as_of.isoformat(), {}).setdefault(
                    "scoring_input_dates", {}
                )[member.ticker] = (
                    latest_input.isoformat() if latest_input is not None else None
                )
                factors = DEFAULT_REGISTRY.compute_all(view, needed)
                score = item.get("investment_score")
                scored_items.append(
                    ScoredObservation(
                        ticker=member.ticker,
                        listing_id=member.listing_id,
                        as_of=as_of,
                        signal_session=_signal_session(session_list, as_of, series_cache[member.ticker]),
                        industry_group=_industry_group_label(view),
                        investment_score=None if score is None else float(score),
                        factor_coverage=item.get("factor_coverage"),
                        needs_agent_review=bool(item.get("needs_agent_review")),
                        score_suppression_reason=item.get("score_suppression_reason"),
                        selected_model=item.get("selected_model"),
                        quality_signals=_quality_signals(item),
                        score_status="scored" if score is not None else "suppressed",
                        score_failure=None,
                        outcomes=_outcomes(
                            series_cache[member.ticker],
                            session_list,
                            as_of,
                            config,
                            signal_session=_signal_session(
                                session_list, as_of, series_cache[member.ticker]
                            ),
                        ),
                        max_input_date=latest_input,
                    )
                )
                screen_members.append(
                    ScreenMember(
                        listing_id=member.listing_id,
                        ticker=member.ticker,
                        industry_group=_industry_group_label(view),
                        factors=factors,
                        context={"as_of": as_of.isoformat()},
                    )
                )
            except (LookupError, SnapshotDataMissingError) as exc:
                diag["failed"] += 1
                failures.append(
                    {
                        "as_of": as_of.isoformat(),
                        "ticker": member.ticker,
                        "stage": "scoring",
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
                scored_items.append(
                    _unscored(
                        member.ticker,
                        member.listing_id,
                        as_of,
                        config,
                        series_cache.get(member.ticker),
                        status="failed",
                        failure=str(exc),
                    )
                )
        _attach_template_hits(
            scored_items,
            screen_members,
            templates,
            screen_config,
            as_of,
            diagnostics[as_of.isoformat()],
        )
        observations.extend(scored_items)

    if config.benchmark == "equal_weight":
        benchmark, benchmark_detail = equal_weight_benchmark(
            sessions=session_list,
            as_of_dates=config.as_of_dates,
            series=series_cache,
            members_by_as_of=members_by_as_of,
            cost=config.cost,
            horizons=config.horizons,
            max_entry_deferral=config.max_entry_deferral,
            max_exit_deferral=config.max_exit_deferral,
        )
    else:
        benchmark = empty_benchmark(
            "csi300", "no stored CSI300 index price series; benchmark unavailable"
        )
        benchmark_detail = {
            "note": "a CSI300 benchmark needs a stored index series, which this store lacks"
        }
    coverage = coverage_window(
        session_list,
        starts=config.as_of_dates,
        horizon_months=config.horizons,
    )
    source_breakdown = _source_breakdown(series_cache)
    market_meta = {
        "provider": "baostock",
        "adjust": "backward",
        "adjust_fallback": "raw when no backward series is stored",
        "knowledge_sentinel": PRICE_KNOWLEDGE_SENTINEL,
        "history_days": {"sessions": SESSION_HISTORY_DAYS, "forward": FORWARD_HISTORY_DAYS},
        "coverage": coverage,
        "series_sources": source_breakdown,
        "benchmark_detail": benchmark_detail,
        "sessions": [day.isoformat() for day in session_list],
    }
    scoring_meta = scoring_metadata(SCORING_VERSION)
    scoring_meta["kernel_version"] = kernel_version
    report = build_report(
        config=config,
        observations=observations,
        benchmark=benchmark,
        sessions=session_list,
        market_data=market_meta,
        scoring=scoring_meta,
        templates=template_meta,
        diagnostics=diagnostics,
        failures=failures,
        code_version=_code_version(),
    )
    report["members_by_as_of"] = {
        key.isoformat(): value for key, value in sorted(members_by_as_of.items())
    }
    report["missing_listings"] = sorted(set(missing_members))
    return LoadedDataset(
        report=report,
        observations=tuple(observations),
        sessions=tuple(session_list),
    )


def _signal_session(sessions: Sequence[date], as_of: date, series: PriceSeries) -> date:
    """Latest stored market session on or before ``as_of``.

    If the stock itself has no bar on that session (suspension), the signal
    session is still the market session; the tradability rules decide entry.
    """

    index = prices.session_on_or_before(list(sessions), as_of)
    if index is not None:
        return sessions[index]
    earlier = [day for day in series.raw_close if day <= as_of]
    return max(earlier) if earlier else as_of


def _outcomes(
    series: PriceSeries,
    sessions: Sequence[date],
    as_of: date,
    config: ValidationConfig,
    *,
    signal_session: date,
) -> dict[int, Any]:
    session_list = list(sessions)
    result: dict[int, Any] = {}
    for months in config.horizons:
        result[months] = prices.compute_outcome(
            series,
            session_list,
            signal_session,
            months,
            config.cost,
            max_entry_deferral=config.max_entry_deferral,
            max_exit_deferral=config.max_exit_deferral,
        )
    return result


def _unscored(
    ticker: str,
    listing_id: int,
    as_of: date,
    config: ValidationConfig,
    series: PriceSeries | None,
    *,
    status: str,
    failure: str,
) -> ScoredObservation:
    outcomes: dict[int, Any] = {}
    for months in config.horizons:
        outcomes[months] = _pending_outcome(months, failure)
    return ScoredObservation(
        ticker=ticker,
        listing_id=listing_id,
        as_of=as_of,
        signal_session=max(series.raw_close) if series and series.raw_close else as_of,
        industry_group="unknown",
        investment_score=None,
        factor_coverage=None,
        needs_agent_review=True,
        score_suppression_reason=None,
        selected_model=None,
        quality_signals=(),
        score_status=status,
        score_failure=failure,
        outcomes=outcomes,
        max_input_date=None,
    )


def _pending_outcome(months: int, reason: str) -> Any:
    from investment_agent.research.forward_validation.contracts import HorizonOutcome

    return HorizonOutcome(months=months, status="no_signal_session", reason=reason)


def _attach_template_hits(
    observations: list[ScoredObservation],
    screen_members: Sequence[ScreenMember],
    templates: Sequence[RuleTemplate],
    screen_config: ScreenConfig,
    as_of: date,
    diag: dict[str, Any],
) -> None:
    if not templates or not screen_members:
        diag["templates"] = {"as_of": as_of.isoformat(), "hits": {}, "reason": "no templates"}
        return
    try:
        run = run_screen(
            as_of=as_of,
            members=list(screen_members),
            templates=list(templates),
            config=screen_config,
        )
    except Exception as exc:  # noqa: BLE001 - reported, never fatal to scoring
        diag["templates"] = {
            "as_of": as_of.isoformat(),
            "hits": {},
            "reason": f"{type(exc).__name__}: {exc}",
        }
        return
    hit_map: dict[str, list[tuple[str, float | None]]] = {}
    for template in run.templates:
        for member in template.members:
            if member.hit:
                hit_map.setdefault(member.ticker, []).append(
                    (template.template_ref, member.strength)
                )
    for position, item in enumerate(list(observations)):
        hits = hit_map.get(item.ticker, [])
        observations[position] = replace(
            item,
            template_hits=tuple(ref for ref, _ in hits),
            template_strength={ref: strength for ref, strength in hits if strength is not None},
        )
    counts: dict[str, int] = {}
    for hits in hit_map.values():
        for ref, _ in hits:
            counts[ref] = counts.get(ref, 0) + 1
    diag["templates"] = {
        "as_of": as_of.isoformat(),
        "hits": counts,
        "template_versions": {template.ref: template.calibration_status for template in templates},
        "reason": None,
    }


def _source_breakdown(series: Mapping[str, PriceSeries]) -> dict[str, int]:
    counts: dict[str, int] = {"backward": 0, "raw_fallback": 0}
    for item in series.values():
        counts[item.adjusted_source] = counts.get(item.adjusted_source, 0) + 1
    return counts


def _code_version() -> dict[str, Any]:
    return {
        "package_version": FORMULA_VERSION,
        "research_kernel": kernel_version,
        "scoring_version": SCORING_VERSION,
        "python_module_hash": _module_hash(),
    }


def _module_hash() -> str:
    """Hash of the package sources, so a changed formula is visible."""

    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


__all__ = ["FORWARD_HISTORY_DAYS", "SESSION_HISTORY_DAYS", "LoadedDataset", "load_dataset"]
