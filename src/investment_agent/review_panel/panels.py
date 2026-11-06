"""Review panel configuration: named groups of lens/model seats (design §8).

``review_panels`` is a JSON string setting:

.. code-block:: json

    {"default_panel": "default",
     "panels": {"default": {"name": "默认四席",
                            "seats": [{"seat_id": "value", "lens_id": "value",
                                       "model_ref": "provider/model"}]}}}

Only shape, ids and model-reference syntax are checked here. Whether a lens
exists is checked when a round is created, because lenses are a separate
editable config document (``config/review_lenses.yaml``). Model availability is
checked by :func:`investment_agent.settings.resolution.resolve_model_ref`.

Messages are user-facing Chinese and never echo model-reference text, so a
mistyped credential-looking value cannot surface in an API error.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field

from investment_agent.domain.llm_catalog import CatalogError, split_model_ref
from investment_agent.review_panel.contracts import MAX_SEATS_PER_ROUND

_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_TOP_KEYS = frozenset({"default_panel", "panels"})
_PANEL_KEYS = frozenset({"name", "seats"})
_SEAT_KEYS = frozenset({"seat_id", "lens_id", "model_ref"})
_MAX_NAME_LENGTH = 80


class ReviewPanelError(ValueError):
    """The review-panel document is invalid. The message names no credentials."""


class SeatSpec(BaseModel):
    """One configured seat: a lens bound to a model reference."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    seat_id: str
    lens_id: str
    model_ref: str


class PanelSpec(BaseModel):
    """A named panel; seats are variable-length (never fixed model slots)."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    name: str
    seats: list[SeatSpec] = Field(min_length=1, max_length=MAX_SEATS_PER_ROUND)


class ReviewPanels(BaseModel):
    """The whole ``review_panels`` setting. Empty means "not configured"."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_panel: str = ""
    panels: dict[str, PanelSpec] = Field(default_factory=dict)


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID_PATTERN.fullmatch(value) is not None


def _object(value: object, message: str) -> dict[object, object]:
    if not isinstance(value, dict):
        raise ReviewPanelError(message)
    return value


def _check_keys(value: dict[object, object], allowed: frozenset[str], message: str) -> None:
    if any(key not in allowed for key in value):
        raise ReviewPanelError(message)


def _parse_seat(raw: object, *, panel_id: str) -> SeatSpec:
    seat = _object(raw, f"评审组 {panel_id} 的席位必须是对象")
    _check_keys(seat, _SEAT_KEYS, f"评审组 {panel_id} 的席位包含未知字段")
    seat_id = seat.get("seat_id")
    lens_id = seat.get("lens_id")
    model_ref = seat.get("model_ref")
    if not _valid_id(seat_id):
        raise ReviewPanelError(
            "席位 id 必须小写字母开头，只能包含小写字母、数字和下划线，最长 40 个字符"
        )
    if not _valid_id(lens_id):
        raise ReviewPanelError(
            "视角 id 必须小写字母开头，只能包含小写字母、数字和下划线，最长 40 个字符"
        )
    if not isinstance(model_ref, str) or not model_ref.strip():
        raise ReviewPanelError("每个席位都要选择一个模型")
    model_ref = model_ref.strip()
    try:
        split_model_ref(model_ref)
    except CatalogError:
        raise ReviewPanelError("模型引用格式应为 provider/model") from None
    assert isinstance(seat_id, str) and isinstance(lens_id, str)
    return SeatSpec(seat_id=seat_id, lens_id=lens_id, model_ref=model_ref)


def _parse_panel(panel_id: str, raw: object) -> PanelSpec:
    panel = _object(raw, f"评审组 {panel_id} 必须是对象")
    _check_keys(panel, _PANEL_KEYS, f"评审组 {panel_id} 包含未知字段")
    name = panel.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ReviewPanelError(f"评审组 {panel_id} 的名称不能为空")
    if len(name.strip()) > _MAX_NAME_LENGTH:
        raise ReviewPanelError(f"评审组 {panel_id} 的名称过长")
    seats_raw = panel.get("seats")
    if not isinstance(seats_raw, list):
        raise ReviewPanelError(f"评审组 {panel_id} 的席位必须是列表")
    if not 1 <= len(seats_raw) <= MAX_SEATS_PER_ROUND:
        raise ReviewPanelError(
            f"每个评审组需要 1–{MAX_SEATS_PER_ROUND} 个席位"
        )
    seats = [_parse_seat(seat, panel_id=panel_id) for seat in seats_raw]
    if len({seat.seat_id for seat in seats}) != len(seats):
        raise ReviewPanelError(f"评审组 {panel_id} 内的席位 id 不能重复")
    return PanelSpec(name=name.strip(), seats=seats)


def parse_review_panels(value: str) -> ReviewPanels:
    """Parse and validate the stored setting; ``""`` means not configured."""

    if not isinstance(value, str):
        raise ReviewPanelError("评审组配置必须是字符串")
    if not value.strip():
        return ReviewPanels()
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        raise ReviewPanelError("评审组配置不是合法 JSON") from None
    document = _object(raw, "评审组配置必须是对象")
    _check_keys(document, _TOP_KEYS, "评审组配置包含未知字段")
    default_panel = document.get("default_panel")
    if not _valid_id(default_panel):
        raise ReviewPanelError(
            "默认评审组 id 必须小写字母开头，只能包含小写字母、数字和下划线，最长 40 个字符"
        )
    panels_raw = document.get("panels")
    if not isinstance(panels_raw, dict) or not panels_raw:
        raise ReviewPanelError("至少需要一个评审组")
    panels: dict[str, PanelSpec] = {}
    for panel_id, body in panels_raw.items():
        if not _valid_id(panel_id):
            raise ReviewPanelError(
                "评审组 id 必须小写字母开头，只能包含小写字母、数字和下划线，最长 40 个字符"
            )
        panels[panel_id] = _parse_panel(panel_id, body)
    assert isinstance(default_panel, str)
    if default_panel not in panels:
        raise ReviewPanelError("默认评审组不存在")
    return ReviewPanels(default_panel=default_panel, panels=panels)


def validate_review_panels(value: str) -> str:
    """Canonical JSON for storage; an empty/blank value stays not configured."""

    if not isinstance(value, str):
        raise ReviewPanelError("评审组配置必须是字符串")
    if not value.strip():
        return ""
    parsed = parse_review_panels(value)
    return json.dumps(
        parsed.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


__all__ = [
    "PanelSpec",
    "ReviewPanelError",
    "ReviewPanels",
    "SeatSpec",
    "parse_review_panels",
    "validate_review_panels",
]
