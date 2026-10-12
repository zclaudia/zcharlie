"""Repositories for research source bindings and immutable dataset versions.

The dataset repository never rewrites a version: :meth:`insert_version` only
computes the next ``revision`` and its ``previous_id``. Content-hash
deduplication belongs to the store (task 05), which checks
:meth:`ResearchDatasetRepository.by_content_hash` before inserting.
"""

from datetime import date

from sqlalchemy import ColumnElement, or_, select

from investment_agent.materials.contracts import DatasetKind, SubjectKind
from investment_agent.storage.models.research_material import (
    ResearchDataset,
    ResearchSourceBinding,
)
from investment_agent.storage.repositories.base import Repository

_SUBJECT_KINDS = tuple(member.value for member in SubjectKind)
_DATASET_KINDS = tuple(member.value for member in DatasetKind)


def _check_subject_kind(subject_kind: str) -> None:
    if subject_kind not in _SUBJECT_KINDS:
        raise ValueError(f"unsupported subject_kind {subject_kind!r} (expected {_SUBJECT_KINDS})")


def _check_kind(kind: str) -> None:
    if kind not in _DATASET_KINDS:
        raise ValueError(f"unsupported dataset kind {kind!r} (expected {_DATASET_KINDS})")


class ResearchSourceBindingRepository(Repository[ResearchSourceBinding]):
    """Read side of the controlled source bindings (rows are append-only)."""

    model = ResearchSourceBinding

    async def list_for(
        self, subject_kind: str, subject_id: int, *, at: date | None = None
    ) -> list[ResearchSourceBinding]:
        """Bindings of one subject, oldest interval first.

        ``at`` keeps only bindings whose interval covers that date
        (``valid_from <= at`` and ``valid_to`` null or ``valid_to > at``).
        """

        _check_subject_kind(subject_kind)
        conditions: list[ColumnElement[bool]] = [
            ResearchSourceBinding.subject_kind == subject_kind,
            ResearchSourceBinding.subject_id == subject_id,
        ]
        if at is not None:
            conditions.append(ResearchSourceBinding.valid_from <= at)
            conditions.append(
                or_(
                    ResearchSourceBinding.valid_to.is_(None),
                    ResearchSourceBinding.valid_to > at,
                )
            )
        stmt = (
            select(ResearchSourceBinding)
            .where(*conditions)
            .order_by(ResearchSourceBinding.valid_from, ResearchSourceBinding.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def symbol_for(
        self, subject_kind: str, subject_id: int, source: str, *, at: date | None = None
    ) -> str | None:
        """The source's symbol for a subject, from the newest covering interval."""

        _check_subject_kind(subject_kind)
        conditions: list[ColumnElement[bool]] = [
            ResearchSourceBinding.subject_kind == subject_kind,
            ResearchSourceBinding.subject_id == subject_id,
            ResearchSourceBinding.source == source,
        ]
        if at is not None:
            conditions.append(ResearchSourceBinding.valid_from <= at)
            conditions.append(
                or_(
                    ResearchSourceBinding.valid_to.is_(None),
                    ResearchSourceBinding.valid_to > at,
                )
            )
        stmt = (
            select(ResearchSourceBinding.source_symbol)
            .where(*conditions)
            .order_by(ResearchSourceBinding.valid_from.desc(), ResearchSourceBinding.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class ResearchDatasetRepository(Repository[ResearchDataset]):
    """Versioned read side of the immutable research datasets.

    Revisions count per ``(kind, subject_kind, subject_id, source, period_end)``
    key, so ``latest`` over a key with several sources is only well defined for
    the pilot's one-source-per-key bindings; ``history`` orders by the revision
    counter, which is the append order of that key.
    """

    model = ResearchDataset

    async def latest(
        self,
        *,
        kind: str,
        subject_kind: str,
        subject_id: int,
        period_end: date | None = None,
    ) -> ResearchDataset | None:
        """Newest version of the subject's datasets of ``kind``.

        ``period_end=None`` selects the null-period (``filing_index``) datasets,
        not "any period".
        """

        _check_kind(kind)
        _check_subject_kind(subject_kind)
        stmt = (
            select(ResearchDataset)
            .where(
                ResearchDataset.kind == kind,
                ResearchDataset.subject_kind == subject_kind,
                ResearchDataset.subject_id == subject_id,
                _period_condition(period_end),
            )
            .order_by(ResearchDataset.revision.desc(), ResearchDataset.id.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def history(
        self, *, kind: str, subject_kind: str, subject_id: int
    ) -> list[ResearchDataset]:
        """Every version of every source for the subject, in revision order."""

        _check_kind(kind)
        _check_subject_kind(subject_kind)
        stmt = (
            select(ResearchDataset)
            .where(
                ResearchDataset.kind == kind,
                ResearchDataset.subject_kind == subject_kind,
                ResearchDataset.subject_id == subject_id,
            )
            .order_by(ResearchDataset.revision, ResearchDataset.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def by_content_hash(self, *, content_hash: str) -> ResearchDataset | None:
        """Earliest row with this content hash, or None.

        The hash identifies a payload/raw version, not a global key: identical
        content under a different subject can share it. Callers deduplicating a
        draft (task 05) must compare the returned row's kind, subject and period
        with the draft before reusing it.
        """

        stmt = (
            select(ResearchDataset)
            .where(ResearchDataset.content_hash == content_hash)
            .order_by(ResearchDataset.id)
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def insert_version(self, row: ResearchDataset) -> ResearchDataset:
        """Append ``row`` as the next version of its dataset key.

        ``revision`` becomes ``max + 1`` of the key (1 for the first version)
        and ``previous_id`` points at the version it supersedes. Duplicate
        content is rejected by the unique constraint; the caller deduplicates
        first (task 05 store).
        """

        _check_kind(row.kind)
        _check_subject_kind(row.subject_kind)
        if not row.content_hash:
            raise ValueError("content_hash is the version identity and must be set")
        stmt = (
            select(ResearchDataset)
            .where(
                ResearchDataset.kind == row.kind,
                ResearchDataset.subject_kind == row.subject_kind,
                ResearchDataset.subject_id == row.subject_id,
                ResearchDataset.source == row.source,
                _period_condition(row.period_end),
            )
            .order_by(ResearchDataset.revision.desc(), ResearchDataset.id.desc())
            .limit(1)
        )
        previous = (await self.session.execute(stmt)).scalar_one_or_none()
        if previous is None:
            row.revision = 1
            row.previous_id = None
        else:
            row.revision = previous.revision + 1
            row.previous_id = previous.id
        self.session.add(row)
        await self.session.flush()
        return row


def _period_condition(period_end: date | None) -> ColumnElement[bool]:
    if period_end is None:
        return ResearchDataset.period_end.is_(None)
    return ResearchDataset.period_end == period_end
