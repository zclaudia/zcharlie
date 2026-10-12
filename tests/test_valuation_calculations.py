"""AG10 deterministic arithmetic, saved provenance and trusted valuation boundaries."""

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from investment_agent.evidence.contracts import CalculationAssumption, EvidenceRecord, Period
from investment_agent.evidence.store import EvidenceService
from investment_agent.factors.valuation import VALUATION_FACTORS, years_before
from investment_agent.factors.view import Bar, SnapshotView
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.enums import CalculationStatus as Status
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.report_overdue import ReportOverdueRepository
from investment_agent.valuation.contracts import (
    PercentileRequest,
    ReverseDCFRequest,
    ValuationQualification,
)
from investment_agent.valuation.service import ValuationService
from investment_agent.valuation.solver import solve
from investment_agent.valuation.tools import valuation_registrations

AS_OF = datetime(2026, 10, 1, 9, tzinfo=UTC)
D = Decimal


def assumption(name, value, **changes):
    return CalculationAssumption(name=name, value=Quantity(value=value, unit="fraction"),
        provenance="agent_scenario", rationale="Fictional sensitivity; no asserted company return", **changes)


class FixtureQualification:
    def __init__(self, qualification):
        self.qualification = qualification

    async def resolve(self, session, owner, request, evidence):
        return self.qualification


async def seed(session, *, amounts=("7.6", "8.2", "9.1"), market="212.4", unit="亿元"):
    listing = await ListingRepository(session).ensure_a_share_listing("600519.SH", company_name="Fictional fixture")
    subject = ObjectRef(kind="listing", id=listing.id, revision=1)
    refs = []
    for index, amount in enumerate((market, *amounts)):
        period = Period(start=date(2026, 9, 30), end=date(2026, 9, 30), basis="ordinary_equity_total") if index == 0 else Period(
            start=date(2022 + index, 1, 1), end=date(2022 + index, 12, 31), basis="verified_fcfe_fixture.v1")
        identifier = uuid4()
        record = EvidenceRecord(id=identifier, revision=1, actor="verified_fixture", origin_ref=None,
            created_at=AS_OF - timedelta(days=1), source_ref=ObjectRef(kind="evidence", id=uuid4(), revision=1),
            source_kind="document", locator="fictional.fixture", content_hash="fixture-hash",
            subject_ref=subject, period=period, published_at=AS_OF - timedelta(days=1),
            acquired_at=AS_OF - timedelta(days=1), available_from=AS_OF - timedelta(days=1),
            verification="verified", conflict="none", value=Quantity(value=amount, unit=unit, currency="CNY"))
        # Explicit fixture ingestion; production document/FCFE ingestion remains disabled.
        session.add(SavedEvidence(id=str(identifier), revision=1, owner_id="local_owner", payload=record.model_dump(mode="json")))
        refs.append(ObjectRef(kind="evidence", id=identifier, revision=1))
    await session.flush()
    request = ReverseDCFRequest(as_of=AS_OF, equity_value_ref=refs[0], annual_fcfe_refs=tuple(refs[1:]),
        main_rate=assumption("discount_rate", "0.09"),
        sensitivity_rates=(assumption("discount_rate", "0.08"), assumption("discount_rate", "0.10")),
        terminal_growth=assumption("terminal_growth", "0.02"))
    qualification = ValuationQualification(equity_value_ref=refs[0], annual_fcfe_refs=tuple(refs[1:]),
        derivation_version="verified_fixture.v1", accounting_basis="ordinary_equity_fcfe",
        latest_complete_year=2025, ordinary_equity=True, non_financial=True,
        same_currency=True, consistent_scope=True, known_net_borrowing=True, known_cash_interest=True,
        interest_classification_checked=True, no_overlapping_financing=True,
        no_unhandled_material_changes=True, non_operating_assets_consistent=True)
    return request, qualification, subject


def test_solver_known_solution_monotonicity_and_prototype():
    known = solve(D(1000), D(100), D("0.1"), D(0))
    assert known.status == Status.OK and abs(known.growth) < D("1e-8")
    assert known.interval_width <= D("1e-8") and known.relative_residual <= D("1e-6")
    assert 0 < known.iterations <= 200
    assert solve(D(1200), D(100), D("0.1"), D(0)).growth > known.growth
    assert solve(D(1000), D(100), D("0.12"), D(0)).growth > known.growth
    for base, expected in (("8.2", ("11.9", "16.0", "19.7")), ("9.1", ("9.4", "13.3", "16.9"))):
        for rate, percent in zip((".08", ".09", ".10"), expected, strict=True):
            result = solve(D("212.4"), D(base), D(rate), D(".02"))
            assert (result.growth * 100).quantize(D(".1")) == D(percent)
    median = solve(D("212.4"), D("8.2"), D(".09"), D(".02"))
    assert (median.terminal_pv_fraction * 100).quantize(D(".1")) == D("76.7")


