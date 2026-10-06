"""Resumable baostock backfill (P1-A4): change detection, survivorship, industry
history, price bounds, financial skip-existing, chunk planning / resume, dry run."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from investment_agent.providers.baostock_market import BaostockMarketClient
from investment_agent.research.worker import ResearchWorker
from investment_agent.snapshots import backfill
from investment_agent.snapshots.backfill import (
    backfill_prices,
    backfill_status,
    dry_run_estimate,
    estimate_backfill,
    industry_samples,
    month_samples,
    plan_backfill,
    price_backfill_end,
    report_periods,
    scan_constituents,
    weekly_dates,
)
from investment_agent.snapshots.collect import ArtifactWriter
from investment_agent.snapshots.handlers import snapshot_job_handlers
from investment_agent.snapshots.jobs import SNAPSHOT_BACKFILL, SnapshotBackfillPayload
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.pit_snapshot import (
    FinancialSnapshot,
    IndustryClassification,
    MarketSnapshot,
)
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.universe import UniverseRepository
from tests.unit._baostock_history_fake import FakeBaostockHistory
from tests.unit._snapshot_support import CAPTURED_AT, runtime_for, settings_for

AS_OF = date(2026, 10, 6)
LIVE = datetime(2026, 9, 30, 9, 0, tzinfo=UTC)
A, B, C, D, E, F = "600519.SH", "000001.SZ", "300750.SZ", "601398.SH", "001248.SZ", "600000.SH"


def _history(**kwargs) -> FakeBaostockHistory:
    """1-year window 2025-10-06..2026-10-06: B leaves and re-enters, C is removed,
    E (listed 2026-07-02) joins; F is never a member."""

    deadline_pub = {
        date(2026, 6, 30): date(2026, 8, 21),
        date(2026, 3, 31): date(2026, 4, 20),
        date(2025, 12, 31): date(2026, 4, 20),
        date(2024, 12, 31): date(2025, 4, 20),
    }
    financials = {
        (ticker, period): pub
        for ticker in (A, B, C, D)
        for period, pub in deadline_pub.items()
    }
    # E publishes 2025 annual / 2026 Q1 before listing; no 2024 annual.
    financials.update(
        {(E, period): pub for period, pub in deadline_pub.items() if period.year >= 2025}
    )
    return FakeBaostockHistory(
        constituent_changes=[
            (date(2025, 1, 6), {A, B, C, D}),
            (date(2026, 1, 12), {A, C, D}),
            (date(2026, 3, 16), {A, B, C, D}),
            (date(2026, 6, 15), {A, B, D}),
            (date(2026, 7, 6), {A, B, D, E}),
        ],
        industry={
            A: [(date(2021, 1, 4), "C14食品制造业"), (date(2025, 11, 17), "C15酒、饮料和精制茶制造业")],
            B: [(date(2021, 1, 4), "J66货币金融服务")],
            C: [(date(2021, 1, 4), "C38电气机械和器材制造业")],
            D: [(date(2021, 1, 4), "J66货币金融服务")],
            E: [(date(2026, 7, 6), "D44电力、热力生产和供应业")],
            F: [(date(2021, 1, 4), "J66货币金融服务")],
        },
        ipo={
            A: date(2001, 8, 27),
            B: date(1991, 4, 3),
            C: date(2018, 6, 11),
            D: date(2006, 10, 27),
            E: date(2026, 7, 2),
        },
        financials=financials,
        **kwargs,
    )


def _client(backend: FakeBaostockHistory, **kwargs) -> BaostockMarketClient:
    kwargs.setdefault("retry_backoff_seconds", 0)
    return BaostockMarketClient(backend, clock=lambda: CAPTURED_AT, **kwargs)  # type: ignore[arg-type]


def _payload(**kwargs) -> SnapshotBackfillPayload:
    values = {
        "universe_code": "CSI300",
        "as_of": AS_OF,
        "part": "membership",
        "price_years": 1,
        "annual_years": 2,
        "quarters": 2,
    }
    values.update(kwargs)
    return SnapshotBackfillPayload(**values)


def _worker(tmp_path, backend: FakeBaostockHistory) -> ResearchWorker:
    settings = settings_for(tmp_path)
    runtime = dataclasses.replace(
        runtime_for(settings, None), backfill_factory=lambda: _client(backend)
    )
    return ResearchWorker(
        settings=settings,
        worker_id="w",
        handlers=snapshot_job_handlers(settings, runtime),
        kinds=[SNAPSHOT_BACKFILL],
    )


async def _drain(worker: ResearchWorker) -> list[int]:
    ran: list[int] = []
    while (run_id := await worker.run_once()) is not None:
        ran.append(run_id)
    return ran


async def _seed_live() -> dict[str, int]:
    """Production state before the backfill: a live sync, live bars and one live report."""

    async with session_scope() as session:
        universes = UniverseRepository(session)
        snapshot = await _client(_history()).fetch_constituents("CSI300", date(2026, 9, 30))
        await universes.apply_constituent_snapshot("CSI300", snapshot)
        listings = ListingRepository(session)
        ids = {t: (await listings.ensure_a_share_listing(t)).id for t in (A, B, D, E)}
        market = MarketSnapshotRepository(session)
        for day in (date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30)):
            for adjust in ("none", "backward"):
                await market.insert(
                    listing_id=ids[A], trade_date=day, open=Decimal(1), high=Decimal(1),
                    low=Decimal(1), close=Decimal(1), volume=Decimal(1), adjust=adjust,
                    provider="baostock", captured_at=LIVE,
                )
        await FinancialSnapshotRepository(session).insert(
            listing_id=ids[A], report_period=date(2026, 6, 30), period_kind="semi",
            fields={"roe": 0.1}, provider="baostock", captured_at=LIVE,
            availability="actual", available_from=date(2026, 8, 15),
        )
    return ids


# --- pure helpers -------------------------------------------------------------------


def test_calendar_helpers() -> None:
    assert month_samples(date(2025, 10, 6), date(2026, 1, 15)) == [
        date(2025, 10, 6), date(2025, 11, 1), date(2025, 12, 1), date(2026, 1, 1),
        date(2026, 1, 15),
    ]
    assert weekly_dates(date(2026, 1, 1), date(2026, 1, 20)) == [date(2026, 1, 8), date(2026, 1, 15)]
    assert industry_samples(date(2021, 10, 6), AS_OF) == [
        date(2021, 10, 6), date(2022, 1, 1), date(2023, 1, 1), date(2024, 1, 1),
        date(2025, 1, 1), date(2026, 1, 1), AS_OF,
    ]
    assert report_periods(AS_OF, annual_years=6, quarters=4) == [
        date(2026, 6, 30), date(2026, 3, 31), date(2025, 12, 31), date(2025, 9, 30),
        date(2024, 12, 31), date(2023, 12, 31), date(2022, 12, 31), date(2021, 12, 31),
        date(2020, 12, 31),
    ]
    # Q3 2026 (deadline 10-31) is not due yet; the 2025 annual counts once.
    assert report_periods(date(2026, 5, 6), annual_years=1, quarters=2) == [
        date(2026, 3, 31), date(2025, 12, 31)
    ]


def test_price_backfill_bounds() -> None:
    start = date(2021, 10, 6)
    assert price_backfill_end(start, AS_OF, None, earliest_is_backfill=False) == AS_OF
    # Live rows from 2026-09-28: stop the day before.
    assert price_backfill_end(
        start, AS_OF, date(2026, 9, 28), earliest_is_backfill=False
    ) == date(2026, 9, 27)
    # Already backfilled to the window start (holiday slack) -> nothing to do.
    assert price_backfill_end(start, AS_OF, date(2021, 10, 8), earliest_is_backfill=True) is None
    assert price_backfill_end(start, AS_OF, start, earliest_is_backfill=False) is None


def test_payload_validation_and_keys() -> None:
    from investment_agent.snapshots.jobs import idempotency_key

    assert idempotency_key(_payload()) == "snapshot_backfill:CSI300:2026-10-06:membership:p1a2q2"
    chunk = _payload(part="prices", symbols=(A, B), chunk=3)
    assert idempotency_key(chunk).endswith(":prices:p1a2q2:c003")
    with pytest.raises(ValueError):
        _payload(part="prices")
    with pytest.raises(ValueError):
        _payload(symbols=(A,))
    with pytest.raises(ValueError):
        _payload(part="prices", symbols=("sh.600519",), chunk=0)


# --- membership change detection ------------------------------------------------------


async def test_monthly_scan_refines_weekly_only_between_differing_months() -> None:
    backend = _history()
    scan = await scan_constituents(_client(backend), "CSI300", date(2025, 10, 6), AS_OF)
    months = month_samples(date(2025, 10, 6), AS_OF)
    assert scan.monthly_queries == len(months)
    # Four member-set changes -> four differing month pairs, each refined weekly.
    assert scan.differing_months == 4
    queried = [p["date"] for p in backend.methods("query_hs300_stocks")]
    assert len(queried) == scan.queries
    weekly = queried[len(months):]
    assert "2026-01-08" in weekly and "2026-01-15" in weekly
    effective = [s.effective_date for s in scan.snapshots]
    for change in (date(2026, 1, 12), date(2026, 3, 16), date(2026, 6, 15), date(2026, 7, 6)):
        assert change in effective
    assert backend.logins == 1


async def test_full_backfill_through_the_worker(db_session, tmp_path, monkeypatch) -> None:
    del db_session
    monkeypatch.setattr(backfill, "CHUNK_SIZE", 2)
    ids = await _seed_live()
    backend = _history()
    async with session_scope() as session:
        plan = await plan_backfill(session, _payload(), triggered_by="test")
    assert plan.membership.action == "queued" and plan.followups == []

    await _drain(_worker(tmp_path, backend))

    async with session_scope() as session:
        runs = list((await session.execute(select(PipelineRun).order_by(PipelineRun.id))).scalars())
        universes = UniverseRepository(session)
        between = await universes.members_between("CSI300", date(2025, 10, 6), AS_OF)
        on_jan20 = await universes.members_as_of("CSI300", date(2026, 1, 20))
        live_e = await universes.members_as_of("CSI300", AS_OF)
    assert {run.status for run in runs} == {"success"}
    parts = [run.request_json["part"] for run in runs]
    # membership, industry, 3 price chunks + 3 financial chunks (5 symbols, chunks of 2)
    assert parts.count("membership") == 1 and parts.count("industry") == 1
    assert parts.count("prices") == 3 and parts.count("financials") == 3
    membership_meta = runs[0].metadata_json
    assert membership_meta["symbols"] == 5
    assert membership_meta["removed_symbols"] == [C]
    assert membership_meta["change_dates"] == [
        "2026-01-12", "2026-03-16", "2026-06-15", "2026-07-06"
    ]
    # Survivorship set: current + removed, never F.
    assert sorted({m.ticker for m in between}) == sorted([A, B, C, D, E])
    assert [m.ticker for m in on_jan20] == sorted([A, C, D])
    assert sorted(m.ticker for m in live_e) == sorted([A, B, D, E])

    async with session_scope() as session:
        market_rows = list((await session.execute(select(MarketSnapshot))).scalars())
        financial_rows = list((await session.execute(select(FinancialSnapshot))).scalars())
        industry_rows = list(
            (
                await session.execute(
                    select(IndustryClassification).order_by(
                        IndustryClassification.listing_id, IndustryClassification.valid_from
                    )
                )
            ).scalars()
        )
        moutai = await ListingRepository(session).get_a_share_listing(A)
        f_listing = await ListingRepository(session).get_a_share_listing(F)

    # Prices: A's backfill stops before the live rows; no duplicate versions.
    a_rows = [r for r in market_rows if r.listing_id == ids[A]]
    live = [r for r in a_rows if not r.is_backfill]
    backfilled = [r for r in a_rows if r.is_backfill]
    assert len(live) == 6 and all(r.version == 1 for r in a_rows)
    assert max(r.trade_date for r in backfilled) == date(2026, 9, 25)
    assert min(r.trade_date for r in backfilled) == date(2025, 10, 6)
    assert {r.adjust for r in backfilled} == {"none", "backward"}
    # E only has bars from its listing date.
    e_rows = [r for r in market_rows if r.listing_id == ids[E]]
    assert min(r.trade_date for r in e_rows) == date(2026, 7, 2)
    assert all(r.is_backfill for r in e_rows)

    # Financials: A's live 2026-06-30 is not re-fetched; E's pre-listing reports are flagged.
    a_fin = {(r.report_period, r.is_backfill) for r in financial_rows if r.listing_id == ids[A]}
    assert a_fin == {
        (date(2026, 6, 30), False),
        (date(2026, 3, 31), True),
        (date(2025, 12, 31), True),
        (date(2024, 12, 31), True),
    }
    a_profit_calls = [
        p for p in backend.methods("query_profit_data") if p["code"] == "sh.600519"
    ]
    assert {(p["year"], p["quarter"]) for p in a_profit_calls} == {(2026, 1), (2025, 4), (2024, 4)}
    e_fin = {r.report_period: r for r in financial_rows if r.listing_id == ids[E]}
    assert set(e_fin) == {date(2026, 6, 30), date(2026, 3, 31), date(2025, 12, 31)}
    assert e_fin[date(2025, 12, 31)].fields["pre_listing"] is True
    assert e_fin[date(2026, 6, 30)].fields["pre_listing"] is False
    assert all(r.availability == "actual" for r in e_fin.values())
    assert e_fin[date(2026, 6, 30)].available_from == date(2026, 8, 21)
    fin_meta = [r.metadata_json for r in runs if r.request_json["part"] == "financials"]
    assert any(f"{E}:2025-12-31" in meta["pre_listing"] for meta in fin_meta)
    assert all(meta["report_overdue"].startswith("not backfilled") for meta in fin_meta)

    # Industry: A reclassified inside the window; F (non-member) never created.
    assert moutai is not None and f_listing is None
    a_industry = [(r.code, r.valid_from, r.valid_to) for r in industry_rows if r.listing_id == moutai.id]
    assert a_industry == [
        ("C14", date(2021, 1, 4), date(2025, 11, 17)),
        ("C15", date(2025, 11, 17), None),
    ]
    assert len(backend.methods("query_stock_industry")) == 3

    # Status: everything done.
    async with session_scope() as session:
        as_of, statuses = await backfill_status(session, "CSI300")
    assert as_of == AS_OF
    by_part = {s.part: s for s in statuses}
    assert by_part["prices"].done_symbols == by_part["prices"].total_symbols == 5
    assert by_part["financials"].done_symbols == 5
    assert by_part["membership"].statuses == {"success": 1}
    assert "removed 1" in by_part["membership"].detail

    # Re-running the plan changes nothing and queues nothing.
    before = len(runs)
    async with session_scope() as session:
        again = await plan_backfill(session, _payload(), triggered_by="test")
        count = (await session.execute(select(func.count(PipelineRun.id)))).scalar_one()
    assert again.membership.action == "done"
    assert {a.action for a in again.followups} == {"done"}
    assert count == before


async def test_failed_symbol_is_resumed_alone(db_session, tmp_path, monkeypatch) -> None:
    del db_session
    monkeypatch.setattr(backfill, "CHUNK_SIZE", 10)
    backend = _history(
        fail=lambda method, params: ("10004011", "bad code")
        if method == "query_history_k_data_plus" and params.get("code") == "sz.000001"
        else None
    )
    async with session_scope() as session:
        await plan_backfill(session, _payload(), triggered_by="test")
    await _drain(_worker(tmp_path, backend))

    async with session_scope() as session:
        _, statuses = await backfill_status(session, "CSI300", AS_OF)
    prices = next(s for s in statuses if s.part == "prices")
    assert prices.statuses == {"partial": 1}
    assert (prices.done_symbols, prices.total_symbols) == (4, 5)
    assert prices.failed_symbols == [B]

    backend.fail = None
    async with session_scope() as session:
        plan = await plan_backfill(session, _payload(), triggered_by="test")
    retries = [a for a in plan.followups if a.action == "retry"]
    assert [(a.part, a.symbols) for a in retries] == [("prices", 1)]
    assert retries[0].key.endswith(":c000:r2")
    calls_before = len(backend.methods("query_history_k_data_plus"))
    await _drain(_worker(tmp_path, backend))
    new_calls = backend.methods("query_history_k_data_plus")[calls_before:]
    assert {p["code"] for p in new_calls} == {"sz.000001"}

    async with session_scope() as session:
        _, statuses = await backfill_status(session, "CSI300", AS_OF)
    prices = next(s for s in statuses if s.part == "prices")
    assert prices.statuses == {"success": 1}
    assert (prices.done_symbols, prices.failed_symbols) == (5, [])


async def test_price_chunk_stops_after_consecutive_transient_failures(db_session, tmp_path) -> None:
    del db_session
    tickers = [f"60000{i}.SH" for i in range(7)]
    async with session_scope() as session:
        listings = ListingRepository(session)
        for ticker in tickers:
            await listings.ensure_a_share_listing(ticker)
    backend = _history(
        fail=lambda method, _params: ("10002007", "network error")
        if method == "query_history_k_data_plus"
        else None
    )
    report = await backfill_prices(
        _payload(part="prices", symbols=tuple(tickers), chunk=0),
        client=_client(backend, max_attempts=1),
        artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
        lease=None,
    )
    assert len(backend.methods("query_history_k_data_plus")) == 5
    assert report.completeness.failed == sorted(tickers)
    skipped = [t for t, (_, reason) in report.completeness.missing.items() if "skipped" in reason]
    assert sorted(skipped) == tickers[5:]
    assert report.status == "failed" and report.all_failures_transient
    assert report.metadata["aborted"]


async def test_dry_run_estimates_without_fetching_data(db_session) -> None:
    del db_session
    backend = _history()
    estimate = await dry_run_estimate(
        _payload(price_years=1, annual_years=6, quarters=4), client=_client(backend)
    )
    called = {method for method, _ in backend.calls} - {"login", "logout"}
    assert called == {"query_hs300_stocks"}
    assert (estimate.symbols, estimate.current, estimate.removed) == (5, 4, 1)
    parts = {part.part: part for part in estimate.parts}
    assert parts["prices"].queries == 10
    assert parts["financials"].queries == 5 * (9 * 4 + 1)
    assert parts["industry"].queries == 3
    assert parts["prices"].seconds == pytest.approx(5 * 12 / 5)
    assert estimate.queries == sum(part.queries for part in estimate.parts)


def test_estimate_for_csi300_scale() -> None:
    estimate = estimate_backfill(
        _payload(price_years=5, annual_years=6, quarters=4),
        symbols=400,
        current=300,
        monthly_queries=61,
        differing_months=12,
        source="test",
    )
    parts = {part.part: part for part in estimate.parts}
    assert parts["prices"].jobs == 20 and parts["prices"].queries == 800
    assert parts["prices"].seconds == pytest.approx(400 * 12)
    assert parts["industry"].seconds == pytest.approx(7 * 60)
    assert parts["membership"].queries == 61 + 36
    assert estimate.removed == 100
    assert timedelta(seconds=estimate.seconds) > timedelta(hours=2)


# --- live smoke (network) ------------------------------------------------------------


@pytest.mark.network
async def test_live_backfill_reference_smoke() -> None:
    client = BaostockMarketClient()
    async with client.session():
        basic = await client.fetch_stock_basic("600519.SH")
        delisted = await client.fetch_stock_basic("600087.SH")
        scan = await scan_constituents(client, "CSI300", date(2021, 10, 6), date(2022, 1, 4))
    assert basic is not None and basic.ipo_date == date(2001, 8, 27) and basic.listed
    assert delisted is not None and delisted.out_date == date(2014, 6, 5)
    assert scan.monthly_queries == 5
    assert all(len(s.members) == 300 for s in scan.snapshots)
    assert scan.snapshots[0].effective_date == date(2021, 10, 4)
