"""Submitted batch mapping artifacts (WP6).

The technical design (§同行计算记录, §任务触发与失败处理) fixes the storage
decision: there is **no bundle table and no bundle ObjectRef**. The per-target
``metric_calculation_refs`` of one submitted batch live in a *batch artifact* -
one JSON file under the existing research-runs data directory (so the regular
backup coverage applies) - and a finalized mapping is never rewritten. The
current result is selected by *generation inside one scope* (cohort_ref, report
period, provider, adapter_version), never by file time, and never across
scopes; there is no publication-pointer table to consult.

Artifact location and name (design: 原始制品按 run_id 存既有 research-runs
数据目录)::

    {data_dir}/research-runs/{batch_run_id}/peer-batch-mapping.json

``batch_run_id`` is the run identity of the submitting batch (WP8: the collect
job's run id; the offline WP6 command: an explicit or generated
``peer-offline-...`` id). Ordinary research runs use integer directory names,
so a string id cannot collide with one.

Schema ``peer_batch_mapping/1`` (WP8 writes through :func:`write_batch_mapping`
or the submission orchestrator in :mod:`investment_agent.industry_peers.batch`;
readers share :func:`read_batch_mapping` / :func:`scan_batch_mappings`):

* identity - ``batch_run_id``, ``cohort_ref`` (snapshot UUID + revision) and
  ``cohort_content_hash``, ``report_period``, ``provider``,
  ``adapter_version``, ``generation`` (>= 1 inside the scope), ``status``
  (``success`` / ``partial``), ``submitted_at`` (aware).
* acquisition spread - ``inputs_acquired_earliest`` / ``inputs_acquired_latest``
  over the frozen inputs, so a mixed-date batch never hides behind one "latest"
  time; ``last_checked_at`` is the batch-level re-check slot (a later re-read
  belongs to batch artifacts, not to the immutable snapshots).
* ``targets`` - one entry per built target with its three
  ``metric_calculation_refs`` (WP5 states ``saved`` / ``reused`` / ``gap``;
  a gap keeps its reason and candidate count).
* ``manifest`` - the frozen batch manifest: field-evidence refs cited by this
  batch's new records, calculation refs reused from identical earlier inputs,
  targets that could not be built at all, and roster members with no frozen
  annual input for the period.
* ``content_hash`` - sha256 over the canonical body (every field except the
  hash itself), so a mutated or half-written file is rejected instead of read.

Reading is defensive and never guesses: a missing, unparseable, wrong-schema,
or hash-mismatched artifact raises :class:`MappingArtifactError` (the service
turns that into an explicit gap, never into a fallback to another batch).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from investment_agent.industry_peers.calculations import METRIC_KEYS
from investment_agent.investor.common import Contract, ObjectRef, Text

MAPPING_SCHEMA: Literal["peer_batch_mapping/1"] = "peer_batch_mapping/1"
"""Schema tag of the batch mapping artifact."""

MAPPING_SCHEMA_VERSION: Literal[1] = 1

DEFAULT_PROVIDER = "eastmoney"
DEFAULT_ADAPTER_VERSION = "offline-run02-v1"
"""The first-phase offline scope, matching the WP3 import constants. Both the
submission (batch.py) and the read side (service.py) default to it; the
trusted configuration passes its own scope explicitly. Generations are never
compared across provider or adapter versions."""

MAPPING_FILENAME = "peer-batch-mapping.json"
"""File name inside the batch's run directory under ``research-runs``."""


class MappingArtifactError(ValueError):
    """A batch mapping artifact is missing, unreadable, or fails validation.

    The reader never guesses: callers surface this as an explicit gap instead
    of silently falling back to another batch or generation.
    """


def body_sha256(body: dict[str, Any]) -> str:
    """sha256 over the canonical body JSON (sort keys, no whitespace)."""

    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class MetricMappingEntry(Contract):
    """One metric's submitted outcome inside a batch: saved, reused, or a gap."""

    metric_key: str
    state: Literal["saved", "reused", "gap"]
    ref: ObjectRef | None = None
    """The saved :class:`SavedCalculation` reference; ``None`` only for gaps."""
    gap_reason: str | None = None
    candidate_peer_count: int | None = None
    """Kept for gaps so the read side can show honest coverage counts."""


