"""Root acceptance: append-only bank proof versions and stored-byte readback."""

import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sqlalchemy import func, select

from tests.integration.test_bank_verified_import import (
    AUDIT_PATH,
    BANK_EVIDENCE,
    ORIGINALS_ROOT,
    PERIOD,
)
from tests.integration.test_bank_verified_import import (
    bank_world as bank_world,
)
from tests.integration.test_bank_verified_import import (
    requires_bank_originals as requires_bank_originals,
)


async def test_verified_import_preserves_frozen_candidates_and_checks_saved_bytes(
    bank_world, requires_bank_originals,
):
    from investment_agent.cli.commands.bank_annual import BANK_CODES, import_bank_annual_snapshots
    from investment_agent.cli.commands.peer_cohort import import_sample_roster, payload_sha256
    from investment_agent.industry_peers.batch import submit_offline_batch
    from investment_agent.industry_peers.originals import validate_field_original
    from investment_agent.industry_peers.resolver import PeerContextResolver
    from investment_agent.industry_peers.service import (
        get_industry_peer_context,
        peer_context_binding_from,
    )
    from investment_agent.storage.database import session_scope
    from investment_agent.storage.models.disclosure import DisclosureBlob, DisclosureOriginal
    from investment_agent.storage.models.peer import PeerAnnualSnapshot, PeerCohortSnapshot
    from investment_agent.storage.repositories.listing import ListingRepository

    started = datetime.now(UTC)
    async with session_scope() as session:
        listings = ListingRepository(session)
        for code in BANK_CODES:
            await listings.ensure_a_share_listing(code, company_name=code)
        roster = await import_sample_roster(
            session, sample_path=BANK_EVIDENCE / "sample.json", industry="bank", acquired_at=started,
        )
        parent_payload = copy.deepcopy(roster.snapshot.payload)
        await import_bank_annual_snapshots(
            session, evidence_root=BANK_EVIDENCE, report_period=PERIOD, acquired_at=started,
        )
    async with session_scope() as session:
        old_batch = await submit_offline_batch(
            session, data_dir=bank_world, cohort_key="bank_run02_sample", report_period=PERIOD,
            submitted_at=started + timedelta(seconds=1),
        )
    async with session_scope() as session:
        report = await import_bank_annual_snapshots(
            session, evidence_root=BANK_EVIDENCE, report_period=PERIOD,
            acquired_at=started + timedelta(seconds=2), audit_path=AUDIT_PATH,
            originals_root=ORIGINALS_ROOT,
        )
        audit = json.loads(AUDIT_PATH.read_text())
        assert report["verified_fields"] == sum(row["status"] == "verified" for row in audit["records"])
        before = await session.scalar(select(func.count()).select_from(PeerAnnualSnapshot))
        assert before == 20
        await import_bank_annual_snapshots(
            session, evidence_root=BANK_EVIDENCE, report_period=PERIOD,
            acquired_at=started + timedelta(seconds=3), audit_path=AUDIT_PATH,
            originals_root=ORIGINALS_ROOT,
        )
        assert await session.scalar(select(func.count()).select_from(PeerAnnualSnapshot)) == before
        rows = list((await session.execute(select(PeerAnnualSnapshot))).scalars())
        verified = [row for row in rows if row.fields["equity_parent"]["verification"] == "verified"]
        assert len(verified) == 10
        # Independently read from the consolidated statements, including their
        # printed million/thousand units; ordinary-share equity is different.
        expected_equity = {
            "601398.SH": "4244259000000", "601939.SH": "3663411000000",
            "601288.SH": "3237182000000", "600036.SH": "1272875000000",
            "601166.SH": "901716000000", "000001.SZ": "551184000000",
            "600919.SH": "337961238000", "601009.SH": "206301002000",
            "002142.SZ": "247324000000", "601128.SH": "31379656000",
        }
        complete_listings = set()
        for row in verified:
            ticker = row.verification["equity_parent"]["report_identity"]["code"]
            assert row.fields["equity_parent"]["value"] == expected_equity[ticker]
            assert set(row.fields) == {
                "equity_parent", "roe_weighted_annual", "net_interest_margin", "npl_ratio", "provision_coverage",
            }
            for metric in row.fields:
                if row.fields[metric]["verification"] == "verified":
                    assert await validate_field_original(session, row, metric), (row.listing_id, metric)
            if all(field["verification"] == "verified" for field in row.fields.values()):
                complete_listings.add(row.listing_id)
        # A fully checked sub-sample is explicit; incomplete members are never
        # silently included in a critical verified comparison.
        assert len(complete_listings) == 9
        snapshot = verified[0]
        blob = await session.get(DisclosureBlob, snapshot.verification["equity_parent"]["pdf_hash"])
        assert blob is not None
        original_body = blob.body
        # Bypass the ORM's separate immutable-record guard to simulate damaged
        # backup/storage bytes and exercise the hash verification on readback.
        await session.execute(DisclosureBlob.__table__.update().where(
            DisclosureBlob.content_hash == blob.content_hash,
        ).values(body=original_body + b"\n% acceptance corruption test\n"))
        await session.refresh(blob)
        assert not await validate_field_original(session, snapshot, "equity_parent")
        await session.execute(DisclosureBlob.__table__.update().where(
            DisclosureBlob.content_hash == blob.content_hash,
        ).values(body=original_body))
        await session.refresh(blob)
        assert await validate_field_original(session, snapshot, "equity_parent")
        ningbo = next(
            row for row in verified
            if row.verification["equity_parent"]["report_identity"]["code"] == "002142.SZ"
        )
        supplements = ningbo.verification["npl_ratio"]["supplemental_originals"]
        assert len(supplements) == 1
        supplemental = supplements[0]
        assert supplemental["publisher"] == "中诚信国际信用评级有限责任公司"
        assert supplemental["publication_precision"] == "minute_local_timezone_unspecified"
        supplemental_row = await session.get(DisclosureOriginal, supplemental["original_ref"]["id"])
        assert supplemental_row is not None
        assert supplemental_row.owner_id == "local_owner"
        assert supplemental_row.payload["listing_id"] == ningbo.listing_id
        assert supplemental_row.payload["source"] == "chinamoney_ccxi_rating_v1"
        assert supplemental_row.payload["source_identity"] == "chinamoney_ccxi_rating_v1:3343869"
        supplement_blob = await session.get(DisclosureBlob, supplemental_row.content_hash)
        assert supplement_blob is not None
        supplement_body = supplement_blob.body
        await session.execute(DisclosureBlob.__table__.update().where(
            DisclosureBlob.content_hash == supplement_blob.content_hash,
        ).values(body=supplement_body + b"\n% supplemental corruption\n"))
        await session.refresh(supplement_blob)
        for metric in ("npl_ratio", "provision_coverage"):
            assert not await validate_field_original(session, ningbo, metric)
        # The independent annual-only NIM proof must remain valid.
        assert await validate_field_original(session, ningbo, "net_interest_margin")
        await session.execute(DisclosureBlob.__table__.update().where(
            DisclosureBlob.content_hash == supplement_blob.content_hash,
        ).values(body=supplement_body))
        await session.refresh(supplement_blob)
        for metric in ("npl_ratio", "provision_coverage"):
            assert await validate_field_original(session, ningbo, metric)
        for mutation in ("missing_ref", "annual_ref", "wrong_hash", "wrong_column", "wrong_source"):
            tampered = copy.deepcopy(ningbo.verification)
            proof = tampered["npl_ratio"]
            if mutation == "missing_ref":
                proof["supplemental_originals"][0].pop("original_ref")
            elif mutation == "annual_ref":
                proof["supplemental_originals"][0]["original_ref"] = proof["original_ref"]
            elif mutation == "wrong_hash":
                proof["supplemental_originals"][0]["pdf_sha256"] = "0" * 64
            elif mutation == "wrong_column":
                # All three annual NPL percentages round to 0.76%; a prior
                # column must fail even though its displayed number matches.
                proof["scope_bridge"]["current_column"] = "2024"
            elif mutation == "wrong_source":
                proof["supplemental_originals"][0]["publisher"] = "宁波银行股份有限公司"
            for key in ("scope_bridge", "supplemental_originals"):
                proof["audit_record"][key] = copy.deepcopy(proof[key])
            detached = SimpleNamespace(
                listing_id=ningbo.listing_id, report_period=ningbo.report_period,
                captured_at=ningbo.captured_at, fields=ningbo.fields, verification=tampered,
            )
            assert not await validate_field_original(session, detached, "npl_ratio"), mutation
        stored_payload = copy.deepcopy(supplemental_row.payload)
        for mutation in ("owner", "listing", "publication_precision", "source_namespace"):
            payload = copy.deepcopy(stored_payload)
            changes = {"payload": payload}
            if mutation == "owner":
                changes["owner_id"] = "another_owner"
            elif mutation == "listing":
                payload["listing_id"] = snapshot.listing_id
                assert snapshot.listing_id != ningbo.listing_id
            elif mutation == "publication_precision":
                payload["publication_precision"] = "exact_utc"
            elif mutation == "source_namespace":
                payload["source"] = "cninfo.v1"
            await session.execute(DisclosureOriginal.__table__.update().where(
                DisclosureOriginal.id == supplemental_row.id,
            ).values(**changes))
            await session.refresh(supplemental_row)
            assert not await validate_field_original(session, ningbo, "npl_ratio"), mutation
            await session.execute(DisclosureOriginal.__table__.update().where(
                DisclosureOriginal.id == supplemental_row.id,
            ).values(owner_id="local_owner", payload=copy.deepcopy(stored_payload)))
            await session.refresh(supplemental_row)
            assert await validate_field_original(session, ningbo, "npl_ratio")
        acquired = datetime.fromisoformat(supplemental["acquired_at"])
        assert not await validate_field_original(
            session, ningbo, "npl_ratio", cutoff=acquired - timedelta(microseconds=1),
        )
    async with session_scope() as session:
        current_batch = await submit_offline_batch(
            session, data_dir=bank_world, cohort_key="bank_run02_sample", report_period=PERIOD,
            submitted_at=started + timedelta(seconds=4),
        )
        payload = copy.deepcopy(parent_payload)
        payload["cohort_key"] = "bank_verified_acceptance_subset"
        payload["entries"] = [entry for entry in payload["entries"] if entry["listing_id"] in complete_listings]
        included_tickers = {entry["ticker"] for entry in payload["entries"]}
        payload["excluded"] = [
            {"ticker": entry["ticker"], "reason": "at least one field lacks consolidated original proof"}
            for entry in parent_payload["entries"] if entry["ticker"] not in included_tickers
        ]
        payload["included"] = [entry for entry in payload["included"] if entry["ticker"] in included_tickers]
        payload["source"] = {"kind": "verified_subset_acceptance", "parent_content_hash": roster.snapshot.content_hash}
        payload["notes"] = ["Explicit audited subset for acceptance; incomplete sample members are listed as excluded."]
        session.add(PeerCohortSnapshot(
            id=str(uuid.uuid4()), cohort_key=payload["cohort_key"], revision=1,
            observed_on=started.date(), acquired_at=started + timedelta(seconds=4),
            verification=roster.snapshot.verification, inclusion_rule_version="bank-proof-subset/1",
            content_hash=payload_sha256(payload), payload=payload,
        ))
    async with session_scope() as session:
        subset = await submit_offline_batch(
            session, data_dir=bank_world, cohort_key="bank_verified_acceptance_subset", report_period=PERIOD,
            submitted_at=started + timedelta(seconds=5),
        )
    async with session_scope() as session:
        historical = await get_industry_peer_context(
            session, ticker="601398.SH", data_dir=bank_world,
            batch_run_id=old_batch.mapping.batch_run_id,
        )
        assert next(metric for metric in historical.metrics if metric.metric_key == "roe").peer_median is None
        current = await get_industry_peer_context(
            session, ticker="601398.SH", data_dir=bank_world, batch_run_id=current_batch.mapping.batch_run_id,
        )
        assert next(metric for metric in current.metrics if metric.metric_key == "roe").peer_median is not None
        checked = await get_industry_peer_context(
            session, ticker="601398.SH", data_dir=bank_world, batch_run_id=subset.mapping.batch_run_id,
        )
        binding = peer_context_binding_from(checked)
        assert binding is not None
        resolver = PeerContextResolver(session, binding=binding, data_dir=bank_world)
        for metric in checked.metrics:
            assert metric.calculation_ref is not None
            result = await resolver.verify(metric.calculation_ref, require_verified_inputs=True)
            assert result.inputs_verified
