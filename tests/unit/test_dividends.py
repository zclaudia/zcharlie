"""Dividend events (F2 / Q2): baostock parsing, storage, collection, schedule
and backfill."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.providers.baostock_market import (
    BaostockMarketClient,
    merge_dividend_rows,
)
from investment_agent.providers.errors import LookaheadDataError, ProviderDataError
from investment_agent.scheduler.snapshot_jobs import snapshot_payloads
from investment_agent.snapshots.backfill import (
    backfill_dividends,
    dividend_years,
    estimate_backfill,
)
from investment_agent.snapshots.collect import ArtifactWriter
from investment_agent.snapshots.dividends import collect_baostock_dividends
from investment_agent.snapshots.jobs import (
    DIVIDEND_SNAPSHOT,
    DividendSnapshotPayload,
    SnapshotBackfillPayload,
    idempotency_key,
    payload_kind,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.pit_snapshot import DividendCoverage, DividendEventSnapshot
from investment_agent.storage.repositories.dividend import DividendRepository
from investment_agent.storage.repositories.listing import ListingRepository
from tests.unit._baostock_fake import FakeBaostockData
from tests.unit._snapshot_support import CAPTURED_AT, seed_universe

AS_OF = date(2026, 9, 30)


def _client(backend: FakeBaostockData, *, now: datetime = CAPTURED_AT) -> BaostockMarketClient:
    return BaostockMarketClient(backend, clock=lambda: now, retry_backoff_seconds=0)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Provider
# --------------------------------------------------------------------------- #


async def test_fetch_dividends_merges_duplicates_and_reads_the_numeric_cash_column():
    backend = FakeBaostockData()
    answer = await _client(backend).fetch_dividends("601398.SH", 2025)

    assert backend.methods("query_dividend_data") == [
        {"code": "sh.601398", "year": "2025", "yearType": "operate"}
    ]
    assert [event.ex_date for event in answer.events] == [
        date(2025, 1, 7),
        date(2025, 7, 14),
        date(2025, 12, 15),
    ]
    assert answer.duplicates == ("2025-01-07",) and answer.combined == ()
    first = answer.events[0]
    # The complete (final) row wins over the preliminary one.
    assert first.details["dividAgmPumDate"] == "2024-12-03"
    assert first.cash_per_share == Decimal("0.1434")  # not the "10派1.434元" text
    assert first.announce_date == date(2024, 8, 31)
    assert first.implement_date == date(2024, 12, 31)
    assert first.record_date == date(2025, 1, 6)
    assert first.pay_date == date(2025, 1, 7)
    assert first.available_from == date(2024, 8, 31)
    assert first.share_multiplier == 1.0


async def test_fetch_dividends_bonus_and_capitalisation_shares():
    backend = FakeBaostockData()
    client = _client(backend)
    catl = (await client.fetch_dividends("300750.SZ", 2023)).events[0]
    assert catl.cash_per_share == Decimal("2.52")
    assert catl.reserve_per_share == Decimal("0.8") and catl.share_multiplier == 1.8
    haitian = (await client.fetch_dividends("603288.SH", 2021)).events[0]
    assert haitian.stock_per_share == Decimal("0.1")
    assert haitian.share_multiplier == pytest.approx(1.3)
    vanke = await client.fetch_dividends("000002.SZ", 2025)
    assert vanke.events == () and vanke.no_ex_date == 0


async def test_fetch_dividends_guards():
    backend = FakeBaostockData()
    with pytest.raises(LookaheadDataError):
        await _client(backend, now=datetime(2026, 3, 1, tzinfo=UTC)).fetch_dividends(
            "601398.SH", 2026
        )
    backend.extra_dividends[("sh.600036", "2025")] = [
        {"code": "sh.600036", "dividOperateDate": "2024-07-11", "dividCashPsBeforeTax": "1.972"}
    ]
    with pytest.raises(ProviderDataError):
        await _client(backend).fetch_dividends("600036.SH", 2025)


async def test_distinct_distributions_on_one_ex_date_are_summed():
    backend = FakeBaostockData()
    mindray = await _client(backend).fetch_dividends("300760.SZ", 2025)
    (combined,) = [e for e in mindray.events if e.ex_date == date(2025, 5, 29)]
    assert mindray.combined == ("2025-05-29",) and mindray.duplicates == ()
    assert combined.cash_per_share == Decimal("1.97")  # 0.56 + 1.41
    assert len(combined.details["components"]) == 2  # type: ignore[arg-type]
    # A copy without the numeric cash is a duplicate, not a second distribution.
    gree = await _client(backend).fetch_dividends("000651.SZ", 2020)
    (copy,) = [e for e in gree.events if e.ex_date == date(2020, 11, 12)]
    assert gree.duplicates == ("2020-11-12",) and gree.combined == ()
    assert copy.cash_per_share == Decimal("1")


def test_merge_dividend_rows_keeps_the_complete_row_and_counts_rows_without_ex_date():
    rows = (
        {"code": "sh.600000", "dividOperateDate": "2025-07-01", "dividCashPsBeforeTax": "0.5"},
        {
            "code": "sh.600000",
            "dividOperateDate": "2025-07-01",
            "dividCashPsBeforeTax": "0.5",
            "dividPlanDate": "2025-06-20",
        },
        {"code": "sh.600000", "dividOperateDate": "2025-07-01", "dividCashPsBeforeTax": ""},
        {"code": "sh.600000", "dividOperateDate": "", "dividCashPsBeforeTax": "0.3"},
    )
    events, duplicates, combined, no_ex_date = merge_dividend_rows("600000.SH", rows)
    assert [event.cash_per_share for event in events] == [Decimal("0.5")]
    assert events[0].implement_date == date(2025, 6, 20)
    assert duplicates == ("2025-07-01",) and combined == ()
    assert no_ex_date == 1


def test_copy_without_the_capitalisation_column_is_one_distribution():
    row = {
        "code": "sz.300015",
        "dividOperateDate": "2021-06-24",
        "dividCashPsBeforeTax": "0.148289",
        "dividStocksPs": "0.000000",
        "dividReserveToStockPs": "",
    }
    rows = (row, {**row, "dividReserveToStockPs": "0.296579"})
    events, duplicates, combined, _ = merge_dividend_rows("300015.SZ", rows)
    assert duplicates == ("2021-06-24",) and combined == ()
    assert events[0].reserve_per_share == Decimal("0.296579")


# --------------------------------------------------------------------------- #
# Payload, schedule
# --------------------------------------------------------------------------- #


def test_payload_defaults_routing_and_key():
    payload = DividendSnapshotPayload(universe_code="CSI300", as_of=AS_OF)
    assert payload.years == (2025, 2026)
    assert payload_kind(payload) == DIVIDEND_SNAPSHOT
    assert idempotency_key(payload) == "dividend_snapshot:baostock:CSI300:2026-09-30:2025+2026"
    with pytest.raises(ValueError, match="baostock"):
        DividendSnapshotPayload(provider="akshare", universe_code="CSI300", as_of=AS_OF)
    with pytest.raises(ValueError, match="out of range"):
        DividendSnapshotPayload(universe_code="CSI300", as_of=AS_OF, years=(2030,))


def test_weekly_financial_schedule_enqueues_dividends():
    payloads = snapshot_payloads(
        "financial_weekly", today=date(2026, 10, 10), calendar=None, universes=["CSI300"]
    )
    dividends = [p for p in payloads if isinstance(p, DividendSnapshotPayload)]
    assert len(dividends) == 1 and dividends[0].years == (2025, 2026)


# --------------------------------------------------------------------------- #
# Collection and storage
# --------------------------------------------------------------------------- #


async def _events() -> list[DividendEventSnapshot]:
    async with session_scope() as session:
        stmt = select(DividendEventSnapshot).order_by(
            DividendEventSnapshot.listing_id, DividendEventSnapshot.ex_date
        )
        return list((await session.execute(stmt)).scalars().all())


async def _coverage() -> list[DividendCoverage]:
    async with session_scope() as session:
        return list((await session.execute(select(DividendCoverage))).scalars().all())


async def test_collector_stores_events_and_coverage_idempotently(db_session, tmp_path):
    del db_session
    ids = await seed_universe(["601398.SH", "600900.SH", "000002.SZ"])
    backend = FakeBaostockData()
    payload = DividendSnapshotPayload(universe_code="CSI300", as_of=date(2026, 10, 6))

    async def run():
        return await collect_baostock_dividends(
            payload,
            client=_client(backend),
            artifacts=ArtifactWriter(tmp_path, run_id=1, attempt=1),
            lease=None,
            now=lambda: CAPTURED_AT,
        )

    report = await run()
    assert report.status == "success" and report.completeness.ratio == 1.0
    assert backend.logins == 1
    events = await _events()
    by_listing: dict[int, list[date]] = {}
    for row in events:
        by_listing.setdefault(row.listing_id, []).append(row.ex_date)
    assert by_listing[ids["601398.SH"]] == [
        date(2025, 1, 7), date(2025, 7, 14), date(2025, 12, 15), date(2026, 5, 13)
    ]
    assert ids["000002.SZ"] not in by_listing
    icbc = events[0]
    assert icbc.version == 1 and icbc.is_backfill is False and icbc.provider == "baostock"
    assert icbc.available_from == date(2024, 8, 31) and icbc.operate_year == 2025
    assert icbc.raw_ref is not None and (tmp_path / icbc.raw_ref).is_file()
    coverage = {(row.listing_id, row.year): row.event_count for row in await _coverage()}
    assert coverage[(ids["000002.SZ"], 2025)] == 0 and coverage[(ids["000002.SZ"], 2026)] == 0
    assert coverage[(ids["601398.SH"], 2025)] == 3
    assert report.metadata["inserted"] == 8 and report.metadata["duplicate_rows_merged"] == 1

    again = await run()
    assert again.metadata["inserted"] == 0 and again.metadata["unchanged"] == 8
    assert len(await _events()) == 8
    assert len(await _coverage()) == 6


async def test_changed_recapture_is_a_new_version(db_session):
    del db_session
    async with session_scope() as session:
        listing = await ListingRepository(session).ensure_a_share_listing("600900.SH")
        repository = DividendRepository(session)
        common = {
            "listing_id": listing.id,
            "ex_date": date(2026, 7, 17),
            "available_from": date(2026, 4, 30),
            "operate_year": 2026,
            "provider": "baostock",
            "captured_at": CAPTURED_AT,
            "details": {},
        }
        first = await repository.upsert_event(
            **common, payload={"cash_per_share_pre_tax": Decimal("0.79")}
        )
        same = await repository.upsert_event(
            **common, payload={"cash_per_share_pre_tax": Decimal("0.790000")}
        )
        changed = await repository.upsert_event(
            **common, payload={"cash_per_share_pre_tax": Decimal("0.80")}
        )
    assert first is not None and first.version == 1
    assert same is None
    assert changed is not None and changed.version == 2


async def test_backfill_part_covers_the_window_plus_one_year_and_skips_covered(
    db_session, tmp_path
):
    del db_session
    ids = await seed_universe(["600941.SH", "600519.SH"])
    payload = SnapshotBackfillPayload(
        universe_code="CSI300",
        as_of=date(2026, 10, 6),
        part="dividends",
        symbols=("600519.SH", "600941.SH"),
        chunk=0,
    )
    assert dividend_years(payload) == [2020, 2021, 2022, 2023, 2024, 2025, 2026]
    async with session_scope() as session:
        await DividendRepository(session).record_coverage(
            listing_id=ids["600519.SH"],
            provider="baostock",
            year=2020,
            checked_at=CAPTURED_AT,
            event_count=0,
        )
    backend = FakeBaostockData()
    report = await backfill_dividends(
        payload,
        client=_client(backend),  # type: ignore[arg-type]
        artifacts=ArtifactWriter(tmp_path, run_id=3, attempt=1),
        lease=None,
    )
    assert report.status == "success"
    assert len(backend.methods("query_dividend_data")) == 13
    assert report.metadata["skipped_existing"] == 1
    assert report.metadata["collected_symbols"] == ["600519.SH", "600941.SH"]
    rows = await _events()
    assert rows and all(row.is_backfill for row in rows)
    estimate = estimate_backfill(
        payload.model_copy(update={"part": "membership", "symbols": (), "chunk": None}),
        symbols=300,
        current=300,
        monthly_queries=60,
        differing_months=0,
        source="test",
    )
    assert {part.part: part.queries for part in estimate.parts}["dividends"] == 300 * 7


async def test_worker_runs_the_dividend_job_without_ifind(db_session, tmp_path):
    del db_session
    from investment_agent.research.worker import ResearchWorker
    from investment_agent.snapshots.jobs import enqueue_snapshot_job
    from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
    from tests.unit._snapshot_support import FakeProxy, handlers_for, settings_for

    await seed_universe(["600941.SH", "000002.SZ"])
    settings = settings_for(tmp_path)
    proxy = FakeProxy()
    async with session_scope() as session:
        run, _ = await enqueue_snapshot_job(
            session,
            DividendSnapshotPayload(universe_code="CSI300", as_of=date(2026, 10, 6)),
            triggered_by="test",
        )
        run_id = run.id
    worker = ResearchWorker(
        settings=settings,
        worker_id="w",
        handlers=handlers_for(settings, proxy, baostock=_client(FakeBaostockData())),
    )
    assert await worker.run_once() == run_id
    assert proxy.calls == []
    async with session_scope() as session:
        finished = await PipelineRunRepository(session).get(run_id)
    assert finished is not None and finished.status == "success"
    assert finished.metadata_json["completeness"]["collected"] == 2
    assert finished.metadata_json["events"] == 4
    assert list((tmp_path / "notifications").rglob("*.md")) == []


@pytest.mark.network
async def test_live_baostock_dividend_smoke() -> None:
    client = BaostockMarketClient()
    async with client.session():
        icbc = await client.fetch_dividends("601398.SH", 2025)
        mindray = await client.fetch_dividends("300760.SZ", 2025)
    assert [str(e.cash_per_share) for e in icbc.events] == ["0.1434", "0.1646", "0.1414"]
    assert icbc.duplicates == ("2025-01-07",)
    assert mindray.combined == ("2025-05-29",)
