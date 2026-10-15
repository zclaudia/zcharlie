"""Read-only financial context of one HK/US listing (plan 09).

This module is the read model behind
``GET /research/listings/{listing_id}/financial-context``. It answers one
question — "what does the saved material of this security actually support, and
where are the gaps?" — from three local sources only:

* the reference rows (``Listing`` / ``Company``) and the hand-checked identity
  list of task 01 (identity, currencies, security type, bank applicability),
* the immutable dataset versions of task 02/05 read through
  :class:`~investment_agent.storage.repositories.research_material.ResearchDatasetRepository`,
* the frozen fact contract of task 02 via
  :func:`~investment_agent.materials.evidence.dataset_facts` (facts are never
  re-parsed from the payload here) and the pure metric layer of task 08.

Zero upstream IO
----------------

The module-level import graph deliberately stops at local readers and pure
functions: the source adapters, the offline importer and every HTTP client are
absent from it, so this read path cannot fetch anything. The one capability
constant it needs (whether online refresh is licensed) is read through a
function-local import, which is why ``httpx`` is not a module-level dependency
even though the handler module that owns the constant has one.

Data discipline carried here:

* fiscal years are the material's *real* periods (``FactPeriod``); no calendar
  year is assumed and no period start is back-computed;
* a missing amount stays missing (``amount`` is null with its reason), never 0;
* the material's ``candidate`` / ``verified`` state is copied verbatim and the
  two coverages are counted separately (``facts_total`` vs ``facts_verified``),
  never merged into one "completeness" number;
* an ``unresolved`` conflict is never resolved by picking a side: it is shown on
  the fact and surfaced as the ``source_conflict`` status;
* every capability the first delivery does not ship is ``False`` and its gap is
  named in ``statuses``; a ``False`` bit is never a silent omission.

The two decisions the plan left to the control agent are recorded in
:data:`BANK_COMPANY_KEYS` (the controlled bank set) and in
:data:`MATERIAL_STALE_AFTER_DAYS` (the period-lag indicator).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.security import (
    DEFAULT_PILOT_LIST_PATH,
    MARKETS,
    SOURCE_EASTMONEY_HK,
    SOURCE_SEC_COMPANYFACTS,
    find_identity,
)
from investment_agent.materials.contracts import (
    AccountingStandard,
    DatasetKind,
    EquityScope,
    FactPeriod,
    FactRecord,
    PeriodBasis,
    PerShareBasis,
    ReportingScope,
    SubjectKind,
    Verification,
)
from investment_agent.materials.evidence import dataset_facts
from investment_agent.materials.metrics import (
    MetricResult,
    compute_metrics,
    profile_from_identity,
)
from investment_agent.storage.models.material_acquisition import ResearchMaterialAcquisition
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.research_material import ResearchDataset
from investment_agent.storage.repositories.research_material import (
    ResearchDatasetRepository,
)

ANNUAL_FACTS: Final[str] = DatasetKind.ANNUAL_FACTS.value
COMPANY_SUBJECT: Final[str] = SubjectKind.COMPANY.value

UNKNOWN_TEXT: Final[str] = "unknown"
"""Value shown where the checked identity states nothing (never an empty guess)."""

BANK_COMPANY_KEYS: Final[frozenset[str]] = frozenset({"hsbc-holdings", "jpmorgan-chase"})
"""The controlled bank set, by the pilot list's ``company_key``.

Task 01's identity list carries no ``is_bank`` field, and a bank is not
recognisable from its numbers, so the applicability rule of task 08 is fed from
this explicit, hand-checked set: the two pilot issuers that are banks (the
identity list's own notes name HSBC as the bank sample and JPM as the bank
sample with a different revenue definition). A security outside the set is not
treated as a bank. This is a controlled placeholder: when the identity list
gains an issuer-type field (or a source review confirms one), the rule must be
upgraded to read that field instead of this constant.
"""

CAPABILITY_ORDER: Final[tuple[str, ...]] = (
    "financials",
    "price",
    "annual_pe",
    "dividends",
    "pb",
    "online_refresh",
)
"""The capability bits of the read model, in report order."""

CONDITIONAL_CAPABILITIES: Final[tuple[str, ...]] = ("price", "annual_pe", "dividends", "pb")
"""Capabilities whose datasets are conditionally enabled and not shipped yet.

