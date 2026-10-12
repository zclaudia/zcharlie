"""Offline import of the run-02 peer annual facts with per-field verification
proofs (WP3 of the industry & peer reference plan).

The importer reads the two fixed evidence files

* ``baijiu-peer-sample.json``  - FY2025 field values for the 19-member cohort,
* ``baijiu-official-audit.json`` - 114 per-field checks against the annual
  report originals (page, header alias, raw number, unit),

validates every binding itself (company, year, field, unit and value between
the two files, and inside each audit record) instead of trusting the audit's
``status`` flag, and only then appends immutable
:class:`~investment_agent.storage.models.peer.PeerAnnualSnapshot` rows. Any
single failed check aborts the whole batch before anything is written.

Field JSON follows the WP1 contract exactly
(:class:`~investment_agent.industry_peers.contracts.PeerAnnualField`):
decimal strings parsed straight from the JSON text (never through float),
period with explicit basis, original provider field name, raw value/unit, and
the import time as ``acquired_at`` (the research collection time is not
recorded per item in the run-02 files, so none is invented). The ROE percent
value is divided by 100 exactly once (``scaleb(-2)``), with the original
percent kept in ``raw_value``. Missing values need an explicit reason
(``--missing-reason CODE:METRIC=...``); an original ``0`` is a value, not a
missing. Booleans, strings, NaN and infinities are rejected with the offending
company and field named.

The per-field proof stored in the snapshot ``verification`` JSON records
everything the audit actually contains (page, header alias, raw number as
printed in the original, parsed value, unit, period, excerpt and its hash,
plus the audit record index for item-by-item traceability) and marks
everything it does not contain as an explicit ``None`` (original ObjectRef,
PDF hash, report identity, accounting scope, check time, checker, rule
version) - nothing is invented. ``revenue_total_check`` has no audit record in
run-02; it is stored as a ``candidate`` check-only field with an explicit note.

Idempotency: ``semantic_hash`` is sha256 over the canonical serialization of
amounts, semantics, source revision, verification proofs and status -
excluding the import time and any local path - so re-importing identical
content skips existing rows; changed values or proofs append a new version and
saved rows are never rewritten.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import typer
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.evidence.contracts import Period
from investment_agent.industry_peers.contracts import PeerAnnualField, PeerFieldVerification
from investment_agent.storage.database import run_with_engine, session_scope
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.peer import PeerAnnualSnapshotRepository

app = typer.Typer(
    help="Import peer annual fact snapshots with per-field verification proofs (WP3)"
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_DERIVED = _REPO_ROOT / "research" / "industry-data-validation" / "evidence" / "run-02" / "derived"
DEFAULT_SAMPLE_PATH = _DERIVED / "baijiu-peer-sample.json"
DEFAULT_AUDIT_PATH = _DERIVED / "baijiu-official-audit.json"

PROVIDER = "eastmoney"
ADAPTER_VERSION = "offline-run02-v1"
SEMANTIC_HASH_SCHEMA = "peer_annual_semantic_hash/1"
_AUDIT_JSON_NAME = "baijiu-official-audit.json"


@dataclass(frozen=True)
class FieldSpec:
    """One canonical metric and how it binds to the sample and audit JSONs."""

    metric: str
    sample_key: str
    api_key: str
    kind: str  # "flow" (Jan 1 - Dec 31), "stock" (Dec 31 point), "ratio" (full-year disclosed)
    unit: str
    currency: str | None
    raw_unit: str
    audited: bool


FIELD_SPECS: dict[str, FieldSpec] = {
    spec.metric: spec
    for spec in (
        FieldSpec("operating_revenue", "营业收入", "OPERATE_INCOME", "flow", "CNY", "CNY", "CNY", True),
        FieldSpec(
            "revenue_total_check", "营业总收入", "TOTAL_OPERATE_INCOME", "flow", "CNY", "CNY", "CNY", False
        ),
        FieldSpec("net_profit_parent", "归母净利润", "PARENT_NETPROFIT", "flow", "CNY", "CNY", "CNY", True),
        FieldSpec("equity_parent", "归母净资产", "TOTAL_PARENT_EQUITY", "stock", "CNY", "CNY", "CNY", True),
        FieldSpec("contract_liabilities", "合同负债", "CONTRACT_LIAB", "stock", "CNY", "CNY", "CNY", True),
        FieldSpec("operating_cash_flow", "经营现金流", "NETCASH_OPERATE", "flow", "CNY", "CNY", "CNY", True),
        FieldSpec("roe_weighted_annual", "加权平均ROE", "ROEJQ", "ratio", "fraction", None, "percent", True),
    )
}
AUDITED_METRICS: tuple[str, ...] = tuple(
    metric for metric, spec in FIELD_SPECS.items() if spec.audited
)
SAMPLE_METRICS: tuple[str, ...] = tuple(FIELD_SPECS)
# Sample keys the importer must ignore: derived ratios and their reasons belong
# to the calculation work package (WP5), never to the fact snapshot.
SAMPLE_DERIVED_PREFIXES = ("合同负债/营业收入", "CFO/归母净利润")


class _NonFinite:
    """Sentinel for a JSON ``NaN``/``Infinity`` token (rejected per field)."""

    __slots__ = ("token",)

    def __init__(self, token: str) -> None:
        self.token = token


@dataclass(frozen=True)
class CompanyPlan:
    """One company's validated facts, ready to append (identity resolved)."""

    code: str
    company_id: int
    listing_id: int
    report_period: date
    fields: dict[str, PeerAnnualField]
    verification: dict[str, dict[str, Any]]
    semantic_hash: str
    content_hash: str


