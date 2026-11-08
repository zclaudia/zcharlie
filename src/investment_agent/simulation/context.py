"""Frozen common facts and strictly bound private simulation context."""

import json
from collections.abc import Callable, Sequence
from datetime import date, datetime, time
from typing import Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.domain.time import as_utc
from investment_agent.investor.common import Contract, utc_timestamp
from investment_agent.simulation.contracts import (
    AccountScope,
    DataManifest,
    Decision,
    EvidenceRef,
    SimulationAccount,
    SimulationReview,
)
from investment_agent.simulation.data import canonical_json, content_hash, validate_listing_scope
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.repositories.financial_snapshot import effective_available_from
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.pit import end_of_business_day_utc


class FrozenFacts(Contract):
    manifest: DataManifest
    payload_json: str

    def payload(self) -> dict[str, object]:
        """Return a fresh decoded copy; callers cannot mutate frozen source facts."""
        value = json.loads(self.payload_json)
        if not isinstance(value, dict):
            raise ValueError("common_payload_must_be_object")
        return cast(dict[str, object], value)


class SharedEvidence(Contract):
    reference: EvidenceRef
    # Canonical content string avoids mutable dictionaries in a frozen contract.
    payload_json: str
    kind: Literal["fact"] = "fact"
    source_scope: Literal["public"] = "public"


def _validate_public_payload(value: object, listing_ids: set[int]) -> None:
    """Trusted public facts may not contain another account's nested records."""
    if isinstance(value, dict):
        if any(key in value for key in ("participant_id", "account_id", "decision_id")):
            raise ValueError("evidence_outside_common_scope")
        if "listing_id" in value and (
            type(value["listing_id"]) is not int or value["listing_id"] not in listing_ids
        ):
            raise ValueError("evidence_outside_common_scope")
        for nested in value.values():
            _validate_public_payload(nested, listing_ids)
    elif isinstance(value, list):
        for nested in value:
            _validate_public_payload(nested, listing_ids)


