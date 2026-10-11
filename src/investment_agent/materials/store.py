"""The single write path for immutable research material (plan 05).

:meth:`MaterialStore.save` is the only way a dataset version enters the
database, and it always runs the same four steps:

1. ``validate_draft`` — a contract violation raises ``ValueError`` before
   anything is written (it is a code defect, not a data problem to swallow);
2. ``content_hash(draft)`` is the version identity. An existing version with
   the same hash *under the same dataset key* is returned with
   ``created=False`` and neither the row nor the raw artifact is touched
   (re-importing the same evidence is a no-op);
3. non-empty ``draft.raw_bytes`` are written to ``raw_path`` — first a
   ``.tmp`` file next to the target, then ``os.replace``, parents created —
   and ``raw_ref`` stores the path **relative to** ``data_dir``;
4. ``insert_version`` appends the row; ``revision`` and ``previous_id`` are
   computed by the repository (task 02), so changed content becomes a new
   revision that points at the version it supersedes.

Timestamps never take part in the version identity: ``captured_at`` and
``source_fetched_at`` are row fields only, so the same evidence imported at
two different moments stays one version, and an import can carry the original
research fetch time without it leaking into the hash.

``by_content_hash`` is not a global key (identical content under another
subject shares the hash, see task 02), so the store compares the returned
row's ``kind``/``subject_kind``/``subject_id``/``source``/``period_end`` with
the draft and falls back to a key-scoped lookup before reusing a version.
:meth:`MaterialStore.latest` has no ``source`` argument (the repository's
``latest`` is one key, not one source); with several sources per key the
caller must compare ``ResearchDataset.source`` itself.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import ColumnElement, select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.materials.contracts import (
    DatasetDraft,
    DatasetKind,
    SubjectKind,
    content_hash,
    validate_draft,
)
from investment_agent.storage.models.research_material import ResearchDataset
from investment_agent.storage.repositories.research_material import ResearchDatasetRepository

RAW_ROOT = Path("research-material")
"""Directory under ``data_dir`` that holds the immutable raw artifacts."""

_HEX_HASH = re.compile(r"[0-9a-f]{2,}")


@dataclass(frozen=True)
class SavedDataset:
    """What one :meth:`MaterialStore.save` call did.

    ``created=False`` means the content was already stored under the same
    dataset key and nothing was written; ``raw_ref`` is relative to
    ``data_dir`` (``None`` when the version carries no raw artifact).
    """

    id: int
    revision: int
    content_hash: str
    raw_ref: str | None
    created: bool


class MaterialStore:
    """Immutable, content-hash versioned storage of one source's material."""

    def __init__(self, session: AsyncSession, *, data_dir: Path) -> None:
        self._session = session
        self._data_dir = Path(data_dir)
        self._datasets = ResearchDatasetRepository(session)

    async def save(self, draft: DatasetDraft) -> SavedDataset:
        """Store ``draft`` as a version; return the existing version when unchanged.

        Raises ``ValueError`` (nothing written, no row) when the draft does not
        pass :func:`validate_draft`.
        """

        problems = validate_draft(draft)
        if problems:
            raise ValueError("; ".join(problems))
        digest = content_hash(draft)
        existing = await self._find_version(draft, digest)
        if existing is not None:
            return SavedDataset(
                id=existing.id,
                revision=existing.revision,
                content_hash=existing.content_hash,
                raw_ref=existing.raw_ref,
                created=False,
            )
        raw_ref: str | None = None
        if draft.raw_bytes:
            target = self.raw_path(
                subject_kind=draft.subject_kind,
                subject_id=draft.subject_id,
                kind=draft.kind,
                content_hash=digest,
            )
            _write_atomically(target, draft.raw_bytes)
            raw_ref = target.relative_to(self._data_dir).as_posix()
        row = ResearchDataset(
            kind=DatasetKind(draft.kind).value,
            subject_kind=SubjectKind(draft.subject_kind).value,
            subject_id=draft.subject_id,
            source=draft.source,
            schema_version=draft.schema_version,
            adapter_version=draft.adapter_version,
            period_end=draft.period.end if draft.period is not None else None,
            period_basis=(draft.period.basis.value if draft.period is not None else None),
            content_hash=digest,
            captured_at=draft.captured_at,
            source_fetched_at=draft.source_fetched_at,
            raw_ref=raw_ref,
            payload=draft.payload,
        )
        stored = await self._datasets.insert_version(row)
        return SavedDataset(
            id=stored.id,
            revision=stored.revision,
            content_hash=stored.content_hash,
            raw_ref=stored.raw_ref,
            created=True,
        )

    async def latest(
        self,
        *,
        kind: str | DatasetKind,
        subject_kind: str | SubjectKind,
        subject_id: int,
        period_end: date | None = None,
    ) -> ResearchDataset | None:
        """Newest revision of the subject's ``kind`` datasets ending ``period_end``.

        ``period_end=None`` selects the null-period datasets (``filing_index``),
        not "any period". The row is not filtered by source: with more than one
        source under a key the caller compares ``row.source``.
        """

        return await self._datasets.latest(
            kind=DatasetKind(kind).value,
            subject_kind=SubjectKind(subject_kind).value,
            subject_id=subject_id,
            period_end=period_end,
        )

    async def history(
        self,
        *,
        kind: str | DatasetKind,
        subject_kind: str | SubjectKind,
        subject_id: int,
    ) -> tuple[ResearchDataset, ...]:
        """Every stored version of the subject's ``kind``, in revision order."""

        rows = await self._datasets.history(
            kind=DatasetKind(kind).value,
            subject_kind=SubjectKind(subject_kind).value,
            subject_id=subject_id,
        )
        return tuple(rows)

    def raw_path(
        self,
        *,
        subject_kind: str | SubjectKind,
        subject_id: int,
        kind: str | DatasetKind,
        content_hash: str,
        suffix: str = ".json",
    ) -> Path:
        """Absolute path of the raw artifact of one version.

        ``<data_dir>/research-material/<subject_kind>/<subject_id>/<kind>/<hash 前 2 位>/<hash><suffix>``
        """

        token = str(content_hash)
        if not _HEX_HASH.fullmatch(token):
            raise ValueError(f"content_hash {content_hash!r} must be a hex digest")
        if not isinstance(subject_id, int) or isinstance(subject_id, bool) or subject_id <= 0:
            raise ValueError("subject_id must be a positive integer")
        return (
            self._data_dir
            / RAW_ROOT
            / SubjectKind(subject_kind).value
            / str(subject_id)
            / DatasetKind(kind).value
            / token[:2]
            / f"{token}{suffix}"
        )

    async def _find_version(self, draft: DatasetDraft, digest: str) -> ResearchDataset | None:
        """The stored version of ``draft``'s key with this hash, if any."""

        found = await self._datasets.by_content_hash(content_hash=digest)
        if found is None:
            return None
        if _is_same_key(found, draft):
            return found
        # by_content_hash returns the earliest row with this hash; identical
        # content under another subject shares it, so look up the exact key.
        stmt = (
            select(ResearchDataset)
            .where(
                ResearchDataset.kind == DatasetKind(draft.kind).value,
                ResearchDataset.subject_kind == SubjectKind(draft.subject_kind).value,
                ResearchDataset.subject_id == draft.subject_id,
                ResearchDataset.source == draft.source,
                _period_condition(draft.period.end if draft.period is not None else None),
                ResearchDataset.content_hash == digest,
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


def _is_same_key(row: ResearchDataset, draft: DatasetDraft) -> bool:
    period_end = draft.period.end if draft.period is not None else None
    return (
        row.kind == DatasetKind(draft.kind).value
        and row.subject_kind == SubjectKind(draft.subject_kind).value
        and row.subject_id == draft.subject_id
        and row.source == draft.source
        and row.period_end == period_end
    )


def _period_condition(period_end: date | None) -> ColumnElement[bool]:
    if period_end is None:
        return ResearchDataset.period_end.is_(None)
    return ResearchDataset.period_end == period_end


def _write_atomically(target: Path, content: bytes) -> None:
    """Write ``content`` to ``target`` via a temporary file and ``os.replace``."""

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.write_bytes(content)
    os.replace(tmp, target)
