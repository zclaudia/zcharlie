"""Generic repository base."""

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.models.base import Base


class Repository[T: Base]:
    model: type[T]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, ident: int | str) -> T | None:
        return await self.session.get(self.model, ident)
