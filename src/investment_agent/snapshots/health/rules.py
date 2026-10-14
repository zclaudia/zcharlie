"""Warning rules of the data health read model.

Pure functions over an already-assembled :class:`WarningsInput`: no session, no
SQL and no import from :mod:`investment_agent.storage` - the rules only decide
the level / code / message of every :class:`HealthWarning` (see the package
docstring and README "数据健康").
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.snapshots.collect import QUOTA_STOP_REASON
from investment_agent.snapshots.health.models import (
    _KIND_LABELS,
    DIRECTORY_STALE_DAYS,
    Accumulation,
    CollectionHealth,
    Freshness,
    HealthWarning,
    IndustryHealth,
    OverdueHealth,
    ProviderHealth,
    RunSummary,
    UniverseHealth,
    VerificationHealth,
)

_UNIVERSE_SYNC_STALE_DAYS = 14
_INDUSTRY_SYNC_STALE_DAYS = 40
_MARKET_LAG_ERROR = 3
_RECENT_CONFLICT = timedelta(days=7)


@dataclass(frozen=True)
class WarningsInput:
    """Every input of the warning rules, as assembled by ``build_data_health``.

    Frozen so a rule set is a pure function of one value: no session, no
    database and no clock - tests build it directly from model instances.
    """

    now: datetime
    threshold: float
    accumulation: Accumulation
    freshness: Freshness
    universes: Sequence[UniverseHealth]
    industry: IndustryHealth
    collections: Sequence[CollectionHealth]
    stopped_runs: Sequence[tuple[CollectionHealth, RunSummary]]
    verification: VerificationHealth
    overdue: OverdueHealth
    calendar_warnings: Sequence[HealthWarning] = ()
    providers: Sequence[ProviderHealth] = ()


# --------------------------------------------------------------------------- #
# Warnings
# --------------------------------------------------------------------------- #


def collection_label(item: CollectionHealth) -> str:
    label = _KIND_LABELS.get(item.kind, item.kind)
    extra = " ".join(part for part in (item.provider, item.part, item.scope) if part)
    return f"{label}（{extra}）"


def derive_warnings_from(data: WarningsInput) -> list[HealthWarning]:
    """Rules (see README "数据健康"); ``error`` > ``warn`` > ``info`` in the output order."""

    now = data.now
    threshold = data.threshold
    accumulation = data.accumulation
    freshness = data.freshness
    universes = data.universes
    industry = data.industry
    collections = data.collections
    stopped_runs = data.stopped_runs
    verification = data.verification
    overdue = data.overdue
    calendar_warnings = data.calendar_warnings
    providers = data.providers

    warnings: list[HealthWarning] = list(calendar_warnings)
    market = freshness.market
    if market.latest_trade_date is None:
        warnings.append(
            HealthWarning(
                code="market_never_collected",
                level="info",
                message=f"{market.universe_code} 尚无日线快照（baostock 不复权）。",
                section="freshness",
            )
        )
    elif market.lag_trading_days:
        warnings.append(
            HealthWarning(
                code="market_stale",
                level="error" if market.lag_trading_days >= _MARKET_LAG_ERROR else "warn",
                message=(
                    f"{market.universe_code} 最新日线 {market.latest_trade_date.isoformat()}，"
                    f"落后应有交易日 {market.expected_trade_date} 共 {market.lag_trading_days} 个交易日。"
                ),
                section="freshness",
            )
        )
    if (
        market.latest_trade_date is not None
        and market.members
        and market.members_at_latest < market.members * threshold
    ):
        warnings.append(
            HealthWarning(
                code="market_coverage_low",
                level="warn",
                message=(
                    f"{market.latest_trade_date.isoformat()} 日线只覆盖 "
                    f"{market.members_at_latest}/{market.members} 只当前成分股。"
                ),
                section="freshness",
            )
        )

    for item in collections:
        run = item.last_run
        if run is None:
            continue
        label = collection_label(item)
        if run.status == "failed":
            warnings.append(
                HealthWarning(
                    code="collection_failed",
                    level="error",
                    message=f"{label} 最近一次运行 #{run.run_id} 失败：{run.error_summary or '无摘要'}",
                    section="collections",
                )
            )
        elif run.ratio is not None and run.ratio < threshold:
            warnings.append(
                HealthWarning(
                    code="completeness_low",
                    level="warn",
                    message=(
                        f"{label} 最近一次运行 #{run.run_id}（{run.subject or '-'}）完整率 "
                        f"{run.ratio:.1%}，低于阈值 {threshold:.0%}。"
                    ),
                    section="collections",
                )
            )
    for item, run in stopped_runs:
        reason = "iFinD 额度耗尽" if run.stop_reason == QUOTA_STOP_REASON else "数据源连续失败"
        warnings.append(
            HealthWarning(
                code="quota_stop" if run.stop_reason == QUOTA_STOP_REASON else "provider_stop",
                level="warn",
                message=(
                    f"{collection_label(item)} 运行 #{run.run_id} 24 小时内因{reason}提前停止，"
                    f"未采 {run.missing_by_kind.get('quota', 0) + run.missing_by_kind.get('stopped', 0)} 只；"
                    "下次调度会续采。"
                ),
                section="collections",
            )
        )

    for universe in universes:
        sync = universe.last_sync
        if sync is None or sync.at is None:
            warnings.append(
                HealthWarning(
                    code="universe_never_synced",
                    level="info",
                    message=f"{universe.code} 尚无成分同步记录。",
                    section="universes",
                )
            )
        elif now - sync.at > timedelta(days=_UNIVERSE_SYNC_STALE_DAYS):
            warnings.append(
                HealthWarning(
                    code="universe_sync_stale",
                    level="warn",
                    message=(
                        f"{universe.code} 最近一次成分同步是 {sync.at.date().isoformat()}，"
                        f"超过 {_UNIVERSE_SYNC_STALE_DAYS} 天（应每周一同步）。"
                    ),
                    section="universes",
                )
            )
    for system in industry.systems:
        if system.at is not None and now - system.at > timedelta(days=_INDUSTRY_SYNC_STALE_DAYS):
            warnings.append(
                HealthWarning(
                    code="industry_sync_stale",
                    level="info",
                    message=(
                        f"{system.system} 行业最近一次同步是 {system.at.date().isoformat()}，"
                        f"超过 {_INDUSTRY_SYNC_STALE_DAYS} 天（应每月同步）。"
                    ),
                    section="industry",
                )
            )

    weeks = accumulation.weeks
    completed = [week for week in weeks if week.status not in ("in_progress", "no_trading")]
    if completed and completed[0].status in ("missing", "below_threshold"):
        week = completed[0]
        warnings.append(
            HealthWarning(
                code="accumulation_reset",
                level="warn",
                message=(
                    f"{week.week} 周"
                    + ("没有日线快照" if week.status == "missing" else f"完整率 {week.min_ratio:.1%} 未达标")
                    + f"，快照连续积累周数归零（目标 {accumulation.target_weeks} 周）。"
                ),
                section="accumulation",
            )
        )

    budget = verification.budget
    if budget.remaining == 0:
        when = budget.next_free_at.isoformat(timespec="minutes") if budget.next_free_at else "-"
        warnings.append(
            HealthWarning(
                code="budget_exhausted",
                level="warn",
                message=(
                    f"iFinD 预算已用尽（{budget.used}/{budget.cap} 次 / {budget.window_hours:g} 小时），"
                    f"核对请求将延后，最早 {when} 恢复。"
                ),
                section="verification",
            )
        )
    recent = [
        item
        for item in verification.conflicts
        if not item.backfill and item.at is not None and now - item.at <= _RECENT_CONFLICT
    ]
    if recent:
        tickers = sorted({item.ticker for item in recent})
        warnings.append(
            HealthWarning(
                code="provider_conflict",
                level="warn",
                message=(
                    f"近 7 天有 {len(tickers)} 只股票存在数据源字段冲突："
                    + "、".join(tickers[:5])
                    + ("等。" if len(tickers) > 5 else "。")
                ),
                section="verification",
            )
        )
    if overdue.open:
        warnings.append(
            HealthWarning(
                code="report_overdue",
                level="warn",
                message=f"{overdue.open} 份定期报告逾期未披露。",
                section="overdue",
            )
        )
    directory = freshness.directory
    if directory.synced_on is None:
        warnings.append(
            HealthWarning(
                code="directory_missing",
                level="info",
                message="股票字典尚未同步（随每周成分同步刷新）；名称 / 拼音搜索暂不可用。",
                section="freshness",
            )
        )
    elif directory.age_days is not None and directory.age_days > DIRECTORY_STALE_DAYS:
        warnings.append(
            HealthWarning(
                code="directory_stale",
                level="warn",
                message=(
                    f"股票字典上次同步于 {directory.synced_on.isoformat()}"
                    f"（{directory.age_days} 天前），超过 {DIRECTORY_STALE_DAYS} 天；"
                    "新上市或更名的标的搜不到。"
                ),
                section="freshness",
            )
        )
    for upstream in providers:
        if upstream.open_until is not None and upstream.open_until > now:
            until = upstream.open_until.astimezone(SHANGHAI_TZ).strftime("%H:%M:%S")
            warnings.append(
                HealthWarning(
                    code="provider_circuit_open",
                    level="warn",
                    message=(
                        f"{upstream.provider} 熔断中，至 {until}（24 小时内调用 "
                        f"{upstream.calls_24h} 次，失败 {upstream.failed_24h} 次）；"
                        "窗口内的采集任务会直接 partial，窗口过后自动试探。"
                    ),
                    section="providers",
                )
            )
    rank = {"error": 0, "warn": 1, "info": 2}
    return sorted(warnings, key=lambda item: rank[item.level])


def derive_warnings(
    *,
    now: datetime,
    threshold: float,
    accumulation: Accumulation,
    freshness: Freshness,
    universes: Sequence[UniverseHealth],
    industry: IndustryHealth,
    collections: Sequence[CollectionHealth],
    stopped_runs: Sequence[tuple[CollectionHealth, RunSummary]],
    verification: VerificationHealth,
    overdue: OverdueHealth,
    calendar_warnings: Sequence[HealthWarning] = (),
    providers: Sequence[ProviderHealth] = (),
) -> list[HealthWarning]:
    """Compatibility entry point with the historical keyword signature.

    Kept for existing call sites; new code builds a :class:`WarningsInput` and
    calls :func:`derive_warnings_from` directly.
    """

    return derive_warnings_from(
        WarningsInput(
            now=now,
            threshold=threshold,
            accumulation=accumulation,
            freshness=freshness,
            universes=universes,
            industry=industry,
            collections=collections,
            stopped_runs=stopped_runs,
            verification=verification,
            overdue=overdue,
            calendar_warnings=calendar_warnings,
            providers=providers,
        )
    )
