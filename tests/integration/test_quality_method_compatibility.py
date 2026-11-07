"""QT-003: actual proposal/evaluation/admission/tools/settlement, synthetic only."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import func, select

from investment_agent.agent.contracts import ContextBinding
from investment_agent.agent.conversations import ConversationCreate, ConversationService
from investment_agent.agent.execution import ExecutionDeniedError, ExecutionGrant, admit
from investment_agent.agent.executor import AgentExecutor
from investment_agent.agent.job import AgentJobHandler
from investment_agent.agent.method_receipt import VALIDATOR_V1
from investment_agent.agent.research import (
    MethodCompanyStudyOutput,
    MethodPlannedProgress,
    ResearchOutput,
    ResearchPolicy,
    registrations,
)
from investment_agent.agent.research_publication import CompanyViewInput, SemanticInput
from investment_agent.agent.tasks import TaskService
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import (
    DIMENSIONS,
    Claim,
    Dependency,
    DimensionAssessment,
    Mandate,
    ResearchAuthorization,
    TextNode,
)
from investment_agent.investor.foundations import InvestorError, InvestorService
from investment_agent.investor.method_packages import (
    MethodStep,
    SnapshotFieldRequirement,
    canonical_hash,
    load_package_file,
)
from investment_agent.investor.methods import (
    CandidateShadowRequest,
    CandidateShadowResultRequest,
    CreateProposal,
    EvaluateProposal,
    EvaluationCase,
    MethodBinding,
    MethodService,
    task_method,
    task_method_receipt,
)
from investment_agent.investor.publication import (
    FIXED_METHOD,
    PublicationCandidate,
    SemanticAssessment,
)
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import engine_lifespan, session_scope
from investment_agent.storage.models import Base
from investment_agent.storage.models.agent import (
    AgentModelRun,
    AgentTask,
    BudgetEntry,
    ExecutionGrantRow,
    PublicTaskEvent,
    ToolAction,
)
from investment_agent.storage.models.investor import InvestorCase, MethodDefaultHead, MethodHead
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.models.publication import (
    PublicationArtifact,
    PublicationEvent,
    PublicationOutbox,
)
from investment_agent.storage.models.reference import Company, Listing, Market
from tests.support.conversations import base, message
from tests.support.quality_dataset import REPO, load_dataset
from tests.support.research import full_progress, progress, response


async def _seed_material_and_authorization():
    case = load_dataset()[0]
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Synthetic", timezone="Asia/Shanghai", currency="CNY"))
        session.add(Company(id=180001, canonical_name="QT003 synthetic company"))
        session.add(Listing(id=180001, company_id=180001, market_code="SSE", ticker="600001"))
        recipes = {item.snapshot.id: item.snapshot for item in case.materials}
        for recipe in recipes.values():
            session.add(FinancialSnapshot(**recipe.model_dump()))
        await session.flush()
        saved = []
        for material in case.materials:
            # The actual adapter re-derives every supplied field; no SavedEvidence is hand-written.
            ref = await EvidenceService(session).save_evidence(
                "local_owner", material.evidence, uuid4()
            )
            result = await EvidenceService(session).get_evidence(
                "local_owner", ref, datetime.now(UTC), subject_ref=case.listing_ref
            )
            assert (
                result.status == "ok"
                and result.data is not None
                and result.data.verification == "verified"
            )
            assert result.data.value.value is not None
            saved.append(ref)
        mandate = await InvestorService(session).create_mandate(
            "local_owner",
            Mandate(
                id=uuid4(),
                revision=1,
                created_at=datetime.now(UTC),
                actor="synthetic_input",
                origin_ref=None,
                goals=(),
                scope_refs=(case.listing_ref,),
                hard_constraints=(),
                unknowns=(),
                authorization=ResearchAuthorization(
                    background_enabled=True,
                    exploration_scope_refs=(case.listing_ref,),
                    max_leads_per_batch=0,
                    max_new_tasks_per_batch=0,
                    source_refs=(),
                ),
            ),
            uuid4(),
        )
    return case.listing_ref, saved[-1], mandate


async def _submit(selected, text, subject=None, *, kind="company_research"):
    async with session_scope() as session:
        service = ConversationService(session, selected)
        conversation = await service.create(ConversationCreate())
        return await service.submit(
            conversation.id,
            message(text=text, context_refs=(subject,) if subject else (), research_kind=kind),
        )


async def _run(selected, task_id, model, kind="agent_turn"):
    executor = AgentExecutor(
        selected,
        model=FunctionModel(model),
        registrations=registrations(),
        specification=ResearchPolicy(selected).specification(),
    )
    worker = ResearchWorker(
        settings=selected,
        worker_id="quality-compatibility-offline",
        handlers=[AgentJobHandler(kind, executor)],
    )
    async with session_scope() as session:
        run_id = (await session.get(AgentTask, str(task_id))).run_id
    await worker.run_once(run_id=run_id)
    async with session_scope() as session:
        task = await TaskService(session).get_task(task_id)
        if task.stop_reason is not None and task.stop_reason.value == "failed":
            errors = list(
                await session.scalars(
                    select(PublicTaskEvent).where(
                        PublicTaskEvent.task_id == str(task_id),
                        PublicTaskEvent.payload["type"].as_string() == "error",
                    )
                )
            )
            print({"fixture_failure": [row.payload for row in errors]})
        return task


def _returns(messages, tool):
    return [
        part
        for msg in messages
        for part in msg.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == tool
    ]


async def _baseline_reviews(selected, subject, evidence):
    dimensions = tuple(
        DimensionAssessment(
            dimension=name,
            status="missing",
            applicable=True,
            reason="Synthetic bounded source window; qualitative originals missing",
        )
        for name in DIMENSIONS
    )
    weak = full_progress(
        dimensions,
        question="Synthetic baseline incomplete question",
        fact_refs=(evidence,),
        answered=False,
    )
    failed = await _submit(selected, weak.question, subject)

    # This actual native task saves progress, reads existing evidence, and stops without a formal deliverable.
    def failure_model(messages, _info):
        if not _returns(messages, "save_research_progress"):
            return ModelResponse(
                [ToolCallPart("save_research_progress", {"business": weak.model_dump(mode="json")})]
            )
        if not _returns(messages, "get_evidence"):
            return ModelResponse(
                [
                    ToolCallPart(
                        "get_evidence",
                        {
                            "business": {
                                "ref": evidence.model_dump(mode="json"),
                                "subject_ref": subject.model_dump(mode="json"),
                                "as_of": failed.task.binding.as_of.isoformat(),
                            }
                        },
                    )
                ]
            )
        return response(ResearchOutput(progress=weak))

    failure = await _run(selected, failed.task.id, failure_model)
    assert failure.status.value == "partial" and failure.stop_reason.value == "evidence_blocked"
    strong = weak.model_copy(
        update={
            "question": "Synthetic baseline counterexample question",
            "hypotheses": (
                weak.hypotheses[0],
                weak.hypotheses[0].model_copy(
                    update={"alternative": "Cash could reflect a temporary investment cycle"}
                ),
            ),
            "counter_refs": (evidence,),
            "answered": True,
        }
    )
    accepted = await _submit(selected, strong.question, subject)
    async with session_scope() as session:
        stored = await session.scalar(
            select(InvestorCase).where(InvestorCase.listing_id == subject.id)
        )
        case_ref = ObjectRef(kind="case", id=UUID(stored.id), revision=stored.revision)
    claim = Claim(
        id=uuid4(),
        kind="fact",
        text=(
            TextNode(type="text", text="The synthetic saved cash has explicit reporting limits"),
        ),
        materiality="critical",
        support_refs=(evidence,),
        opposition_refs=(),
        assumption_refs=(),
        verification="verified",
        impact_if_wrong="The bounded cash observation must be reconsidered",
    )
    business = CompanyViewInput(
        case_ref=case_ref,
        question_scope=strong.question,
        change_reason="Synthetic native counterexample setup",
        assessed_dimensions=dimensions,
        claims=(claim,),
        counter_case=(
            TextNode(type="text", text="Temporary changes could explain the saved cash"),
        ),
        gaps=("Synthetic structural fixture; economic drivers remain unverified",),
        dependencies=(Dependency(ref=evidence, materiality="critical", fields=("value",)),),
        business_view="undetermined",
        price_view="undetermined",
        sufficiency="bounded",
    )

    def success_model(messages, _info):
        if not _returns(messages, "save_research_progress"):
            return ModelResponse(
                [
                    ToolCallPart(
                        "save_research_progress", {"business": strong.model_dump(mode="json")}
                    )
                ]
            )
        if not _returns(messages, "get_evidence"):
            return ModelResponse(
                [
                    ToolCallPart(
                        "get_evidence",
                        {
                            "business": {
                                "ref": evidence.model_dump(mode="json"),
                                "subject_ref": subject.model_dump(mode="json"),
                                "as_of": accepted.task.binding.as_of.isoformat(),
                            }
                        },
                    )
                ]
            )
        prepared = _returns(messages, "prepare_company_view")
        if not prepared:
            return ModelResponse(
                [
                    ToolCallPart(
                        "prepare_company_view", {"business": business.model_dump(mode="json")}
                    )
                ]
            )
        data = prepared[-1].content
        exact = PublicationCandidate.model_validate(data["candidate"])
        semantic = SemanticInput(
            preparation_ref=ObjectRef.model_validate(data["preparation_ref"]),
            assessments=tuple(
                SemanticAssessment(
                    claim_id=c.id,
                    fact_relation="Exact synthetic source supports only this bounded observation",
                    counter_evidence_relevance="Temporary cash changes remain possible",
                    materiality_reason="Cash persistence affects the observation",
                )
                for c in exact.thesis.claims
            ),
            checked_sources=(evidence,),
            unresolved_questions=(),
            conclusion="pass",
        )
        return response(ResearchOutput(progress=strong, semantic_review=semantic))

    success = await _run(selected, accepted.task.id, success_model)
    assert success.status.value == "succeeded" and success.stop_reason.value == "answered"
    outside = await _submit(selected, "Explain the synthetic research method", kind="explanation")
    outside_task = await _run(
        selected, outside.task.id, lambda _m, _i: response(ResearchOutput(progress=progress()))
    )
    assert outside_task.status.value == "succeeded"
    async with session_scope() as session:
        svc = MethodService(session)
        return (
            weak,
            strong,
            await svc.process_review(failure.id),
            await svc.process_review(success.id),
            await svc.process_review(outside_task.id),
        )


def _candidate_model(plan, subject, evidence, cutoff, *, block=False):
    def model(messages, _info):
        saved = _returns(messages, "save_research_progress")
        if not saved:
            return ModelResponse(
                [ToolCallPart("save_research_progress", {"business": plan.model_dump(mode="json")})]
            )
        if not _returns(messages, "get_evidence"):
            return ModelResponse(
                [
                    ToolCallPart(
                        "get_evidence",
                        {
                            "business": {
                                "ref": evidence.model_dump(mode="json"),
                                "subject_ref": subject.model_dump(mode="json"),
                                "as_of": cutoff.isoformat(),
                            }
                        },
                    )
                ]
            )
        operation = saved[0].content["artifact_ref"]["id"]
        results = [
            {
                "method_step_id": step,
                "evidence_step_indices": [0],
                "status": "satisfied",
                "action_ids": [operation],
                "hypothesis_indices": [0, 1],
                "output_refs": [evidence.model_dump(mode="json")]
                if step == "counter_analysis"
                else [],
            }
            for step in ("falsifiable_plan", "counter_analysis", "dimension_assessment")
        ]
        final = MethodPlannedProgress.model_validate(
            {
                **plan.model_dump(mode="json"),
                "answered": True,
                "step_results": results,
                "counter_refs": [] if block else [evidence.model_dump(mode="json")],
            }
        )
        return ModelResponse(
            [TextPart(MethodCompanyStudyOutput(progress=final).model_dump_json())],
            usage=RequestUsage(input_tokens=20, output_tokens=10),
        )

    return model


async def test_v1_c1_original_qualification_admission_read_and_settlement(tmp_path):
    selected = base(investor_agent_background_enabled=True)
    # Existing test configuration and all BudgetService checks remain unchanged.
    async with engine_lifespan(
        f"sqlite+aiosqlite:///{tmp_path / 'quality-compatibility.sqlite'}"
    ) as engine:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        subject, evidence, mandate = await _seed_material_and_authorization()
        weak, strong, failure, success, outside = await _baseline_reviews(
            selected, subject, evidence
        )
        definition = load_package_file(
            REPO / "config/methods/company-research.c1-counter-analysis.yaml"
        )
        request = CreateProposal(
            mutation_id=uuid4(),
            review_id=UUID(failure["id"]),
            listing_ref=subject,
            old_method=FIXED_METHOD,
            error_pattern="Synthetic incomplete output lacked verified counter analysis",
            summary="Require explicit opposing explanations and saved counter source",
            config=definition.discipline,
            package=definition,
        )
        async with session_scope() as session:
            svc = MethodService(session)
            with pytest.raises(InvestorError, match="原方法"):
                await svc.create(
                    request.model_copy(
                        update={
                            "mutation_id": uuid4(),
                            "old_method": ObjectRef(kind="method", id=uuid4(), revision=1),
                        }
                    )
                )
            proposal = await svc.create(request)
            assert await svc.create(request) == proposal
            count_before = await session.scalar(select(func.count()).select_from(AgentTask))
            with pytest.raises(InvestorError, match="已通过评估"):
                await svc.start_candidate_shadow(
                    CandidateShadowRequest(
                        mutation_id=uuid4(),
                        proposal_id=UUID(proposal["id"]),
                        prompt="Must not bypass structural evaluation",
                        expected="pass",
                    ),
                    base=selected,
                )
            assert await session.scalar(select(func.count()).select_from(AgentTask)) == count_before
        async with session_scope() as session:
            evaluation = await MethodService(session).evaluate(
                EvaluateProposal(
                    mutation_id=uuid4(),
                    proposal_id=UUID(proposal["id"]),
                    expected_revision=proposal["revision"],
                    cases=(
                        EvaluationCase(
                            review_id=UUID(failure["id"]), role="failure", expected="block"
                        ),
                        EvaluationCase(
                            review_id=UUID(success["id"]), role="counterexample", expected="pass"
                        ),
                        EvaluationCase(
                            review_id=UUID(outside["id"]), role="counterexample", expected="pass"
                        ),
                    ),
                )
            )
            assert evaluation["evaluation"]["passed"]
            proposal = evaluation["proposal"]
            assert proposal["status"] == "shadow"
        plan = MethodPlannedProgress.model_validate(
            {
                **strong.model_dump(mode="json"),
                "answered": False,
                "plan_bindings": [
                    {"evidence_step_index": 0, "method_step_id": step}
                    for step in ("falsifiable_plan", "counter_analysis", "dimension_assessment")
                ],
            }
        )
        for expected in ("pass", "block"):
            mutation = CandidateShadowRequest(
                mutation_id=uuid4(),
                proposal_id=UUID(proposal["id"]),
                prompt="Synthetic bounded C1 counter-analysis",
                expected=expected,
            )
            async with session_scope() as session:
                svc = MethodService(session)
                before = [
                    await session.scalar(select(func.count()).select_from(model))
                    for model in (PublicationArtifact, PublicationEvent, PublicationOutbox)
                ]
                started = await svc.start_candidate_shadow(mutation, base=selected)
                assert await svc.start_candidate_shadow(mutation, base=selected) == started
                task_id = UUID(started["task_id"])
                task = await TaskService(session).get_task(task_id)
                frozen = await task_method_receipt(session, task_id)
                binding = await task_method(session, task_id)
                assert (
                    frozen.validator_version == VALIDATOR_V1
                    and frozen.shadow == "candidate_package"
                )
                assert binding.package_hash == canonical_hash(definition)
                grant = ExecutionGrant.model_validate(
                    (await session.get(ExecutionGrantRow, str(task_id))).payload
                )
                assert (
                    grant.mandate_id == mandate.id
                    and "prepare_company_view" not in grant.tool_names
                )
            completed = await _run(
                selected,
                task_id,
                _candidate_model(
                    plan, subject, evidence, task.binding.as_of, block=expected == "block"
                ),
                "agent_research",
            )
            assert (completed.status.value, completed.stop_reason.value) == (
                ("succeeded", "answered") if expected == "pass" else ("partial", "failed")
            )
            async with session_scope() as session:
                actions = list(
                    await session.scalars(
                        select(ToolAction).where(
                            ToolAction.task_id == str(task_id), ToolAction.state == "committed"
                        )
                    )
                )
                read = [
                    action for action in actions if action.parameters.get("tool") == "get_evidence"
                ]
                assert len(read) == 1 and read[0].result["data"]["verification"] == "verified"
                assert not any(
                    action.parameters.get("tool")
                    in {"capture_snapshot", "request_data_refresh", "prepare_company_view"}
                    for action in actions
                )
                assert any(action.intent_key == "completion:progress" for action in actions) == (
                    expected == "pass"
                )
                if expected == "block":
                    errors = list(
                        await session.scalars(
                            select(PublicTaskEvent).where(PublicTaskEvent.task_id == str(task_id))
                        )
                    )
                    assert any(
                        row.payload.get("type") == "error"
                        and row.payload.get("payload", {}).get("code")
                        == "method_verified_counter_required"
                        for row in errors
                    )
                assert before == [
                    await session.scalar(select(func.count()).select_from(model))
                    for model in (PublicationArtifact, PublicationEvent, PublicationOutbox)
                ]
                result = await MethodService(session).candidate_shadow_result(
                    CandidateShadowResultRequest(
                        mutation_id=uuid4(),
                        proposal_id=UUID(proposal["id"]),
                        task_id=task_id,
                        expected_revision=proposal["revision"],
                    )
                )
                assert result["candidate_shadow"]["matched"]
                assert result["candidate_shadow"]["outcome"] == (
                    "passed" if expected == "pass" else "blocked"
                )
                proposal = result["proposal"]
                ledger = list(
                    await session.scalars(
                        select(BudgetEntry).where(BudgetEntry.task_id == str(task_id))
                    )
                )
                assert ledger and all(row.state == "settled" for row in ledger)
        # Original admission rejects a package requiring capture against the intended
        # saved-read profile. No fake capture or altered capability registry is used.
        capture = definition.model_copy(
            update={
                "package_key": "qt003-capture-negative",
                "steps": (
                    *definition.steps,
                    MethodStep(
                        id="capture",
                        title="Capture negative control",
                        instruction_kind="snapshot_capture",
                        blocking=True,
                        required_capabilities=("snapshot_read",),
                        required_outputs=(
                            SnapshotFieldRequirement(type="snapshot_field", field="roe"),
                        ),
                    ),
                ),
            }
        )
        async with session_scope() as session:
            saved = await MethodService(session).save_package(
                capture, source="QT003 synthetic negative control"
            )
            override = MethodBinding(
                schema_version=2,
                method_ref=FIXED_METHOD,
                listing_ref=subject,
                config=capture.discipline,
                package_ref=ObjectRef(
                    kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"]
                ),
                package_hash=saved["content_hash"],
            )
            task_count = await session.scalar(select(func.count()).select_from(AgentTask))
        with pytest.raises(ExecutionDeniedError, match="method_capability_unavailable"):
            async with session_scope() as session:
                await admit(
                    session,
                    base=selected,
                    input_key=f"qt003-capture:{uuid4()}",
                    binding=ContextBinding(
                        context_refs=(subject,), base_refs=(subject,), as_of=datetime.now(UTC)
                    ),
                    grant=ExecutionGrant(
                        mode="interactive",
                        source="authenticated_message",
                        prompt="Synthetic saved-read negative control",
                        scope_refs=(subject,),
                        tool_names=("save_research_progress", "get_evidence"),
                        research_kind="company_research",
                    ),
                    method_override=override,
                )
        async with session_scope() as session:
            assert await session.scalar(select(func.count()).select_from(AgentTask)) == task_count
            assert await session.scalar(select(func.count()).select_from(MethodHead)) == 0
            assert await session.scalar(select(func.count()).select_from(MethodDefaultHead)) == 0
            assert (
                await MethodService(session).resolve_binding(subject, "company_research")
            ).method_ref == FIXED_METHOD
            assert len(set(await session.scalars(select(AgentModelRun.config_version)))) == 1
            assert not await session.scalar(
                select(BudgetEntry.id).where(BudgetEntry.state.in_(("unknown", "released")))
            )
