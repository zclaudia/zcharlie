"""Offline batch submission: freeze -> compute & save -> mapping artifact (WP6).

This is the offline equivalent of WP8's "final transaction saves the batch
reference mapping" - shared, function for function, with the future collect
job. It performs **no HTTP, no worker logic and no upstream request**: the
caller hands in a session over already-imported data (WP2 roster, WP3 annual
snapshots) and the artifacts directory.

Pipeline (design §模块与数据流, §任务触发与失败处理):

1. **Freeze** - one :class:`~...calculations.FrozenBatch` per target from the
   roster entries and each company's latest annual snapshot version for the
   batch scope (``provider`` / ``adapter_version``). Conflict detection uses
   the WP4 dedicated query: an unresolved conflict on a field marks that
   frozen input conflicted, so the pure calculation excludes it instead of
   silently preferring one value. Roster members without a snapshot version
   are recorded in the manifest, never invented.
2. **Materialize** - every snapshot field with a usable date basis becomes its
   one :class:`EvidenceRecord` (WP4 idempotent operation identity). The run-02
   fixed evidence carries no disclosure dates, so it materializes nothing;
   such submissions then run in declared-inputs mode
   (``require_fact_refs=False``) and the read side shows the evidence gap.
3. **Compute & save** - WP5 :func:`save_peer_calculations` per target
   (idempotent: identical inputs reuse the old record UUIDs).
4. **Submit** - assemble the :class:`~...artifacts.PeerBatchMapping` (status
   ``success``/``partial``, acquisition spread, manifest), derive the next
   ``generation`` inside the scope from the already-submitted mappings, and
   write the artifact atomically. Identical inputs already submitted are
   *reused*: the existing batch is returned unchanged and no new generation
   is created.

The caller owns the database transaction (``session_scope`` for the CLI; the
``fence_writes``-protected final transaction for WP8).
"""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.store import EvidenceService
from investment_agent.industry_peers.artifacts import (
    DEFAULT_ADAPTER_VERSION,
    DEFAULT_PROVIDER,
    BatchManifest,
    MetricMappingEntry,
    MissingCompany,
    PeerBatchMapping,
    TargetGap,
    TargetMapping,
    find_identical_submission,
    mapping_artifact_path,
    mappings_in_scope,
    next_generation,
    register_batch_commit,
    reserve_batch_transaction,
    scan_committed_batch_mappings,
    targets_signature,
    write_batch_mapping,
)
from investment_agent.industry_peers.calculations import (
    BatchCompany,
    FrozenBatch,
    TargetCalculationRefs,
    batch_field_from_snapshot,
    save_peer_calculations,
)
from investment_agent.industry_peers.contracts import PeerAnnualField
from investment_agent.industry_peers.evidence import (
    peer_field_evidence,
    peer_field_operation_id,
    peer_field_states,
)
from investment_agent.industry_peers.metric_packs import cohort_metric_pack
from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.storage.models.peer import PeerAnnualSnapshot, PeerCohortSnapshot
from investment_agent.storage.repositories.peer import (
    PeerAnnualSnapshotRepository,
    PeerCohortSnapshotRepository,
)

DEFAULT_COHORT_KEY = "baijiu_current"
"""The first cohort's stable roster name (WP2 import default)."""


@dataclass(frozen=True)
class FrozenCompany:
    """One roster member's frozen slice: roster identity plus snapshot row."""

    company_id: int
    listing_id: int
    ticker: str
    name: str
    row: PeerAnnualSnapshot
    fields: dict[str, str]
    """Per-field conflict markers from the WP4 query (empty when consistent)."""


@dataclass(frozen=True)
class OfflineBatchSubmission:
    """Outcome of one offline batch submission."""

    mapping: PeerBatchMapping
    """The submitted mapping; on idempotent reuse, the already-submitted one."""
    path: Path
    created: bool
    """``False`` when an identical submission already existed (no new file)."""
    saved_records: int
    reused_records: int
    gap_metrics: int
    frozen_companies: int

    @property
    def status(self) -> str:
        return self.mapping.status


def _batch_run_id(submitted_at: datetime) -> str:
    """Offline batch identity: a plain directory name under ``research-runs``.

    Ordinary research runs use integer directory names, so this cannot
    collide with one; an explicit ``batch_run_id`` is honoured as given.
    """

    return f"peer-offline-{submitted_at.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


async def _latest_version(
    session: AsyncSession,
    *,
    company_id: int,
    report_period: date,
    provider: str,
    adapter_version: str,
) -> PeerAnnualSnapshot | None:
    """The frozen input version of one company: latest capture, stable tie-break."""

    rows = await PeerAnnualSnapshotRepository(session).versions(
        company_id=company_id, report_period=report_period, provider=provider
    )
    rows = [row for row in rows if row.adapter_version == adapter_version]
    if not rows:
        return None
    return max(rows, key=lambda row: (row.captured_at, row.semantic_hash))


