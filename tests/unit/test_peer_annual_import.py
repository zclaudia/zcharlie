"""WP3 peer annual fact snapshot offline import: golden fidelity against the
run-02 evidence, per-field verification-proof traceability, unit discipline,
tamper rejection without partial writes, and idempotent versioning.

All fixtures live in this file (shared conftest helpers stay untouched); the
run-02 evidence files are read-only golden inputs.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pytest_asyncio import fixture as async_fixture
from sqlalchemy import func, select
from typer.testing import CliRunner

from investment_agent.cli.commands.peer_annual import (
    AUDITED_METRICS,
    FIELD_SPECS,
    SAMPLE_METRICS,
    PeerAnnualImportError,
    build_import_plan,
    import_peer_annual_snapshots,
    load_audit_json,
    load_sample_json,
)
from investment_agent.cli.commands.peer_annual import (
    app as peer_annual_app,
)
from investment_agent.evidence.contracts import Period
from investment_agent.industry_peers.contracts import PeerAnnualField
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.peer import PeerAnnualSnapshotRepository
from tests.support.peer_world import AUDIT_PATH, ORIGINALS_ROOT, SAMPLE_PATH
from tests.support.peer_world import (
    requires_originals as requires_originals,
)

ACQUIRED_AT = datetime(2026, 10, 12, 4, 0, tzinfo=UTC)
LATER_ACQUIRED_AT = datetime(2026, 10, 12, 5, 30, tzinfo=UTC)
SAMPLE_KEY_BY_METRIC = {metric: spec.sample_key for metric, spec in FIELD_SPECS.items()}
PROVIDER = "eastmoney"
ADAPTER_VERSION = "offline-run02-v1"
REPORT_PERIOD = date(2025, 12, 31)


# --- loading helpers ---------------------------------------------------------


def dec_json(path: Path) -> Any:
    """Parse keeping decimal precision, like the importer does."""

    return json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)


def raw_tokens(path: Path) -> Any:
    """Parse every number as its exact source text (char-for-char golden)."""

    return json.loads(path.read_text(encoding="utf-8"), parse_float=str, parse_int=str)


def fake_identities(sample: dict[str, Any]) -> dict[str, tuple[int, int]]:
    return {
        entry["code"]: (index + 1, 1000 + index)
        for index, entry in enumerate(sample["companies"])
    }


def plan(sample: dict[str, Any], audit: list[dict[str, Any]], **kwargs: Any) -> list:
    kwargs.setdefault("acquired_at", ACQUIRED_AT)
    kwargs.setdefault("identities", fake_identities(sample))
    return build_import_plan(sample, audit, **kwargs)


def company_by_code(
    rows: list[PeerAnnualSnapshot],
    identities: dict[str, tuple[int, int]],
    code: str,
) -> PeerAnnualSnapshot:
    company_id, _ = identities[code]
    return next(row for row in rows if row.company_id == company_id)


def aware(value: datetime) -> datetime:
    """SQLite drops the offset on retrieval; compare on the UTC instant."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def snapshot_count(session: Any) -> int:
    return (
        await session.execute(select(func.count()).select_from(PeerAnnualSnapshot))
    ).scalar_one()


def code_of(row: PeerAnnualSnapshot, identities: dict[str, tuple[int, int]]) -> str:
    return next(code for code, ids in identities.items() if ids[0] == row.company_id)


# --- fixtures (all local to this file) ---------------------------------------


@async_fixture
async def session(tmp_path):
    """Fresh disk-backed SQLite with every table; one session per test."""

    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path}/peer_annual_import.db")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with session_scope() as session:
            yield session
    finally:
        await engine.dispose()


@async_fixture
async def seeded(session):
    """The 19 run-02 companies resolved in the stock directory."""

    sample = load_sample_json(SAMPLE_PATH)
    repo = ListingRepository(session)
    identities: dict[str, tuple[int, int]] = {}
    for entry in sample["companies"]:
        listing = await repo.ensure_a_share_listing(entry["code"], company_name=entry["name"])
        identities[entry["code"]] = (listing.company_id, listing.id)
    return identities


