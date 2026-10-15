"""Pure evaluator for HSBC's audited IFRS 4 to IFRS 17 opening-equity bridge.

The evaluator validates the fixed, versioned input vocabulary and exact arithmetic,
then returns a derived value with a stable content reference. It checks citation
structure and binds every citation into the hash; it does not read or verify captured
source bytes, excerpts, publication dates, or source-document digests. The caller's
source verifier owns those checks before invoking this function.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Final

METHOD_VERSION: Final = "hsbc.ifrs17.ordinary_opening.v1"
_TRANSITION_PERIOD_END: Final = "2021-12-31"
_TRANSITION_DATE: Final = "2022-01-01"
_REPORT_PUBLISHED_ON: Final = "2024-02-21"
_CURRENCY: Final = "USD"
_SCALE: Final = 1_000_000
_INPUT_IDS: Final[tuple[str, ...]] = (
    "old_parent",
    "old_instruments",
    "old_ordinary",
    "new_parent",
    "new_instruments",
    "adjustment_1",
    "adjustment_2",
    "adjustment_3",
    "adjustment_4",
    "adjustment_5",
)
_BASIS_BY_ID: Final[dict[str, str]] = {
    "old_parent": "ifrs4",
    "old_instruments": "ifrs4",
    "old_ordinary": "ifrs4",
    "new_parent": "ifrs17",
    "new_instruments": "ifrs17",
    **{f"adjustment_{index}": "transition" for index in range(1, 6)},
}
_INPUT_FIELDS: Final[frozenset[str]] = frozenset(
    {"id", "value", "scale", "currency", "period_end", "basis", "source_ref"}
)
_SOURCE_REF_FIELDS: Final[frozenset[str]] = frozenset(
    {"document_id", "sha256", "locator", "excerpt", "source_label", "published_on"}
)
_DECIMAL_TEXT: Final[re.Pattern[str]] = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
_SHA256: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


def evaluate_equity_reconciliation(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and compute the audited HSBC ordinary-equity opening bridge.

    ``record`` must contain ``method_version`` and ten cited monetary inputs. A
    caller-supplied ``output`` or ``calculation_ref`` is optional; when present,
    each is checked against the derived result. The result is plain data and is
    deliberately not a ``FactRecord`` or a persisted database record.
    """

    normalized, output = _validated_values(record)
    input_hash = _input_hash(normalized, output)
    calculation_ref = f"sha256:{input_hash}"
    supplied_ref = record.get("calculation_ref")
    if supplied_ref is not None and supplied_ref != calculation_ref:
        raise ValueError("calculation_ref does not match the canonical reconciliation hash")
    supplied_output = record.get("output")
    if supplied_output is not None and supplied_output != output:
        raise ValueError("supplied output does not match the computed reconciliation")
    return {
        "calculation_ref": calculation_ref,
        "input_hash": input_hash,
        "method_version": METHOD_VERSION,
        "output": output,
        "inputs": normalized,
    }


def equity_reconciliation_ref(record: Mapping[str, Any]) -> str:
    """Return the canonical content reference for a valid reconciliation record."""

    reference = evaluate_equity_reconciliation(record)["calculation_ref"]
    if not isinstance(reference, str):
        raise ValueError("evaluator returned a malformed calculation reference")
    return reference