@dataclass(frozen=True)
class ImportReport:
    """What one import run did (no partial-batch success exists)."""

    period: str
    companies: int
    fields_per_company: int
    verification_checks: int
    inserted: int
    skipped_existing: int
    semantic_hashes: dict[str, str] = dataclass_field(default_factory=dict)


class PeerAnnualImportError(ValueError):
    """The batch failed validation; every offending item is listed."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        heading = f"peer annual import rejected: {len(problems)} problem(s)"
        super().__init__(heading + "\n" + "\n".join(f"- {p}" for p in problems))


# --- loading -----------------------------------------------------------------


def load_sample_json(path: Path) -> dict[str, Any]:
    """Parse the sample JSON keeping decimal precision (never through float)."""

    data = _load_json(path)
    if not isinstance(data, dict):
        raise ValueError(f"sample JSON must be an object: {path}")
    return data


def load_audit_json(path: Path) -> list[dict[str, Any]]:
    """Parse the audit JSON keeping decimal precision (never through float)."""

    data = _load_json(path)
    if not isinstance(data, list):
        raise ValueError(f"audit JSON must be a list of check records: {path}")
    return data


def _load_json(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    return json.loads(
        text,
        parse_int=int,
        parse_float=Decimal,
        # NaN / Infinity tokens survive parsing only as sentinels so the
        # rejection can name the company and field instead of only the file.
        parse_constant=_NonFinite,
    )


# --- pure validation and planning (no database access) -----------------------


def build_import_plan(
    sample: dict[str, Any],
    audit: list[dict[str, Any]],
    *,
    acquired_at: datetime,
    identities: Mapping[str, tuple[int, int]],
    audit_source: str = _AUDIT_JSON_NAME,
    missing_reasons: Mapping[tuple[str, str], str] | None = None,
) -> list[CompanyPlan]:
    """Validate both files against each other and build one plan per company.

    Every consistency rule runs here, on parsed data only: structure, decimal
    integrity, the data-to-original binding for all audited fields (19 x 6 =
    114 in the run-02 golden), and unit consistency. Raises
    :class:`PeerAnnualImportError` listing every failing item when anything is off;
    the caller has written nothing at that point. ``identities`` maps each
    sample code to its ``(company_id, listing_id)`` from the stock directory.
    """

    problems: list[str] = []
    period = _sample_period(sample, problems)
    companies = _sample_companies(sample, period, missing_reasons or {}, problems)
    audits = _audit_index(audit, companies, period, problems)
    _check_bindings(companies, audits, problems)
    if problems:
        raise PeerAnnualImportError(problems)

    plans: list[CompanyPlan] = []
    for code, entry in sorted(companies.items()):
        company_id, listing_id = identities[code]
        fields: dict[str, PeerAnnualField] = {}
        proofs: dict[str, dict[str, Any]] = {}
        for metric, spec in FIELD_SPECS.items():
            entry_field, proof = _build_field(
                spec,
                value=entry["values"][metric],
                missing_reason=entry["reasons"][metric],
                record=audits.get((code, metric)),
                period=period,
                acquired_at=acquired_at,
                audit_source=audit_source,
            )
            fields[metric] = entry_field
            proofs[metric] = proof
        semantic_hash, content_hash = _hashes(fields, proofs)
        plans.append(
            CompanyPlan(
                code=code,
                company_id=company_id,
                listing_id=listing_id,
                report_period=period,
                fields=fields,
                verification=proofs,
                semantic_hash=semantic_hash,
                content_hash=content_hash,
            )
        )
    return plans


def _sample_period(sample: dict[str, Any], problems: list[str]) -> date:
    period_text = sample.get("period")
    if not isinstance(period_text, str):
        problems.append(f"sample 'period' must be a string, got {type(period_text).__name__}")
        raise PeerAnnualImportError(problems)
    try:
        period = date.fromisoformat(period_text)
    except ValueError as exc:
        problems.append(f"sample 'period' is not an ISO date: {period_text!r} ({exc})")
        raise PeerAnnualImportError(problems) from exc
    if period != period.replace(month=12, day=31):
        problems.append(f"sample 'period' must be a fiscal year end (Dec 31): {period_text}")
    return period


def _sample_companies(
    sample: dict[str, Any],
    period: date,
    missing_reasons: Mapping[tuple[str, str], str],
    problems: list[str],
) -> dict[str, dict[str, Any]]:
    """Per-company validated decimal values (or explicit missing reasons)."""

    raw_companies = sample.get("companies")
    if not isinstance(raw_companies, list) or not raw_companies:
        problems.append("sample 'companies' must be a non-empty list")
        raise PeerAnnualImportError(problems)

    companies: dict[str, dict[str, Any]] = {}
    for position, entry in enumerate(raw_companies):
        if not isinstance(entry, dict):
            problems.append(f"sample company #{position} must be an object")
            continue
        code = entry.get("code")
        if not isinstance(code, str) or not code:
            problems.append(f"sample company #{position} has no 'code'")
            continue
        if code in companies:
            problems.append(f"sample company {code}: duplicate code")
            continue
        entry_period = entry.get("period")
        if entry_period != period.isoformat():
            problems.append(
                f"sample company {code}: period {entry_period!r} does not match the "
                f"batch period {period.isoformat()}"
            )
        values: dict[str, Decimal | None] = {}
        reasons: dict[str, str | None] = {}
        for metric in SAMPLE_METRICS:
            spec = FIELD_SPECS[metric]
            if spec.sample_key not in entry:
                problems.append(
                    f"sample company {code}: field {spec.sample_key} ({metric}) is absent"
                )
                values[metric] = None
                reasons[metric] = None
                continue
            raw = entry[spec.sample_key]
            values[metric] = _checked_number(raw, f"sample company {code} field {metric}", problems)
            if raw is None:
                reason = missing_reasons.get((code, metric))
                if reason is None:
                    problems.append(
                        f"sample company {code} field {metric} is null and no missing reason "
                        "was given (--missing-reason CODE:METRIC=...); a missing value is "
                        "never silently dropped"
                    )
                reasons[metric] = reason
            else:
                reasons[metric] = None
        companies[code] = {"name": entry.get("name"), "values": values, "reasons": reasons}
    return companies


def _checked_number(raw: Any, where: str, problems: list[str]) -> Decimal | None:
    """Validate one JSON value as a decimal source number (or explicit null)."""

    if isinstance(raw, bool):
        problems.append(f"{where}: boolean values are rejected (got {raw})")
        return None
    if isinstance(raw, _NonFinite):
        problems.append(f"{where}: {raw.token} is not a finite number and is rejected")
        return None
    if raw is None:
        return None
    if isinstance(raw, str) or not isinstance(raw, (int, Decimal)):
        problems.append(
            f"{where}: expected a JSON number or null, got {type(raw).__name__} ({raw!r})"
        )
        return None
    return Decimal(raw)


def _sample_codes(sample: dict[str, Any]) -> list[str]:
    """The sample's company codes, far enough for identity resolution."""

    raw_companies = sample.get("companies")
    if not isinstance(raw_companies, list):
        return []
    return [
        entry["code"]
        for entry in raw_companies
        if isinstance(entry, dict) and isinstance(entry.get("code"), str) and entry["code"]
    ]


