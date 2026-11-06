"""Review panel parsing and validation (design §8)."""

from __future__ import annotations

import json

import pytest

from investment_agent.review_panel.contracts import MAX_SEATS_PER_ROUND
from investment_agent.review_panel.panels import (
    ReviewPanelError,
    ReviewPanels,
    parse_review_panels,
    validate_review_panels,
)
from investment_agent.settings.agents import (
    CONSUMERS,
    REVIEW_PANEL,
    consumer_public,
    parse_model_slots,
)
from investment_agent.settings.registry import RuntimeSettings


def _seats(count: int = 2) -> list[dict[str, str]]:
    return [
        {
            "seat_id": f"seat{index}",
            "lens_id": "value",
            "model_ref": "primary/fast",
        }
        for index in range(count)
    ]


def _document(seats: list[dict[str, str]] | None = None) -> str:
    return json.dumps(
        {
            "default_panel": "default",
            "panels": {
                "default": {"name": "默认评审组", "seats": _seats() if seats is None else seats},
            },
        }
    )


def test_parse_valid_panel_returns_models() -> None:
    panels = parse_review_panels(_document())
    assert isinstance(panels, ReviewPanels)
    assert panels.default_panel == "default"
    assert panels.panels["default"].name == "默认评审组"
    assert [seat.seat_id for seat in panels.panels["default"].seats] == ["seat0", "seat1"]
    assert panels.panels["default"].seats[0].model_ref == "primary/fast"


def test_empty_value_means_not_configured() -> None:
    assert parse_review_panels("") == ReviewPanels()
    assert parse_review_panels("   ") == ReviewPanels()
    assert validate_review_panels("") == ""
    assert validate_review_panels("  ") == ""


def test_validate_canonicalizes_json() -> None:
    canonical = validate_review_panels(
        json.dumps({"panels": {"default": {"seats": _seats(1), "name": "组"}},
                    "default_panel": "default"})
    )
    assert canonical == json.dumps(
        {
            "default_panel": "default",
            "panels": {
                "default": {
                    "name": "组",
                    "seats": [{"lens_id": "value", "model_ref": "primary/fast", "seat_id": "seat0"}],
                }
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ("{", "不是合法 JSON"),
        ("[]", "必须是对象"),
        (json.dumps({"default_panel": "default"}), "至少需要一个评审组"),
        (json.dumps({"default_panel": "default", "panels": {}}), "至少需要一个评审组"),
        (
            json.dumps({"default_panel": "missing", "panels": {"default": {"name": "n", "seats": _seats(1)}}}),
            "默认评审组不存在",
        ),
        (
            json.dumps({"default_panel": "Default", "panels": {"default": {"name": "n", "seats": _seats(1)}}}),
            "默认评审组 id",
        ),
        (
            json.dumps({"default_panel": "default", "panels": {"default": {"name": "", "seats": _seats(1)}}}),
            "名称不能为空",
        ),
        (
            json.dumps({"default_panel": "default", "panels": {"default": {"name": "n", "seats": []}}}),
            "1–",
        ),
        (
            json.dumps({
                "default_panel": "default",
                "panels": {"default": {"name": "n", "seats": _seats(MAX_SEATS_PER_ROUND + 1)}},
            }),
            "1–",
        ),
        (
            json.dumps({
                "default_panel": "default",
                "panels": {"default": {"name": "n", "seats": [
                    {"seat_id": "same", "lens_id": "value", "model_ref": "primary/fast"},
                    {"seat_id": "same", "lens_id": "value", "model_ref": "primary/fast"},
                ]}},
            }),
            "不能重复",
        ),
        (
            json.dumps({
                "default_panel": "default",
                "panels": {"default": {"name": "n", "seats": [
                    {"seat_id": "seat", "lens_id": "value", "model_ref": "not-a-ref"},
                ]}},
            }),
            "provider/model",
        ),
        (
            json.dumps({
                "default_panel": "default",
                "panels": {"default": {"name": "n", "seats": [
                    {"seat_id": "seat", "lens_id": "value", "model_ref": "primary/fast", "extra": 1},
                ]}},
            }),
            "未知字段",
        ),
    ],
)
def test_invalid_documents_raise_chinese_errors(document: str, message: str) -> None:
    with pytest.raises(ReviewPanelError, match=message):
        parse_review_panels(document)


def test_error_never_echoes_a_credential_like_reference() -> None:
    secret = "sk-live-not-a-model-ref"
    document = json.dumps({
        "default_panel": "default",
        "panels": {"default": {"name": "n", "seats": [
            {"seat_id": "seat", "lens_id": "value", "model_ref": secret},
        ]}},
    })
    with pytest.raises(ReviewPanelError) as caught:
        parse_review_panels(document)
    assert secret not in str(caught.value)


def test_runtime_settings_validator_canonicalizes_review_panels() -> None:
    runtime = RuntimeSettings(review_panels=_document())
    assert runtime.review_panels == validate_review_panels(_document())
    with pytest.raises(ValueError, match="至少需要一个评审组"):
        RuntimeSettings(review_panels='{"default_panel": "default"}')


def test_review_panel_consumer_has_no_model_slots_and_parses() -> None:
    assert REVIEW_PANEL in CONSUMERS
    assert REVIEW_PANEL.slots == ()
    assert parse_model_slots('{"review_panel": {}}') == {"review_panel": {}}
    public = {item["id"]: item for item in consumer_public()}
    assert public["review_panel"]["slots"] == []
    assert public["review_panel"]["section_id"] == "review_panel"
    topics = {topic["id"]: topic for topic in public["review_panel"]["topics"]}
    assert topics["panels"]["keys"] == ["review_panels"]
    assert "review_panel_enabled" in topics["switches"]["keys"]
    quota_keys = [key for block in topics["quota"]["blocks"] for key in block["keys"]]
    assert "review_panel_seat_tokens" in quota_keys
