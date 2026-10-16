"""Independent compatibility checks for the P1 account-fit review fixes."""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from investment_agent.investor.common import ObjectRef
from investment_agent.portfolio.purposes import Purpose, PurposeVersion
from investment_agent.portfolio.suitability_calculations import evaluate_fit
from investment_agent.portfolio.suitability_contracts import PurposeBinding
from tests.support.account_fit import fit_request
from tests.support.account_fit_db import make_inputs


def test_old_purpose_snapshot_is_retained_as_unknown_instead_of_rejected() -> None:
    """A historical purpose is valid evidence, but it cannot prove current fit."""
    inputs = make_inputs()
    purpose_ref = ObjectRef(kind="portfolio_purposes", id=uuid4(), revision=1)
    purpose = Purpose(
        id=uuid4(),
        name="historical reserve",
        amount=Decimal("1000"),
        available_budget=Decimal("500"),
        horizon="short_term",
        required_cash=Decimal("100"),
        required_on=date(2026, 12, 31),
        source="synthetic_fixture",
        source_version="v1",
    )
    old_snapshot = ObjectRef(kind="portfolio_snapshot", id=uuid4(), revision=1)
    version = PurposeVersion(
        reference=purpose_ref,
        account_id=inputs.account_id,
        snapshot_ref=old_snapshot,
        confirmed_at=inputs.assessment_as_of,
        purposes=(purpose,),
        confirmed_cash=Decimal("1000"),
        unallocated=Decimal("0"),
        semantic_hash="a" * 64,
    )
    frozen = type(inputs).model_validate(
        inputs.model_dump(mode="python")
        | {
            "purpose_version": version,
            "purpose_binding": PurposeBinding(
                state="selected",
                purpose_ref=purpose_ref,
                purpose_id=purpose.id,
                confirmed_at=version.confirmed_at,
            ),
        }
    )

    assessment, _ = evaluate_fit(
        frozen,
        fit_request().items[0],
        operation_id=UUID("00000000-0000-0000-0000-000000000777"),
    )

    assert any(
        check.code == "purpose_snapshot_changed" and check.state == "unknown"
        for check in assessment.checks
    )
    assert assessment.frozen_inputs.purpose_version == version
