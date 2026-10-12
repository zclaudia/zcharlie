"""Notification transport and settings helpers (moved from tests/test_external_notifications.py)."""

from cryptography.fernet import Fernet

from investment_agent.settings.loader import store_for
from investment_agent.storage.database import session_scope
from tests.support.conversations import base


class FixtureTransport:
    def __init__(self, outcomes=("delivered",)):
        self.calls = []
        self.outcomes = iter(outcomes)

    async def send(self, **kwargs):
        self.calls.append(kwargs)
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return result


async def enable(*, idempotent=False, actor="api"):
    settings = base(settings_encryption_key=Fernet.generate_key().decode())
    async with session_scope() as session:
        await store_for(session, settings).apply(
            {
                "notification_external_enabled": True,
                "notification_webhook_url": "https://receiver.example/notify",
                "notification_webhook_token": "fixture-token",
                "notification_webhook_idempotent": idempotent,
            },
            actor=actor,
        )
    return settings
