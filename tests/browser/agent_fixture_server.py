"""Isolated AG17 browser fixture. Never launch against an existing database."""
import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import uvicorn
from fastapi import Depends
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import update

from investment_agent.agent.contracts import WaitRequirement
from investment_agent.agent.conversations import ConversationService
from investment_agent.agent.executor import AgentExecutor, RunSpecification, TaskDisposition
from investment_agent.agent.job import AgentJobHandler
from investment_agent.agent.research import registrations
from investment_agent.agent.tasks import TaskService
from investment_agent.api.app import create_app
from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.config import Settings
from investment_agent.investor.enums import StopReason
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models import (  # noqa: F401
    disclosure,
    discovery,
    monitoring,
    portfolio,
    portfolio_plan,
    relationship,
)
from investment_agent.storage.models.agent import AgentTask, PublicTaskEvent
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease


async def main():
    target = await asyncio.to_thread(Path(os.environ["AG17_FIXTURE_DIR"]).resolve)
    if not str(target).startswith("/private/var/folders/") and not str(target).startswith("/tmp/"):
        raise RuntimeError("AG17 fixture requires a system temporary directory")
    db = target / "browser.db"
    if db.exists():
        raise RuntimeError("fixture refuses existing database")
    os.environ.update(API_KEY="ag17-fixture-key", LLM_TIER3_API_KEY="inert", DATA_DIR=str(target),
        DATABASE_URL=f"sqlite+aiosqlite:///{db}", INVESTOR_AGENT_ENABLED="true",
        AGENT_API_KEY="inert", AGENT_BASE_URL="https://fixture.invalid/v1", AGENT_MODEL="fixture")
    Settings.model_config["env_file"] = None
    engine = init_engine(os.environ["DATABASE_URL"])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        session.add(Market(code="SSE", name="fixture", timezone="Asia/Shanghai", currency="CNY"))
        company = Company(canonical_name="fixture 同名 <img src=x onerror=alert(1)>")
        session.add(company)
        await session.flush()
        session.add_all([Listing(id=77, company_id=company.id, market_code="SSE", ticker="600001"),
                         Listing(id=78, company_id=company.id, market_code="SSE", ticker="600002")])
    if os.environ.get("AG19_FIXTURE") == "true":
        from cryptography.fernet import Fernet
        os.environ["INVESTOR_AGENT_ENABLED"] = "false"
        os.environ["SETTINGS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
    base = Settings(_env_file=None, api_key="ag17-fixture-key", investor_agent_enabled=os.environ.get("AG19_FIXTURE") != "true",
        agent_api_key="inert", agent_base_url="https://fixture.invalid/v1", agent_model="fixture",
        agent_max_tokens=100, investor_agent_task_tokens=100000, investor_agent_daily_tokens=500000)

    async def complete(session, execution, outcome):
        await ConversationService(session, base).save_assistant(execution,
            text="确定性公开回答 <img src=x onerror=alert(1)>；仅回答本问题。")
        return TaskDisposition("succeeded", StopReason.ANSWERED)

    model = FunctionModel(lambda messages, info: ModelResponse([TextPart('{"summary":"private draft"}')],
        usage=RequestUsage(input_tokens=10, output_tokens=2)))
    executor = AgentExecutor(base, model=model, registrations=registrations(), specification=RunSpecification(completion=complete))
    worker = ResearchWorker(settings=base, worker_id="browser-fixture",
        handlers=[AgentJobHandler("agent_turn", executor)])
    app = create_app(settings=base)

    @app.post("/fixture/run", dependencies=[Depends(verify_api_key_or_session)])
    async def run():
        await worker.run_once()
        return {"done": True}

    @app.post("/fixture/ag26", dependencies=[Depends(verify_api_key_or_session)])
    async def ag26():
        import sys

        sys.path.insert(0, str(Path.cwd()))
        from tests.test_agent_research import (
            ResearchOutput,
            message,
            new_conversation,
            progress,
            response,
            run,
            submit,
        )
        from tests.test_original_parsing import html, original, snapshot

        from investment_agent.evidence.documents import parse_staged, save_candidates
        from investment_agent.investor.common import ObjectRef

        body = html().replace(b"</body>", ("<p>&lt;img src=x onerror=alert(1)&gt;</p>" + "".join(f"<p>原文段落 {n}：保存文本，不是模型指令。</p>" for n in range(20)) + "</body>").encode())
        async with session_scope() as session:
            payload, source, request = await original(session, body=body)
            await snapshot(session, request.listing_id)
            saved = await save_candidates(session, payload, source, await parse_staged(source, "text/html"), request)
            fact = ObjectRef.model_validate(saved["fact_refs"][0])
            subject = ObjectRef(kind="listing", id=request.listing_id, revision=1)
        conversation = await new_conversation()
        receipt = await submit(conversation, message(text="核查已保存的合并利润表原文", context_refs=(subject,), research_kind="local_check"))
        output = ResearchOutput(progress=progress(kind="local_check", question="核查原文出处与单位", fact_refs=(fact,), gaps=("仅支持明确年度与合并口径；扫描和复杂表格待补。",)))
        task = await run(receipt, FunctionModel(lambda messages, info: response(output)))
        return {"task": task.model_dump(mode="json"), "fact": fact.model_dump(mode="json"), "original_id": str(request.original_id)}

    @app.post("/fixture/ag18", dependencies=[Depends(verify_api_key_or_session)])
    async def ag18():
        import runpy
        import sys

        sys.path.insert(0, str(Path.cwd()))
        from investment_agent.evidence.contracts import CalculationRecord, EvidenceRecord
        from investment_agent.investor.common import Quantity
        from investment_agent.storage.models.discovery import DiscoveryBatch
        from investment_agent.storage.models.evidence import SavedCalculation, SavedEvidence
        from investment_agent.storage.models.investor import InvestorCase
        from investment_agent.storage.models.monitoring import InvestorNotification, ReviewCycle
        helpers = runpy.run_path("tests/test_investor_publication.py")
        candidate, fact, subject = await helpers["fixture"]()
        first = await helpers["publish"](candidate)
        candidate = candidate.model_copy(update={"thesis": candidate.thesis.model_copy(update={"previous_ref": first.thesis_ref, "case_ref": candidate.thesis.case_ref.model_copy(update={"revision": candidate.thesis.case_ref.revision + 1})})})
        second = await helpers["publish"](candidate)
        assert second.published, second.check
        research_helpers = runpy.run_path("tests/test_agent_research.py")
        conversation = await research_helpers["new_conversation"]()
        receipt = await research_helpers["submit"](conversation, research_helpers["message"](
            text="核查现金来源", context_refs=(subject,), research_kind="local_check"))
        progress = research_helpers["progress"](kind="local_check", question="现金来源是否可靠", content=candidate.recommendation.claims[0].text,
            fact_refs=(fact,), evidence_steps=("核对原始现金口径",), completed_steps=("读取保存的现金证据",), gaps=("仍需现金流附注",))
        output = research_helpers["ResearchOutput"](progress=progress)
        await research_helpers["run"](receipt, FunctionModel(lambda messages, info: research_helpers["response"](output)))
        case_id = str(candidate.recommendation.case_ids[0])
        calc_id, batch_id = uuid4(), uuid4()
        async with session_scope() as session:
            original = await session.get(SavedEvidence, (str(fact.id), 1))
            corrected = EvidenceRecord.model_validate(original.payload).model_copy(update={
                "id": uuid4(), "created_at": datetime.now(UTC), "published_at": datetime.now(UTC),
                "acquired_at": datetime.now(UTC), "available_from": datetime.now(UTC),
                "value": Quantity(value="99", unit="亿元", currency="CNY"), "correction_refs": (fact,)})
            session.add(SavedEvidence(id=str(corrected.id), revision=1, owner_id="local_owner", payload=corrected.model_dump(mode="json")))
            record = CalculationRecord(**helpers["metadata"](), method="reverse_dcf_fcfe", method_version="v1",
                fact_refs=(fact,), assumption_refs=(), input_hash="fixture-missing-fcfe", as_of=datetime.now(UTC),
                applicable=False, status="missing_inputs", results={}, errors=("unverified_fcfe",),
                details={"qualification": "真实覆盖0/4，资格未启用", "inputs": {"debt": None}})
            record = record.model_copy(update={"id": calc_id})
            session.add(SavedCalculation(id=str(calc_id), revision=1, owner_id="local_owner", payload=record.model_dump(mode="json")))
            case_row = await session.get(InvestorCase, case_id)
            case_row.source_refs = [*case_row.source_refs, {"kind": "calculation", "id": str(calc_id), "revision": 1}]
            session.add(InvestorNotification(key="ag18-fixture-dedup", owner_id="local_owner", case_id=case_id,
                revision=1, payload={"category": "major_change", "stage": "review_required", "reasons": ["现金来源更正 <img src=x onerror=alert(1)>"],
                "read": False, "recommendation_ref": first.recommendation_ref.model_dump(mode="json")}))
            session.add(ReviewCycle(id=str(uuid4()), case_id=case_id, task_id=None, state="blocked",
                payload={"checked_at": datetime.now(UTC).isoformat(), "pending_reason": "background_paused", "result_refs": []}))
            session.add(DiscoveryBatch(id=str(batch_id), owner_id="local_owner", task_id=None, state="selection_completed",
                payload={"summary": "有限短名单已选择，研究未完成", "leads": [{"listing_ref": subject.model_dump(mode="json"),
                "status": "blocked", "processing_reason": "background_paused", "reason": "现金问题需要核查",
                "reevaluate_when": "恢复后台并获得核验材料", "sources": [fact.model_dump(mode="json")]}]}))
        return {"recommendations": [first.recommendation_ref.model_dump(mode="json"), second.recommendation_ref.model_dump(mode="json")],
            "case_id": case_id, "fact": fact.model_dump(mode="json"), "calculation": {"kind":"calculation","id":str(calc_id),"revision":1},
            "batch_id": str(batch_id), "progress_task_id": str(receipt.task.id)}

    claims = {}

    @app.post("/fixture/claim/{task_id}", dependencies=[Depends(verify_api_key_or_session)])
    async def claim(task_id: UUID):
        async with session_scope() as session:
            row = await session.get(AgentTask, str(task_id))
            run = await PipelineRunRepository(session).claim_next(worker_id="fixture-held", run_id=row.run_id)
            claims[task_id] = await TaskService(session).claim(task_id,
                RunLease(run.id, "fixture-held", run.attempt_count))
        return {"done": True}

    @app.post("/fixture/ack/{task_id}", dependencies=[Depends(verify_api_key_or_session)])
    async def ack(task_id: UUID):
        async with session_scope() as session:
            await TaskService(session).ack_cancel(claims[task_id])
        return {"done": True}

    @app.post("/fixture/wait/{task_id}", dependencies=[Depends(verify_api_key_or_session)])
    async def wait(task_id: UUID):
        async with session_scope() as session:
            await TaskService(session).checkpoint(claims[task_id], checkpoint={}, waiting=WaitRequirement(
                required_input="fixture 等待材料", dependency_ids=(uuid4(),),
                deadline=datetime.now(UTC)+timedelta(minutes=5)))
        return {"done": True}

    @app.post("/fixture/expire/{task_id}", dependencies=[Depends(verify_api_key_or_session)])
    async def expire(task_id: UUID):
        async with session_scope() as session:
            await session.execute(update(PublicTaskEvent).where(PublicTaskEvent.task_id == str(task_id))
                .values(occurred_at=datetime.now(UTC)-timedelta(days=31)))
            await TaskService(session).retain_events()
        return {"done": True}

    await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=int(os.environ["AG17_FIXTURE_PORT"]),
        lifespan="off", log_level="warning")).serve()


if __name__ == "__main__":
    asyncio.run(main())
