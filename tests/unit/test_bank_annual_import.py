"""Offline bank FY2025 supplier facts remain candidate and append idempotently."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from investment_agent.cli.commands.bank_annual import (
    BANK_CODES,
    MISSING_EQUITY_REASON,
    _snapshot_hash,
    import_bank_annual_snapshots,
    load_bank_plan,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.disclosure import DisclosureOriginal
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from tests.support.peer_world import ORIGINALS_ROOT
from tests.support.peer_world import requires_originals as requires_originals

FIXTURE = Path(__file__).parents[1] / "fixtures/bank_run02"
ACQUIRED = datetime(2026, 10, 15, 2, tzinfo=UTC)


@pytest.fixture
async def bank_session(tmp_path):
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path}/bank.db")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            repo = ListingRepository(session)
            for code in BANK_CODES:
                await repo.ensure_a_share_listing(code, company_name=code)
            yield session
    finally:
        await engine.dispose()


def _load(root: Path, code: str):
    path = root / "raw/financial" / f"{code}_main.json"
    return path, json.loads(path.read_text())


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, default=lambda value: float(value)))


async def test_ten_company_import_is_idempotent_and_candidate(bank_session, tmp_path):
    first = await import_bank_annual_snapshots(
        bank_session,
        evidence_root=FIXTURE,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
    )
    await bank_session.commit()
    assert (first["companies"], first["inserted"], first["verified_fields"]) == (10, 10, 0)
    rows = list((await bank_session.execute(select(PeerAnnualSnapshot))).scalars())
    assert len(rows) == 10
    for row in rows:
        assert set(row.fields) == {
            "net_interest_margin",
            "npl_ratio",
            "provision_coverage",
            "roe_weighted_annual",
            "equity_parent",
        }
        assert all(field["verification"] == "candidate" for field in row.fields.values())
        for metric in (
            "net_interest_margin",
            "npl_ratio",
            "provision_coverage",
            "roe_weighted_annual",
        ):
            field = row.fields[metric]
            assert field["unit"] == "fraction" and field["raw_unit"] == "percent"
            assert Decimal(field["value"]) == Decimal(field["raw_value"]) / 100
            assert field["disclosed_on"] is not None and field["updated_on"] is not None
            assert row.verification[metric]["status"] == "candidate"
            assert row.verification[metric]["pdf_page"] is None
        equity = row.fields["equity_parent"]
        assert equity["value"] is None and equity["missing_reason"] == MISSING_EQUITY_REASON

    old_times = {row.id: row.captured_at for row in rows}
    relocated = tmp_path / "copied-evidence-root"
    shutil.copytree(FIXTURE, relocated)
    replay = await import_bank_annual_snapshots(
        bank_session,
        evidence_root=relocated,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED.replace(hour=3),
    )
    assert replay["inserted"] == 0 and replay["skipped_existing"] == 10
    current = list((await bank_session.execute(select(PeerAnnualSnapshot))).scalars())
    assert len(current) == 10 and {row.id: row.captured_at for row in current} == old_times


async def test_audit_inputs_must_be_paired_and_fail_before_writing(bank_session, tmp_path):
    with pytest.raises(ValueError, match="must be supplied together"):
        await import_bank_annual_snapshots(
            bank_session,
            evidence_root=FIXTURE,
            report_period=date(2025, 12, 31),
            acquired_at=ACQUIRED,
            audit_path=tmp_path / "audit.json",
        )
    assert not list((await bank_session.execute(select(PeerAnnualSnapshot))).scalars())

    bad_audit = tmp_path / "audit.json"
    bad_audit.write_text('{"schema":"wrong"}', encoding="utf-8")
    with pytest.raises(ValueError, match="bank-annual-original/1"):
        await import_bank_annual_snapshots(
            bank_session,
            evidence_root=FIXTURE,
            report_period=date(2025, 12, 31),
            acquired_at=ACQUIRED,
            audit_path=bad_audit,
            originals_root=tmp_path,
        )
    assert not list((await bank_session.execute(select(PeerAnnualSnapshot))).scalars())


def test_supplier_zero_is_a_value_and_fake_verified_flag_is_ignored(tmp_path):
    root = tmp_path / "evidence"
    shutil.copytree(FIXTURE, root)
    path, data = _load(root, BANK_CODES[0])
    row = data["result"]["data"][0]
    row["NET_INTEREST_MARGIN"] = 0
    row["verification"] = "verified"
    _write(path, data)
    field = load_bank_plan(root, date(2025, 12, 31), ACQUIRED)[0]["fields"]["net_interest_margin"]
    assert field.value == Decimal("0") and field.missing_reason is None
    assert field.verification.value == "candidate"


def test_missing_source_dates_stay_missing(tmp_path):
    root = tmp_path / "evidence"
    shutil.copytree(FIXTURE, root)
    path, data = _load(root, BANK_CODES[0])
    row = data["result"]["data"][0]
    row.pop("NOTICE_DATE")
    row.pop("UPDATE_DATE")
    _write(path, data)
    field = load_bank_plan(root, date(2025, 12, 31), ACQUIRED)[0]["fields"]["npl_ratio"]
    assert field.disclosed_on is None and field.updated_on is None
    assert field.source_version is None and field.verification.value == "candidate"


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda rows: rows.append(rows[0]), "exactly one"),
        (lambda rows: rows[0].update(REPORT_TYPE="中报"), "not labelled 年报"),
        (lambda rows: rows[0].update(NET_INTEREST_MARGIN="1.23"), "expected finite JSON number"),
        (lambda rows: rows[0].pop("BLDKBBL"), "required supplier field BLDKBBL is absent"),
    ],
)
def test_bad_rows_fields_and_types_are_rejected(tmp_path, mutation, message):
    root = tmp_path / "evidence"
    shutil.copytree(FIXTURE, root)
    path, data = _load(root, BANK_CODES[0])
    mutation(data["result"]["data"])
    _write(path, data)
    with pytest.raises(ValueError, match=message):
        load_bank_plan(root, date(2025, 12, 31), ACQUIRED)


def test_non_annual_requested_period_is_rejected():
    with pytest.raises(ValueError, match="annual Dec 31"):
        load_bank_plan(FIXTURE, date(2025, 6, 30), ACQUIRED)


def test_audit_hash_ignores_relocated_paths_but_keeps_audit_facts():
    plan = load_bank_plan(FIXTURE, date(2025, 12, 31), ACQUIRED)[0]
    fields = plan["fields"]
    proofs = json.loads(json.dumps(plan["proofs"]))
    proof = proofs["net_interest_margin"]
    proof.update(
        {
            "pdf_hash": "a" * 64,
            "pdf_path": "/first/root/report.pdf",
            "source_manifest_ref": "first/source-manifest.json",
            "checked_at": "2026-10-15T13:29:46+00:00",
            "audit_record": {
                "pdf_sha256": "a" * 64,
                "pdf_path": "/first/root/report.pdf",
                "source_manifest_ref": "first/source-manifest.json",
                "acquired_at": "2026-10-15T13:00:00+00:00",
                "checked_at": "2026-10-15T13:29:46+00:00",
            },
        }
    )
    first = _snapshot_hash(fields, proofs)
    relocated = json.loads(json.dumps(proofs))
    relocated["net_interest_margin"]["pdf_path"] = "/other/root/report.pdf"
    relocated["net_interest_margin"]["source_manifest_ref"] = "other/source-manifest.json"
    relocated["net_interest_margin"]["audit_record"].update(
        pdf_path="/other/root/report.pdf",
        source_manifest_ref="other/source-manifest.json",
        acquired_at="2026-10-15T14:00:00+00:00",
    )
    assert _snapshot_hash(fields, relocated) == first
    changed_fact = json.loads(json.dumps(relocated))
    changed_fact["net_interest_margin"]["audit_record"]["checked_at"] = "2026-10-15T13:30:00+00:00"
    assert _snapshot_hash(fields, changed_fact) != first
    changed_hash = json.loads(json.dumps(relocated))
    changed_hash["net_interest_margin"]["audit_record"]["pdf_sha256"] = "b" * 64
    assert _snapshot_hash(fields, changed_hash) != first


async def test_changed_source_appends_version_and_preserves_old_snapshot(bank_session, tmp_path):
    root = tmp_path / "evidence"
    shutil.copytree(FIXTURE, root)
    first = await import_bank_annual_snapshots(
        bank_session,
        evidence_root=root,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
    )
    await bank_session.commit()
    old = list((await bank_session.execute(select(PeerAnnualSnapshot))).scalars())
    old_hash = old[0].semantic_hash
    path, data = _load(root, BANK_CODES[0])
    data["result"]["data"][0]["ROEJQ"] = Decimal("9.46")
    _write(path, data)
    changed = await import_bank_annual_snapshots(
        bank_session,
        evidence_root=root,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
    )
    assert changed["inserted"] == 1 and changed["skipped_existing"] == 9
    now = list((await bank_session.execute(select(PeerAnnualSnapshot))).scalars())
    assert len(now) == 11 and any(row.semantic_hash == old_hash for row in now)
    assert first["semantic_hashes"][BANK_CODES[0]] != changed["semantic_hashes"][BANK_CODES[0]]


async def test_available_bank_pdfs_are_retained_but_fields_stay_candidate(
    bank_session, requires_originals
):
    evidence_root = ORIGINALS_ROOT
    required = [evidence_root / "sample.json"] + [
        evidence_root / "raw/financial" / f"{code}_main.json" for code in BANK_CODES
    ]
    for code in ("601398.SH", "600036.SH", "600919.SH"):
        required.extend(
            (
                evidence_root / "derived/filings" / f"{code}.json",
                evidence_root / "raw/official" / f"{code}_2025.pdf",
            )
        )
    if missing := [str(path.relative_to(evidence_root)) for path in required if not path.is_file()]:
        pytest.skip(
            "Bank original acceptance requires the complete local run-02 bank capture "
            f"under ZCHARLIE_PEER_ORIGINALS; missing: {', '.join(missing)}"
        )
    result = await import_bank_annual_snapshots(
        bank_session,
        evidence_root=evidence_root,
        report_period=date(2025, 12, 31),
        acquired_at=ACQUIRED,
    )
    assert result["inserted"] == 10 and result["verified_fields"] == 0
    originals = list((await bank_session.execute(select(DisclosureOriginal))).scalars())
    assert len(originals) == 2
    rows = list((await bank_session.execute(select(PeerAnnualSnapshot))).scalars())
    assert len(rows) == 10
    for row in rows:
        assert all(field["verification"] == "candidate" for field in row.fields.values())
    assert sum("available_original" in row.verification["roe_weighted_annual"] for row in rows) == 2
    assert result["originals"]["600036.SH"]["status"] == "not_registered"
    assert (
        "cover does not confirm issuer and annual period"
        in result["originals"]["600036.SH"]["reason"]
    )
    assert result["originals"]["601398.SH"]["status"] == "retained_as_candidate_original"