They are ``False`` by decision (design: 尚未验证能力 / 条件性开启), not because
a check failed; the gap is still stated in ``statuses`` so a page can never
mistake "not delivered" for "nothing to report".
"""

CAPABILITY_FUNCTIONS: Final[dict[str, tuple[str, ...]]] = {
    "financials": ("财务展示", "指标计算"),
    "price": ("价格走势",),
    "annual_pe": ("年度市盈率",),
    "dividends": ("分红记录",),
    "pb": ("市净率",),
}
"""Human-readable functions each capability enables (used by the status list)."""

AMOUNT_METRICS: Final[frozenset[str]] = frozenset({"fcf_simple"})
"""Metric keys whose definition is an amount in the reporting currency.

Used only when a metric produced no value: its unit still has to be the unit of
its own definition, so a missing amount never looks like a ratio.
"""

MATERIAL_STALE_AFTER_DAYS: Final[int] = 366
"""Period lag after which the material is reported as behind the calendar.

The newest annual period of the material is compared with the *real* current
date: an annual period that ended more than this many days ago means the
following annual report should already have been collected, so the page shows
the periods it really covers and asks for a refresh. This is a lag indicator
over real dates — it asserts no filing deadline and invents no date. (The plan
leaves this rule to the control agent; the threshold is the one-year-plus-1-day
boundary so a 12-31 fiscal year turns stale when its successor should be out.)
"""

STATUS_CODES: Final[tuple[str, ...]] = (
    "listing_not_covered",
    "material_missing",
    "partial_verification",
    "metric_not_applicable",
    "source_conflict",
    "raw_archive_missing",
    "material_stale",
    "online_refresh_disabled",
)
"""The fixed business-status vocabulary of the read model."""


@dataclass(frozen=True)
class _StatusCopy:
    """Fixed copy of one status code: what it affects and the next step."""

    affected: tuple[str, ...]
    next_step: str


STATUS_COPY: Final[dict[str, _StatusCopy]] = {
    "listing_not_covered": _StatusCopy(
        affected=("全部财务研究",),
        next_step="先在受控证券清单中完成该证券的身份核对，再采集资料；名单外证券的资料一律不采信。",
    ),
    "material_missing": _StatusCopy(
        affected=(),
        next_step=(
            "登记一次资料采集任务；价格、估值与分红类数据集属条件性开启，需先确认来源与使用许可。"
        ),
    ),
    "partial_verification": _StatusCopy(
        affected=("关键结论",),
        next_step="对照发行人原件逐项核对候选事实，核对通过后再作为结论依据。",
    ),
    "metric_not_applicable": _StatusCopy(
        affected=(),
        next_step="查看该指标的适用口径说明；不适用不等于零，也不会以其他口径替代。",
    ),
    "source_conflict": _StatusCopy(
        affected=(),
        next_step="人工判定来源差异后再使用；系统不会自动挑选对结论更有利的一边。",
    ),
    "raw_archive_missing": _StatusCopy(
        affected=("原始响应归档",),
        next_step="这批旧资料没有采集原件包；保留现有资料读取，并在下一次采集时归档上游响应。",
    ),
    "material_stale": _StatusCopy(
        affected=("财务展示", "指标计算"),
        next_step="登记一次资料采集，补齐最新已披露的财年；页面只展示资料实际覆盖的报告期。",
    ),
    "online_refresh_disabled": _StatusCopy(
        affected=("刷新入口",),
        next_step="确认来源使用许可后开启在线刷新；当前登记的任务不会联网采集。",
    ),
}
"""Business copy per status code: affected functions + next step.

