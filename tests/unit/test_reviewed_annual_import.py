from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
from investment_agent.materials import reviewed_import
from investment_agent.materials.reviewed_import import (
    SOURCE_REVIEWED_ANNUAL,
    _company_drafts,
    import_reviewed_annual,
)
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.models.research_material import ResearchDataset
from tests.unit.test_annual_validation import _make_package

IDENTITIES = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)


async def _seed_company_ids(db_session) -> dict[str, int]:
    markets = {
        identity.market_code: identity.market_code for identity in IDENTITIES
    }
    for market_code in markets:
        db_session.add(
            Market(
                code=market_code,
                name=market_code,
                timezone="UTC",
                currency="USD",
            )
        )
    await db_session.flush()
    result: dict[str, int] = {}
    for identity in IDENTITIES:
        company = Company(canonical_name=identity.company_name)
        db_session.add(company)
        await db_session.flush()
        db_session.add(
            Listing(
                company_id=company.id,
                market_code=identity.market_code,
                ticker=identity.ticker,
                is_primary=True,
            )
        )
        result[identity.company_key] = company.id
    await db_session.flush()
    return result


@pytest.mark.asyncio
async def test_imports_verified_all_controlled_facts_and_is_idempotent(db_session, tmp_path):
    manifest_path, source_root, _ = _make_package(tmp_path)
    data_dir = tmp_path / "data"
    company_ids = await _seed_company_ids(db_session)
    first_time = datetime(2026, 10, 16, 2, 30, tzinfo=UTC)
    second_time = datetime(2026, 10, 18, 9, 0, tzinfo=UTC)

    first = await import_reviewed_annual(
        db_session,
        manifest_path=manifest_path,
        source_root=source_root,
        data_dir=data_dir,
        identities=IDENTITIES,
        company_ids=company_ids,
        imported_at=first_time,
    )
    assert len(first.companies) == len(IDENTITIES)
    assert all(item.status == "complete" and item.imported for item in first.companies)
    assert len(first.saved) == len(IDENTITIES)
    assert all(saved.created for saved in first.saved)
    rows = (
        await db_session.execute(
            select(ResearchDataset).where(ResearchDataset.source == SOURCE_REVIEWED_ANNUAL)
        )
    ).scalars().all()
    assert len(rows) == len(IDENTITIES)
    assert {row.captured_at.replace(tzinfo=UTC) for row in rows} == {first_time}
    raw_mtimes = {
        document["raw_ref"]: (data_dir / document["raw_ref"]).stat().st_mtime_ns
        for row in rows
        for document in row.payload["review"]["documents"]
    }
    second = await import_reviewed_annual(
        db_session,
        manifest_path=manifest_path,
        source_root=source_root,
        data_dir=data_dir,
        identities=IDENTITIES,
        company_ids=company_ids,
        imported_at=second_time,
    )
    assert all(not saved.created for saved in second.saved)
    assert raw_mtimes == {
        raw_ref: (data_dir / raw_ref).stat().st_mtime_ns for raw_ref in raw_mtimes
    }
    for row in rows:
        review = row.payload["review"]
        assert review["status"] == "complete"
        assert review["checked_on"] == "2026-10-15"
        assert review["latest_period_start"]
        assert row.source_fetched_at is None
        assert row.raw_ref is not None
        index = json.loads((data_dir / row.raw_ref).read_bytes())
        assert index["artifact_kind"] == "reviewed-annual-evidence-index"
        assert "not an upstream response" in index["description"]
        for document in review["documents"]:
            copied = data_dir / document["raw_ref"]
            assert copied.is_file()
            assert copied.read_bytes() == (
                source_root / document["manifest_path"]
            ).read_bytes()
        fact_period_keys = [
            (fact["metric"], fact["period"]["end"]) for fact in row.payload["facts"]
        ]
        assert len(fact_period_keys) == len(set(fact_period_keys))
    assert any(
        item.get("equivalent_facts")
        for row in rows
        for item in row.payload["review"]["fact_evidence"]
    )
    equivalent = next(
        item["equivalent_facts"][0]
        for row in rows
        for item in row.payload["review"]["fact_evidence"]
        if item.get("equivalent_facts")
    )
    assert equivalent["fact"]["provenance"]["original_hash"]
    assert equivalent["references"]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["hash", "missing", "escape", "derived"])
