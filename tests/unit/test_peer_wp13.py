"""WP13: the fixed review-material formatter, publication formats, renewal guard.

Covers the WP13 deliverables (implementation plan §7) on top of the WP12
resolver, without touching any WP1-WP12 committed contract:

* **Fixed material formatter** - ``review_material`` returns the bounded
  metric digest (definition, target, period, results with units, sample
  counts, inclusion/exclusion rules as reason categories with counts, check
  coverage, limitations and hashes); never a per-company value table. The
  digests count toward the existing 32-material / 64000-character review
  window and an over-limit review still blocks (never trimmed into a pass).
* **Hashes** - ``manifest_hash`` binds the complete input manifest and the
  exclusion audit into the material set hash; ``summary_hash`` records the
  model-visible digest separately. ``checked_sources`` citing the metric
  calculation record is re-verified by the server along its fact_refs and
  audit; insufficient source verification keeps claims pending no matter
  what the digest shows.
* **Publication protocol** - peer statistics cannot publish as
  ``Claim.kind="fact"`` and render as output nodes only; the registered
  display rules keep the page (WP10) and the agent identical (fractions as
  two-decimal percents, the cash-flow ratio at four decimals, positions as
  percents); every other method keeps the two-decimal default.
* **Renewal guard** - opinions with critical peer dependencies refuse
  "maintain unchanged and extend" with structured reasons; the original
  validity dates and known limitation reasons stay; a task that read the old
  summary does not renew anything.
* **current_limits** - a method-version change, a peer field re-check,
  unverified inputs and a new cohort revision answer structured re-check
  reasons.
* **Progress and recovery** - the peer-read action receipt replays unchanged
  and the no-progress guard stays deterministic; continuation recovery never
  imports peer calculation details (the binding, not ``recovered_evidence``,
  restores the summary).
* **Semantic boundary** - the digest derives no strength ranking and no
  channel-inventory / sales / competitive-advantage / expected-return field;
  the ``report_views`` strength summaries do not consume peer results.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from investment_agent.agent.execution import ExecutionDeniedError, ExecutionGrant
from investment_agent.agent.research import check_progress_round, save_progress
from investment_agent.agent.research_publication import (
    CompanyViewInput,
    SemanticInput,
    prepare,
    reviewed_candidate,
)
from investment_agent.agent.tasks import TaskService
from investment_agent.evidence.contracts import EvidenceRecord, Period
from investment_agent.industry_peers.contracts import PeerContextBinding
from investment_agent.industry_peers.resolver import (
    DIGEST_SCHEMA,
    MANIFEST_HASH_SCHEMA,
    PeerContextResolver,
    RENEWAL_BLOCK_REASON,
    registered_display,
)
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.contracts import (
    DIMENSIONS,
    Claim,
    NumericNode,
    TextNode,
)
from investment_agent.investor.enums import NumericRole
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.publication import PublicationService
from investment_agent.investor.publication_numbers import format_quantity
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask, ExecutionGrantRow
from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
from investment_agent.storage.models.monitoring import ReviewCycle
from investment_agent.storage.models.peer import PeerCohortSnapshot
from tests.support.conversations import (
    base,
    claim,
    conversation_db as conversation_db,  # noqa: F401  (pytest fixture)
    message,
    new_conversation,
    submit,
)
from tests.support.publication import metadata, semantic, unchanged_review
from tests.unit.test_peer_resolver import (
    FEATURE,
    GAP_PEER,
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
    write_craft_mapping,
)
from tests.unit.test_peer_service import SYNTHETIC_ROSTER, SYNTHETIC_VALUES, store_snapshot

ROE = "roe"
CFO = "cfo_parent_profit"


@pytest.fixture
def peer_env(tmp_path, monkeypatch):
    """The service and the settlement re-read this environment (WP14 gate)."""

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "fixture-key")
    monkeypatch.setenv("INDUSTRY_PEER_CONTEXT_ENABLED", "true")
    return tmp_path


def canonical_hash(payload: dict) -> str:
    """The test-side twin of the resolver's canonical sha256."""

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- the fixed material formatter ------------------------------------------------


async def _world(conversation_db, peer_env):
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
    async with session_scope() as session:
        refs = await metric_refs(submission, synthetic)
        material = await PeerContextResolver(session, binding=binding).review_material(refs[ROE])
    return synthetic, submission, refs, binding, material


@pytest.mark.asyncio
async def test_review_material_is_the_fixed_metric_digest(conversation_db, peer_env):
    synthetic, _submission, refs, _binding, material = await _world(conversation_db, peer_env)
    digest = material.material
    # The fixed digest shape: no per-company value table, no peer identities,
    # no strength or business-judgement fields - the fixed keys only.
    assert set(digest) == {
        "summary_schema",
        "id",
        "revision",
        "method",
        "method_version",
        "metric_key",
        "definition",
        "unit",
        "position_rule",
        "target_ticker",
        "report_period",
        "metric_status",
        "results",
        "sample",
        "inclusion_rule",
        "exclusion_reasons",
        "verification_counts",
        "unmaterialized_inputs",
        "limitations",
        "input_hash",
        "manifest_hash",
        "summary_hash",
    }
    assert digest["summary_schema"] == DIGEST_SCHEMA
    assert digest["metric_key"] == ROE
    assert digest["definition"] == "roe_weighted_annual (issuer weighted-average ROE, saved as fraction)"
    assert digest["target_ticker"] == TARGET
    assert digest["report_period"] == "2025-12-31"
    assert digest["metric_status"] == "comparable"
    assert digest["results"]["peer_median"] == {"value": "0.175", "unit": "fraction"}
    assert digest["results"]["ascending_position"]["unit"] == "percent"
    assert digest["sample"] == {"valid_peer_count": 6, "candidate_peer_count": 6}
    assert digest["verification_counts"] == {"verified": 14}  # 7 companies x (value + equity)
    assert digest["unmaterialized_inputs"] == 0
    assert not any("stronger" in key or "weaker" in key for key in digest)
    assert not any(
        word in json.dumps(digest)
        for word in ("channel_inventory", "sales_volume", "competitive_advantage", "expected_return")
    )
    # The backend checklist answer is honest: the dated fixture is verified.
    assert material.verified
    # Limitations are the record's own quality notes (coverage facts), nothing derived.
    async with session_scope() as session:
        refs = await metric_refs(_submission, synthetic)
        assert digest["limitations"] == tuple((await record_of(session, refs[ROE])).quality)
        assert digest["input_hash"] == (await record_of(session, refs[ROE])).input_hash


