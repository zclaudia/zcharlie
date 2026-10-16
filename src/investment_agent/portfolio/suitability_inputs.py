"""Read-only materialization of account-fit evidence at two explicit times.

Admission reserves the SQLite writer before calling this module. Reads never
ensure a listing, update a valuation, or enqueue work.
"""

from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.investor.common import Contract, ObjectRef
from investment_agent.investor.contracts import Mandate
from investment_agent.investor.foundations import InvestorError
from investment_agent.portfolio.calculations import TradingRuleResolver, UnavailableTradingRules
from investment_agent.portfolio.contracts import Account, Snapshot
from investment_agent.portfolio.prices import latest_prices_as_of
from investment_agent.portfolio.purposes import PurposeVersion
from investment_agent.portfolio.service import PortfolioService, digest
from investment_agent.portfolio.suitability_contracts import (
    AccountAssessmentRequest,
    AccountFitRequest,
    FitRequestItem,
    FrozenAccountInputs,
    FrozenListingSource,
    FrozenPrice,
    FrozenSector,
    PurposeBinding,
    ValuationBinding,
)
from investment_agent.portfolio.valuation import Valuation, ValuationService, trading_days_after
from investment_agent.storage.models.investor import MandateHead, MandateRevision
from investment_agent.storage.models.pit_snapshot import IndustryClassification, MarketSnapshot
from investment_agent.storage.models.portfolio_plan import (
    PortfolioPurposeHead,
    PortfolioPurposeVersion,
)
from investment_agent.storage.models.reference import Company, Listing, Market

MAX_ACCOUNT_HOLDINGS = 500


def account_fit_calendar() -> Callable[[date], bool | None] | None:
    from pathlib import Path

    from investment_agent.scheduler.trading_calendar import (
        is_trading_day_or_none,
        load_trading_calendar,
    )
    from investment_agent.settings.loader import calendar_path

    try:
        calendar = load_trading_calendar(Path(calendar_path()))
    except (OSError, ValueError):
        return None
    return lambda day: is_trading_day_or_none(calendar, day)


