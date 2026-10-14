"""The SEC concept table and the static selectors derived from it (plan 04).

Split out of ``us_sec.py`` so the source fact knowledge — which taxonomy concept
means which metric, with the caveats of that binding — changes at its own pace.
Nothing here parses documents, selects periods or builds payloads.

* :data:`CONCEPT_MAP` (built with :class:`ConceptBinding` / ``_b``) is the source
  of truth; :data:`METRIC_ORDER` fixes the metric order inside one draft's
  ``facts`` list; ``_TAXONOMY_WHITELIST`` names the accepted taxonomies.
* the static selectors read that table and nothing else: ``_choose_taxonomy``
  and ``_metrics_for`` / ``_bindings_of`` / ``_bindings_for`` /
  ``_metric_concepts`` pick the taxonomy, the metric and the concept sets, and
  ``_ExpectedUnit`` / ``_expected_unit`` / ``_unit_matches`` /
  ``_unit_skipped_counts`` state and apply the unit each binding expects in the
  issuer's report currency.

``us_sec.py`` imports from here — never the other way around. ``FactRow``, the
keyed CompanyFacts observation these selectors scan, belongs to
``us_sec_periods.py`` and is imported from there.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from investment_agent.materials.contracts import EquityScope, PeriodBasis, PerShareBasis
from investment_agent.materials.us_sec_periods import FactRow

_TAXONOMY_WHITELIST: tuple[str, ...] = ("us-gaap", "ifrs-full")

_UnitKind = Literal["currency", "per_share", "shares"]


# --- the concept table ---


@dataclass(frozen=True)
class ConceptBinding:
    """One taxonomy concept bound to a metric, with the caveats of that binding."""

    taxonomy: str
    concept: str
    metric: str
    unit_kind: _UnitKind
    basis: PeriodBasis
    equity_scope: EquityScope
    per_share_basis: PerShareBasis
    priority: int
    note: str


def _b(
    taxonomy: str,
    concept: str,
    metric: str,
    unit_kind: _UnitKind,
    basis: PeriodBasis,
    equity_scope: EquityScope,
    per_share_basis: PerShareBasis = PerShareBasis.NA,
    priority: int = 0,
    note: str = "",
) -> ConceptBinding:
    return ConceptBinding(
        taxonomy=taxonomy,
        concept=concept,
        metric=metric,
        unit_kind=unit_kind,
        basis=basis,
        equity_scope=equity_scope,
        per_share_basis=per_share_basis,
        priority=priority,
        note=note,
    )


#: Concept table. Every entry states the source concept's own definition; the
#: metric is this application's name for it. Both travel on the fact
#: (``source_field`` / ``source_label``), so a definition difference stays
#: visible instead of being silently normalised away.
CONCEPT_MAP: tuple[ConceptBinding, ...] = (
    # --- us-gaap (10-K filers: AAPL, COST, JPM, XOM) -------------------------
    _b(
        "us-gaap",
        "Revenues",
        "revenue_operating",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note=(
            "standard Revenues; for XOM this includes equity-method income and other "
            "revenue (349,585M FY2024) and is explicitly not the pure sales line "
            "(339,247M). The plan binds XOM's revenue_operating to this concept."
        ),
    ),
    _b(
        "us-gaap",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "revenue_operating",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        priority=1,
        note="contract revenue excluding assessed taxes; the current tag for AAPL/COST.",
    ),
    _b(
        "us-gaap",
        "NetIncomeLoss",
        "net_profit",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.ALL_PARENT,
        note=(
            "standard GAAP net income as filed (JPM 58,471M FY2024). us-gaap defines it "
            "as the portion attributable to the parent."
        ),
    ),
    _b(
        "us-gaap",
        "ProfitLoss",
        "profit_parent",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note=(
            "bound to profit_parent exactly as the plan's table says. us-gaap:ProfitLoss "
            "is the consolidated result *including* noncontrolling interests (XOM FY2024 "
            "35,063M vs NetIncomeLoss 33,680M), so the fact keeps equity_scope=total and "
            "never stands in for a parent-only number."
        ),
    ),
    _b(
        "us-gaap",
        "StockholdersEquity",
        "equity_parent",
        "currency",
        PeriodBasis.INSTANT,
        EquityScope.ALL_PARENT,
        note=(
            "total stockholders' equity (all parent equity holders, preferred included); "
            "never filled from or into equity_ordinary."
        ),
    ),
    _b(
        "us-gaap",
        "Assets",
        "total_assets",
        "currency",
        PeriodBasis.INSTANT,
        EquityScope.TOTAL,
        note="consolidated total assets, instant fact (no start).",
    ),
    _b(
        "us-gaap",
        "Liabilities",
        "total_liabilities",
        "currency",
        PeriodBasis.INSTANT,
        EquityScope.TOTAL,
        note="consolidated total liabilities, instant fact (no start).",
    ),
    _b(
        "us-gaap",
        "NetCashProvidedByUsedInOperatingActivities",
        "operating_cash_flow",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note="cash provided by (used in) operating activities, as reported (JPM is negative).",
    ),
    _b(
        "us-gaap",
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "capex_ppe",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note=(
            "one cash expenditure component (PP&E purchases), not a universal capex "
            "definition. JPM does not tag it, so the metric is reported as missing."
        ),
    ),
    _b(
        "us-gaap",
        "EarningsPerShareBasic",
        "eps_basic",
        "per_share",
        PeriodBasis.ANNUAL,
        EquityScope.ORDINARY,
        PerShareBasis.BASIC,
        note="basic earnings per share of the issuer's common stock.",
    ),
    _b(
        "us-gaap",
        "EarningsPerShareDiluted",
        "eps_diluted",
        "per_share",
        PeriodBasis.ANNUAL,
        EquityScope.ORDINARY,
        PerShareBasis.DILUTED,
        note="diluted earnings per share of the issuer's common stock.",
    ),
    _b(
        "us-gaap",
        "WeightedAverageNumberOfSharesOutstandingBasic",
        "shares_weighted_basic",
        "shares",
        PeriodBasis.ANNUAL,
        EquityScope.ORDINARY,
        note="weighted average basic shares outstanding over the fiscal year.",
    ),
    _b(
        "us-gaap",
        "CommonStockSharesOutstanding",
        "shares_outstanding_end",
        "shares",
        PeriodBasis.INSTANT,
        EquityScope.ORDINARY,
        note="common shares outstanding at the balance-sheet date (instant fact).",
    ),
    # ``dei:EntityCommonStockSharesOutstanding`` is the plan's other candidate for
    # shares_outstanding_end. Rule 7 keeps ``dei`` outside the whitelist, so its
    # rows are counted in ``unmapped.taxonomies`` and never produce a fact; a test
    # asserts exactly that, and the us-gaap tag above carries the metric.
    # --- ifrs-full (20-F filer: TSM) -----------------------------------------
    _b(
        "ifrs-full",
        "Revenue",
        "revenue_operating",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note="IFRS revenue from contracts with customers, 20-F series only.",
    ),
    _b(
        "ifrs-full",
        "ProfitLoss",
        "net_profit",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note="IFRS consolidated profit for the period, including non-controlling interests.",
    ),
    _b(
        "ifrs-full",
        "ProfitLossAttributableToOwnersOfParent",
        "profit_parent",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.ALL_PARENT,
        note=(
            "the IFRS concept the plan's bare ``ProfitLoss`` entry means for a 20-F filer: "
            "TSM FY2024 1,158,380.2M TWD as filed in the 20-F, never the local-report "
            "figure and never stitched together with it."
        ),
    ),
    _b(
        "ifrs-full",
        "EquityAttributableToOwnersOfParent",
        "equity_parent",
        "currency",
        PeriodBasis.INSTANT,
        EquityScope.ALL_PARENT,
        note="equity attributable to owners of the parent; excludes non-controlling interests.",
    ),
    _b(
        "ifrs-full",
        "Assets",
        "total_assets",
        "currency",
        PeriodBasis.INSTANT,
        EquityScope.TOTAL,
        note="consolidated total assets, instant fact.",
    ),
    _b(
        "ifrs-full",
        "Liabilities",
        "total_liabilities",
        "currency",
        PeriodBasis.INSTANT,
        EquityScope.TOTAL,
        note="consolidated total liabilities, instant fact.",
    ),
    _b(
        "ifrs-full",
        "CashFlowsFromUsedInOperatingActivities",
        "operating_cash_flow",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note="IFRS cash flows from (used in) operating activities.",
    ),
    _b(
        "ifrs-full",
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        "capex_ppe",
        "currency",
        PeriodBasis.ANNUAL,
        EquityScope.TOTAL,
        note="cash purchases of PP&E classified as investing activities.",
    ),
    _b(
        "ifrs-full",
        "BasicEarningsLossPerShare",
        "eps_basic",
        "per_share",
        PeriodBasis.ANNUAL,
        EquityScope.ORDINARY,
        PerShareBasis.BASIC,
        note="basic earnings (loss) per share, TWD — no USD convenience value is read.",
    ),
    _b(
        "ifrs-full",
        "DilutedEarningsLossPerShare",
        "eps_diluted",
        "per_share",
        PeriodBasis.ANNUAL,
        EquityScope.ORDINARY,
        PerShareBasis.DILUTED,
        note="diluted earnings (loss) per share, TWD — no USD convenience value is read.",
    ),
    _b(
        "ifrs-full",
        "WeightedAverageShares",
        "shares_weighted_basic",
        "shares",
        PeriodBasis.ANNUAL,
        EquityScope.ORDINARY,
        note="weighted average number of shares over the fiscal year.",
    ),
    # TSM's share count is tagged as NumberOfSharesIssuedAndFullyPaid (issued and
    # fully paid). That is a different definition from "outstanding", so this
    # adapter does not bind it to shares_outstanding_end; the metric is simply
    # not part of the IFRS metric set.
)


#: Deterministic metric order inside one draft's ``facts`` list.
METRIC_ORDER: tuple[str, ...] = (
    "revenue_operating",
    "net_profit",
    "profit_parent",
    "total_assets",
    "total_liabilities",
    "equity_parent",
    "operating_cash_flow",
    "capex_ppe",
    "eps_basic",
    "eps_diluted",
    "shares_weighted_basic",
    "shares_outstanding_end",
)


# --- unit and metric selectors ---


@dataclass(frozen=True)
class _ExpectedUnit:
    unit: str
    currency: str | None
    raw_unit: str


def _expected_unit(binding: ConceptBinding, report_currency: str) -> _ExpectedUnit:
    if binding.unit_kind == "currency":
        return _ExpectedUnit(report_currency, report_currency, report_currency)
    if binding.unit_kind == "per_share":
        return _ExpectedUnit(
            f"{report_currency}/share", report_currency, f"{report_currency}/shares"
        )
    return _ExpectedUnit("shares", None, "shares")


def _unit_matches(unit: str, binding: ConceptBinding, report_currency: str) -> bool:
    if binding.unit_kind == "currency":
        return unit == report_currency
    if binding.unit_kind == "per_share":
        return unit in (f"{report_currency}/share", f"{report_currency}/shares")
    return unit == "shares"


def _metric_concepts(taxonomy: str, metric: str) -> frozenset[str]:
    return frozenset(
        binding.concept
        for binding in CONCEPT_MAP
        if binding.taxonomy == taxonomy and binding.metric == metric
    )


def _choose_taxonomy(rows: Sequence[FactRow], report_currency: str) -> str:
    """Whitelisted taxonomy satisfying the most mapped metrics (ties: us-gaap)."""

    best = _TAXONOMY_WHITELIST[0]
    best_score = -1
    for taxonomy in _TAXONOMY_WHITELIST:
        bindings = _bindings_of(taxonomy)
        present = {(row.concept, row.unit) for row in rows if row.taxonomy == taxonomy}
        score = sum(
            1
            for metric in _metrics_for(taxonomy)
            if any(
                binding.metric == metric
                and (binding.concept, _expected_unit(binding, report_currency).raw_unit) in present
                for binding in bindings
            )
        )
        if score > best_score:
            best, best_score = taxonomy, score
    return best


def _bindings_of(taxonomy: str) -> tuple[ConceptBinding, ...]:
    return tuple(binding for binding in CONCEPT_MAP if binding.taxonomy == taxonomy)


def _metrics_for(taxonomy: str) -> tuple[str, ...]:
    bound = {binding.metric for binding in _bindings_of(taxonomy)}
    return tuple(metric for metric in METRIC_ORDER if metric in bound)


def _bindings_for(taxonomy: str, concept: str) -> tuple[ConceptBinding, ...]:
    return tuple(binding for binding in _bindings_of(taxonomy) if binding.concept == concept)


def _unit_skipped_counts(
    rows: Sequence[FactRow], taxonomy: str, report_currency: str
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if row.taxonomy != taxonomy:
            continue
        bindings = _bindings_for(taxonomy, row.concept)
        if not bindings:
            continue
        if any(_unit_matches(row.unit, binding, report_currency) for binding in bindings):
            continue
        counts[row.unit] = counts.get(row.unit, 0) + 1
    return counts