@pytest.mark.asyncio
async def test_digest_hashes_are_deterministic_and_recomputable(conversation_db, peer_env):
    _synthetic, submission, refs, _binding, material = await _world(conversation_db, peer_env)
    digest = material.material
    # summary_hash covers exactly the digest without its own hash; recomputing
    # the canonical serialization must reproduce it (deterministic formatter).
    assert digest["summary_hash"] == canonical_hash(
        {key: value for key, value in digest.items() if key != "summary_hash"}
    )
    assert material.summary_hash == digest["summary_hash"]
    # manifest_hash covers the complete input manifest and exclusion audit.
    async with session_scope() as session:
        rows = (
            await session.scalars(select(SavedCalculation).where(SavedCalculation.owner_id == "local_owner"))
        ).all()
        record = next(
            row.payload
            for row in rows
            if isinstance(row.payload, dict) and row.payload.get("details", {}).get("metric_key") == ROE
        )
    expected = canonical_hash(
        {
            "schema": MANIFEST_HASH_SCHEMA,
            "input_hash": record["input_hash"],
            "fact_evidence": record["details"]["fact_evidence"],
            "included_companies": record["details"]["included_companies"],
            "excluded_companies": record["details"]["excluded_companies"],
            "unmaterialized_inputs": record["details"]["unmaterialized_inputs"],
        }
    )
    assert material.manifest_hash == digest["manifest_hash"] == expected


@pytest.mark.asyncio
async def test_manifest_hash_binds_the_complete_input_manifest(conversation_db, peer_env):
    """A reordered input manifest passes the checklist but changes the hashes."""

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

    def reverse_manifest(data):
        data["details"]["fact_evidence"] = list(reversed(data["details"]["fact_evidence"]))
        return data

    async with session_scope() as session:
        mutated = await clone_calculation(session, refs[ROE], reverse_manifest)
    write_craft_mapping(
        peer_env,
        batch_run_id="peer-offline-craft-wp13-manifest",
        record=record,
        target_company_id=_company_id(synthetic, TARGET),
        target_listing_id=synthetic["listings"][TARGET],
        refs_by_metric={ROE: mutated},
        submitted_at=datetime.now(UTC) - timedelta(minutes=30),
    )
    mutated_binding = make_binding(
        record=record,
        batch_run_id="peer-offline-craft-wp13-manifest",
        refs_by_metric={ROE: mutated},
        cutoff=datetime.now(UTC),
    )
    async with session_scope() as session:
        resolver = PeerContextResolver(session, binding=binding)
        first = await resolver.review_material(refs[ROE])
        second = await PeerContextResolver(session, binding=mutated_binding).review_material(mutated)
    # Both manifests verify; only the binding hashes react to the reorder.
    assert first.verified and second.verified
    assert first.manifest_hash != second.manifest_hash
    assert first.summary_hash != second.summary_hash
    stripped = {k: v for k, v in first.material.items() if k not in {"revision", "manifest_hash", "summary_hash"}}
    stripped2 = {k: v for k, v in second.material.items() if k not in {"revision", "manifest_hash", "summary_hash"}}
    assert stripped == stripped2  # the model-visible content is otherwise identical


def _company_id(synthetic: dict, code: str) -> int:
    return synthetic["ids"][code]


@pytest.mark.asyncio
async def test_digest_counts_exclusion_reason_categories_not_companies(conversation_db, peer_env):
    """The digest reports reason categories with counts, never a value table."""

    gap_values = {
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
            entry = gap_values
        values.append(entry)
    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env, values=values)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs["contract_revenue"])
    binding = make_binding(
        record=record,
        batch_run_id=submission.mapping.batch_run_id,
        refs_by_metric={"contract_revenue": refs["contract_revenue"]},
        cutoff=datetime.now(UTC),
    )
    async with session_scope() as session:
        material = await PeerContextResolver(session, binding=binding).review_material(
            refs["contract_revenue"]
        )
    digest = material.material
    assert digest["exclusion_reasons"] == {"non_positive_denominator": 1}
    assert "included_companies" not in digest and "excluded_companies" not in digest
    # No peer identity appears anywhere in the digest: identities are
    # page-only, the model sees reason categories with counts. (The absence of
    # a per-company value table is pinned structurally by the fixed key set in
    # the digest-structure test.)
    digest_text = json.dumps(digest)
    assert "600003.SH" not in digest_text  # the excluded peer's ticker
    assert "同行三" not in digest_text  # the excluded peer's name
    for ticker, _name in SYNTHETIC_ROSTER:
        if ticker != TARGET:
            assert ticker not in digest_text


# --- registered publication display rules ----------------------------------------


def test_registered_formats_render_the_design_golden_values():
    # fraction metrics: percent with two decimals (ROE and contract ratio)
    assert format_quantity(Quantity(value="0.3253", unit="fraction"), "percent")[0] == "32.53%"
    # the cash-flow ratio: registered four decimals (its unit is ``ratio``)
    assert registered_display("peer_median", "ratio") == ("decimal", 4)
    assert format_quantity(Quantity(value="0.5792", unit="ratio"), "decimal", precision=4)[0] == "0.5792 ratio"
    # positions are saved as percent and shown with two decimals
    assert format_quantity(Quantity(value="94.44", unit="percent"), "percent")[0] == "94.44%"
    # counts stay whole numbers
    assert registered_display("valid_peer_count", "count") == ("decimal", 0)
    assert format_quantity(Quantity(value="18", unit="count"), "decimal", precision=0)[0] == "18 count"