def utc(value: datetime) -> datetime:
    """SQLite stores UTC timestamps without an offset."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class FitContext(Contract):
    account: Account
    snapshot: Snapshot | None
    valuation: Valuation | None
    valuation_binding: ValuationBinding
    mandate: Mandate | None
    mandate_ref: ObjectRef | None
    purpose_version: PurposeVersion | None
    purpose_binding: PurposeBinding
    gaps: tuple[str, ...]


def holding_quantity(snapshot: Snapshot, ref: ObjectRef) -> Decimal | None:
    holding = next((h for h in snapshot.holdings if h.listing_ref == ref), None)
    if holding is not None:
        return holding.confirmed_quantity
    return Decimal(0) if snapshot.coverage == "complete" else None


def resolve_buy_action(snapshot: Snapshot, ref: ObjectRef) -> str | None:
    quantity = holding_quantity(snapshot, ref)
    return None if quantity is None else "add" if quantity > 0 else "open"


class SuitabilityInputService:
    def __init__(self, session: AsyncSession, owner: str = "local_owner", rules: TradingRuleResolver | None = None) -> None:
        self.session, self.owner = session, owner
        self.ledger = PortfolioService(session, owner)
        self.rules = rules or UnavailableTradingRules()
        self.calendar = account_fit_calendar()

    async def context(self, account_id: UUID, *, as_of: datetime | None = None) -> FitContext:
        at = as_of or datetime.now(UTC)
        row = await self.ledger._account(account_id)
        snapshot = await self.ledger.snapshot(account_id) if row.current_snapshot_id else None
        valuation = await ValuationService(self.session, self.owner).current(account_id)
        if valuation is not None and (snapshot is None or valuation.snapshot_id != snapshot.id or valuation.ledger_revision != snapshot.revision):
            valuation = None
        valuation_gap = "valuation_missing"
        if valuation is not None and (valuation.knowledge_cutoff > at or valuation.created_at > at):
            # Existing scheduled valuations use a deterministic future day-end
            # cutoff. Keep their identity intact, but do not consume that basis
            # before it is eligible for this assessment.
            valuation = None
            valuation_gap = "valuation_knowledge_in_future"
        binding = ValuationBinding(state="available", valuation_ref=valuation.reference) if valuation else ValuationBinding(state="missing", reason=valuation_gap)
        head = await self.session.get(MandateHead, self.owner, populate_existing=True)
        mandate = None
        if head is not None:
            revision = await self.session.get(MandateRevision, (head.mandate_id, head.revision))
            if revision is None or revision.owner_id != self.owner:
                raise RuntimeError("mandate head has no authorized revision")
            mandate = Mandate.model_validate(revision.payload)
        purpose_head = await self.session.get(PortfolioPurposeHead, str(account_id), populate_existing=True)
        purpose = None
        if purpose_head is not None:
            version = await self.session.get(PortfolioPurposeVersion, purpose_head.version_id)
            if version is None or version.owner_id != self.owner or version.account_id != str(account_id):
                raise RuntimeError("purpose head has no authorized version")
            purpose = PurposeVersion.model_validate(version.payload)
        purpose_binding = PurposeBinding(state="none_recorded") if purpose is None else PurposeBinding(state="unselected") if purpose.purposes else PurposeBinding(state="confirmed_empty", purpose_ref=purpose.reference, confirmed_at=purpose.confirmed_at)
        gaps = tuple(reason for missing, reason in ((snapshot is None, "snapshot_missing"), (valuation is None, "valuation_missing"), (mandate is None, "mandate_missing")) if missing)
        if valuation is None and valuation_gap != "valuation_missing":
            gaps += (valuation_gap,)
        if mandate is not None and (mandate.portfolio_constraints is None or mandate.portfolio_constraints.account_id != account_id):
            gaps += ("account_constraints_not_configured",)
        return FitContext(account=Account.model_validate(row.payload), snapshot=snapshot, valuation=valuation, valuation_binding=binding, mandate=mandate, mandate_ref=ObjectRef(kind="mandate", id=mandate.id, revision=mandate.revision) if mandate else None, purpose_version=purpose, purpose_binding=purpose_binding, gaps=gaps)

    async def freeze(self, request: AccountAssessmentRequest, *, as_of: datetime, extra_listings: tuple[ObjectRef, ...] = (), validate_actions: bool = True) -> FrozenAccountInputs:
        if request.mode != "live":
            raise InvestorError("historical_manifest_unavailable", "历史回放必须由接收服务读取已有冻结清单")
        context = await self.context(request.account_id, as_of=as_of)
        snapshot = context.snapshot
        if snapshot is None:
            raise InvestorError("snapshot_required", "请先确认账户起始事实")
        if len(snapshot.holdings) > MAX_ACCOUNT_HOLDINGS:
            raise InvestorError("account_capacity_exceeded", "账户持仓事实超过安全容量，不能截断评估")
        if snapshot.reference != request.snapshot_ref or context.mandate_ref != request.mandate_ref:
            raise InvestorError("revision_conflict", "账户事实或约定已变化，请刷新")
        if (context.valuation_binding.state, context.valuation_binding.valuation_ref) != (request.valuation_binding.state, request.valuation_binding.valuation_ref):
            raise InvestorError("revision_conflict", "估值绑定已变化，请刷新")
        if context.mandate is None:
            raise InvestorError("invalid_reference", "账户检查需要已保存的约定")
        purpose = context.purpose_version
        binding = request.purpose_binding
        if binding.state in {"selected", "confirmed_empty"}:
            try:
                binding.validate_version(purpose)
            except ValueError as exc:
                raise InvestorError("revision_conflict", "用途版本已变化，请刷新") from exc
        elif binding.state == "none_recorded" and purpose is not None:
            raise InvestorError("revision_conflict", "该账户已有用途版本，请重新确认绑定")
        elif binding.state == "unselected" and (purpose is None or not purpose.purposes):
            raise InvestorError("revision_conflict", "用途选择状态已变化，请刷新")
        if snapshot.recorded_at > as_of or context.mandate.created_at > as_of or (purpose is not None and purpose.confirmed_at > as_of):
            raise InvestorError("invalid_reference", "来源晚于评估截止时间")
        if context.valuation is not None and (context.valuation.created_at > as_of or context.valuation.knowledge_cutoff > as_of):
            raise InvestorError("invalid_reference", "估值资料晚于评估截止时间")
        items = request.items if isinstance(request, AccountFitRequest) else ()
        if validate_actions:
            for item in items:
                self.validate_action(snapshot, item)
        refs = {h.listing_ref for h in snapshot.holdings} | {item.listing_ref for item in items} | set(extra_listings)
        rule_refs = {item.listing_ref for item in items if item.action != "hold"} | set(extra_listings)
        sources = await self.listing_sources(tuple(sorted(refs, key=lambda ref: int(ref.id))), valuation=context.valuation, as_of=as_of, rule_refs=rule_refs, holding_refs={h.listing_ref for h in snapshot.holdings})
        values: dict[str, Any] = dict(account_id=request.account_id, snapshot=snapshot, valuation=context.valuation, valuation_binding=context.valuation_binding, mandate=context.mandate, purpose_version=purpose, purpose_binding=binding, listing_sources=sources, assessment_as_of=as_of, price_basis_at=context.valuation.knowledge_cutoff if context.valuation else None, mode="live", manifest_hash="0" * 64, source_fingerprint="0" * 64)
        frozen = FrozenAccountInputs(**values)
        fingerprint = digest(frozen.model_dump(mode="json", exclude={"manifest_hash", "source_fingerprint"}))
        return frozen.model_copy(update={"manifest_hash": fingerprint, "source_fingerprint": fingerprint})

    @staticmethod
    def validate_action(snapshot: Snapshot, item: FitRequestItem) -> None:
        quantity = holding_quantity(snapshot, item.listing_ref)
        if quantity is None:
            raise InvestorError("holding_presence_unknown", "该标的持仓状态未确认，不能指定建仓、加仓或持有")
        if (item.action == "open" and quantity != 0) or (item.action in {"add", "hold"} and quantity <= 0):
            raise InvestorError("action_holding_mismatch", "动作与已确认持仓不一致")

    async def listing_sources(self, refs: tuple[ObjectRef, ...], *, valuation: Valuation | None, as_of: datetime, rule_refs: set[ObjectRef] | None = None, holding_refs: set[ObjectRef] | None = None) -> tuple[FrozenListingSource, ...]:
        ids = [int(ref.id) for ref in refs]
        business_date = as_of.astimezone(SHANGHAI_TZ).date()
        rows = (await self.session.execute(select(Listing, Company.canonical_name, Market.currency).join(Company, Company.id == Listing.company_id).join(Market, Market.code == Listing.market_code).where(Listing.id.in_(ids)))).all()
        listings = {row[0].id: row for row in rows}
        if len(listings) != len(ids) or any(row[0].market_code not in {"CN", "SH", "SZ", "BJ", "SSE", "SZSE", "BSE"} or row[2] != "CNY" for row in rows):
            raise InvestorError("invalid_reference", "标的不在已保存人民币A股范围内")
        prices = await latest_prices_as_of(self.session, ids, trade_date=valuation.valuation_date, knowledge_cutoff=valuation.knowledge_cutoff) if valuation else {}
        valuation_prices = {int(h.listing_ref.id): h for h in valuation.holdings} if valuation else {}
        selected_ids = {p.id for p in prices.values()}
        selected_ids.update(int(h.price_ref.id) for h in valuation_prices.values() if h.price_ref is not None)
        price_rows = {row.id: row for row in await self.session.scalars(select(MarketSnapshot).where(MarketSnapshot.id.in_(selected_ids)))}
        sectors: dict[int, list[IndustryClassification]] = defaultdict(list)
        for row in await self.session.scalars(select(IndustryClassification).where(IndustryClassification.listing_id.in_(ids), IndustryClassification.system == "CSRC", IndustryClassification.valid_from <= business_date, IndustryClassification.valid_to.is_(None) | (IndustryClassification.valid_to > business_date), IndustryClassification.captured_at <= as_of)):
            sectors[row.listing_id].append(row)
        result = []
        for ref in refs:
            listing, name, _ = listings[int(ref.id)]
            basis = valuation_prices.get(int(ref.id))
            selected = prices.get(int(ref.id))
            price_id = int(basis.price_ref.id) if basis and basis.price_ref else selected.id if basis is None and selected else None
            if holding_refs is not None and ref in holding_refs and basis is None:
                price_id = None  # a missing valuation holding is never backfilled
            price = price_rows.get(price_id) if price_id else None
            if price is not None and (price.listing_id != ref.id or price.adjust != "none" or price.close <= 0 or valuation is None or utc(price.captured_at) > valuation.knowledge_cutoff or price.trade_date > valuation.valuation_date or (basis is not None and (basis.price_ref is None or price.version != basis.price_ref.revision or price.close != basis.price))):
                raise InvestorError("invalid_reference", "冻结估值价格与原始未复权来源不一致")
            price_reason = None if price else "valuation_price_missing"
            if price is not None:
                days = trading_days_after(price.trade_date, business_date, self.calendar)
                if self.calendar is None or days is None:
                    price_reason = "price_calendar_unavailable"
                elif days > 5:
                    price_reason = "price_stale"
            frozen_price = FrozenPrice(listing_ref=ref, price=price.close if price else None, price_ref=ObjectRef(kind="market_snapshot", id=price.id, revision=price.version) if price else None, price_date=price.trade_date if price else None, provider=price.provider if price else None, captured_at=utc(price.captured_at) if price else None, available_at=utc(price.captured_at) if price else None, acquired_at=as_of, unknown_reason=price_reason)
            classified = sectors[int(ref.id)]
            sector = classified[0] if len(classified) == 1 else None
            frozen_sector = FrozenSector(listing_ref=ref, row_id=sector.id if sector else None, code=sector.code if sector else None, name=sector.name if sector else None, valid_from=sector.valid_from if sector else None, valid_to=sector.valid_to if sector else None, source=sector.source if sector else None, captured_at=utc(sector.captured_at) if sector else None, available_at=utc(sector.captured_at) if sector else None, acquired_at=as_of, unknown_reason=None if sector else "csrc_classification_missing")
            rule_applicable = rule_refs is None or ref in rule_refs
            trusted = await self.rules.resolve(ref, as_of) if rule_applicable else None
            if trusted is not None and (trusted.rule.listing_ref != ref or trusted.market_code != listing.market_code or trusted.available_from > as_of or trusted.valid_from > business_date or (trusted.valid_to is not None and trusted.valid_to <= business_date)):
                raise InvestorError("invalid_reference", "交易规则身份或有效区间不一致")
            result.append(FrozenListingSource(listing_ref=ref, market_code=listing.market_code, name=name, ticker=listing.ticker, listing_source_ref=ref, price=frozen_price, sector=frozen_sector, trusted_rule=trusted, rule_unknown_reason=None if trusted else "trading_rule_unavailable" if rule_applicable else "not_applicable"))
        return tuple(result)
