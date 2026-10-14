"""WP12: the PeerContextResolver and the unified method-scoped entry.

Covers the design's exit conditions (implementation plan §7, WP12):

* a rewritten ``subject_ref`` cannot disguise a peer fact as the target's;
* a fabricated ``calculation_ref`` (not in the submitted mapping / not the
  binding's reference / an unbound batch) is rejected - and a reused old
  record UUID validates through a *different* submitted batch mapping when
  its input manifest is compatible, while its original ``details.batch_run_id``
  neither has to match nor restores any authorization;
* the generic resolver still refuses any other company's facts;
* ``validate_progress``: a research progress carrying a bound peer-context
  calculation is no longer refused by the single-subject pre-check, every
  result is verified per real subject, mixed fake/peer facts are refused,
  and an unbound peer-method calculation keeps the pre-check rejection;
* the comparison narrow branch: one authorized target citing the bound
  metric references passes by the fixed-sample statistics; comparisons
  without the bound references keep the original two-subject rule; mixing
  unbound peer facts never passes;
* a record or fact created after the binding cutoff fails verification;
  a correction-state change invalidates the verification for the next
  request's resolver while the same request's cache holds;
* applicability exclusions need their field evidence, and a collection gap
  is never accepted as "not applicable";
* ``require_verified_counter_evidence`` still demands raw saved evidence -
  peer statistics cannot substitute (guarded);
* the trusted save path recomputes each built record before persisting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from investment_agent.agent.execution import (
    ExecutionDeniedError,
    ExecutionGrant,
)
from investment_agent.agent.research import validate_progress
from investment_agent.evidence.contracts import CalculationRecord
from investment_agent.evidence.store import EvidenceService
from investment_agent.industry_peers.calculations import (
    METHOD,
    METRIC_KEYS,
    ROE,
    BatchCompany,
    FrozenBatch,
    batch_field_from_snapshot,
    verify_built_metric_record,
)
from investment_agent.industry_peers.contracts import (
    PeerContextBinding,
)
from investment_agent.industry_peers.resolver import (
    PeerContextResolver,
    peer_method_entry,
)
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import (
    TextNode,
)
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.methods import MethodConfig, enforce_steps
from investment_agent.investor.publication_numbers import NumericResolver
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask, ExecutionGrantRow
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.peer import PeerAnnualSnapshot
from tests.support.conversations import (
    base,
    message,
    new_conversation,
    submit,
)
from tests.support.conversations import (
    conversation_db as conversation_db,  # noqa: F401  (pytest fixture)
)
from tests.support.peer_world import (
    FEATURE,
    GAP_PEER,
    PEER,
    PERIOD_END,
    SYNTHETIC_ROSTER,
    SYNTHETIC_VALUES,
    TARGET,
    clone_calculation,
    full_progress_for,
    listing_ref,
    make_binding,
    metric_refs,
    output_node,
    peer_candidate,
    prepared_with_peer,
    record_of,
    seed_world,
    store_snapshot,
    write_craft_mapping,
)
from tests.support.research import progress


@pytest.fixture
def peer_env(tmp_path, monkeypatch):
    """The service and the resolver resolve their artifact root at call time."""

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "fixture-key")
    return tmp_path


async def start_task(target: ObjectRef, *, kind: str, feature: dict | None = None):
    """Admit one message for the exact target and return its submission."""

    conversation = await new_conversation()
    return await submit(
        conversation,
        message(text="Compare these companies", research_kind=kind, context_refs=(target,)),
        settings=base(**(feature if feature is not None else FEATURE)),
    )


async def bound_refs(receipt) -> tuple[PeerContextBinding | None, dict[str, ObjectRef]]:
    """The admitted task's saved binding and its citable metric references."""

    async with session_scope() as session:
        grant = ExecutionGrant.model_validate(
            (await session.get(ExecutionGrantRow, str(receipt.task.id))).payload
        )
        if grant.peer_context is None:
            return None, {}
        refs = {
            entry.metric_key: entry.ref
            for entry in grant.peer_context.metric_calculation_refs
            if entry.ref is not None
        }
        return grant.peer_context, refs


