"""WP5 deterministic peer metric calculations and saved records.

Three layers are covered against the run-02 golden evidence and synthetic
batches:

* **Golden reproduction (pure layer).** The run-02 data is loaded through the
  WP2/WP3 import path, frozen into a :class:`~...calculations.FrozenBatch` and
  computed without any ``EvidenceRecord`` - the run-02 fields carry no
  disclosure-date basis, so they stay snapshot-only by design and the pure
  functions consume the snapshot facts directly. Every one of the 19 golden
  ``peers[]`` rows must reproduce (target value, median, midrank position,
  valid/candidate counts and the excluded codes).
* **Failure boundaries (pure layer).** The design's verification points:
  revenue-total misuse, double percent conversion, duplicate companies,
  cross-industry samples, period mixing, empty and zero values, a loss-making
  target, negative cash flow, fewer than five valid peers, and the tie
  behaviour of the ascending midrank position.
* **Saved chain (dated synthetic fixture).** Fields with a disclosure-date
  basis materialize through the WP4 trusted adapter into real ``SavedEvidence``;
  the builder cites them as ``fact_refs`` and :func:`save_peer_calculations`
  submits ``SavedCalculation`` records idempotently (same ``input_hash`` reuses
  the old UUID, a changed input appends a new record, a total gap creates no
  ``ok`` record). Synthetic dates live only in this fixture - the golden data
  is never given dates.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import func, select

from investment_agent.cli.commands.peer_annual import (
    import_peer_annual_snapshots,
    load_audit_json,
    load_sample_json,
)
from investment_agent.cli.commands.peer_cohort import import_sample_roster
from investment_agent.evidence.contracts import Period
from investment_agent.evidence.store import EvidenceService
from investment_agent.industry_peers.calculations import (
    APPLICABILITY_INPUT_MISSING,
    CFO_PARENT_PROFIT,
    CONTRACT_REVENUE,
    DUPLICATE_COMPANY,
    FIELD_CONFLICT,
    METHOD,
    METHOD_VERSION,
    METRIC_KEYS,
    MIN_VALID_PEERS,
    MISSING_FIELD,
    NON_POSITIVE_DENOMINATOR,
    NON_POSITIVE_EQUITY,
    NOT_IN_COHORT,
    PERIOD_MISMATCH,
    ROE,
    SOURCE_FIELD_MISMATCH,
    BatchCompany,
    BatchField,
    FrozenBatch,
    MetricGap,
    MetricOutcome,
    PeerExclusion,
    ascending_position,
    batch_field_from_snapshot,
    build_peer_metric_record,
    compute_peer_metric,
    find_peer_calculations,
    metric_input_hash,
    save_peer_calculations,
)
from investment_agent.industry_peers.contracts import (
    PeerAnnualField,
    PeerFieldVerification,
    PeerMetricStatus,
)
from investment_agent.industry_peers.evidence import peer_field_evidence
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.models.evidence import SavedCalculation
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.peer import PeerAnnualSnapshotRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN02_SAMPLE = REPO_ROOT / "research/industry-data-validation/evidence/run-02/sample.json"
DERIVED = REPO_ROOT / "research/industry-data-validation/evidence/run-02/derived"
SAMPLE_PATH = DERIVED / "baijiu-peer-sample.json"
AUDIT_PATH = DERIVED / "baijiu-official-audit.json"

ACQUIRED_AT = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
AS_OF = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
NOTICE_DATE = date(2026, 4, 25)
PERIOD_END = date(2025, 12, 31)
OWNER = "local_owner"
MAOTAI = "600519.SH"

COHORT_REF = ObjectRef(
    kind="peer_cohort_snapshot", id=UUID("00000000-0000-0000-0000-00000000c0a1"), revision=1
)
COHORT_HASH = "f" * 64


def dec(value: str) -> Decimal:
    return Decimal(value)


# --- golden batch built through the WP2/WP3 import path -----------------------


@async_fixture
async def golden(db_session):
    """run-02 fully imported: roster (WP2), annual facts and proofs (WP3)."""

    sample = load_sample_json(SAMPLE_PATH)
    listing_repo = ListingRepository(db_session)
    for entry in sample["companies"]:
        await listing_repo.ensure_a_share_listing(entry["code"], company_name=entry["name"])
    roster = await import_sample_roster(
        db_session, sample_path=RUN02_SAMPLE, acquired_at=ACQUIRED_AT
    )
    report = await import_peer_annual_snapshots(
        db_session,
        sample=sample,
        audit=load_audit_json(AUDIT_PATH),
        acquired_at=ACQUIRED_AT,
    )
    await db_session.commit()
    assert (report.inserted, report.skipped_existing) == (19, 0)
    return roster.snapshot


async def golden_batch(session: Any, cohort_row: Any, *, target_code: str = MAOTAI) -> FrozenBatch:
    """Freeze the imported snapshots into the pure calculation input."""

    rows = {
        row.company_id: row for row in (await session.execute(select(PeerAnnualSnapshot))).scalars()
    }
    entries = sorted(cohort_row.payload["entries"], key=lambda entry: entry["company_id"])
    target = next(entry for entry in entries if entry["ticker"] == target_code)
    companies = []
    for entry in entries:
        row = rows[entry["company_id"]]
        fields = {metric: batch_field_from_snapshot(row, metric) for metric in row.fields}
        companies.append(
            BatchCompany(
                company_id=entry["company_id"],
                listing_id=entry["listing_id"],
                ticker=entry["ticker"],
                name=entry["name"],
                in_cohort=True,
                report_period=row.report_period,
                fields=fields,
            )
        )
    return FrozenBatch(
        target_company_id=target["company_id"],
        report_period=PERIOD_END,
        cohort_ref=ObjectRef(
            kind="peer_cohort_snapshot", id=UUID(cohort_row.id), revision=cohort_row.revision
        ),
        cohort_content_hash=cohort_row.content_hash,
        companies=tuple(companies),
    )


# --- synthetic batches for the failure boundaries -----------------------------

BASE_FIELDS: dict[str, str] = {
    "roe_weighted_annual": "0.20",
    "equity_parent": "100",
    "contract_liabilities": "10",
    "operating_revenue": "100",
    "operating_cash_flow": "60",
    "net_profit_parent": "100",
}


def field_map(**overrides: str | BatchField) -> dict[str, BatchField]:
    """The synthetic default field map; overrides replace single entries."""

    fields = {metric: BatchField(value=dec(value)) for metric, value in BASE_FIELDS.items()}
    for name, value in overrides.items():
        fields[name] = value if isinstance(value, BatchField) else BatchField(value=dec(value))
    return fields


def make_company(
    company_id: int,
    *,
    ticker: str | None = None,
    in_cohort: bool = True,
    report_period: date = PERIOD_END,
    fields: dict[str, BatchField] | None = None,
) -> BatchCompany:
    return BatchCompany(
        company_id=company_id,
        listing_id=1000 + company_id,
        ticker=ticker or f"SYN{company_id:03d}",
        name=f"company-{company_id}",
        in_cohort=in_cohort,
        report_period=report_period,
        fields=fields if fields is not None else field_map(),
    )


def standard_companies(**target_overrides: str | BatchField) -> list[BatchCompany]:
    """Target + 6 peers; peer ROE 0.05..0.35, peer ratios 0.10 and 0.5..1.0."""

    roe_steps = ["0.05", "0.10", "0.15", "0.20", "0.25", "0.35"]
    cfo_steps = ["50", "60", "70", "80", "90", "100"]
    companies = [make_company(1, ticker="TARGET", fields=field_map(**target_overrides))]
    for index, (roe_value, cfo_value) in enumerate(zip(roe_steps, cfo_steps, strict=True)):
        companies.append(
            make_company(
                2 + index,
                fields=field_map(
                    roe_weighted_annual=roe_value,
                    operating_cash_flow=cfo_value,
                ),
            )
        )
    return companies


def make_batch(
    companies: list[BatchCompany],
    *,
    target_company_id: int = 1,
    report_period: date = PERIOD_END,
) -> FrozenBatch:
    return FrozenBatch(
        target_company_id=target_company_id,
        report_period=report_period,
        cohort_ref=COHORT_REF,
        cohort_content_hash=COHORT_HASH,
        companies=tuple(companies),
    )


def outcome_of(batch: FrozenBatch, metric_key: str) -> MetricOutcome:
    outcome = compute_peer_metric(batch, metric_key)
    assert isinstance(outcome, MetricOutcome)
    return outcome


def reason_of(outcome: MetricOutcome, company_id: int) -> str:
    """The exclusion reason of one company; the target carries its own field."""

    if outcome.target_exclusion is not None and outcome.target_exclusion.company_id == company_id:
        return outcome.target_exclusion.reason
    entries = [entry for entry in outcome.exclusions if entry.company_id == company_id]
    assert len(entries) == 1, (company_id, outcome.exclusions, outcome.target_exclusion)
    return entries[0].reason


# --- golden reproduction ------------------------------------------------------


async def test_golden_statistics_reproduce_for_maotai(golden, db_session) -> None:
    batch = await golden_batch(db_session, golden)

    roe = outcome_of(batch, ROE)
    assert roe.metric_status is PeerMetricStatus.COMPARABLE
    assert roe.target_value == dec("0.3253")
    assert roe.peer_median == dec("0.0739")
    assert roe.ascending_position is not None
    assert roe.ascending_position.quantize(Decimal("0.01")) == dec("94.44")
    assert (roe.valid_peer_count, roe.candidate_peer_count) == (18, 18)
    assert roe.exclusions == ()
    assert all(inclusion.company_id != batch.target_company_id for inclusion in roe.inclusions)

    cr = outcome_of(batch, CONTRACT_REVENUE)
    assert cr.metric_status is PeerMetricStatus.COMPARABLE
    assert cr.target_value is not None
    assert cr.target_value.quantize(Decimal("0.0001")) == dec("0.0474")
    assert cr.peer_median is not None
    assert cr.peer_median.quantize(Decimal("0.0001")) == dec("0.1630")
    assert cr.ascending_position is not None
    assert cr.ascending_position.quantize(Decimal("0.01")) == dec("16.67")
    assert (cr.valid_peer_count, cr.candidate_peer_count) == (18, 18)

    cfo = outcome_of(batch, CFO_PARENT_PROFIT)
    assert cfo.metric_status is PeerMetricStatus.COMPARABLE
    assert cfo.target_value is not None
    assert cfo.target_value.quantize(Decimal("0.0001")) == dec("0.7474")
    assert cfo.peer_median is not None
    assert cfo.peer_median.quantize(Decimal("0.0001")) == dec("0.5792")
    assert cfo.ascending_position is not None
    assert cfo.ascending_position.quantize(Decimal("0.01")) == dec("73.33")
    assert (cfo.valid_peer_count, cfo.candidate_peer_count) == (15, 18)
    assert {exclusion.ticker: exclusion.reason for exclusion in cfo.exclusions} == {
        "000799.SZ": NON_POSITIVE_DENOMINATOR,
        "000860.SZ": NON_POSITIVE_DENOMINATOR,
        "600199.SH": NON_POSITIVE_DENOMINATOR,
    }


async def test_every_golden_peer_row_reproduces(golden, db_session) -> None:
    """All 19 x 3 golden rows: counts, target, median, position, exclusions."""

    golden_peers = load_sample_json(SAMPLE_PATH)["peers"]
    key_map = {
        ROE: ("加权平均ROE", Decimal(100)),
        CONTRACT_REVENUE: ("合同负债/营业收入", Decimal(1)),
        CFO_PARENT_PROFIT: ("CFO/归母净利润", Decimal(1)),
    }
    batch = await golden_batch(db_session, golden)
    assert len(golden_peers) == 19
    for entry in golden_peers:
        target = next(c for c in batch.companies if c.ticker == entry["code"])
        mini = FrozenBatch(
            target_company_id=target.company_id,
            report_period=batch.report_period,
            cohort_ref=batch.cohort_ref,
            cohort_content_hash=batch.cohort_content_hash,
            companies=batch.companies,
        )
        for metric_key, (sample_key, scale) in key_map.items():
            outcome = outcome_of(mini, metric_key)
            expected = entry["metrics"][sample_key]
            assert outcome.valid_peer_count == expected["valid_peer_count"], (
                entry["code"],
                metric_key,
            )
            assert outcome.candidate_peer_count == expected["candidate_peer_count"]
            for actual, expected_value in (
                (outcome.target_value, expected["target_value"]),
                (outcome.peer_median, expected["peer_median"]),
            ):
                if expected_value is None:
                    assert actual is None
                else:
                    assert actual is not None
                    assert float(actual) * float(scale) == pytest.approx(
                        float(expected_value), rel=1e-9, abs=1e-12
                    )
            if expected["ascending_midrank_percentile"] is None:
                assert outcome.ascending_position is None
            else:
                assert outcome.ascending_position is not None
                assert float(outcome.ascending_position) == pytest.approx(
                    float(expected["ascending_midrank_percentile"]), rel=1e-9, abs=1e-12
                )
            assert {e.ticker for e in outcome.exclusions} == set(
                expected.get("excluded_peer_codes") or []
            )


async def test_run02_original_backed_fields_are_materialized(golden, db_session) -> None:
    """Official metadata supplies real dates; only core original-backed fields materialize."""

    rows = list((await db_session.execute(select(PeerAnnualSnapshot))).scalars())
    assert len(rows) == 19
    for row in rows:
        for metric in row.fields:
            materialization = peer_field_evidence(row, metric)
            if metric == "revenue_total_check":
                assert not materialization.materialized
            else:
                assert materialization.materialized
                assert materialization.record.verification == "verified"



# --- failure boundaries (design verification points) --------------------------


def test_revenue_total_check_is_rejected_as_denominator() -> None:
    """The total-revenue check-only field is never an operating-revenue input."""

    companies = standard_companies(
        operating_revenue=BatchField(value=dec("110"), source_semantic="revenue_total_check")
    )
    batch = make_batch(companies)
    outcome = outcome_of(batch, CONTRACT_REVENUE)
    # The target's mislabeled input excludes it; the peer median still stands.
    assert outcome.target_value is None
    assert reason_of(outcome, 1) == SOURCE_FIELD_MISMATCH
    assert outcome.metric_status is PeerMetricStatus.MISSING
    assert outcome.peer_median == dec("0.10")

    # The same misuse on a peer shrinks the valid set instead of computing.
    companies[2] = make_company(
        3,
        fields=field_map(
            operating_revenue=BatchField(value=dec("100"), source_semantic="revenue_total_check")
        ),
    )
    peer_outcome = outcome_of(make_batch(companies), CONTRACT_REVENUE)
    assert peer_outcome.valid_peer_count == 5
    assert reason_of(peer_outcome, 3) == SOURCE_FIELD_MISMATCH


def test_roe_percent_is_never_converted_twice() -> None:
    """The stored fraction is used as-is: 0.3253 stays 0.3253 (32.53%)."""

    companies = standard_companies(roe_weighted_annual="0.3253")
    outcome = outcome_of(make_batch(companies), ROE)
    assert outcome.target_value == dec("0.3253")
    assert outcome.target_value != dec("0.003253")
    assert outcome.ascending_position is not None
    # 5 of 6 peers strictly below -> 83.33%, computed on the fraction values.
    assert outcome.ascending_position.quantize(Decimal("0.01")) == dec("83.33")


def test_duplicate_company_is_excluded_wherever_it_appears() -> None:
    companies = standard_companies()
    # One peer appears twice with conflicting numbers: no silent first-wins.
    companies.append(make_company(4, fields=field_map(roe_weighted_annual="0.99")))
    outcome = outcome_of(make_batch(companies), ROE)
    assert reason_of(outcome, 4) == DUPLICATE_COMPANY
    assert outcome.valid_peer_count == 5
    assert outcome.candidate_peer_count == 6
    # The duplicated values never enter the median (0.15 or 0.99 would).
    assert outcome.peer_median == dec("0.20")
    with pytest.raises(ValueError, match="exactly once"):
        make_batch(standard_companies() + [make_company(1)])


def test_cross_industry_company_outside_the_roster_is_excluded() -> None:
    companies = standard_companies() + [make_company(9, ticker="BANK", in_cohort=False)]
    for metric_key in METRIC_KEYS:
        outcome = outcome_of(make_batch(companies), metric_key)
        assert reason_of(outcome, 9) == NOT_IN_COHORT
        assert outcome.candidate_peer_count == 6
        assert outcome.valid_peer_count == 6


def test_period_mismatch_is_never_mixed_in() -> None:
    companies = standard_companies()
    companies[3] = make_company(4, report_period=date(2024, 12, 31))
    outcome = outcome_of(make_batch(companies), ROE)
    assert reason_of(outcome, 4) == PERIOD_MISMATCH
    assert outcome.valid_peer_count == 5
    assert outcome.peer_median == dec("0.20")
    # A stale-period target has no comparable value either.
    stale_target = [make_company(1, report_period=date(2024, 12, 31)), *companies[1:]]
    target_outcome = outcome_of(make_batch(stale_target), ROE)
    assert target_outcome.target_value is None
    assert reason_of(target_outcome, 1) == PERIOD_MISMATCH
    assert target_outcome.metric_status is PeerMetricStatus.MISSING
    assert target_outcome.peer_median == dec("0.20")


def test_null_and_zero_values_stay_distinct() -> None:
    # A null denominator is a missing input - never an applicability claim.
    companies = standard_companies(operating_revenue=BatchField.missing("source value is null"))
    outcome = outcome_of(make_batch(companies), CONTRACT_REVENUE)
    assert reason_of(outcome, 1) == MISSING_FIELD
    assert outcome.metric_status is PeerMetricStatus.MISSING
    assert outcome.peer_median == dec("0.10")

    # An original zero is a value: the applicability exclusion is evidence-backed.
    zero = standard_companies(operating_revenue="0")
    zero_outcome = outcome_of(make_batch(zero), CONTRACT_REVENUE)
    assert reason_of(zero_outcome, 1) == NON_POSITIVE_DENOMINATOR
    assert zero_outcome.metric_status is PeerMetricStatus.TARGET_NOT_APPLICABLE
    assert zero_outcome.peer_median == dec("0.10")
    assert zero_outcome.target_exclusion is not None
    assert "0" in zero_outcome.target_exclusion.detail


def test_roe_without_equity_cannot_claim_inapplicability() -> None:
    """Missing applicability input is an unknown - not 'not applicable'."""

    companies = standard_companies(equity_parent=BatchField.missing("source value is null"))
    outcome = outcome_of(make_batch(companies), ROE)
    assert reason_of(outcome, 1) == APPLICABILITY_INPUT_MISSING
    assert outcome.metric_status is PeerMetricStatus.MISSING
    assert outcome.peer_median is not None

    # A non-positive equity IS an evidence-backed applicability exclusion.
    negative = standard_companies(equity_parent="-1")
    negative_outcome = outcome_of(make_batch(negative), ROE)
    assert reason_of(negative_outcome, 1) == NON_POSITIVE_EQUITY
    assert negative_outcome.metric_status is PeerMetricStatus.TARGET_NOT_APPLICABLE
    assert negative_outcome.peer_median is not None


def test_loss_target_keeps_the_peer_median() -> None:
    companies = standard_companies(net_profit_parent="-20")
    outcome = outcome_of(make_batch(companies), CFO_PARENT_PROFIT)
    assert outcome.metric_status is PeerMetricStatus.TARGET_NOT_APPLICABLE
    assert outcome.target_value is None
    assert reason_of(outcome, 1) == NON_POSITIVE_DENOMINATOR
    assert outcome.peer_median == dec("0.75")
    assert outcome.ascending_position is None
    assert (outcome.valid_peer_count, outcome.candidate_peer_count) == (6, 6)
    # The other metrics are unaffected: the three metrics stay independent.
    assert outcome_of(make_batch(companies), ROE).metric_status is PeerMetricStatus.COMPARABLE


def test_negative_cash_flow_is_a_value_not_an_error() -> None:
    companies = standard_companies()
    companies[2] = make_company(3, fields=field_map(operating_cash_flow="-30"))
    outcome = outcome_of(make_batch(companies), CFO_PARENT_PROFIT)
    assert outcome.valid_peer_count == 6
    included = {inclusion.company_id: inclusion.value for inclusion in outcome.inclusions}
    assert included[3] == dec("-0.30")
    # Median of -0.3, 0.5, 0.7, 0.8, 0.9, 1.0
    assert outcome.peer_median == (dec("0.7") + dec("0.8")) / 2


def test_fewer_than_five_valid_peers_saves_only_coverage() -> None:
    companies = standard_companies()
    companies[2] = make_company(
        3, fields=field_map(roe_weighted_annual=BatchField.missing("source value is null"))
    )
    companies[3] = make_company(
        4, fields=field_map(roe_weighted_annual=BatchField.missing("source value is null"))
    )
    batch = make_batch(companies)
    outcome = outcome_of(batch, ROE)
    assert outcome.metric_status is PeerMetricStatus.INSUFFICIENT_PEERS
    assert (outcome.valid_peer_count, outcome.candidate_peer_count) == (4, 6)
    assert outcome.peer_median is None
    assert outcome.ascending_position is None
    assert outcome.target_value == dec("0.20")
    built = build_peer_metric_record(
        batch,
        ROE,
        evidence_refs=dummy_refs(companies, ("roe_weighted_annual", "equity_parent")),
        as_of=AS_OF,
    )
    assert built.record is not None
    assert set(built.record.results) == {"target", "valid_peer_count", "candidate_peer_count"}
    assert built.record.status.value == "ok"
    assert built.record.details["metric_status"] == "insufficient_peers"


def dummy_refs(
    companies: list[BatchCompany], metrics: tuple[str, ...]
) -> dict[tuple[int, str], ObjectRef]:
    return {
        (company.company_id, metric): ObjectRef(
            kind="evidence", id=UUID(int=company.company_id * 1000 + index), revision=1
        )
        for company in companies
        for index, metric in enumerate(metrics)
    }


def test_tie_positions_use_the_midrank_rule() -> None:
    """(strictly_below + 0.5 * equal) / valid_peer_count * 100, fixed here."""

    def roe_batch(target_roe: str, peer_roes: list[str]) -> FrozenBatch:
        companies = [make_company(1, fields=field_map(roe_weighted_annual=target_roe))]
        companies += [
            make_company(2 + index, fields=field_map(roe_weighted_annual=value))
            for index, value in enumerate(peer_roes)
        ]
        return make_batch(companies)

    # Golden shape (no ties): strictly below / n. Target above all -> 100%.
    top = outcome_of(roe_batch("0.90", ["0.05", "0.10", "0.15", "0.20", "0.25", "0.35"]), ROE)
    assert top.ascending_position == Decimal(100)
    # Target strictly below all -> 0%.
    bottom = outcome_of(roe_batch("0.01", ["0.05", "0.10", "0.15", "0.20", "0.25", "0.35"]), ROE)
    assert bottom.ascending_position == Decimal(0)
    # Two of six peers equal the target: (1 + 1) / 6 * 100 = 33.33%.
    tied = outcome_of(roe_batch("0.20", ["0.10", "0.20", "0.20", "0.30", "0.40", "0.50"]), ROE)
    assert tied.ascending_position is not None
    assert tied.ascending_position.quantize(Decimal("0.01")) == dec("33.33")
    # Target equal to every peer: exactly 50%.
    all_equal = outcome_of(roe_batch("0.20", ["0.20"] * (MIN_VALID_PEERS + 1)), ROE)
    assert all_equal.ascending_position == Decimal(50)
    assert ascending_position([dec("1"), dec("2"), dec("2"), dec("3")], dec("2")) == Decimal(50)


def stripped_companies(*remove: str) -> list[BatchCompany]:
    """The standard cohort with whole field columns removed for every company."""

    return [
        make_company(
            company.company_id,
            ticker=company.ticker,
            in_cohort=company.in_cohort,
            report_period=company.report_period,
            fields={name: entry for name, entry in company.fields.items() if name not in remove},
        )
        for company in standard_companies()
    ]


def test_metrics_are_independent_of_each_others_gaps() -> None:
    """A missing cash flow never blocks ROE (and vice versa)."""

    full = make_batch(standard_companies())
    gaps = make_batch(stripped_companies("operating_cash_flow", "net_profit_parent"))
    assert outcome_of(full, ROE).target_value == outcome_of(gaps, ROE).target_value
    assert outcome_of(full, ROE).peer_median == outcome_of(gaps, ROE).peer_median
    assert outcome_of(full, ROE).valid_peer_count == outcome_of(gaps, ROE).valid_peer_count == 6
    assert isinstance(compute_peer_metric(gaps, CFO_PARENT_PROFIT), MetricGap)


def test_metric_gap_when_no_usable_fact_exists() -> None:
    companies = stripped_companies("operating_cash_flow", "net_profit_parent")
    gap = compute_peer_metric(make_batch(companies), CFO_PARENT_PROFIT)
    assert isinstance(gap, MetricGap)
    assert gap.candidate_peer_count == 6
    assert gap.exclusions
    assert all(exclusion.reason == MISSING_FIELD for exclusion in gap.exclusions)


def test_conflicted_input_is_excluded_and_marked_conflict() -> None:
    companies = standard_companies(
        roe_weighted_annual=BatchField(
            value=dec("0.30"), conflict_reason="two saved sources disagree"
        )
    )
    outcome = outcome_of(make_batch(companies), ROE)
    assert reason_of(outcome, 1) == FIELD_CONFLICT
    assert outcome.metric_status is PeerMetricStatus.CONFLICT
    assert outcome.peer_median == dec("0.175")
    assert outcome.ascending_position is None


def test_input_hash_covers_the_declared_dependency_scope() -> None:
    companies = standard_companies()
    base = make_batch(companies)

    def hash_of(batch: FrozenBatch, metric_key: str, **decision: Any) -> str:
        return metric_input_hash(batch, metric_key, **decision)

    full_peers = [2, 3, 4, 5, 6, 7]
    baseline = hash_of(
        base,
        ROE,
        included_peer_ids=full_peers,
        excluded=[],
        target_included=True,
        target_value=dec("0.20"),
    )
    assert baseline == hash_of(
        base,
        ROE,
        included_peer_ids=full_peers,
        excluded=[],
        target_included=True,
        target_value=dec("0.20"),
    )  # deterministic, independent of call order

    changed_target = make_batch(standard_companies(roe_weighted_annual="0.21"))
    assert (
        hash_of(
            changed_target,
            ROE,
            included_peer_ids=full_peers,
            excluded=[],
            target_included=True,
            target_value=dec("0.20"),
        )
        != baseline
    )

    changed_peer = standard_companies()
    changed_peer[2] = make_company(3, fields=field_map(roe_weighted_annual="0.11"))
    assert (
        hash_of(
            make_batch(changed_peer),
            ROE,
            included_peer_ids=full_peers,
            excluded=[],
            target_included=True,
            target_value=dec("0.20"),
        )
        != baseline
    )

    changed_exclusions = standard_companies()
    changed_exclusions[2] = make_company(3, report_period=date(2024, 12, 31))
    assert (
        hash_of(
            make_batch(changed_exclusions),
            ROE,
            included_peer_ids=[2, 4, 5, 6, 7],
            excluded=[PeerExclusion(3, "SYN003", "company-3", PERIOD_MISMATCH, "d")],
            target_included=True,
            target_value=dec("0.20"),
        )
        != baseline
    )

    changed_cohort = FrozenBatch(
        target_company_id=1,
        report_period=PERIOD_END,
        cohort_ref=COHORT_REF,
        cohort_content_hash="0" * 64,
        companies=base.companies,
    )
    assert (
        hash_of(
            changed_cohort,
            ROE,
            included_peer_ids=full_peers,
            excluded=[],
            target_included=True,
            target_value=dec("0.20"),
        )
        != baseline
    )

    assert (
        hash_of(
            base,
            CFO_PARENT_PROFIT,
            included_peer_ids=full_peers,
            excluded=[],
            target_included=True,
            target_value=dec("0.60"),
        )
        != baseline
    )  # a different metric is a different dependency statement


def test_batch_field_shape_matches_the_wp1_discipline() -> None:
    with pytest.raises(ValueError, match="requires a reason"):
        BatchField(value=None)
    with pytest.raises(ValueError, match="cannot carry one"):
        BatchField(value=dec("1"), missing_reason="no")
    assert BatchField.missing("gap").missing_reason == "gap"


# --- saved chain on the dated synthetic fixture -------------------------------

FIELD_KINDS: dict[str, tuple[str, str, str | None]] = {
    "operating_revenue": ("flow", "CNY", "CNY"),
    "net_profit_parent": ("flow", "CNY", "CNY"),
    "operating_cash_flow": ("flow", "CNY", "CNY"),
    "equity_parent": ("stock", "CNY", "CNY"),
    "contract_liabilities": ("stock", "CNY", "CNY"),
    "roe_weighted_annual": ("ratio", "fraction", None),
}
SYNTHETIC_ROSTER: tuple[tuple[str, str], ...] = (
    ("600001.SH", "目标公司"),
    ("600002.SH", "同行二"),
    ("600003.SH", "同行三"),
    ("600004.SH", "同行四"),
    ("600005.SH", "同行五"),
    ("600006.SH", "同行六"),
    ("600007.SH", "同行七"),
)
# target 0.30 ROE / 0.15 contract-revenue / 0.75 cfo; peers 0.05..0.35, 0.10, 0.5..1.0
SYNTHETIC_VALUES: tuple[dict[str, str], ...] = (
    {
        "roe_weighted_annual": "0.30",
        "equity_parent": "500",
        "contract_liabilities": "15",
        "operating_revenue": "100",
        "operating_cash_flow": "75",
        "net_profit_parent": "100",
    },
) + tuple(
    {
        "roe_weighted_annual": roe,
        "equity_parent": "100",
        "contract_liabilities": "10",
        "operating_revenue": "100",
        "operating_cash_flow": cfo,
        "net_profit_parent": "100",
    }
    for roe, cfo in zip(
        ("0.05", "0.10", "0.15", "0.20", "0.25", "0.35"),
        ("50", "60", "70", "80", "90", "100"),
        strict=True,
    )
)


def synthetic_field(metric: str, value: str) -> PeerAnnualField:
    kind, unit, currency = FIELD_KINDS[metric]
    if kind == "flow":
        period = Period(start=date(2025, 1, 1), end=PERIOD_END, basis="period_to_date/annual")
        raw_unit = "CNY"
    elif kind == "stock":
        period = Period(start=PERIOD_END, end=PERIOD_END, basis="point_in_time")
        raw_unit = "CNY"
    else:
        period = Period(start=date(2025, 1, 1), end=PERIOD_END, basis="annual_ratio")
        raw_unit = "percent"
    saved = Decimal(value)
    raw_value = str(saved * 100) if metric == "roe_weighted_annual" else value
    return PeerAnnualField(
        metric=metric,
        value=saved,
        raw_value=raw_value,
        unit=unit,
        currency=currency,
        period=period,
        source_field="SYNTHETIC",
        raw_unit=raw_unit,
        disclosed_on=NOTICE_DATE,
        acquired_at=ACQUIRED_AT,
        verification=PeerFieldVerification.VERIFIED,
    )


def synthetic_proof(metric: str, value: str) -> dict[str, Any]:
    excerpt = f"synthetic {metric} {value}"
    return {
        "metric": metric,
        "status": "verified",
        "original_ref": None,
        "pdf_hash": None,
        "report_identity": None,
        "pdf_page": 3,
        "header": metric,
        "raw_value": value,
        "raw_unit": FIELD_KINDS[metric][1],
        "parsed_value": value,
        "period": PERIOD_END.isoformat(),
        "accounting_scope": None,
        "locator_excerpt": excerpt,
        "locator_excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        "checked_at": None,
        "checked_by": None,
        "check_rule_version": None,
        "audit_record": {
            "source": "synthetic-fixture",
            "index": 0,
            "code": "synthetic",
            "field": metric,
            "api_key": "SYNTHETIC",
        },
    }


async def store_snapshot(
    session: Any,
    company_id: int,
    listing_id: int,
    values: dict[str, str],
    *,
    semantic_hash: str,
    overrides: dict[str, str] | None = None,
) -> PeerAnnualSnapshot:
    """Append one snapshot version exactly as the WP3 importer would store it."""

    merged = {**values, **(overrides or {})}
    return await PeerAnnualSnapshotRepository(session).add(
        PeerAnnualSnapshot(
            id=str(uuid4()),
            company_id=company_id,
            listing_id=listing_id,
            report_period=PERIOD_END,
            provider="synthetic",
            adapter_version="wp5-fixture-v1",
            captured_at=ACQUIRED_AT,
            content_hash=semantic_hash,
            semantic_hash=semantic_hash,
            fields={
                metric: synthetic_field(metric, value).model_dump(mode="json")
                for metric, value in merged.items()
            },
            verification={
                metric: synthetic_proof(metric, value) for metric, value in merged.items()
            },
        )
    )


async def materialize_snapshot(
    session: Any, row: PeerAnnualSnapshot
) -> dict[tuple[int, str], ObjectRef]:
    """Materialize every field of one snapshot and save the evidence rows."""

    service = EvidenceService(session)
    refs: dict[tuple[int, str], ObjectRef] = {}
    for metric in row.fields:
        materialization = peer_field_evidence(row, metric)
        assert materialization.materialized and materialization.record is not None
        refs[(row.company_id, metric)] = await service.save_evidence(
            OWNER, materialization.record, uuid4()
        )
    await session.flush()
    return refs


def batch_companies_from_rows(
    rows: list[PeerAnnualSnapshot], listings: dict[int, tuple[str, str]]
) -> list[BatchCompany]:
    companies = []
    for row in sorted(rows, key=lambda item: item.company_id):
        ticker, name = listings[row.company_id]
        companies.append(
            BatchCompany(
                company_id=row.company_id,
                listing_id=row.listing_id,
                ticker=ticker,
                name=name,
                in_cohort=True,
                report_period=row.report_period,
                fields={metric: batch_field_from_snapshot(row, metric) for metric in row.fields},
            )
        )
    return companies


def synthetic_batch(
    rows: list[PeerAnnualSnapshot],
    listings: dict[int, tuple[str, str]],
    *,
    target_company_id: int = 1,
) -> FrozenBatch:
    return FrozenBatch(
        target_company_id=target_company_id,
        report_period=PERIOD_END,
        cohort_ref=COHORT_REF,
        cohort_content_hash=COHORT_HASH,
        companies=tuple(batch_companies_from_rows(rows, listings)),
    )


def company_fields(row: PeerAnnualSnapshot) -> dict[str, BatchField]:
    return {metric: batch_field_from_snapshot(row, metric) for metric in row.fields}


async def calculation_count(session: Any) -> int:
    return int(await session.scalar(select(func.count()).select_from(SavedCalculation)) or 0)


@async_fixture
async def saved_fixture(db_session):
    """Seven dated snapshot versions plus their materialized evidence refs."""

    listings: dict[int, tuple[str, str]] = {}
    rows: list[PeerAnnualSnapshot] = []
    for index, ((ticker, name), values) in enumerate(
        zip(SYNTHETIC_ROSTER, SYNTHETIC_VALUES, strict=True)
    ):
        listing = await ListingRepository(db_session).ensure_a_share_listing(
            ticker, company_name=name
        )
        listings[listing.company_id] = (ticker, name)
        assert listing.company_id == index + 1
        rows.append(
            await store_snapshot(
                db_session,
                listing.company_id,
                listing.id,
                values,
                semantic_hash=f"{index + 1:02d}" * 32,
            )
        )
    refs: dict[tuple[int, str], ObjectRef] = {}
    for row in rows:
        refs.update(await materialize_snapshot(db_session, row))
    await db_session.commit()
    return {"rows": rows, "refs": refs, "listings": listings}


async def test_saved_chain_builds_and_saves_ok_records(db_session, saved_fixture) -> None:
    batch = synthetic_batch(saved_fixture["rows"], saved_fixture["listings"])
    result = await save_peer_calculations(
        db_session, owner_id=OWNER, batch=batch, evidence_refs=saved_fixture["refs"], as_of=AS_OF
    )
    await db_session.commit()

    assert [entry.state for entry in result.metric_refs] == ["saved", "saved", "saved"]
    assert (result.target_ticker, result.report_period) == ("600001.SH", PERIOD_END)

    saved = await find_peer_calculations(
        db_session, owner_id=OWNER, target_company_id=1, report_period=PERIOD_END
    )
    assert len(saved) == 3
    by_metric = {entry.record.details["metric_key"]: entry for entry in saved}

    roe = by_metric[ROE].record
    assert roe.method == METHOD and roe.method_version == METHOD_VERSION
    assert roe.status.value == "ok" and roe.applicable is True
    assert roe.details["metric_status"] == "comparable"
    assert roe.results["target"].value == dec("0.30")
    assert roe.results["target"].unit == "fraction"
    assert roe.results["peer_median"].value == dec("0.175")
    assert roe.results["ascending_position"].unit == "percent"
    assert roe.results["ascending_position"].value is not None
    assert roe.results["ascending_position"].value.quantize(Decimal("0.01")) == dec("83.33")
    assert roe.results["valid_peer_count"].value == 6
    assert roe.results["valid_peer_count"].unit == "count"
    assert roe.results["candidate_peer_count"].value == 6
    # fact_refs: 7 companies x (roe value + equity applicability) = 14
    assert len(roe.fact_refs) == 14
    service = EvidenceService(db_session)
    for ref in roe.fact_refs:
        resolved = await service.get_evidence(OWNER, ref, AS_OF)
        assert resolved.data is not None and resolved.status == "ok"
    assert roe.details["target"]["ticker"] == "600001.SH"
    assert roe.details["cohort"]["content_hash"] == COHORT_HASH
    assert roe.details["batch_run_id"] is None
    assert len(roe.details["included_companies"]) == 6
    assert roe.details["excluded_companies"] == []
    assert "verified: 14" in roe.quality[0]

    cfo = by_metric[CFO_PARENT_PROFIT].record
    assert cfo.results["target"].value == dec("0.75")
    assert cfo.results["target"].unit == "ratio"
    assert cfo.results["peer_median"].value == dec("0.75")
    assert cfo.results["ascending_position"].value is not None
    assert cfo.results["ascending_position"].value.quantize(Decimal("0.01")) == dec("50.00")
    assert len(cfo.fact_refs) == 14  # 7 x (cash flow + parent profit)

    cr = by_metric[CONTRACT_REVENUE].record
    assert cr.results["target"].value == dec("0.15")
    assert cr.results["peer_median"].value == dec("0.10")
    assert cr.results["ascending_position"].value == Decimal(100)


async def test_same_input_hash_reuses_the_saved_uuid(db_session, saved_fixture) -> None:
    batch = synthetic_batch(saved_fixture["rows"], saved_fixture["listings"])
    first = await save_peer_calculations(
        db_session, owner_id=OWNER, batch=batch, evidence_refs=saved_fixture["refs"], as_of=AS_OF
    )
    await db_session.commit()
    before = await calculation_count(db_session)

    second = await save_peer_calculations(
        db_session, owner_id=OWNER, batch=batch, evidence_refs=saved_fixture["refs"], as_of=AS_OF
    )
    await db_session.commit()

    assert [entry.state for entry in second.metric_refs] == ["reused", "reused", "reused"]
    assert [entry.ref for entry in second.metric_refs] == [entry.ref for entry in first.metric_refs]
    assert await calculation_count(db_session) == before


async def test_changed_input_appends_new_records(db_session, saved_fixture) -> None:
    batch = synthetic_batch(saved_fixture["rows"], saved_fixture["listings"])
    await save_peer_calculations(
        db_session, owner_id=OWNER, batch=batch, evidence_refs=saved_fixture["refs"], as_of=AS_OF
    )
    await db_session.commit()
    before = await calculation_count(db_session)

    # Peer five's ROE is corrected: a new snapshot version, a new evidence row.
    peer_row = saved_fixture["rows"][4]
    updated = await store_snapshot(
        db_session,
        peer_row.company_id,
        peer_row.listing_id,
        SYNTHETIC_VALUES[4],
        semantic_hash="ff" * 32,
        overrides={"roe_weighted_annual": "0.26"},
    )
    refs = dict(saved_fixture["refs"])
    refs.update(await materialize_snapshot(db_session, updated))
    rows = [row for row in saved_fixture["rows"] if row.company_id != peer_row.company_id]
    rows.append(updated)
    changed = synthetic_batch(rows, saved_fixture["listings"])

    second = await save_peer_calculations(
        db_session, owner_id=OWNER, batch=changed, evidence_refs=refs, as_of=AS_OF
    )
    await db_session.commit()

    # No per-result-key trimming: the whole frozen input is the dependency.
    assert [entry.state for entry in second.metric_refs] == ["saved", "saved", "saved"]
    assert await calculation_count(db_session) == before + 3
    versions = await find_peer_calculations(
        db_session, owner_id=OWNER, target_company_id=1, metric_key=ROE
    )
    assert len(versions) == 2
    assert len({version.record.input_hash for version in versions}) == 2
    corrected = next(
        version
        for version in versions
        if version.record.results["peer_median"].value == dec("0.20")
    )
    assert corrected.record.details["included_companies"][3]["value"] == "0.26"


async def test_metric_gap_creates_no_ok_record(db_session, saved_fixture) -> None:
    """A metric without any usable source fact keeps a gap and saves nothing."""

    rows = saved_fixture["rows"]
    companies = []
    for row in sorted(rows, key=lambda item: item.company_id):
        ticker, name = saved_fixture["listings"][row.company_id]
        companies.append(
            BatchCompany(
                company_id=row.company_id,
                listing_id=row.listing_id,
                ticker=ticker,
                name=name,
                in_cohort=True,
                report_period=row.report_period,
                fields={
                    metric: entry
                    for metric, entry in company_fields(row).items()
                    if metric not in {"operating_cash_flow", "net_profit_parent"}
                },
            )
        )
    gap_batch = FrozenBatch(
        target_company_id=1,
        report_period=PERIOD_END,
        cohort_ref=COHORT_REF,
        cohort_content_hash=COHORT_HASH,
        companies=tuple(companies),
    )
    result = await save_peer_calculations(
        db_session,
        owner_id=OWNER,
        batch=gap_batch,
        evidence_refs=saved_fixture["refs"],
        as_of=AS_OF,
    )
    await db_session.commit()

    cfo = result.ref_for(CFO_PARENT_PROFIT)
    assert cfo is not None and cfo.state == "gap" and cfo.ref is None
    assert cfo.gap_reason is not None and "no usable source fact" in cfo.gap_reason
    roe = result.ref_for(ROE)
    assert roe is not None and roe.state == "saved" and roe.ref is not None
    assert await calculation_count(db_session) == 2  # roe + contract revenue only
    assert (
        await find_peer_calculations(db_session, owner_id=OWNER, metric_key=CFO_PARENT_PROFIT) == []
    )


async def test_loss_target_saves_median_with_applicability_evidence(
    db_session, saved_fixture
) -> None:
    """target_not_applicable keeps the median and cites the parent-profit fact."""

    rows = list(saved_fixture["rows"])
    loss_row = rows[0]
    updated = await store_snapshot(
        db_session,
        loss_row.company_id,
        loss_row.listing_id,
        SYNTHETIC_VALUES[0],
        semantic_hash="ee" * 32,
        overrides={"net_profit_parent": "-20"},
    )
    refs = dict(saved_fixture["refs"])
    refs.update(await materialize_snapshot(db_session, updated))
    rows = [row for row in rows if row.company_id != loss_row.company_id]
    rows.append(updated)
    batch = synthetic_batch(rows, saved_fixture["listings"])

    result = await save_peer_calculations(
        db_session, owner_id=OWNER, batch=batch, evidence_refs=refs, as_of=AS_OF
    )
    await db_session.commit()

    cfo_ref = result.ref_for(CFO_PARENT_PROFIT)
    assert cfo_ref is not None and cfo_ref.state == "saved"
    saved = await find_peer_calculations(
        db_session,
        owner_id=OWNER,
        target_company_id=1,
        report_period=PERIOD_END,
        metric_key=CFO_PARENT_PROFIT,
    )
    assert len(saved) == 1
    record = saved[0].record
    assert record.details["metric_status"] == "target_not_applicable"
    assert set(record.results) == {"peer_median", "valid_peer_count", "candidate_peer_count"}
    assert record.results["peer_median"].value == dec("0.75")
    assert record.applicable is True and record.status.value == "ok"
    # The loss fact itself is cited: value inputs + the target's applicability proof.
    assert len(record.fact_refs) == 13  # 6 peers x 2 + the target's net profit
    roles = {
        (item["company_id"], item["field"], item["role"])
        for item in record.details["fact_evidence"]
    }
    assert (1, "net_profit_parent", "applicability") in roles
    exclusion = record.details["excluded_companies"][0]
    assert exclusion["company_id"] == 1
    assert exclusion["reason"] == NON_POSITIVE_DENOMINATOR


async def test_query_filters_by_method_target_period_and_metric(db_session, saved_fixture) -> None:
    batch = synthetic_batch(saved_fixture["rows"], saved_fixture["listings"])
    await save_peer_calculations(
        db_session, owner_id=OWNER, batch=batch, evidence_refs=saved_fixture["refs"], as_of=AS_OF
    )
    await db_session.commit()

    assert len(await find_peer_calculations(db_session, owner_id=OWNER)) == 3
    assert len(await find_peer_calculations(db_session, owner_id=OWNER, metric_key=ROE)) == 1
    assert (
        len(
            await find_peer_calculations(
                db_session, owner_id=OWNER, target_company_id=1, report_period=PERIOD_END
            )
        )
        == 3
    )
    assert (
        await find_peer_calculations(
            db_session, owner_id=OWNER, target_company_id=1, report_period=date(2024, 12, 31)
        )
        == []
    )
    assert await find_peer_calculations(db_session, owner_id=OWNER, target_company_id=999) == []
    assert await find_peer_calculations(db_session, owner_id="other_owner") == []
