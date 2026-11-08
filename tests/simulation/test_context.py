from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from investment_agent.simulation.context import (
    SharedEvidence,
    build_common_manifest,
    own_account_context,
    verify_frozen_facts,
)
from investment_agent.simulation.contracts import (
    AccountScope,
    AccountState,
    EvidenceRef,
    SimulationAccount,
)
from investment_agent.simulation.data import canonical_json, content_hash
from investment_agent.storage.repositories.financial_snapshot import FinancialSnapshotRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository

DAY = date(2026, 11, 6)
CUTOFF = datetime(2026, 11, 6, 15, tzinfo=UTC)  # 23:00 Shanghai


async def seed(session):
    listing = await ListingRepository(session).ensure_a_share_listing("600519.SH")
    repo = MarketSnapshotRepository(session)
    await repo.insert(
        listing_id=listing.id,
        trade_date=DAY,
        open=Decimal(10),
        high=Decimal(11),
        low=Decimal(9),
        close=Decimal(10),
        volume=Decimal(100),
        amount=Decimal(1000),
        provider="baostock",
        captured_at=CUTOFF - timedelta(hours=1),
        trade_status=1,
        is_st=False,
    )
    return listing.id


async def test_strict_bar_capture_no_backfill_leak_and_hash_stable(db_session):
    listing_id = await seed(db_session)
    await MarketSnapshotRepository(db_session).insert(
        listing_id=listing_id,
        trade_date=DAY,
        open=Decimal(99),
        high=Decimal(99),
        low=Decimal(99),
        close=Decimal(99),
        volume=Decimal(100),
        provider="baostock",
        captured_at=CUTOFF + timedelta(days=1),
        is_backfill=True,
        trade_status=1,
        is_st=False,
    )

    def archive(digest, payload):
        return f"simulation/{digest}.json"

    args = dict(
        listing_ids=[listing_id],
        observation_date=DAY,
        cutoff=CUTOFF,
        archive=archive,
        require_financials=False,
    )
    frozen = await build_common_manifest(db_session, **args)
    again = await build_common_manifest(db_session, **args)
    assert frozen == again
    assert frozen.manifest.complete
    assert Decimal(frozen.payload()["facts"][0]["data"]["close"]) == 10
    mutated = frozen.payload()
    mutated["facts"] = []
    assert frozen.payload()["facts"]
    verify_frozen_facts(frozen)


async def test_financial_public_date_and_system_capture_both_enforced(db_session):
    listing_id = await seed(db_session)
    repo = FinancialSnapshotRepository(db_session)
    await repo.insert(
        listing_id=listing_id,
        report_period=date(2026, 6, 30),
        period_kind="semi",
        fields={"revenue": 10},
        provider="baostock",
        captured_at=CUTOFF - timedelta(hours=1),
        availability="actual",
        available_from=DAY,
    )
    await repo.insert(
        listing_id=listing_id,
        report_period=date(2026, 3, 31),
        period_kind="q1",
        fields={"revenue": 99},
        provider="baostock",
        captured_at=CUTOFF + timedelta(days=1),
        availability="actual",
        available_from=date(2026, 4, 30),
        is_backfill=True,
    )
    frozen = await build_common_manifest(
        db_session,
        listing_ids=[listing_id],
        observation_date=DAY,
        cutoff=CUTOFF,
        archive=lambda h, p: h,
    )
    assert not frozen.manifest.complete
    assert frozen.manifest.missing_reasons == (f"financial_missing:{listing_id}",)
    await repo.insert(
        listing_id=listing_id,
        report_period=date(2026, 3, 31),
        period_kind="q1",
        fields={"revenue": 20},
        provider="baostock",
        captured_at=CUTOFF - timedelta(days=1),
        availability="actual",
        available_from=date(2026, 4, 30),
    )
    frozen = await build_common_manifest(
        db_session,
        listing_ids=[listing_id],
        observation_date=DAY,
        cutoff=CUTOFF,
        archive=lambda h, p: h,
    )
    assert frozen.manifest.complete
    assert [f for f in frozen.payload()["facts"] if f["ref"].startswith("financial")][0]["data"][
        "fields"
    ]["revenue"] == 20


async def test_legacy_restated_financial_is_dated_from_its_restatement(db_session):
    listing_id = await seed(db_session)
    repo = FinancialSnapshotRepository(db_session)
    await repo.insert(
        listing_id=listing_id,
        report_period=date(2025, 12, 31),
        period_kind="annual",
        # Pre-fix artifact: restated values under the original pubDate.
        fields={"revenue": 30, "restated": True, "notice_date": "2026-11-01"},
        provider="akshare",
        captured_at=CUTOFF - timedelta(hours=1),
        availability="actual",
        available_from=date(2026, 4, 30),
    )
    frozen = await build_common_manifest(
        db_session,
        listing_ids=[listing_id],
        observation_date=DAY,
        cutoff=CUTOFF,
        archive=lambda h, p: h,
    )
    assert frozen.manifest.complete
    financial = next(
        fact for fact in frozen.payload()["facts"] if fact["ref"].startswith("financial")
    )
    assert financial["data"]["available_from"] == "2026-11-01"