No entry names a supplier endpoint, a filing identifier or a run id; the status
is what a page shows next to the data.
"""

SOURCE_DOCUMENT_LABELS: Final[dict[str, str]] = {
    SOURCE_EASTMONEY_HK: "交易所年报原件",
    SOURCE_SEC_COMPANYFACTS: "发行人申报原件",
}
"""Neutral document label per controlled source id (never the endpoint name)."""

OTHER_DOCUMENT_LABEL: Final[str] = "已保存原件"
"""Document label for a dataset whose source is not one of the checked two."""

STANDARD_LABELS: Final[dict[AccountingStandard, str]] = {
    AccountingStandard.US_GAAP: "美国会计准则",
    AccountingStandard.IFRS: "国际财务报告准则",
    AccountingStandard.HKFRS: "香港财务报告准则",
    AccountingStandard.UNKNOWN: "会计准则未标注",
}

REPORTING_SCOPE_LABELS: Final[dict[ReportingScope, str]] = {
    ReportingScope.CONSOLIDATED: "合并报表口径",
    ReportingScope.PARENT_ONLY: "母公司报表口径",
    ReportingScope.UNKNOWN: "报表口径未标注",
}

EQUITY_SCOPE_LABELS: Final[dict[EquityScope, str]] = {
    EquityScope.ORDINARY: "普通股股东权益",
    EquityScope.ALL_PARENT: "母公司全部权益",
    EquityScope.TOTAL: "全部权益",
    EquityScope.UNKNOWN: "权益归属未标注",
}

PER_SHARE_LABELS: Final[dict[PerShareBasis, str]] = {
    PerShareBasis.BASIC: "基本每股口径",
    PerShareBasis.DILUTED: "稀释每股口径",
}

DISPLAY_SUFFIXES: Final[dict[str, str]] = {"HKEX": ".HK"}
"""Per-market suffix of the display code (the stored ticker stays canonical)."""

FACT_LABELS: Final[dict[str, str]] = {
    "revenue_operating": "营业收入",
    "revenue_gross": "总收入",
    "revenue_net_credit": "收入（扣除预期信用损失后）",
    "credit_loss_expense": "预期信用损失",
    "cost_of_revenue": "营业成本",
    "net_profit": "净利润",
    "profit_parent": "归属于母公司股东的利润",
    "profit_ordinary_shareholders": "归属于普通股股东的利润",
    "profit_other_equity_holders": "归属于其他权益工具持有者的利润",
    "total_assets": "总资产",
    "total_liabilities": "总负债",
    "total_equity": "权益总额",
    "equity_parent": "归属于母公司股东的权益",
    "equity_ordinary": "归属于普通股股东的权益",
    "operating_cash_flow": "经营活动现金流量净额",
    "capex_ppe": "固定资产类现金支出",
    "eps_basic": "基本每股收益",
    "eps_diluted": "稀释每股收益",
    "shares_weighted_basic": "加权平均股数（基本）",
    "shares_outstanding_end": "期末已发行股数",
}
"""Readable label per frozen fact metric; an unknown key shows its own name."""


@dataclass(frozen=True)
class FiscalYearView:
    """One real fiscal year the material covers."""

    end: date
    label: str | None
    days: int | None
    """Day span: the period's stated value, else the real ``start``-``end`` span."""
    basis: str


@dataclass(frozen=True)
class FactView:
    """One material fact as the read model shows it.

    ``amount`` is the canonical amount of the source's own figure (raw value x
    scale) in ``unit``; it is null exactly when the source states no value, and
    then ``missing_reason`` explains why. ``source`` names the kind of original
    document, never the supplier endpoint.
    """

    metric: str
    label: str
    period_end: date
    basis: str
    amount: str | None
    unit: str
    currency: str | None
    missing_reason: str | None
    definition: str
    verification: str
    conflict: str
    source: str
    source_date: date | None
    locator: str | None


@dataclass(frozen=True)
class MetricView:
    """One computed metric with its status, definition and caveat notes."""

    key: str
    label: str
    status: str
    value: str | None
    unit: str
    currency: str | None
    basis: str
    periods: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class StatusView:
    """One business status: the code, the functions it affects, the next step."""

    code: str
    affected: tuple[str, ...]
    next_step: str


