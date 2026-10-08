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
"""

from pathlib import Path
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings, load_app_yaml
from investment_agent.settings.crypto import EncryptionKeyError
from investment_agent.settings.registry import ENV_KEYS, SECRET_KEYS, SPECS, flatten
from investment_agent.settings.store import SettingsStore, SettingsValidationError
from investment_agent.storage.database import session_scope

log = structlog.get_logger(__name__)

IMPORT_ACTOR = "import"
DEFAULT_APP_YAML = Path("config/app.yaml")


def store_for(session: AsyncSession, settings: Settings) -> SettingsStore:
    return SettingsStore(session, encryption_key=settings.settings_encryption_key)


async def _overlay(session: AsyncSession, base: Settings) -> Settings:
    store = store_for(session, base)
    if await store.imported_at() is None:
        return base
    state = await store.state()
    values = flatten(state.runtime)
    update: dict[str, Any] = {
        key: values[key]
        for key in ENV_KEYS
        if key not in SECRET_KEYS or key in state.stored_keys
    }
    # Validated by RuntimeSettings with the same bounds as Settings.
    return base.model_copy(update=update)


async def load_settings(
    session: AsyncSession | None = None, *, base: Settings | None = None
) -> Settings:
    """``Settings`` with the runtime fields taken from the DB.

    ``base`` is the bootstrap ``Settings`` (read from env when omitted).
    Without ``session`` a short ``session_scope()`` is opened (the engine
    must be initialized).
    """

    bootstrap = base if base is not None else Settings()  # type: ignore[call-arg]
    if session is not None:
        return await _overlay(session, bootstrap)
    async with session_scope() as own:
        return await _overlay(own, bootstrap)


def env_import_values(settings: Settings) -> dict[str, Any]:
    """Runtime keys explicitly set in env / ``.env`` (not the code defaults)."""

    dumped = settings.model_dump(mode="json", include=set(ENV_KEYS))
    return {key: dumped[key] for key in ENV_KEYS if key in settings.model_fields_set}


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
