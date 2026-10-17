"""Contract for the six HK2-06 operational drill evidence files (schema v1).

Plan 35 (``docs/product/plans/hk-us-expansion/35-hk2-06-observation-plan.md``)
requires each controlled source to keep local, hash-bound evidence for six
independent drills before any real observation window can advance. This
module is that evidence's schema and validator only: it reads nothing from the
environment, opens no network connection, and trusts no report it did not
hash itself. The drill names mirror ``source_observations._DRILLS`` (the
acceptor's vocabulary); the controlled company scope is imported from
``source_observations.SOURCE_COMPANIES`` so the drill contract and the
acceptance contract can never drift apart.

Simulation is the only mode this repository's drill tool writes; ``real`` is
accepted by the validator because hand-produced real-run evidence uses the
same shape. A simulation record must carry zero network calls and mock
transports only — anything else is rejected before it can be cited.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from investment_agent.materials.source_observations import SOURCE_COMPANIES

SCHEMA_VERSION = 1
EVIDENCE_FILENAME = "evidence.json"

DRILL_NAMES: tuple[str, ...] = (
    "shutdown",
    "inflight_stop",
    "restart_resume",
    "old_versions_readable",
    "offline_replay",
    "probe_stop_withdrawal",
)
"""The six drills plan 35 demands; equal to ``source_observations._DRILLS``."""

STOP_SEMANTICS_DRILLS: frozenset[str] = frozenset(
    {"shutdown", "inflight_stop", "probe_stop_withdrawal"}
)
"""Drills that must describe how they stopped (and which must not fake one)."""

DRILL_MODES: frozenset[str] = frozenset({"simulation", "real"})
TRANSPORT_MOCK = "mock"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DrillEvidenceError(ValueError):
    """Drill evidence is malformed, unsafe, or does not match its own hashes."""


@dataclass(frozen=True)
class DrillStep:
    """One drill assertion: the action, its expectation, and what happened."""

    action: str
    expected: str
    observed: str
    passed: bool


@dataclass(frozen=True)
class DrillRequest:
    """One request the drill actually issued (always a mock transport here)."""

    company_key: str
    name: str
    http_status: int
    transport: str


@dataclass(frozen=True)
class DrillArtifact:
    """A hash-bound file next to ``evidence.json`` that backs a drill claim."""

    kind: str
    path: str
    sha256: str


@dataclass(frozen=True)
class DrillStopSemantics:
    """How a stopping drill stopped, and what survived the stop."""

    stop_mechanism: str
    requests_after_stop: int = 0
    received_responses_retained: bool = False


@dataclass(frozen=True)
class DrillEvidence:
    """One validated drill record (see the module docstring for the rules)."""

    schema_version: int
    drill: str
    mode: str
    source: str
    adapter_version: str
    executed_at: datetime
    isolated_environment: dict[str, str]
    service_chain: tuple[str, ...]
    company_scope: tuple[str, ...]
    steps: tuple[DrillStep, ...]
    requests: tuple[DrillRequest, ...]
    artifacts: tuple[DrillArtifact, ...]
    network_calls: int
    passed: bool
    failure_reasons: tuple[str, ...]
    stop_semantics: DrillStopSemantics | None = None

    def to_mapping(self) -> dict[str, object]:
        """The canonical JSON shape of this record (values are JSON-safe)."""

        value: dict[str, object] = {
            "schema_version": self.schema_version,
            "drill": self.drill,
            "mode": self.mode,
            "source": self.source,
            "adapter_version": self.adapter_version,
            "executed_at": self.executed_at.astimezone(UTC).isoformat(),
            "isolated_environment": dict(self.isolated_environment),
            "service_chain": list(self.service_chain),
            "company_scope": list(self.company_scope),
            "steps": [
                {
                    "action": step.action,
                    "expected": step.expected,
                    "observed": step.observed,
                    "passed": step.passed,
                }
                for step in self.steps
            ],
            "requests": [
                {
                    "company_key": item.company_key,
                    "name": item.name,
                    "http_status": item.http_status,
                    "transport": item.transport,
                }
                for item in self.requests
            ],
            "artifacts": [
                {"kind": item.kind, "path": item.path, "sha256": item.sha256}
                for item in self.artifacts
            ],
            "network_calls": self.network_calls,
            "passed": self.passed,
            "failure_reasons": list(self.failure_reasons),
        }
        if self.stop_semantics is not None:
            value["stop_semantics"] = {
                "stop_mechanism": self.stop_semantics.stop_mechanism,
                "requests_after_stop": self.stop_semantics.requests_after_stop,
                "received_responses_retained": (
                    self.stop_semantics.received_responses_retained
                ),
            }
        return value


_TOP_LEVEL_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "drill",
        "mode",
        "source",
        "adapter_version",
        "executed_at",
        "isolated_environment",
        "service_chain",
        "company_scope",
        "steps",
        "requests",
        "artifacts",
        "network_calls",
        "stop_semantics",
        "passed",
        "failure_reasons",
    }
)
_STOP_KEYS: frozenset[str] = frozenset(
    {"stop_mechanism", "requests_after_stop", "received_responses_retained"}
)
_ABSENT: object = object()
"""Sentinel distinguishing a missing key from an explicit ``null``."""


def validate_drill_evidence(value: Mapping[str, Any]) -> DrillEvidence:
    """Validate an in-memory drill record; malformed input raises
    :class:`DrillEvidenceError`.

    The check is pure: no file, environment or network access happens here.
    Artifact hash verification needs the evidence directory and therefore
    belongs to :func:`load_drill_evidence`.
    """

    unknown = sorted(set(value) - _TOP_LEVEL_KEYS)
    if unknown:
        raise DrillEvidenceError(f"unknown drill evidence keys: {unknown}")
    if _plain_int(value.get("schema_version"), "schema_version") != SCHEMA_VERSION:
        raise DrillEvidenceError(f"schema_version must be {SCHEMA_VERSION}")
    drill = _text(value.get("drill"), "drill")
    if drill not in DRILL_NAMES:
        raise DrillEvidenceError(f"unknown drill {drill!r}; expected one of {list(DRILL_NAMES)}")
    mode = _text(value.get("mode"), "mode")
    if mode not in DRILL_MODES:
        raise DrillEvidenceError(f"mode must be one of {sorted(DRILL_MODES)}, got {mode!r}")
    source = _text(value.get("source"), "source")
    if source not in SOURCE_COMPANIES:
        raise DrillEvidenceError(f"source {source!r} is not an HK2-06 controlled source")
    adapter_version = _text(value.get("adapter_version"), "adapter_version")
    executed_at = _aware_timestamp(value.get("executed_at"), "executed_at")
    if executed_at > datetime.now(UTC):
        raise DrillEvidenceError("executed_at must not be in the future")

    environment = _mapping(value.get("isolated_environment"), "isolated_environment")
    _reject_unknown_keys(environment, frozenset({"database_uri", "data_dir"}), "isolated_environment")
    isolated_environment = {
        "database_uri": _text(environment.get("database_uri"), "isolated_environment.database_uri"),
        "data_dir": _text(environment.get("data_dir"), "isolated_environment.data_dir"),
    }

    service_chain = _string_tuple(value.get("service_chain"), "service_chain")
    if not service_chain:
        raise DrillEvidenceError("service_chain must list at least one exercised entry point")

    controlled = SOURCE_COMPANIES[source]
    company_scope = _string_tuple(value.get("company_scope"), "company_scope")
    if not company_scope:
        raise DrillEvidenceError("company_scope must not be empty")
    uncontrolled = sorted(set(company_scope) - set(controlled))
    if uncontrolled:
        raise DrillEvidenceError(
            f"company_scope contains keys outside {source}'s controlled range: {uncontrolled}"
        )

    steps_value = value.get("steps")
    if not isinstance(steps_value, list) or not steps_value:
        raise DrillEvidenceError("steps must be a non-empty list")
    steps = tuple(_step(item, index) for index, item in enumerate(steps_value))

    requests_value = value.get("requests")
    if not isinstance(requests_value, list):
        raise DrillEvidenceError("requests must be a list")
    requests = tuple(_request(item, index, source, mode) for index, item in enumerate(requests_value))

    artifacts_value = value.get("artifacts")
    if not isinstance(artifacts_value, list):
        raise DrillEvidenceError("artifacts must be a list")
    artifacts = tuple(_artifact(item, index) for index, item in enumerate(artifacts_value))

    network_calls = _plain_int(value.get("network_calls"), "network_calls")
    if network_calls < 0:
        raise DrillEvidenceError("network_calls must be a nonnegative integer")
    if mode == "simulation" and network_calls != 0:
        raise DrillEvidenceError("a simulation drill must report zero network calls")

    # An explicit null must still count as "carried" on a non-stopping drill,
    # so the key's presence is checked before defaulting to the sentinel.
    if "stop_semantics" in value:
        stop_semantics = _stop_semantics(value["stop_semantics"], drill)
    else:
        stop_semantics = _stop_semantics(_ABSENT, drill)

    failure_reasons_value = value.get("failure_reasons")
    if not isinstance(failure_reasons_value, list):
        raise DrillEvidenceError("failure_reasons must be a list")
    failure_reasons = tuple(
        _text(item, f"failure_reasons[{index}]")
        for index, item in enumerate(failure_reasons_value)
    )
    passed = _plain_bool(value.get("passed"), "passed")
    if passed != (all(step.passed for step in steps) and not failure_reasons):
        raise DrillEvidenceError(
            "passed must equal (every step passed and no failure reasons); "
            "a failing drill must say so instead of claiming success"
        )
    return DrillEvidence(
        schema_version=SCHEMA_VERSION,
        drill=drill,
        mode=mode,
        source=source,
        adapter_version=adapter_version,
        executed_at=executed_at,
        isolated_environment=isolated_environment,
        service_chain=service_chain,
        company_scope=company_scope,
        steps=steps,
        requests=requests,
        artifacts=artifacts,
        network_calls=network_calls,
        passed=passed,
        failure_reasons=failure_reasons,
        stop_semantics=stop_semantics,
    )


def load_drill_evidence(path: Path) -> DrillEvidence:
    """Read, validate and hash-check one drill evidence record.

    Every artifact is resolved relative to the evidence file's own directory
    (symlinks, absolute paths and ``..`` escapes are refused) and its SHA-256
    must match the recorded digest before the record is trusted.
    """

    target = _checked_existing_file(Path(path))
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DrillEvidenceError(f"invalid drill evidence JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise DrillEvidenceError(f"drill evidence must be a JSON object: {path}")
    evidence = validate_drill_evidence(raw)
    root = target.parent
    for artifact in evidence.artifacts:
        artifact_path = _safe_relative_file(root, artifact.path)
        if _sha256_file(artifact_path) != artifact.sha256:
            raise DrillEvidenceError(
                f"artifact sha256 mismatch for {artifact.path!r} "
                f"(evidence file {target})"
            )
    return evidence


def write_drill_evidence(directory: Path, evidence: DrillEvidence) -> Path:
    """Atomically write ``<directory>/evidence.json``; refuse to overwrite."""

    root = Path(directory)
    _reject_symlink_path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / EVIDENCE_FILENAME
    if target.exists() or target.is_symlink():
        raise DrillEvidenceError(f"refusing to overwrite existing drill evidence: {target}")
    payload = (
        json.dumps(evidence.to_mapping(), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n"
    ).encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{EVIDENCE_FILENAME}.", suffix=".tmp", dir=root
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if Path(temporary).exists():
            os.unlink(temporary)
    return target


# --- field helpers -----------------------------------------------------------


def _step(value: object, index: int) -> DrillStep:
    label = f"steps[{index}]"
    step = _mapping(value, label)
    _reject_unknown_keys(step, frozenset({"action", "expected", "observed", "passed"}), label)
    return DrillStep(
        action=_text(step.get("action"), f"{label}.action"),
        expected=_text(step.get("expected"), f"{label}.expected"),
        observed=_text(step.get("observed"), f"{label}.observed"),
        passed=_plain_bool(step.get("passed"), f"{label}.passed"),
    )


def _request(value: object, index: int, source: str, mode: str) -> DrillRequest:
    label = f"requests[{index}]"
    request = _mapping(value, label)
    _reject_unknown_keys(
        request, frozenset({"company_key", "name", "http_status", "transport"}), label
    )
    company_key = _text(request.get("company_key"), f"{label}.company_key")
    if company_key not in SOURCE_COMPANIES[source]:
        raise DrillEvidenceError(f"{label}.company_key is outside {source}'s controlled range")
    http_status = _plain_int(request.get("http_status"), f"{label}.http_status")
    if not 100 <= http_status <= 599:
        raise DrillEvidenceError(f"{label}.http_status must be an HTTP status code")
    transport = _text(request.get("transport"), f"{label}.transport")
    if mode == "simulation" and transport != TRANSPORT_MOCK:
        raise DrillEvidenceError(f"{label}.transport must be {TRANSPORT_MOCK!r} in simulation mode")
    return DrillRequest(
        company_key=company_key,
        name=_text(request.get("name"), f"{label}.name"),
        http_status=http_status,
        transport=transport,
    )


def _artifact(value: object, index: int) -> DrillArtifact:
    label = f"artifacts[{index}]"
    artifact = _mapping(value, label)
    _reject_unknown_keys(artifact, frozenset({"kind", "path", "sha256"}), label)
    path = _text(artifact.get("path"), f"{label}.path")
    posix = PurePosixPath(path)
    if (
        posix.is_absolute()
        or not path
        or "\\" in path
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise DrillEvidenceError(f"{label}.path must be a safe relative path, got {path!r}")
    sha256 = _text(artifact.get("sha256"), f"{label}.sha256")
    if not _SHA256.fullmatch(sha256):
        raise DrillEvidenceError(f"{label}.sha256 must be a lowercase SHA-256 digest")
    return DrillArtifact(
        kind=_text(artifact.get("kind"), f"{label}.kind"),
        path=path,
        sha256=sha256,
    )


def _stop_semantics(value: object, drill: str) -> DrillStopSemantics | None:
    if value is _ABSENT:
        if drill in STOP_SEMANTICS_DRILLS:
            raise DrillEvidenceError(f"{drill} must record its stop_semantics")
        return None
    if drill not in STOP_SEMANTICS_DRILLS:
        raise DrillEvidenceError(
            f"{drill} must not carry stop_semantics; only {sorted(STOP_SEMANTICS_DRILLS)} stop"
        )
    label = "stop_semantics"
    record = _mapping(value, label)
    _reject_unknown_keys(record, _STOP_KEYS, label)
    requests_after_stop = record.get("requests_after_stop", 0)
    after_stop = _plain_int(requests_after_stop, f"{label}.requests_after_stop")
    if after_stop < 0:
        raise DrillEvidenceError(f"{label}.requests_after_stop must be nonnegative")
    return DrillStopSemantics(
        stop_mechanism=_text(record.get("stop_mechanism"), f"{label}.stop_mechanism"),
        requests_after_stop=after_stop,
        received_responses_retained=_plain_bool(
            record.get("received_responses_retained"), f"{label}.received_responses_retained"
        ),
    )


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise DrillEvidenceError(f"{label} must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DrillEvidenceError(f"{label} must be a non-empty string")
    return value


def _plain_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DrillEvidenceError(f"{label} must be an integer (booleans do not count)")
    return value


def _plain_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise DrillEvidenceError(f"{label} must be a boolean")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise DrillEvidenceError(f"{label} must be a list of strings")
    items = tuple(_text(item, f"{label}[{index}]") for index, item in enumerate(value))
    if len(set(items)) != len(items):
        raise DrillEvidenceError(f"{label} must not repeat an entry")
    return items


def _reject_unknown_keys(
    value: Mapping[str, object], allowed: frozenset[str], label: str
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise DrillEvidenceError(f"{label} has unknown keys: {unknown}")


def _aware_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise DrillEvidenceError(f"{label} must be an ISO timestamp string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DrillEvidenceError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DrillEvidenceError(f"{label} must carry a timezone offset")
    return parsed.astimezone(UTC)


def _checked_existing_file(path: Path) -> Path:
    _reject_symlink_path(path)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise DrillEvidenceError(f"drill evidence file does not exist: {path}") from exc
    if not resolved.is_file():
        raise DrillEvidenceError(f"drill evidence path is not a regular file: {path}")
    return resolved


def _safe_relative_file(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root`` refusing escapes and symlinks."""

    posix = PurePosixPath(relative)
    if posix.is_absolute() or not relative or ".." in posix.parts or "\\" in relative:
        raise DrillEvidenceError(f"artifact path must be a safe relative path: {relative!r}")
    candidate = root.joinpath(*posix.parts)
    _reject_symlink_path(candidate)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DrillEvidenceError(f"artifact path does not exist: {relative}") from exc
    if root not in resolved.parents:
        raise DrillEvidenceError(f"artifact path escaped the evidence directory: {relative}")
    if not resolved.is_file():
        raise DrillEvidenceError(f"artifact path is not a regular file: {relative}")
    return resolved


def _reject_symlink_path(path: Path) -> None:
    current = Path(path.anchor) if path.is_absolute() else Path.cwd()
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current = current / part
        if current.is_symlink():
            raise DrillEvidenceError(f"symlink path is not allowed: {current}")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = [
    "DRILL_MODES",
    "DRILL_NAMES",
    "DrillArtifact",
    "DrillEvidence",
    "DrillEvidenceError",
    "DrillRequest",
    "DrillStep",
    "DrillStopSemantics",
    "EVIDENCE_FILENAME",
    "SCHEMA_VERSION",
    "STOP_SEMANTICS_DRILLS",
    "TRANSPORT_MOCK",
    "load_drill_evidence",
    "validate_drill_evidence",
    "write_drill_evidence",
]
