"""LLMCall repository — admin audit queries."""

from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import func, select

from investment_agent.storage.models.ops import LLMCall
from investment_agent.storage.repositories.base import Repository


class LLMCallRepository(Repository[LLMCall]):
    model = LLMCall

    async def list_recent(
        self,
        *,
        limit: int = 100,
        since: datetime | None = None,
    ) -> list[LLMCall]:
        stmt = select(LLMCall).order_by(LLMCall.started_at.desc()).limit(limit)
        if since is not None:
            stmt = stmt.where(LLMCall.started_at >= since)
        return list((await self.session.execute(stmt)).scalars().all())

    async def cost_summary(
        self,
        *,
        month_start: date,
    ) -> dict[str, object]:
        """Return total cost + breakdown by purpose since `month_start` (inclusive)."""
        boundary = datetime.combine(month_start, time.min)

        total_stmt = select(
            func.count(LLMCall.id).label("n_calls"),
            func.coalesce(func.sum(LLMCall.cost_usd), 0).label("total_cost"),
            func.coalesce(func.sum(LLMCall.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(LLMCall.completion_tokens), 0).label(
                "completion_tokens"
            ),
        ).where(LLMCall.started_at >= boundary)
        total_row = (await self.session.execute(total_stmt)).one()

        bp_stmt = (
            select(
                LLMCall.purpose,
                func.count(LLMCall.id).label("n_calls"),
                func.coalesce(func.sum(LLMCall.cost_usd), 0).label("cost"),
            )
            .where(LLMCall.started_at >= boundary)
            .group_by(LLMCall.purpose)
            .order_by(func.sum(LLMCall.cost_usd).desc())
        )
        bp_rows = list((await self.session.execute(bp_stmt)).all())

        return {
            "month_start": month_start.isoformat(),
            "total_calls": int(total_row.n_calls),
            "total_cost_usd": float(Decimal(str(total_row.total_cost))),
            "total_prompt_tokens": int(total_row.prompt_tokens),
            "total_completion_tokens": int(total_row.completion_tokens),
            "by_purpose": [
                {
                    "purpose": r.purpose,
                    "n_calls": int(r.n_calls),
                    "cost_usd": float(Decimal(str(r.cost))),
                }
                for r in bp_rows
            ],
        }
