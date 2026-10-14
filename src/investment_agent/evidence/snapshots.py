"""Trusted snapshot materialization: explicit calibrated units, no model metadata."""

import hashlib
import json
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal
from uuid import NAMESPACE_URL, uuid5

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.evidence.contracts import EvidenceRecord, Period
from investment_agent.factors.field_resolution import PROVIDER_FIELD_RENAMES, factor_fields
from investment_agent.factors.field_sources import default_field_sources
from investment_agent.investor.common import ObjectRef, Quantity
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot, MarketSnapshot

# Units follow the canonical field registry and calibrated snapshot adapters.
# Deliberately omit ambiguous/unregistered fields, FCFE and derived inputs.
UNITS: dict[str, str] = {
    "close": "CNY/share",
    "pe_ttm": "ratio",
    "pb_mrq": "ratio",
    "ps_ttm": "ratio",
    "turnover": "fraction",
    "total_shares": "shares",
    "roe": "fraction",
    "gross_margin": "fraction",
    "net_margin": "fraction",
    "eps_ttm": "CNY/share",
    "debt_ratio": "fraction",
    "total_equity": "CNY",
    "net_profit_parent": "CNY",
    "net_profit_total": "CNY",
    "revenue_main": "CNY",
    "revenue_total": "CNY",
    "operating_cash_flow": "CNY",
    "capital_expenditure": "CNY",
    "free_cash_flow": "CNY",
    "cfo_to_np": "ratio",
}


def digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, SHANGHAI_TZ).astimezone(UTC)


def materialize(row: FinancialSnapshot | MarketSnapshot, field: str) -> EvidenceRecord:
    registry = default_field_sources()
    source = registry.get(field)
    if field not in UNITS or source is None:
        raise InvestorError("invalid_reference", "Field has no trusted unit adapter")
    financial = isinstance(row, FinancialSnapshot)
    if source.table != ("financial" if financial else "market"):
        raise InvestorError("invalid_reference", "Field does not belong to this snapshot table")
    value_decimal: Decimal | None
    conflict: Literal["none", "unresolved", "resolved"] = "none"
    if isinstance(row, FinancialSnapshot):
        if row.available_from is None or row.availability == "unknown":
            raise InvestorError("invalid_reference", "Snapshot availability is unknown")
        if row.provider not in PROVIDER_FIELD_RENAMES:
            raise InvestorError("invalid_reference", "Unsupported financial source adapter")
        fields = factor_fields(row.provider, row.fields)
        value = fields.get(field)
        # Preserve the stored value exactly rather than a float round trip.
        names = PROVIDER_FIELD_RENAMES[row.provider]
        stored = next((key for key, target in names.items() if target == field), field)
        raw = row.fields.get(stored)
        if raw is not None and isinstance(raw, int | float) and not isinstance(raw, bool):
            value_decimal = Decimal(str(raw))
        else:
            value_decimal = Decimal(str(value)) if value is not None else None
        end = row.report_period
        if field in {"total_shares", "total_equity", "debt_ratio"}:
            period = Period(start=end, end=end, basis="point_in_time")
        elif field == "eps_ttm":
            period = Period(
                start=date(end.year - 1, end.month, end.day) + timedelta(days=1),
                end=end,
                basis="trailing_twelve_months",
            )
        else:
            period = Period(
                start=date(end.year, 1, 1), end=end, basis=f"period_to_date/{row.period_kind}"
            )
        available = day_start(row.available_from)
        locator = f"fields.{stored}"
        verification = row.fields.get("verification")
        verified = isinstance(verification, dict) and verification.get(field) == "verified"
        conflicts = row.fields.get("provider_conflicts")
        if (isinstance(verification, dict) and verification.get(field) == "conflict") or (
            isinstance(conflicts, dict) and field in conflicts
        ):
            conflict = "unresolved"
        # pubDate/availability is a date, not an exact announcement timestamp.
        quality = f"availability={row.availability}; date precision; registry={registry.version}"
        suspect = (
            source.sanity_problem(row.provider, float(value_decimal))
            if value_decimal is not None
            else None
        )
        if suspect:
            value_decimal = None
            quality += f"; rejected={suspect}"
    else:
        raw_market = getattr(row, field, None)
        value_decimal = Decimal(str(raw_market)) if raw_market is not None else None
        end = row.trade_date
        period = Period(start=end, end=end, basis=f"daily/{row.adjust}")
        # Daily close is usable only after the trading session, never midnight.
        available = datetime.combine(end, time(15), SHANGHAI_TZ).astimezone(UTC)
        locator = field
        verified = False
        quality = f"daily close; registry={registry.version}"
    quantity = Quantity(
        value=value_decimal,
        unit=UNITS[field],
        currency="CNY" if UNITS[field].startswith("CNY") else None,
        unknown_reason="field_missing_or_rejected" if value_decimal is None else None,
    )
    ref = ObjectRef(
        kind="financial_snapshot" if financial else "market_snapshot",
        id=row.id,
        revision=row.version,
        field_path=locator,
    )
    subject = ObjectRef(kind="listing", id=row.listing_id, revision=1)
    captured = aware(row.captured_at)
    payload = {
        "source": ref.model_dump(mode="json"),
        "subject": subject.model_dump(mode="json"),
        "period": period.model_dump(mode="json"),
        "value": quantity.model_dump(mode="json"),
        "available_from": available.isoformat(),
        "captured_at": captured.isoformat(),
        "provider": row.provider,
        "quality": quality,
    }
    return EvidenceRecord(
        id=uuid5(NAMESPACE_URL, digest(payload)),
        revision=1,
        created_at=captured,
        actor="snapshot_adapter",
        origin_ref=None,
        source_ref=ref,
        source_kind="snapshot",
        locator=locator,
        content_hash=digest(payload),
        subject_ref=subject,
        period=period,
        published_at=available,
        acquired_at=captured,
        available_from=available,
        verification="verified" if verified else "candidate",
        conflict=conflict,
        value=quantity,
        text=quality,
    )
