"""In-app notice for a noteworthy review round (design §10.5).

Only two comparison outcomes notify: *high divergence* and a seat whose stance
flipped relative to the same seat's previous completed round. The notice is
written to the existing ``investor_notification`` inbox, so the user sees it
where every other in-app message appears. It is never sent to the external
webhook (no ``freeze`` call), and its wording never uses buy/sell/position
vocabulary.

The row is keyed ``review_round:<round_id>``: one round yields at most one
notice, so a retried finalisation cannot duplicate it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.review_panel.comparison import ComparisonResult
from investment_agent.review_panel.contracts import (
    COMPANY_STANCE_LABELS,
    DIMENSION_LABELS,
    CompanyStance,
)
from investment_agent.storage.models.investor import InvestorCase
from investment_agent.storage.models.monitoring import InvestorNotification
from investment_agent.storage.repositories.review_panel import RoundDetail

log = structlog.get_logger(__name__)

OWNER = "local_owner"

HIGH_DIVERGENCE = "high_divergence"
STANCE_FLIP = "stance_flip"

_DIVERGENCE_LEVEL_LABELS = {"high": "高", "medium": "中", "low": "低"}

# §10.5: the reminder wording contains no buy/sell/position terms. Kept as a
# module constant so tests can assert every rendered notice stays clear of it.
FORBIDDEN_REMINDER_WORDS: tuple[str, ...] = (
    "买入",
    "卖出",
    "减持",
    "增持",
    "加仓",
    "仓位",
    "建仓",
    "清仓",
    "止损",
    "止盈",
    "买入价",
    "卖出价",
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _lens_names(detail: RoundDetail) -> dict[str, str]:
    snapshot = detail.round.panel_snapshot
    seats = snapshot.get("seats") if isinstance(snapshot, dict) else None
    if not isinstance(seats, list):
        return {}
    names: dict[str, str] = {}
    for entry in seats:
        if not isinstance(entry, dict):
            continue
        seat_id = entry.get("seat_id")
        name = entry.get("lens_name")
        if isinstance(seat_id, str) and isinstance(name, str) and name.strip():
            names[seat_id] = name.strip()
    return names


def _stock_label(detail: RoundDetail) -> str:
    pack = detail.round.base_pack
    name: str | None = None
    ticker: str | None = None
    if isinstance(pack, dict):
        raw_ticker = pack.get("ticker")
        if isinstance(raw_ticker, str) and raw_ticker.strip():
            ticker = raw_ticker.strip()
        items = pack.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict) or item.get("ref") != "base:snapshot":
                    continue
                data = item.get("data")
                if isinstance(data, dict):
                    raw_name = data.get("company_name")
                    if isinstance(raw_name, str) and raw_name.strip():
                        name = raw_name.strip()
                break
    if name and ticker:
        return f"{name}（{ticker}）"
    if name:
        return name
    if ticker:
        return ticker
    return f"股票 #{detail.round.listing_id}"


def _stance_label(value: CompanyStance | None) -> str:
    if value is None:
        return "未知"
    return COMPANY_STANCE_LABELS.get(value, value.value)


def render_notification_text(
    detail: RoundDetail, comparison: ComparisonResult
) -> str | None:
    """The one-line Chinese notice, or ``None`` when nothing noteworthy happened."""

    reasons = set(comparison.notify_reasons)
    if not reasons:
        return None

    parts = [f"{_stock_label(detail)}多视角评审已完成"]
    if HIGH_DIVERGENCE in reasons:
        level = _DIVERGENCE_LEVEL_LABELS.get(
            comparison.divergence_level.value, comparison.divergence_level.value
        )
        parts.append(f"分歧程度：{level}")
        dimensions = [
            DIMENSION_LABELS[dimension] for dimension in comparison.divergent_dimensions
        ]
        if dimensions:
            parts.append("分歧维度：" + "、".join(dimensions[:3]))

    if STANCE_FLIP in reasons:
        lens_names = _lens_names(detail)
        flips: list[str] = []
        for change in comparison.seat_changes:
            if not change.stance_flipped:
                continue
            lens = lens_names.get(change.seat_id, change.seat_id)
            flips.append(
                f"{lens}席位由{_stance_label(change.previous_stance)}"
                f"转为{_stance_label(change.current_stance)}"
            )
        if flips:
            parts.append("立场变化：" + "；".join(flips[:3]))

    return "；".join(parts) + "。"


async def _effective_settings(session: AsyncSession) -> Settings:
    """Runtime settings (DB overlay); isolated so tests can replace it."""

    from investment_agent.settings.loader import load_settings

    return await load_settings(session)


def _flipped_seats(comparison: ComparisonResult) -> list[str]:
    return [
        change.seat_id for change in comparison.seat_changes if change.stance_flipped
    ]


def _payload(detail: RoundDetail, comparison: ComparisonResult) -> dict[str, Any]:
    round_row = detail.round
    reasons = list(comparison.notify_reasons)
    text = render_notification_text(detail, comparison)
    assert text is not None
    return {
        "category": "major_change" if HIGH_DIVERGENCE in reasons else "change",
        "stage": "reviewed",
        "reasons": [text],
        "review": {
            "round_id": round_row.id,
            "listing_id": round_row.listing_id,
            "divergence_level": comparison.divergence_level.value,
            "divergent_dimensions": [
                DIMENSION_LABELS[dimension]
                for dimension in comparison.divergent_dimensions
            ],
            "notify_reasons": reasons,
            "stance_flipped_seats": _flipped_seats(comparison),
        },
        "listing_ref": {
            "kind": "listing",
            "id": round_row.listing_id,
            "revision": 1,
        },
        "round_id": round_row.id,
        "result_refs": [
            {"kind": "review", "id": str(round_row.id), "revision": 1}
        ],
        "updated_at": _iso(round_row.completed_at) or datetime.now(UTC).isoformat(),
        "read": False,
    }


async def _write_notification(
    session: AsyncSession, detail: RoundDetail, comparison: ComparisonResult
) -> bool:
    round_row = detail.round
    key = f"review_round:{round_row.id}"
    existing = await session.scalar(
        select(InvestorNotification.sequence).where(InvestorNotification.key == key)
    )
    if existing is not None:
        return False
    case = await session.scalar(
        select(InvestorCase).where(
            InvestorCase.owner_id == OWNER,
            InvestorCase.listing_id == round_row.listing_id,
        )
    )
    if case is None:
        # Every in-app notice is case-scoped; without a case there is no inbox
        # entry the UI could resolve, so there is nothing to write.
        return False
    from investment_agent.investor.controls import UserControlService

    if not await UserControlService(session).notifications_allowed(UUID(str(case.id))):
        return False
    session.add(
        InvestorNotification(
            key=key,
            owner_id=OWNER,
            case_id=str(case.id),
            revision=1,
            payload=_payload(detail, comparison),
        )
    )
    await session.flush()
    return True


async def notify_round_completed(
    session: AsyncSession,
    detail: RoundDetail,
    comparison: ComparisonResult,
    *,
    settings: Settings | None = None,
) -> bool:
    """Write one in-app notice when the comparison asks for one.

    Best effort: the write runs in its own savepoint and any failure is logged
    and swallowed, so round finalisation never fails because of a notification.
    """

    try:
        if not comparison.notify_reasons:
            return False
        effective = settings or await _effective_settings(session)
        if not effective.review_panel_enabled:
            return False
        async with session.begin_nested():
            return await _write_notification(session, detail, comparison)
    except Exception:  # noqa: BLE001 - a notice must never fail finalisation
        log.exception(
            "review_panel.notification_failed",
            round_id=detail.round.id,
            listing_id=detail.round.listing_id,
        )
        return False


__all__ = [
    "FORBIDDEN_REMINDER_WORDS",
    "HIGH_DIVERGENCE",
    "STANCE_FLIP",
    "notify_round_completed",
    "render_notification_text",
]