def test_other_methods_keep_the_two_decimal_default():
    # The registered precision is opt-in: every caller without it keeps the
    # historical default (two decimals), byte for byte.
    assert format_quantity(Quantity(value="0.5792", unit="ratio"), "decimal")[0] == "0.58 ratio"
    assert format_quantity(Quantity(value="12.345", unit="亿元"), "decimal")[0] == "1234500000.00 CNY"
    assert format_quantity(Quantity(value="12.345", unit="fraction"), "percent")[0] == "1234.50%"


@pytest.mark.asyncio
async def test_numeric_output_renders_through_the_registered_rules(conversation_db, peer_env):
    """The method entry formats, not the model: display_format cannot diverge."""

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
    async with session_scope() as session:
        resolver = PeerContextResolver(session, binding=binding)
        # ROE median: fraction -> percent, two decimals.
        roe_median = await resolver.numeric_output(output_node(refs[ROE]))
        assert roe_median.text == "17.50%" and roe_median.precision == 2
        # The position renders as a percent with two decimals.
        position = await resolver.numeric_output(output_node(refs[ROE], "ascending_position"))
        assert position.text == "83.33%"
        # Counts stay whole numbers.
        counts = await resolver.numeric_output(output_node(refs[ROE], "valid_peer_count"))
        assert counts.text == "6 count"
        # The cash-flow ratio renders with four decimals even when the model
        # asks for a percent display: the registered rule wins.
        cfo_node = NumericNode(
            type="numeric_claim",
            ref=refs[CFO].model_copy(update={"field_path": "results.peer_median"}),
            role=NumericRole.OUTPUT,
            display_format="percent",
        )
        cfo_median = await resolver.numeric_output(cfo_node)
        assert cfo_median.text == "0.7500 ratio" and cfo_median.precision == 4


def test_agent_display_matches_the_page_formatter():
    """The Python numeric tokens equal the WP10 JS rules for the same decimals."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
        return
    uri = (Path("src/investment_agent/web/static") / "industry_peers.js").resolve().as_uri()
    script = (
        "globalThis.document = undefined;\n"
        f"const m = await import({json.dumps(uri)});\n"
        + """
