"""Fail-closed policy evaluation for controlled research-material sources.

Licensing and technical approval are read from a versioned, controlled YAML
record, independently of runtime operational settings. ``approvals`` exists
for explicit test injection and controlled offline evaluation only.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from investment_agent.config import Settings
from investment_agent.domain.security import ControlledIdentity

DEFAULT_SOURCE_POLICY_PATH = (
    Path(__file__).resolve().parents[3]
    / "config"
    / "research_material"
    / "source_policy.yaml"
)
_REVISION = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CITATION = re.compile(r"^(?:https?://[^\s]+|[a-z][a-z0-9+.-]*:[A-Za-z0-9][A-Za-z0-9._/#:-]{0,159})$")


@dataclass(frozen=True)
class SourceDecision:
    source: str
    enabled: bool
    bound: bool
    licensing: str
    technical_acceptance: str
    operational_enabled: bool
    reason: str
    reason_codes: tuple[str, ...]
    policy_revision: str

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe decision record."""

        return {
            "source": self.source,
            "enabled": self.enabled,
            "bound": self.bound,
            "licensing": self.licensing,
            "technical_acceptance": self.technical_acceptance,
            "operational_enabled": self.operational_enabled,
            "reason": self.reason,
            "reason_codes": list(self.reason_codes),
            "policy_revision": self.policy_revision,
        }


def load_source_policy() -> Mapping[str, Any]:
    """Read the controlled approval record without consulting runtime settings."""

    try:
        value = yaml.safe_load(DEFAULT_SOURCE_POLICY_PATH.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return value if isinstance(value, Mapping) else {}


def _policy_metadata(root: Mapping[str, Any]) -> tuple[bool, str]:
    schema_version = root.get("schema_version")
    revision = root.get("revision")
    valid = (
        type(schema_version) is int
        and schema_version == 1
        and isinstance(revision, str)
        and _REVISION.fullmatch(revision) is not None
        and isinstance(root.get("sources"), Mapping)
    )
    serialized = json.dumps(root, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    label = revision if isinstance(revision, str) else "invalid"
    return valid, f"{label}@{digest}"


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _valid_citation(value: object) -> bool:
    return isinstance(value, str) and _CITATION.fullmatch(value.strip()) is not None


def _report_dates_and_citations(value: object) -> tuple[set[date], bool, bool]:
    reports = value if isinstance(value, list) else []
    dates: set[date] = set()
    all_cited = bool(reports)
    dates_valid = bool(reports)
    for report in reports:
        row = _mapping(report)
        observed = row.get("observed_on")
        citation = row.get("citation")
        try:
            parsed_date = date.fromisoformat(observed) if isinstance(observed, str) else None
        except ValueError:
            parsed_date = None
        if parsed_date is None or parsed_date.isoformat() != observed or parsed_date > date.today():
            dates_valid = False
        else:
            dates.add(parsed_date)
        if not _valid_citation(citation):
            all_cited = False
    return dates, all_cited, dates_valid


def evaluate_source_policy(
    source: str,
    identity: ControlledIdentity,
    *,
    settings: Settings,
    adapter_version: str | None,
    global_enabled: bool,
    approvals: Mapping[str, object] | None = None,
) -> SourceDecision:
    """Evaluate binding, licensing, acceptance and ops gates independently.

    ``approvals`` maps source IDs to test-only approval records. Its record
    shape matches the ``sources.<id>`` entries in ``source_policy.yaml``.
    """

    root = load_source_policy()
    records = _mapping(root.get("sources"))
    policy_valid, policy_revision = _policy_metadata(root)
    record = _mapping((approvals or {}).get(source, records.get(source)))
    bound = any(binding.source == source and bool(binding.symbol.strip()) for binding in identity.sources)
    licensing_row = _mapping(record.get("licensing"))
    technical_row = _mapping(record.get("technical_acceptance"))
    raw_licensing = str(licensing_row.get("status", "unknown"))
    raw_technical = str(technical_row.get("status", "not_accepted"))
    allowed_keys = licensing_row.get("company_keys", [])
    licensing_scope_ok = isinstance(allowed_keys, list) and identity.company_key in allowed_keys
    licensing_citation_ok = _valid_citation(licensing_row.get("citation"))
    license_ok = raw_licensing == "approved" and licensing_scope_ok and licensing_citation_ok

    reports = technical_row.get("reports", [])
    dates, reports_cited, dates_valid = _report_dates_and_citations(reports)
    technical_keys = technical_row.get("company_keys", [])
    technical_scope_ok = isinstance(technical_keys, list) and identity.company_key in technical_keys
    technical_ok = (
        raw_technical == "accepted"
        and technical_scope_ok
        and isinstance(adapter_version, str)
        and bool(adapter_version)
        and technical_row.get("adapter_version") == adapter_version
        and len(dates) >= 3
        and dates_valid
        and reports_cited
    )

    sec_sources = {"sec.companyfacts.v1"}
    if source in sec_sources:
        operational_enabled = settings.materials_sec_refresh_enabled
    elif source == "eastmoney.hk.native.v1":
        operational_enabled = settings.materials_hk_refresh_enabled
    else:
        operational_enabled = False

    reasons: list[str] = []
    if not policy_valid:
        reasons.append("invalid_policy_schema")
    if source not in records:
        reasons.append("unknown_source")
    if not bound:
        reasons.append("identity_not_bound")
    if not license_ok:
        if raw_licensing != "approved":
            reasons.append("licensing_unknown")
        elif not licensing_scope_ok:
            reasons.append("licensing_scope_mismatch")
        else:
            reasons.append("licensing_citation_missing_or_invalid")
    if not technical_ok:
        if raw_technical != "accepted":
            reasons.append("technical_acceptance_missing_or_mismatched")
        elif not technical_scope_ok:
            reasons.append("technical_scope_mismatch")
        elif not dates_valid or len(dates) < 3:
            reasons.append("technical_report_dates_invalid_or_insufficient")
        elif not reports_cited:
            reasons.append("technical_report_citation_missing_or_invalid")
        else:
            reasons.append("technical_acceptance_missing_or_mismatched")
    if not operational_enabled:
        reasons.append("operationally_disabled")
    if not global_enabled:
        reasons.append("global_refresh_disabled")

    enabled = not reasons
    reason = "enabled" if enabled else reasons[0]
    if not policy_valid:
        licensing_state = "not_approved" if raw_licensing == "approved" else raw_licensing
        technical_state = "not_accepted" if raw_technical == "accepted" else raw_technical
    else:
        licensing_state = (
            "approved" if license_ok else "scope_mismatch" if raw_licensing == "approved" and not licensing_scope_ok
            else "not_approved" if raw_licensing == "approved" else raw_licensing
        )
        technical_state = (
            "accepted" if technical_ok else "scope_mismatch" if raw_technical == "accepted" and not technical_scope_ok
            else "not_accepted" if raw_technical == "accepted" else raw_technical
        )
    return SourceDecision(
        source=source,
        enabled=enabled,
        bound=bound,
        licensing=licensing_state,
        technical_acceptance=technical_state,
        operational_enabled=operational_enabled,
        reason=reason,
        reason_codes=tuple(reasons),
        policy_revision=policy_revision,
    )
