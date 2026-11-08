"""AG10 deterministic arithmetic, saved provenance and trusted valuation boundaries."""

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from investment_agent.evidence.contracts import EvidenceRecord, Period
from investment_agent.evidence.store import EvidenceService
from investment_agent.factors.field_resolution import FieldResolution
from investment_agent.factors.library import CORE_FACTORS
from investment_agent.factors.snapshot_model import Bar, FinancialPeriod, SnapshotView
from investment_agent.factors.valuation import VALUATION_FACTORS
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.enums import CalculationStatus as Status
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.report_overdue import ReportOverdueRepository
from investment_agent.valuation.contracts import (
    DuPontRequest,
    PercentileRequest,
    ReverseDCFRequest,
)
from investment_agent.valuation.service import ValuationService
from investment_agent.valuation.solver import solve
from investment_agent.valuation.tools import valuation_registrations
from tests.support.valuation import (  # noqa: F401
    AS_OF,
    D,
    FixtureQualification,
    assumption,
    seed,
    seed_bars,
)


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
    end = date(2026, 9, 30)
    count = 260

    def pe(index: int) -> D:
        if index < 100:
            return D(8)
        if index < count - 1:
            return D(12)
        return D(10)

    repository = MarketSnapshotRepository(db_session)
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
    dcf, percentile, dupont = valuation_registrations()
    assert dcf.name == "reverse_dcf_fcfe" and percentile.name == "historical_valuation_percentile"
    assert dupont.name == "roe_dupont"
    with pytest.raises(ValidationError):
        DuPontRequest(ticker="600519.SH", as_of=AS_OF, operation_id=uuid4())
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
    from tests.support.execution import admitted, grant, settings
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
    from tests.support.execution import admitted, grant, settings
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


# --------------------------------------------------------------------------- #
# roe_dupont.v1: ROE identity decomposition (roe_dupont factor + tool)
# --------------------------------------------------------------------------- #


def _dupont_view(fields: dict[str, float | None]) -> SnapshotView:
    # akshare-anchored, pre-resolved period: ``sources`` keeps the view from
    # re-anchoring to baostock and renaming the factor-layer fields away.
    sources = {name: FieldResolution("akshare", "unverified", None) for name in fields}
    period = FinancialPeriod(report_period=date(2025, 12, 31), period_kind="annual",
                             available_from=date(2026, 4, 1), availability="actual",
                             fields=dict(fields), sources=sources, ref="financial_snapshot:fixture@1")
    return SnapshotView.build(ticker="600519.SH", as_of=AS_OF.date(), periods=(period,))


def test_roe_dupont_factor_identity_components_and_gates():
    factor = next(f for f in CORE_FACTORS if f.name == "roe_dupont")
    fields = {"net_profit_parent": 12.0, "revenue_total": 100.0,
              "total_assets": 200.0, "total_equity": 80.0}
    value = factor.compute(_dupont_view(fields))
    assert value.value == pytest.approx(0.15) and value.quality == "ok"
    components = value.inputs_ref["components"]
    assert components["net_margin"] == pytest.approx(0.12)
    assert components["asset_turnover"] == pytest.approx(0.5)
    assert components["equity_multiplier"] == pytest.approx(2.5)
    assert components["roe_product"] == pytest.approx(0.15)
    # No stored roe in the same akshare row means no residual, never a guessed one.
    assert components["stored_roe"] is None and components["identity_residual"] is None

    residual = factor.compute(_dupont_view({**fields, "roe": 0.14}))
    assert residual.inputs_ref["components"]["identity_residual"] == pytest.approx(0.01)

    missing = factor.compute(_dupont_view({"net_profit_parent": 12.0}))
    assert missing.value is None
    non_positive = factor.compute(_dupont_view({**fields, "total_equity": -5.0}))
    assert non_positive.value is None and "non_positive_denominator" in non_positive.flags
    no_report = factor.compute(SnapshotView.build(ticker="600519.SH", as_of=AS_OF.date()))
    assert no_report.value is None and "no_annual_report" in no_report.flags