def _audit_index(
    audit: list[dict[str, Any]],
    companies: dict[str, dict[str, Any]],
    period: date,
    problems: list[str],
) -> dict[tuple[str, str], dict[str, Any]]:
    """Index the audit records by (code, metric) after checking their binding.

    The check set must be exactly one record per (company x audited field);
    the run-02 golden is 19 x 6 = 114. Extra, missing or duplicate records are
    batch failures, not warnings.
    """

    expected = {(code, metric) for code in companies for metric in AUDITED_METRICS}
    index: dict[tuple[str, str], dict[str, Any]] = {}
    for position, item in enumerate(audit):
        if not isinstance(item, dict):
            problems.append(f"audit record #{position} must be an object")
            continue
        code = item.get("code")
        sample_key = item.get("field")
        spec = next((s for s in FIELD_SPECS.values() if s.sample_key == sample_key), None)
        if spec is None:
            problems.append(f"audit record #{position} ({code}): unknown field {sample_key!r}")
            continue
        where = f"audit record #{position} ({code} {sample_key})"
        if code not in companies:
            problems.append(f"{where}: company {code!r} is not in the sample")
            continue
        key = (code, spec.metric)
        if key in index:
            problems.append(f"{where}: duplicate check record")
            continue
        if item.get("period") != period.isoformat():
            problems.append(
                f"{where}: period {item.get('period')!r} does not match the "
                f"batch period {period.isoformat()}"
            )
        if item.get("api_key") != spec.api_key:
            problems.append(
                f"{where}: api_key {item.get('api_key')!r} does not match the "
                f"expected {spec.api_key!r}"
            )
        api_value = _checked_number(item.get("api_value"), f"{where} api_value", problems)
        if api_value is None:
            problems.append(f"{where}: api_value is null; a matched original must record a value")
        matched = item.get("matched_row")
        if not isinstance(matched, dict):
            problems.append(
                f"{where}: no matched_row object (audit status {item.get('status')!r} is "
                "not trusted; the original row itself is required)"
            )
            continue
        _verify_matched_row(where, spec, api_value, matched, problems)
        index[key] = {
            "position": position,
            "item": item,
            "api_value": api_value,
            "matched_row": matched,
        }

    missing = sorted(expected - set(index))
    for code, metric in missing:
        problems.append(
            f"audit record missing for company {code} field "
            f"{FIELD_SPECS[metric].sample_key} ({metric}): {len(index)}/{len(expected)} "
            "expected check records present"
        )
    return index


