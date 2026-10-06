"""Shenwan 2021 history -> industry_classification (P1-A9)."""

from __future__ import annotations

from datetime import UTC, date, datetime

from investment_agent.providers.akshare_shenwan import ShenwanChange
from investment_agent.providers.baostock_reference import IndustryEntry, IndustrySnapshot
from investment_agent.providers.shenwan_directory import (
    ShenwanIndustry,
    build_directory,
    default_shenwan_directory,
)
from investment_agent.snapshots.shenwan import (
    apply_sw2021_history,
    existing_listing_ids,
    sw2021_points,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.listing import ListingRepository
from tests.unit._shenwan_fake import fake_shenwan_client

AS_OF = date(2026, 10, 6)


async def _history():
    return await fake_shenwan_client().fetch_industry_history()


async def _listings(*tickers: str) -> dict[str, int]:
    async with session_scope() as session:
        listings = ListingRepository(session)
        return {
            ticker: (await listings.ensure_a_share_listing(ticker)).id for ticker in tickers
        }


async def _history_rows(listing_id: int, system: str = "sw2021"):
    async with session_scope() as session:
        rows = await IndustryRepository(session).history(listing_id, system=system)
        return [(row.code, row.name, row.valid_from, row.valid_to) for row in rows]


def test_points_name_2021_codes_and_keep_legacy_codes():
    directory = build_directory(
        [
            ShenwanIndustry("340000", "食品饮料", 1),
            ShenwanIndustry("340500", "白酒Ⅱ", 2, "340000"),
            ShenwanIndustry("340501", "白酒Ⅲ", 3, "340500"),
            ShenwanIndustry("340300", "饮料乳品", 2, "340000"),
            ShenwanIndustry("340301", "软饮料", 3, "340300"),
        ],
        version="t",
    )
    changes = (
        ShenwanChange(date(2001, 7, 31), "340301"),
        ShenwanChange(date(2021, 7, 30), "340501"),
    )
    points, unresolved, legacy = sw2021_points(changes, directory=directory, as_of=AS_OF)
    # 340301 ended on the 2021 cutover: an SW2014 code, never named via the 2021 table
    assert points == [
        (date(2001, 7, 31), ("340301", "340301")),
        (date(2021, 7, 30), ("340501", "白酒Ⅲ")),
        (AS_OF, ("340501", "白酒Ⅲ")),
    ]
    assert unresolved == set() and legacy == 1

    early, _, _ = sw2021_points(changes, directory=directory, as_of=date(2021, 1, 1))
    assert early == [
        (date(2001, 7, 31), ("340301", "340301")),
        (date(2021, 1, 1), ("340301", "340301")),
    ]


async def test_moutai_pit_industry_differs_around_2021_07_30(db_session):
    ids = await _listings("600519.SH")
    history = await _history()
    meta = await apply_sw2021_history(history, scope=ids, as_of=AS_OF, lease=None)

    rows = await _history_rows(ids["600519.SH"])
    assert rows == [
        ("340301", "340301", date(2001, 7, 31), date(2021, 7, 30)),
        ("340501", "白酒Ⅲ", date(2021, 7, 30), None),
    ]
    async with session_scope() as session:
        industries = IndustryRepository(session)
        before = await industries.industry_as_of(ids["600519.SH"], date(2021, 7, 29), system="sw2021")
        after = await industries.industry_as_of(ids["600519.SH"], date(2021, 7, 30), system="sw2021")
        assert before is not None and before.code == "340301"
        assert after is not None and (after.code, after.name) == ("340501", "白酒Ⅲ")
        assert after.source == "akshare"
        assert await industries.industry_as_of(ids["600519.SH"], date(2000, 1, 1), system="sw2021") is None
    assert meta["applied"] == 1 and meta["legacy_intervals"] == 1
    assert meta["unresolved_codes"] == {}
    assert meta["directory_version"] == default_shenwan_directory().version


async def test_multiple_changes_become_contiguous_intervals(db_session):
    ids = await _listings("000004.SZ")
    history = await _history()
    changes = history.changes["000004.SZ"]
    assert len(changes) >= 5
    await apply_sw2021_history(history, scope=ids, as_of=AS_OF, lease=None)
    rows = await _history_rows(ids["000004.SZ"])
    # consecutive equal codes would merge; otherwise one interval per change
    assert [row[2] for row in rows] == [change.start_date for change in changes]
    for current, following in zip(rows, rows[1:], strict=False):
        assert current[3] == following[2]  # valid_to = next start_date: no gaps, no overlaps
    assert rows[-1][3] is None
    assert rows[-1][0] == changes[-1].code
    assert rows[-1][1] == default_shenwan_directory().name_of(changes[-1].code)


async def test_rebuild_is_idempotent_and_coexists_with_csrc(db_session):
    ids = await _listings("600519.SH")
    async with session_scope() as session:
        await IndustryRepository(session).apply_industry_snapshot(
            IndustrySnapshot(
                as_of=AS_OF,
                effective_date=AS_OF,
                entries=(
                    IndustryEntry(
                        ticker="600519.SH",
                        company_name="贵州茅台",
                        industry_code="C15",
                        industry_name="酒、饮料和精制茶制造业",
                        effective_date=date(2026, 9, 28),
                        raw_industry="C15酒、饮料和精制茶制造业",
                        classification_label="证监会行业分类",
                    ),
                ),
                captured_at=datetime(2026, 10, 6, tzinfo=UTC),
                raw_rows=(),
            )
        )
    history = await _history()
    first = await apply_sw2021_history(history, scope=ids, as_of=AS_OF, lease=None)
    again = await apply_sw2021_history(history, scope=ids, as_of=AS_OF, lease=None)
    assert first["intervals"]["inserted"] == 2
    assert again["intervals"] == {"kept": 2, "reused": 0, "deleted": 0, "inserted": 0}
    csrc = await _history_rows(ids["600519.SH"], system="csrc")
    assert csrc == [("C15", "酒、饮料和精制茶制造业", date(2026, 9, 28), None)]


async def test_missing_directory_entry_keeps_the_code_and_reports_it(db_session):
    ids = await _listings("600519.SH")
    history = await _history()
    sparse = build_directory([ShenwanIndustry("110000", "农林牧渔", 1)], version="sparse")
    meta = await apply_sw2021_history(
        history, scope=ids, as_of=AS_OF, lease=None, directory=sparse
    )
    rows = await _history_rows(ids["600519.SH"])
    assert rows[-1][:2] == ("340501", "340501")
    assert meta["unresolved_codes"] == {"340501": ["600519.SH"]}


async def test_as_of_truncates_later_changes(db_session):
    ids = await _listings("600519.SH")
    history = await _history()
    await apply_sw2021_history(history, scope=ids, as_of=date(2021, 1, 4), lease=None)
    assert await _history_rows(ids["600519.SH"]) == [
        ("340301", "340301", date(2001, 7, 31), None)
    ]
    # a later full sync extends the history without duplicates
    await apply_sw2021_history(history, scope=ids, as_of=AS_OF, lease=None)
    assert [row[0] for row in await _history_rows(ids["600519.SH"])] == ["340301", "340501"]


async def test_only_existing_listings_are_touched(db_session):
    await _listings("600519.SH")
    async with session_scope() as session:
        scope = await existing_listing_ids(session)
        assert set(scope) == {"600519.SH"}
        assert await existing_listing_ids(session, {"600519.SH", "000001.SZ"}) == {
            "600519.SH": scope["600519.SH"]
        }
    history = await _history()
    meta = await apply_sw2021_history(history, scope=scope, as_of=AS_OF, lease=None)
    assert meta["provider_tickers"] > 300 and meta["applied"] == 1
    async with session_scope() as session:
        assert await ListingRepository(session).get_a_share_listing("000001.SZ") is None


async def test_scope_ticker_absent_from_history_is_reported(db_session):
    ids = await _listings("600519.SH", "603999.SH")  # 603999 is outside the fixture
    meta = await apply_sw2021_history(await _history(), scope=ids, as_of=AS_OF, lease=None)
    assert meta["not_in_history"] == ["603999.SH"]
    assert await _history_rows(ids["603999.SH"]) == []