async def test_future_evidence_analysis_and_mutated_manifest_rejected(db_session):
    listing_id = await seed(db_session)
    payload = {"fact": "record"}
    ref = EvidenceRef(
        id="external:1",
        content_hash=content_hash(payload),
        published_at=CUTOFF,
        acquired_at=CUTOFF,
        available_at=CUTOFF,
        source="official",
    )
    shared = SharedEvidence(reference=ref, payload_json=canonical_json(payload))
    args = dict(
        listing_ids=[listing_id],
        observation_date=DAY,
        cutoff=CUTOFF,
        archive=lambda h, p: h,
        require_financials=False,
    )
    with pytest.raises(ValueError, match="factual"):
        await build_common_manifest(
            db_session, **args, evidence=[shared.model_copy(update={"kind": "analysis"})]
        )
    with pytest.raises(ValueError, match="after_cutoff"):
        await build_common_manifest(
            db_session,
            **args,
            evidence=[
                shared.model_copy(
                    update={
                        "reference": ref.model_copy(
                            update={"available_at": CUTOFF + timedelta(seconds=1)}
                        )
                    }
                )
            ],
        )
    frozen = await build_common_manifest(db_session, **args, evidence=[shared])
    with pytest.raises(ValueError, match="metadata"):
        verify_frozen_facts(
            frozen.model_copy(
                update={
                    "manifest": frozen.manifest.model_copy(
                        update={"cutoff": CUTOFF + timedelta(days=1)}
                    )
                }
            )
        )


async def test_private_context_rejects_other_account_and_mutable_or_unsettled_inputs(db_session):
    listing_id = await seed(db_session)
    common = await build_common_manifest(
        db_session,
        listing_ids=[listing_id],
        observation_date=DAY,
        cutoff=CUTOFF,
        archive=lambda h, p: h,
        require_financials=False,
    )
    scope = AccountScope(experiment_id="e", participant_id="p", account_id="a")
    account = SimulationAccount(
        **scope.model_dump(), initial_cash="100", state=AccountState(cash="100")
    )
    result = own_account_context(scope=scope, account=account, common=common)
    assert result.account == account
    with pytest.raises(ValueError, match="cross_participant"):
        own_account_context(
            scope=scope,
            account=account.model_copy(update={"participant_id": "other"}),
            common=common,
        )
    with pytest.raises(ValueError, match="closed"):
        await build_common_manifest(
            db_session,
            listing_ids=[listing_id],
            observation_date=DAY,
            cutoff=datetime(2026, 11, 6, 6, tzinfo=UTC),
            archive=lambda h, p: h,
        )


async def test_duplicate_shared_references_rejected_before_archiving(db_session):
    listing_id = await seed(db_session)
    payload = {"fact": "record"}
    ref = EvidenceRef(
        id="official:1",
        content_hash=content_hash(payload),
        published_at=CUTOFF,
        acquired_at=CUTOFF,
        available_at=CUTOFF,
        source="official",
    )
    shared = SharedEvidence(reference=ref, payload_json=canonical_json(payload))
    archived = []
    with pytest.raises(ValueError, match="duplicate_common"):
        await build_common_manifest(
            db_session,
            listing_ids=[listing_id],
            observation_date=DAY,
            cutoff=CUTOFF,
            archive=lambda h, p: archived.append(h) or h,
            require_financials=False,
            evidence=[shared, shared],
        )
    assert archived == []


@pytest.mark.parametrize(
    "reference_id,payload",
    [
        ("decision:another-agent", {"fact": "analysis"}),
        ("official:1", {"listing_id": 99999, "fact": "outside universe"}),
        ("official:1", {"facts": [{"account_id": "another-account", "cash": "100"}]}),
    ],
)
async def test_private_or_foreign_shared_evidence_cannot_enter_common_data(
    db_session, reference_id, payload
):
    listing_id = await seed(db_session)
    ref = EvidenceRef(
        id=reference_id,
        content_hash=content_hash(payload),
        published_at=CUTOFF,
        acquired_at=CUTOFF,
        available_at=CUTOFF,
        source="official",
    )
    with pytest.raises(ValueError, match="private_evidence|outside_common"):
        await build_common_manifest(
            db_session,
            listing_ids=[listing_id],
            observation_date=DAY,
            cutoff=CUTOFF,
            archive=lambda h, p: h,
            require_financials=False,
            evidence=[SharedEvidence(reference=ref, payload_json=canonical_json(payload))],
        )


