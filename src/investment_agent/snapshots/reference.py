"""``universe_sync`` / ``industry_sync``: baostock reference data -> PIT tables."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from investment_agent.providers.baostock_reference import (
    ConstituentSnapshot,
    IndexCode,
    IndustrySnapshot,
)
from investment_agent.snapshots.collect import ArtifactWriter, fence_writes
from investment_agent.snapshots.jobs import IndustrySyncPayload, UniverseSyncPayload
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.pipeline_run import RunLease
from investment_agent.storage.repositories.universe import UniverseRepository

# Keep run metadata bounded; the raw response file has everything.
_MAX_LISTED_CHANGES = 50


class ReferenceClient(Protocol):
    async def fetch_constituents(
        self, index_code: IndexCode, as_of: date
    ) -> ConstituentSnapshot: ...

    async def fetch_industry(self, as_of: date, symbol: str | None = None) -> IndustrySnapshot: ...


async def sync_universe(
    payload: UniverseSyncPayload,
    *,
    client: ReferenceClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
) -> dict[str, object]:
    snapshot = await client.fetch_constituents(payload.universe_code, payload.as_of)
    raw_ref = artifacts.write(
        f"constituents-{payload.universe_code}-{payload.as_of.isoformat()}",
        {
            "provider": snapshot.source,
            "index_code": snapshot.index_code,
            "as_of": snapshot.as_of.isoformat(),
            "captured_at": snapshot.captured_at.isoformat(),
            "rows": [dict(row) for row in snapshot.raw_rows],
        },
    )
    async with session_scope() as session:
        await fence_writes(session, lease)
        diff = await UniverseRepository(session).apply_constituent_snapshot(
            payload.universe_code, snapshot
        )
    return {
        "universe_code": payload.universe_code,
        "as_of": payload.as_of.isoformat(),
        "effective_date": diff.effective_date.isoformat() if diff.effective_date else None,
        "members": len(snapshot.members),
        "added": list(diff.added),
        "removed": list(diff.removed),
        "changed": diff.changed,
        "raw_ref": raw_ref,
    }


async def sync_industry(
    payload: IndustrySyncPayload,
    *,
    client: ReferenceClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
) -> dict[str, object]:
    snapshot = await client.fetch_industry(payload.as_of)
    raw_ref = artifacts.write(
        f"industry-{payload.as_of.isoformat()}",
        {
            "provider": snapshot.source,
            "as_of": snapshot.as_of.isoformat(),
            "captured_at": snapshot.captured_at.isoformat(),
            "rows": [dict(row) for row in snapshot.raw_rows],
        },
    )
    scope: set[str] | None = None
    if payload.universe_codes:
        scope = set()
        async with session_scope() as session:
            universes = UniverseRepository(session)
            for code in payload.universe_codes:
                members = await universes.members_as_of(code, payload.as_of)
                scope.update(member.ticker for member in members)
    entries = tuple(
        entry for entry in snapshot.entries if scope is None or entry.ticker in scope
    )
    filtered = IndustrySnapshot(
        as_of=snapshot.as_of,
        effective_date=max((entry.effective_date for entry in entries), default=None),
        entries=entries,
        captured_at=snapshot.captured_at,
        raw_rows=(),
        system=snapshot.system,
        source=snapshot.source,
    )
    async with session_scope() as session:
        await fence_writes(session, lease)
        changes = await IndustryRepository(session).apply_industry_snapshot(filtered)
    uncovered = sorted(scope - {entry.ticker for entry in entries}) if scope else []
    return {
        "as_of": payload.as_of.isoformat(),
        "universe_codes": list(payload.universe_codes),
        "effective_date": (
            filtered.effective_date.isoformat() if filtered.effective_date else None
        ),
        "provider_entries": len(snapshot.entries),
        "applied_entries": len(entries),
        "changed": len(changes),
        "changes": [
            {
                "ticker": change.ticker,
                "effective_date": change.effective_date.isoformat(),
                "old": change.old_name,
                "new": change.new_name,
            }
            for change in changes[:_MAX_LISTED_CHANGES]
        ],
        "unclassified_members": uncovered,
        "raw_ref": raw_ref,
    }