# --- golden import -----------------------------------------------------------


async def test_golden_import_matches_sample_exactly(
    seeded, session, requires_originals
) -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    report = await import_peer_annual_snapshots(
        session,
        sample=sample,
        audit=audit,
        acquired_at=ACQUIRED_AT,
        originals_root=ORIGINALS_ROOT,
    )
    await session.commit()

    assert (report.companies, report.fields_per_company) == (19, 7)
    assert report.verification_checks == 114
    assert (report.inserted, report.skipped_existing) == (19, 0)
    assert len(set(report.semantic_hashes.values())) == 19

    token_by_code = {entry["code"]: entry for entry in raw_tokens(SAMPLE_PATH)["companies"]}
    rows = list((await session.execute(select(PeerAnnualSnapshot))).scalars())
    assert len(rows) == 19
    for row in rows:
        code = code_of(row, seeded)
        token = token_by_code[code]
        year = row.report_period.year
        for metric in SAMPLE_METRICS:
            spec = FIELD_SPECS[metric]
            stored = row.fields[metric]
            # Decimal strings match the source tokens character for character
            # (the ROE token stores the single /100 conversion).
            if metric == "roe_weighted_annual":
                assert stored["value"] == str(Decimal(token[spec.sample_key]).scaleb(-2))
                assert stored["raw_value"] == token[spec.sample_key]
            else:
                assert stored["value"] == token[spec.sample_key], (code, metric)
                assert stored["raw_value"] == token[spec.sample_key]
            assert isinstance(stored["value"], str)
            assert stored["unit"] == spec.unit
            assert stored["currency"] == spec.currency
            assert stored["source_field"] == spec.api_key
            assert stored["raw_unit"] == spec.raw_unit
            # The Contract normalizes timestamps to UTC on validation.
            assert PeerAnnualField.model_validate(stored).acquired_at == ACQUIRED_AT
            if spec.kind == "flow":
                expected = Period(start=date(year, 1, 1), end=date(year, 12, 31),
                                  basis="period_to_date/annual")
            elif spec.kind == "stock":
                expected = Period(start=REPORT_PERIOD, end=REPORT_PERIOD, basis="point_in_time")
            else:
                expected = Period(start=date(year, 1, 1), end=date(year, 12, 31),
                                  basis="annual_ratio")
            assert Period.model_validate(stored["period"]) == expected
            assert stored["verification"] == ("verified" if spec.audited else "candidate")
            assert (stored["missing_reason"] is None) is (stored["value"] is not None)
            # Every stored entry validates against the WP1 contract.
            PeerAnnualField.model_validate(stored)
        assert not _contains_float(row.fields)
        assert not _contains_float(row.verification)


def _contains_float(payload: Any) -> bool:
    if isinstance(payload, float):
        return True
    if isinstance(payload, dict):
        return any(_contains_float(value) for value in payload.values())
    if isinstance(payload, list):
        return any(_contains_float(value) for value in payload)
    return False


async def test_integer_token_survives_exactly() -> None:
    """000596.SZ operating cash flow is a bare integer token in the source."""

    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    by_code = {entry_plan.code: entry_plan for entry_plan in plan(sample, audit)}
    cash_flow = by_code["000596.SZ"].fields["operating_cash_flow"]
    assert cash_flow.model_dump(mode="json")["value"] == "1947212977"
    assert cash_flow.value == Decimal("1947212977")
    # 贵州茅台 revenue keeps both decimal places exactly.
    revenue = by_code["600519.SH"].fields["operating_revenue"]
    assert revenue.model_dump(mode="json")["value"] == "168838102514.79"


async def test_every_stored_field_round_trips_through_the_wp1_contract() -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    for entry_plan in plan(sample, audit):
        for metric, field in entry_plan.fields.items():
            dumped = field.model_dump(mode="json")
            assert PeerAnnualField.model_validate(dumped) == field, (entry_plan.code, metric)


# --- verification proofs: all 114 checks traceable ---------------------------


