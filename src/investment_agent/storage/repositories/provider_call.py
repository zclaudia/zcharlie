"""``provider_call`` repository: record, 24-hour summary per provider, prune (A2)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.engine import CursorResult

from investment_agent.domain.time import as_utc
from investment_agent.storage.models.provider_call import (
    CIRCUIT_ENDPOINT,
    FAILED_OUTCOMES,
    PROVIDER_CALL_OUTCOMES,
    ProviderCall,
)
from investment_agent.storage.repositories.base import Repository
from investment_agent.storage.repositories.pit import to_utc


@dataclass(frozen=True)
class ProviderStat:
    """Calls of one provider since a given moment."""

    provider: str
    calls: int
    """Rows that reached (or tried to reach) the upstream: every outcome but
    ``skipped_open``."""
    failed: int
    """``transient`` + ``terminal`` rows."""
    skipped: int
    """``skipped_open`` rows (calls refused by the open circuit, incl. the
    breaker's own ``circuit`` rows)."""
    last_error_at: datetime | None
    last_error: str | None
    """``<endpoint>: <outcome>`` of the newest failed row."""
    last_open_at: datetime | None
    """Newest ``circuit`` row (the breaker opened)."""
    last_open_until: datetime | None
    """``last_open_at`` + that row's window (``duration_ms``)."""


class ProviderCallRepository(Repository[ProviderCall]):
    model = ProviderCall

    async def record(
        self,
        *,
        at: datetime,
        provider: str,
        endpoint: str,
        outcome: str,
        duration_ms: int | None = None,
        ticker_count: int = 0,
        run_id: int | None = None,
    ) -> ProviderCall:
        if outcome not in PROVIDER_CALL_OUTCOMES:
            raise ValueError(f"unknown provider call outcome {outcome!r}")
        if ticker_count < 0:
            raise ValueError("ticker_count must be >= 0")
        row = ProviderCall(
            at=to_utc(at),
            provider=provider[:32],
            endpoint=endpoint[:64],
            outcome=outcome,
            duration_ms=max(int(duration_ms), 0) if duration_ms is not None else None,
            ticker_count=ticker_count,
            run_id=run_id,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def summary(self, since: datetime) -> list[ProviderStat]:
        """Per-provider counts of the rows with ``at > since`` (provider order)."""

        boundary = to_utc(since)
        counts = (
            select(ProviderCall.provider, ProviderCall.outcome, func.count(ProviderCall.id))
            .where(ProviderCall.at > boundary)
            .group_by(ProviderCall.provider, ProviderCall.outcome)
        )
        by_provider: dict[str, dict[str, int]] = {}
        for provider, outcome, count in (await self.session.execute(counts)).all():
            by_provider.setdefault(provider, {})[outcome] = int(count)
        stats: list[ProviderStat] = []
        for provider in sorted(by_provider):
            outcomes = by_provider[provider]
            failed_row = await self._latest(
                provider, boundary, ProviderCall.outcome.in_(FAILED_OUTCOMES)
            )
            circuit_row = await self._latest(
                provider, boundary, ProviderCall.endpoint == CIRCUIT_ENDPOINT
            )
            last_open_at = as_utc(circuit_row.at) if circuit_row is not None else None
            stats.append(
                ProviderStat(
                    provider=provider,
                    calls=sum(
                        count for outcome, count in outcomes.items() if outcome != "skipped_open"
                    ),
                    failed=sum(outcomes.get(outcome, 0) for outcome in FAILED_OUTCOMES),
                    skipped=outcomes.get("skipped_open", 0),
                    last_error_at=as_utc(failed_row.at) if failed_row is not None else None,
                    last_error=(
                        f"{failed_row.endpoint}: {failed_row.outcome}"
                        if failed_row is not None
                        else None
                    ),
                    last_open_at=last_open_at,
                    last_open_until=(
                        last_open_at + _window(circuit_row.duration_ms)
                        if last_open_at is not None and circuit_row is not None
                        else None
                    ),
                )
            )
        return stats

    async def prune(self, *, before: datetime) -> int:
        """Delete rows older than ``before``; returns the number removed."""

        result = await self.session.execute(
            delete(ProviderCall).where(ProviderCall.at < to_utc(before))
        )
        assert isinstance(result, CursorResult)
        return int(result.rowcount or 0)

    async def _latest(self, provider: str, since: datetime, *criteria: object) -> ProviderCall | None:
        stmt = (
            select(ProviderCall)
            .where(ProviderCall.provider == provider, ProviderCall.at > since, *criteria)  # type: ignore[arg-type]
            .order_by(ProviderCall.at.desc(), ProviderCall.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()


def _window(duration_ms: int | None) -> timedelta:
    return timedelta(milliseconds=duration_ms or 0)