async def evidence_ref_for(session, listing_id: int) -> ObjectRef:
    rows = (await session.scalars(select(SavedEvidence).where(SavedEvidence.owner_id == "local_owner"))).all()
    return next(
        ObjectRef(kind="evidence", id=UUID(row.id), revision=row.revision)
        for row in rows
        if row.payload["subject_ref"]["id"] == listing_id
    )


# --- registration and the five fixed operations ---------------------------------


def test_only_this_method_is_registered() -> None:
    entry = peer_method_entry(METHOD)
    assert entry is not None
    assert entry.operations == (
        "scope",
        "verify",
        "numeric_output",
        "review_material",
        "current_limits",
    )
    assert peer_method_entry("valuation.dcf") is None


# --- the resolver checklist ------------------------------------------------------


@pytest.mark.asyncio
async def test_bound_record_verifies_and_renders_output(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs[ROE])
        binding = make_binding(
            record=record,
            batch_run_id=submission.mapping.batch_run_id,
            refs_by_metric={ROE: refs[ROE]},
            cutoff=datetime.now(UTC),
        )
        resolver = PeerContextResolver(session, binding=binding)
        validation = await resolver.verify(refs[ROE])
        assert validation.metric_key == ROE
        assert validation.target_ref == listing_ref(synthetic, TARGET)
        assert validation.inputs_verified  # the dated synthetic fixture is verified
        assert validation.record.results["peer_median"].value == Decimal("0.175")
        assert validation.facts
        # numeric_output: the median renders as an output node on the target.
        resolved = await resolver.numeric_output(output_node(refs[ROE]))
        assert resolved.role == "calculation_output"
        assert resolved.text.endswith("%")
        assert resolved.subject_ref == listing_ref(synthetic, TARGET)
        # scope: only the bound target listing is in scope.
        assert await resolver.scope(listing_ref(synthetic, TARGET)) == listing_ref(synthetic, TARGET)
        with pytest.raises(InvestorError, match="scope"):
            await resolver.scope(listing_ref(synthetic, PEER))
        # review_material: the backend summary is verified for this fixture.
        material = await resolver.review_material(refs[ROE])
        assert material.verified
        assert material.material["input_hash"] == record.input_hash
        # current_limits: clean state answers no limitations.
        assert await resolver.current_limits(refs[ROE]) == ()




@pytest.mark.asyncio
async def test_subject_ref_rewrite_cannot_disguise_a_peer_fact(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs[ROE])
        peer_fact = next(
            ObjectRef.model_validate(entry["ref"])
            for entry in record.details["fact_evidence"]
            if entry["company_id"] == synthetic["ids"][PEER]
        )
        # The attack: rewrite the peer fact's subject into the target listing
        # and cite the rewritten evidence from a cloned record.
        evidence_row = await session.get(SavedEvidence, (str(peer_fact.id), peer_fact.revision))
        payload = dict(evidence_row.payload)
        payload["subject_ref"] = listing_ref(synthetic, TARGET).model_dump(mode="json")
        session.add(
            SavedEvidence(id=str(peer_fact.id), revision=2, owner_id="local_owner", payload=payload)
        )
        await session.flush()

        def rewrite(data):
            data["fact_refs"] = [
                {**fact, "revision": 2} if fact["id"] == str(peer_fact.id) else fact
                for fact in data["fact_refs"]
            ]
            data["details"]["fact_evidence"] = [
                {**entry, "ref": {**entry["ref"], "revision": 2}}
                if entry["ref"]["id"] == str(peer_fact.id)
                else entry
                for entry in data["details"]["fact_evidence"]
            ]
            return data

        mutated = await clone_calculation(session, refs[ROE], rewrite)
        await write_craft_mapping(
            peer_env, session,
            batch_run_id="peer-offline-craft-rewrite",
            record=record,
            target_company_id=synthetic["ids"][TARGET],
            target_listing_id=synthetic["listings"][TARGET],
            refs_by_metric={ROE: mutated},
            submitted_at=datetime.now(UTC) - timedelta(minutes=30),
        )
        binding = make_binding(
            record=record,
            batch_run_id="peer-offline-craft-rewrite",
            refs_by_metric={ROE: mutated},
            cutoff=datetime.now(UTC),
        )
        with pytest.raises(InvestorError, match="disguise"):
            await PeerContextResolver(session, binding=binding).verify(mutated)