async def build_common_manifest(
    session: AsyncSession,
    *,
    listing_ids: Sequence[int],
    observation_date: date,
    cutoff: datetime,
    archive: Callable[[str, str], str],
    evidence: Sequence[SharedEvidence] = (),
    require_financials: bool = True,
    history_start: date | None = None,
) -> FrozenFacts:
    """Strict captured cutoff, raw prices, conservative date-only publication.

    ``archive`` writes exact canonical UTF-8 bytes without overwriting an old
    content-addressed artifact, and returns its path. No latest-state tools or
    participant-generated research enter this common package.
    """
    cutoff = utc_timestamp(cutoff)
    history_start = history_start or observation_date
    if history_start > observation_date:
        raise ValueError("history_starts_after_observation")
    await validate_listing_scope(session, listing_ids)
    if cutoff < datetime.combine(observation_date, time(15), SHANGHAI_TZ):
        raise ValueError("observation_day_not_closed")
    refs: list[EvidenceRef] = []
    facts: list[dict[str, object]] = []
    missing = []
    for listing_id in sorted(listing_ids):
        bars = await MarketSnapshotRepository(session).series_as_of(
            listing_id,
            history_start,
            observation_date,
            cutoff,
            adjust="none",
            include_backfill=False,
        )
        if not any(bar.trade_date == observation_date for bar in bars):
            missing.append(f"market_missing:{listing_id}:{observation_date}")
        for bar in bars:
            value = {
                "listing_id": listing_id,
                "trade_date": str(bar.trade_date),
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
                "volume": str(bar.volume),
                "amount": str(bar.amount) if bar.amount is not None else None,
                "trade_status": bar.trade_status,
                "is_st": bar.is_st,
                "provider": bar.provider,
                "version": bar.version,
                "adjust": bar.adjust,
                "raw_ref": bar.raw_ref,
            }
            if (
                not all(
                    value.is_finite()
                    for value in (bar.open, bar.high, bar.low, bar.close, bar.volume)
                )
                or (bar.amount is not None and (not bar.amount.is_finite() or bar.amount < 0))
                or min(bar.open, bar.high, bar.low, bar.close) <= 0
                or bar.volume < 0
                or bar.low > min(bar.open, bar.close)
                or bar.high < max(bar.open, bar.close)
                or bar.high < bar.low
            ):
                missing.append(f"market_invalid:{listing_id}:{bar.trade_date}")
            if bar.trade_status not in {0, 1} or bar.is_st is None:
                missing.append(f"market_status_missing:{listing_id}:{bar.trade_date}")
            published = datetime.combine(bar.trade_date, time(15), SHANGHAI_TZ)
            captured = as_utc(bar.captured_at)
            ref = EvidenceRef(
                id=f"market_snapshot:{bar.id}",
                content_hash=content_hash(value),
                published_at=published,
                acquired_at=captured,
                available_at=max(published, captured),
                source=bar.provider,
            )
            refs.append(ref)
            facts.append({"ref": ref.id, "data": value})
        # Existing financial repository is date-only and defaults to relaxed
        # backfill. Query strict timestamps here and release date-only records
        # only after their publication date fully ended.
        rows = (
            await session.scalars(
                select(FinancialSnapshot)
                .where(
                    FinancialSnapshot.listing_id == listing_id,
                    FinancialSnapshot.report_period <= observation_date,
                    FinancialSnapshot.availability != "unknown",
                    FinancialSnapshot.available_from.is_not(None),
                    FinancialSnapshot.captured_at <= cutoff,
                )
                .order_by(FinancialSnapshot.report_period.desc(), FinancialSnapshot.version.desc())
            )
        ).all()
        financial = None
        financial_available_from: date | None = None
        for row in rows:
            effective = effective_available_from(row.fields, row.available_from)
            if effective is not None and end_of_business_day_utc(effective) <= cutoff:
                financial = row
                financial_available_from = effective
                break
        if financial is None:
            if require_financials:
                missing.append(f"financial_missing:{listing_id}")
        else:
            assert financial_available_from is not None
            value = {
                "listing_id": listing_id,
                "report_period": str(financial.report_period),
                "available_from": str(financial_available_from),
                "fields": financial.fields,
                "provider": financial.provider,
                "version": financial.version,
                "raw_ref": financial.raw_ref,
            }
            published = end_of_business_day_utc(financial_available_from)
            captured = as_utc(financial.captured_at)
            ref = EvidenceRef(
                id=f"financial_snapshot:{financial.id}",
                content_hash=content_hash(value),
                published_at=published,
                acquired_at=captured,
                available_at=max(published, captured),
                source=financial.provider,
            )
            refs.append(ref)
            facts.append({"ref": ref.id, "data": value})
    for item in sorted(evidence, key=lambda x: x.reference.id):
        ref = item.reference
        if item.kind != "fact" or item.source_scope != "public":
            raise ValueError("common_evidence_must_be_factual")
        if max(ref.published_at, ref.acquired_at, ref.available_at) > cutoff:
            raise ValueError("common_evidence_after_cutoff")
        payload = json.loads(item.payload_json)
        if ref.id.split(":", 1)[0] in {
            "account",
            "participant",
            "decision",
            "review",
            "order",
            "fill",
            "simulation_account",
            "simulation_participant",
            "simulation_decision",
            "simulation_review",
            "simulation_order",
            "simulation_fill",
        }:
            raise ValueError("private_evidence_in_common_package")
        _validate_public_payload(payload, set(listing_ids))
        if content_hash(payload) != ref.content_hash:
            raise ValueError("evidence_hash_mismatch")
        refs.append(ref)
        facts.append({"ref": ref.id, "data": payload})
    if len({ref.id for ref in refs}) != len(refs):
        raise ValueError("duplicate_common_evidence")
    refs.sort(key=lambda r: r.id)
    facts.sort(key=lambda r: str(r["ref"]))
    value = {
        "version": "simulation_common_v1",
        "cutoff": cutoff.isoformat(),
        "observation_date": str(observation_date),
        "history_start": str(history_start),
        "universe": sorted(listing_ids),
        "facts": facts,
        "evidence": [r.model_dump(mode="json") for r in refs],
        "missing_reasons": sorted(missing),
    }
    serialized = canonical_json(value)
    digest = content_hash(value)
    path = archive(digest, serialized)
    manifest = DataManifest(
        version="simulation_common_v1",
        content_hash=digest,
        cutoff=cutoff,
        universe=tuple(sorted(listing_ids)),
        evidence=tuple(refs),
        artifact_path=path,
        complete=not missing,
        missing_reasons=tuple(sorted(missing)),
    )
    return FrozenFacts(manifest=manifest, payload_json=serialized)