async def test_manifest_verification_checks_each_fact_reference_hash(db_session):
    listing_id = await seed(db_session)
    common = await build_common_manifest(
        db_session,
        listing_ids=[listing_id],
        observation_date=DAY,
        cutoff=CUTOFF,
        archive=lambda h, p: h,
        require_financials=False,
    )
    tampered = common.payload()
    tampered["facts"][0]["data"]["close"] = "999"
    changed = common.model_copy(
        update={
            "payload_json": canonical_json(tampered),
            "manifest": common.manifest.model_copy(update={"content_hash": content_hash(tampered)}),
        }
    )
    with pytest.raises(ValueError, match="fact_hash"):
        verify_frozen_facts(changed)
    tampered = common.payload()
    tampered["facts"] = tampered["facts"] * 2
    changed = common.model_copy(
        update={
            "payload_json": canonical_json(tampered),
            "manifest": common.manifest.model_copy(update={"content_hash": content_hash(tampered)}),
        }
    )
    with pytest.raises(ValueError, match="facts_mismatch"):
        verify_frozen_facts(changed)


async def test_future_report_period_not_shared_even_if_corrupt_publication_is_past(db_session):
    listing_id = await seed(db_session)
    await FinancialSnapshotRepository(db_session).insert(
        listing_id=listing_id,
        report_period=date(2026, 12, 31),
        period_kind="annual",
        fields={"revenue": 999},
        provider="baostock",
        captured_at=CUTOFF - timedelta(hours=1),
        availability="actual",
        available_from=date(2026, 11, 1),
    )
    common = await build_common_manifest(
        db_session,
        listing_ids=[listing_id],
        observation_date=DAY,
        cutoff=CUTOFF,
        archive=lambda h, p: h,
    )
    assert not common.manifest.complete
    assert all(not fact["ref"].startswith("financial") for fact in common.payload()["facts"])


async def test_private_history_rejects_drafts_future_revisions_and_detached_reviews(db_session):
    from investment_agent.simulation.contracts import Decision, SimulationReview

    listing_id = await seed(db_session)
    common = await build_common_manifest(
        db_session,
        listing_ids=[listing_id],
        observation_date=DAY,
        cutoff=CUTOFF,
        archive=lambda h, p: h,
        require_financials=False,
    )
    scope = AccountScope(experiment_id="e", participant_id="p", account_id="a")
    account = SimulationAccount(
        **scope.model_dump(), revision=5, initial_cash="100", state=AccountState(cash="100")
    )
    decision = Decision(
        **scope.model_dump(),
        id="d",
        round_id="settled",
        revision=1,
        account_revision=4,
        status="locked",
        action="hold",
        rationale="hold",
        submitted_at=CUTOFF - timedelta(days=1),
        idempotency_key="decision-key",
    )
    review = SimulationReview(
        **scope.model_dump(),
        id="review",
        decision_id="d",
        observation_start=DAY - timedelta(days=2),
        observation_end=DAY - timedelta(days=1),
        available_at=CUTOFF - timedelta(hours=1),
        status="matured",
        findings="observed",
        memory_eligible=True,
    )
    args = dict(scope=scope, account=account, common=common, settled_round_ids=["settled"])
    assert own_account_context(**args, decisions=[decision], reviews=[review]).matured_reviews == (
        review,
    )
    for patch in (
        {"status": "draft"},
        {"account_revision": 6},
        {"submitted_at": CUTOFF + timedelta(seconds=1)},
    ):
        with pytest.raises(ValueError, match="not_settled_or_after"):
            own_account_context(**args, decisions=[decision.model_copy(update=patch)])
    for patch in (
        {"decision_id": "another-decision"},
        {"available_at": CUTOFF + timedelta(seconds=1)},
        {"status": "observing"},
    ):
        with pytest.raises(ValueError, match="not_matured_or_after"):
            own_account_context(
                **args, decisions=[decision], reviews=[review.model_copy(update=patch)]
            )


async def test_noncanonical_payload_cannot_hide_unhashed_json_tokens(db_session):
    listing_id = await seed(db_session)
    common = await build_common_manifest(
        db_session,
        listing_ids=[listing_id],
        observation_date=DAY,
        cutoff=CUTOFF,
        archive=lambda h, p: h,
        require_financials=False,
    )
    # json.loads would silently ignore the first duplicate key, although the
    # serialized string passed to a model would still expose its value.
    changed = common.model_copy(
        update={
            "payload_json": common.payload_json.replace(
                "{", '{"cutoff":"private-unhashed-content",', 1
            )
        }
    )
    with pytest.raises(ValueError, match="not_canonical"):
        verify_frozen_facts(changed)