@pytest.mark.asyncio
async def test_fabricated_or_unmapped_calculation_refs_are_rejected(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs[ROE])

        # A calculation reference that does not exist at all.
        ghost = ObjectRef(kind="calculation", id=uuid4(), revision=1)
        binding = make_binding(
            record=record,
            batch_run_id=submission.mapping.batch_run_id,
            refs_by_metric={ROE: ghost},
            cutoff=datetime.now(UTC),
        )
        with pytest.raises(InvestorError, match="unavailable"):
            await PeerContextResolver(session, binding=binding).verify(ghost)

        # The real record, but the binding points its metric elsewhere.
        swapped = make_binding(
            record=record,
            batch_run_id=submission.mapping.batch_run_id,
            refs_by_metric={ROE: refs["cfo_parent_profit"]},
            cutoff=datetime.now(UTC),
        )
        with pytest.raises(InvestorError, match="binding"):
            await PeerContextResolver(session, binding=swapped).verify(refs[ROE])

        # A bound batch whose submitted mapping does not carry the reference.
        await write_craft_mapping(
            peer_env, session,
            batch_run_id="peer-offline-craft-gap",
            record=record,
            target_company_id=synthetic["ids"][TARGET],
            target_listing_id=synthetic["listings"][TARGET],
            refs_by_metric={},
            submitted_at=datetime.now(UTC) - timedelta(minutes=30),
        )
        unbound_batch = make_binding(
            record=record,
            batch_run_id="peer-offline-craft-gap",
            refs_by_metric={ROE: refs[ROE]},
            cutoff=datetime.now(UTC),
        )
        with pytest.raises(InvestorError, match="mapping does not submit"):
            await PeerContextResolver(session, binding=unbound_batch).verify(refs[ROE])


@pytest.mark.asyncio
async def test_audit_company_outside_the_roster_is_rejected(conversation_db, peer_env):
    """WP14 acceptance mapping (跨行业补样本 → WP12 名单成员校验): a crafted
    record whose inclusion audit carries a company outside the bound roster -
    the cross-industry sample - is rejected at verification time, not just
    excluded by the trusted builder (WP5's roster-limit test)."""

    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs[ROE])

        def add_outsider(data):
            data["details"]["included_companies"] = [
                *data["details"]["included_companies"],
                {
                    "company_id": 999999,
                    "listing_id": 999999,
                    "ticker": "999999.SH",
                    "name": "跨界样本",
                },
            ]
            return data

        mutated = await clone_calculation(session, refs[ROE], add_outsider)
        await write_craft_mapping(
            peer_env, session,
            batch_run_id="peer-offline-craft-outsider",
            record=record,
            target_company_id=synthetic["ids"][TARGET],
            target_listing_id=synthetic["listings"][TARGET],
            refs_by_metric={ROE: mutated},
            submitted_at=datetime.now(UTC) - timedelta(minutes=30),
        )
        binding = make_binding(
            record=record,
            batch_run_id="peer-offline-craft-outsider",
            refs_by_metric={ROE: mutated},
            cutoff=datetime.now(UTC),
        )
        with pytest.raises(InvestorError, match="not a member of the bound"):
            await PeerContextResolver(session, binding=binding).verify(mutated)


