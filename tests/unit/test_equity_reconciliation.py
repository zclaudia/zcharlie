from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from investment_agent.materials.equity_reconciliation import (
    METHOD_VERSION,
    equity_reconciliation_ref,
    evaluate_equity_reconciliation,
)

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "tests/fixtures/materials/annual_validation/hsbc-restatement-reviewed-facts.json"
)


def _record() -> dict[str, object]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    source = fixture["source_document"]
    bridge = fixture["transition_bridge"]
    original = fixture["original_ifrs4"]
    restated = fixture["restated_ifrs17"]
    same_period = "2021-12-31"
    transition_locator = bridge["transition_table_locator"]
    transition_excerpt = bridge["transition_table_excerpt"]
    balance_locator = restated["locator"]
    balance_excerpt = restated["excerpt"]
    original_ref = {
        "document_id": source["raw_path"],
        "sha256": source["sha256"],
        "locator": original["reported_comparator"]["locator"],
        "excerpt": original["reported_comparator"]["excerpt"],
        "source_label": original["reported_comparator"]["source_label"],
        "published_on": source["published_on"],
    }
    return {
        "method_version": METHOD_VERSION,
        "inputs": [
            _input(
                "old_parent",
                bridge["ifrs4_total_shareholders_equity"],
                "ifrs4",
                transition_locator,
                transition_excerpt,
                "Total shareholders’ equity under IFRS 4",
                source,
            ),
            _input(
                "old_instruments",
                bridge["other_equity_instruments_under_ifrs4"],
                "ifrs4",
                balance_locator,
                balance_excerpt,
                "Other equity instruments under IFRS 4",
                source,
            ),
            {
                "id": "old_ordinary",
                "value": original["value"],
                "scale": 1_000_000,
                "currency": "USD",
                "period_end": same_period,
                "basis": "ifrs4",
                "source_ref": original_ref,
            },
            _input(
                "new_parent",
                bridge["ifrs17_total_shareholders_equity"],
                "ifrs17",
                transition_locator,
                transition_excerpt,
                "Total shareholders’ equity under IFRS 17",
                source,
            ),
            _input(
                "new_instruments",
                bridge["other_equity_instruments_under_ifrs17"],
                "ifrs17",
                balance_locator,
                balance_excerpt,
                "Other equity instruments under IFRS 17",
                source,
            ),
            *[
                _input(
                    f"adjustment_{index}",
                    component["value"],
                    "transition",
                    transition_locator,
                    transition_excerpt,
                    component["label"],
                    source,
                )
                for index, component in enumerate(
                    bridge["total_shareholders_equity_adjustment_components"], start=1
                )
            ],
        ],
    }


def _input(
    identifier: str,
    value: str,
    basis: str,
    locator: object,
    excerpt: str,
    source_label: str,
    source_document: dict[str, object],
) -> dict[str, object]:
    return {
        "id": identifier,
        "value": str(value),
        "scale": 1_000_000,
        "currency": "USD",
        "period_end": "2021-12-31",
        "basis": basis,
        "source_ref": {
            "document_id": source_document["raw_path"],
            "sha256": source_document["sha256"],
            "locator": locator,
            "excerpt": excerpt,
            "source_label": source_label,
            "published_on": source_document["published_on"],
        },
    }


def test_audited_ifrs17_ordinary_equity_is_computed_from_the_cited_bridge():
    record = _record()

    result = evaluate_equity_reconciliation(record)

    assert result["method_version"] == METHOD_VERSION
    assert result["output"] == {
        "metric": "equity_ordinary",
        "value": "166601",
        "scale": 1_000_000,
        "currency": "USD",
        "period_end": "2021-12-31",
        "equity_scope": "ordinary_shareholders",
        "basis": "ifrs17",
    }
    assert result["calculation_ref"] == f"sha256:{result['input_hash']}"
    assert result["calculation_ref"] == equity_reconciliation_ref(record)
    assert [item["id"] for item in result["inputs"]] == [
        "old_parent",
        "old_instruments",
        "old_ordinary",
        "new_parent",
        "new_instruments",
        "adjustment_1",
        "adjustment_2",
        "adjustment_3",
        "adjustment_4",
        "adjustment_5",
    ]
    # The source's original IFRS 4 balance remains a distinct input and value.
    assert (
        next(item for item in result["inputs"] if item["id"] == "old_ordinary")["value"] == "175836"
    )


