from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.providers.baostock_market import (
    DailyBar,
    DailyBarSeries,
    DividendYear,
    QuarterlyFinancials,
    StockBasic,
)
from investment_agent.scheduler.trading_calendar import (
    TradingCalendar,
    TradingCalendarSource,
    TradingCalendarYear,
    UnknownTradingCalendarYearError,
)
from investment_agent.simulation.data import (
    ExecutionEvidenceInput,
    collect_experiment_data,
    execution_eligibility,
    import_execution_evidence,
    next_trading_day,
    validate_listing_scope,
)
from investment_agent.storage.models.pit_snapshot import (
    DividendCoverage,
    FinancialSnapshot,
    MarketSnapshot,
)
from investment_agent.storage.models.simulation import SimulationExperimentRecord
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.listing import ListingRepository

DAY = date(2026, 11, 6)
NOW = datetime(2026, 11, 6, 16, tzinfo=UTC)


def proof(listing_id, **changes):
    values = dict(
        listing_id=listing_id,
        trade_date=DAY,
        exchange="SSE",
        board="mainboard",
        security_type="ordinary_a",
        ipo_date=date(2000, 1, 1),
        ordinary_limit_session=True,
        reference_price="10",
        upper_limit="11",
        lower_limit="9",
        tick_size="0.01",
        lot_size=100,
        is_st=False,
        rule_version="reviewed-fixture-v1",
        rule_source="exchange-archive:fixture",
        rule_verified=True,
        corporate_actions_complete=True,
        published_at=NOW - timedelta(days=1),
        captured_at=NOW - timedelta(hours=1),
    )
    return ExecutionEvidenceInput(**(values | changes))


async def experiment(session):
    listing = await ListingRepository(session).ensure_a_share_listing("600519.SH")
    session.add(
        SimulationExperimentRecord(
            id="exp",
            status="draft",
            revision=0,
            config_hash="a" * 64,
            config={"universe": [listing.id]},
            payload={},
        )
    )
    await session.flush()
    return listing.id


async def test_import_is_versioned_scoped_cutoff_and_pinnable(db_session):
    listing_id = await experiment(db_session)
    evidence = proof(listing_id)
    args = dict(
        experiment_id="exp",
        allowed_listing_ids=[listing_id],
        evidence=evidence,
        source_kind="user",
        source_ref="official-archive:one",
        imported_at=NOW,
    )
    first = await import_execution_evidence(db_session, **args)
    assert await import_execution_evidence(db_session, **args) == first
    params = dict(experiment_id="exp", listing_id=listing_id, trade_date=DAY)
    assert not (
        await execution_eligibility(db_session, **params, cutoff=NOW - timedelta(seconds=1))
    ).ready
    ready = await execution_eligibility(db_session, **params, cutoff=NOW)
    assert ready.ready and ready.evidence_id == first
    bad = evidence.model_copy(update={"corporate_actions_complete": False})
    second = await import_execution_evidence(
        db_session,
        **(
            args
            | {
                "evidence": bad,
                "source_ref": "official-archive:two",
                "imported_at": NOW + timedelta(seconds=1),
            }
        ),
    )
    latest = await execution_eligibility(db_session, **params, cutoff=NOW + timedelta(seconds=2))
    assert latest.evidence_id == second
    assert latest.missing == ("corporate_action_coverage_missing",)
    pinned = await execution_eligibility(
        db_session, **params, cutoff=NOW + timedelta(seconds=2), evidence_id=first
    )
    assert pinned.ready
    other = await execution_eligibility(
        db_session, **(params | {"experiment_id": "other"}), cutoff=NOW, evidence_id=first
    )
    assert not other.ready


async def test_rule_and_corporate_action_unknowns_never_default_ready(db_session):
    listing_id = await experiment(db_session)
    await import_execution_evidence(
        db_session,
        experiment_id="exp",
        allowed_listing_ids=[listing_id],
        evidence=proof(
            listing_id,
            rule_verified=False,
            ordinary_limit_session=False,
            unsupported_corporate_actions=("rights-issue",),
        ),
        source_kind="provider",
        source_ref="provider:fixture",
        imported_at=NOW,
    )
    result = await execution_eligibility(
        db_session, experiment_id="exp", listing_id=listing_id, trade_date=DAY, cutoff=NOW
    )
    assert not result.ready
    assert set(result.missing) == {
        "rule_unverified",
        "special_session_unsupported",
        "corporate_action_unsupported",
    }
    with pytest.raises(ValueError, match="scope"):
        await import_execution_evidence(
            db_session,
            experiment_id="exp",
            allowed_listing_ids=[listing_id + 1],
            evidence=proof(listing_id),
            source_kind="user",
            source_ref="fixture",
            imported_at=NOW,
        )
    with pytest.raises(ValueError, match="exchange"):
        await import_execution_evidence(
            db_session,
            experiment_id="exp",
            allowed_listing_ids=[listing_id],
            evidence=proof(listing_id, exchange="SZSE"),
            source_kind="user",
            source_ref="fixture",
            imported_at=NOW,
        )
    with pytest.raises(ValueError, match="before_available"):
        await import_execution_evidence(
            db_session,
            experiment_id="exp",
            allowed_listing_ids=[listing_id],
            evidence=proof(listing_id, captured_at=NOW + timedelta(seconds=1)),
            source_kind="user",
            source_ref="fixture",
            imported_at=NOW,
        )