@dataclass(frozen=True)
class FinancialContextView:
    """The whole read model of one listing; immutable and freely serialisable."""

    listing_id: int
    market: str
    display_code: str
    company_name: str
    security_type: str
    quote_currency: str
    report_currency: str
    covered: bool
    fiscal_years: tuple[FiscalYearView, ...]
    facts: tuple[FactView, ...]
    metrics: tuple[MetricView, ...]
    capabilities: dict[str, bool]
    coverage: dict[str, int]
    statuses: tuple[StatusView, ...]


class ListingNotFoundError(LookupError):
    """``listing_id`` matches no row; the endpoint answers 404, not an invented view."""


def online_refresh_enabled() -> bool:
    """Whether a refresh run may call upstream at all.

    The flag is read from its owner (the collection handler) through a
    function-local import: importing the handler module at module level would
    pull the HTTP client into this read model's graph, and the read path must
    stay provably free of any fetcher.
    """

    from investment_agent.materials.handlers import ONLINE_REFRESH_ENABLED

    return ONLINE_REFRESH_ENABLED


async def financial_context(session: AsyncSession, listing_id: int) -> FinancialContextView:
    """The read-only financial context of ``listing_id`` (no upstream IO).

    Raises :class:`ListingNotFoundError` when the listing does not exist. A
    listing that exists but is not in the hand-checked identity list is answered
    with ``covered=False`` and the ``listing_not_covered`` status: absence from
    the controlled list is information the caller needs, not an error, and no
    material of an unchecked security is ever read or offered.
    """

    listing = await session.get(Listing, listing_id)
    if listing is None:
        raise ListingNotFoundError(f"listing {listing_id} does not exist")
    company = await session.get(Company, listing.company_id)
    identity = find_identity(
        market_code=listing.market_code,
        ticker=listing.ticker,
        path=DEFAULT_PILOT_LIST_PATH,
    )
    if identity is None or company is None:
        return _uncovered_view(listing, company)

    datasets = await ResearchDatasetRepository(session).history(
        kind=ANNUAL_FACTS,
        subject_kind=COMPANY_SUBJECT,
        subject_id=company.id,
    )
    material = _current_versions(datasets)
    missing_archive_sources = await _missing_archive_sources(
        session, subject_id=company.id, datasets=material
    )
    records = tuple(
        (dataset, fact) for dataset in material for fact in dataset_facts(dataset)
    )
    material_facts = tuple(fact for _, fact in records)
    fiscal_views, fiscal_periods = _fiscal_years(material_facts)
    facts = tuple(_fact_view(dataset, fact) for dataset, fact in records)
    profile = profile_from_identity(
        identity, is_bank=identity.company_key in BANK_COMPANY_KEYS
    )
    metric_set = (
        compute_metrics(
            facts=material_facts,
            profile=profile,
            fiscal_years=fiscal_periods,
        )
        if material_facts
        else None
    )
    metrics = (
        ()
        if metric_set is None
        else tuple(
            _metric_view(item, report_currency=identity.report_currency)
            for item in metric_set.metrics
        )
    )
    capabilities = _capabilities(covered=True, has_material=bool(material))
    coverage = _coverage(facts=facts, fiscal_years=fiscal_views)
    statuses = _statuses(
        facts=facts,
        metrics=metrics,
        capabilities=capabilities,
        newest_period_end=fiscal_views[-1].end if fiscal_views else None,
        today=date.today(),
    )
    if missing_archive_sources:
        statuses = (
            *statuses,
            _status(
                "raw_archive_missing",
                affected=tuple(
                    SOURCE_DOCUMENT_LABELS.get(source, OTHER_DOCUMENT_LABEL)
                    for source in missing_archive_sources
                ),
            ),
        )
    statuses = tuple(sorted(statuses, key=lambda item: STATUS_CODES.index(item.code)))
    return FinancialContextView(
        listing_id=listing.id,
        market=listing.market_code,
        display_code=_display_code(listing.market_code, listing.ticker),
        company_name=identity.company_name,
        security_type=identity.security_type.value,
        quote_currency=identity.quote_currency,
        report_currency=identity.report_currency,
        covered=True,
        fiscal_years=fiscal_views,
        facts=facts,
        metrics=metrics,
        capabilities=capabilities,
        coverage=coverage,
        statuses=statuses,
    )


