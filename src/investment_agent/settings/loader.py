"""Effective ``Settings``: bootstrap layer from env, runtime layer from the DB.

``load_settings()`` keeps the ``Settings`` attribute names (``settings.
ifind_budget_per_5h`` ...) so consumers only swap ``Settings()`` for it. Until
the one-time import has run (``app_setting_meta.imported_at`` is ``NULL``) it
returns the env ``Settings`` unchanged; afterwards every runtime field comes
from the store (registry default when no row) and env values for runtime keys
are ignored - the DB is the source of truth (settings-plan S-D2).

Transitional exception: a secret key without any stored row (e.g. imported
while ``SETTINGS_ENCRYPTION_KEY`` was unset) keeps its env value, so a
deployment without the master key keeps working until the secret is saved.

``ensure_imported()`` runs the import: runtime keys explicitly set in env
(``Settings.model_fields_set``: process env or ``.env``) plus the schedule
values written in ``config/app.yaml``.

``load_runtime()`` / ``load_app_config()`` give the schedule the scheduler,
``ia snapshot`` and the health pages use: the stored one, or before the
import the values the import would store (so behaviour does not change when
the import runs). ``a_share_calendar_path`` always comes from app.yaml.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import AppYamlConfig, SchedulerConfig, Settings, load_app_yaml

# Deployment resource locations live in settings.calendar_paths (C4 split); this
# block keeps the names this module's existing importers (scheduler, api, cli,
# tools, tests) use re-exported unchanged.
from investment_agent.settings.calendar_paths import (
    DEFAULT_APP_YAML as DEFAULT_APP_YAML,
)
from investment_agent.settings.calendar_paths import (
    RUNTIME_CALENDAR_DIR as RUNTIME_CALENDAR_DIR,
)
from investment_agent.settings.calendar_paths import calendar_path as calendar_path
from investment_agent.settings.calendar_paths import (
    configured_calendar_path as configured_calendar_path,
)
from investment_agent.settings.calendar_paths import (
    resolved_calendar_path as resolved_calendar_path,
)
from investment_agent.settings.calendar_paths import (
    runtime_calendar_path as runtime_calendar_path,
)
from investment_agent.settings.calendar_paths import runtime_data_dir as runtime_data_dir
from investment_agent.settings.crypto import EncryptionKeyError
from investment_agent.settings.registry import (
    ENV_KEYS,
    SECRET_KEYS,
    SPECS,
    SPECS_BY_KEY,
    RuntimeSettings,
    ScheduleSettings,
    defaults,
    flatten,
    unflatten,
)
from investment_agent.settings.store import SettingsStore, SettingsValidationError
from investment_agent.storage.database import session_scope

if TYPE_CHECKING:
    from investment_agent.agent.model import AgentModelConfig

log = structlog.get_logger(__name__)

IMPORT_ACTOR = "import"


def store_for(session: AsyncSession, settings: Settings) -> SettingsStore:
    return SettingsStore(session, encryption_key=settings.settings_encryption_key)


async def _overlay(session: AsyncSession, base: Settings) -> Settings:
    from investment_agent.domain.llm_catalog import CatalogError

    store = store_for(session, base)
    if await store.imported_at() is None:
        return base
    state = await store.state()
    if state.secrets["llm_catalog"].decrypt_error:
        raise CatalogError("LLM catalog cannot be decrypted; restore SETTINGS_ENCRYPTION_KEY")
    values = flatten(state.runtime)
    update: dict[str, Any] = {
        key: values[key]
        for key in ENV_KEYS
        if key not in SECRET_KEYS or key in state.stored_keys
    }
    for key, credential in base._llm_legacy_credentials.items():
        if key not in state.stored_keys:
            update[key] = credential
    # Validated by RuntimeSettings with the same bounds as Settings.
    return base.model_copy(update=update)


async def load_settings(
    session: AsyncSession | None = None, *, base: Settings | None = None, resolve_models: bool = True
) -> Settings:
    """``Settings`` with the runtime fields taken from the DB.

    ``base`` is the bootstrap ``Settings`` (read from env when omitted).
    Without ``session`` a short ``session_scope()`` is opened (the engine
    must be initialized).
    """

    from investment_agent.settings.llm_catalog import resolve_catalog_settings

    bootstrap = base if base is not None else Settings()  # type: ignore[call-arg]
    if session is not None:
        effective = await _overlay(session, bootstrap)
    else:
        async with session_scope() as own:
            effective = await _overlay(own, bootstrap)
    return resolve_catalog_settings(effective) if resolve_models else effective


def env_import_values(settings: Settings) -> dict[str, Any]:
    """Runtime keys explicitly set in env / ``.env`` (not the code defaults)."""

    dumped = settings.model_dump(mode="json", include=set(ENV_KEYS))
    return {key: dumped[key] for key in ENV_KEYS if key in settings.model_fields_set and not key.startswith("notification_")}


def yaml_import_values(app_yaml_path: Path) -> dict[str, Any]:
    """Schedule values written in app.yaml; an absent section disables its crons.

    The scheduler skips a section missing from app.yaml, so its jobs import as
    disabled (``None`` crons) rather than as the registry's enabled defaults.
    """

    if not app_yaml_path.exists():
        return {}
    config = load_app_yaml(app_yaml_path)
    values: dict[str, Any] = {}
    for spec in SPECS:
        if spec.yaml_path is None:
            continue
        section_name, field_name = spec.yaml_path
        section = getattr(config, section_name)
        if section is None:
            if field_name.endswith("cron"):
                values[spec.key] = None
            continue
        if field_name in section.model_fields_set:
            values[spec.key] = section.model_dump(mode="json")[field_name]
    return values


async def import_values(
    store: SettingsStore, values: dict[str, Any], *, note: str
) -> list[str]:
    """Apply imported values, skipping (with a warning) those that cannot be stored."""

    values = dict(values)
    secrets = sorted(key for key, value in values.items() if key in SECRET_KEYS and value)
    if secrets:
        try:
            store.check_encryption_key()
        except EncryptionKeyError as exc:
            log.warning(
                "settings.import_skipped_secrets",
                keys=secrets,
                reason=str(exc),
                hint="they keep their env value until saved; set SETTINGS_ENCRYPTION_KEY "
                "and run `ia settings import --force` or save them in the UI",
            )
            for key in secrets:
                values.pop(key)
    try:
        return await store.apply(values, actor=IMPORT_ACTOR, note=note)
    except SettingsValidationError as exc:
        log.warning("settings.import_skipped_invalid", issues=exc.issues)
        for key in exc.issues:
            values.pop(key, None)
        return await store.apply(values, actor=IMPORT_ACTOR, note=note)


async def ensure_imported(
    session: AsyncSession,
    settings: Settings,
    app_yaml_path: Path = DEFAULT_APP_YAML,
    *,
    force: bool = False,
) -> list[str] | None:
    """One-time import of env / app.yaml runtime values into the store.

    Returns the keys imported (changed), or ``None`` when an earlier run had
    already imported. ``force`` re-imports, overwriting stored values with the
    current env / app.yaml ones. Use a fresh session; the caller commits.
    Afterwards, logs one warning naming env runtime keys whose value differs
    from the DB (ignored: the DB is the source of truth).
    """

    store = store_for(session, settings)
    imported: list[str] | None = None
    if await store.claim_import(force=force):
        values = {**env_import_values(settings), **yaml_import_values(app_yaml_path)}
        imported = await import_values(store, values, note=f"env + {app_yaml_path}")
        log.info("settings.imported", keys=imported, force=force)
    await warn_env_drift(store, settings)
    return imported


async def warn_env_drift(store: SettingsStore, settings: Settings) -> list[str]:
    """Env runtime keys whose explicit value differs from the store (keys only)."""

    state = await store.state()
    stored = flatten(state.runtime)
    env_values = env_import_values(settings)
    drift = sorted(
        key
        for key, value in env_values.items()
        if (key not in SECRET_KEYS or key in state.stored_keys) and stored[key] != value
    )
    if drift:
        log.warning(
            "settings.env_ignored",
            keys=drift,
            detail="ignored; DB is the source of truth - change them in the settings UI "
            "or `ia settings set`, and remove them from env",
        )
    return drift


def import_preview(settings: Settings, app_yaml_path: Path = DEFAULT_APP_YAML) -> RuntimeSettings:
    """The runtime settings the import would store (defaults for invalid values)."""

    flat = defaults()
    flat.update(env_import_values(settings))
    flat.update(yaml_import_values(app_yaml_path))
    try:
        return RuntimeSettings.model_validate(unflatten(flat))
    except ValidationError as exc:
        invalid = {
            key
            for error in exc.errors()
            if (key := ".".join(str(part) for part in error["loc"])) in SPECS_BY_KEY
        }
        if not invalid:
            raise
        fallback = defaults()
        flat.update({key: fallback[key] for key in invalid})
        return RuntimeSettings.model_validate(unflatten(flat))


async def load_runtime(
    session: AsyncSession, settings: Settings, app_yaml_path: Path = DEFAULT_APP_YAML
) -> RuntimeSettings:
    """The stored runtime layer; before the import, ``import_preview()``."""

    store = store_for(session, settings)
    if await store.imported_at() is None:
        return import_preview(settings, app_yaml_path)
    return await store.load()


def app_config_from(schedule: ScheduleSettings, *, calendar: str) -> AppYamlConfig:
    """The runtime schedule in the ``AppYamlConfig`` shape the scheduler builds from.

    Every section is present; a disabled job has a ``None`` cron.
    """

    return AppYamlConfig(
        scheduler=SchedulerConfig(
            watchlist_refresh_cron=schedule.watchlist.cron,
            watchlist_refresh_timezone=schedule.watchlist.timezone,
            # The valuation and zone-detection crons are runtime settings too
            # (WP6/WP8): dropping them here would register no daily job at all.
            portfolio_valuation_cron=schedule.portfolio.cron,
            portfolio_valuation_timezone=schedule.portfolio.timezone,
            zone_detection_cron=schedule.zone_detection.cron,
            zone_detection_timezone=schedule.zone_detection.timezone,
            a_share_calendar_path=calendar,
        ),
        snapshot=schedule.snapshot,
        screen=schedule.screen,
        candidates=schedule.candidates,
        verification=schedule.verification,
    )


async def load_app_config(
    session: AsyncSession, settings: Settings, app_yaml_path: Path = DEFAULT_APP_YAML
) -> tuple[AppYamlConfig, int]:
    """Runtime schedule as ``AppYamlConfig`` plus the store revision it was read at."""

    revision = await store_for(session, settings).revision()
    runtime = await load_runtime(session, settings, app_yaml_path)
    # The scheduler resolves the data-dir copy per fire; the config here is the
    # baseline path it resolves from.
    return (
        app_config_from(runtime.schedule, calendar=configured_calendar_path(app_yaml_path)),
        revision,
    )


async def import_and_load(
    settings: Settings, app_yaml_path: Path = DEFAULT_APP_YAML
) -> tuple[Settings, int]:
    """Process start-up: run the one-time import, then load settings and revision.

    The import commits in its own session; the read is one transaction, so
    the returned settings match the returned revision.
    """

    async with session_scope() as session:
        await ensure_imported(session, settings, app_yaml_path)
    async with session_scope() as session:
        revision = await store_for(session, settings).revision()
        return await load_settings(session, base=settings), revision


async def load_agent_model_config(
    session: AsyncSession, *, base: Settings | None = None
) -> "AgentModelConfig":
    """Resolve credentials with bounded revision rechecks, without a long read lock.

    SQLite's legacy SELECT behavior does not imply a read snapshot. A writer
    between reads forces a fresh overlay; repeated churn fails explicitly.
    Caller closes its short read session before invoking the model.
    """
    from investment_agent.agent.model import AgentModelConfig

    bootstrap = base if base is not None else Settings()  # type: ignore[call-arg]
    store = store_for(session, bootstrap)
    for _ in range(3):
        revision = await store.revision()
        effective = await load_settings(session, base=bootstrap)
        if await store.revision() == revision:
            return AgentModelConfig.from_settings(effective, revision)
    from investment_agent.agent.model import AgentConfigurationError

    raise AgentConfigurationError("Agent settings changed repeatedly; retry configuration load")
