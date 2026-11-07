"""Local S1 canonical values; no event subsystem or provider dependency."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

from investment_agent.investor.common import utc_timestamp

MAX_INPUT_DIGITS = 40
MAX_INPUT_DECIMAL_PLACES = 18


def input_decimal(value: object) -> Decimal:
    """Reject dangerous exponents before formatting or a precision allocation.

    Limits apply to the numeric value, so redundant trailing fractional zeros
    do not change whether an otherwise equivalent input is accepted.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("Use a decimal string or integer, never float/bool")
    if isinstance(value, str) and len(value) > 256:
        raise ValueError("Decimal input text exceeds bounded length")
    if isinstance(value, int) and value.bit_length() > 136:
        raise ValueError("Decimal input exceeds 40 digits")
    try:
        number = Decimal(value)
    except (ValueError, ArithmeticError) as exc:
        raise ValueError("Invalid decimal input") from exc
    if not number.is_finite():
        raise ValueError("Decimal must be finite")
    _, digits, exponent = number.as_tuple()
    if not isinstance(exponent, int) or abs(exponent) > 256 or len(digits) > 256:
        raise ValueError("Decimal exponent or coefficient exceeds bounded input")
    if number.is_zero():
        return Decimal(0)
    # Strip trailing coefficient zeros without consulting Decimal context.
    significant = list(digits)
    while significant[-1] == 0:
        significant.pop()
        exponent += 1
    integer_places = max(len(significant) + exponent, 0)
    fractional_places = max(-exponent, 0)
    if max(len(significant), integer_places + fractional_places) > MAX_INPUT_DIGITS:
        raise ValueError("Decimal input exceeds 40 digits")
    if fractional_places > MAX_INPUT_DECIMAL_PLACES:
        raise ValueError("Decimal input exceeds 18 decimal places")
    return number


def decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Nonfinite decimal cannot be hashed")
    _, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int) or abs(exponent) > 4096 or len(digits) > 4096:
        raise ValueError("Decimal exceeds canonical expansion bound")
    if value.is_zero():
        return "0"
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def normalize(value: object) -> object:
    if isinstance(value, BaseModel):
        result: dict[str, object] = {}
        for name, field in type(value).model_fields.items():
            key = field.serialization_alias or field.alias or name
            item = normalize(getattr(value, name))
            extra = field.json_schema_extra
            if isinstance(extra, dict) and extra.get("canonical_set"):
                if not isinstance(item, list):
                    raise ValueError("A canonical set must be an array")
                members = {canonical_json(member): member for member in item}
                item = [members[key] for key in sorted(members)]
            result[key] = item
        return result
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, datetime):
        stamp = utc_timestamp(value).isoformat(timespec="microseconds")
        return stamp.removesuffix("+00:00").rstrip("0").rstrip(".") + "Z"
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, (tuple, list)):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("Canonical object keys must be strings")
        return {key: normalize(item) for key, item in value.items()}
    raise ValueError(f"Unsupported canonical type: {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(
        normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def canonical_hash(value: object) -> str:
    """Hash parsed DTOs for semantic numeric/UTC/default normalization."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