async def test_non_mainboard_never_admitted(db_session):
    listing = await ListingRepository(db_session).ensure_a_share_listing("300750.SZ")
    with pytest.raises(ValueError, match="unsupported_mainboard"):
        await validate_listing_scope(db_session, [listing.id])


def test_reviewed_calendar_holidays_and_unknown_year():
    calendar = TradingCalendar(
        calendar_id="fixture",
        timezone="Asia/Shanghai",
        source=TradingCalendarSource(
            title="reviewed", url="https://example.test", published_on=DAY
        ),
        years=[TradingCalendarYear(year=2026, closed_dates={date(2026, 11, 9)})],
    )
    assert next_trading_day(calendar, DAY) == date(2026, 11, 10)
    with pytest.raises(UnknownTradingCalendarYearError):
        next_trading_day(calendar, date(2026, 12, 31))


class FakeCollector:
    def __init__(self, wrong_scope=False):
        self.calls = []
        self.wrong_scope = wrong_scope

    async def fetch_stock_basic(self, symbol):
        self.calls.append(("basic", symbol))
        return StockBasic(symbol, "fixture", date(2000, 1, 1), None, True)

    async def fetch_daily_bars(self, symbol, start, end, adjust):
        self.calls.append(("bars", symbol, start, end, adjust))
        bar = DailyBar(
            DAY,
            Decimal(10),
            Decimal(11),
            Decimal(9),
            Decimal(10),
            Decimal(100),
            Decimal(1000),
            None,
            1,
            False,
            None,
            None,
            None,
        )
        return DailyBarSeries(
            "000001.SZ" if self.wrong_scope else symbol, adjust, start, end, (bar,), (), NOW, ()
        )

    async def fetch_dividends(self, symbol, year):
        self.calls.append(("dividends", symbol, year))
        return DividendYear(symbol, year, (), (), (), 0, NOW, ())

    async def fetch_quarterly_financials(self, symbol, year, quarter):
        self.calls.append(("financial", symbol, year, quarter))
        return QuarterlyFinancials(
            symbol, date(2026, 6, 30), date(2026, 8, 30), date(2026, 6, 30), {}, {}, (), NOW, {}
        )


async def test_collects_exact_experiment_scope_without_watchlist_or_claiming_ready(db_session):
    listing_id = await experiment(db_session)
    collector = FakeCollector()
    artifacts = {}

    def archive(name, payload):
        artifacts[name] = payload
        return f"archive/{name}"

    result = await collect_experiment_data(
        db_session,
        listing_ids=[listing_id],
        start=DAY,
        end=DAY,
        client=collector,
        archive=archive,
        financial_periods=[(2026, 2)],
    )
    assert result["inserted_bars"] == 1 and result["ready"] is False
    assert all(call[1] == "600519.SH" for call in collector.calls)
    assert (await db_session.scalars(select(WatchlistItem))).all() == []
    assert len((await db_session.scalars(select(MarketSnapshot))).all()) == 1
    assert len((await db_session.scalars(select(DividendCoverage))).all()) == 1
    assert len((await db_session.scalars(select(FinancialSnapshot))).all()) == 1
    with pytest.raises(ValueError, match="scope_mismatch"):
        await collect_experiment_data(
            db_session,
            listing_ids=[listing_id],
            start=DAY,
            end=DAY,
            client=FakeCollector(wrong_scope=True),
            archive=archive,
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"reference_price": "NaN"},
        {"upper_limit": "Infinity"},
        {"lower_limit": "0"},
        {"tick_size": "0"},
        {"reference_price": "10.001"},
        {"rule_verified": "false"},
        {"corporate_actions_complete": 1},
        {"ordinary_limit_session": "yes"},
        {"corporate_action_refs": ("event", "event")},
    ],
)
def test_execution_proof_rejects_invalid_prices_flags_and_references(patch):
    with pytest.raises(ValueError):
        proof(1, **patch)


def test_execution_tick_alignment_keeps_full_contract_precision():
    result = proof(
        1,
        reference_price="123456789012345678901.123456789012345678",
        lower_limit="123456789012345678901.123456789012345677",
        upper_limit="123456789012345678901.123456789012345679",
        tick_size="0.000000000000000001",
    )
    assert result.reference_price == Decimal("123456789012345678901.123456789012345678")
    with pytest.raises(ValueError, match="align"):
        proof(
            1,
            reference_price="123456789012345678901.123456789012345678",
            lower_limit="123456789012345678901.123456789012345670",
            upper_limit="123456789012345678901.123456789012345680",
            tick_size="0.000000000000000010",
        )


