"""Import saved research evidence into the immutable material store (plan 05).

The first phase is offline: the material that the HK/US pilot needs already
exists as saved evidence under ``research/market-data-validation/evidence/``
(response body plus a ``<name>.meta.json`` sidecar per request). This module
replays that evidence through the very same adapters the online path uses —
injected as ``adapters: Mapping[str, MaterialAdapter]`` so the module never
imports ``hk_native``/``us_sec`` itself — and writes the result through
:class:`~investment_agent.materials.store.MaterialStore`.

Time discipline: the stored ``captured_at`` is the moment this import really
ran (``imported_at`` or now), never the historical ``fetched_at_utc`` from the
sidecar; that original research fetch time is kept in ``source_fetched_at``,
and when the sidecar is missing or carries no ``fetched_at_utc`` the value
stays ``None`` — the import time is never written into it. The same corpus
imported twice is one set of versions (identical content hash → the store
returns the existing version), so re-imports are no-ops.

Failure isolation: one unreadable or missing file, one missing sidecar or one
adapter ``problems`` entry never stops the other sources; each is reported in
:class:`ImportReport.problems` and the affected ``<company_key>/<source>``
label is listed in ``skipped``. A draft that fails
:func:`~investment_agent.materials.contracts.validate_draft` is a code defect
and raises out of :meth:`MaterialStore.save` instead of being swallowed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.security import ControlledIdentity, source_symbol
from investment_agent.materials.contracts import (
    DatasetDraft,
    MaterialAdapter,
    RawPayload,
    SubjectKind,
    UpstreamRequest,
)
from investment_agent.materials.store import MaterialStore, SavedDataset

META_SUFFIX = ".meta.json"
"""Sidecar written next to every saved response (``<name>.json.meta.json``)."""


@dataclass(frozen=True)
class ImportReport:
    """What one offline import stored, and what it could not.

    ``saved`` are the store outcomes in import order; ``problems`` are
    human-readable per-item reasons (missing file, missing sidecar, unknown
    fetch time, adapter problems, ...), each prefixed with the
    ``<company_key>/<source>`` label it belongs to; ``skipped`` lists the
    labels that were not imported cleanly (no binding, nothing located,
    adapter problems, or no dataset produced), so a caller can tell a clean
    replay from a partial one without parsing the reasons. A source whose
    response file is usable but whose sidecar is missing still imports — it is
    listed in both ``problems`` and ``skipped``.
    """

    saved: tuple[SavedDataset, ...]
    problems: tuple[str, ...]
    skipped: tuple[str, ...]


async def import_offline_evidence(
    session: AsyncSession,
    *,
    data_dir: Path,
    source_dir: Path,
    identities: Sequence[ControlledIdentity],
    company_ids: Mapping[str, int],
    adapters: Mapping[str, MaterialAdapter],
    imported_at: datetime | None = None,
) -> ImportReport:
    """Import the evidence under ``source_dir`` for ``identities``.

    ``company_ids`` maps ``company_key -> company.id`` (resolved by the caller
    from the controlled pilot list); the adapter never sees database ids, so
    this module stamps the resolved ``subject_id`` on company-level drafts.
    ``source_dir`` is the evidence root (for example
    ``.../evidence/remaining-final-03/raw``); each adapter request is matched
    to ``<name>`` and ``<name>.meta.json`` inside it. Nothing is committed:
    the session stays the caller's to commit.
    """

    captured_at = _import_time(imported_at)
    batch = _read_evidence_batch(
        root=Path(source_dir),
        identities=identities,
        company_ids=company_ids,
        adapters=adapters,
        captured_at=captured_at,
    )
    store = MaterialStore(session, data_dir=Path(data_dir))
    saved = [await store.save(draft) for draft in batch.drafts]
    return ImportReport(saved=tuple(saved), problems=batch.problems, skipped=batch.skipped)


@dataclass(frozen=True)
class _Batch:
    """Everything one pass over the evidence directory produced."""

    drafts: tuple[DatasetDraft, ...]
    problems: tuple[str, ...]
    skipped: tuple[str, ...]


def _read_evidence_batch(
    *,
    root: Path,
    identities: Sequence[ControlledIdentity],
    company_ids: Mapping[str, int],
    adapters: Mapping[str, MaterialAdapter],
    captured_at: datetime,
) -> _Batch:
    """Read, locate and parse the evidence (all blocking IO happens here)."""

    if not root.is_dir():
        return _Batch((), (f"evidence directory not found: {root}",), ())
    drafts: list[DatasetDraft] = []
    problems: list[str] = []
    skipped: list[str] = []
    for identity in identities:
        company_id = company_ids.get(identity.company_key)
        if company_id is None:
            problems.append(f"{identity.company_key}: no company id supplied for the identity")
            skipped.append(identity.company_key)
            continue
        for source, adapter in adapters.items():
            label = f"{identity.company_key}/{source}"
            source_problem_count = len(problems)
            symbol = source_symbol(identity, source)
            if symbol is None:
                skipped.append(f"{label}: the pilot list binds no symbol for this source")
                continue
            requests = tuple(adapter.requests(identity, periods=()))
            if not requests:
                problems.append(f"{label}: the adapter requested no evidence file")
                skipped.append(label)
                continue
            symbol_dirs = _symbol_dirs(identity, symbol)
            raws: list[_LocatedRaw] = []
            for request in requests:
                raw, reason = _locate(root, request, symbol_dirs=symbol_dirs)
                if raw is None:
                    problems.append(f"{label}: {reason}")
                    continue
                if reason is not None:
                    problems.append(f"{label}: {reason}")
                raws.append(raw)
            if not raws:
                skipped.append(label)
                continue
            result = adapter.parse(
                identity,
                raw=tuple(raw.as_payload(imported_at=captured_at) for raw in raws),
                periods=(),
            )
            for problem in result.problems:
                problems.append(f"{label}: {problem}")
            if not result.drafts and not result.problems:
                problems.append(f"{label}: the adapter produced no dataset")
            drafts.extend(
                _prepare_draft(draft, company_id=company_id, raws=raws, captured_at=captured_at)
                for draft in result.drafts
            )
            if len(problems) > source_problem_count:
                skipped.append(label)
    return _Batch(tuple(drafts), tuple(problems), tuple(skipped))


@dataclass(frozen=True)
class _LocatedRaw:
    """One located response file with everything its sidecar told us."""

    request_name: str
    """The request's own name; ``RawPayload.name`` carries it for parity with the
    online path (where the fetcher names each response after its request)."""
    media_type: str
    path: Path
    content: bytes
    fetched_at: datetime | None
    """Original research fetch time; ``None`` means unknown, never back-filled."""
    source_url: str
    """The sidecar's ``url`` when it has one, else the URL the request asked for."""
    http_status: int
    """Reported HTTP status; ``0`` when the sidecar did not report one."""

    def as_payload(self, *, imported_at: datetime) -> RawPayload:
        """The adapter-facing payload of this file.

        ``fetched_at`` carries the import moment only when the sidecar has no
        original fetch time: it describes when *this* run read the bytes, and
        the persisted provenance keeps ``source_fetched_at`` unknown.
        """

        return RawPayload(
            name=self.request_name,
            content=self.content,
            media_type=self.media_type,
            fetched_at=self.fetched_at or imported_at,
            source_url=self.source_url,
            http_status=self.http_status,
        )