@pytest.mark.parametrize("market,base,rate,terminal,status,reason", [
    ("1e20", "1", ".1", "0", Status.OUTSIDE_SEARCH_RANGE, "above_search_range"),
    (".0000001", "100", ".1", "0", Status.OUTSIDE_SEARCH_RANGE, "below_search_range"),
    ("1000", "100", ".02", ".02", Status.CALCULATION_ERROR, "invalid_parameters"),
    ("1000", "-1", ".1", "0", Status.CALCULATION_ERROR, "invalid_parameters"),
])
def test_solver_failures_have_no_result(market, base, rate, terminal, status, reason):
    result = solve(D(market), D(base), D(rate), D(terminal))
    assert result.status == status and result.reason == reason
    assert result.growth is None and result.terminal_pv_fraction is None


@pytest.mark.asyncio
async def test_saved_prototype_units_and_receipt_replay(db_session):
    request, qualified, subject = await seed(db_session)
    service = ValuationService(db_session, FixtureQualification(qualified))
    operation = uuid4()
    output = await service.reverse_dcf("local_owner", request, operation)
    assert output["status"] == "ok"
    record = output["calculation"]
    assert len(record["details"]["scenarios"]) == 6
    assert record["details"]["report_age_days"] == [1005, 639, 274]
    assert len(record["fact_refs"]) == 4 and record["assumptions"][0]["rationale"]
    assert set(await service.scope("local_owner", request)) == {subject}
    ref = ObjectRef.model_validate(output["calculation_ref"])
    assert (await EvidenceService(db_session).get_calculation("local_owner", ref, AS_OF)).data.details == record["details"]
    await ReportOverdueRepository(db_session).record(listing_id=int(subject.id), report_period=date(2026, 6, 30),
        deadline=date(2026, 8, 31), detected_on=date(2026, 9, 1), provider="fixture")
    assert await service.reverse_dcf("local_owner", request, operation) == output
    with pytest.raises(InvestorError, match="different request"):
        await service.reverse_dcf("local_owner", request.model_copy(update={"as_of": AS_OF - timedelta(hours=1)}), operation)
    assert (await service.reverse_dcf("local_owner", request, uuid4()))["status"] == "stale_inputs"


@pytest.mark.asyncio
async def test_default_mapping_missing_and_no_cfo_minus_capex_alias(db_session):
    request, _, _ = await seed(db_session)
    output = await ValuationService(db_session).reverse_dcf("local_owner", request, uuid4())
    assert output["status"] == "missing_inputs" and output["result"] is None
    assert "fcfe_source_mapping_unavailable" in output["calculation"]["errors"]
    assert all(c["growth"] is None for c in output["calculation"]["details"]["scenarios"])


@pytest.mark.parametrize("flag,status", [
    ("ordinary_equity", "not_applicable"), ("non_financial", "not_applicable"),
    ("consistent_scope", "not_applicable"), ("known_net_borrowing", "missing_inputs"),
    ("known_cash_interest", "missing_inputs"), ("interest_classification_checked", "not_applicable"),
    ("no_overlapping_financing", "not_applicable"), ("non_operating_assets_consistent", "not_applicable"),
    ("no_unhandled_material_changes", "not_applicable"),
])
@pytest.mark.asyncio
async def test_accounting_qualification_failures(db_session, flag, status):
    request, qualified, _ = await seed(db_session)
    output = await ValuationService(db_session, FixtureQualification(replace(qualified, **{flag: False}))).reverse_dcf(
        "local_owner", request, uuid4())
    assert output["status"] == status and output["result"] is None
    assert flag in output["calculation"]["errors"]


@pytest.mark.parametrize("amounts,reason", [(("7", "-1", "9"), "non_positive_fcfe"),
    (("1", "2", "4"), "fcfe_max_min_exceeds_three")])
@pytest.mark.asyncio
async def test_negative_and_volatile_cashflow(db_session, amounts, reason):
    request, qualified, _ = await seed(db_session, amounts=amounts)
    output = await ValuationService(db_session, FixtureQualification(qualified)).reverse_dcf("local_owner", request, uuid4())
    assert output["status"] == "not_applicable" and reason in output["calculation"]["errors"]