@pytest.mark.asyncio
async def test_dupont_service_known_answer_overdue_and_replay(db_session):
    listing = await ListingRepository(db_session).ensure_a_share_listing("600519.SH", company_name="Fictional fixture")
    db_session.add(FinancialSnapshot(listing_id=listing.id, report_period=date(2025, 12, 31),
        period_kind="annual", provider="akshare", version=1, availability="actual",
        available_from=date(2026, 4, 1), captured_at=AS_OF - timedelta(days=1), is_backfill=False,
        fields={"net_profit_parent": 12.0, "revenue_total": 100.0, "total_assets": 200.0,
                "total_equity": 80.0}, raw_ref="fixture-only"))
    await db_session.flush()
    service = ValuationService(db_session)
    request = DuPontRequest(ticker="600519.SH", as_of=AS_OF)
    operation = uuid4()
    output = await service.dupont("local_owner", request, operation)
    assert output["status"] == "ok"
    record = output["calculation"]
    assert record["method"] == "roe_dupont" and record["method_version"] == "roe_dupont.v1"
    results = {key: float(item["value"]) for key, item in record["results"].items()}
    assert results["net_margin"] == pytest.approx(0.12)
    assert results["asset_turnover"] == pytest.approx(0.5)
    assert results["equity_multiplier"] == pytest.approx(2.5)
    assert results["roe_product"] == pytest.approx(0.15)
    assert "identity_residual" not in results
    assert len(record["fact_refs"]) == 4 and record["details"]["components"]["report_period"] == "2025-12-31"
    # Same operation replays the identical receipt; no new calculation row.
    assert await service.dupont("local_owner", request, operation) == output

    await ReportOverdueRepository(db_session).record(listing_id=listing.id,
        report_period=date(2025, 12, 31), deadline=date(2026, 4, 30),
        detected_on=date(2026, 5, 1), provider="fixture")
    assert (await service.dupont("local_owner", request, uuid4()))["status"] == "stale_inputs"


async def test_dupont_ignores_pre_fix_restated_row_before_its_restatement(db_session):
    """The dupont direct query applies the same read-side legacy gate."""

    listing = await ListingRepository(db_session).ensure_a_share_listing(
        "600519.SH", company_name="Fictional fixture"
    )
    db_session.add(FinancialSnapshot(
        listing_id=listing.id, report_period=date(2025, 12, 31), period_kind="annual",
        provider="akshare", version=1, availability="actual",
        available_from=date(2026, 4, 1),  # pre-fix artifact: original pubDate
        captured_at=datetime(2026, 1, 1, tzinfo=UTC), is_backfill=False,
        fields={"part": "summary", "net_profit_parent": 12.0, "revenue_total": 100.0,
                "total_assets": 200.0, "total_equity": 80.0,
                "notice_date": "2026-06-01", "restated": True,
                "restated_basis": "notice_after_baostock_pub_date"},
        raw_ref="fixture-only"))
    await db_session.flush()
    service = ValuationService(db_session)
    before = await service.dupont(
        "local_owner",
        DuPontRequest(ticker="600519.SH", as_of=datetime(2026, 5, 1, 9, tzinfo=UTC)),
        uuid4(),
    )
    assert before["status"] == "missing_inputs"
    after = await service.dupont(
        "local_owner",
        DuPontRequest(ticker="600519.SH", as_of=datetime(2026, 6, 2, 9, tzinfo=UTC)),
        uuid4(),
    )
    assert after["status"] == "ok"


# --------------------------------------------------------------------------- #
# P1 review item 11 (2026-11-07): solver/service boundaries not covered above.
# Expected values below come from an independent local bisection oracle at 60
# decimal digits, never from the implementation under test.
# --------------------------------------------------------------------------- #


def _oracle_value(base: str, growth: Decimal, rate: str | Decimal,
                  terminal: str | Decimal) -> Decimal:
    """Independent five-year + Gordon value; mirrors the documented formula only."""

    with localcontext() as context:
        context.prec = 60
        base_d, growth_d, rate_d, terminal_d = D(base), D(growth), D(rate), D(terminal)
        cash, discount, interim = base_d, D(1), D(0)
        for _ in range(5):
            cash *= 1 + growth_d
            discount *= 1 + rate_d
            interim += cash / discount
        return interim + cash * (1 + terminal_d) / (rate_d - terminal_d) / discount


def _oracle_growth(market: str, base: str, rate: str | Decimal,
                   terminal: str | Decimal) -> Decimal:
    """Independent bisection on the same documented interval."""

    with localcontext() as context:
        context.prec = 60
        low, high, target = D("-0.95"), D(1), D(market)
        for _ in range(200):
            middle = (low + high) / 2
            if _oracle_value(base, middle, rate, terminal) < target:
                low = middle
            else:
                high = middle
        return low


