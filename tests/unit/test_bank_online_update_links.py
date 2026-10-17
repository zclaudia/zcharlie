"""Trust-boundary tests for linking bank online revisions to a checked original."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from investment_agent.evidence.contracts import Period
from investment_agent.industry_peers.contracts import PeerAnnualField, PeerFieldVerification
from investment_agent.industry_peers.evidence import peer_field_states
from investment_agent.providers.eastmoney_bank_annual import BANK_ADAPTER_VERSION
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.peer import PeerAnnualSnapshotRepository

PERIOD_END = date(2025, 12, 31)
CAPTURE_OLD = datetime(2026, 4, 1, tzinfo=UTC)
CAPTURE_NEW = datetime(2026, 4, 2, tzinfo=UTC)
METRIC = "npl_ratio"
ORIGINAL_REF = {"kind": "disclosure_original", "id": str(uuid4()), "revision": 1}
PDF_HASH = "b" * 64


def _field(value: str, verification: PeerFieldVerification) -> PeerAnnualField:
    return PeerAnnualField(
        metric=METRIC,
        value=Decimal(value),
        unit="fraction",
        currency=None,
        period=Period(start=date(2025, 1, 1), end=PERIOD_END, basis="period_to_date/annual"),
        source_field="NONPERLOAN",
        raw_value=str(Decimal(value) * 100),
        raw_unit="percent",
        disclosed_on=date(2026, 3, 28),
        acquired_at=CAPTURE_OLD if verification is PeerFieldVerification.VERIFIED else CAPTURE_NEW,
        verification=verification,
    )


def _baseline_proof() -> dict[str, object]:
    return {
        "metric": METRIC,
        "check_rule_version": "bank-annual-original/1",
        "original_ref": ORIGINAL_REF,
        "pdf_hash": PDF_HASH,
        "accounting_scope": "group_consolidated",
        "parsed_value": "0.0100",
        "period": PERIOD_END.isoformat(),
        "checked_at": "2026-04-01T00:00:00+00:00",
    }


def _comparison(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "status": "mismatch",
        "metric": METRIC,
        "period": PERIOD_END.isoformat(),
        "accounting_scope": "group_consolidated",
        "original_ref": ORIGINAL_REF,
        "pdf_hash": PDF_HASH,
        "old_value": "0.0100",
        "new_value": "0.0110",
    }
    value.update(changes)
    return value


async def _save(
    session,
    *,
    company_id: int,
    listing_id: int,
    captured_at: datetime,
    value: str,
    verification: PeerFieldVerification,
    proof: dict[str, object],
    provider: str = "eastmoney",
    adapter_version: str = BANK_ADAPTER_VERSION,
) -> PeerAnnualSnapshot:
    row = PeerAnnualSnapshot(
        id=str(uuid4()),
        company_id=company_id,
        listing_id=listing_id,
        report_period=PERIOD_END,
        provider=provider,
        adapter_version=adapter_version,
        captured_at=captured_at,
        content_hash=uuid4().hex * 2,
        semantic_hash=uuid4().hex * 2,
        fields={METRIC: _field(value, verification).model_dump(mode="json")},
        verification={METRIC: proof},
    )
    return await PeerAnnualSnapshotRepository(session).add(row)


async def _paired_rows(
    session,
    listing,
    *,
    comparison: dict[str, object] | None = None,
    old_listing_id: int | None = None,
    provider: str = "eastmoney",
    adapter_version: str = BANK_ADAPTER_VERSION,
):
    old = await _save(
        session,
        company_id=listing.company_id,
        listing_id=old_listing_id or listing.id,
        captured_at=CAPTURE_OLD,
        value="0.0100",
        verification=PeerFieldVerification.VERIFIED,
        proof=_baseline_proof(),
    )
    new_proof: dict[str, object] = {"original_comparison": comparison or _comparison()}
    new = await _save(
        session,
        company_id=listing.company_id,
        listing_id=listing.id,
        captured_at=CAPTURE_NEW,
        value="0.0110",
        verification=PeerFieldVerification.CANDIDATE,
        proof=new_proof,
        provider=provider,
        adapter_version=adapter_version,
    )
    return old, new


@pytest.mark.asyncio
async def test_valid_link_marks_both_versions_pending_but_keeps_new_scope_unknown(
    db_session, monkeypatch
) -> None:
    listing = await ListingRepository(db_session).ensure_a_share_listing(
        "601939.SH", company_name="建设银行"
    )
    old, new = await _paired_rows(db_session, listing)
    readbacks: list[str] = []

    async def valid_readback(_session, snapshot, metric, *, cutoff=None):
        readbacks.append(snapshot.id)
        assert metric == METRIC
        assert cutoff is None
        return True

    monkeypatch.setattr(
        "investment_agent.industry_peers.originals.validate_field_original", valid_readback
    )
    report = await peer_field_states(
        db_session,
        company_id=listing.company_id,
        metric=METRIC,
        accounting_scope="group_consolidated",
    )

    assert report.status == "pending_recheck"
    assert {version.source_ref.id for version in report.versions} == {UUID(old.id), UUID(new.id)}
    assert {version.state for version in report.versions} == {"pending_recheck"}
    current = next(version for version in report.versions if str(version.source_ref.id) == new.id)
    assert (
        current.accounting_scope is None
    )  # comparison link admits it; proof does not establish scope
    assert readbacks == [old.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_comparison",
    [
        {"original_ref": {"kind": "disclosure_original", "id": str(uuid4()), "revision": 1}},
        {"pdf_hash": "c" * 64},
        {"metric": "roe_weighted_annual"},
        {"period": "2024-12-31"},
        {"old_value": "0.0200"},
        {"new_value": "0.0200"},
        {"accounting_scope": "parent_only"},
    ],
)
async def test_malformed_link_markers_do_not_expand_scope(
    db_session, monkeypatch, bad_comparison
) -> None:
    listing = await ListingRepository(db_session).ensure_a_share_listing(
        "601939.SH", company_name="建设银行"
    )
    await _paired_rows(db_session, listing, comparison=_comparison(**bad_comparison))
    calls = 0

    async def valid_readback(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(
        "investment_agent.industry_peers.originals.validate_field_original", valid_readback
    )
    report = await peer_field_states(
        db_session,
        company_id=listing.company_id,
        metric=METRIC,
        accounting_scope="group_consolidated",
    )
    # Only the independently scoped baseline remains visible. The marker does
    # not make the supplier value look consolidated.
    assert len(report.versions) == 1
    assert report.versions[0].accounting_scope == "group_consolidated"
    assert report.versions[0].state == "consistent"
    assert calls == 0


@pytest.mark.asyncio
async def test_wrong_listing_and_failed_byte_readback_do_not_link(db_session, monkeypatch) -> None:
    listing = await ListingRepository(db_session).ensure_a_share_listing(
        "601939.SH", company_name="建设银行"
    )
    other = await ListingRepository(db_session).ensure_a_share_listing(
        "601288.SH", company_name="农业银行"
    )
    await _paired_rows(db_session, listing, old_listing_id=other.id)

    async def false_readback(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        "investment_agent.industry_peers.originals.validate_field_original", false_readback
    )
    report = await peer_field_states(
        db_session,
        company_id=listing.company_id,
        metric=METRIC,
        accounting_scope="group_consolidated",
    )
    assert len(report.versions) == 1
    assert report.versions[0].state == "consistent"


@pytest.mark.asyncio
async def test_failed_original_bytes_validator_does_not_link(db_session, monkeypatch) -> None:
    listing = await ListingRepository(db_session).ensure_a_share_listing(
        "601939.SH", company_name="建设银行"
    )
    await _paired_rows(db_session, listing)
    calls: list[str] = []

    async def false_readback(_session, snapshot, metric, *, cutoff=None):
        calls.append(snapshot.id)
        assert metric == METRIC and cutoff is None
        return False

    monkeypatch.setattr(
        "investment_agent.industry_peers.originals.validate_field_original", false_readback
    )
    report = await peer_field_states(
        db_session,
        company_id=listing.company_id,
        metric=METRIC,
        accounting_scope="group_consolidated",
    )
    assert calls and len(report.versions) == 1
    assert report.versions[0].state == "consistent"


@pytest.mark.asyncio
async def test_cutoff_excludes_future_revision_before_link_validation(
    db_session, monkeypatch
) -> None:
    listing = await ListingRepository(db_session).ensure_a_share_listing(
        "601939.SH", company_name="建设银行"
    )
    await _paired_rows(db_session, listing)
    calls = 0

    async def valid_readback(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(
        "investment_agent.industry_peers.originals.validate_field_original", valid_readback
    )
    report = await peer_field_states(
        db_session,
        company_id=listing.company_id,
        metric=METRIC,
        cutoff=CAPTURE_NEW - timedelta(microseconds=1),
        accounting_scope="group_consolidated",
    )
    assert len(report.versions) == 1
    assert report.versions[0].state == "consistent"
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "adapter"),
    [("eastmoney", "baijiu-annual-v1"), ("akshare", BANK_ADAPTER_VERSION)],
)
async def test_non_bank_adapter_versions_never_use_bank_update_link(
    db_session, monkeypatch, provider, adapter
) -> None:
    listing = await ListingRepository(db_session).ensure_a_share_listing(
        "601939.SH", company_name="建设银行"
    )
    await _paired_rows(db_session, listing, provider=provider, adapter_version=adapter)
    calls = 0

    async def valid_readback(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return True

    monkeypatch.setattr(
        "investment_agent.industry_peers.originals.validate_field_original", valid_readback
    )
    report = await peer_field_states(
        db_session,
        company_id=listing.company_id,
        metric=METRIC,
        accounting_scope="group_consolidated",
    )
    assert len(report.versions) == 1
    assert calls == 0