# --- the uncovered answer ------------------------------------------------------


def _uncovered_view(listing: Listing, company: Company | None) -> FinancialContextView:
    """The honest answer for a listing outside the checked list.

    Nothing about the security is known here — not even its reporting currency —
    so those fields say ``unknown`` instead of guessing from the market default,
    no fact, metric or fiscal year is offered, and the only capability that can
    be true is the licensed refresh bit (which is read, not assumed).
    """

    market = MARKETS.get(listing.market_code)
    return FinancialContextView(
        listing_id=listing.id,
        market=listing.market_code,
        display_code=_display_code(listing.market_code, listing.ticker),
        company_name=company.canonical_name if company is not None else listing.ticker,
        security_type=UNKNOWN_TEXT,
        quote_currency=market.quote_currency if market is not None else UNKNOWN_TEXT,
        report_currency=UNKNOWN_TEXT,
        covered=False,
        fiscal_years=(),
        facts=(),
        metrics=(),
        capabilities=_capabilities(covered=False, has_material=False),
        coverage={"periods_covered": 0, "facts_total": 0, "facts_candidate": 0, "facts_verified": 0},
        statuses=(_status("listing_not_covered"),),
    )


# --- material, facts and metrics ------------------------------------------------


def _current_versions(datasets: Sequence[ResearchDataset]) -> tuple[ResearchDataset, ...]:
    """Current source/year versions, preserving legacy per-table HK rows.

    Older imports saved the three statements as separate revisions of one key.
    Keep the newest version of each table until a consolidated annual version
    supersedes it. Sources remain separate and identical content is deduplicated.
    """

    newest: dict[tuple[str, date | None, str | None], ResearchDataset] = {}
    seen_content: set[tuple[str, date | None, str]] = set()
    for dataset in sorted(datasets, key=lambda row: (row.revision, row.id)):
        report = dataset.payload.get("report")
        report = report if isinstance(report, str) else None
        key = (dataset.source, dataset.period_end, report)
        content = (dataset.source, dataset.period_end, dataset.content_hash)
        if content in seen_content:
            continue
        seen_content.add(content)
        current = newest.get(key)
        if current is None or dataset.revision >= current.revision:
            newest[key] = dataset
    return tuple(
        sorted(
            (
                row for (source, end, report), row in newest.items()
                if report is None
                or (source, end, None) not in newest
                or row.revision > newest[(source, end, None)].revision
            ),
            key=lambda row: (row.period_end or date.min, row.source),
        )
    )


async def _missing_archive_sources(
    session: AsyncSession,
    *,
    subject_id: int,
    datasets: Sequence[ResearchDataset],
) -> tuple[str, ...]:
    """Find current saved datasets with no acquisition linkage, using DB only."""

    if not datasets:
        return ()
    acquisitions = (
        await session.execute(
            select(ResearchMaterialAcquisition).where(
                ResearchMaterialAcquisition.subject_id == subject_id
            )
        )
    ).scalars()
    linked_ids = {
        link.get("id")
        for acquisition in acquisitions
        for link in acquisition.dataset_links
        if isinstance(link, dict) and isinstance(link.get("id"), int)
    }
    return tuple(
        sorted({dataset.source for dataset in datasets if dataset.id not in linked_ids})
    )


