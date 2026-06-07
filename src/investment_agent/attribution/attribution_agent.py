"""Attribution Agent: Pydantic-AI Agent loop with 5 attribution tools.

Built once per pipeline run; reused across listings. System prompt is rendered
per call (depends on listing + beta) and prepended to the user message.
"""

from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.attribution.tools import (
    get_peer_returns,
    get_recent_earnings,
    get_recent_news,
    get_recent_ratings,
    get_related_company_events,
)
from investment_agent.config import LLMTierConfig
from investment_agent.domain.strategy_types import (
    AttributionContext,
    AttributionResult,
)
from investment_agent.llm.agent_factory import build_agent
from investment_agent.strategies.prompt_loader import PromptLoader

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROMPTS_DIR = PROJECT_ROOT / "prompts"


class AttributionAgent:
    """Wraps a Pydantic-AI Agent with the 5 attribution tools registered.

    The agent's system prompt is empty at construction; the per-call rendered
    prompt is injected as the first part of the user message via run_for_context().
    """

    def __init__(
        self,
        *,
        tier3_config: LLMTierConfig,
        prompts_dir: Path = DEFAULT_PROMPTS_DIR,
    ) -> None:
        self._prompt_loader = PromptLoader(prompts_dir)
        self._agent: Any = build_agent(
            config=tier3_config,
            system_prompt="",
            output_type=AttributionResult,
        )
        self._register_tools()

    def _register_tools(self) -> None:
        @self._agent.tool  # type: ignore[untyped-decorator]
        async def _tool_get_recent_news(
            ctx: RunContext[AsyncSession], company_id: int, days: int = 7,
        ) -> list[dict[str, object]]:
            """Recent news headlines for the given company (last `days` days)."""
            return await get_recent_news(ctx.deps, company_id=company_id, days=days)

        @self._agent.tool  # type: ignore[untyped-decorator]
        async def _tool_get_recent_ratings(
            ctx: RunContext[AsyncSession], company_id: int, days: int = 14,
        ) -> list[dict[str, object]]:
            """Recent analyst rating changes for the given company."""
            return await get_recent_ratings(
                ctx.deps, company_id=company_id, days=days,
            )

        @self._agent.tool  # type: ignore[untyped-decorator]
        async def _tool_get_recent_earnings(
            ctx: RunContext[AsyncSession], company_id: int, periods: int = 3,
        ) -> list[dict[str, object]]:
            """Most-recent financial reports for the given company."""
            return await get_recent_earnings(
                ctx.deps, company_id=company_id, periods=periods,
            )

        @self._agent.tool  # type: ignore[untyped-decorator]
        async def _tool_get_related_company_events(
            ctx: RunContext[AsyncSession], company_id: int,
            days: int = 7, max_companies: int = 5,
        ) -> list[dict[str, object]]:
            """Recent news from strongly-related companies (suppliers, competitors, partners)."""
            return await get_related_company_events(
                ctx.deps,
                company_id=company_id,
                days=days,
                max_companies=max_companies,
            )

        @self._agent.tool  # type: ignore[untyped-decorator]
        async def _tool_get_peer_returns(
            ctx: RunContext[AsyncSession], listing_id: int,
            days: int = 1, limit: int = 5,
        ) -> list[dict[str, object]]:
            """Same-sector peer returns over a trailing window."""
            return await get_peer_returns(
                ctx.deps, listing_id=listing_id, days=days, limit=limit,
            )

    async def run_for_context(
        self,
        *,
        session: AsyncSession,
        context: AttributionContext,
        as_of: str,
    ) -> AttributionResult:
        """Render system prompt + run agent loop until AttributionResult returned."""
        system_prompt = self._prompt_loader.render(
            "attribution/attribution",
            listing=context.listing.model_dump(),
            beta=context.beta.model_dump(),
            as_of=as_of,
        )
        user_msg = (
            system_prompt
            + "\n\n# Task\nInvestigate now and return an AttributionResult."
        )
        result = await self._agent.run(user_msg, deps=session)
        output = result.output
        assert isinstance(output, AttributionResult)
        return output