async def test_invalid_company_is_partial_and_writes_no_facts(db_session, tmp_path, damage):
    manifest_path, source_root, manifest = _make_package(tmp_path)
    target_key = "hsbc-holdings" if damage == "derived" else "apple"
    target_entry = next(row for row in manifest["companies"] if row["company_key"] == target_key)
    source_doc = target_entry["documents"][0]
    if damage == "hash":
        path = source_root / source_doc["path"]
        path.write_bytes(path.read_bytes() + b" tampered")
    elif damage == "missing":
        (source_root / source_doc["path"]).unlink()
    elif damage == "escape":
        source_doc["path"] = "../../outside.txt"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        target_entry["equity_reconciliations"] = [{"method_version": "broken"}]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    company_ids = await _seed_company_ids(db_session)
    report = await import_reviewed_annual(
        db_session,
        manifest_path=manifest_path,
        source_root=source_root,
        data_dir=tmp_path / "data",
        identities=IDENTITIES,
        company_ids=company_ids,
        imported_at=datetime(2026, 10, 16, tzinfo=UTC),
    )

    failed = next(item for item in report.companies if item.company_key == target_key)
    assert failed.status == "partial"
    assert not failed.imported and not failed.saved and failed.problems
    rows = (
        await db_session.execute(
            select(ResearchDataset).where(
                ResearchDataset.source == SOURCE_REVIEWED_ANNUAL,
                ResearchDataset.subject_id == company_ids[target_key],
            )
        )
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_global_verification_issues_block_every_company_write(
    db_session, tmp_path, monkeypatch
):
    manifest_path, source_root, _ = _make_package(tmp_path)
    company_ids = await _seed_company_ids(db_session)
    monkeypatch.setattr(
        reviewed_import,
        "verify_annual_package",
        lambda *_args, **_kwargs: {
            "scope": "all_controlled",
            "as_of": "2026-10-15",
            "status": "partial",
            "issues": ["duplicate company entry"],
            "companies": [{"company_key": "apple", "status": "complete", "issues": []}],
        },
    )

    report = await import_reviewed_annual(
        db_session,
        manifest_path=manifest_path,
        source_root=source_root,
        data_dir=tmp_path / "data",
        identities=IDENTITIES,
        company_ids=company_ids,
        imported_at=datetime(2026, 10, 16, tzinfo=UTC),
    )

    apple = next(item for item in report.companies if item.company_key == "apple")
    assert apple.status == "partial" and not apple.imported
    assert "duplicate company entry" in apple.problems
    rows = (
        await db_session.execute(
            select(ResearchDataset).where(ResearchDataset.source == SOURCE_REVIEWED_ANNUAL)
        )
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_import_timestamp_before_checked_on_blocks_company(db_session, tmp_path):
    manifest_path, source_root, _ = _make_package(tmp_path)
    company_ids = await _seed_company_ids(db_session)

    report = await import_reviewed_annual(
        db_session,
        manifest_path=manifest_path,
        source_root=source_root,
        data_dir=tmp_path / "data",
        identities=IDENTITIES,
        company_ids=company_ids,
        imported_at=datetime(2026, 10, 14, tzinfo=UTC),
    )

    assert report.problems and "precedes checked_on" in report.problems[0]
    assert all(not item.imported and item.status == "partial" for item in report.companies)
    rows = (
        await db_session.execute(
            select(ResearchDataset).where(ResearchDataset.source == SOURCE_REVIEWED_ANNUAL)
        )
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_source_changed_between_preparation_and_copy_is_rejected(
    db_session, tmp_path, monkeypatch
):
    manifest_path, source_root, _ = _make_package(tmp_path)
    company_ids = await _seed_company_ids(db_session)
    original_copy = reviewed_import._copy_verified_documents
    changed = False

    def change_apple_source(data_dir, copies):
        nonlocal changed
        for source, _target, _digest in copies:
            if not changed and source.is_relative_to(source_root / "apple"):
                source.write_bytes(source.read_bytes() + b"changed after preparation")
                changed = True
                break
        original_copy(data_dir, copies)

    monkeypatch.setattr(reviewed_import, "_copy_verified_documents", change_apple_source)
    report = await import_reviewed_annual(
        db_session,
        manifest_path=manifest_path,
        source_root=source_root,
        data_dir=tmp_path / "data",
        identities=IDENTITIES,
        company_ids=company_ids,
        imported_at=datetime(2026, 10, 16, tzinfo=UTC),
    )

    apple = next(item for item in report.companies if item.company_key == "apple")
    assert changed and apple.status == "partial" and not apple.imported
    assert any("source changed after verification" in issue for issue in apple.problems)
    rows = (
        await db_session.execute(
            select(ResearchDataset).where(
                ResearchDataset.source == SOURCE_REVIEWED_ANNUAL,
                ResearchDataset.subject_id == company_ids["apple"],
            )
        )
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_tampered_existing_copy_is_not_repaired_or_overwritten(db_session, tmp_path):
    manifest_path, source_root, _ = _make_package(tmp_path)
    company_ids = await _seed_company_ids(db_session)
    data_dir = tmp_path / "data"
    first = await import_reviewed_annual(
        db_session,
        manifest_path=manifest_path,
        source_root=source_root,
        data_dir=data_dir,
        identities=IDENTITIES,
        company_ids=company_ids,
        imported_at=datetime(2026, 10, 16, tzinfo=UTC),
    )
    apple_row = next(
        row
        for row in (await db_session.execute(select(ResearchDataset))).scalars()
        if row.subject_id == company_ids["apple"] and row.source == SOURCE_REVIEWED_ANNUAL
    )
    raw_path = data_dir / apple_row.payload["review"]["documents"][0]["raw_ref"]
    raw_path.write_bytes(b"corrupt immutable raw artifact")
    corrupted_mtime = raw_path.stat().st_mtime_ns

    second = await import_reviewed_annual(
        db_session,
        manifest_path=manifest_path,
        source_root=source_root,
        data_dir=data_dir,
        identities=IDENTITIES,
        company_ids=company_ids,
        imported_at=datetime(2026, 10, 18, tzinfo=UTC),
    )

    apple = next(item for item in second.companies if item.company_key == "apple")
    assert apple.status == "partial" and not apple.imported
    assert any("existing immutable reviewed source has wrong hash" in issue for issue in apple.problems)
    assert raw_path.read_bytes() == b"corrupt immutable raw artifact"
    assert raw_path.stat().st_mtime_ns == corrupted_mtime
    assert first.saved


@pytest.mark.parametrize("dimension", ["scope", "currency"])
def test_same_metric_period_with_incompatible_duplicate_is_rejected(tmp_path, dimension):
    manifest_path, source_root, manifest = _make_package(tmp_path)
    entry = next(row for row in manifest["companies"] if row["company_key"] == "apple")
    duplicate = deepcopy(entry["fields"][0])
    fact = duplicate["fact"]
    if dimension == "scope":
        fact["semantics"]["equity_scope"] = "total"
    else:
        fact["value"]["currency"] = "HKD"
        fact["value"]["unit"] = "HKD"
        fact["value"]["raw_unit"] = "HKD"
    entry["fields"].append(duplicate)
    identity = next(item for item in IDENTITIES if item.company_key == "apple")

    with pytest.raises(ValueError, match="ambiguous duplicate"):
        _company_drafts(
            entry=entry,
            validation={"issues": []},
            identity=identity,
            subject_id=1,
            manifest_sha256="a" * 64,
            checked_on=date(2026, 10, 15),
            source_root=source_root,
            captured_at=datetime(2026, 10, 16, tzinfo=UTC),
        )
