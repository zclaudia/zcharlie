"""Read-only source freezing for account-specific candidate rankings."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from investment_agent.candidates.lifecycle import CandidatePolicy
from investment_agent.candidates.ranking_contracts import (
    OpenCandidateSource,
    RankingSourcePolicy,
    ScreenCandidateSource,
)
from investment_agent.candidates.ranking_sources import (
    SourceManifestCapacityError,
    freeze_candidate_sources,
    resolve_source_runs,
)
from investment_agent.clock import SHANGHAI_TZ
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.candidates import CandidateRecord
from investment_agent.storage.models.rules import ScreenResultRecord
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from tests.unit._candidate_support import (
    AS_OF,
    cand_db,  # noqa: F401 - fixture
    seed_listing,
    seed_result,
    seed_template,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
POLICY = CandidatePolicy(expiry_days=14, dismiss_cooldown_days=30)

pytestmark = pytest.mark.usefixtures("cand_db")


async def _complete_run(
    *,
    as_of: date,
    finished_at: datetime,
    rows: list[tuple[int, str, int, str, int | None]],
    template_ids: tuple[str, ...] | None = None,
) -> int:
    """Create a screen_run with the same summary shape as ScreenRunHandler."""

    templates = sorted({row[1] for row in rows} | set(template_ids or ()))
    async with session_scope() as session:
        for template_id in templates:
            await seed_template(session, id=template_id, name=f"{template_id} template")
        run = await PipelineRunRepository(session).create(
            kind="screen_run",
            triggered_by="test",
            status="success",
            stage="done",
            request_json={"universe_code": "TEST", "as_of": as_of.isoformat()},
        )
        for listing_id, template_id, version, outcome, rank in rows:
            listing = await session.get(
                __import__(
                    "investment_agent.storage.models.reference", fromlist=["Listing"]
                ).Listing,
                listing_id,
            )
            assert listing is not None
            await seed_result(
                session,
                run_id=run.id,
                listing=listing,
                ticker=listing.ticker,
                template_id=template_id,
                template_version=version,
                outcome=outcome,
                rank=rank,
                as_of=as_of,
            )
        outcome_counts: dict[str, Counter[str]] = {
            template_id: Counter() for template_id in templates
        }
        versions: dict[str, int] = {}
        for _listing_id, template_id, version, outcome, _rank in rows:
            outcome_counts[template_id][outcome] += 1
            versions[template_id] = version
        # Empty batches still declare the exact template version and zero counts.
        for template_id in templates:
            versions.setdefault(template_id, 1)
        summary = {
            f"{template_id}@{versions[template_id]}": {
                "counts": {
                    key: outcome_counts[template_id].get(key, 0)
                    for key in ("hit", "truncated", "miss", "skipped", "excluded")
                }
            }
            for template_id in templates
        }
        run.finished_at = finished_at
        run.metadata_json = {
            "as_of": as_of.isoformat(),
            "templates": summary,
            "result_rows": len(rows),
            "load_errors": {},
        }
        await session.flush()
        return run.id


async def _source_manifest(
    *,
    as_of: datetime,
    template_ids: tuple[str, ...] = ("rank_a",),
    source_cap: int = 2_000,
    candidate_policy: CandidatePolicy = POLICY,
):
    async with session_scope() as session:
        policy = RankingSourcePolicy(template_ids=template_ids)
        runs, issues = await resolve_source_runs(session, as_of=as_of, policy=policy)
        return await freeze_candidate_sources(
            session,
            as_of=as_of,
            source_runs=runs,
            source_issues=issues,
            candidate_policy=candidate_policy,
            source_cap=source_cap,
        )


async def test_freezes_hit_and_truncated_across_declared_templates_without_writes() -> None:
    async with session_scope() as session:
        a = await seed_listing(session, "600519.SH", "贵州茅台")
        b = await seed_listing(session, "601398.SH", "工商银行")
    run_a = await _complete_run(
        as_of=AS_OF,
        finished_at=NOW - timedelta(hours=2),
        rows=[(a.id, "rank_a", 1, "hit", 3), (b.id, "rank_a", 1, "truncated", 21)],
    )
    run_b = await _complete_run(
        as_of=AS_OF,
        finished_at=NOW - timedelta(hours=1),
        rows=[(a.id, "rank_b", 1, "hit", 1)],
    )

    manifest = await _source_manifest(as_of=NOW, template_ids=("rank_a", "rank_b"))

    assert manifest.coverage == "complete"
    assert manifest.source_run_refs[0].id == run_a
    assert manifest.source_run_refs[1].id == run_b
    assert [int(member.listing_ref.id) for member in manifest.members] == [a.id, b.id]
    a_sources = manifest.members[0].sources
    assert len(a_sources) == 2
    assert all(isinstance(source, ScreenCandidateSource) for source in a_sources)
    assert {(source.template_id, source.outcome) for source in a_sources} == {
        ("rank_a", "hit"),
        ("rank_b", "hit"),
    }
    [b_member] = [member for member in manifest.members if member.listing_ref.id == b.id]
    [truncated] = b_member.sources
    assert isinstance(truncated, ScreenCandidateSource)
    assert truncated.outcome == "truncated" and truncated.rank == 21
    assert truncated.reasons[0]["factor"] == "roe"
    assert dict((item.key, item.value) for item in truncated.context)["provider"] == "baostock"
    assert manifest.canonical_bytes > 0
    assert len(manifest.manifest_hash) == 64

    async with session_scope() as session:
        assert await session.scalar(select(func_count(CandidateRecord.id))) == 0
        assert (
            await session.scalar(
                select(func_count(PipelineRun.id)).where(PipelineRun.kind == "rule_draft")
            )
            == 0
        )


async def test_latest_complete_batch_skips_incomplete_run_and_missing_batch_is_visible() -> None:
    async with session_scope() as session:
        listing = await seed_listing(session)
    good = await _complete_run(
        as_of=AS_OF,
        finished_at=NOW - timedelta(days=2),
        rows=[(listing.id, "rank_a", 1, "hit", 1)],
    )
    bad = await _complete_run(
        as_of=AS_OF,
        finished_at=NOW - timedelta(days=1),
        rows=[(listing.id, "rank_a", 1, "hit", 1)],
    )
    async with session_scope() as session:
        run = await session.get(PipelineRun, bad)
        assert run is not None
        run.metadata_json = {"as_of": AS_OF.isoformat(), "templates": {}, "result_rows": 1}

    manifest = await _source_manifest(as_of=NOW, template_ids=("rank_a", "missing"))
    assert any(run.run_ref.id == good for run in manifest.source_runs)
    assert manifest.coverage == "partial"
    issue = next(issue for issue in manifest.source_issues if issue.template_id == "missing")
    assert issue.kind == "missing_run"
    assert len(manifest.members) == 1


async def test_active_open_survives_cooldown_but_watchlist_excludes_it() -> None:
    async with session_scope() as session:
        listing = await seed_listing(session)
        session.add(
            CandidateRecord(
                listing_id=listing.id,
                ticker=listing.ticker,
                status="dismissed",
                opened_at=NOW - timedelta(days=40),
                expires_at=NOW - timedelta(days=26),
                closed_at=NOW - timedelta(days=26),
                sources=[{"kind": "manual", "as_of": AS_OF.isoformat()}],
            )
        )
        session.add(
            CandidateRecord(
                listing_id=listing.id,
                ticker=listing.ticker,
                status="open",
                opened_at=NOW - timedelta(days=2),
                expires_at=NOW + timedelta(days=12),
                sources=[{"kind": "manual", "as_of": AS_OF.isoformat()}],
            )
        )
        session.add(
            WatchlistItem(
                listing_id=listing.id,
                added_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(days=1),
                provider="test",
                enabled=True,
                last_refresh_status="never",
            )
        )
        await session.flush()
    await _complete_run(
        as_of=AS_OF,
        finished_at=NOW - timedelta(hours=1),
        rows=[(listing.id, "rank_a", 1, "hit", 2)],
    )

    manifest = await _source_manifest(as_of=NOW)
    assert not manifest.members
    assert {item.reason_code for item in manifest.exclusions} == {"active_watchlist"}
    snapshots = manifest.exclusions[0].source_snapshots
    assert any(
        isinstance(source, OpenCandidateSource) and source.manual_nomination for source in snapshots
    )
    assert any(source.source_kind == "watchlist" for source in snapshots)
    assert any(isinstance(source, ScreenCandidateSource) for source in snapshots)


async def test_dismiss_cooldown_excludes_old_hits_and_late_batch_reenters_as_new_lead() -> None:
    async with session_scope() as session:
        listing = await seed_listing(session)
        session.add(
            CandidateRecord(
                listing_id=listing.id,
                ticker=listing.ticker,
                status="dismissed",
                opened_at=NOW - timedelta(days=20),
                expires_at=NOW - timedelta(days=6),
                closed_at=NOW - timedelta(days=6),
                sources=[{"kind": "screen", "screen_run_id": 20}],
            )
        )
        await session.flush()
    await _complete_run(
        as_of=AS_OF,
        finished_at=NOW - timedelta(days=2),
        rows=[(listing.id, "rank_a", 1, "hit", 1)],
    )
    blocked = await _source_manifest(as_of=NOW)
    assert not blocked.members
    assert blocked.exclusions[0].reason_code == "dismiss_cooldown"
    assert any(
        item.source_kind == "candidate_lifecycle" for item in blocked.exclusions[0].source_snapshots
    )

    await _complete_run(
        as_of=AS_OF,
        finished_at=NOW + timedelta(days=25),
        rows=[(listing.id, "rank_a", 1, "truncated", 30)],
    )
    reentered = await _source_manifest(as_of=NOW + timedelta(days=26))
    assert len(reentered.members) == 1
    [source] = reentered.members[0].sources
    assert isinstance(source, ScreenCandidateSource) and source.outcome == "truncated"


async def test_dismiss_cooldown_boundary_matches_screen_intake_inclusive_cutoff() -> None:
    async with session_scope() as session:
        listing = await seed_listing(session)
        dismissed_at = NOW - timedelta(days=30)
        session.add(
            CandidateRecord(
                listing_id=listing.id,
                ticker=listing.ticker,
                status="dismissed",
                opened_at=dismissed_at - timedelta(days=14),
                expires_at=dismissed_at,
                closed_at=dismissed_at,
                sources=[{"kind": "manual"}],
            )
        )
        await session.flush()
    # The batch arrived after dismissal, so it is a fresh source, but exactly
    # at the cooldown boundary screen_intake still treats the listing as cooled.
    await _complete_run(
        as_of=AS_OF,
        finished_at=dismissed_at + timedelta(seconds=1),
        rows=[(listing.id, "rank_a", 1, "hit", 1)],
    )

    manifest = await _source_manifest(as_of=NOW)

    assert not manifest.members
    assert any(item.reason_code == "dismiss_cooldown" for item in manifest.exclusions)


async def test_zero_dismiss_cooldown_does_not_suppress_at_dismissal_time() -> None:
    async with session_scope() as session:
        listing = await seed_listing(session)
        session.add(
            CandidateRecord(
                listing_id=listing.id,
                ticker=listing.ticker,
                status="dismissed",
                opened_at=NOW - timedelta(days=1),
                expires_at=NOW,
                closed_at=NOW,
                sources=[{"kind": "manual"}],
            )
        )
        await session.flush()
    await _complete_run(
        as_of=AS_OF,
        finished_at=NOW + timedelta(seconds=1),
        rows=[(listing.id, "rank_a", 1, "hit", 1)],
    )

    manifest = await _source_manifest(
        as_of=NOW + timedelta(minutes=1),
        candidate_policy=CandidatePolicy(expiry_days=14, dismiss_cooldown_days=0),
    )

    assert [int(member.listing_ref.id) for member in manifest.members] == [listing.id]


async def test_source_cap_is_partial_and_does_not_confuse_business_exclusions() -> None:
    async with session_scope() as session:
        a = await seed_listing(session, "600519.SH", "贵州茅台")
        b = await seed_listing(session, "601398.SH", "工商银行")
        c = await seed_listing(session, "000001.SZ", "平安银行")
    await _complete_run(
        as_of=AS_OF,
        finished_at=NOW - timedelta(minutes=1),
        rows=[
            (a.id, "rank_a", 1, "hit", 3),
            (b.id, "rank_a", 1, "truncated", 21),
            (c.id, "rank_a", 1, "hit", 1),
        ],
    )
    manifest = await _source_manifest(as_of=NOW, source_cap=2)
    assert manifest.coverage == "partial"
    assert manifest.total_unique_count == 3
    assert manifest.processed_count == 2 and manifest.unprocessed_count == 1
    assert manifest.exclusions[-1].kind == "source_cap"
    assert manifest.exclusions[-1].unprocessed_range_start == 2
    assert all(exclusion.kind != "business" for exclusion in manifest.exclusions)


async def test_member_and_business_exclusion_hashes_share_one_sorted_manifest_vector() -> None:
    async with session_scope() as session:
        excluded_listing = await seed_listing(session, "600519.SH", "贵州茅台")
        included_listing = await seed_listing(session, "601398.SH", "工商银行")
        session.add(
            CandidateRecord(
                listing_id=excluded_listing.id,
                ticker=excluded_listing.ticker,
                status="dismissed",
                opened_at=NOW - timedelta(days=20),
                expires_at=NOW - timedelta(days=6),
                closed_at=NOW - timedelta(days=6),
                sources=[{"kind": "manual"}],
            )
        )
        await session.flush()
    await _complete_run(
        as_of=AS_OF,
        finished_at=NOW - timedelta(days=2),
        rows=[
            (excluded_listing.id, "rank_a", 1, "hit", 1),
            (included_listing.id, "rank_a", 1, "hit", 2),
        ],
    )

    manifest = await _source_manifest(as_of=NOW)

    assert [member.listing_ref.id for member in manifest.members] == [included_listing.id]
    assert any(item.kind == "business" for item in manifest.exclusions)
    expected = sorted(
        [source.source_snapshot_hash for member in manifest.members for source in member.sources]
        + [
            source.source_snapshot_hash
            for exclusion in manifest.exclusions
            for source in exclusion.source_snapshots
        ]
    )
    assert list(manifest.source_hashes) == expected


async def test_manifest_byte_cap_marks_remaining_candidates_partial() -> None:
    async with session_scope() as session:
        listing = await seed_listing(session)
    run_id = await _complete_run(
        as_of=AS_OF,
        finished_at=NOW - timedelta(minutes=1),
        rows=[(listing.id, "rank_a", 1, "hit", 1)],
    )
    async with session_scope() as session:
        row = await session.scalar(
            select(ScreenResultRecord).where(ScreenResultRecord.screen_run_id == run_id)
        )
        assert row is not None
        row.context = {"large_context": "x" * (4 * 1024 * 1024 + 1)}

    manifest = await _source_manifest(as_of=NOW)

    assert manifest.coverage == "partial"
    assert manifest.processed_count == 0
    assert manifest.total_unique_count == 1
    assert manifest.unprocessed_count == 1
    assert manifest.exclusions[-1].kind == "source_cap"
    assert manifest.canonical_bytes <= 4 * 1024 * 1024


async def test_non_capacity_manifest_validation_error_is_not_treated_as_byte_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from investment_agent.candidates import ranking_sources
    from investment_agent.candidates.ranking_contracts import SourceMember
    from investment_agent.investor.common import ObjectRef

    async with session_scope() as session:
        listing = await seed_listing(session)
    await _complete_run(
        as_of=AS_OF,
        finished_at=NOW - timedelta(minutes=1),
        rows=[(listing.id, "rank_a", 1, "hit", 1)],
    )
    with pytest.raises(ValidationError) as invalid_member:
        SourceMember.model_validate(
            {"listing_ref": ObjectRef(kind="listing", id=listing.id, revision=1), "sources": []}
        )

    def fail_for_an_unrelated_dto_error(_payload):
        raise invalid_member.value

    monkeypatch.setattr(ranking_sources, "build_source_manifest", fail_for_an_unrelated_dto_error)
    async with session_scope() as session:
        runs, issues = await resolve_source_runs(
            session,
            as_of=NOW,
            policy=RankingSourcePolicy(template_ids=("rank_a",)),
        )
        with pytest.raises(ValidationError) as raised:
            await freeze_candidate_sources(
                session,
                as_of=NOW,
                source_runs=runs,
                source_issues=issues,
                candidate_policy=POLICY,
            )
    assert raised.value is invalid_member.value


async def test_local_current_day_run_is_included_at_shanghai_0030_without_lookahead() -> None:
    async with session_scope() as session:
        listing = await seed_listing(session)
    source_date = date(2026, 10, 16)
    cutoff = datetime(2026, 10, 16, 0, 30, tzinfo=SHANGHAI_TZ)
    earlier_run = await _complete_run(
        as_of=source_date,
        finished_at=(cutoff - timedelta(minutes=15)).astimezone(UTC),
        rows=[(listing.id, "rank_a", 1, "hit", 1)],
    )
    later_run = await _complete_run(
        as_of=source_date,
        finished_at=(cutoff + timedelta(minutes=15)).astimezone(UTC),
        rows=[(listing.id, "rank_a", 1, "hit", 1)],
    )

    manifest = await _source_manifest(as_of=cutoff)

    assert manifest.source_run_refs[0].id == earlier_run
    assert manifest.source_run_refs[0].id != later_run
    assert manifest.source_runs[0].finished_at <= cutoff
    assert manifest.source_runs[0].as_of == datetime(2026, 10, 15, 16, 0, tzinfo=UTC)
    source = manifest.members[0].sources[0]
    assert source.source_as_of == datetime(2026, 10, 15, 16, 0, tzinfo=UTC)


async def test_oversized_business_exclusion_raises_typed_capacity_error() -> None:
    async with session_scope() as session:
        listing = await seed_listing(session)
        session.add(
            CandidateRecord(
                listing_id=listing.id,
                ticker=listing.ticker,
                status="open",
                opened_at=NOW - timedelta(days=1),
                expires_at=NOW + timedelta(days=10),
                sources=[{"kind": "manual", "large": "x" * (4 * 1024 * 1024)}],
            )
        )
        session.add(
            WatchlistItem(
                listing_id=listing.id,
                added_at=NOW - timedelta(days=1),
                updated_at=NOW - timedelta(days=1),
                provider="test",
                enabled=True,
                last_refresh_status="never",
            )
        )
        await session.flush()
    await _complete_run(
        as_of=AS_OF,
        finished_at=NOW - timedelta(minutes=1),
        rows=[(listing.id, "rank_a", 1, "hit", 1)],
    )

    with pytest.raises(SourceManifestCapacityError) as capacity:
        await _source_manifest(as_of=NOW)

    assert capacity.value.detail.code == "source_capacity_exceeded"


async def test_selected_source_run_change_is_a_revision_conflict() -> None:
    async with session_scope() as session:
        listing = await seed_listing(session)
    run_id = await _complete_run(
        as_of=AS_OF,
        finished_at=NOW - timedelta(minutes=1),
        rows=[(listing.id, "rank_a", 1, "hit", 1)],
    )
    async with session_scope() as session:
        runs, issues = await resolve_source_runs(
            session,
            as_of=NOW,
            policy=RankingSourcePolicy(template_ids=("rank_a",)),
        )
        run = await session.get(PipelineRun, run_id)
        assert run is not None
        run.metadata_json = {**(run.metadata_json or {}), "changed_after_selection": True}
        await session.flush()
        from investment_agent.investor.foundations import InvestorError

        with pytest.raises(InvestorError) as conflict:
            await freeze_candidate_sources(
                session,
                as_of=NOW,
                source_runs=runs,
                source_issues=issues,
                candidate_policy=POLICY,
            )
    assert conflict.value.detail.code == "revision_conflict"


def func_count(column):
    """Keep SQLAlchemy's count helper local so the tests seed no app queries."""

    from sqlalchemy import func

    return func.count(column)
