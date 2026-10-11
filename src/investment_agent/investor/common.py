"""Versioned wire primitives. No persistence or authorization is implied by parsing."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer, model_validator


def utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


def decimal_input(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("decimal values must use strings, integers or Decimal")
    return value


DecimalValue = Annotated[
    Decimal,
    BeforeValidator(decimal_input),
    Field(allow_inf_nan=False),
    PlainSerializer(str, return_type=str, when_used="json"),
]
Revision = Annotated[int, Field(strict=True, ge=1)]
Text = Annotated[str, Field(min_length=1, max_length=20_000)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    @model_validator(mode="after")
    def normalize_timestamps(self) -> "Contract":
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, datetime):
                object.__setattr__(self, name, utc_timestamp(value))
        return self


class ObjectRef(Contract):
    kind: Literal[
        "mandate",
        "case",
        "episode",
        "plan",
        "thesis",
        "draft",
        "publication_check", "research_progress", "publication_preparation",
        "evidence",
        "disclosure_original",
        "calculation",
        "recommendation",
        "monitor",
        "review",
        "decision",
        "proposal",
        "instruction",
        "memory",
        "method",
        "conversation",
        "message",
        "task",
        "listing",
        "market_snapshot",
        "financial_snapshot",
        "earnings_forecast",
        "disclosure_schedule",
        "business_segment",
        "candidate",
        "watchlist_item",
        "pipeline_run",
        "research_dataset",
        "portfolio_snapshot",
        "portfolio_valuation",
        "portfolio_plan",
        "portfolio_purposes",
    ]
    id: UUID | Annotated[int, Field(strict=True, ge=1)]
    revision: Revision
    field_path: str | None = None

    @model_validator(mode="after")
    def identifier_namespace(self) -> "ObjectRef":
        legacy = self.kind in {
            "listing",
            "market_snapshot",
            "financial_snapshot",
            "earnings_forecast",
            "disclosure_schedule",
            "business_segment",
            "candidate",
            "watchlist_item",
            "pipeline_run",
            "research_dataset",
        }
        if legacy != isinstance(self.id, int):
            raise ValueError("existing storage references use integer IDs; new objects use UUIDs")
        return self


OwnerId = Literal["local_owner"]


class Record(Contract):
    id: UUID
    schema_version: Literal[1] = 1
    revision: Revision
    created_at: datetime
    actor: Text
    origin_ref: ObjectRef | None


class Quantity(Contract):
    value: DecimalValue | None
    unit: Text
    currency: str | None = None
    unknown_reason: Text | None = None

    @model_validator(mode="after")
    def unknown_is_explicit(self) -> "Quantity":
        if (self.value is None) != (self.unknown_reason is not None):
            raise ValueError("unknown values require a reason; known values cannot have one")
        return self


class FieldError(Contract):
    field: Text
    message: Text


class BusinessError(Contract):
    code: Text
    message: Text
    field_errors: tuple[FieldError, ...] = ()
    retryable: bool
    current_refs: tuple[ObjectRef, ...] = ()


class Mutation(Contract):
    expected_revision: Revision
    idempotency_key: UUID