async def latest_snapshot_period(
    session: AsyncSession,
    *,
    company_ids: Sequence[int],
    provider: str,
    adapter_version: str,
) -> date | None:
    """The latest period with imported annual snapshots in the batch scope."""

    rows = list(
        (
            await session.execute(
                select(PeerAnnualSnapshot.report_period).where(
                    PeerAnnualSnapshot.company_id.in_(list(company_ids)),
                    PeerAnnualSnapshot.provider == provider,
                    PeerAnnualSnapshot.adapter_version == adapter_version,
                )
            )
        ).all()
    )
    return max((row[0] for row in rows), default=None)


async def _freeze_companies(
    session: AsyncSession,
    *,
    cohort_row: PeerCohortSnapshot,
    report_period: date,
    provider: str,
    adapter_version: str,
    snapshot_ids: dict[int, str | None] | None = None,
) -> tuple[dict[int, FrozenCompany], list[MissingCompany]]:
    """Freeze the roster once per batch: latest snapshot version per member.

    Conflict marking (WP4 dedicated query, never auto-judged): only an
    unresolved ``conflict`` marks the frozen input - a supplier update without
    a correction basis stays usable here and is flagged stale at read time.
    """

    frozen: dict[int, FrozenCompany] = {}
    missing: list[MissingCompany] = []
    for entry in sorted(cohort_row.payload["entries"], key=lambda item: item["company_id"]):
        company_id = int(entry["company_id"])
        if snapshot_ids is None:
            row = await _latest_version(
                session, company_id=company_id, report_period=report_period,
                provider=provider, adapter_version=adapter_version,
            )
        else:
            snapshot_id = snapshot_ids.get(company_id)
            row = await session.get(PeerAnnualSnapshot, snapshot_id) if snapshot_id else None
        if row is not None and (row.company_id != company_id or row.listing_id != int(entry["listing_id"]) or row.report_period != report_period or row.provider != provider or row.adapter_version != adapter_version):
            raise ValueError("frozen company snapshot is outside this batch scope")
        if row is None:
            missing.append(
                MissingCompany(
                    company_id=company_id,
                    ticker=str(entry["ticker"]),
                    reason=(
                        f"no imported annual snapshot for {report_period.isoformat()} "
                        f"(provider={provider}, adapter={adapter_version})"
                    ),
                )
            )
            continue
        markers: dict[str, str] = {}
        for metric in row.fields:
            report = await peer_field_states(
                session, company_id=company_id, metric=metric, report_period=report_period
            )
            if report.status == "conflict":
                markers[metric] = (
                    f"unresolved peer field conflict ({len(report.versions)} saved versions "
                    "disagree without a correction basis)"
                )
        frozen[company_id] = FrozenCompany(
            company_id=company_id,
            listing_id=int(entry["listing_id"]),
            ticker=str(entry["ticker"]),
            name=str(entry["name"]),
            row=row,
            fields=markers,
        )
    return frozen, missing


def _frozen_batch(
    *,
    cohort_row: PeerCohortSnapshot,
    report_period: date,
    frozen: dict[int, FrozenCompany],
    target_company_id: int,
) -> FrozenBatch:
    """The WP5 immutable calculation input for one target."""

    companies = []
    for company_id in sorted(frozen):
        company = frozen[company_id]
        fields = {}
        for metric in company.row.fields:
            entry = batch_field_from_snapshot(company.row, metric)
            marker = company.fields.get(metric)
            if marker is not None:
                entry = replace(entry, conflict_reason=marker)
            fields[metric] = entry
        companies.append(
            BatchCompany(
                company_id=company.company_id,
                listing_id=company.listing_id,
                ticker=company.ticker,
                name=company.name,
                in_cohort=True,
                report_period=company.row.report_period,
                fields=fields,
            )
        )
    return FrozenBatch(
        target_company_id=target_company_id,
        report_period=report_period,
        cohort_ref=ObjectRef(
            kind="peer_cohort_snapshot", id=UUID(cohort_row.id), revision=cohort_row.revision
        ),
        cohort_content_hash=cohort_row.content_hash,
        companies=tuple(companies),
        metric_pack=cohort_metric_pack(cohort_row.payload),
    )


async def _materialize_evidence(
    session: AsyncSession,
    *,
    owner_id: OwnerId,
    frozen: dict[int, FrozenCompany],
) -> dict[tuple[int, str], ObjectRef]:
    """Materialize every frozen field that has a usable date basis (WP4).

    Idempotent per snapshot field (stable operation identity): a retry replays
    the saved receipt instead of writing a second evidence row. Fields with no
    source date at all stay snapshot-only - they are never given a fabricated
    publication time.
    """

    service = EvidenceService(session)
    refs: dict[tuple[int, str], ObjectRef] = {}
    for company_id in sorted(frozen):
        row = frozen[company_id].row
        for metric in sorted(row.fields):
            materialization = peer_field_evidence(row, metric)
            if not materialization.materialized or materialization.record is None:
                continue
            refs[(company_id, metric)] = await service.save_evidence(
                owner_id, materialization.record, peer_field_operation_id(row, metric)
            )
    await session.flush()
    return refs