@pytest.mark.asyncio
async def test_missing_year_assumptions_and_per_cell_failure(db_session):
    request, qualified, _ = await seed(db_session)
    missing = request.model_copy(update={"annual_fcfe_refs": request.annual_fcfe_refs[:2]})
    output = await ValuationService(db_session, FixtureQualification(replace(qualified, annual_fcfe_refs=missing.annual_fcfe_refs))).reverse_dcf("local_owner", missing, uuid4())
    assert output["status"] == "missing_inputs" and output["result"] is None
    bad = request.model_copy(update={"main_rate": request.main_rate.model_copy(update={"rationale": None})})
    output = await ValuationService(db_session, FixtureQualification(qualified)).reverse_dcf("local_owner", bad, uuid4())
    assert output["status"] == "missing_assumptions"
    bad = request.model_copy(update={"sensitivity_rates": (assumption("discount_rate", ".01"), assumption("discount_rate", ".10"))})
    output = await ValuationService(db_session, FixtureQualification(qualified)).reverse_dcf("local_owner", bad, uuid4())
    assert output["status"] == "ok"
    cells = output["calculation"]["details"]["scenarios"]
    assert [c["status"] for c in cells] == ["ok", "calculation_error", "ok", "ok", "calculation_error", "ok"]
    assert cells[1]["growth"] is None and "median_1_growth" not in output["result"]
    bad = request.model_copy(update={"main_rate": assumption("discount_rate", ".01")})
    output = await ValuationService(db_session, FixtureQualification(qualified)).reverse_dcf("local_owner", bad, uuid4())
    assert output["result"] is None and output["calculation"]["results"] == {}


@pytest.mark.parametrize("change,status", [
    ({"verification": "candidate"}, "missing_inputs"),
    ({"conflict": "unresolved"}, "missing_inputs"),
    ({"available_from": AS_OF + timedelta(seconds=1)}, "missing_inputs"),
    ({"value": Quantity(value="8.2", unit="shares", currency="CNY")}, "not_applicable"),
    ({"value": Quantity(value="8.2", unit="亿元", currency="USD")}, "not_applicable"),
    ({"period": Period(start=date(2024, 1, 1), end=date(2024, 6, 30), basis="semi")}, "not_applicable"),
])
@pytest.mark.asyncio
async def test_saved_evidence_visibility_and_units(db_session, change, status):
    request, qualified, _ = await seed(db_session)
    old = await db_session.get(SavedEvidence, (str(request.annual_fcfe_refs[1].id), 1))
    replacement = EvidenceRecord.model_validate(old.payload).model_copy(update=change)
    # Replace the fixture with a separate immutable version, never modify a stored fact.
    replacement = replacement.model_copy(update={"id": uuid4()})
    db_session.add(SavedEvidence(id=str(replacement.id), revision=1, owner_id="local_owner", payload=replacement.model_dump(mode="json")))
    await db_session.flush()
    refs = (request.annual_fcfe_refs[0], ObjectRef(kind="evidence", id=replacement.id, revision=1), request.annual_fcfe_refs[2])
    request = request.model_copy(update={"annual_fcfe_refs": refs})
    qualified = replace(qualified, annual_fcfe_refs=refs)
    output = await ValuationService(db_session, FixtureQualification(qualified)).reverse_dcf("local_owner", request, uuid4())
    assert output["status"] == status and output["result"] is None
    if status == "missing_inputs":
        assert len(output["calculation"]["fact_refs"]) == 3


@pytest.mark.asyncio
async def test_unit_scaling_equivalent_and_rejected_mixed_subject(db_session):
    request, qualified, _ = await seed(db_session, amounts=("76000", "82000", "91000"), market="2124000", unit="万元")
    output = await ValuationService(db_session, FixtureQualification(qualified)).reverse_dcf("local_owner", request, uuid4())
    assert (D(output["result"]["median_0_growth"]["value"]) * 100).quantize(D(".1")) == D("16.0")
    other_request, other_qualified, _ = await seed(db_session)
    other = await ListingRepository(db_session).ensure_a_share_listing("000001.SZ", company_name="Other fixture")
    row = await db_session.get(SavedEvidence, (str(other_request.annual_fcfe_refs[0].id), 1))
    material = EvidenceRecord.model_validate(row.payload).model_copy(update={"id": uuid4(), "subject_ref": ObjectRef(kind="listing", id=other.id, revision=1)})
    db_session.add(SavedEvidence(id=str(material.id), revision=1, owner_id="local_owner", payload=material.model_dump(mode="json")))
    await db_session.flush()
    refs = (ObjectRef(kind="evidence", id=material.id, revision=1), *other_request.annual_fcfe_refs[1:])
    other_request = other_request.model_copy(update={"annual_fcfe_refs": refs})
    service = ValuationService(db_session, FixtureQualification(replace(other_qualified, annual_fcfe_refs=refs)))
    assert len(await service.scope("local_owner", other_request)) == 2
    output = await service.reverse_dcf("local_owner", other_request, uuid4())
    assert output["status"] == "not_applicable" and "mixed_subjects" in output["calculation"]["errors"]


