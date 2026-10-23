"""Shared read-model helpers of the HTTP API and the MCP transport (D4).

The serialization converters and read queries below used to live inside the
FastAPI route modules and were imported from there by
:mod:`investment_agent.mcp_server`. That made the stdio MCP transport depend
on the HTTP transport: importing a tool pulled in route modules, their routers
and their FastAPI dependencies. Everything both transports share lives here
instead; the route modules import these names and *re-export* them, so any
existing importer (web layer, other routes, tests) keeps working, while
``mcp_server`` never imports ``api.routes``.

Nothing here registers a route or imports FastAPI: every helper takes a
session plus the values it needs and returns schema or ORM objects.

One function-level borrow remains: the calendar-warning part of
:func:`health_settings` / :func:`_calendar` still imports ``calendar_warnings``
and ``_calendar_path`` from :mod:`investment_agent.api.routes.dashboard`, which
is where that logic is defined and which is outside this module's scope. The
imports are deliberately *function-level*: importing this module (and hence
the MCP server) must not pull in route modules.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.schemas.admin import (
    WatchlistItemOut,
    WatchlistLatestResearch,
    WatchlistRuleOut,
)
from investment_agent.api.schemas.candidates import (
    CandidateDetailOut,
    CandidateSourceOut,
    CandidateSummaryOut,
    DecisionOut,
    ParamRangeOut,
    RuleDetailOut,
    RuleSummaryOut,
    ScreenReasonOut,
    ScreenResultOut,
    SegmentOut,
)
from investment_agent.candidates.lifecycle import (
    context_with_industry_name,
    effective_draft,
    latest_screen_results,
    rule_params,
)
from investment_agent.clock import shanghai_today, today_in
from investment_agent.config import Settings
from investment_agent.directory_search import snapshot_universes
from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
from investment_agent.domain.time import (
    as_utc as _as_utc,
)
from investment_agent.domain.time import (
    optional_utc as _optional_utc,
)
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.field_sources import default_field_sources, market_precedence
from investment_agent.rules.drafting import load_stock_context
from investment_agent.rules.screen import evaluate_zone_conditions
from investment_agent.rules.store import instance_from_record, template_from_record
from investment_agent.rules.validation import effective_params
from investment_agent.rules.zone_detection import CANDIDATE_SCOPE, zone_summaries
from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    load_trading_calendar,
)
from investment_agent.snapshots.health import (
    DataHealth,
    HealthSettings,
    HealthWarning,
    build_data_health,
)
from investment_agent.storage.models.candidates import CandidateRecord, DecisionRecord
from investment_agent.storage.models.pit_snapshot import (
    MarketSnapshot,
)
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.rules import (
    RuleInstanceRecord,
    RuleTemplateRecord,
    ScreenResultRecord,
)
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.candidate import DecisionRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.research_change import (
    ResearchChangeRepository,
)
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
    score_item_from_payload,
)
from investment_agent.watchlist.drafts import rule_snapshot

log = structlog.get_logger(__name__)


# --------------------------------------------------------------------------- #
# Market bars (GET /market/{ticker}/bars and the MCP get_bars tool)
# --------------------------------------------------------------------------- #

BARS_PROVIDERS: tuple[str, ...] = market_precedence(default_field_sources())
"""Provider precedence of the series (``baostock`` first, then ``tencent``)."""
BARS_PROVIDER = ",".join(BARS_PROVIDERS)
BARS_ADJUST = "none"
# Trading days make up roughly 68% of calendar days; the lookback window is
# generous so ``days`` trading days always fit, then the tail is sliced.
_CALENDAR_DAYS_PER_BAR = 2
_WINDOW_SLACK_DAYS = 30


async def load_bars(
    session: AsyncSession, listing_id: int, days: int, *, as_of: datetime
) -> list[MarketSnapshot]:
    """The last ``days`` uncorrected bars of ``listing_id`` known at ``as_of``,
    oldest first, in the registry's provider precedence (shared by the API
    and the MCP ``get_bars`` tool)."""

    end = shanghai_today()
    start = end - timedelta(days=days * _CALENDAR_DAYS_PER_BAR + _WINDOW_SLACK_DAYS)
    rows = await MarketSnapshotRepository(session).series_as_of(
        listing_id,
        start,
        end,
        as_of,
        adjust=BARS_ADJUST,
        provider=BARS_PROVIDERS,
    )
    return rows[-days:]


# --------------------------------------------------------------------------- #
# Candidate / rule converters
# (GET /candidates, /rules, /screen-runs and the MCP candidate tools)
# --------------------------------------------------------------------------- #


class TemplateCache:
    """Per-request cache of template records (also used by the MCP tools)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self._cache: dict[tuple[str, int], RuleTemplateRecord | None] = {}

    async def get(self, template_id: str, version: int) -> RuleTemplateRecord | None:
        key = (template_id, version)
        if key not in self._cache:
            self._cache[key] = await self.session.get(RuleTemplateRecord, key)
        return self._cache[key]

    async def calibration(self, template_id: str | None, version: int | None) -> str | None:
        if template_id is None or version is None:
            return None
        record = await self.get(template_id, version)
        return record.calibration_status if record is not None else None


