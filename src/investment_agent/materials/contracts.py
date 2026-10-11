"""Cross-source contract for immutable research materials (frozen, plan 02).

This module is the shared language of the HK/US material pipeline: adapters
build :class:`DatasetDraft` objects from :class:`FactRecord` values, the store
persists the canonical payload, evidence and metrics read it back. Later tasks
use these names and signatures; they do not change them.

Canonical payload shape — ``DatasetDraft.payload`` is always::

    {"facts": [<fact_to_payload output>, ...], **source_specific_keys}

``fact_to_payload`` / ``fact_from_payload`` are the only sanctioned
serializers ("03/04 只准用这对函数写 payload，07 只准用它读"): an entry's key
set is exactly :data:`FACT_ENTRY_KEYS` and a round trip through both functions
is the identity. Source-specific information (东财 ``source_item_code``, SEC
accession, page numbers, unmapped counts) belongs in top-level payload keys
outside ``facts`` or in ``original_locator``.

Two contract notes recorded deliberately:

* ``FACT_ENTRY_KEYS`` extends the plan's 12-key list with ``conflict_note``
  and ``source_version``. The frozen :class:`FactRecord` has both fields,
  validation rule 7 needs ``conflict_note`` on the payload, and the entry key
  set is exact — without the two keys the rules could not hold together. The
  plan's 12 keys are all present, unchanged.
* The ``identity`` argument of :class:`MaterialAdapter` is typed ``Any``:
  ``ControlledIdentity`` lives in task 01's ``domain.security`` and same-wave
  tasks must not import each other. Implementations annotate the concrete
  type; callers pass it through untouched.

Data discipline enforced here: missing is never zero (``raw_value`` is null
exactly when ``missing_reason`` is set), amounts are decimal strings (never
JSON floats), unknown publication precision keeps both time fields null, and a
period's real length is never back-computed from natural quarters.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

SCHEMA_VERSION = 1


class DatasetKind(StrEnum):
    """What a dataset holds. One dataset = one subject, source and period."""

    ANNUAL_FACTS = "annual_facts"
    """Annual-report facts (company level)."""
    FILING_INDEX = "filing_index"
    """Filing and version status (company level; ``period`` stays empty)."""
    PRICE_DAILY = "price_daily"
    """Daily prices (listing level; conditionally enabled)."""
    FX_DAILY = "fx_daily"
    """Daily FX rates (company or listing level; conditionally enabled)."""
    CORPORATE_ACTION = "corporate_action"
    """Distributions and other corporate actions (listing level; conditionally enabled)."""


class SubjectKind(StrEnum):
    """Which id space ``subject_id`` points into (``company.id`` / ``listing.id``)."""

    COMPANY = "company"
    LISTING = "listing"


class Verification(StrEnum):
    CANDIDATE = "candidate"
    VERIFIED = "verified"


class PeriodBasis(StrEnum):
    ANNUAL = "annual"
    INTERIM = "interim"
    QUARTER = "quarter"
    YTD = "ytd"
    INSTANT = "instant"
    DAILY = "daily"


class AccountingStandard(StrEnum):
    US_GAAP = "us_gaap"
    IFRS = "ifrs"
    HKFRS = "hkfrs"
    UNKNOWN = "unknown"


class ReportingScope(StrEnum):
    CONSOLIDATED = "consolidated"
    PARENT_ONLY = "parent_only"
    UNKNOWN = "unknown"


class EquityScope(StrEnum):
    ORDINARY = "ordinary_shareholders"
    ALL_PARENT = "all_parent_equity_holders"
    TOTAL = "total"
    UNKNOWN = "unknown"


class PerShareBasis(StrEnum):
    BASIC = "basic"
    DILUTED = "diluted"
    NA = "not_applicable"


class DatePrecision(StrEnum):
    TIMESTAMP = "timestamp"
    DATE = "date"
    UNKNOWN = "unknown"


FACT_METRICS: frozenset[str] = frozenset(
    {
        "revenue_operating",
        "revenue_gross",
        "revenue_net_credit",
        "credit_loss_expense",
        "cost_of_revenue",
        "net_profit",
        "profit_parent",
        "profit_ordinary_shareholders",
        "profit_other_equity_holders",
        "total_assets",
        "total_liabilities",
        "total_equity",
        "equity_parent",
        "equity_ordinary",
        "operating_cash_flow",
        "capex_ppe",
        "eps_basic",
        "eps_diluted",
        "shares_weighted_basic",
        "shares_outstanding_end",
    }
)

CURRENCY_UNITS: frozenset[str] = frozenset({"USD", "CNY", "HKD", "TWD"})
"""Units that are an amount of one currency: ``currency`` must repeat the unit."""

CURRENCY_FREE_UNITS: frozenset[str] = frozenset({"ratio", "shares", "percent"})
"""Units that are not currency amounts: ``currency`` must stay null."""

CONFLICT_STATES: tuple[str, ...] = ("none", "unresolved", "resolved")
APPLICABILITY_STATES: tuple[str, ...] = ("applicable", "not_applicable", "unknown")

#: The exact key set of one ``payload["facts"]`` entry.
FACT_ENTRY_KEYS: frozenset[str] = frozenset(
    {
        "metric",
        "period",
        "semantics",
        "value",
        "provenance",
        "verification",
        "conflict",
        "applicability",
        "source_label",
        "source_field",
        "original_locator",
        "missing_reason",
        "conflict_note",
        "source_version",
    }
)

_PERIOD_KEYS = frozenset({"start", "end", "basis", "label", "days"})
_SEMANTICS_KEYS = frozenset(
    {
        "metric",
        "source_label",
        "source_field",
        "accounting_standard",
        "reporting_scope",
        "equity_scope",
        "per_share_basis",
    }
)
_VALUE_KEYS = frozenset(
    {"raw_value", "raw_unit", "scale", "unit", "currency", "sign_convention", "missing_reason"}
)
_PROVENANCE_KEYS = frozenset(
    {
        "source",
        "accession",
        "original_hash",
        "source_published_at",
        "source_published_date",
        "source_published_precision",
        "accepted_at",
        "source_available_at",
    }
)


@dataclass(frozen=True)
class FactPeriod:
    """The real period a fact describes; 52/53-week spans keep their true length."""

    start: date | None
    end: date
    basis: PeriodBasis
    label: str | None = None
    """Fiscal-year label ("FY2024"); never derived from calendar quarters."""
    days: int | None = None
    """Actual day span when the source states one (52/53-week years)."""


@dataclass(frozen=True)
class FactSemantics:
    """What a fact means: metric, source labels, and the scope qualifiers."""

    metric: str
    source_label: str | None
    source_field: str | None
    accounting_standard: AccountingStandard
    reporting_scope: ReportingScope
    equity_scope: EquityScope
    per_share_basis: PerShareBasis


@dataclass(frozen=True)
class FactValue:
    """A raw value with its original unit and the canonical-conversion factors.

    ``raw_value`` is the source's decimal string (``scale`` turns it into the
    canonical ``unit``); null means missing, and then ``missing_reason`` is
    required — a missing value is never 0.
    """

    raw_value: str | None
    raw_unit: str
    scale: int
    unit: str
    currency: str | None
    sign_convention: str
    missing_reason: str | None = None


@dataclass(frozen=True)
class FactProvenance:
    """Where a fact came from and when it became public (possibly unknown)."""

    source: str
    accession: str | None
    original_hash: str | None
    source_published_at: datetime | None
    source_published_date: date | None
    source_published_precision: DatePrecision
    accepted_at: datetime | None
    source_available_at: datetime | None


@dataclass(frozen=True)
class FactRecord:
    """One fact: value, meaning, period, provenance and verification state."""

    metric: str
    period: FactPeriod
    semantics: FactSemantics
    value: FactValue
    provenance: FactProvenance
    verification: Verification
    conflict: str
    """``none`` | ``unresolved`` | ``resolved`` — conflicts stay visible."""
    applicability: str
    """``applicable`` | ``not_applicable`` | ``unknown``."""
    conflict_note: str | None = None
    original_locator: str | None = None
    """Page number, snippet or table locator in the original document."""
    source_version: str | None = None
    """Upstream revision (may be unknown); never the system's own version."""


