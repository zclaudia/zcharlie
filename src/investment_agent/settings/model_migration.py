"""Move legacy agent_* / llm_tier3_* rows into providers, models, and slot refs.

The catalog stops recording who uses a model. Slot bindings live in
``model_slots``. Legacy keys are deleted. An empty database is a no-op.
Running the conversion again, after the legacy keys are gone, is also a no-op.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from investment_agent.domain.llm_catalog import LLMCatalog, model_ref
from investment_agent.settings.agents import validate_model_slots
from investment_agent.settings.crypto import SecretCipher

LEGACY_AGENT_KEYS = (
    "agent_provider", "agent_base_url", "agent_model", "agent_api_key", "agent_credential_ref",
    "agent_profile", "agent_profile_model", "agent_output_mode",
    "agent_supports_json_schema", "agent_supports_json_object", "agent_tool_choice_required",
    "agent_thinking", "agent_timeout_seconds", "agent_temperature", "agent_max_tokens", "agent_retries",
)
LEGACY_TIER3_KEYS = (
    "llm_tier3_base_url", "llm_tier3_model", "llm_tier3_api_key", "llm_tier3_thinking",
    "llm_rule_draft_thinking", "llm_request_timeout_seconds",
)
LEGACY_KEYS = LEGACY_AGENT_KEYS + LEGACY_TIER3_KEYS
_ACTOR = "migration-057"
_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class MigrationPlan:
    catalog: str | None
    slots: str | None
    delete: tuple[str, ...]

    @property
    def changed(self) -> bool:
        return self.catalog is not None or self.slots is not None or bool(self.delete)


def _slug(value: str, fallback: str) -> str:
    cleaned = _SLUG.sub("-", value).strip("-.")[:80]
    return cleaned or fallback


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    return str(value).lower() in {"1", "true", "yes"}


def _as_float(value: Any, default: float) -> float:
    if value in (None, ""):
        return default
    return float(value)


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _capabilities(model: dict[str, Any], *, tools: bool) -> list[str]:
    caps = list(model.get("capabilities") or [])
    if model.get("thinking") == "enabled" and "thinking" not in caps:
        caps.append("thinking")
    if tools and "tools" not in caps:
        caps.append("tools")
    if _as_bool(model.get("supports_json_object"), True) and "json_object" not in caps:
        caps.append("json_object")
    if _as_bool(model.get("supports_json_schema"), False) and "json_schema" not in caps:
        caps.append("json_schema")
    if _as_bool(model.get("tool_choice_required"), False) and "tool_choice" not in caps:
        caps.append("tool_choice")
    return caps


def _normalize_model(raw: dict[str, Any], *, tools: bool) -> dict[str, Any]:
    model = {
        "id": str(raw.get("id") or _slug(str(raw.get("model") or "model"), "model")),
        "name": str(raw.get("name") or raw.get("model") or raw.get("id") or "模型"),
        "model": str(raw.get("model") or raw.get("name") or "unspecified"),
        "enabled": _as_bool(raw.get("enabled"), True),
        "capabilities": _capabilities(raw, tools=tools),
        "thinking": raw.get("thinking") or "auto",
        "timeout_seconds": _as_float(raw.get("timeout_seconds"), 120),
        "profile": raw.get("profile") or "openai",
        "profile_model": raw.get("profile_model") or "",
        "output_mode": raw.get("output_mode") or "prompted",
        "supports_json_schema": _as_bool(raw.get("supports_json_schema"), False),
        "supports_json_object": _as_bool(raw.get("supports_json_object"), True),
        "tool_choice_required": _as_bool(raw.get("tool_choice_required"), False),
        "temperature": raw.get("temperature"),
        "max_tokens": _as_int(raw.get("max_tokens")),
        "retries": int(str(raw.get("retries"))) if raw.get("retries") not in (None, "") else 1,
    }
    if raw.get("context_length") not in (None, ""):
        model["context_length"] = int(str(raw["context_length"]))
    return model


def _ref_of(binding: dict[str, Any]) -> str:
    return model_ref(str(binding["provider_id"]), str(binding["model_id"]))


def _find(providers: list[dict[str, Any]], ref: str) -> dict[str, Any] | None:
    provider_id, _, model_id = ref.partition("/")
    for provider in providers:
        if provider["id"] != provider_id:
            continue
        models = provider.get("models")
        if not isinstance(models, list):
            continue
        for model in models:
            if isinstance(model, dict) and model.get("id") == model_id:
                return model
    return None


def _unique_id(existing: set[str], wanted: str) -> str:
    candidate = wanted
    index = 2
    while candidate in existing:
        candidate = f"{wanted}-{index}"
        index += 1
    return candidate


def _provider_for(
    providers: list[dict[str, Any]], *, base_url: str, kind: str, name: str, api_key: str, preferred_id: str,
) -> dict[str, Any]:
    for provider in providers:
        if provider["base_url"].rstrip("/") == base_url.rstrip("/") and provider["kind"] == kind:
            if api_key and not provider.get("api_key"):
                provider["api_key"] = api_key
            return provider
    provider = {
        "id": _unique_id({item["id"] for item in providers}, preferred_id),
        "name": name,
        "kind": kind,
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "models": [],
    }
    providers.append(provider)
    return provider


def _add_model(provider: dict[str, Any], raw: dict[str, Any], *, tools: bool) -> str:
    model = _normalize_model(raw, tools=tools)
    ids = {item["id"] for item in provider["models"]}
    model["id"] = _unique_id(ids, model["id"])
    provider["models"].append(model)
    return model_ref(provider["id"], model["id"])


def _agent_configured(rows: dict[str, str]) -> bool:
    return any(rows.get(key) for key in ("agent_base_url", "agent_model", "agent_api_key"))


def _tier3_configured(rows: dict[str, str]) -> bool:
    return any(key in rows for key in LEGACY_TIER3_KEYS)


def _agent_model(rows: dict[str, str]) -> dict[str, Any]:
    return {
        "id": "main",
        "name": rows.get("agent_model") or "投资 Agent",
        "model": rows.get("agent_model") or "unspecified",
        "thinking": rows.get("agent_thinking") or "auto",
        "timeout_seconds": rows.get("agent_timeout_seconds") or 120,
        "profile": rows.get("agent_profile") or "openai",
        "profile_model": rows.get("agent_profile_model") or "",
        "output_mode": rows.get("agent_output_mode") or "prompted",
        "supports_json_schema": rows.get("agent_supports_json_schema") or False,
        "supports_json_object": rows.get("agent_supports_json_object", "true"),
        "tool_choice_required": rows.get("agent_tool_choice_required") or False,
        "temperature": rows.get("agent_temperature") or None,
        "max_tokens": rows.get("agent_max_tokens") or None,
        "retries": rows.get("agent_retries") or 1,
    }


def _tier3_model(rows: dict[str, str], *, thinking: str | None = None) -> dict[str, Any]:
    mode = thinking if thinking not in (None, "") else (rows.get("llm_tier3_thinking") or "auto")
    return {
        "id": "explain" if thinking in (None, "", "auto") else "rule-draft",
        "name": rows.get("llm_tier3_model") or "研究解释",
        "model": rows.get("llm_tier3_model") or "gpt-5",
        "thinking": mode,
        "timeout_seconds": rows.get("llm_request_timeout_seconds") or 120,
        "profile": "openai",
        "supports_json_object": True,
    }


def _clone_thinking(providers: list[dict[str, Any]], ref: str, thinking: str) -> str:
    provider_id, _, model_id = ref.partition("/")
    provider = next(item for item in providers if item["id"] == provider_id)
    source = next(item for item in provider["models"] if item["id"] == model_id)
    if source.get("thinking") == thinking:
        return ref
    copied = dict(source)
    copied["id"] = _unique_id({item["id"] for item in provider["models"]}, f"{model_id}-rule-draft")
    copied["name"] = f"{source.get('name') or source.get('model')}（规则草拟）"
    copied["thinking"] = thinking
    copied["capabilities"] = list(source.get("capabilities") or [])
    if thinking == "enabled" and "thinking" not in copied["capabilities"]:
        copied["capabilities"].append("thinking")
    provider["models"].append(_normalize_model(copied, tools="tools" in copied["capabilities"]))
    return model_ref(provider["id"], copied["id"])


def plan_model_migration(rows: dict[str, str]) -> MigrationPlan:
    """``rows`` maps an existing setting key to its plaintext value."""

    present_legacy = tuple(key for key in LEGACY_KEYS if key in rows)
    raw_catalog = rows.get("llm_catalog", "")
    old = json.loads(raw_catalog) if raw_catalog else {"providers": [], "bindings": {}}
    if not isinstance(old, dict):
        old = {"providers": [], "bindings": {}}
    raw_bindings = old.get("bindings")
    bindings: dict[str, Any] = raw_bindings if isinstance(raw_bindings, dict) else {}
    had_bindings = bool(bindings)
    if not present_legacy and not had_bindings:
        return MigrationPlan(None, None, ())
    providers = []
    for provider in old.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        providers.append({
            "id": provider.get("id"),
            "name": provider.get("name") or provider.get("id") or "Provider",
            "kind": provider.get("kind") or "openai",
            "base_url": provider.get("base_url"),
            "api_key": provider.get("api_key") or "",
            "models": [_normalize_model(model, tools=False) for model in provider.get("models") or [] if isinstance(model, dict)],
        })
    slots: dict[str, dict[str, str]] = {}

    def bind(consumer: str, slot: str, ref: str) -> None:
        slots.setdefault(consumer, {})[slot] = ref

    if isinstance(bindings.get("agent"), dict):
        ref = _ref_of(bindings["agent"])
        model = _find(providers, ref)
        if model is not None and "tools" not in model["capabilities"]:
            model["capabilities"] = [*model["capabilities"], "tools"]
        bind("investor", "main", ref)
        if model is not None and "thinking" in model["capabilities"]:
            bind("investor", "thinking", ref)
    if isinstance(bindings.get("research"), dict):
        ref = _ref_of(bindings["research"])
        bind("research", "explain", ref)
        bind("rule_draft", "main", ref)

    if "main" not in slots.get("investor", {}) and _agent_configured(rows):
        provider = _provider_for(
            providers,
            base_url=rows.get("agent_base_url") or "https://api.openai.com/v1",
            kind=rows.get("agent_provider") or "openai",
            name="投资 Agent",
            api_key=rows.get("agent_api_key") or "",
            preferred_id="legacy-agent",
        )
        ref = _add_model(provider, _agent_model(rows), tools=True)
        bind("investor", "main", ref)
        if "thinking" in (_find(providers, ref) or {}).get("capabilities", []):
            bind("investor", "thinking", ref)

    override = rows.get("llm_rule_draft_thinking") or "auto"
    if "explain" not in slots.get("research", {}) and _tier3_configured(rows):
        provider = _provider_for(
            providers,
            base_url=rows.get("llm_tier3_base_url") or "https://api.openai.com/v1",
            kind="openai",
            name="研究解释",
            api_key=rows.get("llm_tier3_api_key") or "",
            preferred_id="legacy-research",
        )
        ref = _add_model(provider, _tier3_model(rows), tools=False)
        bind("research", "explain", ref)
        draft = ref
        if override not in {"", "auto"}:
            draft = _clone_thinking(providers, ref, override)
        bind("rule_draft", "main", draft)
    elif override not in {"", "auto"} and slots.get("rule_draft", {}).get("main"):
        slots["rule_draft"]["main"] = _clone_thinking(providers, slots["rule_draft"]["main"], override)

    if not providers and not slots and not present_legacy and not had_bindings:
        return MigrationPlan(None, None, ())
    catalog = LLMCatalog.model_validate({"providers": providers}).model_dump_json() if providers else ""
    slot_document = validate_model_slots(json.dumps(slots)) if slots else ""
    catalog_changed = catalog != (raw_catalog or "")
    slots_changed = slot_document != (rows.get("model_slots") or "")
    return MigrationPlan(
        catalog if catalog_changed else None,
        slot_document if slots_changed else None,
        present_legacy,
    )


def _plain(value: Any, ciphertext: str | None, cipher: SecretCipher | None) -> str:
    if ciphertext:
        if cipher is None:
            raise RuntimeError(
                "SETTINGS_ENCRYPTION_KEY is required to migrate stored model credentials"
            )
        return cipher.decrypt(ciphertext)
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def apply_model_migration(connection: sa.Connection) -> MigrationPlan:
    """Convert rows on an open connection. Safe to run when there is nothing to convert."""

    table = sa.table(
        "app_setting",
        sa.column("key"), sa.column("value_json", sa.JSON()), sa.column("ciphertext"), sa.column("secret_hint"),
        sa.column("updated_at"), sa.column("updated_by"),
    )
    stored = {
        row._mapping["key"]: row._mapping
        for row in connection.execute(sa.select(table.c.key, table.c.value_json, table.c.ciphertext))
    }
    interesting = {"llm_catalog", "model_slots", *LEGACY_KEYS}
    if not interesting & set(stored):
        return MigrationPlan(None, None, ())
    needs_cipher = any(stored[key]["ciphertext"] for key in interesting if key in stored)
    cipher = SecretCipher(os.environ.get("SETTINGS_ENCRYPTION_KEY", "")) if needs_cipher else None
    rows = {
        key: _plain(stored[key]["value_json"], stored[key]["ciphertext"], cipher)
        for key in interesting if key in stored
    }
    plan = plan_model_migration(rows)
    if not plan.changed:
        return plan
    now = datetime.now(UTC)
    if plan.catalog is not None:
        if cipher is None:
            raise RuntimeError("SETTINGS_ENCRYPTION_KEY is required to store the migrated model catalog")
        _upsert_secret(connection, "llm_catalog", cipher.encrypt(plan.catalog), now)
    if plan.slots is not None:
        _upsert_value(connection, "model_slots", plan.slots, now)
    if plan.delete:
        connection.execute(sa.delete(table).where(table.c.key.in_(plan.delete)))
    meta = sa.table("app_setting_meta", sa.column("id"), sa.column("revision"))
    connection.execute(sa.update(meta).where(meta.c.id == 1).values(revision=meta.c.revision + 1))
    return plan


def _upsert_value(connection: sa.Connection, key: str, value: str, now: datetime) -> None:
    table = sa.table(
        "app_setting",
        sa.column("key"), sa.column("value_json", sa.JSON()), sa.column("ciphertext"),
        sa.column("secret_hint"), sa.column("updated_at"), sa.column("updated_by"),
    )
    payload = {
        "value_json": value, "ciphertext": None, "secret_hint": None,
        "updated_at": now, "updated_by": _ACTOR,
    }
    existing = connection.execute(sa.select(table.c.key).where(table.c.key == key)).first()
    if existing is None:
        connection.execute(sa.insert(table).values(key=key, **payload))
    else:
        connection.execute(sa.update(table).where(table.c.key == key).values(**payload))


def _upsert_secret(connection: sa.Connection, key: str, ciphertext: str, now: datetime) -> None:
    table = sa.table(
        "app_setting",
        sa.column("key"), sa.column("value_json", sa.JSON()), sa.column("ciphertext"),
        sa.column("secret_hint"), sa.column("updated_at"), sa.column("updated_by"),
    )
    payload = {
        "value_json": None, "ciphertext": ciphertext, "secret_hint": "",
        "updated_at": now, "updated_by": _ACTOR,
    }
    existing = connection.execute(sa.select(table.c.key).where(table.c.key == key)).first()
    if existing is None:
        connection.execute(sa.insert(table).values(key=key, **payload))
    else:
        connection.execute(sa.update(table).where(table.c.key == key).values(**payload))