def _verify_matched_row(
    where: str,
    spec: FieldSpec,
    api_value: Decimal | None,
    matched: dict[str, Any],
    problems: list[str],
) -> None:
    """Re-derive the audit's own matching from the recorded original row.

    The audit's ``status`` flag is never trusted; the recorded page, header,
    raw number, parsed value and unit must reproduce the match themselves.
    """

    unit = matched.get("unit")
    if unit != spec.raw_unit:
        problems.append(f"{where}: matched_row unit {unit!r} does not match the expected {spec.raw_unit!r}")
    if matched.get("scale") != 1:
        problems.append(
            f"{where}: matched_row scale {matched.get('scale')!r} is not supported by this "
            "importer (run-02 records scale 1)"
        )
    page = matched.get("pdf_page")
    if not isinstance(page, int) or isinstance(page, bool) or page < 1:
        problems.append(f"{where}: matched_row pdf_page must be a positive integer, got {page!r}")
    alias = matched.get("alias")
    if not isinstance(alias, str) or not alias:
        problems.append(f"{where}: matched_row alias (original header) is missing")
    excerpt = matched.get("excerpt")
    if not isinstance(excerpt, str) or not excerpt:
        problems.append(f"{where}: matched_row excerpt (original locator) is missing")

    raw_number = matched.get("raw_number")
    if not isinstance(raw_number, str) or not raw_number:
        problems.append(f"{where}: matched_row raw_number is missing")
        return
    cleaned = raw_number.replace(",", "").strip()
    if cleaned.endswith("%"):
        cleaned = cleaned[:-1]
    try:
        raw_decimal = Decimal(cleaned)
    except Exception:  # noqa: BLE001 - any malformed original number is a batch failure
        problems.append(f"{where}: matched_row raw_number {raw_number!r} is not a decimal number")
        return
    parsed_value = _checked_number(matched.get("parsed_first_value"), f"{where} parsed_first_value", problems)
    if parsed_value is not None and raw_decimal != parsed_value:
        problems.append(
            f"{where}: matched_row raw_number {raw_number!r} does not reproduce "
            f"parsed_first_value {_decimal_text(parsed_value)!r}"
        )
    if api_value is not None and parsed_value is not None and api_value != parsed_value:
        problems.append(
            f"{where}: api_value {_decimal_text(api_value)!r} does not match the original "
            f"row value {_decimal_text(parsed_value)!r}"
        )