@dataclass(frozen=True)
class DatasetDraft:
    """One not-yet-stored version of a source's material for one subject/period."""

    kind: DatasetKind
    subject_kind: SubjectKind
    subject_id: int
    source: str
    schema_version: int
    adapter_version: str
    captured_at: datetime
    """When the system really acquired the material (timezone-aware)."""
    payload: dict[str, Any]
    period: FactPeriod | None = None
    raw_bytes: bytes | None = None
    raw_media_type: str | None = None
    raw_source_url: str | None = None
    source_fetched_at: datetime | None = None
    """When the original research fetched it (imports only); never back-filled."""


@dataclass(frozen=True)
class UpstreamRequest:
    """One bounded upstream call an adapter asks the fetch stage to make."""

    name: str
    url: str
    params: tuple[tuple[str, str], ...] = ()
    media_type: str = "application/json"


@dataclass(frozen=True)
class RawPayload:
    """An upstream response as saved evidence: bytes, not a parsed structure."""

    name: str
    content: bytes
    media_type: str
    fetched_at: datetime
    source_url: str
    http_status: int


@dataclass(frozen=True)
class ParseResult:
    """What a parse produced, and the structural problems it must expose.

    Problems are not retried blindly: unmapped labels, pagination gaps and
    response-shape failures are reported so a human (or a later task) sees
    them instead of a silently short payload.
    """

    drafts: tuple[DatasetDraft, ...]
    problems: tuple[str, ...]