async def test_all_114_checks_reproduce_and_trace_to_audit_records(
    seeded, session, requires_originals
) -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    report = await import_peer_annual_snapshots(
        session,
        sample=sample,
        audit=audit,
        acquired_at=ACQUIRED_AT,
        originals_root=ORIGINALS_ROOT,
    )
    await session.commit()
    assert report.verification_checks == 19 * 6 == 114

    audit_dec = dec_json(AUDIT_PATH)
    rows = list((await session.execute(select(PeerAnnualSnapshot))).scalars())
    traced = 0
    for row in rows:
        code = code_of(row, seeded)
        for metric in AUDITED_METRICS:
            proof = row.verification[metric]
            assert proof["status"] == "verified"
            anchor = proof["audit_record"]
            record = audit_dec[anchor["index"]]
            # The anchor points at exactly this company/field check record.
            assert anchor["source"] == AUDIT_PATH.name
            assert record["code"] == code == anchor["code"]
            assert record["field"] == SAMPLE_KEY_BY_METRIC[metric] == anchor["field"]
            assert record["api_key"] == FIELD_SPECS[metric].api_key == anchor["api_key"]
            assert Decimal(anchor["api_value"]) == Decimal(record["api_value"])
            # The stored value is the audited original value (ROE once /100).
            expected = Decimal(record["api_value"])
            if metric == "roe_weighted_annual":
                expected = expected.scaleb(-2)
            assert Decimal(row.fields[metric]["value"]) == expected
            matched = record["matched_row"]
            assert proof["pdf_page"] == matched["pdf_page"]
            assert proof["header"] == matched["alias"]
            assert proof["raw_value"] == matched["raw_number"]
            assert proof["raw_unit"] == matched["unit"]
            assert proof["parsed_value"] == str(Decimal(matched["parsed_first_value"]))
            assert proof["period"] == record["period"] == row.report_period.isoformat()
            assert proof["locator_excerpt"] == matched["excerpt"]
            assert proof["locator_excerpt_sha256"] == hashlib.sha256(
                matched["excerpt"].encode("utf-8")
            ).hexdigest()
            # Missing audit metadata is now proven by the actual stored original.
            for absent in (
                "original_ref", "pdf_hash", "report_identity",
                "accounting_scope", "checked_at", "checked_by", "check_rule_version",
            ):
                assert proof[absent] is not None, (code, metric, absent)
            traced += 1
    assert traced == 114


async def test_revenue_total_check_is_candidate_without_fabricated_proof(seeded, session) -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    await import_peer_annual_snapshots(
        session,
        sample=sample,
        audit=audit,
        acquired_at=ACQUIRED_AT,
        originals_root=ORIGINALS_ROOT,
    )
    await session.commit()
    rows = list((await session.execute(select(PeerAnnualSnapshot))).scalars())
    assert len(rows) == 19
    for row in rows:
        proof = row.verification["revenue_total_check"]
        assert proof["status"] == "candidate"
        assert proof["audit_record"] is None
        assert "check-only" in proof["note"]
        assert proof["pdf_hash"] is None and proof["checked_at"] is None
        # It keeps the source value but is never marked verified.
        assert Decimal(proof["parsed_value"]) == Decimal(
            row.fields["revenue_total_check"]["value"]
        )


async def test_moutai_golden_roe_fraction(seeded, session) -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    await import_peer_annual_snapshots(
        session,
        sample=sample,
        audit=audit,
        acquired_at=ACQUIRED_AT,
        originals_root=ORIGINALS_ROOT,
    )
    await session.commit()
    rows = list((await session.execute(select(PeerAnnualSnapshot))).scalars())
    roe = company_by_code(rows, seeded, "600519.SH").fields["roe_weighted_annual"]
    assert roe["raw_value"] == "32.53"  # original percent preserved
    assert roe["raw_unit"] == "percent"
    assert roe["value"] == "0.3253"  # divided by 100 exactly once
    assert roe["unit"] == "fraction" and roe["currency"] is None