def _check_bindings(
    companies: dict[str, dict[str, Any]],
    audits: dict[tuple[str, str], dict[str, Any]],
    problems: list[str],
) -> None:
    """The data-to-original binding: each sample value must be the audited one."""

    for (code, metric), record in sorted(audits.items()):
        if code not in companies:
            continue  # already reported as "not in the sample" by _audit_index
        sample_value = companies[code]["values"][metric]
        api_value = record["api_value"]
        sample_key = FIELD_SPECS[metric].sample_key
        where = f"binding company {code} field {sample_key} ({metric})"
        if sample_value is None:
            problems.append(
                f"{where}: the sample value is null but the original records "
                f"{_decimal_text(api_value)!r}; a missing value cannot be verified against "
                "an original and is rejected"
            )
        elif api_value is None:
            problems.append(f"{where}: the audit record carries no original value to bind")
        elif sample_value != api_value:
            problems.append(
                f"{where}: sample value {_decimal_text(sample_value)!r} does not match the "
                f"audited original value {_decimal_text(api_value)!r}"
            )


def _build_field(
    spec: FieldSpec,
    *,
    value: Decimal | None,
    missing_reason: str | None,
    record: dict[str, Any] | None,
    period: date,
    acquired_at: datetime,
    audit_source: str,
) -> tuple[PeerAnnualField, dict[str, Any]]:
    """One canonical field entry plus its per-field proof.

    ``value`` is ``None`` only for a source null with a caller-supplied reason;
    the binding checks already rejected nulls on audited metrics, so a missing
    value can only be the unaudited check-only field.
    """

    if value is None:
        entry = PeerAnnualField(
            metric=spec.metric,
            value=None,
            unit=spec.unit,
            currency=spec.currency,
            period=_period_for(spec, period),
            source_field=spec.api_key,
            raw_unit=spec.raw_unit,
            acquired_at=acquired_at,
            verification=PeerFieldVerification.CANDIDATE,
            missing_reason=missing_reason or "source value is null",
        )
        return entry, _proof_without_audit(spec, None, period, audit_source)

    # The /100 happens exactly once, here; the original percent stays in raw_value.
    saved_value = value.scaleb(-2) if spec.metric == "roe_weighted_annual" else value
    raw_value, raw_unit = _decimal_text(value), spec.raw_unit
    entry = PeerAnnualField(
        metric=spec.metric,
        value=saved_value,
        unit=spec.unit,
        currency=spec.currency,
        period=_period_for(spec, period),
        source_field=spec.api_key,
        raw_value=raw_value,
        raw_unit=raw_unit,
        # disclosed_on / updated_on / source_version: the run-02 derived files
        # carry no per-item disclosure or revision dates; left unset instead of
        # invented. The absence is recorded in the proof below.
        acquired_at=acquired_at,
        verification=(
            PeerFieldVerification.VERIFIED if spec.audited else PeerFieldVerification.CANDIDATE
        ),
    )
    if spec.audited:
        # Binding checks reject null values on audited metrics, so a record
        # must exist whenever the field is audited.
        assert record is not None, "audited metric without audit record"
        proof = _proof_from_audit(spec, record, period, audit_source)
    else:
        proof = _proof_without_audit(spec, value, period, audit_source)
    return entry, proof


