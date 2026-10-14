"""Owner/PIT-scoped valuation and immutable calculation receipts. Caller owns commit."""

from datetime import UTC, date, datetime
from decimal import Decimal, localcontext
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.evidence.contracts import CalculationAssumption, CalculationRecord
from investment_agent.evidence.snapshots import digest
from investment_agent.evidence.store import EvidenceService
from investment_agent.factors.library import STALE_PRICE_DAYS
from investment_agent.factors.snapshot_model import Bar, FinancialPeriod, SnapshotView
from investment_agent.factors.valuation import VALUATION_FACTORS, years_before
from investment_agent.investor.common import ObjectRef, OwnerId, Quantity
from investment_agent.investor.enums import CalculationStatus as Status
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.pit_snapshot import FinancialSnapshot
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.market_snapshot import MarketSnapshotRepository
from investment_agent.storage.repositories.report_overdue import ReportOverdueRepository
from investment_agent.valuation.contracts import (
    PercentileRequest,
    QualificationResolver,
    ReverseDCFRequest,
    UnavailableQualification,
)
from investment_agent.valuation.solver import METHOD_VERSION, solve

# Explicit units, no currency inference or float conversion for cash flows.
MONEY_UNITS = {"CNY": Decimal(1), "元": Decimal(1),
               "CNY/10000": Decimal(10000), "万元": Decimal(10000),
               "CNY/100000000": Decimal(100000000), "亿元": Decimal(100000000)}
WARNING = "Historical FCFE is a scenario annual base; equal annual periods from as_of, no stub period."


