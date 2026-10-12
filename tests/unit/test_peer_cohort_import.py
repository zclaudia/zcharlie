"""WP2 peer cohort roster import (``ia peers import-roster``): golden run-02
sample against the seeded directory, idempotency by content hash, all-or-nothing
identity resolution, revision appends and hash discipline.

CLI tests run against the ``peer_cohort`` typer app directly (``cli/main.py``
registration is a separate step); they use a file-backed SQLite so the command's
own ``run_with_engine`` sees the seeded directory. Direct-function tests reuse
the shared in-memory ``db_session``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import typer
from sqlalchemy import func, select
from typer.testing import CliRunner

from investment_agent.cli.commands import peer_cohort as peer_cohort_cmd
from investment_agent.cli.commands.peer_cohort import (
    COHORT_KEY,
    import_sample_roster,
    payload_sha256,
)
from investment_agent.storage.database import init_engine, run_with_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.peer import PeerCohortSnapshot
from investment_agent.storage.models.reference import Universe, UniverseMembership
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.peer import PeerCohortSnapshotRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN02_SAMPLE = REPO_ROOT / "research/industry-data-validation/evidence/run-02/sample.json"

# The golden roster: the run-02 baijiu sample's 19 companies with the names the
# local directory carries. Identity resolution is by code only - the names are
# fixture data the directory returns, never a merge key.
ROSTER: list[tuple[str, str]] = [
    ("000568.SZ", "泸州老窖"),
    ("000596.SZ", "古井贡酒"),
    ("000799.SZ", "酒鬼酒"),
    ("000858.SZ", "五粮液"),
    ("000860.SZ", "顺鑫农业"),
    ("000995.SZ", "皇台酒业"),
    ("002304.SZ", "洋河股份"),
    ("002646.SZ", "天佑德酒"),
    ("600197.SH", "伊力特"),
    ("600199.SH", "金种子酒"),
    ("600519.SH", "贵州茅台"),
    ("600559.SH", "老白干酒"),
    ("600702.SH", "舍得酒业"),
    ("600779.SH", "水井坊"),
    ("600809.SH", "山西汾酒"),
    ("603198.SH", "迎驾贡酒"),
    ("603369.SH", "今世缘"),
    ("603589.SH", "口子窖"),
    ("603919.SH", "金徽酒"),
]
# In the sample's classification list but not its index list (run-02: delisted
# 2026-06-29); the recorded reason must stay the source-supported difference.
CLASSIFICATION_ONLY: tuple[str, str] = ("600696.SH", "岩石股份")

# Small synthetic roster for the direct-function tests (same file shape).
SAMPLE_TICKERS: list[tuple[str, str]] = [
    ("600519.SH", "贵州茅台"),
    ("000858.SZ", "五粮液"),
    ("603919.SH", "金徽酒"),
]


@pytest.fixture
def peer_db(tmp_path, monkeypatch):
    """File-backed SQLite + CLI env, so the command's own engine sees the data."""

    db_path = tmp_path / "peers.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    monkeypatch.chdir(tmp_path)  # no stray .env / config/app.yaml
    monkeypatch.setenv("API_KEY", "secret")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "x")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", url)

    async def create() -> None:
        engine = init_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()

    asyncio.run(create())
    return url


def seed_directory(
    url: str, rows: list[tuple[str, str]] | None = None
) -> dict[str, tuple[int, int]]:
    """Seed the existing directory; returns ``ticker -> (listing_id, company_id)``."""

    async def run() -> dict[str, tuple[int, int]]:
        async with session_scope() as session:
            repo = ListingRepository(session)
            seeded: dict[str, tuple[int, int]] = {}
            for ticker, name in rows if rows is not None else [*ROSTER, CLASSIFICATION_ONLY]:
                listing = await repo.ensure_a_share_listing(ticker, company_name=name)
                seeded[ticker] = (listing.id, listing.company_id)
            return seeded

    return run_with_engine(url, run)


