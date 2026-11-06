"""Explicit model-reference resolution for review seats (design §4.1)."""

from __future__ import annotations

import json

import pytest

from investment_agent.config import Settings
from investment_agent.review_panel.contracts import REQUIRED_SEAT_CAPABILITIES
from investment_agent.settings.resolution import (
    ModelUnavailableError,
    resolve_model_ref,
    resolve_slot,
)
from tests.support.llm_catalog import catalog_values, slot_values

LABEL = "多视角评审 / 价值席位"


def _settings(catalog: dict[str, object] | None = None) -> Settings:
    return Settings(
        _env_file=None,
        api_key="fixture",
        llm_catalog=json.dumps(catalog or catalog_values()),
        model_slots=json.dumps(slot_values()),
    )


def test_resolves_an_explicit_reference_with_required_capabilities() -> None:
    resolved = resolve_model_ref(
        _settings(),
        "primary/reason",
        required=REQUIRED_SEAT_CAPABILITIES,
        label=LABEL,
    )
    assert resolved.ref == "primary/reason"
    assert resolved.provider.id == "primary"
    assert resolved.model.id == "reason"
    assert resolved.selection.model_id == "reason"


def test_empty_reference_says_not_selected() -> None:
    with pytest.raises(ModelUnavailableError, match="未选择模型") as caught:
        resolve_model_ref(_settings(), "", required=(), label=LABEL)
    assert LABEL in str(caught.value)


def test_unknown_reference_is_unavailable_not_substituted() -> None:
    with pytest.raises(ModelUnavailableError, match="模型不可用"):
        resolve_model_ref(
            _settings(), "primary/missing", required=(), label=LABEL
        )


def test_disabled_model_is_unavailable() -> None:
    value = catalog_values()
    value["providers"][0]["models"][1]["enabled"] = False
    with pytest.raises(ModelUnavailableError, match="模型不可用"):
        resolve_model_ref(
            _settings(value), "primary/reason", required=(), label=LABEL
        )


def test_missing_capability_is_unavailable() -> None:
    with pytest.raises(ModelUnavailableError, match="模型不可用"):
        resolve_model_ref(
            _settings(), "primary/fast", required=("thinking",), label=LABEL
        )


def test_resolve_slot_keeps_its_messages() -> None:
    with pytest.raises(ModelUnavailableError, match="投资 Agent / 主模型：未选择模型"):
        resolve_slot(Settings(_env_file=None, api_key="fixture"), "investor", "main")
    with pytest.raises(ModelUnavailableError, match="模型不可用"):
        resolve_slot(
            Settings(
                _env_file=None,
                api_key="fixture",
                llm_catalog=json.dumps(catalog_values()),
                model_slots=json.dumps({"investor": {"main": "primary/missing"}}),
            ),
            "investor",
            "main",
        )