def test_input_order_is_normalized_by_id_for_the_content_reference():
    record = _record()
    reversed_record = {**record, "inputs": list(reversed(record["inputs"]))}

    assert equity_reconciliation_ref(reversed_record) == equity_reconciliation_ref(record)


def test_amount_tampering_breaks_crosschecks_or_the_bound_content_reference():
    record = _record()
    with_ref = {**record, "calculation_ref": equity_reconciliation_ref(record)}

    changed_old_ordinary = copy.deepcopy(record)
    next(item for item in changed_old_ordinary["inputs"] if item["id"] == "old_ordinary")[
        "value"
    ] = "175837"
    with pytest.raises(ValueError, match="IFRS 4 parent equity"):
        evaluate_equity_reconciliation(changed_old_ordinary)

    changed_new_instruments = copy.deepcopy(with_ref)
    next(item for item in changed_new_instruments["inputs"] if item["id"] == "new_instruments")[
        "value"
    ] = "22413"
    with pytest.raises(ValueError, match="restated ordinary equity"):
        evaluate_equity_reconciliation(changed_new_instruments)

    changed_adjustment = copy.deepcopy(record)
    next(item for item in changed_adjustment["inputs"] if item["id"] == "adjustment_1")["value"] = (
        "92739"
    )
    with pytest.raises(ValueError, match="transition adjustments"):
        evaluate_equity_reconciliation(changed_adjustment)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("value", 198250),
        ("value", True),
        ("scale", True),
        ("scale", 1),
        ("currency", "HKD"),
        ("period_end", "2022-01-01"),
        ("basis", "ifrs17"),
    ),
)
def test_input_amount_metadata_and_basis_are_strict(field: str, value: object):
    record = _record()
    record["inputs"][0][field] = value

    with pytest.raises(ValueError):
        evaluate_equity_reconciliation(record)


def test_duplicate_missing_unknown_or_unversioned_inputs_are_rejected():
    record = _record()
    record["inputs"].append(copy.deepcopy(record["inputs"][0]))
    with pytest.raises(ValueError, match="duplicate"):
        evaluate_equity_reconciliation(record)

    record = _record()
    record["inputs"].pop()
    with pytest.raises(ValueError, match="input ids must be exact"):
        evaluate_equity_reconciliation(record)

    record = _record()
    record["inputs"][0]["id"] = "parent"
    with pytest.raises(ValueError, match="unknown reconciliation input id"):
        evaluate_equity_reconciliation(record)

    record = _record()
    record["method_version"] = "caller.supplied.expression"
    with pytest.raises(ValueError, match="unsupported reconciliation method_version"):
        evaluate_equity_reconciliation(record)

    record = _record()
    record["expression"] = "old_parent - old_instruments"
    with pytest.raises(ValueError, match="unsupported reconciliation record fields"):
        evaluate_equity_reconciliation(record)


def test_equity_scope_output_and_citation_shape_are_bound_and_checked():
    record = _record()
    result = evaluate_equity_reconciliation(record)
    record_with_output = {
        **record,
        "output": {**result["output"], "equity_scope": "total"},
    }
    with pytest.raises(ValueError, match="supplied output"):
        evaluate_equity_reconciliation(record_with_output)

    record = _record()
    with_ref = {**record, "calculation_ref": equity_reconciliation_ref(record)}
    citation_tampered = copy.deepcopy(with_ref)
    citation_tampered["inputs"][0]["source_ref"]["excerpt"] = "edited source excerpt"
    with pytest.raises(ValueError, match="canonical reconciliation hash"):
        evaluate_equity_reconciliation(citation_tampered)

    record = _record()
    record["inputs"][0]["source_ref"]["sha256"] = "not-a-digest"
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        evaluate_equity_reconciliation(record)
