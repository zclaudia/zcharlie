"""Offline import: replaying saved evidence into the immutable store (plan 05).

The evidence fixtures are trimmed copies of the checked research evidence:

* ``tests/fixtures/materials/offline/00005/income-four-years.json`` keeps 3 of
  the 132 rows (FY2024 only) of the saved response
  ``research/market-data-validation/evidence/remaining-final-03/raw/native/00005/income-four-years.json``
  fetched ``2026-10-11T15:47:50.746113+00:00``;
* its ``.meta.json`` keeps the original ``url``/``fetched_at_utc``/``status``
  and carries the ``bytes``/``sha256`` of the trimmed body.

The adapters here are fakes defined in this file (``materials/hk_native.py``
and ``materials/us_sec.py`` belong to the same wave and are not imported); the
real-adapter end-to-end import belongs to task 06.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from freezegun import freeze_time
from sqlalchemy import func, select

from investment_agent.domain.security import ControlledIdentity
from investment_agent.materials.archive import verify_capture
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
    ParseResult,
    PeriodBasis,
    PerShareBasis,
    RawPayload,
    ReportingScope,
    SubjectKind,
    UpstreamRequest,
    Verification,
    fact_to_payload,
)
from investment_agent.materials.offline_import import import_offline_evidence
from investment_agent.materials.store import MaterialStore
from investment_agent.storage.models.material_acquisition import ResearchMaterialAcquisition
from investment_agent.storage.models.research_material import ResearchDataset
from tests.unit._hk_us_support import pilot_identities

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "materials" / "offline"
SOURCE = "eastmoney.hk.native.v1"
OTHER_SOURCE = "yahoo.chart.v1"
IMPORTED_AT = datetime(2026, 10, 12, 6, 30, tzinfo=UTC)
META_FETCHED_AT = datetime(2026, 10, 11, 15, 47, 50, 746113, tzinfo=UTC)
COMPANY_ID = 7
INCOME_NAME = "income-four-years.json"
BALANCE_NAME = "balance-four-years.json"

IDENTITY: ControlledIdentity = next(
    identity for identity in pilot_identities() if identity.company_key == "hsbc-holdings"
)


def utc(moment: datetime) -> datetime:
    """SQLite drops ``tzinfo`` on read-back; compare instants in UTC."""

    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


def table_request(name: str) -> UpstreamRequest:
    return UpstreamRequest(name=name, url=f"https://example.invalid/get?file={name}")


def table_fact(raw_value: str) -> FactRecord:
    return FactRecord(
        metric="revenue_operating",
        period=FactPeriod(
            start=date(2024, 1, 1), end=date(2024, 12, 31), basis=PeriodBasis.ANNUAL
        ),
        semantics=FactSemantics(
            metric="revenue_operating",
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


@dataclass
class FakeTableAdapter:
    """Minimal ``MaterialAdapter``: one draft per response file, from its rows.

    It deliberately leaves ``raw_bytes`` empty and stamps a wrong historical
    ``captured_at``, so the tests can prove the importer (not the adapter)
    attaches the artifact and the real import time.
    """

    source: str = SOURCE
    adapter_version: str = "fake-1"
    names: tuple[str, ...] = (INCOME_NAME,)
    problems: tuple[str, ...] = ()
    received: list[RawPayload] = field(default_factory=list)

    def requests(
        self, identity: ControlledIdentity, *, periods: Sequence[FactPeriod]
    ) -> tuple[UpstreamRequest, ...]:
        return tuple(table_request(name) for name in self.names)

    def parse(
        self,
        identity: ControlledIdentity,
        *,
        raw: Sequence[RawPayload],
        periods: Sequence[FactPeriod],
        subject_id: int | None = None,
    ) -> ParseResult:
        self.received.extend(raw)
        if self.problems:
            return ParseResult(drafts=(), problems=self.problems)
        document = json.loads(raw[0].content.decode("utf-8"))
        amounts = {row["STD_ITEM_CODE"]: row["AMOUNT"] for row in document["result"]["data"]}
        return ParseResult(drafts=(self._draft(str(amounts["001003999"])),), problems=())

    def _draft(self, raw_value: str) -> DatasetDraft:
        return DatasetDraft(
            kind=DatasetKind.ANNUAL_FACTS,
            subject_kind=SubjectKind.COMPANY,
            subject_id=1,  # placeholder: the importer stamps the real company id
            source=self.source,
            schema_version=SCHEMA_VERSION,
            adapter_version=self.adapter_version,
            captured_at=datetime(2000, 1, 1, tzinfo=UTC),  # must be replaced
            payload={"facts": [fact_to_payload(table_fact(raw_value))], "report": "income"},
            period=FactPeriod(
                start=date(2024, 1, 1), end=date(2024, 12, 31), basis=PeriodBasis.ANNUAL
            ),
        )


def copy_fixture(root: Path, *names: str) -> Path:
    """Copy the trimmed evidence (response plus sidecar) under ``root/native/00005``."""

    target_dir = root / "native" / "00005"
    target_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy(FIXTURE_DIR / "00005" / name, target_dir / name)
        shutil.copy(FIXTURE_DIR / "00005" / (name + ".meta.json"), target_dir / (name + ".meta.json"))
    return root


@pytest.fixture
def evidence_dir(tmp_path: Path) -> Path:
    return copy_fixture(tmp_path / "evidence", INCOME_NAME)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def saved_drafts(monkeypatch: pytest.MonkeyPatch) -> list[DatasetDraft]:
    """Drafts handed to ``MaterialStore.save`` (some fields have no column)."""

    seen: list[DatasetDraft] = []
    original = MaterialStore.save

    async def spy(self: MaterialStore, draft: DatasetDraft):
        seen.append(draft)
        return await original(self, draft)

    monkeypatch.setattr(MaterialStore, "save", spy)
    return seen


async def load_rows(session) -> list[ResearchDataset]:
    return list((await session.execute(select(ResearchDataset))).scalars())


async def test_import_stores_the_dataset_with_real_times_and_the_raw_bytes(
    db_session, evidence_dir: Path, data_dir: Path, saved_drafts: list[DatasetDraft]
) -> None:
    adapter = FakeTableAdapter()
    report = await import_offline_evidence(
        db_session,
        data_dir=data_dir,
        source_dir=evidence_dir,
        identities=(IDENTITY,),
        company_ids={IDENTITY.company_key: COMPANY_ID},
        adapters={SOURCE: adapter},
        imported_at=IMPORTED_AT,
    )

    assert report.problems == ()
    assert report.skipped == ()
    assert len(report.saved) == 1
    outcome = report.saved[0]
    assert (outcome.created, outcome.revision) == (True, 1)

    rows = await load_rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert (row.kind, row.subject_kind, row.subject_id, row.source) == (
        "annual_facts",
        "company",
        COMPANY_ID,
        SOURCE,
    )
    assert (row.period_end, row.period_basis) == (date(2024, 12, 31), "annual")
    assert utc(row.captured_at) == IMPORTED_AT
    assert row.source_fetched_at is not None
    assert utc(row.source_fetched_at) == META_FETCHED_AT < IMPORTED_AT
    assert row.raw_ref == outcome.raw_ref

    artifact = data_dir / str(row.raw_ref)
    fixture_bytes = (FIXTURE_DIR / "00005" / INCOME_NAME).read_bytes()
    assert artifact.read_bytes() == fixture_bytes
    assert json.loads(fixture_bytes)["result"]["count"] == 3
    digest = row.content_hash
    assert row.raw_ref == (
        f"research-material/company/{COMPANY_ID}/annual_facts/{digest[:2]}/{digest}.json"
    )

    # The adapter sees the response exactly as the request named it, with the
    # sidecar's provenance, before the importer takes over the draft.
    (received,) = adapter.received
    assert received.name == INCOME_NAME
    assert received.media_type == "application/json"
    assert utc(received.fetched_at) == META_FETCHED_AT
    assert received.http_status == 200
    assert received.content == fixture_bytes

    (prepared,) = saved_drafts
    assert prepared.subject_id == COMPANY_ID
    assert prepared.captured_at == IMPORTED_AT
    assert prepared.source_fetched_at == META_FETCHED_AT
    assert prepared.raw_bytes == fixture_bytes
    assert prepared.raw_source_url is not None and prepared.raw_source_url.startswith(
        "https://datacenter.eastmoney.com/"
    )


async def test_second_import_of_the_same_directory_is_idempotent(
    db_session, evidence_dir: Path, data_dir: Path
) -> None:
    first = await import_offline_evidence(
        db_session,
        data_dir=data_dir,
        source_dir=evidence_dir,
        identities=(IDENTITY,),
        company_ids={IDENTITY.company_key: COMPANY_ID},
        adapters={SOURCE: FakeTableAdapter()},
        imported_at=IMPORTED_AT,
    )
    artifact = data_dir / str(first.saved[0].raw_ref)
    before = artifact.stat().st_mtime_ns

    second = await import_offline_evidence(
        db_session,
        data_dir=data_dir,
        source_dir=evidence_dir,
        identities=(IDENTITY,),
        company_ids={IDENTITY.company_key: COMPANY_ID},
        adapters={SOURCE: FakeTableAdapter()},
        imported_at=IMPORTED_AT.replace(hour=8),
    )

    assert [outcome.created for outcome in second.saved] == [False]
    assert second.problems == () and second.skipped == ()
    assert second.saved[0] == replace(first.saved[0], created=False)
    assert len(await load_rows(db_session)) == 1
    assert artifact.stat().st_mtime_ns == before


async def test_missing_sidecar_keeps_the_fetch_time_unknown(
    db_session, evidence_dir: Path, data_dir: Path
) -> None:
    (evidence_dir / "native" / "00005" / (INCOME_NAME + ".meta.json")).unlink()
    adapter = FakeTableAdapter()

    report = await import_offline_evidence(
        db_session,
        data_dir=data_dir,
        source_dir=evidence_dir,
        identities=(IDENTITY,),
        company_ids={IDENTITY.company_key: COMPANY_ID},
        adapters={SOURCE: adapter},
        imported_at=IMPORTED_AT,
    )

    assert len(report.problems) == 1
    assert "missing sidecar" in report.problems[0] and INCOME_NAME in report.problems[0]
    assert report.skipped == (f"{IDENTITY.company_key}/{SOURCE}",)
    assert len(report.saved) == 1 and report.saved[0].created is True

    (row,) = await load_rows(db_session)
    assert row.source_fetched_at is None
    assert utc(row.captured_at) == IMPORTED_AT
    # The import moment is only the in-memory placeholder for RawPayload, and
    # it is never back-filled into the persisted provenance.
    (received,) = adapter.received
    assert received.fetched_at == IMPORTED_AT
    assert received.http_status == 0
    assert received.source_url == table_request(INCOME_NAME).url


async def test_sidecar_without_fetched_at_utc_does_not_back_fill(
    db_session, evidence_dir: Path, data_dir: Path
) -> None:
    sidecar = evidence_dir / "native" / "00005" / (INCOME_NAME + ".meta.json")
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    document.pop("fetched_at_utc")
    sidecar.write_text(json.dumps(document), encoding="utf-8")

    report = await import_offline_evidence(
        db_session,
        data_dir=data_dir,
        source_dir=evidence_dir,
        identities=(IDENTITY,),
        company_ids={IDENTITY.company_key: COMPANY_ID},
        adapters={SOURCE: FakeTableAdapter()},
        imported_at=IMPORTED_AT,
    )

    assert any("fetched_at_utc" in problem for problem in report.problems)
    assert len(report.saved) == 1
    (row,) = await load_rows(db_session)
    assert row.source_fetched_at is None
    assert utc(row.captured_at) == IMPORTED_AT
    assert row.source_fetched_at != row.captured_at


async def test_one_missing_response_file_does_not_stop_the_other_tables(
    db_session, evidence_dir: Path, data_dir: Path
) -> None:
    report = await import_offline_evidence(
        db_session,
        data_dir=data_dir,
        source_dir=evidence_dir,
        identities=(IDENTITY,),
        company_ids={IDENTITY.company_key: COMPANY_ID},
        adapters={SOURCE: FakeTableAdapter(names=(INCOME_NAME, BALANCE_NAME))},
        imported_at=IMPORTED_AT,
    )

    assert len(report.problems) == 1
    assert BALANCE_NAME in report.problems[0]
    assert report.skipped == (f"{IDENTITY.company_key}/{SOURCE}",)
    assert len(report.saved) == 1 and report.saved[0].created is True
    assert len(await load_rows(db_session)) == 1


async def test_adapter_problems_skip_only_their_source(
    db_session, evidence_dir: Path, data_dir: Path
) -> None:
    working_name = "yahoo-clp-chart.json"
    shutil.copy(
        FIXTURE_DIR / "00005" / INCOME_NAME, evidence_dir / "native" / "00005" / working_name
    )
    shutil.copy(
        FIXTURE_DIR / "00005" / (INCOME_NAME + ".meta.json"),
        evidence_dir / "native" / "00005" / (working_name + ".meta.json"),
    )
    broken = FakeTableAdapter(source=SOURCE, problems=("structural_failure: income",))
    working = FakeTableAdapter(source=OTHER_SOURCE, names=(working_name,))

    report = await import_offline_evidence(
        db_session,
        data_dir=data_dir,
        source_dir=evidence_dir,
        identities=(IDENTITY,),
        company_ids={IDENTITY.company_key: COMPANY_ID},
        adapters={SOURCE: broken, OTHER_SOURCE: working},
        imported_at=IMPORTED_AT,
    )

    assert any("structural_failure: income" in problem for problem in report.problems)
    assert report.skipped == (f"{IDENTITY.company_key}/{SOURCE}",)
    assert len(report.saved) == 1 and report.saved[0].created is True
    (row,) = await load_rows(db_session)
    assert row.source == OTHER_SOURCE


async def test_unbound_source_and_unknown_company_are_reported(
    db_session, evidence_dir: Path, data_dir: Path
) -> None:
    report = await import_offline_evidence(
        db_session,
        data_dir=data_dir,
        source_dir=evidence_dir,
        identities=(IDENTITY,),
        company_ids={},  # the caller did not resolve the company id
        adapters={SOURCE: FakeTableAdapter()},
        imported_at=IMPORTED_AT,
    )
    assert report.problems == (
        f"{IDENTITY.company_key}: no company id supplied for the identity",
    )
    assert report.skipped == (IDENTITY.company_key,)
    assert report.saved == ()

    us_identity = next(
        identity for identity in pilot_identities() if identity.company_key == "apple"
    )
    report = await import_offline_evidence(
        db_session,
        data_dir=data_dir,
        source_dir=evidence_dir,
        identities=(us_identity,),
        company_ids={us_identity.company_key: 9},
        adapters={SOURCE: FakeTableAdapter()},
        imported_at=IMPORTED_AT,
    )
    assert report.problems == ()
    assert report.skipped == (
        f"{us_identity.company_key}/{SOURCE}: the pilot list binds no symbol for this source",
    )
    assert report.saved == ()


async def test_missing_evidence_directory_is_reported_once(
    db_session, tmp_path: Path, data_dir: Path
) -> None:
    report = await import_offline_evidence(
        db_session,
        data_dir=data_dir,
        source_dir=tmp_path / "absent",
        identities=(IDENTITY,),
        company_ids={IDENTITY.company_key: COMPANY_ID},
        adapters={SOURCE: FakeTableAdapter()},
        imported_at=IMPORTED_AT,
    )
    assert report.problems == (f"evidence directory not found: {tmp_path / 'absent'}",)
    assert report.saved == ()


@freeze_time("2026-10-12 07:00:00")
async def test_default_import_time_is_now(db_session, evidence_dir: Path, data_dir: Path) -> None:
    report = await import_offline_evidence(
        db_session,
        data_dir=data_dir,
        source_dir=evidence_dir,
        identities=(IDENTITY,),
        company_ids={IDENTITY.company_key: COMPANY_ID},
        adapters={SOURCE: FakeTableAdapter()},
    )

    assert len(report.saved) == 1
    (row,) = await load_rows(db_session)
    assert utc(row.captured_at) == datetime(2026, 10, 12, 7, 0, tzinfo=UTC)
    assert row.source_fetched_at is not None and utc(row.source_fetched_at) == META_FETCHED_AT
    total = (
        await db_session.execute(select(func.count()).select_from(ResearchDataset))
    ).scalar_one()
    assert total == 1


@pytest.mark.parametrize("status", [301, 403, 429, 500, 0, "500", None])
async def test_failed_http_evidence_never_reaches_the_adapter(
    db_session, evidence_dir, data_dir, status
) -> None:
    sidecar = evidence_dir / "native" / "00005" / (INCOME_NAME + ".meta.json")
    metadata = json.loads(sidecar.read_text())
    metadata["status"] = status
    sidecar.write_text(json.dumps(metadata))
    adapter = FakeTableAdapter()
    report = await import_offline_evidence(
        db_session, data_dir=data_dir, source_dir=evidence_dir,
        identities=(IDENTITY,), company_ids={IDENTITY.company_key: COMPANY_ID},
        adapters={SOURCE: adapter},
    )
    assert not adapter.received and not report.saved
    assert any("rejected HTTP status" in problem for problem in report.problems)
    assert await load_rows(db_session) == []
    assert len(report.acquisition_ids) == 1
    acquisition = await db_session.get(ResearchMaterialAcquisition, report.acquisition_ids[0])
    assert acquisition is not None and acquisition.dataset_links == []
    capture = verify_capture(data_dir, acquisition.archive_ref, acquisition.manifest_sha256)
    assert len(capture.responses) == 1
    assert capture.responses[0].content == (evidence_dir / "native/00005" / INCOME_NAME).read_bytes()
    assert capture.responses[0].parse_eligible is False
    assert not (data_dir / "research-material/company").exists()


@pytest.mark.parametrize("field,value", [("sha256", "0" * 64), ("bytes", 1)])
async def test_corrupted_evidence_is_refused(db_session, evidence_dir, data_dir, field, value):
    sidecar = evidence_dir / "native" / "00005" / (INCOME_NAME + ".meta.json")
    metadata = json.loads(sidecar.read_text())
    metadata[field] = value
    sidecar.write_text(json.dumps(metadata))
    adapter = FakeTableAdapter()
    report = await import_offline_evidence(
        db_session, data_dir=data_dir, source_dir=evidence_dir,
        identities=(IDENTITY,), company_ids={IDENTITY.company_key: COMPANY_ID},
        adapters={SOURCE: adapter},
    )
    assert not adapter.received and not report.saved
    assert any("mismatch" in problem for problem in report.problems)