class ValuationService:
    def __init__(self, session: AsyncSession,
                 qualification: QualificationResolver | None = None) -> None:
        self.session = session
        self.store = EvidenceService(session)
        self.qualification = qualification or UnavailableQualification()

    async def scope(self, owner: OwnerId, request: ReverseDCFRequest) -> tuple[ObjectRef, ...]:
        """Resolve actual visible subjects; never accept a claimed ticker or subject."""
        subjects = []
        assumption_sources = tuple(a.source_ref for a in
            (request.main_rate, request.terminal_growth, *request.sensitivity_rates)
            if a is not None and a.source_ref is not None)
        for ref in (request.equity_value_ref, *request.annual_fcfe_refs, *assumption_sources):
            found = await self.store.get_evidence(owner, ref, request.as_of)
            if found.data is None:
                raise InvestorError("invalid_reference", "Valuation input is not visible")
            if found.data.subject_ref not in subjects:
                subjects.append(found.data.subject_ref)
        return tuple(subjects)

    async def _save(self, owner: OwnerId, operation: UUID, method: str, version: str,
                    as_of: datetime, request: object, facts: tuple[ObjectRef, ...],
                    assumptions: tuple[CalculationAssumption, ...], status: Status,
                    results: dict[str, Quantity], quality: tuple[str, ...], errors: tuple[str, ...],
                    details: dict[str, Any]) -> dict[str, Any]:
        record = CalculationRecord(
            id=uuid5(NAMESPACE_URL, f"valuation:{operation}"), revision=1,
            created_at=as_of, actor="valuation_service", origin_ref=None,
            method=method, method_version=version, fact_refs=facts,
            assumption_refs=tuple(dict.fromkeys(a.source_ref for a in assumptions
                                               if a.source_ref is not None)),
            input_hash=digest({"request": request, "method_version": version,
                               "inputs": details.get("inputs"),
                               "qualification": details.get("qualification"),
                               "financial_inputs": details.get("financial_inputs"),
                               "report_context": details.get("report_context"),
                               "policy": details.get("policy")}),
            assumptions=assumptions, as_of=as_of, applicable=status == Status.OK,
            status=status, results=results if status == Status.OK else {},
            quality=quality, errors=errors, details=details)
        async with self.session.begin_nested():
            ref = await self.store.save_calculation(owner, record, uuid5(operation, "calculation"))
            self.store.receipts._receipt(owner, operation,
                {"operation": "valuation", "version": version, "request": request}, ref)
            await self.session.flush()
        return await self._response(owner, ref, as_of)

    async def _response(self, owner: OwnerId, ref: ObjectRef, as_of: datetime) -> dict[str, Any]:
        saved = await self.store.get_calculation(owner, ref, max(as_of, datetime.now(UTC)))
        assert saved.data is not None
        record = saved.data
        return {"status": record.status.value, "method_version": record.method_version,
                "calculation_ref": ref.model_dump(mode="json"),
                "calculation": record.model_dump(mode="json"),
                "result": {k: v.model_dump(mode="json") for k, v in record.results.items()}
                if record.status == Status.OK else None}

    async def _replay(self, owner: OwnerId, operation: UUID, version: str,
                      request: ReverseDCFRequest | PercentileRequest) -> dict[str, Any] | None:
        replay = await self.store.receipts._replay(owner, operation,
            {"operation": "valuation", "version": version, "request": request.model_dump(mode="json")})
        if replay is not None:
            return await self._response(owner, ObjectRef.model_validate(replay), request.as_of)
        return None

    async def _assumptions(self, owner: OwnerId, request: ReverseDCFRequest
                           ) -> tuple[tuple[CalculationAssumption, ...], tuple[str, ...]]:
        errors, accepted = [], []
        for name, item in [("discount_rate", request.main_rate),
                           ("terminal_growth", request.terminal_growth),
                           *(("discount_rate", a) for a in request.sensitivity_rates)]:
            if item is None:
                errors.append(f"missing_{name}")
                continue
            valid = (item.name == name and item.value.value is not None
                     and item.value.unit == "fraction" and item.value.currency is None
                     and bool(item.rationale and item.rationale.strip()))
            if item.source_ref is not None:
                source = await self.store.get_evidence(owner, item.source_ref, request.as_of)
                valid = valid and source.data is not None
                if source.data is not None:
                    valid = valid and (item.provenance != "user_scenario"
                                       or source.data.verification == "user_report")
            elif item.provenance == "user_scenario":
                valid = False
            if valid:
                accepted.append(item)
            else:
                errors.append(f"unsubstantiated_{name}")
        if request.main_rate and request.main_rate.value.value is not None:
            main = request.main_rate.value.value
            rates = [a.value.value for a in request.sensitivity_rates if a.value.value is not None]
            if not any(r < main for r in rates) or not any(r > main for r in rates):
                errors.append("sensitivity_requires_lower_and_higher_rate")
        return tuple(accepted), tuple(errors)

    async def _source_conflict(self, record: Any, as_of: datetime) -> bool:
        """Keep the same cross-source visibility gate as EvidenceService.save_calculation."""
        if record.source_ref.kind not in {"financial_snapshot", "market_snapshot"}:
            return False
        from investment_agent.domain.a_share import format_listing_ticker
        from investment_agent.evidence.queries import SnapshotQueryService
        from investment_agent.factors.field_resolution import PROVIDER_FIELD_RENAMES
        from investment_agent.storage.models.reference import Listing
        listing = await self.session.get(Listing, record.subject_ref.id)
        if listing is None or record.period is None:
            return True
        field = record.source_ref.field_path or ""
        if record.source_ref.kind == "financial_snapshot":
            row = await self.session.get(FinancialSnapshot, record.source_ref.id)
            if row is None:
                return True
            stored = field.removeprefix("fields.")
            field = PROVIDER_FIELD_RENAMES[row.provider].get(stored, stored)
        result = await SnapshotQueryService(self.session).get_snapshot(
            format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code),
            field, as_of, report_period=record.period.end)
        return result.status == "conflict"

    async def reverse_dcf(self, owner: OwnerId, request: ReverseDCFRequest,
                          operation: UUID) -> dict[str, Any]:
        await self.ensure_consistent_read()
        replay = await self._replay(owner, operation, METHOD_VERSION, request)
        if replay is not None:
            return replay
        details: dict[str, Any] = {"inputs": [], "requested": request.model_dump(mode="json"),
                                   "scenarios": [], "warnings": [WARNING], "horizon_years": 5,
                                   "equity_valuation": "ordinary_equity; no net debt deduction or cash addition"}
        errors: list[str] = []
        facts, records, quality = [], [], []
        status = Status.OK
        for ref in (request.equity_value_ref, *request.annual_fcfe_refs):
            result = await self.store.get_evidence(owner, ref, request.as_of)
            if result.data is not None:
                details["inputs"].append({"ref": ref.model_dump(mode="json"),
                                           "evidence": result.data.model_dump(mode="json")})
            conflict = result.data is not None and await self._source_conflict(result.data, request.as_of)
            if (result.status != "ok" or result.data is None
                or result.data.verification != "verified" or conflict):
                reason = "cross_source_conflict" if conflict else result.status
                errors.append(f"unusable_evidence:{ref.id}:{reason}")
                status = Status.MISSING_INPUTS
                quality.append(f"{ref.id}:cross_source_conflict" if conflict else
                               f"{ref.id}:{result.data.verification if result.data else 'missing'}")
                # Do not put rejected/unknown/user-reported material in fact_refs.
                continue
            facts.append(ref)
            records.append(result.data)
            quality.append(f"{ref.id}:verified")
        assumptions, assumption_errors = await self._assumptions(owner, request)
        errors.extend(assumption_errors)
        if assumption_errors and status == Status.OK:
            status = Status.MISSING_ASSUMPTIONS
        qualified = await self.qualification.resolve(self.session, owner, request, tuple(records))
        if qualified is None:
            errors.append("fcfe_source_mapping_unavailable")
            if status == Status.OK:
                status = Status.MISSING_INPUTS
        else:
            from dataclasses import asdict
            details["qualification"] = {
                **{k: v for k, v in asdict(qualified).items()
                   if k not in {"equity_value_ref", "annual_fcfe_refs"}},
                "equity_value_ref": qualified.equity_value_ref.model_dump(mode="json"),
                "annual_fcfe_refs": [r.model_dump(mode="json") for r in qualified.annual_fcfe_refs]}
            if (qualified.equity_value_ref != request.equity_value_ref
                or qualified.annual_fcfe_refs != request.annual_fcfe_refs):
                errors.append("qualification_reference_mismatch")
                status = Status.NOT_APPLICABLE
            checks = ("ordinary_equity", "non_financial", "same_currency", "consistent_scope",
                      "interest_classification_checked", "no_overlapping_financing",
                      "no_unhandled_material_changes", "non_operating_assets_consistent")
            for check in checks:
                if not getattr(qualified, check):
                    errors.append(check)
                    status = Status.NOT_APPLICABLE
            for check in ("known_net_borrowing", "known_cash_interest"):
                if not getattr(qualified, check):
                    errors.append(check)
                    status = Status.MISSING_INPUTS
            errors.extend(qualified.reasons)
            if qualified.reasons and status == Status.OK:
                status = Status.NOT_APPLICABLE
            if not qualified.derivation_version or not qualified.accounting_basis:
                errors.append("missing_derivation_or_accounting_basis")
                status = Status.MISSING_INPUTS
            if qualified.report_overdue or qualified.cashflow_superseded:
                errors.append("report_overdue" if qualified.report_overdue else "cashflow_superseded")
                status = Status.STALE_INPUTS
        amounts: list[Decimal] = []
        annual_amounts: list[Decimal] = []
        subjects = {r.subject_ref for r in records}
        if len(subjects) > 1:
            errors.append("mixed_subjects")
            status = Status.NOT_APPLICABLE
        for record in records:
            value = record.value
            if (value is None or value.value is None or value.unit not in MONEY_UNITS
                or value.currency != "CNY"):
                errors.append("unknown_or_inconsistent_money_unit")
                status = Status.NOT_APPLICABLE
            else:
                with localcontext() as ctx:
                    ctx.prec = 40
                    amounts.append(value.value * MONEY_UNITS[value.unit])
        day = request.as_of.astimezone(SHANGHAI_TZ).date()
        if qualified is not None:
            required_year = day.year - (1 if day > date(day.year, 4, 30) else 2)
            latest_available = await self.session.scalar(select(FinancialSnapshot.report_period)
                .where(FinancialSnapshot.listing_id.in_([int(s.id) for s in subjects]),
                       FinancialSnapshot.captured_at <= request.as_of,
                       FinancialSnapshot.available_from <= day,
                       FinancialSnapshot.availability != "unknown",
                       FinancialSnapshot.period_kind == "annual",
                       FinancialSnapshot.report_period < day)
                .order_by(FinancialSnapshot.report_period.desc()).limit(1))
            if latest_available is not None:
                required_year = max(required_year, latest_available.year)
            if qualified.latest_complete_year < required_year:
                errors.append("annual_base_outdated")
                status = Status.STALE_INPUTS
        if len(records) == 1 + len(request.annual_fcfe_refs):
            market, annual = records[0], records[1:]
            if market.period is None or market.period.start != market.period.end:
                errors.append("equity_value_requires_price_date")
                status = Status.NOT_APPLICABLE
            elif (day - market.period.end).days > STALE_PRICE_DAYS:
                errors.append("stale_price")
                status = Status.STALE_INPUTS
            if market.period and market.period.end > day:
                errors.append("future_price_period")
                status = Status.MISSING_INPUTS
            years = [r.period.end.year for r in annual if r.period]
            if (len(annual) != 3 or len(set(years)) != 3 or qualified is None
                or sorted(years) != list(range(qualified.latest_complete_year - 2,
                                               qualified.latest_complete_year + 1))):
                errors.append("requires_three_recent_complete_years")
                if status == Status.OK:
                    status = Status.MISSING_INPUTS
            for record in annual:
                period = record.period
                if (period is None or period.start != date(period.end.year, 1, 1)
                    or period.end != date(period.end.year, 12, 31) or period.end >= day):
                    errors.append("not_complete_annual_cashflow")
                    status = Status.NOT_APPLICABLE
            if len({r.period.basis for r in annual if r.period}) > 1:
                errors.append("inconsistent_annual_basis")
                status = Status.NOT_APPLICABLE
            details["report_age_days"] = [(day - r.period.end).days for r in annual if r.period]
            if annual and all(r.period for r in annual):
                annual = sorted(annual, key=lambda r: r.period.end if r.period else date.min)
                # Amounts follow request order; reorder alongside the evidence.
                annual_amounts = [amounts[records.index(r)] for r in annual] if len(amounts) == len(records) else []
                if annual_amounts:
                    if min(annual_amounts) <= 0:
                        errors.append("non_positive_fcfe")
                        status = Status.NOT_APPLICABLE
                    elif max(annual_amounts) > min(annual_amounts) * 3:
                        errors.append("fcfe_max_min_exceeds_three")
                        status = Status.NOT_APPLICABLE
        else:
            annual_amounts = []
        if len(amounts) == len(records) and amounts and amounts[0] <= 0:
            errors.append("non_positive_equity_value")
            status = Status.NOT_APPLICABLE
        # Server overdue records cannot be overridden by resolver claims.
        if len(subjects) == 1:
            subject = next(iter(subjects))
            overdue = await ReportOverdueRepository(self.session).open_as_of(int(subject.id), day)
            details["report_context"] = {"overdue_periods": [r.report_period.isoformat() for r in overdue]}
            if overdue:
                errors.append("report_overdue")
                status = Status.STALE_INPUTS
        results: dict[str, Quantity] = {}
        if status == Status.OK:
            bases = {"median": sorted(annual_amounts)[1], "latest": annual_amounts[-1]}
            assert request.main_rate is not None and request.terminal_growth is not None
            terminal = request.terminal_growth.value.value
            assert terminal is not None
            rates = (request.main_rate, *request.sensitivity_rates)
            for basis, base in bases.items():
                for index, assumption in enumerate(rates):
                    rate = assumption.value.value
                    assert rate is not None
                    solution = solve(amounts[0], base, rate, terminal)
                    cell = {"basis": basis, "base": str(base), "currency": "CNY",
                            "rate": str(rate), "terminal_growth": str(terminal),
                            **solution.model_dump(mode="json")}
                    details["scenarios"].append(cell)
                    if index == 0 and basis == "median":
                        status = solution.status
                        if solution.reason:
                            errors.append(solution.reason)
                    if solution.status == Status.OK:
                        assert solution.growth is not None and solution.terminal_pv_fraction is not None
                        results[f"{basis}_{index}_growth"] = Quantity(value=solution.growth, unit="fraction")
                        results[f"{basis}_{index}_terminal_pv_fraction"] = Quantity(
                            value=solution.terminal_pv_fraction, unit="fraction")
            details["input_quality"] = "verified_historical_inputs; future_cashflows_and_rates_are_scenarios"
        if not details["scenarios"]:
            for basis in ("median", "latest"):
                for fallback_assumption in (request.main_rate, *request.sensitivity_rates):
                    details["scenarios"].append({"basis": basis,
                        "rate": str(fallback_assumption.value.value) if fallback_assumption else None,
                        "status": status.value, "growth": None, "terminal_pv_fraction": None,
                        "reason": errors[0] if errors else "unavailable"})
        return await self._save(owner, operation, "reverse_dcf_fcfe", METHOD_VERSION,
                                request.as_of, request.model_dump(mode="json"), tuple(facts),
                                assumptions, status, results, tuple(quality), tuple(dict.fromkeys(errors)), details)

    async def percentile(self, owner: OwnerId, request: PercentileRequest,
                         operation: UUID) -> dict[str, Any]:
        """Call the existing v1 factor with a strict capture-time view, never reimplement rank."""
        from investment_agent.evidence.snapshots import materialize
        await self.ensure_consistent_read()
        version = f"{request.metric}_percentile_5y.v1"
        replay = await self._replay(owner, operation, version, request)
        if replay is not None:
            return replay
        listing = await ListingRepository(self.session).get_a_share_listing(request.ticker)
        if listing is None:
            raise InvestorError("invalid_reference", "Stored canonical listing required")
        day = request.as_of.astimezone(SHANGHAI_TZ).date()
        start = years_before(day, 5)
        rows = await MarketSnapshotRepository(self.session).series_as_of(
            listing.id, start, day, request.as_of, provider="baostock",
            adjust="none", include_backfill=False)
        # A capture cannot make a same-day close visible before market close.
        rows = [r for r in rows if materialize(r, "close").available_from <= request.as_of]
        financial = list(await self.session.scalars(select(FinancialSnapshot).where(
            FinancialSnapshot.listing_id == listing.id,
            FinancialSnapshot.provider == "baostock",
            FinancialSnapshot.captured_at <= request.as_of,
            FinancialSnapshot.available_from <= day,
            FinancialSnapshot.availability != "unknown",
            FinancialSnapshot.report_period <= day,
        ).order_by(FinancialSnapshot.report_period, FinancialSnapshot.version)))
        latest_fin: dict[date, FinancialSnapshot] = {}
        for row in financial:
            latest_fin[row.report_period] = row
        overdue = await ReportOverdueRepository(self.session).open_as_of(listing.id, day)
        column = "pe_ttm" if request.metric == "pe" else "pb_mrq"
        # Decimal -> float here is deliberate and rank-only: the v1 percentile
        # factor (factors/valuation.py) only compares bar values (`value > 0`,
        # `value <= current`) and never does money arithmetic on them, and the
        # stored 2-4 digit fixed-point ratios map order-preserving into float.
        # The saved result is converted back through Decimal(str(...)) below.
        bars = [Bar(trade_date=r.trade_date, close=float(r.close),
                    pe_ttm=float(r.pe_ttm) if r.pe_ttm is not None else None,
                    pb_mrq=float(r.pb_mrq) if r.pb_mrq is not None else None,
                    ref=f"market_snapshot:{r.id}@{r.version}") for r in rows]
        periods = [FinancialPeriod(report_period=r.report_period, period_kind=r.period_kind,
                                   available_from=r.available_from, availability=r.availability,
                                   fields={}, ref=f"financial_snapshot:{r.id}@{r.version}")
                   for r in latest_fin.values()]
        view = SnapshotView.build(ticker=request.ticker, as_of=day, raw_bars=bars,
                                  periods=periods, overdue_periods=[r.report_period for r in overdue])
        factor = next(f for f in VALUATION_FACTORS if f.name.startswith(request.metric + "_"))
        value = factor.compute(view)
        flags = value.flags
        status = Status.OK if value.value is not None else Status.MISSING_INPUTS
        if "negative_earnings" in flags or "negative_book_value" in flags:
            status = Status.NOT_APPLICABLE
        if "report_overdue" in flags or "stale_price" in flags:
            status = Status.STALE_INPUTS
        results = {"percentile": Quantity(value=Decimal(str(value.value)), unit="fraction")} if value.value is not None else {}
        inputs = [{"ref": ObjectRef(kind="market_snapshot", id=r.id, revision=r.version,
                                    field_path=column).model_dump(mode="json"),
                   "trade_date": r.trade_date.isoformat(), "captured_at": str(r.captured_at),
                   "provider": r.provider, "adjust": r.adjust,
                   "value": str(getattr(r, column)) if getattr(r, column) is not None else None,
                   "close": str(r.close)} for r in rows]
        facts = []
        errors = list(flags)
        if rows:
            for field in (column, "close"):
                evidence = materialize(rows[-1], field)
                # A non-positive current PE is still a real input, but never a cheap percentile.
                if evidence.value is not None and evidence.value.value is not None:
                    if await self._source_conflict(evidence, request.as_of):
                        errors.append("cross_source_conflict")
                        status = Status.MISSING_INPUTS
                        continue
                    facts.append(await self.store.save_evidence(owner, evidence,
                        uuid5(operation, f"percentile-input:{field}")))
        details = {"inputs": inputs,
                   "financial_inputs": [{"ref": ObjectRef(kind="financial_snapshot", id=r.id,
                       revision=r.version).model_dump(mode="json"),
                       "report_period": r.report_period.isoformat(),
                       "available_from": r.available_from.isoformat() if r.available_from else None,
                       "captured_at": str(r.captured_at)} for r in latest_fin.values()],
                   "factor": {"name": value.name, "version": value.version,
                              "quality": value.quality, "flags": list(flags),
                              "inputs_ref": dict(value.inputs_ref)},
                   "policy": "baostock raw daily; captured and public <= as_of; no backfill exception",
                   "current_price": str(rows[-1].close) if rows else None,
                   "current_price_date": rows[-1].trade_date.isoformat() if rows else None,
                   "latest_report_period": max(latest_fin).isoformat() if latest_fin else None}
        details["report_context"] = {"overdue_periods": [r.report_period.isoformat() for r in overdue]}
        return await self._save(owner, operation, factor.name, f"{factor.name}.v{factor.version}",
                                request.as_of, request.model_dump(mode="json"), tuple(facts), (),
                                status, results, (value.quality, *errors), tuple(errors), details)

    async def ensure_consistent_read(self) -> None:
        """SQLite legacy SELECT does not start a physical transaction. Caller commits promptly.

        Use only for a short valuation operation, without collection/model calls. Do not
        change global SQLite transaction mode or commit any caller-owned work here.
        """
        connection = await self.session.connection()
        if connection.dialect.name == "sqlite":
            def in_transaction(conn: Any) -> bool:
                driver = conn.connection.driver_connection
                assert driver is not None
                return bool(driver.in_transaction)
            active = await connection.run_sync(in_transaction)
            if not active:
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                # Reserve writes before taking the valuation snapshot even when
                # a caller has already started a physical transaction.
                await connection.exec_driver_sql(
                    "UPDATE investor_calculation SET id=id WHERE 0")