def _period_for(spec: FieldSpec, period: date) -> Period:
    """Flows cover Jan 1 - Dec 31; stocks are the Dec 31 point; ROE is the
    disclosed full-year ratio."""
    if spec.kind == "flow":
        return Period(start=date(period.year, 1, 1), end=period, basis="period_to_date/annual")
    if spec.kind == "stock":
        return Period(start=period, end=period, basis="point_in_time")
    return Period(start=date(period.year, 1, 1), end=period, basis="annual_ratio")


def _proof_from_audit(
    spec: FieldSpec, record: dict[str, Any], period: date, audit_source: str
) -> dict[str, Any]:
    """The per-field proof, filled strictly from the audit record.

    Keys the audit does not carry are explicit ``None`` - the original
    ObjectRef, the PDF hash, the report identity, the accounting scope, the
    check time, the checker and the check rule version are simply not in the
    run-02 audit JSON, and none of them is fabricated here.
    """

    matched = record["matched_row"]
    excerpt: str = matched["excerpt"]
    return {
        "metric": spec.metric,
        "status": "verified",
        "original_ref": None,  # ObjectRef arrives when WP4 materializes evidence
        "pdf_hash": None,  # not present in the run-02 audit record
        "report_identity": None,  # not present in the run-02 audit record
        "pdf_page": matched["pdf_page"],
        "header": matched["alias"],
        "raw_value": matched["raw_number"],  # as printed in the original (e.g. "22.66%")
        "raw_unit": matched["unit"],  # CNY / percent as the audit recorded it
        "parsed_value": _decimal_text(Decimal(matched["parsed_first_value"])),
        "period": period.isoformat(),
        "accounting_scope": None,  # not stated by the audit record
        "locator_excerpt": excerpt,
        "locator_excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
        "checked_at": None,  # the run-02 audit carries no check timestamps
        "checked_by": None,  # no checker recorded
        "check_rule_version": None,  # no rule version recorded
        "audit_record": {
            "source": audit_source,
            "index": record["position"],
            "code": record["item"]["code"],
            "field": record["item"]["field"],
            "status_field": record["item"].get("status"),
            "api_key": record["item"]["api_key"],
            "api_value": _decimal_text(Decimal(record["api_value"])),
        },
    }


