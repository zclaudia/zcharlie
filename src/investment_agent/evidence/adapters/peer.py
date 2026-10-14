"""Trusted peer-annual-field adapter (WP4).

The record is never trusted: every fact is re-derived from the stored
immutable snapshot row and its per-field proof before it is accepted.
Verified can only come out of this re-derivation, so a model or client upload
claiming a peer source and verified is refused here, never relaxed.

The industry_peers imports stay function-level, as in the inline branch this
replaces: the adapter must read the freshest stored row inside the caller's
nested transaction.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.adapters._fields import (
    EVIDENCE_FULL_FIELDS,
    require_matching_fields,
)
from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.investor.foundations import InvestorError


async def materialize_expected(
    session: AsyncSession, record: EvidenceRecord
) -> tuple[EvidenceRecord, str]:
    """Re-derive the expected record and return ``(record_to_save, actor)``."""
    from investment_agent.industry_peers.evidence import ADAPTER_ACTOR, peer_field_evidence
    from investment_agent.storage.models.peer import PeerAnnualSnapshot

    field_path = record.source_ref.field_path
    if (
        record.source_ref.revision != 1
        or field_path is None
        or not field_path.startswith("fields.")
        or len(field_path) <= len("fields.")
    ):
        raise InvestorError("invalid_reference", "Snapshot field is not supported")
    metric = field_path.removeprefix("fields.")
    snapshot = await session.get(PeerAnnualSnapshot, str(record.source_ref.id))
    if snapshot is None:
        raise InvestorError("invalid_reference", "Snapshot version does not exist")
    if metric not in (snapshot.fields or {}):
        raise InvestorError("invalid_reference", "Snapshot field is not supported")
    materialization = peer_field_evidence(snapshot, metric)
    peer_expected = materialization.record
    if peer_expected is not None and peer_expected.verification == "verified":
        from investment_agent.industry_peers.originals import validate_field_original

        if not await validate_field_original(session, snapshot, metric):
            raise InvestorError(
                "invalid_reference", "Peer original proof does not match the saved original bytes"
            )
    if peer_expected is None:
        raise InvestorError(
            "invalid_reference",
            "Peer annual field is not materializable from this snapshot version "
            f"({materialization.reason}); the fact stays in the snapshot until a "
            "version that states its source dates exists",
        )
    require_matching_fields(
        peer_expected,
        record,
        EVIDENCE_FULL_FIELDS,
        "Peer snapshot {name} does not match saved source; "
        "re-materialize from the current snapshot version",
    )
    return record, ADAPTER_ACTOR