def _factor_unit(name: object) -> str | None:
    if isinstance(name, str) and name in DEFAULT_REGISTRY:
        return DEFAULT_REGISTRY.get(name).unit
    return None


def _reasons(raw: list[dict[str, Any]] | None) -> list[ScreenReasonOut]:
    reasons: list[ScreenReasonOut] = []
    for item in raw or []:
        reason = ScreenReasonOut.model_validate(item)
        if reason.unit is None:
            reason.unit = _factor_unit(reason.factor)
        reasons.append(reason)
    return reasons


def _zone_factors(node: Any) -> list[str]:
    if isinstance(node, dict):
        if isinstance(node.get("factor"), str):
            return [node["factor"]]
        found: list[str] = []
        for key in ("all", "any"):
            for child in node.get(key) or []:
                found += _zone_factors(child)
        return found
    return []


async def _screen_result_out(row: ScreenResultRecord, templates: TemplateCache) -> ScreenResultOut:
    return ScreenResultOut(
        screen_result_id=row.id,
        screen_run_id=row.screen_run_id,
        as_of=row.as_of,
        template_id=row.template_id,
        template_version=row.template_version,
        template_ref=f"{row.template_id}@{row.template_version}",
        calibration_status=await templates.calibration(row.template_id, row.template_version),
        outcome=row.outcome,
        rank=row.rank,
        reasons=_reasons(row.reasons),
        context=context_with_industry_name(row.context),
    )


async def _rule_summary(rule: RuleInstanceRecord, templates: TemplateCache) -> RuleSummaryOut:
    return RuleSummaryOut(
        rule_instance_id=rule.id,
        listing_id=rule.listing_id,
        ticker=rule.symbol,
        scope=rule.scope,
        template_id=rule.template_id,
        template_version=rule.template_version,
        template_ref=f"{rule.template_id}@{rule.template_version}",
        calibration_status=await templates.calibration(rule.template_id, rule.template_version),
        version=rule.version,
        status=rule.status,
        params=rule_params(rule),
        drafted_by=rule.drafted_by,
    )


