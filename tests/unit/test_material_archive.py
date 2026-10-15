"""Archive evidence is content addressed, safely readable and append-only."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

import investment_agent.materials.archive as archive_module
from investment_agent.materials.archive import (
    ArchiveCapture,
    ArchiveIntegrityError,
    DatasetLink,
    ResponseEvidence,
    archive_capture,
    identity_snapshot,
    period_snapshot,
    record_acquisition,
    sanitize_diagnostic,
    sanitize_url,
    verify_capture,
)
from investment_agent.storage.models.material_acquisition import ResearchMaterialAcquisition
from investment_agent.storage.models.research_material import ResearchDataset

FETCHED = datetime(2026, 10, 10, 1, 2, tzinfo=UTC)


def capture(*, fetched_at: datetime | None = FETCHED) -> ArchiveCapture:
    return ArchiveCapture(
        identity={"company_key": "acme", "market_code": "NASDAQ", "ticker": "ACME"},
        source="sec.companyfacts.v1",
        subject_id=7,
        adapter_version="1",
        periods=({"start": "2025-01-01", "end": "2025-12-31"},),
        expected_requests=("companyfacts", "submissions", "submissions_2025"),
        responses=(
            ResponseEvidence(
                "companyfacts",
                b'{"facts": {}}',
                "https://user:pass@example.test/facts?cik=1&api_key=secret#private",
                "application/json",
                200,
                fetched_at,
            ),
            ResponseEvidence(
                "submissions",
                b'{"recent": []}',
                "https://example.test/submissions?limit=1",
                "application/json",
                200,
                fetched_at,
                parse_eligible=False,
            ),
            ResponseEvidence(
                "failed-request",
                b"upstream error body",
                "https://example.test/fail?attempt=2",
                "text/plain",
                503,
                fetched_at,
            ),
        ),
        problems=("partial response https://example.test/fail?token=secret",),
        captured_at=datetime(2026, 10, 15, 8, tzinfo=UTC),
    )


def test_multi_response_archive_dedups_offline_capture_and_preserves_unknown_time(tmp_path: Path) -> None:
    first = archive_capture(tmp_path, capture())
    replay = archive_capture(
        tmp_path, replace(capture(), captured_at=datetime(2026, 10, 16, 9, tzinfo=UTC))
    )
    assert replay.manifest_ref == first.manifest_ref
    assert replay.manifest_sha256 == first.manifest_sha256
    verified = verify_capture(tmp_path, first.manifest_ref)
    assert verified.source == "sec.companyfacts.v1"
    assert verified.subject_id == 7
    assert verified.expected_requests == ("companyfacts", "submissions", "submissions_2025")
    assert [item.content for item in verified.responses] == [
        b'{"facts": {}}', b'{"recent": []}', b"upstream error body"
    ]
    assert [item.parse_eligible for item in verified.responses] == [True, False, False]
    assert verified.responses[0].fetched_at == FETCHED
    assert "api_key=%5BREDACTED%5D" in verified.responses[0].source_url
    assert "user" not in verified.responses[0].source_url
    assert "token=secret" not in verified.problems[0]
    unsafe_identity = archive_capture(
        tmp_path,
        replace(
            capture(),
            identity={"homepage": "https://user:pass@example.test/?apiKey=secret", "note": "token=private"},
        ),
    )
    identity_text = str(verify_capture(tmp_path, unsafe_identity.manifest_ref).identity)
    assert "pass" not in identity_text and "secret" not in identity_text and "private" not in identity_text

    changed = archive_capture(
        tmp_path,
        capture(fetched_at=datetime(2026, 10, 11, 1, 2, tzinfo=UTC)),
    )
    assert changed.manifest_sha256 != first.manifest_sha256

    unknown = archive_capture(tmp_path, capture(fetched_at=None))
    assert verify_capture(tmp_path, unknown.manifest_ref).responses[0].fetched_at is None


def test_unknown_http_status_remains_eligible_and_http_403_does_not(tmp_path: Path) -> None:
    evidence = replace(
        capture(),
        responses=(
            ResponseEvidence("unknown", b"original", "https://example.test/unknown", "text/plain", 0, None),
            ResponseEvidence("forbidden", b"denied", "https://example.test/forbidden", "text/plain", 403, None),
        ),
    )
    saved = archive_capture(tmp_path, evidence)
    verified = verify_capture(tmp_path, saved.manifest_ref, expected_sha256=saved.manifest_sha256)
    assert [(response.http_status, response.fetched_at, response.parse_eligible) for response in verified.responses] == [
        (0, None, True),
        (403, None, False),
    ]


def test_capture_url_redaction_and_safe_query_retention(tmp_path: Path) -> None:
    assert sanitize_url("https://u:p@e.test/path?a=2&access_token=x&from=2024#frag") == (
        "https://e.test/path?a=2&access_token=%5BREDACTED%5D&from=2024"
    )
    assert sanitize_url("https://e.test/?apiKey=x&accessToken=y&from=2025") == (
        "https://e.test/?apiKey=%5BREDACTED%5D&accessToken=%5BREDACTED%5D&from=2025"
    )
    assert "secret-value" not in archive_capture(
        tmp_path, replace(capture(), problems=("Authorization: Bearer secret-value denied",))
    ).problems[0]
    diagnostic = sanitize_diagnostic("token=one key:two Bearer three")
    assert all(secret not in diagnostic for secret in ("one", "two", "three"))
    assert sanitize_diagnostic("url https://e.test/?secret=x") == (
        "url https://e.test/?secret=%5BREDACTED%5D"
    )


def test_archive_detects_missing_tampered_traversal_and_symlink_files(tmp_path: Path) -> None:
    saved = archive_capture(tmp_path, capture())
    verified = verify_capture(tmp_path, saved.manifest_ref)
    body = verified.responses[0]
    digest = hashlib.sha256(body.content).hexdigest()
    body_path = tmp_path / "research-material" / "archive" / "sec.companyfacts.v1" / "bodies" / digest[:2] / f"{digest}.bin"
    body_path.write_bytes(b"tampered")
    with pytest.raises(ArchiveIntegrityError, match="integrity"):
        verify_capture(tmp_path, saved.manifest_ref)
    with pytest.raises(ArchiveIntegrityError, match="corrupt"):
        archive_capture(tmp_path, capture())
    body_path.unlink()
    with pytest.raises(ArchiveIntegrityError):
        verify_capture(tmp_path, saved.manifest_ref)
    with pytest.raises(ArchiveIntegrityError, match="escapes"):
        verify_capture(tmp_path, "research-material/archive/../../outside.json")
    manifest_path = tmp_path / saved.manifest_ref
    original_manifest = manifest_path.read_bytes()
    manifest_path.write_bytes(original_manifest + b" ")
    with pytest.raises(ArchiveIntegrityError, match="SHA-256"):
        verify_capture(tmp_path, saved.manifest_ref, expected_sha256=saved.manifest_sha256)
    manifest_path.write_bytes(original_manifest)

    outside = tmp_path / "outside"
    outside.mkdir()
    link_dir = tmp_path / "research-material" / "archive" / "sec.companyfacts.v1" / "bodies" / digest[:2]
    link_dir.rmdir()
    link_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArchiveIntegrityError, match="symbolic link"):
        verify_capture(tmp_path, saved.manifest_ref)


def test_manifest_requires_array_shapes_and_non_boolean_schema_version(tmp_path: Path) -> None:
    saved = archive_capture(tmp_path, capture())
    original = json.loads((tmp_path / saved.manifest_ref).read_bytes())
    for key, value in (("expected_requests", "companyfacts"), ("schema_version", True)):
        manifest = dict(original)
        manifest[key] = value
        content = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(content).hexdigest()
        ref = (
            Path("research-material/archive/sec.companyfacts.v1/manifests")
            / digest[:2]
            / f"{digest}.json"
        )
        target = tmp_path / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        with pytest.raises(ArchiveIntegrityError):
            verify_capture(tmp_path, ref.as_posix())


async def test_acquisition_is_idempotent_links_datasets_and_records_no_dataset(db_session, tmp_path) -> None:
    archived = archive_capture(tmp_path, capture())
    dataset = ResearchDataset(
        kind="filing_index",
        subject_kind="company",
        subject_id=7,
        source="sec.submissions.v1",
        schema_version=1,
        adapter_version="1",
        period_end=None,
        period_basis=None,
        revision=2,
        previous_id=None,
        content_hash="a" * 64,
        captured_at=FETCHED,
        source_fetched_at=FETCHED,
        raw_ref=None,
        payload={"facts": []},
    )
    db_session.add(dataset)
    await db_session.flush()
    first = await record_acquisition(
        db_session,
        archived,
        datasets=(DatasetLink(id=dataset.id, revision=2, content_hash="a" * 64),),
    )
    # SEC's acquisition source is companyfacts while an index dataset is submissions.
    assert first.source == "sec.companyfacts.v1"
    assert first.dataset_links == [{"id": dataset.id, "revision": 2, "content_hash": "a" * 64}]
    await db_session.flush()
    replay_archive = archive_capture(
        tmp_path, replace(capture(), captured_at=datetime(2026, 10, 17, 1, tzinfo=UTC))
    )
    assert await record_acquisition(
        db_session,
        replay_archive,
        datasets=(DatasetLink(id=dataset.id, revision=2, content_hash="a" * 64),),
    ) is first
    assert (await db_session.execute(select(ResearchMaterialAcquisition))).scalars().all() == [first]

    no_data = archive_capture(
        tmp_path, replace(capture(), problems=("valid response but no dataset produced",))
    )
    observation = await record_acquisition(db_session, no_data)
    assert observation.dataset_links == []


async def test_archive_can_precede_rolled_back_acquisition_without_dangling_db_reference(
    db_session, tmp_path
) -> None:
    archived = archive_capture(tmp_path, capture())
    tx = await db_session.begin_nested()
    await record_acquisition(db_session, archived)
    await tx.rollback()
    assert (await db_session.execute(select(ResearchMaterialAcquisition))).scalars().all() == []
    assert verify_capture(tmp_path, archived.manifest_ref).responses


async def test_archive_write_failure_creates_no_row_and_retry_repairs_missing_manifest(
    db_session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = archive_module._write_content_addressed
    failed = False

    def fail_manifest_once(target: Path, content: bytes) -> None:
        nonlocal failed
        if target.suffix == ".json" and not failed:
            failed = True
            raise OSError("injected manifest write failure")
        original(target, content)

    monkeypatch.setattr(archive_module, "_write_content_addressed", fail_manifest_once)
    with pytest.raises(OSError, match="injected"):
        archive_module.archive_capture(tmp_path, capture())
    assert (await db_session.execute(select(ResearchMaterialAcquisition))).scalars().all() == []

    monkeypatch.setattr(archive_module, "_write_content_addressed", original)
    archived = archive_module.archive_capture(tmp_path, capture())
    row = await archive_module.record_acquisition(db_session, archived)
    assert row.archive_ref == archived.manifest_ref
    assert archive_module.verify_capture(tmp_path, archived.manifest_ref).responses


async def test_acquisition_rejects_invalid_dataset_links(db_session, tmp_path) -> None:
    archived = archive_capture(tmp_path, capture())
    dataset = ResearchDataset(
        kind="filing_index", subject_kind="company", subject_id=7,
        source="unrelated.provider", schema_version=1, adapter_version="1",
        period_end=None, period_basis=None, revision=1, previous_id=None,
        content_hash="a" * 64, captured_at=FETCHED, source_fetched_at=None,
        raw_ref=None, payload={"facts": []},
    )
    db_session.add(dataset)
    await db_session.flush()
    with pytest.raises(ValueError, match="unrelated"):
        await record_acquisition(
            db_session,
            archived,
            datasets=(DatasetLink(dataset.id, 1, "a" * 64),),
        )
    with pytest.raises(ValueError, match="does not exist"):
        await record_acquisition(
            db_session,
            archived,
            datasets=(DatasetLink(9999, 1, "a" * 64),),
        )

    listing_dataset = ResearchDataset(
        kind="annual_facts", subject_kind="listing", subject_id=7,
        source="sec.companyfacts.v1", schema_version=1, adapter_version="1",
        period_end=None, period_basis=None, revision=1, previous_id=None,
        content_hash="c" * 64, captured_at=FETCHED, source_fetched_at=None,
        raw_ref=None, payload={"facts": []},
    )
    db_session.add(listing_dataset)
    await db_session.flush()
    with pytest.raises(ValueError, match="not a company"):
        await record_acquisition(
            db_session,
            archived,
            datasets=(DatasetLink(listing_dataset.id, 1, "c" * 64),),
        )


def test_controlled_identity_and_period_snapshot_helpers(tmp_path: Path) -> None:
    from investment_agent.domain.security import ControlledIdentity, SecurityType, SourceBinding
    from investment_agent.materials.contracts import FactPeriod, PeriodBasis

    identity = ControlledIdentity(
        market_code="NASDAQ", ticker="ACME", company_key="acme", company_name="Acme",
        company_name_zh="阿克米", security_type=SecurityType.ORDINARY,
        quote_currency="USD", report_currency="USD", fiscal_year_end_note="12-31",
        sources=(SourceBinding(source="sec.companyfacts.v1", symbol="123"),),
        valid_from=FETCHED.date(),
        notes="Docs: https://example.test/?apiKey=private; token=hidden",
    )
    snapshot = identity_snapshot(identity)
    assert snapshot["ticker"] == "ACME"
    assert "data_dir" not in snapshot and "archive_ref" not in snapshot
    assert "private" not in str(snapshot) and "hidden" not in str(snapshot)
    period = FactPeriod(
        FETCHED.date(), FETCHED.date(), PeriodBasis.INSTANT,
        label="report https://example.test/?accessToken=private",
    )
    snapshot_period = period_snapshot(period)
    archived = archive_capture(
        tmp_path, replace(capture(), identity=snapshot, periods=(snapshot_period,))
    )
    replay = verify_capture(tmp_path, archived.manifest_ref)
    assert replay.identity == snapshot
    assert replay.periods == (snapshot_period,)


async def test_acquisition_rejects_orm_and_bulk_sql_mutation(db_session, tmp_path) -> None:
    archived = archive_capture(tmp_path, capture())
    row = await record_acquisition(db_session, archived)
    with pytest.raises((ValueError, RuntimeError), match="immutable"):
        row.problems = ["edited"]
        await db_session.flush()
    await db_session.rollback()

    # Use a fresh transaction/row after the ORM listener rejection.
    row = await record_acquisition(db_session, archived)
    await db_session.delete(row)
    with pytest.raises((ValueError, RuntimeError), match="immutable"):
        await db_session.flush()
    await db_session.rollback()

    row = await record_acquisition(db_session, archived)
    with pytest.raises(IntegrityError, match="immutable"):
        await db_session.execute(
            ResearchMaterialAcquisition.__table__.update().values(source="changed")
        )
    await db_session.rollback()
