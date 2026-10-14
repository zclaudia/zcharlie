"""Shared field-name witnesses for the strict re-derivation comparisons.

Every re-deriving adapter compares its freshly materialized ``expected`` record
to the submitted one attribute by attribute. These tuples are the single
spelling of *which* attributes are compared; the comparison itself stays a
strict ``!=`` in the given order, so a multi-field forgery keeps naming the
same field the previous inline loops named.

The constants live here, not in ``evidence/contracts.py``: that module stays a
pure pydantic contract (no logic, no constants), while the field list is
save-time verification policy consumed by the adapters and their shared
helper.
"""

from investment_agent.evidence.contracts import EvidenceRecord
from investment_agent.investor.foundations import InvestorError

EVIDENCE_IDENTITY_FIELDS: tuple[str, ...] = (
    "subject_ref",
    "period",
    "source_ref",
    "locator",
    "value",
    "text",
    "published_at",
    "acquired_at",
    "available_from",
    "content_hash",
    "verification",
    "conflict",
)
"""The 12 record-identity fields every re-derived source must reproduce."""

EVIDENCE_FULL_FIELDS: tuple[str, ...] = (
    *EVIDENCE_IDENTITY_FIELDS,
    "date_precision",
    "document_provenance",
)
"""The 14 fields for sources that also carry optional precision/provenance."""


def require_matching_fields(
    expected: EvidenceRecord,
    submitted: EvidenceRecord,
    fields: tuple[str, ...],
    template: str,
) -> None:
    """Reject ``submitted`` unless every named field equals ``expected``'s.

    ``template`` is the branch's own message with a ``{name}`` placeholder, so
    the error code, text and the field it names stay exactly as before.
    """
    for name in fields:
        if getattr(expected, name) != getattr(submitted, name):
            raise InvestorError("invalid_reference", template.format(name=name))