def test_solver_keeps_growth_inside_the_documented_endpoints():
    # Equality at an endpoint is a last-digit artifact of the 40-digit context,
    # so probe one relative epsilon inside/outside instead of equality. Inside
    # is solved, outside is refused; the interval is never widened.
    lower = _oracle_value("8.2", D("-0.95"), ".09", ".02")
    upper = _oracle_value("8.2", D("1"), ".09", ".02")
    for endpoint, edge, inside_sign in (("-0.95", lower, 1), ("1", upper, -1)):
        inside = solve(edge * (1 + inside_sign * D("1e-12")), D("8.2"), D(".09"), D(".02"))
        assert inside.status == Status.OK, (endpoint, inside.reason)
        assert inside.growth is not None and inside.growth.is_finite()
        assert D("-0.95") <= inside.growth <= D(1)
        assert abs(inside.growth - D(endpoint)) <= D("1e-7")
        assert inside.terminal_pv_fraction is not None
        assert 0 <= inside.terminal_pv_fraction <= 1
        outside = solve(edge * (1 - inside_sign * D("1e-12")), D("8.2"), D(".09"), D(".02"))
        assert outside.status == Status.OUTSIDE_SEARCH_RANGE, (endpoint, outside.reason)
        assert outside.reason == ("below_search_range" if endpoint == "-0.95" else "above_search_range")
        assert outside.growth is None and outside.terminal_pv_fraction is None


@pytest.mark.parametrize("terminal", [".09", ".10", ".20", "1", "-0.0001"])
def test_solver_rejects_terminal_growth_at_or_above_rate_and_negative(terminal):
    # documented constraint: 0 <= gT < r; equality is not "just converged".
    result = solve(D(212.4), D("8.2"), D(".09"), D(terminal))
    assert result.status == Status.CALCULATION_ERROR
    assert result.reason == "invalid_parameters"
    assert result.growth is None and result.terminal_pv_fraction is None


def test_solver_near_singular_g_to_rate_minus_matches_oracle_and_fails_closed():
    # r - g = 1e-3 / 1e-6 still has a finite solution; the oracle confirms it.
    for delta in ("1e-3", "1e-6"):
        rate = D(".09")
        terminal = rate - D(delta)
        result = solve(D(212.4), D("8.2"), rate, terminal)
        expected = _oracle_growth("212.4", "8.2", rate, terminal)
        assert result.status == Status.OK, (delta, result.reason)
        assert result.growth is not None and result.growth.is_finite()
        assert abs(result.growth - expected) <= D("1e-7")
        assert result.terminal_pv_fraction is not None
        assert 0 <= result.terminal_pv_fraction <= 1
    # As g -> r- the Gordon term dwarfs the interim cash flows: the lowest
    # supported h already exceeds the market value, so the method refuses
    # rather than expanding the search interval or returning a huge number.
    for delta in ("1e-9", "1e-12"):
        terminal = D(".09") - D(delta)
        result = solve(D(212.4), D("8.2"), D(".09"), terminal)
        assert result.status == Status.OUTSIDE_SEARCH_RANGE
        assert result.reason == "below_search_range"
        assert result.growth is None


@pytest.mark.asyncio
async def test_reverse_dcf_rejects_terminal_growth_at_or_above_rate(db_session):
    request, qualified, _ = await seed(db_session)
    service = ValuationService(db_session, FixtureQualification(qualified))
    equal = request.model_copy(update={"terminal_growth": assumption("terminal_growth", "0.09")})
    output = await service.reverse_dcf("local_owner", equal, uuid4())
    assert output["status"] == "calculation_error" and output["result"] is None
    assert output["calculation"]["errors"] == ["invalid_parameters"]
    cells = {(cell["basis"], cell["rate"]): cell for cell in output["calculation"]["details"]["scenarios"]}
    # Only the cell where r > gT shares the terminal growth and can solve.
    assert cells[("median", "0.09")]["status"] == "calculation_error"
    assert cells[("median", "0.08")]["status"] == "calculation_error"
    assert cells[("median", "0.10")]["status"] == "ok"
    assert cells[("median", "0.09")]["growth"] is None
    above = request.model_copy(update={"terminal_growth": assumption("terminal_growth", "0.10")})
    output = await service.reverse_dcf("local_owner", above, uuid4())
    assert output["status"] == "calculation_error" and output["result"] is None
    assert all(cell["growth"] is None for cell in output["calculation"]["details"]["scenarios"])


