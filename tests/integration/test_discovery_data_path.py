"""Plan E regression: verify step1 populates financial_report + _build_listing_snapshots
fills derived fields + Tier 1 value strategy actually passes with rich data.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select

from investment_agent.config import LLMTierConfig
from investment_agent.pipelines.steps import (
    _build_listing_snapshots,
    step1_data_ingestion,
    step2_tier1_screening,
)
from investment_agent.sources.records import (
    DailyPriceRecord,
    FinancialReportRecord,
    FundamentalSnapshotRecord,
)
from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.base import Base
from investment_agent.storage.models.market_data import (
    DailyPrice,
    FinancialReport,
    FundamentalSnapshot,
)
from investment_agent.storage.models.reference import (
    Listing,
    Market,
    Universe,
    UniverseMembership,
)
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.strategies.registry import build_strategy_agent


class _RichFakeDS:
    """Fake DataSource that returns realistic per-ticker data for AAPL only.

    Designed so the value strategy's required (pe<25, pb<4, roe>0.10, de<2)
    + excluded (net_income_ttm<0) + preferred (fcf_yield>0.05) all pass.
    """

    name = "fake"
    supported_markets = ["NASDAQ"]

    _FUNDAMENTALS: dict[str, dict[str, Decimal]] = {
        "AAPL": dict(
            pe=Decimal("20"),
            pb=Decimal("3"),
            roe=Decimal("0.35"),
            roa=Decimal("0.20"),
            fcf_yield=Decimal("0.06"),
            debt_to_equity=Decimal("1.2"),
            market_cap=Decimal("3000000000000"),
            beta=Decimal("1.25"),
        ),
    }
    # 8 quarters: 2025 Q1-Q4 (current TTM), 2024 Q1-Q4 (prior TTM)
    # 2025 TTM revenue = 125+95+85+90 = 395B
    # 2024 TTM revenue = 100+75+70+80 = 325B
    # YoY growth = (395-325)/325 = ~0.215
    # Latest quarter gross_margin = 55/125 = 0.44
    _FINANCIALS: dict[str, list[tuple[str, date, Decimal, Decimal, Decimal, Decimal]]] = {
        "AAPL": [
            ("2025-Q4", date(2025, 12, 31), Decimal("125000000000"),
             Decimal("55000000000"), Decimal("36000000000"), Decimal("32000000000")),
            ("2025-Q3", date(2025, 9, 30), Decimal("95000000000"),
             Decimal("42000000000"), Decimal("23000000000"), Decimal("21000000000")),
            ("2025-Q2", date(2025, 6, 30), Decimal("85000000000"),
             Decimal("38000000000"), Decimal("21000000000"), Decimal("19000000000")),
            ("2025-Q1", date(2025, 3, 31), Decimal("90000000000"),
             Decimal("40000000000"), Decimal("24000000000"), Decimal("22000000000")),
            ("2024-Q4", date(2024, 12, 31), Decimal("100000000000"),
             Decimal("44000000000"), Decimal("28000000000"), Decimal("25000000000")),
            ("2024-Q3", date(2024, 9, 30), Decimal("75000000000"),
             Decimal("33000000000"), Decimal("18000000000"), Decimal("16000000000")),
            ("2024-Q2", date(2024, 6, 30), Decimal("70000000000"),
             Decimal("31000000000"), Decimal("17000000000"), Decimal("15000000000")),
            ("2024-Q1", date(2024, 3, 31), Decimal("80000000000"),
             Decimal("36000000000"), Decimal("20000000000"), Decimal("18000000000")),
        ],
    }

    async def fetch_universe(self, code, as_of_date):
        return []

    async def fetch_daily_prices(self, tickers, start, end):
        out: list[DailyPriceRecord] = []
        for t in tickers:
            d = start
            close = 200.0
            while d <= end:
                out.append(DailyPriceRecord(
                    ticker=t, date=d,
                    open=Decimal(str(close)),
                    high=Decimal(str(close * 1.01)),
                    low=Decimal(str(close * 0.99)),
                    close=Decimal(str(close)),
                    volume=10_000_000,
                    adjusted_close=Decimal(str(close)),
                ))
                close *= 1.001  # tiny upward drift
                d += timedelta(days=1)
        return out

    async def fetch_fundamental_snapshot(self, ticker, as_of_date):
        f = self._FUNDAMENTALS.get(ticker, {})
        return FundamentalSnapshotRecord(
            ticker=ticker,
            date=as_of_date,
            pe=f.get("pe"),
            pb=f.get("pb"),
            peg=None,
            ps=None,
            roe=f.get("roe"),
            roa=f.get("roa"),
            fcf_yield=f.get("fcf_yield"),
            debt_to_equity=f.get("debt_to_equity"),
            market_cap=f.get("market_cap"),
            beta=f.get("beta"),
        )

    async def fetch_financial_report(self, ticker, periods):
        rows = self._FINANCIALS.get(ticker, [])
        return [
            FinancialReportRecord(
                ticker=ticker, fiscal_period=p, period_end=pe,
                revenue=rev, net_income=ni, fcf=fcf,
                gross_profit=gp, total_assets=None, total_equity=None,
                raw_data={},
            )
            for (p, pe, rev, gp, ni, fcf) in rows
        ]

    async def fetch_news(self, t, d):
        return []

    async def fetch_analyst_ratings(self, t, d):
        return []


@pytest_asyncio.fixture
async def data_path_engine(tmp_path):
    """Disk-backed sqlite engine for tests that use session_scope across boundaries."""
    db_path = tmp_path / "datapath.db"
    engine = init_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


async def _seed_universe(as_of: date) -> int:
    """Create market + AAPL listing + universe + pipeline_run; return run_id."""
    async with session_scope() as session:
        session.add(Market(
            code="NASDAQ", name="NASDAQ",
            timezone="America/New_York", currency="USD",
        ))
        session.add(Universe(code="TEST_U", name="Test"))
        repo = ListingRepository(session)
        aapl = await repo.upsert_listing(
            market_code="NASDAQ", ticker="AAPL", company_name="Apple",
        )
        session.add(UniverseMembership(
            universe_code="TEST_U", listing_id=aapl.id,
            valid_from=date(2024, 1, 1),
        ))
        await session.flush()
        run = PipelineRun(
            kind="discovery", universe_code="TEST_U", triggered_by="test",
            started_at=datetime.now(UTC), status="running", metadata_json={},
        )
        session.add(run)
        await session.flush()
        return run.id


@pytest.mark.asyncio
async def test_step1_populates_financial_report(data_path_engine):
    as_of = date(2026, 6, 5)
    run_id = await _seed_universe(as_of)

    async with session_scope() as session:
        universe, _aux = await step1_data_ingestion(
            session, universe_code="TEST_U",
            data_source=_RichFakeDS(),  # type: ignore[arg-type]
            as_of=as_of, pipeline_run_id=run_id,
        )
        assert len(universe) == 1

    async with session_scope() as session:
        fr_rows = list(
            (await session.execute(select(FinancialReport))).scalars().all(),
        )
        # _RichFakeDS provides 8 quarters; step1 caps at 8
        assert len(fr_rows) == 8
        # Verify gross_profit was persisted
        assert all(r.gross_profit is not None for r in fr_rows)

        snap_rows = list(
            (await session.execute(select(FundamentalSnapshot))).scalars().all(),
        )
        assert len(snap_rows) == 1
        # Verify beta was persisted
        assert snap_rows[0].beta == Decimal("1.25")

        price_rows = list(
            (await session.execute(select(DailyPrice))).scalars().all(),
        )
        # 8-day window (as_of - 7d to as_of) per step1's fetch_daily_prices call
        assert len(price_rows) > 0


@pytest.mark.asyncio
async def test_build_listing_snapshots_fills_derived_fields(data_path_engine):
    as_of = date(2026, 6, 5)
    run_id = await _seed_universe(as_of)

    async with session_scope() as session:
        await step1_data_ingestion(
            session, universe_code="TEST_U",
            data_source=_RichFakeDS(),  # type: ignore[arg-type]
            as_of=as_of, pipeline_run_id=run_id,
        )

    # Add more price history beyond step1's 8-day window so volatility_30d has data
    async with session_scope() as session:
        aapl = (await session.execute(
            select(Listing).where(Listing.ticker == "AAPL"),
        )).scalar_one()
        d = as_of - timedelta(days=35)
        close = 200.0
        while d < as_of - timedelta(days=7):
            session.add(DailyPrice(
                listing_id=aapl.id, date=d,
                open=Decimal(str(close)),
                high=Decimal(str(close * 1.01)),
                low=Decimal(str(close * 0.99)),
                close=Decimal(str(close)),
                volume=10_000_000,
                adjusted_close=Decimal(str(close)),
            ))
            close *= 1.001
            d += timedelta(days=1)
        await session.flush()

    async with session_scope() as session:
        aapl = (await session.execute(
            select(Listing).where(Listing.ticker == "AAPL"),
        )).scalar_one()
        snaps = await _build_listing_snapshots(session, [aapl], as_of)
        assert len(snaps) == 1
        snap = snaps[0]
        # value-strategy fields populated from fundamentals
        assert snap.pe == Decimal("20")
        assert snap.pb == Decimal("3")
        assert snap.roe == Decimal("0.35")
        # TTM aggregate from 4 recent quarters
        assert snap.net_income_ttm is not None
        assert snap.net_income_ttm > 0
        # YoY revenue growth: 2025 TTM (395) vs 2024 TTM (325) ~= 0.215
        assert snap.revenue_growth_yoy is not None
        assert Decimal("0.20") < snap.revenue_growth_yoy < Decimal("0.25")
        # Gross margin: latest qtr 55/125 = 0.44
        assert snap.gross_margin is not None
        assert Decimal("0.40") < snap.gross_margin < Decimal("0.50")
        # Beta from fundamental snapshot
        assert snap.beta == Decimal("1.25")
        # Volatility from price history (small positive)
        assert snap.volatility_30d is not None
        assert snap.volatility_30d > 0


@pytest.mark.asyncio
async def test_tier1_value_strategy_passes_with_rich_data(data_path_engine):
    """End-to-end Tier 1: value strategy passes AAPL given rich fundamentals + financials."""
    as_of = date(2026, 6, 5)
    run_id = await _seed_universe(as_of)

    async with session_scope() as session:
        await step1_data_ingestion(
            session, universe_code="TEST_U",
            data_source=_RichFakeDS(),  # type: ignore[arg-type]
            as_of=as_of, pipeline_run_id=run_id,
        )

    async with session_scope() as session:
        aapl_list = list((await session.execute(
            select(Listing).where(Listing.ticker == "AAPL"),
        )).scalars().all())
        dummy_cfg = LLMTierConfig(base_url="http://x", api_key="x", model="m")
        strategies = {
            "value": build_strategy_agent(
                yaml_path=Path("strategies/value.yaml"),
                tier2_config=dummy_cfg, tier3_config=dummy_cfg,
            ),
        }
        result = await step2_tier1_screening(
            session, listings=aapl_list, strategies=strategies, as_of=as_of,
        )
        assert len(result["value"]) == 1, (
            f"value strategy should pass AAPL with rich data; "
            f"got {result['value']}"
        )
