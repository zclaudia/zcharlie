"""Financial/market snapshot adapter, and the router for ``source_kind="snapshot"``.

The router keeps the shell's dispatch table keyed by source kind only: for a
snapshot-kind record the concrete adapter is chosen by ``source_ref.kind``,
exactly as the previous inline elif chain did (peer annual snapshot, then the
two point-in-time tables, then every other kind falls through to the event
adapter, which is also where unsupported kinds are refused).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.adapters import event, peer
from investment_agent.evidence.adapters._fields import (
    EVIDENCE_IDENTITY_FIELDS,
    require_matching_fields,
)
from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.evidence.snapshots import UNITS, materialize
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot

ACTOR = "snapshot_adapter"

_PIT_MODELS: dict[str, type[FinancialSnapshot] | type[MarketSnapshot]] = {
    "financial_snapshot": FinancialSnapshot,
    "market_snapshot": MarketSnapshot,
}


async def materialize_expected(
    session: AsyncSession, record: EvidenceRecord
) -> tuple[EvidenceRecord, str]:
    """Re-derive the expected record and return ``(record_to_save, actor)``."""
    kind = record.source_ref.kind
    if kind == "peer_annual_snapshot":
        return await peer.materialize_expected(session, record)
    if kind not in _PIT_MODELS:
        return await event.materialize_expected(session, record)
    return await _pit_snapshot(session, record)


async def _pit_snapshot(
    session: AsyncSession, record: EvidenceRecord
) -> tuple[EvidenceRecord, str]:
    """One stored financial/market snapshot field, matched by exact source ref."""
    row = await session.get(_PIT_MODELS[record.source_ref.kind], record.source_ref.id)
    if (
        not isinstance(row, FinancialSnapshot | MarketSnapshot)
        or row.version != record.source_ref.revision
    ):
        raise InvestorError("invalid_reference", "Snapshot version does not exist")
    candidates: list[EvidenceRecord] = []
    for field in UNITS:
        try:
            candidate = materialize(row, field)
        except InvestorError:
            continue
        if candidate.source_ref == record.source_ref:
            candidates.append(candidate)
    if len(candidates) != 1:
        raise InvestorError("invalid_reference", "Snapshot field is not supported")
    require_matching_fields(
        candidates[0],
        record,
        EVIDENCE_IDENTITY_FIELDS,
        "Snapshot {name} does not match saved source",
    )
    return record, ACTOR
