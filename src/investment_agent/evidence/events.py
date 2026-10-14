"""Saved Eastmoney events remain forecasts/schedules/segment facts, never realized earnings."""

import json
from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from investment_agent.evidence.contracts import EvidenceRecord, Period
from investment_agent.evidence.snapshots import aware, day_start, digest
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.earnings_events import (
    BusinessSegmentRecord,
    DisclosureScheduleRecord,
    EarningsForecastRecord,
)

EventRow = EarningsForecastRecord | DisclosureScheduleRecord | BusinessSegmentRecord

EventModel = type[EventRow]
"""The concrete row classes behind :data:`EventRow`, as values for ``session.get``."""

EVENT_MODELS: dict[str, EventModel] = {
    "earnings_forecast": EarningsForecastRecord,
    "disclosure_schedule": DisclosureScheduleRecord,
    "business_segment": BusinessSegmentRecord,
}
"""The single home of the saved-event object-kind -> row-model mapping."""


def event_model(kind: str) -> EventModel | None:
    """The saved-event row model for an object kind, or ``None`` when it is not one."""
    return EVENT_MODELS.get(kind)


def event_material(row: EventRow, field: str) -> EvidenceRecord:
    if row.provider != "akshare":
        raise InvestorError("invalid_reference", "Event provider has no trusted adapter")
    captured = aware(row.captured_at)
    kind: Literal["earnings_forecast", "business_segment", "disclosure_schedule"]
    value = None
    if isinstance(row, EarningsForecastRecord):
        kind = "earnings_forecast"
        allowed = {
            "profit_low": "CNY",
            "profit_high": "CNY",
            "yoy_low": "fraction",
            "yoy_high": "fraction",
        }
        text = json.dumps(
            {
                "forecast_type": row.forecast_type,
                "indicator": row.indicator,
                "content": row.content,
                "notice_date": row.notice_date.isoformat(),
                "quality": "forecast; notice date precision; not realized earnings",
            },
            ensure_ascii=False,
        )
        available = day_start(row.notice_date)
    elif isinstance(row, BusinessSegmentRecord):
        kind = "business_segment"
        allowed = {"revenue": "CNY", "profit": "CNY", "share": "fraction", "margin": "fraction"}
        text = json.dumps(
            {
                "kind": row.kind,
                "name": row.name,
                "quality": "segment; publication time unknown; visibility starts at capture",
            },
            ensure_ascii=False,
        )
        available = captured
    else:
        kind = "disclosure_schedule"
        allowed = {}
        text = json.dumps(
            {
                "first_date": row.first_date.isoformat(),
                "scheduled_date": row.scheduled_date.isoformat(),
                "actual_date": row.actual_date.isoformat() if row.actual_date else None,
                "quality": "appointment; publication time unknown; visibility starts at capture",
            }
        )
        available = captured
    if field != "text":
        if field not in allowed:
            raise InvestorError("invalid_reference", "Event field has no trusted adapter")
        raw = getattr(row, field)
        unit = allowed[field]
        value = Quantity(
            value=Decimal(str(raw)) if raw is not None else None,
            unit=unit,
            currency="CNY" if unit == "CNY" else None,
            unknown_reason="field_missing" if raw is None else None,
        )
    ref = ObjectRef(kind=kind, id=row.id, revision=1, field_path=field)
    period = Period(
        start=date(row.report_period.year, 1, 1), end=row.report_period, basis="event_report_period"
    )
    subject = ObjectRef(kind="listing", id=row.listing_id, revision=1)
    content_hash = digest(
        {
            "source": ref.model_dump(mode="json"),
            "subject": subject.model_dump(mode="json"),
            "period": period.model_dump(mode="json"),
            "text": text,
            "value": value.model_dump(mode="json") if value else None,
            "provider": row.provider,
            "captured_at": captured.isoformat(),
        }
    )
    return EvidenceRecord(
        id=uuid5(NAMESPACE_URL, content_hash),
        revision=1,
        created_at=captured,
        actor="event_adapter",
        origin_ref=None,
        source_ref=ref,
        source_kind="snapshot",
        locator=field,
        content_hash=content_hash,
        subject_ref=subject,
        period=period,
        published_at=available,
        acquired_at=captured,
        available_from=available,
        verification="candidate",
        conflict="none",
        text=text,
        value=value,
    )