async def test_roe_conversion_is_exactly_one_division_for_all_companies(seeded, session) -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    await import_peer_annual_snapshots(
        session,
        sample=sample,
        audit=audit,
        acquired_at=ACQUIRED_AT,
        originals_root=ORIGINALS_ROOT,
    )
    await session.commit()
    tokens = {e["code"]: e for e in raw_tokens(SAMPLE_PATH)["companies"]}
    rows = list((await session.execute(select(PeerAnnualSnapshot))).scalars())
    assert len(rows) == 19
    for row in rows:
        percent_token = tokens[code_of(row, seeded)]["加权平均ROE"]
        roe = row.fields["roe_weighted_annual"]
        assert roe["raw_value"] == percent_token
        assert Decimal(roe["value"]) == Decimal(percent_token) / Decimal(100)
        # A second /100 would be visible: no stored fraction equals percent/10000.
        assert Decimal(roe["value"]) != Decimal(percent_token) / Decimal(10000)


# --- zeros, missing values, precision ---------------------------------------


def _mini_sample_and_audit(*, audited_null: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One synthetic company with a zero, a null and a 30-digit decimal.

    ``audited_null=True`` moves the null onto an audited metric to exercise the
    binding failure; by default the null is the unaudited check-only field.
    """

    code = "000999.SZ"
    values: dict[str, Decimal | None] = {
        "营业收入": Decimal("1234567890123456789012345678.90"),
        "营业总收入": None if not audited_null else Decimal("1234567890123456789012345679.00"),
        "归母净利润": Decimal("1.00"),
        "归母净资产": Decimal("10.00"),
        "合同负债": Decimal(0),  # original zero: a value, never a missing
        "经营现金流": None if audited_null else Decimal("2.00"),
        "加权平均ROE": Decimal("5.00"),
    }
    # The audit always records real original numbers; with ``audited_null`` the
    # sample value is null while the original records a value (binding failure).
    audit_values = {key: value if value is not None else Decimal("2.00") for key, value in values.items()}
    sample = {
        "period": "2025-12-31",
        "companies": [
            {"code": code, "name": "测试酒业", "period": "2025-12-31", **values}
        ],
    }
    audit = []
    for spec in FIELD_SPECS.values():
        if not spec.audited:
            continue
        value = audit_values[spec.sample_key]
        audit.append(
            {
                "code": code,
                "period": "2025-12-31",
                "field": spec.sample_key,
                "api_key": spec.api_key,
                "api_value": value,
                "status": "matched",
                "matched_row": {
                    "pdf_page": 4,
                    "alias": spec.sample_key,
                    "parsed_first_value": value,
                    "raw_number": f"{value:,}",
                    "excerpt": "关键会计数据 …… （合并）",
                    "scale": 1,
                    "unit": spec.raw_unit,
                },
                "candidates": [],
            }
        )
    return sample, audit


REVENUE_CHECK_REASON = {("000999.SZ", "revenue_total_check"): "source column absent"}


def test_zero_kept_as_value_and_missing_explicit() -> None:
    sample, audit = _mini_sample_and_audit()
    entry_plan = plan(sample, audit, missing_reasons=REVENUE_CHECK_REASON)[0]

    zero = entry_plan.fields["contract_liabilities"]
    assert zero.value == Decimal(0) and zero.missing_reason is None
    assert zero.model_dump(mode="json")["value"] == "0"
    missing = entry_plan.fields["revenue_total_check"]
    assert missing.value is None
    assert missing.missing_reason == "source column absent"
    assert missing.verification == "candidate"
    assert entry_plan.fields["operating_cash_flow"].value == Decimal("2.00")


def test_long_decimal_precision_round_trips() -> None:
    sample, audit = _mini_sample_and_audit()
    entry_plan = plan(sample, audit, missing_reasons=REVENUE_CHECK_REASON)[0]
    text = "1234567890123456789012345678.90"
    field = entry_plan.fields["operating_revenue"]
    dumped = field.model_dump(mode="json")
    assert dumped["value"] == text  # char-for-char, no float detour
    assert field.value == Decimal(text)
    assert PeerAnnualField.model_validate(dumped) == field
    # And the audit binding holds numerically at full precision.
    assert Decimal(dumped["value"]) == Decimal(text)


def test_missing_reason_is_required_for_null_values() -> None:
    sample, audit = _mini_sample_and_audit()
    with pytest.raises(PeerAnnualImportError) as excinfo:
        plan(sample, audit)
    assert any(
        "000999.SZ field revenue_total_check is null and no missing reason" in problem
        for problem in excinfo.value.problems
    )


def test_null_on_audited_metric_cannot_bind_to_an_original() -> None:
    sample, audit = _mini_sample_and_audit(audited_null=True)
    reasons = {("000999.SZ", "operating_cash_flow"): "missing upstream"}
    with pytest.raises(PeerAnnualImportError) as excinfo:
        plan(sample, audit, missing_reasons=reasons)
    assert any(
        "sample value is null but the original records" in problem
        for problem in excinfo.value.problems
    )


# --- tamper scenarios: rejected, no partial writes ---------------------------


async def test_tampered_audit_value_rejected_even_with_matched_status(seeded, session) -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = dec_json(AUDIT_PATH)
    # Status still claims "matched"; the value no longer matches the sample.
    audit[0]["api_value"] = audit[0]["api_value"] + Decimal("1.00")
    with pytest.raises(PeerAnnualImportError) as excinfo:
        await import_peer_annual_snapshots(
            session,
            sample=sample,
            audit=audit,
            acquired_at=ACQUIRED_AT,
            originals_root=ORIGINALS_ROOT,
        )
    assert any("does not match the audited original value" in p for p in excinfo.value.problems)
    assert await snapshot_count(session) == 0  # nothing was written


async def test_tampered_sample_value_rejected(seeded, session) -> None:
    sample = dec_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    sample["companies"][0]["营业收入"] = sample["companies"][0]["营业收入"] + Decimal("0.01")
    with pytest.raises(PeerAnnualImportError) as excinfo:
        await import_peer_annual_snapshots(
            session,
            sample=sample,
            audit=audit,
            acquired_at=ACQUIRED_AT,
            originals_root=ORIGINALS_ROOT,
        )
    assert any("does not match the audited original value" in p for p in excinfo.value.problems)
    assert await snapshot_count(session) == 0


async def test_missing_audit_record_rejects_whole_batch(seeded, session) -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    del audit[3]
    with pytest.raises(PeerAnnualImportError) as excinfo:
        await import_peer_annual_snapshots(
            session,
            sample=sample,
            audit=audit,
            acquired_at=ACQUIRED_AT,
            originals_root=ORIGINALS_ROOT,
        )
    assert "113/114" in str(excinfo.value)
    assert await snapshot_count(session) == 0


async def test_extra_audit_record_for_unknown_company_rejected(seeded, session) -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    extra = dict(audit[0])
    extra["code"] = "999999.SZ"
    audit.append(extra)
    with pytest.raises(PeerAnnualImportError) as excinfo:
        await import_peer_annual_snapshots(
            session,
            sample=sample,
            audit=audit,
            acquired_at=ACQUIRED_AT,
            originals_root=ORIGINALS_ROOT,
        )
    assert any("999999.SZ" in p and "not in the sample" in p for p in excinfo.value.problems)
    assert await snapshot_count(session) == 0


async def test_unit_mismatch_rejected(seeded, session) -> None:
    sample = load_sample_json(SAMPLE_PATH)

    audit = load_audit_json(AUDIT_PATH)
    next(r for r in audit if r["field"] == "加权平均ROE")["matched_row"]["unit"] = "CNY"
    with pytest.raises(PeerAnnualImportError) as excinfo:
        await import_peer_annual_snapshots(
            session,
            sample=sample,
            audit=audit,
            acquired_at=ACQUIRED_AT,
            originals_root=ORIGINALS_ROOT,
        )
    assert any(
        "unit 'CNY' does not match the expected 'percent'" in p for p in excinfo.value.problems
    )

    audit2 = load_audit_json(AUDIT_PATH)
    next(r for r in audit2 if r["field"] == "营业收入")["matched_row"]["unit"] = "percent"
    with pytest.raises(PeerAnnualImportError) as excinfo:
        await import_peer_annual_snapshots(
            session,
            sample=sample,
            audit=audit2,
            acquired_at=ACQUIRED_AT,
            originals_root=ORIGINALS_ROOT,
        )
    assert any(
        "unit 'percent' does not match the expected 'CNY'" in p for p in excinfo.value.problems
    )
    assert await snapshot_count(session) == 0


async def test_row_that_does_not_reproduce_its_raw_number_rejected() -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    next(r for r in audit if r["field"] == "营业收入")["matched_row"]["raw_number"] = (
        "9,999,999,999.99"
    )
    with pytest.raises(PeerAnnualImportError) as excinfo:
        plan(sample, audit)
    assert any("does not reproduce parsed_first_value" in p for p in excinfo.value.problems)


def _load_nan_sample() -> dict[str, Any]:
    """A sample whose first 营业收入 token is NaN, via the importer's loader."""

    text = SAMPLE_PATH.read_text(encoding="utf-8").replace("25731010647.32", "NaN", 1)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        handle.write(text)
        path = Path(handle.name)
    try:
        return load_sample_json(path)
    finally:
        path.unlink()


def test_boolean_nan_and_string_values_rejected_with_field_named() -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)

    boolean_sample = {
        **sample,
        "companies": [
            {**sample["companies"][0], "营业收入": True},
            *sample["companies"][1:],
        ],
    }
    with pytest.raises(PeerAnnualImportError) as excinfo:
        plan(boolean_sample, audit)
    assert any(
        "sample company 000568.SZ field operating_revenue: boolean values are rejected" in p
        for p in excinfo.value.problems
    )

    nan_sample = _load_nan_sample()
    with pytest.raises(PeerAnnualImportError) as excinfo:
        plan(nan_sample, audit)
    assert any(
        "field operating_revenue: NaN is not a finite number and is rejected" in p
        for p in excinfo.value.problems
    )

    string_sample = {
        **sample,
        "companies": [
            {**sample["companies"][0], "合同负债": "3,367,443,727.83"},
            *sample["companies"][1:],
        ],
    }
    with pytest.raises(PeerAnnualImportError) as excinfo:
        plan(string_sample, audit)
    assert any(
        "sample company 000568.SZ field contract_liabilities: expected a JSON number" in p
        for p in excinfo.value.problems
    )