async def rule_detail_out(rule: RuleInstanceRecord, templates: TemplateCache) -> RuleDetailOut:
    summary = await _rule_summary(rule, templates)
    record = await templates.get(rule.template_id, rule.template_version)
    template_params: dict[str, float] = {}
    ranges: dict[str, ParamRangeOut] = {}
    effective: dict[str, float] = dict(summary.params)
    definition: dict[str, Any] = dict(rule.definition or {})
    raw_zones = definition.get("zones")
    zones: dict[str, Any] = dict(raw_zones) if isinstance(raw_zones, dict) else {}
    if record is not None:
        template = template_from_record(record)
        if not zones:
            zones = {
                name: group.model_dump(mode="json", by_alias=True, exclude_none=True)
                for name, group in template.zones_for(rule.scope).items()
            }
        template_params = dict(template.params)
        ranges = {
            name: ParamRangeOut(min=item.min, max=item.max)
            for name, item in template.param_ranges.items()
        }
        effective = effective_params(template) | summary.params
    return RuleDetailOut(
        **summary.model_dump(),
        definition=definition,
        draft_rationale=rule.draft_rationale,
        approved_by=rule.approved_by,
        approved_at=_optional_utc(rule.approved_at),
        retired_at=_optional_utc(rule.retired_at),
        created_at=_as_utc(rule.created_at),
        template_params=template_params,
        param_ranges=ranges,
        effective_params=effective,
        zones=zones,
        factor_units={
            name: unit
            for name in dict.fromkeys(_zone_factors({"all": list(zones.values())}))
            if (unit := _factor_unit(name)) is not None
        },
    )


def _decision_out(decision: DecisionRecord) -> DecisionOut:
    return DecisionOut(
        id=decision.id,
        kind=decision.kind,
        actor=decision.actor,
        ticker=decision.ticker,
        reason_text=decision.reason_text,
        rule_instance_id=decision.rule_instance_id,
        rule_ref=decision.rule_ref,
        candidate_id=decision.candidate_id,
        at=_as_utc(decision.at),
        context=dict(decision.context or {}),
    )


async def _company_name(session: AsyncSession, listing_id: int) -> str | None:
    listing = await session.get(Listing, listing_id)
    if listing is None:
        return None
    company = await session.get(Company, listing.company_id)
    return company.canonical_name if company is not None else None


def _overall_rationale(raw: str | None) -> str | None:
    """A draft's overall analysis: ``overall_rationale`` of the rationale JSON;
    a plain-text rationale (manual drafts) passes through as-is."""

    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if isinstance(data, dict):
        overall = data.get("overall_rationale")
        if isinstance(overall, str) and overall.strip():
            return overall.strip()
    return None


async def _draft_checks(
    session: AsyncSession,
    candidate: CandidateRecord,
    draft: RuleInstanceRecord,
    templates: TemplateCache,
) -> list[dict[str, Any]]:
    """The draft rule's candidate zones evaluated against the stock's current
    factors - lets a manual nomination show the same per-factor checks as a
    screen hit. Empty on any data problem; never breaks the list."""

    try:
        record = await templates.get(draft.template_id, draft.template_version)
        if record is None:
            return []
        template = template_from_record(record)
        rule = instance_from_record(draft)
        zones = rule.zones or template.zones_for(rule.scope)
        if not zones:
            return []
        params = effective_params(template) | rule_params(draft)
        context = await load_stock_context(session, candidate.ticker, shanghai_today())
        checks = evaluate_zone_conditions(zones=zones, factors=context.factors, params=params)
    except Exception:
        log.warning(
            "candidate.draft_checks_failed", candidate_id=candidate.id, exc_info=True
        )
        return []
    return [reason for name in sorted(checks) for reason in checks[name]]


