"""Stable whole-snapshot ordering and cursor pagination."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_agent.candidates.ranking import (
    RankingCursorError,
    build_ranking_entries,
    make_ranking_cursor,
    order_ranking_entries,
    page_ranking_entries,
    parse_ranking_cursor,
)
from investment_agent.candidates.ranking_contracts import (
    CandidatePolicySnapshot,
    OpenCandidateSource,
    RankingEntry,
    ScreenCandidateSource,
    SourceExclusion,
    SourceManifest,
    SourceMember,
    SourceRunSnapshot,
    WatchlistCandidateSource,
    build_source_manifest,
)
from investment_agent.investor.common import ObjectRef

AS_OF = datetime(2026, 10, 16, 4, 0, tzinfo=UTC)
HASH = "a" * 64


def _run(run_id: int, template_id: str, version: str = "1") -> SourceRunSnapshot:
    return SourceRunSnapshot(
        run_ref=ObjectRef(kind="pipeline_run", id=run_id, revision=1),
        status="success",
        finished_at=AS_OF,
        run_hash=HASH,
        as_of=AS_OF,
        template_id=template_id,
        template_version=version,
        row_count=1,
    )


def _screen_source(
    *, listing_id: int, row_id: int, run: SourceRunSnapshot, rank: int, outcome: str = "hit"
) -> ScreenCandidateSource:
    listing_ref = ObjectRef(kind="listing", id=listing_id, revision=1)
    row_snapshot = {"listing_id": listing_id, "row_id": row_id}
    row_bytes = json.dumps(
        row_snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return ScreenCandidateSource(
        source_kind="screen_hit" if outcome == "hit" else "screen_truncated",
        source_ref=ObjectRef(
            kind="pipeline_run",
            id=run.run_ref.id,
            revision=1,
            field_path=f"screen_results.{row_id}",
        ),
        listing_ref=listing_ref,
        run_ref=run.run_ref,
        source_row_id=row_id,
        run_status="success",
        run_finished_at=AS_OF,
        run_hash=HASH,
        source_snapshot_hash=sha256(row_bytes).hexdigest(),
        row_snapshot=row_snapshot,
        outcome=outcome,
        source_as_of=AS_OF,
        template_id=run.template_id,
        template_version=run.template_version,
        rank=rank,
        strength="0.999",
        reasons=({"factor": "roe", "actual": 0.31},),
        context=(),
    )


def _manifest(
    *, include_manual: bool = True, template_order: tuple[str, str] = ("a_template", "b_template")
) -> SourceManifest:
    run_a = _run(201, "a_template")
    run_b = _run(202, "b_template")
    members = [
        SourceMember(
            listing_ref=ObjectRef(kind="listing", id=101, revision=1),
            sources=(_screen_source(listing_id=101, row_id=301, run=run_a, rank=1),),
        ),
        SourceMember(
            listing_ref=ObjectRef(kind="listing", id=102, revision=1),
            sources=(
                _screen_source(listing_id=102, row_id=302, run=run_a, rank=2),
                _screen_source(listing_id=102, row_id=303, run=run_b, rank=1),
            ),
        ),
        SourceMember(
            listing_ref=ObjectRef(kind="listing", id=103, revision=1),
            sources=(
                _screen_source(listing_id=103, row_id=304, run=run_b, rank=2, outcome="truncated"),
            ),
        ),
    ]
    if include_manual:
        listing_ref = ObjectRef(kind="listing", id=104, revision=1)
        open_source = OpenCandidateSource(
            source_kind="open_candidate",
            source_ref=ObjectRef(kind="candidate", id=404, revision=1),
            listing_ref=listing_ref,
            candidate_id=404,
            status="open",
            opened_at=AS_OF,
            expires_at=AS_OF,
            closed_at=None,
            source_snapshot_hash=HASH,
            candidate_sources=({"kind": "manual"},),
            manual_nomination=True,
        )
        members.append(SourceMember(listing_ref=listing_ref, sources=(open_source,)))
    runs_by_template = {"a_template": run_a, "b_template": run_b}
    source_runs = tuple(runs_by_template[template_id] for template_id in template_order)
    return build_source_manifest(
        {
            "source_run_refs": tuple(run.run_ref for run in source_runs),
            "source_runs": source_runs,
            "candidate_policy": CandidatePolicySnapshot(expiry_days=14, dismiss_cooldown_days=30),
            "as_of": AS_OF,
            "members": tuple(members),
            "exclusions": (),
            "source_issues": (),
            "coverage": "complete",
            "total_unique_count": len(members),
            "processed_count": len(members),
            "unprocessed_count": 0,
            "source_hashes": tuple(
                sorted(source.source_snapshot_hash for member in members for source in member.sources)
            ),
        }
    )


def _entry(
    ranking_ref: ObjectRef,
    member: SourceMember,
    *,
    status: str = "pass",
) -> RankingEntry:
    blocked = status == "blocked"
    return RankingEntry(
        ranking_ref=ranking_ref,
        listing_ref=member.listing_ref,
        ordinal=0,
        ranking_bucket="blocked" if blocked else "incomplete",
        financial_status=status,
        resolved_action="open",
        source_refs=tuple(source.source_ref for source in member.sources),
    )


def test_fixed_template_round_robin_preserves_original_rank_and_deduplicates() -> None:
    manifest = _manifest()
    ranking_ref = ObjectRef(kind="candidate_ranking", id=uuid4(), revision=1)
    entries = [
        _entry(ranking_ref, member, status="blocked" if member.listing_ref.id == 103 else "pass")
        for member in manifest.members
    ]

    ordered = order_ranking_entries(manifest, entries)

    assert [entry.listing_ref.id for entry in ordered] == [101, 102, 104, 103]
    assert [entry.ordinal for entry in ordered] == [0, 1, 2, 3]
    # Listing 102 matched both templates, yet appears once.  Its source list
    # still carries both original rows and their independent ranks.
    assert len(ordered[1].source_refs) == 2


def test_template_queue_order_follows_the_frozen_policy_order() -> None:
    manifest = _manifest(template_order=("b_template", "a_template"), include_manual=False)
    ranking_ref = ObjectRef(kind="candidate_ranking", id=uuid4(), revision=1)
    entries = [_entry(ranking_ref, member) for member in manifest.members]

    ordered = order_ranking_entries(manifest, entries)

    assert [entry.listing_ref.id for entry in ordered] == [102, 101, 103]


def test_round_robin_skips_a_duplicate_within_the_same_template_turn() -> None:
    run_a, run_b = _run(201, "a_template"), _run(202, "b_template")
    queues = ((run_a, (101, 102, 103)), (run_b, (101, 104, 105)))
    sources = {}
    for run, listing_ids in queues:
        for rank, listing_id in enumerate(listing_ids, 1):
            sources.setdefault(listing_id, []).append(_screen_source(
                listing_id=listing_id, row_id=int(run.run_ref.id) * 10 + rank,
                run=run, rank=rank,
            ))
    members = tuple(SourceMember(
        listing_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
        sources=tuple(rows),
    ) for listing_id, rows in sources.items())
    manifest = build_source_manifest({
        "source_run_refs": (run_a.run_ref, run_b.run_ref),
        "source_runs": (run_a, run_b),
        "candidate_policy": CandidatePolicySnapshot(expiry_days=14, dismiss_cooldown_days=30),
        "as_of": AS_OF, "members": members, "exclusions": (), "source_issues": (),
        "coverage": "complete", "total_unique_count": 5,
        "processed_count": 5, "unprocessed_count": 0,
        "source_hashes": tuple(sorted(s.source_snapshot_hash for m in members for s in m.sources)),
    })
    reference = ObjectRef(kind="candidate_ranking", id=uuid4(), revision=1)
    entries = order_ranking_entries(manifest, [_entry(reference, member) for member in members])
    # B's shared first hit must not cost it a turn; its next unseen hit is 104.
    assert [entry.listing_ref.id for entry in entries] == [101, 104, 102, 105, 103]
    assert [entry.ordinal for entry in entries] == list(range(5))
    assert len(entries[0].source_refs) == 2


def test_partial_holding_unknown_is_not_fabricated_as_open_or_failed() -> None:
    manifest = _manifest(include_manual=False)
    ranking_ref = ObjectRef(kind="candidate_ranking", id=uuid4(), revision=1)
    entries = build_ranking_entries(
        manifest,
        ranking_ref=ranking_ref,
        resolved_actions={101: None, 102: "add", 103: "open"},
        assessments={},
        action_unknown_reasons={101: "holding_presence_unknown"},
        failed_items={102: "evaluation_failed"},
    )

    by_listing = {int(entry.listing_ref.id): entry for entry in entries}
    assert by_listing[101].resolved_action is None
    assert by_listing[101].financial_status == "unknown"
    assert by_listing[101].fit_ref is None
    assert by_listing[101].execution_error is None
    assert by_listing[101].action_unknown_reason == "holding_presence_unknown"
    assert by_listing[102].resolved_action == "add"
    assert by_listing[102].execution_error == "evaluation_failed"
    assert by_listing[102].financial_status == "unknown"


def test_entry_cannot_hide_a_financial_block_or_claim_pass_with_unknown_action() -> None:
    manifest = _manifest(include_manual=False)
    reference = ObjectRef(kind="candidate_ranking", id=uuid4(), revision=1)
    blocked = _entry(reference, manifest.members[0], status="blocked")
    with pytest.raises(ValidationError, match="blocked financial result"):
        RankingEntry.model_validate({**blocked.model_dump(), "ranking_bucket": "incomplete"})
    ordinary = _entry(reference, manifest.members[0])
    with pytest.raises(ValidationError, match="unknown holding presence"):
        RankingEntry.model_validate({
            **ordinary.model_dump(), "resolved_action": None,
            "action_unknown_reason": "holding_presence_unknown",
        })


def test_cursor_pagination_uses_snapshot_ordinals_and_binds_filters() -> None:
    manifest = _manifest()
    ranking_ref = ObjectRef(kind="candidate_ranking", id=uuid4(), revision=1)
    entries = [
        _entry(
            ranking_ref,
            member,
            status="blocked" if member.listing_ref.id == 103 else "pass",
        )
        for member in manifest.members
    ]
    ordered = order_ranking_entries(manifest, entries)

    first = page_ranking_entries(ordered, ranking_ref=ranking_ref, limit=2)
    assert [entry.listing_ref.id for entry in first.entries] == [101, 102]
    assert first.count == 4 and first.next_cursor
    second = page_ranking_entries(
        ordered, ranking_ref=ranking_ref, limit=2, cursor=first.next_cursor
    )
    assert [entry.listing_ref.id for entry in second.entries] == [104, 103]
    assert second.next_cursor is None

    filtered = page_ranking_entries(
        ordered,
        ranking_ref=ranking_ref,
        limit=1,
        bucket_filter="incomplete",
        financial_status_filter="pass",
    )
    assert filtered.count == 3
    assert filtered.next_cursor
    with pytest.raises(RankingCursorError, match="filters changed"):
        page_ranking_entries(
            ordered,
            ranking_ref=ranking_ref,
            limit=1,
            bucket_filter="incomplete",
            financial_status_filter="unknown",
            cursor=filtered.next_cursor,
        )
    with pytest.raises(RankingCursorError, match="another snapshot"):
        parse_ranking_cursor(
            first.next_cursor,
            ranking_ref=ObjectRef(kind="candidate_ranking", id=uuid4(), revision=1),
        )


def test_empty_snapshot_has_a_valid_empty_page_and_cursor_binds_exact_reference() -> None:
    manifest = _manifest(include_manual=False)
    ranking_ref = ObjectRef(kind="candidate_ranking", id=uuid4(), revision=1)
    empty = page_ranking_entries((), ranking_ref=ranking_ref, limit=100)
    assert empty.entries == () and empty.count == 0 and empty.next_cursor is None
    cursor = make_ranking_cursor(ranking_ref=ranking_ref, ordinal=0)
    claims = parse_ranking_cursor(cursor, ranking_ref=ranking_ref)
    assert claims.ordinal == 0
    assert manifest.processed_count == 3


def test_maximum_legitimate_source_multiplicity_is_representable() -> None:
    listing_ref = ObjectRef(kind="listing", id=101, revision=1)
    screens = tuple(
        _screen_source(
            listing_id=101,
            row_id=1000 + index,
            run=_run(1000 + index, f"template_{index}"),
            rank=1,
        )
        for index in range(100)
    )
    opened = OpenCandidateSource(
        source_kind="open_candidate",
        source_ref=ObjectRef(kind="candidate", id=904, revision=1),
        listing_ref=listing_ref,
        candidate_id=904,
        status="open",
        opened_at=AS_OF - timedelta(days=1),
        expires_at=AS_OF + timedelta(days=10),
        closed_at=None,
        source_snapshot_hash=HASH,
        candidate_sources=({"kind": "manual"},),
        manual_nomination=True,
    )
    member = SourceMember(listing_ref=listing_ref, sources=(*screens, opened))
    entry = RankingEntry(
        ranking_ref=ObjectRef(kind="candidate_ranking", id=uuid4(), revision=1),
        listing_ref=listing_ref,
        ordinal=0,
        ranking_bucket="incomplete",
        financial_status="unknown",
        resolved_action=None,
        action_unknown_reason="holding_presence_unknown",
        source_refs=tuple(source.source_ref for source in member.sources),
    )
    watch = WatchlistCandidateSource(
        source_kind="watchlist",
        source_ref=ObjectRef(kind="watchlist_item", id=55, revision=1),
        listing_ref=listing_ref,
        item_id=55,
        status="active",
        changed_at=AS_OF,
        source_snapshot_hash=HASH,
        provider="synthetic",
        last_refresh_status="never",
    )
    exclusion = SourceExclusion(
        kind="business",
        listing_ref=listing_ref,
        reason_code="active_watchlist",
        source_snapshots=(*screens, opened, watch),
    )
    runs = tuple(_run(1000 + index, f"template_{index}") for index in range(100))
    member_manifest = build_source_manifest(
        {
            "source_run_refs": tuple(run.run_ref for run in runs),
            "source_runs": runs,
            "candidate_policy": CandidatePolicySnapshot(expiry_days=14, dismiss_cooldown_days=30),
            "as_of": AS_OF,
            "members": (member,),
            "exclusions": (),
            "source_issues": (),
            "coverage": "complete",
            "total_unique_count": 1,
            "processed_count": 1,
            "unprocessed_count": 0,
            "source_hashes": tuple(sorted(source.source_snapshot_hash for source in member.sources)),
        }
    )
    exclusion_manifest = build_source_manifest(
        {
            "source_run_refs": tuple(run.run_ref for run in runs),
            "source_runs": runs,
            "candidate_policy": CandidatePolicySnapshot(expiry_days=14, dismiss_cooldown_days=30),
            "as_of": AS_OF,
            "members": (),
            "exclusions": (exclusion,),
            "source_issues": (),
            "coverage": "complete",
            "total_unique_count": 0,
            "processed_count": 0,
            "unprocessed_count": 0,
            "source_hashes": tuple(
                sorted(source.source_snapshot_hash for source in exclusion.source_snapshots)
            ),
        }
    )

    assert len(member.sources) == 101
    assert len(entry.source_refs) == 101
    assert len(exclusion.source_snapshots) == 102
    assert len(member_manifest.members[0].sources) == 101
    assert len(exclusion_manifest.exclusions[0].source_snapshots) == 102


def test_screen_row_snapshot_hash_is_verified_when_raw_row_is_present() -> None:
    source = _screen_source(listing_id=101, row_id=301, run=_run(201, "template"), rank=1)
    with pytest.raises(ValidationError, match="screen row snapshot hash mismatch"):
        ScreenCandidateSource.model_validate(
            {**source.model_dump(mode="python"), "row_snapshot": {"id": 301, "ticker": "600519.SH"}}
        )