async def test_unresolvable_code_rejects_whole_batch(session) -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    # No listing seeded: 000568.SZ cannot be resolved.
    with pytest.raises(ValueError) as excinfo:
        await import_peer_annual_snapshots(
            session,
            sample=sample,
            audit=audit,
            acquired_at=ACQUIRED_AT,
            originals_root=ORIGINALS_ROOT,
        )
    message = str(excinfo.value)
    assert "000568.SZ" in message and "whole batch rejected" in message
    assert await snapshot_count(session) == 0


# --- idempotency and versioning ---------------------------------------------


async def test_reimport_is_idempotent_and_import_time_stays_out_of_the_hash(
    seeded, session
) -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    first = await import_peer_annual_snapshots(
        session,
        sample=sample,
        audit=audit,
        acquired_at=ACQUIRED_AT,
        originals_root=ORIGINALS_ROOT,
    )
    await session.commit()
    # A later re-run (different import time) must not add rows or double-divide.
    second = await import_peer_annual_snapshots(
        session,
        sample=sample,
        audit=audit,
        acquired_at=LATER_ACQUIRED_AT,
        originals_root=ORIGINALS_ROOT,
    )
    await session.commit()
    assert (first.inserted, first.skipped_existing) == (19, 0)
    assert (second.inserted, second.skipped_existing) == (0, 19)
    assert second.semantic_hashes == first.semantic_hashes
    assert await snapshot_count(session) == 19
    moutai = company_by_code(
        list((await session.execute(select(PeerAnnualSnapshot))).scalars()), seeded, "600519.SH"
    )
    assert moutai.fields["roe_weighted_annual"]["value"] == "0.3253"
    assert aware(moutai.captured_at) == ACQUIRED_AT  # first save time never moves


