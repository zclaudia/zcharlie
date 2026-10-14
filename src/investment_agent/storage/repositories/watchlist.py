"""Watchlist repository."""

from datetime import UTC, datetime

import structlog
from sqlalchemy import select

from investment_agent.domain.pipeline_status import TERMINAL_RUN_STATUSES
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.models.user_domain import WatchlistItem
from investment_agent.storage.repositories.base import Repository

log = structlog.get_logger(__name__)

_LIVE_RUN_STATUSES = frozenset({"queued", "running"})


class WatchlistRepository(Repository[WatchlistItem]):
    model = WatchlistItem

    async def get_by_listing(self, listing_id: int) -> WatchlistItem | None:
        stmt = select(WatchlistItem).where(
            WatchlistItem.listing_id == listing_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_all(self) -> list[WatchlistItem]:
        stmt = select(WatchlistItem).order_by(WatchlistItem.added_at.desc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_enabled(self) -> list[WatchlistItem]:
        stmt = (
            select(WatchlistItem)
            .where(WatchlistItem.enabled.is_(True))
            .order_by(WatchlistItem.added_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def add(
        self,
        listing_id: int,
        note: str | None = None,
        *,
        industry: str | None = None,
        provider: str | None = None,
        report_year: int | None = None,
        enabled: bool | None = None,
    ) -> WatchlistItem:
        existing = await self.get_by_listing(listing_id)
        if existing is not None:
            if note is not None:
                existing.note = note
            if industry is not None:
                existing.industry = industry
            if provider is not None:
                existing.provider = provider
            if report_year is not None:
                existing.report_year = report_year
            if enabled is not None:
                existing.enabled = enabled
            await self.session.flush()
            await self.session.refresh(existing, attribute_names=["updated_at"])
            return existing
        item = WatchlistItem(
            listing_id=listing_id,
            added_at=datetime.now(UTC),
            note=note,
            industry=industry,
            provider=provider or "auto",
            report_year=report_year,
            enabled=True if enabled is None else enabled,
            last_refresh_status="never",
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def mark_refresh_queued(
        self,
        listing_id: int,
        *,
        run_id: int,
        status: str = "queued",
    ) -> bool:
        if status not in {"queued", "running"}:
            raise ValueError(f"Unsupported active refresh status: {status}")
        item = await self.get_by_listing(listing_id)
        if item is None:
            return False
        if (
            item.last_research_run_id is not None
            and item.last_research_run_id > run_id
        ):
            return False
        item.last_refresh_status = status
        item.last_refresh_error = None
        item.last_research_run_id = run_id
        await self.session.flush()
        return True

    async def mark_refresh_finished(
        self,
        listing_id: int,
        *,
        run_id: int,
        status: str,
        refreshed_at: datetime,
        error: str | None,
    ) -> bool:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"Unsupported terminal refresh status: {status}")
        item = await self.get_by_listing(listing_id)
        if item is None:
            return False
        if item.last_research_run_id not in {None, run_id}:
            return False
        item.last_refresh_status = status
        item.last_refreshed_at = refreshed_at
        item.last_refresh_error = error
        item.last_research_run_id = run_id
        await self.session.flush()
        return True

    async def mark_refresh_retrying(
        self,
        listing_id: int,
        *,
        run_id: int,
        error: str,
    ) -> bool:
        item = await self.get_by_listing(listing_id)
        if item is None or item.last_research_run_id not in {None, run_id}:
            return False
        item.last_refresh_status = "queued"
        item.last_refresh_error = error
        item.last_research_run_id = run_id
        await self.session.flush()
        return True

    async def mark_refresh_enqueue_failed(
        self,
        listing_id: int,
        *,
        error: str,
        failed_at: datetime | None = None,
    ) -> bool:
        """Record a failed enqueue attempt.

        If the item already tracks a live (queued/running) research run, its
        state belongs to that run: keep status/run id/refreshed_at untouched and
        only surface the enqueue error in ``last_refresh_error``. Returns True
        only when the refresh status was switched to ``failed``.
        """
        item = await self.get_by_listing(listing_id)
        if item is None:
            return False
        if item.last_research_run_id is not None:
            live_run = await self.session.get(PipelineRun, item.last_research_run_id)
            if live_run is not None and live_run.status in _LIVE_RUN_STATUSES:
                log.warning(
                    "watchlist.enqueue_failed_with_live_run",
                    listing_id=listing_id,
                    live_run_id=live_run.id,
                    live_run_status=live_run.status,
                    error=error,
                )
                item.last_refresh_error = error
                await self.session.flush()
                return False
        item.last_refresh_status = "failed"
        item.last_refreshed_at = failed_at or datetime.now(UTC)
        item.last_refresh_error = error
        await self.session.flush()
        return True

    async def remove(self, listing_id: int) -> bool:
        stmt = select(WatchlistItem).where(WatchlistItem.listing_id == listing_id)
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            return False
        await self.session.delete(existing)
        await self.session.flush()
        return True

    async def set_enabled(self, listing_id: int, *, enabled: bool) -> WatchlistItem | None:
        stmt = select(WatchlistItem).where(WatchlistItem.listing_id == listing_id)
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            return None
        existing.enabled = enabled
        await self.session.flush()
        await self.session.refresh(existing)
        return existing
