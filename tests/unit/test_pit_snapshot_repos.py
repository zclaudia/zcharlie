"""Point-in-time snapshot repositories (P1-A2)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.providers.baostock_reference import (
    ConstituentSnapshot,
    IndustryEntry,
    IndustrySnapshot,
)
from investment_agent.storage.models.reference import Listing, Universe, UniverseMembership
from investment_agent.storage.repositories.financial_snapshot import (
    FinancialSnapshotRepository,
)
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.pit import OutOfOrderSnapshotError
from investment_agent.storage.repositories.universe import UniverseRepository

CAPTURED = datetime(2026, 10, 6, 2, 0, tzinfo=UTC)


def _constituents(
    effective: date | None, members: list[str], *, captured_at: datetime = CAPTURED
) -> ConstituentSnapshot:
    return ConstituentSnapshot(
        index_code="CSI300",
        as_of=effective or date(2004, 1, 2),
        effective_date=effective,
        members=tuple(sorted(members)),
        member_names={ticker: f"name-{ticker}" for ticker in members},
        captured_at=captured_at,
        raw_rows=(),
    )


def _industry(
    ticker: str, code: str | None, name: str | None, effective: date
) -> IndustrySnapshot:
    entry = IndustryEntry(
        ticker=ticker,
        company_name=f"name-{ticker}",
        industry_code=code,
        industry_name=name,
        effective_date=effective,
        raw_industry=f"{code or ''}{name or ''}",
        classification_label="证监会行业分类",
    )
    return IndustrySnapshot(
        as_of=effective,
        effective_date=effective,
        entries=(entry,),
        captured_at=CAPTURED,
        raw_rows=(),
    )


async def _tickers(repo: UniverseRepository, as_of: date) -> list[str]:
    return [member.ticker for member in await repo.members_as_of("CSI300", as_of)]


# --------------------------------------------------------------------------- #
# Listings
# --------------------------------------------------------------------------- #


async def test_ensure_a_share_listing_creates_market_and_reuses(db_session) -> None:
    repo = ListingRepository(db_session)
    first = await repo.ensure_a_share_listing("600519.SH", company_name="贵州茅台")
    again = await repo.ensure_a_share_listing("600519.SH", company_name="ignored")
    assert first.id == again.id
    assert first.market_code == "SSE"
    assert first.ticker == "600519"
    assert (await repo.get_a_share_listing("600519.SH")) is not None
    assert (await repo.get_a_share_listing("000001.SZ")) is None
    with pytest.raises(ValueError):
        await repo.ensure_a_share_listing("AAPL")


# --------------------------------------------------------------------------- #
# Universe membership
# --------------------------------------------------------------------------- #


async def test_first_snapshot_opens_all_members_and_seeds_universe(db_session) -> None:
    repo = UniverseRepository(db_session)
    diff = await repo.apply_constituent_snapshot(
        "CSI300", _constituents(date(2026, 6, 15), ["600519.SH", "000001.SZ"])
    )
    assert diff.added == ("000001.SZ", "600519.SH")
    assert diff.removed == ()
    universe = await db_session.get(Universe, "CSI300")
    assert universe is not None and universe.name == "沪深300"
    rows = (await db_session.execute(select(UniverseMembership))).scalars().all()
    assert {row.source for row in rows} == {"baostock"}
    assert all(row.captured_at is not None for row in rows)
    assert await _tickers(repo, date(2026, 6, 15)) == ["000001.SZ", "600519.SH"]
    assert await _tickers(repo, date(2026, 6, 14)) == []


async def test_applying_same_snapshot_twice_is_noop(db_session) -> None:
    repo = UniverseRepository(db_session)
    snapshot = _constituents(date(2026, 6, 15), ["600519.SH", "000001.SZ"])
    await repo.apply_constituent_snapshot("CSI300", snapshot)
    before = (await db_session.execute(select(UniverseMembership))).scalars().all()
    diff = await repo.apply_constituent_snapshot("CSI300", snapshot)
    assert not diff.changed
    # A later weekly snapshot with identical members is also a no-op.
    later = await repo.apply_constituent_snapshot(
        "CSI300", _constituents(date(2026, 6, 22), ["600519.SH", "000001.SZ"])
    )
    assert not later.changed
    after = (await db_session.execute(select(UniverseMembership))).scalars().all()
    assert len(before) == len(after) == 2
    assert all(row.valid_to is None and row.valid_from == date(2026, 6, 15) for row in after)


async def test_member_leaves_and_reenters(db_session) -> None:
    repo = UniverseRepository(db_session)
    await repo.apply_constituent_snapshot(
        "CSI300", _constituents(date(2025, 1, 6), ["600519.SH", "000001.SZ"])
    )
    left = await repo.apply_constituent_snapshot(
        "CSI300", _constituents(date(2025, 6, 16), ["600519.SH", "601398.SH"])
    )
    assert left.added == ("601398.SH",)
    assert left.removed == ("000001.SZ",)
    # Idempotent re-application of the leaving snapshot.
    assert not (
        await repo.apply_constituent_snapshot(
            "CSI300", _constituents(date(2025, 6, 16), ["600519.SH", "601398.SH"])
        )
    ).changed
    back = await repo.apply_constituent_snapshot(
        "CSI300", _constituents(date(2025, 12, 15), ["600519.SH", "601398.SH", "000001.SZ"])
    )
    assert back.added == ("000001.SZ",)
    assert back.removed == ()

    assert await _tickers(repo, date(2025, 6, 15)) == ["000001.SZ", "600519.SH"]
    assert await _tickers(repo, date(2025, 6, 16)) == ["600519.SH", "601398.SH"]
    assert await _tickers(repo, date(2025, 12, 14)) == ["600519.SH", "601398.SH"]
    assert await _tickers(repo, date(2025, 12, 15)) == ["000001.SZ", "600519.SH", "601398.SH"]

    listing = await ListingRepository(db_session).get_a_share_listing("000001.SZ")
    assert listing is not None
    intervals = (
        (
            await db_session.execute(
                select(UniverseMembership)
                .where(UniverseMembership.listing_id == listing.id)
                .order_by(UniverseMembership.valid_from)
            )
        )
        .scalars()
        .all()
    )
    assert [(row.valid_from, row.valid_to) for row in intervals] == [
        (date(2025, 1, 6), date(2025, 6, 16)),
        (date(2025, 12, 15), None),
    ]


async def test_empty_snapshot_is_noop(db_session) -> None:
    repo = UniverseRepository(db_session)
    await repo.apply_constituent_snapshot("CSI300", _constituents(date(2025, 1, 6), ["600519.SH"]))
    diff = await repo.apply_constituent_snapshot("CSI300", _constituents(None, []))
    assert not diff.changed
    assert await _tickers(repo, date(2026, 1, 1)) == ["600519.SH"]


async def test_out_of_order_change_is_rejected(db_session) -> None:
    repo = UniverseRepository(db_session)
    await repo.apply_constituent_snapshot("CSI300", _constituents(date(2025, 6, 16), ["600519.SH"]))
    with pytest.raises(OutOfOrderSnapshotError):
        await repo.apply_constituent_snapshot(
            "CSI300", _constituents(date(2025, 1, 6), ["000001.SZ"])
        )


async def test_same_day_correction_replaces_without_empty_interval(db_session) -> None:
    repo = UniverseRepository(db_session)
    await repo.apply_constituent_snapshot("CSI300", _constituents(date(2025, 1, 6), ["600519.SH"]))
    await repo.apply_constituent_snapshot(
        "CSI300", _constituents(date(2025, 6, 16), ["600519.SH", "000001.SZ"])
    )
    corrected = await repo.apply_constituent_snapshot(
        "CSI300", _constituents(date(2025, 6, 16), ["600519.SH", "601398.SH"])
    )
    assert corrected.added == ("601398.SH",)
    assert corrected.removed == ("000001.SZ",)
    rows = (await db_session.execute(select(UniverseMembership))).scalars().all()
    assert all(row.valid_to is None or row.valid_to > row.valid_from for row in rows)
    assert await _tickers(repo, date(2025, 6, 16)) == ["600519.SH", "601398.SH"]


async def test_unknown_universe_requires_name(db_session) -> None:
    repo = UniverseRepository(db_session)
    with pytest.raises(ValueError):
        await repo.ensure_universe("CSI1000")
    created = await repo.ensure_universe("CSI1000", name="中证1000")
    assert created.name == "中证1000"


# --------------------------------------------------------------------------- #
# Industry classification
# --------------------------------------------------------------------------- #


async def test_industry_change_over_time(db_session) -> None:
    repo = IndustryRepository(db_session)
    first = await repo.apply_industry_snapshot(
        _industry("300750.SZ", "C38", "电气机械和器材制造业", date(2020, 1, 6))
    )
    assert len(first) == 1 and first[0].old_code is None and first[0].new_code == "C38"
    assert await repo.apply_industry_snapshot(
        _industry("300750.SZ", "C38", "电气机械和器材制造业", date(2020, 6, 1))
    ) == []
    changed = await repo.apply_industry_snapshot(
        _industry("300750.SZ", "C39", "计算机、通信和其他电子设备制造业", date(2023, 1, 2))
    )
    assert [(c.old_code, c.new_code) for c in changed] == [("C38", "C39")]

    listing = await ListingRepository(db_session).get_a_share_listing("300750.SZ")
    assert listing is not None
    assert await repo.industry_as_of(listing.id, date(2019, 12, 31)) is None
    old = await repo.industry_as_of(listing.id, date(2023, 1, 1))
    new = await repo.industry_as_of(listing.id, date(2023, 1, 2))
    assert old is not None and old.code == "C38"
    assert new is not None and new.code == "C39" and new.source == "baostock"
    history = await repo.history(listing.id)
    assert [(r.code, r.valid_from, r.valid_to) for r in history] == [
        ("C38", date(2020, 1, 6), date(2023, 1, 2)),
        ("C39", date(2023, 1, 2), None),
    ]


async def test_industry_cleared_and_restored(db_session) -> None:
    repo = IndustryRepository(db_session)
    await repo.apply_industry_snapshot(_industry("600001.SH", "C31", "黑色金属", date(2010, 1, 4)))
    cleared = await repo.apply_industry_snapshot(_industry("600001.SH", None, None, date(2012, 1, 2)))
    assert cleared[0].new_code is None
    listing = await ListingRepository(db_session).get_a_share_listing("600001.SH")
    assert listing is not None
    assert await repo.industry_as_of(listing.id, date(2013, 1, 1)) is None
    # Clearing again is a no-op.
    assert await repo.apply_industry_snapshot(_industry("600001.SH", None, None, date(2013, 1, 7))) == []


async def test_industry_legacy_label_without_code(db_session) -> None:
    repo = IndustryRepository(db_session)
    label = "制造业-饮料制造业-食品、饮料-酒精及饮料酒制造业"
    await repo.apply_industry_snapshot(_industry("600519.SH", None, label, date(2010, 1, 4)))
    await repo.apply_industry_snapshot(
        _industry("600519.SH", "C15", "酒、饮料和精制茶制造业", date(2013, 1, 7))
    )
    listing = await ListingRepository(db_session).get_a_share_listing("600519.SH")
    assert listing is not None
    legacy = await repo.industry_as_of(listing.id, date(2011, 1, 1))
    assert legacy is not None and legacy.code is None and legacy.name == label


async def test_industry_out_of_order_rejected(db_session) -> None:
    repo = IndustryRepository(db_session)
    await repo.apply_industry_snapshot(_industry("600519.SH", "C15", "酒", date(2020, 1, 6)))
    with pytest.raises(OutOfOrderSnapshotError):
        await repo.apply_industry_snapshot(_industry("600519.SH", "C14", "食品", date(2019, 1, 7)))


# --------------------------------------------------------------------------- #
# Market snapshots
# --------------------------------------------------------------------------- #


async def _listing_id(db_session, ticker: str = "600519.SH") -> int:
    listing: Listing = await ListingRepository(db_session).ensure_a_share_listing(ticker)
    return listing.id


def _bar(close: str) -> dict[str, Decimal]:
    value = Decimal(close)
    return {"open": value, "high": value, "low": value, "close": value, "volume": Decimal(100)}


async def test_market_snapshot_versioning_and_pit_series(db_session) -> None:
    listing_id = await _listing_id(db_session)
    repo = MarketSnapshotRepository(db_session)
    t1 = datetime(2026, 9, 30, 8, 0, tzinfo=UTC)
    t2 = datetime(2026, 10, 2, 8, 0, tzinfo=UTC)
    t3 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)

    v1 = await repo.insert(
        listing_id=listing_id, trade_date=date(2026, 9, 29), provider="ifind", captured_at=t1,
        **_bar("1500"),
    )
    v2 = await repo.insert(
        listing_id=listing_id, trade_date=date(2026, 9, 29), provider="ifind", captured_at=t3,
        raw_ref="raw/2026-10-05.json", **_bar("1510"),
    )
    await repo.insert(
        listing_id=listing_id, trade_date=date(2026, 9, 30), provider="ifind", captured_at=t2,
        **_bar("1520"),
    )
    # A backward-adjusted capture of the same day is versioned independently.
    fwd = await repo.insert(
        listing_id=listing_id, trade_date=date(2026, 9, 29), provider="ifind", captured_at=t1,
        adjust="backward", **_bar("1400"),
    )
    assert (v1.version, v2.version, fwd.version) == (1, 2, 1)

    start, end = date(2026, 9, 1), date(2026, 9, 30)
    at_t1 = await repo.series_as_of(listing_id, start, end, t1)
    assert [(r.trade_date, r.close, r.version) for r in at_t1] == [
        (date(2026, 9, 29), Decimal("1500"), 1)
    ]
    at_t2 = await repo.series_as_of(listing_id, start, end, t2)
    assert [(r.trade_date, r.close) for r in at_t2] == [
        (date(2026, 9, 29), Decimal("1500")),
        (date(2026, 9, 30), Decimal("1520")),
    ]
    at_t3 = await repo.series_as_of(listing_id, start, end, t3)
    assert [r.close for r in at_t3] == [Decimal("1510"), Decimal("1520")]
    before_any = await repo.series_as_of(
        listing_id, start, end, datetime(2026, 9, 29, tzinfo=UTC)
    )
    assert before_any == []
    forward = await repo.series_as_of(listing_id, start, end, t3, adjust="backward")
    assert [r.close for r in forward] == [Decimal("1400")]
    # Captures never exceed the knowledge time.
    for rows, cutoff in ((at_t1, t1), (at_t2, t2), (at_t3, t3)):
        assert all(r.captured_at.replace(tzinfo=UTC) <= cutoff for r in rows)
    with pytest.raises(ValueError):
        await repo.insert(
            listing_id=listing_id, trade_date=date(2026, 9, 29), provider="ifind",
            captured_at=t1, adjust="forward", **_bar("1"),
        )


async def test_market_snapshot_rejects_naive_captured_at(db_session) -> None:
    listing_id = await _listing_id(db_session)
    with pytest.raises(ValueError):
        await MarketSnapshotRepository(db_session).insert(
            listing_id=listing_id, trade_date=date(2026, 9, 29), provider="ifind",
            captured_at=datetime(2026, 9, 30, 8, 0), **_bar("1"),
        )


# --------------------------------------------------------------------------- #
# Financial snapshots
# --------------------------------------------------------------------------- #


async def test_financial_snapshot_latest_available(db_session) -> None:
    listing_id = await _listing_id(db_session)
    repo = FinancialSnapshotRepository(db_session)
    live = datetime(2026, 4, 20, 8, 0, tzinfo=UTC)

    # Backfilled 2024 annual report, disclosed 2025-04-03, captured today.
    fy2024 = await repo.insert(
        listing_id=listing_id, report_period=date(2024, 12, 31), period_kind="annual",
        fields={"revenue": 1.0}, provider="ifind", captured_at=CAPTURED,
        availability="actual", available_from=date(2025, 4, 3), is_backfill=True,
    )
    # Live capture of the 2025 annual report on 2026-04-20 (disclosed 2026-04-02).
    fy2025 = await repo.insert(
        listing_id=listing_id, report_period=date(2025, 12, 31), period_kind="annual",
        fields={"revenue": 2.0}, provider="ifind", captured_at=live,
        availability="approx", available_from=date(2026, 4, 2),
    )
    # A re-capture (correction) of 2025 on 2026-05-10.
    fy2025_v2 = await repo.insert(
        listing_id=listing_id, report_period=date(2025, 12, 31), period_kind="annual",
        fields={"revenue": 2.5}, provider="ifind",
        captured_at=datetime(2026, 5, 10, 8, 0, tzinfo=UTC),
        availability="actual", available_from=date(2026, 4, 2),
    )
    # Q1 2026 with unknown availability is never served.
    await repo.insert(
        listing_id=listing_id, report_period=date(2026, 3, 31), period_kind="q1",
        fields={"revenue": 0.7}, provider="ifind", captured_at=live, availability="unknown",
    )
    assert (fy2024.version, fy2025.version, fy2025_v2.version) == (1, 1, 2)

    assert await repo.latest_available(listing_id, date(2025, 4, 2)) is None
    hit = await repo.latest_available(listing_id, date(2025, 4, 3))
    assert hit is not None and hit.id == fy2024.id
    assert await repo.latest_available(listing_id, date(2025, 4, 3), include_backfill=False) is None

    # Disclosed but not yet captured by us on 2026-04-19 -> still FY2024.
    before_capture = await repo.latest_available(listing_id, date(2026, 4, 19))
    assert before_capture is not None and before_capture.id == fy2024.id
    on_capture = await repo.latest_available(listing_id, date(2026, 4, 20))
    assert on_capture is not None and on_capture.id == fy2025.id
    assert on_capture.fields == {"revenue": 2.0}
    after_fix = await repo.latest_available(listing_id, date(2026, 5, 10))
    assert after_fix is not None and after_fix.id == fy2025_v2.id
    latest = await repo.latest_available(listing_id, date(2026, 10, 6))
    assert latest is not None and latest.report_period == date(2025, 12, 31)


async def test_financial_snapshot_validation(db_session) -> None:
    listing_id = await _listing_id(db_session)
    repo = FinancialSnapshotRepository(db_session)
    base = {
        "listing_id": listing_id, "report_period": date(2025, 12, 31), "fields": {},
        "provider": "ifind", "captured_at": CAPTURED,
    }
    with pytest.raises(ValueError):
        await repo.insert(period_kind="annual", availability="actual", **base)  # no available_from
    with pytest.raises(ValueError):
        await repo.insert(
            period_kind="q2", availability="approx", available_from=date(2026, 4, 30), **base
        )
    with pytest.raises(ValueError):
        await repo.insert(
            period_kind="annual", availability="guess", available_from=date(2026, 4, 30), **base
        )