async def test_value_change_appends_new_version_and_keeps_the_old_readable(
    seeded, session
) -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    first = await import_peer_annual_snapshots(
        session, sample=sample, audit=audit, acquired_at=ACQUIRED_AT, originals_root=None
    )
    await session.commit()

    v2_sample = dec_json(SAMPLE_PATH)
    v2_audit = dec_json(AUDIT_PATH)
    new_value = Decimal("168838102514.80")
    next(c for c in v2_sample["companies"] if c["code"] == "600519.SH")["营业收入"] = new_value
    record = next(
        r for r in v2_audit if r["code"] == "600519.SH" and r["field"] == "营业收入"
    )
    record["api_value"] = new_value
    record["matched_row"]["parsed_first_value"] = new_value
    record["matched_row"]["raw_number"] = "168,838,102,514.80"
    second = await import_peer_annual_snapshots(
        session, sample=v2_sample, audit=v2_audit, acquired_at=LATER_ACQUIRED_AT, originals_root=None
    )
    await session.commit()
    assert (second.inserted, second.skipped_existing) == (1, 18)

    company_id, _ = seeded["600519.SH"]
    repo = PeerAnnualSnapshotRepository(session)
    versions = await repo.versions(company_id=company_id, report_period=REPORT_PERIOD)
    assert len(versions) == 2
    old_hash = first.semantic_hashes["600519.SH"]
    new_hash = second.semantic_hashes["600519.SH"]
    assert old_hash != new_hash
    old_row = await repo.by_unique_key(
        company_id=company_id, report_period=REPORT_PERIOD,
        provider=PROVIDER, adapter_version=ADAPTER_VERSION, semantic_hash=old_hash,
    )
    new_row = await repo.by_unique_key(
        company_id=company_id, report_period=REPORT_PERIOD,
        provider=PROVIDER, adapter_version=ADAPTER_VERSION, semantic_hash=new_hash,
    )
    assert old_row is not None and new_row is not None
    assert old_row.fields["operating_revenue"]["value"] == "168838102514.79"
    assert new_row.fields["operating_revenue"]["value"] == "168838102514.80"
    assert aware(new_row.captured_at) == LATER_ACQUIRED_AT
    assert aware(old_row.captured_at) == ACQUIRED_AT
    assert await snapshot_count(session) == 20


