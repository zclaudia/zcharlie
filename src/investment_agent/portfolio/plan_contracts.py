"""Immutable mutually exclusive whole-account proposals, never orders."""
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from investment_agent.investor.common import Contract, ObjectRef, Quantity, Record, Text
from investment_agent.investor.contracts import ContentNode

Stance = Literal["buy", "add", "hold", "reduce", "exit", "wait", "further_research"]
Constraint = Literal["max_single_weight", "max_sector_weight", "minimum_cash", "minimum_cash_weight"]


class PortfolioAction(Contract):
    scope: Literal["portfolio_action"] = "portfolio_action"
    id: UUID
    listing_ref: ObjectRef
    stance: Stance
    basis: Literal["company_thesis", "portfolio_constraint"]
    company_view_refs: tuple[ObjectRef, ...] = ()
    constraint: Constraint | None = None
    research_gap: Text | None = None
    # Exact output path, never a model-supplied quantity.
    target_ref: ObjectRef | None = None
    content: tuple[ContentNode, ...] = ()

    @model_validator(mode="after")
    def namespaces(self) -> "PortfolioAction":
        if self.listing_ref.kind != "listing" or self.listing_ref.field_path is not None:
            raise ValueError("exact listing required")
        if self.target_ref and (self.target_ref.kind != "calculation" or not self.target_ref.field_path):
            raise ValueError("target requires exact calculation output")
        if any(r.kind != "recommendation" or r.field_path for r in self.company_view_refs):
            raise ValueError("exact company views required")
        return self


class OptionInput(Contract):
    id: UUID
    scenario_index: Annotated[int, Field(strict=True, ge=1, le=20)]
    actions: Annotated[tuple[PortfolioAction, ...], Field(max_length=100)]


class PlanInput(Contract):
    account_id: UUID
    snapshot_ref: ObjectRef
    valuation_ref: ObjectRef | None = Field(default=None, exclude_if=lambda value: value is None)
    """The daily valuation the calculation was priced on (plan 2.5): the plan
    carries it so reads can mark a plan whose price basis has moved on."""
    mandate_ref: ObjectRef
    calculation_ref: ObjectRef
    previous_ref: ObjectRef | None
    options: Annotated[tuple[OptionInput, ...], Field(min_length=1, max_length=20)]
    question_scope: Text

    @model_validator(mode="after")
    def identities(self) -> "PlanInput":
        for ref, kind in ((self.snapshot_ref, "portfolio_snapshot"), (self.mandate_ref, "mandate"), (self.calculation_ref, "calculation")):
            if ref.kind != kind or ref.field_path is not None:
                raise ValueError("exact snapshot, mandate and calculation required")
        if self.valuation_ref is not None and (
            self.valuation_ref.kind != "portfolio_valuation"
            or self.valuation_ref.revision != 1
            or self.valuation_ref.field_path is not None
        ):
            raise ValueError("exact portfolio valuation required")
        if self.previous_ref and (self.previous_ref.kind != "portfolio_plan" or self.previous_ref.field_path):
            raise ValueError("explicit exact previous whole plan required")
        if len({o.id for o in self.options}) != len(self.options) or len({o.scenario_index for o in self.options}) != len(self.options):
            raise ValueError("unique mutually exclusive options required")
        actions = [a.id for o in self.options for a in o.actions]
        if len(set(actions)) != len(actions):
            raise ValueError("unique action identities required")
        return self


class PlanOption(Contract):
    id: UUID
    scenario_index: int
    name: Text
    actions: tuple[PortfolioAction, ...]
    totals: dict[str, Quantity]
    result_refs: tuple[ObjectRef, ...]
    violations: tuple[str, ...]
    unknowns: tuple[str, ...]
    executable: bool
    constraint_improvements: tuple[str, ...] = ()
    rendered_actions: dict[str, tuple[str, ...]] = {}


class PortfolioPlan(Record):
    scope: Literal["portfolio_plan"] = "portfolio_plan"
    account_id: UUID
    snapshot_ref: ObjectRef
    valuation_ref: ObjectRef | None = None
    """The exact valuation the plan's prices were frozen on (ag22-v2 plans)."""
    mandate_ref: ObjectRef
    calculation_ref: ObjectRef
    previous_ref: ObjectRef | None
    question_scope: Text
    as_of: datetime
    price_basis_at: datetime
    valid_until: datetime
    next_review_at: datetime
    no_action: PlanOption
    options: tuple[PlanOption, ...]
    mutually_exclusive: Literal[True] = True
    acceptance_reserves_cash: Literal[False] = False


class PlanView(Contract):
    plan: PortfolioPlan
    status: Literal["published", "review_required", "expired", "superseded"]
    limitation_reasons: tuple[str, ...]
    replacement_ref: ObjectRef | None
    current_refs: tuple[ObjectRef, ...]


class PlanSemanticInput(Contract):
    preparation_ref: ObjectRef
    # Each action must have a concrete premise/countercase/materiality assessment.
    assessments: dict[str, tuple[Text, Text, Text]]
    checked_sources: tuple[ObjectRef, ...]
    unresolved_questions: tuple[Text, ...] = ()
    conclusion: Literal["pass", "revise"]


class PlanSemanticReview(Contract):
    candidate_hash: Text
    assessments: dict[str, tuple[Text, Text, Text]]
    checked_sources: tuple[ObjectRef, ...]
    unresolved_questions: tuple[Text, ...]
    conclusion: Literal["pass", "revise"]