@pytest.mark.asyncio
async def test_reused_uuid_passes_a_new_mapping_and_restores_nothing(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs[ROE])
        # The old record keeps its original batch identity: its
        # details.batch_run_id differs from the new binding's batch.
        assert record.details["batch_run_id"] == submission.mapping.batch_run_id
        await write_craft_mapping(
            peer_env, session,
            batch_run_id="peer-offline-craft-reuse",
            record=record,
            target_company_id=synthetic["ids"][TARGET],
            target_listing_id=synthetic["listings"][TARGET],
            refs_by_metric={ROE: refs[ROE]},
            submitted_at=datetime.now(UTC) - timedelta(minutes=30),
        )
        binding = make_binding(
            record=record,
            batch_run_id="peer-offline-craft-reuse",
            refs_by_metric={ROE: refs[ROE]},
            cutoff=datetime.now(UTC),
        )
        validation = await PeerContextResolver(session, binding=binding).verify(refs[ROE])
        assert validation.record.details["batch_run_id"] == submission.mapping.batch_run_id
        assert validation.record.details["batch_run_id"] != binding.batch_run_id

        # A metric the new mapping did not submit is not rescued by the old
        # record itself: its batch field never restores authorization.
        await write_craft_mapping(
            peer_env, session,
            batch_run_id="peer-offline-craft-reuse2",
            record=record,
            target_company_id=synthetic["ids"][TARGET],
            target_listing_id=synthetic["listings"][TARGET],
            refs_by_metric={},
            submitted_at=datetime.now(UTC) - timedelta(minutes=29),
        )
        narrow_batch = make_binding(
            record=record,
            batch_run_id="peer-offline-craft-reuse2",
            refs_by_metric={"contract_revenue": refs["contract_revenue"]},
            cutoff=datetime.now(UTC),
        )
        with pytest.raises(InvestorError, match="mapping does not submit"):
            await PeerContextResolver(session, binding=narrow_batch).verify(refs["contract_revenue"])


@pytest.mark.asyncio
async def test_records_after_the_cutoff_fail(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs[ROE])
        early = make_binding(
            record=record,
            batch_run_id=submission.mapping.batch_run_id,
            refs_by_metric={ROE: refs[ROE]},
            cutoff=record.created_at - timedelta(seconds=1),
        )
        with pytest.raises(InvestorError, match="cutoff"):
            await PeerContextResolver(session, binding=early).verify(refs[ROE])


