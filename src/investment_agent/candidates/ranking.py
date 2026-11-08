"""Deterministic ordering and cursor pagination for frozen ranking snapshots."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from investment_agent.candidates.portfolio_context import (
    CandidatePortfolioContext,
    ContextState,
    context_order,
)
from investment_agent.candidates.ranking_contracts import (
    OpenCandidateSource,
    RankingCursorClaims,
    RankingEntry,
    ScreenCandidateSource,
    SourceManifest,
)
from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.suitability_contracts import AccountFitAssessment, FitCheck

Bucket = Literal["incomplete", "blocked", "ready", "limited"]
FinancialStatus = Literal["pass", "warning", "blocked", "unknown"]
ResearchUrgency = Literal["critical_pending", "coverage_pending", "review_due"]
# ready/limited are new buckets; existing snapshots only ever hold the two
# legacy values, so sweeping them first never reorders earlier snapshots.
BUCKET_ORDER: tuple[Bucket, ...] = ("ready", "limited", "incomplete", "blocked")
CONTEXT_STATES = ("eligible", "unverified", "not_assessed", "constrained")
FILTERS: dict[str, set[str]] = {
    "bucket": set(BUCKET_ORDER),
    "financial_status": {"pass", "warning", "blocked", "unknown"},
    "portfolio_context": set(CONTEXT_STATES),
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
    portfolio_context_filter: ContextState | None,
) -> str:
    payload: dict[str, object] = {
        "bucket": bucket_filter,
        "financial_status": financial_status_filter,
    }
    # Keep the hash of the pre-existing filters byte-identical so cursors
    # issued before the optional portfolio filter still validate.
    if portfolio_context_filter is not None:
        payload["portfolio_context"] = portfolio_context_filter
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _manual_source(member: object) -> tuple[bool, bool, object | None]:
    sources = getattr(member, "sources", ())
    candidates = [source for source in sources if isinstance(source, OpenCandidateSource)]
    manual = any(source.manual_nomination for source in candidates)
    if not candidates:
        return False, False, None
    opened_at = min(source.opened_at for source in candidates)
    return True, manual, opened_at


def research_urgency_level(
    *, open_critical_issues: bool, coverage_limits: Sequence[str]
) -> ResearchUrgency | None:
    """Bounded display projection of one subject's research urgency.

    Source is the subject's own rule state (AF-P3-07 recheck limits plus its
    critical open issues). It is display/filter information only and never
    enters the ordering key or any score.
    """

    if open_critical_issues:
        return "critical_pending"
    for limit in coverage_limits:
        head = limit.split(":", 1)[0]
        if head in {
            "rule_coverage_pending",
            "rule_coverage_scan_incomplete",
            "rule_coverage_binding_missing",
            "unbound_event",
            "event_publication_boundary",
            "event_effective_started",
            "event_effective_ended",
        }:
            return "coverage_pending"
        if head in {"rule_review_due", "rule_expired", "research_check_due"}:
            return "review_due"
    return None


def build_ranking_entries(
    manifest: SourceManifest,
    *,
    ranking_ref: ObjectRef,
    resolved_actions: Mapping[int, Literal["open", "add", "hold"] | None],
    assessments: Mapping[int, AccountFitAssessment],
    listing_labels: Mapping[int, tuple[str, str]] | None = None,
    action_unknown_reasons: Mapping[int, str] | None = None,
    failed_items: Mapping[int, str] | None = None,
    rule_projections: Mapping[int, tuple[tuple[ObjectRef, ...], ResearchUrgency | None]] | None = None,
    portfolio_contexts: Mapping[int, CandidatePortfolioContext] | None = None,
) -> tuple[RankingEntry, ...]:
    """Create one finite account-ranking entry for each processed member.

    ``resolved_actions`` is supplied by the account snapshot resolver.  A
    ``None`` action represents unknown holding presence and intentionally does
    not create a fit assessment or execution failure. ``rule_projections``
    carries the optional P3 supplemental projection per listing; with the
    switches off it stays empty and rows keep their legacy shape.
    ``portfolio_contexts`` is the optional P1 portfolio annotation; ``None``
    keeps the research-only ordering and payload exactly as before.
    """

    if ranking_ref.kind != "candidate_ranking" or ranking_ref.revision != 1:
        raise ValueError("ranking_ref must identify revision 1 of a candidate ranking")
    reasons = action_unknown_reasons or {}
    failures = failed_items or {}
    labels = listing_labels or {}
    projections = rule_projections or {}
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
        if assessment is not None and assessment.ranking_bucket in {"ready", "limited"}:
            bucket = assessment.ranking_bucket
        projection = projections.get(listing_id, ((), None))
        label = labels.get(listing_id)
        context = portfolio_contexts.get(listing_id) if portfolio_contexts is not None else None
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
                rule_application_refs=projection[0],
                research_urgency=projection[1],
                portfolio_context=context,
            )
        )
    return order_ranking_entries(
        manifest, tuple(entries), portfolio_contexts=portfolio_contexts
    )


def _context_partition_order(contexts: Mapping[int, CandidatePortfolioContext]) -> dict[int, int]:
    return {listing_id: context_order(context.state) for listing_id, context in contexts.items()}


def order_ranking_entries(
    manifest: SourceManifest,
    entries: Sequence[RankingEntry],
    *,
    portfolio_contexts: Mapping[int, CandidatePortfolioContext] | None = None,
) -> tuple[RankingEntry, ...]:
    """Apply the frozen v1 bucket and template round-robin policy.

    Within a selected ``template_id@version`` queue, the original screen rank
    is preserved.  Manual and otherwise-open candidates have explicit stable
    queues after template queues; no strength values are compared across
    templates.

    When an explicit portfolio context is supplied, every queue is partitioned
    by a stable *constraint visibility* key (eligible / unverified /
    not-assessed / constrained).  The partition never uses strength, return or
    any cross-template score; it only moves candidates whose known constraints
    are already exceeded after those without a known block.  Without a context
    the ordering is byte-for-byte the research-only policy.
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
    if portfolio_contexts is not None and set(portfolio_contexts) != set(source_by_listing):
        raise RankingOrderError("portfolio context must cover every frozen source member")
    partition = (
        _context_partition_order(portfolio_contexts) if portfolio_contexts is not None else None
    )

    selected_run_by_template = {
        (run.template_id, run.template_version): _ref_key(run.run_ref)
        for run in manifest.source_runs
    }
    template_keys = list(dict.fromkeys(selected_run_by_template))
    result: list[RankingEntry] = []
    for bucket in BUCKET_ORDER:
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
            ordered = sorted(ranked, key=lambda item: (item[0], item[1]))
            if partition is not None:
                # Stable within the template: original rank, then the
                # constraint-visibility partition.  Never a strength compare.
                ordered = sorted(
                    ordered, key=lambda item: (partition[item[1]], item[0], item[1])
                )
            queues[template] = [listing_id for _, listing_id in ordered]

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

        def _tail_key(item: tuple[object, int]) -> tuple[int, object, int]:
            opened_at, listing_id = item
            rank = partition[listing_id] if partition is not None else 0
            return (rank, opened_at, listing_id)

        for _, listing_id in sorted(open_only, key=_tail_key):
            emitted.add(listing_id)
            result.append(entry_by_listing[listing_id])
        for _, listing_id in sorted(manual_only, key=_tail_key):
            emitted.add(listing_id)
            result.append(entry_by_listing[listing_id])

        # Defensive final queue: only unusual source kinds can reach here.
        final_ids = sorted(bucket_ids - emitted)
        if partition is not None:
            final_ids = sorted(final_ids, key=lambda listing_id: (partition[listing_id], listing_id))
        for listing_id in final_ids:
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
    portfolio_context_filter: ContextState | None = None,
) -> str:
    if not ordinal >= 0:
        raise ValueError("ordinal must be non-negative")
    claims = RankingCursorClaims(
        ranking_ref=ranking_ref,
        ordinal=ordinal,
        bucket_filter=bucket_filter,
        financial_status_filter=financial_status_filter,
        portfolio_context_filter=portfolio_context_filter,
        filter_hash=_filter_hash(bucket_filter, financial_status_filter, portfolio_context_filter),
    )
    raw = _canonical(claims.model_dump(mode="json"))
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def parse_ranking_cursor(
    cursor: str,
    *,
    ranking_ref: ObjectRef,
    bucket_filter: Bucket | None = None,
    financial_status_filter: FinancialStatus | None = None,
    portfolio_context_filter: ContextState | None = None,
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
        or claims.portfolio_context_filter != portfolio_context_filter
        or claims.filter_hash
        != _filter_hash(bucket_filter, financial_status_filter, portfolio_context_filter)
    ):
        raise RankingCursorError("ranking cursor filters changed")
    return claims


