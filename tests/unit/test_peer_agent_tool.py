"""WP11: the agent peer-context grant binding and the read-only tool.

Covers the design's exit conditions (implementation plan §7, WP11): a bound
task reads the three metrics with citable calculation references; an unbound
task is answered ``missing`` with the recorded gap and never a single value;
historical conversation material (old tool returns, calculation UUIDs) cannot
restore the permission; results created after the binding cutoff never enter
the old task; the same task's re-admission keeps the original binding; a
subtask continuation inherits only when target, cutoff and method are
consistent and the source is still available; legacy grants with
``peer_context`` null (or without the key) stay byte-compatible; the return
never carries ``pending``/``dependency_id``, the peer identity table, raw
responses or field-level raw values; and the no-progress guard registers the
tool's calculation references as material.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import select, update

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.execution import (
    ExecutionDeniedError,
    ExecutionGrant,
    NoProgressError,
    admit,
)
from investment_agent.agent.peer_tools import (
    PeerContextParameters,
    read_peer_context,
    registrations as peer_registrations,
)
from investment_agent.agent.research import ResearchOutput, check_progress_round
from investment_agent.agent.tasks import TaskService
from investment_agent.evidence.contracts import CalculationRecord
from investment_agent.industry_peers.artifacts import (
    DEFAULT_ADAPTER_VERSION,
    DEFAULT_PROVIDER,
    MAPPING_FILENAME,
)
from investment_agent.industry_peers.contracts import (
    BINDING_METRIC_KEYS,
    PeerContextBinding,
    PeerMetricBinding,
)
from investment_agent.industry_peers.jobs import (
    PEER_COLLECT_KIND,
    PeerCollectPayload,
    peer_collect_idempotency_key,
)
from investment_agent.industry_peers.service import (
    get_industry_peer_context,
    peer_context_binding_from,
)
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.enums import TaskStatus
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import ExecutionGrantRow, ToolAction
from investment_agent.storage.models.evidence import SavedCalculation
from investment_agent.storage.models.peer import PeerCohortSnapshot
from investment_agent.storage.repositories.pipeline_run import (
    IdempotencyPayloadMismatchError,
    PipelineRunRepository,
)
from tests.support.conversations import (
    base,
    claim,
    conversation_db as conversation_db,
    message,
    new_conversation,
    submit,
)
from tests.support.research import progress, response, run
from tests.unit.test_peer_service import (
    PERIOD_END,
    SYNTHETIC_ROSTER,
    seed_cohort,
    seed_listings,
    store_standard,
)

TARGET = "600001.SH"
PEER = "600002.SH"
FEATURE = dict(industry_peer_context_enabled=True)


@pytest.fixture
def peer_env(tmp_path, monkeypatch):
    """The service resolves its artifact root from the environment at call time.

    The flag is pinned in the environment too: since WP14 the tool's settlement
    re-reads the *current* configuration (env bootstrap + runtime store), so a
    direct ``read_peer_context`` call answers from the same source the run's
    ``base(**FEATURE)`` settings express.
    """

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "fixture-key")
    monkeypatch.setenv("INDUSTRY_PEER_CONTEXT_ENABLED", "true")
    return tmp_path


async def peer_world(session, tmp_path, *, with_batch=True):
    """The synthetic roster, its 2025 snapshots and (optionally) the batch.

    All fixture times are relative to the real clock so the scenario never
    depends on the wall calendar: snapshots are captured three hours ago and
    the batch was submitted one hour ago, so a message admitted now binds both
    while a message bound to an earlier cutoff binds neither.
    """

    from investment_agent.industry_peers.batch import submit_offline_batch

    identities = await seed_listings(session, SYNTHETIC_ROSTER)
    await seed_cohort(session, identities)
    synthetic = {
        "ids": {code: company for code, (company, _listing) in identities.items()},
        "listings": {code: listing for code, (_company, listing) in identities.items()},
    }
    await store_standard(session, synthetic, captured_at=datetime.now(UTC) - timedelta(hours=3))
    submission = None
    if with_batch:
        submission = await submit_offline_batch(
            session,
            data_dir=tmp_path,
            tickers=[TARGET],
            provider=DEFAULT_PROVIDER,
            adapter_version=DEFAULT_ADAPTER_VERSION,
            submitted_at=datetime.now(UTC) - timedelta(hours=1),
        )
    await session.commit()
    return synthetic, submission


def listing_ref(synthetic: dict, code: str) -> ObjectRef:
    return ObjectRef(kind="listing", id=synthetic["listings"][code], revision=1)


def peer_tool_model(ticker: str, sink: dict):
    """A fake model that calls the peer tool once, then answers from the return."""

    turns = 0

    def model(messages, info):
        nonlocal turns
        turns += 1
        if turns == 1:
            return ModelResponse(
                [ToolCallPart("get_industry_peer_context", {"business": {"ticker": ticker}}, tool_call_id="peer")],
                usage=RequestUsage(input_tokens=10, output_tokens=5),
            )
        for item in messages:
            for part in item.parts:
                if isinstance(part, ToolReturnPart) and part.tool_name == "get_industry_peer_context":
                    sink.clear()
                    sink.update(part.content)
        return response(ResearchOutput(progress=progress()))

    return model


def _feature_settings(data_dir):
    return base(**FEATURE, data_dir=data_dir)


# --- contracts: typed grant field, schema, legacy compatibility -----------------


def test_tool_schema_carries_only_the_target_ticker() -> None:
    spec = peer_registrations()[0]
    assert spec.name == "get_industry_peer_context"
    schema = spec.parameters.model_json_schema()
    assert set(schema["properties"]) == {"ticker"}
    assert "get_industry_peer_context" in {item.name for item in peer_registrations()}


def test_legacy_grant_payloads_stay_serialization_compatible() -> None:
    old = (
        '{"schema_version":1,"owner":"local_owner","mode":"interactive",'
        '"source":"authenticated_message","prompt":"Explain the saved question",'
        '"tool_names":[],"scope_refs":[],"watchlist":false,"memory_revision":0,'
        '"memory_valid_until":null,"mandate_id":null,"expires_at":null,'
        '"task_cost_usd":null,"daily_cost_usd":null,"purpose":"interactive",'
        '"current_research":false,"research_kind":"explanation",'
        '"disclosure_permissions":[],"portfolio_account_id":null,'
        '"portfolio_snapshot_ref":null,"portfolio_valuation_ref":null,"peer_context":null}'
    )
    grant = ExecutionGrant.model_validate_json(old)
    assert grant.peer_context is None
    assert json.loads(grant.model_dump_json()) == json.loads(old)
    # The pre-peer payload (no ``peer_context`` key, WP1) loads identically.
    assert ExecutionGrant.model_validate_json(old.replace(',"peer_context":null}', "}")) == grant


def test_typed_peer_context_round_trips_exactly() -> None:
    binding = PeerContextBinding(
        target=TARGET,
        cohort_ref=ObjectRef(kind="peer_cohort_snapshot", id=uuid4(), revision=3),
        batch_run_id="peer-batch-fixture",
        report_period=date(2025, 12, 31),
        cutoff=datetime(2026, 10, 12, 4, 0, tzinfo=UTC),
        metric_calculation_refs=tuple(
            PeerMetricBinding(
                metric_key=key,
                state="saved",
                ref=ObjectRef(kind="calculation", id=uuid4(), revision=1),
            )
            for key in sorted(BINDING_METRIC_KEYS)
        ),
    )
    grant = ExecutionGrant(
        mode="interactive", source="authenticated_message", prompt="p", peer_context=binding
    )
    data = grant.model_dump(mode="json")
    assert isinstance(data["peer_context"], dict)
    restored = ExecutionGrant.model_validate(data)
    assert restored.peer_context == binding
    assert restored.model_dump(mode="json") == data


# --- bound read: values, references, registration -------------------------------


@pytest.mark.asyncio
async def test_bound_task_reads_three_metrics_and_registers_refs(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, submission = await peer_world(session, peer_env)
        target = listing_ref(synthetic, TARGET)
    conversation = await new_conversation()
    receipt = await submit(
        conversation,
        message(text="Explain this company", context_refs=(target,)),
        settings=base(**FEATURE),
    )
    async with session_scope() as session:
        grant = ExecutionGrant.model_validate(
            (await session.get(ExecutionGrantRow, str(receipt.task.id))).payload
        )
        assert grant.peer_context is not None
        assert grant.peer_context.target == TARGET
        assert grant.peer_context.batch_run_id == submission.mapping.batch_run_id
        assert grant.peer_context.report_period == PERIOD_END
        assert "get_industry_peer_context" in grant.tool_names
        assert sorted(item.metric_key for item in grant.peer_context.metric_calculation_refs) == [
            "cfo_parent_profit",
            "contract_revenue",
            "roe",
        ]
    sink: dict = {}
    task = await run(
        receipt, FunctionModel(peer_tool_model(TARGET, sink)), settings=base(**FEATURE)
    )
    assert task.status == TaskStatus.SUCCEEDED
    payload = sink
    assert payload["status"] == "ok"
    assert payload["status"] != "pending" and payload["dependency_id"] is None
    data = payload["data"]
    assert data["coverage"] == "ready"
    assert data["target_ticker"] == TARGET
    assert data["report_period"] == PERIOD_END.isoformat()
    assert data["old_reference"] is False
    metrics = {item["metric_key"]: item for item in data["metrics"]}
    assert set(metrics) == {"roe", "contract_revenue", "cfo_parent_profit"}
    roe = metrics["roe"]
    assert roe["status"] == "comparable" and roe["state"] == "saved"
    assert Decimal(roe["target_value"]["value"]) > 0  # decimal string, never a float
    assert Decimal(roe["peer_median"]["value"]) > 0
    assert roe["target_value"]["unit"] == "fraction"
    assert roe["valid_peer_count"] >= 5 and roe["candidate_peer_count"] >= roe["valid_peer_count"]
    assert roe["verification_counts"].get("verified", 0) > 0
    assert roe["result_refs"]["peer_median"]["field_path"] == "results.peer_median"
    assert roe["calculation_ref"]["kind"] == "calculation"
    assert payload["refs"] and all(item["kind"] == "calculation" for item in payload["refs"])
    # The reduced return: no peer identity table, no raw material, no raw values.
    flat = json.dumps(payload)
    for forbidden in ("excluded_companies", "included_companies", "fact_evidence", "raw_value", "companies"):
        assert forbidden not in flat
    async with session_scope() as session:
        action = await session.scalar(
            select(ToolAction).where(
                ToolAction.task_id == str(task.id),
                ToolAction.parameters["tool"].as_string() == "get_industry_peer_context",
            )
        )
        assert action is not None and action.state == "committed"
        assert action.result["data"]["metrics"]
        assert {item["metric_key"] for item in action.result["data"]["metrics"]} == set(
            BINDING_METRIC_KEYS
        )


@pytest.mark.asyncio
async def test_out_of_scope_target_fails_closed(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await peer_world(session, peer_env)
        target = listing_ref(synthetic, TARGET)
    conversation = await new_conversation()
    receipt = await submit(
        conversation,
        message(text="Explain this company", context_refs=(target,)),
        settings=base(**FEATURE),
    )
    sink: dict = {}
    task = await run(
        receipt, FunctionModel(peer_tool_model(PEER, sink)), settings=base(**FEATURE)
    )
    assert task.status == TaskStatus.FAILED
    assert sink == {}  # nothing was returned for the unbound peer ticker


@pytest.mark.asyncio
async def test_adapter_rejects_ticker_outside_the_bound_target(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await peer_world(session, peer_env)
        result = await get_industry_peer_context(
            session, ticker=TARGET, data_dir=peer_env, cutoff=datetime.now(UTC)
        )
        binding = peer_context_binding_from(result)
        assert binding is not None
        task = await admit(
            session,
            base=_feature_settings(peer_env),
            input_key="peer-adapter-scope",
            binding=ContextBinding(
                context_refs=(),
                base_refs=(listing_ref(synthetic, TARGET),),
                as_of=datetime.now(UTC),
            ),
            grant=ExecutionGrant(
                mode="interactive",
                source="authenticated_message",
                prompt="peer",
                scope_refs=(listing_ref(synthetic, TARGET), listing_ref(synthetic, PEER)),
                tool_names=("get_industry_peer_context",),
                research_kind="company_research",
                peer_context=binding,
            ),
            kind="agent_research",
        )
    trusted = await claim(task)
    async with session_scope() as session:
        service = TaskService(session)
        action = await service.prepare_action(
            trusted,
            intent_key="peer:peer",
            effect="database",
            parameters={
                "tool": "get_industry_peer_context",
                "business": {"ticker": PEER},
                "phase": "research:v1",
            },
        )
        with pytest.raises(ExecutionDeniedError, match="peer_context_target_not_bound"):
            await service.settle_action(
                trusted,
                UUID(action.operation_id),
                lambda s, op: read_peer_context(s, op, PeerContextParameters(ticker=PEER)),
            )
        row = await session.get(ToolAction, str(action.operation_id))
        assert row.state != "committed" and row.result is None


# --- unbound reads: recorded gap, no values, run reference ----------------------


@pytest.mark.asyncio
async def test_unbound_task_is_missing_without_any_values(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await peer_world(session, peer_env, with_batch=False)
        target = listing_ref(synthetic, TARGET)
    conversation = await new_conversation()
    receipt = await submit(
        conversation,
        message(text="Explain this company", context_refs=(target,)),
        settings=base(**FEATURE),
    )
    async with session_scope() as session:
        grant = ExecutionGrant.model_validate(
            (await session.get(ExecutionGrantRow, str(receipt.task.id))).payload
        )
        assert grant.peer_context is None  # no submitted result: the gap is recorded
    sink: dict = {}
    task = await run(
        receipt, FunctionModel(peer_tool_model(TARGET, sink)), settings=base(**FEATURE)
    )
    assert task.status == TaskStatus.SUCCEEDED
    payload = sink
    assert payload["status"] == "missing"
    assert payload["dependency_id"] is None
    data = payload["data"]
    assert data["metrics"] == []
    assert data["coverage"] == "missing"
    assert "no peer-context result was bound" in data["gaps"][0]
    assert data["run_hint"] == []
    flat = json.dumps(payload)
    for forbidden in ("target_value", "peer_median", "ascending_position", "verification_counts"):
        assert forbidden not in flat


@pytest.mark.asyncio
async def test_background_collection_is_a_run_reference_never_a_wait(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await peer_world(session, peer_env, with_batch=False)
        target = listing_ref(synthetic, TARGET)
        cohort = (await session.scalars(select(PeerCohortSnapshot))).one()
        payload = PeerCollectPayload(
            cohort_snapshot_id=cohort.id,
            cohort_revision=cohort.revision,
            report_period=PERIOD_END,
            provider=DEFAULT_PROVIDER,
            adapter_version=DEFAULT_ADAPTER_VERSION,
            generation=1,
        )
        collect_run, created = await PipelineRunRepository(session).enqueue(
            kind=PEER_COLLECT_KIND,
            idempotency_key=peer_collect_idempotency_key(payload),
            triggered_by="test",
            request_json=payload.model_dump(mode="json"),
        )
        assert created
    conversation = await new_conversation()
    receipt = await submit(
        conversation,
        message(text="Explain this company", context_refs=(target,)),
        settings=base(**FEATURE),
    )
    sink: dict = {}
    task = await run(
        receipt, FunctionModel(peer_tool_model(TARGET, sink)), settings=base(**FEATURE)
    )
    assert task.status == TaskStatus.SUCCEEDED
    body = sink
    assert body["status"] == "missing"
    assert body["status"] != "pending" and body["dependency_id"] is None
    assert body["data"]["run_hint"] == [str(collect_run.id)]


# --- lifecycle: historical cutoff, retry, subtask continuation ------------------


@pytest.mark.asyncio
async def test_historical_cutoff_stays_unbound_and_a_new_message_rebinds(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await peer_world(session, peer_env, with_batch=True)
        target = listing_ref(synthetic, TARGET)
    conversation = await new_conversation()
    # Message 1 binds at a cutoff before the batch existed: the recorded gap
    # never adopts the later result, and no conversation history can restore it.
    historical_cutoff = datetime.now(UTC) - timedelta(minutes=90)
    first = await submit(
        conversation,
        message(
            text="Explain this company",
            context_refs=(target,),
            research_at=historical_cutoff,
        ),
        settings=base(**FEATURE),
    )
    # Message 2 is an ordinary new message: it re-binds from the current read.
    second = await submit(
        conversation,
        message(text="Explain this company again", context_refs=(target,)),
        settings=base(**FEATURE),
    )
    async with session_scope() as session:
        first_grant = ExecutionGrant.model_validate(
            (await session.get(ExecutionGrantRow, str(first.task.id))).payload
        )
        second_grant = ExecutionGrant.model_validate(
            (await session.get(ExecutionGrantRow, str(second.task.id))).payload
        )
        assert first_grant.peer_context is None
        assert second_grant.peer_context is not None
    sink: dict = {}
    task = await run(
        first, FunctionModel(peer_tool_model(TARGET, sink)), settings=base(**FEATURE)
    )
    assert task.status == TaskStatus.SUCCEEDED
    payload = sink
    assert payload["status"] == "missing"
    assert payload["data"]["metrics"] == []
    # The current read may honestly report that results now exist; it still
    # returns no value for the task bound before them.
    assert payload["data"]["coverage"] == "ready"
    assert "no peer-context result was bound" in payload["data"]["gaps"][0]
    flat = json.dumps(payload)
    assert "target_value" not in flat and "peer_median" not in flat


@pytest.mark.asyncio
async def test_records_created_after_the_cutoff_never_enter_the_old_task(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await peer_world(session, peer_env, with_batch=True)
        target = listing_ref(synthetic, TARGET)
    conversation = await new_conversation()
    receipt = await submit(
        conversation,
        message(text="Explain this company", context_refs=(target,)),
        settings=base(**FEATURE),
    )
    async with session_scope() as session:
        grant = ExecutionGrant.model_validate(
            (await session.get(ExecutionGrantRow, str(receipt.task.id))).payload
        )
        binding = grant.peer_context
        assert binding is not None
        later = binding.cutoff + timedelta(hours=1)
        for entry in binding.metric_calculation_refs:
            assert entry.ref is not None
            saved = await session.get(SavedCalculation, (str(entry.ref.id), entry.ref.revision))
            assert saved is not None
            record = CalculationRecord.model_validate(saved.payload)
            assert record.created_at <= binding.cutoff
            # Simulate a record whose creation time moved past the old task's
            # cutoff. Saved calculations are immutable through the ORM on
            # purpose; the core statement is the test-only time control.
            await session.execute(
                update(SavedCalculation)
                .where(
                    SavedCalculation.id == str(entry.ref.id),
                    SavedCalculation.revision == entry.ref.revision,
                )
                .values(payload={**saved.payload, "created_at": later.isoformat()})
            )
        await session.commit()
    sink: dict = {}
    task = await run(
        receipt, FunctionModel(peer_tool_model(TARGET, sink)), settings=base(**FEATURE)
    )
    assert task.status == TaskStatus.SUCCEEDED
    payload = sink
    assert payload["status"] == "missing"
    metrics = payload["data"]["metrics"]
    assert metrics and all(item["cutoff_excluded"] for item in metrics)
    assert any("after the cutoff" in warning for warning in payload["data"]["warnings"])
    assert all(item["target_value"] is None and item["peer_median"] is None for item in metrics)


@pytest.mark.asyncio
async def test_same_task_readmission_keeps_the_original_binding(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await peer_world(session, peer_env, with_batch=True)
        target = listing_ref(synthetic, TARGET)
        result = await get_industry_peer_context(
            session, ticker=TARGET, data_dir=peer_env, cutoff=datetime.now(UTC)
        )
        original = peer_context_binding_from(result)
        assert original is not None
        settings = _feature_settings(peer_env)
        binding_args = ContextBinding(
            context_refs=(), base_refs=(target,), as_of=datetime.now(UTC)
        )
        task = await admit(
            session,
            base=settings,
            input_key="peer-retry",
            binding=binding_args,
            grant=ExecutionGrant(
                mode="interactive",
                source="authenticated_message",
                prompt="peer",
                scope_refs=(target,),
                tool_names=("get_industry_peer_context",),
                peer_context=original,
            ),
        )
    rebound = original.model_copy(update={"batch_run_id": "peer-batch-submitted-later"})
    async with session_scope() as session:
        # The same task admitted again keeps the original binding even when the
        # trusted application's fresh read would now freeze a different batch.
        retried = await admit(
            session,
            base=_feature_settings(peer_env),
            input_key="peer-retry",
            binding=binding_args,
            grant=ExecutionGrant(
                mode="interactive",
                source="authenticated_message",
                prompt="peer",
                scope_refs=(target,),
                tool_names=("get_industry_peer_context",),
                peer_context=rebound,
            ),
        )
        assert retried.id == task.id
        stored = ExecutionGrant.model_validate(
            (await session.get(ExecutionGrantRow, str(task.id))).payload
        )
        assert stored.peer_context is not None
        assert stored.peer_context.batch_run_id == original.batch_run_id
    async with session_scope() as session:
        with pytest.raises(IdempotencyPayloadMismatchError):
            await admit(
                session,
                base=_feature_settings(peer_env),
                input_key="peer-retry",
                binding=binding_args,
                grant=ExecutionGrant(
                    mode="interactive",
                    source="authenticated_message",
                    prompt="peer-changed",
                    scope_refs=(target,),
                    tool_names=("get_industry_peer_context",),
                    peer_context=rebound,
                ),
            )


@pytest.mark.asyncio
async def test_subtask_inherits_binding_only_when_consistent(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await peer_world(session, peer_env, with_batch=True)
        target = listing_ref(synthetic, TARGET)
        result = await get_industry_peer_context(
            session, ticker=TARGET, data_dir=peer_env, cutoff=datetime.now(UTC)
        )
        binding = peer_context_binding_from(result)
        assert binding is not None
        settings = _feature_settings(peer_env)
        parent = await admit(
            session,
            base=settings,
            input_key="peer-parent",
            binding=ContextBinding(context_refs=(), base_refs=(target,), as_of=datetime.now(UTC)),
            grant=ExecutionGrant(
                mode="interactive",
                source="authenticated_message",
                prompt="parent",
                scope_refs=(target,),
                tool_names=(),
                research_kind="company_research",
                peer_context=binding,
            ),
            kind="agent_research",
        )

        async def child(input_key: str, *, scope, kind="company_research", as_of=None):
            return await admit(
                session,
                base=settings,
                input_key=input_key,
                binding=ContextBinding(
                    context_refs=(),
                    base_refs=(target,),
                    as_of=as_of or datetime.now(UTC),
                ),
                grant=ExecutionGrant(
                    mode="interactive",
                    source="authenticated_message",
                    prompt="child",
                    scope_refs=scope,
                    tool_names=(),
                    research_kind=kind,
                ),
                kind="agent_research",
                parent_task_id=parent.id,
            )

        async def stored_binding(task_id) -> PeerContextBinding | None:
            grant = ExecutionGrant.model_validate(
                (await session.get(ExecutionGrantRow, str(task_id))).payload
            )
            return grant.peer_context

        consistent = await child("peer-child-consistent", scope=(target,))
        inherited = await stored_binding(consistent.id)
        assert inherited is not None
        assert inherited.batch_run_id == binding.batch_run_id
        assert inherited == binding

        other_target = await child("peer-child-target", scope=(listing_ref(synthetic, PEER),))
        assert await stored_binding(other_target.id) is None

        other_kind = await child("peer-child-kind", scope=(target,), kind="explanation")
        assert await stored_binding(other_kind.id) is None

        earlier_cutoff = await child(
            "peer-child-cutoff", scope=(target,), as_of=binding.cutoff - timedelta(seconds=1)
        )
        assert await stored_binding(earlier_cutoff.id) is None

        # The bound source disappeared: the continuation degrades to no binding.
        for path in peer_env.rglob(MAPPING_FILENAME):
            path.unlink()
        unavailable = await child("peer-child-source", scope=(target,))
        assert await stored_binding(unavailable.id) is None


# --- feature gate and the no-progress guard -------------------------------------


@pytest.mark.asyncio
async def test_disabled_feature_neither_whitelists_nor_binds(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await peer_world(session, peer_env, with_batch=True)
        target = listing_ref(synthetic, TARGET)
    conversation = await new_conversation()
    # The gate at admission is the *effective* settings (base over runtime
    # store); the flag is explicitly off here even though the process
    # environment pins it on for the settlement-level tests below.
    receipt = await submit(
        conversation,
        message(text="Explain this company", context_refs=(target,)),
        settings=base(industry_peer_context_enabled=False),
    )
    async with session_scope() as session:
        grant = ExecutionGrant.model_validate(
            (await session.get(ExecutionGrantRow, str(receipt.task.id))).payload
        )
        assert grant.peer_context is None
        assert "get_industry_peer_context" not in grant.tool_names


@pytest.mark.asyncio
async def test_progress_guard_registers_peer_calculation_refs(conversation_db, peer_env):
    async with session_scope() as session:
        synthetic, _submission = await peer_world(session, peer_env, with_batch=True)
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
            parameters={
                "tool": "get_industry_peer_context",
                "business": {"ticker": TARGET},
                "phase": "research:v1",
            },
        )

        async def write(session, operation):
            return await read_peer_context(session, operation, PeerContextParameters(ticker=TARGET))

        result = await service.settle_action(trusted, UUID(action.operation_id), write)
        assert result["status"] == "ok"
        assert result["refs"] and all(item["kind"] == "calculation" for item in result["refs"])
        await check_progress_round(session, trusted)
        envelope, _ = await service.get_checkpoint(receipt.task.id)
        guard = envelope["data"]["progress_guard"]
        assert guard["signature"] != before  # the peer calculation refs are material
        assert guard["stagnant"] == 0
        await check_progress_round(session, trusted)
        with pytest.raises(NoProgressError):
            await check_progress_round(session, trusted)