const f = m.formatMetricNumber;
console.log(JSON.stringify({
  roe: f("0.3253", "roe", "value"),
  contract: f("0.0474", "contract_revenue", "value"),
  cfo: f("0.5792", "cfo_parent_profit", "value"),
  position: f("94.44", "roe", "position"),
}));
"""
    )
    result = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    page = json.loads(result.stdout)
    # Python side: the same quantities through the agent's registered rules.
    roe = format_quantity(Quantity(value="0.3253", unit="fraction"), "percent")[0]
    contract = format_quantity(Quantity(value="0.0474", unit="fraction"), "percent")[0]
    cfo_text = format_quantity(Quantity(value="0.5792", unit="ratio"), "decimal", precision=4)[0]
    position = format_quantity(Quantity(value="94.44", unit="percent"), "percent")[0]
    assert roe == page["roe"] == "32.53%"
    assert contract == page["contract"] == "4.74%"
    assert position == page["position"] == "94.44%"
    # The ratio text carries the unit suffix; its numeric token must be identical.
    assert cfo_text.split(" ")[0] == page["cfo"] == "0.5792"


# --- prepare: digests, material set hash, pending honesty, review window ---------


async def _create_case_world(session, synthetic):
    """Mandate + case over the synthetic target (the prepare prerequisites)."""

    from investment_agent.investor.contracts import Mandate, ResearchAuthorization
    from investment_agent.investor.foundations import CreateCase, InvestorService

    target = listing_ref(synthetic, TARGET)
    service = InvestorService(session)
    unknown = Quantity(value=None, unit="USD", unknown_reason="Not specified")
    mandate = await service.create_mandate(
        "local_owner",
        Mandate(
            **metadata(),
            goals=(),
            scope_refs=(target,),
            hard_constraints=(),
            unknowns=(),
            authorization=ResearchAuthorization(
                daily_budget=unknown,
                per_task_budget=unknown,
                max_leads_per_batch=0,
                max_new_tasks_per_batch=0,
                source_refs=(),
            ),
        ),
        uuid4(),
    )
    case = await service.create_case(
        "local_owner",
        CreateCase(listing_ref=target, question="Peer reference view", idempotency_key=uuid4()),
    )
    return mandate, case


def _document_evidence(subject: ObjectRef, *, text: str | None = None, index: int = 0):
    """One verified document evidence row for the target listing; returns the
    unsaved ``(record, ref)`` pair."""

    from datetime import date, timedelta as _timedelta

    now = datetime.now(UTC) - _timedelta(days=1)
    record = EvidenceRecord(
        **metadata(),
        source_ref=ObjectRef(kind="evidence", id=uuid4(), revision=1),
        source_kind="document",
        locator="fixture",
        content_hash=f"fixture-wp13-{index}",
        subject_ref=subject,
        period=Period(start=date(2025, 1, 1), end=date(2025, 12, 31), basis="annual"),
        published_at=now,
        acquired_at=now,
        available_from=now,
        verification="verified",
        conflict="none",
        value=None if text is not None else Quantity(value="12.345", unit="亿元", currency="CNY"),
        text=text,
    )
    return record, ObjectRef(kind="evidence", id=record.id, revision=1)


async def _admit_peer_task(case_ref: ObjectRef, listing: ObjectRef, binding: PeerContextBinding):
    """One admitted company_research task carrying the trusted peer binding."""

    from investment_agent.agent.contracts import ContextBinding
    from investment_agent.agent.execution import admit

    async with session_scope() as session:
        from investment_agent.investor.foundations import InvestorService

        mandate = await InvestorService(session).get_mandate("local_owner")
        grant = ExecutionGrant(
            mode="interactive",
            source="authenticated_message",
            prompt="Prepare the peer-referenced view",
            tool_names=("prepare_company_view", "publish_recommendation", "extend_recommendation"),
            scope_refs=(case_ref, listing),
            mandate_id=mandate.id,
            current_research=True,
            research_kind="company_research",
            peer_context=binding,
        )
        task = await admit(
            session,
            base=base(),
            input_key=str(uuid4()),
            binding=ContextBinding(context_refs=(case_ref,), base_refs=(case_ref,), as_of=datetime.now(UTC)),
            grant=grant,
            kind="agent_research",
        )
    return task


async def _committed_progress(trusted, *, fact_refs=()) -> None:
    async with session_scope() as session:
        tasks = TaskService(session)
        note = full_progress_for(fact_refs=fact_refs)
        action = await tasks.prepare_action(
            trusted,
            intent_key="fixture:progress",
            parameters={"tool": "save_research_progress", "business": note.model_dump(mode="json"), "phase": "research:v1"},
            effect="database",
        )
        await tasks.settle_action(trusted, UUID(action.operation_id), lambda s, op: save_progress(s, op, note))


async def _committed_prepare(task_id: UUID, trusted, business: CompanyViewInput) -> dict:
    async with session_scope() as session:
        tasks = TaskService(session)
        action = await tasks.prepare_action(
            trusted,
            intent_key=f"fixture:prepare:{uuid4()}",
            parameters={"tool": "prepare_company_view", "business": business.model_dump(mode="json")},
            effect="database",
        )
        return await tasks.settle_action(
            trusted, UUID(action.operation_id), lambda s, op: prepare(s, op, business)
        )


def _business(case, claims, dimensions) -> CompanyViewInput:
    return CompanyViewInput(
        case_ref=ObjectRef(kind="case", id=case.id, revision=case.revision),
        question_scope="Peer-referenced sample position",
        change_reason="Initial research",
        assessed_dimensions=dimensions,
        claims=claims,
        counter_case=(TextNode(type="text", text="Fixed-sample statistics may not generalize"),),
        gaps=("Company report detail remains unavailable",),
        dependencies=(),
        business_view="undetermined",
        price_view="undetermined",
        sufficiency="bounded",
    )


def _dimensions():
    from investment_agent.investor.contracts import DimensionAssessment

    return tuple(
        DimensionAssessment(dimension=name, status="missing", applicable=True, reason="Report detail remains unavailable", refs=())
        for name in DIMENSIONS
    )


def _claim(refs, *, kind: str = "inference") -> Claim:
    return Claim(
        id=uuid4(),
        kind=kind,
        text=(TextNode(type="text", text="The roster statistics describe the sample position"),),
        materiality="critical",
        support_refs=tuple(refs),
        opposition_refs=(),
        assumption_refs=(),
        verification="pending",
        impact_if_wrong="The sample comparison must be reconsidered",
    )


async def _prepare_world(conversation_db, peer_env, *, evidence_specs):
    """Case + admitted peer-bound task + committed progress; returns the pieces."""

    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs[ROE])
        _mandate, case = await _create_case_world(session, synthetic)
        subject = listing_ref(synthetic, TARGET)
        evidence_refs = []
        for index, spec in enumerate(evidence_specs):
            evidence_record, ref = _document_evidence(subject, index=index, **spec)
            session.add(
                SavedEvidence(id=str(evidence_record.id), revision=1, owner_id="local_owner", payload=evidence_record.model_dump(mode="json"))
            )
            evidence_refs.append(ref)
        await session.flush()
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    binding = make_binding(
        record=record,
        batch_run_id=submission.mapping.batch_run_id,
        refs_by_metric=dict(refs),
        cutoff=datetime.now(UTC),
    )
    task = await _admit_peer_task(case_ref, listing_ref(synthetic, TARGET), binding)
    trusted = await claim(task)
    await _committed_progress(trusted)
    return synthetic, submission, refs, record, case, case_ref, evidence_refs, task, trusted


@pytest.mark.asyncio
async def test_prepare_delivers_digests_and_the_material_set_hash_binds_them(conversation_db, peer_env):
    synthetic, _submission, refs, _record, case, _case_ref, evidence_refs, _task, trusted = (
        await _prepare_world(conversation_db, peer_env, evidence_specs=[{}])
    )
    result = await _committed_prepare(
        trusted.task_id, trusted, _business(case, (_claim((*evidence_refs, *refs.values())),), _dimensions())
    )
    assert result["material_review_blocked"] is False
    digests = [m for m in result["materials"] if m.get("summary_schema") == DIGEST_SCHEMA]
    assert len(digests) == 3  # one fixed digest per bound metric
    assert {d["metric_key"] for d in digests} == set(refs)
    for digest in digests:
        assert digest["summary_hash"] and digest["manifest_hash"] and digest["input_hash"]
    evidence_summaries = [m for m in result["materials"] if "subject_ref" in m]
    assert len(evidence_summaries) == 1  # the generic evidence projection is unchanged
    assert result["material_count"] == 4
    # The delivered digest equals the resolver's formatter output (deterministic).
    async with session_scope() as session:
        binding = make_binding(
            record=await record_of(session, refs[ROE]),
            batch_run_id=_submission.mapping.batch_run_id,
            refs_by_metric=dict(refs),
            cutoff=datetime.now(UTC),
        )
        material = await PeerContextResolver(session, binding=binding).review_material(refs[ROE])
    assert digests[[d["metric_key"] for d in digests].index(ROE)] == material.material


@pytest.mark.asyncio
async def test_material_window_still_blocks_with_digests_counted(conversation_db, peer_env):
    """Thirty-two exact sources pass; three digests on top block the window."""

    specs = [{} for _ in range(32)]
    synthetic, _submission, refs, _record, case, _case_ref, evidence_refs, _task, trusted = (
        await _prepare_world(conversation_db, peer_env, evidence_specs=specs)
    )
    # 32 evidence materials alone: inside the window (other methods unchanged).
    inside = await _committed_prepare(
        trusted.task_id, trusted, _business(case, (_claim(evidence_refs),), _dimensions())
    )
    assert inside["material_review_blocked"] is False and inside["material_count"] == 32
    # The three peer digests push the same review over the limit: blocked, and
    # never trimmed into a passing review.
    blocked = await _committed_prepare(
        trusted.task_id, trusted, _business(case, (_claim((*evidence_refs, *refs.values())),), _dimensions())
    )
    assert blocked["material_review_blocked"] is True
    assert blocked["materials"] == [] and blocked["material_count"] == 35
    review = SemanticInput(
        preparation_ref=ObjectRef.model_validate(blocked["preparation_ref"]),
        assessments=(),
        checked_sources=(),
        unresolved_questions=(),
        conclusion="pass",
    )
    async with session_scope() as session:
        with pytest.raises(ExecutionDeniedError, match="material_review_window"):
            await reviewed_candidate(session, trusted.task_id, review)


@pytest.mark.asyncio
async def test_material_character_limit_blocks_with_a_bounded_digest_set(conversation_db, peer_env):
    specs = [{} for _ in range(3)] + [{"text": "x" * 70000}]
    synthetic, _submission, refs, _record, case, _case_ref, evidence_refs, _task, trusted = (
        await _prepare_world(conversation_db, peer_env, evidence_specs=specs)
    )
    result = await _committed_prepare(
        trusted.task_id, trusted, _business(case, (_claim((*evidence_refs, *refs.values())),), _dimensions())
    )
    assert result["material_review_blocked"] is True
    assert result["materials"] == [] and result["material_count"] == 7


@pytest.mark.asyncio
async def test_insufficient_source_verification_stays_pending_despite_the_digest(conversation_db, peer_env):
    """A digest never promotes a pending source: the claim stays pending."""

    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs[ROE])
        mandate, case = await _create_case_world(session, synthetic)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)

        def strip_all_facts(data):
            data["fact_refs"] = []
            data["details"]["fact_evidence"] = []
            data["details"]["unmaterialized_inputs"] = 12
            return data

        mutated = await clone_calculation(session, refs[ROE], strip_all_facts)
    write_craft_mapping(
        peer_env,
        batch_run_id="peer-offline-craft-wp13-pending",
        record=record,
        target_company_id=synthetic["ids"][TARGET],
        target_listing_id=synthetic["listings"][TARGET],
        refs_by_metric={ROE: mutated},
        submitted_at=datetime.now(UTC) - timedelta(minutes=30),
    )
    binding = make_binding(
        record=record,
        batch_run_id="peer-offline-craft-wp13-pending",
        refs_by_metric={ROE: mutated},
        cutoff=datetime.now(UTC),
    )
    task = await _admit_peer_task(case_ref, listing_ref(synthetic, TARGET), binding)
    trusted = await claim(task)
    await _committed_progress(trusted)
    result = await _committed_prepare(trusted.task_id, trusted, _business(case, (_claim((mutated,)),), _dimensions()))
    digests = [m for m in result["materials"] if m.get("summary_schema") == DIGEST_SCHEMA]
    assert len(digests) == 1  # the model received the digest
    assert digests[0]["verification_counts"] == {}
    assert digests[0]["unmaterialized_inputs"] == 12
    # ...and the claim stayed pending: the checklist answer flows into the
    # candidate, the summary alone never flips a verification.
    assert result["candidate"]["recommendation"]["claims"][0]["verification"] == "pending"


# --- publication protocol guards -------------------------------------------------


@pytest.mark.asyncio
async def test_peer_statistics_cannot_publish_as_fact_or_a_non_output_node(conversation_db, peer_env):
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
        task_id = UUID(task.id)
    async with session_scope() as session:
        refs = await metric_refs(submission, synthetic)
        candidate = await peer_candidate(session, synthetic, refs, task_id)

        # Attack 1: the descriptive statistic published as a historical fact.
        fact_claim = candidate.recommendation.claims[0].model_copy(update={"kind": "fact"})
        fact_candidate = candidate.model_copy(
            update={
                "thesis": candidate.thesis.model_copy(update={"claims": (fact_claim,)}),
                "recommendation": candidate.recommendation.model_copy(update={"claims": (fact_claim,)}),
            }
        )
        service = PublicationService(session)
        check, _resolved = await service.check(fact_candidate, semantic(fact_candidate), peer_context=binding)
        assert any("Historical fact requires evidence sources" in msg for msg in check.field_errors.values())

        # Attack 2: the statistic rendered as a historical-fact node.
        fact_node = NumericNode(
            type="numeric_claim",
            ref=refs[ROE].model_copy(update={"field_path": "results.peer_median"}),
            role=NumericRole.FACT,
            display_format="percent",
        )
        node_claim = candidate.recommendation.claims[0].model_copy(update={"text": (TextNode(type="text", text="median"), fact_node)})
        node_candidate = candidate.model_copy(
            update={
                "thesis": candidate.thesis.model_copy(update={"claims": (node_claim,)}),
                "recommendation": candidate.recommendation.model_copy(update={"claims": (node_claim,)}),
            }
        )
        check2, _resolved2 = await service.check(node_candidate, semantic(node_candidate), peer_context=binding)
        assert any("output nodes only" in msg for msg in check2.field_errors.values())


@pytest.mark.asyncio
async def test_checked_sources_reverify_along_the_fact_refs_and_audit(conversation_db, peer_env):
    """checked_sources citing the metric record re-verifies server-side."""

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
        task_id = UUID(task.id)
    async with session_scope() as session:
        refs = await metric_refs(submission, synthetic)
        candidate = await peer_candidate(session, synthetic, refs, task_id)
        service = PublicationService(session)
        # The metric calculation record as the model's checked source: the
        # server re-verifies it along fact_refs and the audit (the reference
        # loop passes; fabricated refs fail it, as the negative case shows).
        review = semantic(candidate)
        review = review.model_copy(
            update={"checked_sources": tuple({*review.checked_sources, refs[ROE]})}
        )
        check, resolved = await service.check(candidate, review, peer_context=binding)
        peer_errors = {k: v for k, v in check.field_errors.items() if k.startswith("semantic_review.checked_sources")}
        assert peer_errors == {}
        assert any("17.50%" in node.text for node in resolved.values())
        # A fabricated checked source still fails the same loop.
        ghost = ObjectRef(kind="calculation", id=uuid4(), revision=1)
        bad_review = semantic(candidate).model_copy(update={"checked_sources": (ghost,)})
        check2, _r = await service.check(candidate, bad_review, peer_context=binding)
        assert check2.field_errors and not check2.passed
        assert any("semantic_review.checked_sources" in key for key in check2.field_errors)


# --- renewal guard ---------------------------------------------------------------


async def _published_peer_view(peer_env):
    """Publish one peer-backed opinion and return the pieces (WP12 flow)."""

    from tests.support.publication import publish

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
        task_id = UUID(task.id)
    async with session_scope() as session:
        refs = await metric_refs(submission, synthetic)
        candidate = await peer_candidate(session, synthetic, refs, task_id)
        review = semantic(candidate)
    claim_, operation, review = await prepared_with_peer(candidate, review, binding)
    async with session_scope() as session:
        result = await publish(candidate, (claim_, operation, review))
    assert result.published, result.check.field_errors
    async with session_scope() as session:
        saved = await PublicationService(session).get_recommendation(result.recommendation_ref)
        recommendation = saved.recommendation
    return synthetic, submission, refs, binding, claim_, result, recommendation


async def _prepared_extension(trusted, recommendation_ref, review, case_revision) -> UUID:
    async with session_scope() as session:
        action = await TaskService(session).prepare_action(
            trusted,
            intent_key=f"extend:{uuid4()}",
            parameters={
                "recommendation_ref": recommendation_ref.model_dump(mode="json"),
                "review": review.model_dump(mode="json"),
                "expected_case_revision": case_revision,
            },
        )
    return UUID(action.operation_id)


@pytest.mark.asyncio
async def test_critical_peer_dependency_blocks_extension_with_structured_reasons(peer_env, conversation_db):
    synthetic, submission, refs, binding, claim_, result, recommendation = await _published_peer_view(peer_env)
    review_ev = unchanged_review(result.recommendation_ref, (refs[ROE],))
    operation = await _prepared_extension(claim_, result.recommendation_ref, review_ev, 2)
    async with session_scope() as session:
        service = PublicationService(session)
        with pytest.raises(InvestorError) as exc_info:
            await service.extend(claim_, operation, result.recommendation_ref, review_ev, expected_case_revision=2, base=base())
        message_text = str(exc_info.value)
        # Structured, not generic: the renewal reason names the dependency.
        assert RENEWAL_BLOCK_REASON in message_text
        assert str(refs[ROE].id) in message_text
        assert "run new research" in message_text
    # The original opinion keeps its established validity: nothing moved.
    async with session_scope() as session:
        view = await PublicationService(session).get_recommendation(result.recommendation_ref)
        assert view.status.value == "published"
        assert view.effective_valid_until == recommendation.valid_until
        assert view.effective_next_review_at == recommendation.next_review_at


@pytest.mark.asyncio
async def test_extension_refusal_carries_the_known_change_limits(peer_env, conversation_db):
    """The structured reasons embed the dependency's current_limits reasons."""

    synthetic, submission, refs, binding, claim_, result, recommendation = await _published_peer_view(peer_env)
    # A supplier update puts one cited peer field into pending re-check.
    async with session_scope() as session:
        await store_snapshot(
            session,
            synthetic["ids"][GAP_PEER],
            synthetic["listings"][GAP_PEER],
            {"roe_weighted_annual": "0.66"},
            semantic_hash="revision-wp13",
            captured_at=datetime.now(UTC),
        )
        # A newer roster revision (a new cohort) also awaits re-check.
        cohort = await session.get(PeerCohortSnapshot, str(binding.cohort_ref.id))
        session.add(
            PeerCohortSnapshot(
                id=str(uuid4()),
                cohort_key=cohort.cohort_key,
                revision=cohort.revision + 1,
                observed_on=cohort.observed_on,
                acquired_at=cohort.acquired_at,
                verification=cohort.verification,
                inclusion_rule_version=cohort.inclusion_rule_version,
                content_hash=cohort.content_hash + "-new",
                payload=cohort.payload,
            )
        )
        await session.commit()
    async with session_scope() as session:
        view = await PublicationService(session).get_recommendation(result.recommendation_ref)
        assert any(reason.startswith("peer_field_recheck:") for reason in view.limitation_reasons)
        assert any(reason.startswith("peer_cohort_revision:") for reason in view.limitation_reasons)
    review_ev = unchanged_review(result.recommendation_ref, (refs[ROE],))
    operation = await _prepared_extension(claim_, result.recommendation_ref, review_ev, 2)
    async with session_scope() as session:
        with pytest.raises(InvestorError) as exc_info:
            await PublicationService(session).extend(claim_, operation, result.recommendation_ref, review_ev, expected_case_revision=2, base=base())
        reasons = str(exc_info.value)
        assert f"{RENEWAL_BLOCK_REASON}:peer_field_recheck:" in reasons
        assert f"{RENEWAL_BLOCK_REASON}:peer_cohort_revision:" in reasons