def _acquired_spread(frozen: dict[int, FrozenCompany]) -> tuple[datetime | None, datetime | None]:
    """Earliest and latest input acquisition time over the frozen fields."""

    times = [
        PeerAnnualField.model_validate(company.row.fields[metric]).acquired_at
        for company in frozen.values()
        for metric in company.row.fields
    ]
    if not times:
        return None, None
    return min(times), max(times)


def _mapping_from_results(
    *,
    batch_run_id: str,
    cohort_row: PeerCohortSnapshot,
    report_period: date,
    provider: str,
    adapter_version: str,
    generation: int,
    submitted_at: datetime,
    results: list[TargetCalculationRefs],
    frozen: dict[int, FrozenCompany],
    missing_companies: list[MissingCompany],
    target_gaps: list[TargetGap],
    evidence_refs: dict[tuple[int, str], ObjectRef],
) -> PeerBatchMapping:
    """Assemble the submitted mapping (pure; the writer hashes and stores it)."""

    by_target = {result.target_company_id: result for result in results}
    targets: list[TargetMapping] = []
    reused: list[ObjectRef] = []
    gap_metrics = 0
    for company_id in sorted(frozen):
        result = by_target.get(company_id)
        if result is None:
            continue
        entries = []
        for ref in result.metric_refs:
            entries.append(
                MetricMappingEntry(
                    metric_key=ref.metric_key,
                    state=ref.state,
                    ref=ref.ref,
                    gap_reason=ref.gap_reason,
                )
            )
            if ref.state == "reused" and ref.ref is not None and ref.ref not in reused:
                reused.append(ref.ref)
            if ref.state == "gap":
                gap_metrics += 1
        targets.append(
            TargetMapping(
                company_id=result.target_company_id,
                listing_id=result.target_listing_id,
                ticker=result.target_ticker,
                name=frozen[company_id].name,
                report_period=result.report_period,
                metric_calculation_refs=tuple(entries),
                metric_pack=cohort_metric_pack(cohort_row.payload),
            )
        )

    has_gaps = bool(gap_metrics or target_gaps or missing_companies)
    referenced = sorted(
        evidence_refs.values(),
        key=lambda ref: (ref.kind, str(ref.id), ref.revision, ref.field_path or ""),
    )
    reused_refs = sorted(reused, key=lambda ref: str(ref.id))
    earliest, latest = _acquired_spread(frozen)
    return PeerBatchMapping.create(
        batch_run_id=batch_run_id,
        cohort_ref=ObjectRef(
            kind="peer_cohort_snapshot", id=UUID(cohort_row.id), revision=cohort_row.revision
        ),
        cohort_content_hash=cohort_row.content_hash,
        report_period=report_period,
        metric_pack=cohort_metric_pack(cohort_row.payload),
        provider=provider,
        adapter_version=adapter_version,
        generation=generation,
        status="partial" if has_gaps else "success",
        submitted_at=submitted_at,
        inputs_acquired_earliest=earliest,
        inputs_acquired_latest=latest,
        targets=tuple(targets),
        manifest=BatchManifest(
            referenced=tuple(referenced),
            reused=tuple(reused_refs),
            target_gaps=tuple(target_gaps),
            missing_companies=tuple(missing_companies),
        ),
    )


