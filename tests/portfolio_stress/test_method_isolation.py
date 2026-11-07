"""PT-005: study addresses and readable results never authorize formal use."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.evidence.contracts import CalculationRecord
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.enums import CalculationStatus
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.publication_numbers import NumericResolver
from investment_agent.portfolio.calculation_contracts import PortfolioScenarioInput
from investment_agent.portfolio.calculations import PortfolioCalculationService
from investment_agent.storage.models.evidence import SavedCalculation

NOW = datetime(2026, 11, 7, tzinfo=UTC)


@pytest.mark.parametrize("kind", ("portfolio_stress", "decision_observation"))
def test_study_refs_have_uuid_namespace_without_relaxing_legacy(kind: str) -> None:
    ref = ObjectRef.model_validate({"kind": kind, "id": str(UUID(int=1)), "revision": 1})
    assert ObjectRef.model_validate_json(ref.model_dump_json()) == ref
    for invalid in (1, True, "1"):
        with pytest.raises(ValidationError):
            ObjectRef.model_validate({"kind": kind, "id": invalid, "revision": 1})
    assert ObjectRef(kind="listing", id=18, revision=1).id == 18


@pytest.mark.parametrize("kind", ("portfolio_stress", "decision_observation"))
async def test_study_addresses_cannot_substitute_financial_evidence_or_ag22_basis(kind: str) -> None:
    ref = ObjectRef.model_validate({"kind": kind, "id": str(UUID(int=1)), "revision": 1})
    service = EvidenceService(cast(AsyncSession, None))
    with pytest.raises(InvestorError, match="Evidence reference"):
        await service.get_evidence("local_owner", ref, NOW)
    with pytest.raises(InvestorError, match="Calculation reference"):
        await service.get_calculation("local_owner", ref, NOW)
    request = {
        "account_id": str(UUID(int=2)),
        "snapshot_ref": {"kind": "portfolio_snapshot", "id": str(UUID(int=3)), "revision": 1},
        "mandate_ref": {"kind": "mandate", "id": str(UUID(int=4)), "revision": 1},
        "scenarios": [{"name": "fixture", "actions": []}],
    }
    PortfolioScenarioInput.model_validate(request)
    for field in ("snapshot_ref", "valuation_ref", "mandate_ref"):
        with pytest.raises(ValidationError):
            PortfolioScenarioInput.model_validate({**request, field: ref})


@pytest.mark.parametrize("method", ("portfolio_stress", "decision_fact_check"))
async def test_saved_study_result_is_readable_but_formal_resolver_and_plan_basis_reject(
    db_session: AsyncSession, method: str,
) -> None:
    account_id = UUID(int=5)
    record = CalculationRecord(
        id=UUID(int=6), revision=1, created_at=NOW, actor="synthetic", origin_ref=None,
        method=method, method_version=f"{method}-v1", input_hash="a" * 64,
        fact_refs=(ObjectRef(kind="evidence", id=UUID(int=7), revision=1),),
        assumption_refs=(), as_of=NOW, applicable=True, status=CalculationStatus.OK,
        results={"known_subtotal": Quantity(value=Decimal("10"), unit="CNY", currency="CNY")},
        details={"request": {"account_id": str(account_id)}},
    )
    db_session.add(SavedCalculation(id=str(record.id), revision=1, owner_id="local_owner",
                                    payload=record.model_dump(mode="json")))
    await db_session.flush()
    ref = ObjectRef(kind="calculation", id=record.id, revision=1)
    evidence = EvidenceService(db_session)
    saved = await evidence.get_calculation("local_owner", ref, NOW)
    assert saved.status == "ok" and saved.data == record
    resolver = NumericResolver(evidence, ObjectRef(kind="listing", id=18, revision=1), NOW)
    # This explicit method guard runs before resolving even apparently valid
    # fact_refs, so adding evidence cannot launder study output into publication.
    with pytest.raises(InvestorError, match="Study calculations are not formal"):
        await resolver.calculation(ref)
    with pytest.raises(InvestorError):
        await PortfolioCalculationService(db_session).get(account_id, record.id)
