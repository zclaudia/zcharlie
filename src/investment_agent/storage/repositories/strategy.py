"""Strategy repository — DB mirror of strategies/*.yaml."""

from sqlalchemy import select

from investment_agent.storage.models.analysis import Strategy
from investment_agent.storage.repositories.base import Repository


class StrategyRepository(Repository[Strategy]):
    model = Strategy

    async def list_all(self) -> list[Strategy]:
        stmt = select(Strategy).order_by(Strategy.code)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_enabled(self) -> list[Strategy]:
        stmt = (
            select(Strategy)
            .where(Strategy.enabled.is_(True))
            .order_by(Strategy.code)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def upsert(
        self,
        *,
        code: str,
        name: str,
        version: str,
        description: str | None,
        config_json: dict[str, object],
        enabled: bool = True,
    ) -> Strategy:
        existing = await self.session.get(Strategy, code)
        if existing is None:
            existing = Strategy(
                code=code,
                name=name,
                version=version,
                description=description,
                config_json=config_json,
                enabled=enabled,
            )
            self.session.add(existing)
        else:
            existing.name = name
            existing.version = version
            existing.description = description
            existing.config_json = config_json
            existing.enabled = enabled
        await self.session.flush()
        return existing
