"""Fundamental repository — snapshot + financial report upserts and lookups."""

from datetime import date
from decimal import Decimal

from sqlalchemy import select

from investment_agent.storage.models.market_data import (
    FinancialReport,
    FundamentalSnapshot,
)
from investment_agent.storage.repositories.base import Repository


class FundamentalRepository(Repository[FundamentalSnapshot]):
    model = FundamentalSnapshot

    async def upsert_snapshot(
        self,
        *,
        listing_id: int,
        date: date,
        pe: Decimal | None = None,
        pb: Decimal | None = None,
        peg: Decimal | None = None,
        ps: Decimal | None = None,
        roe: Decimal | None = None,
        roa: Decimal | None = None,
        fcf_yield: Decimal | None = None,
        debt_to_equity: Decimal | None = None,
        market_cap: Decimal | None = None,
    ) -> FundamentalSnapshot:
        existing = await self.session.get(FundamentalSnapshot, (listing_id, date))
        if existing is None:
            existing = FundamentalSnapshot(
                listing_id=listing_id,
                date=date,
                pe=pe,
                pb=pb,
                peg=peg,
                ps=ps,
                roe=roe,
                roa=roa,
                fcf_yield=fcf_yield,
                debt_to_equity=debt_to_equity,
                market_cap=market_cap,
            )
            self.session.add(existing)
        else:
            existing.pe = pe
            existing.pb = pb
            existing.peg = peg
            existing.ps = ps
            existing.roe = roe
            existing.roa = roa
            existing.fcf_yield = fcf_yield
            existing.debt_to_equity = debt_to_equity
            existing.market_cap = market_cap
        await self.session.flush()
        return existing

    async def latest_snapshot(self, listing_id: int) -> FundamentalSnapshot | None:
        stmt = (
            select(FundamentalSnapshot)
            .where(FundamentalSnapshot.listing_id == listing_id)
            .order_by(FundamentalSnapshot.date.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert_financial_report(
        self,
        *,
        company_id: int,
        fiscal_period: str,
        period_end: date,
        revenue: Decimal | None = None,
        net_income: Decimal | None = None,
        fcf: Decimal | None = None,
        total_assets: Decimal | None = None,
        total_equity: Decimal | None = None,
        raw_data: dict[str, object] | None = None,
    ) -> FinancialReport:
        existing = await self.session.get(
            FinancialReport, (company_id, fiscal_period)
        )
        if existing is None:
            existing = FinancialReport(
                company_id=company_id,
                fiscal_period=fiscal_period,
                period_end=period_end,
                revenue=revenue,
                net_income=net_income,
                fcf=fcf,
                total_assets=total_assets,
                total_equity=total_equity,
                raw_data=raw_data or {},
            )
            self.session.add(existing)
        else:
            existing.period_end = period_end
            existing.revenue = revenue
            existing.net_income = net_income
            existing.fcf = fcf
            existing.total_assets = total_assets
            existing.total_equity = total_equity
            existing.raw_data = raw_data or {}
        await self.session.flush()
        return existing

    async def latest_reports(
        self, company_id: int, limit: int = 5
    ) -> list[FinancialReport]:
        stmt = (
            select(FinancialReport)
            .where(FinancialReport.company_id == company_id)
            .order_by(FinancialReport.period_end.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())
