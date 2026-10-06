"""History rebuild for the backfill (P1-A4): universe membership and industry
intervals merged with live rows, idempotency, and backfilled market rows in
point-in-time reads."""

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
from investment_agent.storage.models.pit_snapshot import IndustryClassification
from investment_agent.storage.models.reference import Company, Listing, UniverseMembership
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.intervals import (
    Span,
    merge_timeline,
    plan_rows,
    scalar_spans,
    set_spans,
    state_at,
)
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.universe import UniverseRepository

LIVE = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)
BACKFILL = datetime(2026, 10, 6, 10, 0, tzinfo=UTC)
WINDOW = date(2021, 10, 6)
A, B, C, D = "600519.SH", "000001.SZ", "300750.SZ", "601398.SH"


def _snap(effective: date, members: list[str], *, at: datetime = BACKFILL) -> ConstituentSnapshot:
    return ConstituentSnapshot(
        index_code="CSI300",
        as_of=effective,
        effective_date=effective,
        members=tuple(sorted(members)),
        member_names={ticker: f"name-{ticker}" for ticker in members},
        captured_at=at,
        raw_rows=(),
    )


async def _membership(session) -> dict[str, list[tuple[date, date | None, str | None, datetime]]]:
    stmt = (
        select(UniverseMembership, Listing)
        .join(Listing, Listing.id == UniverseMembership.listing_id)
        .order_by(Listing.ticker, UniverseMembership.valid_from)
    )
    result: dict[str, list[tuple[date, date | None, str | None, datetime]]] = {}
    for row, listing in (await session.execute(stmt)).all():
        suffix = "SH" if listing.market_code == "SSE" else "SZ"
        result.setdefault(f"{listing.ticker}.{suffix}", []).append(
            (row.valid_from, row.valid_to, row.source, row.captured_at)
        )
    return result


def _bounds(rows):
    return [(start, end) for start, end, *_ in rows]


# --- pure interval helpers --------------------------------------------------------


def test_merge_timeline_keeps_stored_changes_outside_the_window() -> None:
    stored = [Span("x", date(2020, 1, 1), date(2022, 1, 1)), Span("y", date(2026, 1, 1), None)]
    timeline = merge_timeline(
        lambda day: state_at(stored, day),
        [date(2020, 1, 1), date(2022, 1, 1), date(2026, 1, 1)],
        [(date(2021, 6, 1), "z"), (date(2024, 1, 1), "z")],
    )
    assert timeline == [
        (date(2020, 1, 1), "x"),
        (date(2021, 6, 1), "z"),
        (date(2026, 1, 1), "y"),
    ]
    assert scalar_spans(timeline) == [
        Span("x", date(2020, 1, 1), date(2021, 6, 1)),
        Span("z", date(2021, 6, 1), date(2026, 1, 1)),
        Span("y", date(2026, 1, 1), None),
    ]
    with pytest.raises(ValueError):
        merge_timeline(lambda _d: None, [], [(date(2024, 1, 1), "a"), (date(2023, 1, 1), "b")])


def test_set_spans_and_row_plan() -> None:
    spans = set_spans(
        [
            (date(2021, 1, 4), frozenset({1, 2})),
            (date(2021, 6, 14), frozenset({1})),
            (date(2021, 12, 13), frozenset({1, 2})),
        ]
    )
    assert spans[2] == [
        Span(True, date(2021, 1, 4), date(2021, 6, 14)),
        Span(True, date(2021, 12, 13), None),
    ]
    rows = [Span(True, date(2021, 12, 13), None), Span(True, date(2026, 9, 28), None)]
    plan = plan_rows(rows, spans[2], lambda row: row)
    assert plan.keep == [Span(True, date(2021, 12, 13), None)]
    assert plan.delete == [Span(True, date(2026, 9, 28), None)]
    assert plan.insert == [Span(True, date(2021, 1, 4), date(2021, 6, 14))]


# --- universe membership ---------------------------------------------------------


async def test_member_leaving_and_reentering(db_session) -> None:
    repo = UniverseRepository(db_session)
    result = await repo.rebuild_history(
        "CSI300",
        [
            _snap(date(2021, 10, 4), [A, B]),
            _snap(date(2021, 11, 1), [A, B]),  # unchanged week collapses
            _snap(date(2022, 6, 13), [A, C]),
            _snap(date(2022, 12, 12), [A, B]),
        ],
        window_start=WINDOW,
    )
    rows = await _membership(db_session)
    assert _bounds(rows[A]) == [(date(2021, 10, 4), None)]
    assert _bounds(rows[B]) == [
        (date(2021, 10, 4), date(2022, 6, 13)),
        (date(2022, 12, 12), None),
    ]
    assert _bounds(rows[C]) == [(date(2022, 6, 13), date(2022, 12, 12))]
    assert result.change_dates == (date(2022, 6, 13), date(2022, 12, 12))
    assert result.listings == tuple(sorted([A, B, C]))
    assert result.inserted == 4
    # Names come from the snapshots.
    listing = await ListingRepository(db_session).get_a_share_listing(C)
    assert listing is not None
    company = await db_session.get(Company, listing.company_id)
    assert company is not None and company.canonical_name == f"name-{C}"

    members = await repo.members_as_of("CSI300", date(2022, 7, 1))
    assert [m.ticker for m in members] == sorted([A, C])
    between = await repo.members_between("CSI300", WINDOW, date(2026, 10, 6))
    assert sorted({m.ticker for m in between}) == sorted([A, B, C])


