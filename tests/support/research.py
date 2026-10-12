"""Research progress constructors and the native run helper (moved from tests/test_agent_research.py)."""

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.usage import RequestUsage

from investment_agent.agent.executor import AgentExecutor
from investment_agent.agent.job import AgentJobHandler
from investment_agent.agent.research import ResearchPolicy, ResearchProgress, registrations
from investment_agent.agent.tasks import TaskService
from investment_agent.investor.contracts import TextNode
from investment_agent.research.worker import ResearchWorker
from investment_agent.storage.database import session_scope
from tests.support.conversations import base


def progress(kind="explanation", **updates):
    data = dict(kind=kind, question="Explain the bounded question", content=(TextNode(type="text", text="The available material has limits."),), answered=True,
        limitations=("This is a bounded answer, not a company-wide judgment.",))
    data.update(updates)
    return ResearchProgress(**data)


def response(output):
    return ModelResponse([TextPart(output.model_dump_json())], usage=RequestUsage(input_tokens=20, output_tokens=10))


async def run(receipt, model, settings=None, run_id=None):
    selected = settings or base()
    executor = AgentExecutor(selected, model=model, registrations=registrations(), specification=ResearchPolicy(selected).specification())
    worker = ResearchWorker(settings=selected, worker_id="research-fixture", handlers=[AgentJobHandler("agent_turn", executor)])
    await worker.run_once(run_id=run_id)
    async with session_scope() as session:
        return await TaskService(session).get_task(receipt.task.id)


def full_progress(dimensions, **updates):
    from investment_agent.agent.research import ResearchHypothesis
    return progress(kind="company_research", hypotheses=(ResearchHypothesis(question="Is cash durable?", explanation="Operating cash may recur", alternative="Temporary working capital release", falsification_condition="Cash reverses without operating growth", evidence_priority="Check cash components first"),),
        evidence_steps=("Check reported cash and opposing explanations",), completion_criteria=("Explain scope and material gaps",),
        stop_conditions=("Stop when critical source unavailable",), dimensions=dimensions, completed_steps=("Clarified scope and alternatives",), **updates)
