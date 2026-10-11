"""Investor-specific query and compare-and-swap operations; caller owns transaction."""

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.models.investor import (
    CaseRevision,
    InvestorCase,
    MandateHead,
    MandateRevision,
)


class InvestorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def mandate(self, owner: str, revision: int | None = None) -> MandateRevision | None:
        head = await self.session.get(MandateHead, owner, populate_existing=True)
        if head is None:
            return None
        return await self.session.get(MandateRevision, (head.mandate_id, revision or head.revision))

    async def case(self, owner: str, case_id: UUID) -> InvestorCase | None:
        result = await self.session.execute(
            select(InvestorCase)
            .where(InvestorCase.owner_id == owner, InvestorCase.id == str(case_id))
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def cases(
        self,
        owner: str,
        listing_id: int | None = None,
        *,
        after: UUID | None = None,
        limit: int = 100,
        status: str | None = None,
    ) -> list[InvestorCase]:
        query = (
            select(InvestorCase)
            .where(InvestorCase.owner_id == owner)
            .execution_options(populate_existing=True)
        )
        if status is not None:
            query = query.where(InvestorCase.payload["status"].as_string() == status)
        if listing_id is not None:
            query = query.where(InvestorCase.listing_id == listing_id)
        if after is not None:
            query = query.where(InvestorCase.id > str(after))
        return list(await self.session.scalars(query.order_by(InvestorCase.id).limit(limit)))

    async def advance_mandate(self, owner: str, expected: int) -> bool:
        result = await self.session.execute(
            update(MandateHead)
            .where(MandateHead.owner_id == owner, MandateHead.revision == expected)
            .values(revision=expected + 1)
        )
        return bool(result.rowcount)  # type: ignore[attr-defined]

    async def advance_case(
        self, row: InvestorCase, expected: int, payload: dict[str, object]
    ) -> bool:
        result = await self.session.execute(
            update(InvestorCase)
            .where(InvestorCase.id == row.id, InvestorCase.revision == expected)
            .values(revision=expected + 1, payload=payload)
        )
        changed = bool(result.rowcount)  # type: ignore[attr-defined]
        if changed:
            self.session.add(CaseRevision(case_id=row.id, revision=expected + 1, payload=payload))
        return changed