@pytest.mark.asyncio
async def test_correction_state_change_invalidates_the_next_request(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs[ROE])
        binding = make_binding(
            record=record,
            batch_run_id=submission.mapping.batch_run_id,
            refs_by_metric={ROE: refs[ROE]},
            cutoff=datetime.now(UTC),
        )
        # Request one: clean; the cache holds inside the same request.
        first = PeerContextResolver(session, binding=binding)
        assert (await first.verify(refs[ROE])).inputs_verified
        # A supplier update for one cited peer field: pending re-check.
        await store_snapshot(
            session,
            synthetic["ids"][GAP_PEER],
            synthetic["listings"][GAP_PEER],
            {"roe_weighted_annual": "0.66"},
            semantic_hash="revision-02",
            captured_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        # Same request: the cached verification still serves the node render.
        assert (await first.verify(refs[ROE])).inputs_verified
        # New request: the correction state is re-checked and refuses.
        second = PeerContextResolver(session, binding=binding)
        with pytest.raises(InvestorError, match="pending_recheck"):
            await second.verify(refs[ROE])
        # The recheck operation reports the reasons instead of refusing.
        limits = await second.current_limits(refs[ROE])
        assert limits and limits[0].startswith("peer_field_recheck:")
        # Critical sources refuse regardless of the flag combination.
        with pytest.raises(InvestorError):
            await PeerContextResolver(session, binding=binding).verify(
                refs[ROE], require_verified_inputs=True
            )


@pytest.mark.asyncio
async def test_applicability_exclusion_requires_its_field_evidence(conversation_db, peer_env):
    gap_peer_values = {
        "roe_weighted_annual": "0.15",
        "equity_parent": "100",
        "contract_liabilities": "10",
        "operating_revenue": "-5",
        "operating_cash_flow": "70",
        "net_profit_parent": "100",
    }
    values = []
    for code, _name in SYNTHETIC_ROSTER:
        entry = dict(SYNTHETIC_VALUES[SYNTHETIC_ROSTER.index((code, _name))])
        if code == GAP_PEER:
            entry = gap_peer_values
        values.append(entry)
    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env, values=values)
        refs = await metric_refs(submission, synthetic)
        contract_ref = refs["contract_revenue"]
        record = await record_of(session, contract_ref)
        assert any(
            entry["reason"] == "non_positive_denominator"
            for entry in record.details["excluded_companies"]
        )
        binding = make_binding(
            record=record,
            batch_run_id=submission.mapping.batch_run_id,
            refs_by_metric={"contract_revenue": contract_ref},
            cutoff=datetime.now(UTC),
        )
        resolver = PeerContextResolver(session, binding=binding)
        assert (await resolver.verify(contract_ref, require_verified_inputs=True)).inputs_verified

        # Strip the applicability evidence: the exclusion loses its basis.
        def strip_applicability(data):
            dropped = [
                entry
                for entry in data["details"]["fact_evidence"]
                if entry["company_id"] == synthetic["ids"][GAP_PEER]
                and entry["role"] == "applicability"
            ]
            assert dropped
            dropped_id = dropped[0]["ref"]["id"]
            data["details"]["fact_evidence"] = [
                entry for entry in data["details"]["fact_evidence"] if entry["ref"]["id"] != dropped_id
            ]
            data["fact_refs"] = [fact for fact in data["fact_refs"] if fact["id"] != dropped_id]
            return data

        mutated = await clone_calculation(session, contract_ref, strip_applicability)
        await write_craft_mapping(
            peer_env, session,
            batch_run_id="peer-offline-craft-strip",
            record=record,
            target_company_id=synthetic["ids"][TARGET],
            target_listing_id=synthetic["listings"][TARGET],
            refs_by_metric={"contract_revenue": mutated},
            submitted_at=datetime.now(UTC) - timedelta(minutes=30),
        )
        stripped = make_binding(
            record=record,
            batch_run_id="peer-offline-craft-strip",
            refs_by_metric={"contract_revenue": mutated},
            cutoff=datetime.now(UTC),
        )
        with pytest.raises(InvestorError, match="applicability exclusion"):
            await PeerContextResolver(session, binding=stripped).verify(mutated)

        # A collection gap can never be presented as "not applicable".
        def claim_not_applicable(data):
            data["details"]["metric_status"] = "target_not_applicable"
            return data

        mutated2 = await clone_calculation(session, contract_ref, claim_not_applicable, revision=3)
        await write_craft_mapping(
            peer_env, session,
            batch_run_id="peer-offline-craft-gapclaim",
            record=record,
            target_company_id=synthetic["ids"][TARGET],
            target_listing_id=synthetic["listings"][TARGET],
            refs_by_metric={"contract_revenue": mutated2},
            submitted_at=datetime.now(UTC) - timedelta(minutes=29),
        )
        gap_claim = make_binding(
            record=record,
            batch_run_id="peer-offline-craft-gapclaim",
            refs_by_metric={"contract_revenue": mutated2},
            cutoff=datetime.now(UTC),
        )
        with pytest.raises(InvestorError, match="evidence-backed applicability exclusion"):
            await PeerContextResolver(session, binding=gap_claim).verify(mutated2)


