"""Process-level access to the config documents (config-docs-plan §4.4).

The DB is async (aiosqlite) but the config documents' consumers are sync —
the factor code calls ``default_field_sources()`` mid-computation. This module
bridges the two with an in-process cache ``{kind: (revision, parsed)}``:

* ``ensure_imported()`` runs at process start-up (API lifespan, scheduler,
  worker: everywhere ``settings.loader.ensure_imported`` runs): the one-time
  baseline import, the drift warning, then ``prime()`` fills the cache;
* ``refresh_if_changed()`` is awaited wherever the worker / scheduler already
  re-check the settings revision (the worker's per-claim
  ``ResearchWorker.refresh_settings``, the scheduler's 60 s
  ``watch_settings`` poll) — every config-doc write bumps the settings
  revision (CD4), so the same hooks propagate both, and only kinds whose own
  revision moved are reloaded;
* ``ConfigDocStore.put()`` / ``import_baseline()`` refresh the cache with the
  just-validated parse, so the writing process's next *sync* read is fresh;
* the sync getters (``config_doc()``, ``field_sources_registry()``) return the
  cached parse and, with a cold cache (CLI, tests — no DB priming ran), parse
  the baseline file directly, the pre-DB behaviour;
* ``document_fingerprint()`` reads the raw document kept beside the parse in
  the cache entry and returns its ``{"version", "hash"}`` fingerprint — what a
  research run persists for reproducibility / audit (config-docs-plan §4.8).

Everything runs on one event loop per process, so the cache needs no lock;
the sync getters are the only cross-thread surface and only read one slot.
"""

import hashlib
import json
from typing import TYPE_CHECKING, Any, cast

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config_docs import registry
from investment_agent.config_docs.registry import ConfigDocSpec
from investment_agent.config_docs.store import ConfigDocStore
from investment_agent.storage.database import session_scope

if TYPE_CHECKING:
    from investment_agent.factors.field_sources import FieldSourceRegistry
    from investment_agent.factors.industry_groups import IndustryGroupMap

log = structlog.get_logger(__name__)

_CACHE: dict[str, tuple[int, Any, dict[str, Any] | None]] = {}
"""``{kind: (revision, parsed, raw)}``: the raw document is kept beside its
parse as the fingerprint basis (§4.8) and is ``None`` only on the store's
post-write refresh, which hands over just the already-validated parse —
``document_fingerprint`` then falls back to the baseline file. Revision ``-1``
marks a baseline-file fallback."""

_COLD = -1
_refresh_broken = False
"""Set when a refresh hit an unmigrated database; stop retrying this process."""


def cache_update(kind: str, revision: int, parsed: Any) -> None:
    """Publish a fresh parse (called by the store after a validated write).

    The store hands over the parse only, so the entry carries no raw document
    (see ``_CACHE``); ``prime`` / ``refresh_if_changed`` refill the full entry.
    """

    _CACHE[kind] = (revision, parsed, None)


def invalidate(kind: str | None = None) -> None:
    """Drop one kind's (default: every) cached parse — tests and manual resets."""

    if kind is None:
        _CACHE.clear()
    else:
        _CACHE.pop(kind, None)


def _cache_parse(spec: ConfigDocSpec, value: dict[str, Any], revision: int) -> None:
    """Parse ``value`` into the cache, falling back to the baseline file.

    A stored document the current code rejects (a registry rule tightened
    after the write) must not take reads down: the baseline parse is used and
    a warning logged, mirroring ``SettingsStore.state``'s per-key fallback.
    The cached raw document is always the one behind the cached parse, so the
    fingerprint (§4.8) names the document reads actually serve.
    """

    try:
        parsed = spec.parse(value)
    except ValueError as exc:
        log.warning("config_docs.invalid_stored_value", kind=spec.kind, error=str(exc))
        try:
            document = registry.baseline_document(spec)
            parsed = spec.parse(document)
        except Exception:
            return  # the baseline is unreadable too: keep whatever is cached
        _CACHE[spec.kind] = (revision, parsed, document)
        return
    _CACHE[spec.kind] = (revision, parsed, value)


async def prime(session: AsyncSession) -> None:
    """Load every kind's effective document and revision into the cache."""

    store = ConfigDocStore(session)
    for spec in registry.KINDS:
        value, _row = await store.get(spec.kind)
        _cache_parse(spec, value, await store.revision(spec.kind))