def _fiscal_years(
    facts: Sequence[FactRecord],
) -> tuple[tuple[FiscalYearView, ...], tuple[FactPeriod, ...]]:
    """The real fiscal years of the material, ordered, plus their periods.

    Annual-basis periods are the fiscal years. When a source's material carries
    no annual-basis fact at all, the distinct periods it does carry are used
    instead so the page still shows real reporting periods rather than a
    calendar year; a period is never extended or re-labelled here. Several facts
    of one period end yield one entry, preferring the labelled / day-counted
    period over a bare instant.
    """

    annual = [fact.period for fact in facts if fact.period.basis == PeriodBasis.ANNUAL]
    chosen = annual if annual else [fact.period for fact in facts]
    by_end: dict[date, FactPeriod] = {}
    for period in sorted(chosen, key=lambda item: (item.end, item.start or item.end)):
        existing = by_end.get(period.end)
        if existing is None or _period_rank(period) > _period_rank(existing):
            by_end[period.end] = period
    periods = tuple(by_end[end] for end in sorted(by_end))
    return (
        tuple(
            FiscalYearView(
                end=period.end,
                label=period.label,
                days=_period_days(period),
                basis=period.basis.value,
            )
            for period in periods
        ),
        periods,
    )


def _period_rank(period: FactPeriod) -> tuple[int, int, int]:
    """How informative a period is when two of them share one end date."""

    return (
        1 if period.basis == PeriodBasis.ANNUAL else 0,
        1 if period.label else 0,
        1 if period.days is not None else 0,
    )


def _period_days(period: FactPeriod) -> int | None:
    """The period's day count: its stated value, else the real span, else unknown."""

    if period.days is not None:
        return period.days
    if period.start is None:
        return None
    return (period.end - period.start).days


def _fact_view(dataset: ResearchDataset, fact: FactRecord) -> FactView:
    """One fact as displayed: its own amount, definition, provenance and state."""

    return FactView(
        metric=fact.metric,
        label=FACT_LABELS.get(fact.metric, fact.metric),
        period_end=fact.period.end,
        basis=fact.period.basis.value,
        amount=_amount_text(fact),
        unit=fact.value.unit,
        currency=fact.value.currency,
        missing_reason=fact.value.missing_reason,
        definition=_definition(fact),
        verification=fact.verification.value,
        conflict=fact.conflict,
        source=SOURCE_DOCUMENT_LABELS.get(dataset.source, OTHER_DOCUMENT_LABEL),
        source_date=_source_date(fact),
        locator=fact.original_locator,
    )


def _amount_text(fact: FactRecord) -> str | None:
    """The canonical amount (raw value x scale) as a plain decimal string."""

    raw = fact.value.raw_value
    if raw is None:
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:  # pragma: no cover - the contract rejects this first
        return None
    if not amount.is_finite():  # pragma: no cover - the contract rejects this first
        return None
    return format(amount * fact.value.scale, "f")


def _definition(fact: FactRecord) -> str:
    """The readable definition line of a fact (account label + scopes)."""

    semantics = fact.semantics
    account = semantics.source_label or semantics.source_field or "原件科目未标注"
    parts = [
        f"原件科目：{account}",
        REPORTING_SCOPE_LABELS[semantics.reporting_scope],
        EQUITY_SCOPE_LABELS[semantics.equity_scope],
        STANDARD_LABELS[semantics.accounting_standard],
    ]
    per_share = PER_SHARE_LABELS.get(semantics.per_share_basis)
    if per_share is not None:
        parts.append(per_share)
    return "；".join(parts)


def _source_date(fact: FactRecord) -> date | None:
    """The publication date the source states, or ``None`` when it states none."""

    if fact.provenance.source_published_date is not None:
        return fact.provenance.source_published_date
    published_at = fact.provenance.source_published_at
    return None if published_at is None else published_at.date()


def _metric_view(result: MetricResult, *, report_currency: str) -> MetricView:
    """One metric result as displayed; its value is never rounded here.

    A metric that produced no value still reports the unit its own definition
    is stated in (an amount metric in the reporting currency, every other
    metric as a ratio) instead of borrowing one from an unrelated result.
    """

    if result.value is not None:
        unit, currency = result.value.unit, result.value.currency
    elif result.key in AMOUNT_METRICS:
        unit, currency = report_currency, report_currency
    else:
        unit, currency = "ratio", None
    return MetricView(
        key=result.key,
        label=result.label,
        status=result.status.value,
        value=None if result.value is None or result.value.value is None else str(result.value.value),
        unit=unit,
        currency=currency,
        basis=result.basis,
        periods=tuple(_period_label(period) for period in result.periods),
        notes=result.notes,
    )