@pytest.mark.asyncio
async def test_declared_inputs_stay_citable_but_never_verified(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs[ROE])

        def strip_all_facts(data):
            data["fact_refs"] = []
            data["details"]["fact_evidence"] = []
            data["details"]["unmaterialized_inputs"] = 12
            return data

        mutated = await clone_calculation(session, refs[ROE], strip_all_facts)
        await write_craft_mapping(
            peer_env, session,
            batch_run_id="peer-offline-craft-declared",
            record=record,
            target_company_id=synthetic["ids"][TARGET],
            target_listing_id=synthetic["listings"][TARGET],
            refs_by_metric={ROE: mutated},
            submitted_at=datetime.now(UTC) - timedelta(minutes=28),
        )
        binding = make_binding(
            record=record,
            batch_run_id="peer-offline-craft-declared",
            refs_by_metric={ROE: mutated},
            cutoff=datetime.now(UTC),
        )
        resolver = PeerContextResolver(session, binding=binding)
        validation = await resolver.verify(mutated)
        assert validation.facts == () and not validation.inputs_verified
        with pytest.raises(InvestorError, match="not fully verified"):
            await resolver.verify(mutated, require_verified_inputs=True)


@pytest.mark.asyncio
async def test_generic_resolver_still_refuses_other_companies(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs[ROE])
        resolver = NumericResolver(
            EvidenceService(session), listing_ref(synthetic, TARGET), datetime.now(UTC)
        )
        with pytest.raises(InvestorError):
            await resolver.calculation(refs[ROE], require_fresh=False)
        peer_fact = next(
            ObjectRef.model_validate(entry["ref"])
            for entry in record.details["fact_evidence"]
            if entry["company_id"] == synthetic["ids"][PEER]
        )
        with pytest.raises(InvestorError):
            await resolver.fact(peer_fact, require_fresh=False)


# --- the trusted save path recomputes before persisting --------------------------


