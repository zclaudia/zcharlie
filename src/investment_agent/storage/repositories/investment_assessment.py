"""Typed persistence for immutable account-fit and ranking artifacts."""

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal, NamedTuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.candidates.ranking_contracts import RankingEntry, RankingSnapshot
from investment_agent.investor.common import ObjectRef, OwnerId
from investment_agent.portfolio.suitability_contracts import AccountFitAssessment
from investment_agent.storage.models.investment_assessment import (
    CandidateRankingEntry as CandidateRankingEntryRow,
)
from investment_agent.storage.models.investment_assessment import InvestmentAssessment
from investment_agent.supplemental_rules.canonical import stable_series_key
from investment_agent.supplemental_rules.contracts import RuleApplication

Assessment = AccountFitAssessment | RankingSnapshot | RuleApplication


class StoredRanking(NamedTuple):
    """A persisted header and its complete, ordinal-ordered entry tuple."""

    snapshot: RankingSnapshot
    entries: tuple[RankingEntry, ...]


class InvestmentAssessmentRepository:
    """Save and read validated P1 artifacts without mutating stored payloads.

    The methods flush inside a savepoint but leave commit ownership with the
    caller. A ranking header and all entries therefore participate in the
    caller's one transaction while a failed bundle cannot leave a partial
    flush in that transaction.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save_assessment(
        self, owner_id: OwnerId, assessment: AccountFitAssessment
    ) -> AccountFitAssessment:
        """Persist one account-fit result or return an exact input-hash replay."""

        _check_owner(owner_id)
        assessment = AccountFitAssessment.model_validate(assessment.model_dump(mode="json"))
        if assessment.reference.kind != "account_fit":
            raise ValueError("account-fit assessment reference required")
        row = await self.session.get(
            InvestmentAssessment,
            (str(assessment.id), assessment.reference.revision),
        )
        if row is not None:
            if row.owner_id != owner_id or row.kind != "account_fit":
                raise ValueError("assessment UUID is already used by another owner or kind")
            saved = _parse_assessment(row)
            _check_header_columns(row, saved)
            if not isinstance(saved, AccountFitAssessment) or _json_payload(saved) != _json_payload(assessment):
                raise ValueError("account-fit UUID conflicts with immutable input or payload")
            return saved

        new_row = InvestmentAssessment(
            id=str(assessment.id),
            revision=assessment.reference.revision,
            owner_id=owner_id,
            kind="account_fit",
            account_id=str(assessment.account_id),
            listing_id=_listing_id(assessment.listing_ref),
            as_of=assessment.assessment_as_of,
            input_hash=assessment.input_hash,
            entry_count=None,
            manifest_hash=None,
            universe_hash=None,
            payload=_json_payload(assessment),
        )
        async with self.session.begin_nested():
            self.session.add(new_row)
            await self.session.flush()
        return assessment

    async def save_rule_application(
        self, owner_id: OwnerId, application: RuleApplication
    ) -> RuleApplication:
        """Persist a strict P2 application in the shared immutable assessment table."""

        _check_owner(owner_id)
        application = RuleApplication.model_validate(application.model_dump(mode="json"))
        if application.owner_id != owner_id or application.application_sequence < 1:
            raise ValueError("rule application owner/sequence is invalid")
        if application.series_key != stable_series_key(
            owner_id, _listing_id(application.listing_ref), str(application.rule_ref.id), application.context
        ):
            raise ValueError("rule application series_key does not match its stable identity")
        if application.previous_application_ref is not None:
            previous_row = await self.session.get(
                InvestmentAssessment,
                (str(application.previous_application_ref.id), application.previous_application_ref.revision),
            )
            if previous_row is None or previous_row.owner_id != owner_id or previous_row.kind != "rule_application":
                raise ValueError("previous application must resolve to an owned rule application")
            previous = RuleApplication.model_validate(previous_row.payload)
            if previous.series_key != application.series_key or previous.application_mode != "live":
                raise ValueError("previous application must be a live predecessor from the same series")
        existing = await self.session.get(
            InvestmentAssessment, (str(application.id), application.revision)
        )
        if existing is not None:
            if existing.owner_id != owner_id or existing.kind != "rule_application":
                raise ValueError("application UUID is already used by another owner or kind")
            saved = _parse_assessment(existing)
            _check_header_columns(existing, saved)
            if not isinstance(saved, RuleApplication) or _json_payload(saved) != _json_payload(application):
                raise ValueError("application UUID conflicts with immutable input or payload")
            return saved

        new_row = InvestmentAssessment(
            id=str(application.id),
            revision=application.revision,
            owner_id=owner_id,
            kind="rule_application",
            account_id=None,
            listing_id=_listing_id(application.listing_ref),
            as_of=application.as_of,
            input_hash=application.input_hash,
            entry_count=None,
            manifest_hash=None,
            universe_hash=None,
            series_key=application.series_key,
            application_sequence=application.application_sequence,
            application_mode=application.application_mode,
            payload=_json_payload(application),
        )
        async with self.session.begin_nested():
            self.session.add(new_row)
            await self.session.flush()
        return application

    async def get_exact(
        self,
        owner_id: OwnerId,
        reference: ObjectRef,
        *,
        expected_kind: Literal["account_fit", "candidate_ranking", "rule_application"] | None = None,
    ) -> Assessment | None:
        """Read an exact UUID/revision only after checking owner and kind."""

        _check_owner(owner_id)
        kind = _kind_for_ref(reference)
        if expected_kind is not None and kind != expected_kind:
            raise ValueError(f"reference kind {kind!r} does not match {expected_kind!r}")
        if not isinstance(reference.id, UUID) or reference.revision != 1 or reference.field_path:
            raise ValueError("exact revision-1 investment assessment reference required")
        row = await self.session.get(InvestmentAssessment, (str(reference.id), reference.revision))
        if row is None or row.owner_id != owner_id or row.kind != kind:
            return None
        result = _parse_assessment(row)
        _check_header_columns(row, result)
        return result

    async def save_ranking(
        self,
        owner_id: OwnerId,
        snapshot: RankingSnapshot,
        entries: Sequence[RankingEntry],
    ) -> StoredRanking:
        """Atomically append a complete ranking header and its full entry set.

        Exact input-hash retries return the original snapshot and entries. A
        changed payload under the same hash is rejected instead of overwriting
        the earlier immutable artifact.
        """

        _check_owner(owner_id)
        _validate_ranking_bundle(snapshot, entries)
        existing = await self.session.get(
            InvestmentAssessment,
            (str(snapshot.id), snapshot.reference.revision),
        )
        if existing is not None:
            if existing.owner_id != owner_id or existing.kind != "candidate_ranking":
                raise ValueError("ranking UUID is already used by another owner or kind")
            stored = await self._read_ranking_row(existing)
            if _json_payload(stored.snapshot) != _json_payload(snapshot) or tuple(
                _json_payload(entry) for entry in stored.entries
            ) != tuple(_json_payload(entry) for entry in entries):
                raise ValueError("ranking UUID conflicts with immutable input or entries")
            return stored

        header = InvestmentAssessment(
            id=str(snapshot.id),
            revision=snapshot.reference.revision,
            owner_id=owner_id,
            kind="candidate_ranking",
            account_id=str(snapshot.account_id),
            listing_id=None,
            as_of=snapshot.as_of,
            input_hash=snapshot.input_hash,
            entry_count=snapshot.entry_count,
            manifest_hash=snapshot.manifest_hash,
            universe_hash=snapshot.universe_hash,
            payload=_json_payload(snapshot),
        )
        child_rows = [
            CandidateRankingEntryRow(
                ranking_id=str(snapshot.id),
                revision=snapshot.reference.revision,
                owner_id=owner_id,
                account_id=str(snapshot.account_id),
                listing_id=_listing_id(entry.listing_ref),
                ordinal=entry.ordinal,
                payload=_json_payload(entry),
            )
            for entry in entries
        ]
        async with self.session.begin_nested():
            self.session.add(header)
            await self.session.flush()
            self.session.add_all(child_rows)
            await self.session.flush()
        return StoredRanking(snapshot, tuple(entries))

    async def read_ranking(
        self, owner_id: OwnerId, reference: ObjectRef
    ) -> StoredRanking | None:
        """Read and revalidate a complete snapshot before exposing any page."""

        artifact = await self.get_exact(
            owner_id, reference, expected_kind="candidate_ranking"
        )
        if artifact is None:
            return None
        if not isinstance(artifact, RankingSnapshot):
            raise ValueError("candidate-ranking header has invalid payload type")
        row = await self.session.get(InvestmentAssessment, (str(reference.id), reference.revision))
        assert row is not None
        return await self._read_ranking_row(row)

    async def list_ranking_entries(
        self,
        owner_id: OwnerId,
        reference: ObjectRef,
        *,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[RankingEntry, ...] | None:
        """Return a validated page; validation always covers the full snapshot."""

        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("ranking page requires offset >= 0 and limit in 1..100")
        ranking = await self.read_ranking(owner_id, reference)
        if ranking is None:
            return None
        stmt = (
            select(CandidateRankingEntryRow)
            .where(
                CandidateRankingEntryRow.ranking_id == str(reference.id),
                CandidateRankingEntryRow.revision == reference.revision,
            )
            .order_by(CandidateRankingEntryRow.ordinal)
            .offset(offset)
            .limit(limit)
        )
        rows = list((await self.session.execute(stmt)).scalars())
        return tuple(_parse_entry(row) for row in rows)

    async def _read_ranking_row(self, row: InvestmentAssessment) -> StoredRanking:
        if row.kind != "candidate_ranking":
            raise ValueError("ranking reference resolves to a non-ranking artifact")
        snapshot = _parse_assessment(row)
        if not isinstance(snapshot, RankingSnapshot):
            raise ValueError("candidate-ranking header has invalid payload type")
        _check_header_columns(row, snapshot)
        stmt = (
            select(CandidateRankingEntryRow)
            .where(
                CandidateRankingEntryRow.ranking_id == row.id,
                CandidateRankingEntryRow.revision == row.revision,
            )
            .order_by(CandidateRankingEntryRow.ordinal)
        )
        entry_rows = list((await self.session.execute(stmt)).scalars())
        entries = tuple(_parse_entry(entry_row) for entry_row in entry_rows)
        _validate_ranking_bundle(snapshot, entries)
        if any(
            entry_row.owner_id != row.owner_id or entry_row.account_id != row.account_id
            for entry_row in entry_rows
        ):
            raise ValueError("ranking entry owner/account does not match its header")
        return StoredRanking(snapshot, entries)


def _check_owner(owner_id: OwnerId) -> None:
    if owner_id != "local_owner":
        raise ValueError("unsupported owner")


def _kind_for_ref(reference: ObjectRef) -> Literal["account_fit", "candidate_ranking", "rule_application"]:
    if reference.kind not in {"account_fit", "candidate_ranking", "rule_application"}:
        raise ValueError("unsupported investment assessment reference kind")
    return reference.kind


def _listing_id(reference: ObjectRef) -> int:
    if reference.kind != "listing" or not isinstance(reference.id, int) or reference.field_path:
        raise ValueError("exact integer listing reference required")
    return reference.id


def _json_payload(value: AccountFitAssessment | RankingSnapshot | RankingEntry | RuleApplication) -> dict[str, object]:
    return value.model_dump(mode="json")


def _parse_assessment(row: InvestmentAssessment) -> Assessment:
    if row.kind == "account_fit":
        return AccountFitAssessment.model_validate(row.payload)
    if row.kind == "candidate_ranking":
        return RankingSnapshot.model_validate(row.payload)
    if row.kind == "rule_application":
        return RuleApplication.model_validate(row.payload)
    raise ValueError(f"unsupported investment assessment kind {row.kind!r}")


def _parse_entry(row: CandidateRankingEntryRow) -> RankingEntry:
    entry = RankingEntry.model_validate(row.payload)
    if _listing_id(entry.listing_ref) != row.listing_id or entry.ordinal != row.ordinal:
        raise ValueError("ranking entry columns do not match its payload")
    if entry.ranking_ref.id != UUID(row.ranking_id) or entry.ranking_ref.revision != row.revision:
        raise ValueError("ranking entry reference does not match its parent row")
    return entry


def _check_header_columns(row: InvestmentAssessment, artifact: Assessment) -> None:
    if row.id != str(artifact.id) or row.revision != artifact.reference.revision:
        raise ValueError("assessment identity columns do not match payload")
    if row.input_hash != artifact.input_hash:
        raise ValueError("assessment input hash column does not match payload")
    if row.kind == "rule_application":
        if not isinstance(artifact, RuleApplication):
            raise ValueError("assessment kind and payload disagree")
        if (
            row.account_id is not None
            or row.listing_id != _listing_id(artifact.listing_ref)
            or not _same_timestamp(row.as_of, artifact.as_of)
            or row.series_key != artifact.series_key
            or row.application_sequence != artifact.application_sequence
            or row.application_mode != artifact.application_mode
            or any(value is not None for value in (row.entry_count, row.manifest_hash, row.universe_hash))
        ):
            raise ValueError("rule-application header columns do not match payload")
        return
    if row.kind == "account_fit":
        if not isinstance(artifact, AccountFitAssessment):
            raise ValueError("assessment kind and payload disagree")
        if row.account_id != str(artifact.account_id):
            raise ValueError("assessment account column does not match payload")
        if row.listing_id != _listing_id(artifact.listing_ref):
            raise ValueError("assessment listing column does not match payload")
        if not _same_timestamp(row.as_of, artifact.assessment_as_of) or any(
            value is not None for value in (row.entry_count, row.manifest_hash, row.universe_hash)
        ):
            raise ValueError("account-fit header columns do not match payload")
    else:
        if not isinstance(artifact, RankingSnapshot):
            raise ValueError("assessment kind and payload disagree")
        if row.account_id != str(artifact.account_id):
            raise ValueError("assessment account column does not match payload")
        if (
            row.listing_id is not None
            or not _same_timestamp(row.as_of, artifact.as_of)
            or row.entry_count != artifact.entry_count
            or row.manifest_hash != artifact.manifest_hash
            or row.universe_hash != artifact.universe_hash
        ):
            raise ValueError("ranking header columns do not match payload")


def _same_timestamp(left: datetime, right: datetime) -> bool:
    """SQLite drops timezone metadata when returning DateTime values."""

    if left.tzinfo is None or left.utcoffset() is None:
        left = left.replace(tzinfo=UTC)
    else:
        left = left.astimezone(UTC)
    if right.tzinfo is None or right.utcoffset() is None:
        right = right.replace(tzinfo=UTC)
    else:
        right = right.astimezone(UTC)
    return left == right


def _validate_ranking_bundle(
    snapshot: RankingSnapshot, entries: Sequence[RankingEntry]
) -> None:
    # Run Pydantic validation again at the persistence boundary, even if a
    # caller bypassed the standard constructor with model_construct().
    snapshot = RankingSnapshot.model_validate(snapshot.model_dump(mode="json"))
    checked_entries = tuple(RankingEntry.model_validate(entry.model_dump(mode="json")) for entry in entries)
    if snapshot.reference.kind != "candidate_ranking" or snapshot.reference.id != snapshot.id:
        raise ValueError("exact candidate-ranking reference required")
    if snapshot.entry_count != len(checked_entries):
        raise ValueError("ranking entry count does not match the complete member set")
    if tuple(entry.ordinal for entry in checked_entries) != tuple(range(len(checked_entries))):
        raise ValueError("ranking ordinals must be unique and continuous from zero")
    if any(entry.ranking_ref != snapshot.reference for entry in checked_entries):
        raise ValueError("ranking entry references must match the exact snapshot")
    entry_ids = tuple(_listing_id(entry.listing_ref) for entry in checked_entries)
    members_by_listing = {
        _listing_id(member.listing_ref): member for member in snapshot.source_manifest.members
    }
    member_ids = tuple(members_by_listing)
    if len(set(entry_ids)) != len(entry_ids) or set(entry_ids) != set(member_ids):
        raise ValueError("ranking entries must exactly cover the frozen source members")
    for entry in checked_entries:
        expected_sources = {
            source.source_ref for source in members_by_listing[_listing_id(entry.listing_ref)].sources
        }
        if set(entry.source_refs) != expected_sources:
            raise ValueError("ranking entry sources must match its frozen manifest member")
    if snapshot.manifest_hash != snapshot.source_manifest.manifest_hash:
        raise ValueError("ranking manifest hash does not match the frozen source manifest")
    if snapshot.entry_count != snapshot.source_manifest.processed_count:
        raise ValueError("ranking header count does not match the processed source manifest")
    if snapshot.coverage != snapshot.source_manifest.coverage:
        raise ValueError("ranking coverage does not match its frozen source manifest")
