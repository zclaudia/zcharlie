from investment_agent.research.contracts import (
    FinancialDataItem,
    MarketDataItem,
    StockPoolItem,
)
from investment_agent.research.ifind_mapping import (
    IFIND_FIELD_MAPPINGS,
    mapping_for,
)


def test_mapping_registry_covers_every_scored_canonical_field_once():
    expected = {
        *(("stock_pool", field) for field in StockPoolItem.provenance_fields),
        *(("market", field) for field in MarketDataItem.provenance_fields),
        *(("financial", field) for field in FinancialDataItem.provenance_fields),
    }
    actual = {
        (mapping.payload_type, mapping.canonical_field)
        for mapping in IFIND_FIELD_MAPPINGS
    }

    assert actual == expected
    assert len(IFIND_FIELD_MAPPINGS) == len(actual)


def test_dividend_yield_is_an_explicit_review_grade_gap():
    mapping = mapping_for("market", "dividend_yield")

    assert mapping.status == "missing"
    assert mapping.source_fields == ()
