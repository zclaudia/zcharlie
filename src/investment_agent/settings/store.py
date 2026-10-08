"""``SettingsStore``: runtime settings persisted in ``app_setting`` (settings-plan §4.1-4.3).

Reads resolve registry defaults overlaid with the stored rows into a validated
``RuntimeSettings``. Writes validate the whole merged model first, then write
the changed rows, one ``app_setting_change`` row per changed key and a single
``app_setting_meta.revision`` bump - all in the caller's session, so the
caller's transaction makes them atomic (``session_scope`` commits).

Secrets (``SettingSpec.secret``) are stored as Fernet ciphertext plus a last-4
hint; an empty string clears one (the row stays, ciphertext ``NULL``). A secret
that cannot be decrypted (``SETTINGS_ENCRYPTION_KEY`` missing or changed)
resolves to ``""`` with a logged warning and ``decrypt_error`` in
``describe()``; every non-secret setting still loads. Storing a non-empty
secret without a usable key raises ``EncryptionKeyError``.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.settings.crypto import (
    SecretCipher,
    SettingsCryptoError,
    secret_hint,
)
from investment_agent.settings.registry import (
    SECRET_KEYS,
    SPECS,
    SPECS_BY_KEY,
    RuntimeSettings,
    defaults,
    flatten,
    input_spec,
    unflatten,
)
from investment_agent.storage.models.app_settings import (
    META_ROW_ID,
    AppSetting,
    AppSettingChange,
    AppSettingMeta,
)

log = structlog.get_logger(__name__)

SECRET_MASK = "***"


class SettingsValidationError(ValueError):
    """A write was rejected; ``issues`` maps a key (or group path) to messages."""

    def __init__(self, issues: dict[str, list[str]]) -> None:
        self.issues = issues
        detail = "; ".join(f"{key}: {', '.join(msgs)}" for key, msgs in sorted(issues.items()))
        super().__init__(f"invalid settings: {detail}")


@dataclass(frozen=True)
class SecretStatus:
    configured: bool
    hint: str = ""
    decrypt_error: str | None = None


@dataclass(frozen=True)
class SettingsState:
    """One consistent read: the resolved settings plus what the rows hold."""

    runtime: RuntimeSettings
    stored_keys: frozenset[str]
    """Keys with an ``app_setting`` row (everything else is a default)."""
    secrets: dict[str, SecretStatus] = field(default_factory=dict)


def _issues_from(exc: ValidationError) -> dict[str, list[str]]:
    issues: dict[str, list[str]] = {}
    for error in exc.errors():
        key = ".".join(str(part) for part in error["loc"]) or "__root__"
        issues.setdefault(key, []).append(error["msg"])
    return issues


def _masked(status: SecretStatus) -> str | None:
    return f"{SECRET_MASK}{status.hint}" if status.configured else None


class SettingsStore:
    def __init__(self, session: AsyncSession, *, encryption_key: str = "") -> None:
        self.session = session
        self._encryption_key = encryption_key
        self._cipher_instance: SecretCipher | None = None

    def _cipher(self) -> SecretCipher:
        """The cipher; raises ``EncryptionKeyError`` when the key is missing / invalid."""

        if self._cipher_instance is None:
            self._cipher_instance = SecretCipher(self._encryption_key)
        return self._cipher_instance

    def check_encryption_key(self) -> None:
        """Raise ``EncryptionKeyError`` unless secrets can be stored."""

        self._cipher()

    async def _rows(self) -> dict[str, AppSetting]:
        rows = (await self.session.execute(select(AppSetting))).scalars()
        return {row.key: row for row in rows}

    def _resolve_secret(self, row: AppSetting) -> tuple[str, SecretStatus]:
        if row.ciphertext is None:
            return "", SecretStatus(configured=False)
        hint = row.secret_hint or ""
        try:
            return self._cipher().decrypt(row.ciphertext), SecretStatus(True, hint)
        except SettingsCryptoError as exc:
            log.warning("settings.secret_unreadable", key=row.key, error=str(exc))
            return "", SecretStatus(True, hint, decrypt_error=str(exc))

    def _resolve(
        self, rows: dict[str, AppSetting]
    ) -> tuple[dict[str, Any], dict[str, SecretStatus]]:
        flat = defaults()
        secrets = {key: SecretStatus(configured=False) for key in SECRET_KEYS}
        for key, row in rows.items():
            if key not in SPECS_BY_KEY:
                continue  # a key retired from the registry: ignored, kept for history
            if key in SECRET_KEYS:
                flat[key], secrets[key] = self._resolve_secret(row)
            else:
                flat[key] = row.value_json
        return flat, secrets

    async def state(self) -> SettingsState:
        rows = await self._rows()
        flat, secrets = self._resolve(rows)
        try:
            runtime = RuntimeSettings.model_validate(unflatten(flat))
        except ValidationError as exc:
            # A stored value the current code rejects (e.g. a universe that was
            # removed) must not take every other setting down: fall back to the
            # default for the offending keys.
            invalid = {key for key in _issues_from(exc) if key in SPECS_BY_KEY}
            if not invalid:
                raise
            log.warning("settings.invalid_stored_values", keys=sorted(invalid), error=str(exc))
            fallback = defaults()
            flat.update({key: fallback[key] for key in invalid})
            runtime = RuntimeSettings.model_validate(unflatten(flat))
        stored = frozenset(key for key in rows if key in SPECS_BY_KEY)
        return SettingsState(runtime=runtime, stored_keys=stored, secrets=secrets)

    async def load(self) -> RuntimeSettings:
        """Defaults overlaid with the stored values."""

        return (await self.state()).runtime

    async def apply(
        self, values: dict[str, Any], *, actor: str, note: str | None = None
    ) -> list[str]:
        """Validate and write ``{key: value}``; returns the keys that changed.

        Raises ``SettingsValidationError`` (nothing written) when a key is
        unknown or the merged settings fail validation, and
        ``EncryptionKeyError`` when a non-empty secret is given without a
        usable ``SETTINGS_ENCRYPTION_KEY``.
        """

        issues: dict[str, list[str]] = {}
        for key, value in values.items():
            if key not in SPECS_BY_KEY:
                issues.setdefault(key, []).append("unknown setting")
            elif key in SECRET_KEYS and not isinstance(value, str):
                issues.setdefault(key, []).append("secret must be a string")
        if issues:
            raise SettingsValidationError(issues)

        rows = await self._rows()
        current, secrets = self._resolve(rows)
        merged = {**current, **values}
        try:
            validated = flatten(RuntimeSettings.model_validate(unflatten(merged)))
        except ValidationError as exc:
            raise SettingsValidationError(_issues_from(exc)) from exc

        changed = [
            key
            for key in values
            if validated[key] != current[key]
            # Rewriting an undecryptable secret replaces it (a change).
            or (key in SECRET_KEYS and secrets[key].decrypt_error is not None)
        ]
        # A key set to the value it already resolves to gets a row without a
        # history entry: it is pinned against future default changes.
        to_write = [key for key in values if key in changed or key not in rows]
        if any(key in SECRET_KEYS and validated[key] for key in to_write):
            self._cipher()  # fail before writing anything

        now = datetime.now(UTC)
        for key in to_write:
            new_value = validated[key]
            if key in SECRET_KEYS:
                new_status = SecretStatus(bool(new_value), secret_hint(new_value))
                old_logged: Any = _masked(secrets[key])
                new_logged: Any = _masked(new_status)
                columns: dict[str, Any] = {
                    "value_json": None,
                    "ciphertext": self._cipher().encrypt(new_value) if new_value else None,
                    "secret_hint": new_status.hint if new_value else None,
                }
            else:
                old_logged, new_logged = current[key], new_value
                columns = {"value_json": new_value, "ciphertext": None, "secret_hint": None}
            row = rows.get(key)
            if row is None:
                self.session.add(AppSetting(key=key, updated_at=now, updated_by=actor, **columns))
            else:
                for name, column_value in columns.items():
                    setattr(row, name, column_value)
                row.updated_at, row.updated_by = now, actor
            if key in changed:
                self.session.add(
                    AppSettingChange(
                        key=key,
                        old_value_json=old_logged,
                        new_value_json=new_logged,
                        changed_at=now,
                        actor=actor,
                        note=note,
                    )
                )
        if changed:
            await self._bump_revision()
            log.info("settings.applied", actor=actor, keys=changed)
        await self.session.flush()
        return changed

    async def _meta(self) -> AppSettingMeta:
        meta = await self.session.get(AppSettingMeta, META_ROW_ID)
        if meta is None:
            # Migration 021 seeds the row; schemas built by ``create_all`` do not.
            meta = AppSettingMeta(id=META_ROW_ID, revision=0, imported_at=None)
            self.session.add(meta)
            await self.session.flush()
        return meta

    async def _bump_revision(self) -> None:
        result = await self.session.execute(
            update(AppSettingMeta)
            .where(AppSettingMeta.id == META_ROW_ID)
            .values(revision=AppSettingMeta.revision + 1)
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            meta = await self._meta()
            meta.revision += 1

    async def revision(self) -> int:
        """Bumped by every write that changed a value; 0 for an untouched store."""

        value = await self.session.scalar(
            select(AppSettingMeta.revision).where(AppSettingMeta.id == META_ROW_ID)
        )
        return value or 0

    async def imported_at(self) -> datetime | None:
        return await self.session.scalar(
            select(AppSettingMeta.imported_at).where(AppSettingMeta.id == META_ROW_ID)
        )

    async def claim_import(self, *, force: bool = False) -> bool:
        """Mark the one-time import done; ``False`` when another run already did.

        The conditional ``UPDATE`` is the first statement, so concurrent
        processes serialize on the database write lock and exactly one of
        them sees ``imported_at IS NULL``. Call it on a fresh session and
        write the imported values in the same transaction.
        """

        now = datetime.now(UTC)
        stmt = update(AppSettingMeta).where(AppSettingMeta.id == META_ROW_ID)
        if not force:
            stmt = stmt.where(AppSettingMeta.imported_at.is_(None))
        result = await self.session.execute(
            stmt.values(imported_at=now).execution_options(synchronize_session="fetch")
        )
        if result.rowcount:  # type: ignore[attr-defined]
            return True
        if await self.session.get(AppSettingMeta, META_ROW_ID) is not None:
            return False
        self.session.add(AppSettingMeta(id=META_ROW_ID, revision=0, imported_at=now))
        await self.session.flush()
        return True

    async def history(self, limit: int = 50) -> list[AppSettingChange]:
        """Most recent changes first."""

        stmt = select(AppSettingChange).order_by(AppSettingChange.id.desc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars())

    async def describe(self) -> list[dict[str, Any]]:
        """Every registered key with its current value, default and metadata.

        Secrets never carry their value: ``{"configured", "hint",
        "decrypt_error"}`` instead. ``input`` is the editing hint of
        ``registry.input_spec`` (type, bounds, options).
        """

        state = await self.state()
        values = flatten(state.runtime)
        fallback = defaults()
        described: list[dict[str, Any]] = []
        for spec in SPECS:
            item: dict[str, Any] = {
                "key": spec.key,
                "group": spec.group,
                "label": spec.label,
                "help": spec.help,
                "apply": spec.apply,
                "source": spec.source,
                "secret": spec.secret,
                "stored": spec.key in state.stored_keys,
                "input": input_spec(spec),
            }
            if spec.secret:
                status = state.secrets[spec.key]
                item.update(
                    configured=status.configured,
                    hint=status.hint,
                    decrypt_error=status.decrypt_error,
                    is_default=not status.configured,
                )
            else:
                item.update(
                    value=values[spec.key],
                    default=fallback[spec.key],
                    is_default=values[spec.key] == fallback[spec.key],
                )
            described.append(item)
        return described