@pytest.mark.asyncio
async def test_reading_the_old_summary_does_not_renew(peer_env, conversation_db):
    """A review task that read the old digest is still refused the renewal."""

    synthetic, _submission, refs, _binding, claim_, result, recommendation = await _published_peer_view(peer_env)
    from investment_agent.agent.peer_tools import PeerContextParameters, read_peer_context

    async with session_scope() as session:
        tasks = TaskService(session)
        action = await tasks.prepare_action(
            claim_,
            intent_key="tool:peer",
            effect="database",
            parameters={"tool": "get_industry_peer_context", "business": {"ticker": TARGET}, "phase": "research:v1"},
        )
        committed = await tasks.settle_action(
            claim_, UUID(action.operation_id), lambda s, op: read_peer_context(s, op, PeerContextParameters(ticker=TARGET))
        )
        assert committed["status"] == "ok"
    review_ev = unchanged_review(result.recommendation_ref, (refs[ROE],))
    operation = await _prepared_extension(claim_, result.recommendation_ref, review_ev, 2)
    async with session_scope() as session:
        with pytest.raises(InvestorError, match=RENEWAL_BLOCK_REASON):
            await PublicationService(session).extend(claim_, operation, result.recommendation_ref, review_ev, expected_case_revision=2, base=base())