async def test_ipo_session_never_ready_from_ordinary_flag_alone(db_session):
    listing_id = await experiment(db_session)
    await import_execution_evidence(
        db_session,
        experiment_id="exp",
        allowed_listing_ids=[listing_id],
        evidence=proof(listing_id, ipo_date=DAY),
        source_kind="user",
        source_ref="source",
        imported_at=NOW,
    )
    result = await execution_eligibility(
        db_session, experiment_id="exp", listing_id=listing_id, trade_date=DAY, cutoff=NOW
    )
    assert not result.ready and "special_session_unsupported" in result.missing


async def test_import_queries_real_experiment_universe_with_disabled_foreign_keys(db_session):
    listing_id = await experiment(db_session)
    other = await ListingRepository(db_session).ensure_a_share_listing("000001.SZ")
    with pytest.raises(ValueError, match="scope"):
        await import_execution_evidence(
            db_session,
            experiment_id="exp",
            allowed_listing_ids=[listing_id, other.id],
            evidence=proof(other.id, exchange="SZSE"),
            source_kind="user",
            source_ref="source",
            imported_at=NOW,
        )
    with pytest.raises(ValueError, match="scope"):
        await import_execution_evidence(
            db_session,
            experiment_id="missing",
            allowed_listing_ids=[listing_id],
            evidence=proof(listing_id),
            source_kind="user",
            source_ref="source",
            imported_at=NOW,
        )
    with pytest.raises(ValueError, match="controlled listing"):
        await validate_listing_scope(db_session, [True])


@pytest.mark.parametrize("mismatch", ["listing", "date", "exchange", "import_time"])
async def test_pinned_read_rejects_hash_valid_but_inconsistent_indexed_proof(db_session, mismatch):
    from investment_agent.simulation.data import content_hash
    from investment_agent.storage.models.simulation import SimulationExecutionEvidenceRecord

    listing_id = await experiment(db_session)
    body = proof(listing_id).model_dump(mode="json")
    if mismatch == "listing":
        body["listing_id"] = listing_id + 100
    elif mismatch == "date":
        body["trade_date"] = str(DAY - timedelta(days=1))
    elif mismatch == "exchange":
        body["exchange"] = "SZSE"
    else:
        body["captured_at"] = (NOW + timedelta(seconds=1)).isoformat()
    digest = content_hash(
        {"experiment_id": "exp", "source_kind": "user", "source_ref": "source", "payload": body}
    )
    db_session.add(
        SimulationExecutionEvidenceRecord(
            id="forged",
            experiment_id="exp",
            listing_id=listing_id,
            trade_date=DAY,
            revision=1,
            source_kind="user",
            source_ref="source",
            captured_at=NOW,
            content_hash=digest,
            payload=body,
        )
    )
    await db_session.flush()
    with pytest.raises(ValueError, match="scope|exchange|before_available"):
        await execution_eligibility(
            db_session,
            experiment_id="exp",
            listing_id=listing_id,
            trade_date=DAY,
            cutoff=NOW + timedelta(minutes=1),
            evidence_id="forged",
        )


async def test_collector_rejects_wrong_financial_period_and_duplicate_bar_dates(db_session):
    listing_id = await experiment(db_session)
    with pytest.raises(ValueError, match="financial_scope"):
        await collect_experiment_data(
            db_session,
            listing_ids=[listing_id],
            start=DAY,
            end=DAY,
            client=FakeCollector(),
            archive=lambda name, payload: name,
            financial_periods=[(2026, 1)],
        )

    class DuplicateBars(FakeCollector):
        async def fetch_daily_bars(self, symbol, start, end, adjust):
            from dataclasses import replace

            series = await super().fetch_daily_bars(symbol, start, end, adjust)
            return replace(series, bars=series.bars * 2)

    with pytest.raises(ValueError, match="market_scope"):
        await collect_experiment_data(
            db_session,
            listing_ids=[listing_id],
            start=DAY,
            end=DAY,
            client=DuplicateBars(),
            archive=lambda name, payload: name,
        )


async def test_collector_archive_names_bind_payload_bytes(db_session):
    from investment_agent.simulation.data import content_hash

    listing_id = await experiment(db_session)
    artifacts = {}

    def archive(name, payload):
        artifacts[name] = payload
        return name

    await collect_experiment_data(
        db_session,
        listing_ids=[listing_id],
        start=DAY,
        end=DAY,
        client=FakeCollector(),
        archive=archive,
    )
    assert all(name.endswith(content_hash(payload)) for name, payload in artifacts.items())


def test_readiness_requires_blockers_or_pinned_proof():
    from investment_agent.simulation.data import ExecutionEligibility

    with pytest.raises(ValueError, match="pinned proof"):
        ExecutionEligibility(ready=True, missing=())
    with pytest.raises(ValueError, match="blockers"):
        ExecutionEligibility(ready=True, missing=("missing",))
    with pytest.raises(ValueError, match="blockers"):
        ExecutionEligibility(ready=False, missing=())