@pytest.mark.asyncio
async def test_reverse_dcf_near_singular_cell_is_not_backfilled(db_session):
    request, qualified, _ = await seed(db_session)
    near = request.model_copy(update={"terminal_growth": assumption("terminal_growth", "0.089999")})
    output = await ValuationService(db_session, FixtureQualification(qualified)).reverse_dcf(
        "local_owner", near, uuid4())
    # Not a status failure: the main cell solves, one sensitivity cell does not.
    assert output["status"] == "ok" and output["result"] is not None
    cells = {(cell["basis"], cell["rate"]): cell for cell in output["calculation"]["details"]["scenarios"]}
    main = cells[("median", "0.09")]
    expected = _oracle_growth("212.4", "8.2", ".09", "0.089999")
    assert main["status"] == "ok" and abs(D(main["growth"]) - expected) <= D("1e-7")
    assert 0 <= D(main["terminal_pv_fraction"]) <= 1
    assert cells[("median", "0.08")]["status"] == "calculation_error"
    assert cells[("median", "0.08")]["growth"] is None
    # The failed cell is absent, never filled from a neighbour.
    assert "median_1_growth" not in output["result"]
    assert "median_0_growth" in output["result"] and "median_2_growth" in output["result"]
    for cell in cells.values():
        if cell["growth"] is not None:
            assert D("-0.95") <= D(cell["growth"]) <= D(1)


@pytest.mark.asyncio
async def test_reverse_dcf_search_range_at_documented_growth_endpoints(db_session):
    request, qualified, _ = await seed(db_session)

    async def run(market: str) -> dict:
        seeded, qualification, _ = await seed(db_session, market=market)
        return await ValuationService(db_session, FixtureQualification(qualification)).reverse_dcf(
            "local_owner", seeded, uuid4())

    # V(-0.95) = 0.3942549...; V(1.00) = 2841.8361... for base 8.2, r 9%, gT 2%.
    inside_low = await run("0.3943")
    assert inside_low["status"] == "ok"
    main = {(c["basis"], c["rate"]): c for c in inside_low["calculation"]["details"]["scenarios"]}[("median", "0.09")]
    assert D("-0.95") <= D(main["growth"]) < D("-0.9499")
    outside_low = await run("0.39425")
    assert outside_low["status"] == "outside_search_range" and outside_low["result"] is None
    assert outside_low["calculation"]["errors"] == ["below_search_range"]
    inside_high = await run("2841.8")
    assert inside_high["status"] == "ok"
    main = {(c["basis"], c["rate"]): c for c in inside_high["calculation"]["details"]["scenarios"]}[("median", "0.09")]
    assert D("0.9999") < D(main["growth"]) <= D("1")
    outside_high = await run("2841.9")
    assert outside_high["status"] == "outside_search_range" and outside_high["result"] is None
    assert outside_high["calculation"]["errors"] == ["above_search_range"]
    for output, rate in ((outside_low, "0.09"), (outside_high, "0.09")):
        cells = {(cell["basis"], cell["rate"]): cell for cell in output["calculation"]["details"]["scenarios"]}
        assert cells[("median", rate)]["growth"] is None
        assert cells[("median", rate)]["status"] == "outside_search_range"
        # No failed cell borrows a number from a solvable neighbour.
        for cell in cells.values():
            assert cell["status"] == "ok" or cell["growth"] is None


@pytest.mark.asyncio
async def test_reverse_dcf_nonpositive_and_extreme_fcfe_never_produce_numbers(db_session):
    async def run(**kwargs) -> dict:
        seeded, qualification, _ = await seed(db_session, **kwargs)
        return await ValuationService(db_session, FixtureQualification(qualification)).reverse_dcf(
            "local_owner", seeded, uuid4())

    zero = await run(amounts=("0", "2", "3"))
    assert zero["status"] == "not_applicable" and zero["result"] is None
    assert "non_positive_fcfe" in zero["calculation"]["errors"]
    assert len(zero["calculation"]["details"]["scenarios"]) == 6
    assert all(cell["growth"] is None for cell in zero["calculation"]["details"]["scenarios"])
    # The 3x stability cut is inclusive at exactly 3 and rejects 3.01.
    exactly_three = await run(amounts=("1", "2", "3"), market="0.5")
    assert exactly_three["status"] == "ok"
    assert "fcfe_max_min_exceeds_three" not in exactly_three["calculation"]["errors"]
    just_over = await run(amounts=("1", "2", "3.01"), market="0.5")
    assert just_over["status"] == "not_applicable" and just_over["result"] is None
    assert "fcfe_max_min_exceeds_three" in just_over["calculation"]["errors"]
    for amounts in (("1e40", "2e40", "3e40"), ("0.0001", "0.0002", "0.0003")):
        extreme = await run(amounts=amounts)
        assert extreme["status"] == "outside_search_range" and extreme["result"] is None
        assert extreme["calculation"]["errors"] in (["below_search_range"], ["above_search_range"])
        assert all(cell["growth"] is None for cell in extreme["calculation"]["details"]["scenarios"])


