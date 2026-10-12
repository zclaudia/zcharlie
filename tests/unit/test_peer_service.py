"""WP6 read-only peer-context service, batch mappings and offline submission.

Coverage follows the design's status table (§任务触发与失败处理) and the WP6
verification points:

* **Status rows** - outside the roster (not_supported), no submission
  (missing with the job-hint slot), the run-02 offline batch (partial with
  per-metric valid counts, check coverage and the evidence gap), a
  non-applicable target (median kept), fewer than five valid peers (no
  statistics), and a conflicted input (conflict, exclusion, median kept).
* **Cutoff** - a batch submitted after the cutoff is invisible; a calculation
  record created after the cutoff is excluded per metric while earlier
  records of the same batch still show; nothing acquired later can hide
  behind a younger record because a record created before the cutoff cannot
  cite later-acquired evidence.
* **Cross-batch discipline** - the current read follows the maximum
  generation inside the scope (never submission time); history reads by
  ``batch_run_id`` return exactly that batch; a newer record without a
  submitted mapping is invisible, so results can never be spliced from the
  three newest records.
* **Old reference** - current year missing plus a previous-year batch reads
  as an explicit old reference, never as the current year.
* **Stale** - a known re-check (new same-provider version) marks the current
  read stale; the old batch stays readable by ``batch_run_id``.
* **Artifacts** - truncated and mutated mappings are explicit gaps; an
  identical resubmission reuses the batch (no new generation).

Synthetic dates live only in the dated fixture; the run-02 golden evidence
carries no date basis, so its submissions run in declared-inputs mode and the
service must show the evidence gap honestly (partial, candidate never
promoted to verified).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from pytest_asyncio import fixture as async_fixture
from typer.testing import CliRunner

from investment_agent.cli.commands.peer_annual import (
    import_peer_annual_snapshots,
    load_audit_json,
    load_sample_json,
)
from investment_agent.cli.commands.peer_cohort import (
    app as peers_app,
)
from investment_agent.cli.commands.peer_cohort import import_sample_roster
from investment_agent.evidence.contracts import Period
from investment_agent.industry_peers.artifacts import (
    MAPPING_FILENAME,
    MappingArtifactError,
    MetricMappingEntry,
    PeerBatchMapping,
    TargetMapping,
    mapping_artifact_path,
    mappings_in_scope,
    next_generation,
    read_batch_mapping,
    scan_batch_mappings,
    select_current_mapping,
    targets_signature,
    write_batch_mapping,
)
from investment_agent.industry_peers.batch import submit_offline_batch
from investment_agent.industry_peers.calculations import (
    ROE,
    build_peer_metric_record,
    save_peer_calculations,
)
from investment_agent.industry_peers.contracts import (
    PeerAnnualField,
    PeerContextStatus,
    PeerFieldVerification,
    PeerMetricStatus,
)
from investment_agent.industry_peers.service import (
    PeerTargetLookupError,
    get_industry_peer_context,
    list_industry_peer_companies,
    resolve_peer_target,
)
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.models import (  # noqa: F401
    agent,
    analysis,
    app_settings,
    candidates,
    disclosure,
    discovery,
    earnings_events,
    evidence,
    investor,
    market_data,
    monitoring,
    ops,
    peer,
    pit_snapshot,
    portfolio,
    portfolio_plan,
    provider_call,
    publication,
    reference,
    relationship,
    research_material,
    rules,
    stock_directory,
    user_domain,
    verification,
    zones,
)
from investment_agent.storage.models.evidence import SavedCalculation
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.repositories.listing import ListingRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN02_SAMPLE = REPO_ROOT / "research/industry-data-validation/evidence/run-02/sample.json"
DERIVED = REPO_ROOT / "research/industry-data-validation/evidence/run-02/derived"
SAMPLE_PATH = DERIVED / "baijiu-peer-sample.json"
AUDIT_PATH = DERIVED / "baijiu-official-audit.json"

ACQUIRED_AT = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
LATER_ACQUIRED_AT = datetime(2026, 10, 12, 5, 0, tzinfo=UTC)
SUBMITTED_AT = datetime(2026, 10, 12, 8, 0, tzinfo=UTC)
NOTICE_DATE = date(2026, 4, 25)
PERIOD_END = date(2025, 12, 31)
PREVIOUS_PERIOD = date(2024, 12, 31)
OWNER = "local_owner"
MAOTAI = "600519.SH"
PROVIDER = "eastmoney"
ADAPTER = "offline-run02-v1"
"""The synthetic fixture freezes in the service's default scope, so reads
with the default selection work; the rival provider below creates conflicts."""


# --- dated synthetic fixture (values as WP5's saved chain) --------------------


def dec(value: str) -> Decimal:
    return Decimal(value)


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


def synthetic_field(metric: str, value: str, *, period_end: date = PERIOD_END) -> PeerAnnualField:
    kind, unit, currency = FIELD_KINDS[metric]
    if kind == "flow":
        period = Period(start=date(period_end.year, 1, 1), end=period_end, basis="period_to_date/annual")
        raw_unit = "CNY"
    elif kind == "stock":
        period = Period(start=period_end, end=period_end, basis="point_in_time")
        raw_unit = "CNY"
    else:
        period = Period(start=date(period_end.year, 1, 1), end=period_end, basis="annual_ratio")
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
    provider: str = PROVIDER,
    adapter_version: str = ADAPTER,
    report_period: date = PERIOD_END,
    captured_at: datetime = ACQUIRED_AT,
) -> PeerAnnualSnapshot:
    """Append one snapshot version exactly as the WP3 importer would store it."""

    from investment_agent.storage.repositories.peer import PeerAnnualSnapshotRepository

    return await PeerAnnualSnapshotRepository(session).add(
        PeerAnnualSnapshot(
            id=str(uuid4()),
            company_id=company_id,
            listing_id=listing_id,
            report_period=report_period,
            provider=provider,
            adapter_version=adapter_version,
            captured_at=captured_at,
            content_hash=semantic_hash,
            semantic_hash=semantic_hash,
            fields={
                metric: synthetic_field(metric, value, period_end=report_period).model_dump(
                    mode="json"
                )
                for metric, value in values.items()
            },
            verification={
                metric: synthetic_proof(metric, value) for metric, value in values.items()
            },
        )
    )


async def seed_listings(
    session: Any, roster: tuple[tuple[str, str], ...]
) -> dict[str, tuple[int, int]]:
    """Create the directory listings; returns ``code -> (company_id, listing_id)``."""

    listing_repo = ListingRepository(session)
    ids: dict[str, tuple[int, int]] = {}
    for code, name in roster:
        listing = await listing_repo.ensure_a_share_listing(code, company_name=name)
        ids[code] = (listing.company_id, listing.id)
    return ids


async def seed_cohort(session: Any, identities: dict[str, tuple[int, int]]) -> None:
    """One roster version over the synthetic members (the default cohort key)."""

    from investment_agent.storage.models.peer import PeerCohortSnapshot
    from investment_agent.storage.repositories.peer import PeerCohortSnapshotRepository

    entries = [
        {
            "ticker": code,
            "company_id": company_id,
            "listing_id": listing_id,
            "name": name,
        }
        for code, name in SYNTHETIC_ROSTER
        for company_id, listing_id in [identities[code]]
    ]
    await PeerCohortSnapshotRepository(session).add(
        PeerCohortSnapshot(
            id=str(uuid4()),
            cohort_key="baijiu_current",
            revision=1,
            observed_on=PERIOD_END,
            acquired_at=ACQUIRED_AT,
            verification="candidate",
            inclusion_rule_version="test/1",
            content_hash="c" * 64,
            payload={"schema": "test-roster/1", "entries": entries},
        )
    )


@async_fixture
async def synthetic(db_session):
    """Directory listings plus one roster over the synthetic members.

    Snapshots are *not* stored here: each test stores exactly the versions of
    its scenario (a second disagreeing version is an honest re-check trigger,
    so scenario data and base data must not both exist unless a test wants
    that).
    """

    identities = await seed_listings(db_session, SYNTHETIC_ROSTER)
    await seed_cohort(db_session, identities)
    await db_session.commit()
    return {
        "ids": {code: company_id for code, (company_id, _listing) in identities.items()},
        "listings": {code: listing_id for code, (_company, listing_id) in identities.items()},
    }


async def store_standard(
    session: Any,
    synthetic: dict[str, Any],
    *,
    target_overrides: dict[str, str] | None = None,
    without_roe: set[str] = frozenset(),
    captured_at: datetime = ACQUIRED_AT,
) -> None:
    """The seven standard 2025 versions, with per-test scenario tweaks."""

    for index, ((code, _name), values) in enumerate(
        zip(SYNTHETIC_ROSTER, SYNTHETIC_VALUES, strict=True)
    ):
        if code in without_roe:
            values = {key: item for key, item in values.items() if key != "roe_weighted_annual"}
        if code == "600001.SH" and target_overrides:
            values = {**values, **target_overrides}
        await store_snapshot(
            session,
            synthetic["ids"][code],
            synthetic["listings"][code],
            values,
            semantic_hash=f"{index + 1:02d}" * 32,
            captured_at=captured_at,
        )
    await session.commit()


@async_fixture
async def golden(db_session):
    """run-02 fully imported: roster (WP2) and annual facts with proofs (WP3)."""

    sample = load_sample_json(SAMPLE_PATH)
    listing_repo = ListingRepository(db_session)
    for entry in sample["companies"]:
        await listing_repo.ensure_a_share_listing(entry["code"], company_name=entry["name"])
    roster = await import_sample_roster(
        db_session, sample_path=RUN02_SAMPLE, acquired_at=ACQUIRED_AT
    )
    await import_peer_annual_snapshots(
        db_session,
        sample=sample,
        audit=load_audit_json(AUDIT_PATH),
        acquired_at=ACQUIRED_AT,
    )
    await db_session.commit()
    return roster.snapshot


def metric_of(result: Any, key: str) -> Any:
    return next(metric for metric in result.metrics if metric.metric_key == key)


# --- status table: outside the roster / unknown target -------------------------


async def test_target_outside_the_roster_is_not_supported(db_session, synthetic) -> None:
    await seed_listings(db_session, (("999999.SH", "名单外公司"),))
    result = await get_industry_peer_context(
        db_session, ticker="999999.SH", data_dir=_tmpdir(), cutoff=_after_everything()
    )
    assert result.status is PeerContextStatus.NOT_SUPPORTED
    assert "does not cover it yet" in result.gaps[0]
    assert result.metrics == ()
    assert result.job_hint is None


async def test_unknown_or_malformed_target_raises(db_session, synthetic) -> None:
    with pytest.raises(PeerTargetLookupError):
        await resolve_peer_target(db_session, ticker="600000.SH")
    with pytest.raises(ValueError, match="canonical"):
        await resolve_peer_target(db_session, ticker="600001")


def _tmpdir() -> Path:
    from tempfile import mkdtemp

    return Path(mkdtemp())


def _after_everything() -> datetime:
    return datetime(2026, 10, 12, 23, 59, tzinfo=UTC)


# --- status table: no submission ------------------------------------------------


async def test_no_submission_is_missing_with_job_hint_slot(db_session, synthetic) -> None:
    await store_standard(db_session, synthetic)
    result = await get_industry_peer_context(
        db_session, ticker="600001.SH", data_dir=_tmpdir(), cutoff=_after_everything()
    )
    assert result.status is PeerContextStatus.MISSING
    assert result.current_report_period == PERIOD_END
    assert any("no submitted peer batch" in gap for gap in result.gaps)
    # The structural slot for WP8/WP11 job hints exists; the service itself
    # never invents job state at read time.
    assert result.job_hint is not None
    assert result.job_hint.has_pending_job is None
    assert result.metrics == ()


# --- status table: the run-02 offline batch -------------------------------------


async def test_run02_offline_batch_reads_partial_with_honest_gaps(db_session, golden, tmp_path) -> None:
    submission = await submit_offline_batch(
        db_session, data_dir=tmp_path, submitted_at=SUBMITTED_AT
    )
    await db_session.commit()
    assert submission.status == "success"
    assert submission.gap_metrics == 0
    assert len(submission.mapping.targets) == 19
    assert submission.saved_records == 57

    result = await get_industry_peer_context(
        db_session, ticker=MAOTAI, data_dir=tmp_path
    )
    # The run-02 records cite no materialized evidence (no date basis), so the
    # read is honest: per-metric values are there, the overall state is partial.
    assert result.status is PeerContextStatus.PARTIAL
    assert result.batch_run_id == submission.mapping.batch_run_id
    assert result.generation == 1
    assert result.batch_status == "success"
    assert result.report_period == PERIOD_END

    roe = metric_of(result, ROE)
    assert roe.status is PeerMetricStatus.COMPARABLE
    assert roe.state == "saved"
    assert roe.target_value.value == dec("0.3253")
    assert roe.peer_median.value == dec("0.0739")
    assert roe.ascending_position is not None
    assert roe.ascending_position.value.quantize(Decimal("0.01")) == dec("94.44")
    assert (roe.valid_peer_count, roe.candidate_peer_count) == (18, 18)
    assert roe.cites_no_evidence is True
    assert roe.verification_counts == {}
    assert any("snapshot-only" in note for note in roe.quality_notes)
    assert roe.calculation_ref is not None
    assert set(roe.result_refs) >= {"target", "peer_median", "ascending_position"}

    from investment_agent.industry_peers.calculations import CFO_PARENT_PROFIT

    cfo = metric_of(result, CFO_PARENT_PROFIT)
    assert (cfo.valid_peer_count, cfo.candidate_peer_count) == (15, 18)
    assert cfo.peer_median is not None
    assert cfo.peer_median.value.quantize(Decimal("0.0001")) == dec("0.5792")
    excluded = {entry.ticker: entry.reason for entry in cfo.excluded_companies}
    assert excluded == {
        "000799.SZ": "non_positive_denominator",
        "000860.SZ": "non_positive_denominator",
        "600199.SH": "non_positive_denominator",
    }

    # Mixed acquisition times are reported separately, not hidden behind one
    # "latest" value (run-02 was imported in one pass, so both ends match).
    assert result.inputs_acquired_earliest == ACQUIRED_AT
    assert result.inputs_acquired_latest == ACQUIRED_AT


async def test_idempotent_resubmission_reuses_the_batch(db_session, golden, tmp_path) -> None:
    first = await submit_offline_batch(db_session, data_dir=tmp_path, submitted_at=SUBMITTED_AT)
    await db_session.commit()
    second = await submit_offline_batch(
        db_session, data_dir=tmp_path, submitted_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    )
    await db_session.commit()

    assert second.created is False
    assert second.mapping.batch_run_id == first.mapping.batch_run_id
    assert second.mapping.generation == 1
    assert targets_signature(second.mapping) == targets_signature(first.mapping)
    # exactly one artifact file exists
    files = list((tmp_path / "research-runs").glob(f"*/{MAPPING_FILENAME}"))
    assert len(files) == 1


# --- status table: not applicable / insufficient / conflict ---------------------


async def test_target_not_applicable_keeps_the_peer_median(db_session, synthetic, tmp_path) -> None:
    # The target's operating revenue is an original zero: the contract/revenue
    # metric does not apply, backed by the saved field value.
    await store_standard(db_session, synthetic, target_overrides={"operating_revenue": "0"})
    target_id = synthetic["ids"]["600001.SH"]
    submission = await submit_offline_batch(
        db_session,
        data_dir=tmp_path,
        tickers=["600001.SH"],
        provider=PROVIDER,
        adapter_version=ADAPTER,
        submitted_at=SUBMITTED_AT,
    )
    await db_session.commit()

    from investment_agent.industry_peers.calculations import CONTRACT_REVENUE

    result = await get_industry_peer_context(
        db_session, ticker="600001.SH", data_dir=tmp_path
    )
    cr = metric_of(result, CONTRACT_REVENUE)
    assert cr.status is PeerMetricStatus.TARGET_NOT_APPLICABLE
    assert cr.target_value is None
    assert cr.peer_median is not None
    assert cr.peer_median.value == dec("0.10")
    assert cr.ascending_position is None
    target_exclusions = [
        entry for entry in cr.excluded_companies if entry.company_id == target_id
    ]
    assert target_exclusions and target_exclusions[0].reason == "non_positive_denominator"
    # Fully evidenced comparable/not-applicable metrics are a complete result,
    # and the cited evidence shows its check coverage (never auto-verified).
    assert result.status is PeerContextStatus.READY
    roe = metric_of(result, ROE)
    assert roe.status is PeerMetricStatus.COMPARABLE
    assert roe.verification_counts == {"verified": 14}
    assert roe.cites_no_evidence is False
    assert submission.mapping.status == "success"


async def test_insufficient_peers_saves_no_statistics(db_session, synthetic, tmp_path) -> None:
    # Two peers lose their ROE input: 4 valid peers stay below the threshold.
    await store_standard(
        db_session, synthetic, without_roe={"600003.SH", "600004.SH"}
    )
    await submit_offline_batch(
        db_session,
        data_dir=tmp_path,
        tickers=["600001.SH"],
        provider=PROVIDER,
        adapter_version=ADAPTER,
        submitted_at=SUBMITTED_AT,
    )
    await db_session.commit()

    result = await get_industry_peer_context(db_session, ticker="600001.SH", data_dir=tmp_path)
    roe = metric_of(result, ROE)
    assert roe.status is PeerMetricStatus.INSUFFICIENT_PEERS
    assert (roe.valid_peer_count, roe.candidate_peer_count) == (4, 6)
    assert roe.peer_median is None
    assert roe.ascending_position is None
    assert roe.target_value is not None
    assert "at least 5" in (roe.reason or "")
    assert result.status is PeerContextStatus.PARTIAL


async def test_conflicted_input_is_excluded_and_marks_conflict(db_session, synthetic, tmp_path) -> None:
    # A second provider disagrees on the target's ROE without a correction
    # basis: the freeze excludes the conflicted input instead of picking one.
    await store_standard(db_session, synthetic)
    target_id = synthetic["ids"]["600001.SH"]
    await store_snapshot(
        db_session,
        target_id,
        synthetic["listings"]["600001.SH"],
        {**SYNTHETIC_VALUES[0], "roe_weighted_annual": "0.99"},
        semantic_hash="cc" * 32,
        provider="synthetic-rival",
        captured_at=LATER_ACQUIRED_AT,
    )
    await db_session.commit()
    await submit_offline_batch(
        db_session,
        data_dir=tmp_path,
        tickers=["600001.SH"],
        provider=PROVIDER,
        adapter_version=ADAPTER,
        submitted_at=SUBMITTED_AT,
    )
    await db_session.commit()

    result = await get_industry_peer_context(db_session, ticker="600001.SH", data_dir=tmp_path)
    roe = metric_of(result, ROE)
    assert roe.status is PeerMetricStatus.CONFLICT
    assert roe.target_value is None
    assert roe.peer_median.value == dec("0.175")
    target_exclusions = [
        entry for entry in roe.excluded_companies if entry.company_id == target_id
    ]
    assert target_exclusions and target_exclusions[0].reason == "field_conflict"
    assert result.status is PeerContextStatus.CONFLICT


# --- cutoff ---------------------------------------------------------------------


async def test_batch_submitted_after_cutoff_is_invisible(db_session, golden, tmp_path) -> None:
    submission = await submit_offline_batch(
        db_session,
        data_dir=tmp_path,
        tickers=[MAOTAI],
        submitted_at=SUBMITTED_AT,
    )
    await db_session.commit()
    assert submission.created is True

    before = await get_industry_peer_context(
        db_session, ticker=MAOTAI, data_dir=tmp_path, cutoff=datetime(2026, 10, 12, 6, 0, tzinfo=UTC)
    )
    assert before.status is PeerContextStatus.MISSING
    assert any("after the cutoff" in gap or "no submitted peer batch" in gap for gap in before.gaps)

    explicit = await get_industry_peer_context(
        db_session,
        ticker=MAOTAI,
        batch_run_id=submission.mapping.batch_run_id,
        data_dir=tmp_path,
        cutoff=datetime(2026, 10, 12, 6, 0, tzinfo=UTC),
    )
    assert explicit.status is PeerContextStatus.MISSING
    assert any("after the cutoff" in gap for gap in explicit.gaps)

    # Before the snapshots were even imported, the target year cannot be
    # determined honestly from local records.
    naive = await get_industry_peer_context(
        db_session, ticker=MAOTAI, data_dir=tmp_path, cutoff=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert naive.status is PeerContextStatus.MISSING
    assert any("cannot determine the target year" in gap for gap in naive.gaps)

    after = await get_industry_peer_context(
        db_session, ticker=MAOTAI, data_dir=tmp_path, cutoff=_after_everything()
    )
    assert after.status is PeerContextStatus.PARTIAL


async def test_record_created_after_cutoff_is_excluded_per_metric(
    db_session, synthetic, tmp_path
) -> None:
    # One metric's mapped record claims a post-cutoff creation time: that
    # metric drops out with an explicit reason while the batch's earlier
    # records still show. A record created before the cutoff cannot cite
    # later-acquired evidence, so the record-level check bounds the inputs.
    await store_standard(db_session, synthetic)
    await submit_offline_batch(
        db_session,
        data_dir=tmp_path,
        tickers=["600001.SH"],
        provider=PROVIDER,
        adapter_version=ADAPTER,
        submitted_at=SUBMITTED_AT,
    )
    await db_session.commit()

    from investment_agent.industry_peers.calculations import (
        CFO_PARENT_PROFIT,
        BatchCompany,
        FrozenBatch,
        batch_field_from_snapshot,
    )

    rows = {
        row.company_id: row
        for row in (await db_session.execute(
            __import__("sqlalchemy").select(PeerAnnualSnapshot)
        )).scalars()
    }
    companies = []
    for company_id in sorted(rows):
        row = rows[company_id]
        companies.append(
            BatchCompany(
                company_id=company_id,
                listing_id=row.listing_id,
                ticker=f"T{company_id:03d}",
                name=f"company-{company_id}",
                in_cohort=True,
                report_period=row.report_period,
                fields={metric: batch_field_from_snapshot(row, metric) for metric in row.fields},
            )
        )
    batch = FrozenBatch(
        target_company_id=synthetic["ids"]["600001.SH"],
        report_period=PERIOD_END,
        cohort_ref=ObjectRef(
            kind="peer_cohort_snapshot", id=UUID(int=1), revision=1
        ),
        cohort_content_hash="0" * 64,
        companies=tuple(companies),
    )
    # A changed input builds a new record; the hand-inserted row below carries
    # the future creation time (SavedCalculation creation is the store's own
    # clock, so the payload is written directly here).
    built = build_peer_metric_record(
        batch,
        CFO_PARENT_PROFIT,
        evidence_refs={
            (company_id, metric): ObjectRef(kind="evidence", id=uuid4(), revision=1)
            for company_id in rows
            for metric in ("operating_cash_flow", "net_profit_parent")
        },
        as_of=datetime(2030, 1, 1, tzinfo=UTC),
        require_fact_refs=False,
    )
    assert built.record is not None
    future_id = uuid4()
    db_session.add(
        SavedCalculation(
            id=str(future_id),
            revision=1,
            owner_id=OWNER,
            payload=built.record.model_copy(update={"id": future_id}).model_dump(mode="json"),
        )
    )
    await db_session.flush()

    # Reshape the submitted mapping to point the cfo metric at the future
    # record (generation 2, same scope), submitted before the cutoff.
    scan = scan_batch_mappings(tmp_path)
    previous = scan.valid[0]
    targets = []
    for target in previous.targets:
        entries = []
        for entry in target.metric_calculation_refs:
            if entry.metric_key == CFO_PARENT_PROFIT:
                entry = MetricMappingEntry(
                    metric_key=entry.metric_key,
                    state="saved",
                    ref=ObjectRef(kind="calculation", id=future_id, revision=1),
                )
            entries.append(entry)
        targets.append(
            TargetMapping(
                company_id=target.company_id,
                listing_id=target.listing_id,
                ticker=target.ticker,
                name=target.name,
                report_period=target.report_period,
                metric_calculation_refs=tuple(entries),
            )
        )
    reshaped = PeerBatchMapping.create(
        batch_run_id="peer-future-record",
        cohort_ref=previous.cohort_ref,
        cohort_content_hash=previous.cohort_content_hash,
        report_period=previous.report_period,
        provider=previous.provider,
        adapter_version=previous.adapter_version,
        generation=2,
        status=previous.status,
        submitted_at=datetime(2026, 10, 12, 7, 0, tzinfo=UTC),
        inputs_acquired_earliest=previous.inputs_acquired_earliest,
        inputs_acquired_latest=previous.inputs_acquired_latest,
        targets=tuple(targets),
        manifest=previous.manifest,
    )
    write_batch_mapping(tmp_path, reshaped)

    # A research bound just after the wall-clock save time: the future record
    # (2030) is invisible, the batch's real records (created now) are not.
    bound = datetime.now(UTC) + timedelta(hours=1)
    result = await get_industry_peer_context(
        db_session, ticker="600001.SH", data_dir=tmp_path, cutoff=bound
    )
    assert result.batch_run_id == "peer-future-record"
    cfo = metric_of(result, CFO_PARENT_PROFIT)
    assert cfo.status is PeerMetricStatus.MISSING
    assert cfo.cutoff_excluded is True
    assert "2030-01-01" in (cfo.gap_reason or "")
    # The batch's earlier records remain visible at this cutoff.
    roe = metric_of(result, ROE)
    assert roe.status is PeerMetricStatus.COMPARABLE
    assert result.status is PeerContextStatus.PARTIAL


# --- cross-batch discipline ------------------------------------------------------


async def test_history_and_generations_never_mix(db_session, synthetic, tmp_path) -> None:
    await store_standard(db_session, synthetic)
    first = await submit_offline_batch(
        db_session,
        data_dir=tmp_path,
        tickers=["600001.SH"],
        provider=PROVIDER,
        adapter_version=ADAPTER,
        submitted_at=SUBMITTED_AT,
    )
    await db_session.commit()

    # Peer 600005.SH (base ROE 0.20, one of the two middle values) is
    # corrected: a new snapshot version, a new generation - and a new median.
    peer_id = synthetic["ids"]["600005.SH"]
    await store_snapshot(
        db_session,
        peer_id,
        synthetic["listings"]["600005.SH"],
        {**SYNTHETIC_VALUES[4], "roe_weighted_annual": "0.26"},
        semantic_hash="dd" * 32,
        captured_at=LATER_ACQUIRED_AT,
    )
    await db_session.commit()
    second = await submit_offline_batch(
        db_session,
        data_dir=tmp_path,
        tickers=["600001.SH"],
        provider=PROVIDER,
        adapter_version=ADAPTER,
        submitted_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
    )
    await db_session.commit()
    assert second.created is True and second.mapping.generation == 2

    current = await get_industry_peer_context(db_session, ticker="600001.SH", data_dir=tmp_path)
    assert current.batch_run_id == second.mapping.batch_run_id
    roe = metric_of(current, ROE)
    assert roe.peer_median.value == (dec("0.15") + dec("0.25")) / 2
    # A same-provider update without a correction basis is awaiting re-check:
    # the current display says so.
    assert current.status is PeerContextStatus.STALE
    assert any("600005.SH" in reason and "pending_recheck" in reason for reason in current.stale_reasons)

    history = await get_industry_peer_context(
        db_session,
        ticker="600001.SH",
        batch_run_id=first.mapping.batch_run_id,
        data_dir=tmp_path,
    )
    # The fully evidenced synthetic batch is complete: history reads report it
    # as it was - ready, without the re-check flag the newer version caused.
    assert history.status is PeerContextStatus.READY
    assert history.stale_reasons == ()
    history_roe = metric_of(history, ROE)
    assert history_roe.peer_median.value == (dec("0.15") + dec("0.20")) / 2
    assert history_roe.calculation_ref != roe.calculation_ref


async def test_late_old_generation_never_covers_the_newer_one(db_session, synthetic, tmp_path) -> None:
    await store_standard(db_session, synthetic)
    await submit_offline_batch(
        db_session,
        data_dir=tmp_path,
        tickers=["600001.SH"],
        provider=PROVIDER,
        adapter_version=ADAPTER,
        submitted_at=SUBMITTED_AT,
    )
    await db_session.commit()
    await store_snapshot(
        db_session,
        synthetic["ids"]["600005.SH"],
        synthetic["listings"]["600005.SH"],
        {**SYNTHETIC_VALUES[4], "roe_weighted_annual": "0.26"},
        semantic_hash="dd" * 32,
        captured_at=LATER_ACQUIRED_AT,
    )
    await db_session.commit()
    second = await submit_offline_batch(
        db_session,
        data_dir=tmp_path,
        tickers=["600001.SH"],
        provider=PROVIDER,
        adapter_version=ADAPTER,
        submitted_at=datetime(2026, 10, 12, 9, 0, tzinfo=UTC),
    )
    await db_session.commit()

    # A third input set arrives with an *earlier* submission time: generation
    # selection never orders by submission or finish time. Peer 600006.SH
    # (0.25) moves to 0.27 on top of the gen2 change.
    await store_snapshot(
        db_session,
        synthetic["ids"]["600006.SH"],
        synthetic["listings"]["600006.SH"],
        {**SYNTHETIC_VALUES[5], "roe_weighted_annual": "0.27"},
        semantic_hash="ab" * 32,
        captured_at=datetime(2026, 10, 12, 6, 0, tzinfo=UTC),
    )
    await db_session.commit()
    third = await submit_offline_batch(
        db_session,
        data_dir=tmp_path,
        tickers=["600001.SH"],
        provider=PROVIDER,
        adapter_version=ADAPTER,
        submitted_at=datetime(2026, 10, 12, 7, 30, tzinfo=UTC),
    )
    await db_session.commit()
    assert third.mapping.generation == 3
    assert third.mapping.submitted_at < second.mapping.submitted_at

    current = await get_industry_peer_context(db_session, ticker="600001.SH", data_dir=tmp_path)
    assert current.generation == 3
    assert current.batch_run_id == third.mapping.batch_run_id
    roe = metric_of(current, ROE)
    assert roe.peer_median.value == (dec("0.15") + dec("0.26")) / 2


async def test_unmapped_newer_records_are_invisible(db_session, synthetic, tmp_path) -> None:
    """Records saved without a submitted mapping never enter the read."""

    from investment_agent.evidence.store import EvidenceService
    from investment_agent.industry_peers.calculations import BatchCompany, batch_field_from_snapshot
    from investment_agent.industry_peers.evidence import (
        peer_field_evidence,
        peer_field_operation_id,
    )

    await store_standard(db_session, synthetic)
    await submit_offline_batch(
        db_session,
        data_dir=tmp_path,
        tickers=["600001.SH"],
        provider=PROVIDER,
        adapter_version=ADAPTER,
        submitted_at=SUBMITTED_AT,
    )
    await db_session.commit()
    baseline = await get_industry_peer_context(db_session, ticker="600001.SH", data_dir=tmp_path)
    baseline_median = metric_of(baseline, ROE).peer_median.value

    # Save newer records for a changed input - but never submit a mapping.
    peer_id = synthetic["ids"]["600006.SH"]
    updated = await store_snapshot(
        db_session,
        peer_id,
        synthetic["listings"]["600006.SH"],
        {**SYNTHETIC_VALUES[5], "roe_weighted_annual": "0.26"},
        semantic_hash="dd" * 32,
        captured_at=LATER_ACQUIRED_AT,
    )
    rows = {
        row.company_id: row
        for row in (await db_session.execute(
            __import__("sqlalchemy").select(PeerAnnualSnapshot)
        )).scalars()
    }
    rows[peer_id] = updated
    service = EvidenceService(db_session)
    refs = {}
    for company_id, row in rows.items():
        for metric in row.fields:
            materialization = peer_field_evidence(row, metric)
            if materialization.record is not None:
                refs[(company_id, metric)] = await service.save_evidence(
                    OWNER, materialization.record, peer_field_operation_id(row, metric)
                )
    companies = []
    for company_id in sorted(rows):
        row = rows[company_id]
        companies.append(
            BatchCompany(
                company_id=company_id,
                listing_id=row.listing_id,
                ticker=f"T{company_id:03d}",
                name=f"company-{company_id}",
                in_cohort=True,
                report_period=row.report_period,
                fields={metric: batch_field_from_snapshot(row, metric) for metric in row.fields},
            )
        )
    from investment_agent.industry_peers.calculations import FrozenBatch

    batch = FrozenBatch(
        target_company_id=synthetic["ids"]["600001.SH"],
        report_period=PERIOD_END,
        cohort_ref=ObjectRef(kind="peer_cohort_snapshot", id=UUID(int=1), revision=1),
        cohort_content_hash="0" * 64,
        companies=tuple(companies),
    )
    await save_peer_calculations(
        db_session, owner_id=OWNER, batch=batch, evidence_refs=refs, as_of=_after_everything()
    )
    await db_session.commit()

    current = await get_industry_peer_context(db_session, ticker="600001.SH", data_dir=tmp_path)
    assert current.generation == 1
    assert metric_of(current, ROE).peer_median.value == baseline_median


# --- old reference ----------------------------------------------------------------


async def test_previous_year_is_an_explicit_old_reference(db_session, synthetic, tmp_path) -> None:
    await store_standard(db_session, synthetic)
    for index, ((code, _name), values) in enumerate(
        zip(SYNTHETIC_ROSTER, SYNTHETIC_VALUES, strict=True)
    ):
        company_id = synthetic["ids"][code]
        await store_snapshot(
            db_session,
            company_id,
            synthetic["listings"][code],
            values,
            semantic_hash=f"9{index}" * 32,
            report_period=PREVIOUS_PERIOD,
            captured_at=LATER_ACQUIRED_AT,
        )
    await db_session.commit()
    await submit_offline_batch(
        db_session,
        data_dir=tmp_path,
        tickers=["600001.SH"],
        report_period=PREVIOUS_PERIOD,
        submitted_at=SUBMITTED_AT,
    )
    await db_session.commit()

    result = await get_industry_peer_context(db_session, ticker="600001.SH", data_dir=tmp_path)
    assert result.old_reference is True
    assert result.report_period == PREVIOUS_PERIOD
    assert result.current_report_period == PERIOD_END
    assert any("old reference" in warning for warning in result.warnings)
    assert metric_of(result, ROE).status is PeerMetricStatus.COMPARABLE


# --- stale -------------------------------------------------------------------------


async def test_known_recheck_marks_current_read_stale_history_stays_readable(
    db_session, golden, tmp_path
) -> None:
    submission = await submit_offline_batch(
        db_session, data_dir=tmp_path, tickers=[MAOTAI], submitted_at=SUBMITTED_AT
    )
    await db_session.commit()

    # A supplier refresh of one peer's ROE without a correction basis: the
    # saved versions now disagree (same provider) - pending re-check.
    from investment_agent.storage.repositories.peer import PeerAnnualSnapshotRepository

    peer_company = await golden_snapshot_company(db_session, "000596.SZ")
    peer_rows = await PeerAnnualSnapshotRepository(db_session).metric_versions(
        company_id=peer_company, metric="roe_weighted_annual"
    )
    assert peer_rows
    original = peer_rows[0]
    fields = dict(original.fields)
    entry = PeerAnnualField.model_validate(fields["roe_weighted_annual"])
    fields["roe_weighted_annual"] = entry.model_copy(
        update={"value": dec("0.99"), "raw_value": "99"}
    ).model_dump(mode="json")
    await PeerAnnualSnapshotRepository(db_session).add(
        PeerAnnualSnapshot(
            id=str(uuid4()),
            company_id=original.company_id,
            listing_id=original.listing_id,
            report_period=original.report_period,
            provider=original.provider,
            adapter_version=original.adapter_version,
            captured_at=LATER_ACQUIRED_AT,
            content_hash="f" * 64,
            semantic_hash="ef" * 32,
            fields=fields,
            verification=dict(original.verification),
        )
    )
    await db_session.commit()

    current = await get_industry_peer_context(db_session, ticker=MAOTAI, data_dir=tmp_path)
    assert current.status is PeerContextStatus.STALE
    assert any("000596.SZ" in reason and "pending_recheck" in reason for reason in current.stale_reasons)
    # The values themselves are unchanged; only the re-check flag is added.
    assert metric_of(current, ROE).target_value.value == dec("0.3253")

    history = await get_industry_peer_context(
        db_session,
        ticker=MAOTAI,
        batch_run_id=submission.mapping.batch_run_id,
        data_dir=tmp_path,
    )
    assert history.status is PeerContextStatus.PARTIAL
    assert history.stale_reasons == ()


async def golden_snapshot_company(session: Any, ticker: str) -> int:
    from investment_agent.storage.repositories.listing import ListingRepository

    listing = await ListingRepository(session).get_a_share_listing(ticker)
    assert listing is not None
    return listing.company_id


# --- artifacts ----------------------------------------------------------------------


def make_mapping(**overrides: Any) -> PeerBatchMapping:
    fields = dict(
        batch_run_id="peer-test-1",
        cohort_ref=ObjectRef(kind="peer_cohort_snapshot", id=UUID(int=17), revision=3),
        cohort_content_hash="a" * 64,
        report_period=PERIOD_END,
        provider="eastmoney",
        adapter_version="offline-run02-v1",
        generation=1,
        status="success",
        submitted_at=SUBMITTED_AT,
        targets=(
            TargetMapping(
                company_id=1,
                listing_id=1001,
                ticker="600001.SH",
                name="target",
                report_period=PERIOD_END,
                metric_calculation_refs=(
                    MetricMappingEntry(metric_key="roe", state="gap", gap_reason="no facts"),
                    MetricMappingEntry(metric_key="contract_revenue", state="gap", gap_reason="no facts"),
                    MetricMappingEntry(metric_key="cfo_parent_profit", state="gap", gap_reason="no facts"),
                ),
            ),
        ),
    )
    fields.update(overrides)
    return PeerBatchMapping.create(**fields)


def test_generation_selection_ignores_submission_time() -> None:
    late_old = make_mapping(generation=1, submitted_at=datetime(2026, 10, 12, 20, 0, tzinfo=UTC))
    early_new = make_mapping(generation=2, batch_run_id="peer-test-2", submitted_at=SUBMITTED_AT)
    scope = mappings_in_scope(
        [late_old, early_new],
        cohort_ref=late_old.cohort_ref,
        report_period=PERIOD_END,
        provider="eastmoney",
        adapter_version="offline-run02-v1",
    )
    assert next_generation(scope) == 3
    assert select_current_mapping(scope).generation == 2
    # A cutoff hides later submissions: the late-arriving old generation never
    # enters the earlier read, while the earlier-submitted new one stays.
    assert select_current_mapping(scope, cutoff=datetime(2026, 10, 12, 9, 0, tzinfo=UTC)).generation == 2
    assert select_current_mapping(scope, cutoff=datetime(2026, 10, 12, 7, 0, tzinfo=UTC)) is None


def test_scopes_never_compare_generations() -> None:
    base = make_mapping(generation=9)
    other_scope = make_mapping(
        batch_run_id="peer-test-2", generation=1, adapter_version="adapter-2"
    )
    scope = mappings_in_scope(
        [base, other_scope],
        cohort_ref=base.cohort_ref,
        report_period=PERIOD_END,
        provider="eastmoney",
        adapter_version="offline-run02-v1",
    )
    assert [mapping.generation for mapping in scope] == [9]
    assert next_generation(scope) == 10


def test_corrupt_or_mutated_mapping_is_rejected(tmp_path: Path) -> None:
    mapping = make_mapping()
    path = write_batch_mapping(tmp_path, mapping)
    assert read_batch_mapping(path) == mapping

    # Truncated JSON cannot pass as a mapping.
    path.write_text(path.read_text()[: len(path.read_text()) // 2], encoding="utf-8")
    with pytest.raises(MappingArtifactError):
        read_batch_mapping(path)

    # A parseable but mutated body fails its content hash.
    payload = json.loads(make_mapping().model_dump_json(by_alias=True))
    payload["targets"][0]["ticker"] = "999999.SH"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(MappingArtifactError, match="content hash"):
        read_batch_mapping(path)

    missing = mapping_artifact_path(tmp_path, "peer-never-submitted")
    with pytest.raises(MappingArtifactError, match="missing"):
        read_batch_mapping(missing)


async def test_unreadable_artifact_is_an_explicit_gap(db_session, golden, tmp_path) -> None:
    submission = await submit_offline_batch(
        db_session, data_dir=tmp_path, tickers=[MAOTAI], submitted_at=SUBMITTED_AT
    )
    await db_session.commit()
    path = mapping_artifact_path(tmp_path, submission.mapping.batch_run_id)
    path.write_text("{not json", encoding="utf-8")

    current = await get_industry_peer_context(db_session, ticker=MAOTAI, data_dir=tmp_path)
    assert current.status is PeerContextStatus.MISSING
    assert any("unreadable" in warning for warning in current.warnings)

    history = await get_industry_peer_context(
        db_session,
        ticker=MAOTAI,
        batch_run_id=submission.mapping.batch_run_id,
        data_dir=tmp_path,
    )
    assert history.status is PeerContextStatus.MISSING
    assert any("unreadable" in gap for gap in history.gaps)


# --- company detail ------------------------------------------------------------------


async def test_companies_endpoint_lists_fixed_versions(db_session, golden, tmp_path) -> None:
    submission = await submit_offline_batch(
        db_session, data_dir=tmp_path, tickers=[MAOTAI], submitted_at=SUBMITTED_AT
    )
    await db_session.commit()

    companies = await list_industry_peer_companies(
        db_session, ticker=MAOTAI, batch_run_id=submission.mapping.batch_run_id, data_dir=tmp_path
    )
    assert companies.status is PeerContextStatus.PARTIAL
    assert companies.report_period == PERIOD_END
    assert len(companies.companies) == 19
    maotai = next(row for row in companies.companies if row.ticker == MAOTAI)
    assert maotai.listing_id > 0
    roe_row = next(metric for metric in maotai.metrics if metric.metric_key == ROE)
    assert roe_row.included is True and roe_row.is_target is True
    assert roe_row.value == "0.3253"

    from investment_agent.industry_peers.calculations import CFO_PARENT_PROFIT

    cfo_excluded = [
        row
        for row in companies.companies
        if any(
            metric.metric_key == CFO_PARENT_PROFIT and not metric.included
            for metric in row.metrics
        )
    ]
    assert {row.ticker for row in cfo_excluded} == {"000799.SZ", "000860.SZ", "600199.SH"}
    for row in cfo_excluded:
        detail = next(
            metric for metric in row.metrics if metric.metric_key == CFO_PARENT_PROFIT
        )
        assert detail.exclusion_reason == "non_positive_denominator"
        assert detail.exclusion_detail

    # A batch without the requested target is rejected (API: 404).
    with pytest.raises(PeerTargetLookupError):
        await list_industry_peer_companies(
            db_session,
            ticker="000858.SZ",
            batch_run_id=submission.mapping.batch_run_id,
            data_dir=tmp_path,
        )
    with pytest.raises(PeerTargetLookupError):
        await get_industry_peer_context(
            db_session,
            ticker="000858.SZ",
            batch_run_id=submission.mapping.batch_run_id,
            data_dir=tmp_path,
        )


async def test_batch_without_the_target_reads_as_missing(db_session, golden, tmp_path) -> None:
    await submit_offline_batch(
        db_session, data_dir=tmp_path, tickers=[MAOTAI], submitted_at=SUBMITTED_AT
    )
    await db_session.commit()
    result = await get_industry_peer_context(db_session, ticker="000858.SZ", data_dir=tmp_path)
    assert result.status is PeerContextStatus.MISSING
    assert any("no mapping entry" in gap for gap in result.gaps)


# --- CLI surface ----------------------------------------------------------------------


def test_cli_app_exposes_the_submit_command() -> None:
    result = CliRunner().invoke(peers_app, ["--help"])
    assert result.exit_code == 0
    assert "submit-offline-batch" in result.output


def test_cli_rejects_malformed_tickers() -> None:
    result = CliRunner().invoke(peers_app, ["submit-offline-batch", "--ticker", "600519"])
    assert result.exit_code == 1
    assert "canonical" in result.output


# --- module-level sanity ----------------------------------------------------------------


def test_engine_is_unused_here() -> None:
    """The service module never opens engines; reads arrive via the session."""

    import investment_agent.industry_peers.service as service_module

    source = Path(service_module.__file__).read_text(encoding="utf-8")
    assert "init_engine" not in source
    assert "session_scope" not in source
    assert "httpx" not in source and "requests" not in source