async def ensure_imported(session: AsyncSession) -> list[str]:
    """Start-up: the one-time baseline import, the drift warning, then prime.

    Resilient on purpose: an unmigrated database (``config_document`` tables
    missing) is logged with the migrate hint and leaves the baseline-file
    fallback in effect, so deploying this code before ``ia migrate up`` keeps
    every read working exactly as before.
    """

    try:
        store = ConfigDocStore(session)
        imported = await store.ensure_imported()
        await prime(session)
        return imported
    except Exception:
        log.exception("config_docs.startup_failed", hint="run `ia migrate up`")
        return []


async def refresh_if_changed(session: AsyncSession | None = None) -> bool:
    """Reload the kinds whose revision moved since the last prime / refresh.

    Opens its own short session when none is given (the scheduler poll). A
    failure (uninitialized engine, unmigrated schema) disables further
    refreshes for the process after one logged exception — reads keep the
    last good parse or the baseline fallback. Returns whether anything was
    reloaded.
    """

    global _refresh_broken
    if _refresh_broken:
        return False
    try:
        if session is None:
            async with session_scope() as own:
                return await _refresh(own)
        return await _refresh(session)
    except Exception:
        _refresh_broken = True
        log.exception("config_docs.refresh_failed", hint="run `ia migrate up`")
        return False


async def _refresh(session: AsyncSession) -> bool:
    store = ConfigDocStore(session)
    changed = False
    for spec in registry.KINDS:
        revision = await store.revision(spec.kind)
        cached = _CACHE.get(spec.kind)
        if cached is not None and cached[0] == revision:
            continue
        value, _row = await store.get(spec.kind)
        _cache_parse(spec, value, revision)
        changed = True
    return changed


def config_doc(kind: str) -> Any:
    """The parsed document for sync consumers: cache when primed, else the baseline."""

    spec = registry.spec_for(kind)
    cached = _CACHE.get(kind)
    if cached is not None:
        return cached[1]
    document = registry.baseline_document(spec)
    parsed = spec.parse(document)
    _CACHE[kind] = (_COLD, parsed, document)
    return parsed


def fingerprint_document(document: dict[str, Any]) -> dict[str, str]:
    """The §4.8 fingerprint of one raw config document.

    ``{"version": <the document's own version string, else "">, "hash": <the
    first 12 hex chars of the canonical-JSON SHA-256>}`` — the same canonical
    form the store's drift comparison and baseline hash use, so a fingerprint
    can be recomputed from any stored document (an async caller may hash
    ``(await ConfigDocStore.get(kind))[0]`` with this function directly). The
    encoder's ``default=str`` keeps YAML's non-JSON scalars (the baseline's
    unquoted ``date`` calibration stamps) hashable — their ``str()`` is the
    ISO string, which equals the quoted form a UI round-trip stores, so the
    file-based and DB-based documents hash identically.
    """

    version = document.get("version")
    canonical = json.dumps(
        document, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"version": version if isinstance(version, str) else "", "hash": digest[:12]}


def document_fingerprint(kind: str) -> dict[str, str]:
    """The fingerprint of ``kind``'s effective raw document (§4.8, audit).

    Reads the raw document kept beside the parse in the cache entry — the
    document ``config_doc(kind)`` serves — so a run recording it stays
    reproducible after the document has since been edited. With a cold cache
    (no priming ran: CLI, tests) or an entry whose raw document is unknown
    (the store's post-write refresh carries only the parse) the baseline file
    is read, mirroring ``config_doc``'s fallback. Safe to call from async
    code: it touches only the in-process cache and possibly the baseline file.
    """

    spec = registry.spec_for(kind)
    cached = _CACHE.get(kind)
    document = cached[2] if cached is not None and cached[2] is not None else None
    if document is None:
        document = registry.baseline_document(spec)
    return fingerprint_document(document)


def field_sources_registry() -> "FieldSourceRegistry":
    """The effective ``FieldSourceRegistry`` behind ``default_field_sources()``."""

    return cast("FieldSourceRegistry", config_doc("field_sources"))


def industry_groups_map() -> "IndustryGroupMap":
    """The effective ``IndustryGroupMap`` behind ``default_industry_groups()`` (WP3)."""

    return cast("IndustryGroupMap", config_doc("industry_groups"))
