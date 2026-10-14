"""User-report adapter: the only upload that is never re-derived.

A client submission can never be ``verified``: the source must be its own
listing subject and keep ``verification == "user_report"``. Server time and
the content hash are recomputed here, so a client cannot backdate its upload.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.evidence.snapshots import digest
from investment_agent.investor.foundations import InvestorError

ACTOR = "user"


async def materialize_expected(
    session: AsyncSession, record: EvidenceRecord
) -> tuple[EvidenceRecord, str]:
    """Stamp server times and the content hash, returning ``(record_to_save, actor)``.

    ``session`` is unused but kept so every adapter entry point has the same
    signature; the shell is what rewrites the saved source ref/locator once
    the new evidence id exists.
    """
    if record.source_ref != record.subject_ref or record.verification != "user_report":
        raise InvestorError(
            "invalid_reference", "User report source must be its listing subject"
        )
    now = datetime.now(UTC)
    record = record.model_copy(
        update={"acquired_at": now, "available_from": now, "published_at": now}
    )
    record = record.model_copy(
        update={
            "content_hash": digest(
                {
                    "subject": record.subject_ref.model_dump(mode="json"),
                    "period": record.period.model_dump(mode="json") if record.period else None,
                    "value": record.value.model_dump(mode="json") if record.value else None,
                    "text": record.text,
                }
            )
        }
    )
    return record, ACTOR
