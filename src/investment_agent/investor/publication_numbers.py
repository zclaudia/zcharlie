"""Finite numeric checks and server-owned identity/unit formatting, not NLP."""
import re
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from investment_agent.evidence.contracts import (
    CalculationAssumption,
    CalculationRecord,
    EvidenceRecord,
    Period,
)
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import Contract, ObjectRef, Quantity
from investment_agent.investor.contracts import EntityNode, NumericNode
from investment_agent.investor.enums import NumericRole
from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.reference import Company, Listing

NUMBER = re.compile(r"[0-9０-９]|[零〇一二两三四五六七八九十百千万亿]+(?:成|倍|年|个月|天|元|股|万元|亿元|个百分点|%)")
MONEY = {"CNY": Decimal(1), "元": Decimal(1), "万元": Decimal(10000), "亿元": Decimal(100000000), "CNY_million": Decimal(1000000)}


class ResolvedNode(Contract):
    ref: ObjectRef
    role: str
    text: str
    subject_ref: ObjectRef
    period: Period | None = None
    value: str | None = None
    unit: str | None = None
    precision: int | None = None
    quality: tuple[str, ...] = ()


def format_quantity(quantity: Quantity, display: str) -> tuple[str, str, int, str]:
    value, unit = quantity.value, quantity.unit
    if value is None:
        raise InvestorError("invalid_reference", "Unknown quantity cannot be rendered")
    if quantity.currency not in {None, "CNY"}:
        raise InvestorError("invalid_reference", "Cross currency publication requires a verified conversion")
    if unit in MONEY:
        value *= MONEY[unit]
        unit = "CNY"
    if display == "percent":
        if unit == "fraction":
            value *= 100
        elif unit != "percent":
            raise InvestorError("invalid_reference", "Percent and percentage points are distinct")
        unit = "percent"
    elif display != "decimal" or unit in {"fraction", "percent"}:
        raise InvestorError("invalid_reference", "Quantity display format is incompatible")
    allowed = {"CNY", "CNY/share", "shares", "ratio", "percent", "percentage_points", "years", "days", "count"}
    if unit not in allowed:
        raise InvestorError("invalid_reference", "Unit has no deterministic display rule")
    precision = 0 if unit in {"shares", "years", "days", "count"} else 2
    rounded = value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_HALF_UP)
    return f"{rounded}{'%' if unit == 'percent' else ' ' + unit}", unit, precision, str(rounded)


