"""Semantic reuse must still honor the mutation receipt's exact request identity."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.foundations import InvestorError
from investment_agent.materials.investor_bridge import save_material_calculation
from investment_agent.materials.publication_prepare import prepare_material_publication
from tests.unit.test_material_publication_prepare import _four_year_dataset


async def test_reused_calculation_reserves_and_checks_explicit_operation_id(db_session, tmp_path):
    listing, dataset = await _four_year_dataset(db_session, tmp_path)
    as_of = datetime.now(UTC) + timedelta(seconds=1)
    report = await prepare_material_publication(
        db_session, listing_id=listing.id, dataset_ids=[dataset.id], as_of=as_of
    )
    metric = next(item for item in report.metrics if item.key == "revenue_yoy")
    assert metric.calculation_ref is not None
    saved = await EvidenceService(db_session).get_calculation(
        "local_owner", metric.calculation_ref, as_of
    )
    assert saved.data is not None
    operation = uuid4()
    arguments = dict(
        listing_id=listing.id, fact_refs=metric.fact_refs, results=saved.data.results,
        as_of=as_of + timedelta(seconds=1), operation_id=operation,
    )
    reused = await save_material_calculation(db_session, **arguments)
    assert reused == metric.calculation_ref
    assert await save_material_calculation(db_session, **arguments) == reused
    # A later visibility boundary may reuse the row, but cannot reuse a prior
    # operation ID with a different full request, even if a semantic row exists.
    with pytest.raises(InvestorError) as error:
        await save_material_calculation(
            db_session, **{**arguments, "as_of": as_of + timedelta(seconds=2)}
        )
    assert error.value.detail.code == "idempotency_conflict"