async def submit_offline_batch(
    session: AsyncSession,
    *,
    data_dir: Path,
    cohort_key: str = DEFAULT_COHORT_KEY,
    tickers: Sequence[str] | None = None,
    provider: str = DEFAULT_PROVIDER,
    adapter_version: str = DEFAULT_ADAPTER_VERSION,
    report_period: date | None = None,
    batch_run_id: str | None = None,
    owner_id: OwnerId = "local_owner",
    submitted_at: datetime | None = None,
) -> OfflineBatchSubmission:
    """Freeze, compute, save and submit one offline batch (see module docstring).

    ``tickers`` selects the target companies (default: the whole roster);
    every roster member always participates as a peer candidate.
    ``report_period`` defaults to the latest period with imported snapshots in
    the scope. ``submitted_at`` is injectable for tests and defaults to now.
    The caller owns the transaction; the artifact is written after the
    database work and before the caller's commit, so a failed commit leaves no
    submitted state even if the file exists (a re-run regenerates it).
    """

    if submitted_at is not None and submitted_at.tzinfo is None:
        raise ValueError("submitted_at must be timezone-aware")
    now = submitted_at or datetime.now(UTC)
    await reserve_batch_transaction(session)

    cohort_row = await PeerCohortSnapshotRepository(session).latest(cohort_key=cohort_key)
    if cohort_row is None:
        raise ValueError(
            f"no cohort roster saved under cohort_key={cohort_key!r}; import the roster first"
        )
    entries = {str(entry["ticker"]): entry for entry in cohort_row.payload["entries"]}
    if tickers:
        unknown = sorted(ticker for ticker in tickers if ticker not in entries)
        if unknown:
            raise ValueError(
                "target tickers outside the saved roster; nothing submitted: "
                + ", ".join(unknown)
            )
        target_entries = [entries[ticker] for ticker in tickers]
    else:
        target_entries = list(entries.values())

    target_ids = [int(entry["company_id"]) for entry in target_entries]
    period = report_period or await latest_snapshot_period(
        session, company_ids=target_ids, provider=provider, adapter_version=adapter_version
    )
    if period is None:
        raise ValueError(
            f"no imported annual snapshots for cohort {cohort_key!r} "
            f"(provider={provider}, adapter={adapter_version}); import the annual facts first"
        )

    frozen, missing_companies = await _freeze_companies(
        session,
        cohort_row=cohort_row,
        report_period=period,
        provider=provider,
        adapter_version=adapter_version,
    )
    target_gaps: list[TargetGap] = []
    buildable = []
    for entry in target_entries:
        company_id = int(entry["company_id"])
        if company_id in frozen:
            buildable.append(entry)
        else:
            target_gaps.append(
                TargetGap(
                    company_id=company_id,
                    ticker=str(entry["ticker"]),
                    reason=(
                        "the target has no frozen annual input for this batch period; "
                        "no calculation record is built for it"
                    ),
                )
            )

    evidence_refs = await _materialize_evidence(session, owner_id=owner_id, frozen=frozen)

    run_id = batch_run_id or _batch_run_id(now)
    results = [
        await save_peer_calculations(
            session,
            owner_id=owner_id,
            batch=_frozen_batch(
                cohort_row=cohort_row,
                report_period=period,
                frozen=frozen,
                target_company_id=int(entry["company_id"]),
            ),
            evidence_refs=evidence_refs,
            as_of=now,
            batch_run_id=run_id,
            # Declared-inputs submission: fields without a materialized
            # evidence reference (e.g. the undated run-02 evidence) are
            # declared in the record, never hidden - the read side shows the
            # evidence gap instead of treating the batch as fully cited.
            require_fact_refs=False,
        )
        for entry in buildable
    ]

    scan = await scan_committed_batch_mappings(session, data_dir)
    scope_cohort_ref = ObjectRef(
        kind="peer_cohort_snapshot", id=UUID(cohort_row.id), revision=cohort_row.revision
    )
    scope = mappings_in_scope(
        scan.valid,
        cohort_ref=scope_cohort_ref,
        report_period=period,
        provider=provider,
        adapter_version=adapter_version,
    )

    draft = _mapping_from_results(
        batch_run_id=run_id,
        cohort_row=cohort_row,
        report_period=period,
        provider=provider,
        adapter_version=adapter_version,
        generation=next_generation(scope),
        submitted_at=now,
        results=results,
        frozen=frozen,
        missing_companies=missing_companies,
        target_gaps=target_gaps,
        evidence_refs=evidence_refs,
    )
    existing = find_identical_submission(scope, targets_signature(draft))
    if existing is not None:
        return OfflineBatchSubmission(
            mapping=existing,
            path=mapping_artifact_path(data_dir, existing.batch_run_id),
            created=False,
            saved_records=0,
            reused_records=sum(
                1
                for target in existing.targets
                for ref in target.metric_calculation_refs
                if ref.state != "gap"
            ),
            gap_metrics=sum(
                1
                for target in existing.targets
                for ref in target.metric_calculation_refs
                if ref.state == "gap"
            ),
            frozen_companies=len(frozen),
        )

    await register_batch_commit(session, draft, owner=owner_id)
    path = write_batch_mapping(data_dir, draft)
    saved = sum(1 for result in results for ref in result.metric_refs if ref.state == "saved")
    reused_records = sum(
        1 for result in results for ref in result.metric_refs if ref.state == "reused"
    )
    gap_metrics = sum(
        1 for result in results for ref in result.metric_refs if ref.state == "gap"
    )
    return OfflineBatchSubmission(
        mapping=draft,
        path=path,
        created=True,
        saved_records=saved,
        reused_records=reused_records,
        gap_metrics=gap_metrics,
        frozen_companies=len(frozen),
    )


__all__ = [
    "DEFAULT_COHORT_KEY",
    "FrozenCompany",
    "OfflineBatchSubmission",
    "latest_snapshot_period",
    "submit_offline_batch",
]
