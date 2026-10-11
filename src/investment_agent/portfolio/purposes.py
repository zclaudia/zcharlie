"""User-confirmed planning envelopes over one actual cash balance, never reservations."""
from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import Contract, DecimalValue, ObjectRef, Text
from investment_agent.investor.foundations import InvestorError, storage_conflicts
from investment_agent.portfolio.calculation_contracts import Nonnegative
from investment_agent.portfolio.service import PortfolioService, digest
from investment_agent.storage.models.portfolio_plan import (
    PortfolioPurposeHead,
    PortfolioPurposeVersion,
)


class Purpose(Contract):
    id: UUID
    name: Annotated[str, Field(min_length=1, max_length=80)]
    amount: Nonnegative
    available_budget: Nonnegative
    horizon: Literal["short_term", "long_term"]
    required_cash: Nonnegative
    required_on: date
    source: Text
    source_version: Text
    unknowns: Annotated[tuple[Text, ...], Field(max_length=20)] = ()

    @model_validator(mode="after")
    def limits(self) -> "Purpose":
        with localcontext() as ctx:
            ctx.prec = 128
            if self.available_budget + self.required_cash > self.amount:
                raise ValueError("可用预算与必留现金合计不能超过用途金额")
        return self


class PurposeRequest(Contract):
    account_id: UUID
    idempotency_key: UUID
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    snapshot_ref: ObjectRef
    purposes: Annotated[tuple[Purpose, ...], Field(max_length=12)]
    confirmation: Literal["user_confirmed"]

    @model_validator(mode="after")
    def identities(self) -> "PurposeRequest":
        if self.snapshot_ref.kind != "portfolio_snapshot" or self.snapshot_ref.field_path:
            raise ValueError("exact account snapshot required")
        if len({p.id for p in self.purposes}) != len(self.purposes):
            raise ValueError("duplicate purpose identity")
        return self


class PurposeVersion(Contract):
    reference: ObjectRef
    account_id: UUID
    snapshot_ref: ObjectRef
    confirmed_at: datetime
    purposes: tuple[Purpose, ...]
    confirmed_cash: Nonnegative
    unallocated: Nonnegative
    semantic_hash: Text
    nature: Literal["planning_allocation_not_actual_cash"] = "planning_allocation_not_actual_cash"


class PurposeView(Contract):
    version: PurposeVersion
    status: Literal["active", "needs_review", "overallocated"]
    known_cash: DecimalValue | None
    allocated: DecimalValue
    required_cash: DecimalValue
    unallocated: DecimalValue | None
    unknowns: tuple[str, ...]


def semantic(purposes: tuple[Purpose, ...]) -> str:
    return digest({"purposes": [{k: v for k, v in p.model_dump(mode="json").items() if k not in {"name", "source", "source_version"}} for p in sorted(purposes, key=lambda p: str(p.id))]})


