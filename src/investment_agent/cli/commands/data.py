"""data sub-commands: seed, backfill, refresh."""

import asyncio
from datetime import date
from pathlib import Path

import typer
from rich import print as rprint

app = typer.Typer(help="Market data commands")


_DEFAULT_UNIVERSE_YAML = Path("config/universes/nasdaq_100.yaml")


@app.command("seed")
def seed_universe(
    universe_yaml: Path = typer.Argument(  # noqa: B008
        default=_DEFAULT_UNIVERSE_YAML,
        help="Path to universe YAML file",
    ),
) -> None:
    """Seed universe + constituents from YAML config."""
    from investment_agent.storage.seeders.nasdaq_100 import seed_nasdaq_100

    n = asyncio.run(seed_nasdaq_100(universe_yaml))
    rprint(f"[green]Seeded {n} new listings[/green]")


@app.command("backfill")
def backfill(
    tickers: str = typer.Option(..., help="Comma-separated ticker list"),
    start: str = typer.Option(..., help="ISO date, e.g. 2024-01-01"),
    end: str = typer.Option(date.today().isoformat(), help="ISO date"),
) -> None:
    """Backfill daily prices for the given tickers between dates."""
    from sqlalchemy import select

    from investment_agent.sources.yfinance_source import YFinanceSource
    from investment_agent.storage.database import session_scope
    from investment_agent.storage.models.market_data import DailyPrice
    from investment_agent.storage.models.reference import Listing

    ticker_list = [t.strip() for t in tickers.split(",") if t.strip()]
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)

    async def _do() -> int:
        src = YFinanceSource(max_concurrent=10)
        records = await src.fetch_daily_prices(ticker_list, start_d, end_d)
        n = 0
        async with session_scope() as session:
            for ticker in set(r.ticker for r in records):
                stmt = select(Listing).where(Listing.ticker == ticker)
                listing = (await session.execute(stmt)).scalar_one_or_none()
                if listing is None:
                    rprint(f"[yellow]Listing not found for {ticker}, skipping[/yellow]")
                    continue
                for r in (rr for rr in records if rr.ticker == ticker):
                    existing = await session.get(DailyPrice, (listing.id, r.date))
                    if existing is None:
                        session.add(
                            DailyPrice(
                                listing_id=listing.id, date=r.date,
                                open=r.open, high=r.high, low=r.low, close=r.close,
                                volume=r.volume, adjusted_close=r.adjusted_close,
                            )
                        )
                        n += 1
        return n

    written = asyncio.run(_do())
    rprint(f"[green]Wrote {written} daily price rows[/green]")


@app.command("refresh")
def refresh(ticker: str = typer.Argument(...)) -> None:
    """Refresh today's daily price for a single ticker."""
    today = date.today()
    backfill(tickers=ticker, start=today.isoformat(), end=today.isoformat())
