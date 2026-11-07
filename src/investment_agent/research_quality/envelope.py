"""Frozen, canonical read manifests. These values confer no execution authority."""

from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from investment_agent.agent.method_receipt import RENDERER_VERSION, VALIDATOR_V1
from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.evidence.original_parser import VERSION as ORIGINAL_PARSER_VERSION
from investment_agent.evidence.queries import SecurityIdentity
from investment_agent.investor.common import Contract, ObjectRef, Text
from investment_agent.research_events.canonical import canonical_json, normalize, payload_hash
from investment_agent.research_quality.contracts import (
    MATERIAL_KINDS,
    Hash,
    MaterialMode,
    QualityCaseSaved,
    exact,
)

READ_PROFILE: Literal["quality.frozen-read.v1"] = "quality.frozen-read.v1"
READ_TOOLS = (
    "get_snapshot",
    "get_evidence",
    "get_event",
    "get_calculation",
    "read_disclosure_original",
    "search_saved_sources",
)


class EnvelopeVersions(Contract):
    schema_version: Literal[1] = 1
    renderer: Literal["quality.material-renderer.v1"] = "quality.material-renderer.v1"
    resolver: Literal["quality.exact-source.v1"] = "quality.exact-source.v1"
    search: Literal["quality.literal-search.v1"] = "quality.literal-search.v1"
    sort: Literal["canonical-ref.asc.v1"] = "canonical-ref.asc.v1"
    paging: Literal["offset-fixed-window.v1"] = "offset-fixed-window.v1"
    tool_profile: Literal["quality.frozen-read.v1"] = READ_PROFILE
    method_renderer: str = RENDERER_VERSION
    method_validator: str = VALIDATOR_V1
    original_parser: str = ORIGINAL_PARSER_VERSION
    response_char_limit: Annotated[int, Field(strict=True, ge=1024, le=64000)] = 32000
    search_page_size: Literal[20] = 20
    source_payload_char_limit: Literal[262144] = 262144


class FrozenBlob(Contract):
    original_ref: ObjectRef
    content_hash: Hash

    @model_validator(mode="after")
    def exact_blob(self) -> Self:
        exact(self.original_ref, "disclosure_original", immutable=True)
        return self


class FrozenMaterial(Contract):
    ref: ObjectRef
    subject_ref: ObjectRef
    category: Literal["evidence", "snapshot", "event", "calculation", "original", "dataset"]
    content_hash: Hash
    payload_hash: Hash
    payload: dict[str, Any]
    published_at: datetime
    acquired_at: datetime
    available_from: datetime
    locator: Text
    unit: str | None = None
    currency: str | None = None
    verification: Literal["candidate", "verified", "user_report", "not_applicable"]
    dependencies: tuple[ObjectRef, ...] = ()
    blob: FrozenBlob | None = None
    source_algorithm: Text

    @model_validator(mode="after")
    def integrity(self) -> Self:
        exact(self.subject_ref, "listing", immutable=True)
        if self.ref.kind not in MATERIAL_KINDS or self.payload_hash != payload_hash(self.payload):
            raise ValueError(
                "Material requires a supported exact source and canonical payload hash"
            )
        if len(canonical_json(self.payload)) > 262144:
            raise ValueError(
                "Material exceeds the frozen response source limit; select a saved fragment"
            )
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("Duplicate exact dependency")
        return self


class FrozenReadEnvelope(Contract):
    schema_version: Literal[1] = 1
    identity: SecurityIdentity
    identity_quality: Literal["stored_identity_name_not_historical"] = (
        "stored_identity_name_not_historical"
    )
    question: Text
    cutoff: datetime
    material_mode: MaterialMode
    materials: Annotated[tuple[FrozenMaterial, ...], Field(max_length=200)]
    gaps: tuple[Text, ...]
    versions: EnvelopeVersions = EnvelopeVersions()
    allowed_tools: tuple[str, ...] = READ_TOOLS

    @model_validator(mode="after")
    def bounded_manifest(self) -> Self:
        exact(self.identity.listing_ref, "listing", immutable=True)
        if not self.question.strip() or self.allowed_tools != READ_TOOLS:
            raise ValueError("Question and fixed read-only profile required")
        refs = tuple(item.ref for item in self.materials)
        if len(set(refs)) != len(refs) or refs != tuple(sorted(refs, key=ref_key)):
            raise ValueError("Materials must be unique and sorted by exact canonical reference")
        if tuple(sorted(set(self.gaps))) != self.gaps:
            raise ValueError("Gaps must be unique and sorted")
        if any(
            item.subject_ref != self.identity.listing_ref
            or max(item.published_at, item.acquired_at, item.available_from) > self.cutoff
            for item in self.materials
        ):
            raise ValueError("Every source must match the subject and all three cutoff boundaries")
        return self

    @property
    def content_hash(self) -> str:
        return payload_hash(self)


def ref_key(ref: ObjectRef) -> str:
    return canonical_json(ref)


def evidence_material(
    ref: ObjectRef,
    record: EvidenceRecord,
    *,
    category: Literal["evidence", "snapshot", "event", "original", "dataset"],
    source_algorithm: str,
    blob: FrozenBlob | None = None,
) -> FrozenMaterial:
    data = normalize(record)
    assert isinstance(data, dict)
    return FrozenMaterial(
        ref=ref,
        subject_ref=record.subject_ref,
        category=category,
        content_hash=record.content_hash,
        payload_hash=payload_hash(data),
        payload=data,
        published_at=record.published_at,
        acquired_at=record.acquired_at,
        available_from=record.available_from,
        locator=record.locator,
        unit=record.value.unit if record.value else None,
        currency=record.value.currency if record.value else None,
        verification=record.verification,
        dependencies=() if record.source_ref == ref else (record.source_ref,),
        blob=blob,
        source_algorithm=source_algorithm,
    )


def build_envelope(
    case: QualityCaseSaved,
    identity: SecurityIdentity,
    materials: tuple[FrozenMaterial, ...],
    *,
    gaps: tuple[str, ...] = (),
    versions: EnvelopeVersions | None = None,
) -> FrozenReadEnvelope:
    if identity.listing_ref != case.listing_ref or set(case.material_refs) != {
        item.ref for item in materials
    }:
        raise ValueError("The envelope must contain precisely the selected case references")
    return FrozenReadEnvelope(
        identity=identity,
        question=case.question,
        cutoff=case.cutoff,
        material_mode=case.material_mode,
        materials=tuple(sorted(materials, key=lambda item: ref_key(item.ref))),
        gaps=tuple(sorted(set((*case.gaps, *gaps)))),
        versions=versions or EnvelopeVersions(),
    )
