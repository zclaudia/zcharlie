"""``MaterialStore``: content-hash versions, raw artifacts and version chains (plan 05).

The unit-level invariants: one content hash is one version *per dataset key*,
the raw artifact is written once and never rewritten, a changed payload is a
new revision linked to the one it supersedes, and the two timestamps
(``captured_at`` / ``source_fetched_at``) never take part in the version
identity. Contract violations must fail before anything reaches disk or DB.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from investment_agent.materials.contracts import (
    SCHEMA_VERSION,
    AccountingStandard,
    DatasetDraft,
    DatasetKind,
    DatePrecision,
    EquityScope,
    FactPeriod,
    FactProvenance,
    FactRecord,
    FactSemantics,
    FactValue,
    PeriodBasis,
    PerShareBasis,
    ReportingScope,
    SubjectKind,
    Verification,
    content_hash,
    fact_to_payload,
)
from investment_agent.materials.store import MaterialStore, SavedDataset
from investment_agent.storage.models.research_material import ResearchDataset

SOURCE = "eastmoney.hk.native.v1"
ADAPTER_VERSION = "1"
CAPTURED_AT = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
PERIOD = FactPeriod(
    start=date(2024, 1, 1),
    end=date(2024, 12, 31),
    basis=PeriodBasis.ANNUAL,
    label="FY2024",
)
RAW = b'{"result": {"count": 1}, "success": true}'


def make_fact(*, raw_value: str = "62440000000", metric: str = "revenue_operating") -> FactRecord:
    return FactRecord(
        metric=metric,
        period=PERIOD,
        semantics=FactSemantics(
            metric=metric,
            source_label="经营收入总额",
            source_field="001003999",
            accounting_standard=AccountingStandard.IFRS,
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=EquityScope.ORDINARY,
            per_share_basis=PerShareBasis.NA,
        ),
        value=FactValue(
            raw_value=raw_value,
            raw_unit="元",
            scale=1,
            unit="USD",
            currency="USD",
            sign_convention="as reported",
        ),
        provenance=FactProvenance(
            source=SOURCE,
            accession=None,
            original_hash=None,
            source_published_at=None,
            source_published_date=None,
            source_published_precision=DatePrecision.UNKNOWN,
            accepted_at=None,
            source_available_at=None,
        ),
        verification=Verification.CANDIDATE,
        conflict="none",
        applicability="applicable",
    )


def make_draft(**overrides: Any) -> DatasetDraft:
    fields: dict[str, Any] = {
        "kind": DatasetKind.ANNUAL_FACTS,
        "subject_kind": SubjectKind.COMPANY,
        "subject_id": 7,
        "source": SOURCE,
        "schema_version": SCHEMA_VERSION,
        "adapter_version": ADAPTER_VERSION,
        "captured_at": CAPTURED_AT,
        "payload": {"facts": [fact_to_payload(make_fact())], "report": "income"},
        "period": PERIOD,
        "raw_bytes": RAW,
        "raw_media_type": "application/json",
        "raw_source_url": "https://example.invalid/income",
    }
    fields.update(overrides)
    return DatasetDraft(**fields)


@pytest.fixture
def store(db_session, tmp_path: Path) -> MaterialStore:
    return MaterialStore(db_session, data_dir=tmp_path / "data")


async def row_count(session) -> int:
    return (await session.execute(select(func.count()).select_from(ResearchDataset))).scalar_one()


def utc(moment: datetime) -> datetime:
    """SQLite drops ``tzinfo`` on read-back; compare instants in UTC."""

    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


async def test_save_writes_the_row_and_the_raw_artifact(db_session, store, tmp_path) -> None:
    draft = make_draft()
    outcome = await store.save(draft)

    digest = content_hash(draft)
    assert outcome == SavedDataset(
        id=outcome.id,
        revision=1,
        content_hash=digest,
        raw_ref=f"research-material/company/7/annual_facts/{digest[:2]}/{digest}.json",
        created=True,
    )
    assert outcome.raw_ref is not None and not Path(outcome.raw_ref).is_absolute()

    artifact = tmp_path / "data" / outcome.raw_ref
    assert artifact.read_bytes() == RAW
    assert artifact.stat().st_size == len(RAW)

    row = await db_session.get(ResearchDataset, outcome.id)
    assert row is not None
    assert (row.kind, row.subject_kind, row.subject_id, row.source) == (
        "annual_facts",
        "company",
        7,
        SOURCE,
    )
    assert (row.period_end, row.period_basis) == (date(2024, 12, 31), "annual")
    assert row.raw_ref == outcome.raw_ref
    assert row.payload == draft.payload
    assert (utc(row.captured_at), row.source_fetched_at, row.revision, row.previous_id) == (
        CAPTURED_AT,
        None,
        1,
        None,
    )


async def test_second_save_of_the_same_content_is_a_no_op(db_session, store, tmp_path) -> None:
    first = await store.save(make_draft())
    artifact = tmp_path / "data" / str(first.raw_ref)
    before = artifact.stat().st_mtime_ns

    second = await store.save(make_draft(captured_at=CAPTURED_AT.replace(hour=9)))

    assert second.created is False
    assert (second.id, second.revision, second.content_hash, second.raw_ref) == (
        first.id,
        first.revision,
        first.content_hash,
        first.raw_ref,
    )
    assert await row_count(db_session) == 1
    assert artifact.stat().st_mtime_ns == before
    assert artifact.read_bytes() == RAW


async def test_changed_payload_is_a_new_revision_and_the_old_row_is_untouched(
    db_session, store, tmp_path
) -> None:
    first = await store.save(make_draft())
    original_row = await db_session.get(ResearchDataset, first.id)
    assert original_row is not None
    original_payload = dict(original_row.payload)
    original_file = tmp_path / "data" / str(first.raw_ref)
    original_mtime = original_file.stat().st_mtime_ns

    changed = replace(
        make_fact(raw_value="62440000001"),
        metric="revenue_operating",
    )
    new_payload = {"facts": [fact_to_payload(changed)], "report": "income"}
    second = await store.save(make_draft(payload=new_payload))

    assert second.created is True
    assert (second.revision, second.content_hash) != (first.revision, first.content_hash)

    stored = await db_session.get(ResearchDataset, second.id)
    assert stored is not None
    assert (stored.revision, stored.previous_id) == (2, first.id)
    assert stored.content_hash == content_hash(make_draft(payload=new_payload))

    reloaded = await db_session.get(ResearchDataset, first.id)
    assert reloaded is not None
    await db_session.refresh(reloaded)
    assert reloaded.payload == original_payload
    assert reloaded.content_hash == first.content_hash
    assert reloaded.raw_ref == first.raw_ref
    assert (reloaded.revision, reloaded.previous_id) == (1, None)
    assert original_file.read_bytes() == RAW
    assert original_file.stat().st_mtime_ns == original_mtime


async def test_history_and_latest_expose_the_version_chain(db_session, store) -> None:
    first = await store.save(make_draft())
    second = await store.save(make_draft(payload={"facts": [], "report": "income"}))

    history = await store.history(kind="annual_facts", subject_kind="company", subject_id=7)
    assert [row.id for row in history] == [first.id, second.id]
    assert [row.revision for row in history] == [1, 2]

    latest = await store.latest(
        kind="annual_facts", subject_kind="company", subject_id=7, period_end=PERIOD.end
    )
    assert latest is not None and latest.id == second.id
    # period_end=None means the null-period rows, not "any period".
    assert (
        await store.latest(
            kind="annual_facts", subject_kind="company", subject_id=7, period_end=None
        )
        is None
    )
    assert await store.history(kind="annual_facts", subject_kind="company", subject_id=8) == ()


async def test_timestamps_do_not_change_the_version(db_session, store) -> None:
    first = await store.save(make_draft(captured_at=CAPTURED_AT))
    later = CAPTURED_AT.replace(year=2026, month=11, day=1)
    second = await store.save(
        make_draft(
            captured_at=later,
            source_fetched_at=datetime(2026, 10, 11, 15, 47, 50, tzinfo=UTC),
        )
    )

    assert second.created is False
    assert second.content_hash == first.content_hash
    assert await row_count(db_session) == 1
    row = await db_session.get(ResearchDataset, first.id)
    assert row is not None
    # The stored row keeps the times of the version that really exists.
    assert utc(row.captured_at) == CAPTURED_AT and row.source_fetched_at is None


async def test_source_fetched_at_is_stored_without_touching_the_hash(db_session, store) -> None:
    fetched = datetime(2026, 10, 11, 15, 47, 50, 746113, tzinfo=UTC)
    outcome = await store.save(make_draft(source_fetched_at=fetched))

    row = await db_session.get(ResearchDataset, outcome.id)
    assert row is not None
    assert row.source_fetched_at is not None and utc(row.source_fetched_at) == fetched
    assert utc(row.captured_at) == CAPTURED_AT != utc(row.source_fetched_at)
    assert outcome.content_hash == content_hash(make_draft())


@pytest.mark.parametrize("raw_bytes", [None, b""])
async def test_draft_without_raw_bytes_has_no_artifact(
    db_session, store, tmp_path, raw_bytes
) -> None:
    outcome = await store.save(make_draft(raw_bytes=raw_bytes, raw_media_type=None))

    assert outcome.raw_ref is None
    row = await db_session.get(ResearchDataset, outcome.id)
    assert row is not None and row.raw_ref is None
    assert not (tmp_path / "data" / "research-material").exists()


async def test_contract_violations_raise_before_anything_is_written(
    db_session, store, tmp_path
) -> None:
    short_year = FactPeriod(
        start=date(2024, 3, 1), end=date(2024, 12, 26), basis=PeriodBasis.ANNUAL
    )
    with pytest.raises(ValueError, match="annual span must be 330..400 days"):
        await store.save(make_draft(period=short_year, payload={"facts": [fact_to_payload(
            replace(make_fact(), period=short_year)
        )]}))

    with pytest.raises(ValueError, match="payload"):
        await store.save(make_draft(payload={"items": []}))

    assert await row_count(db_session) == 0
    assert not (tmp_path / "data").exists()


async def test_identical_content_under_another_subject_is_its_own_row(
    db_session, store, tmp_path
) -> None:
    first = await store.save(make_draft(subject_id=7))
    other = await store.save(make_draft(subject_id=8))

    assert other.created is True
    assert (other.id, other.revision) != (first.id, first.revision)
    assert other.content_hash == first.content_hash
    assert other.raw_ref != first.raw_ref
    assert (tmp_path / "data" / str(first.raw_ref)).is_file()
    assert (tmp_path / "data" / str(other.raw_ref)).is_file()
    assert await row_count(db_session) == 2

    # by_content_hash returns the earliest row with the hash (subject 7), so
    # re-saving subject 8 must still find its own version, not insert a third.
    again = await store.save(make_draft(subject_id=8))
    assert again.created is False
    assert (again.id, again.revision) == (other.id, other.revision)
    assert await row_count(db_session) == 2
    assert await store.save(make_draft(subject_id=7)) == replace(first, created=False)


async def test_file_less_dataset_has_its_own_version_chain(db_session, store) -> None:
    first = await store.save(
        make_draft(
            kind=DatasetKind.FILING_INDEX,
            period=None,
            raw_bytes=None,
            raw_media_type=None,
            payload={"facts": [], "cik": "0000320193", "filings": []},
        )
    )
    second = await store.save(
        make_draft(
            kind=DatasetKind.FILING_INDEX,
            period=None,
            raw_bytes=None,
            raw_media_type=None,
            payload={
                "facts": [],
                "cik": "0000320193",
                "filings": [{"accession": "0000320193-24-000001"}],
            },
        )
    )

    row = await db_session.get(ResearchDataset, second.id)
    assert row is not None
    assert (second.revision, row.previous_id) == (2, first.id)
    assert second.raw_ref is None
    latest = await store.latest(kind="filing_index", subject_kind="company", subject_id=7)
    assert latest is not None and latest.id == second.id and latest.period_end is None


def test_raw_path_is_relative_to_data_dir_and_rejects_bad_hashes(store, tmp_path) -> None:
    digest = "ab" * 32
    target = store.raw_path(
        subject_kind="listing",
        subject_id=12,
        kind="price_daily",
        content_hash=digest,
        suffix=".ndjson",
    )
    assert target == (
        tmp_path / "data" / "research-material" / "listing" / "12" / "price_daily"
        / "ab" / f"{digest}.ndjson"
    )
    with pytest.raises(ValueError, match="hex digest"):
        store.raw_path(subject_kind="company", subject_id=7, kind="annual_facts", content_hash="")
    with pytest.raises(ValueError, match="subject_id"):
        store.raw_path(subject_kind="company", subject_id=0, kind="annual_facts", content_hash=digest)
    with pytest.raises(ValueError):
        store.raw_path(
            subject_kind="company", subject_id=7, kind="unknown_kind", content_hash=digest
        )