async def seed_bars(session, *, negative=False, count=260, old=False):
    listing = await ListingRepository(session).ensure_a_share_listing("600519.SH", company_name="Fictional fixture")
    repository = MarketSnapshotRepository(session)
    end = date(2026, 9, 30) - (timedelta(days=30) if old else timedelta())
    rows = []
    # Exact left boundary is stored and audited but must not enter the distribution.
    dates = [years_before(AS_OF.date(), 5), *(end - timedelta(days=count - 1 - i) for i in range(count))]
    for index, day in enumerate(dates):
        pe = D(-1) if index == len(dates) - 1 and negative else D(index % 17 + 1)
        row = await repository.insert(listing_id=listing.id, trade_date=day,
            open=D(10), high=D(10), low=D(10), close=D(10), volume=D(100),
            pe_ttm=pe, pb_mrq=D("1.2"), provider="baostock",
            captured_at=AS_OF - timedelta(days=1), is_backfill=False)
        rows.append(row)
    # A later recapture and future-known backfill must never enter a strict historical view.
    await repository.insert(listing_id=listing.id, trade_date=end,
        open=D(10), high=D(10), low=D(10), close=D(10), volume=D(100),
        pe_ttm=D(9999), pb_mrq=D(9999), provider="baostock",
        captured_at=AS_OF + timedelta(seconds=1), is_backfill=True)
    return listing, rows


@pytest.mark.parametrize("metric", ["pe", "pb"])
@pytest.mark.asyncio
async def test_percentile_exact_existing_algorithm_and_versioned_inputs(db_session, metric):
    _, rows = await seed_bars(db_session)
    request = PercentileRequest(ticker="600519.SH", as_of=AS_OF, metric=metric)
    output = await ValuationService(db_session).percentile("local_owner", request, uuid4())
    bars = [Bar(trade_date=r.trade_date, close=float(r.close), pe_ttm=float(r.pe_ttm), pb_mrq=float(r.pb_mrq)) for r in rows]
    expected = next(f for f in VALUATION_FACTORS if f.name.startswith(metric + "_")).compute(
        SnapshotView.build(ticker=request.ticker, as_of=AS_OF.date(), raw_bars=bars))
    assert D(output["result"]["percentile"]["value"]) == D(str(expected.value))
    record = output["calculation"]
    assert record["details"]["factor"]["inputs_ref"] == dict(expected.inputs_ref)
    assert record["details"]["factor"]["inputs_ref"]["valid_sessions"] == 260
    assert record["details"]["factor"]["inputs_ref"]["window"][0] == "2021-10-01"
    assert "short_history" in record["quality"]
    assert len(record["details"]["inputs"]) == 261
    assert record["details"]["inputs"][-1]["ref"]["id"] == rows[-1].id
    assert record["details"]["inputs"][-1]["ref"]["revision"] == rows[-1].version
    assert len(record["fact_refs"]) == 2
    from investment_agent.valuation.tools import tool_summary
    summary = tool_summary(output)
    assert "calculation" not in summary and "inputs" not in str(summary.keys())
    assert summary["coverage"]["inputs_ref"]["valid_sessions"] == 260
    assert len(str(summary)) < 3000


