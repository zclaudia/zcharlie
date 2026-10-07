"""Snapshot source mode: the canonical research bundle from free-source snapshots (P1-B5).

Watchlist research reads the point-in-time snapshot store instead of calling
iFinD (decision D6 rev 2): :func:`load_snapshot_view` with the ``TARGET_POOL``
policy resolves every financial field through the field source registry
(P1-A12: iFinD-verified value > primary > fallback over baostock, AkShare /
Eastmoney and verified iFinD rows), and every canonical metric is a factor
value of that view (P1-B4: one definition per metric).

Canonical field mapping (documented in ``docs/product/observation-snapshot-mode.md``):

=========================  =======================================================
canonical field            snapshot source
=========================  =======================================================
pool.market_cap            factor ``market_cap`` (raw close x total shares)
pool.industry              user text, else the Shenwan 2021 (CSRC fallback) name
pool.industry_group        industry group on ``as_of`` (P1-B3)
market.latest_price        latest raw (unadjusted) baostock close <= as_of
market.pe / pb / ps        factors ``pe_ttm`` / ``pb_mrq`` / ``ps_ttm`` (baostock
                           daily valuation; iFinD mode uses FY multiples)
market.dividend_yield      factor ``dividend_yield_ttm`` (trailing 365 days)
market.return_1y ...       factors ``return_1y`` / ``volatility_1y`` /
                           ``max_drawdown_1y`` (backward-adjusted series)
financial.revenue          ``revenue_total`` (营业总收入) of the latest annual report
financial.net_profit       ``net_profit_parent`` (归母净利润) of the latest annual report
financial.operating_cash_flow  ``operating_cash_flow`` of the latest annual report
financial.roe ...          factors ``roe`` / ``roe_stability`` / ``gross_margin`` /
                           ``net_margin`` / ``debt_ratio`` / ``free_cash_flow``
financial.*_growth_3y      factors ``revenue_growth_3y`` (target pool: baostock
                           ``revenue_main``, approx) / ``net_profit_growth_3y``
                           (baostock ``net_profit_total``)
=========================  =======================================================

Every field source records the provider(s) behind the value and its iFinD
verification state (``unverified`` / ``verified`` / ``conflict``); a
``conflict`` lands in ``provider_conflicts`` (review). Bundles carry
``metadata.source_mode = "snapshot"``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.factors.catalog import DEFAULT_REGISTRY
from investment_agent.factors.library import (
    FLAG_DIVIDENDS_NOT_COLLECTED,
    FLAG_MAIN_BUSINESS_REVENUE,
    FLAG_PROVIDER_FALLBACK,
    FLAG_STALE_PRICE,
    FLAG_SUSPECT_PROVIDER_VALUE,
)
from investment_agent.factors.metrics import MIN_ONE_YEAR_SESSIONS, ONE_YEAR_WINDOW_DAYS
from investment_agent.factors.registry import (
    FLAG_NEGATIVE_BOOK_VALUE,
    FLAG_NEGATIVE_EARNINGS,
    FLAG_NON_POSITIVE_REVENUE,
    FLAG_SHORT_HISTORY,
    FactorValue,
)
from investment_agent.factors.view import (
    TARGET_POOL,
    FinancialPeriod,
    SnapshotView,
    load_snapshot_view,
)
from investment_agent.providers.shenwan_directory import (
    ShenwanDirectory,
    default_shenwan_directory,
)
from investment_agent.research.contracts import (
    CANONICAL_SCHEMA_VERSION,
    CanonicalResearchBundle,
)
from investment_agent.research.ifind_chain import (
    _TICKER_PATTERN,
    CanonicalBundle,
    _validate_ticker,
    with_quality,
)
from investment_agent.storage.models.reference import Company
from investment_agent.storage.repositories.listing import ListingRepository

SOURCE_MODE_SNAPSHOT = "snapshot"
SOURCE_MODE_IFIND = "ifind"
SOURCE_MODES = (SOURCE_MODE_SNAPSHOT, SOURCE_MODE_IFIND)
SNAPSHOT_SOURCE = "snapshot_view"
SNAPSHOT_PROVIDER = "snapshot"
RAW_SCHEMA_VERSION = "snapshot-raw.v1"

# canonical market field -> factor (target-pool view)
MARKET_FACTORS: dict[str, str] = {
    "pe": "pe_ttm",
    "pb": "pb_mrq",
    "ps": "ps_ttm",
    "dividend_yield": "dividend_yield_ttm",
    "return_1y": "return_1y",
    "volatility_1y": "volatility_1y",
    "max_drawdown_1y": "max_drawdown_1y",
    "market_cap": "market_cap",
}
# canonical financial field -> factor
FINANCIAL_FACTORS: dict[str, str] = {
    "gross_margin": "gross_margin",
    "net_margin": "net_margin",
    "roe": "roe",
    "roe_stability": "roe_stability",
    "debt_ratio": "debt_ratio",
    "free_cash_flow": "free_cash_flow",
    "revenue_growth_3y": "revenue_growth_3y",
    "net_profit_growth_3y": "net_profit_growth_3y",
}
# canonical financial field -> resolved field of the latest annual report
FINANCIAL_FIELDS: dict[str, str] = {
    "revenue": "revenue_total",
    "net_profit": "net_profit_parent",
    "operating_cash_flow": "operating_cash_flow",
}
# Factors whose value is a provider column / a single report field (else derived).
_DIRECT_FACTORS = frozenset(
    {"pe_ttm", "pb_mrq", "ps_ttm", "roe", "gross_margin", "net_margin", "debt_ratio"}
)
_NOT_MEANINGFUL: dict[str, tuple[str, str]] = {
    "pe": (FLAG_NEGATIVE_EARNINGS, "PE not meaningful: baostock peTTM {value} <= 0 (loss-making)"),
    "pb": (FLAG_NEGATIVE_BOOK_VALUE, "PB not meaningful: baostock pbMRQ {value} <= 0"),
    "ps": (FLAG_NON_POSITIVE_REVENUE, "PS not meaningful: baostock psTTM {value} <= 0"),
}
# Factor flags surfaced as review flags on the canonical field.
_REVIEW_FLAGS: dict[str, str] = {
    FLAG_STALE_PRICE: "latest price is older than the staleness limit before as_of",
    FLAG_SUSPECT_PROVIDER_VALUE: "provider value rejected by the field source sanity rule",
}
_INFO_FLAGS: dict[str, str] = {
    FLAG_PROVIDER_FALLBACK: "value from the fallback source (primary failed its sanity rule)",
    FLAG_MAIN_BUSINESS_REVENUE: "growth on main-business revenue (baostock), approximates total",
}
# canonical field -> registry (factor-layer) fields an iFinD ``missing``
# verification can fill (P1-A13 trigger ``missing``: no free source has them).
MISSING_VERIFICATION_FIELDS: dict[str, tuple[str, ...]] = {
    "revenue": ("revenue_total",),
    "net_profit": ("net_profit_parent",),
    "operating_cash_flow": ("operating_cash_flow",),
    "free_cash_flow": ("capital_expenditure", "free_cash_flow"),
    "debt_ratio": ("debt_ratio",),
    "roe": ("roe",),
}
_MISSING_MESSAGE = "snapshot view has no value for {field}"
_DERIVED_MESSAGE = "{field} was derived by the factor library from snapshot fields"


def missing_key_fields(score_item: dict[str, Any]) -> list[str]:
    """Registry fields to ask iFinD for: key fields the free sources lack.

    Only fields the kernel counts as missing *and* applicable to the company
    profile (e.g. not free cash flow for a bank) qualify.
    """

    profile = score_item.get("company_profile") or {}
    non_applicable = set(profile.get("non_applicable_fields") or [])
    names: set[str] = set()
    for field in score_item.get("missing_fields") or []:
        if field in non_applicable:
            continue
        names.update(MISSING_VERIFICATION_FIELDS.get(str(field), ()))
    return sorted(names)


class SnapshotDataMissingError(LookupError):
    """The snapshot store cannot answer for this listing (no bars / no annual report)."""

    def __init__(self, reason: str, *, listing_id: int | None = None) -> None:
        self.reason = reason
        self.listing_id = listing_id
        super().__init__(reason)


@dataclass(frozen=True)
class SnapshotBundle:
    bundle: CanonicalBundle
    view: SnapshotView
    report_year: int
    listing_id: int | None = None


def _iso_close(day: date) -> str:
    return datetime.combine(day, time(hour=15), tzinfo=SHANGHAI_TZ).astimezone(UTC).isoformat()


def _iso_day(day: date) -> str:
    return datetime.combine(day, time.min, tzinfo=SHANGHAI_TZ).astimezone(UTC).isoformat()


def _input_sources(value: FactorValue, view: SnapshotView) -> list[dict[str, Any]]:
    """Per-input ``{provider, verification, ...}`` of a factor value."""

    sources: list[dict[str, Any]] = []
    for period in value.inputs_ref.get("financial", []) or []:
        for detail in (period.get("sources") or {}).values():
            if detail.get("provider") is not None:
                sources.append(detail)
    shares = value.inputs_ref.get("total_shares")
    if isinstance(shares, dict):
        for detail in (shares.get("sources") or {}).values():
            if detail.get("provider") is not None:
                sources.append(detail)
    if not sources and value.value is not None:
        sources.append({"provider": view.provider, "verification": "unverified"})
    return sources


def _period_sources(period: FinancialPeriod, name: str, view: SnapshotView) -> list[dict[str, Any]]:
    resolution = period.source(name)
    if resolution is not None and resolution.provider is not None:
        return [resolution.describe()]
    if period.value(name) is not None:
        return [{"provider": view.provider, "verification": "unverified"}]
    return []


def _aggregate(sources: list[dict[str, Any]]) -> tuple[str, str]:
    providers = sorted({str(item["provider"]) for item in sources if item.get("provider")})
    states = [str(item.get("verification") or "unverified") for item in sources]
    if "conflict" in states:
        verification = "conflict"
    elif states and all(state == "verified" for state in states):
        verification = "verified"
    else:
        verification = "unverified"
    return ("+".join(providers) or "none"), verification


def _conflicts(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"provider": item.get("provider"), "conflicts": item.get("conflicts", [])}
        for item in sources
        if item.get("verification") == "conflict"
    ]


class _ItemBuilder:
    """Collects values, field sources, flags and conflicts of one canonical item."""

    def __init__(self, *, provider_label: str) -> None:
        self.values: dict[str, Any] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self.flags: list[dict[str, Any]] = []
        self.conflicts: dict[str, Any] = {}
        self.not_meaningful: dict[str, tuple[str, str]] = {}
        self.derived: list[str] = []
        self.provider_label = provider_label

    def flag(self, field: str, flag: str, message: str, severity: str) -> None:
        self.flags.append(
            {
                "field": field,
                "flag": flag,
                "message": message,
                "provider": SNAPSHOT_PROVIDER,
                "severity": severity,
            }
        )

    def put(
        self,
        field: str,
        value: Any,
        *,
        sources: list[dict[str, Any]],
        source: str,
        quality: str,
        updated_at: str,
        note: str,
        unavailable: bool = False,
    ) -> None:
        provider, verification = _aggregate(sources)
        self.values[field] = value
        self.sources[field] = {
            "field": field,
            "provider": provider if value is not None else SNAPSHOT_PROVIDER,
            "source": "unavailable" if unavailable else source,
            "quality": quality if value is not None else "missing",
            "updated_at": updated_at,
            "note": note,
            "verification": verification if value is not None else None,
        }
        if value is not None and verification == "conflict":
            self.conflicts[field] = {"verification": "conflict", "sources": _conflicts(sources)}
            self.flag(
                field,
                "provider_conflict",
                f"{field}: free sources disagree with iFinD or each other (field source registry)",
                "review",
            )

    def put_factor(
        self, field: str, value: FactorValue, view: SnapshotView, *, updated_at: str
    ) -> None:
        flags = set(value.flags)
        unavailable = value.quality == "not_applicable" or (
            field == "dividend_yield" and FLAG_DIVIDENDS_NOT_COLLECTED in flags
        )
        if value.value is None:
            quality = "missing"
        elif value.quality == "approx":
            quality = "proxy"
        elif value.name in _DIRECT_FACTORS:
            quality = "direct"
        else:
            quality = "derived"
            self.derived.append(field)
        note = f"factor {value.name} v{value.version} ({value.quality})"
        if value.flags:
            note += "; flags: " + ", ".join(value.flags)
        self.put(
            field,
            value.value,
            sources=_input_sources(value, view),
            source=f"{SNAPSHOT_SOURCE}:factor:{value.name}",
            quality=quality,
            updated_at=updated_at,
            note=note,
            unavailable=unavailable,
        )
        meaningful = _NOT_MEANINGFUL.get(field)
        if meaningful is not None and meaningful[0] in flags and value.value is None:
            raw = value.inputs_ref.get("provider_value")
            self.not_meaningful[field] = (meaningful[0], meaningful[1].format(value=raw))
        for flag in flags & set(_REVIEW_FLAGS):
            self.flag(field, flag, f"{field}: {_REVIEW_FLAGS[flag]}", "review")
        for flag in flags & set(_INFO_FLAGS):
            self.flag(field, flag, f"{field}: {_INFO_FLAGS[flag]}", "info")

    def finish(self, base: dict[str, Any], required: tuple[str, ...]) -> dict[str, Any]:
        item = {**base, **self.values, "field_sources": self.sources}
        return with_quality(
            item,
            required,
            derived_fields=tuple(self.derived),
            not_meaningful=self.not_meaningful,
            extra_flags=self.flags,
            provider=SNAPSHOT_PROVIDER,
            missing_message=_MISSING_MESSAGE,
            derived_message=_DERIVED_MESSAGE,
            provider_conflicts=self.conflicts,
        )


def _metadata(payload_type: str, updated_at: str, **extra: Any) -> dict[str, Any]:
    return {
        "source": SNAPSHOT_SOURCE,
        "updated_at": updated_at,
        "provider": SNAPSHOT_PROVIDER,
        "payload_type": payload_type,
        "schema_version": CANONICAL_SCHEMA_VERSION,
        "source_mode": SOURCE_MODE_SNAPSHOT,
        **extra,
    }


def _industry_display(
    view: SnapshotView, directory: ShenwanDirectory
) -> tuple[str, str]:
    """``(name, system)`` of the classification that decided the group."""

    if view.sw_code is not None:
        return directory.name_of(view.sw_code) or view.sw_code, "sw2021"
    if view.industry_name:
        return view.industry_name, "csrc"
    return "", "none"


def build_snapshot_bundle(
    view: SnapshotView,
    *,
    company_name: str,
    industry: str | None,
    retrieved_at: str,
    requested_report_year: int | None = None,
    shenwan_directory: ShenwanDirectory | None = None,
) -> CanonicalBundle:
    """Canonical bundle from a target-pool snapshot view (pure, offline).

    ``industry`` is the user's text (overrides the data industry group in the
    kernel); ``None`` / empty uses the view's Shenwan / CSRC classification.
    Raises :class:`SnapshotDataMissingError` without a price or an annual report.
    """

    ticker = _validate_ticker(view.ticker)
    match = _TICKER_PATTERN.fullmatch(ticker)
    assert match is not None
    symbol, market_code = match.group("symbol"), match.group("market")
    bar = view.latest_raw_bar()
    latest = view.latest_annual()
    if bar is None:
        raise SnapshotDataMissingError(f"no free-source price snapshot for {ticker} on {view.as_of}")
    if latest is None:
        raise SnapshotDataMissingError(
            f"no free-source annual report for {ticker} public on {view.as_of}"
        )
    report_year = latest.report_period.year
    quote_at = _iso_close(bar.trade_date)
    financial_at = _iso_day(latest.available_from or view.as_of)
    factors = {
        name: DEFAULT_REGISTRY.get(name).compute(view, enforce_applicability=False)
        for name in (*MARKET_FACTORS.values(), *FINANCIAL_FACTORS.values())
    }

    # -- pool ------------------------------------------------------------- #
    directory = shenwan_directory or default_shenwan_directory()
    user_text = (industry or "").strip()
    data_name, system = _industry_display(view, directory)
    pool = _ItemBuilder(provider_label=SNAPSHOT_PROVIDER)
    pool.sources["name"] = {
        "field": "name",
        "provider": "reference",
        "source": "listing.company",
        "quality": "direct",
        "updated_at": retrieved_at,
        "note": "",
    }
    industry_match = view.industry_match.describe() if view.industry_match else {}
    pool.sources["industry"] = {
        "field": "industry",
        "provider": "manual" if user_text else system,
        "source": "command_input" if user_text else f"industry_classification:{system}",
        "quality": "direct",
        "updated_at": retrieved_at,
        "note": (
            f"user text; data group {view.industry_group} "
            f"({industry_match.get('matched_by')}, {system})"
            if user_text
            else f"group {view.industry_group} ({industry_match.get('matched_by')})"
        ),
    }
    if view.industry_fallback:
        pool.flag(
            "industry",
            "industry_fallback",
            "industry group from the CSRC fallback table (no Shenwan 2021 classification)",
            "info",
        )
    pool.put_factor("market_cap", factors["market_cap"], view, updated_at=quote_at)
    stock_item = pool.finish(
        {
            "symbol": symbol,
            "name": company_name or symbol,
            "market": market_code,
            "industry": user_text or data_name,
            "industry_group": view.industry_group,
            "source": f"{SNAPSHOT_SOURCE}:listing+industry+market",
            "updated_at": retrieved_at,
        },
        ("symbol", "name", "market", "industry", "market_cap"),
    )

    # -- market ----------------------------------------------------------- #
    market = _ItemBuilder(provider_label=SNAPSHOT_PROVIDER)
    market.put(
        "latest_price",
        bar.close,
        sources=[{"provider": view.provider, "verification": "unverified"}],
        source=f"{SNAPSHOT_SOURCE}:market_snapshot(adjust=none).close",
        quality="direct",
        updated_at=quote_at,
        note=f"raw close of {bar.trade_date.isoformat()} ({bar.ref})",
    )
    for field, name in MARKET_FACTORS.items():
        market.put_factor(field, factors[name], view, updated_at=quote_at)
    returns = factors["return_1y"]
    if returns.value is not None and FLAG_SHORT_HISTORY in returns.flags:
        sessions = returns.inputs_ref.get("sessions")
        for field in ("return_1y", "volatility_1y", "max_drawdown_1y"):
            market.flag(
                field,
                "short_price_history",
                f"{field} covers only {sessions} trading sessions (< {MIN_ONE_YEAR_SESSIONS}) "
                f"within the trailing {ONE_YEAR_WINDOW_DAYS}-day window",
                "review",
            )
    market_item = market.finish(
        {
            "symbol": symbol,
            "source": f"{SNAPSHOT_SOURCE}:market_snapshot+financial_snapshot",
            "updated_at": quote_at,
        },
        (
            "latest_price",
            "pe",
            "pb",
            "ps",
            "dividend_yield",
            "return_1y",
            "volatility_1y",
            "max_drawdown_1y",
            "market_cap",
        ),
    )

    # -- financial -------------------------------------------------------- #
    financial = _ItemBuilder(provider_label=SNAPSHOT_PROVIDER)
    period_note = (
        f"FY{report_year} (public {latest.available_from}, {latest.availability}; {latest.ref})"
    )
    for field, name in FINANCIAL_FIELDS.items():
        financial.put(
            field,
            latest.value(name),
            sources=_period_sources(latest, name, view),
            source=f"{SNAPSHOT_SOURCE}:financial_snapshot.{name}",
            quality="proxy"
            if (resolution := latest.source(name)) is not None and resolution.approx
            else "direct",
            updated_at=financial_at,
            note=f"{name}; {period_note}",
        )
    for field, name in FINANCIAL_FACTORS.items():
        financial.put_factor(field, factors[name], view, updated_at=financial_at)
    if view.overdue_periods:
        financial.flag(
            "roe",
            "report_overdue",
            "a periodic report is past its statutory deadline and unpublished: "
            + ", ".join(period.isoformat() for period in view.overdue_periods),
            "review",
        )
    if requested_report_year is not None and requested_report_year != report_year:
        financial.flag(
            "roe",
            "report_year_substituted",
            f"requested FY{requested_report_year}; the latest annual report public on "
            f"{view.as_of} is FY{report_year}",
            "info",
        )
    financial_item = financial.finish(
        {
            "symbol": symbol,
            "source": f"{SNAPSHOT_SOURCE}:financial_snapshot(field_sources)",
            "updated_at": financial_at,
        },
        (
            "revenue",
            "net_profit",
            "gross_margin",
            "net_margin",
            "roe",
            "roe_stability",
            "debt_ratio",
            "operating_cash_flow",
            "free_cash_flow",
            "revenue_growth_3y",
            "net_profit_growth_3y",
        ),
    )

    validated = CanonicalResearchBundle.model_validate(
        {
            "pool": {"metadata": _metadata("stock_pool", retrieved_at), "items": [stock_item]},
            "market": {"metadata": _metadata("market", quote_at), "items": [market_item]},
            "financial": {
                "metadata": _metadata("financial", financial_at, report_year=report_year),
                "items": [financial_item],
            },
        }
    )
    raw = {
        "schema_version": RAW_SCHEMA_VERSION,
        "ticker": ticker,
        "as_of": view.as_of.isoformat(),
        "retrieved_at": retrieved_at,
        "policy": view.policy.name,
        "industry": {
            "group": view.industry_group,
            "groups_version": view.industry_groups_version,
            "match": industry_match,
            "user_text": user_text or None,
        },
        "latest_bar": {"trade_date": bar.trade_date.isoformat(), "ref": bar.ref},
        "periods": [period.describe(period.sources) for period in view.periods],
        "overdue_periods": [period.isoformat() for period in view.overdue_periods],
        "factors": {
            name: {
                "value": value.value,
                "quality": value.quality,
                "flags": list(value.flags),
                "inputs_ref": json.loads(json.dumps(value.inputs_ref, default=str)),
            }
            for name, value in factors.items()
        },
    }
    return CanonicalBundle(
        pool=validated.pool.model_dump(mode="json"),
        market=validated.market.model_dump(mode="json"),
        financial=validated.financial.model_dump(mode="json"),
        raw=raw,
    )


async def load_snapshot_bundle(
    session: AsyncSession,
    *,
    ticker: str,
    as_of: date,
    industry: str | None,
    requested_report_year: int | None = None,
    retrieved_at: str | None = None,
) -> SnapshotBundle:
    """Read the target-pool view of ``ticker`` on ``as_of`` and build the bundle.

    Raises :class:`SnapshotDataMissingError` when the listing is unknown or
    the store has no price / annual report for it (e.g. outside the target
    pool); ``listing_id`` is set when the listing exists.
    """

    ticker = _validate_ticker(ticker)
    listing = await ListingRepository(session).get_a_share_listing(ticker)
    if listing is None:
        raise SnapshotDataMissingError(f"no listing for {ticker} in the snapshot store")
    company = await session.get(Company, listing.company_id)
    view = await load_snapshot_view(session, ticker, as_of, policy=TARGET_POOL)
    try:
        bundle = build_snapshot_bundle(
            view,
            company_name=company.canonical_name if company is not None else "",
            industry=industry,
            retrieved_at=retrieved_at or datetime.now(UTC).isoformat(),
            requested_report_year=requested_report_year,
        )
    except SnapshotDataMissingError as exc:
        raise SnapshotDataMissingError(exc.reason, listing_id=listing.id) from exc
    latest = view.latest_annual()
    assert latest is not None
    return SnapshotBundle(bundle, view, latest.report_period.year, listing.id)
