"""Freeze PIT CSRC copies; saved alternatives never borrow live membership."""

from copy import deepcopy
from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import Contract, ObjectRef, utc_timestamp
from investment_agent.portfolio.stress_canonical import canonical_hash
from investment_agent.portfolio.stress_contracts import FrozenIndustry, exact
from investment_agent.storage.models.pit_snapshot import IndustryClassification


def source_time(value: str | datetime) -> datetime:
    """Only trusted persisted SQLite/legacy AG22 timestamps may be naive UTC."""
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else utc_timestamp(parsed)


class InputDiagnostic(Contract):
    listing_ref: ObjectRef
    reason: str
    path_name: str


class IndustryEntry(Contract):
    listing_ref: ObjectRef
    path_name: str
    origin: Literal["current_pit", "saved_ag22"]
    classification: FrozenIndustry | None
    unknown_reason: str | None
    # Preserve the original system and naive/aware spelling as audit copies.
    source_copies: tuple[dict[str, Any], ...] = ()
    source_hashes: tuple[str, ...] = ()


class IndustryManifest(Contract):
    schema_version: Literal[1] = 1
    system: Literal["csrc"] = "csrc"
    entries: tuple[IndustryEntry, ...]

    def industries(self, path_name: str) -> tuple[FrozenIndustry, ...]:
        return tuple(entry.classification for entry in self.entries
                     if entry.path_name == path_name and entry.classification is not None)

    @property
    def diagnostics(self) -> tuple[InputDiagnostic, ...]:
        return tuple(InputDiagnostic(listing_ref=entry.listing_ref, reason=entry.unknown_reason,
                                     path_name=entry.path_name)
                     for entry in self.entries if entry.unknown_reason is not None)


def _copy(row: IndustryClassification) -> dict[str, Any]:
    return {"id": row.id, "system": row.system, "code": row.code, "source": row.source,
            "valid_from": row.valid_from.isoformat(), "valid_to": row.valid_to.isoformat() if row.valid_to else None,
            "captured_at": source_time(row.captured_at).isoformat()}


def _source_hash(listing_ref: ObjectRef, value: dict[str, Any]) -> str:
    copied = deepcopy(value)
    copied["captured_at"] = source_time(copied["captured_at"])
    return canonical_hash({"listing_ref": listing_ref, "source": copied})


def _entry(listing: ObjectRef, path: str, origin: Literal["current_pit", "saved_ag22"],
           copies: tuple[dict[str, Any], ...], valuation_date: date, cutoff: datetime) -> IndustryEntry:
    hashes: tuple[str, ...] = ()
    reason: str | None = None
    classification: FrozenIndustry | None = None
    try:
        hashes = tuple(_source_hash(listing, value) for value in copies)
        if not copies:
            reason = "industry_missing"
        elif len(copies) != 1:
            reason = "industry_conflict"
        else:
            value = copies[0]
            if value["system"] not in ("csrc", "CSRC") or (origin == "current_pit" and value["system"] != "csrc"):
                reason = "industry_system_unavailable"
            elif not value["code"]:
                reason = "industry_code_missing"
            else:
                classification = FrozenIndustry(row_id=value["id"], listing_ref=listing,
                    system="csrc", code=value["code"], source=value["source"],
                    valid_from=value["valid_from"], valid_to=value["valid_to"],
                    captured_at=source_time(value["captured_at"]))
                if (classification.captured_at > cutoff or classification.valid_from > valuation_date
                        or classification.valid_to is not None and classification.valid_to <= valuation_date):
                    classification, reason = None, "industry_outside_basis"
    except (ValidationError, ValueError, TypeError, KeyError):
        classification, reason = None, "industry_source_invalid"
    return IndustryEntry(listing_ref=listing, path_name=path, origin=origin,
        classification=classification, unknown_reason=reason, source_copies=deepcopy(copies), source_hashes=hashes)


class IndustryResolver:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def freeze(self, listing_refs: tuple[ObjectRef, ...], *, valuation_date: date,
                     knowledge_cutoff: datetime, path_name: str = "actual") -> IndustryManifest:
        cutoff = utc_timestamp(knowledge_cutoff)
        entries: list[IndustryEntry] = []
        with self.session.no_autoflush:
            for listing in listing_refs:
                exact(listing, "listing", immutable=True)
                rows = list(await self.session.scalars(select(IndustryClassification).where(
                    IndustryClassification.listing_id == listing.id, IndustryClassification.system == "csrc",
                    IndustryClassification.valid_from <= valuation_date,
                    IndustryClassification.valid_to.is_(None) | (IndustryClassification.valid_to > valuation_date),
                    IndustryClassification.captured_at <= cutoff,
                ).order_by(IndustryClassification.id).execution_options(populate_existing=True)))
                entries.append(_entry(listing, path_name, "current_pit", tuple(_copy(row) for row in rows),
                                      valuation_date, cutoff))
        return IndustryManifest(entries=tuple(entries))

    def freeze_saved(self, listing_refs: tuple[ObjectRef, ...], sources: tuple[dict[str, Any], ...], *,
                     valuation_date: date, knowledge_cutoff: datetime, path_name: str) -> IndustryManifest:
        entries: list[IndustryEntry] = []
        for listing in listing_refs:
            exact(listing, "listing", immutable=True)
            copies = tuple(deepcopy(item["industry"]) for item in sources
                           if ObjectRef.model_validate(item["listing_ref"]) == listing and item.get("industry") is not None)
            entries.append(_entry(listing, path_name, "saved_ag22", copies, valuation_date, utc_timestamp(knowledge_cutoff)))
        return IndustryManifest(entries=tuple(entries))

    async def check_sources(self, manifest: IndustryManifest) -> tuple[InputDiagnostic, ...]:
        """Report drift only. Never replace a frozen source with today's row."""
        changed: list[InputDiagnostic] = []
        with self.session.no_autoflush:
            for entry in manifest.entries:
                mismatch = len(entry.source_hashes) != len(entry.source_copies)
                for original, expected in zip(entry.source_copies, entry.source_hashes, strict=False):
                    row = await self.session.get(IndustryClassification, original.get("id"), populate_existing=True)
                    if row is None or row.listing_id != entry.listing_ref.id or _source_hash(entry.listing_ref, _copy(row)) != expected:
                        mismatch = True
                if mismatch:
                    changed.append(InputDiagnostic(listing_ref=entry.listing_ref, path_name=entry.path_name,
                                                   reason="source_changed"))
        return tuple(changed)