async def _summary_parts(
    session: AsyncSession, candidate: CandidateRecord, templates: TemplateCache
) -> tuple[dict[str, Any], list[ScreenResultRecord], RuleInstanceRecord | None]:
    results = await latest_screen_results(session, candidate)
    draft_state, draft = await effective_draft(session, candidate)
    sources = [CandidateSourceOut.model_validate(item) for item in candidate.sources or []]
    refs = list(
        dict.fromkeys(
            f"{item.template_id}@{item.template_version}"
            for item in sources
            if item.template_id is not None and item.template_version is not None
        )
    )
    newest = next(
        (item for item in reversed(sources) if item.template_id and item.template_version),
        None,
    )
    draft_checks: list[dict[str, Any]] = []
    if not results and draft is not None:
        draft_checks = await _draft_checks(session, candidate, draft, templates)
    # The daily zone detection's record of this listing's approved rule (WP8):
    # the page shows which zone it is in, and with which rule version.
    zone = (
        await zone_summaries(session, listing_ids=[candidate.listing_id], scope=CANDIDATE_SCOPE)
    ).get(candidate.listing_id)
    fields: dict[str, Any] = {
        "id": candidate.id,
        "listing_id": candidate.listing_id,
        "ticker": candidate.ticker,
        "name": await _company_name(session, candidate.listing_id),
        "status": candidate.status,
        "opened_at": _as_utc(candidate.opened_at),
        "expires_at": _as_utc(candidate.expires_at),
        "closed_at": _optional_utc(candidate.closed_at),
        "sources": sources,
        "template_refs": refs,
        "calibration_status": (
            await templates.calibration(newest.template_id, newest.template_version)
            if newest is not None
            else None
        ),
        "latest_screen": await _screen_result_out(results[0], templates) if results else None,
        "draft_state": draft_state,
        "draft_rule": await _rule_summary(draft, templates) if draft is not None else None,
        "draft_overall_rationale": _overall_rationale(draft.draft_rationale) if draft is not None else None,
        "draft_checks": _reasons(draft_checks),
        "zone_events": zone,
        "note": candidate.note,
    }
    return fields, results, draft


async def candidate_summary_out(
    session: AsyncSession, candidate: CandidateRecord, templates: TemplateCache
) -> CandidateSummaryOut:
    fields, _results, _draft = await _summary_parts(session, candidate, templates)
    return CandidateSummaryOut(**fields)


async def _segments_out(session: AsyncSession, candidate: CandidateRecord) -> list[SegmentOut]:
    """Latest 主营构成 of the listing (facts; the same loader as the drafting prompt)."""

    from investment_agent.rules.drafting import load_segments

    return [
        SegmentOut(
            report_period=item.report_period,
            kind=item.kind,
            name=item.name,
            revenue=item.revenue,
            share=item.share,
            margin=item.margin,
            profit=item.profit,
        )
        for item in await load_segments(session, candidate.ticker, shanghai_today())
    ]


async def candidate_detail_out(session: AsyncSession, candidate: CandidateRecord) -> CandidateDetailOut:
    templates = TemplateCache(session)
    fields, results, draft = await _summary_parts(session, candidate, templates)
    decisions = await DecisionRepository(session).for_candidate(candidate.id)
    return CandidateDetailOut(
        **fields,
        screen_results=[await _screen_result_out(row, templates) for row in results],
        draft_rule_detail=await rule_detail_out(draft, templates) if draft is not None else None,
        decisions=[_decision_out(item) for item in decisions],
        segments=await _segments_out(session, candidate),
    )


# --------------------------------------------------------------------------- #
# Watchlist rows (GET /watchlist and the MCP list_watchlist tool)
# --------------------------------------------------------------------------- #


async def watchlist_item_out(session: AsyncSession, item: WatchlistItem) -> WatchlistItemOut:
    """One watchlist row with its company name and newest research summary
    (shared by ``GET /watchlist`` and the MCP ``list_watchlist`` tool)."""

    state = inspect(item)
    if "updated_at" in state.expired_attributes:
        # ``onupdate=func.now()`` expires the column after an UPDATE flush;
        # load it explicitly so serialization never triggers a lazy IO.
        await session.refresh(item, attribute_names=["updated_at"])
    listing = await session.get(Listing, item.listing_id)
    if listing is None:
        raise ValueError(f"Listing {item.listing_id} not found")
    company = await session.get(Company, listing.company_id)
    if company is None:
        raise ValueError(f"Company {listing.company_id} not found")
    latest = await _latest_research(
        session,
        symbol=listing.ticker,
        market_code=listing.market_code,
    )
    return WatchlistItemOut(
        listing_id=item.listing_id,
        ticker=format_listing_ticker(
            ticker=listing.ticker,
            market_code=listing.market_code,
        ),
        market_code=listing.market_code,
        name=company.canonical_name,
        industry=item.industry,
        provider=item.provider,
        report_year=item.report_year,
        enabled=item.enabled,
        added_at=_as_utc(item.added_at),
        updated_at=_as_utc(item.updated_at),
        note=item.note,
        last_refresh_status=item.last_refresh_status,
        last_refreshed_at=(
            _as_utc(item.last_refreshed_at)
            if item.last_refreshed_at is not None
            else None
        ),
        last_refresh_error=item.last_refresh_error,
        last_research_run_id=item.last_research_run_id,
        latest_research=latest,
        rule=WatchlistRuleOut.model_validate(await rule_snapshot(session, item)),
    )


