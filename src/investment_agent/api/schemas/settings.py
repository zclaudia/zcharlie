"""Schemas of the runtime settings endpoints (settings-plan §4.5).

Secret settings never carry a value in any response: only ``configured``,
the last-4 ``hint`` and a ``decrypt_error``. Non-secret items carry
``value`` / ``default``; responses are serialized with ``exclude_unset`` so
a secret item has no ``value`` key at all.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class SettingInputOut(BaseModel):
    type: str
    """``secret`` / ``select`` / ``multi`` / ``int`` / ``float`` / ``cron`` /
    ``timezone`` / ``url`` / ``text``."""
    nullable: bool = False
    options: list[str] | None = None
    min: float | None = None
    min_exclusive: bool | None = None
    max: float | None = None
    max_exclusive: bool | None = None
    min_items: int | None = None


class SettingItemOut(BaseModel):
    key: str
    group: str
    label: str
    help: str
    apply: str
    """``live`` / ``next_job`` / ``reschedule``."""
    source: str
    secret: bool
    stored: bool
    is_default: bool
    input: SettingInputOut
    value: Any = None
    default: Any = None
    configured: bool | None = None
    hint: str | None = None
    decrypt_error: str | None = None
    env_fallback: bool | None = None
    """Secret without a stored row whose env value is still in use."""
    next_run: datetime | None = None
    """Next fire time of an enabled schedule cron (in its section timezone)."""
    timezone: str | None = None


class SettingGroupOut(BaseModel):
    key: str
    title: str
    items: list[SettingItemOut]


class BootstrapItemOut(BaseModel):
    key: str
    label: str
    secret: bool = False
    value: Any = None
    configured: bool | None = None
    hint: str | None = None


class SettingsOut(BaseModel):
    revision: int
    encryption_key_configured: bool
    encryption_key_error: str | None = None
    groups: list[SettingGroupOut]
    bootstrap: list[BootstrapItemOut]
    llm_catalog: dict[str, Any] = Field(default_factory=dict)


class SettingsPatchRequest(BaseModel):
    values: dict[str, Any] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=500)
    expected_revision: int | None = Field(default=None, ge=0)


class SettingsPatchOut(BaseModel):
    changed: list[str]
    revision: int
    settings: SettingsOut


class SettingChangeOut(BaseModel):
    id: int
    key: str
    label: str | None
    old_value: Any = None
    new_value: Any = None
    changed_at: datetime
    actor: str
    note: str | None = None


class SettingsHistoryOut(BaseModel):
    items: list[SettingChangeOut]


class LlmTestRequest(BaseModel):
    use_legacy: bool = False
    provider_id: str | None = Field(default=None, max_length=100)
    model_id: str | None = Field(default=None, max_length=100)
    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=500)
    thinking: str | None = Field(default=None, pattern="^(auto|enabled|disabled)$")
    """Empty / omitted = the stored key."""


class IfindTestRequest(BaseModel):
    url: str | None = Field(default=None, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    """Empty / omitted = the stored key."""


class ConnectionTestOut(BaseModel):
    ok: bool
    latency_ms: int
    message: str