async def test_proof_change_alone_appends_new_version(seeded, session) -> None:
    sample = load_sample_json(SAMPLE_PATH)
    audit = load_audit_json(AUDIT_PATH)
    first = await import_peer_annual_snapshots(
        session, sample=sample, audit=audit, acquired_at=ACQUIRED_AT, originals_root=None
    )
    await session.commit()

    v2_audit = dec_json(AUDIT_PATH)
    # Only the recorded header alias changes; all values stay identical, so the
    # verification proof - and therefore the semantic version - changes.
    next(
        r for r in v2_audit if r["code"] == "600809.SH" and r["field"] == "归母净资产"
    )["matched_row"]["alias"] = "归属于上市公司股东的净资产（更正后）"
    second = await import_peer_annual_snapshots(
        session, sample=sample, audit=v2_audit, acquired_at=LATER_ACQUIRED_AT, originals_root=None
    )
    await session.commit()
    assert (second.inserted, second.skipped_existing) == (1, 18)
    assert second.semantic_hashes["600809.SH"] != first.semantic_hashes["600809.SH"]
    assert await snapshot_count(session) == 20


# --- CLI surface -------------------------------------------------------------


def test_cli_app_exposes_the_import_command() -> None:
    result = CliRunner().invoke(peer_annual_app, ["--help"])
    assert result.exit_code == 0
    assert "import" in result.output
