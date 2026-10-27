"""Runtime settings endpoints (settings-plan §4.5).

* ``GET /settings`` - the runtime layer grouped for the settings page (secrets
  as ``configured`` + hint only), the store revision, the next fire time of
  every enabled schedule cron and a read-only view of the bootstrap (env)
  layer with credentials redacted;
* ``PATCH /settings`` - ``{values, note?}`` validated as a whole; ``422``
  with per-key issues, ``409`` when a secret cannot be encrypted;
* ``GET /settings/history`` - most recent changes first;
* ``POST /settings/test/llm`` / ``/settings/test/ifind`` - a minimal live call
  with candidate (unsaved) values; an omitted secret uses the effective one.
  Nothing is persisted and secrets are never echoed.

Auth: ``X-API-Key`` or the web session; cookie writes need the same-origin
proof of :func:`verify_api_key_or_session`. Before the one-time import has
run, the first call performs it so the page shows what consumers read.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import httpx
import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.api.auth import (
    get_bootstrap_settings,
    get_settings,
    verify_api_key_or_session,
)
from investment_agent.api.schemas.settings import (
    BootstrapItemOut,
    ConnectionTestOut,
    IfindTestRequest,
    LlmTestRequest,
    SettingChangeOut,
    SettingGroupOut,
    SettingItemOut,
    SettingsHistoryOut,
    SettingsOut,
    SettingsPatchOut,
    SettingsPatchRequest,
)
from investment_agent.config import Settings
from investment_agent.domain.llm_catalog import CatalogError, merge_catalog, parse_catalog
from investment_agent.settings.crypto import EncryptionKeyError, SecretCipher, secret_hint
from investment_agent.settings.loader import ensure_imported, load_settings, store_for
from investment_agent.settings.registry import GROUPS, SPECS_BY_KEY, cron_timezone_key
from investment_agent.settings.store import SettingsConflictError, SettingsValidationError
from investment_agent.storage.database import session_scope

log = structlog.get_logger(__name__)

router = APIRouter(
    prefix="/settings",
    tags=["settings"],
    dependencies=[Depends(verify_api_key_or_session)],
)

TEST_TIMEOUT_SECONDS = 15.0
_MAX_MESSAGE_CHARS = 300

_BOOTSTRAP_LABELS: tuple[tuple[str, str, str], ...] = (
    # (env name, Settings field, label)
    ("APP_ENV", "app_env", "运行环境"),
    ("LOG_LEVEL", "log_level", "日志级别"),
    ("DATA_DIR", "data_dir", "数据目录"),
    ("DATABASE_URL", "database_url", "数据库连接"),
    ("API_KEY", "api_key", "API Key"),
    ("WEB_SESSION_SECRET", "web_session_secret", "会话签名密钥"),
    ("WEB_SESSION_TTL_MINUTES", "web_session_ttl_minutes", "会话有效期（分钟）"),
    ("WEB_COOKIE_SECURE", "web_cookie_secure", "仅 HTTPS Cookie"),
    ("WEB_PUBLIC_ORIGIN", "web_public_origin", "公开访问地址"),
    ("SETTINGS_ENCRYPTION_KEY", "settings_encryption_key", "配置加密主密钥"),
)
_BOOTSTRAP_SECRETS = frozenset({"API_KEY", "WEB_SESSION_SECRET", "SETTINGS_ENCRYPTION_KEY"})


# --------------------------------------------------------------------------- #
# Read model
# --------------------------------------------------------------------------- #


def redact_database_url(url: str) -> str:
    """``DATABASE_URL`` without its user / password."""

    try:
        parsed = make_url(url)
    except Exception:
        return "（无法解析）"
    if parsed.username is None and parsed.password is None:
        return parsed.render_as_string(hide_password=False)
    bare = parsed._replace(username=None, password=None).render_as_string(hide_password=False)
    return bare.replace("://", "://***@", 1)


def bootstrap_items(settings: Settings) -> list[BootstrapItemOut]:
    items: list[BootstrapItemOut] = []
    for env_name, field, label in _BOOTSTRAP_LABELS:
        raw = getattr(settings, field)
        if env_name in _BOOTSTRAP_SECRETS:
            value = str(raw or "")
            items.append(
                BootstrapItemOut(
                    key=env_name,
                    label=label,
                    secret=True,
                    configured=bool(value.strip()),
                    hint=secret_hint(value),
                )
            )
            continue
        if env_name == "DATABASE_URL":
            raw = redact_database_url(str(raw))
        elif not isinstance(raw, bool | int | float):
            raw = str(raw)
        items.append(BootstrapItemOut(key=env_name, label=label, value=raw))
    return items


def next_run(cron: str, timezone: str, now: datetime | None = None) -> datetime | None:
    from investment_agent.scheduler.runner import _parse_cron

    zone = ZoneInfo(timezone)
    try:
        trigger = _parse_cron(cron, timezone=timezone)
    except (ValueError, TypeError):
        return None
    fire = trigger.get_next_fire_time(None, (now or datetime.now(UTC)).astimezone(zone))
    return fire.astimezone(zone) if fire is not None else None


def _encryption_status(settings: Settings) -> tuple[bool, str | None]:
    try:
        SecretCipher(settings.settings_encryption_key)
    except EncryptionKeyError as exc:
        return False, str(exc) if settings.settings_encryption_key.strip() else None
    return True, None


async def _ensure_imported(settings: Settings) -> None:
    """Run the one-time import if no process has (normally the API start-up did)."""

    async with session_scope() as session:
        if await store_for(session, settings).imported_at() is not None:
            return
    async with session_scope() as session:
        await ensure_imported(session, settings)


async def settings_view(session: AsyncSession, settings: Settings) -> SettingsOut:
    store = store_for(session, settings)
    described = await store.describe()
    values = {item["key"]: item.get("value") for item in described}
    grouped: dict[str, list[SettingItemOut]] = {group: [] for group in GROUPS}
    for item in described:
        spec = SPECS_BY_KEY[item["key"]]
        extra: dict[str, Any] = {}
        if spec.secret and not item["stored"]:
            env_value = str(getattr(settings, spec.key, "") or "")
            if env_value:
                extra.update(env_fallback=True, hint=secret_hint(env_value))
        tz_key = cron_timezone_key(spec)
        if tz_key is not None:
            timezone = str(values[tz_key])
            extra["timezone"] = timezone
            if item.get("value"):
                fire = next_run(str(item["value"]), timezone)
                if fire is not None:
                    extra["next_run"] = fire
        grouped[spec.group].append(SettingItemOut.model_validate({**item, **extra}))
    configured, error = _encryption_status(settings)
    state = await store.state()
    catalog_value = state.runtime.llm_catalog if "llm_catalog" in state.stored_keys else settings.llm_catalog
    return SettingsOut(
        revision=await store.revision(),
        encryption_key_configured=configured,
        encryption_key_error=error,
        groups=[
            SettingGroupOut(key=key, title=title, items=grouped[key])
            for key, title in GROUPS.items()
        ],
        bootstrap=bootstrap_items(settings),
        llm_catalog=parse_catalog(catalog_value).public(),
    )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #


@router.get("", response_model=SettingsOut, response_model_exclude_unset=True)
async def get_settings_view(
    settings: Annotated[Settings, Depends(get_bootstrap_settings)],
) -> SettingsOut:
    await _ensure_imported(settings)
    async with session_scope() as session:
        return await settings_view(session, settings)


@router.patch("", response_model=SettingsPatchOut, response_model_exclude_unset=True)
async def patch_settings(
    body: SettingsPatchRequest,
    settings: Annotated[Settings, Depends(get_bootstrap_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> SettingsPatchOut:
    actor = "api" if x_api_key is not None else "web"
    note = body.note.strip() if body.note and body.note.strip() else None
    await _ensure_imported(settings)
    try:
        async with session_scope() as session:
            store = store_for(session, settings)
            values = dict(body.values)
            # An import made before encryption was configured may still
            # use an env catalog. Its redacted keys must survive first save.
            if (isinstance(values.get("llm_catalog"), str) and settings.llm_catalog
                    and "llm_catalog" not in (await store.state()).stored_keys):
                values["llm_catalog"] = merge_catalog(values["llm_catalog"], settings.llm_catalog)
            changed = await store.apply(
                values, actor=actor, note=note, expected_revision=body.expected_revision
            )
    except CatalogError as exc:
        raise HTTPException(status_code=422, detail={"message": "settings validation failed", "issues": [
            {"key": "llm_catalog", "messages": [str(exc)]}
        ]}) from None
    except SettingsConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SettingsValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "settings validation failed",
                "issues": [
                    {"key": key, "messages": messages}
                    for key, messages in sorted(exc.issues.items())
                ],
            },
        ) from exc
    except EncryptionKeyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    async with session_scope() as session:
        view = await settings_view(session, settings)
    return SettingsPatchOut(changed=changed, revision=view.revision, settings=view)


@router.get("/history", response_model=SettingsHistoryOut)
async def settings_history(
    settings: Annotated[Settings, Depends(get_bootstrap_settings)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SettingsHistoryOut:
    async with session_scope() as session:
        rows = await store_for(session, settings).history(limit)
        return SettingsHistoryOut(
            items=[
                SettingChangeOut(
                    id=row.id,
                    key=row.key,
                    label=spec.label if (spec := SPECS_BY_KEY.get(row.key)) else None,
                    old_value=row.old_value_json,
                    new_value=row.new_value_json,
                    changed_at=(
                        row.changed_at
                        if row.changed_at.tzinfo
                        else row.changed_at.replace(tzinfo=UTC)
                    ),
                    actor=row.actor,
                    note=row.note,
                )
                for row in rows
            ]
        )


# --------------------------------------------------------------------------- #
# Connection tests
# --------------------------------------------------------------------------- #


def _scrub(message: str, *secrets: str) -> str:
    """One line, secrets replaced, bounded length."""

    text = " ".join(message.split())
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "***")
    if len(text) > _MAX_MESSAGE_CHARS:
        text = text[: _MAX_MESSAGE_CHARS - 3] + "..."
    return text


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _pick(candidate: str | None, stored: str) -> str:
    return candidate.strip() if candidate and candidate.strip() else stored


@router.post("/test/llm", response_model=ConnectionTestOut)
async def check_llm(
    body: LlmTestRequest,
    settings: Annotated[Settings, Depends(get_bootstrap_settings)],
) -> ConnectionTestOut:
    """One tiny chat completion with the candidate endpoint / model / key."""

    async with session_scope() as session:
        settings = await load_settings(session, base=settings, resolve_models=not body.use_legacy)
    base_url = _pick(body.base_url, settings.llm_tier3_base_url)
    model = _pick(body.model, settings.llm_tier3_model)
    api_key = _pick(body.api_key, settings.llm_tier3_api_key)
    thinking = body.thinking or settings.llm_tier3_thinking
    if body.provider_id is not None:
        catalog = parse_catalog(settings.llm_catalog)
        provider = next((p for p in catalog.providers if p.id == body.provider_id), None)
        saved_model = None
        if provider and body.model_id:
            saved_model = next((m for m in provider.models if m.id == body.model_id), None)
            if saved_model is None and not body.model:
                raise HTTPException(status_code=422, detail="Selected model is missing")
        # Candidate fields support testing providers/models before saving them.
        if not body.base_url and provider is None:
            raise HTTPException(status_code=422, detail="Provider is missing; specify its endpoint")
        base_url = _pick(body.base_url, provider.base_url if provider else "")
        model = _pick(body.model, saved_model.model if saved_model else "")
        saved_key = (provider.api_key or "") if provider else ""
        api_key = body.api_key.strip() if body.api_key is not None else saved_key
        thinking = body.thinking or (saved_model.thinking if saved_model else "auto")
        if not model:
            raise HTTPException(status_code=422, detail="Choose a model to test")
    if not api_key:
        return ConnectionTestOut(ok=False, latency_ms=0, message="未配置 API Key，无法测试。")
    timeout = min(TEST_TIMEOUT_SECONDS, settings.llm_request_timeout_seconds)
    started = time.perf_counter()
    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with: ok"}],
            max_tokens=8,
            extra_body={"thinking": {"type": thinking}} if thinking != "auto" else None,
        )
    except APITimeoutError:
        return ConnectionTestOut(
            ok=False, latency_ms=_elapsed_ms(started), message=f"请求超时（{timeout:g} 秒）。"
        )
    except APIStatusError as exc:
        detail = _scrub(str(exc.message), api_key)
        return ConnectionTestOut(
            ok=False,
            latency_ms=_elapsed_ms(started),
            message=f"HTTP {exc.status_code}：{detail}",
        )
    except APIConnectionError as exc:
        return ConnectionTestOut(
            ok=False,
            latency_ms=_elapsed_ms(started),
            message=f"无法连接：{_scrub(str(exc.__cause__ or exc), api_key)}",
        )
    except Exception as exc:  # unexpected payloads must not 500 the settings page
        log.warning("settings.test_llm_failed", error_type=type(exc).__name__)
        return ConnectionTestOut(
            ok=False,
            latency_ms=_elapsed_ms(started),
            message=_scrub(f"{type(exc).__name__}: {exc}", api_key),
        )
    finally:
        await client.close()
    returned = _scrub(getattr(response, "model", None) or model, api_key)
    return ConnectionTestOut(
        ok=True, latency_ms=_elapsed_ms(started), message=f"连接成功，模型 {returned} 已响应。"
    )


@router.post("/test/ifind", response_model=ConnectionTestOut)
async def check_ifind(
    body: IfindTestRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> ConnectionTestOut:
    """``GET {proxy}/health`` with the key: checks reachability and the key, no quota."""

    url = _pick(body.url, settings.ifind_proxy_url).rstrip("/")
    api_key = _pick(body.api_key, settings.ifind_proxy_api_key)
    if not api_key:
        return ConnectionTestOut(ok=False, latency_ms=0, message="未配置代理 API Key，无法测试。")
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{url}/health", headers={"X-API-Key": api_key})
    except httpx.TimeoutException:
        return ConnectionTestOut(
            ok=False,
            latency_ms=_elapsed_ms(started),
            message=f"请求超时（{TEST_TIMEOUT_SECONDS:g} 秒）。",
        )
    except (httpx.HTTPError, ValueError) as exc:
        return ConnectionTestOut(
            ok=False,
            latency_ms=_elapsed_ms(started),
            message=f"无法连接：{_scrub(str(exc) or type(exc).__name__, api_key)}",
        )
    latency = _elapsed_ms(started)
    if response.status_code == 401:
        return ConnectionTestOut(ok=False, latency_ms=latency, message="代理拒绝了 API Key（HTTP 401）。")
    if response.status_code != 200:
        return ConnectionTestOut(
            ok=False,
            latency_ms=latency,
            message=_scrub(f"HTTP {response.status_code}：{response.text}", api_key),
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    version = payload.get("version") if isinstance(payload, dict) else None
    suffix = f"（版本 {_scrub(str(version), api_key)}）" if version else ""
    return ConnectionTestOut(ok=True, latency_ms=latency, message=f"代理连接正常{suffix}。")
