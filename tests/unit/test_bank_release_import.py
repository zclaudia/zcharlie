"""Guard the explicitly audited bank-annual import path and preserve legacy default."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from investment_agent.cli.commands import bank_annual
from investment_agent.industry_peers.metric_packs import BANK_PACK
from investment_agent.providers.eastmoney_bank_annual import BANK_ADAPTER_VERSION
from investment_agent.storage.models.peer import PeerCohortSnapshot
from investment_agent.storage.repositories.listing import ListingRepository

EVIDENCE = Path(__file__).parents[1] / "fixtures/bank_run02"
COHORT = "bank_run02_sample"
AUDIT = Path(__file__).parents[1] / "fixtures/bank_proofs_v2/audit-manifest.json"


def test_public_cli_exposes_opt_in_and_rejects_missing_audited_originals():
    from investment_agent.cli.main import app

    runner = CliRunner()
    help_result = runner.invoke(app, ["peer-annual", "import-bank", "--help"])
    assert help_result.exit_code == 0
    assert "--bank-annual-source" in help_result.output
    assert "--cohort-key" in help_result.output

    rejected = runner.invoke(
        app,
        [
            "peer-annual",
            "import-bank",
            "--evidence-root",
            str(EVIDENCE),
            "--report-period",
            "2025-12-31",
            "--bank-annual-source",
            "--cohort-key",
            COHORT,
            "--audit-path",
            str(AUDIT),
        ],
    )
    assert rejected.exit_code == 1
    assert "requires both --audit-path and --originals-root" in rejected.output


async def _save_cohort(
    session, *, scope: bool, metric_pack: str = BANK_PACK, member_count: int = 10
):
    listings = ListingRepository(session)
    saved = [
        await listings.ensure_a_share_listing(code, company_name=code)
        for code in bank_annual.BANK_CODES
    ]
    payload = {
        "metric_pack": metric_pack,
        "entries": [{"listing_id": listing.id} for listing in saved[:member_count]],
    }
    if scope:
        payload["read_scope"] = {
            "provider": "eastmoney",
            "adapter_version": BANK_ADAPTER_VERSION,
        }
    row = PeerCohortSnapshot(
        id=f"cohort-{scope}-{metric_pack}-{member_count}",
        cohort_key=COHORT,
        revision=1,
        observed_on=date(2026, 10, 17),
        acquired_at=datetime(2026, 10, 17, tzinfo=UTC),
        verification="candidate",
        inclusion_rule_version="test",
        content_hash="a" * 64,
        payload=payload,
    )
    session.add(row)
    await session.flush()


@pytest.mark.asyncio
async def test_bank_annual_source_requires_both_audit_roots(db_session, tmp_path):
    with pytest.raises(ValueError, match="requires both --audit-path and --originals-root"):
        await bank_annual.import_bank_annual_source_snapshots(
            db_session,
            cohort_key=COHORT,
            evidence_root=EVIDENCE,
            report_period=date(2025, 12, 31),
            acquired_at=datetime.now(UTC),
            audit_path=tmp_path / "audit.json",
            originals_root=None,
        )


@pytest.mark.asyncio
async def test_bank_annual_source_rejects_latest_unscoped_cohort(db_session, tmp_path):
    await _save_cohort(db_session, scope=True)
    # A later immutable revision without the explicit read scope is not eligible.
    cohort = await db_session.get(PeerCohortSnapshot, "cohort-True-bank_annual_v1-10")
    db_session.add(
        PeerCohortSnapshot(
            id="latest-unscoped",
            cohort_key=COHORT,
            revision=2,
            observed_on=cohort.observed_on,
            acquired_at=cohort.acquired_at,
            verification=cohort.verification,
            inclusion_rule_version=cohort.inclusion_rule_version,
            content_hash="b" * 64,
            payload={"metric_pack": BANK_PACK, "entries": cohort.payload["entries"]},
        )
    )
    await db_session.flush()
    with pytest.raises(ValueError, match="bank annual read scope"):
        await bank_annual.import_bank_annual_source_snapshots(
            db_session,
            cohort_key=COHORT,
            evidence_root=EVIDENCE,
            report_period=date(2025, 12, 31),
            acquired_at=datetime.now(UTC),
            audit_path=tmp_path / "audit.json",
            originals_root=tmp_path,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "metric_pack", "member_count", "error"),
    [
        (False, BANK_PACK, 10, "bank annual read scope"),
        (True, "baijiu_annual_v1", 10, "bank annual metric pack"),
        (True, BANK_PACK, 9, "not a saved member"),
    ],
)
async def test_bank_annual_source_rejects_bad_cohort_before_import(
    db_session, tmp_path, monkeypatch, scope, metric_pack, member_count, error
):
    await _save_cohort(db_session, scope=scope, metric_pack=metric_pack, member_count=member_count)

    async def should_not_import(*_args, **_kwargs):
        raise AssertionError("cohort checks must precede importer")

    monkeypatch.setattr(bank_annual, "import_bank_annual_snapshots", should_not_import)
    with pytest.raises(ValueError, match=error):
        await bank_annual.import_bank_annual_source_snapshots(
            db_session,
            cohort_key=COHORT,
            evidence_root=EVIDENCE,
            report_period=date(2025, 12, 31),
            acquired_at=datetime.now(UTC),
            audit_path=tmp_path / "audit.json",
            originals_root=tmp_path,
        )


@pytest.mark.asyncio
async def test_bank_annual_source_uses_fixed_adapter_and_default_stays_legacy(
    db_session, tmp_path, monkeypatch
):
    await _save_cohort(db_session, scope=True)
    captured = {}

    async def importer(session, **kwargs):
        captured.update(kwargs)
        return {"inserted": 10}

    monkeypatch.setattr(bank_annual, "import_bank_annual_snapshots", importer)
    result = await bank_annual.import_bank_annual_source_snapshots(
        db_session,
        cohort_key=COHORT,
        evidence_root=EVIDENCE,
        report_period=date(2025, 12, 31),
        acquired_at=datetime.now(UTC),
        audit_path=tmp_path / "audit.json",
        originals_root=tmp_path,
    )
    assert result == {"inserted": 10}
    assert captured["provider"] == "eastmoney"
    assert captured["adapter_version"] == BANK_ADAPTER_VERSION
    assert captured["audit_path"] == tmp_path / "audit.json"
    assert bank_annual.ADAPTER_VERSION == "offline-run02-v1"
