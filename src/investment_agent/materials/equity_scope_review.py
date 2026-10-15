"""Pure validator for Costco's reviewed parent-equity scope bridge.

The review ties the two historical balance instants to explicit parent, NCI,
and total-equity rows from one filed SEC document. It does not read source bytes;
the annual-source verifier remains responsible for checking captured bytes and
matching each locator to the filing.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, Final

REVIEW_ID: Final = "costco.parent-equity.v1"
_SOURCE_DOCUMENT_ID: Final = "0000909832-23-000042"
_SOURCE_SHA256: Final = "4ad0ed774dc46baca606e6d6db5f86d841211c3de97986861df71194e6bd8cc1"
_SOURCE_URL: Final = (
    "https://www.sec.gov/Archives/edgar/data/909832/000090983223000042/cost-20230903.htm"
)
_SOURCE_PUBLISHED_ON: Final = "2023-10-11"
_CURRENCY: Final = "USD"
_RAW_UNIT: Final = "USD million"
_SCALE: Final = 1_000_000
_PERIODS: Final[dict[str, dict[str, Any]]] = {
    "2022-08-28": {
        "context_ref": "c-13",
        "parent": 20_642,
        "nci": 5,
        "total": 20_647,
        "nci_notation": "reported",
    },
    "2023-09-03": {
        "context_ref": "c-12",
        "parent": 25_058,
        "nci": 0,
        "total": 25_058,
        "nci_notation": "em_dash_zero",
    },
}
_SOURCE_REF_FIELDS: Final[frozenset[str]] = frozenset(
    {"document_id", "sha256", "published_on", "url"}
)
_COMPONENT_REF_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "document_id",
        "sha256",
        "published_on",
        "url",
        "context_ref",
        "tag",
        "source_label",
        "locator",
        "excerpt",
    }
)
_RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    {"id", "currency", "raw_unit", "scale", "period_type", "source_ref", "periods"}
)
_PERIOD_FIELDS: Final[frozenset[str]] = frozenset({"date", "context_ref", "parent", "nci", "total"})
_COMPONENT_FIELDS: Final[dict[str, frozenset[str]]] = {
    "parent": frozenset({"scope", "amount", "source_ref"}),
    "nci": frozenset({"scope", "amount", "amount_notation", "source_ref"}),
    "total": frozenset({"scope", "amount", "source_ref"}),
}
_SOURCE_FACTS: Final[dict[str, tuple[str, str, str]]] = {
    "parent": (
        "us-gaap:StockholdersEquity",
        "Total Costco stockholders’ equity",
        "Total Costco stockholders’ equity | 25,058 | 20,642",
    ),
    "nci": (
        "us-gaap:MinorityInterest",
        "Noncontrolling interests",
        "Noncontrolling interests | — | 5",
    ),
    "total": (
        "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "TOTAL EQUITY",
        "TOTAL EQUITY | 25,058 | 20,647",
    ),
}
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


def evaluate_equity_scope_review(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate Costco parent/NCI/total equity inputs and return a stable review.

    The reviewed vocabulary is deliberately narrow: only the two known Costco
    balance-sheet instants from the audited FY2023 Form 10-K are accepted. Any
    change to a scope, amount, period, source identity, or source-row citation
    raises ``ValueError``. The function is pure and performs no I/O.
    """

    _require_mapping(record, "record")
    _require_exact_fields(record, _RECORD_FIELDS, "record")
    if record["id"] != REVIEW_ID:
        raise ValueError(f"id must be {REVIEW_ID}")
    if record["currency"] != _CURRENCY:
        raise ValueError(f"currency must be {_CURRENCY}")
    if record["raw_unit"] != _RAW_UNIT:
        raise ValueError(f"raw_unit must be {_RAW_UNIT!r}")
    if _integer(record["scale"], "scale") != _SCALE:
        raise ValueError(f"scale must be {_SCALE}")
    if record["period_type"] != "instant":
        raise ValueError("period_type must be 'instant'")

    canonical_source = _require_mapping(record["source_ref"], "source_ref")
    _require_exact_fields(canonical_source, _SOURCE_REF_FIELDS, "source_ref")
    _validate_source_identity(canonical_source, "source_ref")

    raw_periods = record["periods"]
    if not isinstance(raw_periods, (list, tuple)) or len(raw_periods) != len(_PERIODS):
        raise ValueError("periods must contain exactly the two reviewed Costco balance instants")
    periods_by_date: dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(raw_periods):
        period = _require_mapping(value, f"periods[{index}]")
        _require_exact_fields(period, _PERIOD_FIELDS, f"periods[{index}]")
        period_date = period["date"]
        if not isinstance(period_date, str) or period_date not in _PERIODS:
            raise ValueError(f"periods[{index}].date is not a reviewed Costco balance instant")
        if period_date in periods_by_date:
            raise ValueError(f"duplicate reviewed Costco balance instant: {period_date}")
        periods_by_date[period_date] = period
    if set(periods_by_date) != set(_PERIODS):
        raise ValueError("periods must include both 2022-08-28 and 2023-09-03")

    normalized_periods: list[dict[str, Any]] = []
    for period_date in _PERIODS:
        period = periods_by_date[period_date]
        expected = _PERIODS[period_date]
        if period["context_ref"] != expected["context_ref"]:
            raise ValueError(
                f"{period_date}.context_ref must be {expected['context_ref']} for that instant"
            )

        normalized_components: dict[str, dict[str, Any]] = {}
        for component_name in ("parent", "nci", "total"):
            component = _require_mapping(period[component_name], f"{period_date}.{component_name}")
            _require_exact_fields(
                component,
                _COMPONENT_FIELDS[component_name],
                f"{period_date}.{component_name}",
            )
            if component["scope"] != component_name:
                raise ValueError(f"{period_date}.{component_name}.scope must be {component_name!r}")
            amount = _integer(component["amount"], f"{period_date}.{component_name}.amount")
            if amount != expected[component_name]:
                raise ValueError(
                    f"{period_date}.{component_name}.amount must be "
                    f"{expected[component_name]} USD million"
                )
            if component_name == "nci":
                notation = component["amount_notation"]
                if notation != expected["nci_notation"]:
                    raise ValueError(
                        f"{period_date}.nci.amount_notation must be {expected['nci_notation']!r}"
                    )

            source_ref = _require_mapping(
                component["source_ref"], f"{period_date}.{component_name}.source_ref"
            )
            _require_exact_fields(
                source_ref,
                _COMPONENT_REF_FIELDS,
                f"{period_date}.{component_name}.source_ref",
            )
            _validate_source_identity(source_ref, f"{period_date}.{component_name}.source_ref")
            for identity_field in _SOURCE_REF_FIELDS:
                if source_ref[identity_field] != canonical_source[identity_field]:
                    raise ValueError(
                        f"{period_date}.{component_name}.source_ref.{identity_field} "
                        "must match the canonical source_ref"
                    )
            if source_ref["context_ref"] != expected["context_ref"]:
                raise ValueError(
                    f"{period_date}.{component_name}.source_ref.context_ref must be "
                    f"{expected['context_ref']}"
                )

            expected_tag, expected_label, expected_excerpt = _SOURCE_FACTS[component_name]
            if source_ref["tag"] != expected_tag:
                raise ValueError(
                    f"{period_date}.{component_name}.source_ref.tag must be {expected_tag}"
                )
            if source_ref["source_label"] != expected_label:
                raise ValueError(
                    f"{period_date}.{component_name}.source_ref.source_label must be "
                    f"{expected_label!r}"
                )
            if source_ref["excerpt"] != expected_excerpt:
                raise ValueError(
                    f"{period_date}.{component_name}.source_ref.excerpt must preserve the "
                    "reviewed original source row"
                )
            locator = source_ref["locator"]
            if not isinstance(locator, (str, Mapping)) or not locator:
                raise ValueError(
                    f"{period_date}.{component_name}.source_ref.locator must be non-empty"
                )
            if (
                component_name == "nci"
                and notation == "em_dash_zero"
                and "—" not in source_ref["excerpt"]
            ):
                raise ValueError("em_dash_zero requires the original em dash in the source row")

            normalized_components[component_name] = {
                "scope": component_name,
                "amount": amount,
                **({"amount_notation": notation} if component_name == "nci" else {}),
                "source_ref": dict(source_ref),
            }

        if (
            normalized_components["parent"]["amount"] + normalized_components["nci"]["amount"]
            != normalized_components["total"]["amount"]
        ):
            raise ValueError(f"{period_date}: parent equity plus NCI must equal total equity")
        normalized_periods.append(
            {
                "date": period_date,
                "context_ref": expected["context_ref"],
                **normalized_components,
            }
        )

    normalized_inputs = {
        "id": REVIEW_ID,
        "currency": _CURRENCY,
        "raw_unit": _RAW_UNIT,
        "scale": _SCALE,
        "period_type": "instant",
        "source_ref": dict(canonical_source),
        "periods": normalized_periods,
    }
    digest = hashlib.sha256(_canonical_json(normalized_inputs)).hexdigest()
    return {
        "review_ref": f"sha256:{digest}",
        "review_id": REVIEW_ID,
        "inputs": normalized_inputs,
    }


