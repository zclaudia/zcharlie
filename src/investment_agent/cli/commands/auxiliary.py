"""ia auxiliary sub-commands: list / add / remove / refresh."""

import asyncio
from datetime import UTC, datetime

import typer
from rich import print as rprint
from rich.table import Table
from sqlalchemy import select

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.user_domain import AuxiliaryListing

app = typer.Typer(help="Auxiliary listings management")


@app.command("list")
def list_auxiliary() -> None:
    """List all auxiliary listings (enabled and disabled)."""

    async def _do() -> list[tuple[AuxiliaryListing, Listing]]:
        async with session_scope() as session:
            stmt = (
                select(AuxiliaryListing, Listing)
                .join(Listing, AuxiliaryListing.listing_id == Listing.id)
                .order_by(AuxiliaryListing.added_at.desc())
            )
            return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]

    rows = asyncio.run(_do())
    table = Table(title="Auxiliary Listings")
    table.add_column("ID")
    table.add_column("Ticker")
    table.add_column("Market")
    table.add_column("Added By")
    table.add_column("Added At")
    table.add_column("Enabled")
    for aux, listing in rows:
        table.add_row(
            str(aux.id),
            listing.ticker,
            listing.market_code,
            aux.added_by,
            aux.added_at.isoformat(timespec="seconds"),
            "yes" if aux.enabled else "no",
        )
    rprint(table)


@app.command("add")
def add_auxiliary(ticker: str, market: str = "NASDAQ") -> None:
    """Manually add an auxiliary listing by market + ticker."""

    async def _do() -> int | None:
        async with session_scope() as session:
            listing_stmt = select(Listing).where(
                Listing.market_code == market,
                Listing.ticker == ticker,
            )
            listing = (await session.execute(listing_stmt)).scalar_one_or_none()
            if listing is None:
                return None
            existing_stmt = select(AuxiliaryListing).where(
                AuxiliaryListing.listing_id == listing.id,
            )
            existing = (await session.execute(existing_stmt)).scalar_one_or_none()
            if existing:
                existing.enabled = True
                await session.flush()
                return existing.id
            new = AuxiliaryListing(
                listing_id=listing.id,
                added_by="cli",
                added_at=datetime.now(UTC),
                enabled=True,
            )
            session.add(new)
            await session.flush()
            return new.id

    result = asyncio.run(_do())
    if result is None:
        rprint(f"[red]Listing not found: {market}/{ticker}[/red]")
        raise typer.Exit(1)
    rprint(f"[green]Added auxiliary listing #{result}[/green]")


@app.command("remove")
def remove_auxiliary(listing_id: int) -> None:
    """Disable (soft-delete) an auxiliary listing by its listing_id."""

    async def _do() -> bool:
        async with session_scope() as session:
            stmt = select(AuxiliaryListing).where(
                AuxiliaryListing.listing_id == listing_id,
            )
            existing = (await session.execute(stmt)).scalar_one_or_none()
            if existing is None:
                return False
            existing.enabled = False
            await session.flush()
            return True

    if asyncio.run(_do()):
        rprint(
            f"[green]Disabled auxiliary listing for listing_id={listing_id}[/green]"
        )
    else:
        rprint(f"[red]No auxiliary listing for listing_id={listing_id}[/red]")
        raise typer.Exit(1)


@app.command("refresh")
def refresh_auxiliary() -> None:
    """Run the derivation job against current company_relationship table."""
    from pathlib import Path

    from investment_agent.auxiliary.derivation import (
        DerivationResult,
        derive_auxiliary_listings,
    )
    from investment_agent.config import load_app_yaml

    async def _do() -> DerivationResult:
        cfg = load_app_yaml(Path("config/app.yaml"))
        aux_cfg = cfg.auxiliary_tracking
        async with session_scope() as session:
            return await derive_auxiliary_listings(
                session,
                strength_threshold=aux_cfg.strength_threshold,
                max_per_company=aux_cfg.max_per_company,
                total_cap=aux_cfg.max_per_company * aux_cfg.total_cap_multiplier,
            )

    result = asyncio.run(_do())
    rprint(
        f"[green]Auxiliary refresh: added={result['added']}, "
        f"skipped_universe={result['skipped_already_universe']}, "
        f"skipped_no_listing={result['skipped_no_listing']}, "
        f"skipped_cap={result['skipped_cap_reached']}, "
        f"total={result['current_total']}[/green]"
    )