class TargetMapping(Contract):
    """One built target's three metric references inside the batch."""

    company_id: int
    listing_id: int
    ticker: str
    name: str
    report_period: date
    metric_calculation_refs: tuple[MetricMappingEntry, ...]

    def ref_for(self, metric_key: str) -> MetricMappingEntry | None:
        return next(
            (entry for entry in self.metric_calculation_refs if entry.metric_key == metric_key),
            None,
        )

    @model_validator(mode="after")
    def covers_the_three_fixed_metrics(self) -> TargetMapping:
        keys = [entry.metric_key for entry in self.metric_calculation_refs]
        if sorted(keys) != sorted(METRIC_KEYS):
            raise ValueError(
                "a target mapping carries exactly the three fixed metric keys "
                f"{list(METRIC_KEYS)}; got {keys}"
            )
        return self


class TargetGap(Contract):
    """A roster member this batch could not build at all."""

    company_id: int
    ticker: str
    reason: str


class MissingCompany(Contract):
    """A roster member with no frozen annual input for the batch period."""

    company_id: int
    ticker: str
    reason: str


class BatchManifest(Contract):
    """The frozen batch manifest: references, reuses, and gaps."""

    referenced: tuple[ObjectRef, ...] = ()
    """Field-evidence refs cited by this batch's newly saved metric records."""
    reused: tuple[ObjectRef, ...] = ()
    """Calculation refs reused from identical earlier inputs."""
    target_gaps: tuple[TargetGap, ...] = ()
    missing_companies: tuple[MissingCompany, ...] = ()


class PeerBatchMapping(Contract):
    """One submitted batch's immutable reference mapping (``peer_batch_mapping/1``).

    A finalized mapping is never rewritten: a later batch appends a new
    artifact with the next ``generation`` inside the same scope. The
    ``content_hash`` covers the canonical body (every field except itself), so
    a mutated or truncated file fails validation instead of being read.
    """

    schema_name: Literal["peer_batch_mapping/1"] = Field(
        default=MAPPING_SCHEMA, alias="schema"
    )
    """The artifact kind tag (stored under the JSON key ``schema``); a reader
    accepts nothing else, so a foreign file can never pass as a mapping."""
    schema_version: Literal[1] = MAPPING_SCHEMA_VERSION
    batch_run_id: Text
    cohort_ref: ObjectRef
    """The roster version the batch was built against (UUID + revision)."""
    cohort_content_hash: Text
    report_period: date
    provider: Text
    adapter_version: Text
    generation: int
    """1-based batch generation inside the scope; selection takes the max."""
    status: Literal["success", "partial"]
    submitted_at: datetime
    inputs_acquired_earliest: datetime | None = None
    inputs_acquired_latest: datetime | None = None
    last_checked_at: datetime | None = None
    """Batch-level re-check slot; a later re-read belongs here, never to the
    immutable snapshot rows."""
    targets: tuple[TargetMapping, ...] = ()
    manifest: BatchManifest = BatchManifest()
    content_hash: Text
    """sha256 over the canonical body; the reader re-derives and compares."""

    @model_validator(mode="after")
    def cohort_is_a_snapshot_ref(self) -> PeerBatchMapping:
        if self.cohort_ref.kind != "peer_cohort_snapshot":
            raise ValueError("cohort_ref must reference a peer_cohort_snapshot")
        if isinstance(self.cohort_ref.id, int):
            raise ValueError("cohort_ref must use the snapshot UUID")
        if self.generation < 1:
            raise ValueError("generation is a positive batch number inside the scope")
        seen: set[int] = set()
        for target in self.targets:
            if target.company_id in seen:
                raise ValueError(f"target company {target.company_id} appears twice")
            seen.add(target.company_id)
        return self

    def body(self) -> dict[str, Any]:
        """The payload without ``content_hash`` (the hash input)."""

        return self.model_dump(mode="json", exclude={"content_hash"}, by_alias=True)

    def target_for(self, company_id: int) -> TargetMapping | None:
        return next((entry for entry in self.targets if entry.company_id == company_id), None)

    @classmethod
    def create(cls, **fields: Any) -> PeerBatchMapping:
        """Build a mapping and derive its ``content_hash`` from the body."""

        draft = cls.model_validate({**fields, "content_hash": "0" * 64})
        body = draft.model_dump(mode="json", exclude={"content_hash"}, by_alias=True)
        return draft.model_copy(update={"content_hash": body_sha256(body)})

    @classmethod
    def from_stored(cls, payload: dict[str, Any]) -> PeerBatchMapping:
        """Validate a stored payload and re-derive its integrity hash."""

        mapping = cls.model_validate(payload)
        expected = body_sha256(
            mapping.model_dump(mode="json", exclude={"content_hash"}, by_alias=True)
        )
        if mapping.content_hash != expected:
            raise MappingArtifactError(
                f"batch mapping {mapping.batch_run_id} fails its content hash check"
            )
        return mapping


