"""Review lenses config document: baseline, validation, and runtime loader."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from investment_agent.config_docs import loader
from investment_agent.review_panel.contracts import Dimension
from investment_agent.review_panel.lenses import (
    DEFAULT_REVIEW_LENSES_PATH,
    ReviewLensError,
    load_review_lenses,
    parse_review_lenses,
)


@pytest.fixture(autouse=True)
def _isolated_loader_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(loader, "_refresh_broken", False)
    loader.invalidate()
    yield
    loader.invalidate()


def _lens(**overrides: Any) -> dict[str, Any]:
    document: dict[str, Any] = {
        "id": "value",
        "version": 1,
        "name": "价值视角",
        "focus": "经营价值与安全边际",
        "prompt": "从价值视角分析这家公司。",
        "required_dimensions": ["valuation", "cash_flow_quality"],
    }
    document.update(overrides)
    return document


def _document(*lenses: dict[str, Any]) -> dict[str, Any]:
    return {"version": "test-1", "lenses": list(lenses)}


def test_baseline_file_parses_with_the_four_builtin_lenses() -> None:
    lenses = load_review_lenses(DEFAULT_REVIEW_LENSES_PATH)

    assert lenses.ids == ("value", "growth", "bear", "risk")
    assert [lens.ref for lens in lenses.lenses] == [
        "value.v1",
        "growth.v1",
        "bear.v1",
        "risk.v1",
    ]
    for lens in lenses.lenses:
        assert lens.name and lens.focus and lens.prompt.strip()
        assert lens.required_dimensions
        assert all(isinstance(dimension, Dimension) for dimension in lens.required_dimensions)
        assert len(lens.prompt.splitlines()) >= 6

    value = lenses.require("value")
    assert {
        Dimension.VALUATION,
        Dimension.CASH_FLOW_QUALITY,
    } <= set(value.required_dimensions)
    growth = lenses.require("growth")
    assert {Dimension.REVENUE_GROWTH, Dimension.MARGIN} <= set(growth.required_dimensions)
    bear = lenses.require("bear")
    assert len(bear.required_dimensions) >= 2
    risk = lenses.require("risk")
    assert {Dimension.DATA_QUALITY, Dimension.GOVERNANCE} <= set(risk.required_dimensions)


def test_runtime_accessor_reads_the_baseline_on_a_cold_cache() -> None:
    lenses = load_review_lenses()

    assert lenses.ids == ("value", "growth", "bear", "risk")
    assert lenses.get("missing") is None
    with pytest.raises(KeyError):
        lenses.require("missing")


def test_mapping_form_defaults_the_id_from_the_key() -> None:
    entry = _lens(required_dimensions=["valuation"])
    del entry["id"]
    lenses = parse_review_lenses({"lenses": {"bear": entry}})

    assert lenses.ids == ("bear",)
    assert lenses.require("bear").required_dimensions == (Dimension.VALUATION,)


@pytest.mark.parametrize(
    "document",
    [
        ["not", "a", "mapping"],
        {"lenses": []},
        {"lenses": "nope"},
        {"lenses": ["not a mapping"]},
        _document(_lens(), _lens(name="duplicate")),
        _document(_lens(id="Value")),
        _document(_lens(id="1value")),
        _document(_lens(id="a" * 41)),
        _document(_lens(version=0)),
        _document(_lens(name="")),
        _document(_lens(focus="   ")),
        _document(_lens(prompt="  \n  ")),
        _document(_lens(required_dimensions=[])),
        _document(_lens(required_dimensions=["valuation", "valuation"])),
        _document(_lens(required_dimensions=["not_a_dimension"])),
        _document(_lens(unknown_field=True)),
    ],
)
def test_invalid_documents_raise_review_lens_error(document: Any) -> None:
    with pytest.raises(ReviewLensError):
        parse_review_lenses(document)


def test_review_lens_error_is_a_value_error() -> None:
    assert issubclass(ReviewLensError, ValueError)