@pytest.mark.asyncio
async def test_reverse_dcf_nonpositive_equity_value(db_session):
    for market in ("0", "-5"):
        request, qualified, _ = await seed(db_session, market=market)
        output = await ValuationService(db_session, FixtureQualification(qualified)).reverse_dcf(
            "local_owner", request, uuid4())
        assert output["status"] == "not_applicable" and output["result"] is None
        assert "non_positive_equity_value" in output["calculation"]["errors"]
        assert all(cell["growth"] is None for cell in output["calculation"]["details"]["scenarios"])


@pytest.mark.asyncio
async def test_reverse_dcf_missing_equity_value_is_missing_inputs(db_session):
    request, qualified, _ = await seed(db_session)
    absent = ObjectRef(kind="evidence", id=uuid4(), revision=1)
    missing_request = request.model_copy(update={"equity_value_ref": absent})
    output = await ValuationService(
        db_session, FixtureQualification(replace(qualified, equity_value_ref=absent))).reverse_dcf(
        "local_owner", missing_request, uuid4())
    assert output["status"] == "missing_inputs" and output["result"] is None
    assert any(str(absent.id) in error for error in output["calculation"]["errors"])
    assert "non_positive_equity_value" not in output["calculation"]["errors"]
    assert all(cell["growth"] is None for cell in output["calculation"]["details"]["scenarios"])


@pytest.mark.asyncio
async def test_reverse_dcf_missing_equity_with_negative_fcfe_is_missing_inputs(db_session):
    """Regression: a missing equity value must not grade an annual FCFE as equity.

    The guard once read the first amount in request order, so a missing equity
    record shifted a negative first-year FCFE into ``non_positive_equity_value``
    and downgraded the status to ``not_applicable``.
    """
    request, qualified, _ = await seed(db_session, amounts=("-1", "2", "3"))
    absent = ObjectRef(kind="evidence", id=uuid4(), revision=1)
    missing_request = request.model_copy(update={"equity_value_ref": absent})
    output = await ValuationService(db_session, FixtureQualification(
        replace(qualified, equity_value_ref=absent))).reverse_dcf("local_owner", missing_request, uuid4())
    assert output["status"] == "missing_inputs" and output["result"] is None
    assert "non_positive_equity_value" not in output["calculation"]["errors"]
    assert all(cell["growth"] is None for cell in output["calculation"]["details"]["scenarios"])


@pytest.mark.asyncio
async def test_percentile_ignores_backward_adjusted_and_other_provider_series(db_session):
    """除权/复权: the percentile contract reads raw (adjust=none) baostock bars only."""

    listing, rows = await seed_bars(db_session)
    service = ValuationService(db_session)
    request = PercentileRequest(ticker="600519.SH", as_of=AS_OF, metric="pe")
    baseline = await service.percentile("local_owner", request, uuid4())
    # A backward-adjusted (后复权) series with the same trade dates but extreme
    # ratios must never enter the raw distribution or the current value.
    for row in rows:
        db_session.add(MarketSnapshot(**{
            "listing_id": listing.id, "trade_date": row.trade_date,
            "open": D(1), "high": D(1), "low": D(1), "close": D(1), "volume": D(1),
            "pe_ttm": D(9999), "pb_mrq": D(9999), "adjust": "backward",
            "provider": "baostock", "captured_at": AS_OF - timedelta(days=1),
            "version": 1, "is_backfill": False}))
    await db_session.flush()
    output = await service.percentile("local_owner", request, uuid4())
    assert output["status"] == "ok"
    assert output["result"] == baseline["result"]
    assert {item["adjust"] for item in output["calculation"]["details"]["inputs"]} == {"none"}
    assert output["calculation"]["details"]["current_price"] == "10"
    assert output["calculation"]["details"]["factor"]["inputs_ref"]["current"] != 9999
