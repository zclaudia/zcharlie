"""Bank-topic normalization; original tokens stay separate from canonical values."""

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import Literal
from uuid import UUID

from investment_agent.bank_topics.contracts import AnnualPeriod, FactMetric, TopicFactRevision
from investment_agent.investor.common import ObjectRef


@dataclass(frozen=True)
class NormalizedFactValue:
    value: Decimal
    unit: Literal["CNY", "fraction"]
    raw_value: str
    raw_unit: Literal["CNY_million", "percent"]


def normalize_fact_value(
    metric: FactMetric, raw_value: str, raw_unit: str,
) -> NormalizedFactValue:
    """Normalize once from an explicitly named original unit, never a saved value."""
    if metric not in {"nii", "nim", "nii_volume_effect", "nii_rate_effect", "nii_change_disclosed"}:
        raise ValueError("unsupported bank topic metric")
    expected = "percent" if metric == "nim" else "CNY_million"
    if raw_unit != expected:
        raise ValueError("bank topic raw unit differs from the metric")
    if not isinstance(raw_value, str) or len(raw_value) > 96 or not re.fullmatch(
        r"(?:-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?|\((?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?\))",
        raw_value,
    ):
        raise ValueError("bank topic original value must be a finite printed decimal token")
    number = Decimal(raw_value.replace(",", "").replace("(", "-").replace(")", ""))
    if len(number.as_tuple().digits) > 32:
        raise ValueError("bank topic printed number exceeds bounded precision")
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)) as context:
        context.prec = 40
        value = number * (Decimal("0.01") if metric == "nim" else Decimal(1000000))
    return NormalizedFactValue(value, "fraction" if metric == "nim" else "CNY", raw_value,
                               "percent" if metric == "nim" else "CNY_million")


# Internal observations are never request schemas or authorization credentials.
# Formal service entry points must obtain them from BankTopicReferenceResolver;
# constructing one (including proof_checked=True) does not grant source authority.
@dataclass(frozen=True)
class FactObservation:
    owner_id: str
    case_id: UUID
    listing_id: int
    stable_fact_id: str
    metric: FactMetric
    value: Decimal | None
    unit: str
    raw_value: str | None
    raw_unit: str | None
    period: AnnualPeriod
    comparison_period: AnnualPeriod | None
    reporting_scope: str
    accounting_standard: str
    currency: str
    verification: str
    unknown_reason: str | None
    source_refs: tuple[ObjectRef, ...]
    basis_kind: Literal["topic_fact", "peer_field", "evidence"]
    proof_checked: bool = False
    source_period_basis: str | None = None


def observation_from_topic_fact(
    fact: TopicFactRevision, *, listing_id: int, proof_checked: bool = False,
    source_refs: tuple[ObjectRef, ...] | None = None,
) -> FactObservation:
    return FactObservation(fact.owner_id, fact.case_id, listing_id, fact.stable_fact_id, fact.metric,
        fact.value, fact.unit, fact.raw_value, fact.raw_unit, fact.period, fact.comparison_period,
        fact.reporting_scope, fact.accounting_standard, fact.currency, fact.verification,
        fact.unknown_reason, fact.source_refs if source_refs is None else source_refs, "topic_fact", proof_checked)


def bank_content_hash(payload: object) -> str:
    """Canonical new-topic DTO payload hash, excluding its own content_hash field.

    Typed Decimal/UUID/UTC values are normalized; raw strings and array order
    remain meaningful. Existing peer/evidence hashes keep their own protocols.
    """
    import hashlib
    import json
    from datetime import date, datetime

    from pydantic import BaseModel

    from investment_agent.investor.common import utc_timestamp

    if isinstance(payload, BaseModel):
        payload = payload.model_dump(mode="python", exclude={"content_hash"})

    def canonical(value: object, depth: int = 0) -> object:
        if depth > 16:
            raise ValueError("bank payload is nested too deeply")
        if isinstance(value, Decimal):
            _, digits, exponent = value.as_tuple()
            if not value.is_finite() or not isinstance(exponent, int) or abs(exponent) > 256 or len(digits) > 256:
                raise ValueError("bank payload decimal exceeds canonical bounds")
            if value.is_zero():
                return "0"
            text = format(value, "f")
            return text.rstrip("0").rstrip(".") if "." in text else text
        if isinstance(value, datetime):
            stamp = utc_timestamp(value).isoformat(timespec="microseconds").removesuffix("+00:00")
            return stamp.rstrip("0").rstrip(".") + "Z"
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, UUID):
            return str(value)
        if value is None or isinstance(value, str | bool | int):
            return value
        if isinstance(value, tuple | list):
            if len(value) > 100:
                raise ValueError("bank payload array exceeds bounded capacity")
            return [canonical(item, depth + 1) for item in value]
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value) or len(value) > 100:
                raise ValueError("bank payload object keys are invalid")
            return {key: canonical(item, depth + 1) for key, item in value.items()}
        raise ValueError("bank payload contains an unsupported canonical value")

    encoded = json.dumps(canonical(payload), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()
