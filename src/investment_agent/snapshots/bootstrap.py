"""Initial per-stock data collection, independent of index membership.

Manual nominations with a template run this inside the draft's heartbeat,
before loading factors or calling the model. Nominations without a template
queue the same work as a stock_bootstrap job. Retries reuse stored history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from investment_agent.providers.baostock_reference import IndustrySnapshot
from investment_agent.snapshots.backfill import (
    StockHistoryPayload,
    backfill_dividends,
    backfill_financials,
    backfill_prices,
)
from investment_agent.snapshots.collect import (
    ArtifactWriter,
    CollectionReport,
    SnapshotPreconditionError,
    fence_writes,
)
from investment_agent.snapshots.earnings_events import collect_segments_for_listings
from investment_agent.snapshots.jobs import EarningsEventPayload, StockBootstrapPayload
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository, RunLease

if TYPE_CHECKING:
    from investment_agent.snapshots.handlers import SnapshotRuntime


async def collect_stock_bootstrap(
    payload: StockBootstrapPayload,
    *,
    runtime: SnapshotRuntime,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
) -> dict[str, object]:
    """Collect only this stock; provider failures prevent an empty-data draft."""
    async with session_scope() as session:
        listing = await ListingRepository(session).get_a_share_listing(payload.ticker)
        if listing is None:
            raise SnapshotPreconditionError(f"no listing for {payload.ticker}")
        listing_id = listing.id
        if lease is not None:
            await PipelineRunRepository(session).set_stage(lease.run_id, "fetching", lease=lease)
    parts: dict[str, object] = {}
    metadata: dict[str, object] = {
        "ticker": payload.ticker, "as_of": payload.as_of.isoformat(), "parts": parts,
    }

    async def record(name: str, result: dict[str, object]) -> None:
        parts[name] = result
        if lease is not None:
            async with session_scope() as session:
                await PipelineRunRepository(session).record_metadata(
                    lease.run_id, {"data_preparation": metadata}, lease=lease
                )

    async def settle(name: str, report: CollectionReport) -> None:
        await record(name, report.to_metadata())
        if report.status != "success":
            raise report.failure()

    # Industry queries are restricted to the selected ticker; never manufacture
    # index membership to make the historical collectors accept a manual stock.
    snapshot = await runtime.reference_factory().fetch_industry(payload.as_of, payload.ticker)
    entries = tuple(entry for entry in snapshot.entries if entry.ticker == payload.ticker)
    raw_ref = artifacts.write(
        f"bootstrap-industry-{payload.ticker}",
        {"provider": snapshot.source, "as_of": payload.as_of.isoformat(),
         "rows": [dict(row) for row in snapshot.raw_rows]},
    )
    filtered = IndustrySnapshot(
        as_of=snapshot.as_of, effective_date=snapshot.effective_date,
        entries=entries, captured_at=snapshot.captured_at, raw_rows=(),
        system=snapshot.system, source=snapshot.source,
    )
    async with session_scope() as session:
        await fence_writes(session, lease)
        await IndustryRepository(session).apply_industry_snapshot(filtered)
    await record("industry", {"entries": len(entries), "raw_ref": raw_ref})

    client = runtime.backfill_factory()
    for name, collect in (
        ("prices", backfill_prices),
        ("financials", backfill_financials),
        ("dividends", backfill_dividends),
    ):
        await settle(name, await collect(
            StockHistoryPayload(as_of=payload.as_of, symbols=(payload.ticker,), part=name),
            client=client, artifacts=artifacts, lease=lease,
        ))
    # Business composition is otherwise collected only once a week. Do not
    # leave a newly nominated stock empty until the next weekly slot.
    period = payload.as_of.replace(year=payload.as_of.year - 1, month=12, day=31)
    await settle("segments", await collect_segments_for_listings(
        EarningsEventPayload(
            scope="watchlist", part="segments", report_period=period, as_of=payload.as_of,
        ),
        {payload.ticker: listing_id}, client=runtime.earnings_client(),
        artifacts=artifacts, lease=lease, circuit=runtime.circuit,
    ))
    return metadata