async def test_merge_with_existing_live_interval_and_idempotency(db_session) -> None:
    repo = UniverseRepository(db_session)
    # Live sync first (the production DB state): full snapshot effective 2026-09-28.
    await repo.apply_constituent_snapshot("CSI300", _snap(date(2026, 9, 28), [A, B, D], at=LIVE))
    before = await _membership(db_session)

    snapshots = [
        _snap(date(2021, 10, 4), [A, B]),
        _snap(date(2026, 6, 15), [A, C]),
    ]
    first = await repo.rebuild_history("CSI300", snapshots, window_start=WINDOW)
    rows = await _membership(db_session)
    # A: the live open row is extended backwards and keeps its provenance.
    assert rows[A] == [(date(2021, 10, 4), None, "baostock", before[A][0][3])]
    assert rows[A][0][3].replace(tzinfo=UTC) == LIVE
    # B: left in 2026-06, re-entered with the live sync (live row untouched).
    assert _bounds(rows[B]) == [(date(2021, 10, 4), date(2026, 6, 15)), (date(2026, 9, 28), None)]
    assert rows[B][1] == before[B][0]
    # C: only between the backfill's last snapshot and the live sync.
    assert _bounds(rows[C]) == [(date(2026, 6, 15), date(2026, 9, 28))]
    assert rows[C][0][3].replace(tzinfo=UTC) == BACKFILL
    assert rows[D] == before[D]
    assert first.reused == 1 and first.changed

    again = await repo.rebuild_history("CSI300", snapshots, window_start=WINDOW)
    assert not again.changed
    assert await _membership(db_session) == rows

    # Live syncs keep working after the rebuild.
    diff = await repo.apply_constituent_snapshot("CSI300", _snap(date(2026, 10, 5), [A, D]))
    assert diff.removed == (B,)


async def test_rebuild_covering_the_live_date(db_session) -> None:
    repo = UniverseRepository(db_session)
    await repo.apply_constituent_snapshot("CSI300", _snap(date(2026, 9, 28), [A, B], at=LIVE))
    await repo.rebuild_history(
        "CSI300",
        [_snap(date(2021, 10, 4), [A, C]), _snap(date(2026, 9, 28), [A, B]),
         _snap(date(2026, 10, 5), [A, B])],
        window_start=WINDOW,
    )
    rows = await _membership(db_session)
    assert _bounds(rows[A]) == [(date(2021, 10, 4), None)]
    assert rows[B] == [(date(2026, 9, 28), None, "baostock", rows[B][0][3])]
    assert rows[B][0][3].replace(tzinfo=UTC) == LIVE  # untouched live row
    assert _bounds(rows[C]) == [(date(2021, 10, 4), date(2026, 9, 28))]


async def test_rebuild_rejects_bad_input(db_session) -> None:
    repo = UniverseRepository(db_session)
    with pytest.raises(ValueError, match="after the window start"):
        await repo.rebuild_history("CSI300", [_snap(date(2022, 1, 3), [A])], window_start=WINDOW)
    with pytest.raises(ValueError, match="ordered"):
        await repo.rebuild_history(
            "CSI300",
            [_snap(date(2022, 1, 3), [A]), _snap(date(2021, 1, 4), [A])],
            window_start=WINDOW,
        )
    with pytest.raises(ValueError, match="disagree"):
        await repo.rebuild_history(
            "CSI300",
            [_snap(date(2021, 1, 4), [A]), _snap(date(2021, 1, 4), [B])],
            window_start=WINDOW,
        )


# --- industry ----------------------------------------------------------------------


def _industry(as_of: date, entries: dict[str, tuple[date, str | None, str | None]]) -> IndustrySnapshot:
    built = tuple(
        IndustryEntry(
            ticker=ticker,
            company_name=f"name-{ticker}",
            industry_code=code,
            industry_name=name,
            effective_date=effective,
            raw_industry=f"{code or ''}{name or ''}",
            classification_label="证监会行业分类",
        )
        for ticker, (effective, code, name) in sorted(entries.items())
    )
    return IndustrySnapshot(
        as_of=as_of,
        effective_date=max((e.effective_date for e in built), default=None),
        entries=built,
        captured_at=BACKFILL,
        raw_rows=(),
    )