def write_sample(
    path: Path,
    *,
    tickers: list[str] | None = None,
    classification_codes: list[str] | None = None,
    captured_at: str = "2026-10-11T15:06:06.796243+00:00",
) -> Path:
    """A minimal run-02 style sample file for direct-function tests."""

    roster = tickers if tickers is not None else [ticker for ticker, _ in SAMPLE_TICKERS]
    codes = classification_codes if classification_codes is not None else list(roster)
    path.write_text(
        json.dumps(
            {
                "captured_at": captured_at,
                "membership_semantics": "Current observed constituents/classification.",
                "selection": "test sample",
                "baijiu": roster,
                "baijiu_classification_codes": codes,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _invoke(*args: str):
    """Invoke through a parent group mirroring the future ``cli/main.py``
    registration (``ia peers ...``); the shared file stays untouched."""

    root = typer.Typer()
    root.add_typer(peer_cohort_cmd.app, name="peers")
    return CliRunner().invoke(root, ["peers", *args])


def _all_output(result: Any) -> str:
    """stdout + stderr across click versions (8.2+ captures them separately)."""

    try:
        return result.output + result.stderr
    except ValueError:
        return result.output


def _summary(result: Any) -> dict[str, Any]:
    return json.loads(result.output[result.output.index("{") :])


def _history(url: str, cohort_key: str = COHORT_KEY) -> list[PeerCohortSnapshot]:
    async def run() -> list[PeerCohortSnapshot]:
        async with session_scope() as session:
            return list(await PeerCohortSnapshotRepository(session).history(cohort_key=cohort_key))

    return run_with_engine(url, run)


def _pool_counts(url: str) -> tuple[int, int, int]:
    async def run() -> tuple[int, int, int]:
        async with session_scope() as session:
            universes = (
                await session.execute(select(func.count()).select_from(Universe))
            ).scalar_one()
            memberships = (
                await session.execute(select(func.count()).select_from(UniverseMembership))
            ).scalar_one()
            watchlist = (
                await session.execute(select(func.count()).select_from(WatchlistItem))
            ).scalar_one()
            return universes, memberships, watchlist

    return run_with_engine(url, run)


# --- golden: the run-02 sample against a seeded directory --------------------


def test_help_lists_import_roster() -> None:
    result = _invoke("--help")
    assert result.exit_code == 0
    assert "import-roster" in result.output


def test_golden_import_matches_run02_sample(peer_db) -> None:
    assert RUN02_SAMPLE.exists(), "run-02 sample.json is the committed golden benchmark"
    seeded = seed_directory(peer_db)
    sample = json.loads(RUN02_SAMPLE.read_text())

    result = _invoke("import-roster", str(RUN02_SAMPLE))
    assert result.exit_code == 0, result.output
    summary = _summary(result)
    assert summary["created"] is True
    assert summary["revision"] == 1
    assert summary["entries"] == 19
    assert summary["included"] == 19
    assert summary["excluded"] == ["600696.SH"]
    assert summary["verification"] == "candidate"

    rows = _history(peer_db)
    assert len(rows) == 1
    row = rows[0]
    assert row.id == summary["snapshot_id"]
    assert row.cohort_key == "baijiu_current"
    assert row.revision == 1
    assert row.observed_on == date(2026, 10, 11)
    assert row.verification == "candidate"
    assert row.inclusion_rule_version == "run-02/1"
    # SQLite keeps the naive wall clock of the aware import time; either way
    # the stored value must be the actual import moment.
    stored = (
        row.acquired_at.replace(tzinfo=UTC)
        if row.acquired_at.tzinfo is None
        else row.acquired_at.astimezone(UTC)
    )
    assert timedelta(0) <= datetime.now(UTC) - stored < timedelta(minutes=5)

    payload = row.payload
    assert payload["schema"] == "peer_cohort_snapshot/1"
    assert payload["classification"] == {
        "system": "sw2021",
        "version": "2021",
        "code": "340501",
        "per_company_evidence": "not_provided_by_source",
    }
    assert payload["index_code"] == "851251.SI"

    # Golden entries: exactly the sample's roster, each resolved by code to the
    # seeded directory identity, with the directory name carried and labelled.
    assert [entry["ticker"] for entry in payload["entries"]] == sample["baijiu"]
    assert {entry["ticker"]: entry["name"] for entry in payload["entries"]} == dict(ROSTER)
    for entry in payload["entries"]:
        listing_id, company_id = seeded[entry["ticker"]]
        assert entry["listing_id"] == listing_id
        assert entry["company_id"] == company_id
        assert entry["market_code"] in {"SSE", "SZSE"}
        assert entry["name_source"] == "local_directory.company.canonical_name"
        assert entry["classification_member"] is True
        # The sample carries no per-company classification dates, listing
        # status or comparability flags - recorded as not provided, not faked.
        assert entry["classification_evidence"] == "not_provided_by_source"
        assert entry["listing_status"] is None
        assert entry["listing_status_evidence"] == "not_provided_by_source"
        assert entry["comparability_flags"] is None
        assert entry["comparability_flags_evidence"] == "not_provided_by_source"

    assert [item["ticker"] for item in payload["included"]] == sample["baijiu"]
    assert payload["excluded"] == [
        {
            "ticker": "600696.SH",
            "company_id": seeded["600696.SH"][1],
            "reason": payload["excluded"][0]["reason"],
        }
    ]
    assert payload["source_differences"] == {
        "index_only": [],
        "classification_only": ["600696.SH"],
    }

    # Source locator + hash + observation metadata from the file itself.
    assert payload["source"]["sha256"] == hashlib.sha256(RUN02_SAMPLE.read_bytes()).hexdigest()
    assert (
        payload["source"]["path"] == "research/industry-data-validation/evidence/run-02/sample.json"
    )
    assert payload["source"]["captured_at"] == sample["captured_at"]
    assert payload["source"]["membership_semantics"] == sample["membership_semantics"]
    assert payload["source"]["selection"] == sample["selection"]

    # The row-level hash is the canonical payload hash (import idempotency key).
    assert row.content_hash == payload_sha256(payload)
    assert row.content_hash == summary["content_hash"]

    # The roster never becomes a screening pool or watchlist entry.
    assert _pool_counts(peer_db) == (0, 0, 0)


# --- idempotency -------------------------------------------------------------


def test_reimport_identical_content_appends_no_revision(peer_db) -> None:
    seed_directory(peer_db)
    first = _invoke("import-roster", str(RUN02_SAMPLE))
    assert first.exit_code == 0, first.output

    second = _invoke("import-roster", str(RUN02_SAMPLE))
    assert second.exit_code == 0, second.output
    assert "no new revision" in second.output
    summary = _summary(second)
    assert summary["created"] is False
    assert summary["revision"] == 1

    rows = _history(peer_db)
    assert len(rows) == 1
    assert rows[0].id == _summary(first)["snapshot_id"] == summary["snapshot_id"]


async def test_identical_content_with_different_import_time_reuses_revision(
    db_session, tmp_path
) -> None:
    """The content hash excludes the import time, so ``acquired_at`` never
    manufactures a new revision."""

    repo = ListingRepository(db_session)
    for ticker, name in SAMPLE_TICKERS:
        await repo.ensure_a_share_listing(ticker, company_name=name)
    path = write_sample(tmp_path / "sample.json")

    first = await import_sample_roster(
        db_session, sample_path=path, acquired_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    second = await import_sample_roster(
        db_session, sample_path=path, acquired_at=datetime(2026, 6, 1, tzinfo=UTC)
    )
    assert first.created is True
    assert second.created is False
    assert second.snapshot.id == first.snapshot.id
    assert first.snapshot.revision == 1
    assert (
        len(list(await PeerCohortSnapshotRepository(db_session).history(cohort_key=COHORT_KEY)))
        == 1
    )


def test_payload_hash_is_canonical_and_content_sensitive() -> None:
    keyed = {"x": 1, "y": {"b": [1, 2], "a": "白酒"}}
    reordered = {"y": {"a": "白酒", "b": [1, 2]}, "x": 1}
    assert payload_sha256(keyed) == payload_sha256(reordered)
    changed = {"x": 2, "y": {"a": "白酒", "b": [1, 2]}}
    assert payload_sha256(keyed) != payload_sha256(changed)


# --- identity resolution: all-or-nothing -------------------------------------


def test_unresolvable_codes_fail_the_whole_batch_without_writes(peer_db) -> None:
    seed_directory(
        peer_db,
        rows=[row for row in ROSTER if row[0] not in {"600519.SH", "603919.SH"}],
    )
    result = _invoke("import-roster", str(RUN02_SAMPLE))
    assert result.exit_code == 1
    output = _all_output(result)
    assert "600519.SH" in output and "603919.SH" in output
    assert "nothing written" in output
    assert _history(peer_db) == []


def test_non_canonical_ticker_is_rejected_before_any_write(peer_db, tmp_path) -> None:
    seed_directory(peer_db)
    path = write_sample(tmp_path / "bad.json", tickers=["600519.SH", "123456"])
    result = _invoke("import-roster", str(path))
    assert result.exit_code == 1
    assert "123456" in _all_output(result)
    assert _history(peer_db) == []


# --- revisions ----------------------------------------------------------------


def test_changed_content_appends_revision_and_keeps_old_rows_readable(peer_db, tmp_path) -> None:
    seed_directory(peer_db)
    assert _invoke("import-roster", str(RUN02_SAMPLE)).exit_code == 0

    changed = json.loads(RUN02_SAMPLE.read_text())
    changed["baijiu"].remove("603919.SH")
    changed_path = tmp_path / "sample-rev2.json"
    changed_path.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    result = _invoke("import-roster", str(changed_path))
    assert result.exit_code == 0, result.output
    summary = _summary(result)
    assert summary["created"] is True
    assert summary["revision"] == 2

    rows = _history(peer_db)
    assert [row.revision for row in rows] == [1, 2]
    assert rows[0].id != rows[1].id
    assert rows[0].content_hash != rows[1].content_hash
    assert len(rows[0].payload["entries"]) == 19
    assert len(rows[1].payload["entries"]) == 18
    # The dropped ticker is still in the classification list, so the recorded
    # source difference grows instead of silently shrinking the roster.
    assert [item["ticker"] for item in rows[1].payload["excluded"]] == [
        "600696.SH",
        "603919.SH",
    ]

    async def read_first() -> PeerCohortSnapshot | None:
        async with session_scope() as session:
            return await PeerCohortSnapshotRepository(session).by_revision(
                cohort_key=COHORT_KEY, revision=1
            )

    old = run_with_engine(peer_db, read_first)
    assert old is not None
    assert old.payload == rows[0].payload
    assert old.content_hash == rows[0].content_hash
    assert old.acquired_at == rows[0].acquired_at


# --- payload semantics on synthetic samples -----------------------------------


async def test_difference_entry_without_directory_identity_stays_null(db_session, tmp_path) -> None:
    """Excluded difference entries are recorded even when the directory cannot
    resolve them; only roster members fail the batch."""

    repo = ListingRepository(db_session)
    for ticker, name in SAMPLE_TICKERS:
        await repo.ensure_a_share_listing(ticker, company_name=name)
    path = write_sample(
        tmp_path / "sample.json",
        classification_codes=[ticker for ticker, _ in SAMPLE_TICKERS] + ["600696.SH"],
    )
    result = await import_sample_roster(db_session, sample_path=path)
    assert result.created is True
    payload = result.snapshot.payload
    assert payload["excluded"][0]["ticker"] == "600696.SH"
    assert payload["excluded"][0]["company_id"] is None
    assert payload["source_differences"]["classification_only"] == ["600696.SH"]
    assert payload["source_differences"]["index_only"] == []
    assert [entry["ticker"] for entry in payload["entries"]] == [
        ticker for ticker, _ in SAMPLE_TICKERS
    ]


async def test_classification_membership_is_per_entry(db_session, tmp_path) -> None:
    repo = ListingRepository(db_session)
    for ticker, name in SAMPLE_TICKERS:
        await repo.ensure_a_share_listing(ticker, company_name=name)
    path = write_sample(
        tmp_path / "sample.json",
        tickers=["600519.SH", "000858.SZ"],
        classification_codes=["600519.SH"],
    )
    result = await import_sample_roster(db_session, sample_path=path)
    entries = {entry["ticker"]: entry for entry in result.snapshot.payload["entries"]}
    assert entries["600519.SH"]["classification_member"] is True
    assert entries["000858.SZ"]["classification_member"] is False
    assert result.snapshot.payload["source_differences"]["index_only"] == ["000858.SZ"]