def equity_scope_review_ref(record: Mapping[str, Any]) -> str:
    """Return the canonical content reference for a valid scope review."""

    reference = evaluate_equity_scope_review(record)["review_ref"]
    if not isinstance(reference, str):
        raise ValueError("scope reviewer returned a malformed review reference")
    return reference


def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{where} must be an object with string keys")
    return value


def _require_exact_fields(value: Mapping[str, Any], fields: frozenset[str], where: str) -> None:
    if frozenset(value) != fields:
        missing = sorted(fields - set(value))
        extra = sorted(set(value) - fields)
        raise ValueError(f"{where} fields must be exact (missing={missing}, extra={extra})")


def _validate_source_identity(source_ref: Mapping[str, Any], where: str) -> None:
    if source_ref["document_id"] != _SOURCE_DOCUMENT_ID:
        raise ValueError(f"{where}.document_id must be {_SOURCE_DOCUMENT_ID}")
    digest = source_ref["sha256"]
    if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
        raise ValueError(f"{where}.sha256 must be a lowercase SHA-256 digest")
    if digest != _SOURCE_SHA256:
        raise ValueError(f"{where}.sha256 must identify the reviewed original SEC filing")
    if source_ref["url"] != _SOURCE_URL:
        raise ValueError(f"{where}.url must be the reviewed official SEC filing URL")
    if source_ref["published_on"] != _SOURCE_PUBLISHED_ON:
        raise ValueError(f"{where}.published_on must be {_SOURCE_PUBLISHED_ON}")
    for field in ("document_id", "published_on", "url"):
        if not isinstance(source_ref[field], str) or not source_ref[field].strip():
            raise ValueError(f"{where}.{field} must be non-empty text")


def _integer(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} must be an integer")
    return value


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
        raise ValueError("scope review data must be canonical JSON data") from exc
