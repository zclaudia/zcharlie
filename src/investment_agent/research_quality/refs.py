"""Quality addresses are independent of financial ObjectRef and public blind maps."""

from typing import Literal
from uuid import UUID

from investment_agent.investor.common import Contract, Revision

type QualityArtifactKind = Literal[
    "quality_case", "quality_envelope", "quality_spec", "quality_result", "quality_report"
]
PUBLIC_ARTIFACT_KINDS = frozenset(
    {"quality_case", "quality_envelope", "quality_spec", "quality_result", "quality_report"}
)


class QualityArtifactRef(Contract):
    kind: QualityArtifactKind
    id: UUID
    revision: Revision


class _BlindMapRef(Contract):
    """Private storage address; never a public artifact or mutation result ref."""

    kind: Literal["quality_blind_map"] = "quality_blind_map"
    id: UUID
    revision: Revision


def parse_public_ref(
    value: object, *, expected_kind: QualityArtifactKind | None = None
) -> QualityArtifactRef:
    ref = QualityArtifactRef.model_validate(value)
    if expected_kind is not None and ref.kind != expected_kind:
        raise ValueError(f"Expected {expected_kind}")
    return ref


__all__ = ["PUBLIC_ARTIFACT_KINDS", "QualityArtifactKind", "QualityArtifactRef", "parse_public_ref"]
