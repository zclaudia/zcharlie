"""Repositories for the immutable peer snapshots (append-only).

Neither repository exposes an UPDATE or DELETE path: rosters and annual facts
are versioned by appending rows (new ``revision`` / new ``semantic_hash``), and
the ORM listeners plus the SQLite triggers refuse mutations of saved rows.
Readers pick a version explicitly (``by_revision`` / ``by_unique_key``); there
is no silent "latest wins" rewrite.
"""

from datetime import date

from sqlalchemy import ColumnElement, func, select

from investment_agent.storage.models.peer import PeerAnnualSnapshot, PeerCohortSnapshot
from investment_agent.storage.repositories.base import Repository


class PeerCohortSnapshotRepository(Repository[PeerCohortSnapshot]):
    """Read side of the named-cohort rosters (rows are append-only)."""

    model = PeerCohortSnapshot

    async def add(self, row: PeerCohortSnapshot) -> PeerCohortSnapshot:
        """Append one roster version; identity is (cohort_key, revision, id).

        Duplicate content for an existing (cohort_key, revision) is rejected by
        the unique constraint and the immutability trigger; the caller
        deduplicates by :meth:`by_content_hash` first.
        """

        if not row.cohort_key:
            raise ValueError("cohort_key is the roster identity and must be set")
        if row.revision < 1:
            raise ValueError("revision must be a positive version number")
        if not row.content_hash:
            raise ValueError("content_hash is the version identity and must be set")
        self.session.add(row)
        await self.session.flush()
        return row

    async def next_revision(self, cohort_key: str) -> int:
        """``max(revision) + 1`` of the cohort (1 for the first version)."""

        current = await self.session.scalar(
            select(func.max(PeerCohortSnapshot.revision)).where(
                PeerCohortSnapshot.cohort_key == cohort_key
            )
        )
        return (current or 0) + 1

    async def latest(self, *, cohort_key: str) -> PeerCohortSnapshot | None:
        """The roster with the highest revision of the cohort."""

        return (
            await self.session.execute(
                select(PeerCohortSnapshot)
                .where(PeerCohortSnapshot.cohort_key == cohort_key)
                .order_by(PeerCohortSnapshot.revision.desc(), PeerCohortSnapshot.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def by_revision(self, *, cohort_key: str, revision: int) -> PeerCohortSnapshot | None:
        return (
            await self.session.execute(
                select(PeerCohortSnapshot).where(
                    PeerCohortSnapshot.cohort_key == cohort_key,
                    PeerCohortSnapshot.revision == revision,
                )
            )
        ).scalar_one_or_none()

    async def history(self, *, cohort_key: str) -> list[PeerCohortSnapshot]:
        """Every roster version of the cohort, oldest revision first."""

        return list(
            (
                await self.session.execute(
                    select(PeerCohortSnapshot)
                    .where(PeerCohortSnapshot.cohort_key == cohort_key)
                    .order_by(PeerCohortSnapshot.revision, PeerCohortSnapshot.id)
                )
            ).scalars()
        )

    async def by_content_hash(self, *, cohort_key: str, content_hash: str) -> PeerCohortSnapshot | None:
        """The roster version with this content hash, or None (import idempotency)."""

        return (
            await self.session.execute(
                select(PeerCohortSnapshot)
                .where(
                    PeerCohortSnapshot.cohort_key == cohort_key,
                    PeerCohortSnapshot.content_hash == content_hash,
                )
                .order_by(PeerCohortSnapshot.revision)
                .limit(1)
            )
        ).scalar_one_or_none()


class PeerAnnualSnapshotRepository(Repository[PeerAnnualSnapshot]):
    """Versioned read side of the immutable annual fact snapshots."""

    model = PeerAnnualSnapshot

    async def add(self, row: PeerAnnualSnapshot) -> PeerAnnualSnapshot:
        """Append one snapshot version.

        The unique key
        ``(company_id, report_period, provider, adapter_version, semantic_hash)``
        rejects a concurrent duplicate; the caller deduplicates by
        :meth:`by_unique_key` first. ``captured_at`` is the first save time and
        stays with the row forever.
        """

        if row.company_id < 1 or row.listing_id < 1:
            raise ValueError("company_id and listing_id must be positive directory ids")
        if not row.provider or not row.adapter_version:
            raise ValueError("provider and adapter_version must be set")
        if not row.semantic_hash or not row.content_hash:
            raise ValueError("semantic_hash and content_hash are the version identity")
        self.session.add(row)
        await self.session.flush()
        return row

    async def by_unique_key(
        self,
        *,
        company_id: int,
        report_period: date,
        provider: str,
        adapter_version: str,
        semantic_hash: str,
    ) -> PeerAnnualSnapshot | None:
        """The exact snapshot version identified by the unique key."""

        return (
            await self.session.execute(
                select(PeerAnnualSnapshot).where(
                    PeerAnnualSnapshot.company_id == company_id,
                    PeerAnnualSnapshot.report_period == report_period,
                    PeerAnnualSnapshot.provider == provider,
                    PeerAnnualSnapshot.adapter_version == adapter_version,
                    PeerAnnualSnapshot.semantic_hash == semantic_hash,
                )
            )
        ).scalar_one_or_none()

    async def versions(
        self,
        *,
        company_id: int,
        report_period: date,
        provider: str | None = None,
    ) -> list[PeerAnnualSnapshot]:
        """Every saved version of the company's period, oldest capture first.

        ``provider`` narrows to one source when given; without it all sources'
        versions are returned so readers can apply their own conflict and
        cutoff rules explicitly.
        """

        conditions: list[ColumnElement[bool]] = [
            PeerAnnualSnapshot.company_id == company_id,
            PeerAnnualSnapshot.report_period == report_period,
        ]
        if provider is not None:
            conditions.append(PeerAnnualSnapshot.provider == provider)
        return list(
            (
                await self.session.execute(
                    select(PeerAnnualSnapshot)
                    .where(*conditions)
                    .order_by(PeerAnnualSnapshot.captured_at, PeerAnnualSnapshot.id)
                )
            ).scalars()
        )
