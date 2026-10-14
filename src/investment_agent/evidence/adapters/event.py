"""Saved event adapter: re-derive a forecast/schedule/segment fact from its row.

The event field order below intentionally differs from
:data:`~investment_agent.evidence.adapters._fields.EVIDENCE_IDENTITY_FIELDS`:
it is the order of the original inline loop, so the field named first on a
multi-field mismatch does not change.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.adapters._fields import require_matching_fields
from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.evidence.events import EventRow, event_material, event_model
from investment_agent.investor.foundations import InvestorError

ACTOR = "event_adapter"

_EVENT_FIELDS: tuple[str, ...] = (
    "source_ref",
    "subject_ref",
    "period",
    "locator",
    "value",
    "text",
    "content_hash",
    "published_at",
    "acquired_at",
    "available_from",
    "verification",
    "conflict",
)


async def materialize_expected(
    session: AsyncSession, record: EvidenceRecord
) -> tuple[EvidenceRecord, str]:
    """Re-derive the expected record and return ``(record_to_save, actor)``."""
    model = event_model(record.source_ref.kind)
    if model is None:
        raise InvestorError("invalid_reference", "Unsupported saved event source")
    event_row = await session.get(model, record.source_ref.id)
    if not isinstance(event_row, EventRow):
        raise InvestorError("invalid_reference", "Event source does not exist")
    expected_event = event_material(event_row, record.source_ref.field_path or "text")
    require_matching_fields(
        expected_event, record, _EVENT_FIELDS, "Event {name} does not match saved source"
    )
    return record.model_copy(update={"actor": ACTOR}), ACTOR
