"""Card 05: assemble injects the frozen rendered_method for v2 tasks and keeps
every v1 company instruction byte-identical (design §6.1 item 3, §11 M1).

The v1 snapshot tests are hard byte-equality assertions (``==``, not ``in``)
against ``COMPANY_INSTRUCTIONS`` / ``COMPANY_REVIEW_INSTRUCTIONS`` through the
real admission path. The v2 test builds a global default head from the baseline
package, freezes a receipt at admission and asserts the exact
``instructions + "\\n\\n" + receipt.rendered_method`` shape plus the new
``method_package`` context key. Non-company kinds never inject, even when a
receipt row is manufactured directly onto the task.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelResponse, ThinkingPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from investment_agent.agent.company_context import COMPANY_INSTRUCTIONS, COMPANY_REVIEW_INSTRUCTIONS
from investment_agent.agent.method_receipt import VALIDATOR_RENDER_ONLY, VALIDATOR_V1, build_receipt
from investment_agent.agent.research import METHOD_INSTRUCTIONS, ResearchOutput, assemble
from investment_agent.agent.research_publication import CompanyViewInput
from investment_agent.agent.tasks import TaskClaim
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.method_packages import LEGACY_BASELINE_DEFINITION
from investment_agent.investor.methods import (
    ActivateGlobal,
    MethodBinding,
    MethodConfig,
    MethodService,
    task_method_receipt,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.investor import TaskMethodBinding
from tests.support.conversations import (
    base,
    claim,
    message,
    new_conversation,
    submit,
)
from tests.support.conversations import (
    conversation_db as conversation_db,
)
from tests.support.publication import fixture, publish
from tests.support.research import full_progress, response, run

CONTEXT_SEPARATOR = "\nTrusted current context:\n"


def _context(rendered: str) -> tuple[str, dict[str, Any]]:
    """Split one assemble result into the instruction head and the context dict."""
    head, separator, remainder = rendered.partition(CONTEXT_SEPARATOR)
    assert separator, "assemble must return instructions followed by the trusted context"
    return head, json.JSONDecoder().raw_decode(remainder)[0]


async def _saved_baseline(**overrides: Any) -> dict[str, Any]:
    async with session_scope() as session:
        return await MethodService(session).save_package(
            LEGACY_BASELINE_DEFINITION.model_copy(update=overrides), source="context-tests"
        )


async def _activate(saved: dict[str, Any]) -> None:
    async with session_scope() as session:
        await MethodService(session).activate_global(
            ActivateGlobal(
                mutation_id=uuid4(),
                package_ref=ObjectRef(kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"]),
                expected_revision=0,
                reason="Enable the reviewed baseline package",
            ),
            actor="authenticated_user",
        )


async def _assemble(owned: TaskClaim) -> str:
    async with session_scope() as session:
        rendered, _, _ = await assemble(session, owned, base(), {})
    return rendered


async def test_v1_company_research_instructions_are_byte_identical(conversation_db) -> None:
    subject = (await fixture())[2]
    receipt = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    owned = await claim(receipt.task)
    head, context = _context(await _assemble(owned))
    assert head == COMPANY_INSTRUCTIONS
    assert "Method package" not in head
    assert "method_package" not in context


async def test_v1_review_reuse_candidate_instructions_are_byte_identical(conversation_db) -> None:
    candidate, _, subject = await fixture()
    assert (await publish(candidate)).published
    conversation = await new_conversation()
    question = "这家公司为什么值得关注？"
    partial = full_progress(candidate.thesis.assessed_dimensions, answered=False, stopped_because="Source coverage remains incomplete")
    first = await submit(conversation, message(text=question, context_refs=(subject,), research_kind="company_research"))
    assert (await run(first, FunctionModel(lambda messages, info: response(ResearchOutput(progress=partial))))).status == "partial"
    turns = 0

    def prepare_only(messages: Any, info: Any) -> Any:
        nonlocal turns
        turns += 1
        if turns == 1:
            context = json.JSONDecoder().raw_decode(info.instructions.split(CONTEXT_SEPARATOR, 1)[1])[0]
            case = context["company_context"][0]["case"]
            business = CompanyViewInput(case_ref=ObjectRef(kind="case", id=case["id"], revision=case["revision"]),
                question_scope="Bounded whole company study", change_reason="Recheck owned sources",
                assessed_dimensions=candidate.thesis.assessed_dimensions, claims=candidate.thesis.claims,
                counter_case=candidate.thesis.counter_case, gaps=candidate.recommendation.gaps,
                dependencies=candidate.recommendation.dependency_refs, business_view="undetermined",
                price_view="undetermined", sufficiency="bounded")
            return ModelResponse([ThinkingPart("PRIVATE_REASONING_MUST_NOT_BE_IMPORTED"),
                ToolCallPart("prepare_company_view", {"business": business.model_dump(mode="json")})])
        return response(ResearchOutput(progress=partial))

    middle = await submit(conversation, message(text=question, context_refs=(subject,), research_kind="company_research"))
    assert (await run(middle, FunctionModel(prepare_only))).status == "partial"
    third = await submit(conversation, message(text=question, context_refs=(subject,), research_kind="company_research"))
    owned = await claim(third.task)
    head, context = _context(await _assemble(owned))
    assert head == COMPANY_REVIEW_INSTRUCTIONS
    assert "Method package" not in head
    assert "method_package" not in context
    assert context["reused_research_plan"]["prepared_review"]


async def test_v2_receipt_appends_rendered_method_and_context_key(conversation_db) -> None:
    subject = (await fixture())[2]
    saved = await _saved_baseline()
    await _activate(saved)
    receipt = await submit(await new_conversation(), message(text="深入研究", context_refs=(subject,), research_kind="company_research"))
    owned = await claim(receipt.task)
    async with session_scope() as session:
        frozen = await task_method_receipt(session, receipt.task.id)
        rendered, _, _ = await assemble(session, owned, base(), {})
    assert frozen is not None
    head, context = _context(rendered)
    assert head == COMPANY_INSTRUCTIONS + "\n\n" + frozen.rendered_method
    assert context["method_package"] == {"package_key": saved["package_key"], "version": saved["version"],
        "package_hash": saved["content_hash"], "validator_version": VALIDATOR_V1}


@pytest.mark.parametrize("kind", ["explanation", "local_check", "comparison"])
async def test_non_company_kinds_never_inject_rendered_method(conversation_db, kind) -> None:
    subject = (await fixture())[2]
    saved = await _saved_baseline()
    await _activate(saved)
    receipt = await submit(await new_conversation(), message(text="解释经营研究方法", context_refs=(subject,), research_kind=kind))
    owned = await claim(receipt.task)
    async with session_scope() as session:
        assert await task_method_receipt(session, receipt.task.id) is None
        rendered, _, _ = await assemble(session, owned, base(), {})
    head, context = _context(rendered)
    assert head == METHOD_INSTRUCTIONS.split("For explicitly admitted portfolio_action")[0]
    assert "Method package" not in head
    assert "method_package" not in context


async def test_manufactured_receipt_on_non_company_task_still_does_not_inject(conversation_db) -> None:
    subject = (await fixture())[2]
    saved = await _saved_baseline()
    await _activate(saved)
    receipt = await submit(await new_conversation(), message(text="解释经营研究方法", context_refs=(subject,), research_kind="explanation"))
    owned = await claim(receipt.task)
    binding = MethodBinding(schema_version=2, method_ref=ObjectRef(kind="method", id=uuid4(), revision=1),
        config=MethodConfig.model_validate(saved["discipline"]),
        package_ref=ObjectRef(kind="package", id=UUID(saved["artifact_id"]), revision=saved["version"]),
        package_hash=saved["content_hash"])
    manufactured = build_receipt(binding, LEGACY_BASELINE_DEFINITION, validator_version=VALIDATOR_RENDER_ONLY)
    async with session_scope() as session:
        # The storage layer rightly refuses receipt rewrites; a corrupt v2 row
        # must enter directly (same adversarial pattern as the corrupted
        # package artifact test) to prove the assemble layer refuses injection.
        await session.execute(
            sa.delete(TaskMethodBinding).where(TaskMethodBinding.task_id == str(receipt.task.id)).execution_options(synchronize_session=False))
        session.add(TaskMethodBinding(task_id=str(receipt.task.id), payload={"schema_version": 2, "receipt": manufactured.model_dump(mode="json")}))
        rendered, _, _ = await assemble(session, owned, base(), {})
    head, context = _context(rendered)
    assert head == METHOD_INSTRUCTIONS.split("For explicitly admitted portfolio_action")[0]
    assert "Method package company-research" not in head
    assert "method_package" not in context