def _period_label(period: FactPeriod) -> str:
    return period.label or period.end.isoformat()


def _capabilities(*, covered: bool, has_material: bool) -> dict[str, bool]:
    """The capability bits; not-delivered capabilities stay ``False``.

    ``financials`` is true exactly when the security is covered *and* at least
    one material dataset exists. The conditional capabilities (price, annual PE,
    dividends, PB) are not shipped by the first delivery and are therefore
    false; ``online_refresh`` mirrors the handler's licence flag, which decides
    whether a collection run may call upstream at all.
    """

    bits = dict.fromkeys(CAPABILITY_ORDER, False)
    bits["financials"] = covered and has_material
    bits["online_refresh"] = online_refresh_enabled()
    return bits


def _coverage(
    *, facts: Sequence[FactView], fiscal_years: Sequence[FiscalYearView]
) -> dict[str, int]:
    """Material coverage and verification coverage, counted separately."""

    return {
        "periods_covered": len(fiscal_years),
        "facts_total": len(facts),
        "facts_candidate": sum(1 for fact in facts if fact.verification == Verification.CANDIDATE),
        "facts_verified": sum(1 for fact in facts if fact.verification == Verification.VERIFIED),
    }


def _statuses(
    *,
    facts: Sequence[FactView],
    metrics: Sequence[MetricView],
    capabilities: dict[str, bool],
    newest_period_end: date | None,
    today: date,
) -> tuple[StatusView, ...]:
    """Every applicable business status, in the fixed report order.

    Each not-yet-delivered capability contributes its gap: the missing financial
    material and the conditional price/valuation/dividend capabilities are both
    named by ``material_missing``'s affected list, so a ``False`` bit is never
    left unexplained.
    """

    statuses: list[StatusView] = []
    material_capabilities = ("financials", *CONDITIONAL_CAPABILITIES)
    missing = [
        capability for capability in material_capabilities if not capabilities[capability]
    ]
    if missing:
        affected = tuple(
            function
            for capability in missing
            for function in CAPABILITY_FUNCTIONS[capability]
        )
        statuses.append(_status("material_missing", affected=affected))
    candidates = [fact for fact in facts if fact.verification == Verification.CANDIDATE]
    if candidates:
        statuses.append(_status("partial_verification"))
    not_applicable = [metric.label for metric in metrics if metric.status == "not_applicable"]
    if not_applicable:
        statuses.append(_status("metric_not_applicable", affected=tuple(not_applicable)))
    conflicts = sorted({fact.metric for fact in facts if fact.conflict == "unresolved"})
    if conflicts:
        statuses.append(
            _status(
                "source_conflict",
                affected=tuple(FACT_LABELS.get(metric, metric) for metric in conflicts),
            )
        )
    if newest_period_end is not None and newest_period_end < today - timedelta(
        days=MATERIAL_STALE_AFTER_DAYS
    ):
        statuses.append(_status("material_stale"))
    if not capabilities["online_refresh"]:
        statuses.append(_status("online_refresh_disabled"))
    return tuple(statuses)


def _status(code: str, *, affected: tuple[str, ...] | None = None) -> StatusView:
    """One status from the fixed copy; ``affected`` overrides when dynamic."""

    copy = STATUS_COPY[code]
    return StatusView(
        code=code,
        affected=copy.affected if affected is None else affected,
        next_step=copy.next_step,
    )


def _display_code(market_code: str, ticker: str) -> str:
    """The display form of a listing's checked code (``00005`` -> ``00005.HK``)."""

    return f"{ticker}{DISPLAY_SUFFIXES[market_code]}" if market_code in DISPLAY_SUFFIXES else ticker
