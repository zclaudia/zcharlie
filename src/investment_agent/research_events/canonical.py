"""Typed canonical bytes and deterministic source identities (BASE-02 v1)."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID, uuid5

from pydantic import BaseModel

from investment_agent.investor.common import utc_timestamp

if TYPE_CHECKING:
    from investment_agent.research_events.contracts import EventSourceRef

EVENT_NAMESPACE = UUID("30d5fd64-e30f-553b-930e-291bbad453ea")


def decimal_text(value: Decimal) -> str:
    """Avoid context-sensitive normalize() and unbounded exponent expansion."""
    if not value.is_finite():
        raise ValueError("non-finite decimals are not canonical")
    if value.is_zero():
        return "0"
    # Hashing is not a numeric-computation API. Reject pathological expansion
    # before format allocates memory; domain input limits may be stricter.
    if abs(value.as_tuple().exponent) > 10_000 or len(value.as_tuple().digits) > 10_000:  # type: ignore[arg-type]
        raise ValueError("decimal exceeds canonical expansion bound")
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def normalize(value: object) -> object:
    """Normalize declared types; ordinary strings and ordered lists stay exact."""
    if isinstance(value, BaseModel):
        result: dict[str, object] = {}
        for name, field in type(value).model_fields.items():
            normalized = normalize(getattr(value, name))
            extra = field.json_schema_extra
            if (isinstance(extra, dict) and extra.get("canonical_set")) or name in getattr(type(value), "canonical_set_fields", ()):
                if not isinstance(normalized, list):
                    raise ValueError("canonical_set requires an array")
                members = {canonical_json(member): member for member in normalized}
                normalized = [members[key] for key in sorted(members)]
            result[name] = normalized
        return result
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, datetime):
        stamp = utc_timestamp(value).isoformat(timespec="microseconds")
        body = stamp.removesuffix("+00:00").rstrip("0").rstrip(".")
        return body + "Z"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (list, tuple)):
        return [normalize(member) for member in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("canonical objects require string keys")
        return {key: normalize(member) for key, member in value.items()}
    # In particular, never let a float enter routing hashes (including NaN).
    raise ValueError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(normalize(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def canonical_bytes(value: object) -> bytes:
    return canonical_json(value).encode("utf-8")


def payload_hash(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def source_identity(source_ref: EventSourceRef) -> str:
    from investment_agent.research_events.contracts import SOURCE_REF_ADAPTER

    source = SOURCE_REF_ADAPTER.validate_python(source_ref)
    return canonical_json({"kind": source.kind, "id": source.id})


def event_id(owner_id: str, event_type: str, source_ref: EventSourceRef,
             source_revision: int) -> UUID:
    from investment_agent.research_events.contracts import SOURCE_REF_ADAPTER

    source = SOURCE_REF_ADAPTER.validate_python(source_ref)
    if type(source_revision) is not int or source_revision < 1 or source.revision != source_revision:
        raise ValueError("source revision must equal the strict positive reference revision")
    key = [owner_id, event_type, {"kind": source.kind, "id": source.id}, source_revision]
    return uuid5(EVENT_NAMESPACE, canonical_json(key))