@pytest.mark.asyncio
async def test_save_path_recomputation_guard(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        rows = (
            await session.scalars(
                select(PeerAnnualSnapshot).where(PeerAnnualSnapshot.report_period == PERIOD_END)
            )
        ).all()
        companies = []
        for row in sorted(rows, key=lambda item: item.company_id):
            code = next(
                code for code, cid in synthetic["ids"].items() if cid == row.company_id
            )
            companies.append(
                BatchCompany(
                    company_id=row.company_id,
                    listing_id=row.listing_id,
                    ticker=code,
                    name="fixture",
                    in_cohort=True,
                    report_period=row.report_period,
                    fields={metric: batch_field_from_snapshot(row, metric) for metric in row.fields},
                )
            )
        batch = FrozenBatch(
            target_company_id=synthetic["ids"][TARGET],
            report_period=PERIOD_END,
            cohort_ref=submission.mapping.cohort_ref,
            cohort_content_hash=submission.mapping.cohort_content_hash,
            companies=tuple(companies),
        )
        refs = await metric_refs(submission, synthetic)
        saved = await record_of(session, refs[ROE])
        # The pre-save draft recomputes exactly (the hook save_peer_calculations runs).
        from investment_agent.industry_peers.calculations import build_peer_metric_record

        built = build_peer_metric_record(batch, ROE, evidence_refs={}, as_of=saved.created_at, require_fact_refs=False)
        assert built.record is not None and built.record.input_hash == saved.input_hash
        verify_built_metric_record(batch, ROE, built.record)

        def tamper(data):
            data["results"].pop("peer_median")
            return data

        forged = CalculationRecord.model_validate(tamper(built.record.model_dump(mode="json")))
        with pytest.raises(ValueError, match="results"):
            verify_built_metric_record(batch, ROE, forged)


# --- validate_progress: the method-scoped pre-check ------------------------------


@pytest.mark.asyncio
async def test_progress_with_bound_peer_calculation_passes_the_pre_check(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await seed_world(session, peer_env)
        target = listing_ref(synthetic, TARGET)
    receipt = await start_task(target, kind="company_research")
    _binding, refs = await bound_refs(receipt)
    async with session_scope() as session:
        note = full_progress_for(
            fact_refs=(refs[ROE],),
            content=(
                TextNode(type="text", text="The peer reference is bound and verified."),
                output_node(refs[ROE]),
            ),
        )
        text, citation_refs = await validate_progress(session, receipt.task.id, note)
        assert "%" in text  # the median rendered as an output node
        assert any(ref.id == refs[ROE].id for ref in citation_refs)


@pytest.mark.asyncio
async def test_progress_pre_check_refuses_mixed_fake_and_peer_facts(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await seed_world(session, peer_env)
        target = listing_ref(synthetic, TARGET)
    receipt = await start_task(target, kind="company_research")
    _binding, refs = await bound_refs(receipt)
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="reference_unavailable"):
            await validate_progress(
                session,
                receipt.task.id,
                full_progress_for(
                    fact_refs=(refs[ROE], ObjectRef(kind="evidence", id=uuid4(), revision=1))
                ),
            )
        peer_fact = await evidence_ref_for(session, synthetic["listings"][PEER])
        with pytest.raises(ExecutionDeniedError, match="scope_mismatch"):
            await validate_progress(
                session,
                receipt.task.id,
                full_progress_for(fact_refs=(refs[ROE], peer_fact)),
            )


@pytest.mark.asyncio
async def test_unbound_peer_calculation_keeps_the_pre_check_rejection(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        target = listing_ref(synthetic, TARGET)
        refs = await metric_refs(submission, synthetic)
    receipt = await start_task(target, kind="company_research", feature={})
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="peer_calculation_not_bound"):
            await validate_progress(
                session, receipt.task.id, full_progress_for(fact_refs=(refs[ROE],))
            )


# --- the comparison narrow branch -------------------------------------------------


@pytest.mark.asyncio
async def test_comparison_narrow_branch_passes_on_bound_references(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await seed_world(session, peer_env)
        target = listing_ref(synthetic, TARGET)
    receipt = await start_task(target, kind="comparison")
    binding, refs = await bound_refs(receipt)
    assert binding is not None and set(refs) == set(METRIC_KEYS)
    async with session_scope() as session:
        text, citation_refs = await validate_progress(
            session,
            receipt.task.id,
            progress(
                kind="comparison",
                answered=True,
                fact_refs=(refs[ROE], refs["contract_revenue"]),
            ),
        )
        assert text
        assert {ref.id for ref in citation_refs} >= {refs[ROE].id, refs["contract_revenue"].id}


@pytest.mark.asyncio
async def test_comparison_without_bound_references_keeps_the_original_rule(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await seed_world(session, peer_env)
        target = listing_ref(synthetic, TARGET)
    receipt = await start_task(target, kind="comparison")
    async with session_scope() as session:
        target_evidence = await evidence_ref_for(session, target.id)
        # One target fact alone: the original two-subject same-period rule
        # still refuses - the narrow branch needs the bound metric references.
        with pytest.raises(ExecutionDeniedError, match="comparison_period_or_basis"):
            await validate_progress(
                session,
                receipt.task.id,
                progress(kind="comparison", answered=True, fact_refs=(target_evidence,)),
            )


@pytest.mark.asyncio
async def test_comparison_mixing_unbound_peer_facts_is_refused(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await seed_world(session, peer_env)
        target = listing_ref(synthetic, TARGET)
    receipt = await start_task(target, kind="comparison")
    _binding, refs = await bound_refs(receipt)
    async with session_scope() as session:
        peer_fact = await evidence_ref_for(session, synthetic["listings"][PEER])
        with pytest.raises(ExecutionDeniedError, match="scope_mismatch"):
            await validate_progress(
                session,
                receipt.task.id,
                progress(kind="comparison", answered=True, fact_refs=(refs[ROE], peer_fact)),
            )


# --- require_verified_counter_evidence keeps its semantics ------------------------


@pytest.mark.asyncio
async def test_counter_evidence_cannot_be_peer_statistics(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        median_ref = refs[ROE].model_copy(update={"field_path": "results.peer_median"})
        for counter in (refs[ROE], median_ref):
            with pytest.raises(ExecutionDeniedError, match="method_verified_counter_required"):
                await enforce_steps(
                    session,
                    MethodConfig(require_verified_counter_evidence=True),
                    full_progress_for(counter_refs=(counter,)),
                    datetime.now(UTC),
                    final=True,
                    formal_directional=True,
                )



@pytest.mark.asyncio
async def test_publication_routes_peer_sources_and_rechecks_the_dependency(conversation_db, peer_env):
    from investment_agent.investor.publication import PublicationService
    from tests.support.publication import semantic

    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs[ROE])
    binding = make_binding(
        record=record,
        batch_run_id=submission.mapping.batch_run_id,
        refs_by_metric=dict(refs),
        cutoff=datetime.now(UTC),
    )
    conversation = await new_conversation()
    receipt = await submit(
        conversation,
        message(text="Compare these companies", research_kind="company_research", context_refs=(listing_ref(synthetic, TARGET),)),
        settings=base(**FEATURE),
    )
    async with session_scope() as session:
        task = await session.get(AgentTask, str(receipt.task.id))
        assert task is not None
        task_id = UUID(task.id)
    async with session_scope() as session:
        refs2 = await metric_refs(submission, synthetic)
        candidate = await peer_candidate(session, synthetic, refs2, task_id)
        review = semantic(candidate)
    claim, operation, review = await prepared_with_peer(candidate, review, binding)
    async with session_scope() as session:
        result = await PublicationService(session).publish(claim, operation, candidate, review, base=base())
    assert result.published, result.check.field_errors
    rendered = " ".join(node.text for node in result.resolved_nodes.values())
    assert rendered.endswith("%")  # the peer median rendered as an output node
    async with session_scope() as session:
        view = await PublicationService(session).get_recommendation(result.recommendation_ref)
    assert view.status.value == "published"
    # A later supplier update on a cited peer field: the dependency recheck
    # flags the published view for review (current_limits operation).
    async with session_scope() as session:
        await store_snapshot(
            session,
            synthetic["ids"][GAP_PEER],
            synthetic["listings"][GAP_PEER],
            {"roe_weighted_annual": "0.66"},
            semantic_hash="revision-02",
            captured_at=datetime.now(UTC),
        )
        await session.commit()
        view2 = await PublicationService(session).get_recommendation(result.recommendation_ref)
    assert any(reason.startswith("peer_field_recheck:") for reason in view2.limitation_reasons)
    assert view2.status.value == "review_required"


@pytest.mark.asyncio
async def test_peer_publication_without_a_binding_fails_closed(conversation_db, peer_env):
    from investment_agent.investor.publication import PublicationService
    from tests.support.publication import semantic

    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
    conversation = await new_conversation()
    receipt = await submit(
        conversation,
        message(text="Compare these companies", research_kind="company_research", context_refs=(listing_ref(synthetic, TARGET),)),
        settings=base(**FEATURE),
    )
    async with session_scope() as session:
        task = await session.get(AgentTask, str(receipt.task.id))
        assert task is not None
        task_id = UUID(task.id)
    async with session_scope() as session:
        refs2 = await metric_refs(submission, synthetic)
        candidate = await peer_candidate(session, synthetic, refs2, task_id)
        review = semantic(candidate)
    claim, operation, review = await prepared_with_peer(candidate, review, None)
    async with session_scope() as session:
        result = await PublicationService(session).publish(claim, operation, candidate, review, base=base())
    assert not result.published
    assert any(key.startswith("critical_source:calculation") for key in result.check.field_errors)
