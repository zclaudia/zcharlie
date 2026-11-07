"""Exact owner/account basis resolution before static inputs are frozen."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import ObjectRef, OwnerId, utc_timestamp
from investment_agent.investor.contracts import Mandate
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.contracts import Account, Snapshot
from investment_agent.portfolio.stress_canonical import canonical_hash
from investment_agent.portfolio.stress_contracts import (
    MAX_SECURITIES,
    StressStudyRequest,
    VersionedInput,
    exact,
)
from investment_agent.portfolio.valuation import Valuation
from investment_agent.storage.models.investor import MandateHead, MandateRevision
from investment_agent.storage.models.portfolio import (
    PortfolioAccount,
    PortfolioSnapshot,
    PortfolioValuation,
)


@dataclass(frozen=True)
class ResolvedStressBasis:
    account_id: UUID
    account_revision: int
    current_basis_changed: bool
    snapshot: Snapshot
    valuation: Valuation
    mandate: Mandate | None
    versions: tuple[VersionedInput, ...]


def _require(value: bool) -> None:
    if not value:
        raise InvestorError("invalid_reference", "Stress basis versions are inconsistent")


class StressInputResolver:
    def __init__(self, session: AsyncSession, owner: OwnerId = "local_owner") -> None:
        self.session, self.owner = session, owner

    async def _account(self, account_id: UUID) -> PortfolioAccount:
        row = await self.session.scalar(select(PortfolioAccount).where(
            PortfolioAccount.id == str(account_id), PortfolioAccount.owner_id == self.owner,
        ).execution_options(populate_existing=True, autoflush=False))
        if row is None:
            raise InvestorError("not_found", "Portfolio account is unavailable")
        return row

    async def _account_relation(self, actual: str, expected: UUID) -> None:
        if actual != str(expected):
            # A foreign account's payload must not be revealed by a mismatch.
            try:
                actual_id = UUID(actual)
            except ValueError as exc:
                raise InvestorError("invalid_reference", "Invalid stored account identity") from exc
            await self._account(actual_id)
            raise InvestorError("invalid_reference", "Source belongs to another account")

    async def mandate(self, account_id: UUID, ref: ObjectRef | None = None) -> Mandate | None:
        """Freeze an exact policy; absent/non-applicable current policy is explicit.

        Explicit refs are server-selected policy versions, never permission to
        rewrite a head or select a foreign account's constraints.
        """
        await self._account(account_id)
        head = await self.session.get(MandateHead, self.owner, populate_existing=True)
        if ref is None:
            if head is None:
                return None
            try:
                ref = ObjectRef(kind="mandate", id=UUID(head.mandate_id), revision=head.revision)
            except ValueError as exc:
                raise InvestorError("invalid_reference", "Stored mandate head is invalid") from exc
            explicit = False
        else:
            try:
                exact(ref, "mandate")
            except ValueError as exc:
                raise InvestorError("invalid_reference", "An exact mandate version is required") from exc
            explicit = True
        row = await self.session.get(MandateRevision, (str(ref.id), ref.revision), populate_existing=True)
        if row is None or row.owner_id != self.owner:
            raise InvestorError("not_found", "Mandate version is unavailable")
        try:
            value = Mandate.model_validate(row.payload)
        except ValidationError as exc:
            raise InvestorError("invalid_reference", "Stored mandate is invalid") from exc
        _require(head is not None and head.mandate_id == str(ref.id) and head.revision >= ref.revision
                 and value.id == ref.id and value.revision == ref.revision)
        if value.portfolio_constraints is not None and value.portfolio_constraints.account_id != account_id:
            if explicit:
                raise InvestorError("invalid_reference", "Mandate is configured for another account")
            return None
        return value

    async def resolve_basis(
        self, request: StressStudyRequest, *, mandate_ref: ObjectRef | None = None,
        now: datetime | None = None,
    ) -> ResolvedStressBasis:
        """No writes, recalculation, current-price fallback or implicit rebinding."""
        at = utc_timestamp(now or datetime.now(UTC))
        with self.session.no_autoflush:
            head = await self._account(request.account_id)
            if head.revision != request.expected_account_revision:
                raise InvestorError("revision_conflict", "Reload the current account revision")
            snapshot_row = await self.session.get(PortfolioSnapshot, str(request.snapshot_ref.id), populate_existing=True)
            valuation_row = await self.session.get(PortfolioValuation, str(request.valuation_ref.id), populate_existing=True)
            if snapshot_row is None or valuation_row is None:
                raise InvestorError("not_found", "Saved stress basis is unavailable")
            await self._account_relation(snapshot_row.account_id, request.account_id)
            await self._account_relation(valuation_row.account_id, request.account_id)
            try:
                account = Account.model_validate(head.payload)
                snapshot = Snapshot.model_validate(snapshot_row.payload)
                valuation = Valuation.model_validate(valuation_row.payload)
            except ValidationError as exc:
                raise InvestorError("invalid_reference", "Stored stress basis is invalid") from exc
            _require(account.id == request.account_id and account.revision == head.revision
                     and str(account.current_snapshot_id) == str(head.current_snapshot_id))
            _require(snapshot.id == request.snapshot_ref.id and snapshot.reference == request.snapshot_ref
                     and snapshot.revision == snapshot_row.revision == request.snapshot_ref.revision
                     and snapshot.account_id == request.account_id and snapshot.revision <= head.revision)
            _require(valuation.id == request.valuation_ref.id and valuation.reference == request.valuation_ref
                     and valuation.account_id == request.account_id
                     and valuation.snapshot_id == snapshot.id and valuation_row.snapshot_id == str(snapshot.id)
                     and valuation.ledger_revision == valuation_row.ledger_revision == snapshot.revision
                     and valuation.valuation_date == valuation_row.valuation_date
                     and valuation.algorithm_version == valuation_row.algorithm_version
                     and valuation.input_digest == valuation_row.input_digest)
            _require(snapshot.recorded_at <= at and valuation.created_at <= at
                     and valuation.knowledge_cutoff <= at)
            facts = {holding.listing_ref: holding.confirmed_quantity for holding in snapshot.holdings}
            valued = {holding.listing_ref: holding.quantity for holding in valuation.holdings}
            _require(len(facts) == len(snapshot.holdings) <= MAX_SECURITIES
                     and len(valued) == len(valuation.holdings) and facts == valued
                     and valuation.known_cash == snapshot.known_cash and valuation.coverage == snapshot.coverage)
            changed = (head.current_snapshot_id != str(snapshot.id)
                       or head.current_valuation_id != str(valuation.id))
            if request.basis_mode == "current" and (changed or head.revision != snapshot.revision):
                raise InvestorError("invalid_reference", "Current mode requires the current snapshot and valuation")
            mandate = await self.mandate(request.account_id, mandate_ref)
            versions = [VersionedInput(ref=snapshot.reference, payload_hash=canonical_hash(snapshot)),
                        VersionedInput(ref=valuation.reference, payload_hash=canonical_hash(valuation))]
            if mandate is not None:
                versions.append(VersionedInput(ref=ObjectRef(kind="mandate", id=mandate.id, revision=mandate.revision),
                                                payload_hash=canonical_hash(mandate)))
            return ResolvedStressBasis(request.account_id, head.revision, changed, snapshot, valuation, mandate, tuple(versions))