async def _industry_rows(session) -> dict[str, list[tuple[date, date | None, str | None, datetime]]]:
    stmt = (
        select(IndustryClassification, Listing)
        .join(Listing, Listing.id == IndustryClassification.listing_id)
        .order_by(Listing.ticker, IndustryClassification.valid_from)
    )
    result: dict[str, list[tuple[date, date | None, str | None, datetime]]] = {}
    for row, listing in (await session.execute(stmt)).all():
        result.setdefault(listing.ticker, []).append(
            (row.valid_from, row.valid_to, row.code, row.captured_at)
        )
    return result


async def test_industry_history_rebuild(db_session) -> None:
    repo = IndustryRepository(db_session)
    live = _industry(date(2026, 9, 30), {A: (date(2026, 9, 28), "C15", "酒")})
    live = IndustrySnapshot(
        as_of=live.as_of, effective_date=live.effective_date, entries=live.entries,
        captured_at=LIVE, raw_rows=(),
    )
    await repo.apply_industry_snapshot(live)
    snapshots = [
        _industry(WINDOW, {A: (date(2021, 10, 4), "C14", "食品"), D: (date(2021, 10, 4), "J66", "银行")}),
        _industry(
            date(2022, 1, 1),
            {
                A: (date(2021, 11, 15), "C15", "酒"),
                B: (date(2021, 11, 15), "J66", "银行"),
                D: (date(2021, 11, 15), "J66", "银行"),
            },
        ),
        _industry(
            date(2026, 10, 6),
            {A: (date(2026, 9, 28), "C15", "酒"), B: (date(2026, 9, 28), "J66", "银行")},
        ),
    ]
    result = await repo.rebuild_history(snapshots, tickers=[A, B])
    rows = await _industry_rows(db_session)
    assert [(s, e, c) for s, e, c, _ in rows["600519"]] == [
        (date(2021, 10, 4), date(2021, 11, 15), "C14"),
        (date(2021, 11, 15), None, "C15"),
    ]
    # The live row was extended backwards; its provenance is kept.
    assert rows["600519"][1][3].replace(tzinfo=UTC) == LIVE
    # B was not listed at the window start: classified from its first appearance.
    assert [(s, e, c) for s, e, c, _ in rows["000001"]] == [(date(2021, 11, 15), None, "J66")]
    # D is not in the backfill set: no listing, no rows.
    assert "601398" not in rows
    assert await ListingRepository(db_session).get_a_share_listing(D) is None
    assert result.reclassified == (A,)

    again = await repo.rebuild_history(snapshots, tickers=[A, B])
    assert (again.inserted, again.reused, again.deleted) == (0, 0, 0)
    assert await _industry_rows(db_session) == rows
    # A live sync later still applies.
    await repo.apply_industry_snapshot(
        _industry(date(2026, 11, 2), {A: (date(2026, 11, 2), "C14", "食品")})
    )
    current = await repo.industry_as_of(rows_listing_id := (await ListingRepository(
        db_session).get_a_share_listing(A)).id, date(2026, 11, 3))  # type: ignore[union-attr]
    assert current is not None and current.code == "C14"
    del rows_listing_id


# --- market snapshot backfill flag -----------------------------------------------


async def test_series_as_of_include_backfill(db_session) -> None:
    listing = await ListingRepository(db_session).ensure_a_share_listing(A)
    repo = MarketSnapshotRepository(db_session)
    bar = {name: Decimal(1) for name in ("open", "high", "low", "close", "volume")}
    await repo.insert(
        listing_id=listing.id, trade_date=date(2021, 10, 8), provider="baostock",
        captured_at=BACKFILL, is_backfill=True, **bar,
    )
    await repo.insert(
        listing_id=listing.id, trade_date=date(2026, 9, 30), provider="baostock",
        captured_at=BACKFILL, **bar,
    )
    then = datetime(2021, 10, 8, 16, 0, tzinfo=UTC)
    historical = await repo.series_as_of(listing.id, date(2021, 1, 1), date(2026, 12, 31), then)
    assert [(r.trade_date, r.is_backfill) for r in historical] == [(date(2021, 10, 8), True)]
    strict = await repo.series_as_of(
        listing.id, date(2021, 1, 1), date(2026, 12, 31), then, include_backfill=False
    )
    assert strict == []
    now = await repo.series_as_of(
        listing.id, date(2021, 1, 1), date(2026, 12, 31), BACKFILL, include_backfill=False
    )
    assert [r.trade_date for r in now] == [date(2021, 10, 8), date(2026, 9, 30)]
    earliest = await repo.earliest_trade_dates([listing.id], adjust="none", provider="baostock")
    assert earliest == {listing.id: date(2021, 10, 8)}
    live_only = await repo.earliest_trade_dates(
        [listing.id], adjust="none", provider="ifind"
    )
    assert live_only == {}
