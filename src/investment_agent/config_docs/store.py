"""``ConfigDocStore``: whole-document config persisted in ``config_document``.

Reads resolve the stored row, falling back to the baseline file's document
while a kind has never been imported. Writes parse-validate the *full*
document through the kind's own loader first (CD3: parse-as-validation; the
parser's message reaches the caller), then in the caller's single transaction
insert/update the row, append one full-document ``config_document_change``
row (old / new / actor / mandatory note, CD7) and bump the *existing*
``app_setting_meta.revision`` (CD4) — the settings single number the worker
checks per claim and the scheduler polls every 60 s, so propagation needed
zero changes there. A write also refreshes the process cache of
:mod:`investment_agent.config_docs.loader` so the same process's next sync
read is fresh.

A kind's *own* revision is the number of its change rows (the schema has no
per-kind counter): ``put(expected_revision=...)`` optimistically locks on it
the way ``SettingsStore.apply`` locks on the settings revision — the stale
editor gets ``ConfigDocConflictError`` (409), never a silent overwrite.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config_docs import registry
from investment_agent.config_docs.registry import ConfigDocSpec
from investment_agent.storage.models.app_settings import (
    META_ROW_ID,
    AppSettingMeta,
)
from investment_agent.storage.models.config_docs import (
    ConfigDocument,
    ConfigDocumentChange,
)

log = structlog.get_logger(__name__)

IMPORT_ACTOR = "import"


class ConfigDocConflictError(ValueError):
    """A stale editor must reload before replacing the document (409)."""


class ConfigDocValidationError(ValueError):
    """A write was rejected by parse-as-validation or a missing note (422)."""


def _canonical(value: Any) -> str:
    """Canonical JSON: the drift comparison and the baseline hash basis."""

    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _baseline_ref(value: dict[str, Any] | None) -> str | None:
    """The document's own ``version`` when it has one, else a canonical-JSON hash."""

    if value is None:
        return None
    version = value.get("version")
    if isinstance(version, str) and version:
        return version
    digest = hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"