class NumericResolver:
    def __init__(self, evidence: EvidenceService, subject: ObjectRef, as_of: datetime) -> None:
        self.evidence, self.subject, self.as_of = evidence, subject, as_of

    async def fact(self, ref: ObjectRef, *, require_verified: bool = False, require_fresh: bool = True) -> EvidenceRecord:
        if ref.field_path not in {None, "value", "text", "period.start", "period.end", "published_at", "acquired_at", "available_from"}:
            raise InvestorError("invalid_reference", "Evidence field path is not allowed")
        bare = ref.model_copy(update={"field_path": None})
        result = await self.evidence.get_evidence("local_owner", bare, self.as_of, subject_ref=self.subject)
        record = result.data
        if record is not None and record.document_provenance is not None and result.status == "conflict":
            raise InvestorError("invalid_reference", "Document fact has a known correction or conflict")
        if result.status != "ok" or record is None or record.verification == "user_report" or (require_verified and record.verification != "verified"):
            raise InvestorError("invalid_reference", "Fact is missing, conflicted or unverified")
        if record.document_provenance is not None:
            from investment_agent.evidence.documents import document_limits
            if await document_limits(self.evidence.session, record, self.as_of):
                raise InvestorError("invalid_reference", "Document fact has a known correction or conflict")
        rows = (await self.evidence.session.scalars(select(SavedEvidence).where(SavedEvidence.owner_id == "local_owner"))).all()
        for row in rows:
            newer = EvidenceRecord.model_validate(row.payload)
            if max(newer.published_at, newer.acquired_at, newer.available_from, newer.created_at) <= self.as_of and newer.subject_ref == self.subject and any(r.kind == "evidence" and r.id == ref.id and r.revision == ref.revision for r in newer.correction_refs):
                raise InvestorError("invalid_reference", "Fact was corrected by a visible newer record")
        if require_fresh and record.source_ref.kind == "market_snapshot" and self.as_of - record.available_from > timedelta(days=14):
            raise InvestorError("invalid_reference", "Market fact is stale")
        if record.source_ref.kind in {"market_snapshot", "financial_snapshot"}:
            from investment_agent.valuation.service import ValuationService
            if await ValuationService(self.evidence.session)._source_conflict(record, self.as_of):
                raise InvestorError("invalid_reference", "Fact has unresolved cross-source conflict")
        return record

    async def calculation(self, ref: ObjectRef, *, require_fresh: bool = True) -> CalculationRecord:
        result = await self.evidence.get_calculation("local_owner", ref.model_copy(update={"field_path": None}), self.as_of)
        record = result.data
        if result.status != "ok" or record is None or not record.applicable or any(error not in {"short_history", "approx", "full"} for error in record.errors) or any(flag in {"stale_price", "report_overdue", "unresolved_conflict", "unverified_fcfe", "missing_inputs"} for flag in record.quality):
            raise InvestorError("invalid_reference", "Calculation status or input quality is not publishable")
        if not record.fact_refs:
            raise InvestorError("invalid_reference", "Company calculation requires bound facts")
        for fact in record.fact_refs:
            await self.fact(fact, require_fresh=require_fresh)
        return record

    async def resolve(self, node: NumericNode | EntityNode) -> ResolvedNode:
        ref = node.ref
        if isinstance(node, EntityNode) or node.role == NumericRole.CODE:
            if ref.kind != "listing" or ref != self.subject:
                raise InvestorError("invalid_reference", "Identity must reference the exact company listing")
            listing = await self.evidence.session.get(Listing, ref.id)
            if listing is None:
                raise InvestorError("invalid_reference", "Listing unavailable")
            company = await self.evidence.session.get(Company, listing.company_id)
            assert company is not None
            if isinstance(node, NumericNode) and node.display_format != "code":
                raise InvestorError("invalid_reference", "Code requires code display")
            return ResolvedNode(ref=ref, role="identity", text=f"{company.canonical_name} ({listing.ticker})", subject_ref=self.subject)
        record: EvidenceRecord | CalculationRecord
        period = None
        quantity = None
        quality: tuple[str, ...] = ()
        if ref.kind == "evidence":
            record = await self.fact(ref)
            period = record.period
            quality = (f"verification={record.verification}",)
            if node.role == NumericRole.FACT and ref.field_path == "value":
                quantity = record.value
            elif node.role == NumericRole.DATE and node.display_format == "date":
                if ref.field_path in {"period.start", "period.end"} and period is not None:
                    value = getattr(period, ref.field_path.split(".")[1]).isoformat()
                elif ref.field_path in {"published_at", "available_from", "acquired_at"}:
                    value = getattr(record, ref.field_path).isoformat()
                else:
                    raise InvestorError("invalid_reference", "Date field is not allowed")
                return ResolvedNode(ref=ref, role=node.role.value, text=value, subject_ref=self.subject, period=period)
        elif ref.kind == "calculation":
            record = await self.calculation(ref)
            quality = record.quality
            path = ref.field_path or ""
            if node.role == NumericRole.OUTPUT and path.startswith("results."):
                quantity = record.results.get(path[8:])
            elif node.role in {NumericRole.SCENARIO, NumericRole.THRESHOLD} and path.startswith("assumptions."):
                assumption = resolve_assumption(record, path)
                if assumption is not None and assumption.rationale:
                    quantity = assumption.value
            elif node.role == NumericRole.DATE and path == "as_of" and node.display_format == "date":
                return ResolvedNode(ref=ref, role=node.role.value, text=record.as_of.isoformat(), subject_ref=self.subject)
        if quantity is None:
            raise InvestorError("invalid_reference", "Numeric role and field path do not match")
        text, unit, precision, value = format_quantity(quantity, node.display_format)
        if node.role in {NumericRole.SCENARIO, NumericRole.THRESHOLD}:
            text = ("假设：" if node.role == NumericRole.SCENARIO else "条件：") + text
        return ResolvedNode(ref=ref, role=node.role.value, text=text, subject_ref=self.subject, period=period, value=value, unit=unit, precision=precision, quality=quality)


def resolve_assumption(record: CalculationRecord, path: str) -> CalculationAssumption | None:
    key = path.removeprefix("assumptions.")
    if key.isascii() and key.isdecimal():
        index = int(key)
        return record.assumptions[index] if index < len(record.assumptions) else None
    matches = [assumption for assumption in record.assumptions if assumption.name == key]
    if len(matches) > 1:
        raise InvestorError("invalid_reference", "Ambiguous assumption name; reference its saved index")
    return matches[0] if matches else None