def verify_frozen_facts(facts: FrozenFacts) -> None:
    value = facts.payload()
    if facts.payload_json != canonical_json(value):
        raise ValueError("manifest_payload_not_canonical")
    if content_hash(value) != facts.manifest.content_hash:
        raise ValueError("manifest_hash_mismatch")
    manifest = facts.manifest
    if (
        value.get("cutoff") != manifest.cutoff.isoformat()
        or value.get("universe") != list(manifest.universe)
        or value.get("version") != manifest.version
        or value.get("missing_reasons") != list(manifest.missing_reasons)
        or value.get("evidence") != [r.model_dump(mode="json") for r in manifest.evidence]
        or manifest.complete != (not manifest.missing_reasons)
    ):
        raise ValueError("manifest_metadata_mismatch")
    records = value.get("facts")
    if not isinstance(records, list):
        raise ValueError("manifest_facts_mismatch")
    expected = {ref.id: ref for ref in manifest.evidence}
    seen = set()
    for record in records:
        if (
            not isinstance(record, dict)
            or set(record) != {"ref", "data"}
            or not isinstance(record["ref"], str)
            or record["ref"] not in expected
            or record["ref"] in seen
        ):
            raise ValueError("manifest_facts_mismatch")
        reference = expected[record["ref"]]
        if content_hash(record["data"]) != reference.content_hash:
            raise ValueError("manifest_fact_hash_mismatch")
        if (
            max(reference.published_at, reference.acquired_at, reference.available_at)
            > manifest.cutoff
        ):
            raise ValueError("manifest_evidence_after_cutoff")
        seen.add(record["ref"])
    if seen != set(expected):
        raise ValueError("manifest_facts_mismatch")


class PrivateContext(Contract):
    account: SimulationAccount
    settled_decisions: tuple[Decision, ...]
    matured_reviews: tuple[SimulationReview, ...]
    common: FrozenFacts


def own_account_context(
    *,
    scope: AccountScope,
    account: SimulationAccount,
    common: FrozenFacts,
    decisions: Sequence[Decision] = (),
    reviews: Sequence[SimulationReview] = (),
    settled_round_ids: Sequence[str] = (),
) -> PrivateContext:
    """Server binds scope and settled rounds. Reject, never silently trim, leaks."""
    verify_frozen_facts(common)
    if not common.manifest.complete:
        raise ValueError("common_data_incomplete")

    def same_scope(value: AccountScope) -> bool:
        return all(
            getattr(value, k) == getattr(scope, k)
            for k in ("experiment_id", "participant_id", "account_id")
        )

    if not same_scope(account) or any(not same_scope(v) for v in (*decisions, *reviews)):
        raise ValueError("cross_participant_context")
    cutoff = common.manifest.cutoff
    if len({d.id for d in decisions}) != len(decisions) or len({r.id for r in reviews}) != len(
        reviews
    ):
        raise ValueError("duplicate_private_context_record")
    historical_statuses = {"locked", "invalid", "timeout", "failed", "unknown", "cancelled"}
    if any(
        d.round_id not in settled_round_ids
        or d.submitted_at is None
        or d.submitted_at > cutoff
        or d.status not in historical_statuses
        or d.account_revision > account.revision
        for d in decisions
    ):
        raise ValueError("decision_not_settled_or_after_cutoff")
    decision_ids = {d.id for d in decisions}
    if any(
        r.status != "matured"
        or not r.memory_eligible
        or r.available_at > cutoff
        or r.decision_id not in decision_ids
        for r in reviews
    ):
        raise ValueError("review_not_matured_or_after_cutoff")
    return PrivateContext(
        account=account,
        common=common,
        settled_decisions=tuple(decisions),
        matured_reviews=tuple(reviews),
    )
