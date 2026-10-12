"""SnapshotParameters whitelist: only registered direct snapshot fields pass.

White-box direct tests of the ``field`` registration guard
(``builtin_tools.SnapshotParameters``) that the agent-facing tool relies on:
every whitelisted field must construct, the JSON-schema enum must name exactly
the financial/market tables, and both rejection branches (unregistered name,
registered but non-direct table) must refuse with a readable error.
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from investment_agent.agent.builtin_tools import SnapshotParameters
from investment_agent.factors.field_sources import default_field_sources

DIRECT_TABLES = {"financial", "market"}


def allowed_fields() -> set[str]:
    sources = default_field_sources().fields
    return {name for name, source in sources.items() if source.table in DIRECT_TABLES}


def snapshot(field: str) -> dict:
    return {"ticker": "600036.SH", "field": field, "as_of": datetime.now(UTC)}


@pytest.mark.parametrize("field", sorted(allowed_fields()))
def test_every_registered_direct_field_constructs(field: str) -> None:
    parameters = SnapshotParameters.model_validate(snapshot(field))
    assert parameters.field == field
    assert parameters.report_period is None


def test_schema_enum_names_exactly_the_direct_fields() -> None:
    enum = SnapshotParameters.model_fields["field"].json_schema_extra["enum"]
    assert set(enum) == allowed_fields()
    assert len(enum) == len(set(enum))


@pytest.mark.parametrize("field", ["", "close ", "Close", "close;drop", "not_a_field"])
def test_unregistered_field_is_refused_with_a_readable_error(field: str) -> None:
    with pytest.raises(ValidationError, match="registered direct snapshot field"):
        SnapshotParameters.model_validate(snapshot(field))


@pytest.mark.parametrize(
    "field",
    [
        "eps_basic_fy",  # derived table
        "revenue_ps_fy",  # derived table
        "cash_dividend_per_share",  # dividend table
        "forecast_profit_high",  # earnings table
        "report_overdue",  # report_overdue table
    ],
)
def test_registered_but_non_direct_table_field_is_refused(field: str) -> None:
    source = default_field_sources().get(field)
    assert source is not None and source.table not in DIRECT_TABLES
    with pytest.raises(ValidationError, match="registered direct snapshot field"):
        SnapshotParameters.model_validate(snapshot(field))
