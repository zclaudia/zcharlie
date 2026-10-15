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

The controlled bank applicability set is recorded in :data:`BANK_COMPANY_KEYS`.
Latestness comes only from a validated review receipt and its checked period;
this module does not infer a filing deadline from the current date.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
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
    SCHEMA_VERSION,
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
    fact_from_payload,
    fact_to_payload,
)
from investment_agent.materials.equity_reconciliation import evaluate_equity_reconciliation
from investment_agent.materials.evidence import dataset_facts
from investment_agent.materials.jobs import MATERIAL_COLLECTION
from investment_agent.materials.metrics import (
    MetricResult,
    compute_metrics,
    profile_from_identity,
)
from investment_agent.storage.models.analysis import PipelineRun
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

REVIEWED_ANNUAL_SOURCE: Final[str] = "issuer.reviewed_annual.v1"
"""Source id reserved for validated, offline reviewed annual material."""

STATUS_CODES: Final[tuple[str, ...]] = (
    "listing_not_covered",
    "material_missing",
    "partial_verification",
    "metric_not_applicable",
    "source_conflict",
    "raw_archive_missing",
    "material_stale",
    "online_refresh_disabled",
    "partial_coverage",
    "awaiting_original_review",
    "latestness_unknown",
    "failed_refresh",
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
    "partial_coverage": _StatusCopy(
        affected=("财务展示", "指标计算"),
        next_step="补齐最近六类字段及连续四个财年的可用证据；不适用项目单独标注。",
    ),
    "awaiting_original_review": _StatusCopy(
        affected=("原件复核",),
        next_step="逐项对照发行人原件并保存复核回执；导入时间不代表原件核对时间。",
    ),
    "latestness_unknown": _StatusCopy(
        affected=("最新性判断",),
        next_step="取得并核对发行人当前年报索引后再判断资料是否最新。",
    ),
    "failed_refresh": _StatusCopy(
        affected=("资料刷新",),
        next_step="查看刷新运行原因并修复来源问题；失败运行不会更新最近成功采集时间。",
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
    evidence_kind: str = "reported"


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
class FreshnessView:
    last_requested_at: datetime | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_imported_at: datetime | None = None
    last_verified_on: date | None = None
    latest_period_end: date | None = None
    latestness: str = "unknown"
    latestness_checked_on: date | None = None
    sources: dict[str, dict[str, str | bool]] = field(default_factory=dict)


@dataclass(frozen=True)
class RefreshLifecycleView:
    run_id: int | None = None
    status: str | None = None
    reason: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class MetricCoverageView:
    applicable: bool
    evidence_status: str
    usable_facts: int
    verified_facts: int


@dataclass(frozen=True)
class ReviewReceiptView:
    checked_on: date
    latest_period_end: date
    manifest_sha256: str
    company_key: str


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
    freshness: FreshnessView = field(default_factory=FreshnessView)
    refresh: RefreshLifecycleView = field(default_factory=RefreshLifecycleView)
    metric_coverage: dict[str, MetricCoverageView] = field(default_factory=dict)


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
    material = _current_versions(datasets, expected_company_key=identity.company_key)
    review_acquisitions = _valid_review_receipts(
        company_id=company.id, datasets=material, company_key=identity.company_key
    )
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
    capabilities = _capabilities(
        covered=True,
        has_material=any(
            fact.value.raw_value is not None and fact.conflict != "unresolved"
            for fact in material_facts
        ),
    )
    coverage = _coverage(facts=facts, fiscal_years=fiscal_views)
    freshest_review = max(
        review_acquisitions.values(),
        key=lambda item: item.checked_on,
        default=None,
    )
    latest_review_period = (
        None if freshest_review is None else freshest_review.latest_period_end
    )
    # Latestness is about every selected financial fact, including an instant
    # opening balance that may not belong to the annual fiscal-year display.
    max_period = max((fact.period.end for fact in material_facts), default=None)
    latestness = (
        "unknown" if freshest_review is None
        else "stale" if max_period is not None and latest_review_period is not None and latest_review_period < max_period
        else "checked_through"
    )
    freshness, refresh = await _freshness(
        session,
        listing_id=listing.id,
        datasets=datasets,
        review_acquisitions=review_acquisitions,
        max_period=max_period,
        latest_period_end=latest_review_period,
        latestness=latestness,
        latestness_checked_on=None if freshest_review is None else freshest_review.checked_on,
    )
    metric_coverage = _metric_coverage(facts, metrics)
    statuses = _statuses(
        facts=facts,
        metrics=metrics,
        capabilities=capabilities,
        latestness=latestness,
        refresh_status=refresh.status,
        fiscal_period_ends=tuple(year.end for year in fiscal_views),
        has_valid_original_review=bool(review_acquisitions),
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
        freshness=freshness,
        refresh=refresh,
        metric_coverage=metric_coverage,
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
        freshness=FreshnessView(sources=_source_states()),
        refresh=RefreshLifecycleView(),
        metric_coverage={},
    )


# --- material, facts and metrics ------------------------------------------------


def _current_versions(
    datasets: Sequence[ResearchDataset], *, expected_company_key: str | None = None
) -> tuple[ResearchDataset, ...]:
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
        existing = newest.get(key)
        if existing is None or dataset.revision >= existing.revision:
            newest[key] = dataset
    selected_rows = [
        row for (source, end, report), row in newest.items()
        if report is None
        or (source, end, None) not in newest
        or row.revision > newest[(source, end, None)].revision
    ]
    reviewed_windows: list[tuple[date, date, ResearchDataset, ReviewReceiptView]] = []
    for row in selected_rows:
        envelope = (
            _valid_review_envelope(row, expected_company_key=expected_company_key)
            if row.source == REVIEWED_ANNUAL_SOURCE
            else None
        )
        if envelope is not None:
            period_ends = [fact.period.end for fact in dataset_facts(row)]
            if period_ends:
                reviewed_windows.append((min(period_ends), max(period_ends), row, envelope))
    latest_reviewed = max(
        reviewed_windows,
        key=lambda item: (
            item[3].latest_period_end,
            item[3].checked_on,
            item[2].revision,
            item[2].id or 0,
        ),
        default=None,
    )
    if latest_reviewed is not None:
        reviewed_windows = [latest_reviewed]
    # The reviewed package contains an authoritative multi-year fact selection.
    # Suppress legacy provider versions inside that exact covered window to
    # avoid duplicate scope-conflict inputs, while retaining older history and
    # newer provider periods beyond the verified horizon.
    selected_rows = [
        row for row in selected_rows
        if (latest_reviewed is not None and row.id == latest_reviewed[2].id)
        or row.period_end is None
        or not any(start <= row.period_end <= end for start, end, _, _ in reviewed_windows)
    ]
    return tuple(sorted(selected_rows, key=lambda row: (row.period_end or date.min, row.source)))


def _valid_review_envelope(
    dataset: ResearchDataset, *, expected_company_key: str | None
) -> ReviewReceiptView | None:
    """Validate the review envelope against this immutable dataset's facts.

    A `review` flag by itself is not evidence. The reserved source, schema,
    company, canonical fact hashes, period horizon and actual import timestamp
    must agree before its checked date can be surfaced.
    """

    if dataset.source != REVIEWED_ANNUAL_SOURCE or dataset.kind != ANNUAL_FACTS:
        return None
    if dataset.subject_kind != COMPANY_SUBJECT or dataset.schema_version != SCHEMA_VERSION:
        return None
    review = dataset.payload.get("review")
    if not isinstance(review, dict):
        return None
    if review.get("schema_version") != 1 or review.get("status") != "complete":
        return None
    company_key = review.get("company_key")
    if not isinstance(company_key, str) or not company_key:
        return None
    if expected_company_key is not None and company_key != expected_company_key:
        return None
    manifest = review.get("manifest_sha256")
    if not isinstance(manifest, str) or len(manifest) != 64 or any(
        character not in "0123456789abcdef" for character in manifest
    ):
        return None
    checked_on = _iso_date(review.get("checked_on"))
    latest_end = _iso_date(review.get("latest_period_end"))
    if checked_on is None or latest_end is None or checked_on > dataset.captured_at.date():
        return None
    if review.get("latest_published_on") is not None and _iso_date(
        review.get("latest_published_on")
    ) is None:
        return None
    latest_published = _iso_date(review.get("latest_published_on"))
    if latest_published is not None and (latest_published > checked_on or latest_end > latest_published):
        return None
    if not isinstance(review.get("issues"), list) or review["issues"]:
        return None
    for key in (
        "documents", "fact_evidence", "superseded_facts", "equity_reconciliations",
        "equity_scope_reviews", "conflict_resolutions",
    ):
        if not isinstance(review.get(key), list):
            return None
    documents = review["documents"]
    evidence = review["fact_evidence"]
    document_raw_refs = {
        document.get("id"): document.get("raw_ref")
        for document in documents
        if isinstance(document, dict)
        and isinstance(document.get("id"), str)
        and isinstance(document.get("raw_ref"), str)
    }
    document_ids = set(document_raw_refs)
    if not documents or len(document_ids) != len(documents):
        return None
    for document in documents:
        raw_ref = document.get("raw_ref")
        digest = document.get("sha256")
        if (
            not isinstance(raw_ref, str)
            or not raw_ref
            or "\\" in raw_ref
            or PurePosixPath(raw_ref).is_absolute()
            or ".." in PurePosixPath(raw_ref).parts
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            return None
    calculation_refs: set[str] = set()
    for reconciliation in review["equity_reconciliations"]:
        if not isinstance(reconciliation, Mapping):
            return None
        try:
            evaluation = evaluate_equity_reconciliation(reconciliation)
        except (TypeError, ValueError):
            return None
        calculation_refs.add(evaluation["calculation_ref"])
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get("references"), list):
            return None
        references = item["references"]
        if not references:
            return None
        for reference in references:
            if not isinstance(reference, dict):
                return None
            if reference.get("kind") == "derived_calculation":
                digest = reference.get("calculation_ref")
                if (
                    not isinstance(digest, str)
                    or digest not in calculation_refs
                ):
                    return None
            else:
                document_id = reference.get("document_id")
                if (
                    document_id not in document_ids
                    or reference.get("raw_ref") != document_raw_refs[document_id]
                    or not isinstance(reference.get("locator"), (str, Mapping))
                    or not isinstance(reference.get("excerpt"), str)
                ):
                    return None
        equivalents = item.get("equivalent_facts", [])
        if not isinstance(equivalents, list):
            return None
        for equivalent in equivalents:
            if (
                not isinstance(equivalent, dict)
                or not isinstance(equivalent.get("fact"), dict)
                or not isinstance(equivalent.get("fact_sha256"), str)
                or not isinstance(equivalent.get("references"), list)
                or not equivalent["references"]
            ):
                return None
            alternate_hash = hashlib.sha256(
                json.dumps(
                    equivalent["fact"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            if alternate_hash != equivalent["fact_sha256"]:
                return None
            for reference in equivalent["references"]:
                if (
                    not isinstance(reference, dict)
                    or reference.get("document_id") not in document_ids
                    or reference.get("raw_ref") != document_raw_refs[reference.get("document_id")]
                    or not isinstance(reference.get("locator"), (str, Mapping))
                    or not isinstance(reference.get("excerpt"), str)
                ):
                    return None
    try:
        facts = dataset_facts(dataset)
    except (TypeError, ValueError):
        return None
    if any(fact.verification != Verification.VERIFIED for fact in facts):
        return None
    fact_by_hash = {
        hashlib.sha256(
            json.dumps(
                fact_to_payload(fact), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest(): fact
        for fact in facts
    }
    supplied_hashes = set()
    for entry in evidence:
        if not isinstance(entry, dict) or not isinstance(entry.get("fact_sha256"), str):
            return None
        if entry.get("fact_sha256") in supplied_hashes:
            return None
        supplied_hashes.add(entry["fact_sha256"])
    if not fact_by_hash or set(fact_by_hash) != supplied_hashes:
        return None
    for item in evidence:
        fact = fact_by_hash[item["fact_sha256"]]
        for reference in item["references"]:
            if reference.get("kind") == "derived_calculation" and (
                fact.provenance.source != "material.equity_reconciliation"
                or reference.get("calculation_input_hash") != fact.provenance.original_hash
            ):
                return None
            if reference.get("kind") != "derived_calculation":
                locator = reference.get("locator")
                if fact.original_locator is not None:
                    canonical_locator = (
                        locator
                        if isinstance(locator, str)
                        else json.dumps(
                            locator, sort_keys=True, separators=(",", ":")
                        )
                    )
                    if canonical_locator != fact.original_locator:
                        return None
        for equivalent in item.get("equivalent_facts", []):
            alternate = equivalent["fact"]
            # An equivalent rendering is proof for the selected fact only when
            # its canonical semantics and value agree; it must never become a
            # second selected application fact.
            try:
                alternate_fact = fact_from_payload(alternate)
            except (AttributeError, TypeError, ValueError):
                return None
            if (
                alternate_fact.metric != fact.metric
                or (
                    alternate_fact.period.start,
                    alternate_fact.period.end,
                    alternate_fact.period.basis,
                    alternate_fact.period.days,
                ) != (
                    fact.period.start,
                    fact.period.end,
                    fact.period.basis,
                    fact.period.days,
                )
                or alternate_fact.value != fact.value
                or alternate_fact.semantics.metric != fact.semantics.metric
                or alternate_fact.semantics.accounting_standard != fact.semantics.accounting_standard
                or alternate_fact.semantics.reporting_scope != fact.semantics.reporting_scope
                or alternate_fact.semantics.equity_scope != fact.semantics.equity_scope
                or alternate_fact.semantics.per_share_basis != fact.semantics.per_share_basis
                or alternate_fact.provenance.source != fact.provenance.source
                or alternate_fact.verification != fact.verification
                or alternate_fact.conflict != fact.conflict
                or alternate_fact.applicability != fact.applicability
                or alternate_fact.conflict_note != fact.conflict_note
            ):
                return None
            if alternate_fact.original_locator is not None:
                for reference in equivalent["references"]:
                    locator = reference["locator"]
                    canonical_locator = (
                        locator if isinstance(locator, str)
                        else json.dumps(locator, sort_keys=True, separators=(",", ":"))
                    )
                    if canonical_locator != alternate_fact.original_locator:
                        return None
    annual_ends = [fact.period.end for fact in facts if fact.period.basis.value == "annual"]
    if not annual_ends or max(annual_ends) != latest_end:
        return None
    if dataset.period_end is not None and dataset.period_end != latest_end:
        return None
    latest_start = review.get("latest_period_start")
    if latest_start is not None:
        start_date = _iso_date(latest_start)
        latest_fact_starts = [
            fact.period.start for fact in facts
            if fact.period.end == latest_end and fact.period.start is not None
        ]
        if start_date is None or (latest_fact_starts and start_date not in latest_fact_starts):
            return None
    return ReviewReceiptView(
        checked_on=checked_on,
        latest_period_end=latest_end,
        manifest_sha256=manifest,
        company_key=company_key,
    )


def _iso_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _valid_review_receipts(
    *,
    company_id: int,
    datasets: Sequence[ResearchDataset],
    company_key: str,
) -> dict[int, ReviewReceiptView]:
    """Validated review envelopes attached to immutable imported datasets."""

    candidate_reviews = {
        row.id: envelope
        for row in datasets
        if (envelope := _valid_review_envelope(row, expected_company_key=company_key)) is not None
        and row.subject_id == company_id
    }
    if not candidate_reviews:
        return {}
    return candidate_reviews


async def _freshness(
    session: AsyncSession,
    *,
    listing_id: int,
    datasets: Sequence[ResearchDataset],
    review_acquisitions: dict[int, ReviewReceiptView],
    max_period: date | None,
    latest_period_end: date | None,
    latestness: str,
    latestness_checked_on: date | None,
) -> tuple[FreshnessView, RefreshLifecycleView]:
    """Read request, worker attempt, provider success, import and review times."""

    runs = (
        await session.execute(
            select(PipelineRun)
            .where(PipelineRun.kind == MATERIAL_COLLECTION, PipelineRun.listing_id == listing_id)
            .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
        )
    ).scalars().all()
    latest = runs[0] if runs else None
    last_success: datetime | None = None
    for run in runs:
        if run.status not in {"success", "partial"} or run.finished_at is None:
            continue
        metadata = run.metadata_json if isinstance(run.metadata_json, dict) else {}
        source_status = metadata.get("source_status")
        saved = metadata.get("saved")
        if not isinstance(source_status, dict) or not isinstance(saved, list):
            continue
        saved_ids = {
            row.get("dataset_id") for row in saved
            if isinstance(row, dict) and isinstance(row.get("dataset_id"), int)
        }
        if not saved_ids:
            continue
        actual = [
            row for row in datasets
            if row.id in saved_ids
            and row.source in {SOURCE_EASTMONEY_HK, SOURCE_SEC_COMPANYFACTS}
            and source_status.get(row.source) == "saved"
            and dataset_facts(row)
        ]
        if actual:
            last_success = run.finished_at
            break
    last_review = max(
        (item.checked_on for item in review_acquisitions.values()),
        default=None,
    )
    imported_at = max(
        (row.captured_at for row in datasets if row.source == REVIEWED_ANNUAL_SOURCE),
        default=None,
    )
    refresh = RefreshLifecycleView(
        run_id=None if latest is None else latest.id,
        status=None if latest is None else latest.status,
        reason=None if latest is None else _refresh_reason(latest.status, latest.request_json),
            started_at=None if latest is None else _as_utc(latest.claimed_at),
            completed_at=None if latest is None else _as_utc(latest.finished_at),
    )
    source_states = _source_states()
    for source in source_states:
        source_states[source]["enabled"] = online_refresh_enabled()
        source_states[source]["state"] = "enabled" if online_refresh_enabled() else "disabled"
    return (
        FreshnessView(
            last_requested_at=None if latest is None else _as_utc(latest.created_at),
            last_attempt_at=_as_utc(
                max((row.claimed_at for row in runs if row.claimed_at), default=None)
            ),
            last_success_at=_as_utc(last_success),
            last_imported_at=_as_utc(imported_at),
            last_verified_on=last_review,
            latest_period_end=max_period,
            latestness=latestness,
            latestness_checked_on=latestness_checked_on,
            sources=source_states,
        ),
        refresh,
    )


def _refresh_reason(status: str, request: object) -> str | None:
    if status == "failed":
        return "刷新运行失败"
    if status == "partial":
        return "部分来源未完成"
    reason = request.get("reason") if isinstance(request, dict) else None
    return reason if isinstance(reason, str) else None


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalize stored instants to explicit UTC; DB-naive values are UTC by convention."""

    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _source_states() -> dict[str, dict[str, str | bool]]:
    return {
        "hk_annual_reports": {"enabled": False, "licensing": "unknown", "state": "disabled"},
        "us_issuer_facts": {"enabled": False, "licensing": "unknown", "state": "disabled"},
    }


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
        sorted({
            dataset.source for dataset in datasets
            if dataset.id not in linked_ids and dataset.source != REVIEWED_ANNUAL_SOURCE
        })
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
        source=(
            "复核计算"
            if fact.provenance.source == "material.equity_reconciliation"
            else SOURCE_DOCUMENT_LABELS.get(dataset.source, OTHER_DOCUMENT_LABEL)
        ),
        source_date=_source_date(fact),
        locator=fact.original_locator,
        evidence_kind=(
            "derived_calculation"
            if fact.provenance.source == "material.equity_reconciliation"
            else "reported"
        ),
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
    one usable financial fact exists. The conditional capabilities (price, annual PE,
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
    latestness: str,
    refresh_status: str | None,
    fiscal_period_ends: tuple[date, ...],
    has_valid_original_review: bool,
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
    if facts and not has_valid_original_review:
        statuses.append(_status("awaiting_original_review"))
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
    if facts and (not _has_four_continuous_years(fiscal_period_ends) or _latest_category_count(facts) < 6):
        statuses.append(_status("partial_coverage"))
    if latestness == "stale":
        statuses.append(_status("material_stale"))
    elif latestness == "unknown":
        statuses.append(_status("latestness_unknown"))
    if refresh_status == "failed":
        statuses.append(_status("failed_refresh"))
    if not capabilities["online_refresh"]:
        statuses.append(_status("online_refresh_disabled"))
    return tuple(statuses)


def _metric_coverage(
    facts: Sequence[FactView], metrics: Sequence[MetricView]
) -> dict[str, MetricCoverageView]:
    """Per-core-metric applicability and usable fact evidence, separate from N/A."""

    inputs: dict[str, frozenset[str]] = {
        "revenue_yoy": frozenset({"revenue_operating", "revenue_net_credit", "revenue_gross"}),
        "revenue_cagr_3y": frozenset({"revenue_operating", "revenue_net_credit", "revenue_gross"}),
        "roe_avg_equity": frozenset({"profit_parent", "profit_ordinary_shareholders", "net_profit", "equity_parent", "equity_ordinary"}),
        "debt_ratio": frozenset({"total_assets", "total_liabilities"}),
        "cfo_to_profit": frozenset({"operating_cash_flow", "profit_parent", "profit_ordinary_shareholders", "net_profit"}),
        "fcf_simple": frozenset({"operating_cash_flow", "capex_ppe"}),
    }
    output: dict[str, MetricCoverageView] = {}
    for metric in metrics:
        related = [fact for fact in facts if fact.metric in inputs.get(metric.key, frozenset())]
        usable = [fact for fact in related if fact.amount is not None and fact.conflict != "unresolved"]
        verified = [fact for fact in usable if fact.verification == Verification.VERIFIED]
        candidate = [fact for fact in usable if fact.verification == Verification.CANDIDATE]
        applicable = metric.status != "not_applicable"
        if not applicable:
            state = "not_applicable"
        elif not usable:
            state = "missing"
        elif verified and candidate:
            state = "mixed"
        elif verified:
            state = "verified"
        else:
            state = "candidate"
        output[metric.key] = MetricCoverageView(
            applicable=applicable,
            evidence_status=state,
            usable_facts=len(usable),
            verified_facts=len(verified),
        )
    return output


def _latest_category_count(facts: Sequence[FactView]) -> int:
    """How many required latest-period categories have direct usable evidence.

    Coverage is distinct from calculability: a validated explicit N/A/missing
    fact records coverage, while ``metric_coverage`` separately says whether
    a value can be calculated. Liabilities are not one of the acceptance
    package's six required categories; operating cash flow and capex are.
    """

    if not facts:
        return 0
    latest = max(fact.period_end for fact in facts)
    latest_facts = [
        fact for fact in facts
        if fact.period_end == latest
        and fact.conflict != "unresolved"
        and (fact.amount is not None or fact.missing_reason is not None)
    ]
    metrics = {fact.metric for fact in latest_facts}
    categories = (
        bool(metrics & {"revenue_operating", "revenue_gross", "revenue_net_credit"}),
        bool(metrics & {"net_profit", "profit_parent", "profit_ordinary_shareholders"}),
        "total_assets" in metrics,
        bool(metrics & {"total_equity", "equity_parent", "equity_ordinary"}),
        "operating_cash_flow" in metrics,
        "capex_ppe" in metrics,
    )
    return sum(categories)


def _has_four_continuous_years(period_ends: tuple[date, ...]) -> bool:
    ordered = sorted(set(period_ends))
    return len(ordered) >= 4 and all(
        300 <= (right - left).days <= 400
        for left, right in zip(ordered[-4:-1], ordered[-3:], strict=True)
    )


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