def _validated_values(
    record: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(record, Mapping):
        raise ValueError("reconciliation record must be a mapping")
    method_version = record.get("method_version")
    if method_version != METHOD_VERSION:
        raise ValueError(f"unsupported reconciliation method_version: {method_version!r}")
    unexpected = set(record) - {"method_version", "inputs", "output", "calculation_ref"}
    if unexpected:
        raise ValueError(f"unsupported reconciliation record fields: {sorted(unexpected)}")

    raw_inputs = record.get("inputs")
    if not isinstance(raw_inputs, (list, tuple)):
        raise ValueError("inputs must be a list of audited amount objects")
    by_id: dict[str, dict[str, Any]] = {}
    amounts: dict[str, Decimal] = {}
    for raw_input in raw_inputs:
        item = _mapping(raw_input, "input")
        if frozenset(item) != _INPUT_FIELDS:
            raise ValueError("input fields must exactly match the audited amount schema")
        identifier = item["id"]
        if not isinstance(identifier, str) or identifier not in _BASIS_BY_ID:
            raise ValueError(f"unknown reconciliation input id: {identifier!r}")
        if identifier in by_id:
            raise ValueError(f"duplicate reconciliation input id: {identifier}")
        _validate_input(item, identifier)
        by_id[identifier] = dict(item)
        amounts[identifier] = _decimal(item["value"], f"{identifier}.value")

    if set(by_id) != set(_INPUT_IDS):
        missing = sorted(set(_INPUT_IDS) - set(by_id))
        extra = sorted(set(by_id) - set(_INPUT_IDS))
        raise ValueError(f"input ids must be exact (missing={missing}, extra={extra})")

    exponents = [value.as_tuple().exponent for value in amounts.values()]
    minimum_exponent = min(exponent for exponent in exponents if isinstance(exponent, int))
    precision = max(max(value.adjusted() for value in amounts.values()) - minimum_exponent + 4, 28)
    with localcontext() as context:
        context.prec = precision
        old_ordinary = amounts["old_parent"] - amounts["old_instruments"]
        if old_ordinary != amounts["old_ordinary"]:
            raise ValueError(
                "IFRS 4 parent equity less other instruments must equal old ordinary equity"
            )
        transition_adjustment = sum(
            (amounts[f"adjustment_{index}"] for index in range(1, 6)), Decimal(0)
        )
        if amounts["old_parent"] + transition_adjustment != amounts["new_parent"]:
            raise ValueError(
                "IFRS 4 parent equity plus transition adjustments must equal IFRS 17 parent equity"
            )
        new_ordinary = amounts["new_parent"] - amounts["new_instruments"]
        if new_ordinary != old_ordinary + transition_adjustment:
            raise ValueError(
                "restated ordinary equity must reconcile through the parent-equity bridge"
            )
    output = {
        "metric": "equity_ordinary",
        "value": _decimal_text(new_ordinary),
        "scale": _SCALE,
        "currency": _CURRENCY,
        "period_end": _TRANSITION_PERIOD_END,
        "equity_scope": "ordinary_shareholders",
        "basis": "ifrs17",
    }
    normalized = [by_id[identifier] for identifier in _INPUT_IDS]
    return normalized, output


def _validate_input(item: Mapping[str, Any], identifier: str) -> None:
    _decimal(item["value"], f"{identifier}.value")
    scale = item["scale"]
    if isinstance(scale, bool) or not isinstance(scale, int) or scale != _SCALE:
        raise ValueError(f"{identifier}.scale must be {_SCALE}")
    if item["currency"] != _CURRENCY:
        raise ValueError(f"{identifier}.currency must be {_CURRENCY}")
    if item["period_end"] != _TRANSITION_PERIOD_END:
        raise ValueError(
            f"{identifier}.period_end must be {_TRANSITION_PERIOD_END}; "
            f"the transition date {_TRANSITION_DATE} is represented by its opening balance at period end"
        )
    expected_basis = _BASIS_BY_ID[identifier]
    if item["basis"] != expected_basis:
        raise ValueError(f"{identifier}.basis must be {expected_basis}")
    source_ref = _mapping(item["source_ref"], f"{identifier}.source_ref")
    if frozenset(source_ref) != _SOURCE_REF_FIELDS:
        raise ValueError(f"{identifier}.source_ref fields must exactly match the citation schema")
    for field in ("document_id", "excerpt", "source_label"):
        if not isinstance(source_ref[field], str) or not source_ref[field].strip():
            raise ValueError(f"{identifier}.source_ref.{field} must be non-empty text")
    digest = source_ref["sha256"]
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError(f"{identifier}.source_ref.sha256 must be a lowercase SHA-256 digest")
    published_on = source_ref["published_on"]
    if published_on != _REPORT_PUBLISHED_ON:
        raise ValueError(f"{identifier}.source_ref.published_on must be {_REPORT_PUBLISHED_ON}")
    locator = source_ref["locator"]
    if not isinstance(locator, (str, Mapping)) or not locator:
        raise ValueError(f"{identifier}.source_ref.locator must be non-empty text or an object")
    _canonical_json(source_ref)


def _mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{where} must be an object with string keys")
    return value


def _decimal(value: Any, where: str) -> Decimal:
    if not isinstance(value, str) or _DECIMAL_TEXT.fullmatch(value) is None:
        raise ValueError(f"{where} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{where} is not a valid decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{where} must be finite")
    return parsed


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def _input_hash(inputs: list[dict[str, Any]], output: dict[str, Any]) -> str:
    canonical = _canonical_json(
        {"method_version": METHOD_VERSION, "inputs": inputs, "output": output}
    )
    return hashlib.sha256(canonical).hexdigest()


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("reconciliation data must be canonical JSON data") from exc