class ConfigDocStore:
    """Reads, validated writes, history and baseline import of config documents."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def _row(self, kind: str) -> ConfigDocument | None:
        return (
            await self.session.execute(
                select(ConfigDocument)
                .where(ConfigDocument.kind == kind)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def get(self, kind: str) -> tuple[dict[str, Any], ConfigDocument | None]:
        """The effective document dict plus its row (``None`` before the import).

        The row's stored value wins; without one the baseline file's document
        is the effective value and the second element is ``None`` (the API /
        UI shows it as "not imported yet").
        """

        spec = registry.spec_for(kind)
        row = await self._row(spec.kind)
        if row is not None:
            return row.value_json, row
        return registry.baseline_document(spec), None

    async def revision(self, kind: str) -> int:
        """The kind's own revision: its change-row count (0 for an untouched kind)."""

        registry.spec_for(kind)
        value = await self.session.scalar(
            select(func.count())
            .select_from(ConfigDocumentChange)
            .where(ConfigDocumentChange.kind == kind)
        )
        return value or 0

    async def history(self, kind: str, limit: int = 50) -> list[ConfigDocumentChange]:
        """The kind's change rows, most recent first."""

        registry.spec_for(kind)
        stmt = (
            select(ConfigDocumentChange)
            .where(ConfigDocumentChange.kind == kind)
            .order_by(ConfigDocumentChange.id.desc())
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def put(
        self,
        kind: str,
        value: dict[str, Any],
        *,
        actor: str,
        note: str,
        expected_revision: int | None = None,
    ) -> int:
        """Validate and write the whole document; returns the kind's new revision.

        Raises ``ConfigDocValidationError`` (nothing written) when the document
        fails the kind's parser or ``note`` is empty, and
        ``ConfigDocConflictError`` when ``expected_revision`` is not the kind's
        current revision. A write that changes nothing writes no row, no
        history and no revision bump (mirroring ``SettingsStore.apply``).
        """

        spec = registry.spec_for(kind)
        if not isinstance(value, dict):
            raise ConfigDocValidationError(f"{kind}: expected a mapping document")
        # A programmatic caller may hand over date objects (a UI / API PATCH
        # cannot): normalize before parse / storage so the parser sees the
        # ISO strings a JSON transport would deliver and every stored value
        # stays JSON-shaped (``registry.jsonable``).
        value = registry.jsonable(value)
        if not note or not note.strip():
            raise ConfigDocValidationError(f"{kind}: a change note is required")
        try:
            parsed = spec.parse(value)
        except ValueError as exc:
            raise ConfigDocValidationError(str(exc)) from exc
        if expected_revision is not None:
            await self._fence(spec, expected_revision)
        await self._write(spec, value, parsed, actor=actor, note=note, mark_imported=False)
        return await self.revision(spec.kind)

    async def _fence(self, spec: ConfigDocSpec, expected_revision: int) -> None:
        """Optimistic-lock fence, mirroring ``SettingsStore.apply``.

        The kind's revision is a row count, so the fence is the conditional
        no-op ``UPDATE`` of the settings meta row the write will bump: a
        concurrent editor (any writer of either store) commits a newer meta
        revision first and this transaction's ``UPDATE`` then matches nothing.
        SQLite's writer lock serializes the two, so exactly one stale editor
        proceeds. A concurrent *settings* write trips the fence too — a false
        conflict the editor resolves by reloading, the same conservatism the
        settings editor already lives with.
        """

        meta_revision = await self.session.scalar(
            select(AppSettingMeta.revision).where(AppSettingMeta.id == META_ROW_ID)
        )
        if meta_revision is None:
            await self._meta()
            meta_revision = 0
        if await self.revision(spec.kind) != expected_revision:
            raise ConfigDocConflictError("配置文档已被其他操作修改，请刷新后重试。")
        matched = await self.session.execute(
            update(AppSettingMeta)
            .where(
                AppSettingMeta.id == META_ROW_ID, AppSettingMeta.revision == meta_revision
            )
            .values(revision=AppSettingMeta.revision)
        )
        if matched.rowcount != 1:  # type: ignore[attr-defined]
            raise ConfigDocConflictError("配置文档已被其他操作修改，请刷新后重试。")

    async def _write(
        self,
        spec: ConfigDocSpec,
        value: dict[str, Any],
        parsed: Any,
        *,
        actor: str,
        note: str,
        mark_imported: bool,
    ) -> bool:
        """Insert/update the row, append the change row, bump the settings revision.

        A no-op write (the stored canonical value equals ``value``) writes
        nothing and returns ``False``. ``mark_imported`` stamps
        ``imported_at`` (the baseline import paths). Every call also refreshes
        the loader's process cache with the already-validated parse.
        """

        from investment_agent.config_docs.loader import cache_update

        row = await self._row(spec.kind)
        old = row.value_json if row is not None else None
        if row is not None and _canonical(old) == _canonical(value):
            cache_update(spec.kind, await self.revision(spec.kind), parsed)
            return False
        now = datetime.now(UTC)
        if row is None:
            row = ConfigDocument(
                kind=spec.kind,
                value_json=value,
                updated_at=now,
                updated_by=actor,
                imported_at=now if mark_imported else None,
            )
            self.session.add(row)
        else:
            row.value_json, row.updated_at, row.updated_by = value, now, actor
            if mark_imported:
                row.imported_at = now
        self.session.add(
            ConfigDocumentChange(
                kind=spec.kind,
                old_value_json=old,
                new_value_json=value,
                changed_at=now,
                actor=actor,
                note=note,
            )
        )
        await self._bump_settings_revision()
        await self.session.flush()
        cache_update(spec.kind, await self.revision(spec.kind), parsed)
        log.info("config_docs.applied", kind=spec.kind, actor=actor)
        return True

    async def _meta(self) -> AppSettingMeta:
        meta = await self.session.get(AppSettingMeta, META_ROW_ID)
        if meta is None:
            # Migration 021 seeds the row; schemas built by ``create_all`` do not.
            meta = AppSettingMeta(id=META_ROW_ID, revision=0, imported_at=None)
            self.session.add(meta)
            await self.session.flush()
        return meta

    async def _bump_settings_revision(self) -> None:
        """+1 on ``app_setting_meta.revision``: the CD4 propagation hook."""

        result = await self.session.execute(
            update(AppSettingMeta)
            .where(AppSettingMeta.id == META_ROW_ID)
            .values(revision=AppSettingMeta.revision + 1)
            .execution_options(synchronize_session="fetch")
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            meta = await self._meta()
            meta.revision += 1

    async def ensure_imported(self) -> list[str]:
        """One-time import of every kind's baseline file, then the drift check.

        Returns the kinds imported (empty when every row already existed). A
        baseline that is missing or does not parse is skipped with a warning
        (§4.3: only a file that parses is imported). A kind whose stored
        document differs from its baseline file is listed in one
        ``config_docs.baseline_drift`` warning (the ``settings.env_ignored``
        pattern); the DB stays the source of truth.
        """

        imported: list[str] = []
        drifted: list[str] = []
        for spec in registry.KINDS:
            try:
                baseline = registry.baseline_document(spec)
                spec.parse(baseline)
            except Exception as exc:
                log.warning("config_docs.import_skipped", kind=spec.kind, error=str(exc))
                continue
            row = await self._row(spec.kind)
            if row is None:
                now = datetime.now(UTC)
                try:
                    async with self.session.begin_nested():
                        self.session.add(
                            ConfigDocument(
                                kind=spec.kind,
                                value_json=baseline,
                                updated_at=now,
                                updated_by=IMPORT_ACTOR,
                                imported_at=now,
                            )
                        )
                        self.session.add(
                            ConfigDocumentChange(
                                kind=spec.kind,
                                old_value_json=None,
                                new_value_json=baseline,
                                changed_at=now,
                                actor=IMPORT_ACTOR,
                                note=f"baseline {spec.baseline}",
                            )
                        )
                        await self.session.flush()
                    await self._bump_settings_revision()
                    imported.append(spec.kind)
                except IntegrityError:
                    continue  # a concurrent start-up imported this kind first
            elif _canonical(row.value_json) != _canonical(baseline):
                drifted.append(spec.kind)
        if imported:
            log.info("config_docs.imported", kinds=imported)
        if drifted:
            log.warning(
                "config_docs.baseline_drift",
                kinds=drifted,
                detail="ignored; DB is the source of truth - change it in the data-sources "
                "UI or re-import from the baseline explicitly",
            )
        return imported

    async def import_baseline(
        self, kind: str, *, actor: str, note: str, force: bool = False
    ) -> bool:
        """Explicit re-import of the baseline file (the 从基线导入 action, §4.3).

        ``force=False`` imports only when no row exists yet (idempotent, the
        ``ensure_imported`` contract for one kind); ``force=True`` overwrites
        the stored document with the baseline. Returns whether something was
        written. The note is mandatory like every other write.
        """

        spec = registry.spec_for(kind)
        if not note or not note.strip():
            raise ConfigDocValidationError(f"{kind}: a change note is required")
        baseline = registry.baseline_document(spec)
        try:
            parsed = spec.parse(baseline)
        except ValueError as exc:
            raise ConfigDocValidationError(str(exc)) from exc
        if not force and await self._row(spec.kind) is not None:
            return False
        return await self._write(
            spec, baseline, parsed, actor=actor, note=note, mark_imported=True
        )

    async def baseline_status(self) -> dict[str, dict[str, Any]]:
        """Per kind ``{kind, title, imported, baseline, drifted}`` for the API / UI.

        ``baseline`` is the baseline document's own ``version`` when it has
        one, else ``sha256:<12 hex>`` of its canonical JSON; ``None`` when the
        file cannot be read or parsed. ``drifted`` compares the stored document
        with the baseline file and is ``False`` before the first import.
        """

        status: dict[str, dict[str, Any]] = {}
        for spec in registry.KINDS:
            try:
                baseline = registry.baseline_document(spec)
                spec.parse(baseline)
            except Exception:
                baseline = None
            row = await self._row(spec.kind)
            status[spec.kind] = {
                "kind": spec.kind,
                "title": spec.title,
                "imported": row is not None,
                "baseline": _baseline_ref(baseline),
                "drifted": row is not None
                and baseline is not None
                and _canonical(row.value_json) != _canonical(baseline),
            }
        return status
