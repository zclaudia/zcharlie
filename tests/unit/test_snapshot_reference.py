"""universe_sync / industry_sync jobs over a fake baostock backend."""

from __future__ import annotations

from datetime import UTC, date, datetime

from investment_agent.providers.baostock_reference import BaostockReferenceClient
from investment_agent.research.worker import ResearchWorker
from investment_agent.snapshots.jobs import (
    IndustrySyncPayload,
    UniverseSyncPayload,
    enqueue_snapshot_job,
    idempotency_key,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.stock_directory import StockDirectoryRepository
from investment_agent.storage.repositories.universe import UniverseRepository
from tests.unit._snapshot_support import FakeProxy, handlers_for, settings_for


class _Status:
    error_code = "0"
    error_msg = "success"


class _Result:
    error_code = "0"
    error_msg = "success"

    def __init__(self, fields: list[str], rows: list[list[str]]) -> None:
        self.fields = fields
        self._rows = rows
        self._index = 0

    def next(self) -> bool:
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        row = self._rows[self._index]
        self._index += 1
        return row


class FakeBaostockBackend:
    """Minimal synchronous baostock stand-in."""

    def __init__(self, members: dict[str, str], update_date: str, industries: dict[str, str]):
        self.members = members
        self.update_date = update_date
        self.industries = industries
        # query_all_stock answer: the members plus an index row to filter out;
        # ``None`` makes the call blow up (directory refresh must stay best effort).
        self.directory: dict[str, str] | None = dict(members)
        self.directory_days: list[str] = []

    def login(self) -> _Status:
        return _Status()

    def logout(self) -> _Status:
        return _Status()

    def query_hs300_stocks(self, date: str = "") -> _Result:
        return _Result(
            ["updateDate", "code", "code_name"],
            [[self.update_date, code, name] for code, name in self.members.items()],
        )

    query_sz50_stocks = query_hs300_stocks
    query_zz500_stocks = query_hs300_stocks

    def query_all_stock(self, day: str = "") -> _Result:
        self.directory_days.append(day)
        if self.directory is None:
            raise RuntimeError("directory unavailable")
        rows = [[code, "1", name] for code, name in self.directory.items()]
        rows.append(["sh.000300", "1", "沪深300指数"])
        return _Result(["code", "tradeStatus", "code_name"], rows)

    def query_stock_industry(self, code: str = "", date: str = "") -> _Result:
        return _Result(
            ["updateDate", "code", "code_name", "industry", "industryClassification"],
            [
                [self.update_date, code_, code_, industry, "证监会行业分类"]
                for code_, industry in self.industries.items()
            ],
        )


def _client(backend: FakeBaostockBackend) -> BaostockReferenceClient:
    return BaostockReferenceClient(
        backend, clock=lambda: datetime(2026, 10, 6, 1, 0, tzinfo=UTC), max_attempts=1
    )


async def _run_job(tmp_path, payload, backend, shenwan=None):
    settings = settings_for(tmp_path)
    async with session_scope() as session:
        run, _ = await enqueue_snapshot_job(session, payload, triggered_by="test")
        run_id = run.id
    worker = ResearchWorker(
        settings=settings,
        worker_id="w",
        handlers=handlers_for(
            settings, FakeProxy(), reference=_client(backend), shenwan=shenwan
        ),
    )
    assert await worker.run_once() == run_id
    async with session_scope() as session:
        return await PipelineRunRepository(session).get(run_id)


async def test_universe_sync_applies_members_with_names_and_notifies(db_session, tmp_path):
    del db_session
    backend = FakeBaostockBackend(
        {"sh.600519": "贵州茅台", "sz.000001": "平安银行"}, "2026-10-05", {}
    )
    first = await _run_job(
        tmp_path, UniverseSyncPayload(universe_code="CSI300", as_of=date(2026, 10, 5)), backend
    )
    assert first.status == "success"
    assert first.metadata_json["added"] == ["000001.SZ", "600519.SH"]
    assert first.metadata_json["removed"] == []
    async with session_scope() as session:
        members = await UniverseRepository(session).members_as_of("CSI300", date(2026, 10, 5))
        listing = await ListingRepository(session).get_a_share_listing("600519.SH")
        assert listing is not None
        await session.refresh(listing, ["company"])
        assert listing.company.canonical_name == "贵州茅台"
    assert [m.ticker for m in members] == ["000001.SZ", "600519.SH"]
    # The stock directory is refreshed right after (mrd-intake D4): the day
    # comes from the runtime clock (2026-10-06 Asia/Shanghai), the index row is dropped.
    assert backend.directory_days == ["2026-10-06"]
    assert first.metadata_json["directory"] == {
        "as_of": "2026-10-06",
        "provider": "baostock",
        "provider_rows": 2,
        "total": 2,
        "added": 2,
        "changed": 0,
        "delisted": 0,
    }
    async with session_scope() as session:
        entry = await StockDirectoryRepository(session).get("600519.SH")
        assert entry is not None and entry.pinyin_initials == "gzmt"
    notes = list((tmp_path / "notifications").rglob("snapshot_universe_sync_*.md"))
    assert len(notes) == 1
    text = notes[0].read_text()
    assert "`info`" in text and "600519.SH" in text

    # Next snapshot: one member swapped -> removed/added recorded.
    backend.members = {"sh.600519": "贵州茅台", "sz.300750": "宁德时代"}
    backend.directory = dict(backend.members)
    backend.update_date = "2026-10-06"
    second = await _run_job(
        tmp_path, UniverseSyncPayload(universe_code="CSI300", as_of=date(2026, 10, 6)), backend
    )
    assert second.metadata_json["added"] == ["300750.SZ"]
    assert second.metadata_json["removed"] == ["000001.SZ"]
    directory = second.metadata_json["directory"]
    assert (directory["added"], directory["delisted"], directory["total"]) == (1, 1, 3)
    raw_ref = second.metadata_json["raw_ref"]
    assert (tmp_path / raw_ref).is_file()


async def test_directory_failure_does_not_fail_the_universe_sync(db_session, tmp_path):
    del db_session
    backend = FakeBaostockBackend({"sh.600519": "贵州茅台"}, "2026-10-05", {})
    backend.directory = None
    run = await _run_job(
        tmp_path, UniverseSyncPayload(universe_code="CSI300", as_of=date(2026, 10, 5)), backend
    )
    assert run.status == "success"
    assert run.metadata_json["added"] == ["600519.SH"]
    assert run.metadata_json["directory"]["error"].startswith("RuntimeError: directory unavailable")
    async with session_scope() as session:
        assert await StockDirectoryRepository(session).freshness() == (None, 0)


async def test_unchanged_universe_sync_does_not_notify(db_session, tmp_path):
    del db_session
    backend = FakeBaostockBackend({"sh.600519": "贵州茅台"}, "2026-10-05", {})
    await _run_job(
        tmp_path, UniverseSyncPayload(universe_code="CSI300", as_of=date(2026, 10, 5)), backend
    )
    run = await _run_job(
        tmp_path, UniverseSyncPayload(universe_code="CSI300", as_of=date(2026, 10, 6)), backend
    )
    assert run.metadata_json["changed"] is False
    assert len(list((tmp_path / "notifications").rglob("*.md"))) == 1


async def test_industry_sync_limited_to_universe_members(db_session, tmp_path):
    del db_session
    backend = FakeBaostockBackend(
        {"sh.600519": "贵州茅台"},
        "2026-10-05",
        {"sh.600519": "C15酒、饮料和精制茶制造业", "sz.000002": "K70房地产业"},
    )
    await _run_job(
        tmp_path, UniverseSyncPayload(universe_code="CSI300", as_of=date(2026, 10, 5)), backend
    )
    payload = IndustrySyncPayload(as_of=date(2026, 10, 5), universe_codes=("CSI300",))
    assert idempotency_key(payload) == "industry_sync:CSI300:2026-10-05"
    run = await _run_job(tmp_path, payload, backend)
    assert run.status == "success"
    assert run.metadata_json["provider_entries"] == 2
    assert run.metadata_json["applied_entries"] == 1
    async with session_scope() as session:
        listing = await ListingRepository(session).get_a_share_listing("600519.SH")
        assert listing is not None
        industry = await IndustryRepository(session).industry_as_of(listing.id, date(2026, 10, 5))
        outsider = await ListingRepository(session).get_a_share_listing("000002.SZ")
    assert industry is not None and industry.code == "C15"
    assert outsider is None  # whole-market rows outside the universe are not applied
    # The same job synced Shenwan 2021 (fixture replay) for the universe members only.
    sw = run.metadata_json["sw2021"]
    assert run.metadata_json["systems"] == ["csrc", "sw2021"]
    assert (sw["scope"], sw["applied"], sw["system"]) == (1, 1, "sw2021")
    assert (tmp_path / sw["raw_ref"]).is_file()
    async with session_scope() as session:
        assert listing is not None
        shenwan = await IndustryRepository(session).industry_as_of(
            listing.id, date(2026, 10, 5), system="sw2021"
        )
    assert shenwan is not None and (shenwan.code, shenwan.name) == ("340501", "白酒Ⅲ")


async def _seed_universe(tmp_path, backend):
    await _run_job(
        tmp_path, UniverseSyncPayload(universe_code="CSI300", as_of=date(2026, 10, 5)), backend
    )


async def test_industry_sync_single_system(db_session, tmp_path):
    del db_session
    backend = FakeBaostockBackend(
        {"sh.600519": "贵州茅台"}, "2026-10-05", {"sh.600519": "C15酒、饮料和精制茶制造业"}
    )
    await _seed_universe(tmp_path, backend)
    payload = IndustrySyncPayload(
        as_of=date(2026, 10, 5), universe_codes=("CSI300",), systems=("sw2021",)
    )
    assert idempotency_key(payload) == "industry_sync:CSI300:2026-10-05:sw2021"
    run = await _run_job(tmp_path, payload, backend)
    assert run.status == "success"
    assert "provider_entries" not in run.metadata_json  # CSRC not synced
    assert run.metadata_json["sw2021"]["applied"] == 1
    async with session_scope() as session:
        listing = await ListingRepository(session).get_a_share_listing("600519.SH")
        assert listing is not None
        assert await IndustryRepository(session).industry_as_of(listing.id, date(2026, 10, 5)) is None


async def test_terminal_shenwan_failure_keeps_csrc_and_ends_partial(db_session, tmp_path):
    del db_session
    from tests.unit._shenwan_fake import FailingShenwanClient

    backend = FakeBaostockBackend(
        {"sh.600519": "贵州茅台"}, "2026-10-05", {"sh.600519": "C15酒、饮料和精制茶制造业"}
    )
    await _seed_universe(tmp_path, backend)
    payload = IndustrySyncPayload(as_of=date(2026, 10, 5), universe_codes=("CSI300",))
    run = await _run_job(tmp_path, payload, backend, shenwan=FailingShenwanClient())
    assert run.status == "partial"
    assert run.metadata_json["failed_systems"] == ["sw2021"]
    assert "HTTP 404" in run.metadata_json["errors"]["sw2021"]
    assert run.metadata_json["applied_entries"] == 1  # CSRC still applied

    only_sw = IndustrySyncPayload(
        as_of=date(2026, 10, 5), universe_codes=("CSI300",), systems=("sw2021",)
    )
    failed = await _run_job(tmp_path, only_sw, backend, shenwan=FailingShenwanClient())
    assert failed.status == "failed"  # a lone system: its terminal error fails the run