@pytest.mark.asyncio
async def test_percentile_matches_a_hand_computed_oracle(db_session):
    """Hard-coded oracle, independent of the factor implementation.

    260 in-window sessions: 100 with pe_ttm=8, 159 with pe_ttm=12 and the
    latest with pe_ttm=10. Inclusive percentile of the latest (10):
    count(value <= 10) / valid_sessions = (100 + 1) / 260 = 0.388462.
    """
    listing = await ListingRepository(db_session).ensure_a_share_listing("600519.SH", company_name="Fictional fixture")
    repository = MarketSnapshotRepository(db_session)
    end = date(2026, 9, 30)
    count = 260

    def pe(index: int) -> D:
        if index < 100:
            return D(8)
        if index < count - 1:
            return D(12)
        return D(10)

    for index in range(count):
        await repository.insert(listing_id=listing.id, trade_date=end - timedelta(days=count - 1 - index),
            open=D(10), high=D(10), low=D(10), close=D(10), volume=D(100),
            pe_ttm=pe(index), pb_mrq=D("1.2"), provider="baostock",
            captured_at=AS_OF - timedelta(days=1), is_backfill=False)
    output = await ValuationService(db_session).percentile("local_owner",
        PercentileRequest(ticker="600519.SH", as_of=AS_OF, metric="pe"), uuid4())
    assert output["status"] == "ok"
    assert D(output["result"]["percentile"]["value"]) == D("0.388462")
    ref = output["calculation"]["details"]["factor"]["inputs_ref"]
    assert ref["valid_sessions"] == 260 and ref["current"] == 10.0
    assert ref["excluded_non_positive"] == 0 and ref["sessions"] == 260


@pytest.mark.parametrize("options,status,flag", [
    ({"negative": True}, "not_applicable", "negative_earnings"),
    ({"count": 239}, "missing_inputs", "short_history"),
    ({"old": True}, "stale_inputs", "stale_price"),
])
@pytest.mark.asyncio
async def test_percentile_quality_failure_no_numbers(db_session, options, status, flag):
    await seed_bars(db_session, **options)
    output = await ValuationService(db_session).percentile("local_owner",
        PercentileRequest(ticker="600519.SH", as_of=AS_OF, metric="pe"), uuid4())
    assert output["status"] == status and output["result"] is None
    assert output["calculation"]["results"] == {} and flag in output["calculation"]["quality"]


@pytest.mark.asyncio
async def test_percentile_overdue_and_replay_after_new_data(db_session):
    listing, _ = await seed_bars(db_session)
    service = ValuationService(db_session)
    request = PercentileRequest(ticker="600519.SH", as_of=AS_OF, metric="pb")
    operation = uuid4()
    output = await service.percentile("local_owner", request, operation)
    await ReportOverdueRepository(db_session).record(listing_id=listing.id,
        report_period=date(2026, 6, 30), deadline=date(2026, 8, 31),
        detected_on=date(2026, 9, 1), provider="fixture")
    assert await service.percentile("local_owner", request, operation) == output
    overdue = await service.percentile("local_owner", request, uuid4())
    assert overdue["status"] == "stale_inputs" and overdue["result"] is None
    assert "report_overdue" in overdue["calculation"]["errors"]


@pytest.mark.asyncio
async def test_file_sqlite_concurrent_receipts_and_outer_rollback(tmp_path):
    async with engine_lifespan(f"sqlite+aiosqlite:///{tmp_path / 'valuation.db'}") as engine:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            request, qualified, _ = await seed(session)
        operation = uuid4()
        async def calculate():
            async with session_scope() as session:
                return await ValuationService(session, FixtureQualification(qualified)).reverse_dcf(
                    "local_owner", request, operation)
        one, two = await asyncio.gather(calculate(), calculate())
        assert one == two
        async with session_scope() as session:
            assert await session.scalar(select(func.count()).select_from(SavedCalculation)) == 1
        async with session_scope() as session:
            rolled = await ValuationService(session, FixtureQualification(qualified)).reverse_dcf(
                "local_owner", request, uuid4())
            await session.rollback()
        async with session_scope() as session:
            assert await session.get(SavedCalculation, (rolled["calculation_ref"]["id"], 1)) is None
            assert await session.scalar(select(func.count()).select_from(SavedCalculation)) == 1


def test_native_registration_closed_parameters_and_bounded_sensitivity():
    dcf, percentile = valuation_registrations()
    assert dcf.name == "reverse_dcf_fcfe" and percentile.name == "historical_valuation_percentile"
    payload = {"as_of": AS_OF, "equity_value_ref": ObjectRef(kind="evidence", id=uuid4(), revision=1), "annual_fcfe_refs": ()}
    for control in ("operation_id", "verified", "subject_ref", "approval", "equity_value", "applicable"):
        with pytest.raises(ValidationError):
            ReverseDCFRequest(**payload, **{control: True})
    with pytest.raises(ValidationError):
        ReverseDCFRequest(**payload, sensitivity_rates=tuple(assumption("discount_rate", ".1") for _ in range(7)))
    with pytest.raises(ValidationError):
        ReverseDCFRequest(**{**payload, "as_of": AS_OF.replace(tzinfo=None)})


