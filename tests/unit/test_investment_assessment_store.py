"""Repository-level account-ranking persistence and integrity checks."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

import pytest
from pydantic import ValidationError

from investment_agent.candidates.ranking_contracts import (
    RANKING_POLICY_VERSION,
    CandidatePolicySnapshot,
    RankingEntry,
    RankingSnapshot,
    ScreenCandidateSource,
    SourceManifest,
    SourceMember,
    SourceRunSnapshot,
    canonical_source_manifest_bytes,
    canonical_universe_hash,
)
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.contracts import Mandate, ResearchAuthorization
from investment_agent.portfolio.contracts import Snapshot
from investment_agent.portfolio.suitability_contracts import (
    AccountFitAssessment,
    FitRequestItem,
    FrozenAccountInputs,
    FrozenListingSource,
    FrozenPrice,
    FrozenSector,
    PurposeBinding,
    TradeAssumptions,
    ValuationBinding,
)
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.storage.repositories.investment_assessment import (
    InvestmentAssessmentRepository,
)

HASH = "a" * 64
AS_OF = datetime(2026, 10, 16, 4, 0, tzinfo=UTC)


def _fit_assessment() -> AccountFitAssessment:
    account_id = uuid4()
    listing_ref = ObjectRef(kind="listing", id=101, revision=1)
    snapshot_id = uuid4()
    snapshot = Snapshot(
        id=snapshot_id,
        account_id=account_id,
        revision=1,
        recorded_at=AS_OF,
        fact_as_of=AS_OF,
        valuation_at=AS_OF,
        coverage="complete",
        asset_scope="synthetic test account",
        known_cash=Decimal("1000"),
        cash_subtotal=Decimal("1000"),
        holdings=(),
        known_subtotal=Decimal("1000"),
        total_assets=Decimal("1000"),
        unknowns=(),
        historical_unknowns=(),
        reconciliations=(),
        conflicts=(),
        source_event_ids=(),
        excluded_prior_event_ids=(),
        reference=ObjectRef(kind="portfolio_snapshot", id=snapshot_id, revision=1),
        price_coverage="complete",
    )
    mandate = Mandate(
        id=uuid4(),
        schema_version=1,
        revision=1,
        created_at=AS_OF,
        actor="test",
        origin_ref=None,
        goals=(),
        scope_refs=(),
        hard_constraints=(),
        unknowns=(),
        authorization=ResearchAuthorization(
            daily_budget=Quantity(value=Decimal("1"), unit="token"),
            per_task_budget=Quantity(value=Decimal("1"), unit="token"),
            max_leads_per_batch=0,
            max_new_tasks_per_batch=0,
            source_refs=(),
        ),
    )
    frozen_listing = FrozenListingSource(
        listing_ref=listing_ref,
        market_code="SSE",
        name="Synthetic listing",
        ticker="600000",
        listing_source_ref=listing_ref,
        price=FrozenPrice(
            listing_ref=listing_ref,
            price=None,
            price_ref=None,
            price_date=None,
            provider=None,
            captured_at=None,
            available_at=None,
            acquired_at=None,
            unknown_reason="no_price",
        ),
        sector=FrozenSector(
            listing_ref=listing_ref,
            row_id=None,
            code=None,
            name=None,
            valid_from=None,
            valid_to=None,
            source=None,
            captured_at=None,
            available_at=None,
            acquired_at=None,
            unknown_reason="no_sector",
        ),
        trusted_rule=None,
        rule_unknown_reason="no_trusted_rule",
    )
    purpose_binding = PurposeBinding(state="none_recorded")
    frozen_inputs = FrozenAccountInputs(
        account_id=account_id,
        snapshot=snapshot,
        valuation=None,
        valuation_binding=ValuationBinding(state="missing", reason="no_valuation"),
        mandate=mandate,
        purpose_version=None,
        purpose_binding=purpose_binding,
        listing_sources=(frozen_listing,),
        assessment_as_of=AS_OF,
        price_basis_at=None,
        mode="live",
        manifest_hash=HASH,
        source_fingerprint=HASH,
    )
    operation_id = uuid4()
    fit_id = uuid4()
    assumptions = TradeAssumptions(
        assumption_source="test",
        unknown_fields=("execution_price", "fixed_fee", "fee_rate"),
    )
    request_item = FitRequestItem(
        listing_ref=listing_ref, action="open", trade_assumptions=assumptions
    )
    return AccountFitAssessment(
        id=fit_id,
        reference=ObjectRef(kind="account_fit", id=fit_id, revision=1),
        operation_id=operation_id,
        input_hash=HASH,
        account_id=account_id,
        listing_ref=listing_ref,
        action="open",
        request_item=request_item,
        purpose_binding_key=HASH,
        calculation_basis_key=HASH,
        assessment_as_of=AS_OF,
        price_basis_at=None,
        expires_at=AS_OF + timedelta(hours=1),
        frozen_inputs=frozen_inputs,
        financial_status="unknown",
        checks=(),
        ranking_bucket="incomplete",
        coverage="partial",
    )


def _ranking_bundle(*, member_ids: tuple[int, ...] = (101,)):
    ranking_id = uuid4()
    account_id = uuid4()
    ranking_ref = ObjectRef(kind="candidate_ranking", id=ranking_id, revision=1)
    members = []
    entries = []
    for ordinal, listing_id in enumerate(member_ids):
        listing_ref = ObjectRef(kind="listing", id=listing_id, revision=1)
        run_ref = ObjectRef(kind="pipeline_run", id=200 + ordinal, revision=1)
        source_ref = ObjectRef(
            kind="pipeline_run",
            id=run_ref.id,
            revision=1,
            field_path=f"screen_results.{300 + ordinal}",
        )
        row_snapshot = {"listing_id": listing_id, "row_id": 300 + ordinal}
        row_hash = sha256(
            json.dumps(
                row_snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        source = ScreenCandidateSource(
            source_kind="screen_hit",
            source_ref=source_ref,
            listing_ref=listing_ref,
            run_ref=run_ref,
            source_row_id=300 + ordinal,
            run_status="success",
            run_finished_at=AS_OF,
            run_hash=HASH,
            outcome="hit",
            source_as_of=AS_OF,
            source_snapshot_hash=row_hash,
            row_snapshot=row_snapshot,
            template_id="synthetic-template",
            template_version="1",
            rank=1,
        )
        members.append(SourceMember(listing_ref=listing_ref, sources=(source,)))
        entries.append(
            RankingEntry(
                ranking_ref=ranking_ref,
                listing_ref=listing_ref,
                ordinal=ordinal,
                ranking_bucket="incomplete",
                financial_status="unknown",
                resolved_action=None,
                action_unknown_reason="synthetic_unknown",
                source_refs=(source_ref,),
            )
        )
    source_runs = tuple(
        SourceRunSnapshot(
            run_ref=ObjectRef(kind="pipeline_run", id=200 + ordinal, revision=1),
            status="success",
            finished_at=AS_OF,
            run_hash=HASH,
            as_of=AS_OF,
            template_id="synthetic-template",
            template_version="1",
            row_count=1,
        )
        for ordinal in range(len(member_ids))
    )
    manifest_values = {
            "source_run_refs": tuple(run.run_ref for run in source_runs),
            "source_runs": source_runs,
            "candidate_policy": CandidatePolicySnapshot(
                expiry_days=14, dismiss_cooldown_days=7
            ),
            "as_of": AS_OF,
            "members": tuple(members),
            "exclusions": (),
            "coverage": "complete",
            "total_unique_count": len(member_ids),
            "processed_count": len(member_ids),
            "unprocessed_count": 0,
            "source_hashes": tuple(
                sorted(source.source_snapshot_hash for member in members for source in member.sources)
            ),
    }
    provisional = SourceManifest.model_construct(**manifest_values)
    canonical_manifest = canonical_source_manifest_bytes(provisional)
    manifest = SourceManifest.model_validate(
        provisional.model_dump(mode="json")
        | {
            "manifest_hash": sha256(canonical_manifest).hexdigest(),
            "canonical_bytes": len(canonical_manifest),
        }
    )
    snapshot = RankingSnapshot(
        id=ranking_id,
        reference=ranking_ref,
        input_hash=HASH,
        manifest_hash=manifest.manifest_hash,
        universe_hash=canonical_universe_hash(manifest.members),
        account_id=account_id,
        created_at=AS_OF,
        as_of=AS_OF,
        source_manifest=manifest,
        entry_count=len(member_ids),
        coverage="complete",
        current_status="valid",
    )
    return snapshot, tuple(entries)


@pytest.mark.asyncio
async def test_account_fit_round_trips_only_for_its_exact_owner_and_kind(db_session) -> None:
    assessment = _fit_assessment()
    repository = InvestmentAssessmentRepository(db_session)

    saved = await repository.save_assessment("local_owner", assessment)
    await db_session.commit()

    assert saved == assessment
    assert await repository.get_exact(
        "local_owner", assessment.reference, expected_kind="account_fit"
    ) == assessment
    with pytest.raises(ValueError, match="does not match"):
        await repository.get_exact(
            "local_owner", assessment.reference, expected_kind="candidate_ranking"
        )
    assert await repository.save_assessment("local_owner", assessment) == assessment


@pytest.mark.asyncio
async def test_ranking_is_saved_as_one_complete_immutable_result(db_session) -> None:
    snapshot, entries = _ranking_bundle(member_ids=(101, 102))
    repository = InvestmentAssessmentRepository(db_session)

    stored = await repository.save_ranking("local_owner", snapshot, entries)
    await db_session.commit()

    assert stored.snapshot == snapshot
    assert stored.snapshot.ranking_policy_version == RANKING_POLICY_VERSION
    assert stored.entries == entries
    exact = await repository.read_ranking("local_owner", snapshot.reference)
    assert exact is not None
    assert exact.snapshot == snapshot
    assert exact.entries == entries
    assert await repository.list_ranking_entries(
        "local_owner", snapshot.reference, offset=1, limit=1
    ) == entries[1:]
    assert await repository.list_ranking_entries(
        "local_owner", snapshot.reference, offset=2, limit=1
    ) == ()
    replay = await repository.save_ranking("local_owner", snapshot, entries)
    assert replay == stored


def test_ranking_snapshot_requires_exact_reference_without_field_path() -> None:
    snapshot, _entries = _ranking_bundle()
    invalid = snapshot.model_dump(mode="python")
    invalid["reference"] = snapshot.reference.model_copy(update={"field_path": "payload"})
    with pytest.raises(ValidationError, match="exact UUID reference"):
        RankingSnapshot.model_validate(invalid)

@pytest.mark.asyncio
async def test_ranking_rejects_missing_members_and_reuses_only_same_uuid(db_session) -> None:
    snapshot, entries = _ranking_bundle(member_ids=(101, 102))
    repository = InvestmentAssessmentRepository(db_session)
    with pytest.raises(ValueError, match="entry count"):
        await repository.save_ranking("local_owner", snapshot, entries[:1])
    assert await db_session.get(
        InvestmentAssessment, (str(snapshot.id), 1)
    ) is None

    await repository.save_ranking("local_owner", snapshot, entries)
    second_id = uuid4()
    second_ref = ObjectRef(kind="candidate_ranking", id=second_id, revision=1)
    second_snapshot = RankingSnapshot.model_validate(
        snapshot.model_dump(mode="json")
        | {"id": second_id, "reference": second_ref, "created_at": AS_OF + timedelta(minutes=1)}
    )
    second_entries = tuple(
        RankingEntry.model_validate(
            entry.model_dump(mode="json") | {"ranking_ref": second_ref}
        )
        for entry in entries
    )
    assert second_snapshot.input_hash == snapshot.input_hash
    assert second_snapshot.account_id == snapshot.account_id
    await repository.save_ranking("local_owner", second_snapshot, second_entries)

    altered = RankingSnapshot.model_validate(
        snapshot.model_dump(mode="json") | {"created_at": "2026-10-16T05:00:00+00:00"}
    )
    with pytest.raises(ValueError, match="UUID conflicts"):
        await repository.save_ranking("local_owner", altered, entries)
