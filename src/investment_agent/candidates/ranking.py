"""Deterministic ordering and cursor pagination for frozen ranking snapshots."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from investment_agent.candidates.ranking_contracts import (
    OpenCandidateSource,
    RankingCursorClaims,
    RankingEntry,
    ScreenCandidateSource,
    SourceManifest,
)
from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.suitability_contracts import AccountFitAssessment, FitCheck

Bucket = Literal["incomplete", "blocked"]
FinancialStatus = Literal["pass", "warning", "blocked", "unknown"]
FILTERS = {
    "bucket": {"incomplete", "blocked"},
    "financial_status": {"pass", "warning", "blocked", "unknown"},
}


class RankingCursorError(ValueError):
    """A cursor is malformed or belongs to different ranking filters."""


class RankingOrderError(ValueError):
    """Entries do not form exactly the frozen source-member set."""


@dataclass(frozen=True)
class RankingPage:
    entries: tuple[RankingEntry, ...]
    count: int
    next_cursor: str | None


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _ref_key(ref: ObjectRef) -> tuple[str, str, int, str]:
    return (
        ref.kind,
        str(ref.id),
        ref.revision,
        ref.field_path or "",
    )


def _filter_hash(
    bucket_filter: Bucket | None,
    financial_status_filter: FinancialStatus | None,
) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "bucket": bucket_filter,
                "financial_status": financial_status_filter,
            }
        )
    ).hexdigest()


def _manual_source(member: object) -> tuple[bool, bool, object | None]:
    sources = getattr(member, "sources", ())
    candidates = [source for source in sources if isinstance(source, OpenCandidateSource)]
    manual = any(source.manual_nomination for source in candidates)
    if not candidates:
        return False, False, None
    opened_at = min(source.opened_at for source in candidates)
    return True, manual, opened_at


def build_ranking_entries(
    manifest: SourceManifest,
    *,
    ranking_ref: ObjectRef,
    resolved_actions: Mapping[int, Literal["open", "add", "hold"] | None],
    assessments: Mapping[int, AccountFitAssessment],
    listing_labels: Mapping[int, tuple[str, str]] | None = None,
    action_unknown_reasons: Mapping[int, str] | None = None,
    failed_items: Mapping[int, str] | None = None,
) -> tuple[RankingEntry, ...]:
    """Create one finite account-ranking entry for each processed member.

    ``resolved_actions`` is supplied by the account snapshot resolver.  A
    ``None`` action represents unknown holding presence and intentionally does
    not create a fit assessment or execution failure.
    """

    if ranking_ref.kind != "candidate_ranking" or ranking_ref.revision != 1:
        raise ValueError("ranking_ref must identify revision 1 of a candidate ranking")
    reasons = action_unknown_reasons or {}
    failures = failed_items or {}
    labels = listing_labels or {}
    entries: list[RankingEntry] = []
    for member in manifest.members:
        listing_id = int(member.listing_ref.id)
        action = resolved_actions.get(listing_id)
        assessment = assessments.get(listing_id)
        execution_error = failures.get(listing_id)
        reason: str | None = None
        fit_ref: ObjectRef | None = None
        checks: tuple[FitCheck, ...] = ()
        if action is None:
            reason = reasons.get(listing_id, "holding_presence_unknown")
            financial_status: FinancialStatus = "unknown"
            execution_error = None
        elif execution_error is not None:
            financial_status = "unknown"
        elif assessment is None:
            financial_status = "unknown"
            execution_error = "evaluation_failed"
        else:
            financial_status = assessment.financial_status
            fit_ref = assessment.reference
            checks = assessment.checks
        bucket: Bucket = "blocked" if financial_status == "blocked" else "incomplete"
        label = labels.get(listing_id)
        entries.append(
            RankingEntry(
                ranking_ref=ranking_ref,
                listing_ref=member.listing_ref,
                listing_ticker=label[0] if label is not None else None,
                listing_name=label[1] if label is not None else None,
                ordinal=0,
                ranking_bucket=bucket,
                financial_status=financial_status,
                resolved_action=action,
                action_unknown_reason=reason,
                fit_ref=fit_ref,
                source_refs=tuple(source.source_ref for source in member.sources),
                checks=checks,
                execution_error=execution_error,
            )
        )
    return order_ranking_entries(manifest, tuple(entries))


def order_ranking_entries(
    manifest: SourceManifest,
    entries: Sequence[RankingEntry],
) -> tuple[RankingEntry, ...]:
    """Apply the frozen v1 bucket and template round-robin policy.

    Within a selected ``template_id@version`` queue, the original screen rank
    is preserved.  Manual and otherwise-open candidates have explicit stable
    queues after template queues; no strength values are compared across
    templates.
    """

    source_by_listing = {int(member.listing_ref.id): member for member in manifest.members}
    entry_by_listing = {int(entry.listing_ref.id): entry for entry in entries}
    if len(entry_by_listing) != len(entries) or set(entry_by_listing) != set(source_by_listing):
        raise RankingOrderError("ranking entries must match each frozen source member exactly once")
    if any(
        entry.listing_ref != source_by_listing[listing_id].listing_ref
        for listing_id, entry in entry_by_listing.items()
    ):
        raise RankingOrderError("ranking entry listing identity differs from its source member")

    selected_run_by_template = {
        (run.template_id, run.template_version): _ref_key(run.run_ref)
        for run in manifest.source_runs
    }
    template_keys = list(dict.fromkeys(selected_run_by_template))
    result: list[RankingEntry] = []
    for bucket in ("incomplete", "blocked"):
        bucket_ids = {
            listing_id
            for listing_id, entry in entry_by_listing.items()
            if entry.ranking_bucket == bucket
        }
        queues: dict[tuple[str, str], list[int]] = {}
        for template in template_keys:
            run_key = selected_run_by_template[template]
            ranked: list[tuple[int, int]] = []
            for listing_id in bucket_ids:
                sources = source_by_listing[listing_id].sources
                matches = [
                    source
                    for source in sources
                    if isinstance(source, ScreenCandidateSource)
                    and (source.template_id, source.template_version) == template
                    and _ref_key(source.run_ref) == run_key
                ]
                if matches:
                    ranked.append((min(source.rank for source in matches), listing_id))
            queues[template] = [listing_id for _, listing_id in sorted(ranked)]

        # Each template gets its next unseen listing on every turn. A shared
        # hit must not consume another template's turn without emitting one.
        positions = {template: 0 for template in template_keys}
        emitted: set[int] = set()
        while True:
            advanced = False
            for template in template_keys:
                queue = queues.get(template, [])
                position = positions[template]
                while position < len(queue) and queue[position] in emitted:
                    position += 1
                positions[template] = position
                if position >= len(queue):
                    continue
                listing_id = queue[position]
                positions[template] = position + 1
                advanced = True
                if listing_id not in emitted:
                    emitted.add(listing_id)
                    result.append(entry_by_listing[listing_id])
            if not advanced:
                break

        open_only: list[tuple[object, int]] = []
        manual_only: list[tuple[object, int]] = []
        for listing_id in bucket_ids - emitted:
            has_open, manual, opened_at = _manual_source(source_by_listing[listing_id])
            if has_open and opened_at is not None:
                (manual_only if manual else open_only).append((opened_at, listing_id))
        for _, listing_id in sorted(open_only, key=lambda item: (item[0], item[1])):
            emitted.add(listing_id)
            result.append(entry_by_listing[listing_id])
        for _, listing_id in sorted(manual_only, key=lambda item: (item[0], item[1])):
            emitted.add(listing_id)
            result.append(entry_by_listing[listing_id])

        # Defensive final queue: only unusual source kinds can reach here.
        for listing_id in sorted(bucket_ids - emitted):
            result.append(entry_by_listing[listing_id])

    return tuple(
        entry.model_copy(update={"ordinal": ordinal}) for ordinal, entry in enumerate(result)
    )


def make_ranking_cursor(
    *,
    ranking_ref: ObjectRef,
    ordinal: int,
    bucket_filter: Bucket | None = None,
    financial_status_filter: FinancialStatus | None = None,
) -> str:
    if not ordinal >= 0:
        raise ValueError("ordinal must be non-negative")
    claims = RankingCursorClaims(
        ranking_ref=ranking_ref,
        ordinal=ordinal,
        bucket_filter=bucket_filter,
        financial_status_filter=financial_status_filter,
        filter_hash=_filter_hash(bucket_filter, financial_status_filter),
    )
    raw = _canonical(claims.model_dump(mode="json"))
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def parse_ranking_cursor(
    cursor: str,
    *,
    ranking_ref: ObjectRef,
    bucket_filter: Bucket | None = None,
    financial_status_filter: FinancialStatus | None = None,
) -> RankingCursorClaims:
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        claims = RankingCursorClaims.model_validate_json(raw)
    except Exception as exc:
        raise RankingCursorError("invalid ranking cursor") from exc
    if claims.ranking_ref != ranking_ref:
        raise RankingCursorError("ranking cursor belongs to another snapshot")
    if (
        claims.bucket_filter != bucket_filter
        or claims.financial_status_filter != financial_status_filter
        or claims.filter_hash != _filter_hash(bucket_filter, financial_status_filter)
    ):
        raise RankingCursorError("ranking cursor filters changed")
    return claims


def page_ranking_entries(
    entries: Sequence[RankingEntry],
    *,
    ranking_ref: ObjectRef,
    limit: int = 50,
    bucket_filter: Bucket | None = None,
    financial_status_filter: FinancialStatus | None = None,
    cursor: str | None = None,
) -> RankingPage:
    if not 1 <= limit <= 100:
        raise ValueError("ranking page limit must be between 1 and 100")
    if bucket_filter is not None and bucket_filter not in FILTERS["bucket"]:
        raise ValueError("unknown ranking bucket filter")
    if (
        financial_status_filter is not None
        and financial_status_filter not in FILTERS["financial_status"]
    ):
        raise ValueError("unknown financial status filter")
    previous_ordinal = -1
    if cursor is not None:
        previous_ordinal = parse_ranking_cursor(
            cursor,
            ranking_ref=ranking_ref,
            bucket_filter=bucket_filter,
            financial_status_filter=financial_status_filter,
        ).ordinal
    snapshot_filtered = [
        entry
        for entry in entries
        if entry.ranking_ref == ranking_ref
        and (bucket_filter is None or entry.ranking_bucket == bucket_filter)
        and (financial_status_filter is None or entry.financial_status == financial_status_filter)
    ]
    snapshot_filtered.sort(key=lambda entry: entry.ordinal)
    after_cursor = [entry for entry in snapshot_filtered if entry.ordinal > previous_ordinal]
    page_entries = tuple(after_cursor[:limit])
    next_cursor = None
    if len(after_cursor) > limit and page_entries:
        next_cursor = make_ranking_cursor(
            ranking_ref=ranking_ref,
            ordinal=page_entries[-1].ordinal,
            bucket_filter=bucket_filter,
            financial_status_filter=financial_status_filter,
        )
    return RankingPage(entries=page_entries, count=len(snapshot_filtered), next_cursor=next_cursor)


__all__ = [
    "RankingCursorError",
    "RankingOrderError",
    "RankingPage",
    "build_ranking_entries",
    "make_ranking_cursor",
    "order_ranking_entries",
    "page_ranking_entries",
    "parse_ranking_cursor",
]