def _proof_without_audit(
    spec: FieldSpec, value: Decimal | None, period: date, audit_source: str
) -> dict[str, Any]:
    """Proof for the check-only field no run-02 audit record covers."""

    return {
        "metric": spec.metric,
        "status": "candidate",
        "original_ref": None,
        "pdf_hash": None,
        "report_identity": None,
        "pdf_page": None,
        "header": None,
        "raw_value": _decimal_text(value) if value is not None else None,
        "raw_unit": spec.raw_unit,
        "parsed_value": _decimal_text(value) if value is not None else None,
        "period": period.isoformat(),
        "accounting_scope": None,
        "locator_excerpt": None,
        "locator_excerpt_sha256": None,
        "checked_at": None,
        "checked_by": None,
        "check_rule_version": None,
        "audit_record": None,
        "note": (
            "check-only field (never a ratio denominator); no original audit "
            f"record exists in {audit_source}"
        ),
    }


# --- hashing (semantic version identity) -------------------------------------


def _hashes(
    fields: dict[str, PeerAnnualField], proofs: dict[str, dict[str, Any]]
) -> tuple[str, str]:
    """sha256 over the canonical semantic serialization of one company snapshot.

    Covers amounts, semantics, source revision, verification proofs and status;
    excludes the import time (``acquired_at``) and every local path, so the
    same content hashes the same on a re-run. ``content_hash`` is the payload
    identity of the same serialization and ``semantic_hash`` goes into the
    unique key. This importer stores no retry times or local paths, so the two
    agree; a provider that saves extra non-semantic payload splits them.
    """

    payload = {
        "schema": SEMANTIC_HASH_SCHEMA,
        "fields": {
            metric: {k: v for k, v in entry.model_dump(mode="json").items() if k != "acquired_at"}
            for metric, entry in fields.items()
        },
        "verification": proofs,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest, digest


def _decimal_text(value: Decimal) -> str:
    """The decimal string as the source wrote it (never through float)."""

    return str(value)


# --- database import ---------------------------------------------------------


async def resolve_identities(
    session: AsyncSession, codes: list[str]
) -> dict[str, tuple[int, int]]:
    """Map every sample code to ``(company_id, listing_id)`` via the directory.

    Resolution only walks the existing catalog (no name matching, no creation);
    any unresolvable code fails the whole batch before a single row is written.
    """

    repo = ListingRepository(session)
    resolved: dict[str, tuple[int, int]] = {}
    unknown: list[str] = []
    for code in codes:
        listing = await repo.get_a_share_listing(code)
        if listing is None:
            unknown.append(code)
            continue
        resolved[code] = (listing.company_id, listing.id)
    if unknown:
        raise ValueError(
            "companies not resolvable in the stock directory (import the roster or "
            "directory first); whole batch rejected: " + ", ".join(sorted(unknown))
        )
    return resolved


async def import_peer_annual_snapshots(
    session: AsyncSession,
    *,
    sample: dict[str, Any],
    audit: list[dict[str, Any]],
    acquired_at: datetime,
    provider: str = PROVIDER,
    adapter_version: str = ADAPTER_VERSION,
    audit_source: str = _AUDIT_JSON_NAME,
    missing_reasons: Mapping[tuple[str, str], str] | None = None,
) -> ImportReport:
    """Validate and append the whole batch, or write nothing.

    The plan (pure validation, all data-to-original binding checks) is built
    before anything is flushed; a rejected batch leaves no rows. Existing rows
    with the same unique key are skipped (idempotent re-import); changed
    content or proofs append a new version - saved rows are never rewritten.
    """

    identities = await resolve_identities(session, _sample_codes(sample))
    plans = build_import_plan(
        sample,
        audit,
        acquired_at=acquired_at,
        identities=identities,
        audit_source=audit_source,
        missing_reasons=missing_reasons,
    )

    repo = PeerAnnualSnapshotRepository(session)
    inserted = 0
    skipped = 0
    semantic_hashes: dict[str, str] = {}
    for plan in plans:
        semantic_hashes[plan.code] = plan.semantic_hash
        existing = await repo.by_unique_key(
            company_id=plan.company_id,
            report_period=plan.report_period,
            provider=provider,
            adapter_version=adapter_version,
            semantic_hash=plan.semantic_hash,
        )
        if existing is not None:
            skipped += 1
            continue
        await repo.add(
            PeerAnnualSnapshot(
                id=str(uuid.uuid4()),
                company_id=plan.company_id,
                listing_id=plan.listing_id,
                report_period=plan.report_period,
                provider=provider,
                adapter_version=adapter_version,
                captured_at=acquired_at,
                content_hash=plan.content_hash,
                semantic_hash=plan.semantic_hash,
                fields={metric: f.model_dump(mode="json") for metric, f in plan.fields.items()},
                verification=plan.verification,
            )
        )
        inserted += 1
    return ImportReport(
        period=plans[0].report_period.isoformat() if plans else str(sample.get("period")),
        companies=len(plans),
        fields_per_company=len(FIELD_SPECS),
        verification_checks=len(plans) * len(AUDITED_METRICS),
        inserted=inserted,
        skipped_existing=skipped,
        semantic_hashes=semantic_hashes,
    )


# --- CLI ---------------------------------------------------------------------


def _parse_missing_reasons(items: list[str]) -> dict[tuple[str, str], str]:
    """Parse ``CODE:METRIC=reason`` repetitions into a lookup map."""

    parsed: dict[tuple[str, str], str] = {}
    for item in items:
        head, separator, reason = item.partition("=")
        if not separator or not reason:
            raise typer.BadParameter(
                f"--missing-reason must be CODE:METRIC=reason, got {item!r}",
                param_hint="--missing-reason",
            )
        code, code_separator, metric = head.partition(":")
        if not code_separator or metric not in FIELD_SPECS:
            raise typer.BadParameter(
                "--missing-reason CODE:METRIC must name one of "
                f"{', '.join(SAMPLE_METRICS)}, got {head!r}",
                param_hint="--missing-reason",
            )
        parsed[(code, metric)] = reason
    return parsed


@app.command("import")
def import_annual(
    sample: Path = typer.Option(  # noqa: B008
        DEFAULT_SAMPLE_PATH, exists=True, dir_okay=False,
        help="Peer sample JSON with the FY field values (run-02 derived)",
    ),
    audit: Path = typer.Option(  # noqa: B008
        DEFAULT_AUDIT_PATH, exists=True, dir_okay=False,
        help="Official audit JSON with the per-field original checks (run-02 derived)",
    ),
    provider: str = typer.Option(  # noqa: B008
        PROVIDER, help="Source provider recorded on every snapshot row"
    ),
    adapter_version: str = typer.Option(  # noqa: B008
        ADAPTER_VERSION, help="Importer/adapter version recorded on every snapshot row"
    ),
    missing_reason: list[str] = typer.Option(  # noqa: B008
        [], help="CODE:METRIC=reason for a null source value; repeat as needed"
    ),
) -> None:
    """Import the annual facts and their per-field verification proofs.

    All data-to-original binding checks (114 in the run-02 golden) must pass
    or nothing is written. Exit 0: batch committed. Exit 1: rejected, with
    every offending item on stderr.
    """

    reasons = _parse_missing_reasons(list(missing_reason))
    acquired_at = datetime.now(UTC)
    sample_data = load_sample_json(sample)
    audit_data = load_audit_json(audit)
    settings = Settings()  # type: ignore[call-arg]

    async def main() -> ImportReport:
        async with session_scope() as session:
            return await import_peer_annual_snapshots(
                session,
                sample=sample_data,
                audit=audit_data,
                acquired_at=acquired_at,
                provider=provider,
                adapter_version=adapter_version,
                audit_source=Path(audit).name,
                missing_reasons=reasons,
            )

    try:
        report = run_with_engine(settings.database_url, main)
    except OperationalError as exc:
        typer.echo(
            "Database is not ready; run `ia migrate up` with the same DATABASE_URL.", err=True
        )
        raise typer.Exit(1) from exc
    except (PeerAnnualImportError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps(
            {
                "period": report.period,
                "companies": report.companies,
                "fields_per_company": report.fields_per_company,
                "verification_checks": report.verification_checks,
                "inserted": report.inserted,
                "skipped_existing": report.skipped_existing,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
