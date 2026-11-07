"""Topic references name artifacts; naming is not permission to cite as facts."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

import pytest
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.bank_topics.contracts import ExactRef, TopicRef
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.publication_numbers import NumericResolver
from investment_agent.portfolio.calculation_contracts import PortfolioScenarioInput

KINDS = ("topic_case", "topic_revision", "topic_condition", "topic_check")


@pytest.mark.parametrize("kind", KINDS)
def test_topic_refs_use_uuid_namespace_and_roundtrip(kind: str) -> None:
    ref = ObjectRef.model_validate({"kind": kind, "id": str(UUID(int=21)), "revision": 1})
    assert ObjectRef.model_validate_json(ref.model_dump_json()) == ref
    assert isinstance(TypeAdapter(ExactRef).validate_python(ref), TopicRef)
    for invalid in (1, True, "1", 0):
        with pytest.raises(ValidationError):
            ObjectRef.model_validate({"kind": kind, "id": invalid, "revision": 1})


@pytest.mark.parametrize("kind", ("listing", "financial_snapshot", "pipeline_run", "research_dataset"))
def test_legacy_integer_references_keep_their_namespace(kind: str) -> None:
    assert ObjectRef.model_validate({"kind": kind, "id": 21, "revision": 1}).id == 21
    with pytest.raises(ValidationError):
        ObjectRef.model_validate({"kind": kind, "id": str(UUID(int=21)), "revision": 1})


@pytest.mark.parametrize("kind", KINDS)
async def test_topic_ref_is_rejected_by_evidence_and_formal_numeric_resolvers(kind: str) -> None:
    ref = ObjectRef.model_validate({"kind": kind, "id": str(UUID(int=21)), "revision": 1})
    service = EvidenceService(cast(AsyncSession, None))  # Guards reject before any storage access.
    now = datetime(2026, 11, 7, tzinfo=UTC)
    with pytest.raises(InvestorError, match="Evidence reference"):
        await service.get_evidence("local_owner", ref, now)
    with pytest.raises(InvestorError, match="Calculation reference"):
        await service.get_calculation("local_owner", ref, now)
    resolver = NumericResolver(service, ObjectRef(kind="listing", id=18, revision=1), now)
    with pytest.raises(InvestorError):
        await resolver.fact(ref, require_verified=True)
    with pytest.raises(InvestorError):
        await resolver.calculation(ref)


@pytest.mark.parametrize("kind", KINDS)
def test_ag22_does_not_accept_topic_as_snapshot_or_valuation(kind: str) -> None:
    base = {
        "account_id": str(UUID(int=1)),
        "snapshot_ref": {"kind": "portfolio_snapshot", "id": str(UUID(int=2)), "revision": 1},
        "mandate_ref": {"kind": "mandate", "id": str(UUID(int=3)), "revision": 1},
        "scenarios": [{"name": "fixture", "actions": []}],
    }
    PortfolioScenarioInput.model_validate(base)
    ref = {"kind": kind, "id": str(UUID(int=21)), "revision": 1}
    for field in ("snapshot_ref", "valuation_ref", "mandate_ref"):
        with pytest.raises(ValidationError):
            PortfolioScenarioInput.model_validate({**base, field: ref})