def _locate(
    root: Path, request: UpstreamRequest, *, symbol_dirs: frozenset[str]
) -> tuple[_LocatedRaw | None, str | None]:
    """Resolve ``request`` to its evidence file, read it and read its sidecar.

    Returns the located raw and, when the file was usable but its sidecar was
    not, the readable reason to report; ``(None, reason)`` means the file
    itself could not be used.
    """

    path, reason = _resolve_file(root, request.name, symbol_dirs)
    if path is None:
        return None, reason
    try:
        content = path.read_bytes()
    except OSError as exc:
        return None, f"unreadable evidence file {path.name}: {type(exc).__name__}: {exc}"

    def located(
        *, fetched_at: datetime | None = None, source_url: str = request.url, http_status: int = 0
    ) -> _LocatedRaw:
        return _LocatedRaw(
            request_name=request.name,
            media_type=request.media_type,
            path=path,
            content=content,
            fetched_at=fetched_at,
            source_url=source_url,
            http_status=http_status,
        )

    sidecar = path.with_name(path.name + META_SUFFIX)
    if not sidecar.is_file():
        return located(), f"missing sidecar {sidecar.name}; source_fetched_at stays unknown"
    try:
        document = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return (
            located(),
            f"unreadable sidecar {sidecar.name}: {type(exc).__name__}: {exc}",
        )
    if not isinstance(document, dict):
        return located(), f"sidecar {sidecar.name} must be a JSON object"
    fetched_at, problem = _sidecar_time(document, sidecar.name)
    url = document.get("url")
    return (
        located(
            fetched_at=fetched_at,
            source_url=url if isinstance(url, str) and url.strip() else request.url,
            http_status=_sidecar_status(document),
        ),
        problem,
    )