@pytest.mark.asyncio
async def test_monitor_cycle_unchanged_review_is_refused_for_peer_dependencies(peer_env, conversation_db):
    synthetic, _submission, refs, _binding, claim_, result, recommendation = await _published_peer_view(peer_env)
    review_ev = unchanged_review(result.recommendation_ref, (refs[ROE],))
    async with session_scope() as session:
        session.add(
            ReviewCycle(id=str(uuid4()), case_id=str(result.recommendation_ref.id), task_id=str(claim_.task_id), state="running", payload={})
        )
        await session.flush()
        from investment_agent.investor.monitoring import verify_unchanged_review

        publication = PublicationService(session)
        with pytest.raises(InvestorError, match=RENEWAL_BLOCK_REASON):
            await verify_unchanged_review(session, claim_.task_id, review_ev, publication)


# --- current_limits extensions ---------------------------------------------------


@pytest.mark.asyncio
async def test_current_limits_answers_the_new_recheck_situations(conversation_db, peer_env):
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

    # A record built by another method version: reported, not refused.
    def reversion(data):
        data["method_version"] = "0.9"
        return data

    async with session_scope() as session:
        mutated = await clone_calculation(session, refs[ROE], reversion)
    write_craft_mapping(
        peer_env,
        batch_run_id="peer-offline-craft-wp13-version",
        record=record,
        target_company_id=synthetic["ids"][TARGET],
        target_listing_id=synthetic["listings"][TARGET],
        refs_by_metric={ROE: mutated},
        submitted_at=datetime.now(UTC) - timedelta(minutes=30),
    )
    old_version_binding = make_binding(
        record=record,
        batch_run_id="peer-offline-craft-wp13-version",
        refs_by_metric={ROE: mutated},
        cutoff=datetime.now(UTC),
    )
    async with session_scope() as session:
        limits = await PeerContextResolver(session, binding=old_version_binding).current_limits(mutated)
        assert limits == ("peer_method_version_changed:0.9",)

    # A new cohort revision: the published peer set awaits re-check.
    async with session_scope() as session:
        cohort = await session.get(PeerCohortSnapshot, str(binding.cohort_ref.id))
        session.add(
            PeerCohortSnapshot(
                id=str(uuid4()),
                cohort_key=cohort.cohort_key,
                revision=cohort.revision + 1,
                observed_on=cohort.observed_on,
                acquired_at=cohort.acquired_at,
                verification=cohort.verification,
                inclusion_rule_version=cohort.inclusion_rule_version,
                content_hash=cohort.content_hash + "-new",
                payload=cohort.payload,
            )
        )
        await session.commit()
    async with session_scope() as session:
        limits = await PeerContextResolver(session, binding=binding).current_limits(refs[ROE])
        assert f"peer_cohort_revision:{binding.cohort_ref.revision + 1}" in limits

    # Declared-inputs records answer the unverified-inputs reason.
    def strip_all_facts(data):
        data["fact_refs"] = []
        data["details"]["fact_evidence"] = []
        data["details"]["unmaterialized_inputs"] = 12
        return data

    async with session_scope() as session:
        stripped = await clone_calculation(session, refs[ROE], strip_all_facts, revision=3)
    write_craft_mapping(
        peer_env,
        batch_run_id="peer-offline-craft-wp13-limits",
        record=record,
        target_company_id=synthetic["ids"][TARGET],
        target_listing_id=synthetic["listings"][TARGET],
        refs_by_metric={ROE: stripped},
        submitted_at=datetime.now(UTC) - timedelta(minutes=29),
    )
    stripped_binding = make_binding(
        record=record,
        batch_run_id="peer-offline-craft-wp13-limits",
        refs_by_metric={ROE: stripped},
        cutoff=datetime.now(UTC),
    )
    async with session_scope() as session:
        limits = await PeerContextResolver(session, binding=stripped_binding).current_limits(stripped)
        assert "peer_inputs_unverified:roe" in limits


