"""Research-dataset adapter: rebuild every fact of the stored version.

The submitted record is never trusted: the stored payload is materialized and
the fact is matched by content hash before any field is accepted. The
materials import stays function-level, as in the inline branch this replaces:
the adapter must read the freshest stored row inside the caller's nested
transaction.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.adapters._fields import (
    EVIDENCE_FULL_FIELDS,
    require_matching_fields,
)
from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.investor.foundations import InvestorError

ACTOR = "material_adapter"


async def materialize_expected(
    session: AsyncSession, record: EvidenceRecord
) -> tuple[EvidenceRecord, str]:
    """Re-derive the expected record and return ``(record_to_save, actor)``."""
    from investment_agent.materials.evidence import material_evidence
    from investment_agent.storage.models.research_material import ResearchDataset

    if record.source_ref.kind != "research_dataset":
        raise InvestorError(
            "invalid_reference", "Research dataset source reference is required"
        )
    material_row = await session.get(ResearchDataset, record.source_ref.id)
    if material_row is None or material_row.revision != record.source_ref.revision:
        raise InvestorError("invalid_reference", "Research dataset version does not exist")
    try:
        materialized, _skipped = material_evidence(
            material_row,
            subject_ref=record.subject_ref,
            created_at=record.created_at,
        )
    except ValueError as exc:
        raise InvestorError(
            "invalid_reference", f"Research dataset cannot be materialized: {exc}"
        ) from exc
    candidates = [
        candidate for candidate in materialized if candidate.content_hash == record.content_hash
    ]
    if len(candidates) != 1:
        raise InvestorError(
            "invalid_reference", "Research dataset fact does not match the saved source"
        )
    require_matching_fields(
        candidates[0],
        record,
        EVIDENCE_FULL_FIELDS,
        "Research dataset {name} does not match saved source",
    )
    return record, ACTOR