def mapping_artifact_path(data_dir: Path, batch_run_id: str) -> Path:
    """The artifact path of one batch inside the research-runs data directory."""

    if (
        not batch_run_id.strip()
        or batch_run_id in {".", ".."}
        or "/" in batch_run_id
        or "\\" in batch_run_id
    ):
        raise ValueError(f"batch_run_id must be a plain directory name, got {batch_run_id!r}")
    return Path(data_dir) / "research-runs" / batch_run_id / MAPPING_FILENAME


def write_batch_mapping(data_dir: Path, mapping: PeerBatchMapping) -> Path:
    """Write one mapping artifact atomically; the caller owns finalization.

    The file is written to a temporary name in the same directory and renamed
    into place, so a crash can never leave a half-written mapping behind.
    """

    path = mapping_artifact_path(data_dir, mapping.batch_run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = mapping.model_dump(mode="json", by_alias=True)
    tmp = path.with_name(f"{MAPPING_FILENAME}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    return path


def read_batch_mapping(path: Path) -> PeerBatchMapping:
    """Load one mapping artifact defensively; never guess past an error."""

    if not path.is_file():
        raise MappingArtifactError(f"batch mapping artifact is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MappingArtifactError(f"batch mapping artifact is unreadable: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise MappingArtifactError(f"batch mapping artifact is not a JSON object: {path}")
    try:
        return PeerBatchMapping.from_stored(payload)
    except MappingArtifactError:
        raise
    except ValueError as exc:
        raise MappingArtifactError(f"batch mapping artifact fails validation: {path}: {exc}") from exc


@dataclass(frozen=True)
class UnreadableMapping:
    """One mapping artifact found on disk that cannot be read or validated."""

    batch_run_id: str
    path: Path
    reason: str


@dataclass(frozen=True)
class BatchMappingScan:
    """Every mapping artifact found under the data directory."""

    valid: tuple[PeerBatchMapping, ...]
    unreadable: tuple[UnreadableMapping, ...] = ()

    @property
    def warnings(self) -> tuple[str, ...]:
        return tuple(
            f"batch mapping artifact unreadable ({item.batch_run_id}): {item.reason}"
            for item in self.unreadable
        )


def scan_batch_mappings(data_dir: Path) -> BatchMappingScan:
    """Read every ``peer-batch-mapping.json`` under ``research-runs/*``.

    Directories without a mapping file are not peer batches and are ignored;
    files that fail to read or validate are reported as unreadable (explicit
    gap) instead of being silently skipped.
    """

    root = Path(data_dir) / "research-runs"
    valid: list[PeerBatchMapping] = []
    unreadable: list[UnreadableMapping] = []
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if not child.is_dir():
                continue
            path = child / MAPPING_FILENAME
            if not path.exists():
                continue
            try:
                valid.append(read_batch_mapping(path))
            except MappingArtifactError as exc:
                unreadable.append(
                    UnreadableMapping(
                        batch_run_id=child.name, path=path, reason=str(exc)
                    )
                )
    return BatchMappingScan(valid=tuple(valid), unreadable=tuple(unreadable))


def mappings_in_scope(
    mappings: tuple[PeerBatchMapping, ...] | list[PeerBatchMapping],
    *,
    cohort_ref: ObjectRef,
    report_period: date,
    provider: str,
    adapter_version: str,
) -> list[PeerBatchMapping]:
    """Mappings of one scope ``(cohort_ref, period, provider, adapter_version)``.

    Generation is only ever compared inside one such scope (design: 名单与来源
    版本切换由可信配置确认，不比较不同范围的 generation).
    """

    return [
        mapping
        for mapping in mappings
        if mapping.cohort_ref == cohort_ref
        and mapping.report_period == report_period
        and mapping.provider == provider
        and mapping.adapter_version == adapter_version
    ]


def select_current_mapping(
    scope_mappings: list[PeerBatchMapping],
    *,
    cutoff: datetime | None = None,
) -> PeerBatchMapping | None:
    """The valid submitted mapping with the maximum generation in the scope.

    Not ordered by file or finish time: an older generation that finished late
    can never cover a newer one. With a ``cutoff`` (research binding time), a
    mapping submitted after it is not yet visible - a future batch must not
    enter an old read because its input year is early.
    """

    candidates = [
        mapping
        for mapping in scope_mappings
        if cutoff is None or mapping.submitted_at <= cutoff
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda mapping: mapping.generation)


def next_generation(scope_mappings: list[PeerBatchMapping]) -> int:
    """The next generation derived from the submitted mappings in the scope."""

    return max((mapping.generation for mapping in scope_mappings), default=0) + 1


def targets_signature(mapping: PeerBatchMapping) -> str:
    """Canonical per-target reference signature of a mapping (idempotency key).

    Two mappings of one scope with the same signature reference exactly the
    same calculation records (or keep the same gaps) - re-submitting identical
    inputs is a reuse, not a new generation. The submission *state*
    (``saved`` vs ``reused``) is deliberately excluded: the first submission
    saves, every identical later one reuses, and both map the same UUIDs.
    """

    body = {
        "targets": [
            {
                "company_id": target.company_id,
                "ticker": target.ticker,
                "report_period": target.report_period.isoformat(),
                "metrics": sorted(
                    (
                        entry.metric_key,
                        str(entry.ref.id) if entry.ref is not None else "",
                        str(entry.ref.revision) if entry.ref is not None else "",
                        entry.gap_reason or "",
                    )
                    for entry in target.metric_calculation_refs
                ),
            }
            for target in sorted(mapping.targets, key=lambda item: item.company_id)
        ],
        "target_gaps": sorted(
            (gap.company_id, gap.ticker, gap.reason) for gap in mapping.manifest.target_gaps
        ),
        "missing_companies": sorted(
            (item.company_id, item.ticker, item.reason)
            for item in mapping.manifest.missing_companies
        ),
    }
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def find_identical_submission(
    scope_mappings: list[PeerBatchMapping], signature: str
) -> PeerBatchMapping | None:
    """The already-submitted mapping in the scope with the same target refs."""

    return next(
        (mapping for mapping in scope_mappings if targets_signature(mapping) == signature),
        None,
    )


__all__ = [
    "BatchManifest",
    "BatchMappingScan",
    "MAPPING_FILENAME",
    "MAPPING_SCHEMA",
    "MAPPING_SCHEMA_VERSION",
    "MappingArtifactError",
    "MetricMappingEntry",
    "MissingCompany",
    "PeerBatchMapping",
    "TargetGap",
    "TargetMapping",
    "UnreadableMapping",
    "body_sha256",
    "find_identical_submission",
    "mappings_in_scope",
    "mapping_artifact_path",
    "next_generation",
    "read_batch_mapping",
    "scan_batch_mappings",
    "select_current_mapping",
    "targets_signature",
    "write_batch_mapping",
]