# --- progress guard and continuation recovery ------------------------------------


@pytest.mark.asyncio
async def test_peer_read_receipt_replays_without_new_material(conversation_db, peer_env):
    from investment_agent.agent.execution import NoProgressError
    from investment_agent.agent.peer_tools import PeerContextParameters, read_peer_context

    async with session_scope() as session:
        synthetic, _submission = await seed_world(session, peer_env)
        target = listing_ref(synthetic, TARGET)
    conversation = await new_conversation()
    receipt = await submit(
        conversation,
        message(text="Explain this company", context_refs=(target,)),
        settings=base(**FEATURE),
    )
    trusted = await claim(receipt.task)
    async with session_scope() as session:
        service = TaskService(session)
        await check_progress_round(session, trusted)
        envelope, _ = await service.get_checkpoint(receipt.task.id)
        before = envelope["data"]["progress_guard"]["signature"]
        action = await service.prepare_action(
            trusted,
            intent_key="tool:peer",
            effect="database",
            parameters={"tool": "get_industry_peer_context", "business": {"ticker": TARGET}, "phase": "research:v1"},
        )
        first = await service.settle_action(
            trusted, UUID(action.operation_id), lambda s, op: read_peer_context(s, op, PeerContextParameters(ticker=TARGET))
        )
        assert first["status"] == "ok"
        await check_progress_round(session, trusted)
        envelope, _ = await service.get_checkpoint(receipt.task.id)
        after = envelope["data"]["progress_guard"]["signature"]
        assert after != before and envelope["data"]["progress_guard"]["stagnant"] == 0
    # The retry replays the same committed receipt: identical result, no new
    # execution, and the no-progress guard stays deterministic (the same
    # material set keeps the signature stable; two stagnant rounds stop).
    async with session_scope() as session:
        service = TaskService(session)
        replay = await service.settle_action(
            trusted, UUID(action.operation_id), lambda s, op: read_peer_context(s, op, PeerContextParameters(ticker=TARGET))
        )
        assert replay == first
        await check_progress_round(session, trusted)
        envelope, _ = await service.get_checkpoint(receipt.task.id)
        assert envelope["data"]["progress_guard"]["signature"] == after
        assert envelope["data"]["progress_guard"]["stagnant"] == 1
        # The next replayed round without new material stops the task.
        with pytest.raises(NoProgressError):
            await check_progress_round(session, trusted)


