"""Offline import of the fixed, independently reviewed FY2024 bank evidence."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.industry_peers.bank_annual_review import FIELDS, build_rollover_snapshot
from investment_agent.industry_peers.bank_annual_sources import _verify_pdf
from investment_agent.industry_peers.bank_annual_v2 import BANK_ANNUAL_V2_RULE, BANKS
from investment_agent.industry_peers.bank_fy2024_profiles import (
    FY2024_PROFILE,
    checked_fy2024_layout,
)
from investment_agent.industry_peers.hashing import canonical_json
from investment_agent.industry_peers.metric_packs import BANK_PACK, cohort_metric_pack
from investment_agent.industry_peers.originals import (
    AnnualOriginal,
    store_annual_original,
    store_bank_supplemental_original,
)
from investment_agent.storage.models.peer import PeerAnnualSnapshot, PeerCohortSnapshot
from investment_agent.storage.repositories.listing import ListingRepository


def load_reviewed_bank_bundle(paths: list[Path]) -> dict[str, Any]:
    """Reject partial/duplicate/unrecognized slot sets before any database write."""
    sources: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    supplemental: dict[str, Any] = {}
    for path in paths:
        bundle = json.loads(path.read_text())
        if bundle.get("schema") != "bank-fy2024-evidence/1":
            raise ValueError("unsupported bank annual evidence schema")
        for code, source in bundle["sources"].items():
            if code in sources or code not in BANKS:
                raise ValueError("duplicate or unsupported bank source")
            sources[code] = source
        records.extend(bundle["records"])
        for code, extra in bundle.get("supplemental_sources", {}).items():
            if code in supplemental:
                raise ValueError("duplicate supplemental source mapping")
            supplemental[code] = extra
    slots = [(r.get("code"), r.get("metric")) for r in records]
    if (
        set(sources) != set(BANKS)
        or len(slots) != 50
        or set(slots) != {(code, metric) for code in BANKS for metric in FIELDS}
        or any(r.get("period") != "2024-12-31" for r in records)
    ):
        raise ValueError(
            "bank FY2024 evidence must contain exactly ten banks and fifty unique slots"
        )
    for record in records:
        if record.get("status") not in {"verified", "candidate", "scope_mismatch"}:
            raise ValueError("invalid bank evidence status")
        if record["status"] != "verified" and (
            not (record.get("reason") or record.get("candidate_reason"))
            or not record.get("remediation")
        ):
            raise ValueError("every unresolved/excluded field needs an explicit reason and remedy")
    return {"sources": sources, "records": records, "supplemental_sources": supplemental}


def _local_file(root: Path, path: str) -> Path:
    result = (root / path).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError("bank evidence path escapes the explicitly selected data root")
    return result


async def import_reviewed_bank_year(
    session: AsyncSession,
    *,
    cohort_key: str,
    paths: list[Path],
    evidence_root: Path,
) -> dict[str, Any]:
    """Read existing official files only; changes append facts, never overwrite them."""
    bundle = load_reviewed_bank_bundle(paths)
    cohort = await session.scalar(
        select(PeerCohortSnapshot)
        .where(PeerCohortSnapshot.cohort_key == cohort_key)
        .order_by(PeerCohortSnapshot.revision.desc())
        .limit(1)
    )
    if cohort is None or cohort_metric_pack(cohort.payload) != BANK_PACK:
        raise ValueError("a saved bank cohort is required")
    from investment_agent.industry_peers.metric_packs import cohort_read_scope

    if cohort_read_scope(cohort.payload) != ("eastmoney", "bank-annual-v1"):
        raise ValueError("the selected bank cohort must explicitly use the bank annual source")
    listing_repo = ListingRepository(session)
    listings = {}
    prepared = {}
    for code, source in bundle["sources"].items():
        listing = await listing_repo.get_a_share_listing(code)
        if listing is None or not any(
            entry.get("listing_id") == listing.id for entry in cohort.payload["entries"]
        ):
            raise ValueError(f"{code}: not a saved member of the selected trusted bank cohort")
        listings[code] = listing
        body = _local_file(evidence_root, source["pdf_path"]).read_bytes()
        metadata = json.loads(_local_file(evidence_root, source["metadata_path"]).read_text())
        chosen = metadata["chosen"]
        published = datetime.fromisoformat(source["published_at"])
        acquired = datetime.fromisoformat(source["acquired_at"])
        if (
            hashlib.sha256(body).hexdigest() != source["pdf_sha256"]
            or chosen.get("secCode") != code[:6]
            or chosen.get("announcementId") != source["disclosure_id"]
            or chosen.get("announcementTitle") != source["title"]
            or metadata.get("url") != source["url"]
            or datetime.fromtimestamp(chosen["announcementTime"] / 1000, UTC) != published
            or published.tzinfo is None
            or acquired.tzinfo is None
            or not published <= acquired <= datetime.now(UTC)
        ):
            raise ValueError(f"{code}: original identity/hash/chronology mismatch")
        from investment_agent.evidence.disclosure_http import validate_url

        validate_url(source["url"], "original")
        _verify_pdf(body, code[:6], 2024, BANKS[code])
        for record in bundle["records"]:
            if record["code"] == code:
                checked_fy2024_layout(
                    body,
                    date(2024, 12, 31),
                    record["metric"],
                    {
                        **record,
                        "layout_profile": FY2024_PROFILE,
                    },
                )
        prepared[code] = (body, metadata, published, acquired)
    results = []
    for code, (body, metadata, published, acquired) in prepared.items():
        listing = listings[code]
        original = await store_annual_original(
            session,
            AnnualOriginal(
                code,
                date(2024, 12, 31),
                body,
                metadata,
                hashlib.sha256(body).hexdigest(),
                published,
            ),
            listing.id,
            acquired,
        )
        supplemental_refs = []
        for source_key, source in bundle["supplemental_sources"].get(code, {}).items():
            from investment_agent.industry_peers.bank_scope_bridges import (
                ningbo_final_source_identity,
            )

            identity = ningbo_final_source_identity()
            if code != "002142.SZ" or any(
                source.get(key) != identity[key]
                for key in ("url", "pdf_sha256", "disclosure_id", "publisher", "title")
            ):
                raise ValueError("unsupported FY2024 supplemental source identity")
            extra_body = _local_file(evidence_root, source["pdf_path"]).read_bytes()
            extra_row = await store_bank_supplemental_original(
                session,
                code=code,
                body=extra_body,
                source={**identity, "source_key": source_key},
                listing_id=listing.id,
                acquired_at=datetime.fromisoformat(source["acquired_at"]),
            )
            supplemental_refs.append(
                {
                    "source_key": f"{code}.{source_key}",
                    "original_ref": {
                        "kind": "disclosure_original",
                        "id": extra_row.id,
                        "revision": 1,
                    },
                    "pdf_sha256": extra_row.content_hash,
                }
            )
        captured = datetime.now(UTC)
        audits = {}
        records = [r for r in bundle["records"] if r["code"] == code]
        for record in records:
            if record["status"] != "verified":
                continue
            audit = copy.deepcopy(record)
            audit.update(
                layout_profile=FY2024_PROFILE,
                check_rule_version=BANK_ANNUAL_V2_RULE,
                original_ref={"kind": "disclosure_original", "id": original.id, "revision": 1},
                report_identity={
                    "code": code,
                    "period": "2024-12-31",
                    "disclosure_id": original.payload["disclosure_id"],
                },
                pdf_hash=original.content_hash,
                source_url=original.payload["source_url"],
                acquired_at=original.payload["acquired_at"],
                reviewed_at=record["checked_at"],
                checked_at=captured.isoformat(),
            )
            if any(context.get("source_key") for context in audit.get("scope_chain", [])):
                audit["supplemental_originals"] = copy.deepcopy(supplemental_refs)
            audits[record["metric"]] = audit
        snapshot, _ = await build_rollover_snapshot(
            session,
            listing_id=listing.id,
            company_id=listing.company_id,
            report_period=date(2024, 12, 31),
            audit={"fields": audits},
            captured_at=captured,
        )
        statuses = {}
        for record in records:
            metric = record["metric"]
            if record["status"] == "verified":
                if snapshot.fields[metric]["verification"] != "verified":
                    raise ValueError(
                        f"{code}.{metric}: actual original validation rejected reviewed field: {snapshot.verification[metric]}"
                    )
            else:
                reason = record.get("reason") or record.get("candidate_reason")
                snapshot.fields[metric]["missing_reason"] = reason
                snapshot.verification[metric] = {
                    "status": "candidate",
                    "evidence_status": record["status"],
                    "scope_exclusion": record["status"] == "scope_mismatch",
                    "rejection_reason": reason,
                    "remediation": record["remediation"],
                    "original_ref": {
                        "kind": "disclosure_original",
                        "id": original.id,
                        "revision": 1,
                    },
                    "pdf_hash": original.content_hash,
                    "audit_record": record,
                }
            statuses[metric] = {
                "status": record["status"],
                "value": snapshot.fields[metric]["value"],
                "reason": record.get("reason") or record.get("candidate_reason"),
                "remediation": record.get("remediation"),
            }
        semantic = hashlib.sha256(
            canonical_json({"base": snapshot.semantic_hash, "outcomes": statuses}).encode()
        ).hexdigest()
        snapshot.semantic_hash = snapshot.content_hash = semantic
        snapshot.id = str(uuid5(NAMESPACE_URL, "bank-reviewed-fy2024/" + semantic))
        if await session.get(PeerAnnualSnapshot, snapshot.id) is None:
            session.add(snapshot)
        results.append({"code": code, "snapshot_id": snapshot.id, "fields": statuses})
    await session.flush()
    return {"report_period": "2024-12-31", "banks": results}
