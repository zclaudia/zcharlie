"""Company repository — lookups by name, sector peers."""

from sqlalchemy import select

from investment_agent.storage.models.reference import Company
from investment_agent.storage.repositories.base import Repository


class CompanyRepository(Repository[Company]):
    model = Company

    async def get_by_canonical_name(self, name: str) -> Company | None:
        stmt = select(Company).where(Company.canonical_name == name)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_ids(self, ids: list[int]) -> list[Company]:
        if not ids:
            return []
        stmt = select(Company).where(Company.id.in_(ids))
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_sector_peers(
        self,
        sector_code: str,
        exclude_company_id: int | None = None,
        limit: int = 5,
    ) -> list[Company]:
        stmt = select(Company).where(Company.sector_code == sector_code)
        if exclude_company_id is not None:
            stmt = stmt.where(Company.id != exclude_company_id)
        stmt = stmt.limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())