def _prepare_draft(
    draft: DatasetDraft,
    *,
    company_id: int,
    raws: Sequence[_LocatedRaw],
    captured_at: datetime,
) -> DatasetDraft:
    """Stamp the import's own facts (ids, times, raw artifact) on an adapter draft."""

    tied = _tied_raw(draft, raws)
    raw_bytes: bytes | None
    if tied is not None:
        raw_bytes = draft.raw_bytes or tied.content
        source_fetched_at = tied.fetched_at
        source_url = tied.source_url
    else:
        raw_bytes = draft.raw_bytes
        source_fetched_at = _earliest_fetch(raws)
        source_url = None
    subject_id = (
        company_id if SubjectKind(draft.subject_kind) is SubjectKind.COMPANY else draft.subject_id
    )
    return replace(
        draft,
        subject_id=subject_id,
        captured_at=captured_at,
        raw_bytes=raw_bytes,
        source_fetched_at=source_fetched_at,
        raw_source_url=source_url or draft.raw_source_url,
    )


def _tied_raw(draft: DatasetDraft, raws: Sequence[_LocatedRaw]) -> _LocatedRaw | None:
    """The raw file a draft was built from: its own bytes, or the only file.

    Adapters may attach the response they parsed to ``raw_bytes``; that byte
    identity is the only per-draft link this module can check. A source with a
    single located response file is unambiguous. With several files and no
    attached bytes the link is unknown and the store writes no raw artifact:
    which response a payload came from is never guessed.
    """

    if draft.raw_bytes:
        for raw in raws:
            if raw.content == draft.raw_bytes:
                return raw
        return None
    if len(raws) == 1:
        return raws[0]
    return None


def _earliest_fetch(raws: Sequence[_LocatedRaw]) -> datetime | None:
    """Earliest original fetch time among the files, or ``None`` if all unknown.

    A real timestamp from the evidence batch, used only when a draft cannot be
    tied to one file; never the import time.
    """

    known = [raw.fetched_at for raw in raws if raw.fetched_at is not None]
    return min(known) if known else None


def _resolve_file(
    root: Path, name: str, symbol_dirs: frozenset[str]
) -> tuple[Path | None, str | None]:
    """Resolve a request name to one evidence file under ``root``.

    ``name`` may carry its own relative directory (``shards/<file>.json``). A
    bare file name is looked up directly, then inside each symbol directory of
    the identity (``00005`` / ``00005.HK`` / the ticker), and finally by a
    bounded recursive search that prefers a matching path suffix and a path
    segment equal to one of those symbol directories. An ambiguous name is
    reported, never guessed.
    """

    relative = Path(name)
    if not name.strip() or relative.is_absolute() or ".." in relative.parts:
        return None, f"request name {name!r} is not a relative evidence file name"
    candidates = [root / relative]
    candidates.extend(root / directory / relative for directory in sorted(symbol_dirs))
    for candidate in candidates:
        if candidate.is_file():
            return candidate, None
    matches = sorted(path for path in root.rglob(relative.name) if path.is_file())
    if not matches:
        return None, f"no evidence file named {name!r} under {root}"
    pool = [path for path in matches if _has_suffix(path, relative)]
    pool = pool or matches
    scoped = [path for path in pool if symbol_dirs.intersection(path.parts)]
    pool = scoped or pool
    if len(pool) == 1:
        return pool[0], None
    return None, f"{len(matches)} evidence files match {name!r} under {root}; none is unique"


def _symbol_dirs(identity: ControlledIdentity, symbol: str) -> frozenset[str]:
    """Directory names the evidence tree may use for this identity's response file."""

    return frozenset(
        part for part in (symbol, symbol.split(".", 1)[0], identity.ticker) if part.strip()
    )


def _sidecar_time(document: Mapping[str, object], where: str) -> tuple[datetime | None, str | None]:
    """Read ``fetched_at_utc``; a missing or unusable value stays unknown."""

    raw_value = document.get("fetched_at_utc")
    if raw_value is None:
        return None, f"{where} has no fetched_at_utc; source_fetched_at stays unknown"
    if not isinstance(raw_value, str):
        return None, f"{where} fetched_at_utc must be an ISO-8601 string"
    try:
        moment = datetime.fromisoformat(raw_value)
    except ValueError:
        return None, f"{where} fetched_at_utc {raw_value!r} is not an ISO-8601 timestamp"
    if moment.tzinfo is None or moment.utcoffset() is None:
        return None, f"{where} fetched_at_utc {raw_value!r} carries no UTC offset"
    return moment, None


def _sidecar_status(document: Mapping[str, object]) -> int:
    status = document.get("status")
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    return 0


def _has_suffix(path: Path, relative: Path) -> bool:
    text = path.as_posix()
    return text == relative.as_posix() or text.endswith("/" + relative.as_posix())


def _import_time(imported_at: datetime | None) -> datetime:
    if imported_at is None:
        return datetime.now(UTC)
    if imported_at.tzinfo is None or imported_at.utcoffset() is None:
        raise ValueError("imported_at must be timezone-aware")
    return imported_at