class MaterialAdapter(Protocol):
    """One source's request builder and parser (tasks 03/04 implement this).

    ``identity`` is ``investment_agent.domain.security.ControlledIdentity``
    (task 01), typed ``Any`` here because wave-1 modules must not import each
    other; implementations annotate the concrete type.
    """

    source: str
    adapter_version: str

    def requests(
        self, identity: Any, *, periods: Sequence[FactPeriod]
    ) -> tuple[UpstreamRequest, ...]: ...

    def parse(
        self, identity: Any, *, raw: Sequence[RawPayload], periods: Sequence[FactPeriod]
    ) -> ParseResult: ...


def payload_facts(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    """Read ``payload["facts"]``; raise ``ValueError`` when it is absent or wrong.

    ``validate_draft`` calls this first, so a ``ValueError`` here means the
    caller skipped validation.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object carrying a 'facts' list")
    facts = payload.get("facts")
    if not isinstance(facts, (list, tuple)):
        raise ValueError("payload['facts'] must be a list of fact entries")
    for index, entry in enumerate(facts):
        if not isinstance(entry, Mapping):
            raise ValueError(f"payload['facts'][{index}] must be an object")
    return tuple(facts)


def fact_to_payload(
    fact: FactRecord,
    *,
    source_label: str | None = None,
    source_field: str | None = None,
    original_locator: str | None = None,
) -> dict[str, Any]:
    """Serialize a fact into one canonical payload entry (keys = FACT_ENTRY_KEYS).

    The three keyword arguments override the corresponding fact fields when
    given; adapters use them to attach labels/locators discovered while
    parsing without rebuilding the semantics object.
    """

    semantics = replace(
        fact.semantics,
        source_label=fact.semantics.source_label if source_label is None else source_label,
        source_field=fact.semantics.source_field if source_field is None else source_field,
    )
    locator = fact.original_locator if original_locator is None else original_locator
    return {
        "metric": fact.metric,
        "period": _period_json(fact.period),
        "semantics": _semantics_json(semantics),
        "value": _value_json(fact.value),
        "provenance": _provenance_json(fact.provenance),
        "verification": _verification_json(fact.verification),
        "conflict": fact.conflict,
        "applicability": fact.applicability,
        "source_label": semantics.source_label,
        "source_field": semantics.source_field,
        "original_locator": locator,
        "missing_reason": fact.value.missing_reason,
        "conflict_note": fact.conflict_note,
        "source_version": fact.source_version,
    }


def fact_from_payload(entry: Mapping[str, Any]) -> FactRecord:
    """Rebuild a fact from one payload entry; raise ``ValueError`` on any drift.

    The nested blocks are authoritative; the flat mirrors (``source_label``,
    ``source_field``, ``missing_reason``) must agree with them.
    """

    if not isinstance(entry, Mapping):
        raise ValueError("fact entry must be an object")
    names = set(entry)
    if names != FACT_ENTRY_KEYS:
        missing = sorted(FACT_ENTRY_KEYS - names)
        extra = sorted(names - FACT_ENTRY_KEYS)
        raise ValueError(
            f"fact entry keys must equal FACT_ENTRY_KEYS (missing={missing}, extra={extra})"
        )
    metric = _text(entry["metric"], "metric")
    period = _period_from(entry["period"], "period")
    semantics = _semantics_from(entry["semantics"], "semantics")
    if semantics.metric != metric:
        raise ValueError(f"metric {metric!r} disagrees with semantics.metric {semantics.metric!r}")
    value = _value_from(entry["value"], "value")
    provenance = _provenance_from(entry["provenance"], "provenance")
    verification = _member(Verification, entry["verification"], "verification")
    conflict = _choice(entry["conflict"], CONFLICT_STATES, "conflict")
    applicability = _choice(entry["applicability"], APPLICABILITY_STATES, "applicability")
    if entry["source_label"] != semantics.source_label:
        raise ValueError("source_label disagrees with semantics.source_label")
    if entry["source_field"] != semantics.source_field:
        raise ValueError("source_field disagrees with semantics.source_field")
    if entry["missing_reason"] != value.missing_reason:
        raise ValueError("missing_reason disagrees with value.missing_reason")
    return FactRecord(
        metric=metric,
        period=period,
        semantics=semantics,
        value=value,
        provenance=provenance,
        verification=verification,
        conflict=conflict,
        applicability=applicability,
        conflict_note=_opt_text(entry["conflict_note"], "conflict_note"),
        original_locator=_opt_text(entry["original_locator"], "original_locator"),
        source_version=_opt_text(entry["source_version"], "source_version"),
    )


def validate_draft(draft: DatasetDraft) -> tuple[str, ...]:
    """Return every reason ``draft`` is not storable; empty means valid.

    Implements the eight plan-02 rules over the draft and its
    ``payload["facts"]`` entries, plus the frozen-shape checks: the entry key
    set equals :data:`FACT_ENTRY_KEYS`, ``fact_from_payload`` /
    ``fact_to_payload`` round-trip exactly, and the payload is canonical JSON
    (``allow_nan=False``; numbers such as NaN raise).
    """

    problems: list[str] = []
    if draft.schema_version != SCHEMA_VERSION:
        problems.append(
            f"schema_version {draft.schema_version!r} != SCHEMA_VERSION {SCHEMA_VERSION}"
        )
    problems.extend(_enum_problem(draft.kind, DatasetKind, "kind"))
    problems.extend(_enum_problem(draft.subject_kind, SubjectKind, "subject_kind"))
    if not isinstance(draft.subject_id, int) or isinstance(draft.subject_id, bool):
        problems.append("subject_id must be an integer")
    elif draft.subject_id <= 0:
        problems.append("subject_id must be positive")
    if not _nonempty(draft.source):
        problems.append("source must be a non-empty string")
    if not _nonempty(draft.adapter_version):
        problems.append("adapter_version must be a non-empty string")
    problems.extend(_timestamp_problems("captured_at", draft.captured_at))
    if draft.source_fetched_at is not None:
        problems.extend(_timestamp_problems("source_fetched_at", draft.source_fetched_at))
    if draft.raw_bytes is not None and not isinstance(draft.raw_bytes, bytes):
        problems.append("raw_bytes must be bytes")
    if draft.raw_media_type is not None and not _nonempty(draft.raw_media_type):
        problems.append("raw_media_type must be null or a non-empty string")
    if draft.raw_source_url is not None and not _nonempty(draft.raw_source_url):
        problems.append("raw_source_url must be null or a non-empty string")
    if draft.period is not None:
        problems.extend(_period_problems("period", draft.period))
    if not isinstance(draft.payload, dict):
        problems.append("payload must be a JSON object")
        return tuple(problems)
    try:
        json.dumps(draft.payload, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        problems.append(f"payload must be canonical-JSON serializable: {exc}")
    try:
        facts = payload_facts(draft.payload)
    except ValueError as exc:
        problems.append(f"payload: {exc}")
    else:
        for index, entry in enumerate(facts):
            problems.extend(_entry_problems(f"payload.facts[{index}]", entry))
    return tuple(problems)


def content_hash(draft: DatasetDraft) -> str:
    """Version identity: sha256 over the canonical payload JSON and raw bytes.

    Independent of ``captured_at`` / ``source_fetched_at`` (same content is
    the same version) and of payload key insertion order; ``raw_bytes`` are
    hashed after a separator so payload-only and empty-body hashes differ.
    Call ``validate_draft`` first: non-JSON payloads raise here.
    """

    canonical = json.dumps(
        draft.payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    digest = hashlib.sha256(canonical.encode("utf-8"))
    if draft.raw_bytes is not None:
        digest.update(b"\x00")
        digest.update(draft.raw_bytes)
    return digest.hexdigest()


# --- per-fact checks -------------------------------------------------------


def _entry_problems(where: str, entry: Mapping[str, Any]) -> list[str]:
    names = set(entry)
    if names != FACT_ENTRY_KEYS:
        missing = sorted(FACT_ENTRY_KEYS - names)
        extra = sorted(names - FACT_ENTRY_KEYS)
        return [f"{where} keys must equal FACT_ENTRY_KEYS (missing={missing}, extra={extra})"]
    try:
        fact = fact_from_payload(entry)
    except ValueError as exc:
        return [f"{where}: {exc}"]
    problems = _fact_problems(where, fact)
    try:
        rebuilt = fact_to_payload(fact)
    except ValueError as exc:
        problems.append(f"{where}: {exc}")
    else:
        if rebuilt != dict(entry):
            problems.append(
                f"{where} does not round-trip through fact_from_payload/fact_to_payload"
            )
    return problems


def _fact_problems(where: str, fact: FactRecord) -> list[str]:
    problems: list[str] = []
    if fact.metric not in FACT_METRICS:
        problems.append(f"{where}.metric {fact.metric!r} is not in FACT_METRICS")
    problems.extend(_period_problems(f"{where}.period", fact.period))
    problems.extend(_value_problems(f"{where}.value", fact.value))
    problems.extend(_provenance_problems(f"{where}.provenance", fact.provenance))
    if fact.conflict == "unresolved" and not _nonempty(fact.conflict_note):
        problems.append(f"{where}.conflict_note is required when conflict is unresolved")
    return problems


def _period_problems(where: str, period: FactPeriod) -> list[str]:
    problems: list[str] = []
    if not isinstance(period.end, date):
        problems.append(f"{where}.end must be a date")
        return problems
    if period.basis == PeriodBasis.INSTANT and period.start is not None:
        problems.append(f"{where}.start must be null for instant basis")
    if period.basis == PeriodBasis.ANNUAL and period.start is None:
        problems.append(f"{where}.start is required for annual basis")
    if period.start is not None:
        if period.start > period.end:
            problems.append(f"{where}.start must not be after end")
        else:
            span = (period.end - period.start).days
            if period.days is not None and period.days != span:
                problems.append(f"{where}.days {period.days} disagrees with the actual span {span}")
            if period.basis == PeriodBasis.ANNUAL and not 330 <= span <= 400:
                problems.append(f"{where} annual span must be 330..400 days, got {span}")
    return problems


def _value_problems(where: str, value: FactValue) -> list[str]:
    problems: list[str] = []
    if not _nonempty(value.raw_unit):
        problems.append(f"{where}.raw_unit must be a non-empty string")
    if not isinstance(value.scale, int) or isinstance(value.scale, bool):
        problems.append(f"{where}.scale must be an integer")
    elif value.scale <= 0:
        problems.append(f"{where}.scale must be positive")
    if not _nonempty(value.sign_convention):
        problems.append(f"{where}.sign_convention must be a non-empty string")
    problems.extend(_unit_problems(where, value))
    if value.raw_value is None:
        if not _nonempty(value.missing_reason):
            problems.append(f"{where}.raw_value is null: missing_reason is required")
    else:
        problems.extend(_decimal_problems(where, value.raw_value))
        if value.missing_reason is not None:
            problems.append(f"{where}.missing_reason must be null when raw_value is present")
    return problems


def _unit_problems(where: str, value: FactValue) -> list[str]:
    unit = value.unit
    if not _nonempty(unit):
        return [f"{where}.unit must be a non-empty string"]
    if unit in CURRENCY_UNITS:
        if value.currency != unit:
            return [f"{where}.unit {unit!r} requires currency {unit!r}"]
        return []
    if unit.endswith(("/share", "/shares")):
        if not _nonempty(value.currency):
            return [f"{where}.unit {unit!r} is per-share and requires a currency"]
        prefix = unit.split("/", 1)[0]
        if prefix in CURRENCY_UNITS and value.currency != prefix:
            return [f"{where}.unit {unit!r} requires currency {prefix!r}"]
        return []
    if unit in CURRENCY_FREE_UNITS:
        if value.currency is not None:
            return [f"{where}.unit {unit!r} must not carry a currency"]
        return []
    return []


def _decimal_problems(where: str, raw_value: object) -> list[str]:
    if not isinstance(raw_value, str):
        return [f"{where}.raw_value must be a decimal string, got {type(raw_value).__name__}"]
    if raw_value.strip() == "":
        return [f"{where}.raw_value must not be blank"]
    if raw_value != raw_value.strip():
        return [f"{where}.raw_value must not carry surrounding whitespace"]
    if "_" in raw_value:
        return [f"{where}.raw_value {raw_value!r} must not use digit separators"]
    try:
        parsed = Decimal(raw_value)
    except InvalidOperation:
        return [f"{where}.raw_value {raw_value!r} is not a decimal string"]
    if not parsed.is_finite():
        return [f"{where}.raw_value {raw_value!r} must be a finite decimal"]
    return []


def _provenance_problems(where: str, provenance: FactProvenance) -> list[str]:
    problems: list[str] = []
    if not _nonempty(provenance.source):
        problems.append(f"{where}.source must be a non-empty string")
    published = (provenance.source_published_at, provenance.source_published_date)
    if provenance.source_published_precision == DatePrecision.UNKNOWN:
        if any(value is not None for value in published):
            problems.append(
                f"{where}: unknown date precision requires both published fields to be null"
            )
    elif published == (None, None):
        problems.append(
            f"{where}: precision {provenance.source_published_precision.value!r} requires "
            "source_published_at or source_published_date"
        )
    for name, moment in (
        ("source_published_at", provenance.source_published_at),
        ("accepted_at", provenance.accepted_at),
        ("source_available_at", provenance.source_available_at),
    ):
        if moment is not None:
            problems.extend(_timestamp_problems(f"{where}.{name}", moment))
    return problems


def _timestamp_problems(where: str, value: object) -> list[str]:
    if not isinstance(value, datetime):
        return [f"{where} must be a timezone-aware datetime"]
    if value.tzinfo is None or value.utcoffset() is None:
        return [f"{where} must be timezone-aware"]
    return []


# --- facts JSON blocks -----------------------------------------------------


def _period_json(period: FactPeriod) -> dict[str, Any]:
    return {
        "start": _iso(period.start),
        "end": _iso(period.end),
        "basis": _member(PeriodBasis, period.basis, "period.basis").value,
        "label": period.label,
        "days": period.days,
    }


def _semantics_json(semantics: FactSemantics) -> dict[str, Any]:
    return {
        "metric": semantics.metric,
        "source_label": semantics.source_label,
        "source_field": semantics.source_field,
        "accounting_standard": _member(
            AccountingStandard, semantics.accounting_standard, "semantics.accounting_standard"
        ).value,
        "reporting_scope": _member(
            ReportingScope, semantics.reporting_scope, "semantics.reporting_scope"
        ).value,
        "equity_scope": _member(
            EquityScope, semantics.equity_scope, "semantics.equity_scope"
        ).value,
        "per_share_basis": _member(
            PerShareBasis, semantics.per_share_basis, "semantics.per_share_basis"
        ).value,
    }


def _value_json(value: FactValue) -> dict[str, Any]:
    return {
        "raw_value": value.raw_value,
        "raw_unit": value.raw_unit,
        "scale": value.scale,
        "unit": value.unit,
        "currency": value.currency,
        "sign_convention": value.sign_convention,
        "missing_reason": value.missing_reason,
    }


def _provenance_json(provenance: FactProvenance) -> dict[str, Any]:
    return {
        "source": provenance.source,
        "accession": provenance.accession,
        "original_hash": provenance.original_hash,
        "source_published_at": _iso(provenance.source_published_at),
        "source_published_date": _iso(provenance.source_published_date),
        "source_published_precision": _member(
            DatePrecision, provenance.source_published_precision, "provenance.precision"
        ).value,
        "accepted_at": _iso(provenance.accepted_at),
        "source_available_at": _iso(provenance.source_available_at),
    }


def _verification_json(verification: Verification) -> str:
    return _member(Verification, verification, "verification").value


def _period_from(entry: object, where: str) -> FactPeriod:
    block = _block(entry, _PERIOD_KEYS, where)
    return FactPeriod(
        start=_opt_date(block["start"], f"{where}.start"),
        end=_date(block["end"], f"{where}.end"),
        basis=_member(PeriodBasis, block["basis"], f"{where}.basis"),
        label=_opt_text(block["label"], f"{where}.label"),
        days=_opt_int(block["days"], f"{where}.days"),
    )


def _semantics_from(entry: object, where: str) -> FactSemantics:
    block = _block(entry, _SEMANTICS_KEYS, where)
    return FactSemantics(
        metric=_text(block["metric"], f"{where}.metric"),
        source_label=_opt_text(block["source_label"], f"{where}.source_label"),
        source_field=_opt_text(block["source_field"], f"{where}.source_field"),
        accounting_standard=_member(
            AccountingStandard, block["accounting_standard"], f"{where}.accounting_standard"
        ),
        reporting_scope=_member(
            ReportingScope, block["reporting_scope"], f"{where}.reporting_scope"
        ),
        equity_scope=_member(EquityScope, block["equity_scope"], f"{where}.equity_scope"),
        per_share_basis=_member(
            PerShareBasis, block["per_share_basis"], f"{where}.per_share_basis"
        ),
    )


def _value_from(entry: object, where: str) -> FactValue:
    block = _block(entry, _VALUE_KEYS, where)
    raw_value = block["raw_value"]
    if raw_value is not None and not isinstance(raw_value, str):
        raise ValueError(f"{where}.raw_value must be a decimal string or null")
    scale = block["scale"]
    if not isinstance(scale, int) or isinstance(scale, bool):
        raise ValueError(f"{where}.scale must be an integer")
    return FactValue(
        raw_value=raw_value,
        raw_unit=_text(block["raw_unit"], f"{where}.raw_unit"),
        scale=scale,
        unit=_text(block["unit"], f"{where}.unit"),
        currency=_opt_text(block["currency"], f"{where}.currency"),
        sign_convention=_text(block["sign_convention"], f"{where}.sign_convention"),
        missing_reason=_opt_text(block["missing_reason"], f"{where}.missing_reason"),
    )


def _provenance_from(entry: object, where: str) -> FactProvenance:
    block = _block(entry, _PROVENANCE_KEYS, where)
    return FactProvenance(
        source=_text(block["source"], f"{where}.source"),
        accession=_opt_text(block["accession"], f"{where}.accession"),
        original_hash=_opt_text(block["original_hash"], f"{where}.original_hash"),
        source_published_at=_opt_datetime(block["source_published_at"], f"{where}.published_at"),
        source_published_date=_opt_date(block["source_published_date"], f"{where}.published_date"),
        source_published_precision=_member(
            DatePrecision, block["source_published_precision"], f"{where}.precision"
        ),
        accepted_at=_opt_datetime(block["accepted_at"], f"{where}.accepted_at"),
        source_available_at=_opt_datetime(block["source_available_at"], f"{where}.available_at"),
    )


# --- scalar helpers --------------------------------------------------------


def _block(entry: object, keys: frozenset[str], where: str) -> Mapping[str, Any]:
    if not isinstance(entry, Mapping):
        raise ValueError(f"{where} must be a JSON object")
    names = set(entry)
    if names != keys:
        missing = sorted(keys - names)
        extra = sorted(names - keys)
        raise ValueError(f"{where} keys mismatch (missing={missing}, extra={extra})")
    return entry


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string")
    return value


def _opt_text(value: object, where: str) -> str | None:
    if value is None:
        return None
    return _text(value, where)


def _choice(value: object, allowed: tuple[str, ...], where: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"{where} must be one of {allowed}, got {value!r}")
    return value


def _member[E: StrEnum](enum_type: type[E], value: object, where: str) -> E:
    if not isinstance(value, str):
        raise ValueError(f"{where} must be a string, got {type(value).__name__}")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from exc


def _opt_int(value: object, where: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{where} must be an integer or null")
    return value


def _date(value: object, where: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{where} must be an ISO date string, got {type(value).__name__}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from exc


def _opt_date(value: object, where: str) -> date | None:
    if value is None:
        return None
    return _date(value, where)


def _opt_datetime(value: object, where: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{where} must be an ISO datetime string or null")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{where}: {exc}") from exc


def _iso(value: date | datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _enum_problem(value: object, enum_type: type[StrEnum], where: str) -> list[str]:
    if not isinstance(value, str):
        return [f"{where} must be a {enum_type.__name__} value, got {type(value).__name__}"]
    try:
        enum_type(value)
    except ValueError:
        return [f"{where} {value!r} is not a {enum_type.__name__} value"]
    return []