async def _latest_research(
    session: AsyncSession,
    *,
    symbol: str,
    market_code: str,
) -> WatchlistLatestResearch | None:
    """Summarize the newest snapshot and its change set for one listing."""

    parsed = parse_a_share_ticker(
        format_listing_ticker(ticker=symbol, market_code=market_code)
    )
    if parsed is None:
        return None
    suffix = parsed[1].suffix
    history = await ResearchSnapshotRepository(session).history(
        symbol=symbol,
        market=suffix,
        limit=1,
    )
    if not history:
        return None
    snapshot = history[0]
    change_set = await ResearchChangeRepository(session).by_snapshot(snapshot.id)
    score = score_item_from_payload(snapshot.score_json)
    freshness = score.get("freshness")
    freshness_ok: bool | None = None
    if isinstance(freshness, dict) and freshness:
        freshness_ok = all(
            block.get("is_stale") is False
            for block in freshness.values()
            if isinstance(block, dict)
        )
    return WatchlistLatestResearch(
        run_id=snapshot.pipeline_run_id,
        as_of=snapshot.as_of,
        strategy_label=_optional_str(score.get("strategy_label")),
        investment_score=_optional_float(score.get("investment_score")),
        research_confidence_score=_optional_float(score.get("research_confidence_score")),
        freshness_ok=freshness_ok,
        needs_agent_review=(
            score["needs_agent_review"]
            if isinstance(score.get("needs_agent_review"), bool)
            else None
        ),
        scoring_version=snapshot.scoring_version,
        changed_at=(
            _as_utc(change_set.notified_at if change_set.notified_at is not None else change_set.created_at)
            if change_set is not None
            else None
        ),
        meaningful_change_count=(
            change_set.meaningful_change_count if change_set is not None else None
        ),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


# --------------------------------------------------------------------------- #
# Data health (GET /health/data and the MCP get_data_health tool)
# --------------------------------------------------------------------------- #

_TIMEZONE = "Asia/Shanghai"


def _calendar() -> TradingCalendar | None:
    # Borrowed from the dashboard route module (see the module docstring).
    from investment_agent.api.routes.dashboard import _calendar_path

    try:
        return load_trading_calendar(_calendar_path())
    except Exception:
        log.warning("data_health.calendar_unavailable")
        return None


def health_settings(
    settings: Settings,
    *,
    universes: tuple[str, ...],
    include_calendar_warnings: bool = True,
) -> HealthSettings:
    warnings: tuple[HealthWarning, ...] = ()
    if include_calendar_warnings:
        # Borrowed from the dashboard route module (see the module docstring).
        from investment_agent.api.routes.dashboard import calendar_warnings

        warnings = tuple(
            HealthWarning(
                code=item.code, level=item.level, message=item.message, section="calendar"
            )
            for item in calendar_warnings(data_dir=settings.data_dir)
        )
    return HealthSettings(
        universes=universes,
        threshold=settings.snapshot_completeness_threshold,
        budget_cap=settings.ifind_budget_per_5h,
        budget_window=timedelta(hours=settings.ifind_budget_window_hours),
        calendar=_calendar(),
        calendar_warnings=warnings,
    )


async def compute_data_health(
    session: AsyncSession, settings: Settings, *, include_calendar_warnings: bool = True
) -> DataHealth:
    return await build_data_health(
        session,
        health_settings(
            settings,
            universes=await snapshot_universes(session, settings),
            include_calendar_warnings=include_calendar_warnings,
        ),
        now=datetime.now(UTC),
        today=today_in(_TIMEZONE),
    )

