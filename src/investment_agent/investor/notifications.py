"""Delivery-only durable outbox projected from actual new station changes.

No model, regeneration or task creation. A frozen payload and stable id survive
retries/restarts. Unknown acknowledgement retries require explicit configured
receiver idempotency, otherwise stop for manual reconciliation.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.tasks import fingerprint
from investment_agent.config import Settings
from investment_agent.investor.controls import UserControlService
from investment_agent.investor.notification_http import HTTPSWebhook, validate_endpoint
from investment_agent.settings.loader import store_for
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.app_settings import AppSetting
from investment_agent.storage.models.monitoring import InvestorNotification, NotificationDelivery


class Transport(Protocol):
    async def send(self, *, endpoint: str, token: str, delivery_id: str, body: bytes) -> str: ...


async def configuration(session: AsyncSession, base: Settings) -> dict[str, object] | None:
    """Only explicit authenticated settings writes grant external publication.

    Environment/bootstrap/import and model outputs cannot authorize destinations.
    Secrets must actually decrypt from the store (no env fallback).
    """
    enabled = await session.get(AppSetting, "notification_external_enabled", populate_existing=True)
    endpoint_row = await session.get(AppSetting, "notification_webhook_url", populate_existing=True)
    if (
        not enabled
        or enabled.value_json is not True
        or enabled.updated_by not in {"api", "web"}
        or not endpoint_row
        or endpoint_row.updated_by not in {"api", "web"}
    ):
        return None
    state = await store_for(session, base).state()
    runtime = state.runtime
    endpoint = runtime.notification_webhook_url
    try:
        validate_endpoint(endpoint)
    except ValueError:
        return None
    idem = await session.get(AppSetting, "notification_webhook_idempotent", populate_existing=True)
    idempotent = bool(idem and idem.value_json is True and idem.updated_by in {"api", "web"})
    token = runtime.notification_webhook_token
    if (
        state.secrets["notification_webhook_url"].decrypt_error
        or state.secrets["notification_webhook_token"].decrypt_error
    ):
        return None
    if len(token) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        return None
    consent_at = enabled.updated_at
    if consent_at.tzinfo is None:
        consent_at = consent_at.replace(tzinfo=UTC)
    return {
        "endpoint": endpoint,
        "token": token,
        "idempotent": idempotent,
        "fingerprint": fingerprint(
            {
                "endpoint": endpoint,
                "token": token,
                "idempotent": idempotent,
                "consent_at": consent_at.astimezone(UTC).isoformat(),
            }
        ),
    }


async def freeze(session: AsyncSession, base: Settings, notification: InvestorNotification) -> None:
    config = await configuration(session, base)
    if config is None:
        return
    case = await UserControlService(session).get_case("local_owner", UUID(notification.case_id))
    data = notification.payload
    ident = fingerprint(
        {
            "key": notification.key,
            "revision": notification.revision,
            "config": config["fingerprint"],
        }
    )
    if await session.get(NotificationDelivery, ident) is not None:
        return
    # Coalesce undelivered revisions only. Never mutate any already-sent bytes.
    await session.execute(
        update(NotificationDelivery)
        .where(
            NotificationDelivery.notification_key == notification.key,
            NotificationDelivery.state.in_(("pending", "retry")),
        )
        .values(state="superseded")
    )
    refs = []
    for name in ("recommendation_ref", "original_recommendation_ref", "result_refs"):
        values = data.get(name, [])
        for ref in values if isinstance(values, list) else [values]:
            if isinstance(ref, dict) and ref.get("kind") in {"recommendation", "thesis", "review"}:
                refs.append({k: ref[k] for k in ("kind", "id", "revision") if k in ref})
    outstanding = (
        await session.scalar(
            select(func.count())
            .select_from(NotificationDelivery)
            .where(NotificationDelivery.state.in_(("pending", "retry", "sending")))
        )
        or 0
    )
    created_at = datetime.now(UTC).isoformat()
    body = {
        "schema_version": 1,
        "id": ident,
        "case_id": notification.case_id,
        "revision": notification.revision,
        "category": data.get("category")
        if data.get("category") in {"major_change", "needs_input"}
        else "change",
        "stage": data.get("stage")
        if data.get("stage") in {"review_required", "reviewed", "published", "needs_input"}
        else "changed",
        "refs": refs[:20],
    }
    await session.execute(
        insert(NotificationDelivery)
        .values(
            id=ident,
            notification_key=notification.key,
            case_id=notification.case_id,
            state="pending" if outstanding < 256 else "capacity_blocked",
            payload={
                "body": body,
                "case_revision": case.revision,
                "configuration": config["fingerprint"],
                "idempotent": config["idempotent"],
                "attempts": 0,
                "created_at": created_at,
                "available_at": created_at,
            },
        )
        .on_conflict_do_nothing()
    )


async def deliver_one(
    base: Settings, *, transport: Transport | None = None, now: datetime | None = None
) -> str | None:
    at = now or datetime.now(UTC)
    async with session_scope() as session:
        # Short writer reservation serializes independent deliverers and config
        # checks; commit sending before any socket can be opened.
        await session.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.id == "")
            .values(state="pending")
        )
        config = await configuration(session, base)
        rows = list(
            await session.scalars(
                select(NotificationDelivery)
                .where(
                    or_(
                        and_(
                            NotificationDelivery.state.in_(("pending", "retry")),
                            NotificationDelivery.payload["available_at"].as_string()
                            <= at.isoformat(),
                        ),
                        and_(
                            NotificationDelivery.state == "sending",
                            NotificationDelivery.payload["sent_at"].as_string()
                            <= (at - timedelta(seconds=30)).isoformat(),
                        ),
                    )
                )
                .order_by(
                    NotificationDelivery.payload["created_at"].as_string(), NotificationDelivery.id
                )
                .limit(100)
            )
        )
        selected = None
        for row in rows:
            data = row.payload
            current = await session.scalar(
                select(InvestorNotification).where(InvestorNotification.key == row.notification_key)
            )
            if current is None or current.revision != data["body"]["revision"]:
                row.state = "superseded"
                if "sent_at" in data:
                    row.payload = {**data, "prior_acknowledgement": "unknown"}
                continue
            if row.state == "sending":
                if datetime.fromisoformat(data["sent_at"]) + timedelta(seconds=30) > at:
                    continue
                # Crash in the sending window has unknown external effect.
                row.state = "retry" if data["idempotent"] and data["attempts"] < 3 else "unknown"
                data = {**data, "available_at": at.isoformat()}
                row.payload = data
                if row.state == "unknown":
                    continue
            if datetime.fromisoformat(data["available_at"]) > at:
                continue
            control = UserControlService(session)
            case = await control.get_case("local_owner", UUID(row.case_id))
            if (
                config is None
                or config["fingerprint"] != data["configuration"]
                or not await control.notifications_allowed(UUID(row.case_id))
                or case.background_paused
                or case.status == "archived"
                or case.revision != data["case_revision"]
            ):
                row.state = "suppressed"
                continue
            selected = row
            break
        if selected is None:
            return None
        ident = selected.id
        selected.state = "sending"
        attempt = int(selected.payload["attempts"]) + 1
        selected.payload = {**selected.payload, "attempts": attempt, "sent_at": at.isoformat()}
        body = json.dumps(
            selected.payload["body"], sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        idempotent = bool(selected.payload["idempotent"])
    assert config is not None
    try:
        outcome = await (transport or HTTPSWebhook()).send(
            endpoint=str(config["endpoint"]),
            token=str(config["token"]),
            delivery_id=ident,
            body=body,
        )
    except Exception:
        # No exception text persisted: URLs/tokens can occur in socket errors.
        outcome = "unknown"
    if outcome not in {"delivered", "failed", "unknown"}:
        outcome = "unknown"
    async with session_scope() as session:
        await session.execute(
            update(NotificationDelivery)
            .where(
                NotificationDelivery.id == ident,
                NotificationDelivery.state == "sending",
                NotificationDelivery.payload["attempts"].as_integer() == attempt,
            )
            .values(
                state="retry" if outcome == "unknown" and idempotent and attempt < 3 else outcome
            )
        )
        settled = await session.get(NotificationDelivery, ident, populate_existing=True)
        if settled and settled.state == "retry":
            settled.payload = {
                **settled.payload,
                "available_at": (at + timedelta(seconds=30 * attempt)).isoformat(),
            }
    return outcome
