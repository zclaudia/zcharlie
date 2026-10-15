#!/usr/bin/env python3
"""Build a disposable, offline HK2-07 browser-state database.

The fixture database is deliberately separate from the normal application
database. AAPL and COST retain the exact reviewed dataset payload/raw index
from a real importer run; the remaining state variations are clearly marked
as fixture data. This tool never starts a server or contacts a provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.security import (
    DEFAULT_PILOT_LIST_PATH,
    load_pilot_identities,
)
from investment_agent.materials.bootstrap import initialize_pilot
from investment_agent.materials.context import financial_context
from investment_agent.materials.contracts import (
    SCHEMA_VERSION,
    DatasetDraft,
    DatasetKind,
    DatePrecision,
    FactPeriod,
    PeriodBasis,
    SubjectKind,
    Verification,
    fact_from_payload,
    fact_to_payload,
)
from investment_agent.materials.jobs import (
    MATERIAL_COLLECTION,
    MaterialCollectionPayload,
    material_idempotency_key,
)
from investment_agent.materials.store import MaterialStore
from investment_agent.storage.database import init_engine
from investment_agent.storage.models import Base
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.reference import Company, Listing
from investment_agent.storage.models.stock_directory import StockDirectoryEntry

FIXTURE_SOURCE = "fixture.hk207_state_matrix.v1"
REVIEWED_SOURCE = "issuer.reviewed_annual.v1"
FIXTURE_TIME = datetime(2026, 10, 15, 2, 0, tzinfo=UTC)
STALE_PERIOD_END = date(2026, 9, 26)


def _read_actual_reviewed_rows(database: Path) -> dict[str, dict[str, Any]]:
    """Read the source DB without opening it through a writable ORM engine."""

    uri = f"file:{quote(str(database.resolve()))}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM research_dataset "
            "WHERE kind = 'annual_facts' AND subject_kind = 'company' "
            "AND source = ? ORDER BY id",
            (REVIEWED_SOURCE,),
        ).fetchall()
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        payload = json.loads(item["payload"])
        review = payload.get("review")
        if not isinstance(review, dict) or review.get("status") != "complete":
            continue
        key = review.get("company_key")
        if isinstance(key, str):
            found[key] = {**item, "payload": payload}
    return found


def _timestamp(value: Any) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if result.tzinfo is None:
        return result.replace(tzinfo=UTC)
    return result


def _actual_draft(row: dict[str, Any], *, company_id: int, data_dir: Path) -> DatasetDraft:
    raw_ref = row.get("raw_ref")
    if not isinstance(raw_ref, str) or not raw_ref:
        raise ValueError(f"reviewed dataset {row['id']} has no raw index reference")
    source_raw_path = (data_dir / raw_ref).resolve()
    try:
        source_raw_path.relative_to(data_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"reviewed raw index escapes data directory: {raw_ref}") from exc
    raw_bytes = source_raw_path.read_bytes()
    period_end = row.get("period_end")
    parsed_end = date.fromisoformat(period_end) if isinstance(period_end, str) else None
    period_start = _payload_period_start(row["payload"], parsed_end)
    period = (
        None
        if parsed_end is None
        else FactPeriod(start=period_start, end=parsed_end, basis=PeriodBasis.ANNUAL)
    )
    return DatasetDraft(
        kind=DatasetKind.ANNUAL_FACTS,
        subject_kind=SubjectKind.COMPANY,
        subject_id=company_id,
        source=REVIEWED_SOURCE,
        schema_version=int(row["schema_version"]),
        adapter_version=str(row["adapter_version"]),
        period=period,
        captured_at=_timestamp(row["captured_at"]),
        source_fetched_at=(
            None if row.get("source_fetched_at") is None else _timestamp(row["source_fetched_at"])
        ),
        payload=row["payload"],
        raw_bytes=raw_bytes,
        raw_media_type=row.get("raw_media_type"),
        raw_source_url=row.get("raw_source_url"),
    )


def _fixture_draft(
    *,
    source_facts: list[dict[str, Any]],
    company_id: int,
    company_key: str,
    note: str,
    period_end: date | None = None,
    extra: dict[str, Any] | None = None,
) -> DatasetDraft:
    payload: dict[str, Any] = {
        "facts": source_facts,
        "fixture": {
            "synthetic": True,
            "fixture_set": "hk2-07-browser-state-matrix",
            "company_key": company_key,
            "note": note,
        },
    }
    if extra:
        payload.update(extra)
    return DatasetDraft(
        kind=DatasetKind.ANNUAL_FACTS,
        subject_kind=SubjectKind.COMPANY,
        subject_id=company_id,
        source=FIXTURE_SOURCE,
        schema_version=SCHEMA_VERSION,
        adapter_version="hk207_fixture_v1",
        period=(
            None
            if period_end is None
            else FactPeriod(
                start=_payload_period_start({"facts": source_facts}, period_end),
                end=period_end,
                basis=PeriodBasis.ANNUAL,
            )
        ),
        captured_at=FIXTURE_TIME,
        source_fetched_at=None,
        payload=payload,
    )


def _facts_for(row: dict[str, Any]) -> list[dict[str, Any]]:
    facts = row["payload"].get("facts")
    if not isinstance(facts, list):
        raise ValueError(f"reviewed dataset {row['id']} has no canonical fact list")
    return [dict(item) for item in facts if isinstance(item, dict)]


def _payload_period_start(payload: dict[str, Any], end: date | None) -> date | None:
    if end is None:
        return None
    starts = {
        date.fromisoformat(period["start"])
        for fact in payload.get("facts", [])
        if isinstance(fact, dict)
        and isinstance((period := fact.get("period")), dict)
        and period.get("end") == end.isoformat()
        and isinstance(period.get("start"), str)
    }
    if not starts:
        raise ValueError(f"annual fixture period {end} has no canonical period start")
    return min(starts)


def _synthetic_stale_fact(entry: dict[str, Any]) -> dict[str, Any]:
    fact = fact_from_payload(entry)
    provenance = replace(
        fact.provenance,
        source="fixture.synthetic_stale_annual",
        accession=None,
        original_hash=None,
        source_published_at=None,
        source_published_date=None,
        source_published_precision=DatePrecision.UNKNOWN,
        accepted_at=None,
        source_available_at=None,
    )
    synthetic = replace(
        fact,
        period=FactPeriod(
            start=date(2025, 9, 28),
            end=STALE_PERIOD_END,
            basis=fact.period.basis,
            label="FY2026 fixture",
            days=363,
        ),
        provenance=provenance,
        verification=Verification.CANDIDATE,
        original_locator=None,
    )
    return fact_to_payload(synthetic)


def _mutate_fixture_facts(entries: list[dict[str, Any]], *, state: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        fact = fact_from_payload(entry)
        if state == "candidate":
            fact = replace(fact, verification=Verification.CANDIDATE)
        elif state == "conflict" and index == 0:
            fact = replace(
                fact,
                conflict="unresolved",
                conflict_note="Synthetic unresolved conflict for HK2-07 UI fixture.",
            )
        elif state == "unknown_publication" and index == 0:
            fact = replace(
                fact,
                verification=Verification.CANDIDATE,
                provenance=replace(
                    fact.provenance,
                    source="fixture.unknown_publication",
                    source_published_at=None,
                    source_published_date=None,
                    source_published_precision=DatePrecision.UNKNOWN,
                    accepted_at=None,
                    source_available_at=None,
                ),
            )
        output.append(fact_to_payload(fact))
    return output


async def _seed(args: argparse.Namespace) -> dict[str, Any]:
    output_path = args.database_out.resolve()
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite existing database: {output_path}")
    source_path = args.reviewed_db.resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"reviewed importer database not found: {source_path}")
    if not args.data_dir.is_dir():
        raise FileNotFoundError(f"app data directory not found: {args.data_dir}")
    reviewed = _read_actual_reviewed_rows(source_path)
    needed = {"apple", "costco", "tencent-holdings", "exxon-mobil", "tsmc", "hsbc-holdings"}
    missing = sorted(needed - set(reviewed))
    if missing:
        raise ValueError(f"reviewed source database is missing complete package rows: {missing}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    engine = init_engine(f"sqlite+aiosqlite:///{output_path}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        identities = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
        listing_ids: dict[str, int] = {}
        company_ids: dict[str, int] = {}
        async with (
            engine.begin() as connection,
            AsyncSession(bind=connection, expire_on_commit=False) as session,
        ):
            setup = await initialize_pilot(session, identities)
            company_ids = setup.company_ids
            listing_ids = {
                identity.company_key: setup.listings[(identity.market_code, identity.ticker)].id
                for identity in identities
            }
            session.add(Company(canonical_name="META fixture, not covered"))
            await session.flush()
            meta_company = await session.scalar(
                select(Company).where(Company.canonical_name == "META fixture, not covered")
            )
            if meta_company is None:
                raise RuntimeError("failed to create uncovered fixture company")
            session.add(
                Listing(
                    company_id=meta_company.id,
                    market_code="NASDAQ",
                    ticker="META",
                    is_primary=True,
                )
            )
            await session.flush()
            meta_listing = await session.scalar(
                select(Listing).where(Listing.market_code == "NASDAQ", Listing.ticker == "META")
            )
            if meta_listing is None:
                raise RuntimeError("failed to create uncovered META fixture listing")
            listing_ids["meta-uncovered"] = meta_listing.id
            session.add(
                StockDirectoryEntry(
                    ticker="600519.SH",
                    name="离线目录演示",
                    pinyin_initials="lxml",
                    status="listed",
                    synced_on=date(2026, 10, 15),
                    provider="offline_fixture",
                    captured_at=FIXTURE_TIME,
                )
            )
            store = MaterialStore(session, data_dir=args.data_dir)

            # Preserve these real immutable package records exactly.
            for key in ("apple", "costco"):
                await store.save(
                    _actual_draft(
                        reviewed[key], company_id=company_ids[key], data_dir=args.data_dir
                    )
                )

            # A single annual subset represents known partial coverage.
            hsbc_facts = _facts_for(reviewed["hsbc-holdings"])
            latest_hsbc_end = max(fact_from_payload(item).period.end for item in hsbc_facts)
            hsbc_latest = [
                item for item in hsbc_facts if fact_from_payload(item).period.end == latest_hsbc_end
            ][:2]
            await store.save(
                _fixture_draft(
                    source_facts=_mutate_fixture_facts(hsbc_latest, state="candidate"),
                    company_id=company_ids["hsbc-holdings"],
                    company_key="hsbc-holdings",
                    note="Synthetic latest-year subset; bank N/A metrics remain not applicable.",
                    period_end=latest_hsbc_end,
                )
            )

            tencent_entries = _facts_for(reviewed["tencent-holdings"])
            await store.save(
                _fixture_draft(
                    source_facts=_mutate_fixture_facts(tencent_entries, state="conflict"),
                    company_id=company_ids["tencent-holdings"],
                    company_key="tencent-holdings",
                    note="Original values retained; one copied canonical fact is marked unresolved.",
                    period_end=max(fact_from_payload(item).period.end for item in tencent_entries),
                )
            )

            apple_fact = _synthetic_stale_fact(_facts_for(reviewed["apple"])[0])
            await store.save(
                DatasetDraft(
                    kind=DatasetKind.ANNUAL_FACTS,
                    subject_kind=SubjectKind.COMPANY,
                    subject_id=company_ids["apple"],
                    source="sec.companyfacts.v1",
                    schema_version=SCHEMA_VERSION,
                    adapter_version="hk207_fixture_provider_v1",
                    period=FactPeriod(
                        start=date(2025, 9, 28),
                        end=STALE_PERIOD_END,
                        basis=PeriodBasis.ANNUAL,
                        label="FY2026 fixture",
                        days=363,
                    ),
                    captured_at=FIXTURE_TIME,
                    payload={
                        "facts": [apple_fact],
                        "fixture": {
                            "synthetic": True,
                            "fixture_set": "hk2-07-browser-state-matrix",
                            "note": "Synthetic later period only; not an actual FY2026 filing.",
                        },
                    },
                )
            )

            # A failed refresh must not manufacture a successful capture.
            jpm = next(item for item in identities if item.company_key == "jpmorgan-chase")
            jpm_listing = setup.listings[(jpm.market_code, jpm.ticker)]
            request = MaterialCollectionPayload(
                listing_id=jpm_listing.id,
                refresh_batch="hk207-fixture-2026-10-15",
                reason="fixture_failed_refresh",
            )
            session.add(
                PipelineRun(
                    kind=MATERIAL_COLLECTION,
                    universe_code=None,
                    listing_id=jpm_listing.id,
                    triggered_by="fixture",
                    started_at=FIXTURE_TIME,
                    claimed_at=FIXTURE_TIME,
                    finished_at=FIXTURE_TIME,
                    status="failed",
                    stage="finished",
                    idempotency_key=material_idempotency_key(request),
                    attempt_count=1,
                    max_attempts=3,
                    request_json=request.model_dump(mode="json"),
                    available_at=FIXTURE_TIME,
                    worker_id="hk207-fixture-worker",
                    error_summary="Synthetic offline fixture failure; no upstream request was made.",
                    metadata_json={"fixture": "hk2-07-browser-state-matrix", "synthetic": True},
                )
            )

            for key in ("exxon-mobil", "tsmc"):
                entries = _facts_for(reviewed[key])
                facts = _mutate_fixture_facts(
                    entries,
                    state="candidate" if key == "exxon-mobil" else "unknown_publication",
                )
                if key == "tsmc":
                    facts = [facts[0]]
                end = max(fact_from_payload(item).period.end for item in facts)
                await store.save(
                    _fixture_draft(
                        source_facts=facts,
                        company_id=company_ids[key],
                        company_key=key,
                        note=(
                            "Four-year candidate copy without review receipt."
                            if key == "exxon-mobil"
                            else "Candidate source fact with publication date and precision unknown."
                        ),
                        period_end=end,
                    )
                )

            await session.flush()
            contexts = {}
            for key, listing_id in listing_ids.items():
                view = await financial_context(session, listing_id)
                contexts[key] = {
                    "listing_id": listing_id,
                    "route": f"/research/listings/{listing_id}/financial-context",
                    "covered": view.covered,
                    "display_code": view.display_code,
                    "report_currency": view.report_currency,
                    "periods_covered": view.coverage["periods_covered"],
                    "facts_total": view.coverage["facts_total"],
                    "facts_candidate": view.coverage["facts_candidate"],
                    "facts_verified": view.coverage["facts_verified"],
                    "fact_source_dates": [
                        None if fact.source_date is None else fact.source_date.isoformat()
                        for fact in view.facts
                    ],
                    "fact_conflicts": [fact.conflict for fact in view.facts],
                    "status_codes": [status.code for status in view.statuses],
                    "latestness": view.freshness.latestness,
                    "last_verified_on": (
                        None
                        if view.freshness.last_verified_on is None
                        else view.freshness.last_verified_on.isoformat()
                    ),
                    "refresh_status": view.refresh.status,
                }

            _assert_expected_contexts(contexts)
        return {
            "schema_version": 1,
            "database": str(output_path),
            "data_dir": str(args.data_dir.resolve()),
            "reviewed_source_database": str(source_path),
            "generated_at": FIXTURE_TIME.isoformat(),
            "online_refresh_enabled": False,
            "fixture_data_is_synthetic": True,
            "actual_reviewed_rows_copied": ["apple", "costco"],
            "states": contexts,
            "expected_statuses": {
                "clp-holdings": ["material_missing", "latestness_unknown"],
                "hsbc-holdings": ["partial_verification", "partial_coverage", "latestness_unknown"],
                "tencent-holdings": [
                    "source_conflict",
                    "awaiting_original_review",
                    "latestness_unknown",
                ],
                "apple": ["material_stale"],
                "costco": [],
                "jpmorgan-chase": ["failed_refresh", "latestness_unknown"],
                "exxon-mobil": [
                    "partial_verification",
                    "awaiting_original_review",
                    "latestness_unknown",
                ],
                "tsmc": ["partial_verification", "awaiting_original_review", "latestness_unknown"],
                "meta-uncovered": ["listing_not_covered"],
            },
        }
    finally:
        await engine.dispose()


def _assert_expected_contexts(contexts: dict[str, dict[str, Any]]) -> None:
    required = {
        "clp-holdings": ("latestness_unknown",),
        "hsbc-holdings": ("partial_verification", "latestness_unknown"),
        "tencent-holdings": ("source_conflict", "latestness_unknown"),
        "apple": ("material_stale",),
        "costco": (),
        "jpmorgan-chase": ("failed_refresh",),
        "exxon-mobil": ("awaiting_original_review", "latestness_unknown"),
        "tsmc": ("awaiting_original_review", "latestness_unknown"),
        "meta-uncovered": ("listing_not_covered",),
    }
    for key, expected in required.items():
        actual = set(contexts[key]["status_codes"])
        missing = set(expected) - actual
        if missing:
            raise AssertionError(
                f"{key} fixture did not produce expected statuses: {sorted(missing)}"
            )
    if contexts["costco"]["latestness"] != "checked_through":
        raise AssertionError("COST fixture is not a complete validated review receipt")
    if contexts["costco"]["facts_candidate"] != 0 or contexts["costco"]["facts_verified"] == 0:
        raise AssertionError(
            "COST must retain only the verified facts from its actual reviewed row"
        )
    if (
        contexts["hsbc-holdings"]["periods_covered"] != 1
        or contexts["hsbc-holdings"]["facts_candidate"] != contexts["hsbc-holdings"]["facts_total"]
    ):
        raise AssertionError("HSBC fixture must be a single-year candidate subset")
    if (
        contexts["exxon-mobil"]["periods_covered"] != 4
        or contexts["exxon-mobil"]["facts_candidate"] != contexts["exxon-mobil"]["facts_total"]
    ):
        raise AssertionError("XOM fixture must contain four years of candidate facts")
    if contexts["clp-holdings"]["facts_total"] != 0:
        raise AssertionError("CLP empty-state fixture unexpectedly contains material")
    if not any(
        conflict == "unresolved" for conflict in contexts["tencent-holdings"]["fact_conflicts"]
    ):
        raise AssertionError("Tencent fixture must retain an unresolved copied-fact conflict")
    if contexts["tsmc"]["facts_candidate"] != 1 or contexts["tsmc"]["fact_source_dates"] != [None]:
        raise AssertionError(
            "TSM fixture must expose one candidate fact with unknown publication date"
        )
    if contexts["apple"]["latestness"] != "stale":
        raise AssertionError("AAPL fixture did not surface the synthetic later reporting period")
    if contexts["meta-uncovered"]["covered"]:
        raise AssertionError("META must remain outside the checked pilot list")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reviewed-db", type=Path, required=True)
    parser.add_argument("--database-out", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--report-out", type=Path)
    args = parser.parse_args()
    report = asyncio.run(_seed(args))
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.report_out is not None:
        args.report_out.parent.mkdir(parents=True, exist_ok=True)
        args.report_out.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