class PurposeService:
    def __init__(self, session: AsyncSession, owner: str = "local_owner") -> None:
        self.session, self.owner = session, owner
        self.ledger = PortfolioService(session, owner)

    async def exact(self, account_id: UUID, ref: ObjectRef) -> PurposeVersion:
        await self.ledger._account(account_id)
        row = await self.session.get(PortfolioPurposeVersion, str(ref.id))
        if row is None or row.owner_id != self.owner or row.account_id != str(account_id) or ref.kind != "portfolio_purposes" or ref.field_path:
            raise InvestorError("not_found", "该账户用途版本不存在")
        value = PurposeVersion.model_validate(row.payload)
        if value.reference != ref:
            raise InvestorError("invalid_reference", "用途版本不一致")
        return value

    async def current(self, account_id: UUID) -> PurposeView | None:
        snapshot = await self.ledger.snapshot(account_id)
        head = await self.session.get(PortfolioPurposeHead, str(account_id), populate_existing=True)
        if head is None:
            return None
        version = await self.exact(account_id, ObjectRef(kind="portfolio_purposes", id=UUID(head.version_id), revision=head.revision))
        with localcontext() as ctx:
            ctx.prec = 128
            allocated = sum((p.amount for p in version.purposes), Decimal(0))
            required = sum((p.required_cash for p in version.purposes), Decimal(0))
            remaining = snapshot.known_cash - allocated if snapshot.known_cash is not None else None
        unknowns = tuple(sorted({u for p in version.purposes for u in p.unknowns} | ({"known_cash_missing"} if snapshot.known_cash is None else set())))
        state: Literal["active", "needs_review", "overallocated"] = "overallocated" if remaining is not None and remaining < 0 else "needs_review" if unknowns or snapshot.reference != version.snapshot_ref else "active"
        return PurposeView(version=version, status=state, known_cash=snapshot.known_cash, allocated=allocated, required_cash=required, unallocated=remaining, unknowns=unknowns)

    @storage_conflicts
    async def confirm(self, request: PurposeRequest, *, enabled: bool) -> PurposeVersion:
        await self.ledger._reserve()
        await self.ledger._account(request.account_id)
        payload = request.model_dump(mode="json")
        prior = await self.session.get(PortfolioPurposeVersion, str(request.idempotency_key))
        if prior is not None:
            if prior.owner_id != self.owner:
                raise InvestorError("not_found", "用途收据不存在")
            if prior.input_hash != digest(payload):
                raise InvestorError("idempotency_conflict", "用途请求标识已用于不同内容")
            return PurposeVersion.model_validate(prior.payload)
        if not enabled:
            raise InvestorError("writes_disabled", "用途确认尚未启用")
        snapshot = await self.ledger.snapshot(request.account_id)
        head = await self.session.get(PortfolioPurposeHead, str(request.account_id), populate_existing=True)
        revision = head.revision if head else 0
        if revision != request.expected_revision or snapshot.reference != request.snapshot_ref:
            raise InvestorError("revision_conflict", "现金或用途版本已变化，请刷新后重新确认")
        with localcontext() as ctx:
            ctx.prec = 128
            allocated = sum((p.amount for p in request.purposes), Decimal(0))
            if snapshot.known_cash is None or allocated > snapshot.known_cash:
                raise InvestorError("invalid_reference", "用途分配须由该账户已确认现金覆盖；未知现金不能分配")
            remaining = snapshot.known_cash - allocated
        value = PurposeVersion(reference=ObjectRef(kind="portfolio_purposes", id=request.idempotency_key, revision=revision + 1), account_id=request.account_id, snapshot_ref=snapshot.reference, confirmed_at=datetime.now(UTC), purposes=request.purposes, confirmed_cash=snapshot.known_cash, unallocated=remaining, semantic_hash=semantic(request.purposes))
        self.session.add(PortfolioPurposeVersion(id=str(request.idempotency_key), account_id=str(request.account_id), owner_id=self.owner, input_hash=digest(payload), payload=value.model_dump(mode="json")))
        if head is None:
            self.session.add(PortfolioPurposeHead(account_id=str(request.account_id), revision=revision + 1, version_id=str(request.idempotency_key)))
        else:
            head.revision, head.version_id = revision + 1, str(request.idempotency_key)
        await self.session.flush()
        return value


def check_scenarios(details: dict[str, Any], version: PurposeVersion) -> None:
    """Independent options consume the same envelopes; fee-inclusive actual outlay."""
    with localcontext() as ctx:
        ctx.prec = 128
        reserve = sum((p.required_cash for p in version.purposes), Decimal(0))
        allocated = sum((p.amount for p in version.purposes), Decimal(0))
        by_id = {str(p.id): p for p in version.purposes}
        request = details["request"]
        for index, result in enumerate(details["scenarios"]):
            spent: dict[str, Decimal | None] = {key: Decimal(0) for key in by_id}
            errors, unknowns = set(result["violations"]), set(result["unknowns"])
            cash = Decimal(result["cash"]) if result["cash"] is not None else None
            original_cash = details["snapshot"]["known_cash"]
            if original_cash is None:
                unknowns.add("purpose_cash_unknown")
            elif allocated > Decimal(original_cash):
                errors.add("purpose_overallocated")
            if cash is None:
                unknowns.add("purpose_reserve_unknown")
            elif cash < reserve:
                errors.add("purpose_required_cash_shortfall")
            inputs = request["scenarios"][index - 1]["actions"] if index else ()
            for action, output in zip(inputs, result["actions"], strict=True):
                if not by_id:
                    continue
                key = action.get("purpose_id")
                if key not in by_id:
                    errors.add("purpose_association_required")
                    continue
                if output["gross"] is None or output["fee"] is None:
                    unknowns.add("purpose_outlay_unknown")
                    spent[key] = None
                else:
                    prior_spent = spent[key]
                    if prior_spent is not None:
                        gross, fee = Decimal(output["gross"]), Decimal(output["fee"])
                        outlay = gross + fee if action["direction"] == "buy" else max(Decimal(0), fee - gross)
                        spent[key] = prior_spent + outlay
            for key, amount in spent.items():
                if amount is not None and amount > by_id[key].available_budget:
                    errors.add(f"purpose_budget_exceeded:{key}")
                unknowns.update(by_id[key].unknowns)
            result["purpose_checks"] = {"purpose_ref": version.reference.model_dump(mode="json"), "required_cash": str(reserve), "remaining_cash": result["cash"], "reserve_shortfall": str(max(Decimal(0), reserve - cash)) if cash is not None else None, "purposes": [{"id": key, "name": by_id[key].name, "spent": str(value) if value is not None else None, "remaining_budget": str(by_id[key].available_budget - value) if value is not None else None} for key, value in spent.items()]}
            result.update(violations=sorted(errors), unknowns=sorted(unknowns), publication_ready=not errors and not unknowns, status="violated" if errors else "needs_data" if unknowns else "ok")
