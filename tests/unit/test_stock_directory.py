"""Stock directory (mrd-intake D4): pinyin initials, sync with step-back,
upsert / delisted semantics, search ordering and the health-page freshness."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta

import pytest

from investment_agent.providers.baostock_reference import DirectoryEntry, DirectorySnapshot
from investment_agent.providers.errors import ProviderDataError
from investment_agent.snapshots.directory import (
    MAX_LOOKBACK_DAYS,
    initialize_directory,
    pinyin_initials,
    sync_directory,
)
from investment_agent.snapshots.health import DIRECTORY_STALE_DAYS, build_data_health
from investment_agent.storage.repositories.stock_directory import (
    DirectoryUpsert,
    StockDirectoryRepository,
)
from tests.unit._health_support import health_settings

CAPTURED_AT = datetime(2026, 10, 9, 0, 30, tzinfo=UTC)  # 08:30 Asia/Shanghai
TODAY = date(2026, 10, 9)


class FakeDirectoryClient:
    """Answers ``query_all_stock`` per day; days absent from ``answers`` are empty."""

    def __init__(self, answers: dict[date, dict[str, tuple[str, bool]]]) -> None:
        self.answers = answers
        self.asked: list[date] = []
        self.sessions = 0

    @asynccontextmanager
    async def session(self):
        self.sessions += 1
        yield

    async def fetch_all_stocks(self, day: date) -> DirectorySnapshot:
        self.asked.append(day)
        rows = self.answers.get(day, {})
        return DirectorySnapshot(
            as_of=day,
            entries=tuple(
                DirectoryEntry(ticker=ticker, name=name, trading=trading)
                for ticker, (name, trading) in sorted(rows.items())
            ),
            captured_at=CAPTURED_AT,
            raw_rows=(),
        )


# ---- pinyin ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "initials"),
    [
        ("贵州茅台", "gzmt"),
        ("平安银行", "payh"),
        ("宁德时代", "ndsd"),
        ("*ST九鼎", "stjd"),  # the "*" is dropped, "ST" stays
        ("ST人福", "strf"),
        ("万科A", "wka"),
        ("TCL科技", "tclkj"),
        ("中国平安", "zgpa"),
        ("长江电力", "cjdl"),  # polyphone: pypinyin's first reading is fine
        ("深证成份指数(价格)", "szcfzsjg"),
        ("  ", ""),
    ],
)
def test_pinyin_initials(name: str, initials: str) -> None:
    assert pinyin_initials(name) == initials


# ---- sync --------------------------------------------------------------------


async def test_sync_steps_back_to_the_latest_trading_day_and_upserts(db_session) -> None:
    client = FakeDirectoryClient(
        {
            date(2026, 10, 8): {
                "600519.SH": ("贵州茅台", True),
                "000001.SZ": ("平安银行", True),
                "600293.SH": ("三峡新材", False),
            }
        }
    )
    result = await sync_directory(db_session, client, today=TODAY)

    assert client.asked == [date(2026, 10, 9), date(2026, 10, 8)]  # today was empty
    assert client.sessions == 1
    assert result == {
        "as_of": "2026-10-08",
        "provider": "baostock",
        "provider_rows": 3,
        "total": 3,
        "added": 3,
        "changed": 0,
        "delisted": 0,
    }
    repository = StockDirectoryRepository(db_session)
    rows = {row.ticker: row for row in await repository.search("", limit=10)}
    assert rows == {}  # blank query -> nothing
    row = await repository.get("600293.SH")
    assert row is not None
    assert (row.name, row.pinyin_initials, row.status) == ("三峡新材", "sxxc", "suspended")
    assert row.synced_on == date(2026, 10, 8)
    assert row.provider == "baostock"
    assert row.captured_at.replace(tzinfo=UTC) == CAPTURED_AT
    assert await repository.freshness() == (date(2026, 10, 8), 3)


async def test_sync_marks_vanished_tickers_delisted_and_counts_changes(db_session) -> None:
    first = FakeDirectoryClient(
        {
            date(2026, 10, 8): {
                "600519.SH": ("贵州茅台", True),
                "600293.SH": ("三峡新材", False),
                "000001.SZ": ("平安银行", True),
            }
        }
    )
    await sync_directory(db_session, first, today=date(2026, 10, 8))
    second = FakeDirectoryClient(
        {
            date(2026, 10, 15): {
                "600519.SH": ("贵州茅台", True),
                "600293.SH": ("ST三峡", True),  # renamed and trading again
                "300750.SZ": ("宁德时代", True),  # new
            }
        }
    )
    result = await sync_directory(db_session, second, today=date(2026, 10, 15))
    assert (result["total"], result["added"], result["changed"], result["delisted"]) == (4, 1, 1, 1)

    repository = StockDirectoryRepository(db_session)
    gone = await repository.get("000001.SZ")
    assert gone is not None
    assert gone.status == "delisted" and gone.name == "平安银行"  # kept, never deleted
    assert gone.synced_on == date(2026, 10, 15)
    renamed = await repository.get("600293.SH")
    assert renamed is not None
    assert (renamed.name, renamed.pinyin_initials, renamed.status) == ("ST三峡", "stsx", "listed")
    assert await repository.freshness() == (date(2026, 10, 15), 4)

    # A third identical answer changes nothing but the sync day.
    again = await sync_directory(db_session, second, today=date(2026, 10, 15))
    assert (again["added"], again["changed"], again["delisted"]) == (0, 0, 0)


async def test_sync_gives_up_after_the_lookback_window(db_session) -> None:
    client = FakeDirectoryClient({})
    with pytest.raises(ProviderDataError, match="answered nothing"):
        await sync_directory(db_session, client, today=TODAY)
    assert len(client.asked) == MAX_LOOKBACK_DAYS + 1
    assert await StockDirectoryRepository(db_session).freshness() == (None, 0)


async def test_initialize_empty_directory_once(db_session, monkeypatch) -> None:
    from investment_agent.snapshots import directory

    @asynccontextmanager
    async def scope():
        yield db_session

    monkeypatch.setattr(directory, "session_scope", scope)
    monkeypatch.setattr(directory, "shanghai_today", lambda: TODAY)
    client = FakeDirectoryClient({TODAY: {"600547.SH": ("山东黄金", True)}})
    await initialize_directory(client)
    assert await StockDirectoryRepository(db_session).freshness() == (TODAY, 1)
    assert client.asked == [TODAY]
    await initialize_directory(client)
    assert client.asked == [TODAY]  # Subsequent startup never fetches again.


async def test_initialize_failure_leaves_empty_directory_and_logs(db_session, monkeypatch) -> None:
    from structlog.testing import capture_logs

    from investment_agent.snapshots import directory

    @asynccontextmanager
    async def scope():
        yield db_session

    monkeypatch.setattr(directory, "session_scope", scope)
    monkeypatch.setattr(directory, "shanghai_today", lambda: TODAY)
    with capture_logs() as logs:
        await initialize_directory(FakeDirectoryClient({}))
    assert await StockDirectoryRepository(db_session).freshness() == (None, 0)
    assert any(entry["event"] == "directory_initialization_failed" for entry in logs)


# ---- search ------------------------------------------------------------------


async def _seed(session, rows: list[tuple[str, str, str]], *, synced_on: date = TODAY) -> None:
    await StockDirectoryRepository(session).upsert_all(
        [
            DirectoryUpsert(ticker=t, name=n, pinyin_initials=pinyin_initials(n), status=s)
            for t, n, s in rows
        ],
        synced_on=synced_on,
        captured_at=CAPTURED_AT,
        provider="baostock",
    )


SEED = [
    ("600519.SH", "贵州茅台", "listed"),
    ("600518.SH", "ST康美", "delisted"),
    ("600516.SH", "方大炭素", "listed"),
    ("000001.SZ", "平安银行", "listed"),
    ("601318.SH", "中国平安", "listed"),
    ("000002.SZ", "万科A", "suspended"),
    ("300750.SZ", "宁德时代", "listed"),
    ("002594.SZ", "比亚迪", "listed"),
]


async def test_search_code_prefix_then_name_then_pinyin(db_session) -> None:
    await _seed(db_session, SEED)
    repository = StockDirectoryRepository(db_session)

    code = await repository.search("6005", limit=10)
    assert [r.ticker for r in code] == ["600516.SH", "600519.SH", "600518.SH"]  # delisted last
    assert [r.ticker for r in await repository.search("600519.s", limit=10)] == ["600519.SH"]
    assert [r.ticker for r in await repository.search("000", limit=10)] == ["000001.SZ", "000002.SZ"]

    name = await repository.search("平安", limit=10)
    assert [r.ticker for r in name] == ["000001.SZ", "601318.SH"]

    pinyin = await repository.search("gzmt", limit=10)
    assert [r.ticker for r in pinyin] == ["600519.SH"]
    assert [r.ticker for r in await repository.search("GZ", limit=10)] == ["600519.SH"]  # case
    assert [r.ticker for r in await repository.search("pa", limit=10)] == ["000001.SZ"]  # prefix only
    assert [r.ticker for r in await repository.search("wka", limit=10)] == ["000002.SZ"]

    # A Latin name match outranks the pinyin match for the same letters.
    await _seed(db_session, [*SEED, ("000100.SZ", "TCL科技", "listed"), ("600100.SH", "同方股份", "listed")])
    mixed = await repository.search("t", limit=10)
    assert mixed[0].ticker == "000100.SZ"  # name contains "T"
    assert "600100.SH" in {r.ticker for r in mixed}  # pinyin "tfgf"


async def test_search_limit_blank_and_no_match(db_session) -> None:
    await _seed(db_session, SEED)
    repository = StockDirectoryRepository(db_session)
    assert len(await repository.search("0", limit=1)) == 1
    assert await repository.search("   ", limit=10) == []
    assert await repository.search("zzzz", limit=10) == []
    assert await repository.search("%", limit=10) == []  # LIKE wildcards are escaped
    assert await repository.search("_", limit=10) == []


# ---- health ------------------------------------------------------------------


async def test_health_reports_directory_freshness_and_staleness(db_session) -> None:
    now = datetime(2026, 10, 15, 4, 0, tzinfo=UTC)
    today = date(2026, 10, 15)
    missing = await build_data_health(db_session, health_settings(), now=now, today=today)
    assert missing.freshness.directory.model_dump() == {"synced_on": None, "total": 0, "age_days": None}
    assert {w.code: w.level for w in missing.warnings}["directory_missing"] == "info"

    await _seed(db_session, SEED[:3], synced_on=date(2026, 10, 8))
    fresh = await build_data_health(db_session, health_settings(), now=now, today=today)
    directory = fresh.freshness.directory
    assert (directory.synced_on, directory.total, directory.age_days) == (date(2026, 10, 8), 3, 7)
    assert not {"directory_missing", "directory_stale"} & {w.code for w in fresh.warnings}

    later = today + timedelta(days=DIRECTORY_STALE_DAYS)
    stale = await build_data_health(
        db_session, health_settings(), now=now + timedelta(days=DIRECTORY_STALE_DAYS), today=later
    )
    assert stale.freshness.directory.age_days == DIRECTORY_STALE_DAYS + 7
    warning = next(w for w in stale.warnings if w.code == "directory_stale")
    assert warning.level == "warn" and warning.section == "freshness"
    assert "2026-10-08" in warning.message
