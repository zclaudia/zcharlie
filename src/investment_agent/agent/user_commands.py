"""Finite exact user commands; model suggestions cannot mint this authority."""
import re
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.contracts import Message
from investment_agent.agent.conversations import ConversationService
from investment_agent.agent.tasks import TaskService
from investment_agent.config import Settings
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.controls import (
    CaseAction,
    ExecuteRequest,
    MemoryAction,
    Operation,
    UserControlService,
    WatchlistAddAction,
    authenticated_message,
)
from investment_agent.investor.enums import StopReason
from investment_agent.storage.models.agent import AgentTask
from investment_agent.storage.models.investor import InvestorCase
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease

_WATCHLIST_ADD = re.compile(r"^把\s*(?P<ticker>\S{1,12})\s*加入观察池$")


async def command(session: AsyncSession, text: str, scope_refs: tuple[ObjectRef, ...], source: ObjectRef) -> Operation | None:
    value = text.strip().rstrip("。.!！")
    actions = {"暂停这家公司的后台研究": "pause", "恢复这家公司的后台研究": "resume",
        "归档这家公司": "archive", "静音这家公司": "mute", "取消这家公司的静音": "unmute"}
    if value in actions:
        ids = [int(r.id) for r in scope_refs if r.kind == "listing"]
        cases = (await session.scalars(select(InvestorCase).where(InvestorCase.owner_id == "local_owner", InvestorCase.listing_id.in_(ids)))).all()
        if len(cases) != 1:
            return None
        row = cases[0]
        return CaseAction(case_ref=ObjectRef(kind="case", id=UUID(row.id), revision=row.revision), action=actions[value], reason=value)  # type: ignore[arg-type]
    prefix = "记住我的偏好："
    if value.startswith(prefix) and value[len(prefix):].strip():
        return MemoryAction(kind="explicit_preference", text=value[len(prefix):].strip(), source_refs=(source,))
    match = _WATCHLIST_ADD.fullmatch(value)
    if match is not None:
        from investment_agent.domain.a_share import parse_a_share_ticker
        from investment_agent.storage.repositories.listing import ListingRepository
        from investment_agent.storage.repositories.stock_directory import StockDirectoryRepository

        parsed = parse_a_share_ticker(match.group("ticker"))
        if parsed is None:
            return None  # not a canonical code; the model turn answers instead
        symbol, market = parsed
        canonical = f"{symbol}.{market.suffix}"
        company_name: str | None = None
        if await ListingRepository(session).get_by_ticker(market.market_code, symbol) is None:
            # The local directory is the only name source for a new listing; an
            # unknown code is not a command and falls back to the model turn.
            rows = await StockDirectoryRepository(session).search(canonical, limit=5)
            company_name = next((row.name for row in rows if row.ticker == canonical), None)
            if company_name is None:
                return None
        return WatchlistAddAction(ticker=canonical, company_name=company_name)
    return None


async def execute_clear_command(session: AsyncSession, message: Message, base: Settings, scope_refs: tuple[ObjectRef, ...]) -> bool:
    """One authenticated transaction, no paid run or hidden automatic rerun.

    Save the fixed acknowledgement before the authorized write so memory/case
    mutation cannot be used to continue an old model grant. Both roll back if
    the write fails. Checkpoint is terminal; no model boundary follows.
    """
    assert message.task_ref is not None
    source = ObjectRef(kind="message", id=message.id, revision=1)
    parameters = await command(session, message.text, scope_refs, source)
    if parameters is None:
        return False
    task_id = UUID(str(message.task_ref.id))
    row = await session.get(AgentTask, str(task_id))
    assert row is not None
    run = await PipelineRunRepository(session).claim_next(worker_id="authenticated-controller", run_id=row.run_id)
    if run is None:
        return False
    claim = await TaskService(session).claim(task_id, RunLease(run.id, "authenticated-controller", run.attempt_count))
    assert claim is not None
    authority = await authenticated_message(session, source, parameters)
    await ConversationService(session, base).save_assistant(claim, text="已执行这条明确的用户指令。")
    result = await UserControlService(session).execute(ExecuteRequest(parameters=parameters,
        idempotency_key=uuid5(message.id, "clear-command")), authority)
    await TaskService(session).checkpoint(claim, checkpoint={"controller_result": result.model_dump(mode="json"),
        "completed_at": datetime.now(UTC).isoformat(), "requires_fresh_context_for_next_task": True},
        terminal="succeeded", stop_reason=StopReason.ANSWERED)
    return True
