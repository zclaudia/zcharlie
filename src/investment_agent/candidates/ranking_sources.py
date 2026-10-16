"""Freeze the read-only candidate universe used by account rankings.

This module deliberately does not call the candidate intake helpers.  Screen
rows, candidate lifecycle and watchlist state are read in batches and copied
into the manifest so a worker never follows mutable "latest" pointers.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, cast

from pydantic import JsonValue, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.candidates.lifecycle import CandidatePolicy
from investment_agent.candidates.ranking_contracts import (
    CandidateContextValue,
    CandidateLifecycleSnapshot,
    CandidatePolicySnapshot,
    OpenCandidateSource,
    RankingSourcePolicy,
    ScreenCandidateSource,
    SourceExclusion,
    SourceIssue,
    SourceManifest,
    SourceMember,
    SourceRunSnapshot,
    WatchlistCandidateSource,
    build_source_manifest,
)
from investment_agent.clock import SHANGHAI_TZ
from investment_agent.domain.time import as_utc
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.rules import ScreenResultRecord
from investment_agent.storage.models.user_domain import WatchlistItem

MAX_SOURCE_MEMBERS = 2_000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_SCREEN_KINDS = {"screen_run"}
_OUTCOME_KEYS = ("hit", "truncated", "miss", "skipped", "excluded")


class SourceManifestCapacityError(InvestorError):
    """The frozen evidence itself exceeds the manifest byte cap."""

    def __init__(self) -> None:
        super().__init__("source_capacity_exceeded", "候选来源冻结内容超过容量限制")


def _is_manifest_size_error(error: ValidationError) -> bool:
    details = error.errors()
    return bool(details) and all(
        detail.get("loc") == ("canonical_bytes",)
        and detail.get("type") == "less_than_equal"
        for detail in details
    )


def _utc(value: datetime) -> datetime:
    return as_utc(value)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda item: item.isoformat() if isinstance(item, (date, datetime)) else str(item),
    ).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _object_ref(
    kind: Literal["listing", "pipeline_run", "candidate", "watchlist_item"], ident: int
) -> ObjectRef:
    return ObjectRef(kind=kind, id=ident, revision=1)


def _date_as_datetime(value: date) -> datetime:
    """Interpret source business dates at Shanghai midnight, then store UTC."""
    return datetime.combine(value, time.min, tzinfo=SHANGHAI_TZ).astimezone(UTC)


def _context_items(raw: Mapping[str, Any] | None) -> tuple[CandidateContextValue, ...]:
    return tuple(
        CandidateContextValue(key=str(key), value=value)
        for key, value in sorted((raw or {}).items())
    )


def _row_payload(row: ScreenResultRecord) -> dict[str, Any]:
    return {
        "id": row.id,
        "screen_run_id": row.screen_run_id,
        "as_of": row.as_of.isoformat(),
        "template_id": row.template_id,
        "template_version": row.template_version,
        "listing_id": row.listing_id,
        "ticker": row.ticker,
        "outcome": row.outcome,
        "hit": row.hit,
        "rank": row.rank,
        "strength": row.strength,
        "reasons": row.reasons or [],
        "context": row.context or {},
        "created_at": _utc(row.created_at).isoformat() if row.created_at else None,
    }


def _run_hash(run: PipelineRun, rows: Sequence[ScreenResultRecord]) -> str:
    return _hash(
        {
            "run_id": run.id,
            "kind": run.kind,
            "status": run.status,
            "request": run.request_json or {},
            "metadata": run.metadata_json or {},
            "started_at": _utc(run.started_at).isoformat() if run.started_at else None,
            "finished_at": _utc(run.finished_at).isoformat() if run.finished_at else None,
            "rows": [_row_payload(row) for row in rows],
        }
    )


def _template_key(template_id: str, version: str | int) -> str:
    return f"{template_id}@{version}"


def _counts_match(
    metadata: Mapping[str, Any], observed: Mapping[tuple[str, str], Mapping[str, int]]
) -> bool:
    templates = metadata.get("templates")
    if not isinstance(templates, dict) or not templates:
        return False
    metadata_templates: set[tuple[str, str]] = set()
    for template_ref, summary in templates.items():
        if not isinstance(summary, dict) or not isinstance(summary.get("counts"), dict):
            return False
        try:
            template_id, version = str(template_ref).rsplit("@", 1)
        except ValueError:
            return False
        metadata_templates.add((template_id, version))
        counts = summary["counts"]
        expected: dict[str, int] = {}
        for key in _OUTCOME_KEYS:
            value = counts.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return False
            expected[key] = value
        actual = observed.get((template_id, version), {})
        if any(actual.get(key, 0) != expected[key] for key in _OUTCOME_KEYS):
            return False
        if sum(expected.values()) != sum(actual.get(key, 0) for key in _OUTCOME_KEYS):
            return False
    return set(observed).issubset(metadata_templates)


def _run_complete(run: PipelineRun, observed: Mapping[tuple[str, str], Mapping[str, int]]) -> bool:
    metadata = run.metadata_json or {}
    if run.status != "success" or run.finished_at is None:
        return False
    if metadata.get("load_errors"):
        return False
    expected_rows = metadata.get("result_rows")
    if isinstance(expected_rows, bool) or not isinstance(expected_rows, int) or expected_rows < 0:
        return False
    if sum(sum(values.values()) for values in observed.values()) != expected_rows:
        return False
    return _counts_match(metadata, observed)


def _template_versions(run: PipelineRun) -> dict[str, str]:
    templates = (run.metadata_json or {}).get("templates")
    if not isinstance(templates, dict):
        return {}
    result: dict[str, str] = {}
    for template_ref in templates:
        try:
            template_id, version = str(template_ref).rsplit("@", 1)
        except ValueError:
            continue
        result[template_id] = version
    return result


def _observed_counts(
    rows: Iterable[ScreenResultRecord],
) -> dict[int, dict[tuple[str, str], dict[str, int]]]:
    counts: dict[int, dict[tuple[str, str], dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    for row in rows:
        counts[row.screen_run_id][(row.template_id, str(row.template_version))][row.outcome] += 1
    return counts


async def resolve_source_runs(
    session: AsyncSession,
    *,
    as_of: datetime,
    policy: RankingSourcePolicy,
    search_batch_size: int = 128,
) -> tuple[tuple[SourceRunSnapshot, ...], tuple[SourceIssue, ...]]:
    """Resolve the latest complete successful batch for each fixed template.

    The search is set-based and keyset paged.  Once selected, exact run ids and
    content hashes are returned; downstream freezing never selects latest.
    """

    if search_batch_size < 1 or search_batch_size > 1_000:
        raise ValueError("search_batch_size must be between 1 and 1000")
    requested = tuple(policy.template_ids)
    as_of_utc = _utc(as_of)
    selected: dict[str, tuple[PipelineRun, str]] = {}
    observed_refs: dict[str, list[ObjectRef]] = {template_id: [] for template_id in requested}
    last_finished: datetime | None = None
    last_id: int | None = None

    while len(selected) < len(requested):
        stmt = select(PipelineRun).where(
            PipelineRun.kind.in_(list(_SCREEN_KINDS)),
            PipelineRun.status == "success",
            PipelineRun.finished_at.is_not(None),
            PipelineRun.finished_at <= as_of_utc,
        )
        if last_finished is not None and last_id is not None:
            stmt = stmt.where(
                (PipelineRun.finished_at < last_finished)
                | ((PipelineRun.finished_at == last_finished) & (PipelineRun.id < last_id))
            )
        stmt = stmt.order_by(PipelineRun.finished_at.desc(), PipelineRun.id.desc()).limit(
            search_batch_size
        )
        runs = list((await session.execute(stmt)).scalars())
        if not runs:
            break
        last_finished = runs[-1].finished_at
        last_id = runs[-1].id
        run_ids = [run.id for run in runs]
        grouped = await session.execute(
            select(
                ScreenResultRecord.screen_run_id,
                ScreenResultRecord.template_id,
                ScreenResultRecord.template_version,
                ScreenResultRecord.outcome,
                func.count(ScreenResultRecord.id),
            )
            .where(ScreenResultRecord.screen_run_id.in_(run_ids))
            .group_by(
                ScreenResultRecord.screen_run_id,
                ScreenResultRecord.template_id,
                ScreenResultRecord.template_version,
                ScreenResultRecord.outcome,
            )
        )
        grouped_counts: dict[int, dict[tuple[str, str], dict[str, int]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        for run_id, template_id, version, outcome, count in grouped:
            grouped_counts[run_id][(template_id, str(version))][outcome] = int(count)

        for run in runs:
            run_counts = grouped_counts.get(run.id, {})
            versions = _template_versions(run)
            if not versions:
                continue
            if not _run_complete(run, run_counts):
                for template_id in requested:
                    if template_id in versions and template_id not in selected:
                        observed_refs[template_id].append(_object_ref("pipeline_run", run.id))
                continue
            request_as_of = (run.metadata_json or {}).get("as_of")
            if not isinstance(request_as_of, str):
                request_as_of = (run.request_json or {}).get("as_of")
            try:
                screen_as_of = date.fromisoformat(str(request_as_of)[:10])
            except ValueError:
                for template_id in requested:
                    if template_id in versions and template_id not in selected:
                        observed_refs[template_id].append(_object_ref("pipeline_run", run.id))
                continue
            if screen_as_of > as_of.astimezone(SHANGHAI_TZ).date():
                continue
            for template_id in requested:
                if template_id in selected or template_id not in versions:
                    continue
                selected[template_id] = (run, versions[template_id])
            if len(selected) == len(requested):
                break

    selected_run_ids = sorted({run.id for run, _ in selected.values()})
    rows_by_run: dict[int, list[ScreenResultRecord]] = defaultdict(list)
    if selected_run_ids:
        rows = list(
            (
                await session.execute(
                    select(ScreenResultRecord)
                    .where(ScreenResultRecord.screen_run_id.in_(selected_run_ids))
                    .order_by(
                        ScreenResultRecord.screen_run_id,
                        ScreenResultRecord.template_id,
                        ScreenResultRecord.template_version,
                        ScreenResultRecord.listing_id,
                    )
                )
            ).scalars()
        )
        for row in rows:
            rows_by_run[row.screen_run_id].append(row)

    snapshots: list[SourceRunSnapshot] = []
    issues: list[SourceIssue] = []
    for template_id in requested:
        picked = selected.get(template_id)
        if picked is None:
            observed = tuple(dict.fromkeys(observed_refs[template_id]))
            issues.append(
                SourceIssue(
                    template_id=template_id,
                    kind=("no_complete_run" if observed else "missing_run"),
                    reason_code=("screen_run_incomplete" if observed else "screen_run_missing"),
                    observed_run_refs=observed,
                )
            )
            continue
        run, version = picked
        if run.finished_at is None:
            raise ValueError(f"selected screen run {run.id} has no finished_at")
        matched = [
            row
            for row in rows_by_run[run.id]
            if row.template_id == template_id and str(row.template_version) == version
        ]
        snapshots.append(
            SourceRunSnapshot(
                run_ref=_object_ref("pipeline_run", run.id),
                status="success",
                finished_at=_utc(run.finished_at),
                run_hash=_run_hash(run, rows_by_run[run.id]),
                as_of=_date_as_datetime(
                    date.fromisoformat(
                        str(
                            (run.metadata_json or {}).get("as_of")
                            or (run.request_json or {}).get("as_of")
                        )[:10]
                    )
                ),
                template_id=template_id,
                template_version=version,
                row_count=len(matched),
            )
        )
    return tuple(snapshots), tuple(issues)


def _candidate_snapshot(row: CandidateRecord, listing_ref: ObjectRef) -> CandidateLifecycleSnapshot:
    sources = tuple(row.sources or ())
    manual = any(isinstance(source, dict) and source.get("kind") == "manual" for source in sources)
    payload = {
        "candidate_id": row.id,
        "listing_ref": listing_ref.model_dump(mode="json"),
        "status": row.status,
        "opened_at": _utc(row.opened_at).isoformat(),
        "expires_at": _utc(row.expires_at).isoformat(),
        "closed_at": _utc(row.closed_at).isoformat() if row.closed_at else None,
        "candidate_sources": sources,
        "manual_nomination": manual,
    }
    return CandidateLifecycleSnapshot(
        source_kind="candidate_lifecycle",
        source_ref=_object_ref("candidate", row.id),
        listing_ref=listing_ref,
        candidate_id=row.id,
        status=cast(Literal["open", "promoted", "dismissed", "expired"], row.status),
        opened_at=_utc(row.opened_at),
        expires_at=_utc(row.expires_at),
        closed_at=_utc(row.closed_at) if row.closed_at else None,
        source_snapshot_hash=_hash(payload),
        candidate_sources=sources,
        manual_nomination=manual,
    )


def _open_source(row: CandidateRecord, listing_ref: ObjectRef) -> OpenCandidateSource:
    sources = tuple(row.sources or ())
    manual = any(isinstance(source, dict) and source.get("kind") == "manual" for source in sources)
    payload = {
        "candidate_id": row.id,
        "listing_ref": listing_ref.model_dump(mode="json"),
        "status": row.status,
        "opened_at": _utc(row.opened_at).isoformat(),
        "expires_at": _utc(row.expires_at).isoformat(),
        "candidate_sources": sources,
        "manual_nomination": manual,
    }
    return OpenCandidateSource(
        source_kind="open_candidate",
        source_ref=_object_ref("candidate", row.id),
        listing_ref=listing_ref,
        candidate_id=row.id,
        status="open",
        opened_at=_utc(row.opened_at),
        expires_at=_utc(row.expires_at),
        closed_at=None,
        source_snapshot_hash=_hash(payload),
        candidate_sources=sources,
        manual_nomination=manual,
    )


def _watchlist_source(row: WatchlistItem, listing_ref: ObjectRef) -> WatchlistCandidateSource:
    changed_at = _utc(row.updated_at or row.added_at)
    payload = {
        "item_id": row.id,
        "listing_ref": listing_ref.model_dump(mode="json"),
        "status": "active" if row.enabled else "paused",
        "changed_at": changed_at.isoformat(),
        "provider": row.provider,
        "report_year": row.report_year,
        "note": row.note,
        "industry": row.industry,
        "last_refreshed_at": _utc(row.last_refreshed_at).isoformat()
        if row.last_refreshed_at
        else None,
        "last_refresh_status": row.last_refresh_status,
    }
    return WatchlistCandidateSource(
        source_kind="watchlist",
        source_ref=_object_ref("watchlist_item", row.id),
        listing_ref=listing_ref,
        item_id=row.id,
        status="active" if row.enabled else "paused",
        changed_at=changed_at,
        source_snapshot_hash=_hash(payload),
        provider=row.provider,
        report_year=row.report_year,
        note=row.note,
        industry=row.industry,
        last_refreshed_at=_utc(row.last_refreshed_at) if row.last_refreshed_at else None,
        last_refresh_status=row.last_refresh_status,
    )


def _screen_source(
    row: ScreenResultRecord,
    run: SourceRunSnapshot,
) -> ScreenCandidateSource:
    listing_ref = _object_ref("listing", row.listing_id)
    run_ref = run.run_ref
    payload = _row_payload(row)
    return ScreenCandidateSource(
        source_kind="screen_hit" if row.outcome == "hit" else "screen_truncated",
        source_ref=run_ref.model_copy(update={"field_path": f"screen_results.{row.id}"}),
        listing_ref=listing_ref,
        run_ref=run_ref,
        source_row_id=row.id,
        run_status="success",
        run_finished_at=run.finished_at,
        run_hash=run.run_hash,
        source_snapshot_hash=_hash(payload),
        row_snapshot=cast(dict[str, JsonValue], payload),
        outcome=cast(Literal["hit", "truncated"], row.outcome),
        source_as_of=_date_as_datetime(row.as_of),
        template_id=row.template_id,
        template_version=str(row.template_version),
        rank=int(row.rank or 1),
        strength=str(row.strength) if row.strength is not None else None,
        reasons=cast(tuple[JsonValue, ...], tuple(row.reasons or ())),
        context=_context_items(row.context),
    )


async def freeze_candidate_sources(
    session: AsyncSession,
    *,
    as_of: datetime,
    source_runs: Sequence[SourceRunSnapshot],
    source_issues: Sequence[SourceIssue] = (),
    candidate_policy: CandidatePolicy,
    source_cap: int = MAX_SOURCE_MEMBERS,
) -> SourceManifest:
    """Copy exact selected rows and current candidate/watchlist decisions.

    ``source_runs`` are already selected by :func:`resolve_source_runs`; this
    function never queries a "latest" run.  All mutable source content needed
    by a worker is embedded in the returned immutable manifest.
    """

    if source_cap < 1 or source_cap > MAX_SOURCE_MEMBERS:
        raise ValueError(f"source_cap must be in [1, {MAX_SOURCE_MEMBERS}]")
    run_by_pair = {
        (int(run.run_ref.id), run.template_id, run.template_version): run for run in source_runs
    }
    run_ids = sorted({int(run.run_ref.id) for run in source_runs})
    frozen_rows: list[ScreenResultRecord] = []
    if run_ids:
        frozen_rows = list(
            (
                await session.execute(
                    select(ScreenResultRecord)
                    .where(
                        ScreenResultRecord.screen_run_id.in_(run_ids),
                        ScreenResultRecord.outcome.in_(["hit", "truncated"]),
                    )
                    .order_by(
                        ScreenResultRecord.screen_run_id,
                        ScreenResultRecord.template_id,
                        ScreenResultRecord.template_version,
                        ScreenResultRecord.rank,
                        ScreenResultRecord.listing_id,
                    )
                )
            ).scalars()
        )
    rows_by_run: dict[int, list[ScreenResultRecord]] = defaultdict(list)
    for row in frozen_rows:
        rows_by_run[row.screen_run_id].append(row)
    run_records = (
        {
            run.id: run
            for run in await session.scalars(select(PipelineRun).where(PipelineRun.id.in_(run_ids)))
        }
        if run_ids
        else {}
    )
    all_rows_by_run: dict[int, list[ScreenResultRecord]] = defaultdict(list)
    if run_ids:
        all_rows = list(
            (
                await session.execute(
                    select(ScreenResultRecord)
                    .where(ScreenResultRecord.screen_run_id.in_(run_ids))
                    .order_by(
                        ScreenResultRecord.screen_run_id,
                        ScreenResultRecord.template_id,
                        ScreenResultRecord.template_version,
                        ScreenResultRecord.listing_id,
                    )
                )
            ).scalars()
        )
        for row in all_rows:
            all_rows_by_run[row.screen_run_id].append(row)
    for run in source_runs:
        run_id = int(run.run_ref.id)
        current = run_records.get(run_id)
        if current is None or current.status != "success" or current.finished_at is None:
            raise InvestorError(
                "revision_conflict",
                f"已选择的筛选批次 {run_id} 在冻结时发生变化",
                (run.run_ref,),
            )
        run_rows = all_rows_by_run[run_id]
        observed = _observed_counts(run_rows).get(run_id, {})
        if not _run_complete(current, observed) or _run_hash(current, run_rows) != run.run_hash:
            raise InvestorError(
                "revision_conflict",
                f"已选择的筛选批次 {run_id} 在冻结时发生变化",
                (run.run_ref,),
            )
    screen_sources_by_listing: dict[int, list[ScreenCandidateSource]] = defaultdict(list)
    for row in frozen_rows:
        snapshot = run_by_pair.get((row.screen_run_id, row.template_id, str(row.template_version)))
        if snapshot is not None:
            screen_sources_by_listing[row.listing_id].append(_screen_source(row, snapshot))

    screen_listing_ids = sorted(screen_sources_by_listing)
    candidates: list[CandidateRecord] = []
    if screen_listing_ids:
        candidates.extend(
            await session.scalars(
                select(CandidateRecord)
                .where(CandidateRecord.listing_id.in_(screen_listing_ids))
                .order_by(CandidateRecord.listing_id, CandidateRecord.opened_at, CandidateRecord.id)
            )
        )
    candidates.extend(
        await session.scalars(
            select(CandidateRecord)
            .where(CandidateRecord.status == "open")
            .order_by(CandidateRecord.listing_id, CandidateRecord.opened_at, CandidateRecord.id)
        )
    )
    unique_candidates = {row.id: row for row in candidates}
    candidates_by_listing: dict[int, list[CandidateRecord]] = defaultdict(list)
    for candidate_row in unique_candidates.values():
        candidates_by_listing[candidate_row.listing_id].append(candidate_row)
    for values in candidates_by_listing.values():
        values.sort(key=lambda row: (row.opened_at, row.id))

    listing_ids = sorted(set(screen_listing_ids) | set(candidates_by_listing))
    watchlists: dict[int, WatchlistItem] = {}
    if listing_ids:
        watchlists = {
            row.listing_id: row
            for row in await session.scalars(
                select(WatchlistItem).where(
                    WatchlistItem.listing_id.in_(listing_ids), WatchlistItem.enabled.is_(True)
                )
            )
        }

    member_sources: dict[int, list[Any]] = defaultdict(list)
    excluded_sources: dict[int, list[Any]] = defaultdict(list)
    exclusion_reasons: dict[int, set[str]] = defaultdict(set)

    for listing_id, sources in screen_sources_by_listing.items():
        listing_ref = _object_ref("listing", listing_id)
        history = candidates_by_listing.get(listing_id, [])
        valid_open = [
            row for row in history if row.status == "open" and _utc(row.expires_at) > as_of
        ]
        open_candidate = max(valid_open, key=lambda row: (row.opened_at, row.id), default=None)
        if open_candidate is not None:
            member_sources[listing_id].append(_open_source(open_candidate, listing_ref))
            member_sources[listing_id].extend(sources)
            continue

        latest = max(
            history, key=lambda row: (row.closed_at or row.expires_at, row.id), default=None
        )
        lifecycle = _candidate_snapshot(latest, listing_ref) if latest is not None else None
        for source in sources:
            reason: str | None = None
            if latest is not None:
                closed_at = _utc(latest.closed_at) if latest.closed_at else _utc(latest.expires_at)
                if latest.status == "dismissed":
                    cooldown_until = closed_at + timedelta(
                        days=candidate_policy.dismiss_cooldown_days
                    )
                    # Match screen_intake.dismissed_since(), whose inclusive
                    # ``closed_at >= at - cooldown`` boundary keeps the listing
                    # suppressed until the instant after the cooldown expires.
                    if (
                        candidate_policy.dismiss_cooldown_days > 0
                        and as_of <= cooldown_until
                    ):
                        reason = "dismiss_cooldown"
                    elif source.run_finished_at <= closed_at:
                        reason = "old_dismissed_source"
                elif latest.status == "open":
                    if source.run_finished_at <= _utc(latest.expires_at):
                        reason = "old_logically_expired_source"
                elif source.run_finished_at <= closed_at:
                    reason = f"old_{latest.status}_source"
            if reason is None:
                member_sources[listing_id].append(source)
            else:
                excluded_sources[listing_id].append(source)
                exclusion_reasons[listing_id].add(reason)
                if lifecycle is not None:
                    excluded_sources[listing_id].append(lifecycle)

    # Existing valid open candidates are members even when no selected screen
    # batch had a hit for them.
    for listing_id, history in candidates_by_listing.items():
        if listing_id in screen_sources_by_listing:
            continue
        valid_open = [
            row for row in history if row.status == "open" and _utc(row.expires_at) > as_of
        ]
        if valid_open:
            candidate_row = max(
                valid_open, key=lambda candidate: (candidate.opened_at, candidate.id)
            )
            member_sources[listing_id].append(
                _open_source(candidate_row, _object_ref("listing", listing_id))
            )

    # Watchlist state wins over any open candidate or screening source.
    all_source_ids = set(member_sources) | set(excluded_sources)
    for listing_id in sorted(all_source_ids & set(watchlists)):
        listing_ref = _object_ref("listing", listing_id)
        watch = _watchlist_source(watchlists[listing_id], listing_ref)
        excluded_sources[listing_id].extend(member_sources.pop(listing_id, []))
        excluded_sources[listing_id].append(watch)
        exclusion_reasons[listing_id] = {"active_watchlist"}

    exclusions: list[SourceExclusion] = []
    for listing_id, sources in sorted(excluded_sources.items()):
        # One source row can be reached through several rules.  Keep each typed
        # snapshot once while retaining all applicable reason codes.
        deduped: dict[tuple[Any, ...], Any] = {}
        for source in sources:
            identity = (
                source.source_ref,
                getattr(source, "source_row_id", None),
                getattr(source, "source_snapshot_hash", None),
            )
            deduped[identity] = source
        for reason in sorted(exclusion_reasons.get(listing_id) or {"business_exclusion"}):
            exclusions.append(
                SourceExclusion(
                    kind="business",
                    listing_ref=_object_ref("listing", listing_id),
                    reason_code=reason,
                    source_snapshots=tuple(deduped.values()),
                )
            )

    eligible = [
        (listing_id, tuple(sorted(sources, key=_source_sort_key)))
        for listing_id, sources in member_sources.items()
        if sources
    ]
    eligible.sort(key=lambda item: item[0])
    total = len(eligible)
    initial_count = min(source_cap, total)

    def build(processed: int) -> SourceManifest:
        unprocessed = total - processed
        final_exclusions = list(exclusions)
        if unprocessed:
            final_exclusions.append(
                SourceExclusion(
                    kind="source_cap",
                    listing_ref=None,
                    reason_code="source_cap_exceeded",
                    unprocessed_range_start=processed,
                    unprocessed_range_end=total - 1,
                )
            )
        members = tuple(
            SourceMember(listing_ref=_object_ref("listing", listing_id), sources=sources)
            for listing_id, sources in eligible[:processed]
        )
        snapshot_hashes = tuple(
            sorted(source.source_snapshot_hash for member in members for source in member.sources)
            + [
                source.source_snapshot_hash
                for exclusion in final_exclusions
                for source in exclusion.source_snapshots
            ]
        )
        # The DTO requires one globally sorted hash vector across both members
        # and exclusion evidence.
        snapshot_hashes = tuple(sorted(snapshot_hashes))
        payload: dict[str, Any] = {
            "source_run_refs": tuple(run.run_ref for run in source_runs),
            "source_runs": tuple(source_runs),
            "candidate_policy": CandidatePolicySnapshot(
                expiry_days=candidate_policy.expiry_days,
                dismiss_cooldown_days=candidate_policy.dismiss_cooldown_days,
            ),
            "as_of": as_of,
            "members": members,
            "exclusions": tuple(final_exclusions),
            "source_issues": tuple(source_issues),
            "coverage": "partial" if unprocessed or source_issues else "complete",
            "total_unique_count": total,
            "processed_count": processed,
            "unprocessed_count": unprocessed,
            "source_hashes": snapshot_hashes,
        }
        return build_source_manifest(payload)

    try:
        return build(initial_count)
    except ValidationError as initial_error:
        if not _is_manifest_size_error(initial_error):
            raise
        # Reduce the frozen member prefix until both the member limit and the
        # 4 MiB canonical manifest limit are satisfied. Exclusion evidence is
        # never silently dropped to make the payload fit.
        low, high = 0, initial_count - 1
        best: SourceManifest | None = None
        while low <= high:
            middle = (low + high) // 2
            try:
                candidate = build(middle)
            except ValidationError as candidate_error:
                if not _is_manifest_size_error(candidate_error):
                    raise
                high = middle - 1
            else:
                best = candidate
                low = middle + 1
        if best is not None:
            return best
        raise SourceManifestCapacityError() from initial_error


def _source_sort_key(source: Any) -> tuple[Any, ...]:
    if isinstance(source, ScreenCandidateSource):
        return (
            0,
            source.template_id,
            source.template_version,
            source.rank,
            source.run_finished_at,
            source.source_row_id,
        )
    if isinstance(source, OpenCandidateSource):
        return (1, source.manual_nomination is False, source.opened_at, source.candidate_id)
    return (2, str(source.source_ref))


__all__ = [
    "MAX_MANIFEST_BYTES",
    "MAX_SOURCE_MEMBERS",
    "SourceManifestCapacityError",
    "freeze_candidate_sources",
    "resolve_source_runs",
]
