"""MCP tools ``list_candidates`` / ``get_candidate`` / ``get_rule``."""

from __future__ import annotations

from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from investment_agent.mcp_server.plumbing import (
    CANDIDATES_LIMIT_DEFAULT,
    LIMIT_MAX,
    ServerContext,
)


def register_candidates(server: MCPServer, ctx: ServerContext) -> None:
    """Register the candidate and rule tools."""

    @server.tool()
    async def list_candidates(
        status: Annotated[
            str | None,
            Field(pattern=r"^(open|promoted|dismissed|expired)$", description="Default: all"),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=LIMIT_MAX)] = CANDIDATES_LIMIT_DEFAULT,
    ) -> dict[str, Any]:
        """Candidates newest first (sources, latest screen reasons, draft state),
        as ``GET /candidates`` returns them."""

        from investment_agent.api.views import TemplateCache, candidate_summary_out
        from investment_agent.clock import shanghai_today
        from investment_agent.storage.repositories.candidate import CandidateRepository

        async with ctx.session() as session:
            rows, total = await CandidateRepository(session).page(
                status=status, limit=limit, offset=0
            )
            templates = TemplateCache(session)
            items = [await candidate_summary_out(session, row, templates) for row in rows]
        return ctx.json({"as_of": shanghai_today(), "status": status, "total": total, "items": items})

    @server.tool()
    async def get_candidate(candidate_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """One candidate with its screen reasons, draft rule (rationale and
        parameters), decision log and main-business segments."""

        from investment_agent.api.views import candidate_detail_out
        from investment_agent.storage.repositories.candidate import CandidateRepository

        async with ctx.session() as session:
            candidate = await CandidateRepository(session).get(candidate_id)
            if candidate is None:
                raise ToolError(f"Candidate {candidate_id} not found")
            detail = await candidate_detail_out(session, candidate)
        return ctx.json(detail)

    @server.tool()
    async def get_rule(rule_id: Annotated[int, Field(ge=1)]) -> dict[str, Any]:
        """One rule instance: template reference, parameters with their ranges,
        zones, status and draft rationale."""

        from investment_agent.api.views import TemplateCache, rule_detail_out
        from investment_agent.storage.models.rules import RuleInstanceRecord

        async with ctx.session() as session:
            rule = await session.get(RuleInstanceRecord, rule_id)
            if rule is None:
                raise ToolError(f"Rule {rule_id} not found")
            detail = await rule_detail_out(rule, TemplateCache(session))
        return ctx.json(detail)
