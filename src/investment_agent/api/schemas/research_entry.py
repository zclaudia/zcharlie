"""Closed contracts for the read-only first research guide."""

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import Field, field_validator

from investment_agent.investor.common import Contract, ObjectRef, Quantity, Text

TemplateKey = Literal["company_research", "cash_flow", "counter_evidence"]


class ResearchEntryInput(Contract):
    listing_ref: ObjectRef
    template_key: TemplateKey
    question: Text

    @field_validator("listing_ref")
    @classmethod
    def listing_only(cls, value: ObjectRef) -> ObjectRef:
        if value.kind != "listing" or value.revision != 1 or value.field_path is not None:
            raise ValueError("An exact listing at revision 1 is required")
        return value

    @field_validator("question")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A research question is required")
        return value.strip()


class EntryGap(Contract):
    code: str
    field: str
    message: str = ""


class EntryMaterial(Contract):
    ref: ObjectRef
    field: str
    source_ref: ObjectRef
    source: str
    locator: str
    report_period: date | None = None
    value: Quantity | None = None
    verification: Literal["candidate", "verified", "user_report"]
    status: Literal["ok", "missing", "conflict", "stale", "error", "pending"]


class EntryMethod(Contract):
    method_ref: ObjectRef
    binding_schema_version: Literal[1, 2]
    availability: Literal["available"] = "available"
    package_ref: ObjectRef | None = None


class EntryModel(Contract):
    availability: Literal["available", "unavailable"]
    policy: Literal["authorized_investor_model"] = "authorized_investor_model"
    model: str | None = None
    profile: str | None = None
    configuration_hash: str | None = None
    reason: str | None = None


class EntryBudget(Contract):
    requests: Annotated[int, Field(strict=True, gt=0)]
    tokens: Annotated[int, Field(strict=True, gt=0)]
    seconds: Annotated[float, Field(gt=0)]


class ResearchEntryResponse(Contract):
    listing_ref: ObjectRef
    template_key: TemplateKey
    question: Text
    material_refs: tuple[ObjectRef, ...] = ()
    coverage: Literal["partial", "missing"]
    gaps: tuple[EntryGap, ...] = ()
    method: EntryMethod
    model: EntryModel
    preflight_at: datetime
    materials: tuple[EntryMaterial, ...] = ()
    budget: EntryBudget
    calculability: Literal["inputs_visible", "missing_inputs"]
    notice: str = "即时预检仅说明已保存材料，不保证覆盖完整或后续准入、研究与观点发布成功；发送时会重新检查权限、模型和预算。"
