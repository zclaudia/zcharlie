"""Evidence and deterministic calculation records; values retain their identity and time."""

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import model_validator

from investment_agent.investor.common import Contract, ObjectRef, Quantity, Record, Text
from investment_agent.investor.enums import CalculationStatus


class DatePrecision(StrEnum):
    """How precisely a stored source states its publication time.

    Deliberately local to the evidence layer: ``materials.contracts`` defines
    the same three values for the material layer, and evidence must not import
    materials (the material adapters build on evidence contracts, not the other
    way around). A test asserts the two value sets stay identical.
    """

    TIMESTAMP = "timestamp"
    DATE = "date"
    UNKNOWN = "unknown"


class Period(Contract):
    start: date
    end: date
    basis: Text

    @model_validator(mode="after")
    def ordered(self) -> "Period":
        if self.start > self.end:
            raise ValueError("period start exceeds end")
        return self


class DocumentProvenance(Contract):
    parser_version: Text
    original_hash: Text
    fragment_hash: Text
    metric: Text
    raw_unit: Text
    raw_currency: Text
    raw_value: Text
    crosscheck_refs: tuple[ObjectRef, ...] = ()
    verification_reason: Text
    reporting_scope: Literal["consolidated", "unknown"] = "unknown"
    header_locators: tuple[str, ...] = ()
    header_hashes: tuple[str, ...] = ()
    correction_locator: str | None = None
    correction_fragment_hash: str | None = None


class EvidenceRecord(Record):
    source_ref: ObjectRef
    source_kind: Literal["snapshot", "document", "user_report", "research_dataset"]
    locator: Text
    content_hash: Text
    subject_ref: ObjectRef
    period: Period | None
    published_at: datetime
    acquired_at: datetime
    available_from: datetime
    verification: Literal["candidate", "verified", "user_report"]
    conflict: Literal["none", "unresolved", "resolved"]
    date_precision: DatePrecision | None = None
    """Optional addition: the source's stated precision for ``published_at``.

    Existing payloads have no such key and keep meaning "precision not recorded"
    (``None``); material-backed evidence sets it explicitly. A ``date``-precision
    record's ``published_at`` is midnight UTC and ``available_from`` starts the
    next day, so the boundary never claims a known time of day.
    """
    value: Quantity | None = None
    text: str | None = None
    correction_refs: tuple[ObjectRef, ...] = ()
    document_provenance: DocumentProvenance | None = None

    @model_validator(mode="after")
    def user_text_is_not_verified(self) -> "EvidenceRecord":
        if self.source_kind == "user_report" and self.verification != "user_report":
            raise ValueError("user reports remain user_report until separate verification")
        if self.value is None and self.text is None:
            raise ValueError("evidence requires saved value or text")
        return self


class CalculationAssumption(Contract):
    """Saved scenario input, explicitly separated from historical fact references."""

    name: Text
    value: Quantity
    provenance: Literal["method_default", "user_scenario", "agent_scenario"]
    source_ref: ObjectRef | None = None
    rationale: str | None = None


class CalculationRecord(Record):
    method: Text
    method_version: Text
    fact_refs: tuple[ObjectRef, ...]
    assumption_refs: tuple[ObjectRef, ...]
    input_hash: Text
    assumptions: tuple[CalculationAssumption, ...] = ()
    as_of: datetime
    applicable: bool
    status: CalculationStatus
    results: dict[str, Quantity]
    quality: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    # Versioned method-owned input snapshots and per-scenario diagnostics.
    # Existing records without these fields retain their original meaning.
    details: dict[str, Any] = {}

    @model_validator(mode="after")
    def failed_calculations_have_no_results(self) -> "CalculationRecord":
        if self.status != CalculationStatus.OK and self.results:
            raise ValueError("failed calculations cannot contain successful results")
        if self.status == CalculationStatus.OK and not self.applicable:
            raise ValueError("successful calculation must be applicable")
        return self


class ToolResult[T](Contract):
    status: Literal["ok", "missing", "conflict", "stale", "error", "pending"]
    data: T | None
    refs: tuple[ObjectRef, ...]
    as_of: datetime
    coverage: Text
    warnings: tuple[str, ...] = ()
    dependency_id: str | None = None

    @model_validator(mode="after")
    def pending_has_dependency(self) -> "ToolResult[T]":
        if (self.status == "pending") != (self.dependency_id is not None):
            raise ValueError("only pending results require a dependency ID")
        return self
