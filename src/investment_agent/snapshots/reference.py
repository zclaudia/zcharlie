"""``universe_sync`` / ``industry_sync``: reference data -> PIT tables.

Constituents and CSRC industries come from baostock; Shenwan 2021
industries (``sw2021``, P1-A9) from AkShare via :mod:`.shenwan`.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

import structlog

from investment_agent.providers.baostock_reference import (
    ConstituentSnapshot,
    IndexCode,
    IndustrySnapshot,
)
from investment_agent.providers.errors import ProviderError, ProviderTransientError
from investment_agent.snapshots.collect import ArtifactWriter, fence_writes
from investment_agent.snapshots.jobs import IndustrySyncPayload, UniverseSyncPayload
from investment_agent.snapshots.shenwan import (
    ShenwanClient,
    apply_sw2021_history,
    existing_listing_ids,
    write_history_artifact,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.pipeline_run import RunLease
from investment_agent.storage.repositories.universe import UniverseRepository

log = structlog.get_logger(__name__)

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
    shenwan: ShenwanClient | None = None,
) -> dict[str, object]:
    """Sync every requested system; one system's terminal failure spares the others.

    CSRC keys stay at the top level of the metadata (as before P1-A9); the
    Shenwan result is under ``sw2021``. A terminal provider error of one
    system is recorded in ``failed_systems`` / ``errors`` (the run ends
    ``partial``) when another system succeeded; transient errors propagate so
    the worker retries the job (both syncs are idempotent).
    """

    scope = await _universe_scope(payload)
    metadata: dict[str, object] = {
        "as_of": payload.as_of.isoformat(),
        "universe_codes": list(payload.universe_codes),
        "systems": list(payload.systems),
    }
    errors: dict[str, str] = {}
    for system in payload.systems:
        try:
            if system == "csrc":
                metadata.update(await _sync_csrc(payload, scope, client, artifacts, lease))
            else:
                if shenwan is None:
                    raise ProviderError("no Shenwan client configured", provider="akshare")
                metadata["sw2021"] = await _sync_sw2021(
                    payload, scope, shenwan, artifacts, lease
                )
        except ProviderTransientError:
            raise
        except ProviderError as exc:
            if len(payload.systems) == 1:
                raise
            log.warning("industry_sync_system_failed", system=system, error=str(exc))
            errors[system] = f"{type(exc).__name__}: {exc}"
    if errors:
        if len(errors) == len(payload.systems):
            raise ProviderError(
                "industry sync failed for every system: " + "; ".join(errors.values()),
                provider="industry_sync",
            )
        metadata["failed_systems"] = sorted(errors)
        metadata["errors"] = errors
    return metadata


async def _universe_scope(payload: IndustrySyncPayload) -> set[str] | None:
    if not payload.universe_codes:
        return None
    scope: set[str] = set()
    async with session_scope() as session:
        universes = UniverseRepository(session)
        for code in payload.universe_codes:
            members = await universes.members_as_of(code, payload.as_of)
            scope.update(member.ticker for member in members)
    return scope


async def _sync_csrc(
    payload: IndustrySyncPayload,
    scope: set[str] | None,
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


async def _sync_sw2021(
    payload: IndustrySyncPayload,
    scope: set[str] | None,
    shenwan: ShenwanClient,
    artifacts: ArtifactWriter,
    lease: RunLease | None,
) -> dict[str, object]:
    history = await shenwan.fetch_industry_history()
    async with session_scope() as session:
        listing_ids = await existing_listing_ids(session, scope)
    raw_ref = write_history_artifact(
        artifacts,
        history,
        name=f"industry-sw2021-{payload.as_of.isoformat()}",
        tickers=listing_ids,
    )
    result = await apply_sw2021_history(
        history, scope=listing_ids, as_of=payload.as_of, lease=lease
    )
    return {**result, "raw_ref": raw_ref}