def _entry_context_state(entry: RankingEntry) -> ContextState:
    # Rows written before the optional context existed carry no state; treat
    # them as "not assessed" rather than inventing a pass.
    return entry.portfolio_context.state if entry.portfolio_context is not None else "not_assessed"


def page_ranking_entries(
    entries: Sequence[RankingEntry],
    *,
    ranking_ref: ObjectRef,
    limit: int = 50,
    bucket_filter: Bucket | None = None,
    financial_status_filter: FinancialStatus | None = None,
    portfolio_context_filter: ContextState | None = None,
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
    if (
        portfolio_context_filter is not None
        and portfolio_context_filter not in FILTERS["portfolio_context"]
    ):
        raise ValueError("unknown portfolio context filter")
    previous_ordinal = -1
    if cursor is not None:
        previous_ordinal = parse_ranking_cursor(
            cursor,
            ranking_ref=ranking_ref,
            bucket_filter=bucket_filter,
            financial_status_filter=financial_status_filter,
            portfolio_context_filter=portfolio_context_filter,
        ).ordinal
    snapshot_filtered = [
        entry
        for entry in entries
        if entry.ranking_ref == ranking_ref
        and (bucket_filter is None or entry.ranking_bucket == bucket_filter)
        and (financial_status_filter is None or entry.financial_status == financial_status_filter)
        and (
            portfolio_context_filter is None
            or _entry_context_state(entry) == portfolio_context_filter
        )
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
            portfolio_context_filter=portfolio_context_filter,
        )
    return RankingPage(entries=page_entries, count=len(snapshot_filtered), next_cursor=next_cursor)


__all__ = [
    "BUCKET_ORDER",
    "CONTEXT_STATES",
    "RankingCursorError",
    "RankingOrderError",
    "RankingPage",
    "build_ranking_entries",
    "make_ranking_cursor",
    "order_ranking_entries",
    "page_ranking_entries",
    "parse_ranking_cursor",
    "research_urgency_level",
]