@pytest.mark.asyncio
async def test_continuation_recovery_carries_no_peer_calculation_details(conversation_db, peer_env):
    """The binding restores the summary; ``recovered_evidence`` never does."""

    from investment_agent.agent.contracts import ContextBinding
    from investment_agent.agent.execution import admit
    from investment_agent.agent.research import recovered_evidence
    from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease

    async with session_scope() as session:
        synthetic, submission = await seed_world(session, peer_env)
        refs = await metric_refs(submission, synthetic)
        record = await record_of(session, refs[ROE])
        _mandate, case = await _create_case_world(session, synthetic)
        case_ref = ObjectRef(kind="case", id=case.id, revision=case.revision)
    binding = make_binding(
        record=record,
        batch_run_id=submission.mapping.batch_run_id,
        refs_by_metric=dict(refs),
        cutoff=datetime.now(UTC),
    )
    parent = await _admit_peer_task(case_ref, listing_ref(synthetic, TARGET), binding)
    trusted = await claim(parent)
    await _committed_progress(trusted, fact_refs=(refs[ROE],))
    # The parent read the peer summary with the bound tool (a committed receipt).
    from investment_agent.agent.peer_tools import PeerContextParameters, read_peer_context

    async with session_scope() as session:
        tasks = TaskService(session)
        action = await tasks.prepare_action(
            trusted,
            intent_key="tool:peer",
            effect="database",
            parameters={"tool": "get_industry_peer_context", "business": {"ticker": TARGET}, "phase": "research:v1"},
        )
        await tasks.settle_action(trusted, UUID(action.operation_id), lambda s, op: read_peer_context(s, op, PeerContextParameters(ticker=TARGET)))
    async with session_scope() as session:
        from investment_agent.investor.foundations import InvestorService

        mandate = await InvestorService(session).get_mandate("local_owner")
        # The continuation inherits only with the parent's exact scope set.
        child_scope = (case_ref, listing_ref(synthetic, TARGET))
        child = await admit(
            session,
            base=base(),
            input_key=str(uuid4()),
            binding=ContextBinding(context_refs=(), base_refs=child_scope, as_of=datetime.now(UTC)),
            grant=ExecutionGrant(
                mode="interactive",
                source="authenticated_message",
                prompt="child",
                scope_refs=child_scope,
                tool_names=(),
                research_kind="company_research",
            ),
            kind="agent_research",
            parent_task_id=parent.id,
        )
        row = await session.get(AgentTask, str(child.id))
        run = await PipelineRunRepository(session).claim_next(worker_id="fixture", run_id=row.run_id)
        child_claim = await TaskService(session).claim(child.id, RunLease(run.id, "fixture", run.attempt_count))
    async with session_scope() as session:
        child_grant = ExecutionGrant.model_validate((await session.get(ExecutionGrantRow, str(child.id))).payload)
        # WP11: the continuation inherited the binding - the summary is
        # recoverable through the bounded tool read (same binding, same batch).
        assert child_grant.peer_context == binding
        # ...while the recovery pack carries none of it: no calculation refs,
        # no digest, no large record details.
        recovered = await recovered_evidence(session, child_claim, child_grant)
        assert recovered is not None
        payload_text = json.dumps(recovered)
        assert "calculation" not in payload_text
        assert "included_companies" not in payload_text and DIGEST_SCHEMA not in payload_text
        assert str(record.id) not in payload_text


# --- semantic boundary guards ----------------------------------------------------


def test_report_views_do_not_consume_peer_results():
    """The old strength summaries never read anything this feature produces."""

    from investment_agent.research_kernel import report_views

    source = Path(report_views.__file__).read_text(encoding="utf-8")
    assert "industry_peers" not in source
    assert "peer_context" not in source
    # Feeding the peer digest (or a whole peer result) into the old views
    # produces no stronger/weaker summary and no metric leakage.
    digest = {
        "summary_schema": DIGEST_SCHEMA,
        "metric_key": "roe",
        "results": {"peer_median": {"value": "0.175", "unit": "fraction"}},
        "sample": {"valid_peer_count": 6, "candidate_peer_count": 6},
    }
    views = report_views.build_structured_views(
        {"name": "fixture", "key_metrics": {}, "investment_score": 90},
        digest,
    )
    assert views["peer_position"]["stronger_than_peers"] == []
    assert views["peer_position"]["weaker_than_peers"] == []
    assert views["peer_position"]["peer_count"] == 0
    assert digest["metric_key"] not in json.dumps(views["positive_thesis"])
    assert digest["metric_key"] not in json.dumps(views["negative_thesis"])
