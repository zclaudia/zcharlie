"""Online bank reconciliation against the real FY2025 filing originals."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest

from tests.integration.test_bank_verified_import import (
    AUDIT_PATH,
    BANK_EVIDENCE,
    PERIOD,
    _import_cutoff_after_audit,
)
from tests.integration.test_bank_verified_import import (
    bank_world as bank_world,
)
from tests.integration.test_bank_verified_import import (
    requires_bank_originals as requires_bank_originals,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_real_online_bank_fields_reconcile_to_stored_annual_originals(
    bank_world, requires_bank_originals
):
    from uuid import uuid4

    from sqlalchemy import select

    from investment_agent.cli.commands.bank_annual import BANK_CODES, import_bank_annual_snapshots
    from investment_agent.industry_peers.bank_online_reconciliation import (
        reconcile_bank_online_fields,
    )
    from investment_agent.industry_peers.jobs import BANK_PACK, build_snapshot_fields
    from investment_agent.providers.eastmoney_annual import RawAnnualResponse
    from investment_agent.providers.eastmoney_bank_annual import EastmoneyBankAnnualCollector
    from investment_agent.storage.database import session_scope
    from investment_agent.storage.models.disclosure import DisclosureBlob, DisclosureOriginal
    from investment_agent.storage.models.peer import PeerAnnualSnapshot
    from investment_agent.storage.repositories.listing import ListingRepository

    class FixtureSource:
        def __init__(self, body: bytes):
            self.body = body

        async def fetch(self, _url, _params):
            return RawAnnualResponse(status=200, body=self.body)

    captured = _import_cutoff_after_audit()
    async with session_scope() as session:
        listings = ListingRepository(session)
        for code in BANK_CODES:
            await listings.ensure_a_share_listing(code, company_name=code)
        await import_bank_annual_snapshots(
            session,
            evidence_root=BANK_EVIDENCE,
            report_period=PERIOD,
            acquired_at=captured,
            audit_path=AUDIT_PATH,
            originals_root=ROOT
            / "research/industry-data-validation/evidence/bank-proof-2026-10-15",
        )

    successful = 0
    pingan_missing = 0
    async with session_scope() as session:
        listings = ListingRepository(session)
        for ticker in BANK_CODES:
            source_payload = json.loads(
                (BANK_EVIDENCE / "raw/financial" / f"{ticker}_main.json").read_text()
            )
            matching = [
                row
                for row in source_payload["result"]["data"]
                if row["REPORT_DATE"][:10] == PERIOD.isoformat()
            ]
            assert len(matching) == 1
            payload = {
                "success": True,
                "version": source_payload.get("version"),
                "result": {"pages": 1, "data": [copy.deepcopy(matching[0])]},
            }
            # The provider's shipped fixture supplies both the bank ORG_TYPE
            # classification and notice/update source metadata.
            assert payload["result"]["data"][0].get("ORG_TYPE") in {"银行", "银行业"}
            collector = EastmoneyBankAnnualCollector(
                FixtureSource(json.dumps(payload, ensure_ascii=False).encode()),
                min_interval_seconds=0,
                clock=lambda: captured,
            )
            collection = await collector.collect_company_annual(ticker, PERIOD)
            fields, proofs = build_snapshot_fields(collection, metric_pack=BANK_PACK)
            listing = await listings.get_a_share_listing(ticker)
            reconciled_fields, reconciled_proofs, summary = await reconcile_bank_online_fields(
                session,
                listing_id=listing.id,
                company_id=listing.company_id,
                report_period=PERIOD,
                fields=fields,
                proofs=proofs,
                captured_at=captured,
            )
            for metric in (
                "roe_weighted_annual",
                "net_interest_margin",
                "npl_ratio",
                "provision_coverage",
            ):
                if ticker == "000001.SZ" and metric in {"npl_ratio", "provision_coverage"}:
                    assert reconciled_fields[metric].value is None
                    assert (
                        reconciled_proofs[metric]["reconciliation"]["status"]
                        == "scope_exclusion_revalidated"
                    )
                    pingan_missing += 1
                else:
                    assert reconciled_fields[metric].verification == "verified", (
                        ticker,
                        metric,
                        summary,
                    )
                    successful += 1
            equity = reconciled_fields["equity_parent"]
            assert equity.value is not None and equity.verification == "verified"
            assert (
                reconciled_proofs["equity_parent"]["reconciliation"]["status"]
                == "annual_report_equity_reused"
            )
            assert reconciled_proofs["equity_parent"]["supplier_comparison"]["missing_reason"]
            successful += 1
    assert successful == 48  # 40 ratios minus Ping An's two exclusions, plus 10 annual equities
    assert pingan_missing == 2

    # A changed supplier value remains a candidate even though an annual value
    # exists and the original itself still passes readback.
    async with session_scope() as session:
        listings = ListingRepository(session)
        ticker = "601398.SH"
        payload = json.loads((BANK_EVIDENCE / "raw/financial" / f"{ticker}_main.json").read_text())
        row = next(
            item
            for item in payload["result"]["data"]
            if item["REPORT_DATE"][:10] == PERIOD.isoformat()
        )
        row["ROEJQ"] = float(row["ROEJQ"]) + 0.01
        body = json.dumps(
            {
                "success": True,
                "version": payload.get("version"),
                "result": {"pages": 1, "data": [row]},
            },
            ensure_ascii=False,
        ).encode()
        collection = await EastmoneyBankAnnualCollector(
            FixtureSource(body), min_interval_seconds=0, clock=lambda: captured
        ).collect_company_annual(ticker, PERIOD)
        fields, proofs = build_snapshot_fields(collection, metric_pack=BANK_PACK)
        listing = await listings.get_a_share_listing(ticker)
        result_fields, result_proofs, _ = await reconcile_bank_online_fields(
            session,
            listing_id=listing.id,
            company_id=listing.company_id,
            report_period=PERIOD,
            fields=fields,
            proofs=proofs,
            captured_at=captured,
        )
        assert result_fields["roe_weighted_annual"].verification == "candidate"
        assert (
            result_proofs["roe_weighted_annual"]["reconciliation"]["reason"]
            == "online_value_changed"
        )

        # Prepare a matching value and baseline row for corruption/identity
        # checks below. These assertions use the production validator against
        # the bytes and metadata stored by the real importer.
        matching_payload = json.loads(
            (BANK_EVIDENCE / "raw/financial" / f"{ticker}_main.json").read_text()
        )
        matching_row = next(
            item
            for item in matching_payload["result"]["data"]
            if item["REPORT_DATE"][:10] == PERIOD.isoformat()
        )
        matching = await EastmoneyBankAnnualCollector(
            FixtureSource(
                json.dumps(
                    {
                        "success": True,
                        "version": matching_payload.get("version"),
                        "result": {"pages": 1, "data": [matching_row]},
                    },
                    ensure_ascii=False,
                ).encode()
            ),
            min_interval_seconds=0,
            clock=lambda: captured,
        ).collect_company_annual(ticker, PERIOD)
        matching_fields, matching_proofs = build_snapshot_fields(matching, metric_pack=BANK_PACK)
        snapshot = await session.scalar(
            select(PeerAnnualSnapshot).where(
                PeerAnnualSnapshot.listing_id == listing.id,
                PeerAnnualSnapshot.report_period == PERIOD,
            )
        )
        assert snapshot is not None
        proof = snapshot.verification["roe_weighted_annual"]
        original = await session.get(DisclosureOriginal, proof["original_ref"]["id"])
        assert original is not None
        blob = await session.get(DisclosureBlob, original.content_hash)
        assert blob is not None
        original_blob = blob.body
        original_owner = original.owner_id
        original_payload = copy.deepcopy(original.payload)
        original_id = original.id
        original_hash = original.content_hash
        listing_id = listing.id
        company_id = listing.company_id

        async def reconcile_matching():
            return await reconcile_bank_online_fields(
                session,
                listing_id=listing_id,
                company_id=company_id,
                report_period=PERIOD,
                fields=matching_fields,
                proofs=matching_proofs,
                captured_at=captured,
            )

        # A future online snapshot is ignored as a historical proof baseline.
        future = PeerAnnualSnapshot(
            id=str(uuid4()),
            company_id=snapshot.company_id,
            listing_id=snapshot.listing_id,
            report_period=snapshot.report_period,
            provider="eastmoney",
            adapter_version="future",
            captured_at=captured + timedelta(days=1),
            content_hash="9" * 64,
            semantic_hash="8" * 64,
            fields=copy.deepcopy(snapshot.fields),
            verification=copy.deepcopy(snapshot.verification),
        )
        session.add(future)
        await session.flush()
        trusted_fields, _, _ = await reconcile_matching()
        assert trusted_fields["roe_weighted_annual"].verification == "verified"

        # Damaged bytes, wrong owner and wrong issuer identity all fail closed.
        await session.execute(
            DisclosureBlob.__table__.update()
            .where(
                DisclosureBlob.content_hash == original_hash,
            )
            .values(body=original_blob + b" damaged")
        )
        session.expire_all()
        damaged_fields, _, damaged_summary = await reconcile_matching()
        assert damaged_fields["roe_weighted_annual"].verification == "candidate"
        assert (
            damaged_summary["metrics"]["roe_weighted_annual"]["reason"]
            == "baseline_original_failed_byte_readback"
        )
        await session.execute(
            DisclosureBlob.__table__.update()
            .where(
                DisclosureBlob.content_hash == original_hash,
            )
            .values(body=original_blob)
        )

        await session.execute(
            DisclosureOriginal.__table__.update()
            .where(
                DisclosureOriginal.id == original_id,
            )
            .values(owner_id="wrong_owner")
        )
        session.expire_all()
        wrong_owner_fields, _, _ = await reconcile_matching()
        assert wrong_owner_fields["roe_weighted_annual"].verification == "candidate"
        await session.execute(
            DisclosureOriginal.__table__.update()
            .where(
                DisclosureOriginal.id == original_id,
            )
            .values(owner_id=original_owner)
        )

        wrong_issuer_payload = copy.deepcopy(original_payload)
        wrong_issuer_payload["ticker"] = "601939.SH"
        await session.execute(
            DisclosureOriginal.__table__.update()
            .where(
                DisclosureOriginal.id == original_id,
            )
            .values(payload=wrong_issuer_payload)
        )
        session.expire_all()
        wrong_issuer_fields, _, _ = await reconcile_matching()
        assert wrong_issuer_fields["roe_weighted_annual"].verification == "candidate"
        await session.execute(
            DisclosureOriginal.__table__.update()
            .where(
                DisclosureOriginal.id == original_id,
            )
            .values(payload=original_payload)
        )