@pytest.mark.asyncio
async def test_trusted_native_tool_permission_scope_replay_and_time(tmp_path):
    from investment_agent.agent.execution import ExecutionDeniedError
    from investment_agent.agent.tools import ExecutionContext, TrustedTools
    from tests.test_agent_execution import admitted, grant, settings
    async with engine_lifespan(f"sqlite+aiosqlite:///{tmp_path / 'trusted-valuation.db'}") as engine:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            request, qualified, subject = await seed(session)
        registration = valuation_registrations(FixtureQualification(qualified))[0]
        base = settings()
        _, claim = await admitted(base, grant(tool_names=(registration.name,), scope_refs=(subject,)))
        trusted = TrustedTools(ExecutionContext(claim, session_scope, base), (registration,))
        output = await trusted.call(registration, request)
        assert output["status"] == "ok" and "calculation" not in output
        assert await trusted.call(registration, request) == output
        async with session_scope() as session:
            assert await session.scalar(select(func.count()).select_from(SavedCalculation)) == 1
        _, claim = await admitted(base, grant(scope_refs=(subject,)))
        with pytest.raises(ExecutionDeniedError):
            await TrustedTools(ExecutionContext(claim, session_scope, base), (registration,)).call(registration, request)
        _, claim = await admitted(base, grant(tool_names=(registration.name,)))
        with pytest.raises(ExecutionDeniedError):
            await TrustedTools(ExecutionContext(claim, session_scope, base), (registration,)).call(registration, request)
        task, claim = await admitted(base, grant(tool_names=(registration.name,), scope_refs=(subject,)))
        with pytest.raises(ExecutionDeniedError, match="tool_as_of_outside_task_binding"):
            await TrustedTools(ExecutionContext(claim, session_scope, base), (registration,)).call(
                registration, request.model_copy(update={"as_of": task.binding.as_of + timedelta(seconds=1)}))


@pytest.mark.asyncio
async def test_assumption_evidence_subject_cannot_bypass_trusted_scope(tmp_path):
    from investment_agent.agent.execution import ExecutionDeniedError
    from investment_agent.agent.tools import ExecutionContext, TrustedTools
    from tests.test_agent_execution import admitted, grant, settings
    async with engine_lifespan(f"sqlite+aiosqlite:///{tmp_path / 'assumption-scope.db'}") as engine:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            request, qualified, subject = await seed(session)
            other = await ListingRepository(session).ensure_a_share_listing("000001.SZ", company_name="Other fixture")
            source_row = await session.get(SavedEvidence, (str(request.annual_fcfe_refs[0].id), 1))
            source = EvidenceRecord.model_validate(source_row.payload).model_copy(update={"id": uuid4(),
                "subject_ref": ObjectRef(kind="listing", id=other.id, revision=1)})
            session.add(SavedEvidence(id=str(source.id), revision=1, owner_id="local_owner", payload=source.model_dump(mode="json")))
            request = request.model_copy(update={"main_rate": request.main_rate.model_copy(update={
                "source_ref": ObjectRef(kind="evidence", id=source.id, revision=1)})})
        spec = valuation_registrations(FixtureQualification(qualified))[0]
        base = settings()
        _, claim = await admitted(base, grant(tool_names=(spec.name,), scope_refs=(subject,)))
        with pytest.raises(ExecutionDeniedError):
            await TrustedTools(ExecutionContext(claim, session_scope, base), (spec,)).call(spec, request)
        async with session_scope() as session:
            assert await session.scalar(select(func.count()).select_from(SavedCalculation)) == 0


@pytest.mark.asyncio
async def test_cross_source_price_conflict_saves_gap_instead_of_unusable_fact(db_session):
    listing, rows = await seed_bars(db_session)
    await MarketSnapshotRepository(db_session).insert(listing_id=listing.id,
        trade_date=rows[-1].trade_date, open=D(20), high=D(20), low=D(20), close=D(20),
        volume=D(100), pe_ttm=rows[-1].pe_ttm, provider="tencent",
        captured_at=AS_OF - timedelta(hours=1))
    output = await ValuationService(db_session).percentile("local_owner",
        PercentileRequest(ticker="600519.SH", as_of=AS_OF, metric="pe"), uuid4())
    assert output["status"] == "missing_inputs" and output["result"] is None
    assert "cross_source_conflict" in output["calculation"]["errors"]
    assert len(output["calculation"]["fact_refs"]) == 1
